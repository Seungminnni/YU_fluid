"""실제 CFD 결과와 검증 상태를 휴대형 HTML/CSS/JavaScript 뷰어로 만든다.

표준/정밀 메시를 서로 대체하지 않고 각 데이터셋을 독립적으로 표시한다.
온도 색, 단면, 유선, KPI와 수렴 그래프는 OpenFOAM 후처리값을 사용하며
브라우저에서 새로운 물리 계산을 수행하지 않는다.
"""

from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import json
import math
import os
import re
import shutil
from pathlib import Path

from .config import ResolvedConfig
from .geometry import cell_placements, geometry_report
from .heat_source import source_from_config
from .mesh_wireframe import mesh_wireframes_for_viewer
from .mesh_viewer import (
    HOME_HTML,
    HTML as MESH_HTML,
    CSS as MESH_CSS,
    JAVASCRIPT as MESH_JAVASCRIPT,
    OFFLINE_JAVASCRIPT,
    enable_multi_case_mesh_selection,
    write_mesh_only_viewer,
)
from .openfoam_cold_flow_validation import parse_cold_flow_log
from .openfoam_results import (
    LEGACY_DISPLAY_TEMPERATURE_OFFSET_C,
    REFERENCE_FIGURE_RANGES,
    OpenFoamResultReader,
    read_openfoam_dat,
    read_openfoam_dat_series,
)
from .production_mesh import SINGLE_MESH_TARGET_COUNTS
from .result_contract import figure_catalog


CURRENT_PRECISION_MESH = {
    "total_cells": 1_316_854,
    "fluid_cells": 1_061_438,
    "solid_cells": 255_416,
    "points": 1_403_119,
    "hexahedra": 1_245_779,
    "hexahedron_fraction": 1_245_779 / 1_316_854,
}


def _energy_balance_payload(
    case: Path,
    *,
    transient_run_dir: Path | None = None,
) -> dict[str, object]:
    """Read the persisted transient heat balance without inventing samples."""

    source = case / "results/energy_balance.csv"
    if transient_run_dir is not None:
        source = transient_run_dir / "reports/solver/energy_balance.csv"
    if not source.is_file():
        source = case / "runs/frozen_flow_actual_1s/reports/solver/energy_balance.csv"
    if not source.is_file():
        return {"available": False}

    rows: list[dict[str, float]] = []
    with source.open(encoding="utf-8", newline="") as stream:
        for raw in csv.DictReader(stream):
            row = {
                "time_s": float(raw["time_s"]),
                "generated_W": float(raw["generated_W"]),
                "removed_W": float(raw["removed_W"]),
                "stored_W": float(raw["stored_W"]),
                "imbalance_percent": 100.0 * float(raw["imbalance_fraction"]),
            }
            if not all(math.isfinite(value) for value in row.values()):
                raise ValueError(f"{source}: non-finite energy-balance value")
            rows.append(row)
    if not rows:
        return {"available": False}
    if any(right["time_s"] <= left["time_s"] for left, right in zip(rows, rows[1:])):
        raise ValueError(f"{source}: time_s must be strictly increasing")

    final = rows[-1]
    return {
        "available": True,
        "source_file": str(source.relative_to(case)),
        "times_s": [row["time_s"] for row in rows],
        "generated_w": [row["generated_W"] for row in rows],
        "removed_w": [row["removed_W"] for row in rows],
        "stored_w": [row["stored_W"] for row in rows],
        "imbalance_percent": [row["imbalance_percent"] for row in rows],
        "sample_count": len(rows),
        "final": {
            "time_s": final["time_s"],
            "generated_w": final["generated_W"],
            "removed_w": final["removed_W"],
            "stored_w": final["stored_W"],
            "imbalance_percent": final["imbalance_percent"],
        },
        "balance_equation": "generated_W = removed_W + stored_W + imbalance",
    }


def _mesh_runs_payload(
    case: Path, *, precision_data_ready: bool, standard_data_ready: bool = False
) -> list[dict[str, object]]:
    """서로 다른 메시의 필드를 바꿔치기하지 않고 선택 가능한 결과를 설명한다."""

    precision = dict(CURRENT_PRECISION_MESH)
    precision_target_report = (
        case / "production/L3/reports/production_mesh/mesh_target_composition.json"
    )
    if not precision_target_report.is_file():
        precision_target_report = case / "reports/production_mesh/mesh_target_composition.json"
    if precision_target_report.is_file():
        target = json.loads(precision_target_report.read_text(encoding="utf-8"))
        actual = target.get("actual", {})
        for name in (
            "total_cells",
            "fluid_cells",
            "solid_cells",
            "points",
            "hexahedron_fraction",
        ):
            if name in actual:
                precision[name] = actual[name]
        if "cell_types" in actual:
            precision["cell_types"] = actual["cell_types"]
            precision["hexahedra"] = int(actual["cell_types"].get("hexahedra", 0))
    precision_zone_report = (
        case / "production/L3/reports/production_mesh/cellZones_pre_split.json"
    )
    if not precision_zone_report.is_file():
        precision_zone_report = case / "reports/production_mesh/cellZones_pre_split.json"
    if precision_zone_report.is_file():
        zone_report = json.loads(precision_zone_report.read_text(encoding="utf-8"))
        zone_counts = zone_report.get("zone_cell_counts", {})
        precision["total_cells"] = int(zone_report["mesh_cell_count"])
        precision["fluid_cells"] = int(zone_counts.get("coolant", 0))
        precision["solid_cells"] = sum(
            int(value)
            for name, value in zone_counts.items()
            if str(name).startswith("battery_C")
        )
    precision_kind = (
        "80만급 생산 결과"
        if 700_000 <= int(precision["total_cells"]) < 1_000_000
        else "정밀 결과"
    )

    target_report_path = (
        case / "production/single/reports/production_mesh/mesh_target_composition.json"
    )
    target_report: dict[str, object] = {}
    if target_report_path.is_file():
        target_report = json.loads(target_report_path.read_text(encoding="utf-8"))
    target_actual = target_report.get("actual", {})
    mesh_ready = target_report.get("status") == "MESH_TARGET_VALIDATION_PASSED"

    runs = [
        {
            "id": "precision",
            "label": f"{precision_kind} · {int(precision['total_cells']):,}셀",
            "label_en": (
                f"{'800k-class Production Result' if precision_kind == '80만급 생산 결과' else 'Precision Result'}"
                f" · {int(precision['total_cells']):,} cells"
            ),
            "state_label": "계산 완료",
            "state_label_en": "Calculation Complete",
            "mesh_ready": True,
            "data_ready": precision_data_ready,
            "counts": precision,
            "element_family": (
                "hexa-dominant body-fitted "
                f"({100 * float(precision['hexahedron_fraction']):.3f}% hexahedra)"
            ),
            "note": f"0–1800 s 직접 결과가 연결된 현재 {precision_kind}입니다.",
            "note_en": (
                "This is the current result with direct 0–1800 s data from its own mesh."
            ),
        },
        {
            "id": "single",
            "label": (
                f"표준 계산 · {int(target_actual['total_cells']):,}셀"
                if mesh_ready and target_actual
                else "표준 계산 · 목표 207,390셀"
            ),
            "label_en": (
                f"Standard Calculation · {int(target_actual['total_cells']):,} cells"
                if mesh_ready and target_actual
                else "Standard Calculation · target 207,390 cells"
            ),
            "state_label": (
                "계산 완료"
                if standard_data_ready
                else ("메시 검증 완료" if mesh_ready else "계산 전")
            ),
            "state_label_en": (
                "Calculation Complete"
                if standard_data_ready
                else ("Mesh Validation Complete" if mesh_ready else "Not Calculated")
            ),
            "mesh_ready": mesh_ready,
            "data_ready": standard_data_ready,
            "counts": target_actual or {
                "total_cells": SINGLE_MESH_TARGET_COUNTS["total_cells"],
                "fluid_cells": SINGLE_MESH_TARGET_COUNTS["fluid_cells"],
                "solid_cells": SINGLE_MESH_TARGET_COUNTS["solid_cells"],
                "points": SINGLE_MESH_TARGET_COUNTS["points"],
            },
            "counts_are_targets": not mesh_ready,
            "element_family": (
                f"hexa-dominant body-fitted ({100 * float(target_actual['hexahedron_fraction']):.3f}% hexahedra)"
                if mesh_ready and target_actual
                else "hexa-dominant target (≥80% hexahedra)"
            ),
            "note": (
                "0–1800 s 직접 결과가 연결된 표준 메시입니다."
                if standard_data_ready
                else "새 단일 계산 프로파일입니다. 자체 CFD 필드가 생성되기 전에는 "
                "정밀 결과를 대신 표시하지 않습니다."
            ),
            "note_en": (
                "Direct 0–1800 s results are linked to this standard mesh."
                if standard_data_ready
                else "This mesh remains selectable, but it never borrows CFD fields from another calculation."
            ),
        },
    ]
    runtime_path = case / "reports/execution_runtime.json"
    if runtime_path.is_file():
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        analysis_seconds = runtime.get("analysis_wall_seconds")
        stages = runtime.get("stage_wall_seconds", {})
        if isinstance(analysis_seconds, (int, float)) and analysis_seconds >= 0:
            runs[0]["analysis_runtime"] = {
                "wall_seconds": float(analysis_seconds),
                "stage_wall_seconds": {
                    str(name): float(value)
                    for name, value in stages.items()
                    if isinstance(value, (int, float)) and value >= 0
                    and name in {"cad", "mesh", "steady", "transient"}
                },
                "solver_mpi_ranks": runtime.get("solver_mpi_ranks"),
                "mesh_mpi_ranks": runtime.get("mesh_mpi_ranks"),
                "semantics": runtime.get("semantics"),
            }
    comparison_path = case / "comparison_manifest.json"
    if comparison_path.is_file():
        comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
        definitions = comparison.get("runs", [])
        by_id = {
            str(item.get("id")): item
            for item in definitions
            if isinstance(item, dict) and item.get("id") in {"precision", "single"}
        }
        for run in runs:
            metadata = by_id.get(str(run["id"]), {})
            for key in (
                "label",
                "label_en",
                "state_label",
                "state_label_en",
                "note",
                "note_en",
                "inlet_diameter_mm",
                "outlet_diameter_mm",
                "comparison_key",
            ):
                if key in metadata:
                    run[key] = metadata[key]
    return runs


def _convergence_payload(case: Path) -> dict[str, object]:
    """OpenFOAM 로그의 raw initial residual을 수렴 그래프용으로 추출한다."""

    transient_candidates = (
        case / "runs/frozen_flow_actual_1s/reports/solver/transient_residuals.csv",
        case
        / "production/L3/runs/frozen_flow_actual_1s_xy5/reports/solver/transient_residuals.csv",
        case
        / "production/L3/runs/frozen_flow_actual_1s/reports/solver/transient_residuals.csv",
        case
        / "production/single/runs/frozen_flow_actual_1s/reports/solver/transient_residuals.csv",
        case
        / "production/single/runs/full_transient_actual_1s/reports/solver/transient_residuals.csv",
        case
        / "production/L3/runs/full_transient_actual_1s/reports/solver/transient_residuals.csv",
    )
    transient_residual_path = next(
        (candidate for candidate in transient_candidates if candidate.is_file()), None
    )
    run_candidates = (
        case / "runs/steady_cht",
        case / "production/single/runs/steady_cht",
        case / "production/L3/runs/steady_cht",
        case / "production/L0/runs/steady_cht",
        # Failed cold-flow attempts are never published as selectable CFD
        # results, but their preserved solver logs can still be rendered as a
        # clearly labelled diagnostic convergence history.
        case / "runs/steady_cold_flow",
        case / "production/single/runs/steady_cold_flow",
        case / "production/L3/runs/steady_cold_flow",
        case / "production/L0/runs/steady_cold_flow",
    )
    run_dir = next(
        (
            candidate
            for candidate in run_candidates
            if (candidate / "logs/solver").is_dir()
        ),
        None,
    )
    grouped: dict[str, dict[int, float]] = {}
    continuity: dict[int, float] = {}
    linear_iteration_total = 0
    source_logs: list[str] = []
    validation: dict[str, object] = {}
    axis_mode = "openfoam_outer_iteration"
    scope = "현재 정밀 결과 · 정상상태 CHT 초기화"
    transient_summary: dict[str, object] = {}
    if transient_residual_path is not None:
        with transient_residual_path.open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                iteration = int(row["global_iteration"])
                qualified = f'{row["region"]}.{row["field"]}'
                value = float(row["initial_residual"])
                grouped.setdefault(qualified, {})[iteration] = max(
                    value, grouped.get(qualified, {}).get(iteration, 0.0)
                )
                linear_iteration_total += int(row.get("linear_iterations") or 0)
        source_logs.append(str(transient_residual_path.relative_to(case)))
        summary_path = transient_residual_path.with_name(
            "transient_residuals_summary.json"
        )
        if summary_path.is_file():
            transient_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            source_logs.append(str(summary_path.relative_to(case)))
        axis_mode = (
            "transient_time_step_iteration"
            if "frozen_flow_actual_1s" in transient_residual_path.parts
            else "cumulative_pimple_outer_iteration"
        )
        result_kind = (
            "새 표준 결과"
            if "single" in transient_residual_path.parts
            else "80만급/정밀 결과"
        )
        calculation_kind = (
            "고정 유동 과도 에너지 계산"
            if "frozen_flow_actual_1s" in transient_residual_path.parts
            else "전체 과도 CHT 계산"
        )
        scope = f"{result_kind} · {calculation_kind}"
    elif run_dir is not None:
        cold_flow_only = run_dir.name == "steady_cold_flow"
        if cold_flow_only:
            scope = "실패 진단 · 유동 초기화"
        elif "single" in run_dir.parts:
            scope = "새 표준 결과 · 정상상태 CHT 초기화"
        log_prefix = "steady_cold_flow" if cold_flow_only else "steady_cht"
        for log_path in sorted((run_dir / "logs/solver").glob(f"{log_prefix}*.log")):
            parsed = parse_cold_flow_log(log_path)
            if not parsed["records"]:
                continue
            source_logs.append(str(log_path.relative_to(case)))
            for iteration_text, record in parsed["records"].items():
                iteration = int(round(float(iteration_text)))
                for qualified, values in record["residuals"].items():
                    if values:
                        linear_iteration_total += sum(
                            int(value.get("linear_iterations", 0)) for value in values
                        )
                        grouped.setdefault(qualified, {})[iteration] = max(
                            float(value["initial"]) for value in values
                        )
                continuity_values = record.get("continuity")
                if continuity_values is not None:
                    value = abs(float(continuity_values["global"]))
                    if value > 0:
                        continuity[iteration] = value
        validation_name = (
            "cold_flow_validation.json"
            if cold_flow_only
            else "steady_cht_validation.json"
        )
        validation_path = run_dir / f"reports/solver/{validation_name}"
        if validation_path.is_file():
            validation = json.loads(validation_path.read_text(encoding="utf-8"))

    # A postprocessed case can retain residuals.csv after solver logs have been
    # archived. This fallback never invents points or interpolates iterations.
    if not grouped:
        residual_path = case / "results/residuals.csv"
        if residual_path.is_file():
            with residual_path.open(encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream):
                    qualified = f'{row["region"]}.{row["field"]}'
                    iteration = int(round(float(row["iteration"])))
                    value = float(row["initial_residual"])
                    grouped.setdefault(qualified, {})[iteration] = max(
                        value, grouped.get(qualified, {}).get(iteration, 0.0)
                    )
            source_logs.append(str(residual_path.relative_to(case)))
    if not grouped:
        return {"available": False}

    labels = {
        "continuity.global": "질량보존 |global|",
        "coolant.p_rgh": "압력 p_rgh",
        "coolant.Ux": "x-velocity",
        "coolant.Uy": "y-velocity",
        "coolant.Uz": "z-velocity",
        "coolant.h": "유체 energy(h)",
        "batterySolid.h": "고체 energy(h)",
        "coolant.k": "k",
        "coolant.omega": "omega",
    }
    colours = {
        # Print-safe saturated traces on a white graph. No white/yellow line
        # may disappear against the exported PNG background.
        "continuity.global": "#d81b60",
        "coolant.p_rgh": "#1565c0",
        "coolant.Ux": "#c62828",
        "coolant.Uy": "#2e7d32",
        "coolant.Uz": "#7b1fa2",
        "coolant.h": "#ef6c00",
        "batterySolid.h": "#8d6e00",
        "coolant.k": "#00838f",
        "coolant.omega": "#283593",
    }
    if continuity:
        grouped["continuity.global"] = continuity
    floors = validation.get("residual_floors", {})
    series = []
    all_iterations: set[int] = set()
    for key in labels:
        points = sorted(grouped.get(key, {}).items())
        if not points:
            continue
        stride = max(1, math.ceil(len(points) / 1200))
        sampled = [
            [iteration, value]
            for index, (iteration, value) in enumerate(points)
            if index % stride == 0 or index == len(points) - 1
        ]
        all_iterations.update(iteration for iteration, _ in points)
        series.append(
            {
                "key": key,
                "label": labels[key],
                "colour": colours[key],
                "points": sampled,
                "raw_point_count": len(points),
                "final": points[-1][1],
                "configured_gate": floors.get(key),
            }
        )
    final_checks = validation.get("residual_checks", {})
    primary_id = (
        "transient_energy"
        if axis_mode == "transient_time_step_iteration"
        else "full_transient"
        if axis_mode == "cumulative_pimple_outer_iteration"
        else "cold_flow"
        if run_dir is not None and run_dir.name == "steady_cold_flow"
        else "steady_cht"
    )
    primary_label = (
        "1초 과도 · 에너지"
        if primary_id == "transient_energy"
        else "전체 과도 CHT"
        if primary_id == "full_transient"
        else "유동 초기화 · U/p/k/ω"
        if primary_id == "cold_flow"
        else "정상 CHT · 유동+에너지"
    )
    payload = {
        "available": bool(series),
        "id": primary_id,
        "label": primary_label,
        "source": "actual OpenFOAM solver log",
        "scope": scope,
        "source_files": source_logs,
        "series": series,
        "axis_mode": axis_mode,
        "iteration_count": len(all_iterations),
        # A solver can retain a residual CSV containing only auxiliary fields
        # that are intentionally not plotted.  Keep the saved steady-stage
        # histories available instead of crashing publication on min(empty).
        "first_iteration": min(all_iterations) if all_iterations else None,
        "last_iteration": max(all_iterations) if all_iterations else None,
        "linear_iteration_total": linear_iteration_total or None,
        "time_step_count": transient_summary.get("time_step_count"),
        "first_time_s": transient_summary.get("first_time_s"),
        "last_time_s": transient_summary.get("last_time_s"),
        "validation_status": validation.get("status", "SAVED_HISTORY"),
        "configured_residual_checks_passed": (
            bool(final_checks) and all(bool(value) for value in final_checks.values())
        ),
        "available_during_calculation": True,
        "live_note": (
            "During a new run these values are emitted on every solver iteration; "
            "the current static viewer contains the saved completed history."
        ),
        "iteration_axis_note": (
            "The x-axis is the cumulative PIMPLE outer-correction number, matching "
            "the transient iteration structure of the ANSYS reference. Residual "
            "values remain raw OpenFOAM residuals, not Fluent scaled residuals."
            if axis_mode == "cumulative_pimple_outer_iteration"
            else "The x-axis is the one-second transient energy-solve index. The "
            "validated velocity/pressure solution was completed separately and held "
            "fixed because this constant-property, zero-gravity model is one-way CHT."
            if axis_mode == "transient_time_step_iteration"
            else "The completed current result uses the OpenFOAM steady outer-"
            "iteration number. A new full-transient result will use the cumulative "
            "PIMPLE outer-correction axis."
        ),
        "semantics": (
            "Curves use maximum initial linear-solver residual per recorded iteration. "
            "OpenFOAM global continuity error is plotted separately and is not "
            "relabeled as a normalized Fluent continuity residual."
        ),
    }

    def _steady_history(
        *, run_name: str, history_id: str, label: str, scope_text: str,
        validation_name: str,
        included_keys: tuple[str, ...] | None = None,
    ) -> dict[str, object] | None:
        # The active production result may live under L3 (the adopted 800k
        # profile) rather than the historical ``single`` profile.  Search the
        # executed precision path first so saved cold-flow/steady-CHT logs are
        # not accidentally hidden from the UI.
        run_path = next(
            (
                candidate
                for profile in (".", "L3", "single", "L0")
                if (
                    candidate := (
                        case / f"runs/{run_name}"
                        if profile == "."
                        else case / f"production/{profile}/runs/{run_name}"
                    )
                ).joinpath(f"logs/solver/{run_name}.log").is_file()
            ),
            None,
        )
        if run_path is None:
            return None
        log_path = run_path / f"logs/solver/{run_name}.log"
        parsed = parse_cold_flow_log(log_path)
        if not parsed["records"]:
            return None

        history_grouped: dict[str, dict[int, float]] = {}
        history_continuity: dict[int, float] = {}
        history_linear_total = 0
        for iteration_text, record in parsed["records"].items():
            iteration = int(round(float(iteration_text)))
            for qualified, values in record["residuals"].items():
                if not values:
                    continue
                history_linear_total += sum(
                    int(value.get("linear_iterations", 0)) for value in values
                )
                history_grouped.setdefault(qualified, {})[iteration] = max(
                    float(value["initial"]) for value in values
                )
            continuity_values = record.get("continuity")
            if continuity_values is not None:
                continuity_value = abs(float(continuity_values["global"]))
                if continuity_value > 0:
                    history_continuity[iteration] = continuity_value
        if history_continuity:
            history_grouped["continuity.global"] = history_continuity

        validation_path = run_path / f"reports/solver/{validation_name}"
        history_validation: dict[str, object] = {}
        history_sources = [str(log_path.relative_to(case))]
        if validation_path.is_file():
            history_validation = json.loads(validation_path.read_text(encoding="utf-8"))
            history_sources.append(str(validation_path.relative_to(case)))

        history_floors = history_validation.get("residual_floors", {})
        history_series = []
        history_iterations: set[int] = set()
        selected_keys = set(included_keys) if included_keys is not None else None
        for key in labels:
            if selected_keys is not None and key not in selected_keys:
                continue
            points = sorted(history_grouped.get(key, {}).items())
            if not points:
                continue
            stride = max(1, math.ceil(len(points) / 1200))
            sampled = [
                [iteration, value]
                for index, (iteration, value) in enumerate(points)
                if index % stride == 0 or index == len(points) - 1
            ]
            history_iterations.update(iteration for iteration, _ in points)
            history_series.append(
                {
                    "key": key,
                    "label": labels[key],
                    "colour": colours[key],
                    "points": sampled,
                    "raw_point_count": len(points),
                    "final": points[-1][1],
                    "configured_gate": history_floors.get(key),
                }
            )
        if not history_series or not history_iterations:
            return None
        history_checks = history_validation.get("residual_checks", {})
        relative_parts = run_path.relative_to(case).parts
        profile = relative_parts[1]
        resolved_scope = (
            scope_text
            if profile == "single"
            else scope_text.replace("새 표준 결과", "80만급/정밀 결과")
        )
        return {
            "available": True,
            "id": history_id,
            "label": label,
            "source": "actual OpenFOAM solver log",
            "scope": resolved_scope,
            "production_profile": profile,
            "source_files": history_sources,
            "series": history_series,
            "axis_mode": "openfoam_outer_iteration",
            "iteration_count": len(history_iterations),
            "first_iteration": min(history_iterations),
            "last_iteration": max(history_iterations),
            "linear_iteration_total": history_linear_total or None,
            "time_step_count": None,
            "first_time_s": None,
            "last_time_s": None,
            "validation_status": history_validation.get("status", "SAVED_HISTORY"),
            "configured_residual_checks_passed": (
                bool(history_checks)
                and all(bool(value) for value in history_checks.values())
            ),
            "available_during_calculation": True,
            "live_note": (
                "This is a completed, saved multi-equation OpenFOAM solver history."
            ),
            "iteration_axis_note": (
                "The x-axis is the OpenFOAM steady outer-iteration number. "
                "Velocity, pressure, turbulence and energy equations are shown from "
                "the actual solver log."
            ),
            "semantics": payload["semantics"],
        }

    histories = []
    for history in (
        _steady_history(
            run_name="steady_cold_flow",
            history_id="cold_flow",
            label="유동 초기화 · U/p/k/ω",
            scope_text="새 표준 결과 · 유동 초기화",
            validation_name="cold_flow_validation.json",
            included_keys=(
                "continuity.global",
                "coolant.p_rgh",
                "coolant.Ux",
                "coolant.Uy",
                "coolant.Uz",
                "coolant.k",
                "coolant.omega",
            ),
        ),
        _steady_history(
            run_name="steady_cht",
            history_id="steady_cht",
            label="정상 CHT · 유동+에너지",
            scope_text="새 표준 결과 · 정상 CHT",
            validation_name="steady_cht_validation.json",
        ),
    ):
        if history is not None and history["id"] != primary_id:
            histories.append(history)
    histories.append(dict(payload))
    payload["histories"] = histories
    # Open the residual history that belongs to the currently delivered result.
    # The steady CHT history remains selectable, but defaulting to it made a
    # completed 362k-iteration transient run look as if only 5,000 iterations
    # had been calculated.
    payload["default_history_id"] = primary_id
    return payload


def _direct_precision_xy_velocity_cut(
    config: ResolvedConfig, case: Path, *, production_profile: str = "L3"
) -> dict[str, object] | None:
    """Return a selected mesh's cell-centred XY cut without interpolation."""

    foam_file = case / f"production/{production_profile}/runs/steady_cht/steady_cht.foam"
    run_dir = foam_file.parent
    if not foam_file.is_file():
        return None
    numeric_times: list[float] = []
    for path in run_dir.iterdir():
        if not path.is_dir():
            continue
        try:
            numeric_times.append(float(path.name))
        except ValueError:
            continue
    if not numeric_times:
        return None

    try:
        import numpy as np
        import vtk
        from vtk.util.numpy_support import vtk_to_numpy
    except ImportError:
        return None

    reader = vtk.vtkOpenFOAMReader()
    reader.SetFileName(str(foam_file))
    reader.CreateCellToPointOff()
    reader.EnableAllCellArrays()
    reader.DisableAllPointArrays()
    reader.DisableAllPatchArrays()
    reader.SetPatchArrayStatus("internalMesh", 1)
    reader.UpdateInformation()
    latest_time = max(numeric_times)
    reader.UpdateTimeStep(latest_time)
    output = reader.GetOutput()
    fluid = None
    for index in range(output.GetNumberOfBlocks()):
        metadata = output.GetMetaData(index)
        name = metadata.Get(vtk.vtkCompositeDataSet.NAME()) if metadata else None
        if name != "coolant":
            continue
        parent = output.GetBlock(index)
        if hasattr(parent, "GetNumberOfBlocks"):
            for child_index in range(parent.GetNumberOfBlocks()):
                child = parent.GetBlock(child_index)
                if child is not None and child.GetNumberOfCells() > 0:
                    fluid = child
                    break
        else:
            fluid = parent
        break
    if fluid is None:
        return None

    plane = vtk.vtkPlane()
    plane.SetOrigin(0.0, 0.0, 0.5 * config.enclosure.height_z_m)
    plane.SetNormal(0.0, 0.0, 1.0)
    cutter = vtk.vtkCutter()
    cutter.SetCutFunction(plane)
    cutter.SetInputData(fluid)
    triangles = vtk.vtkTriangleFilter()
    triangles.SetInputConnection(cutter.GetOutputPort())
    triangles.Update()
    cut = triangles.GetOutput()
    velocity = cut.GetCellData().GetArray("U")
    if cut.GetPoints() is None or velocity is None:
        return None
    points = vtk_to_numpy(cut.GetPoints().GetData())[:, :2] * 1000.0
    cells = np.asarray(
        [
            [cut.GetCell(index).GetPointId(item) for item in range(3)]
            for index in range(cut.GetNumberOfCells())
        ],
        dtype=int,
    )
    speeds = np.linalg.norm(vtk_to_numpy(velocity), axis=1)
    return {
        "source": f"{production_profile} steady_cht cell-centred U on the Z-centre vtkCutter plane",
        "time": latest_time,
        "interpolation": "none; one containing OpenFOAM cell value per cut triangle",
        "points_xy_mm": np.round(points, 4).tolist(),
        "triangles": cells.tolist(),
        "velocity_m_s": np.round(speeds, 6).tolist(),
        "velocity_range_m_s": [0.0, float(np.max(speeds))],
        "triangle_count": int(len(cells)),
        "point_count": int(len(points)),
    }


def _interactive_legacy_cfd_payload(
    config: ResolvedConfig, case: Path, *, enabled: bool
) -> dict[str, object]:
    if not enabled:
        return {"enabled": False}
    foam_file = case / "production/L0/runs/frozen_flow/frozen_flow.foam"
    history_file = case / "results/per_cell_time_series.csv"
    telemetry_file = case / "results/telemetry.csv"
    if not foam_file.is_file() or not history_file.is_file() or not telemetry_file.is_file():
        return {"enabled": False}

    backend = OpenFoamResultReader(foam_file)
    fluid, _ = backend.regions(1800.0)
    all_paths = backend.streamline_paths(fluid, config)
    # Keep shorter inlet-fed paths as well.  They are important around the
    # ports and the first/last cell rows, and make the browser view resemble
    # the denser native post-processing streamline display.
    long_paths = [path for path in all_paths if path[4] >= 35.0]
    if not long_paths:
        return {"enabled": False}
    np = backend.np
    display_count = min(260, len(long_paths))
    selected = [
        long_paths[int(index)]
        for index in np.linspace(0, len(long_paths) - 1, display_count)
    ]
    browser_paths: list[dict[str, object]] = []
    for points, speeds, _, cumulative_mm, total_mm in selected:
        distances = np.linspace(0.0, total_mm, 72)
        coordinates = np.column_stack(
            [np.interp(distances, cumulative_mm, points[:, axis]) for axis in range(3)]
        )
        sampled_speed = np.interp(distances, cumulative_mm, speeds)
        browser_paths.append(
            {
                "p": [[round(float(value), 3) for value in row] for row in coordinates],
                "s": [round(float(value), 5) for value in sampled_speed],
                "length_mm": round(float(total_mm), 2),
            }
        )

    raw_history: dict[str, dict[int, float]] = {
        f"C{index:02d}": {
            0: config.boundary.initial_solid_temperature_k
            - 273.15
            + LEGACY_DISPLAY_TEMPERATURE_OFFSET_C
        }
        for index in range(1, 24)
    }
    with history_file.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            time_s = int(round(float(row["time_s"])))
            if time_s % 15 != 0:
                continue
            raw_history[row["cell_id"]][time_s] = (
                float(row["surface_tavg_K"])
                - 273.15
                + LEGACY_DISPLAY_TEMPERATURE_OFFSET_C
            )
    times = list(range(0, 1801, 15))
    if any(set(values) != set(times) for values in raw_history.values()):
        return {"enabled": False}
    temperature_series = {
        cell_id: [round(values[time_s], 5) for time_s in times]
        for cell_id, values in raw_history.items()
    }
    all_temperatures = [
        value for values in temperature_series.values() for value in values
    ]
    port_temperature: dict[str, dict[int, float]] = {
        "IN": {
            0: config.boundary.inlet_temperature_k
            - 273.15
            + LEGACY_DISPLAY_TEMPERATURE_OFFSET_C
        },
        "OUT": {
            0: config.boundary.initial_fluid_temperature_k
            - 273.15
            + LEGACY_DISPLAY_TEMPERATURE_OFFSET_C
        },
    }
    with telemetry_file.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            time_s = int(round(float(row["time_s"])))
            if time_s % 15 != 0:
                continue
            port_temperature["IN"][time_s] = (
                float(row["coolant_tin_K"])
                - 273.15
                + LEGACY_DISPLAY_TEMPERATURE_OFFSET_C
            )
            port_temperature["OUT"][time_s] = (
                float(row["coolant_tout_K"])
                - 273.15
                + LEGACY_DISPLAY_TEMPERATURE_OFFSET_C
            )
    if any(set(values) != set(times) for values in port_temperature.values()):
        return {"enabled": False}

    flow_root = case / "production/L0/runs/frozen_flow/postProcessing/coolant"
    inlet_mass_rows = read_openfoam_dat(
        flow_root / "coolant_inlet_mass_flow/0/surfaceFieldValue.dat"
    )
    outlet_mass_rows = read_openfoam_dat(
        flow_root / "coolant_outlet_mass_flow/0/surfaceFieldValue.dat"
    )
    inlet_pressure_rows = read_openfoam_dat(
        flow_root / "coolant_inlet_pressure_avg/0/surfaceFieldValue.dat"
    )
    outlet_pressure_rows = read_openfoam_dat(
        flow_root / "coolant_outlet_pressure_avg/0/surfaceFieldValue.dat"
    )
    inlet_velocity_path = (
        flow_root / "coolant_inlet_normal_velocity_avg/0/surfaceFieldValue.dat"
    )
    outlet_velocity_path = (
        flow_root / "coolant_outlet_normal_velocity_avg/0/surfaceFieldValue.dat"
    )
    inlet_area_m2 = math.pi * (config.ports.inlet_diameter_m / 2) ** 2
    outlet_area_m2 = math.pi * (config.ports.outlet_diameter_m / 2) ** 2
    inlet_mass_flow = abs(inlet_mass_rows[-1][1])
    outlet_mass_flow = abs(outlet_mass_rows[-1][1])
    inlet_pressure_pa = inlet_pressure_rows[-1][1]
    outlet_pressure_pa = outlet_pressure_rows[-1][1]
    port_flow = {
        "inlet_mass_flow_kg_s": inlet_mass_flow,
        "outlet_mass_flow_kg_s": outlet_mass_flow,
        "inlet_mean_velocity_m_s": (
            abs(read_openfoam_dat(inlet_velocity_path)[-1][1])
            if inlet_velocity_path.is_file()
            else inlet_mass_flow / (config.fluid.density_kg_m3 * inlet_area_m2)
        ),
        "outlet_mean_velocity_m_s": (
            abs(read_openfoam_dat(outlet_velocity_path)[-1][1])
            if outlet_velocity_path.is_file()
            else outlet_mass_flow / (config.fluid.density_kg_m3 * outlet_area_m2)
        ),
        "inlet_area_average_pressure_pa": inlet_pressure_pa,
        "outlet_area_average_pressure_pa": outlet_pressure_pa,
        "pressure_drop_pa": inlet_pressure_pa - outlet_pressure_pa,
        "spatial_field_semantics": "fixed converged U(x) and p(x) at 1800 s",
    }

    def sampling_plane(
        kind: str, columns: int, rows: int, *, fixed_fraction: float = 0.5
    ):
        vtk_points = backend.vtk.vtkPoints()
        coordinates: list[tuple[float, float, float]] = []
        if kind == "xy":
            fixed = config.enclosure.height_z_m * fixed_fraction
            for row in range(rows):
                y = config.enclosure.length_y_m * row / (rows - 1)
                for column in range(columns):
                    x = config.enclosure.width_x_m * column / (columns - 1)
                    coordinates.append((x, y, fixed))
        else:
            fixed = config.enclosure.length_y_m * fixed_fraction
            for row in range(rows):
                z = config.enclosure.height_z_m * row / (rows - 1)
                for column in range(columns):
                    x = config.enclosure.width_x_m * column / (columns - 1)
                    coordinates.append((x, fixed, z))
        for point in coordinates:
            vtk_points.InsertNextPoint(*point)
        samples = backend.vtk.vtkPolyData()
        samples.SetPoints(vtk_points)
        return samples, coordinates

    def probe(dataset, samples, array_name: str):
        sampler = backend.vtk.vtkProbeFilter()
        sampler.SetInputData(samples)
        sampler.SetSourceData(dataset)
        sampler.Update()
        point_data = sampler.GetOutput().GetPointData()
        array = point_data.GetArray(array_name)
        validity = point_data.GetArray("vtkValidPointMask")
        if array is None or validity is None:
            raise RuntimeError(f"VTK probe did not expose {array_name}")
        return (
            backend.vtk_to_numpy(array).copy(),
            backend.vtk_to_numpy(validity).astype(bool),
        )

    def json_values(values, validity):
        return [
            round(float(value), 5) if valid else None
            for value, valid in zip(values, validity)
        ]

    plane_specs = {
        "xy04": {
            "kind": "xy",
            "columns": 37,
            "rows": 41,
            "fixed_fraction": 3.5 / 82.0,
            "label": "Z = 3.5 mm · 하부 냉각수 간극",
        },
        "xy": {
            "kind": "xy",
            "columns": 37,
            "rows": 41,
            "fixed_fraction": 0.5,
            "label": "Z = 41.0 mm",
        },
        "xy96": {
            "kind": "xy",
            "columns": 37,
            "rows": 41,
            "fixed_fraction": 78.5 / 82.0,
            "label": "Z = 78.5 mm · 상부 냉각수 간극",
        },
        "xz15": {
            "kind": "xz",
            "columns": 37,
            "rows": 27,
            "fixed_fraction": 0.15,
            "label": "Y = 18.2 mm",
        },
        "xz33": {
            "kind": "xz",
            "columns": 37,
            "rows": 27,
            "fixed_fraction": 0.325,
            "label": "Y = 39.3 mm",
        },
        "xz50": {
            "kind": "xz",
            "columns": 37,
            "rows": 27,
            "fixed_fraction": 0.5,
            "label": "Y = 60.5 mm",
        },
        "xz68": {
            "kind": "xz",
            "columns": 37,
            "rows": 27,
            "fixed_fraction": 0.675,
            "label": "Y = 81.7 mm",
        },
        "xz85": {
            "kind": "xz",
            "columns": 37,
            "rows": 27,
            "fixed_fraction": 0.85,
            "label": "Y = 102.8 mm",
        },
    }
    plane_sources: dict[str, object] = {}
    sections: dict[str, dict[str, object]] = {}
    for name, specification in plane_specs.items():
        samples, coordinates = sampling_plane(
            specification["kind"],
            specification["columns"],
            specification["rows"],
            fixed_fraction=specification["fixed_fraction"],
        )
        plane_sources[name] = samples
        first = coordinates[0]
        horizontal = coordinates[1]
        vertical = coordinates[specification["columns"]]
        sections[name] = {
            "kind": specification["kind"],
            "columns": specification["columns"],
            "rows": specification["rows"],
            "fixed_fraction": specification["fixed_fraction"],
            "label": specification["label"],
            "origin_mm": [round(1000 * value, 4) for value in first],
            "column_step_mm": [
                round(1000 * (horizontal[axis] - first[axis]), 4)
                for axis in range(3)
            ],
            "row_step_mm": [
                round(1000 * (vertical[axis] - first[axis]), 4)
                for axis in range(3)
            ],
            "temperature_frames_c": [],
        }

    # Figure 8 uses the actual spatially varying battery-surface field.  Keep a
    # compact but genuinely field-derived sample lattice for the interactive
    # cylinders instead of painting every cylinder with its surface average.
    angular_segments = 28
    axial_segments = 12
    radius = config.cells.diameter_m / 2
    half_length = config.cells.length_m / 2
    surface_targets: dict[str, object] = {}
    for placement in cell_placements(config):
        points: list[tuple[float, float, float]] = []
        for axial in range(axial_segments):
            z_value = (
                placement.center_z_m
                - half_length
                + config.cells.length_m * (axial + 0.5) / axial_segments
            )
            for angular in range(angular_segments):
                angle = 2 * math.pi * (angular + 0.5) / angular_segments
                points.append(
                    (
                        placement.center_x_m + radius * math.cos(angle),
                        placement.center_y_m + radius * math.sin(angle),
                        z_value,
                    )
                )
        for z_value in (
            placement.center_z_m - half_length,
            placement.center_z_m + half_length,
        ):
            for angular in range(angular_segments):
                angle = 2 * math.pi * (angular + 0.5) / angular_segments
                points.append(
                    (
                        placement.center_x_m + 0.58 * radius * math.cos(angle),
                        placement.center_y_m + 0.58 * radius * math.sin(angle),
                        z_value,
                    )
                )
        surface_targets[placement.cell_id] = np.asarray(points)

    surface_point_indices: dict[str, object] = {}
    surface_temperature_frames: dict[str, list[list[float]]] = {
        cell_id: [] for cell_id in surface_targets
    }
    maximum_surface_sample_distance_mm = 0.0

    section_temperature_values: dict[str, list[float]] = {
        name: [] for name in sections
    }
    final_fluid = fluid
    for time_s in times:
        transient_fluid, transient_solid = backend.regions(float(time_s))
        if time_s == times[-1]:
            final_fluid = transient_fluid
        surface_points, _, surface_temperature = backend.surface(transient_solid)
        if not surface_point_indices:
            for placement in cell_placements(config):
                delta_xy = np.linalg.norm(
                    surface_points[:, :2]
                    - np.asarray(
                        [placement.center_x_m, placement.center_y_m]
                    ),
                    axis=1,
                )
                candidates = np.flatnonzero(
                    (delta_xy <= radius * 1.08)
                    & (
                        np.abs(surface_points[:, 2] - placement.center_z_m)
                        <= half_length * 1.03
                    )
                )
                if not len(candidates):
                    raise RuntimeError(
                        f"no solid surface points found near {placement.cell_id}"
                    )
                targets = surface_targets[placement.cell_id]
                candidate_points = surface_points[candidates]
                distances_squared = np.sum(
                    (targets[:, None, :] - candidate_points[None, :, :]) ** 2,
                    axis=2,
                )
                closest = candidates[np.argmin(distances_squared, axis=1)]
                surface_point_indices[placement.cell_id] = closest
                maximum_surface_sample_distance_mm = max(
                    maximum_surface_sample_distance_mm,
                    1000
                    * float(
                        np.sqrt(
                            distances_squared[
                                np.arange(len(targets)),
                                np.argmin(distances_squared, axis=1),
                            ]
                        ).max()
                    ),
                )
        for cell_id, indices in surface_point_indices.items():
            values = (
                surface_temperature[indices]
                - 273.15
                + LEGACY_DISPLAY_TEMPERATURE_OFFSET_C
            )
            surface_temperature_frames[cell_id].append(
                [round(float(value), 5) for value in values]
            )
        for name, section in sections.items():
            samples = plane_sources[name]
            fluid_temperature, fluid_valid = probe(transient_fluid, samples, "T")
            solid_temperature, solid_valid = probe(transient_solid, samples, "T")
            combined_valid = fluid_valid | solid_valid
            combined_temperature = np.where(
                solid_valid, solid_temperature, fluid_temperature
            ) - 273.15 + LEGACY_DISPLAY_TEMPERATURE_OFFSET_C
            section["temperature_frames_c"].append(
                json_values(combined_temperature, combined_valid)
            )
            section_temperature_values[name].extend(
                float(value)
                for value, valid in zip(combined_temperature, combined_valid)
                if valid
            )

    for name, section in sections.items():
        samples = plane_sources[name]
        velocity, velocity_valid = probe(final_fluid, samples, "U")
        pressure, pressure_valid = probe(final_fluid, samples, "p")
        speed = np.linalg.norm(velocity, axis=1)
        section["velocity_m_s"] = json_values(speed, velocity_valid)
        section["velocity_vectors_m_s"] = [
            [round(float(component), 5) for component in vector] if valid else None
            for vector, valid in zip(velocity, velocity_valid)
        ]
        temperatures = section_temperature_values[name]
        section["temperature_range_c"] = [min(temperatures), max(temperatures)]
        valid_speed = [
            float(value) for value, valid in zip(speed, velocity_valid) if valid
        ]
        section["velocity_range_m_s"] = [0.0, max(valid_speed)]
        pressure_gauge = pressure - port_flow["outlet_area_average_pressure_pa"]
        section["pressure_gauge_pa"] = json_values(
            pressure_gauge, pressure_valid
        )
        valid_pressure = [
            float(value)
            for value, valid in zip(pressure_gauge, pressure_valid)
            if valid
        ]
        section["pressure_range_pa"] = [
            min(valid_pressure),
            max(valid_pressure),
        ]

    final_cell_velocity = backend.vtk_to_numpy(
        final_fluid.GetCellData().GetArray("U")
    )
    velocity_max_m_s = float(np.linalg.norm(final_cell_velocity, axis=1).max())
    lengths = [float(path[4]) for path in selected]
    direct_xy_velocity = _direct_precision_xy_velocity_cut(config, case)
    return {
        "enabled": True,
        "semantics": (
            "OpenFOAM cell temperature T(t) with a +0.150 K display-only "
            "datum correction; moving heads follow the validated fixed "
            "velocity field U(x), not transient U(x,t)."
        ),
        "temperature_display_offset_c": LEGACY_DISPLAY_TEMPERATURE_OFFSET_C,
        "temperature_display_semantics": (
            "raw solver temperatures are preserved; viewer values equal raw degC + 0.150 K"
        ),
        "times_s": times,
        "temperature_c": temperature_series,
        "port_temperature_c": {
            name: [round(values[time_s], 5) for time_s in times]
            for name, values in port_temperature.items()
        },
        "port_flow": port_flow,
        "paper_temperature_ranges_c": {
            "cell": REFERENCE_FIGURE_RANGES["figure_09_temperature_degC"],
            "xy": REFERENCE_FIGURE_RANGES["figure_14_temperature_degC"],
            "xz": REFERENCE_FIGURE_RANGES["figure_13b_temperature_degC"],
        },
        "temperature_range_c": [min(all_temperatures), max(all_temperatures)],
        "cell_surface": {
            "angular_segments": angular_segments,
            "axial_segments": axial_segments,
            "temperature_frames_c": surface_temperature_frames,
            "temperature_range_c": [
                min(min(values) for values in section_temperature_values.values()),
                max(max(values) for values in section_temperature_values.values()),
            ],
            "sample_semantics": (
                "nearest actual OpenFOAM batterySolid surface point on a "
                "28-angle by 12-height display lattice; no per-cell flat fill"
            ),
            "maximum_sample_distance_mm": round(
                maximum_surface_sample_distance_mm, 4
            ),
        },
        "velocity_range_m_s": [0.0, velocity_max_m_s],
        "velocity_max_m_s": velocity_max_m_s,
        "velocity_scale_semantics": (
            "shared global range from every coolant cell-centred U magnitude; "
            "used by streamlines, XY sections, XZ sections, vectors, and ports"
        ),
        "pressure_available": True,
        "pressure_reference_pa": port_flow["outlet_area_average_pressure_pa"],
        "pressure_semantics": (
            "direct coolant-cell absolute p minus outlet area-average p; "
            "displayed as gauge pressure on XY/XZ/YZ sections"
        ),
        "streamlines": browser_paths,
        "streamline_count": len(browser_paths),
        "source_streamline_count": len(all_paths),
        "minimum_displayed_length_mm": min(lengths),
        "maximum_displayed_length_mm": max(lengths),
        "sections": sections,
        "direct_xy_velocity": direct_xy_velocity,
        "vertical_section_keys": ["xz15", "xz33", "xz50", "xz68", "xz85"],
    }


def _regional_initial_conditions_match(
    config: ResolvedConfig, manifest: dict[str, object]
) -> bool:
    """Return true only when persisted thermal initials match current inputs."""

    try:
        solid = float(
            manifest.get("initial_solid_temperature_k", manifest["initial_temperature_k"])
        )
        fluid = float(manifest.get("initial_fluid_temperature_k", solid))
    except (KeyError, TypeError, ValueError):
        return False
    return math.isclose(
        solid,
        config.boundary.initial_solid_temperature_k,
        rel_tol=0.0,
        abs_tol=1e-9,
    ) and math.isclose(
        fluid,
        config.boundary.initial_fluid_temperature_k,
        rel_tol=0.0,
        abs_tol=1e-9,
    )


def _interactive_actual_cfd_payload(
    config: ResolvedConfig,
    case: Path,
    *,
    enabled: bool,
    mesh_id: str = "precision",
    steady_run_dir: Path | None = None,
    transient_run_dir: Path | None = None,
) -> dict[str, object] | None:
    """Build the browser payload from a completed, validated result package."""

    if not enabled:
        return None
    if mesh_id not in {"precision", "single"}:
        raise ValueError(f"unsupported viewer mesh id: {mesh_id}")
    production_profile = "L3" if mesh_id == "precision" else "single"
    mesh_label = "precision" if mesh_id == "precision" else "standard"
    runs_root = (
        case / "runs"
        if mesh_id == "precision" and (case / "runs/frozen_flow_actual_1s").is_dir()
        else case / f"production/{production_profile}/runs"
    )
    upgraded_run = runs_root / "frozen_flow_actual_1s_xy5"
    run = (
        transient_run_dir.resolve()
        if mesh_id == "precision" and transient_run_dir is not None
        else runs_root / "frozen_flow_actual_1s"
    )
    if (
        mesh_id == "precision"
        and (upgraded_run / "reports/viewer/actual_1s_manifest.json").is_file()
    ):
        run = upgraded_run
    manifest_path = run / "reports/viewer/actual_1s_manifest.json"
    validation_path = run / "reports/viewer/actual_1s_validation.json"
    foam_file = (
        steady_run_dir.resolve() / "steady_cht.foam"
        if mesh_id == "precision" and steady_run_dir is not None
        else runs_root / "steady_cht/steady_cht.foam"
    )
    if (
        not manifest_path.is_file()
        or not validation_path.is_file()
        or not foam_file.is_file()
    ):
        return None

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if not _regional_initial_conditions_match(config, manifest):
        # Never compare or expose an archived thermal run under a new input
        # label.  In particular, the old standard result started both regions
        # at 293.15 K whereas the current Fluent-aligned case uses coolant
        # 293 K and batteries 293.15 K.
        return None
    if (
        manifest.get("status") != "ACTUAL_ONE_SECOND_SAMPLES_READY"
        or validation.get("status")
        != "ACTUAL_ONE_SECOND_BUNDLE_VALIDATION_PASSED"
    ):
        return None
    expected_times = list(range(0, 1801))
    initial_solid_temperature_k = float(
        manifest.get("initial_solid_temperature_k", manifest["initial_temperature_k"])
    )
    initial_fluid_temperature_k = float(
        manifest.get("initial_fluid_temperature_k", initial_solid_temperature_k)
    )

    def exact_series(path: Path, initial_k: float) -> list[float]:
        values = {0: initial_k}
        for row in read_openfoam_dat_series(path):
            time_s = int(round(row[0]))
            if abs(row[0] - time_s) > 1e-7:
                raise ValueError(f"{path}: non-integral time {row[0]}")
            values[time_s] = row[1]
        if sorted(values) != expected_times:
            raise ValueError(
                f"{path}: expected exact 0..1800 s values; found {len(values)}"
            )
        return [round(values[time_s] - 273.15, 8) for time_s in expected_times]

    def header_measure(path: Path, name: str) -> float:
        prefix = f"# {name}"
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(prefix) and ":" in line:
                return float(line.split(":", 1)[1].strip().split()[0])
        raise ValueError(f"{path}: missing {name} header")

    solid_root = run / "postProcessing/batterySolid"
    cell_temperature: dict[str, list[float]] = {}
    cell_internal_temperature: dict[str, list[float]] = {}
    cell_mesh_maximum_temperature: dict[str, list[float]] = {}
    cell_surface_areas_m2: dict[str, float] = {}
    cell_volumes_m3: dict[str, float] = {}
    for index in range(1, 24):
        cell_id = f"C{index:02d}"
        surface_path = (
            solid_root
            / f"battery_{cell_id}_surface_Tavg/0/surfaceFieldValue.dat"
        )
        internal_path = solid_root / f"battery_{cell_id}_Tavg/0/volFieldValue.dat"
        maximum_path = solid_root / f"battery_{cell_id}_Tmax/0/volFieldValue.dat"
        cell_temperature[cell_id] = exact_series(
            surface_path, initial_solid_temperature_k
        )
        cell_internal_temperature[cell_id] = exact_series(
            internal_path, initial_solid_temperature_k
        )
        cell_mesh_maximum_temperature[cell_id] = exact_series(
            maximum_path, initial_solid_temperature_k
        )
        cell_surface_areas_m2[cell_id] = header_measure(surface_path, "Area")
        cell_volumes_m3[cell_id] = header_measure(internal_path, "Volume")

    def weighted_average_series(
        values: dict[str, list[float]], weights: dict[str, float]
    ) -> list[float]:
        total_weight = sum(weights.values())
        return [
            round(
                sum(values[cell_id][time_s] * weights[cell_id] for cell_id in values)
                / total_weight,
                8,
            )
            for time_s in expected_times
        ]

    battery_surface_average = weighted_average_series(
        cell_temperature, cell_surface_areas_m2
    )
    battery_internal_average = weighted_average_series(
        cell_internal_temperature, cell_volumes_m3
    )
    battery_mesh_maximum = [
        round(
            max(values[time_s] for values in cell_mesh_maximum_temperature.values()),
            8,
        )
        for time_s in expected_times
    ]
    port_temperature = {
        "IN": exact_series(
            run
            / "postProcessing/coolant/coolant_inlet_mass_weighted_temperature/0/surfaceFieldValue.dat",
            initial_fluid_temperature_k,
        ),
        "OUT": exact_series(
            run
            / "postProcessing/coolant/coolant_outlet_mass_weighted_temperature/0/surfaceFieldValue.dat",
            initial_fluid_temperature_k,
        ),
    }

    flow_root = run / "postProcessing/coolant"
    inlet_mass = abs(
        read_openfoam_dat_series(
            flow_root / "coolant_inlet_mass_flow/0/surfaceFieldValue.dat"
        )[-1][1]
    )
    outlet_mass = abs(
        read_openfoam_dat_series(
            flow_root / "coolant_outlet_mass_flow/0/surfaceFieldValue.dat"
        )[-1][1]
    )
    inlet_pressure = read_openfoam_dat_series(
        flow_root / "coolant_inlet_pressure_avg/0/surfaceFieldValue.dat"
    )[-1][1]
    outlet_pressure = read_openfoam_dat_series(
        flow_root / "coolant_outlet_pressure_avg/0/surfaceFieldValue.dat"
    )[-1][1]
    inlet_velocity_path = (
        flow_root / "coolant_inlet_normal_velocity_avg/0/surfaceFieldValue.dat"
    )
    outlet_velocity_path = (
        flow_root / "coolant_outlet_normal_velocity_avg/0/surfaceFieldValue.dat"
    )
    inlet_area = math.pi * (0.5 * config.ports.inlet_diameter_m) ** 2
    outlet_area = math.pi * (0.5 * config.ports.outlet_diameter_m) ** 2
    port_flow = {
        "inlet_mass_flow_kg_s": inlet_mass,
        "outlet_mass_flow_kg_s": outlet_mass,
        "inlet_mean_velocity_m_s": (
            abs(read_openfoam_dat_series(inlet_velocity_path)[-1][1])
            if inlet_velocity_path.is_file()
            else inlet_mass / (config.fluid.density_kg_m3 * inlet_area)
        ),
        "outlet_mean_velocity_m_s": (
            abs(read_openfoam_dat_series(outlet_velocity_path)[-1][1])
            if outlet_velocity_path.is_file()
            else outlet_mass / (config.fluid.density_kg_m3 * outlet_area)
        ),
        "inlet_area_average_pressure_pa": inlet_pressure,
        "outlet_area_average_pressure_pa": outlet_pressure,
        "pressure_drop_pa": inlet_pressure - outlet_pressure,
        "spatial_field_semantics": f"validated {mesh_label}-mesh fixed field U(x), p(x)",
    }

    backend = OpenFoamResultReader(foam_file)
    numeric_times = []
    for path in foam_file.parent.iterdir():
        if path.is_dir():
            try:
                numeric_times.append(float(path.name))
            except ValueError:
                pass
    fluid, _ = backend.regions(max(numeric_times))
    # 냉각수 1초 probe는 batterySolid 내부를 의도적으로 Not Found로 둔다.
    # 하지만 두 보존 실행 모두 1800 s의 전체 batterySolid/T 필드를 갖고
    # 있으므로, 최종 프레임만큼은 절단점이 들어있는 실제 고체 메시 셀의
    # 온도를 직접 읽는다. 중간 프레임의 고체 공간장을 추정해 만들지는 않는다.
    thermal_backend = OpenFoamResultReader(run / "frozen_flow_actual_1s.foam")
    final_fluid, final_solid = thermal_backend.regions(1800.0)
    final_fluid_locator = thermal_backend.vtk.vtkStaticCellLocator()
    final_fluid_locator.SetDataSet(final_fluid)
    final_fluid_locator.BuildLocator()
    final_fluid_temperature = thermal_backend.vtk_to_numpy(
        final_fluid.GetCellData().GetArray("T")
    )
    solid_locator = thermal_backend.vtk.vtkStaticCellLocator()
    solid_locator.SetDataSet(final_solid)
    solid_locator.BuildLocator()
    solid_temperature = thermal_backend.vtk_to_numpy(
        final_solid.GetCellData().GetArray("T")
    )
    all_paths = backend.streamline_paths(fluid, config)
    long_paths = [path for path in all_paths if path[4] >= 35.0]
    selected_count = min(260, len(long_paths))
    selected = [
        long_paths[int(index)]
        for index in backend.np.linspace(0, len(long_paths) - 1, selected_count)
    ]
    browser_paths = [
        {
            "p": [
                [round(float(component), 4) for component in point]
                for point in points
            ],
            "s": [round(float(value), 7) for value in speeds],
            "length_mm": round(float(total_mm), 3),
        }
        for points, speeds, _, _, total_mm in selected
    ]

    def centred_axis(length: float, spacing: float) -> list[float]:
        count = max(1, math.ceil(length / spacing))
        return [length * (index + 0.5) / count for index in range(count)]

    x_values = centred_axis(config.enclosure.width_x_m, 0.001)
    y_values = centred_axis(config.enclosure.length_y_m, 0.001)
    z_values = centred_axis(config.enclosure.height_z_m, 0.0015)
    lower_gap_z_m = (config.enclosure.height_z_m - config.cells.length_m) / 4
    upper_gap_z_m = config.enclosure.height_z_m - lower_gap_z_m
    plane_specs = {
        "xy04": ("xy", x_values, y_values, lower_gap_z_m / config.enclosure.height_z_m, f"Z = {1000 * lower_gap_z_m:.1f} mm · 하부 냉각수 간극"),
        "xy15": ("xy", x_values, y_values, 0.15, "Z = 12.3 mm"),
        "xy33": ("xy", x_values, y_values, 0.325, "Z = 26.7 mm"),
        "xy": ("xy", x_values, y_values, 0.5, "Z = 41.0 mm"),
        "xy68": ("xy", x_values, y_values, 0.675, "Z = 55.4 mm"),
        "xy85": ("xy", x_values, y_values, 0.85, "Z = 69.7 mm"),
        "xy96": ("xy", x_values, y_values, upper_gap_z_m / config.enclosure.height_z_m, f"Z = {1000 * upper_gap_z_m:.1f} mm · 상부 냉각수 간극"),
        "xz15": ("xz", x_values, z_values, 0.15, "Y = 18.2 mm"),
        "xz33": ("xz", x_values, z_values, 0.325, "Y = 39.3 mm"),
        "xz50": ("xz", x_values, z_values, 0.5, "Y = 60.5 mm"),
        "xz68": ("xz", x_values, z_values, 0.675, "Y = 81.7 mm"),
        "xz85": ("xz", x_values, z_values, 0.85, "Y = 102.8 mm"),
        "yz15": ("yz", y_values, z_values, 0.15, "X = 17.1 mm"),
        "yz33": ("yz", y_values, z_values, 0.325, "X = 37.1 mm"),
        "yz50": ("yz", y_values, z_values, 0.5, "X = 57.0 mm"),
        "yz68": ("yz", y_values, z_values, 0.675, "X = 77.0 mm"),
        "yz85": ("yz", y_values, z_values, 0.85, "X = 96.9 mm"),
    }
    locator = backend.vtk.vtkStaticCellLocator()
    locator.SetDataSet(fluid)
    locator.BuildLocator()
    cell_velocity = backend.vtk_to_numpy(fluid.GetCellData().GetArray("U"))
    pressure_array = fluid.GetCellData().GetArray("p")
    if pressure_array is None:
        raise RuntimeError("validated steady coolant field has no pressure p")
    cell_pressure = backend.vtk_to_numpy(pressure_array)
    velocity_max_m_s = float(backend.np.linalg.norm(cell_velocity, axis=1).max())
    sections: dict[str, dict[str, object]] = {}
    for name, (kind, horizontal, vertical, fraction, label) in plane_specs.items():
        if name not in manifest["datasets"] and kind != "yz":
            continue
        coordinates = (
            [
                (x, y, fraction * config.enclosure.height_z_m)
                for y in vertical
                for x in horizontal
            ]
            if kind == "xy"
            else (
                [
                (x, fraction * config.enclosure.length_y_m, z)
                for z in vertical
                for x in horizontal
                ]
                if kind == "xz"
                else [
                    (fraction * config.enclosure.width_x_m, y, z)
                    for z in vertical
                    for y in horizontal
                ]
            )
        )
        velocity_vectors: list[list[float] | None] = []
        velocity_magnitudes: list[float | None] = []
        pressure_gauge_pa: list[float | None] = []
        fluid_temperature_1800_c: list[float | None] = []
        solid_temperature_1800_c: list[float | None] = []
        for point in coordinates:
            final_fluid_cell_id = final_fluid_locator.FindCell(point)
            fluid_temperature_1800_c.append(
                round(
                    float(final_fluid_temperature[final_fluid_cell_id]) - 273.15,
                    8,
                )
                if final_fluid_cell_id >= 0
                else None
            )
            solid_cell_id = solid_locator.FindCell(point)
            solid_temperature_1800_c.append(
                round(float(solid_temperature[solid_cell_id]) - 273.15, 8)
                if solid_cell_id >= 0
                else None
            )
            cell_id = locator.FindCell(point)
            if cell_id < 0:
                velocity_vectors.append(None)
                velocity_magnitudes.append(None)
                pressure_gauge_pa.append(None)
                continue
            vector = cell_velocity[cell_id]
            velocity_vectors.append(
                [round(float(component), 7) for component in vector]
            )
            velocity_magnitudes.append(
                round(float(backend.np.linalg.norm(vector)), 7)
            )
            pressure_gauge_pa.append(
                round(float(cell_pressure[cell_id]) - outlet_pressure, 7)
            )
        valid_velocity = [value for value in velocity_magnitudes if value is not None]
        valid_pressure = [value for value in pressure_gauge_pa if value is not None]
        valid_solid_temperature = [
            value for value in solid_temperature_1800_c if value is not None
        ]
        valid_final_temperature = [
            value
            for value in fluid_temperature_1800_c + solid_temperature_1800_c
            if value is not None
        ]
        origin = coordinates[0]
        next_column = coordinates[1]
        next_row = coordinates[len(horizontal)]
        sections[name] = {
            "kind": kind,
            "columns": len(horizontal),
            "rows": len(vertical),
            "fixed_fraction": fraction,
            "label": label,
            "origin_mm": [round(1000 * value, 6) for value in origin],
            "column_step_mm": [
                round(1000 * (next_column[axis] - origin[axis]), 6)
                for axis in range(3)
            ],
            "row_step_mm": [
                round(1000 * (next_row[axis] - origin[axis]), 6)
                for axis in range(3)
            ],
            "actual_dataset": name if name in manifest["datasets"] else None,
            "temperature_range_c": (
                manifest["datasets"][name]["temperature_range_c"]
                if name in manifest["datasets"]
                else [min(valid_final_temperature), max(valid_final_temperature)]
            ),
            "fluid_temperature_1800_c": fluid_temperature_1800_c,
            "solid_temperature_1800_c": solid_temperature_1800_c,
            "solid_temperature_time_s": 1800,
            # The user-requested horizontal cuts above and below the battery
            # pack intentionally contain coolant only.  An empty solid sample
            # set is therefore valid geometry, not a failed CFD field.
            "solid_temperature_range_c": (
                [min(valid_solid_temperature), max(valid_solid_temperature)]
                if valid_solid_temperature
                else None
            ),
            "solid_temperature_semantics": (
                f"direct containing {mesh_label}-mesh batterySolid cell T at 1800 s; "
                "other integer seconds use the direct per-cell volume average"
            ),
            "velocity_m_s": velocity_magnitudes,
            "velocity_vectors_m_s": velocity_vectors,
            "velocity_range_m_s": [0.0, max(valid_velocity)],
            "pressure_gauge_pa": pressure_gauge_pa,
            "pressure_range_pa": [min(valid_pressure), max(valid_pressure)],
            "spatial_sampling": f"direct containing {mesh_label}-mesh cell; no interpolation or padding",
        }

    datasets = json.loads(json.dumps(manifest["datasets"]))
    for specification in datasets.values():
        specification["file"] = f"data/{specification['file']}"
    temperatures = [value for series in cell_temperature.values() for value in series]
    surface_spec = datasets["cell_surface"]
    heat_transfer_spec = datasets.get("cell_surface_heat_transfer_coefficient")
    surface_heat_transfer = None
    if heat_transfer_spec is not None:
        surface_heat_transfer = {
            "angular_segments": heat_transfer_spec["angular_segments"],
            "axial_segments": heat_transfer_spec["axial_segments"],
            "actual_dataset": "cell_surface_heat_transfer_coefficient",
            "value_range_w_m2k": heat_transfer_spec["value_range_w_m2k"],
            "distribution_percentiles_w_m2k": heat_transfer_spec.get(
                "distribution_percentiles_w_m2k", {}
            ),
            "recommended_fixed_display_upper_w_m2k": heat_transfer_spec.get(
                "recommended_fixed_display_upper_w_m2k"
            ),
            # A run-level range is fixed over the complete 1..1800 s result.
            # The cumulative viewer merger later replaces this with one shared
            # range for every run that has the direct interface dataset.
            "fixed_legend_range_w_m2k": [
                0.0,
                heat_transfer_spec["value_range_w_m2k"][1],
            ],
            "definition": heat_transfer_spec["definition"],
            "scope": "coolant-facing C01-C23 battery interface only",
            "sample_semantics": heat_transfer_spec["sampling"],
        }
    return {
        "enabled": True,
        "semantics": (
            f"{mesh_label}-result OpenFOAM containing-cell T samples at every integer second; "
            "no temporal interpolation, spatial interpolation, display correction, or padding. "
            f"Moving heads follow the validated fixed {mesh_label}-result U(x)."
        ),
        "temperature_display_offset_c": 0.0,
        "temperature_display_semantics": "raw solver kelvin converted only by subtracting 273.15",
        "times_s": expected_times,
        "time_step_s": 1,
        "temporal_interpolation": False,
        "spatial_interpolation": False,
        "padding": False,
        "temperature_c": cell_temperature,
        "port_temperature_c": port_temperature,
        "temperature_graphs": {
            "time_step_s": 1,
            "outlet_mass_weighted_average_c": port_temperature["OUT"],
            "battery_mesh_maximum_c": battery_mesh_maximum,
            "battery_surface_area_average_c": battery_surface_average,
            "battery_internal_volume_average_c": battery_internal_average,
            "per_cell": {
                "surface_area_average_c": cell_temperature,
                "internal_volume_average_c": cell_internal_temperature,
                "mesh_maximum_c": cell_mesh_maximum_temperature,
            },
            "surface_area_m2": cell_surface_areas_m2,
            "volume_m3": cell_volumes_m3,
            "semantics": {
                "outlet": "coolant outlet phi-weighted average temperature",
                "battery_maximum": "maximum of batterySolid cell-mesh max(T) across C01-C23",
                "surface_average": "area-weighted mean of C01-C23 interface areaAverage(T)",
                "internal_average": "volume-weighted mean of C01-C23 cell-zone volAverage(T)",
                "per_cell": "direct C01-C23 function-object values at every integer second",
            },
        },
        "port_flow": port_flow,
        "paper_temperature_ranges_c": {
            "cell": REFERENCE_FIGURE_RANGES["figure_09_temperature_degC"],
            "xy": REFERENCE_FIGURE_RANGES["figure_14_temperature_degC"],
            "xz": REFERENCE_FIGURE_RANGES["figure_13b_temperature_degC"],
        },
        "temperature_range_c": [min(temperatures), max(temperatures)],
        "cell_surface": {
            "angular_segments": surface_spec["angular_segments"],
            "axial_segments": surface_spec["axial_segments"],
            "actual_dataset": "cell_surface",
            "temperature_range_c": surface_spec["temperature_range_c"],
            "sample_semantics": "direct containing batterySolid cell at 32 angles x 16 heights; invalid samples omitted",
        },
        "cell_surface_heat_transfer": surface_heat_transfer,
        "actual_one_second": {
            "validation_status": validation["status"],
            "mesh_cell_count": manifest["mesh_cell_count"],
            "initial_temperature_k": manifest["initial_temperature_k"],
            "initial_fluid_temperature_k": initial_fluid_temperature_k,
            "initial_solid_temperature_k": initial_solid_temperature_k,
            "times_s": manifest["times_s"],
            "temperature_encoding": manifest["temperature_encoding"],
            "temporal_interpolation": False,
            "spatial_interpolation": False,
            "padding": False,
            "datasets": datasets,
        },
        "velocity_range_m_s": [0.0, velocity_max_m_s],
        "velocity_max_m_s": velocity_max_m_s,
        "velocity_scale_semantics": (
            f"shared global range from every {mesh_label}-mesh coolant "
            "cell-centred U magnitude; used by streamlines, XY sections, "
            "XZ sections, vectors, and ports"
        ),
        "pressure_available": True,
        "pressure_reference_pa": outlet_pressure,
        "pressure_semantics": (
            "direct coolant-cell absolute p minus outlet area-average p; "
            "displayed as gauge pressure on XY/XZ/YZ sections"
        ),
        "streamlines": browser_paths,
        "streamline_count": len(browser_paths),
        "source_streamline_count": len(all_paths),
        "minimum_displayed_length_mm": min(float(path[4]) for path in selected),
        "maximum_displayed_length_mm": max(float(path[4]) for path in selected),
        "sections": sections,
        "direct_xy_velocity": _direct_precision_xy_velocity_cut(
            config, case, production_profile=production_profile
        ),
        "horizontal_section_keys": ["xy04", "xy15", "xy33", "xy", "xy68", "xy85", "xy96"],
        "vertical_section_keys": ["xz15", "xz33", "xz50", "xz68", "xz85"],
        "transverse_section_keys": ["yz15", "yz33", "yz50", "yz68", "yz85"],
    }


def _interactive_cfd_payload(
    config: ResolvedConfig, case: Path, *, enabled: bool
) -> dict[str, object]:
    actual = _interactive_actual_cfd_payload(config, case, enabled=enabled)
    if actual is not None:
        return actual
    return _interactive_legacy_cfd_payload(config, case, enabled=enabled)


HTML = '''<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CFD 결과 뷰어</title>
  <link rel="stylesheet" href="__STYLE_ASSET__">
</head>
<body>
  <header>
    <div class="brand">
      <h1>CFD 결과 뷰어</h1>
      <span>DIGITAL TWIN · SIMULATION REPLAY</span>
    </div>
    <div class="header-actions"><a id="home-tab" href="index.html">← 처음으로</a>
      <a id="cad-tab" href="cad/index.html" __CAD_LINK_HIDDEN__>CAD 상면도·측면도 ↗</a>
      <a id="mesh-tab" href="mesh.html" hidden>메시 보기 ↗</a>
      <div class="language-switch" role="group" aria-label="Language / 언어"><button type="button" data-language="ko" aria-pressed="true">한국어</button><button type="button" data-language="en" aria-pressed="false">English</button></div>
      <div id="status" class="status">GEOMETRY PREVIEW · CFD 미실행</div></div>
  </header>
  <main>
    <section class="viewer-panel">
      <div class="viewer-controls">
        <fieldset id="viz-modes" class="viz-modes" aria-label="결과 화면 선택">
          <legend>결과 화면</legend>
          <label><input type="radio" name="viz-mode" value="combined" checked><span>종합 화면</span></label>
          <label><input type="radio" name="viz-mode" value="cell-temperature"><span>셀 온도</span></label>
          <label><input type="radio" name="viz-mode" value="cell-extrema-map"><span>셀 최고·최저 온도</span></label>
          <label><input type="radio" name="viz-mode" value="velocity-streamlines"><span>유체 유선</span></label>
          <label><input type="radio" name="viz-mode" value="xy-location"><span>가로 단면 위치</span></label>
          <label><input type="radio" name="viz-mode" value="xy-temperature"><span>가로 단면 온도</span></label>
          <label><input type="radio" name="viz-mode" value="xy-velocity"><span>가로 단면 유속</span></label>
          <label><input type="radio" name="viz-mode" value="xy-mass-flow"><span>가로 단면 질량유량</span></label>
          <label><input type="radio" name="viz-mode" value="xy-pressure"><span>가로 단면 압력</span></label>
          <label><input type="radio" name="viz-mode" value="surface-heat-transfer"><span>배터리 표면 열전달계수</span></label>
          <label><input type="radio" name="viz-mode" value="xz-location"><span>세로 단면 위치</span></label>
          <label><input type="radio" name="viz-mode" value="xz-temperature"><span>세로 단면 온도</span></label>
          <label><input type="radio" name="viz-mode" value="xz-velocity"><span>세로 단면 유속</span></label>
          <label><input type="radio" name="viz-mode" value="xz-mass-flow"><span>세로 단면 질량유량</span></label>
          <label><input type="radio" name="viz-mode" value="xz-pressure"><span>세로 단면 압력</span></label>
        </fieldset>
        <div class="control-head">
          <div class="toolbar" aria-label="camera views">
            <span class="control-title">시점</span>
            <button data-view="px">+X</button><button data-view="nx">−X</button>
            <button data-view="py">+Y</button><button data-view="ny">−Y</button>
            <button data-view="pz">+Z</button><button data-view="nz">−Z</button>
            <button data-view="iso" class="active">등각</button>
            <button id="reset">맞춤</button>
          </div>
          <fieldset id="display-options" class="display-options" aria-label="화면 레이어">
            <legend>레이어</legend>
            <label><input id="show-cells" type="checkbox" checked> 셀</label>
            <label><input id="show-flow" type="checkbox" checked> 유선·벡터</label>
            <label><input id="show-particles" type="checkbox" checked> 이동 헤드</label>
            <label><input id="show-section" type="checkbox" checked> 단면</label>
            <label><input id="show-grid" type="checkbox"> 샘플선</label>
            <input id="show-mesh" type="checkbox" hidden>
            <label title="셀 표면과 실제 유효 단면 샘플 사이만 부드럽게 연결하며 무효영역은 채우지 않습니다"><input id="smooth-section" type="checkbox" checked> 부드러운 색상</label>
            <label id="section-3d-option" title="가로·세로 단면을 회전·확대 가능한 3D 평면으로 표시합니다" hidden><input id="section-3d" type="checkbox" checked> 3D 단면</label>
            <label><input id="show-enclosure" type="checkbox" checked> 외곽선</label>
            <label><input id="show-temperature-legend" type="checkbox" checked> 온도 범례</label>
            <label><input id="show-velocity-legend" type="checkbox" checked> 속도 범례</label>
            <label><input id="show-mass-flow-legend" type="checkbox" checked> 질량유량 범례</label>
            <label><input id="show-pressure-legend" type="checkbox" checked> 압력 범례</label>
            <label><input id="show-heat-transfer-legend" type="checkbox" checked> 열전달계수 범례</label>
            <label><input id="show-port-callouts" type="checkbox" checked> 입·출구 정보</label>
            <label><input id="show-extrema-callouts" type="checkbox" checked> 최고 온도 지점</label>
            <button id="reset-overlays" type="button">표시 위치 초기화</button>
            <button id="flow-only" type="button">유동만</button>
            <button id="show-all" type="button">전체</button>
          </fieldset>
        </div>
        <fieldset id="vertical-sections" class="section-positions vertical-sections" aria-label="vertical section direction and position" hidden>
          <legend>세로 단면</legend>
          <select id="vertical-section-plane" aria-label="vertical section plane direction">
            <option value="yz" selected>YZ · X 고정 (5/4열)</option>
            <option value="xz">XZ · Y 고정</option>
          </select>
          <label><input type="radio" name="vertical-section" value="15"> 15%</label>
          <label><input type="radio" name="vertical-section" value="33"> 32.5%</label>
          <label><input type="radio" name="vertical-section" value="50" checked> 50%</label>
          <label><input type="radio" name="vertical-section" value="68"> 67.5%</label>
          <label><input type="radio" name="vertical-section" value="85"> 85%</label>
          <button id="vertical-centre" type="button">중앙</button>
          <output id="vertical-section-state">X = 57.0 mm · YZ 단면</output>
        </fieldset>
        <fieldset id="horizontal-sections" class="section-positions horizontal-sections" aria-label="horizontal XY section positions" hidden>
          <legend>가로 XY 위치</legend>
          <label><input type="radio" name="horizontal-section" value="xy04"> 하부 간극 3.5 mm</label>
          <label><input type="radio" name="horizontal-section" value="xy15"> Z 15%</label>
          <label><input type="radio" name="horizontal-section" value="xy33"> Z 32.5%</label>
          <label><input type="radio" name="horizontal-section" value="xy" checked> Z 50%</label>
          <label><input type="radio" name="horizontal-section" value="xy68"> Z 67.5%</label>
          <label><input type="radio" name="horizontal-section" value="xy85"> Z 85%</label>
          <label><input type="radio" name="horizontal-section" value="xy96"> 상부 간극 78.5 mm</label>
          <button id="horizontal-centre" type="button">중앙</button>
          <output id="horizontal-section-state">Z = 41.0 mm · 직접 단면</output>
        </fieldset>
      </div>
      <div class="viewport">
        <div class="scene-stage">
          <canvas id="scene" aria-label="interactive 3D battery geometry"></canvas>
          <canvas id="scene-overlay" aria-hidden="true"></canvas>
          <div id="data-load-status" class="data-load-status" role="status" aria-live="polite" hidden></div>
        </div>
        <div id="flow-controls" class="flow-controls" aria-label="interactive CFD playback">
        <button id="flow-play" type="button">❚❚ 정지</button>
        <input id="flow-seek" type="range" min="0" max="1800" step="1" value="0" aria-label="simulation time in one-second display increments">
        <label for="flow-rate">배속</label>
        <select id="flow-rate" aria-label="interactive playback speed">
          <option value="1" selected>1×</option>
          <option value="5">5×</option>
          <option value="10">10×</option>
          <option value="30">30×</option>
          <option value="60">60×</option>
          <option value="180">180×</option>
        </select>
        <label for="head-rate">헤드 속도</label>
        <select id="head-rate" aria-label="moving head display speed" title="유속 수치는 그대로 유지하고 이동 헤드의 표시속도만 조절합니다">
          <option value="0.25">0.25×</option>
          <option value="0.5" selected>0.5× 보기</option>
          <option value="1">1× 실제</option>
        </select>
        <output id="flow-duration">1× 완주 30:00</output>
        <output id="flow-time">t = 0 s</output>
        <span id="flow-sampling">화면 1 s</span>
        <span id="current-mode" aria-live="polite">종합 화면</span>
        <span id="port-state" aria-live="polite">IN/OUT 면적평균</span>
        </div>
        <div id="figure-preview" class="figure-preview" hidden>
        <img id="figure-image" alt="generated figure artifact">
        <video id="figure-video" muted playsinline hidden></video>
        <div id="video-controls" class="video-controls" hidden>
          <button id="video-play" type="button">❚❚ 정지</button>
          <button id="video-prev" type="button" aria-label="previous animation frame">◀ 1프레임</button>
          <button id="video-next" type="button" aria-label="next animation frame">1프레임 ▶</button>
          <input id="video-seek" type="range" min="0" max="1000" value="0" aria-label="animation timeline">
          <label for="playback-rate">배속</label>
          <select id="playback-rate" aria-label="animation playback speed">
            <option value="1" selected>1×</option>
            <option value="5">5×</option>
            <option value="10">10×</option>
            <option value="30">30×</option>
            <option value="60">60×</option>
            <option value="180">180×</option>
          </select>
          <output id="video-duration">1× 완주 30:00</output>
          <output id="video-time">대기 중</output>
          <span id="video-semantics"></span>
        </div>
        <div id="artifact-tabs" class="artifact-tabs"></div>
        </div>
        <div id="mesh-figure-panel" class="mesh-figure-panel" hidden>
          <div class="mesh-figure-toolbar">
            <button id="mesh-figure-close" type="button">← 3D 화면</button>
            <strong id="mesh-figure-title">계산 메시 비교</strong>
            <button id="mesh-figure-png" type="button">PNG 저장</button>
          </div>
          <div id="mesh-figure-image-grid" class="mesh-figure-image-grid" hidden></div>
          <div id="mesh-figure-canvas-wrap" class="mesh-figure-canvas-wrap">
            <canvas id="mesh-figure-canvas" aria-label="precision and standard mesh result comparison"></canvas>
          </div>
          <p id="mesh-figure-status"></p>
        </div>
        <div id="temperature-chart-panel" class="temperature-chart-panel" hidden>
          <div class="temperature-chart-toolbar">
            <button id="temperature-chart-close" type="button">← 3D 화면</button>
            <label for="temperature-chart-metric">그래프</label>
            <select id="temperature-chart-metric">
              <option value="outlet">1. 출구 평균 온도</option>
              <option value="battery-maximum">2. 배터리 최고 온도</option>
              <option value="surface-average">3. 배터리 표면 평균 온도</option>
              <option value="internal-average">4. 배터리 내부 온도</option>
              <option value="per-cell">5. 각 배터리별 온도</option>
              <option value="energy-balance">6. 에너지 수지</option>
              <option value="key-cells">7. 주요 배터리 5셀 온도</option>
              <option value="cell-extrema">8. 각 셀 최고·최저 온도</option>
            </select>
            <label id="per-cell-metric-label" for="per-cell-metric" hidden>셀 기준</label>
            <select id="per-cell-metric" hidden>
              <option value="internal_volume_average_c">내부 체적평균</option>
              <option value="surface_area_average_c">표면 면적평균</option>
              <option value="mesh_maximum_c">mesh 최고값</option>
            </select>
            <label id="cell-extrema-cell-label" for="cell-extrema-cell" hidden>배터리 셀</label>
            <select id="cell-extrema-cell" hidden aria-label="battery cell for maximum and minimum temperature"></select>
            <button id="temperature-chart-png" type="button">PNG 추출</button>
            <button id="temperature-chart-csv" type="button">현재 CSV</button>
            <button id="temperature-chart-all-csv" type="button">전체 CSV</button>
          </div>
          <div class="temperature-chart-canvas-wrap">
            <canvas id="temperature-chart-canvas" aria-label="actual CFD temperature time-series chart"></canvas>
          </div>
          <output id="temperature-chart-status" aria-live="polite"></output>
        </div>
        <div id="convergence-chart-panel" class="convergence-chart-panel" hidden>
          <div class="convergence-chart-toolbar">
            <button id="convergence-chart-close" type="button">← 3D 화면</button>
            <strong>CFD 수렴 이력</strong>
            <label for="convergence-history-select">계산 단계</label>
            <select id="convergence-history-select" aria-label="CFD convergence calculation stage"></select>
            <span>실제 솔버 로그 · 잔차 로그 축</span>
            <button id="convergence-chart-png" type="button">PNG 추출</button>
            <button id="convergence-chart-csv" type="button">CSV 추출</button>
          </div>
          <div class="convergence-chart-canvas-wrap">
            <canvas id="convergence-chart-canvas" aria-label="actual CFD residual convergence chart"></canvas>
          </div>
          <output id="convergence-chart-status" aria-live="polite"></output>
        </div>
        <div id="canvas-note" class="canvas-note">드래그: 재생 중 3D 회전 · 휠: 확대/축소 · Ū·p̄ 실제 면적평균</div>
      </div>
    </section>
    <aside>
      <section id="calculation-card" class="card result-card warning">
        <div class="card-title-row">
          <h2>현재 결과</h2>
          <span id="result-badge" class="result-badge">대기</span>
        </div>
        <div class="kpi-grid" aria-label="주요 CFD 결과">
          <article><span>출구 평균 온도</span><strong id="kpi-outlet-temperature">—</strong><small id="kpi-outlet-time">현재 시점</small></article>
          <article><span>최고 셀 온도</span><strong id="kpi-cell-maximum">—</strong><small id="kpi-cell-time">23개 셀 · 현재 시점</small></article>
          <article><span>표면 최고온도 지점</span><strong id="kpi-cell-hotspot">—</strong><small id="kpi-cell-hotspot-location">배터리 표면 실제 샘플 좌표</small></article>
          <article><span>입구 유속</span><strong id="kpi-inlet-velocity">—</strong><small>면적 평균</small></article>
          <article><span>압력 강하</span><strong id="kpi-pressure-drop">—</strong><small>입구 − 출구</small></article>
          <article><span>입구 질량유량</span><strong id="kpi-inlet-mass-flow">—</strong><small>면적 적분 |φ|</small></article>
          <article><span>출구 질량유량</span><strong id="kpi-outlet-mass-flow">—</strong><small>질량보존 확인</small></article>
          <article id="kpi-htc-average-card" hidden><span>표면 평균 h</span><strong id="kpi-htc-average">—</strong><small>현재 시각 · 전체 유효 표본</small></article>
          <article id="kpi-htc-minimum-card" hidden><span>표면 최소 h</span><strong id="kpi-htc-minimum">—</strong><small>현재 시각 · 직접 표본</small></article>
          <article id="kpi-htc-maximum-card" hidden><span>표면 최대 h</span><strong id="kpi-htc-maximum">—</strong><small>현재 시각 · 직접 표본</small></article>
        </div>
        <p class="result-meta"><a href="battery_hotspots_1800s.csv" download>Ø19–Ø34 · 1800 s 최고온도 지점 CSV</a></p>
        <p id="calculation-summary" class="result-meta"><strong>CFD 미실행</strong></p>
        <p id="calculation-note" hidden></p>
      </section>
      <section class="card condition-card">
        <h2>경계 조건</h2>
        <dl id="inputs"></dl>
      </section>
      <section class="card mesh-run-card">
        <h2>계산 메시 선택</h2>
        <label for="mesh-run-select">결과 데이터</label>
        <select id="mesh-run-select" aria-label="calculation mesh result"></select>
        <dl id="mesh-run-detail"></dl>
        <p id="mesh-run-note" hidden></p>
      </section>
      <section class="card convergence-card">
        <h2>CFD 수렴 모니터</h2>
        <dl id="convergence-summary"></dl>
        <button id="convergence-chart-open" type="button">수렴 그래프 열기</button>
      </section>
      <section id="failed-convergence-card" class="card failed-convergence-card" hidden>
        <div class="card-title-row">
          <h2>Ø34 실패 수렴 기록</h2>
          <span class="result-badge failed">검증 실패</span>
        </div>
        <dl id="failed-convergence-summary"></dl>
        <p>합격 결과 목록과 분리된 진단 자료이며, 실제 OpenFOAM 로그를 표시합니다.</p>
        <button id="failed-convergence-chart-open" type="button">실패 수렴 그래프 열기</button>
      </section>
      <section class="card temperature-export-card">
        <h2>온도 그래프</h2>
        <dl id="temperature-graph-summary"></dl>
        <button id="temperature-chart-open" type="button">온도 그래프 열기</button>
      </section>
      <details class="card detail-card">
        <summary>모델 정보</summary>
        <div class="detail-body"><dl id="geometry"></dl></div>
      </details>
      <details class="card detail-card">
        <summary>해석 세부정보</summary>
        <div class="detail-body"><dl id="derived"></dl></div>
      </details>
      <details class="card detail-card">
        <summary>논문 Figure 결과</summary>
        <div class="detail-body">
          <div id="figures" class="figure-grid"></div>
          <p id="figure-detail">Fig. 1 · 3D 인터랙티브 화면</p>
        </div>
      </details>
      <details class="card detail-card">
        <summary>셀 ID 보기</summary>
        <div class="detail-body"><div id="cells" class="cell-grid"></div></div>
      </details>
    </aside>
  </main>
  <div id="cell-toast" class="cell-toast" role="status" aria-live="polite" hidden></div>
  <footer id="provenance"></footer>
  <script src="i18n.js"></script>
  <script src="__MODEL_ASSET__"></script>
  <script src="__APP_ASSET__"></script>
</body>
</html>
'''


CSS = '''
:root { color-scheme:light; --bg:#eef3f6; --panel:#ffffff; --panel-raised:#f7fafb;
  --line:#ccd8df; --text:#17232d; --muted:#61727e; --accent:#168ac1;
  --accent-soft:#e7f4fa; --cyan:#168ca5; --amber:#9b6a16; --success:#168468; }
* { box-sizing:border-box; }
html.capture-mode header,html.capture-mode aside,html.capture-mode footer { display:none; }
html.capture-mode main { display:block; min-height:100vh; padding:0; }
html.capture-mode .viewer-panel { position:relative; top:0; width:100%; height:100vh; min-height:0; border:0; border-radius:0; }
html.capture-mode .viewer-controls,html.capture-mode .flow-controls,html.capture-mode .canvas-note { display:none; }
html.capture-mode .convergence-chart-toolbar,html.capture-mode #convergence-chart-status { display:none; }
html.capture-mode .convergence-chart-panel { padding:0; gap:0; }
html { min-width:320px; background:var(--bg); }
body { margin:0; min-width:320px; min-height:100vh; min-height:100dvh; overflow-x:hidden; color:var(--text);
  background-color:var(--bg); background-image:linear-gradient(#168ac10a 1px,transparent 1px),linear-gradient(90deg,#168ac10a 1px,transparent 1px);
  background-size:32px 32px;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
header { position:sticky; top:0; z-index:20; height:68px; display:flex; justify-content:space-between; align-items:center; padding:0 20px;
  border-bottom:1px solid var(--line); background:#f9fbfcf2; backdrop-filter:blur(12px); }
.brand { display:flex; align-items:baseline; gap:12px; min-width:0; }
h1 { margin:0; font-size:20px; font-weight:660; letter-spacing:-.02em; }
.brand span { color:#6e8491; font:600 9px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace; letter-spacing:.14em; white-space:nowrap; }
.header-actions{display:flex;align-items:center;justify-content:flex-end;gap:10px;min-width:0}
.language-switch{display:inline-flex;gap:2px;padding:3px;border:1px solid #b9cbd4;border-radius:6px;background:#fff;white-space:nowrap}
.language-switch button{border:0;border-radius:4px;padding:6px 9px;background:transparent;color:#526774;font-size:11px;font-weight:700;cursor:pointer}
.language-switch button[aria-pressed="true"]{background:#146f9d;color:#fff}
#home-tab,#mesh-tab,#cad-tab{color:#116d8b;border:1px solid #8ab9c7;border-radius:4px;padding:8px 12px;background:#eef7fa;
  text-decoration:none;font-size:12px;font-weight:700;white-space:nowrap}
#home-tab[hidden],#mesh-tab[hidden],#cad-tab[hidden]{display:none}
#home-tab:hover,#mesh-tab:hover,#cad-tab:hover{background:#dceff4}
.status { max-width:60%; display:flex; align-items:center; gap:8px; padding:7px 10px; border:1px solid #b9cbd4;
  color:#425762; background:#fff; border-radius:4px; font:600 10px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace; text-align:right; }
.status::before { content:""; flex:0 0 auto; width:6px; height:6px; border-radius:50%; background:var(--success); box-shadow:0 0 8px #16846866; }
main { display:grid; grid-template-columns:minmax(0,1fr) 330px; align-items:start; gap:12px; padding:12px; min-height:calc(100dvh - 68px); }
.viewer-panel { position:sticky; top:80px; height:calc(100dvh - 92px); min-height:620px; display:flex; flex-direction:column;
  border:1px solid var(--line); border-radius:8px; overflow:hidden; background:#0c1721; box-shadow:0 8px 28px #39516114; }
.viewer-controls { position:relative; z-index:4; flex:0 0 auto; display:grid; gap:8px; padding:0 10px 9px;
  border-bottom:1px solid var(--line); background:#f8fafb; color:var(--text); }
.viewer-controls[hidden] { display:none; }
.control-head { display:flex; align-items:center; justify-content:space-between; gap:14px; min-width:0; padding-top:1px; }
.control-title { color:var(--muted); font-size:11px; font-weight:650; }
.viewport { position:relative; flex:1 1 auto; min-height:0; overflow:hidden; display:flex;
  flex-direction:column; background:#0c1721; }
.scene-stage { position:relative; flex:1 1 0; width:100%; min-width:0; min-height:0; overflow:hidden;
  overflow:clip; contain:strict; isolation:isolate; clip-path:inset(0); background:#0c1721; }
#scene,#scene-overlay { position:absolute; inset:0; width:100%; height:100%; max-width:100%; max-height:100%;
  min-width:0; min-height:0; display:block; }
#scene { cursor:grab; background:#0c1721; }
#scene-overlay { z-index:1; pointer-events:none; }
#scene:active { cursor:grabbing; }
.data-load-status { position:absolute; z-index:5; top:14px; left:50%; transform:translateX(-50%); max-width:min(760px,calc(100% - 28px));
  padding:8px 11px; border:1px solid #496f9a; border-radius:5px; background:#101a25f2; color:#d7e6f7;
  font-size:11px; font-weight:650; text-align:center; }
.data-load-status.error { border-color:#9e7535; background:#241c11f2; color:#f3d89e; }
.data-load-status[hidden] { display:none; }
.toolbar { display:flex; align-items:center; gap:5px; flex-wrap:wrap; }
.toolbar button { min-width:36px; padding:5px 8px; }
.viz-modes { display:flex; gap:0; margin:0; padding:0; overflow-x:auto; overflow-y:hidden;
  border:0; border-bottom:1px solid var(--line); color:#43545f; font-size:12px; min-width:0;
  scrollbar-width:thin; scrollbar-color:#a9bdc8 transparent; }
.viz-modes[hidden] { display:none; }
.viz-modes legend { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0,0,0,0); }
.viz-modes label { position:relative; flex:1 0 112px; min-height:43px; display:flex; align-items:center; justify-content:center; padding:7px 8px;
  border:0; border-bottom:2px solid transparent; background:transparent; white-space:nowrap; cursor:pointer; text-align:center; }
.viz-modes label:hover { color:#17232d; background:#eef4f7; }
.viz-modes label:has(input:checked) { border-bottom-color:var(--accent); color:#0a638d; background:var(--accent-soft); }
.viz-modes label:focus-within { outline:2px solid #168ac155; outline-offset:-2px; }
.viz-modes input { position:absolute; width:1px; height:1px; opacity:0; pointer-events:none; }
.viz-modes label:has(input[value="cell-extrema-map"]) { flex-basis:150px; font-weight:750; }
.control-head[hidden] { display:none; }
.display-options { display:flex; align-items:center; justify-content:flex-end; gap:6px 10px; flex-wrap:wrap;
  margin:0; padding:0; border:0; color:#4d5d67; font-size:11px; }
.display-options[hidden] { display:none; }
.display-options legend { color:var(--muted); padding:0; margin-right:2px; font-size:11px; font-weight:650; }
.display-options label { display:flex; align-items:center; gap:4px; padding:4px 2px; white-space:nowrap; cursor:pointer; }
.display-options label[hidden] { display:none; }
.display-options input { margin:0; accent-color:var(--accent); }
.display-options button { padding:5px 8px; font-size:10px; }
.vertical-sections,.horizontal-sections { display:flex; align-items:center; justify-content:center; gap:7px; flex-wrap:wrap;
  margin:0; padding:7px 9px; border:1px solid var(--line); border-radius:5px; background:#fff; color:#40515d;
  font-size:11px; }
.vertical-sections[hidden],.horizontal-sections[hidden] { display:none; }
.vertical-sections legend,.horizontal-sections legend { color:var(--cyan); padding:0 5px; font-weight:650; }
.vertical-sections label,.horizontal-sections label { display:flex; align-items:center; gap:5px; min-height:28px; padding:4px 9px;
  border:1px solid #c9d5dc; border-radius:4px; background:#f7fafb; white-space:nowrap; cursor:pointer; }
.vertical-sections label:hover,.horizontal-sections label:hover { border-color:#68a7c4; }
.vertical-sections label:has(input:checked),.horizontal-sections label:has(input:checked) { border-color:var(--accent); color:#0a638d; background:var(--accent-soft); }
.vertical-sections input,.horizontal-sections input { margin:0; accent-color:var(--cyan); }
.vertical-sections select { padding:5px 8px; border:1px solid #b8c9d2; border-radius:4px; background:#f7fafb; color:#263b48; font-weight:700; }
.vertical-sections button,.horizontal-sections button { padding:5px 9px; font-size:10px; }
#vertical-section-state,#horizontal-section-state { color:var(--amber); font-variant-numeric:tabular-nums; font-weight:700; }
.flow-controls { position:relative; z-index:3; flex:0 0 auto; min-height:46px; margin:0 12px 4px;
  display:flex; align-items:center; justify-content:center; gap:9px; padding:7px 10px;
  border:1px solid #304651; border-radius:5px; background:#09121aed; color:#a9bfd0;
  font-size:11px; flex-wrap:wrap; }
.flow-controls[hidden] { display:none; }
.flow-controls button { min-width:72px; padding:5px 8px; border-color:#45606d; color:#e1edf2; background:#132532; }
.flow-controls button:hover { color:#fff; background:#1b3343; border-color:#6b94a8; }
.flow-controls button.active { color:#fff; background:#176e91; border-color:#4fb8d8; }
.flow-controls input[type="range"] { width:min(330px,36vw); accent-color:var(--cyan); }
.flow-controls select { border:1px solid #344653; border-radius:4px; color:#dcecf2; background:#121e28;
  padding:5px 7px; font-weight:700; }
#flow-time { color:var(--cyan); min-width:92px; font-variant-numeric:tabular-nums; font-weight:750; }
#flow-sampling { color:#a9bfd0; white-space:nowrap; }
#flow-sampling,#current-mode,.canvas-note { display:none !important; }
#flow-duration,#video-duration { color:var(--amber); min-width:92px; font-variant-numeric:tabular-nums; font-weight:750; }
button { border:1px solid #bccbd3; border-radius:4px; color:#31434f; background:#fff;
  padding:7px 10px; font-weight:650; cursor:pointer; transition:border-color .15s,background .15s,color .15s; }
button:hover { color:#075f85; background:#eef7fb; border-color:#68a7c4; }
button.active { color:#fff; background:#167dac; border-color:#167dac; }
.canvas-note { position:relative; z-index:2; flex:0 0 auto; margin:0 12px 10px; padding:6px 9px; background:#09121ad1;
  border:1px solid #253844; border-radius:4px; color:#9fb2c0; font-size:11px; }
.figure-preview { position:absolute; inset:0; padding:18px; background:#0a1118; z-index:1; }
.figure-preview img,.figure-preview video { width:100%; height:calc(100% - 76px); object-fit:contain; display:block; }
.video-controls { min-height:34px; display:flex; align-items:center; justify-content:center; gap:8px;
  color:#a9bfd0; font-size:11px; padding-top:6px; white-space:nowrap; flex-wrap:wrap; }
.video-controls[hidden] { display:none; }
.video-controls button { padding:4px 7px; font-size:10px; }
.video-controls input[type="range"] { width:min(220px,28vw); accent-color:var(--cyan); }
.video-controls select { border:1px solid #344653; border-radius:4px; color:#dcecf2; background:#121e28;
  padding:5px 7px; font-weight:700; }
#video-time { color:var(--cyan); min-width:180px; font-variant-numeric:tabular-nums; font-weight:750; }
#video-semantics { overflow:hidden; text-overflow:ellipsis; }
.artifact-tabs { display:flex; justify-content:center; gap:7px; padding-top:8px; }
.mesh-figure-panel,.temperature-chart-panel,.convergence-chart-panel { position:absolute; inset:0; z-index:7; display:flex; flex-direction:column; gap:8px;
  padding:12px; background:#f7fafcfa; }
.temperature-chart-panel,.convergence-chart-panel{background:#fff}
.mesh-figure-panel[hidden],.temperature-chart-panel[hidden],.convergence-chart-panel[hidden] { display:none; }
.mesh-figure-toolbar,.temperature-chart-toolbar,.convergence-chart-toolbar { display:flex; align-items:center; gap:7px; flex-wrap:wrap; color:var(--muted); font-size:11px; }
.mesh-figure-toolbar strong { margin-right:auto; color:#263b48; font-size:13px; }
.mesh-figure-toolbar button,.temperature-chart-toolbar button,.convergence-chart-toolbar button { padding:6px 9px; font-size:10px; }
.temperature-chart-toolbar select,.convergence-chart-toolbar select { border:1px solid #bccbd3; border-radius:4px; color:#31434f; background:#fff;
  padding:6px 8px; font-weight:700; }
.mesh-figure-canvas-wrap,.temperature-chart-canvas-wrap,.convergence-chart-canvas-wrap { flex:1 1 auto; min-height:280px; overflow:hidden; }
.mesh-figure-canvas-wrap[hidden] { display:none; }
.mesh-figure-image-grid { flex:1 1 auto; min-height:0; display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; overflow:hidden; }
.mesh-figure-image-grid[hidden] { display:none; }
.mesh-figure-image-grid figure { min-width:0; min-height:0; margin:0; padding:8px; display:flex; flex-direction:column; gap:6px; border:1px solid #c8d7de; border-radius:5px; background:#07131f; }
.mesh-figure-image-grid figcaption { display:flex; align-items:center; justify-content:space-between; gap:8px; color:#d9e8ef; font-size:11px; font-weight:700; }
.mesh-figure-image-grid img { width:100%; height:calc(100% - 27px); min-height:0; object-fit:contain; }
.mesh-figure-image-grid a { padding:4px 7px; border:1px solid #537487; border-radius:4px; color:#d9eef7; text-decoration:none; font-size:9px; }
#mesh-figure-canvas,#temperature-chart-canvas,#convergence-chart-canvas { width:100%; height:100%; display:block; }
#mesh-figure-status { min-height:20px; margin:0; color:#536874; font-size:11px; font-variant-numeric:tabular-nums; }
#temperature-chart-status,#convergence-chart-status { min-height:20px; color:#536874; font-size:11px; font-variant-numeric:tabular-nums; }
.temperature-export-card dl { margin-bottom:11px; }
#temperature-chart-open,#convergence-chart-open { width:100%; border-color:#1685b3; color:#fff; background:#1685b3; }
#failed-convergence-chart-open { width:100%; border-color:#b42318; color:#fff; background:#b42318; }
.failed-convergence-card { border-left:4px solid #b42318; background:#fff8f7; }
.failed-convergence-card p { margin:8px 0 11px; color:#7a271a; font-size:11px; line-height:1.5; }
.result-badge.failed { color:#b42318; background:#fee4e2; }
.mesh-run-card label { display:block; margin-bottom:5px; color:var(--muted); font-size:10px; font-weight:700; }
.mesh-run-card select { width:100%; margin-bottom:11px; padding:8px; border:1px solid #b9cbd4; border-radius:4px; color:#243946; background:#f8fbfc; font-weight:700; }
.mesh-run-card dl,.convergence-card dl { margin-bottom:11px; }
aside { min-width:0; overflow:visible; padding-right:3px; }
.card { padding:14px 15px; margin-bottom:10px; background:#fff; border:1px solid var(--line); border-radius:6px; box-shadow:0 2px 8px #3951610a; }
.card h2 { margin:0 0 11px; padding-bottom:8px; border-bottom:1px solid #e1e8ec; font-size:13px; font-weight:650; color:var(--text); }
.card p { color:var(--muted); font-size:12px; line-height:1.55; margin:5px 0; }
.card-title-row { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:11px; padding-bottom:9px; border-bottom:1px solid #e1e8ec; }
.card-title-row h2 { margin:0; padding:0; border:0; }
.result-badge { display:inline-flex; align-items:center; gap:5px; padding:4px 7px; border-radius:999px;
  color:#526873; background:#edf2f4; font-size:9px; font-weight:750; letter-spacing:.04em; }
.result-badge::before { content:""; width:6px; height:6px; border-radius:50%; background:#8ba0aa; }
.validated .result-badge { color:#126a57; background:#e7f6f0; }
.validated .result-badge::before { background:var(--success); box-shadow:0 0 7px #16846855; }
.quality-warning .result-badge { color:#955300; background:#fff1d8; }
.quality-warning .result-badge::before { background:#e59318; box-shadow:0 0 7px #e5931855; }
.kpi-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:7px; }
.kpi-grid article { min-width:0; padding:10px; border:1px solid #dce6eb; border-radius:5px; background:#f7fafb; }
.kpi-grid span,.kpi-grid small { display:block; overflow:hidden; color:#71838d; font-size:9px; white-space:nowrap; text-overflow:ellipsis; }
.kpi-grid strong { display:block; margin:3px 0 2px; color:#142a36; font-size:17px; line-height:1.15; font-variant-numeric:tabular-nums; letter-spacing:-.02em; }
.kpi-grid article:first-child strong { color:#087c9a; }
.kpi-grid article:nth-child(2) strong { color:#b04d1e; }
.result-meta { margin-top:10px !important; padding:8px 9px; border-radius:4px; color:#536874 !important; background:#eef4f6; }
.result-meta strong { color:#176b5a; }
.card.detail-card { padding:0; margin-bottom:8px; }
.detail-card summary { position:relative; padding:11px 40px 11px 16px; list-style:none; color:#334852; font-size:14px; line-height:1.25; font-weight:720; cursor:pointer; user-select:none; }
.detail-card summary::-webkit-details-marker { display:none; }
.detail-card summary::after { content:"+"; position:absolute; top:50%; right:16px; transform:translateY(-50%); color:#71838d; font-size:20px; line-height:1; font-weight:400; }
.detail-card[open] summary::after { content:"−"; }
.detail-card[open] summary { border-bottom:1px solid #e1e8ec; }
.detail-body { padding:13px 15px 15px; }
.warning { border-left:3px solid #8d6d36; }
.warning strong { color:var(--amber); }
.warning.validated { border-color:var(--line); border-left-color:var(--success); background:#f5fbf8; }
.warning.validated strong { color:#15745f; }
dl { margin:0; display:grid; grid-template-columns:minmax(76px,.72fr) minmax(0,1.28fr); gap:7px 12px; font-size:12px; }
dt { min-width:0; color:var(--muted); word-break:keep-all; overflow-wrap:normal; }
dd { min-width:0; margin:0; text-align:right; overflow-wrap:anywhere; word-break:keep-all; font-variant-numeric:tabular-nums; font-weight:700; }
.cell-grid { display:grid; grid-template-columns:repeat(8,1fr); gap:5px; }
.cell-grid button { min-width:0; padding:6px 2px; text-align:center; border:1px solid #c9d8df; border-radius:3px;
  color:#346477; background:#f4f9fb; font-size:10px; }
.cell-grid button:hover { border-color:#168ac1; color:#075f85; background:#e5f5fb; }
.cell-grid button.active { border-color:#05080a; color:#fff; background:#05080a; box-shadow:0 0 0 2px #ffffffb8; }
.cell-toast { position:fixed; z-index:50; top:82px; right:18px; max-width:min(320px,calc(100vw - 36px));
  padding:10px 13px; border:1px solid #38a5ca; border-radius:5px; color:#eaf9ff; background:#0b2230ee;
  box-shadow:0 10px 28px #07131c42; font-size:12px; font-weight:700; }
.cell-toast[hidden] { display:none; }
.figure-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:5px; }
.figure-grid button { padding:6px 2px; text-align:center; border:1px solid #d8cfbc; border-radius:3px;
  color:#846b38; background:#fffaf0; font-size:10px; }
.figure-grid button.ready { border-color:#97bccb; color:#1a627c; background:#eef8fb; }
footer { min-height:32px; padding:6px 19px 8px; color:#617d91; font-size:10px; overflow:hidden; white-space:nowrap; }
@media (min-width:1181px) {
  header { height:84px; padding:0 26px; }
  h1 { font-size:26px; }
  .brand span { font-size:12px; }
  .header-actions { gap:13px; }
  .language-switch button { padding:9px 12px; font-size:14px; }
  #home-tab,#mesh-tab,#cad-tab { padding:10px 15px; font-size:15px; }
  .status { padding:9px 13px; font-size:13px; }
  main { grid-template-columns:minmax(0,1fr) 420px; gap:16px; padding:16px; min-height:calc(100dvh - 84px); }
  .viewer-panel { top:100px; height:calc(100dvh - 116px); }
  .viewer-controls { gap:10px; padding:0 13px 12px; }
  .control-title,.viz-modes,.display-options,.display-options legend,
  .vertical-sections,.horizontal-sections,.flow-controls,.canvas-note,
  .mesh-figure-toolbar,.temperature-chart-toolbar,.convergence-chart-toolbar { font-size:14px; }
  .viz-modes label { flex-basis:142px; min-height:54px; padding:9px 10px; }
  .display-options button,.vertical-sections button,.horizontal-sections button,
  .mesh-figure-toolbar button,.temperature-chart-toolbar button,.convergence-chart-toolbar button { padding:7px 10px; font-size:13px; }
  button { padding:9px 13px; font-size:14px; }
  .flow-controls { min-height:58px; gap:11px; padding:9px 13px; }
  .canvas-note { padding:8px 12px; }
  aside { padding-right:4px; }
  .card { padding:18px 19px; margin-bottom:13px; }
  .card h2 { margin-bottom:14px; padding-bottom:10px; font-size:17px; }
  .card p,dl { font-size:15px; }
  .result-badge { padding:5px 9px; font-size:12px; }
  .kpi-grid { gap:9px; }
  .kpi-grid article { padding:13px; }
  .kpi-grid span,.kpi-grid small { font-size:12px; }
  .kpi-grid strong { margin:4px 0 3px; font-size:22px; }
  .mesh-run-card label { font-size:13px; }
  .mesh-run-card select { padding:10px; font-size:14px; }
  .card.detail-card { padding:0; margin-bottom:9px; }
  .detail-card summary { padding:12px 48px 12px 19px; font-size:17px; }
  .detail-card summary::after { right:19px; font-size:23px; }
  .detail-body { padding:14px 19px 17px; }
  .cell-grid button,.figure-grid button { font-size:12px; }
  footer { min-height:40px; padding:8px 24px 10px; font-size:13px; }
}
@media (max-width:1180px) {
  main { grid-template-columns:minmax(0,1fr) 300px; }
  .control-head { align-items:flex-start; flex-direction:column; gap:7px; }
  .display-options { justify-content:flex-start; }
}
@media (max-width:1050px) {
  header { height:auto; min-height:76px; gap:12px; padding:14px 18px; }
  .status { max-width:48%; text-align:right; }
  main { grid-template-columns:1fr; padding:10px; }
  .viewer-panel { position:relative; top:auto; width:100%; height:min(78dvh,760px); min-height:620px; }
  aside { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; padding-right:0; }
  .result-card { grid-column:1/-1; }
  .card { margin-bottom:0; }
}
@media (max-width:680px) {
  header { align-items:flex-start; flex-direction:column; min-height:0; gap:7px; padding:10px 12px; }
  .brand { width:100%; justify-content:space-between; gap:7px; }
  .brand span { font-size:8px; letter-spacing:.09em; }
  .status { width:100%; max-width:100%; text-align:left; }
  main { gap:8px; padding:6px; }
  .viewer-panel { height:82dvh; min-height:620px; }
  .viewer-controls { padding:0 6px 7px; }
  .viz-modes label { flex-basis:128px; }
  .control-head { width:100%; overflow-x:auto; }
  .toolbar,.display-options { min-width:max-content; }
  .flow-controls { margin:0 6px 4px; display:grid; grid-template-columns:auto minmax(110px,1fr) auto; gap:6px; }
  .flow-controls input[type="range"] { width:100%; }
  .flow-controls > label { display:none; }
  #flow-play { grid-column:1; }
  #flow-seek { grid-column:2; }
  #flow-rate { grid-column:3; }
  #head-rate { grid-column:1; grid-row:2; }
  #flow-duration,#flow-sampling,#current-mode { display:none; }
  #flow-time { grid-column:2/-1; grid-row:2; text-align:center; }
  #port-state { grid-column:1/-1; text-align:center; }
  .canvas-note { margin:0 6px 6px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  aside { grid-template-columns:1fr; }
}
'''


JAVASCRIPT = r'''
"use strict";
const model = window.CFD_TWIN_MODEL;
const tr = value => window.CFDTwinI18n?.t(value) ?? value;
document.getElementById("mesh-tab").hidden=!model.mesh_only_ready;
const initialParams=new URLSearchParams(window.location.search);
if(initialParams.get("capture")==="1")document.documentElement.classList.add("capture-mode");
const canvas = document.getElementById("scene");
const ctx = canvas.getContext("2d");
const sceneStage = document.querySelector(".scene-stage");
const overlayCanvas = document.getElementById("scene-overlay");
const overlayCtx = overlayCanvas.getContext("2d");
const viewerControls = document.querySelector(".viewer-controls");
const meshRuns=model.mesh_runs||[];
const meshRunText=(run,key)=>{
  if(!run)return "";
  // The language toggle updates the page without reloading, so read its
  // current state instead of the initial query string captured at startup.
  const language=(window.CFDTwinI18n?.getLanguage()||new URLSearchParams(window.location.search).get("lang")||document.documentElement.lang||"ko").toLowerCase();
  return language.startsWith("en")?(run[`${key}_en`]||run[key]||""):(run[key]||run[`${key}_en`]||"");
};
// A freshly meshed case is selectable before its thermal solve completes.
// In that state show its own polyMesh without borrowing another run's CFD.
const selectableMeshRuns=meshRuns.filter(run=>run.mesh_ready||run.data_ready);
const meshWireframes=model.mesh_wireframes||{};
const requestedMeshRunRaw=initialParams.get("mesh");
const requestedMeshRun=requestedMeshRunRaw==="single"&&selectableMeshRuns.some(run=>run.id==="exact_phi23")
  ?"exact_phi23":requestedMeshRunRaw;
let activeMeshRunId=selectableMeshRuns.some(run=>run.id===requestedMeshRun)?requestedMeshRun:(selectableMeshRuns[0]?.id||meshRuns[0]?.id||"precision");
// A comparison bundle may contain independently generated CADs.  Select the
// matching geometry together with the field data so a changed port diameter
// or centre is not drawn with another run's enclosure.
const activeGeometry=(model.geometry_by_mesh||{})[activeMeshRunId]||{};
const sceneCells=activeGeometry.cells||model.cells;
const scenePorts=activeGeometry.ports||model.ports;
const sceneOutline=activeGeometry.outline_mm||model.outline_mm;
const transformByMesh=model.spatial_transform_by_mesh||{};
const activeSpatialTransform=Object.prototype.hasOwnProperty.call(transformByMesh,activeMeshRunId)?transformByMesh[activeMeshRunId]:model.spatial_transform;
const activeCalculationNote=(model.calculation_note_by_mesh||{})[activeMeshRunId]||model.calculation_note;
const activeDerived=(model.derived_by_mesh||{})[activeMeshRunId]||model.derived;
const activeInputs=(model.inputs_by_mesh||{})[activeMeshRunId]||model.inputs;
const flow=(model.interactive_cfd_by_mesh||{})[activeMeshRunId]||{enabled:false};
const flowLegendContract=model.flow_legend_contract||{};
const temperatureGraphs=flow.temperature_graphs||null;
const energyBalance=(model.energy_balance_by_mesh||{})[activeMeshRunId]||model.energy_balance||{available:false};
const convergenceBundle=(model.convergence_by_mesh||{})[activeMeshRunId]||model.convergence||{available:false};
const validatedConvergenceHistories=convergenceBundle.histories?.length?convergenceBundle.histories:[convergenceBundle];
const failedConvergenceHistories=(model.failed_convergence_histories||[]).filter(history=>history?.available);
const convergenceHistories=[...validatedConvergenceHistories,...failedConvergenceHistories];
let convergence=convergenceHistories.find(history=>history.id===convergenceBundle.default_history_id)||convergenceHistories[0]||{available:false};
const meshRunSelect=document.getElementById("mesh-run-select");
const meshRunDetail=document.getElementById("mesh-run-detail");
const meshRunNote=document.getElementById("mesh-run-note");
const sections = flow.sections || {};
const directXYVelocity = flow.direct_xy_velocity || null;
const flowControls = document.getElementById("flow-controls");
const flowPlay = document.getElementById("flow-play");
const flowSeek = document.getElementById("flow-seek");
const flowRate = document.getElementById("flow-rate");
const headRate = document.getElementById("head-rate");
const flowDuration = document.getElementById("flow-duration");
const flowTime = document.getElementById("flow-time");
const flowSampling = document.getElementById("flow-sampling");
const portState = document.getElementById("port-state");
const dataLoadStatus = document.getElementById("data-load-status");
const modeFieldset = document.getElementById("viz-modes");
const modeInputs = Array.from(document.querySelectorAll('input[name="viz-mode"]'));
const controlHead = document.querySelector(".control-head");
const displayOptions = document.getElementById("display-options");
const smoothSectionInput = document.getElementById("smooth-section");
const section3DInput = document.getElementById("section-3d");
const section3DOption = document.getElementById("section-3d-option");
const horizontalSections = document.getElementById("horizontal-sections");
const horizontalSectionInputs = Array.from(document.querySelectorAll('input[name="horizontal-section"]'));
const horizontalSectionState = document.getElementById("horizontal-section-state");
const requestedHorizontalCut=initialParams.get("zcut");
if(requestedHorizontalCut){const requestedCutInput=horizontalSectionInputs.find(input=>input.value===requestedHorizontalCut);if(requestedCutInput){for(const input of horizontalSectionInputs)input.checked=input===requestedCutInput;}}
const verticalSections = document.getElementById("vertical-sections");
const verticalSectionInputs = Array.from(document.querySelectorAll('input[name="vertical-section"]'));
const verticalSectionPlane = document.getElementById("vertical-section-plane");
const verticalSectionState = document.getElementById("vertical-section-state");
const requestedVerticalPlane=initialParams.get("vplane");
if(["yz","xz"].includes(requestedVerticalPlane))verticalSectionPlane.value=requestedVerticalPlane;
const requestedVerticalCut=initialParams.get("vcut");
if(requestedVerticalCut){const requestedCutInput=verticalSectionInputs.find(input=>input.value===requestedVerticalCut);if(requestedCutInput){for(const input of verticalSectionInputs)input.checked=input===requestedCutInput;}}
const currentMode = document.getElementById("current-mode");
const canvasNote = document.getElementById("canvas-note");
const cellToast = document.getElementById("cell-toast");
const dashboard={
  outlet:document.getElementById("kpi-outlet-temperature"),cellMaximum:document.getElementById("kpi-cell-maximum"),
  cellHotspot:document.getElementById("kpi-cell-hotspot"),cellHotspotLocation:document.getElementById("kpi-cell-hotspot-location"),
  inletVelocity:document.getElementById("kpi-inlet-velocity"),pressureDrop:document.getElementById("kpi-pressure-drop"),
  inletMassFlow:document.getElementById("kpi-inlet-mass-flow"),outletMassFlow:document.getElementById("kpi-outlet-mass-flow"),
  outletTime:document.getElementById("kpi-outlet-time"),cellTime:document.getElementById("kpi-cell-time"),
  htcAverage:document.getElementById("kpi-htc-average"),htcMinimum:document.getElementById("kpi-htc-minimum"),htcMaximum:document.getElementById("kpi-htc-maximum"),
  htcCards:[document.getElementById("kpi-htc-average-card"),document.getElementById("kpi-htc-minimum-card"),document.getElementById("kpi-htc-maximum-card")]
};
const visibility={cells:true,flow:true,particles:true,section:true,grid:false,mesh:false,enclosure:true,temperatureLegend:true,velocityLegend:true,massFlowLegend:true,pressureLegend:true,heatTransferLegend:true,ports:true,extrema:true};
const optionInputs={
  cells:document.getElementById("show-cells"),flow:document.getElementById("show-flow"),
  particles:document.getElementById("show-particles"),section:document.getElementById("show-section"),
  grid:document.getElementById("show-grid"),mesh:document.getElementById("show-mesh"),enclosure:document.getElementById("show-enclosure"),
  temperatureLegend:document.getElementById("show-temperature-legend"),velocityLegend:document.getElementById("show-velocity-legend"),massFlowLegend:document.getElementById("show-mass-flow-legend"),pressureLegend:document.getElementById("show-pressure-legend"),heatTransferLegend:document.getElementById("show-heat-transfer-legend"),ports:document.getElementById("show-port-callouts"),extrema:document.getElementById("show-extrema-callouts")
};
// Overlay dimensions and default anchors changed with the large-screen UI.
// Use a new key so positions saved for the smaller boxes do not stack the
// enlarged legend and port cards on top of one another.
const overlayStorageKey="yuFluidViewerOverlayPositionsV3";
let overlayPositions={};
try{overlayPositions=JSON.parse(localStorage.getItem(overlayStorageKey)||"{}");}catch(_error){overlayPositions={};}
let overlayHitboxes=[],overlayDrag=null;
function overlayBoxPosition(id,w,h,boxWidth,boxHeight,defaultLeft,defaultTop){
  const maxLeft=Math.max(0,w-boxWidth-8),maxTop=Math.max(0,h-boxHeight-8),saved=overlayPositions[id];
  return {
    left:clamp(Number.isFinite(saved?.x)?saved.x*maxLeft:defaultLeft,0,maxLeft),
    top:clamp(Number.isFinite(saved?.y)?saved.y*maxTop:defaultTop,0,maxTop)
  };
}
function registerOverlayHitbox(id,left,top,width,height){overlayHitboxes.push({id,left,top,width,height});}
function saveOverlayPositions(){try{localStorage.setItem(overlayStorageKey,JSON.stringify(overlayPositions));}catch(_error){}}
function overlayAt(x,y){return [...overlayHitboxes].reverse().find(box=>x>=box.left&&x<=box.left+box.width&&y>=box.top&&y<=box.top+box.height)||null;}
// PPT slide 6 completion-model view: inlet at the upper-right and outlet at
// the lower-left. This avoids making the confirmed diagonal placement look
// reversed when the viewer first opens.
let yaw = -0.96, pitch = 0.52, zoom = 1, dragging = false, lastX = 0, lastY = 0;
let highlightedCellId=null,cellHighlightTimer=null,cellExtremaCache={frame:-1,value:null};
const cellTemperatureExtremaCache=new Map();
const requestedCell=initialParams.get("cell");
if(sceneCells.some(cell=>cell.id===requestedCell))highlightedCellId=requestedCell;
const requestedModeRaw=initialParams.get("mode");
const requestedMode=requestedModeRaw==="xy-mass-flux"?"xy-mass-flow":requestedModeRaw==="xz-mass-flux"?"xz-mass-flow":requestedModeRaw;
if(requestedMode){const requestedInput=modeInputs.find(input=>input.value===requestedMode);if(requestedInput){for(const input of modeInputs)input.checked=input===requestedInput;}}
const requestedTime=Number(initialParams.get("t"));
const overlayTargetFps=90;
let simulationTime = Number.isFinite(requestedTime)?Math.max(0,Math.min(1800,requestedTime)):0, flowPlaying = Boolean(flow.enabled)&&initialParams.get("autoplay")!=="0", previousFrameTime = 0, sceneRenderAccumulator = 0, overlayRenderAccumulator = 0, lastSceneFrame = -1, lastFlowClockSecond = -1;
let visualMode = modeInputs.find(input=>input.checked)?.value || "combined";
const box = model.enclosure_mm;
const centre = [box.x/2, box.y/2, box.z/2];
const actualOneSecond=flow.actual_one_second||null;
const actualDatasets=new Map();
const actualDatasetPromises=new Map();
const actualDatasetErrors=new Map();

function setDataLoadStatus(message,state="loading"){
  dataLoadStatus.textContent=message;
  dataLoadStatus.classList.toggle("error",state==="error");
  dataLoadStatus.hidden=!message;
}
function base64Bytes(payload){
  const binary=atob(payload),bytes=new Uint8Array(binary.length);
  for(let index=0;index<binary.length;index++)bytes[index]=binary.charCodeAt(index);
  return bytes;
}
async function portableDatasetBuffer(name,specification){
  window.CFD_TWIN_PACKED_DATA=window.CFD_TWIN_PACKED_DATA||{};
  if(!window.CFD_TWIN_PACKED_DATA[name])await new Promise((resolve,reject)=>{
    const script=document.createElement("script");
    script.src=specification.portable_file;
    script.onload=resolve;
    script.onerror=()=>reject(new Error(`${specification.portable_file}: local data script missing`));
    document.head.appendChild(script);
  });
  const packed=window.CFD_TWIN_PACKED_DATA[name];
  if(!packed)throw new Error(`${name}: local packed payload missing`);
  if(typeof DecompressionStream!=="function")throw new Error("이 브라우저는 로컬 CFD 압축 해제를 지원하지 않습니다. 최신 Chrome, Edge 또는 Safari를 사용하십시오.");
  const compressed=base64Bytes(packed.payload);
  const stream=new Blob([compressed]).stream().pipeThrough(new DecompressionStream("gzip"));
  const buffer=await new Response(stream).arrayBuffer();
  delete window.CFD_TWIN_PACKED_DATA[name];
  return buffer;
}
async function actualDatasetBuffer(name,specification){
  if(location.protocol==="file:")return portableDatasetBuffer(name,specification);
  const response=await fetch(specification.file,{cache:"no-store"});
  if(!response.ok)throw new Error(`${specification.file}: HTTP ${response.status}`);
  return response.arrayBuffer();
}

async function loadActualDataset(name){
  if(!actualOneSecond)return null;
  if(actualDatasets.has(name))return actualDatasets.get(name);
  if(actualDatasetPromises.has(name))return actualDatasetPromises.get(name);
  const specification=actualOneSecond.datasets?.[name];
  if(!specification)return null;
  actualDatasetErrors.delete(name);
  const promise=actualDatasetBuffer(name,specification).then(buffer=>{
    const encoded=specification.dtype==="float32-little-endian"?new Float32Array(buffer):new Uint16Array(buffer);
    const expected=specification.frame_count*specification.valid_count;
    if(encoded.length!==expected)throw new Error(`${name}: expected ${expected} samples, found ${encoded.length}`);
    const lookup=new Int32Array(specification.point_count);lookup.fill(-1);
    specification.valid_indices.forEach((pointIndex,validIndex)=>{lookup[pointIndex]=validIndex;});
    const dataset={specification,encoded,lookup};actualDatasets.set(name,dataset);return dataset;
  }).catch(error=>{
    console.error(error);actualDatasetErrors.set(name,String(error?.message||error));return null;
  });
  actualDatasetPromises.set(name,promise);return promise;
}
function actualDatasetValue(name,pointIndex){
  const dataset=actualDatasets.get(name);if(!dataset)return null;
  const validIndex=dataset.lookup[pointIndex];if(validIndex<0)return null;
  const step=Number(dataset.specification.time_step_s)||1,first=Number(dataset.specification.first_time_s)||0;
  const frame=Math.round((simulationTime-first)/step);
  if(frame<0||frame>=dataset.specification.frame_count)return null;
  const encoded=dataset.encoded[frame*dataset.specification.valid_count+validIndex];
  if(dataset.specification.dtype==="float32-little-endian")return Number.isFinite(encoded)?encoded:null;
  const scale=actualOneSecond.temperature_encoding;
  return scale.minimum_k+encoded*scale.quantum_k-273.15;
}

function dot(a,b){ return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]; }
function normalised(vector){
  const length=Math.hypot(...vector)||1;return vector.map(value=>value/length);
}
function cameraBasis(){
  const cp=Math.cos(pitch), sp=Math.sin(pitch), cy=Math.cos(yaw), sy=Math.sin(yaw);
  const direction=[cp*cy,cp*sy,sp];
  const right=[-sy,cy,0];
  const up=[-sp*cy,-sp*sy,cp];
  return {direction,right,up};
}
function projected(point,basis,scale,cx,cy){
  const v=[point[0]-centre[0],point[1]-centre[1],point[2]-centre[2]];
  return {x:cx+dot(v,basis.right)*scale,y:cy-dot(v,basis.up)*scale,d:dot(v,basis.direction)};
}
function cylinderFaces(cell,segments=24,axialSegments=1){
  const faces=[], lower=cell.z-cell.length/2, upper=cell.z+cell.length/2;
  const rings=[];
  for(let axial=0;axial<=axialSegments;axial++){
    const z=lower+cell.length*axial/axialSegments,ring=[];
    for(let i=0;i<segments;i++){
      const a=2*Math.PI*i/segments;
      ring.push([cell.x+cell.radius*Math.cos(a),cell.y+cell.radius*Math.sin(a),z]);
    }
    rings.push(ring);
  }
  for(let axial=0;axial<axialSegments;axial++)for(let i=0;i<segments;i++){
    const j=(i+1)%segments;
    faces.push({points:[rings[axial][i],rings[axial][j],rings[axial+1][j],rings[axial+1][i]],kind:`${cell.kind||"cell"}-side`,id:cell.id,surfaceIndex:axial*segments+i,angularIndex:i,axialIndex:axial});
  }
  const bottomCentre=[cell.x,cell.y,lower],topCentre=[cell.x,cell.y,upper],sideCount=segments*axialSegments;
  for(let i=0;i<segments;i++){
    const j=(i+1)%segments;
    faces.push({points:[bottomCentre,rings[0][j],rings[0][i]],kind:`${cell.kind||"cell"}-cap`,id:cell.id,surfaceIndex:sideCount+i});
    faces.push({points:[topCentre,rings[axialSegments][i],rings[axialSegments][j]],kind:`${cell.kind||"cell"}-cap`,id:cell.id,surfaceIndex:sideCount+segments+i});
  }
  return faces;
}
const cellSurface=flow.cell_surface||null;
const cellSurfaceHeatTransfer=flow.cell_surface_heat_transfer||null;
const waterTemperatureRunIds=new Set(["precision","single","exact_phi21","exact_phi23","exact_phi25"]);
function sharedTemperatureRange(){
  // Display scales only: preserve every original CFD temperature sample.
  // The four original water results use the supplied Fluent reference. The
  // additional Ø25 water display variant and dielectric fluids share 23 C.
  const selectedRange=activeMeshRun()?.temperature_legend_range_c;
  if(Array.isArray(selectedRange)&&selectedRange.length===2&&selectedRange[1]>selectedRange[0])return selectedRange;
  const inputs=model.inputs_by_mesh?.[activeMeshRunId]||model.inputs||{};
  const fluidCp=Number(inputs.fluid_cp_J_kgK);
  const dielectric=Number.isFinite(fluidCp)&&fluidCp>0?fluidCp<3000:!waterTemperatureRunIds.has(activeMeshRunId);
  return dielectric?[19.500,23.000]:[19.908,21.044];
}
const cellFaces=sceneCells.flatMap(cell=>cylinderFaces(cell,cellSurface?.angular_segments||24,cellSurface?.axial_segments||1));
const portFaces=scenePorts.flatMap(port=>cylinderFaces(port,24,1));
const faces=cellFaces.concat(portFaces);
const cellById=new Map(sceneCells.map(cell=>[cell.id,cell]));
const entityById=new Map([...sceneCells,...scenePorts].map(entity=>[entity.id,entity]));
let projectedFaceCache={key:null,faces:null};
const vertices=sceneOutline.map(p=>[p[0],p[1],0]).concat(sceneOutline.map(p=>[p[0],p[1],box.z]));
const n=sceneOutline.length,edges=[];
for(let i=0;i<n;i++){const j=(i+1)%n;edges.push([i,j],[i+n,j+n],[i,i+n]);}

function clamp(value,low=0,high=1){return Math.max(low,Math.min(high,value));}
function velocityDisplayRange(){
  const range=flow.fixed_velocity_legend_range_m_s||flowLegendContract.fixed_velocity_legend_range_m_s;
  if(Array.isArray(range)&&range.length===2&&Number.isFinite(range[1])&&range[1]>range[0])return range;
  return [0,Math.max(1e-9,Number(flow.velocity_max_m_s)||0)];
}
function velocityDisplayMaximum(){return Math.max(1e-9,velocityDisplayRange()[1]);}
function fluidDensity(){return Math.max(0,Number(activeInputs.fluid_density_kg_m3)||0);}
function massFlowDisplayRange(){
  const range=flow.fixed_mass_flow_legend_range_kg_s||flowLegendContract.fixed_mass_flow_legend_range_kg_s;
  return Array.isArray(range)&&range.length===2&&Number.isFinite(range[1])&&range[1]>range[0]?range:[0,.0006];
}
function pressureDisplayRange(){
  const range=flow.fixed_pressure_legend_range_pa||flowLegendContract.fixed_pressure_legend_range_pa;
  return Array.isArray(range)&&range.length===2&&Number.isFinite(range[0])&&Number.isFinite(range[1])&&range[1]>range[0]?range:null;
}
function pressureAvailable(){
  return Boolean(flow.pressure_available)&&Object.values(sections).some(section=>Array.isArray(section?.pressure_gauge_pa));
}
function jet(value){
  const t=clamp(value),r=clamp(1.5-Math.abs(4*t-3)),g=clamp(1.5-Math.abs(4*t-2)),b=clamp(1.5-Math.abs(4*t-1));
  return [Math.round(255*r),Math.round(255*g),Math.round(255*b)];
}
// Sampled from the supplied Fluent temperature legend at its labelled levels.
// Keep the velocity palette separate; only temperature views use this ramp.
const temperaturePaletteStops=[
  [0,[0,42,249]],[.13026,[71,119,239]],[.26,[88,195,221]],
  [.38974,[122,241,142]],[.52,[192,243,84]],[.65026,[241,195,78]],
  [.78,[229,110,42]],[.91026,[199,60,25]],[1,[186,42,19]]
];
function temperaturePalette(value){
  const t=clamp(value);
  for(let index=1;index<temperaturePaletteStops.length;index++){
    const [upper,high]=temperaturePaletteStops[index], [lower,low]=temperaturePaletteStops[index-1];
    if(t<=upper){const weight=(t-lower)/(upper-lower);return low.map((channel,axis)=>Math.round(channel+weight*(high[axis]-channel)));}
  }
  return temperaturePaletteStops.at(-1)[1];
}
function cellSurfacePalette(value){
  // The rendered battery and the legend must use the exact same colour map.
  // Do not apply a separate saturation or lighting correction here: doing so
  // makes a colour on the 3-D model correspond to a different legend value.
  return temperaturePalette(value);
}
function scalarPalette(value,field){return field==="temperature"?temperaturePalette(value):jet(value);}
function activeMeshRun(){return meshRuns.find(run=>run.id===activeMeshRunId)||meshRuns[0]||null;}
function activeMeshWireframe(){return meshWireframes[activeMeshRunId]||null;}
let projectedMeshCache={key:null,points:null};
function projectedMeshPoints(mesh,basis,scale,cx,cy,w,h){
  if(!mesh?.points_mm)return null;
  const key=[activeMeshRunId,yaw,pitch,zoom,w,h,scale,cx,cy,mesh.point_count].map(value=>String(value)).join("|");
  if(projectedMeshCache.key===key)return projectedMeshCache.points;
  const points=mesh.points_mm.map(point=>projected(point,basis,scale,cx,cy));
  projectedMeshCache={key,points};return points;
}
function drawActualMeshWireframe(basis,scale,cx,cy,w,h){
  const mesh=activeMeshWireframe();
  if(!visibility.mesh||!mesh?.groups?.length)return;
  const points=projectedMeshPoints(mesh,basis,scale,cx,cy,w,h);if(!points)return;
  const styles={
    battery:["rgba(116,225,247,.70)",.42],
    enclosure:["rgba(164,195,216,.34)",.38],
    port:["rgba(255,189,91,.78)",.58]
  };
  ctx.save();ctx.globalCompositeOperation="source-over";
  for(const group of mesh.groups){
    const style=styles[group.kind]||["rgba(180,210,226,.45)",.4];
    ctx.beginPath();
    for(const edge of group.edges){const a=points[edge[0]],b=points[edge[1]];ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);}
    ctx.strokeStyle=style[0];ctx.lineWidth=style[1];ctx.stroke();
  }
  ctx.restore();
}
const geometryFrameStroke="rgba(164,195,216,.67)",geometryFrameLineWidth=1.25;
function drawTemperaturePortOutlines(basis,scale,cx,cy){
  // In the cell-only temperature view, retain the port geometry as clean
  // outlines without filling it or implying a cell-surface temperature there.
  // Use precisely the same stroke as the enclosure frame for both ports.
  ctx.save();ctx.strokeStyle=geometryFrameStroke;ctx.lineWidth=geometryFrameLineWidth;
  for(const port of scenePorts){
    const bottom=port.z-port.length/2,top=port.z+port.length/2,segments=32;
    for(const z of [bottom,top]){
      ctx.beginPath();
      for(let index=0;index<=segments;index++){
        const angle=2*Math.PI*index/segments;
        const point=projected([port.x+port.radius*Math.cos(angle),port.y+port.radius*Math.sin(angle),z],basis,scale,cx,cy);
        if(index===0)ctx.moveTo(point.x,point.y);else ctx.lineTo(point.x,point.y);
      }
      ctx.stroke();
    }
    for(let index=0;index<4;index++){
      const angle=Math.PI*index/2;
      const x=port.x+port.radius*Math.cos(angle),y=port.y+port.radius*Math.sin(angle);
      const a=projected([x,y,bottom],basis,scale,cx,cy),b=projected([x,y,top],basis,scale,cx,cy);
      ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
    }
  }
  ctx.restore();
}
function faceNormal(face){
  const entity=entityById.get(face.id);if(!entity)return [0,0,1];
  if(face.kind.endsWith("-cap")){
    return [0,0,face.points[0][2]>=entity.z?1:-1];
  }
  const centreX=face.points.reduce((sum,point)=>sum+point[0],0)/face.points.length;
  const centreY=face.points.reduce((sum,point)=>sum+point[1],0)/face.points.length;
  return normalised([centreX-entity.x,centreY-entity.y,0]);
}
function faceVisible(face,basis){
  return dot(faceNormal(face),basis.direction)>-0.02;
}
function fluentSurfaceShadow(face,basis){
  // Fluent applies scene lighting after mapping the scalar value through the
  // legend. Keep the legend/data mapping untouched and reproduce only that
  // geometric darkening on the 3-D cell and port surfaces.
  const light=normalised([
    basis.direction[0]+.48*basis.up[0]-.22*basis.right[0],
    basis.direction[1]+.48*basis.up[1]-.22*basis.right[1],
    basis.direction[2]+.48*basis.up[2]-.22*basis.right[2]
  ]);
  const diffuse=clamp(dot(faceNormal(face),light),0,1);
  return .08+.30*(1-diffuse);
}
function projectedFaces(basis,scale,cx,cy,w,h){
  const key=[yaw,pitch,zoom,w,h,scale,cx,cy].map(value=>Number(value).toFixed(5)).join("|");
  if(projectedFaceCache.key===key&&projectedFaceCache.faces)return projectedFaceCache.faces;
  // 면 전체를 한꺼번에 정렬하면 서로 다른 원통의 앞/뒤 면이 섞여 한 셀이
  // 중간부터 투명해 보인다. 원통 중심 깊이로 몸체를 먼저 정렬한 뒤, 같은
  // 몸체 안에서만 면 깊이정렬을 수행한다.
  const groups=new Map();
  for(const face of faces){
    if(!faceVisible(face,basis))continue;
    const points=face.points.map(point=>projected(point,basis,scale,cx,cy));
    const item={face,points,depth:points.reduce((sum,point)=>sum+point.d,0)/points.length};
    if(!groups.has(face.id)){
      const entity=entityById.get(face.id),entityPoint=[entity.x,entity.y,entity.z];
      groups.set(face.id,{depth:projected(entityPoint,basis,scale,cx,cy).d,faces:[]});
    }
    groups.get(face.id).faces.push(item);
  }
  const visible=[...groups.values()]
    .sort((a,b)=>a.depth-b.depth)
    .flatMap(group=>group.faces.sort((a,b)=>a.depth-b.depth));
  projectedFaceCache={key,faces:visible};return visible;
}
let directXYVelocityCache=null;
const sectionRasterCache=new WeakMap();
function sectionRaster(section,field,range){
  let fields=sectionRasterCache.get(section);if(!fields){fields={};sectionRasterCache.set(section,fields);}
  let raster=fields[field];
  if(!raster){
    const canvas=document.createElement("canvas");canvas.width=section.columns;canvas.height=section.rows;
    const context=canvas.getContext("2d");raster={canvas,context,pixels:context.createImageData(section.columns,section.rows),fixedReady:false};fields[field]=raster;
  }
  if(field!=="temperature"&&raster.fixedReady)return raster.canvas;
  raster.pixels.data.fill(0);
  // The smooth XY texture is placed with screen X decreasing and screen Y
  // increasing.  Reflected runs reverse the sampled Y row step, so derive
  // both image axes from the section coordinates just like the exact tiles.
  const flipColumn=section.column_step_mm[0]>0;
  const flipRow=section.row_step_mm[1]<0;
  for(let row=0;row<section.rows;row++)for(let column=0;column<section.columns;column++){
    const index=row*section.columns+column,value=sectionFieldValue(section,field,index);
    if(!Number.isFinite(value))continue;
    const imageRow=flipRow?section.rows-1-row:row,imageColumn=flipColumn?section.columns-1-column:column;
    const colour=scalarPalette((value-range[0])/Math.max(1e-12,range[1]-range[0]),field),pixel=4*(imageRow*section.columns+imageColumn);
    raster.pixels.data[pixel]=colour[0];raster.pixels.data[pixel+1]=colour[1];raster.pixels.data[pixel+2]=colour[2];raster.pixels.data[pixel+3]=255;
  }
  raster.context.putImageData(raster.pixels,0,0);if(field!=="temperature")raster.fixedReady=true;return raster.canvas;
}
function buildDirectXYVelocityCache(){
  if(directXYVelocityCache||!directXYVelocity)return directXYVelocityCache;
  const factor=8,width=Math.round(box.x*factor),height=Math.round(box.y*factor);
  const target=document.createElement("canvas");target.width=width;target.height=height;
  const targetContext=target.getContext("2d"),range=velocityDisplayRange();
  targetContext.imageSmoothingEnabled=false;
  for(let index=0;index<directXYVelocity.triangles.length;index++){
    const triangle=directXYVelocity.triangles[index],value=directXYVelocity.velocity_m_s[index];
    if(!Number.isFinite(value))continue;
    const colour=jet((value-range[0])/Math.max(1e-12,range[1]-range[0]));
    const points=triangle.map(pointIndex=>directXYVelocity.points_xy_mm[pointIndex]);
    targetContext.beginPath();
    targetContext.moveTo((box.x-points[0][0])*factor,points[0][1]*factor);
    targetContext.lineTo((box.x-points[1][0])*factor,points[1][1]*factor);
    targetContext.lineTo((box.x-points[2][0])*factor,points[2][1]*factor);
    targetContext.closePath();targetContext.fillStyle=`rgb(${colour[0]},${colour[1]},${colour[2]})`;targetContext.fill();
  }
  directXYVelocityCache=target;return target;
}
function drawXYPlaneTexture(image,section,basis,scale,cx,cy,alpha=.86){
  if(!image||!section)return false;
  const z=section.origin_mm[2];
  const topLeft=projected([box.x,0,z],basis,scale,cx,cy);
  const topRight=projected([0,0,z],basis,scale,cx,cy);
  const bottomLeft=projected([box.x,box.y,z],basis,scale,cx,cy);
  ctx.save();ctx.globalAlpha=alpha;ctx.imageSmoothingEnabled=true;ctx.imageSmoothingQuality="high";
  ctx.transform(
    (topRight.x-topLeft.x)/image.width,(topRight.y-topLeft.y)/image.width,
    (bottomLeft.x-topLeft.x)/image.height,(bottomLeft.y-topLeft.y)/image.height,
    topLeft.x,topLeft.y
  );
  ctx.drawImage(image,0,0);ctx.restore();return true;
}
function drawDirectXYVelocity3D(section,basis,scale,cx,cy){
  const image=buildDirectXYVelocityCache();
  if(!drawXYPlaneTexture(image,section,basis,scale,cx,cy,.9))return false;
  ctx.fillStyle="rgba(232,242,250,.96)";ctx.font="700 10px ui-monospace,monospace";
  ctx.fillText(`PRECISION MESH ACTUAL VALUES · ${directXYVelocity.triangle_count.toLocaleString()} CUT TRIANGLES · 3D · NO PADDING`,96,46);
  return true;
}
function directXYLayout(w,h){
  const plotLeft=96,plotRight=Math.max(plotLeft+160,w-28),plotTop=58,plotBottom=Math.max(plotTop+160,h-104);
  const plotWidth=plotRight-plotLeft,plotHeight=plotBottom-plotTop;
  const scale=Math.min(plotWidth/box.x,plotHeight/box.y)*zoom;
  const width=box.x*scale,height=box.y*scale;
  return {
    left:plotLeft+(plotWidth-width)/2,
    top:plotTop+(plotHeight-height)/2,
    width,height,scale,plotLeft,plotTop,plotRight,plotBottom
  };
}
function drawDirectXYGeometry(layout){
  ctx.strokeStyle="rgba(205,225,238,.86)";ctx.lineWidth=1.1;ctx.beginPath();
  for(let index=0;index<sceneOutline.length;index++){
    const point=sceneOutline[index],x=layout.left+(box.x-point[0])*layout.scale,y=layout.top+point[1]*layout.scale;
    if(index===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
  }
  ctx.closePath();ctx.stroke();
  for(const cell of sceneCells){
    const x=layout.left+(box.x-cell.x)*layout.scale,y=layout.top+cell.y*layout.scale,radius=cell.radius*layout.scale;
    ctx.beginPath();ctx.arc(x,y,radius,0,2*Math.PI);ctx.fillStyle="rgba(7,16,27,1)";ctx.fill();
    ctx.strokeStyle="rgba(218,231,240,.94)";ctx.lineWidth=1;ctx.stroke();
  }
}
function drawDirectXYVelocity(w,h,showScalar=true){
  const image=buildDirectXYVelocityCache();if(!image)return false;
  const layout=directXYLayout(w,h);
  ctx.save();ctx.beginPath();ctx.rect(layout.plotLeft,layout.plotTop,layout.plotRight-layout.plotLeft,layout.plotBottom-layout.plotTop);ctx.clip();
  ctx.fillStyle="rgba(7,16,27,.98)";ctx.fillRect(layout.left,layout.top,layout.width,layout.height);
  if(showScalar){ctx.imageSmoothingEnabled=true;ctx.drawImage(image,layout.left,layout.top,layout.width,layout.height);}
  drawDirectXYGeometry(layout);
  ctx.restore();
  ctx.fillStyle="rgba(232,242,250,.96)";ctx.font="700 10px ui-monospace,monospace";
  ctx.fillText(`PRECISION MESH ACTUAL VALUES · ${directXYVelocity.triangle_count.toLocaleString()} CUT TRIANGLES · NO PADDING`,layout.plotLeft,layout.plotTop-12);
  ctx.fillStyle="rgba(145,168,188,.98)";ctx.font="650 10px system-ui";
  ctx.fillText("FROZEN-FLOW U(x) · 전체 유동장 공통 색상범위 · 이동 마커 속도는 |U|와 공통 배율에 비례",layout.plotLeft,layout.plotBottom+22);
  return true;
}
function drawDirectXYGrid(section,field,w,h,showScalar=true){
  if(!section)return false;
  const layout=directXYLayout(w,h),range=field==="temperature"?temperatureSectionRange(section):field==="pressure"?pressureDisplayRange():field==="mass-flow"?massFlowDisplayRange():velocityDisplayRange();
  ctx.save();ctx.beginPath();ctx.rect(layout.plotLeft,layout.plotTop,layout.plotRight-layout.plotLeft,layout.plotBottom-layout.plotTop);ctx.clip();
  ctx.fillStyle="rgba(7,16,27,.98)";ctx.fillRect(layout.left,layout.top,layout.width,layout.height);
  if(showScalar&&range){
    const stepX=Math.abs(section.column_step_mm[0])*layout.scale,stepY=Math.abs(section.row_step_mm[1])*layout.scale;
    if(smoothSectionInput.checked){
      const fieldCanvas=sectionRaster(section,field,range);ctx.imageSmoothingEnabled=true;ctx.imageSmoothingQuality="high";
      ctx.drawImage(fieldCanvas,layout.left,layout.top,layout.width,layout.height);
    }else for(let index=0;index<section.columns*section.rows;index++){
      const value=sectionFieldValue(section,field,index);if(!Number.isFinite(value))continue;
      const point=planePoint(section,index),x=layout.left+(box.x-point[0])*layout.scale,y=layout.top+point[1]*layout.scale;
      const colour=scalarPalette((value-range[0])/Math.max(1e-12,range[1]-range[0]),field);
      ctx.fillStyle=`rgb(${colour[0]},${colour[1]},${colour[2]})`;ctx.fillRect(x-stepX/2,y-stepY/2,stepX+.35,stepY+.35);
    }
    if(visibility.grid){
      ctx.strokeStyle="rgba(225,238,246,.11)";ctx.lineWidth=.3;
      for(let column=0;column<=section.columns;column++){const x=layout.left+column*layout.width/section.columns;ctx.beginPath();ctx.moveTo(x,layout.top);ctx.lineTo(x,layout.top+layout.height);ctx.stroke();}
      for(let row=0;row<=section.rows;row++){const y=layout.top+row*layout.height/section.rows;ctx.beginPath();ctx.moveTo(layout.left,y);ctx.lineTo(layout.left+layout.width,y);ctx.stroke();}
    }
  }
  drawDirectXYGeometry(layout);
  // 냉각수 probe가 배터리 고체 내부를 "Not Found"로 남긴 자리에는
  // 해당 절단면과 실제로 교차한 셀 단면만 별도로 겹쳐 그린다.
  if(field==="temperature"&&showScalar&&visibility.cells)drawDirectXYCellCuts(section,layout,range);
  ctx.restore();
  ctx.fillStyle="rgba(232,242,250,.96)";ctx.font="700 10px ui-monospace,monospace";
  const renderLabel=smoothSectionInput.checked?"DISPLAY SMOOTH · VALID NEIGHBOURS ONLY":"EXACT SAMPLE TILES";
  ctx.fillText(`PRECISION MESH DIRECT SAMPLES · ${section.columns} × ${section.rows} · ${section.label} · ${renderLabel} · NO INVALID PADDING`,layout.plotLeft,layout.plotTop-12);
  return true;
}
function headDisplayScale(){return Number(headRate?.value||.5);}
function sectionMarkerPhase(speed,index){
  const displayTravelM=Math.max(.12,Math.hypot(box.x,box.y,box.z)/1000);
  return ((simulationTime*headDisplayScale()*Math.max(0,speed)/displayTravelM+index*.381966)%1+1)%1;
}
function drawDirectXYVelocityVectors(layout,movingOnly=false,targetContext=ctx){
  const section=horizontalSection();if(!section?.velocity_vectors_m_s)return;
  const limit=velocityDisplayMaximum();
  targetContext.save();targetContext.beginPath();targetContext.rect(layout.plotLeft,layout.plotTop,layout.plotRight-layout.plotLeft,layout.plotBottom-layout.plotTop);targetContext.clip();
  for(let row=2;row<section.rows-2;row+=5)for(let column=2;column<section.columns-2;column+=5){
    const index=row*section.columns+column,vector=section.velocity_vectors_m_s[index];if(!vector)continue;
    const magnitude=Math.hypot(...vector),planarMagnitude=Math.hypot(vector[0],vector[1]);
    if(magnitude<limit*.012||planarMagnitude<1e-10)continue;
    const point=planePoint(section,index),x=layout.left+(box.x-point[0])*layout.scale,y=layout.top+point[1]*layout.scale;
    const directionX=-vector[0]/planarMagnitude,directionY=vector[1]/planarMagnitude;
    const length=6+12*Math.sqrt(clamp(magnitude/limit)),endX=x+directionX*length,endY=y+directionY*length;
    if(!movingOnly){
      targetContext.strokeStyle="rgba(242,249,253,.72)";targetContext.lineWidth=.85;targetContext.beginPath();targetContext.moveTo(x,y);targetContext.lineTo(endX,endY);targetContext.stroke();
      const angle=Math.atan2(directionY,directionX),head=3.2;
      targetContext.beginPath();targetContext.moveTo(endX,endY);targetContext.lineTo(endX-head*Math.cos(angle-.55),endY-head*Math.sin(angle-.55));
      targetContext.moveTo(endX,endY);targetContext.lineTo(endX-head*Math.cos(angle+.55),endY-head*Math.sin(angle+.55));targetContext.stroke();
    }else if(visibility.particles){
      const phase=sectionMarkerPhase(magnitude,index);
      for(let tail=2;tail>=0;tail--){
        const fraction=((phase-tail*.075)%1+1)%1,mx=x+directionX*length*fraction,my=y+directionY*length*fraction;
        targetContext.beginPath();targetContext.arc(mx,my,tail===0?2.05:1.25,0,2*Math.PI);
        targetContext.fillStyle=tail===0?"rgba(255,255,255,.98)":`rgba(106,221,239,${.18+(2-tail)*.2})`;targetContext.fill();
      }
    }
  }
  targetContext.restore();
}
function timeSeriesValue(values){
  if(!values||values.length===0)return null;
  if(actualOneSecond)return values[clamp(Math.round(simulationTime),0,values.length-1)];
  const position=clamp(simulationTime/1800)*(values.length-1);
  const left=Math.floor(position),right=Math.min(values.length-1,left+1),weight=position-left;
  return values[left]+weight*(values[right]-values[left]);
}
function thermalValue(id){
  if(!flow.enabled||!flow.temperature_c[id])return null;
  return timeSeriesValue(flow.temperature_c[id]);
}
function reflectedSurfaceIndex(index){
  if(activeSpatialTransform?.type!=="rigid_reflection"||activeSpatialTransform?.axis!=="y"||!Number.isInteger(index))return index;
  const segments=cellSurface.angular_segments,rows=cellSurface.axial_segments,sideCount=segments*rows;
  if(index<sideCount)return Math.floor(index/segments)*segments+(segments-1-index%segments);
  const base=index<sideCount+segments?sideCount:sideCount+segments;
  return base+(segments-1-(index-base));
}
function surfaceThermalValue(id,index){
  if(actualOneSecond&&cellSurface?.actual_dataset&&Number.isInteger(index)){
    const cellIndex=sceneCells.findIndex(cell=>cell.id===id);
    const samplesPerCell=actualOneSecond.datasets[cellSurface.actual_dataset].samples_per_cell;
    return actualDatasetValue(cellSurface.actual_dataset,cellIndex*samplesPerCell+reflectedSurfaceIndex(index));
  }
  const frames=cellSurface?.temperature_frames_c?.[id];
  if(!frames||!Number.isInteger(index))return thermalValue(id);
  index=reflectedSurfaceIndex(index);
  const position=clamp(simulationTime/1800)*Math.max(0,frames.length-1);
  const left=Math.floor(position),right=Math.min(frames.length-1,left+1),weight=position-left;
  const a=frames[left]?.[index],b=frames[right]?.[index];
  if(Number.isFinite(a)&&Number.isFinite(b))return a+weight*(b-a);
  return Number.isFinite(a)?a:Number.isFinite(b)?b:thermalValue(id);
}
let solidSectionRangeCache=null;
function solidSectionTemperatureRange(){
  if(solidSectionRangeCache)return solidSectionRangeCache;
  let low=Infinity,high=-Infinity;
  const include=range=>{
    if(Array.isArray(range)&&range.length===2&&range.every(Number.isFinite)){low=Math.min(low,range[0]);high=Math.max(high,range[1]);}
  };
  include(cellSurface?.temperature_range_c);
  // 고체 내부 단면의 모든 셀 값을 저장한 데이터셋은 없지만, 각 셀의
  // batterySolid 체적평균은 매 정수 초마다 직접 저장되어 있다.
  for(const values of Object.values(temperatureGraphs?.per_cell?.internal_volume_average_c||{})){
    for(const value of values)if(Number.isFinite(value)){low=Math.min(low,value);high=Math.max(high,value);}
  }
  solidSectionRangeCache=Number.isFinite(low)&&Number.isFinite(high)&&high>low?[low,high]:cellSurface?.temperature_range_c||flow.temperature_range_c;
  return solidSectionRangeCache;
}
function cellSectionTemperature(id){
  const values=temperatureGraphs?.per_cell?.internal_volume_average_c?.[id];
  const value=timeSeriesValue(values);
  return Number.isFinite(value)?value:thermalValue(id);
}
function temperatureSectionRange(section){
  return sharedTemperatureRange();
}
function temperatureColour(value,range,alpha=1){
  if(!Number.isFinite(value)||!range)return null;
  const [r,g,b]=temperaturePalette((value-range[0])/Math.max(1e-9,range[1]-range[0]));
  return alpha>=1?`rgb(${r},${g},${b})`:`rgba(${r},${g},${b},${alpha})`;
}
function surfaceHeatTransferValue(id,index){
  if(!actualOneSecond||!cellSurfaceHeatTransfer?.actual_dataset||!Number.isInteger(index))return null;
  const cellIndex=sceneCells.findIndex(cell=>cell.id===id);if(cellIndex<0)return null;
  const specification=actualOneSecond.datasets?.[cellSurfaceHeatTransfer.actual_dataset];
  if(!specification)return null;
  return actualDatasetValue(cellSurfaceHeatTransfer.actual_dataset,cellIndex*specification.samples_per_cell+reflectedSurfaceIndex(index));
}
function heatTransferRange(){
  const range=cellSurfaceHeatTransfer?.fixed_legend_range_w_m2k;
  return Array.isArray(range)&&range.length===2&&range.every(Number.isFinite)&&range[1]>range[0]?range:null;
}
function heatTransferColour(value,range,alpha=1){
  if(!Number.isFinite(value)||!range)return null;
  const [r,g,b]=jet((value-range[0])/Math.max(1e-9,range[1]-range[0]));
  return alpha>=1?`rgb(${r},${g},${b})`:`rgba(${r},${g},${b},${alpha})`;
}
let surfaceHeatTransferStatsCache={frame:null,value:null};
function surfaceHeatTransferFrameStats(){
  if(!cellSurfaceHeatTransfer?.actual_dataset)return null;
  const dataset=actualDatasets.get(cellSurfaceHeatTransfer.actual_dataset);if(!dataset)return null;
  const specification=dataset.specification,step=Number(specification.time_step_s)||1,first=Number(specification.first_time_s)||0;
  const frame=Math.round((simulationTime-first)/step);
  if(frame<0||frame>=specification.frame_count)return null;
  if(surfaceHeatTransferStatsCache.frame===frame)return surfaceHeatTransferStatsCache.value;
  const offset=frame*specification.valid_count,end=offset+specification.valid_count;
  let minimum=Infinity,maximum=-Infinity,sum=0,count=0;
  for(let index=offset;index<end;index++){
    const value=dataset.encoded[index];if(!Number.isFinite(value))continue;
    minimum=Math.min(minimum,value);maximum=Math.max(maximum,value);sum+=value;count++;
  }
  const value=count?{minimum,maximum,average:sum/count,count,total:specification.point_count}:null;
  surfaceHeatTransferStatsCache={frame,value};return value;
}
function cellSurfaceRingIndex(cell,z){
  if(!cellSurface)return null;
  const rows=cellSurface.axial_segments,lower=cell.z-cell.length/2;
  return clamp(Math.round((z-lower)/cell.length*rows-.5),0,rows-1);
}
function cellSurfaceAngularIndex(angle){
  if(!cellSurface)return null;
  const segments=cellSurface.angular_segments,turn=((angle%(2*Math.PI))+2*Math.PI)%(2*Math.PI);
  return ((Math.round(turn/(2*Math.PI)*segments-.5)%segments)+segments)%segments;
}
function cellSurfaceAtCut(cell,z,angle){
  const axial=cellSurfaceRingIndex(cell,z),angular=cellSurfaceAngularIndex(angle);
  if(!Number.isInteger(axial)||!Number.isInteger(angular))return null;
  return surfaceThermalValue(cell.id,axial*cellSurface.angular_segments+angular);
}
const horizontalCutAngularSubdivisions=4,horizontalCutRadialBands=8;
const verticalCutAxialSubdivisions=4,verticalCutTransverseBands=16;
function interpolatedCellSurfaceAtCut(cell,z,angle){
  if(!cellSurface)return null;
  const axial=cellSurfaceRingIndex(cell,z),segments=cellSurface.angular_segments;
  if(!Number.isInteger(axial)||!segments)return null;
  const turn=((angle%(2*Math.PI))+2*Math.PI)%(2*Math.PI),position=turn/(2*Math.PI)*segments-.5;
  const left=Math.floor(position),weight=position-left;
  const sample=column=>surfaceThermalValue(cell.id,axial*segments+((column%segments)+segments)%segments);
  const a=sample(left),b=sample(left+1);
  if(Number.isFinite(a)&&Number.isFinite(b))return a+weight*(b-a);
  return Number.isFinite(a)?a:Number.isFinite(b)?b:null;
}
function projectedHorizontalCellTemperature(cell,surfaceValue,radialFraction){
  const centreValue=cellSectionTemperature(cell.id);
  if(!Number.isFinite(centreValue))return surfaceValue;
  if(!Number.isFinite(surfaceValue))return centreValue;
  return centreValue+clamp(radialFraction)*(surfaceValue-centreValue);
}
function interpolatedCellSurfaceAtAxial(cell,angular,axialFraction){
  if(!cellSurface||!Number.isInteger(angular))return null;
  const rows=cellSurface.axial_segments,segments=cellSurface.angular_segments,position=clamp(axialFraction)*rows-.5;
  const lower=Math.floor(position),weight=position-lower;
  const sample=row=>surfaceThermalValue(cell.id,clamp(row,0,rows-1)*segments+angular);
  const a=sample(lower),b=sample(lower+1);
  if(Number.isFinite(a)&&Number.isFinite(b))return a+weight*(b-a);
  return Number.isFinite(a)?a:Number.isFinite(b)?b:null;
}
function projectedVerticalCellTemperature(cell,leftValue,rightValue,transverseFraction){
  const centreValue=cellSectionTemperature(cell.id),fraction=clamp(transverseFraction);
  if(Number.isFinite(centreValue)){
    const edge=fraction<=.5?leftValue:rightValue,weight=fraction<=.5?fraction*2:(1-fraction)*2;
    if(Number.isFinite(edge))return edge+weight*(centreValue-edge);
    return centreValue;
  }
  if(Number.isFinite(leftValue)&&Number.isFinite(rightValue))return leftValue+fraction*(rightValue-leftValue);
  return Number.isFinite(leftValue)?leftValue:rightValue;
}
function meanFinite(values){
  const valid=values.filter(Number.isFinite);return valid.length?valid.reduce((sum,value)=>sum+value,0)/valid.length:null;
}
function surfaceCornerValues(face,valueAt){
  if(!cellSurface||!Number.isInteger(face.surfaceIndex))return null;
  const segments=cellSurface.angular_segments,rows=cellSurface.axial_segments,sideCount=segments*rows;
  if(face.kind.endsWith("-side")&&Number.isInteger(face.axialIndex)){
    const axial=face.axialIndex,angular=face.angularIndex;
    const sample=(row,column)=>valueAt(face.id,clamp(row,0,rows-1)*segments+((column%segments)+segments)%segments);
    return [
      meanFinite([sample(axial,angular),sample(axial-1,angular),sample(axial,angular-1),sample(axial-1,angular-1)]),
      meanFinite([sample(axial,angular),sample(axial-1,angular),sample(axial,angular+1),sample(axial-1,angular+1)]),
      meanFinite([sample(axial,angular),sample(axial+1,angular),sample(axial,angular+1),sample(axial+1,angular+1)]),
      meanFinite([sample(axial,angular),sample(axial+1,angular),sample(axial,angular-1),sample(axial+1,angular-1)])
    ];
  }
  if(face.kind.endsWith("-cap")){
    const top=face.surfaceIndex>=sideCount+segments,base=sideCount+(top?segments:0),angular=face.surfaceIndex-base;
    const sample=column=>valueAt(face.id,base+((column%segments)+segments)%segments);
    const centre=meanFinite(Array.from({length:segments},(_,index)=>sample(index)));
    const before=meanFinite([sample(angular-1),sample(angular)]),after=meanFinite([sample(angular),sample(angular+1)]);
    return top?[centre,before,after]:[centre,after,before];
  }
  return null;
}
function surfaceCornerTemperatures(face){return surfaceCornerValues(face,surfaceThermalValue);}
function surfaceCornerHeatTransfer(face){return surfaceCornerValues(face,surfaceHeatTransferValue);}
function surfaceSamplePoint(cell,surfaceIndex){
  surfaceIndex=reflectedSurfaceIndex(surfaceIndex);
  const segments=cellSurface.angular_segments,rows=cellSurface.axial_segments,sideCount=segments*rows;
  if(surfaceIndex<sideCount){
    const axial=Math.floor(surfaceIndex/segments),angular=surfaceIndex%segments,angle=2*Math.PI*(angular+.5)/segments;
    return [cell.x+cell.radius*Math.cos(angle),cell.y+cell.radius*Math.sin(angle),cell.z-cell.length/2+cell.length*(axial+.5)/rows];
  }
  const top=surfaceIndex>=sideCount+segments,angular=surfaceIndex-sideCount-(top?segments:0),angle=2*Math.PI*(angular+.5)/segments;
  return [cell.x+.58*cell.radius*Math.cos(angle),cell.y+.58*cell.radius*Math.sin(angle),cell.z+(top?1:-1)*cell.length/2];
}
function cellSurfaceExtrema(){
  const dataset=actualDatasets.get(cellSurface?.actual_dataset);if(!dataset)return null;
  const frame=clamp(Math.round(simulationTime),0,dataset.specification.frame_count-1);
  if(cellExtremaCache.frame===frame)return cellExtremaCache.value;
  const validCount=dataset.specification.valid_count,offset=frame*validCount,scale=actualOneSecond.temperature_encoding;
  let minimum=null,maximum=null;
  for(let validIndex=0;validIndex<validCount;validIndex++){
    const pointIndex=dataset.specification.valid_indices[validIndex],encoded=dataset.encoded[offset+validIndex];
    const value=scale.minimum_k+encoded*scale.quantum_k-273.15;
    const cellIndex=Math.floor(pointIndex/dataset.specification.samples_per_cell),surfaceIndex=pointIndex%dataset.specification.samples_per_cell,cell=sceneCells[cellIndex];
    const sample={value,cellId:cell.id,surfaceIndex,point:surfaceSamplePoint(cell,surfaceIndex)};
    if(!minimum||value<minimum.value)minimum=sample;if(!maximum||value>maximum.value)maximum=sample;
  }
  cellExtremaCache={frame,value:{minimum,maximum}};return cellExtremaCache.value;
}
function portTemperature(id){return timeSeriesValue(flow.port_temperature_c?.[id]);}
function portMetrics(id){
  const prefix=id==="IN"?"inlet":"outlet";
  return {
    temperatureC:portTemperature(id),
    velocityMS:flow.port_flow?.[`${prefix}_mean_velocity_m_s`],
    pressurePa:flow.port_flow?.[`${prefix}_area_average_pressure_pa`],
    massFlowKgS:flow.port_flow?.[`${prefix}_mass_flow_kg_s`]
  };
}
function updateDashboardMetrics(inletMetrics=portMetrics("IN"),outletMetrics=portMetrics("OUT")){
  const timeIndex=clamp(Math.round(simulationTime),0,1800),timeLabel=`${timeIndex} s`;
  const outletTemperature=temperatureGraphs?.outlet_mass_weighted_average_c?.[timeIndex]??outletMetrics.temperatureC;
  const cellMaximum=temperatureGraphs?.battery_mesh_maximum_c?.[timeIndex];
  dashboard.outlet.textContent=Number.isFinite(outletTemperature)?`${outletTemperature.toFixed(3)} °C`:"—";
  dashboard.cellMaximum.textContent=Number.isFinite(cellMaximum)?`${cellMaximum.toFixed(3)} °C`:"—";
  const hotspot=cellSurfaceExtrema()?.maximum,hotspotPoint=hotspot?.point;
  dashboard.cellHotspot.textContent=hotspot?`${hotspot.cellId} · ${hotspot.value.toFixed(3)} °C`:"—";
  dashboard.cellHotspotLocation.textContent=hotspotPoint
    ?`X ${hotspotPoint[0].toFixed(1)} · Y ${hotspotPoint[1].toFixed(1)} · Z ${hotspotPoint[2].toFixed(1)} mm · ${timeLabel}`
    :`배터리 표면 실제 샘플 좌표 · ${timeLabel}`;
  dashboard.inletVelocity.textContent=Number.isFinite(inletMetrics.velocityMS)?`${inletMetrics.velocityMS.toFixed(3)} m/s`:"—";
  dashboard.pressureDrop.textContent=Number.isFinite(flow.port_flow?.pressure_drop_pa)?`${flow.port_flow.pressure_drop_pa.toFixed(1)} Pa`:"—";
  dashboard.inletMassFlow.textContent=Number.isFinite(inletMetrics.massFlowKgS)?`${inletMetrics.massFlowKgS.toFixed(5)} kg/s`:"—";
  dashboard.outletMassFlow.textContent=Number.isFinite(outletMetrics.massFlowKgS)?`${outletMetrics.massFlowKgS.toFixed(5)} kg/s`:"—";
  dashboard.outletTime.textContent=timeLabel;
  dashboard.cellTime.textContent=`23개 셀 · ${timeLabel}`;
  const showHeatTransfer=visualMode==="surface-heat-transfer";
  for(const card of dashboard.htcCards)card.hidden=!showHeatTransfer;
  const heatTransferStats=showHeatTransfer?surfaceHeatTransferFrameStats():null;
  dashboard.htcAverage.textContent=heatTransferStats?`${heatTransferStats.average.toFixed(1)} W/(m²·K)`:"—";
  dashboard.htcMinimum.textContent=heatTransferStats?`${heatTransferStats.minimum.toFixed(1)} W/(m²·K)`:"—";
  dashboard.htcMaximum.textContent=heatTransferStats?`${heatTransferStats.maximum.toFixed(1)} W/(m²·K)`:"—";
}
function updatePortStateText(inletMetrics=portMetrics("IN"),outletMetrics=portMetrics("OUT")){
  const temperatureSemantics=actualOneSecond?"T 원본(무보정)":"T 표시보정 +"+flow.temperature_display_offset_c.toFixed(3)+" K";
  const massFlow=value=>Number.isFinite(value)?`${value.toFixed(5)} kg/s`:"—";
  portState.textContent=`${temperatureSemantics} · IN T ${inletMetrics.temperatureC.toFixed(3)} °C / Ū ${inletMetrics.velocityMS.toFixed(3)} m/s / ṁ ${massFlow(inletMetrics.massFlowKgS)} / p̄ ${(inletMetrics.pressurePa/1000).toFixed(3)} kPa · OUT T ${outletMetrics.temperatureC.toFixed(3)} °C / Ū ${outletMetrics.velocityMS.toFixed(3)} m/s / ṁ ${massFlow(outletMetrics.massFlowKgS)} / p̄ ${(outletMetrics.pressurePa/1000).toFixed(3)} kPa · Δp ${flow.port_flow.pressure_drop_pa.toFixed(3)} Pa`;
  updateDashboardMetrics(inletMetrics,outletMetrics);
}
function drawPortCallout(anchor,id,metrics,accent,w,h){
  if(!Number.isFinite(metrics.temperatureC)||!Number.isFinite(metrics.velocityMS)||!Number.isFinite(metrics.pressurePa))return;
  const uiScale=w>=1100?1.35:w>=760?1.18:1,boxWidth=Math.round(178*uiScale),boxHeight=Math.round(73*uiScale),pad=Math.round(8*uiScale);
  const position=overlayBoxPosition(`port-${id}`,w,h,boxWidth,boxHeight,w-boxWidth-Math.round(18*uiScale),id==="IN"?Math.round(20*uiScale):h-boxHeight-Math.round(20*uiScale));
  const {left,top}=position;
  registerOverlayHitbox(`port-${id}`,left,top,boxWidth,boxHeight);
  const targetX=anchor.x<left?left:anchor.x>left+boxWidth?left+boxWidth:anchor.x;
  const targetY=anchor.y<top?top:anchor.y>top+boxHeight?top+boxHeight:anchor.y;
  ctx.save();
  ctx.strokeStyle=accent;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(anchor.x,anchor.y);ctx.lineTo(targetX,targetY);ctx.stroke();
  ctx.fillStyle="rgba(5,14,24,.92)";ctx.fillRect(left,top,boxWidth,boxHeight);
  ctx.strokeStyle=accent;ctx.lineWidth=1;ctx.strokeRect(left+.5,top+.5,boxWidth-1,boxHeight-1);
  const temperatureLabel=actualOneSecond?"RAW T":`T CAL +${flow.temperature_display_offset_c.toFixed(3)} K`;
  ctx.fillStyle=accent;ctx.font=`800 ${Math.round(10*uiScale)}px system-ui`;ctx.fillText(`${id==="IN"?"INLET":"OUTLET"} · ${temperatureLabel}`,left+pad,top+Math.round(15*uiScale));
  ctx.fillStyle="rgba(239,247,252,.98)";ctx.font=`700 ${Math.round(10*uiScale)}px ui-monospace,monospace`;
  ctx.fillText(`T   ${metrics.temperatureC.toFixed(3)} °C`,left+pad,top+Math.round(32*uiScale));
  ctx.fillText(`Ū   ${metrics.velocityMS.toFixed(3)} m/s`,left+pad,top+Math.round(48*uiScale));
  ctx.fillText(`p̄   ${(metrics.pressurePa/1000).toFixed(3)} kPa`,left+pad,top+Math.round(64*uiScale));
  ctx.restore();
}
function thermalFaceGradient(face,points,range,fallback){
  // Thousands of per-face CanvasGradient allocations landing on the exact
  // one-second boundary caused the visible periodic hitch. During playback,
  // keep the actual mesh-face value and reserve the cosmetic intra-face
  // gradient for paused inspection.
  if(flowPlaying)return fallback;
  const temperatures=surfaceCornerTemperatures(face);
  if(!smoothSectionInput.checked||!temperatures||!temperatures.every(Number.isFinite))return fallback;
  let cold=0,hot=0;for(let index=1;index<temperatures.length;index++){if(temperatures[index]<temperatures[cold])cold=index;if(temperatures[index]>temperatures[hot])hot=index;}
  if(Math.abs(temperatures[hot]-temperatures[cold])<1e-10)return fallback;
  const gradient=ctx.createLinearGradient(points[cold].x,points[cold].y,points[hot].x,points[hot].y);
  for(const [position,index] of [[0,cold],[.5,-1],[1,hot]]){
    const temperature=index<0?meanFinite(temperatures):temperatures[index],colour=cellSurfacePalette((temperature-range[0])/Math.max(1e-9,range[1]-range[0]));
    // 배터리는 불투명한 고체다. 반투명 색을 쓰면 painter 정렬 오차나
    // 겹친 원통의 뒷면이 비쳐 재생 중 셀이 갑자기 투명해 보일 수 있다.
    gradient.addColorStop(position,`rgb(${colour[0]},${colour[1]},${colour[2]})`);
  }
  return gradient;
}
function heatTransferFaceGradient(face,points,range,fallback){
  if(flowPlaying)return fallback;
  const values=surfaceCornerHeatTransfer(face);
  if(!smoothSectionInput.checked||!values||!values.every(Number.isFinite))return fallback;
  let low=0,high=0;for(let index=1;index<values.length;index++){if(values[index]<values[low])low=index;if(values[index]>values[high])high=index;}
  if(Math.abs(values[high]-values[low])<1e-10)return fallback;
  const gradient=ctx.createLinearGradient(points[low].x,points[low].y,points[high].x,points[high].y);
  for(const [position,index] of [[0,low],[.5,-1],[1,high]]){
    const value=index<0?meanFinite(values):values[index],colour=jet((value-range[0])/Math.max(1e-9,range[1]-range[0]));
    gradient.addColorStop(position,`rgb(${colour[0]},${colour[1]},${colour[2]})`);
  }
  return gradient;
}
function drawCellExtremumCallout(sample,title,accent,basis,scale,cx,cy,w,h,upper){
  if(!sample)return;
  const uiScale=w>=1100?1.3:w>=760?1.15:1,anchor=projected(sample.point,basis,scale,cx,cy),boxWidth=Math.round(224*uiScale),boxHeight=Math.round(67*uiScale),pad=Math.round(8*uiScale);
  const overlayId=upper?"extrema-max":"extrema-min";
  const position=overlayBoxPosition(overlayId,w,h,boxWidth,boxHeight,116,upper?22:h-boxHeight-22),left=position.left,top=position.top;
  registerOverlayHitbox(overlayId,left,top,boxWidth,boxHeight);
  const targetX=anchor.x<left?left:anchor.x>left+boxWidth?left+boxWidth:anchor.x,targetY=anchor.y<top?top:anchor.y>top+boxHeight?top+boxHeight:anchor.y;
  ctx.save();ctx.strokeStyle=accent;ctx.lineWidth=1.2;ctx.beginPath();ctx.moveTo(anchor.x,anchor.y);ctx.lineTo(targetX,targetY);ctx.stroke();
  ctx.beginPath();ctx.arc(anchor.x,anchor.y,6,0,2*Math.PI);ctx.fillStyle="rgba(6,16,25,.95)";ctx.fill();ctx.strokeStyle=accent;ctx.lineWidth=2.4;ctx.stroke();
  ctx.beginPath();ctx.moveTo(anchor.x-10,anchor.y);ctx.lineTo(anchor.x+10,anchor.y);ctx.moveTo(anchor.x,anchor.y-10);ctx.lineTo(anchor.x,anchor.y+10);ctx.strokeStyle=accent;ctx.lineWidth=1.2;ctx.stroke();
  ctx.fillStyle="rgba(5,14,24,.94)";ctx.fillRect(left,top,boxWidth,boxHeight);ctx.strokeStyle=accent;ctx.lineWidth=1;ctx.strokeRect(left+.5,top+.5,boxWidth-1,boxHeight-1);
  ctx.fillStyle=accent;ctx.font=`800 ${Math.round(10*uiScale)}px system-ui`;ctx.fillText(`${title} · ${sample.cellId}`,left+pad,top+Math.round(16*uiScale));
  ctx.fillStyle="rgba(239,247,252,.98)";ctx.font=`700 ${Math.round(11*uiScale)}px ui-monospace,monospace`;ctx.fillText(`${sample.value.toFixed(4)} °C · t ${Math.round(simulationTime)} s`,left+pad,top+Math.round(35*uiScale));
  ctx.fillStyle="rgba(199,218,231,.98)";ctx.font=`650 ${Math.round(9*uiScale)}px ui-monospace,monospace`;ctx.fillText(`X ${sample.point[0].toFixed(1)} · Y ${sample.point[1].toFixed(1)} · Z ${sample.point[2].toFixed(1)} mm`,left+pad,top+Math.round(54*uiScale));ctx.restore();
}
function prepareFlowPathPhysics(path){
  if(path.physics)return path.physics;
  const cumulativeTimeS=[0],segmentLengthM=[];
  for(let index=1;index<path.p.length;index++){
    const lengthM=Math.hypot(...path.p[index].map((value,axis)=>value-path.p[index-1][axis]))/1000;
    const speed0=Math.max(1e-6,path.s[index-1]),speed1=Math.max(1e-6,path.s[index]),delta=speed1-speed0;
    const durationS=lengthM<=0?0:Math.abs(delta)<1e-9?lengthM/speed0:lengthM*Math.log(speed1/speed0)/delta;
    segmentLengthM.push(lengthM);cumulativeTimeS.push(cumulativeTimeS.at(-1)+durationS);
  }
  path.physics={cumulativeTimeS,segmentLengthM,transitTimeS:cumulativeTimeS.at(-1)};
  return path.physics;
}
function physicalFlowPoint(path,elapsedTimeS){
  const physics=prepareFlowPathPhysics(path),total=physics.transitTimeS;
  if(!Number.isFinite(total)||total<=0)return {point:path.p[0],speed:path.s[0]};
  const target=((elapsedTimeS%total)+total)%total,times=physics.cumulativeTimeS;
  let low=0,high=times.length-1;
  while(low+1<high){const middle=(low+high)>>1;if(times[middle]<=target)low=middle;else high=middle;}
  const left=low,right=Math.min(path.p.length-1,left+1),localTime=target-times[left],lengthM=physics.segmentLengthM[left];
  const speed0=Math.max(1e-6,path.s[left]),speed1=Math.max(1e-6,path.s[right]),delta=speed1-speed0;
  const weight=clamp(lengthM<=0?0:Math.abs(delta)<1e-9?localTime*speed0/lengthM:speed0*Math.expm1(delta*localTime/lengthM)/delta);
  const point=[0,1,2].map(axis=>path.p[left][axis]+weight*(path.p[right][axis]-path.p[left][axis]));
  return {point,speed:path.s[left]+weight*(path.s[right]-path.s[left])};
}
function drawScalarLegend(w,h,title,range,unit,legendId="legend",side="left"){
  if(!flow.enabled||!range)return;
  const displayUnit=unit==="C"?"°C":unit;
  const legendTitle=unit==="m/s"?"Velocity [m/s]":unit==="C"?"Temperature [°C]":`${title} [${displayUnit}]`;
  const uiScale=w>=1100?1.34:w>=760?1.17:1,[low,high]=range;
  // Keep the larger type requested for presentations, but adapt the number of
  // labelled ticks to the short embedded viewer. Fixed 19-label legends were
  // unreadable once the text grew, even though the colour bar itself fitted.
  const height=Math.min(Math.round(320*uiScale),Math.max(Math.round(112*uiScale),h-Math.round(72*uiScale)));
  const width=Math.round(12*uiScale),panelWidth=Math.round(102*uiScale),panelHeight=height+Math.round(60*uiScale);
  const defaultLeft=side==="right"?w-panelWidth-Math.round(18*uiScale):Math.round(18*uiScale);
  const position=overlayBoxPosition(legendId,w,h,panelWidth,panelHeight,defaultLeft,Math.round(16*uiScale)),x=position.left,y=position.top+Math.round(30*uiScale);
  registerOverlayHitbox(legendId,position.left,position.top,panelWidth,panelHeight);
  const gradient=ctx.createLinearGradient(0,y+height,0,y);
  const paletteStops=unit==="C"?temperaturePaletteStops:Array.from({length:9},(_,index)=>[index/8,jet(index/8)]);
  for(const [t,[r,g,b]] of paletteStops)gradient.addColorStop(t,`rgb(${r},${g},${b})`);
  ctx.fillStyle=gradient;ctx.fillRect(x,y,width,height);ctx.strokeStyle="rgba(230,240,250,.7)";ctx.strokeRect(x,y,width,height);
  ctx.fillStyle="rgba(232,242,250,.96)";ctx.font=`700 ${Math.round(11*uiScale)}px system-ui`;ctx.fillText(legendTitle,x-1,y-Math.round(24*uiScale));
  const tickFontPx=Math.round(9*uiScale),preferredIntervals=unit==="C"?10:legendId==="mass-flow-legend"?6:9;
  const intervals=Math.min(preferredIntervals,Math.max(4,Math.floor(height/Math.max(18,tickFontPx*1.7))));
  const fluentWaterTicks=unit==="C"&&Math.abs(low-19.908)<1e-9&&Math.abs(high-21.044)<1e-9
    ?[21.044,20.930,20.817,20.703,20.590,20.476,20.362,20.249,20.135,20.022,19.908]:null;
  const values=fluentWaterTicks?(height>=Math.round(145*uiScale)?fluentWaterTicks:[21.044,20.817,20.590,20.362,20.135,19.908])
    :Array.from({length:intervals+1},(_,index)=>high-(high-low)*index/intervals);
  ctx.font=`700 ${tickFontPx}px ui-monospace,monospace`;
  for(let index=0;index<values.length;index++){
    const value=values[index],position=y+height*(high-value)/Math.max(1e-9,high-low);
    ctx.strokeStyle="rgba(235,243,249,.88)";ctx.lineWidth=.8;ctx.beginPath();ctx.moveTo(x+width,position);ctx.lineTo(x+width+Math.round(7*uiScale),position);ctx.stroke();
    const tickLabel=legendId==="mass-flow-legend"?value.toFixed(6):value.toFixed(3);
    ctx.fillStyle="rgba(232,242,250,.96)";ctx.fillText(tickLabel,x+width+Math.round(10*uiScale),position+Math.round(3*uiScale));
    if(!fluentWaterTicks&&index<values.length-1)for(let minor=1;minor<5;minor++){
      const nextY=y+height*(high-values[index+1])/Math.max(1e-9,high-low);
      const minorY=position+(nextY-position)*minor/5;ctx.strokeStyle="rgba(220,233,242,.58)";ctx.beginPath();ctx.moveTo(x+width,minorY);ctx.lineTo(x+width+Math.round(3*uiScale),minorY);ctx.stroke();
    }
  }
}
function planePoint(section,index){
  const row=Math.floor(index/section.columns),column=index%section.columns;
  return [0,1,2].map(axis=>section.origin_mm[axis]+column*section.column_step_mm[axis]+row*section.row_step_mm[axis]);
}
function sectionTemperature(section,index){
  if(actualOneSecond){
    const coolant=section.actual_dataset?actualDatasetValue(section.actual_dataset,index):null;
    if(Number.isFinite(coolant))return coolant;
    // 1800 s에는 원본 batterySolid/T 전체장이 남아 있어 포함 메시 셀의
    // 값을 직접 쓴다. 다른 시각에는 존재하지 않는 공간장을 만들지 않는다.
    if(Math.round(simulationTime)===section.solid_temperature_time_s){
      const finalCoolant=section.fluid_temperature_1800_c?.[index];
      if(Number.isFinite(finalCoolant))return finalCoolant;
      const solid=section.solid_temperature_1800_c?.[index];
      if(Number.isFinite(solid))return solid;
    }
    return null;
  }
  const frames=section.temperature_frames_c||[],position=clamp(simulationTime/1800)*Math.max(0,frames.length-1);
  const left=Math.floor(position),right=Math.min(frames.length-1,left+1),weight=position-left;
  const a=frames[left]?.[index],b=frames[right]?.[index];
  if(Number.isFinite(a)&&Number.isFinite(b))return a+weight*(b-a);
  return Number.isFinite(a)?a:Number.isFinite(b)?b:null;
}
function sectionFieldValue(section,field,index){
  if(field==="temperature")return sectionTemperature(section,index);
  if(field==="pressure")return section.pressure_gauge_pa?.[index]??null;
  if(field==="mass-flow"){
    const vector=section.velocity_vectors_m_s?.[index],column=section.column_step_mm,row=section.row_step_mm;
    if(!Array.isArray(vector)||vector.length<3||!Array.isArray(column)||column.length<3||!Array.isArray(row)||row.length<3)return null;
    const areaVectorMm2=[column[1]*row[2]-column[2]*row[1],column[2]*row[0]-column[0]*row[2],column[0]*row[1]-column[1]*row[0]];
    const normalVolumeFlow=Math.abs(vector[0]*areaVectorMm2[0]+vector[1]*areaVectorMm2[1]+vector[2]*areaVectorMm2[2])*1e-6;
    return Number.isFinite(normalVolumeFlow)?fluidDensity()*normalVolumeFlow:null;
  }
  return section.velocity_m_s[index];
}
function bilinear(values,u,v){
  return values[0]*(1-u)*(1-v)+values[1]*u*(1-v)+values[2]*u*v+values[3]*(1-u)*v;
}
function bilinearPoint(points,u,v){
  return [0,1,2].map(axis=>bilinear(points.map(point=>point[axis]),u,v));
}
function drawDirectSampleTiles(section,field,basis,scale,cx,cy,range,alpha){
  const quads=[];
  for(let index=0;index<section.columns*section.rows;index++){
    const value=sectionFieldValue(section,field,index);if(!Number.isFinite(value))continue;
    const centrePoint=planePoint(section,index),columnHalf=section.column_step_mm.map(value=>.5*value),rowHalf=section.row_step_mm.map(value=>.5*value);
    const points=[
      centrePoint.map((value,axis)=>value-columnHalf[axis]-rowHalf[axis]),
      centrePoint.map((value,axis)=>value+columnHalf[axis]-rowHalf[axis]),
      centrePoint.map((value,axis)=>value+columnHalf[axis]+rowHalf[axis]),
      centrePoint.map((value,axis)=>value-columnHalf[axis]+rowHalf[axis])
    ];
    const depth=points.reduce((sum,point)=>sum+projected(point,basis,scale,cx,cy).d,0)/4;
    quads.push({points,value,depth});
  }
  quads.sort((a,b)=>a.depth-b.depth);
  for(const quad of quads){
    const normalised=(quad.value-range[0])/Math.max(1e-9,range[1]-range[0]),[r,g,b]=scalarPalette(normalised,field),points=quad.points.map(point=>projected(point,basis,scale,cx,cy));
    ctx.beginPath();ctx.moveTo(points[0].x,points[0].y);for(let index=1;index<points.length;index++)ctx.lineTo(points[index].x,points[index].y);ctx.closePath();
    ctx.fillStyle=`rgba(${r},${g},${b},${alpha})`;ctx.fill();
    if(visibility.grid){ctx.strokeStyle="rgba(225,238,246,.13)";ctx.lineWidth=.25;ctx.stroke();}
  }
}
function drawSection(section,field,basis,scale,cx,cy,rangeOverride=null,alpha=.82,subdivisions=3){
  if(!section)return;
  const range=rangeOverride||(field==="temperature"?section.temperature_range_c:field==="pressure"?pressureDisplayRange():field==="mass-flow"?massFlowDisplayRange():velocityDisplayRange());
  const directlySampled=field!=="temperature"||(field==="temperature"&&actualOneSecond&&(section.actual_dataset||Math.round(simulationTime)===section.solid_temperature_time_s));
  if(directlySampled&&!smoothSectionInput.checked){drawDirectSampleTiles(section,field,basis,scale,cx,cy,range,alpha);return;}
  const quads=[];
  for(let row=0;row<section.rows-1;row++)for(let column=0;column<section.columns-1;column++){
    const ids=[row*section.columns+column,row*section.columns+column+1,(row+1)*section.columns+column+1,(row+1)*section.columns+column];
    const values=ids.map(index=>sectionFieldValue(section,field,index));
    if(!values.every(Number.isFinite))continue;
    const points=ids.map(index=>planePoint(section,index));
    for(let subRow=0;subRow<subdivisions;subRow++)for(let subColumn=0;subColumn<subdivisions;subColumn++){
      const u0=subColumn/subdivisions,u1=(subColumn+1)/subdivisions,v0=subRow/subdivisions,v1=(subRow+1)/subdivisions;
      const subPoints=[bilinearPoint(points,u0,v0),bilinearPoint(points,u1,v0),bilinearPoint(points,u1,v1),bilinearPoint(points,u0,v1)];
      const value=bilinear(values,.5*(u0+u1),.5*(v0+v1));
      const depth=subPoints.reduce((sum,point)=>sum+projected(point,basis,scale,cx,cy).d,0)/subPoints.length;
      quads.push({points:subPoints,value,depth});
    }
  }
  quads.sort((a,b)=>a.depth-b.depth);
  for(const quad of quads){
    const normalised=(quad.value-range[0])/Math.max(1e-9,range[1]-range[0]),[r,g,b]=scalarPalette(normalised,field);
    const points=quad.points.map(point=>projected(point,basis,scale,cx,cy));
    ctx.beginPath();ctx.moveTo(points[0].x,points[0].y);for(let index=1;index<points.length;index++)ctx.lineTo(points[index].x,points[index].y);ctx.closePath();
    ctx.fillStyle=`rgba(${r},${g},${b},${alpha})`;ctx.fill();
    if(visibility.grid){ctx.strokeStyle="rgba(225,238,246,.15)";ctx.lineWidth=.32;ctx.stroke();}
  }
}
function drawDirectXYCellCuts(section,layout,range){
  const z=section.origin_mm[2],sourceSegments=cellSurface?.angular_segments||32,displaySegments=sourceSegments*horizontalCutAngularSubdivisions;
  for(const cell of sceneCells){
    const lower=cell.z-cell.length/2,upper=cell.z+cell.length/2;
    if(z<lower-1e-6||z>upper+1e-6)continue;
    const centreX=layout.left+(box.x-cell.x)*layout.scale,centreY=layout.top+cell.y*layout.scale,radius=cell.radius*layout.scale;
    const projectedSurface=!exactSolidSectionActive(section),radialBands=projectedSurface?horizontalCutRadialBands:1;
    // At intermediate times, colour the complete circular cut by projecting
    // each directly sampled surface sector radially to the cell centre. At
    // t=1800 s the stored batterySolid section remains underneath and only a
    // thin direct-surface ring is overlaid.
    for(let index=0;index<displaySegments;index++){
      const a0=2*Math.PI*index/displaySegments,a1=2*Math.PI*(index+1)/displaySegments,midAngle=.5*(a0+a1);
      const surfaceValue=interpolatedCellSurfaceAtCut(cell,z,midAngle);
      const point=(radial,angle)=>[layout.left+(box.x-(cell.x+radial*Math.cos(angle)))*layout.scale,layout.top+(cell.y+radial*Math.sin(angle))*layout.scale];
      for(let radial=0;radial<radialBands;radial++){
        const r0=projectedSurface?cell.radius*radial/radialBands:cell.radius*.84,r1=projectedSurface?cell.radius*(radial+1)/radialBands:cell.radius;
        const value=projectedSurface?projectedHorizontalCellTemperature(cell,surfaceValue,(radial+.5)/radialBands):surfaceValue,colour=temperatureColour(value,range,.99);
        if(!colour)continue;
        const points=r0<1e-9?[[centreX,centreY],point(r1,a0),point(r1,a1)]:[point(r0,a0),point(r1,a0),point(r1,a1),point(r0,a1)];
        ctx.beginPath();ctx.moveTo(points[0][0],points[0][1]);for(let item=1;item<points.length;item++)ctx.lineTo(points[item][0],points[item][1]);ctx.closePath();ctx.fillStyle=colour;ctx.fill();
      }
    }
    ctx.beginPath();ctx.arc(centreX,centreY,radius,0,2*Math.PI);ctx.strokeStyle="rgba(232,242,250,.94)";ctx.lineWidth=1;ctx.stroke();
  }
}
function fillProjectedCellCut(points,value,range,basis,scale,cx,cy,alpha){
  const colour=temperatureColour(value,range,alpha);if(!colour)return;
  const projectedPoints=points.map(point=>projected(point,basis,scale,cx,cy));
  ctx.beginPath();ctx.moveTo(projectedPoints[0].x,projectedPoints[0].y);for(let index=1;index<projectedPoints.length;index++)ctx.lineTo(projectedPoints[index].x,projectedPoints[index].y);ctx.closePath();ctx.fillStyle=colour;ctx.fill();
}
function drawHorizontalCellCuts(section,basis,scale,cx,cy,range,alpha=.96){
  const z=section.origin_mm[2],sourceSegments=cellSurface?.angular_segments||32,displaySegments=sourceSegments*horizontalCutAngularSubdivisions;
  for(const cell of sceneCells){
    const lower=cell.z-cell.length/2,upper=cell.z+cell.length/2;
    if(z<lower-1e-6||z>upper+1e-6)continue;
    const ring=Array.from({length:displaySegments},(_,index)=>{
      const angle=2*Math.PI*index/displaySegments;return [cell.x+cell.radius*Math.cos(angle),cell.y+cell.radius*Math.sin(angle),z];
    });
    const projectedSurface=!exactSolidSectionActive(section),radialBands=projectedSurface?horizontalCutRadialBands:1;
    for(let index=0;index<displaySegments;index++){
      const a0=2*Math.PI*index/displaySegments,a1=2*Math.PI*(index+1)/displaySegments,midAngle=.5*(a0+a1),surfaceValue=interpolatedCellSurfaceAtCut(cell,z,midAngle);
      for(let radial=0;radial<radialBands;radial++){
        const r0=projectedSurface?cell.radius*radial/radialBands:cell.radius*.84,r1=projectedSurface?cell.radius*(radial+1)/radialBands:cell.radius;
        const value=projectedSurface?projectedHorizontalCellTemperature(cell,surfaceValue,(radial+.5)/radialBands):surfaceValue;
        const points=r0<1e-9
          ?[[cell.x,cell.y,z],[cell.x+r1*Math.cos(a0),cell.y+r1*Math.sin(a0),z],[cell.x+r1*Math.cos(a1),cell.y+r1*Math.sin(a1),z]]
          :[[cell.x+r0*Math.cos(a0),cell.y+r0*Math.sin(a0),z],[cell.x+r1*Math.cos(a0),cell.y+r1*Math.sin(a0),z],[cell.x+r1*Math.cos(a1),cell.y+r1*Math.sin(a1),z],[cell.x+r0*Math.cos(a1),cell.y+r0*Math.sin(a1),z]];
        fillProjectedCellCut(points,value,range,basis,scale,cx,cy,.99);
      }
    }
    const projectedRing=ring.map(point=>projected(point,basis,scale,cx,cy));
    ctx.beginPath();ctx.moveTo(projectedRing[0].x,projectedRing[0].y);for(let index=1;index<projectedRing.length;index++)ctx.lineTo(projectedRing[index].x,projectedRing[index].y);ctx.closePath();ctx.strokeStyle="rgba(232,242,250,.92)";ctx.lineWidth=.9;ctx.stroke();
  }
}
function cellCapTemperature(cell,top){
  if(!cellSurface)return null;
  const segments=cellSurface.angular_segments,sideCount=segments*cellSurface.axial_segments,base=sideCount+(top?segments:0);
  return meanFinite(Array.from({length:segments},(_,index)=>surfaceThermalValue(cell.id,base+index)));
}
function drawVerticalCellCuts(section,basis,scale,cx,cy,range,alpha=.94){
  const isYZ=section.kind==="yz",fixed=isYZ?section.origin_mm[0]:section.origin_mm[1],segments=cellSurface?.angular_segments||32,rows=cellSurface?.axial_segments||16;
  for(const cell of sceneCells){
    const delta=fixed-(isYZ?cell.x:cell.y);if(Math.abs(delta)>cell.radius)continue;
    const halfChord=Math.sqrt(Math.max(0,cell.radius*cell.radius-delta*delta)),left=(isYZ?cell.y:cell.x)-halfChord,right=(isYZ?cell.y:cell.x)+halfChord;
    const lower=cell.z-cell.length/2,upper=cell.z+cell.length/2;
    const edgePoint=(position,z)=>isYZ?[fixed,position,z]:[position,fixed,z];
    const cut=[edgePoint(left,lower),edgePoint(right,lower),edgePoint(right,upper),edgePoint(left,upper)];
    const positions=[left,right],angularIndices=positions.map(position=>cellSurfaceAngularIndex(isYZ?Math.atan2(position-cell.y,delta):Math.atan2(delta,position-cell.x)));
    const displayRows=rows*verticalCutAxialSubdivisions;
    // Only t=1800 s has a stored batterySolid internal section. At every
    // other second, subdivide the vertical cut into 64 x 16 display tiles.
    // Their colours are constrained by the two directly sampled surfaces and
    // the measured per-cell internal volume average. They are display
    // interpolation tiles, not 1,024 independent CFD cells.
    if(!exactSolidSectionActive(section))for(let axial=0;axial<displayRows;axial++){
      const z0=lower+cell.length*axial/displayRows,z1=lower+cell.length*(axial+1)/displayRows,axialFraction=(axial+.5)/displayRows;
      const leftValue=interpolatedCellSurfaceAtAxial(cell,angularIndices[0],axialFraction),rightValue=interpolatedCellSurfaceAtAxial(cell,angularIndices[1],axialFraction);
      for(let column=0;column<verticalCutTransverseBands;column++){
        const fraction0=column/verticalCutTransverseBands,fraction1=(column+1)/verticalCutTransverseBands,middleFraction=(column+.5)/verticalCutTransverseBands;
        const a=left+(right-left)*fraction0,b=left+(right-left)*fraction1,value=projectedVerticalCellTemperature(cell,leftValue,rightValue,middleFraction);
        fillProjectedCellCut([edgePoint(a,z0),edgePoint(b,z0),edgePoint(b,z1),edgePoint(a,z1)],value,range,basis,scale,cx,cy,alpha);
      }
    }
    // Retain the two directly sampled surface traces so the origin of the
    // projected colours remains visually explicit.
    for(let side=0;side<2;side++){
      const position=positions[side],angular=angularIndices[side];
      for(let axial=0;axial<displayRows;axial++){
        const value=interpolatedCellSurfaceAtAxial(cell,angular,(axial+.5)/displayRows),colour=temperatureColour(value,range,.99);
        if(!colour)continue;
        const z0=lower+cell.length*axial/displayRows,z1=lower+cell.length*(axial+1)/displayRows;
        const a=projected(edgePoint(position,z0),basis,scale,cx,cy),b=projected(edgePoint(position,z1),basis,scale,cx,cy);
        ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.strokeStyle=colour;ctx.lineWidth=2.1;ctx.stroke();
      }
    }
    for(const [z,top] of [[lower,false],[upper,true]]){
      const colour=temperatureColour(cellCapTemperature(cell,top),range,.99);if(!colour)continue;
      const a=projected(edgePoint(left,z),basis,scale,cx,cy),b=projected(edgePoint(right,z),basis,scale,cx,cy);
      ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.strokeStyle=colour;ctx.lineWidth=2.1;ctx.stroke();
    }
    const outline=cut.map(point=>projected(point,basis,scale,cx,cy));
    ctx.beginPath();ctx.moveTo(outline[0].x,outline[0].y);for(let index=1;index<outline.length;index++)ctx.lineTo(outline[index].x,outline[index].y);ctx.closePath();ctx.strokeStyle="rgba(232,242,250,.9)";ctx.lineWidth=.75;ctx.stroke();
  }
}
function drawCellSectionCuts(section,basis,scale,cx,cy,range,alpha=.96){
  if(!section||!visibility.cells)return;
  if(section.kind==="xy")drawHorizontalCellCuts(section,basis,scale,cx,cy,range,alpha);
  else if(section.kind==="xz"||section.kind==="yz")drawVerticalCellCuts(section,basis,scale,cx,cy,range,alpha);
}
function exactSolidSectionActive(section){
  return Math.round(simulationTime)===section?.solid_temperature_time_s&&Array.isArray(section?.solid_temperature_1800_c);
}
function horizontalSectionKey(){
  return horizontalSectionInputs.find(input=>input.checked)?.value||"xy";
}
function horizontalSection(){
  return sections[horizontalSectionKey()]||sections.xy;
}
function verticalSectionKey(){
  const suffix=verticalSectionInputs.find(input=>input.checked)?.value||"50";
  return `${verticalSectionPlane?.value||"yz"}${suffix}`;
}
function verticalSection(){
  return sections[verticalSectionKey()]||sections.yz50||sections.xz50;
}
function verticalSelection(){
  const section=verticalSection();return section?[section]:[];
}
function verticalViewName(){
  return verticalSection()?.kind==="yz"?"px":"py";
}
function vertical2DView(w,h){
  const section=verticalSection(),kind=section?.kind||"yz";
  const horizontalExtent=kind==="yz"?box.y:box.x;
  const plotLeft=96,plotRight=Math.max(plotLeft+160,w-28),plotTop=58,plotBottom=Math.max(plotTop+160,h-104);
  const scale=Math.min((plotRight-plotLeft)/Math.max(1,horizontalExtent),(plotBottom-plotTop)/Math.max(1,box.z))*zoom;
  const basis=kind==="yz"?{direction:[1,0,0],right:[0,1,0],up:[0,0,1]}:{direction:[0,1,0],right:[-1,0,0],up:[0,0,1]};
  return {basis,scale,cx:(plotLeft+plotRight)/2,cy:(plotTop+plotBottom)/2,plotLeft,plotRight,plotTop,plotBottom};
}
function verticalRange(field){
  if(field==="velocity")return velocityDisplayRange();
  if(field==="mass-flow")return massFlowDisplayRange();
  if(field==="pressure")return pressureDisplayRange();
  const selected=verticalSelection(),ranges=selected.map(section=>field==="temperature"?temperatureSectionRange(section):section.velocity_range_m_s).filter(Boolean);
  if(!ranges.length)return null;
  return [Math.min(...ranges.map(range=>range[0])),Math.max(...ranges.map(range=>range[1]))];
}
function selectedSectionVelocityPeak(mode=visualMode){
  if(mode.startsWith("xy-"))return horizontalSection()?.velocity_range_m_s?.[1]??null;
  if(mode.startsWith("xz-")){
    const peaks=verticalSelection().map(section=>section.velocity_range_m_s?.[1]).filter(Number.isFinite);
    return peaks.length?Math.max(...peaks):null;
  }
  return null;
}
function drawSectionLabel(section,basis,scale,cx,cy){
  const point=projected(section.origin_mm,basis,scale,cx,cy),label=section.label||"XZ";
  ctx.save();ctx.font="700 9px ui-monospace,monospace";
  const width=ctx.measureText(label).width+8;
  ctx.fillStyle="rgba(5,14,24,.82)";ctx.fillRect(point.x+4,point.y-15,width,15);
  ctx.fillStyle="rgba(255,215,149,.98)";ctx.fillText(label,point.x+8,point.y-4);ctx.restore();
}
function sectionLocationMetrics(section){
  if(!section)return null;
  const kind=(section.kind||"xy").toLowerCase();
  const axis=kind==="xy"?"Z":kind==="yz"?"X":"Y",axisIndex=axis==="X"?0:axis==="Y"?1:2;
  const length=axis==="X"?box.x:axis==="Y"?box.y:box.z,distance=Number(section.origin_mm?.[axisIndex]);
  if(!Number.isFinite(length)||!Number.isFinite(distance)||length<=0)return null;
  return {kind:kind.toUpperCase(),axis,length,distance,ratio:100*distance/length};
}
function sectionLocationPlane(section){
  const marginX=Math.max(7,box.x*.09),marginY=Math.max(7,box.y*.09),marginZ=Math.max(7,box.z*.09),kind=(section?.kind||"xy").toLowerCase();
  if(kind==="xy"){
    const z=section.origin_mm[2];return [[-marginX,-marginY,z],[box.x+marginX,-marginY,z],[box.x+marginX,box.y+marginY,z],[-marginX,box.y+marginY,z]];
  }
  if(kind==="yz"){
    const x=section.origin_mm[0];return [[x,-marginY,-marginZ],[x,box.y+marginY,-marginZ],[x,box.y+marginY,box.z+marginZ],[x,-marginY,box.z+marginZ]];
  }
  const y=section.origin_mm[1];return [[-marginX,y,-marginZ],[box.x+marginX,y,-marginZ],[box.x+marginX,y,box.z+marginZ],[-marginX,y,box.z+marginZ]];
}
function drawLocationMeasureLine(section,metrics,basis,scale,cx,cy){
  const marginX=Math.max(7,box.x*.09),marginY=Math.max(7,box.y*.09),marginZ=Math.max(7,box.z*.09);
  let start,end,cut;
  if(metrics.axis==="Z"){
    start=[-marginX,-marginY,0];end=[-marginX,-marginY,box.z];cut=[-marginX,-marginY,metrics.distance];
  }else if(metrics.axis==="X"){
    start=[0,-marginY,-marginZ];end=[box.x,-marginY,-marginZ];cut=[metrics.distance,-marginY,-marginZ];
  }else{
    start=[-marginX,0,-marginZ];end=[-marginX,box.y,-marginZ];cut=[-marginX,metrics.distance,-marginZ];
  }
  const a=projected(start,basis,scale,cx,cy),b=projected(end,basis,scale,cx,cy),marker=projected(cut,basis,scale,cx,cy);
  ctx.save();ctx.lineCap="round";
  ctx.strokeStyle="rgba(255,255,255,.82)";ctx.lineWidth=1.4;ctx.setLineDash([5,4]);ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();ctx.setLineDash([]);
  ctx.strokeStyle="rgba(255,52,52,.98)";ctx.lineWidth=3;ctx.beginPath();ctx.moveTo(marker.x-7,marker.y-7);ctx.lineTo(marker.x+7,marker.y+7);ctx.moveTo(marker.x+7,marker.y-7);ctx.lineTo(marker.x-7,marker.y+7);ctx.stroke();
  ctx.fillStyle="rgba(255,255,255,.95)";ctx.font="700 10px ui-monospace,monospace";ctx.fillText("0",a.x+6,a.y-6);ctx.fillText(`${metrics.axis} ${metrics.length.toFixed(1)} mm`,b.x+6,b.y-6);
  ctx.fillStyle="rgba(255,92,92,.99)";ctx.fillText(`${metrics.distance.toFixed(1)} mm`,marker.x+10,marker.y+14);ctx.restore();
}
function drawSectionLocationCard(section,metrics,w){
  const width=Math.min(292,Math.max(236,w*.34)),height=174,left=Math.max(12,w-width-16),top=15;
  ctx.save();ctx.fillStyle="rgba(8,18,28,.94)";ctx.strokeStyle="rgba(255,61,61,.92)";ctx.lineWidth=1.5;
  ctx.beginPath();ctx.roundRect(left,top,width,height,7);ctx.fill();ctx.stroke();
  ctx.fillStyle="rgba(255,83,83,.99)";ctx.font="800 14px system-ui";ctx.fillText(`${metrics.kind} 절단면 위치`,left+15,top+25);
  ctx.strokeStyle="rgba(255,83,83,.35)";ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(left+14,top+35);ctx.lineTo(left+width-14,top+35);ctx.stroke();
  const rows=[
    ["축 전체 길이",`${metrics.axis} = ${metrics.length.toFixed(1)} mm`],
    ["절단 축",`${metrics.axis}축 (${metrics.kind} 평면)`],
    ["원점부터 거리",`${metrics.distance.toFixed(1)} mm`],
    ["위치 비율",`${metrics.ratio.toFixed(1)} %`]
  ];
  rows.forEach(([label,value],index)=>{
    const y=top+58+index*27;ctx.fillStyle="rgba(178,197,211,.96)";ctx.font="650 11px system-ui";ctx.fillText(label,left+15,y);
    ctx.textAlign="right";ctx.fillStyle="rgba(255,255,255,.98)";ctx.font="800 12px ui-monospace,monospace";ctx.fillText(value,left+width-15,y);ctx.textAlign="left";
  });
  ctx.restore();
}
function drawSectionLocationGuide(section,basis,scale,cx,cy,w){
  const metrics=sectionLocationMetrics(section);if(!metrics)return;
  const points=sectionLocationPlane(section).map(point=>projected(point,basis,scale,cx,cy));
  ctx.save();ctx.beginPath();ctx.moveTo(points[0].x,points[0].y);for(let index=1;index<points.length;index++)ctx.lineTo(points[index].x,points[index].y);ctx.closePath();
  ctx.fillStyle="rgba(255,28,28,.13)";ctx.fill();ctx.strokeStyle="rgba(255,50,50,.99)";ctx.lineWidth=3;ctx.stroke();
  for(const point of points){ctx.fillStyle="rgba(255,58,58,.99)";ctx.fillRect(point.x-3,point.y-3,6,6);}
  ctx.restore();
  drawLocationMeasureLine(section,metrics,basis,scale,cx,cy);
  drawSectionLocationCard(section,metrics,w);
}
function drawSectionVelocity(section,basis,scale,cx,cy,movingOnly=false,targetContext=ctx){
  if(!section||!section.velocity_vectors_m_s)return;
  const limit=velocityDisplayMaximum(),stride=Math.max(2,Math.ceil(Math.max(section.rows,section.columns)/24));
  for(let row=1;row<section.rows-1;row+=stride)for(let column=1;column<section.columns-1;column+=stride){
    const index=row*section.columns+column,vector=section.velocity_vectors_m_s[index];
    if(!vector)continue;
    const magnitude=Math.hypot(...vector);if(magnitude<limit*.012)continue;
    const point=planePoint(section,index),length=3+8*Math.sqrt(clamp(magnitude/limit));
    const direction=vector.map(value=>value/magnitude),end=point.map((value,axis)=>value+direction[axis]*length);
    const a=projected(point,basis,scale,cx,cy),b=projected(end,basis,scale,cx,cy);
    if(!movingOnly){
      targetContext.strokeStyle="rgba(245,251,255,.82)";targetContext.lineWidth=.8;targetContext.beginPath();targetContext.moveTo(a.x,a.y);targetContext.lineTo(b.x,b.y);targetContext.stroke();
    }else if(visibility.particles){
      const phase=sectionMarkerPhase(magnitude,index),moving=point.map((value,axis)=>value+direction[axis]*length*phase);
      const marker=projected(moving,basis,scale,cx,cy);targetContext.beginPath();targetContext.arc(marker.x,marker.y,2,0,2*Math.PI);targetContext.fillStyle="rgba(255,255,255,.97)";targetContext.fill();
      targetContext.strokeStyle="rgba(47,157,220,.9)";targetContext.lineWidth=1;targetContext.stroke();
    }
  }
}
const streamlineDisplayLimit=260;
function displayedFlowPaths(paths){
  if(paths.length<=streamlineDisplayLimit)return paths;
  return Array.from({length:streamlineDisplayLimit},(_,index)=>paths[Math.round(index*(paths.length-1)/(streamlineDisplayLimit-1))]);
}
function rayHitsCell(point,direction,cell){
  // The camera is located in +direction.  A positive ray intersection means
  // that this streamline sample sits behind the opaque finite cylinder.
  const epsilon=1e-4,dx=point[0]-cell.x,dy=point[1]-cell.y;
  const lower=cell.z-cell.length/2,upper=cell.z+cell.length/2,radius=cell.radius;
  const a=direction[0]*direction[0]+direction[1]*direction[1];
  if(a>1e-10){
    const b=2*(dx*direction[0]+dy*direction[1]),c=dx*dx+dy*dy-radius*radius;
    const discriminant=b*b-4*a*c;
    if(discriminant>=0){
      const root=Math.sqrt(discriminant);
      for(const t of [(-b-root)/(2*a),(-b+root)/(2*a)]){
        if(t<=epsilon)continue;
        const z=point[2]+t*direction[2];
        if(z>=lower-epsilon&&z<=upper+epsilon)return true;
      }
    }
  }
  if(Math.abs(direction[2])>1e-10){
    for(const z of [lower,upper]){
      const t=(z-point[2])/direction[2];if(t<=epsilon)continue;
      const x=dx+t*direction[0],y=dy+t*direction[1];
      if(x*x+y*y<=radius*radius+epsilon)return true;
    }
  }
  return false;
}
function flowPointOccluded(point,basis){
  if(visualMode!=="combined"||!visibility.cells)return false;
  return sceneCells.some(cell=>rayHitsCell(point,basis.direction,cell));
}
function drawFlow(paths,basis,scale,cx,cy){
  if(!flow.enabled)return;
  for(const path of displayedFlowPaths(paths)){
    for(let index=1;index<path.p.length;index++){
      const midpoint=path.p[index-1].map((value,axis)=>(value+path.p[index][axis])*.5);
      if(flowPointOccluded(midpoint,basis))continue;
      const a=projected(path.p[index-1],basis,scale,cx,cy),b=projected(path.p[index],basis,scale,cx,cy);
      const speed=0.5*(path.s[index-1]+path.s[index]),strength=clamp(speed/velocityDisplayMaximum());
      const [r,g,bColor]=jet(strength);
      ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);
      if(visualMode==="velocity-streamlines"){
        ctx.strokeStyle=`rgba(${Math.min(255,Math.round(.45*r+80))},${Math.min(255,Math.round(.45*g+90))},${Math.min(255,Math.round(.45*bColor+130))},${.58+.24*strength})`;
        ctx.lineWidth=2;ctx.stroke();
      }else{
        // 종합 화면에서도 같은 실제 속도 범위로 유선을 색칠한다. 셀 표면의
        // 온도 컬러맵과 유체 속도 컬러맵은 서로 다른 범례로 함께 표시된다.
        ctx.strokeStyle=`rgba(${r},${g},${bColor},.86)`;ctx.lineWidth=2;ctx.stroke();
      }
    }
  }
}
function drawFlowParticles(paths,basis,scale,cx,cy,targetContext=ctx){
  if(!flow.enabled||!visibility.particles)return;
  const displayedPaths=displayedFlowPaths(paths);
  for(let index=0;index<displayedPaths.length;index++){
    const path=displayedPaths[index],physics=prepareFlowPathPhysics(path),baseTime=simulationTime*headDisplayScale()+index*.61803398875*physics.transitTimeS;
    const trailStepS=Math.min(.006,physics.transitTimeS/80);
    for(let tail=4;tail>=0;tail--){
      const sample=physicalFlowPoint(path,baseTime-tail*trailStepS),screen=projected(sample.point,basis,scale,cx,cy);
      if(flowPointOccluded(sample.point,basis))continue;
      const strength=clamp(sample.speed/velocityDisplayMaximum()),[r,g,bColor]=jet(strength);
      const alpha=.16+(4-tail)*.19,radius=1.4+(4-tail)*.72;
      targetContext.beginPath();targetContext.arc(screen.x,screen.y,radius,0,2*Math.PI);
      targetContext.fillStyle=tail===0?`rgba(255,255,255,.96)`:`rgba(${r},${g},${bColor},${alpha})`;targetContext.fill();
      if(tail===0){targetContext.strokeStyle=`rgba(${r},${g},${bColor},.98)`;targetContext.lineWidth=1.6;targetContext.stroke();}
    }
  }
}

function resize(){
  const rect=sceneStage.getBoundingClientRect(),cssPixels=Math.max(1,rect.width*rect.height);
  const ratio=Math.max(.8,Math.min(window.devicePixelRatio||1,1.75,Math.sqrt(3200000/cssPixels)));
  for(const [target,targetContext] of [[canvas,ctx],[overlayCanvas,overlayCtx]]){
    target.width=Math.max(1,Math.floor(rect.width*ratio));target.height=Math.max(1,Math.floor(rect.height*ratio));
    targetContext.setTransform(ratio,0,0,ratio,0,0);
  }
  draw();
  if(!meshFigurePanel.hidden)requestAnimationFrame(drawMeshResultFigure);
}
function drawScene(){
  // Clear the complete backing store in device coordinates. The control rows
  // can change the CSS height before ResizeObserver updates the bitmap; using
  // CSS dimensions here left a stale lower strip that looked like a duplicate.
  ctx.save();ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,canvas.width,canvas.height);ctx.restore();
  // 이전 draw helper가 남긴 투명도/합성 상태가 다음 프레임으로 전파되지 않게 한다.
  ctx.globalAlpha=1;ctx.globalCompositeOperation="source-over";
  overlayHitboxes=[];
  const w=canvas.clientWidth,h=canvas.clientHeight;
  const meshRun=activeMeshRun();
  if(meshRun&&!meshRun.data_ready){
    ctx.fillStyle="#0c1721";ctx.fillRect(0,0,w,h);
    ctx.textAlign="center";ctx.fillStyle="rgba(99,215,229,.98)";ctx.font="800 18px system-ui";
    ctx.fillText(meshRun.label,w/2,h/2-34);
    ctx.fillStyle="rgba(232,242,250,.92)";ctx.font="650 13px system-ui";
    ctx.fillText(meshRun.mesh_ready?"메시는 생성됐지만 CFD 결과 필드는 아직 없습니다.":"메시 및 CFD 계산 전입니다.",w/2,h/2-5);
    ctx.fillStyle="rgba(145,168,188,.95)";ctx.font="600 11px system-ui";
    ctx.fillText("다른 메시의 결과를 이 옵션에 대신 표시하지 않습니다.",w/2,h/2+21);
    ctx.textAlign="left";return;
  }
  if(visualMode==="cell-extrema-map"){
    drawCellExtremaMap(w,h);
    if(flow.enabled){updatePortStateText();updateFlowClock();}
    return;
  }
  const basis=cameraBasis(), scale=Math.min(w/(box.x*1.65),h/(box.y*1.65))*zoom;
  const cx=w/2,cy=h/2+8;
  // 단면 온도 모드에서는 원통 전체 표면을 칠하지 않는다. 절단 평면과
  // 실제로 교차한 셀 단면만 아래 drawCellSectionCuts에서 온도색으로 표시한다.
  const thermalCells=["combined","cell-temperature"].includes(visualMode);
  const heatTransferSurface=visualMode==="surface-heat-transfer";
  const thermalDisplay=flow.enabled&&(thermalCells||visualMode.endsWith("-temperature"));
  const velocityDisplay=flow.enabled&&["velocity-streamlines","xy-velocity","xz-velocity","xy-mass-flow","xz-mass-flow"].includes(visualMode);
  const sectionMode=visualMode.startsWith("xy-")||visualMode.startsWith("xz-");
  const locationGuideMode=visualMode.endsWith("-location");
  if(visualMode.startsWith("xy-")&&!locationGuideMode&&!section3DInput.checked&&horizontalSection()){
    const selectedHorizontal=horizontalSection();
    if(visualMode==="xy-temperature")drawDirectXYGrid(selectedHorizontal,"temperature",w,h,visibility.section);
    else if(visualMode==="xy-pressure")drawDirectXYGrid(selectedHorizontal,"pressure",w,h,visibility.section);
    else if(visualMode==="xy-mass-flow")drawDirectXYGrid(selectedHorizontal,"mass-flow",w,h,visibility.section);
    else if(smoothSectionInput.checked)drawDirectXYGrid(selectedHorizontal,"velocity",w,h,visibility.section);
    else if(horizontalSectionKey()==="xy"&&directXYVelocity)drawDirectXYVelocity(w,h,visibility.section);
    else drawDirectXYGrid(selectedHorizontal,"velocity",w,h,visibility.section);
    if(visibility.flow)drawDirectXYVelocityVectors(directXYLayout(w,h));
    if(flow.enabled){
      updatePortStateText();
      if(visibility.temperatureLegend&&visibility.section&&visualMode==="xy-temperature")drawScalarLegend(w,h,"Fluid + cell cut T · XY",temperatureSectionRange(selectedHorizontal),"C");
      if(visibility.velocityLegend&&visibility.section&&visualMode==="xy-velocity")drawScalarLegend(w,h,"Velocity · shared scale",velocityDisplayRange(),"m/s");
      if(visibility.massFlowLegend&&visibility.section&&visualMode==="xy-mass-flow")drawScalarLegend(w,h,"Mass flow rate",massFlowDisplayRange(),"kg/s","mass-flow-legend");
      if(visibility.pressureLegend&&visibility.section&&visualMode==="xy-pressure")drawScalarLegend(w,h,"Gauge pressure · shared scale",pressureDisplayRange(),"Pa(g)","pressure-legend");
      updateFlowClock();
    }
    return;
  }
  if(visualMode.startsWith("xz-")&&!locationGuideMode&&!section3DInput.checked&&verticalSection()){
    const section=verticalSection(),view=vertical2DView(w,h),field=visualMode.slice(3);
    const range=verticalRange(field),subdivisions=smoothSectionInput.checked?3:1;
    ctx.fillStyle="rgba(7,16,27,.98)";ctx.fillRect(view.plotLeft,view.plotTop,view.plotRight-view.plotLeft,view.plotBottom-view.plotTop);
    if(visibility.section&&range){
      drawSection(section,field,view.basis,view.scale,view.cx,view.cy,range,.98,subdivisions);
      if(field==="temperature")drawCellSectionCuts(section,view.basis,view.scale,view.cx,view.cy,range,.99);
      drawSectionLabel(section,view.basis,view.scale,view.cx,view.cy);
    }
    if(visibility.flow)drawSectionVelocity(section,view.basis,view.scale,view.cx,view.cy);
    ctx.strokeStyle="rgba(205,225,238,.86)";ctx.lineWidth=1;
    ctx.strokeRect(view.plotLeft,view.plotTop,view.plotRight-view.plotLeft,view.plotBottom-view.plotTop);
    if(flow.enabled){
      updatePortStateText();
      if(visibility.temperatureLegend&&visibility.section&&field==="temperature")drawScalarLegend(w,h,`Fluid + cell cut T · ${section.kind.toUpperCase()}`,range,"C");
      if(visibility.velocityLegend&&visibility.section&&field==="velocity")drawScalarLegend(w,h,"Velocity · shared scale",velocityDisplayRange(),"m/s");
      if(visibility.massFlowLegend&&visibility.section&&field==="mass-flow")drawScalarLegend(w,h,"Mass flow rate",massFlowDisplayRange(),"kg/s","mass-flow-legend");
      if(visibility.pressureLegend&&visibility.section&&field==="pressure")drawScalarLegend(w,h,"Gauge pressure · shared scale",pressureDisplayRange(),"Pa(g)","pressure-legend");
      updateFlowClock();
    }
    return;
  }
  const sorted=projectedFaces(basis,scale,cx,cy,w,h);
  for(const item of sorted){
    const isPort=item.face.kind.startsWith("port");
    // Cell-temperature view is only about battery cells: omit the inlet and
    // outlet geometry entirely. Other views retain their original port styling.
    if(isPort&&visualMode==="cell-temperature")continue;
    const pts=item.points;
    ctx.beginPath(); ctx.moveTo(pts[0].x,pts[0].y); for(let i=1;i<pts.length;i++)ctx.lineTo(pts[i].x,pts[i].y); ctx.closePath();
    const shade=Math.max(0,Math.min(1,(item.depth+90)/180));
    if(!isPort&&!visibility.cells)continue;
    const temperature=isPort?null:surfaceThermalValue(item.face.id,item.face.surfaceIndex);
    if(isPort&&velocityDisplay){
      const key=item.face.id==="IN"?"inlet_mean_velocity_m_s":"outlet_mean_velocity_m_s";
      const normalised=flow.port_flow[key]/velocityDisplayMaximum(),[r,g,b]=jet(normalised);
      ctx.fillStyle=`rgba(${r},${g},${b},.88)`;
    }
    else if(isPort&&locationGuideMode){ctx.fillStyle=`rgba(12,28,43,${.08+.07*shade})`;}
    else if(isPort&&heatTransferSurface){ctx.fillStyle=`rgba(32,50,64,${.24+.08*shade})`;}
    else if(isPort){ctx.fillStyle=`rgba(239,147,43,${0.62+0.14*shade})`;}
    else if(heatTransferSurface){
      const range=heatTransferRange(),value=surfaceHeatTransferValue(item.face.id,item.face.surfaceIndex),colour=heatTransferColour(value,range);
      if(colour)ctx.fillStyle=smoothSectionInput.checked?heatTransferFaceGradient(item.face,pts,range,colour):colour;
      else ctx.fillStyle="rgb(46,57,66)";
    }
    else if(thermalCells&&Number.isFinite(temperature)){
      const range=sharedTemperatureRange(),normalised=(temperature-range[0])/Math.max(1e-9,range[1]-range[0]);
      const [r,g,b]=cellSurfacePalette(normalised);
      const fallback=`rgb(${r},${g},${b})`;ctx.fillStyle=smoothSectionInput.checked?thermalFaceGradient(item.face,pts,range,fallback):fallback;
    }else if(thermalCells){
      // 직접 표본이 invalid인 면도 투명하게 만들지 않는다. 중립 불투명색으로
      // 형상은 유지하되, 이웃 온도로 채워 넣어 가짜 CFD 값을 만들지는 않는다.
      ctx.fillStyle="rgb(32,112,145)";
    }else if(sectionMode||visualMode==="velocity-streamlines"){
      ctx.fillStyle=`rgba(12,28,43,${.08+.07*shade})`;
    }else{ctx.fillStyle=item.face.kind.endsWith("cap")?`rgba(79,207,222,${0.66+0.13*shade})`:`rgba(32,112,145,${0.68+0.16*shade})`;}
    ctx.fill();
    if(thermalDisplay&&(isPort||thermalCells)){
      // Shade the rendered solid like Fluent without altering temperature,
      // colour-map normalisation, section plots, or the scalar legend.
      // The cell cylinders need a little more depth than the 2-D legend;
      // leave port styling and temperature cut planes unchanged.
      const shadow=fluentSurfaceShadow(item.face,basis)+(thermalCells&&!isPort?.19:0);
      ctx.fillStyle=`rgba(0,0,0,${shadow})`;ctx.fill();
    }
    const highlighted=!isPort&&item.face.id===highlightedCellId;
    ctx.strokeStyle=highlighted?"rgba(2,5,7,.99)":thermalCells&&!isPort?"rgba(225,238,246,.045)":"rgba(225,238,246,.26)";
    ctx.lineWidth=highlighted?2.4:thermalCells&&!isPort?.22:.55;ctx.stroke();
  }
  if(visualMode==="cell-temperature")drawTemperaturePortOutlines(basis,scale,cx,cy);
  if(visibility.section){
    if(visualMode==="xy-temperature"){
      const section=horizontalSection();
      const range=temperatureSectionRange(section);
      if(smoothSectionInput.checked)drawXYPlaneTexture(sectionRaster(section,"temperature",range),section,basis,scale,cx,cy,.88);
      else drawSection(section,"temperature",basis,scale,cx,cy,range,.88,1);
      drawCellSectionCuts(section,basis,scale,cx,cy,range,.96);
    }
    if(visualMode==="xy-velocity"){
      const section=horizontalSection();
      if(!smoothSectionInput.checked&&horizontalSectionKey()==="xy"&&directXYVelocity)drawDirectXYVelocity3D(section,basis,scale,cx,cy);
      else if(smoothSectionInput.checked)drawXYPlaneTexture(sectionRaster(section,"velocity",velocityDisplayRange()),section,basis,scale,cx,cy,.88);
      else drawSection(section,"velocity",basis,scale,cx,cy,velocityDisplayRange(),.88,1);
    }
    if(visualMode==="xy-pressure"){
      const section=horizontalSection();
      if(smoothSectionInput.checked)drawXYPlaneTexture(sectionRaster(section,"pressure",pressureDisplayRange()),section,basis,scale,cx,cy,.88);
      else drawSection(section,"pressure",basis,scale,cx,cy,pressureDisplayRange(),.88,1);
    }
    if(visualMode==="xy-mass-flow"){
      const section=horizontalSection();
      if(smoothSectionInput.checked)drawXYPlaneTexture(sectionRaster(section,"mass-flow",massFlowDisplayRange()),section,basis,scale,cx,cy,.88);
      else drawSection(section,"mass-flow",basis,scale,cx,cy,massFlowDisplayRange(),.88,1);
    }
    const selectedVertical=verticalSelection(),verticalAlpha=selectedVertical.length>1?.52:.82;
    const verticalSubdivisions=selectedVertical.length>1?2:3;
    if(visualMode==="xz-temperature")for(const section of selectedVertical){
      const range=verticalRange("temperature");
      drawSection(section,"temperature",basis,scale,cx,cy,range,verticalAlpha,verticalSubdivisions);
      drawCellSectionCuts(section,basis,scale,cx,cy,range,selectedVertical.length>1?.82:.96);
    }
    if(visualMode==="xz-velocity")for(const section of selectedVertical)drawSection(section,"velocity",basis,scale,cx,cy,verticalRange("velocity"),verticalAlpha,verticalSubdivisions);
    if(visualMode==="xz-mass-flow")for(const section of selectedVertical)drawSection(section,"mass-flow",basis,scale,cx,cy,verticalRange("mass-flow"),verticalAlpha,verticalSubdivisions);
    if(visualMode==="xz-pressure")for(const section of selectedVertical)drawSection(section,"pressure",basis,scale,cx,cy,verticalRange("pressure"),verticalAlpha,verticalSubdivisions);
    if(visualMode.startsWith("xz-")&&!locationGuideMode)for(const section of selectedVertical)drawSectionLabel(section,basis,scale,cx,cy);
  }
  if(visibility.flow&&!locationGuideMode){
    // Draw depth-tested streamlines after the cells.  Samples on the camera
    // side stay visible, while samples whose camera ray intersects a cell are
    // omitted.  This works from top, bottom, and oblique views.
    if(["combined","cell-temperature"].includes(visualMode))drawFlow(flow.streamlines||[],basis,scale,cx,cy);
    if(visualMode.startsWith("xy-")&&visualMode!=="xy-velocity")drawSectionVelocity(horizontalSection(),basis,scale,cx,cy);
    if(visualMode==="xy-velocity"&&horizontalSectionKey()!=="xy")drawSectionVelocity(horizontalSection(),basis,scale,cx,cy);
    if(visualMode.startsWith("xz-"))for(const section of verticalSelection())drawSectionVelocity(section,basis,scale,cx,cy);
    if(visualMode==="velocity-streamlines")drawFlow(flow.streamlines||[],basis,scale,cx,cy);
  }
  if(visibility.enclosure&&visualMode!=="xy-velocity"){
    ctx.lineWidth=geometryFrameLineWidth; ctx.strokeStyle=geometryFrameStroke;
    for(const edge of edges){
      const a=projected(vertices[edge[0]],basis,scale,cx,cy),b=projected(vertices[edge[1]],basis,scale,cx,cy);
      ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
    }
  }
  if(locationGuideMode&&visibility.section){
    drawSectionLocationGuide(visualMode.startsWith("xy-")?horizontalSection():verticalSection(),basis,scale,cx,cy,w);
  }
  if(flow.enabled){
    const inlet=projected([scenePorts[0].x,scenePorts[0].y,scenePorts[0].z+scenePorts[0].length/2],basis,scale,cx,cy);
    const outlet=projected([scenePorts[1].x,scenePorts[1].y,scenePorts[1].z-scenePorts[1].length/2],basis,scale,cx,cy);
    const inletMetrics=portMetrics("IN"),outletMetrics=portMetrics("OUT");
    if(visibility.ports&&!locationGuideMode&&visualMode!=="xy-velocity"&&visualMode!=="cell-temperature"){
      drawPortCallout(inlet,"IN",inletMetrics,"rgba(255,189,91,.98)",w,h);
      drawPortCallout(outlet,"OUT",outletMetrics,"rgba(99,215,229,.98)",w,h);
    }
    if(visibility.extrema&&["combined","cell-temperature","xy-temperature","xz-temperature"].includes(visualMode)&&visibility.cells){
      const extrema=cellSurfaceExtrema();
      if(extrema){
        drawCellExtremumCallout(extrema.maximum,"BATTERY HOTTEST POINT","rgba(255,80,52,.99)",basis,scale,cx,cy,w,h,true);
      }
    }
    updatePortStateText(inletMetrics,outletMetrics);
    if(visibility.temperatureLegend&&visualMode==="combined"&&visibility.cells)drawScalarLegend(w,h,"Cell surface T",sharedTemperatureRange(),"C","temperature-legend","right");
    if(visibility.velocityLegend&&visualMode==="combined"&&visibility.flow)drawScalarLegend(w,h,"Velocity · shared scale",velocityDisplayRange(),"m/s","velocity-legend","left");
    if(visibility.temperatureLegend&&visualMode==="cell-temperature"&&visibility.cells)drawScalarLegend(w,h,"Cell surface T",sharedTemperatureRange(),"C","temperature-legend","right");
    if(visibility.velocityLegend&&visualMode==="cell-temperature"&&visibility.flow)drawScalarLegend(w,h,"Velocity · shared scale",velocityDisplayRange(),"m/s","velocity-legend","left");
    if(visibility.velocityLegend&&visualMode==="velocity-streamlines"&&visibility.flow)drawScalarLegend(w,h,"Velocity · shared scale",velocityDisplayRange(),"m/s");
    if(visibility.temperatureLegend&&visualMode==="xy-temperature"&&visibility.section)drawScalarLegend(w,h,"Fluid + cell cut T",temperatureSectionRange(horizontalSection()),"C");
    if(visibility.velocityLegend&&visualMode==="xy-velocity"&&visibility.section)drawScalarLegend(w,h,"Velocity · shared scale",velocityDisplayRange(),"m/s");
    if(visibility.massFlowLegend&&visualMode==="xy-mass-flow"&&visibility.section)drawScalarLegend(w,h,"Mass flow rate",massFlowDisplayRange(),"kg/s","mass-flow-legend");
    if(visibility.pressureLegend&&visualMode==="xy-pressure"&&visibility.section)drawScalarLegend(w,h,"Gauge pressure · shared scale",pressureDisplayRange(),"Pa(g)","pressure-legend");
    if(visibility.heatTransferLegend&&visualMode==="surface-heat-transfer"&&visibility.cells&&heatTransferRange())drawScalarLegend(w,h,"Local convective h · battery–coolant interface",heatTransferRange(),"W/(m²·K)","heat-transfer-legend","right");
    if(visibility.temperatureLegend&&visualMode==="xz-temperature"&&visibility.section){const section=verticalSection(),plane=section?.kind?.toUpperCase()||"VERTICAL",hasFluidSection=Boolean(section?.actual_dataset)||exactSolidSectionActive(section);drawScalarLegend(w,h,`${hasFluidSection?"Fluid + ":""}cell cut T · ${plane}`,verticalRange("temperature"),"C");}
    if(visibility.velocityLegend&&visualMode==="xz-velocity"&&visibility.section)drawScalarLegend(w,h,"Velocity · shared scale",velocityDisplayRange(),"m/s");
    if(visibility.massFlowLegend&&visualMode==="xz-mass-flow"&&visibility.section)drawScalarLegend(w,h,"Mass flow rate",massFlowDisplayRange(),"kg/s","mass-flow-legend");
    if(visibility.pressureLegend&&visualMode==="xz-pressure"&&visibility.section)drawScalarLegend(w,h,"Gauge pressure · shared scale",pressureDisplayRange(),"Pa(g)","pressure-legend");
    updateFlowClock();
  }else{ctx.fillStyle="rgba(255,189,91,.92)";ctx.font="700 11px system-ui";ctx.fillText("Ø19 mm IN / OUT",16,h-34);}
}
function drawDynamicOverlay(){
  overlayCtx.save();overlayCtx.setTransform(1,0,0,1,0,0);overlayCtx.clearRect(0,0,overlayCanvas.width,overlayCanvas.height);overlayCtx.restore();
  overlayCtx.globalAlpha=1;overlayCtx.globalCompositeOperation="source-over";
  if(visualMode.endsWith("-location"))return;
  if(!flow.enabled||!visibility.flow||(activeMeshRun()?.id==="single"&&!activeMeshRun()?.data_ready))return;
  if(visualMode.startsWith("xy-")&&!section3DInput.checked&&horizontalSection()){
    drawDirectXYVelocityVectors(directXYLayout(canvas.clientWidth,canvas.clientHeight),true,overlayCtx);
    updateFlowClock();return;
  }
  if((section3DInput.checked&&visualMode.startsWith("xy-"))||(section3DInput.checked&&visualMode.startsWith("xz-"))){
    const w=canvas.clientWidth,h=canvas.clientHeight,basis=cameraBasis(),scale=Math.min(w/(box.x*1.65),h/(box.y*1.65))*zoom;
    const selected=visualMode.startsWith("xy-")?[horizontalSection()]:verticalSelection();
    for(const section of selected)drawSectionVelocity(section,basis,scale,w/2,h/2+8,true,overlayCtx);
    updateFlowClock();return;
  }
  if(visualMode.startsWith("xz-")&&!section3DInput.checked){
    const w=canvas.clientWidth,h=canvas.clientHeight,view=vertical2DView(w,h);
    for(const section of verticalSelection())drawSectionVelocity(section,view.basis,view.scale,view.cx,view.cy,true,overlayCtx);
    updateFlowClock();return;
  }
  if(!["combined","cell-temperature","velocity-streamlines"].includes(visualMode))return;
  const w=canvas.clientWidth,h=canvas.clientHeight,basis=cameraBasis(),scale=Math.min(w/(box.x*1.65),h/(box.y*1.65))*zoom;
  drawFlowParticles(flow.streamlines||[],basis,scale,w/2,h/2+8,overlayCtx);
  updateFlowClock();
}
function draw(){drawScene();lastSceneFrame=Math.round(simulationTime);drawDynamicOverlay();}
canvas.addEventListener("pointerdown",e=>{
  const rect=canvas.getBoundingClientRect(),x=e.clientX-rect.left,y=e.clientY-rect.top,hit=overlayAt(x,y);
  canvas.setPointerCapture(e.pointerId);
  if(hit){overlayDrag={...hit,dx:x-hit.left,dy:y-hit.top};canvas.style.cursor="move";e.preventDefault();return;}
  dragging=true;lastX=e.clientX;lastY=e.clientY;canvas.style.cursor="grabbing";
});
canvas.addEventListener("pointermove",e=>{
  const rect=canvas.getBoundingClientRect(),x=e.clientX-rect.left,y=e.clientY-rect.top;
  if(overlayDrag){
    const maxLeft=Math.max(1,canvas.clientWidth-overlayDrag.width-8),maxTop=Math.max(1,canvas.clientHeight-overlayDrag.height-8);
    const left=clamp(x-overlayDrag.dx,0,maxLeft),top=clamp(y-overlayDrag.dy,0,maxTop);
    overlayPositions[overlayDrag.id]={x:left/maxLeft,y:top/maxTop};draw();e.preventDefault();return;
  }
  if(dragging){yaw+=(e.clientX-lastX)*.009;pitch=Math.max(-1.48,Math.min(1.48,pitch-(e.clientY-lastY)*.009));lastX=e.clientX;lastY=e.clientY;draw();return;}
  canvas.style.cursor=overlayAt(x,y)?"move":"grab";
});
function finishPointerInteraction(){if(overlayDrag)saveOverlayPositions();overlayDrag=null;dragging=false;canvas.style.cursor="grab";}
canvas.addEventListener("pointerup",finishPointerInteraction);
canvas.addEventListener("pointercancel",finishPointerInteraction);
canvas.addEventListener("wheel",e=>{e.preventDefault();zoom=Math.max(.45,Math.min(3,zoom*Math.exp(-e.deltaY*.001)));draw();},{passive:false});
const views={px:[0,0],nx:[Math.PI,0],py:[Math.PI/2,0],ny:[-Math.PI/2,0],pz:[0,Math.PI/2-.001],nz:[0,-Math.PI/2+.001],iso:[2.18,.52]};
function selectView(name){
  [yaw,pitch]=views[name];document.querySelectorAll("[data-view]").forEach(button=>button.classList.toggle("active",button.dataset.view===name));draw();
}
document.querySelectorAll("[data-view]").forEach(button=>button.addEventListener("click",()=>selectView(button.dataset.view)));
document.getElementById("reset").addEventListener("click",()=>{zoom=1;selectView("iso");});
const modeDescriptions={
  combined:["종합 화면","셀 온도와 실제 U(x)에 비례해 이동하는 유동 헤드 · 단면과 동일한 U 기준"],
  "cell-temperature":["셀 온도",`배터리 직접 표면 샘플 ${cellSurface?.angular_segments||28} × ${cellSurface?.axial_segments||12}+캡 · 현재 최고온도 지점·좌표와 선택한 속도 유선 표시`],
  "cell-extrema-map":["셀 최고·최저 온도","탱크 평면 배치에서 C01–C23 각각의 최고·최저 온도를 큰 숫자로 표시 · 재생 시간에 따라 매초 갱신"],
  "velocity-streamlines":["유체 유선","전체 유체 셀 공통 U 범위로 색칠한 고정 유동장과 실제 U(x)에 비례하는 이동 헤드"],
  "xy-location":["가로 단면 위치","탱크보다 크게 표시한 빨간 XY 직사각형과 Z축 전체 길이·원점 기준 절단 거리·위치 비율"],
  "xy-temperature":["가로 단면 온도","선택한 저장 높이의 냉각수 계산값 + 그 높이에서 절단된 셀만 온도색"],
  "xy-velocity":["가로 단면 유속","선택한 높이의 실제 셀 중심 유속 · 전체 유동장 공통 U 범위 · 보간/패딩 없음"],
  "xy-mass-flow":["가로 단면 질량유량","미세 타일별 ṁ=ρ·|U·n|·ΔA · 단면 법선방향 통과량 [kg/s] · 모든 결과 공통 범례"],
  "xy-pressure":["가로 단면 압력","선택한 높이의 실제 냉각수 셀 압력 · 출구 면적평균 기준 게이지압 · 모든 결과 공통 범례"],
  "surface-heat-transfer":["배터리 표면 열전달계수","냉각수 접촉 배터리 표면의 국부 대류 열전달계수 h=|q''|/|Twall−Tfluid| · 범례는 모든 시각과 결과에서 고정"],
  "xz-location":["세로 단면 위치","탱크보다 크게 표시한 빨간 YZ/XZ 직사각형과 X/Y축 전체 길이·원점 기준 절단 거리·위치 비율"],
  "xz-temperature":["세로 단면 온도","선택한 저장 비율과 방향의 계산 단면 + 그 위치에서 절단된 셀만 온도색"],
  "xz-velocity":["세로 단면 유속","선택한 세로 위치의 유속 크기와 방향 · 전체 유동장 공통 U 범위"],
  "xz-mass-flow":["세로 단면 질량유량","미세 타일별 ṁ=ρ·|U·n|·ΔA · 단면 법선방향 통과량 [kg/s] · 모든 결과 공통 범례"],
  "xz-pressure":["세로 단면 압력","선택한 XZ/YZ 위치의 실제 냉각수 셀 압력 · 출구 면적평균 기준 게이지압 · 모든 결과 공통 범례"]
};
function prepareActualMode(mode){
  if(!actualOneSecond)return;
  const names=[];
  if(["combined","cell-temperature","cell-extrema-map","xy-temperature","xz-temperature"].includes(mode))names.push("cell_surface");
  if(mode==="surface-heat-transfer"&&cellSurfaceHeatTransfer?.actual_dataset)names.push(cellSurfaceHeatTransfer.actual_dataset);
  if(mode==="surface-heat-transfer"&&!cellSurfaceHeatTransfer?.actual_dataset){
    currentMode.textContent="이 결과에는 국부 경계면 HTC 데이터 없음";
    setDataLoadStatus("이 보존 결과에는 wallHeatFlux·벽면 온도·근접 냉각수 온도 표본이 없어 국부 열전달계수를 표시하지 않습니다.","error");
    return;
  }
  if(mode.endsWith("-pressure")&&!pressureAvailable()){
    currentMode.textContent="이 결과에는 공간 압력장 데이터 없음";
    setDataLoadStatus("이 보존 결과 패키지에는 단면 p(x) 원본이 없어 압력 분포를 만들지 않습니다. 입·출구 평균 압력만으로 공간장을 합성하지 않습니다.","error");
    return;
  }
  if(mode==="xy-temperature")names.push(horizontalSectionKey());
  if(mode==="xz-temperature"&&verticalSection()?.actual_dataset)names.push(verticalSection().actual_dataset);
  const pending=names.filter(name=>!actualDatasets.has(name));
  if(!pending.length){setDataLoadStatus("");return;}
  const source=location.protocol==="file:"?"로컬 압축 데이터":"직접 셀 데이터";
  currentMode.textContent=`실제 1 s 셀값 로드 중 · ${pending.join(", ")}`;
  setDataLoadStatus(`결과 데이터 로드 중 · ${source} · ${pending.join(", ")}`);
  Promise.all(pending.map(loadActualDataset)).then(()=>{
    if(visualMode!==mode)return;
    const failed=pending.filter(name=>actualDatasetErrors.has(name));
    if(failed.length){
      currentMode.textContent=`실제값 로드 실패 · ${failed.join(", ")}`;
      const detail=actualDatasetErrors.get(failed[0]);
      setDataLoadStatus(`온도 색상 데이터 로드 실패 · ${failed.join(", ")} · viewer 폴더의 data 파일을 모두 함께 복사하십시오. (${detail})`,"error");
    }else{
      const sectionPosition=mode.startsWith("xy-")?` · ${horizontalSection()?.label||"Z = 50%"}`:mode.startsWith("xz-")?` · ${verticalSection()?.label||"50%"} · ${verticalSection()?.kind?.toUpperCase()||"세로"}`:"";
      const heatTransferDataset=mode==="surface-heat-transfer"?actualDatasets.get(cellSurfaceHeatTransfer?.actual_dataset):null;
      const coverage=heatTransferDataset?` · ${heatTransferDataset.specification.valid_count.toLocaleString()} / ${heatTransferDataset.specification.point_count.toLocaleString()} 표면점`:"";
      currentMode.textContent=(modeDescriptions[mode]||modeDescriptions.combined)[0]+sectionPosition+coverage;
      setDataLoadStatus("");
    }
    draw();
  });
}
function updateCanvasNote(mode=visualMode){
  const description=modeDescriptions[mode]||modeDescriptions.combined;
  const sectionPosition=mode.startsWith("xy-")?` · ${horizontalSection()?.label||"Z = 50%"}`:mode.startsWith("xz-")?` · ${verticalSection()?.label||"50%"} · ${verticalSection()?.kind?.toUpperCase()||"세로"}`:"";
  const temperatureNote=actualOneSecond?"T 원본 · 시간보간/패딩 없음":`온도 표시보정 +${flow.temperature_display_offset_c.toFixed(3)} K`;
  const sectionRenderNote=mode==="surface-heat-transfer"?" · q''=OpenFOAM wallHeatFlux · Tfluid=벽면 바깥 0.35 mm 냉각수 셀 · 고정 범례":mode.endsWith("-pressure")?" · p−pOUT 면적평균 · 공통 고정 범례 −500–+500 Pa · 범위 밖 값은 색상 포화":mode.endsWith("-mass-flow")?` · 미세 타일별 ṁ=ρ·|U·n|·ΔA · 공통 고정 범례 0–${massFlowDisplayRange()[1].toFixed(6)} kg/s`:mode.startsWith("xy-")||mode.startsWith("xz-")?(smoothSectionInput.checked?" · 실제 샘플 사이 화면만 부드럽게 · 무효영역 채움 없음":" · 실제 샘플 타일 그대로"):"";
  const selectedTemperatureSection=mode.startsWith("xy-")?horizontalSection():mode.startsWith("xz-")?verticalSection():null;
  const exactSolidCut=exactSolidSectionActive(selectedTemperatureSection);
  const horizontalProjectionNote=mode==="xy-temperature"&&!exactSolidCut?` · 배터리당 ${((cellSurface?.angular_segments||32)*horizontalCutAngularSubdivisions).toLocaleString()}방향 × ${horizontalCutRadialBands}반경 표시분할(보간)` :"";
  const verticalProjectionNote=mode==="xz-temperature"&&!exactSolidCut?` · 배터리당 ${((cellSurface?.axial_segments||16)*verticalCutAxialSubdivisions).toLocaleString()}높이 × ${verticalCutTransverseBands}가로 표시분할(보간)` :"";
  const solidCutNote=mode.endsWith("-temperature")&&mode!=="cell-temperature"?(exactSolidCut?" · 셀 전체표면 숨김 · 실제 교차 단면만 표시 · 1,800 s batterySolid 저장 단면값":" · 고체 내부 공간장 미저장 · 직접 표면+체적평균 제약 투영(표시 보간, 내부 CFD 셀 아님)"):"";
  const headScale=headDisplayScale(),headNote=visibility.particles?` · 헤드 ${headScale===1?"1× 실제속도":headScale+"× 표시속도"}`:"";
  const velocityMode=["combined","cell-temperature","velocity-streamlines","xy-velocity","xz-velocity","xy-mass-flow","xz-mass-flow"].includes(mode);
  const localPeak=selectedSectionVelocityPeak(mode),velocityNote=velocityMode?` · U 공통범위 0–${velocityDisplayMaximum().toFixed(3)} m/s${Number.isFinite(localPeak)?` · 현재 단면 최고 ${localPeak.toFixed(3)} m/s`:""}`:"";
  const dimensionNote=mode.endsWith("-location")?" · 3D 절단 위치 안내":(mode.startsWith("xy-")||mode.startsWith("xz-"))?` · ${section3DInput.checked?"3D 회전":"평면 보기"}`:"";
  canvasNote.textContent=`${description[1]}${sectionPosition}${sectionRenderNote}${solidCutNote}${horizontalProjectionNote}${verticalProjectionNote}${dimensionNote}${velocityNote}${headNote} · 드래그 회전 · 휠 확대/축소 · ${temperatureNote} · Ū·p̄ 실제 면적평균`;
}
function selectVisualMode(mode){
  visualMode=mode;
  setDataLoadStatus("");
  const description=modeDescriptions[mode]||modeDescriptions.combined;
  const sectionPosition=mode.startsWith("xy-")?` · ${horizontalSection()?.label||"Z = 50%"}`:mode.startsWith("xz-")?` · ${verticalSection()?.label||"50%"} · ${verticalSection()?.kind?.toUpperCase()||"세로"}`:"";
  currentMode.textContent=description[0]+sectionPosition;
  updateCanvasNote(mode);
  prepareActualMode(mode);
  controlHead.hidden=mode==="cell-extrema-map";
  horizontalSections.hidden=!mode.startsWith("xy-");
  verticalSections.hidden=!mode.startsWith("xz-");
  section3DOption.hidden=mode.endsWith("-location")||!(mode.startsWith("xy-")||mode.startsWith("xz-"));
  flowControls.hidden=!flow.enabled||mode.endsWith("-location");
  if(mode.endsWith("-location"))selectView("iso");
  else if(mode.startsWith("xy-"))selectView(section3DInput.checked?"iso":"pz");
  else if(mode.startsWith("xz-"))selectView(verticalViewName());
  else selectView("iso");
  requestAnimationFrame(resize);
}
modeFieldset.hidden=!flow.enabled;
for(const input of modeInputs){
  input.disabled=!flow.enabled||(input.value.endsWith("-pressure")&&!pressureAvailable());
  if(input.value.endsWith("-pressure")&&input.disabled)input.title="이 보존 결과에는 공간 압력장 원본이 없습니다";
  input.addEventListener("change",()=>{if(input.checked)selectVisualMode(input.value);});
}
function setLayerVisibility(values){
  Object.assign(visibility,values);
  for(const [name,input] of Object.entries(optionInputs))input.checked=visibility[name];
  draw();
}
displayOptions.hidden=!flow.enabled&&!activeMeshWireframe();
horizontalSections.hidden=true;
verticalSections.hidden=true;
for(const [name,input] of Object.entries(optionInputs)){
  input.disabled=name==="mesh"?!activeMeshWireframe():!flow.enabled;
  input.addEventListener("change",()=>{visibility[name]=input.checked;draw();});
}
smoothSectionInput.disabled=!flow.enabled;
smoothSectionInput.addEventListener("change",()=>{directXYVelocityCache=null;updateCanvasNote();draw();});
section3DInput.disabled=!flow.enabled;
section3DInput.addEventListener("change",()=>{
  prepareActualMode(visualMode);updateCanvasNote();
  if(visualMode.startsWith("xz-"))selectView(verticalViewName());
  else selectView(section3DInput.checked?"iso":"pz");
});
headRate.disabled=!flow.enabled;
headRate.addEventListener("change",()=>{updateCanvasNote();drawDynamicOverlay();});
function updateHorizontalSection({changeView=true}={}){
  let selected=horizontalSectionInputs.find(input=>input.checked&&!input.disabled);
  if(!selected){
    selected=horizontalSectionInputs.find(input=>input.value==="xy"&&!input.disabled)||horizontalSectionInputs.find(input=>!input.disabled);
    if(selected)selected.checked=true;
  }
  const section=selected?sections[selected.value]:null,percent=selected?.parentElement?.textContent?.trim()||"";
  if(visualMode==="xy-temperature"&&Number.isFinite(section?.snapshot_time_s)&&!section?.actual_dataset){
    simulationTime=Number(section.snapshot_time_s);flowPlaying=false;flowPlay.textContent="▶ 재생";updateFlowClock({force:true});
  }
  const snapshotNote=Number.isFinite(section?.snapshot_time_s)&&!section?.actual_dataset?` · ${section.snapshot_time_s} s 원본 고정 단면`:"";
  horizontalSectionState.textContent=selected?`${section?.label||selected.value} · ${percent}${snapshotNote}`:"단면 데이터 없음";
  if(visualMode==="xy-temperature")prepareActualMode(visualMode);
  if(changeView&&visualMode.startsWith("xy-"))selectVisualMode(visualMode);else draw();
}
for(const input of horizontalSectionInputs){
  input.disabled=!flow.enabled||!sections[input.value];
  input.addEventListener("change",()=>{if(input.checked)updateHorizontalSection();});
}
document.getElementById("horizontal-centre").addEventListener("click",()=>{
  const centreInput=horizontalSectionInputs.find(input=>input.value==="xy"&&!input.disabled);
  if(centreInput){centreInput.checked=true;updateHorizontalSection();}
});
updateHorizontalSection({changeView:false});
function updateVerticalSection({changeView=true}={}){
  let selected=verticalSectionInputs.find(input=>input.checked&&!input.disabled);
  if(!selected){
    selected=verticalSectionInputs.find(input=>input.value==="50"&&!input.disabled)||verticalSectionInputs.find(input=>!input.disabled);
    if(selected)selected.checked=true;
  }
  const section=verticalSection(),directTemperature=Math.round(simulationTime)===section?.solid_temperature_time_s,percent=selected?.parentElement?.textContent?.trim()||"";
  verticalSectionState.textContent=section?`${section.label} · ${percent} · ${section.kind.toUpperCase()} · ${directTemperature?"저장된 고체 내부 단면":"고체 내부장 미저장 · 직접 표면 온도 투영"}`:"단면 데이터 없음";
  if(visualMode==="xz-temperature")prepareActualMode(visualMode);
  if(changeView&&visualMode==="xz-location")selectView("iso");
  else if(changeView&&visualMode.startsWith("xz-"))selectView(verticalViewName());
  else draw();
}
for(const input of verticalSectionInputs)input.addEventListener("change",()=>{if(input.checked)updateVerticalSection();});
verticalSectionPlane.addEventListener("change",()=>updateVerticalSection());
document.getElementById("vertical-centre").addEventListener("click",()=>{
  const centreInput=verticalSectionInputs.find(input=>input.value==="50"&&!input.disabled);
  if(centreInput){centreInput.checked=true;updateVerticalSection();}
});
updateVerticalSection({changeView:false});
document.getElementById("flow-only").addEventListener("click",()=>{
  const input=modeInputs.find(candidate=>candidate.value==="velocity-streamlines");if(input)input.checked=true;
  setLayerVisibility({cells:false,flow:true,particles:true,section:false,grid:false,mesh:false,enclosure:true,temperatureLegend:false,velocityLegend:true,massFlowLegend:false,ports:true,extrema:true});selectVisualMode("velocity-streamlines");
});
document.getElementById("show-all").addEventListener("click",()=>{
  const input=modeInputs.find(candidate=>candidate.value==="combined");if(input)input.checked=true;
  setLayerVisibility({cells:true,flow:true,particles:true,section:true,grid:true,mesh:false,enclosure:true,temperatureLegend:true,velocityLegend:true,massFlowLegend:true,ports:true,extrema:true});selectVisualMode("combined");
});
document.getElementById("reset-overlays").addEventListener("click",()=>{overlayPositions={};saveOverlayPositions();draw();});
if(flow.enabled)selectVisualMode(visualMode);
flowControls.hidden=!flow.enabled||visualMode.endsWith("-location");
function formatWallDuration(seconds){
  const rounded=Math.max(0,Math.round(seconds)),minutes=Math.floor(rounded/60),remainder=rounded%60;
  return `${minutes}:${String(remainder).padStart(2,"0")}`;
}
function updateFlowDuration(){
  const rate=Number(flowRate.value);flowDuration.textContent=`${rate}× 완주 ${formatWallDuration(1800/rate)}`;
}
function updateFlowClock({force=false}={}){
  const second=Math.round(simulationTime);
  if(!force&&second===lastFlowClockSecond)return;
  lastFlowClockSecond=second;
  flowSeek.value=String(second);
  flowTime.textContent=`t = ${second} s / 1800 s`;
  if(visualMode.startsWith("xz-")&&verticalSectionState){
    const section=verticalSection(),selected=verticalSectionInputs.find(input=>input.checked),percent=selected?.parentElement?.textContent?.trim()||"";
    if(section){
      const directTemperature=second===section.solid_temperature_time_s;
      verticalSectionState.textContent=`${section.label} · ${percent} · ${section.kind.toUpperCase()} · ${directTemperature?"저장된 고체 내부 단면":"고체 내부장 미저장 · 직접 표면 온도 투영"}`;
    }
  }
}
if(flow.enabled){
  flowSampling.textContent=actualOneSecond?"CFD 원본 1 s · 직접 셀값 · 시간보간/패딩 없음 · 헤드 최대 90 FPS":"화면 1 s · CFD 원본 15 s 선형보간 · 헤드 최대 90 FPS";
  updateFlowClock({force:true});
  flowPlay.textContent=flowPlaying?"❚❚ 정지":"▶ 재생";
  flowPlay.addEventListener("click",()=>{flowPlaying=!flowPlaying;flowPlay.textContent=flowPlaying?"❚❚ 정지":"▶ 재생";draw();});
  flowSeek.addEventListener("input",()=>{simulationTime=Number(flowSeek.value);draw();});
  flowRate.addEventListener("change",updateFlowDuration);updateFlowDuration();
}
function animate(timestamp){
  if(previousFrameTime===0)previousFrameTime=timestamp;
  const elapsed=Math.min(.1,(timestamp-previousFrameTime)/1000);previousFrameTime=timestamp;
  if(flow.enabled&&flowPlaying&&figurePreview.hidden&&activeMeshRun()?.data_ready!==false){
    simulationTime=(simulationTime+elapsed*Number(flowRate.value))%1800;
    sceneRenderAccumulator+=elapsed;overlayRenderAccumulator+=elapsed;updateFlowClock();
    const actualFrame=Math.round(simulationTime);
    const timeVaryingScene=["combined","cell-temperature","cell-extrema-map","xy-temperature","xz-temperature","surface-heat-transfer"].includes(visualMode);
    const refreshScene=timeVaryingScene&&(actualOneSecond
      ?actualFrame!==lastSceneFrame&&sceneRenderAccumulator>=1/12
      :sceneRenderAccumulator>=1/12);
    if(refreshScene){sceneRenderAccumulator=0;overlayRenderAccumulator=0;draw();}
    else if(overlayRenderAccumulator>=1/overlayTargetFps-.001){overlayRenderAccumulator=Math.max(0,overlayRenderAccumulator-1/overlayTargetFps);drawDynamicOverlay();}
  }
  advanceVideoPlayback(elapsed);
  requestAnimationFrame(animate);
}
function rows(element,values){ element.innerHTML=Object.entries(values).map(([key,value])=>`<dt>${key}</dt><dd>${value}</dd>`).join(""); }
function formatAnalysisDuration(seconds){
  if(!Number.isFinite(Number(seconds)))return "기록 없음";
  const rounded=Math.max(0,Math.round(Number(seconds))),hours=Math.floor(rounded/3600),minutes=Math.floor((rounded%3600)/60),remainder=rounded%60;
  return `${hours}시간 ${String(minutes).padStart(2,"0")}분 ${String(remainder).padStart(2,"0")}초`;
}
function renderActiveMeshRun(){
  const run=activeMeshRun();if(!run)return;
  const counts=run.counts||{},prefix=run.counts_are_targets?"목표 ":"";
  const count=(value)=>Number.isFinite(value)?Number(value).toLocaleString():"—";
  const runtime=run.analysis_runtime||null,stageRuntime=runtime?.stage_wall_seconds||{};
  rows(meshRunDetail,{
    "상태":meshRunText(run,"state_label"),
    "전체 셀":`${prefix}${count(counts.total_cells)}`,
    "유체 / 고체":`${prefix}${count(counts.fluid_cells)} / ${count(counts.solid_cells)}`,
    "노드":`${prefix}${count(counts.points)}`,
    "요소":run.element_family,
    "해석 벽시계 시간":runtime?formatAnalysisDuration(runtime.wall_seconds):"기존 실행 기록 없음",
    "시간 구성":runtime?`메시 ${formatAnalysisDuration(stageRuntime.mesh)} · 정상해석 ${formatAnalysisDuration(stageRuntime.steady)} · 과도해석 ${formatAnalysisDuration(stageRuntime.transient)}`:"—",
    "병렬 계산":runtime?`솔버 ${runtime.solver_mpi_ranks} MPI · 메시 ${runtime.mesh_mpi_ranks} MPI`:"—"
  });
  meshRunNote.textContent=meshRunText(run,"note");
  const waiting=run.data_ready===false;
  const qualityWarning=Boolean(model.quality_warning)&&!waiting;
  const runLabel=meshRunText(run,"label"),runState=meshRunText(run,"state_label");
  document.getElementById("status").textContent=waiting?`${runLabel} · ${runState}`:(qualityWarning?`계산 완료 · 에너지 수지 경고 · ${runLabel} · 1초 단위`:(actualOneSecond?`계산 완료 · ${runLabel} · 1초 단위`:(model.review_only?`계산 완료 · 단일 메시 검토용 · ${runLabel} · 1초 단위`:model.status_label)));
  document.getElementById("result-badge").textContent=waiting?runState:(qualityWarning?"계산 완료 · 경고":(model.review_only?"계산 완료 · 검토용":(model.actual_solver_data?"계산 완료":"준비 중")));
  document.getElementById("calculation-summary").innerHTML=waiting
    ?`<strong>${runLabel}</strong> · 자체 CFD 결과 없음`
    :`<strong>${runLabel}</strong> · 0–1800 s`;
  document.getElementById("calculation-note").textContent=waiting?meshRunText(run,"note"):activeCalculationNote;
  document.getElementById("calculation-card").classList.toggle("validated",model.actual_solver_data&&!waiting&&!qualityWarning);
  document.getElementById("calculation-card").classList.toggle("quality-warning",qualityWarning);
  flowPlay.disabled=waiting||!flow.enabled;flowSeek.disabled=waiting||!flow.enabled;
  draw();
}
if(selectableMeshRuns.length){
  meshRunSelect.innerHTML=selectableMeshRuns.map(run=>`<option value="${run.id}">${meshRunText(run,"label")} · ${meshRunText(run,"state_label")}</option>`).join("");
  meshRunSelect.value=activeMeshRunId;
  const cadTab=document.getElementById("cad-tab");
  if(cadTab){
    const cadUrl=new URL(cadTab.getAttribute("href"),window.location.href);
    cadUrl.searchParams.set("mesh",activeMeshRunId);
    cadUrl.searchParams.set("lang",new URLSearchParams(window.location.search).get("lang")==="en"?"en":"ko");
    cadTab.href=cadUrl.toString();
  }
  const meshTab=document.getElementById("mesh-tab");
  if(meshTab){
    const meshUrl=new URL(meshTab.getAttribute("href"),window.location.href);
    meshUrl.searchParams.set("mesh",activeMeshRunId);
    meshUrl.searchParams.set("lang",new URLSearchParams(window.location.search).get("lang")==="en"?"en":"ko");
    meshTab.href=meshUrl.toString();
  }
  meshRunSelect.addEventListener("change",()=>{
    activeMeshRunId=meshRunSelect.value;
    const url=new URL(window.location.href);url.searchParams.set("mesh",activeMeshRunId);url.searchParams.set("autoplay","0");
    window.location.assign(url.toString());
  });
}else{document.querySelector(".mesh-run-card").hidden=true;}
const geometryRun=activeMeshRun(),inletDiameter=geometryRun?.inlet_diameter_mm??model.port_diameter_mm,outletDiameter=geometryRun?.outlet_diameter_mm??model.port_diameter_mm;
rows(document.getElementById("geometry"),{"해석 영역":`${box.x} × ${box.y} × ${box.z} mm`,"배터리":`${sceneCells.length}개 · Ø${model.cell_diameter_mm} × ${model.cell_length_mm} mm`,"입·출구":inletDiameter===outletDiameter?`각 Ø${inletDiameter} mm`:`Ø${inletDiameter} / Ø${outletDiameter} mm`,"셀 배열":"5–4–5–4–5 교차 배열"});
const heat=model.heat_source||null;
const heatRange=(start,end,digits,unit)=>Math.abs(Number(start)-Number(end))<1e-12
  ?`${Number(start).toFixed(digits)} ${unit}`
  :`${Number(start).toFixed(digits)} → ${Number(end).toFixed(digits)} ${unit}`;
rows(document.getElementById("derived"),{
  "계산 격자":actualOneSecond?.mesh_cell_count?`${actualOneSecond.mesh_cell_count.toLocaleString()} cells`:"—",
  "Reynolds 수":activeDerived.reynolds.toLocaleString(undefined,{maximumFractionDigits:1}),
  "발열 모델":heat?.label||activeDerived.heat_source_mode,
  "셀 발열 (0 → 1800 s)":heat?heatRange(heat.cell_heat_start_w,heat.cell_heat_end_w,6,"W"):`${activeDerived.cell_heat_w.toFixed(6)} W`,
  "23셀 명목 발열 (0 → 1800 s)":heat?heatRange(heat.total_heat_start_w,heat.total_heat_end_w,6,"W"):`${activeDerived.total_heat_w.toFixed(6)} W`,
  "체적발열 (0 → 1800 s)":heat?heatRange(heat.qvol_start_w_m3,heat.qvol_end_w_m3,2,"W/m³"):`${activeDerived.qvol_w_m3.toFixed(2)} W/m³`,
  "냉각수 밀도":`${activeInputs.fluid_density_kg_m3} kg/m³`,
  "비열 Cp":`${activeInputs.fluid_cp_J_kgK} J/(kg·K)`,
  "점도 μ":`${activeInputs.dynamic_viscosity_Pa_s} Pa·s`,
  "열전도율 k":`${activeInputs.fluid_k_W_mK} W/(m·K)`
});
const rawInletC=activeInputs.inlet_temperature_K-273.15;
rows(document.getElementById("inputs"),{
  "냉각수":"물 (Water)",
  "입구 온도":`${rawInletC.toFixed(3)} °C`,
  "입구 속도":`${activeInputs.inlet_velocity_m_s.toFixed(3)} m/s`,
  "출구 압력":flow.port_flow?`${(flow.port_flow.outlet_area_average_pressure_pa/1000).toFixed(3)} kPa`:"—",
  "해석 시간":"0–1800 s · 1 s 간격"
});
const figureGrid=document.getElementById("figures"),figureDetail=document.getElementById("figure-detail");
const figurePreview=document.getElementById("figure-preview"),figureImage=document.getElementById("figure-image"),figureVideo=document.getElementById("figure-video"),artifactTabs=document.getElementById("artifact-tabs");
const meshFigurePanel=document.getElementById("mesh-figure-panel"),meshFigureCanvas=document.getElementById("mesh-figure-canvas"),meshFigureTitle=document.getElementById("mesh-figure-title"),meshFigureStatus=document.getElementById("mesh-figure-status");
const meshFigureImageGrid=document.getElementById("mesh-figure-image-grid"),meshFigureCanvasWrap=document.getElementById("mesh-figure-canvas-wrap");
const meshFigureClose=document.getElementById("mesh-figure-close"),meshFigurePng=document.getElementById("mesh-figure-png");
const videoControls=document.getElementById("video-controls"),playbackRate=document.getElementById("playback-rate"),videoTime=document.getElementById("video-time"),videoSemantics=document.getElementById("video-semantics"),videoDuration=document.getElementById("video-duration");
const videoPlay=document.getElementById("video-play"),videoSeek=document.getElementById("video-seek"),videoPrev=document.getElementById("video-prev"),videoNext=document.getElementById("video-next");
const temperatureGraphPanel=document.getElementById("temperature-chart-panel");
const temperatureChartCanvas=document.getElementById("temperature-chart-canvas");
const temperatureChartOpen=document.getElementById("temperature-chart-open");
const temperatureChartClose=document.getElementById("temperature-chart-close");
const temperatureChartMetric=document.getElementById("temperature-chart-metric");
const perCellMetric=document.getElementById("per-cell-metric");
const perCellMetricLabel=document.getElementById("per-cell-metric-label");
const cellExtremaCell=document.getElementById("cell-extrema-cell");
const cellExtremaCellLabel=document.getElementById("cell-extrema-cell-label");
const temperatureChartStatus=document.getElementById("temperature-chart-status");
const temperatureChartPng=document.getElementById("temperature-chart-png");
const temperatureChartCsv=document.getElementById("temperature-chart-csv");
const temperatureChartAllCsv=document.getElementById("temperature-chart-all-csv");
const temperatureGraphSummary=document.getElementById("temperature-graph-summary");
const convergenceGraphPanel=document.getElementById("convergence-chart-panel");
const convergenceChartCanvas=document.getElementById("convergence-chart-canvas");
const convergenceChartOpen=document.getElementById("convergence-chart-open");
const convergenceChartClose=document.getElementById("convergence-chart-close");
const convergenceChartStatus=document.getElementById("convergence-chart-status");
const convergenceChartPng=document.getElementById("convergence-chart-png");
const convergenceChartCsv=document.getElementById("convergence-chart-csv");
const convergenceSummary=document.getElementById("convergence-summary");
const convergenceHistorySelect=document.getElementById("convergence-history-select");
const failedConvergenceCard=document.getElementById("failed-convergence-card");
const failedConvergenceSummary=document.getElementById("failed-convergence-summary");
const failedConvergenceChartOpen=document.getElementById("failed-convergence-chart-open");
const temperatureMetricMeta={
  outlet:{title:"출구 평균 온도",filename:"outlet-average",key:"outlet_mass_weighted_average_c",definition:"출구 phi 질량유량 가중 평균"},
  "battery-maximum":{title:"배터리 최고 온도",filename:"battery-mesh-maximum",key:"battery_mesh_maximum_c",definition:"C01–C23 cell zone의 mesh max(T) 중 최고"},
  "surface-average":{title:"배터리 표면 평균 온도",filename:"battery-surface-average",key:"battery_surface_area_average_c",definition:"23개 셀 interface의 실제 면적가중 평균"},
  "internal-average":{title:"배터리 내부 온도",filename:"battery-internal-average",key:"battery_internal_volume_average_c",definition:"23개 batterySolid cell zone의 실제 체적가중 평균"}
};
const perCellMetricMeta={
  internal_volume_average_c:["내부 체적평균","per-cell-internal-average"],
  surface_area_average_c:["표면 면적평균","per-cell-surface-average"],
  mesh_maximum_c:["mesh 최고값","per-cell-mesh-maximum"]
};
for(const cell of [...sceneCells].sort((a,b)=>a.id.localeCompare(b.id))){
  const option=document.createElement("option");option.value=cell.id;option.textContent=cell.id;cellExtremaCell.appendChild(option);
}
function decodedActualTemperature(dataset,encoded){
  if(dataset.specification.dtype==="float32-little-endian")return Number.isFinite(encoded)?encoded:null;
  const scale=actualOneSecond?.temperature_encoding;
  return scale?scale.minimum_k+encoded*scale.quantum_k-273.15:null;
}
function buildCellTemperatureExtrema(cellId){
  const cacheKey=`${activeMeshRun()?.id||"current"}:${cellId}`;
  const dataset=actualDatasets.get(cellSurface?.actual_dataset);
  if(cellTemperatureExtremaCache.has(cacheKey))return cellTemperatureExtremaCache.get(cacheKey);
  // Do not cache an empty fallback while the direct one-second surface data
  // are still loading.  The map redraws with the real samples after loading.
  if(actualOneSecond&&cellSurface?.actual_dataset&&!dataset){
    return {minimum:[],maximum:temperatureGraphs?.per_cell?.mesh_maximum_c?.[cellId]||[]};
  }
  let minimum=[],sampledMaximum=[];
  const cellIndex=sceneCells.findIndex(cell=>cell.id===cellId);
  if(dataset&&cellIndex>=0){
    const count=dataset.specification.samples_per_cell,start=cellIndex*count;
    for(let frame=0;frame<dataset.specification.frame_count;frame++){
      let low=Infinity,high=-Infinity;
      const offset=frame*dataset.specification.valid_count;
      for(let pointIndex=start;pointIndex<start+count;pointIndex++){
        const validIndex=dataset.lookup[pointIndex];if(validIndex<0)continue;
        const value=decodedActualTemperature(dataset,dataset.encoded[offset+validIndex]);
        if(!Number.isFinite(value))continue;low=Math.min(low,value);high=Math.max(high,value);
      }
      minimum.push(Number.isFinite(low)?low:null);sampledMaximum.push(Number.isFinite(high)?high:null);
    }
  }else{
    const frames=cellSurface?.temperature_frames_c?.[cellId]||[];
    minimum=frames.map(values=>Math.min(...values.filter(Number.isFinite)));
    sampledMaximum=frames.map(values=>Math.max(...values.filter(Number.isFinite)));
  }
  const storedMaximum=temperatureGraphs?.per_cell?.mesh_maximum_c?.[cellId]||[];
  const maximum=storedMaximum.length===minimum.length?storedMaximum:sampledMaximum;
  const result={minimum,maximum};cellTemperatureExtremaCache.set(cacheKey,result);return result;
}
function cellTemperatureExtremaAtTime(cellId){
  const extrema=buildCellTemperatureExtrema(cellId),dataset=actualDatasets.get(cellSurface?.actual_dataset);
  const storedMaximum=temperatureGraphs?.per_cell?.mesh_maximum_c?.[cellId]||[];
  const second=clamp(Math.round(simulationTime),0,1800);
  let sampleIndex=second;
  if(dataset){
    const step=Number(dataset.specification.time_step_s)||1,first=Number(dataset.specification.first_time_s)||0;
    sampleIndex=Math.round((simulationTime-first)/step);
  }
  const seriesValue=(values,index)=>Array.isArray(values)&&values.length
    ?values[clamp(index,0,values.length-1)]:null;
  const maximum=storedMaximum.length?seriesValue(storedMaximum,second):seriesValue(extrema.maximum,sampleIndex);
  const minimum=seriesValue(extrema.minimum,sampleIndex);
  return {maximum:Number.isFinite(maximum)?maximum:null,minimum:Number.isFinite(minimum)?minimum:null};
}
function drawCellExtremaMap(w,h){
  ctx.save();
  ctx.fillStyle="#07131e";ctx.fillRect(0,0,w,h);
  const titleSize=clamp(Math.round(w/62),19,27),subtitleSize=clamp(Math.round(w/92),13,17);
  ctx.textAlign="center";ctx.textBaseline="middle";
  ctx.fillStyle="#edf7ff";ctx.font=`800 ${titleSize}px system-ui`;
  ctx.fillText("각 배터리 셀 최고·최저 온도",w/2,27);
  ctx.fillStyle="#9fc0d5";ctx.font=`700 ${subtitleSize}px system-ui`;
  ctx.fillText(`t = ${Math.round(simulationTime).toLocaleString()} s · ▲ 최고 · ▼ 최저 · °C · 매초 갱신`,w/2,53);

  const top=74,bottom=44,side=42,rotatedWidth=box.y,rotatedHeight=box.x;
  const scale=Math.max(.1,Math.min((w-side*2)/rotatedWidth,(h-top-bottom)/rotatedHeight));
  const left=(w-rotatedWidth*scale)/2,layoutTop=top+Math.max(0,(h-top-bottom-rotatedHeight*scale)/2);
  // Rotate this plan view in the opposite 90-degree direction: the 5–4–5–4–5
  // rows run horizontally, IN is lower-left, and OUT is upper-right.
  const screenPoint=(x,y)=>({x:left+(box.y-y)*scale,y:layoutTop+(box.x-x)*scale});
  ctx.beginPath();
  sceneOutline.forEach((point,index)=>{const p=screenPoint(point[0],point[1]);if(index===0)ctx.moveTo(p.x,p.y);else ctx.lineTo(p.x,p.y);});
  ctx.closePath();ctx.fillStyle="#eaf3f9";ctx.fill();ctx.strokeStyle="#7293aa";ctx.lineWidth=2;ctx.stroke();

  const range=sharedTemperatureRange(),portByCell=new Map();
  for(const port of scenePorts){
    const nearest=sceneCells.reduce((best,cell)=>{
      const distance=Math.hypot(cell.x-port.x,cell.y-port.y);
      return !best||distance<best.distance?{cell,distance}:best;
    },null);
    if(nearest)portByCell.set(nearest.cell.id,port);
  }
  const cellScreenPoints=sceneCells.map(cell=>screenPoint(cell.x,cell.y));
  let nearestCellSpacing=Infinity;
  for(let i=0;i<cellScreenPoints.length;i++)for(let j=i+1;j<cellScreenPoints.length;j++){
    nearestCellSpacing=Math.min(nearestCellSpacing,Math.hypot(
      cellScreenPoints[i].x-cellScreenPoints[j].x,
      cellScreenPoints[i].y-cellScreenPoints[j].y
    ));
  }
  for(const cell of sceneCells){
    const p=screenPoint(cell.x,cell.y),radius=Math.max(16,cell.radius*scale),values=cellTemperatureExtremaAtTime(cell.id);
    const colourValue=Number.isFinite(values.maximum)?clamp((values.maximum-range[0])/(range[1]-range[0])):.12;
    const [red,green,blue]=cellSurfacePalette(colourValue),port=portByCell.get(cell.id);
    ctx.beginPath();ctx.arc(p.x,p.y,radius,0,Math.PI*2);ctx.fillStyle=`rgb(${red},${green},${blue})`;ctx.fill();
    ctx.strokeStyle=port?.id==="IN"?"#0878e8":port?.id==="OUT"?"#e52d31":"rgba(8,45,74,.72)";
    ctx.lineWidth=port?3.4:1.25;ctx.stroke();
    // Size the labels from both the cell radius and the closest cell-to-cell
    // spacing.  Values may extend slightly beyond the circle, but the spacing
    // cap keeps neighbouring cell labels from touching each other.
    const spacingValueCap=Number.isFinite(nearestCellSpacing)
      ?clamp(Math.floor(nearestCellSpacing/4.25),16,28):28;
    const valueSize=Math.min(clamp(Math.floor(radius*.52),16,28),spacingValueCap);
    const idSize=Math.min(clamp(Math.floor(radius*.46),15,23),Math.max(15,valueSize-3));
    ctx.shadowColor="transparent";ctx.shadowBlur=0;ctx.shadowOffsetY=0;ctx.lineJoin="round";
    const outlinedText=(text,y,colour,size)=>{
      ctx.font=`900 ${size}px system-ui`;ctx.lineWidth=Math.max(2.5,size*.24);
      ctx.strokeStyle="rgba(255,255,255,.96)";ctx.strokeText(text,p.x,y);
      ctx.fillStyle=colour;ctx.fillText(text,p.x,y);
    };
    outlinedText(cell.id,p.y-radius*.48,"#07131e",idSize);
    // Three evenly separated rows keep the enlarged values readable.  The
    // lowest row can cross the circle edge on compact screens by design.
    const maximumText=Number.isFinite(values.maximum)?`▲${values.maximum.toFixed(2)}`:"▲—";
    const minimumText=Number.isFinite(values.minimum)?`▼${values.minimum.toFixed(2)}`:"▼—";
    outlinedText(maximumText,p.y+.02*radius,"#8b1028",valueSize);
    outlinedText(minimumText,p.y+.54*radius,"#003f73",valueSize);
    if(port){
      const diameter=Number(port.radius)*2,label=`${port.id} Ø${Number.isFinite(diameter)?diameter.toFixed(0):"—"}`;
      ctx.fillStyle=port.id==="IN"?"#075fc5":"#d12228";ctx.font=`900 ${clamp(valueSize,11,15)}px system-ui`;
      ctx.fillText(label,p.x,p.y-radius-12);
    }
  }
  const dimensionY=Math.min(h-15,layoutTop+rotatedHeight*scale+22),start=screenPoint(0,box.y),end=screenPoint(0,0);
  ctx.strokeStyle="#9fc0d5";ctx.lineWidth=1.3;ctx.beginPath();ctx.moveTo(start.x,dimensionY);ctx.lineTo(end.x,dimensionY);ctx.stroke();
  ctx.beginPath();ctx.moveTo(start.x,dimensionY-5);ctx.lineTo(start.x,dimensionY+5);ctx.moveTo(end.x,dimensionY-5);ctx.lineTo(end.x,dimensionY+5);ctx.stroke();
  ctx.fillStyle="#d8e8f2";ctx.font=`750 ${subtitleSize}px system-ui`;ctx.fillText(`${Number(box.y).toFixed(0)} mm`,(start.x+end.x)/2,dimensionY-9);
  ctx.restore();
}
async function ensureCellTemperatureExtrema(cellId){
  if(actualOneSecond&&cellSurface?.actual_dataset)await loadActualDataset(cellSurface.actual_dataset);
  return buildCellTemperatureExtrema(cellId);
}
function temperatureSeriesColour(index,count){
  if(count===1)return "#1565c0";
  // Twenty-three individual cells need hue spacing, but every trace must
  // retain enough luminance contrast on white for exported figures.
  return `hsl(${(215+index*137.508)%360} 82% 39%)`;
}
function activeTemperatureGraph(){
  const metric=temperatureChartMetric.value;
  if(metric==="energy-balance"){
    return {kind:"energy-balance",title:"에너지 수지",filename:"energy-balance",
      definition:"발열량 = 출구 제거 열량 + 내부 저장 열량 + 수치 불균형",
      times:energyBalance.times_s||[],series:[
        {label:"배터리 발열",values:energyBalance.generated_w||[],colour:"#c62828",axis:"power",unit:"W"},
        {label:"출구 제거열",values:energyBalance.removed_w||[],colour:"#1565c0",axis:"power",unit:"W"},
        {label:"내부 저장열",values:energyBalance.stored_w||[],colour:"#2e7d32",axis:"power",unit:"W"},
        {label:"불균형률",values:energyBalance.imbalance_percent||[],colour:"#7b1fa2",axis:"percent",unit:"%"}
      ]};
  }
  if(metric==="per-cell"){
    const source=temperatureGraphs?.per_cell?.[perCellMetric.value]||{};
    const [basis,filename]=perCellMetricMeta[perCellMetric.value];
    return {title:`각 배터리별 온도 · ${basis}`,filename,definition:`C01–C23 각각의 ${basis}`,
      series:Object.entries(source).map(([label,values],index,array)=>({label,values,colour:temperatureSeriesColour(index,array.length)}))};
  }
  if(metric==="cell-extrema"){
    const cellId=cellExtremaCell.value||"C01",extrema=buildCellTemperatureExtrema(cellId);
    return {kind:"cell-extrema",title:`${cellId} 최고·최저 온도`,filename:`${cellId.toLowerCase()}-temperature-extrema`,
      definition:`${cellId} 배터리 셀의 매초 최고·최저 온도`,series:[
        {label:`${cellId} 최고온도`,values:extrema.maximum||[],colour:"#c62828",emphasis:true},
        {label:`${cellId} 최저온도`,values:extrema.minimum||[],colour:"#1565c0",emphasis:true}
      ]};
  }
  if(metric==="key-cells"){
    const source=temperatureGraphs?.per_cell?.[perCellMetric.value]||{};
    const [basis,filename]=perCellMetricMeta[perCellMetric.value];
    const colours=["#c62828","#1565c0","#2e7d32","#7b1fa2","#00838f"];
    const cells=model.featured_temperature_cells||[];
    return {kind:"key-cells",title:`주요 배터리 5셀 온도 · ${basis}`,filename:`key-cells-${filename}`,
      definition:`외곽 4셀과 중앙 1셀의 ${basis}`,
      series:cells.map((cell,index)=>({label:`${cell.id} ${cell.position}`,values:source[cell.id]||[],
        colour:colours[index],emphasis:cell.position==="중앙"})).filter(line=>line.values.length===1801)};
  }
  const meta=temperatureMetricMeta[metric]||temperatureMetricMeta.outlet;
  return {title:meta.title,filename:meta.filename,definition:meta.definition,
    series:[{label:meta.title,values:temperatureGraphs?.[meta.key]||[],colour:temperatureSeriesColour(0,1)}]};
}
function paintEnergyBalanceChart(context,width,height,graph){
  const powerSeries=graph.series.filter(line=>line.axis==="power"),percentSeries=graph.series.filter(line=>line.axis==="percent"),times=graph.times;
  context.clearRect(0,0,width,height);context.fillStyle="#ffffff";context.fillRect(0,0,width,height);
  context.fillStyle="#17232d";context.font="700 18px system-ui";context.textAlign="left";context.fillText(tr("에너지 수지 · 실제 CFD 후처리값"),24,28);
  const powers=powerSeries.flatMap(line=>line.values).filter(Number.isFinite),percents=percentSeries.flatMap(line=>line.values).filter(Number.isFinite);
  if(!times.length||!powers.length||!percents.length){context.fillStyle="#c62828";context.font="700 14px system-ui";context.fillText(tr("저장된 에너지 수지 데이터가 없습니다."),24,62);return;}
  const left=78,right=78,top=70,bottom=58,plotWidth=Math.max(100,width-left-right),plotHeight=Math.max(100,height-top-bottom);
  const timeLow=0,timeHigh=Math.max(...times),timeSpan=Math.max(1,timeHigh-timeLow);
  const powerLow=0,powerHigh=Math.max(1,Math.max(...powers)*1.08),percentLow=0,percentHigh=Math.max(.01,Math.max(...percents)*1.08);
  context.font="700 10px ui-monospace,monospace";
  graph.series.forEach((line,index)=>{const x=left+index*plotWidth/4,y=51;context.fillStyle=line.colour;context.fillRect(x,y-7,13,3);context.fillStyle="#243946";context.fillText(`${tr(line.label)} [${line.unit}]`,x+18,y-3);});
  context.strokeStyle="#8495a0";context.lineWidth=1;context.strokeRect(left,top,plotWidth,plotHeight);
  context.font="600 11px ui-monospace,monospace";
  for(let tick=0;tick<=6;tick++){
    const fraction=tick/6,x=left+fraction*plotWidth,time=Math.round(timeLow+fraction*timeSpan),y=top+plotHeight-fraction*plotHeight;
    context.strokeStyle="#e4ebef";context.beginPath();context.moveTo(x,top);context.lineTo(x,top+plotHeight);context.stroke();
    context.fillStyle="#526774";context.textAlign=tick===0?"left":tick===6?"right":"center";context.fillText(String(time),x,top+plotHeight+20);
    context.textAlign="right";context.fillText((powerLow+fraction*(powerHigh-powerLow)).toFixed(2),left-9,y+4);
    context.textAlign="left";context.fillStyle="#7b1fa2";context.fillText((percentLow+fraction*(percentHigh-percentLow)).toFixed(2),left+plotWidth+9,y+4);
  }
  context.textAlign="center";context.fillStyle="#243946";context.font="700 12px system-ui";context.fillText(tr("시간 [s]"),left+plotWidth/2,height-16);
  context.save();context.translate(18,top+plotHeight/2);context.rotate(-Math.PI/2);context.fillText(tr("열량률 [W]"),0,0);context.restore();
  context.save();context.fillStyle="#7b1fa2";context.translate(width-18,top+plotHeight/2);context.rotate(Math.PI/2);context.fillText(tr("불균형률 [%]"),0,0);context.restore();
  for(const line of graph.series){
    const high=line.axis==="percent"?percentHigh:powerHigh;
    context.strokeStyle=line.colour;context.lineWidth=line.axis==="percent"?2:2.4;context.globalAlpha=.96;context.beginPath();
    line.values.forEach((value,index)=>{const x=left+(times[index]-timeLow)/timeSpan*plotWidth,y=top+(high-value)/high*plotHeight;if(index===0)context.moveTo(x,y);else context.lineTo(x,y);});
    context.stroke();context.globalAlpha=1;
  }
  const markerX=left+clamp(simulationTime/timeHigh)*plotWidth;
  context.strokeStyle="#6b7280";context.lineWidth=1;context.beginPath();context.moveTo(markerX,top);context.lineTo(markerX,top+plotHeight);context.stroke();
  context.fillStyle="#374151";context.font="700 10px ui-monospace,monospace";context.textAlign="center";context.fillText(`${Math.round(simulationTime)} s`,markerX,top-8);
}
function paintTemperatureChart(context,width,height){
  const graph=activeTemperatureGraph(),series=graph.series;
  if(graph.kind==="energy-balance"){paintEnergyBalanceChart(context,width,height,graph);return;}
  context.clearRect(0,0,width,height);context.fillStyle="#ffffff";context.fillRect(0,0,width,height);
  context.fillStyle="#17232d";context.font="700 18px system-ui";context.fillText(tr(`${graph.title} · 정밀 CFD 실제값 · 1 s`),24,28);
  const values=series.flatMap(line=>line.values).filter(Number.isFinite);
  if(!values.length){context.fillStyle="#c62828";context.font="700 14px system-ui";context.fillText(tr("실제 온도 시계열 데이터가 없습니다."),24,62);return;}
  let low=Math.min(...values),high=Math.max(...values),span=Math.max(high-low,.002),padding=Math.max(span*.08,.001);
  low-=padding;high+=padding;
  const left=76,right=24,legendWidth=graph.kind==="key-cells"?150:72;
  const legendColumns=Math.max(1,Math.min(series.length,Math.floor((width-left-right)/legendWidth)));
  const legendRows=Math.ceil(series.length/legendColumns),top=48+legendRows*17,bottom=56;
  const plotWidth=Math.max(100,width-left-right),plotHeight=Math.max(100,height-top-bottom);
  context.font="700 10px ui-monospace,monospace";
  series.forEach((line,index)=>{
    const column=index%legendColumns,row=Math.floor(index/legendColumns),x=left+column*((width-left-right)/legendColumns),y=47+row*17;
    context.fillStyle=line.colour;context.fillRect(x,y-8,12,3);context.fillStyle="#243946";context.fillText(tr(line.label),x+17,y-4);
  });
  context.strokeStyle="#8495a0";context.lineWidth=1;context.strokeRect(left,top,plotWidth,plotHeight);
  context.font="600 11px ui-monospace,monospace";
  for(let tick=0;tick<=6;tick++){
    const fraction=tick/6,x=left+fraction*plotWidth,time=Math.round(1800*fraction);
    context.strokeStyle="#e4ebef";context.beginPath();context.moveTo(x,top);context.lineTo(x,top+plotHeight);context.stroke();
    context.fillStyle="#526774";context.textAlign=tick===0?"left":tick===6?"right":"center";context.fillText(String(time),x,top+plotHeight+20);
  }
  for(let tick=0;tick<=6;tick++){
    const fraction=tick/6,y=top+plotHeight-fraction*plotHeight,value=low+fraction*(high-low);
    context.strokeStyle="#e4ebef";context.beginPath();context.moveTo(left,y);context.lineTo(left+plotWidth,y);context.stroke();
    context.fillStyle="#526774";context.textAlign="right";context.fillText(value.toFixed(3),left-9,y+4);
  }
  context.textAlign="center";context.fillStyle="#243946";context.font="700 12px system-ui";context.fillText(tr("시간 [s]"),left+plotWidth/2,height-16);
  context.save();context.translate(18,top+plotHeight/2);context.rotate(-Math.PI/2);context.fillText(tr("온도 [°C]"),0,0);context.restore();
  for(const line of series){
    context.strokeStyle=line.colour;context.lineWidth=line.emphasis?3:series.length<=5?2.2:1.15;
    context.globalAlpha=series.length<=5?.96:.78;context.beginPath();
    line.values.forEach((value,index)=>{
      const x=left+index/Math.max(1,line.values.length-1)*plotWidth,y=top+(high-value)/(high-low)*plotHeight;
      if(index===0)context.moveTo(x,y);else context.lineTo(x,y);
    });
    context.stroke();context.globalAlpha=1;
  }
  const markerX=left+clamp(simulationTime/1800)*plotWidth;
  context.strokeStyle="#6b7280";context.lineWidth=1;context.beginPath();context.moveTo(markerX,top);context.lineTo(markerX,top+plotHeight);context.stroke();
  context.fillStyle="#374151";context.font="700 10px ui-monospace,monospace";context.textAlign="center";context.fillText(`${Math.round(simulationTime)} s`,markerX,top-7);
}
function updateTemperatureChartStatus(){
  const graph=activeTemperatureGraph(),last=1800;
  if(graph.kind==="energy-balance"){
    const final=energyBalance.final;
    temperatureChartStatus.textContent=final?`${final.time_s.toLocaleString()} s · 발열 ${final.generated_w.toFixed(6)} W · 제거 ${final.removed_w.toFixed(6)} W · 저장 ${final.stored_w.toFixed(6)} W · 불균형 ${final.imbalance_percent.toFixed(4)}%`:"에너지 수지 데이터 없음";
    return;
  }
  if(!graph.series.length){temperatureChartStatus.textContent="해당 셀의 실제 온도 데이터 없음";return;}
  if(graph.kind==="cell-extrema"){
    const time=clamp(Math.round(simulationTime),0,1800),maximum=graph.series[0].values[time],minimum=graph.series[1].values[time];
    temperatureChartStatus.textContent=Number.isFinite(maximum)&&Number.isFinite(minimum)
      ?`${graph.definition} · ${time} s · 최고 ${maximum.toFixed(6)} °C · 최저 ${minimum.toFixed(6)} °C · 온도차 ${(maximum-minimum).toFixed(6)} K`
      :"해당 셀의 실제 최고·최저 온도 데이터 없음";
    return;
  }
  if(graph.series.length===1){
    const value=graph.series[0].values[last];temperatureChartStatus.textContent=`${graph.definition} · 1800 s = ${value.toFixed(6)} °C`;
  }else{
    const final=graph.series.map(line=>({label:line.label,value:line.values[last]})).sort((a,b)=>b.value-a.value);
    temperatureChartStatus.textContent=`${graph.definition} · 1800 s 최고 ${final[0].label} ${final[0].value.toFixed(6)} °C · 최저 ${final.at(-1).label} ${final.at(-1).value.toFixed(6)} °C`;
  }
}
function drawTemperatureChart(){
  if(temperatureGraphPanel.hidden)return;
  const rect=temperatureChartCanvas.getBoundingClientRect();if(rect.width<1||rect.height<1)return;
  const ratio=Math.min(window.devicePixelRatio||1,2);
  temperatureChartCanvas.width=Math.round(rect.width*ratio);temperatureChartCanvas.height=Math.round(rect.height*ratio);
  const context=temperatureChartCanvas.getContext("2d");context.setTransform(ratio,0,0,ratio,0,0);
  paintTemperatureChart(context,rect.width,rect.height);updateTemperatureChartStatus();
}
function downloadBlob(blob,filename){
  const link=document.createElement("a"),url=URL.createObjectURL(blob);link.href=url;link.download=filename;
  document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function csvValue(value){return `"${String(value).replaceAll('"','""')}"`;}
function downloadCsv(filename,headers,rowValues){
  const lines=[headers.map(csvValue).join(",")];
  for(let time=0;time<=1800;time++)lines.push(rowValues(time).map(value=>csvValue(typeof value==="number"?value.toFixed(8):value)).join(","));
  downloadBlob(new Blob(["\ufeff",lines.join("\n")],{type:"text/csv;charset=utf-8"}),filename);
}
async function updateTemperatureChart(){
  const perCell=["per-cell","key-cells"].includes(temperatureChartMetric.value);
  const extrema=temperatureChartMetric.value==="cell-extrema";
  perCellMetric.hidden=!perCell;perCellMetricLabel.hidden=!perCell;cellExtremaCell.hidden=!extrema;cellExtremaCellLabel.hidden=!extrema;
  if(extrema){temperatureChartStatus.textContent="배터리 셀 최고·최저 온도 계산 중…";await ensureCellTemperatureExtrema(cellExtremaCell.value||"C01");}
  drawTemperatureChart();
}
temperatureChartMetric.addEventListener("change",updateTemperatureChart);
perCellMetric.addEventListener("change",drawTemperatureChart);
cellExtremaCell.addEventListener("change",updateTemperatureChart);
temperatureChartOpen.addEventListener("click",async()=>{
  if(!temperatureGraphs)return;
  flowPlaying=false;flowPlay.textContent="▶ 재생";videoPlaying=false;figureVideo.pause();figurePreview.hidden=true;meshFigurePanel.hidden=true;convergenceGraphPanel.hidden=true;
  viewerControls.hidden=true;flowControls.hidden=true;canvasNote.hidden=true;temperatureGraphPanel.hidden=false;setDataLoadStatus("");
  await updateTemperatureChart();requestAnimationFrame(drawTemperatureChart);
});
temperatureChartClose.addEventListener("click",()=>{
  temperatureGraphPanel.hidden=true;viewerControls.hidden=false;flowControls.hidden=!flow.enabled;canvasNote.hidden=false;
  modeFieldset.hidden=!flow.enabled;displayOptions.hidden=!flow.enabled;horizontalSections.hidden=!flow.enabled||!visualMode.startsWith("xy-");verticalSections.hidden=!flow.enabled||!visualMode.startsWith("xz-");resize();
});
temperatureChartPng.addEventListener("click",()=>{
  const graph=activeTemperatureGraph(),exportCanvas=document.createElement("canvas");exportCanvas.width=1600;exportCanvas.height=900;
  paintTemperatureChart(exportCanvas.getContext("2d"),1600,900);exportCanvas.toBlob(blob=>{if(blob)downloadBlob(blob,`${graph.filename}.png`);},"image/png");
});
temperatureChartCsv.addEventListener("click",()=>{
  const graph=activeTemperatureGraph();
  if(graph.kind==="energy-balance"){
    const lines=[["time_s","generated_W","removed_W","stored_W","imbalance_percent"].map(csvValue).join(",")];
    graph.times.forEach((time,index)=>lines.push([time,...graph.series.map(line=>line.values[index])].map(value=>csvValue(typeof value==="number"?value.toFixed(10):value)).join(",")));
    downloadBlob(new Blob(["\ufeff",lines.join("\n")],{type:"text/csv;charset=utf-8"}),"energy-balance.csv");return;
  }
  downloadCsv(`${graph.filename}.csv`,["time_s",...graph.series.map(line=>`${line.label}_C`)],time=>[time,...graph.series.map(line=>line.values[time])]);
});
temperatureChartAllCsv.addEventListener("click",async()=>{
  const cells=Object.keys(temperatureGraphs?.per_cell?.internal_volume_average_c||{});
  for(const cell of cells)await ensureCellTemperatureExtrema(cell);
  const extrema=Object.fromEntries(cells.map(cell=>[cell,buildCellTemperatureExtrema(cell)]));
  const headers=["time_s","outlet_mass_weighted_average_C","battery_mesh_maximum_C","battery_surface_area_average_C","battery_internal_volume_average_C",...cells.flatMap(cell=>[`${cell}_minimum_C`,`${cell}_maximum_C`])];
  downloadCsv("all-temperature-metrics.csv",headers,time=>[time,temperatureGraphs.outlet_mass_weighted_average_c[time],temperatureGraphs.battery_mesh_maximum_c[time],temperatureGraphs.battery_surface_area_average_c[time],temperatureGraphs.battery_internal_volume_average_c[time],...cells.flatMap(cell=>[extrema[cell].minimum[time],extrema[cell].maximum[time]])]);
});
if(temperatureGraphs){
  const last=1800;
  rows(temperatureGraphSummary,{"출구 평균":`${temperatureGraphs.outlet_mass_weighted_average_c[last].toFixed(4)} °C`,"배터리 최고":`${temperatureGraphs.battery_mesh_maximum_c[last].toFixed(4)} °C`,"표면 평균":`${temperatureGraphs.battery_surface_area_average_c[last].toFixed(4)} °C`,"내부 평균":`${temperatureGraphs.battery_internal_volume_average_c[last].toFixed(4)} °C`});
}else{temperatureChartOpen.disabled=true;temperatureGraphSummary.innerHTML="<dt>상태</dt><dd>실제 데이터 없음</dd>";}
function paintConvergenceChart(context,width,height){
  context.clearRect(0,0,width,height);context.fillStyle="#ffffff";context.fillRect(0,0,width,height);
  context.fillStyle="#17232d";context.font="700 18px system-ui";context.textAlign="left";
  const displayLabel=convergence.display_label||meshRunText(activeMeshRun(),"label");
  context.fillText(`${tr(displayLabel)} · ${tr(convergence.label||"")} · ${tr("수렴 이력")}`,24,28);
  const series=convergence.series||[],allValues=series.flatMap(line=>line.points.map(point=>point[1])).filter(value=>Number.isFinite(value)&&value>0);
  if(!allValues.length){context.fillStyle="#c62828";context.font="700 14px system-ui";context.fillText(tr("저장된 residual 데이터가 없습니다."),24,62);return;}
  const configuredGates=series.map(line=>Number(line.configured_gate)).filter(value=>Number.isFinite(value)&&value>0);
  const residualGate=Number.isFinite(Number(convergence.residual_gate))?Number(convergence.residual_gate):(configuredGates.length?Math.min(...configuredGates):null);
  const scaledValues=residualGate?[...allValues,residualGate]:allValues;
  const logHigh=Math.ceil(Math.log10(Math.max(...scaledValues))),logLow=Math.floor(Math.log10(Math.min(...scaledValues)));
  const first=convergence.first_iteration,last=convergence.last_iteration,span=Math.max(1,last-first);
  const left=82,right=28,columns=width>900?3:2,legendRows=Math.ceil(series.length/columns),top=50+legendRows*18,bottom=58;
  const plotWidth=Math.max(100,width-left-right),plotHeight=Math.max(100,height-top-bottom),logSpan=Math.max(1,logHigh-logLow);
  context.font="700 10px ui-monospace,monospace";
  series.forEach((line,index)=>{
    const column=index%columns,row=Math.floor(index/columns),x=left+column*plotWidth/columns,y=47+row*18;
    context.strokeStyle=line.colour;context.lineWidth=2.4;
    context.beginPath();context.moveTo(x,y-4);context.lineTo(x+24,y-4);context.stroke();
    context.fillStyle="#243946";context.fillText(`${tr(line.label)} · ${line.final.toExponential(2)}`,x+30,y);
  });
  context.strokeStyle="#8495a0";context.lineWidth=1;context.strokeRect(left,top,plotWidth,plotHeight);
  context.font="600 11px ui-monospace,monospace";
  for(let tick=0;tick<=6;tick++){
    const fraction=tick/6,x=left+fraction*plotWidth,iteration=Math.round(first+fraction*span);
    context.strokeStyle="#e4ebef";context.beginPath();context.moveTo(x,top);context.lineTo(x,top+plotHeight);context.stroke();
    context.fillStyle="#526774";context.textAlign=tick===0?"left":tick===6?"right":"center";context.fillText(String(iteration),x,top+plotHeight+21);
  }
  for(let exponent=logLow;exponent<=logHigh;exponent++){
    const y=top+(logHigh-exponent)/logSpan*plotHeight;
    context.strokeStyle="#e4ebef";context.beginPath();context.moveTo(left,y);context.lineTo(left+plotWidth,y);context.stroke();
    context.fillStyle="#526774";context.textAlign="right";context.fillText(`1e${exponent}`,left-10,y+4);
  }
  if(residualGate){
    const gateY=top+(logHigh-Math.log10(residualGate))/logSpan*plotHeight;
    context.save();context.setLineDash([7,5]);context.strokeStyle="#b42318";context.lineWidth=1.6;
    context.beginPath();context.moveTo(left,gateY);context.lineTo(left+plotWidth,gateY);context.stroke();context.restore();
    context.fillStyle="#b42318";context.font="700 10px ui-monospace,monospace";context.textAlign="right";
    context.fillText(`${tr("합격 기준")} ${residualGate.toExponential(1)}`,left+plotWidth-4,gateY-6);
  }
  for(const line of series){
    context.strokeStyle=line.colour;context.lineWidth=2;context.beginPath();
    line.points.forEach((point,index)=>{
      const x=left+(point[0]-first)/span*plotWidth,y=top+(logHigh-Math.log10(Math.max(point[1],Number.MIN_VALUE)))/logSpan*plotHeight;
      if(index===0)context.moveTo(x,y);else context.lineTo(x,y);
    });
    context.stroke();
  }
  const pimpleAxis=convergence.axis_mode==="cumulative_pimple_outer_iteration",timeStepAxis=convergence.axis_mode==="transient_time_step_iteration";
  const axisLabel=pimpleAxis?"누적 PIMPLE 전역 반복 횟수":timeStepAxis?"1초 과도 에너지 반복 횟수":"OpenFOAM 외부 반복 횟수";
  context.fillStyle="#243946";context.font="700 12px system-ui";context.textAlign="center";context.fillText(tr(axisLabel),left+plotWidth/2,height-16);
  context.save();context.translate(18,top+plotHeight/2);context.rotate(-Math.PI/2);context.fillText(tr("Initial residual / |global continuity|"),0,0);context.restore();
}
function drawConvergenceChart(){
  if(convergenceGraphPanel.hidden)return;
  const rect=convergenceChartCanvas.getBoundingClientRect();if(rect.width<1||rect.height<1)return;
  const ratio=Math.min(window.devicePixelRatio||1,2);
  convergenceChartCanvas.width=Math.round(rect.width*ratio);convergenceChartCanvas.height=Math.round(rect.height*ratio);
  const context=convergenceChartCanvas.getContext("2d");context.setTransform(ratio,0,0,ratio,0,0);
  paintConvergenceChart(context,rect.width,rect.height);
  const displayLabel=convergence.display_label||meshRunText(activeMeshRun(),"label");
  const pimpleAxis=convergence.axis_mode==="cumulative_pimple_outer_iteration",timeStepAxis=convergence.axis_mode==="transient_time_step_iteration",transientAxis=pimpleAxis||timeStepAxis;
  if(window.CFDTwinI18n?.getLanguage()==="en"){
    const iterationLabel=pimpleAxis?"Cumulative PIMPLE Global Iterations":timeStepAxis?"Transient Energy Iterations per Second":"Steady-state Outer Iterations";
    const linearTotal=convergence.linear_iteration_total?` · total inner linear iterations ${convergence.linear_iteration_total.toLocaleString()}`:"";
    const timeRange=transientAxis&&Number.isFinite(convergence.last_time_s)?` · physical time ${Number(convergence.first_time_s||0).toLocaleString()}–${Number(convergence.last_time_s).toLocaleString()} s`:"";
    const comparison=pimpleAxis?" · fully coupled transient iterations · original OpenFOAM residuals":timeStepAxis?" · velocity, pressure, and turbulence converged earlier and then held fixed; this stage solves energy only":" · actual U/p/k/ω/h log; numerical definition differs from Fluent scaled residuals";
    const failure=convergence.diagnostic_status==="failed"?` · FAILED: ${convergence.failure_reason_en||convergence.failure_reason||"acceptance gate not met"}`:"";
    convergenceChartStatus.textContent=`${displayLabel} · ${iterationLabel} ${convergence.first_iteration.toLocaleString()}–${convergence.last_iteration.toLocaleString()} iterations${timeRange}${linearTotal}${comparison}${failure}`;
    return;
  }
  const iterationLabel=pimpleAxis?"누적 PIMPLE 전역 반복":timeStepAxis?"1초 과도 에너지 반복":"정상상태 외부 반복";
  const linearTotal=convergence.linear_iteration_total?` · 방정식 내부 선형 반복 합계 ${convergence.linear_iteration_total.toLocaleString()}회`:"";
  const timeRange=transientAxis&&Number.isFinite(convergence.last_time_s)?` · 물리시간 ${Number(convergence.first_time_s||0).toLocaleString()}–${Number(convergence.last_time_s).toLocaleString()} s`:"";
  const comparison=pimpleAxis?" · 완전 연성 과도 반복 구조 · OpenFOAM 원본 잔차":timeStepAxis?" · 속도·압력·난류는 선행 단계에서 수렴 후 고정 · 이 단계는 energy만 계산":" · U/p/k/ω/h 실제 로그 · Fluent scaled residual과 수치 정의는 다름";
  const failure=convergence.diagnostic_status==="failed"?` · 실패: ${convergence.failure_reason||"합격 기준 미달"}`:"";
  convergenceChartStatus.textContent=`${displayLabel} · ${iterationLabel} ${convergence.first_iteration.toLocaleString()}–${convergence.last_iteration.toLocaleString()}회${timeRange}${linearTotal}${comparison}${failure}`;
}
for(const history of convergenceHistories){
  if(!history?.available)continue;
  const option=document.createElement("option");option.value=history.id||"";
  const iterationText=Number.isFinite(history.last_iteration)?` · 누적 ${Number(history.last_iteration).toLocaleString()}회`:"";
  const timeText=Number.isFinite(history.last_time_s)?` · ${Number(history.first_time_s||0).toLocaleString()}–${Number(history.last_time_s).toLocaleString()} s`:"";
  const diagnosticPrefix=history.diagnostic_status==="failed"?"[실패 진단] ":"";
  option.textContent=`${diagnosticPrefix}${history.label||history.scope||history.id}${iterationText}${timeText}`;
  convergenceHistorySelect.appendChild(option);
}
const requestedConvergenceHistory=initialParams.get("history");
if(requestedConvergenceHistory){convergence=convergenceHistories.find(history=>history.id===requestedConvergenceHistory)||convergence;}
convergenceHistorySelect.value=convergence.id||"";
convergenceHistorySelect.hidden=convergenceHistorySelect.options.length<=1;
convergenceHistorySelect.addEventListener("change",()=>{
  convergence=convergenceHistories.find(history=>history.id===convergenceHistorySelect.value)||convergence;
  renderConvergenceSummary();drawConvergenceChart();
});
function showConvergenceChart(){
  if(!convergence.available)return;
  flowPlaying=false;flowPlay.textContent="▶ 재생";temperatureGraphPanel.hidden=true;meshFigurePanel.hidden=true;figurePreview.hidden=true;
  viewerControls.hidden=true;flowControls.hidden=true;canvasNote.hidden=true;convergenceGraphPanel.hidden=false;setDataLoadStatus("");
  requestAnimationFrame(drawConvergenceChart);
}
convergenceChartOpen.addEventListener("click",showConvergenceChart);
failedConvergenceChartOpen.addEventListener("click",()=>{
  if(!failedConvergenceHistories.length)return;
  convergence=failedConvergenceHistories[0];convergenceHistorySelect.value=convergence.id||"";
  renderConvergenceSummary();showConvergenceChart();
});
convergenceChartClose.addEventListener("click",()=>{
  convergenceGraphPanel.hidden=true;viewerControls.hidden=false;flowControls.hidden=!flow.enabled;canvasNote.hidden=false;
  modeFieldset.hidden=!flow.enabled;displayOptions.hidden=!flow.enabled;horizontalSections.hidden=!flow.enabled||!visualMode.startsWith("xy-");verticalSections.hidden=!flow.enabled||!visualMode.startsWith("xz-");resize();
});
convergenceChartPng.addEventListener("click",()=>{
  const exportCanvas=document.createElement("canvas");exportCanvas.width=1600;exportCanvas.height=900;
  paintConvergenceChart(exportCanvas.getContext("2d"),1600,900);exportCanvas.toBlob(blob=>{if(blob)downloadBlob(blob,`cfd-residual-${convergence.id||"convergence"}.png`);},"image/png");
});
convergenceChartCsv.addEventListener("click",()=>{
  const series=convergence.series||[],iterations=[...new Set(series.flatMap(line=>line.points.map(point=>point[0])))].sort((a,b)=>a-b);
  const axisHeader=convergence.axis_mode==="cumulative_pimple_outer_iteration"?"cumulative_pimple_global_iteration":convergence.axis_mode==="transient_time_step_iteration"?"transient_energy_iteration":"openfoam_outer_iteration";
  const maps=series.map(line=>new Map(line.points)),lines=[[axisHeader,...series.map(line=>line.key)].map(csvValue).join(",")];
  for(const iteration of iterations)lines.push([iteration,...maps.map(values=>values.get(iteration)??"")].map(csvValue).join(","));
  downloadBlob(new Blob(["\ufeff",lines.join("\n")],{type:"text/csv;charset=utf-8"}),`cfd-residual-${convergence.id||"convergence"}.csv`);
});
function renderConvergenceSummary(){
  if(convergence.available){
    const pimpleAxis=convergence.axis_mode==="cumulative_pimple_outer_iteration",timeStepAxis=convergence.axis_mode==="transient_time_step_iteration";
    const diagnosticFailed=convergence.diagnostic_status==="failed";
    const validationPassed=!diagnosticFailed&&(String(convergence.validation_status||"").endsWith("_PASSED")||convergence.configured_residual_checks_passed);
    const summaryRows={"상태":diagnosticFailed?"검증 실패":validationPassed?"복합 검증 통과":"원본 로그 저장","계산 단계":convergence.label||convergence.scope};
    summaryRows[pimpleAxis?"누적 PIMPLE 반복":timeStepAxis?"1초 에너지 반복":"정상 외부 반복"]=`${convergence.first_iteration.toLocaleString()}–${convergence.last_iteration.toLocaleString()}회`;
    if(convergence.linear_iteration_total)summaryRows["내부 반복 합계"]=`${convergence.linear_iteration_total.toLocaleString()}회`;
    rows(convergenceSummary,summaryRows);convergenceChartOpen.disabled=false;
  }else{convergenceChartOpen.disabled=true;convergenceSummary.innerHTML="<dt>상태</dt><dd>로그 없음</dd>";}
}
renderConvergenceSummary();
if(failedConvergenceHistories.length){
  const failed=failedConvergenceHistories[0],observed=Number(failed.observed_residual),gate=Number(failed.residual_gate);
  failedConvergenceCard.hidden=false;
  rows(failedConvergenceSummary,{
    "상태":"실패 · 합격 결과와 분리",
    "계산":"Ø34 mm · 유동 초기화",
    "반복":`${Number(failed.first_iteration).toLocaleString()}–${Number(failed.last_iteration).toLocaleString()}회`,
    "압력 잔차":Number.isFinite(observed)&&Number.isFinite(gate)?`${observed.toExponential(3)} > ${gate.toExponential(1)}`:"기준 미달",
    "메시":Number.isFinite(Number(failed.total_cells))?`${Number(failed.total_cells).toLocaleString()} cells`:"—",
    "y⁺<1 면적":Number.isFinite(Number(failed.yplus_area_fraction))?`${(100*Number(failed.yplus_area_fraction)).toFixed(3)}% · 통과`:"—"
  });
}else{failedConvergenceCard.hidden=true;failedConvergenceChartOpen.disabled=true;}
if(initialParams.get("panel")==="convergence")showConvergenceChart();
let videoMode="none",videoPlaying=false,videoSimulationTime=0,videoFileTime=0,videoRenderAccumulator=0;
function formatSimulationTime(value){
  const seconds=Math.max(0,Math.round(value));
  const minutes=Math.floor(seconds/60),remainder=seconds%60;
  return `${minutes}:${String(remainder).padStart(2,"0")} (${seconds} s)`;
}
function videoFraction(){
  if(videoMode==="thermal")return clamp(videoSimulationTime/1800);
  return Number.isFinite(figureVideo.duration)&&figureVideo.duration>0?clamp(videoFileTime/figureVideo.duration):0;
}
function updateVideoTime(){
  const fraction=videoFraction(),rate=Number(playbackRate.value);
  videoSeek.value=String(Math.round(1000*fraction));
  if(videoMode==="thermal"){
    videoTime.textContent=`시뮬레이션 t = ${formatSimulationTime(videoSimulationTime)} / 30:00`;
    videoDuration.textContent=`${rate}× 완주 ${formatWallDuration(1800/rate)}`;
  }else if(videoMode==="fixed-flow"){
    videoTime.textContent=`시각화 위상 ${(100*fraction).toFixed(0)}%`;
    videoDuration.textContent=`${rate}× 시각화`;
  }else{videoTime.textContent="대기 중";videoDuration.textContent="";}
}
function syncVideoFrame(){
  if(!Number.isFinite(figureVideo.duration)||figureVideo.duration<=0)return;
  figureVideo.currentTime=figureVideo.duration*videoFraction();updateVideoTime();
}
function advanceVideoPlayback(elapsed){
  if(!videoPlaying||figurePreview.hidden||figureVideo.hidden||videoMode==="none")return;
  const rate=Number(playbackRate.value);
  if(videoMode==="thermal")videoSimulationTime=(videoSimulationTime+elapsed*rate)%1800;
  else if(Number.isFinite(figureVideo.duration)&&figureVideo.duration>0)videoFileTime=(videoFileTime+elapsed*rate)%figureVideo.duration;
  videoRenderAccumulator+=elapsed;
  if(videoRenderAccumulator>=1/12){videoRenderAccumulator=0;syncVideoFrame();}
}
playbackRate.addEventListener("change",updateVideoTime);
videoPlay.addEventListener("click",()=>{videoPlaying=!videoPlaying;videoPlay.textContent=videoPlaying?"❚❚ 정지":"▶ 재생";});
videoSeek.addEventListener("input",()=>{
  const fraction=Number(videoSeek.value)/1000;
  if(videoMode==="thermal")videoSimulationTime=1800*fraction;
  else if(Number.isFinite(figureVideo.duration))videoFileTime=figureVideo.duration*fraction;
  syncVideoFrame();
});
function stepVideo(direction){
  videoPlaying=false;videoPlay.textContent="▶ 재생";
  if(videoMode==="thermal")videoSimulationTime=clamp(videoSimulationTime+direction*15,0,1800);
  else{const fps=videoMode==="fixed-flow"?20:25;videoFileTime=clamp(videoFileTime+direction/fps,0,figureVideo.duration||0);}
  syncVideoFrame();
}
videoPrev.addEventListener("click",()=>stepVideo(-1));
videoNext.addEventListener("click",()=>stepVideo(1));
figureVideo.addEventListener("loadedmetadata",()=>{figureVideo.pause();syncVideoFrame();});
let meshFigureMode=2;
const meshFigureColours=["#137da7","#e1842e"];
function meshFlowFor(run){return (model.interactive_cfd_by_mesh||{})[run.id]||null;}
function lastFinite(values){
  if(!Array.isArray(values))return null;
  for(let index=values.length-1;index>=0;index--)if(Number.isFinite(values[index]))return Number(values[index]);
  return null;
}
function meshFinalMetrics(run){
  const selectedFlow=meshFlowFor(run),graphs=selectedFlow?.temperature_graphs||{},inletC=activeInputs.inlet_temperature_K-273.15;
  const rise=values=>{const value=lastFinite(values);return value===null?null:value-inletC;};
  return {
    batteryMaximum:rise(graphs.battery_mesh_maximum_c),
    batteryInternalAverage:rise(graphs.battery_internal_volume_average_c),
    batterySurfaceAverage:rise(graphs.battery_surface_area_average_c),
    outlet:rise(graphs.outlet_mass_weighted_average_c),
    pressureDrop:Number.isFinite(selectedFlow?.port_flow?.pressure_drop_pa)?Number(selectedFlow.port_flow.pressure_drop_pa):null,
    velocityMaximum:Number.isFinite(selectedFlow?.velocity_max_m_s)?Number(selectedFlow.velocity_max_m_s):null
  };
}
function canvasLabel(context,text,x,y,{colour="#243946",font="600 13px system-ui",align="left"}={}){
  context.fillStyle=colour;context.font=font;context.textAlign=align;context.fillText(tr(text),x,y);
}
function paintMeshStructureFigure(context,width,height){
  context.clearRect(0,0,width,height);context.fillStyle="#f7fafc";context.fillRect(0,0,width,height);
  canvasLabel(context,"Figure 2 · 현재 두 계산 메시의 실제 구성",width/2,34,{font:"750 22px system-ui",align:"center"});
  canvasLabel(context,"production mesh 검증 보고서의 실제 셀 수와 요소 구성을 비교합니다.",width/2,58,{colour:"#607684",font:"500 12px system-ui",align:"center"});
  const runs=meshRuns.slice(0,2),gap=Math.max(14,width*.025),outer=Math.max(18,width*.035),cardWidth=(width-2*outer-gap)/Math.max(1,runs.length),top=82,bottom=22,cardHeight=height-top-bottom;
  const maxCells=Math.max(1,...runs.map(run=>Number(run.counts?.total_cells)||0));
  runs.forEach((run,index)=>{
    const counts=run.counts||{},x=outer+index*(cardWidth+gap),active=run.id===activeMeshRunId,colour=meshFigureColours[index%meshFigureColours.length];
    context.fillStyle="#fff";context.fillRect(x,top,cardWidth,cardHeight);context.strokeStyle=active?colour:"#cbd8df";context.lineWidth=active?3:1.2;context.strokeRect(x,top,cardWidth,cardHeight);
    canvasLabel(context,meshRunText(run,"label"),x+18,top+32,{colour,font:"750 17px system-ui"});
    canvasLabel(context,active?"현재 선택 결과":"비교 결과",x+cardWidth-18,top+31,{colour:active?colour:"#758b97",font:"700 11px system-ui",align:"right"});
    const count=value=>Number.isFinite(Number(value))?Number(value).toLocaleString():"—";
    const rows=[
      ["전체 셀",count(counts.total_cells)],
      ["유체 셀",count(counts.fluid_cells)],
      ["고체 셀",count(counts.solid_cells)],
      ["노드",count(counts.points)],
      ["육면체",count(counts.hexahedra??counts.cell_types?.hexahedra)],
      ["육면체 비율",Number.isFinite(counts.hexahedron_fraction)?`${(100*counts.hexahedron_fraction).toFixed(3)} %`:"—"]
    ];
    let y=top+68;
    for(const [label,value] of rows){
      canvasLabel(context,label,x+18,y,{colour:"#647985",font:"600 12px system-ui"});
      canvasLabel(context,value,x+cardWidth-18,y,{colour:"#203440",font:"750 13px ui-monospace,monospace",align:"right"});y+=28;
    }
    const barX=x+18,barWidth=cardWidth-36,barY=Math.min(top+cardHeight-126,y+9),barHeight=17,total=Math.max(1,Number(counts.total_cells)||1),fluid=Math.max(0,Number(counts.fluid_cells)||0),solid=Math.max(0,Number(counts.solid_cells)||0);
    canvasLabel(context,"유체 / 고체 셀 구성",barX,barY-9,{colour:"#647985",font:"650 11px system-ui"});
    context.fillStyle="#e7eef2";context.fillRect(barX,barY,barWidth,barHeight);context.fillStyle="#35a9d1";context.fillRect(barX,barY,barWidth*fluid/total,barHeight);context.fillStyle="#ef9b46";context.fillRect(barX+barWidth*fluid/total,barY,barWidth*solid/total,barHeight);
    canvasLabel(context,`유체 ${(100*fluid/total).toFixed(1)}% · 고체 ${(100*solid/total).toFixed(1)}%`,barX,barY+37,{colour:"#526874",font:"600 11px system-ui"});
    const densityY=barY+66;canvasLabel(context,"정밀 메시 대비 셀 수",barX,densityY-8,{colour:"#647985",font:"650 11px system-ui"});
    context.fillStyle="#e7eef2";context.fillRect(barX,densityY,barWidth,12);context.fillStyle=colour;context.fillRect(barX,densityY,barWidth*total/maxCells,12);
    canvasLabel(context,`${(100*total/maxCells).toFixed(1)} %`,x+cardWidth-18,densityY+31,{colour,font:"750 11px ui-monospace,monospace",align:"right"});
    canvasLabel(context,run.element_family||"요소 정보 없음",barX,top+cardHeight-18,{colour:"#718691",font:"500 10px system-ui"});
  });
}
function paintTwoResultFigure(context,width,height){
  context.clearRect(0,0,width,height);context.fillStyle="#f7fafc";context.fillRect(0,0,width,height);
  canvasLabel(context,"Figure 3 · 정밀/표준 계산 결과 비교",width/2,32,{font:"750 22px system-ui",align:"center"});
  canvasLabel(context,"t = 1800 s 직접 저장값 · 시간보간 및 무효점 패딩 없음",width/2,55,{colour:"#607684",font:"500 12px system-ui",align:"center"});
  const runs=meshRuns.slice(0,2),metricsByRun=runs.map(meshFinalMetrics),inletC=activeInputs.inlet_temperature_K-273.15;
  runs.forEach((run,index)=>{
    const x=width/2+(index-.5)*Math.min(310,width*.32),colour=meshFigureColours[index%meshFigureColours.length];
    context.fillStyle=colour;context.fillRect(x-88,70,14,4);canvasLabel(context,meshRunText(run,"label"),x-66,76,{colour,font:"700 11px system-ui"});
    if(run.id===activeMeshRunId){context.strokeStyle=colour;context.lineWidth=1.5;context.strokeRect(x-94,62,Math.min(280,width*.29),23);}
  });
  const metricDefinitions=[
    ["배터리 최고 온도 상승", "batteryMaximum", "K", 6],
    ["배터리 내부 체적평균 상승", "batteryInternalAverage", "K", 6],
    ["배터리 표면 면적평균 상승", "batterySurfaceAverage", "K", 6],
    ["출구 질량가중 평균 온도 상승", "outlet", "K", 6],
    ["입구–출구 압력강하", "pressureDrop", "Pa", 3],
    ["유체 최대 속도", "velocityMaximum", "m/s", 6]
  ];
  const left=Math.max(170,width*.23),right=Math.max(185,width*.22),barWidth=Math.max(120,width-left-right),startY=105,rowHeight=Math.max(70,(height-startY-25)/metricDefinitions.length);
  metricDefinitions.forEach(([label,key,unit,digits],metricIndex)=>{
    const values=metricsByRun.map(metrics=>metrics[key]),valid=values.filter(Number.isFinite),maximum=Math.max(1e-12,...valid),y=startY+metricIndex*rowHeight;
    canvasLabel(context,label,left-14,y+18,{colour:"#2e4654",font:"700 12px system-ui",align:"right"});
    values.forEach((value,index)=>{
      const barY=y+index*22,colour=meshFigureColours[index%meshFigureColours.length],fraction=Number.isFinite(value)?Math.max(.005,value/maximum):0;
      context.fillStyle="#e4ebef";context.fillRect(left,barY,barWidth,13);context.fillStyle=colour;context.fillRect(left,barY,barWidth*fraction,13);
      canvasLabel(context,Number.isFinite(value)?`${value.toFixed(digits)} ${unit}`:"결과 없음",left+barWidth+12,barY+11,{colour:Number.isFinite(value)?colour:"#8a9aa3",font:"750 11px ui-monospace,monospace"});
    });
    if(values.length>=2&&values.every(Number.isFinite)){
      const difference=values[1]-values[0],sign=difference>=0?"+":"";
      canvasLabel(context,`표준 − 정밀 = ${sign}${difference.toFixed(digits)} ${unit}`,left,y+49,{colour:"#6b7e89",font:"600 10px ui-monospace,monospace"});
    }
  });
  canvasLabel(context,`기준 입구 온도 ${inletC.toFixed(3)} °C · 온도 항목은 T − Tin`,width-20,height-12,{colour:"#718691",font:"500 10px system-ui",align:"right"});
}
function drawMeshResultFigure(){
  if(meshFigurePanel.hidden)return;
  const rect=meshFigureCanvas.getBoundingClientRect();if(rect.width<1||rect.height<1)return;
  const ratio=Math.min(window.devicePixelRatio||1,2);meshFigureCanvas.width=Math.round(rect.width*ratio);meshFigureCanvas.height=Math.round(rect.height*ratio);
  const context=meshFigureCanvas.getContext("2d");context.setTransform(ratio,0,0,ratio,0,0);
  if(meshFigureMode===2)paintMeshStructureFigure(context,rect.width,rect.height);else paintTwoResultFigure(context,rect.width,rect.height);
}
function showMeshResultFigure(figureId,artifacts=[]){
  meshFigureMode=figureId;videoPlaying=false;figureVideo.pause();figurePreview.hidden=true;temperatureGraphPanel.hidden=true;convergenceGraphPanel.hidden=true;
  viewerControls.hidden=true;flowControls.hidden=true;canvasNote.hidden=true;modeFieldset.hidden=true;displayOptions.hidden=true;horizontalSections.hidden=true;verticalSections.hidden=true;meshFigurePanel.hidden=false;
  meshFigureTitle.textContent=figureId===2?"Figure 2 · 두 계산 메시 구성":"Figure 3 · 두 계산 결과 비교";
  const imageMode=figureId===2&&artifacts.length>=2;
  meshFigureImageGrid.hidden=!imageMode;meshFigureCanvasWrap.hidden=imageMode;meshFigurePng.hidden=imageMode;
  if(imageMode){
    const labels=["정밀 메시 · 실제 polyMesh","표준 메시 · 실제 polyMesh"];
    meshFigureImageGrid.innerHTML=artifacts.slice(0,2).map((path,index)=>`<figure><figcaption><span>${labels[index]}</span><a href="${path}" download>PNG 저장</a></figcaption><img src="${path}" alt="${labels[index]} 중앙 XY 절단면"></figure>`).join("");
  }else{meshFigureImageGrid.innerHTML="";}
  const precision=meshRuns[0],standard=meshRuns[1];
  meshFigureStatus.textContent=figureId===2
    ?`${meshRunText(precision,"label")||"정밀 결과"} / ${meshRunText(standard,"label")||"표준 결과"} · 각 계산에 사용된 실제 polyMesh · 중앙 XY, Z=41 mm`
    :`${meshRunText(precision,"label")||"정밀 결과"} / ${meshRunText(standard,"label")||"표준 결과"} · 1800 s 실제 CFD 값 직접 비교`;
  if(!imageMode)requestAnimationFrame(drawMeshResultFigure);
}
meshFigureClose.addEventListener("click",()=>{
  meshFigurePanel.hidden=true;viewerControls.hidden=false;flowControls.hidden=!flow.enabled;canvasNote.hidden=false;modeFieldset.hidden=!flow.enabled;displayOptions.hidden=!flow.enabled;horizontalSections.hidden=!flow.enabled||!visualMode.startsWith("xy-");verticalSections.hidden=!flow.enabled||!visualMode.startsWith("xz-");resize();
});
meshFigurePng.addEventListener("click",()=>{
  const exportCanvas=document.createElement("canvas");exportCanvas.width=1600;exportCanvas.height=900;const context=exportCanvas.getContext("2d");
  if(meshFigureMode===2)paintMeshStructureFigure(context,1600,900);else paintTwoResultFigure(context,1600,900);
  exportCanvas.toBlob(blob=>{if(blob)downloadBlob(blob,meshFigureMode===2?"figure-02-two-mesh-structure.png":"figure-03-two-result-comparison.png");},"image/png");
});
const figureUi={
  1:["3D 종합 화면","셀 온도와 유체 흐름을 시간에 따라 확인"],
  2:["두 계산 메시","정밀·표준 메시의 실제 셀 구성과 품질 비교"],
  3:["두 계산 결과","정밀·표준 CFD 결과를 1800초 값으로 비교"],
  4:["배터리 온도 변화","시간에 따른 배터리 온도 확인"],
  6:["가로 단면 온도","선택 높이의 온도 분포 확인"],
  7:["셀 표면 온도","배터리 표면 온도 분포 확인"],
  8:["온도·유동 종합","셀 온도와 냉각수 흐름 동시 확인"],
  9:["배터리 온도와 유선","배터리별 온도와 유선 확인"],
  10:["유체 유선","냉각수 흐름 경로와 유속 확인"],
  11:["가로 단면 유속","선택 높이의 유속 분포 확인"],
  12:["세로 단면 유속","선택 위치의 세로 유속 분포 확인"],
  13:["세로 단면 온도","선택 위치의 세로 온도 분포 확인"],
  14:["가로 단면 온도","가로 온도 분포 확인"]
};
function figureSummary(item){const ui=figureUi[item.id]||["결과 화면","계산 결과 확인"];return `Fig. ${item.id} · ${ui[0]} — ${ui[1]}`;}
// 기존 Figure 2·3 정적 비교는 UI에서 제외한다. 실제 메시 자체는 CAD 위의
// `실제 메시` 레이어로 회전·확대하며 확인한다.
const visibleFigures=model.figures.filter(item=>![2,3].includes(Number(item.id)));
figureGrid.innerHTML=visibleFigures.map(item=>`<button data-figure="${item.id}" class="${item.status.endsWith("_READY")?"ready":""}" title="${(figureUi[item.id]||["결과 화면"])[0]}">Fig ${item.id}</button>`).join("");
const initialFigure=model.figures.find(item=>item.id===1);
if(initialFigure)figureDetail.textContent=figureSummary(initialFigure);
figureGrid.querySelectorAll("[data-figure]").forEach(button=>button.addEventListener("click",()=>{
  temperatureGraphPanel.hidden=true;
  convergenceGraphPanel.hidden=true;
  meshFigurePanel.hidden=true;
  const item=model.figures.find(candidate=>String(candidate.id)===button.dataset.figure);
  figureDetail.textContent=figureSummary(item);
  const artifacts=item.viewer_artifacts||[];
  const interactiveFigureModes={4:"cell-temperature",6:"xy-temperature",7:"cell-temperature",8:"combined",9:"combined",10:"velocity-streamlines",11:"xy-velocity",12:"xz-velocity",13:"xz-temperature",14:"xy-temperature"};
  if(actualOneSecond&&interactiveFigureModes[item.id]){
    const mode=interactiveFigureModes[item.id],input=modeInputs.find(candidate=>candidate.value===mode);
    if(input)input.checked=true;
    videoPlaying=false;figureVideo.pause();figurePreview.hidden=true;viewerControls.hidden=false;canvasNote.hidden=false;flowControls.hidden=false;modeFieldset.hidden=false;displayOptions.hidden=false;
    selectVisualMode(mode);return;
  }
  if(item.id===1||artifacts.length===0){videoPlaying=false;figureVideo.pause();figurePreview.hidden=true;viewerControls.hidden=false;canvasNote.hidden=false;flowControls.hidden=!flow.enabled;modeFieldset.hidden=!flow.enabled;displayOptions.hidden=!flow.enabled;horizontalSections.hidden=!flow.enabled||!visualMode.startsWith("xy-");verticalSections.hidden=!flow.enabled||!visualMode.startsWith("xz-");draw();return;}
  const show=index=>{
    const path=artifacts[index],video=/\.(mp4|webm)(?:\?.*)?$/i.test(path);
    figureImage.hidden=video;figureVideo.hidden=!video;videoControls.hidden=!video;
    if(video){
      videoMode=/actual_thermal_xy/i.test(path)?"thermal":/frozen_flow_particles_fixed_U/i.test(path)?"fixed-flow":"generic";
      videoSemantics.textContent=videoMode==="thermal"?"Fig 6 실제 냉각수 T(x,t) + 고정 U(x) · 15 s 간격 · 1×=실제 1800 s":videoMode==="fixed-flow"?"고정 U(x)의 긴 유선을 따라 이동 헤드가 방향 표시 · 과도속도 아님":"동영상 결과";
      videoSimulationTime=0;videoFileTime=0;videoRenderAccumulator=0;videoPlaying=true;videoPlay.textContent="❚❚ 정지";
      figureVideo.pause();figureVideo.src=path;figureVideo.load();updateVideoTime();
    }else{
      videoMode="none";videoPlaying=false;figureVideo.pause();figureImage.src=path;
    }
    artifactTabs.querySelectorAll("button").forEach((tab,i)=>tab.classList.toggle("active",i===index));
  };
  artifactTabs.innerHTML=artifacts.map((path,index)=>`<button data-artifact="${index}">${/\.(mp4|webm)(?:\?.*)?$/i.test(path)?"애니메이션":"정적 결과"}</button>`).join("");
  artifactTabs.querySelectorAll("[data-artifact]").forEach(tab=>tab.addEventListener("click",()=>show(Number(tab.dataset.artifact))));
  figurePreview.hidden=false;viewerControls.hidden=true;canvasNote.hidden=true;flowControls.hidden=true;modeFieldset.hidden=true;displayOptions.hidden=true;horizontalSections.hidden=true;verticalSections.hidden=true;show(0);
}));
const cellGrid=document.getElementById("cells");
cellGrid.innerHTML=sceneCells.map(cell=>`<button type="button" data-cell-id="${cell.id}" aria-label="${cell.id} 셀 강조">${cell.id}</button>`).join("");
function showCellNotification(cellId){
  highlightedCellId=cellId;clearTimeout(cellHighlightTimer);
  for(const button of cellGrid.querySelectorAll("[data-cell-id]"))button.classList.toggle("active",button.dataset.cellId===cellId);
  cellToast.textContent=`${cellId} 선택 · 3D 모델에서 잠시 강조됩니다`;cellToast.hidden=false;draw();
  cellHighlightTimer=setTimeout(()=>{
    highlightedCellId=null;cellToast.hidden=true;
    for(const button of cellGrid.querySelectorAll("[data-cell-id]"))button.classList.remove("active");draw();
  },1800);
}
cellGrid.querySelectorAll("[data-cell-id]").forEach(button=>button.addEventListener("click",()=>showCellNotification(button.dataset.cellId)));
document.getElementById("status").textContent=model.quality_warning?"계산 완료 · 에너지 수지 경고 · 1초 단위":(actualOneSecond?"계산 완료 · 정밀 결과 · 1초 단위":(model.review_only?"계산 완료 · 단일 메시 검토용 · 1초 단위":model.status_label));
document.getElementById("result-badge").textContent=model.quality_warning?"계산 완료 · 경고":(model.review_only?"계산 완료 · 검토용":(model.actual_solver_data?"계산 완료":"준비 중"));
document.getElementById("calculation-summary").innerHTML=`<strong>${meshRunText(activeMeshRun(),"label")}</strong> · 0–1800 s`;
document.getElementById("calculation-note").textContent=activeCalculationNote;
document.getElementById("calculation-card").classList.toggle("validated",model.actual_solver_data&&!model.quality_warning);
document.getElementById("calculation-card").classList.toggle("quality-warning",Boolean(model.quality_warning));
document.getElementById("provenance").textContent=model.quality_warning?"실제 CFD 결과 · 품질 경고":(model.review_only?"실제 CFD 결과 · 단일 메시 검토용":(model.actual_solver_data?"검증된 CFD 결과 뷰어":"형상 미리보기"));
renderActiveMeshRun();
let stageResizeFrame=0;
if(typeof ResizeObserver==="function"){
  const stageResizeObserver=new ResizeObserver(()=>{
    cancelAnimationFrame(stageResizeFrame);stageResizeFrame=requestAnimationFrame(resize);
  });
  stageResizeObserver.observe(sceneStage);
}
window.addEventListener("resize",()=>{resize();drawTemperatureChart();drawConvergenceChart();});resize();requestAnimationFrame(animate);
window.addEventListener("cfdtwin:languagechange",()=>{
  for(const option of meshRunSelect.options){
    const run=meshRuns.find(item=>item.id===option.value);
    if(run)option.textContent=`${meshRunText(run,"label")} · ${meshRunText(run,"state_label")}`;
  }
  renderActiveMeshRun();
  drawTemperatureChart();drawConvergenceChart();drawMeshResultFigure();
});
'''


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _portable_dataset_javascript(name: str, raw: bytes) -> str:
    """Wrap one binary field in an on-demand file:// compatible script."""

    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    payload = base64.b64encode(compressed).decode("ascii")
    packed = json.dumps(
        {
            "encoding": "gzip-base64",
            "byte_length": len(raw),
            "payload": payload,
        },
        separators=(",", ":"),
    )
    return (
        "window.CFD_TWIN_PACKED_DATA = window.CFD_TWIN_PACKED_DATA || {};\n"
        f"window.CFD_TWIN_PACKED_DATA[{json.dumps(name)}] = "
        f"{packed};\n"
    )


def refresh_viewer_ui_assets(output_dir: str | Path) -> None:
    """Update an existing portable viewer shell without re-reading CFD fields."""

    output = Path(output_dir).resolve()
    results_html = output / "results.html"
    if not results_html.is_file() or not (output / "model.js").is_file():
        raise FileNotFoundError("An existing viewer with results.html and model.js is required")
    previous = results_html.read_text(encoding="utf-8")
    model_match = re.search(r'<script src="(model\.[0-9a-f]+\.js)"', previous)
    model_asset = model_match.group(1) if model_match else "model.js"
    style_asset = f"style.{hashlib.sha256(CSS.encode('utf-8')).hexdigest()[:12]}.css"
    app_asset = f"app.{hashlib.sha256(JAVASCRIPT.encode('utf-8')).hexdigest()[:12]}.js"
    cad_assets = Path(__file__).with_name("assets") / "cad_reference"
    include_cad = (output / "cad").is_dir() or (output.parent / "distribution_identity.json").is_file()
    if include_cad:
        shutil.copytree(cad_assets, output / "cad", dirs_exist_ok=True)
        _write_english_cad_drawings(output / "cad", cad_assets)
    html = (HTML.replace("__CAD_LINK_HIDDEN__", "" if include_cad else "hidden")
            .replace("__STYLE_ASSET__", style_asset)
            .replace("__APP_ASSET__", app_asset)
            .replace("__MODEL_ASSET__", model_asset))
    mesh_html = MESH_HTML
    case_manifest = output / "four_case_manifest.json"
    if case_manifest.is_file():
        manifest = json.loads(case_manifest.read_text(encoding="utf-8"))
        ready_runs = [run for run in manifest.get("runs", []) if run.get("data_ready")]
        if ready_runs:
            for run in ready_runs:
                run_id = str(run.get("id", ""))
                asset_id = str(run.get("asset_source_id") or run_id)
                for stem in ("mesh-data", "mesh-volume-data"):
                    asset = output / f"{stem}-{asset_id}.js"
                    if not asset.is_file() or asset.stat().st_size == 0:
                        raise FileNotFoundError(asset)
            if not (output / "four_case_data.js").is_file():
                raise FileNotFoundError(output / "four_case_data.js")
            mesh_html = enable_multi_case_mesh_selection(mesh_html)
    assets = Path(__file__).with_name("workflow_assets")
    for name, content in {
        "index.html": HOME_HTML,
        "home.html": HOME_HTML,
        "results.html": html,
        "portal.css": (assets / "style.css").read_text(encoding="utf-8"),
        "i18n.js": (assets / "i18n.js").read_text(encoding="utf-8"),
        "offline-app.js": OFFLINE_JAVASCRIPT,
        "style.css": CSS,
        "app.js": JAVASCRIPT,
        style_asset: CSS,
        app_asset: JAVASCRIPT,
        "mesh.html": mesh_html,
        "mesh.css": MESH_CSS,
        "mesh.js": MESH_JAVASCRIPT,
    }.items():
        _atomic_write(output / name, content)
    shutil.copy2(assets / "cheoma-logo.png", output / "cheoma-logo.png")
    if (output.parent / "distribution_identity.json").is_file():
        _atomic_write(output.parent / "index.html", HOME_HTML.replace("<head>", '<head>\n  <base href="viewer/">', 1))


def _write_english_cad_drawings(output_cad: Path, source_cad: Path) -> None:
    replacements = {
        "dimensioned_top.svg": {
            "상면도 · X–Y 정투영": "Top View · X–Y Orthographic",
            "파랑: 입수구 · 빨강: 출수구 · 셀 직경 Ø19 mm":
                "Blue: Inlet · Red: Outlet · Cell Diameter Ø19 mm",
        },
        "dimensioned_side.svg": {
            "측면도 · X–Z 정투영": "Side View · X–Z Orthographic",
            "동일 X열의 4/5개 셀은 측면 정투영에서 겹쳐 표시됨":
                "4/5 cells in each X column overlap in side projection",
        },
    }
    for filename, labels in replacements.items():
        drawing = (source_cad / filename).read_text(encoding="utf-8")
        for original, translated in labels.items():
            if original not in drawing:
                raise ValueError(f"Missing CAD drawing label: {original}")
            drawing = drawing.replace(original, translated)
        _atomic_write(output_cad / filename.replace(".svg", ".en.svg"), drawing)


def write_viewer_bundle(
    config: ResolvedConfig,
    output_dir: str | Path,
    *,
    case_dir: str | Path | None = None,
    include_reference_cad: bool = True,
    precision_steady_run_dir: str | Path | None = None,
    precision_transient_run_dir: str | Path | None = None,
) -> dict[str, object]:
    """검증 보고서와 CFD 필드를 읽어 첫 화면, 결과 및 데이터 파일을 만든다.

    결과가 없거나 gate가 실패하면 완료 상태로 표시하지 않는다. 준비된 경우에만
    셀 온도, 단면, 유선, 압력강하, 1초 그래프와 수렴 이력을 UI에 연결한다.
    """

    output = Path(output_dir).resolve()
    report = geometry_report(config)
    placements = cell_placements(config)
    column_indexes = sorted({placement.column_index for placement in placements})
    columns = {
        index: sorted(
            (placement for placement in placements if placement.column_index == index),
            key=lambda placement: placement.center_y_m,
        )
        for index in column_indexes
    }
    left = columns[column_indexes[0]]
    right = columns[column_indexes[-1]]
    middle = columns[column_indexes[len(column_indexes) // 2]]
    featured_temperature_cells = [
        {"id": left[0].cell_id, "position": "좌하단"},
        {"id": left[-1].cell_id, "position": "좌상단"},
        {"id": right[0].cell_id, "position": "우하단"},
        {"id": right[-1].cell_id, "position": "우상단"},
        {"id": middle[len(middle) // 2].cell_id, "position": "중앙"},
    ]
    heat_source = source_from_config(config)
    heat_source_manifest = heat_source.manifest()
    heat_source_end_time_s = 1800.0
    qvol_start_w_m3 = heat_source.qvol(0.0)
    qvol_end_w_m3 = heat_source.qvol(heat_source_end_time_s)
    cell_heat_start_w = qvol_start_w_m3 * config.derived.cell_volume_m3
    cell_heat_end_w = qvol_end_w_m3 * config.derived.cell_volume_m3
    heat_source_labels = {
        "constant_qvol": "상수 체적발열",
        "table_qvol": "시간표 체적발열",
        "electrical_joule": "Joule 발열",
        "peneklioglu_expanded_first_order": "Peneklioglu 1차 상관식",
        "peneklioglu_bernardi_2c_293p15": "PPT Bernardi · 2C · 293.15 K",
    }
    figures = figure_catalog()
    result_manifest: dict[str, object] | None = None
    mesh_independence: dict[str, object] = {}
    case: Path | None = None
    if case_dir is not None:
        case = Path(case_dir).resolve()
        manifest_path = case / "results" / "result_manifest.json"
        if manifest_path.is_file():
            result_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        mesh_path = case / "results" / "mesh_independence.json"
        if mesh_path.is_file():
            mesh_independence = json.loads(mesh_path.read_text(encoding="utf-8"))
        artifact_output = output / "artifacts"
        for figure in figures:
            contract_path = case / "results" / "figures" / f'figure_{int(figure["id"]):02d}.json'
            if not contract_path.is_file():
                continue
            persisted = json.loads(contract_path.read_text(encoding="utf-8"))
            figure.update(persisted)
            viewer_artifacts: list[str] = []
            for artifact in persisted.get("artifacts", []):
                source = case / artifact
                if not source.is_file() or source.suffix.lower() not in {".png", ".mp4", ".webm", ".gif"}:
                    continue
                destination = artifact_output / f'fig{int(figure["id"]):02d}_{source.name}'
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
                viewer_artifacts.append(
                    f"{destination.relative_to(output).as_posix()}?v={digest}"
                )
            figure["viewer_artifacts"] = viewer_artifacts
    actual_solver_data = bool(
        result_manifest
        and result_manifest.get("data_status")
        in {"L0_CFD_RESULTS_READY", "CFD_PILOT_RESULTS_READY"}
        and result_manifest.get("actual_solver_data") is True
    )
    review_only = bool(
        result_manifest
        and result_manifest.get("data_status") == "CFD_PILOT_RESULTS_READY"
        and result_manifest.get("publication_authorized") is False
    )
    quality_status = str(
        result_manifest.get("quality_status", "") if result_manifest else ""
    )
    validation_status = str(
        result_manifest.get("validation_status", "") if result_manifest else ""
    )
    validation_warnings = list(
        result_manifest.get("validation_warnings", []) if result_manifest else []
    )
    quality_warning = bool(
        actual_solver_data
        and ("WARNING" in quality_status or validation_status.endswith("_FAILED"))
    )
    if actual_solver_data:
        summary = result_manifest.get("final_summary", {})
        final_tmax_k = float(summary.get("battery_tmax_k", float("nan")))
        status = "PRECISION_RESULT_READY"
        mesh_passed = mesh_independence.get("status") == "MESH_INDEPENDENCE_PASSED"
        status_label = (
            "정밀 결과 · 역사적 격자 독립성 PASS"
            if mesh_passed
            else "검증 CFD 결과 · 논문 최종 아님"
        )
        calculation_summary = (
            "<strong>실제 CFD 검증 완료</strong> — 1800 s 열 과도와 "
            + ("역사적 격자 독립성" if mesh_passed else "solver gate")
            + "를 통과했습니다."
        )
        if mesh_passed:
            comparison = mesh_independence["comparisons"]["L2_to_L3"]
            calculation_note = (
                f"1800 s 체적 Tmax {final_tmax_k:.6f} K · "
                f"정밀도 비교 ΔTmax {float(comparison['battery_tmax_difference_k']):.6f} K · "
                "Figure 1의 이동 헤드는 검증된 고정 U(x)를 따르며 "
                "과도속도장이 아닙니다."
            )
        else:
            calculation_note = (
                f"최종 체적 Tmax {final_tmax_k:.6f} K · Figure 3의 "
                "격자 독립성 완료 전에는 논문 최종값으로 사용하지 않습니다."
            )
        if review_only:
            status_label = "실제 CFD 결과 · 단일 메시 검토용"
            calculation_summary = (
                "<strong>CFD 계산·검증 완료</strong> — 이 결과는 단일 메시의 "
                "내부 검토용이며 발표·논문 최종 승인 결과가 아닙니다."
            )
        if quality_warning:
            balance_fraction = float(
                summary.get("complete_energy_balance_mean_fraction", float("nan"))
            )
            balance_label = (
                f"{100.0 * balance_fraction:.4f}%"
                if math.isfinite(balance_fraction)
                else "기준 초과"
            )
            status = "RESULT_READY_WITH_QUALITY_WARNING"
            status_label = "CFD 결과 · 에너지 수지 경고"
            calculation_summary = (
                "<strong>계산 완료 · 품질 경고</strong> — 마지막 60초 "
                f"에너지 수지 불균형 평균 {balance_label} (목표 2% 이하)."
            )
            calculation_note += (
                " 결과값은 보존하되 최종 검증 PASS로 분류하지 않습니다."
            )
    else:
        status = "GEOMETRY_PREVIEW_CFD_NOT_RUN"
        status_label = "GEOMETRY PREVIEW · CFD 미실행"
        calculation_summary = (
            "<strong>CFD 미실행</strong> — PPT 외곽/노치는 반영됐고 포트 y 좌표와 "
            "입구 열 배정은 기준 형상 가정입니다."
        )
        calculation_note = "이 화면의 색은 셀 구분용이며 온도장이 아닙니다."
    payload_case = case if case is not None else Path(".")
    precision_steady_run = (
        Path(precision_steady_run_dir).resolve()
        if precision_steady_run_dir is not None
        else None
    )
    precision_transient_run = (
        Path(precision_transient_run_dir).resolve()
        if precision_transient_run_dir is not None
        else None
    )
    precision_cfd = _interactive_actual_cfd_payload(
        config,
        payload_case,
        enabled=actual_solver_data and case is not None,
        mesh_id="precision",
        steady_run_dir=precision_steady_run,
        transient_run_dir=precision_transient_run,
    )
    if precision_cfd is None:
        precision_cfd = _interactive_legacy_cfd_payload(
            config,
            payload_case,
            enabled=actual_solver_data and case is not None,
        )
    standard_cfd = _interactive_actual_cfd_payload(
        config,
        payload_case,
        enabled=case is not None,
        mesh_id="single",
    )
    interactive_cfd_by_mesh = {
        mesh_id: payload
        for mesh_id, payload in (
            ("precision", precision_cfd),
            ("single", standard_cfd),
        )
        if payload is not None and payload.get("enabled")
    }
    interactive_cfd = interactive_cfd_by_mesh.get(
        "precision", interactive_cfd_by_mesh.get("single", {"enabled": False})
    )
    actual_one_second = None
    if interactive_cfd.get("enabled"):
        actual_one_second = interactive_cfd.get("actual_one_second")
        if actual_one_second:
            status = "PRECISION_ONE_SECOND_RESULTS_READY"
            status_label = (
                "CFD 결과 · 1 s 실제값 · 단일 메시 검토용"
                if review_only
                else "CFD 결과 · 1 s 실제값 · 영역별 초기온도 · 보간/패딩 없음"
            )
            calculation_summary = (
                "<strong>CFD 계산 완료</strong> — 선택된 80만급 메시 결과를 "
                "t=0–1800 s를 1 s 간격으로 직접 저장했습니다."
            )
            if review_only:
                calculation_summary = (
                    "<strong>단일 메시 검토용 CFD 결과</strong> — 0–1800 s의 "
                    "원본값을 1 s 간격으로 저장·검증했습니다. 격자 독립성·"
                    "교차검증 전에는 논문 최종값으로 사용하지 않습니다."
                )
            quantization_error = float(
                actual_one_second["temperature_encoding"][
                    "maximum_quantization_error_k"
                ]
            )
            calculation_note = (
                "Figure 1은 셀 293.15 K와 냉각수 293 K 초기장을 구분하고, "
                "각 원본 K 값에서 273.15만 뺍니다. 시간/공간 보간과 "
                "무효점 패딩을 하지 않습니다. "
                f"브라우저 저장 양자화 최대오차 {quantization_error:.3e} K."
            )
            if quality_warning:
                final_summary = result_manifest.get("final_summary", {})
                balance_fraction = float(
                    final_summary.get(
                        "complete_energy_balance_mean_fraction", float("nan")
                    )
                )
                balance_label = (
                    f"{100.0 * balance_fraction:.4f}%"
                    if math.isfinite(balance_fraction)
                    else "기준 초과"
                )
                status = "ONE_SECOND_RESULTS_READY_WITH_QUALITY_WARNING"
                status_label = "CFD 결과 · 에너지 수지 경고 · 1 s 실제값"
                calculation_summary = (
                    "<strong>계산 완료 · 품질 경고</strong> — 849,284셀, "
                    f"0–1800 s. 마지막 60초 에너지 수지 불균형 평균 {balance_label} "
                    "(목표 2% 이하)."
                )
                calculation_note += (
                    " 결과값은 보존하되 최종 검증 PASS로 분류하지 않습니다."
                )
        else:
            status_label += " · 화면온도 +0.150 K"
            calculation_note += (
                " · Figure 1 온도는 원본장을 보존한 채 표시값에 +0.150 K를 "
                "적용했습니다."
            )
        figure1 = next(figure for figure in figures if int(figure["id"]) == 1)
        figure1.update(
            {
                "title": "3D 종합 결과",
                "status": (
                    "PRECISION_ONE_SECOND_INTERACTIVE_READY"
                    if actual_one_second
                    else "PRECISION_INTERACTIVE_READY"
                ),
                "time_mode": "transient_temperature_fixed_velocity",
            }
        )
    if case is not None:
        for mesh_id, payload in interactive_cfd_by_mesh.items():
            packaged = payload.get("actual_one_second")
            if not packaged:
                continue
            runs_root = (
                case / "runs"
                if mesh_id == "precision" and (case / "runs/frozen_flow_actual_1s").is_dir()
                else case / (
                    "production/L3/runs"
                    if mesh_id == "precision"
                    else "production/single/runs"
                )
            )
            upgraded_source = (
                runs_root / "frozen_flow_actual_1s_xy5/reports/viewer"
            )
            source_dir = (
                precision_transient_run / "reports/viewer"
                if mesh_id == "precision" and precision_transient_run is not None
                else
                upgraded_source
                if mesh_id == "precision"
                and (upgraded_source / "actual_1s_manifest.json").is_file()
                else runs_root / "frozen_flow_actual_1s/reports/viewer"
            )
            data_dir = output / "data" / mesh_id
            data_dir.mkdir(parents=True, exist_ok=True)
            for name, specification in packaged["datasets"].items():
                source = source_dir / Path(specification["file"]).name
                destination = data_dir / source.name
                shutil.copy2(source, destination)
                raw = source.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                relative = destination.relative_to(output).as_posix()
                specification["file"] = f"{relative}?v={digest[:12]}"
                specification["portable_file"] = f"{relative}.js"
                specification["sha256"] = digest
                specification["byte_length"] = len(raw)
                _atomic_write(
                    data_dir / f"{source.name}.js",
                    _portable_dataset_javascript(name, raw),
                )
            shutil.copy2(
                source_dir / "actual_1s_manifest.json",
                data_dir / "actual_1s_manifest.json",
            )
            shutil.copy2(
                source_dir / "actual_1s_validation.json",
                data_dir / "actual_1s_validation.json",
            )
    mesh_runs = _mesh_runs_payload(
        case if case is not None else output / "__no_case__",
        precision_data_ready="precision" in interactive_cfd_by_mesh,
        standard_data_ready="single" in interactive_cfd_by_mesh,
    )
    convergence_by_mesh: dict[str, dict[str, object]] = {}
    energy_balance_by_mesh: dict[str, dict[str, object]] = {}
    if case is not None:
        case_by_mesh = {
            "precision": case,
            "single": case / "production/single",
        }
        for mesh_id, mesh_case in case_by_mesh.items():
            if mesh_id not in interactive_cfd_by_mesh:
                continue
            convergence_by_mesh[mesh_id] = _convergence_payload(mesh_case)
            energy_balance_by_mesh[mesh_id] = _energy_balance_payload(
                mesh_case,
                transient_run_dir=(
                    precision_transient_run if mesh_id == "precision" else None
                ),
            )
    convergence = convergence_by_mesh.get(
        "precision",
        convergence_by_mesh.get("single", {"available": False}),
    )
    energy_balance = energy_balance_by_mesh.get(
        "precision",
        energy_balance_by_mesh.get("single", {"available": False}),
    )
    mesh_wireframes = mesh_wireframes_for_viewer(case) if case is not None else {}
    mesh_only_summary = (
        write_mesh_only_viewer(
            case,
            output,
            steady_run_dir=precision_steady_run,
        )
        if case is not None
        and actual_solver_data
        and (case / "constant/polyMesh/points").is_file()
        and (case / "reports/production_mesh/mesh_target_composition.json").is_file()
        else None
    )
    model = {
        "schema_version": 1,
        "status": status,
        "status_label": status_label,
        "actual_solver_data": actual_solver_data,
        "quality_status": quality_status,
        "validation_status": validation_status,
        "quality_warning": quality_warning,
        "review_only": review_only,
        "validation_warnings": validation_warnings,
        "execution_variant": (
            result_manifest.get("execution_variant") if result_manifest else None
        ),
        "energy_coupling_cap_failures": (
            int(result_manifest.get("energy_coupling_cap_failures", 0))
            if result_manifest else 0
        ),
        "interactive_cfd": interactive_cfd,
        "interactive_cfd_by_mesh": interactive_cfd_by_mesh,
        "mesh_runs": mesh_runs,
        "mesh_wireframes": mesh_wireframes,
        "mesh_only_ready": mesh_only_summary is not None,
        "mesh_only_summary": mesh_only_summary,
        "convergence": convergence,
        "convergence_by_mesh": convergence_by_mesh,
        "energy_balance": energy_balance,
        "energy_balance_by_mesh": energy_balance_by_mesh,
        "calculation_summary": calculation_summary,
        "calculation_note": calculation_note,
        "production_cad_ready": report["production_cad_ready"],
        "blocking_assumptions": report["blocking_assumptions"],
        "nonblocking_assumptions": report["nonblocking_assumptions"],
        "enclosure_mm": {
            "x": config.enclosure.width_x_m * 1000,
            "y": config.enclosure.length_y_m * 1000,
            "z": config.enclosure.height_z_m * 1000,
            "notch_depth": config.enclosure.notch.depth_m * 1000,
        },
        "cell_diameter_mm": config.cells.diameter_m * 1000,
        "cell_length_mm": config.cells.length_m * 1000,
        "port_diameter_mm": config.ports.inlet_diameter_m * 1000,
        "topology": config.cells.topology,
        "outline_mm": [
            [x_m * 1000, y_m * 1000]
            for x_m, y_m in config.enclosure.outline_polygon_m
        ],
        "pitch_mm": {
            "x": config.cells.column_pitch_x_m * 1000,
            "y": config.cells.row_pitch_y_m * 1000,
            "offset": config.cells.stagger_offset_y_m * 1000,
        },
        "cells": [
            {
                "id": placement.cell_id,
                "x": placement.center_x_m * 1000,
                "y": placement.center_y_m * 1000,
                "z": placement.center_z_m * 1000,
                "radius": config.cells.diameter_m * 500,
                "length": config.cells.length_m * 1000,
            }
            for placement in placements
        ],
        "featured_temperature_cells": featured_temperature_cells,
        "ports": [
            {
                "id": "IN",
                "kind": "port",
                "x": config.ports.inlet_center_xy_m[0] * 1000,
                "y": config.ports.inlet_center_xy_m[1] * 1000,
                "z": config.enclosure.height_z_m * 1000
                + config.ports.external_length_m * 500,
                "radius": config.ports.inlet_diameter_m * 500,
                "length": config.ports.external_length_m * 1000,
            },
            {
                "id": "OUT",
                "kind": "port",
                "x": config.ports.outlet_center_xy_m[0] * 1000,
                "y": config.ports.outlet_center_xy_m[1] * 1000,
                "z": -config.ports.external_length_m * 500,
                "radius": config.ports.outlet_diameter_m * 500,
                "length": config.ports.external_length_m * 1000,
            },
        ],
        "derived": {
            "reynolds": config.derived.reynolds_table4,
            "paper_reynolds": config.derived.paper_reynolds,
            "cell_heat_w": cell_heat_start_w,
            "total_heat_w": cell_heat_start_w * config.cells.count,
            "qvol_w_m3": qvol_start_w_m3,
            "heat_source_mode": heat_source.mode,
        },
        "heat_source": {
            "mode": heat_source.mode,
            "label": heat_source_labels.get(heat_source.mode, heat_source.mode),
            "time_start_s": 0,
            "time_end_s": heat_source_end_time_s,
            "qvol_start_w_m3": qvol_start_w_m3,
            "qvol_end_w_m3": qvol_end_w_m3,
            "cell_heat_start_w": cell_heat_start_w,
            "cell_heat_end_w": cell_heat_end_w,
            "total_heat_start_w": cell_heat_start_w * config.cells.count,
            "total_heat_end_w": cell_heat_end_w * config.cells.count,
            "time_varying": not math.isclose(
                qvol_start_w_m3, qvol_end_w_m3, rel_tol=1e-12, abs_tol=1e-12
            ),
            "manifest": heat_source_manifest,
        },
        "surface_heat_transfer_coefficient": {
            "status": (
                "DIRECT_INTERFACE_DATA_AVAILABLE"
                if interactive_cfd.get("cell_surface_heat_transfer")
                else "NOT_SAMPLED_FOR_THIS_ARCHIVED_RUN"
            ),
            "definition": "h=abs(wallHeatFlux)/abs(Twall-Tnear-fluid)",
            "wall_heat_flux": "OpenFOAM coolant-side wallHeatFlux patch field",
            "fluid_reference_temperature": (
                "containing coolant cell 0.35 mm outward from the interface"
            ),
            "scope": "coolant-facing surfaces of batteries C01-C23 only",
            "units": "W/(m^2*K)",
            "invalid_policy": (
                "unavailable where abs(Twall-Tnear-fluid)<=1e-6 K; "
                "no temporal or spatial fill"
            ),
        },
        "inputs": {
            "inlet_velocity_m_s": config.boundary.inlet_velocity_m_s,
            "inlet_temperature_K": config.boundary.inlet_temperature_k,
            "initial_fluid_temperature_K": (
                config.boundary.initial_fluid_temperature_k
            ),
            "initial_solid_temperature_K": (
                config.boundary.initial_solid_temperature_k
            ),
            "fluid_density_kg_m3": config.fluid.density_kg_m3,
            "dynamic_viscosity_Pa_s": config.fluid.dynamic_viscosity_pa_s,
            "fluid_cp_J_kgK": config.fluid.specific_heat_j_kgk,
            "fluid_k_W_mK": config.fluid.thermal_conductivity_w_mk,
            "battery_density_kg_m3": config.battery.density_kg_m3,
            "battery_cp_J_kgK": config.battery.specific_heat_j_kgk,
        },
        "figures": figures,
        "resolved_input_sha256": config.resolved_input_sha256,
        "geometry_sha256": config.geometry_sha256,
    }
    model_javascript = (
        "window.CFD_TWIN_MODEL = "
        + json.dumps(model, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + ";\n"
    )
    style_asset = f"style.{hashlib.sha256(CSS.encode('utf-8')).hexdigest()[:12]}.css"
    app_asset = f"app.{hashlib.sha256(JAVASCRIPT.encode('utf-8')).hexdigest()[:12]}.js"
    model_asset = (
        f"model.{hashlib.sha256(model_javascript.encode('utf-8')).hexdigest()[:12]}.js"
    )
    rendered_html = HTML.replace("__CAD_LINK_HIDDEN__", "" if include_reference_cad else "hidden").replace("__STYLE_ASSET__", style_asset).replace(
        "__APP_ASSET__", app_asset
    ).replace("__MODEL_ASSET__", model_asset)
    precision = model.get("interactive_cfd_by_mesh", {}).get("precision", {})
    graphs = precision.get("temperature_graphs", {})
    def last_temperature(key: str) -> float | None:
        series = graphs.get(key, [])
        return float(series[-1]) if isinstance(series, list) and series else None

    offline_summary = {
        "actual_solver_data": actual_solver_data,
        "label": "적응형 84.9만셀 결과" if model.get("execution_variant") == "adaptive_energy_coupling_trial" else "현재 결과",
        "mesh_cells": next((run.get("counts", {}).get("total_cells") for run in mesh_runs if run.get("data_ready")), None),
        "reference_cad_ready": include_reference_cad,
        "tank_mm": [model["enclosure_mm"][axis] for axis in ("x", "y", "z")],
        "cell_count": len(placements),
        "port_diameter_mm": model["port_diameter_mm"],
        "tmax_c": last_temperature("battery_mesh_maximum_c"),
        "outlet_c": last_temperature("outlet_mass_weighted_average_c"),
    }
    for obsolete in (
        *output.glob("style.*.css"),
        *output.glob("app.*.js"),
        *output.glob("model.*.js"),
    ):
        if obsolete.name not in {style_asset, app_asset, model_asset}:
            obsolete.unlink()
    _atomic_write(output / "index.html", HOME_HTML)
    _atomic_write(output / "home.html", HOME_HTML)
    _atomic_write(output / "results.html", rendered_html)
    if include_reference_cad:
        reference_assets = Path(__file__).with_name("assets") / "cad_reference"
        shutil.copytree(reference_assets, output / "cad", dirs_exist_ok=True)
        _write_english_cad_drawings(output / "cad", reference_assets)
    _atomic_write(output / "offline-data.js", "window.CFD_OFFLINE_SUMMARY=" + json.dumps(offline_summary, ensure_ascii=False) + ";\n")
    _atomic_write(output / "offline-app.js", OFFLINE_JAVASCRIPT)
    _atomic_write(output / "i18n.js", (Path(__file__).with_name("workflow_assets") / "i18n.js").read_text(encoding="utf-8"))
    _atomic_write(output / "portal.css", (Path(__file__).with_name("workflow_assets") / "style.css").read_text(encoding="utf-8"))
    shutil.copy2(
        Path(__file__).with_name("workflow_assets") / "cheoma-logo.png",
        output / "cheoma-logo.png",
    )
    _atomic_write(output / "style.css", CSS)
    _atomic_write(output / "app.js", JAVASCRIPT)
    _atomic_write(output / style_asset, CSS)
    _atomic_write(output / app_asset, JAVASCRIPT)
    _atomic_write(output / "model.js", model_javascript)
    _atomic_write(output / model_asset, model_javascript)
    _atomic_write(
        output / "viewer_manifest.json",
        json.dumps(model, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(
        output / "README_VIEWER.txt",
        "CFD 결과 뷰어\n\n"
        "이 폴더 전체를 같은 구조로 유지한 뒤 index.html을 더블클릭하십시오.\n"
        "첫 화면의 결과 보기에서 results.html을 열고, 상단 메시 보기에서 mesh.html을 엽니다.\n"
        "메시 전용 화면에는 실제 polyMesh의 경계·단면 및 3D 전체 체적 격자가 있습니다.\n"
        "전체 격자를 보려면 mesh-volume-data.js가 반드시 mesh.html과 같은 폴더에 있어야 합니다.\n"
        "ZIP 내부에서 직접 실행하지 말고 폴더 전체를 압축 해제해 보관하십시오.\n"
        "가로와 세로 단면 위치는 계산에 사용한 15%, 32.5%, 50%, 67.5%, 85%만 선택합니다.\n"
        "세로 단면은 YZ(X 고정, 5/4 셀 열)와 XZ(Y 고정) 두 방향을 지원합니다.\n"
        "1,800초에는 보존된 냉각수와 batterySolid 공간장을 직접 표시합니다.\n"
        "중간 시각의 YZ 냉각수 공간장은 저장되지 않았으므로 표시하지 않고,\n"
        "해당 위치에서 잘린 셀에 저장된 셀 온도만 표시합니다.\n"
        + (
            "주의: 계산은 1,800초까지 완료됐지만 최종 60초 완전 에너지 수지 "
            f"평균이 {balance_label}로 2% 목표를 넘어서 품질 경고 상태입니다.\n"
            if quality_warning
            else ""
        )
        +
        "로컬 file:// 재생에는 data 폴더와 해시가 붙은 JS/CSS/model 파일이 필요하므로\n"
        "index.html만 따로 복사하지 마십시오. 최신 Chrome, Edge 또는 Safari를 권장합니다.\n",
    )
    return model
