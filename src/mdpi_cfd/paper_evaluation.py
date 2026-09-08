from __future__ import annotations

import json
import os
from pathlib import Path

from .config import ResolvedConfig
from .errors import MdpiCfdError
from .postprocess import PAPER_FIGURE_RANGES, _ScientificBackend


PAPER_COARSE_FINE_TMAX_DIFFERENCE_K = 0.016
PAPER_HOTTEST_COOLEST_CELL_DIFFERENCE_K = 0.147


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _json(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise MdpiCfdError(f"required final result is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _relative_difference(actual: float, reference: float) -> float:
    return (actual - reference) / reference


def evaluate_paper_alignment(
    config: ResolvedConfig, case_dir: str | Path
) -> dict[str, object]:
    case = Path(case_dir).resolve()
    mesh = _json(case / "results/mesh_independence.json")
    animation = _json(case / "results/animation_validation.json")
    if mesh.get("status") != "MESH_INDEPENDENCE_PASSED":
        raise MdpiCfdError("mesh independence must pass before paper evaluation")
    if animation.get("status") != "ANIMATION_VALIDATION_PASSED":
        raise MdpiCfdError("animation validation must pass before paper evaluation")

    foam = case / "production/L0/runs/frozen_flow/frozen_flow.foam"
    backend = _ScientificBackend(foam)
    np = backend.np
    fluid, solid = backend.regions(1800.0)
    fluid_temperature = backend.vtk_to_numpy(fluid.GetCellData().GetArray("T")) - 273.15
    solid_temperature = backend.vtk_to_numpy(solid.GetCellData().GetArray("T")) - 273.15
    velocity = backend.vtk_to_numpy(fluid.GetCellData().GetArray("U"))
    global_umax = float(np.linalg.norm(velocity, axis=1).max())
    z_mid = 0.5 * (fluid.GetBounds()[4] + fluid.GetBounds()[5])
    y_mid = 0.5 * (fluid.GetBounds()[2] + fluid.GetBounds()[3])
    xy_speed = backend._triangulated_cut(
        fluid, (0, 0, z_mid), (0, 0, 1), "Umag"
    )[2]
    xz_speed = backend._triangulated_cut(
        fluid, (0, y_mid, 0), (0, 1, 0), "Umag"
    )[2]

    levels = mesh["levels"]
    l0_l3_tmax = abs(
        float(levels["L3"]["battery_tmax_k"])
        - float(levels["L0"]["battery_tmax_k"])
    )
    l3_cells = levels["L3"]["cell_average_temperature_k"]
    cell_spread = max(float(value) for value in l3_cells.values()) - min(
        float(value) for value in l3_cells.values()
    )
    paper_temperature = PAPER_FIGURE_RANGES["figure_08_temperature_degC"]
    actual_solid_range = [float(solid_temperature.min()), float(solid_temperature.max())]
    baseline_c = config.boundary.initial_solid_temperature_k - 273.15
    actual_rise_range = [value - baseline_c for value in actual_solid_range]
    paper_rise_range = [value - 20.0 for value in paper_temperature]
    actual = {
        "reynolds_table4": config.derived.reynolds_table4,
        "l0_l3_steady_tmax_difference_k": l0_l3_tmax,
        "l3_hottest_coolest_cell_average_difference_k": cell_spread,
        "l0_1800s_fluid_temperature_range_degC": [
            float(fluid_temperature.min()),
            float(fluid_temperature.max()),
        ],
        "l0_1800s_solid_temperature_range_degC": actual_solid_range,
        "l0_1800s_solid_temperature_rise_range_k": actual_rise_range,
        "l0_1800s_global_velocity_max_m_s": global_umax,
        "l0_1800s_xy_velocity_p99p5_m_s": float(np.percentile(xy_speed, 99.5)),
        "l0_1800s_xy_velocity_max_m_s": float(xy_speed.max()),
        "l0_1800s_xz_velocity_p99p5_m_s": float(np.percentile(xz_speed, 99.5)),
        "l0_1800s_xz_velocity_max_m_s": float(xz_speed.max()),
    }
    references = {
        "paper_reynolds": config.derived.paper_reynolds,
        "figure_03_coarse_fine_tmax_difference_k": PAPER_COARSE_FINE_TMAX_DIFFERENCE_K,
        "figure_08_09_13_14_temperature_range_degC": paper_temperature,
        "figure_08_09_13_14_temperature_rise_range_k_from_20C": paper_rise_range,
        "hottest_coolest_cell_difference_k": PAPER_HOTTEST_COOLEST_CELL_DIFFERENCE_K,
        "figure_10_velocity_max_m_s": PAPER_FIGURE_RANGES["figure_10_velocity_m_s"][1],
        "figure_11_xy_velocity_upper_m_s": PAPER_FIGURE_RANGES["figure_11_velocity_m_s"][1],
        "figure_12_xz_velocity_upper_m_s": PAPER_FIGURE_RANGES["figure_12_velocity_m_s"][1],
    }
    comparisons = {
        "reynolds_relative_difference": _relative_difference(
            actual["reynolds_table4"], references["paper_reynolds"]
        ),
        "coarse_fine_tmax_difference_from_paper_k": (
            l0_l3_tmax - PAPER_COARSE_FINE_TMAX_DIFFERENCE_K
        ),
        "cell_spread_relative_difference": _relative_difference(
            cell_spread, PAPER_HOTTEST_COOLEST_CELL_DIFFERENCE_K
        ),
        "solid_temperature_max_absolute_offset_from_paper_k": (
            actual_solid_range[1] - paper_temperature[1]
        ),
        "solid_temperature_rise_max_difference_k": (
            actual_rise_range[1] - paper_rise_range[1]
        ),
        "figure_10_umax_relative_difference": _relative_difference(
            global_umax, references["figure_10_velocity_max_m_s"]
        ),
        "figure_11_xy_p99p5_relative_difference": _relative_difference(
            actual["l0_1800s_xy_velocity_p99p5_m_s"],
            references["figure_11_xy_velocity_upper_m_s"],
        ),
        "figure_12_xz_p99p5_relative_difference": _relative_difference(
            actual["l0_1800s_xz_velocity_p99p5_m_s"],
            references["figure_12_xz_velocity_upper_m_s"],
        ),
    }
    figure_reviews = {
        "Figure 3": "actual L0/L3 steady XZ fields use one plane, camera and shared scale; four-grid trends included",
        "Figures 4, 6, 7": "style adaptations using actual baseline fields; not claimed as the paper's external experiment",
        "Figures 8, 9, 13, 14": "temperature-rise scale is comparable, but absolute colourbar is offset by the 293 K baseline versus the paper figure's approximately 293.15 K datum",
        "Figure 10": "global velocity maximum is close to the digitized paper reference",
        "Figure 11": "XY p99.5 is lower than the paper upper reference while the local maximum straddles it; distribution is not an exact match",
        "Figure 12": "XZ section velocity remains lower than the digitized paper reference",
        "Animations": "actual T(x,t) changes from 0 to 1800 s; moving comet heads traverse inlet-fed streamlines in fixed U(x) and are explicitly not transient velocity",
    }
    limitations = [
        "Port y locations and inlet-column heat assignment retain ASSUMPTION_A04.",
        "The unpublished Fluent dynamic heat-source UDF, I(t), R(t), and interpolation rules are unavailable; constant qvol is used.",
        "The thermal movie contains actual transient T(x,t), but U(x) is the validated frozen flow field rather than U(x,t).",
        "Paper colourbar endpoints are digitized references, not strict golden values, and are never forced onto the computed fields.",
        "Publication authorization remains false despite numerical mesh-independence PASS because the source assumptions above remain unresolved.",
    ]
    report = {
        "schema_version": 1,
        "status": "PAPER_ALIGNMENT_EVALUATED_WITH_LIMITATIONS",
        "publication_results_authorized": False,
        "mesh_independence_status": mesh["status"],
        "animation_validation_status": animation["status"],
        "actual": actual,
        "paper_digitized_references": references,
        "comparisons": comparisons,
        "figure_reviews": figure_reviews,
        "limitations": limitations,
        "overall_assessment": (
            "Composition and qualitative thermal/flow patterns are reproduced from actual fields. "
            "Temperature rise and global Umax are broadly comparable; absolute temperature datum "
            "and sectional velocity ranges are not exact paper matches."
        ),
    }
    json_path = case / "results/paper_comparison_evaluation.json"
    _atomic_write(
        json_path,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    lines = [
        "# 최종 CFD–논문 비교 평가",
        "",
        f"- 판정: `{report['status']}`",
        f"- 격자 독립성: `{mesh['status']}`",
        f"- 애니메이션: `{animation['status']}`",
        "- 논문 최종값 사용 승인: `false` (아래 공개 가정·제약 때문)",
        "",
        "## 핵심 정량 비교",
        "",
        "| 항목 | 계산 | 논문 참고 | 평가 |",
        "|---|---:|---:|---|",
        f"| Reynolds 수 | {actual['reynolds_table4']:.1f} | {references['paper_reynolds']:.0f} | Table 4 물성 기준 불일치 {100*comparisons['reynolds_relative_difference']:.1f}% |",
        f"| L0–L3 정상 Tmax 차이 | {l0_l3_tmax:.5f} K | 약 0.016 K | 차이 {comparisons['coarse_fine_tmax_difference_from_paper_k']:+.5f} K |",
        f"| L3 최고–최저 셀 평균온도 | {cell_spread:.5f} K | 약 0.147 K | {100*comparisons['cell_spread_relative_difference']:+.1f}% |",
        f"| L0 1800 s 고체 온도 범위 | {actual_solid_range[0]:.5f}–{actual_solid_range[1]:.5f} °C | {paper_temperature[0]:.3f}–{paper_temperature[1]:.3f} °C | 초기온도 datum 차이 포함 |",
        f"| L0 1800 s 고체 온도상승 최대 | {actual_rise_range[1]:.5f} K | 약 {paper_rise_range[1]:.3f} K | {comparisons['solid_temperature_rise_max_difference_k']:+.5f} K |",
        f"| Figure 10 전역 Umax | {global_umax:.5f} m/s | 약 {references['figure_10_velocity_max_m_s']:.3f} m/s | {100*comparisons['figure_10_umax_relative_difference']:+.1f}% |",
        f"| Figure 11 XY U 99.5% | {actual['l0_1800s_xy_velocity_p99p5_m_s']:.5f} m/s | 약 {references['figure_11_xy_velocity_upper_m_s']:.3f} m/s | {100*comparisons['figure_11_xy_p99p5_relative_difference']:+.1f}% |",
        f"| Figure 12 XZ U 99.5% | {actual['l0_1800s_xz_velocity_p99p5_m_s']:.5f} m/s | 약 {references['figure_12_xz_velocity_upper_m_s']:.3f} m/s | {100*comparisons['figure_12_xz_p99p5_relative_difference']:+.1f}% |",
        "",
        "## 그림·애니메이션 평가",
        "",
    ]
    lines.extend(f"- {name}: {value}" for name, value in figure_reviews.items())
    lines.extend(["", "## 남은 제약", ""])
    lines.extend(f"- {item}" for item in limitations)
    lines.extend(["", "## 종합", "", report["overall_assessment"], ""])
    _atomic_write(case / "results/paper_comparison_evaluation.md", "\n".join(lines))
    return report
