from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np

from cfd_twin.multi_case_viewer import read_viewer_model, write_four_case_model
from cfd_twin.viewer import CSS, HTML, JAVASCRIPT, _convergence_payload


ROOT = Path(__file__).resolve().parents[1]


def attach_robust_surface_htc_ranges(viewer: Path, model: dict) -> None:
    """Record full-data percentiles without altering any direct HTC sample."""

    for payload in model.get("interactive_cfd_by_mesh", {}).values():
        surface = payload.get("cell_surface_heat_transfer")
        if not isinstance(surface, dict) or not surface.get("actual_dataset"):
            continue
        name = str(surface["actual_dataset"])
        datasets = payload.get("actual_one_second", {}).get("datasets", {})
        specification = datasets.get(name)
        if not isinstance(specification, dict):
            raise ValueError(f"surface HTC dataset metadata is missing: {name}")
        relative = urlsplit(str(specification["file"])).path
        path = (viewer / relative).resolve()
        if not path.is_relative_to(viewer) or not path.is_file():
            raise FileNotFoundError(path)
        values = np.memmap(path, dtype="<f4", mode="r")
        finite = np.asarray(values[np.isfinite(values)], dtype=float)
        percentiles = {
            str(percentile): float(np.percentile(finite, percentile))
            for percentile in (1, 5, 25, 50, 75, 90, 95, 99, 99.5, 99.9)
        }
        upper = max(1000.0, 1000.0 * np.ceil(percentiles["99.9"] / 1000.0))
        specification["distribution_percentiles_w_m2k"] = percentiles
        specification["recommended_fixed_display_upper_w_m2k"] = float(upper)
        specification["display_range_policy"] = (
            "shared fixed 0..global max of per-run 99.9th-percentile rounded up "
            "to 1000 W/(m^2*K); higher direct values remain valid and saturate"
        )
        surface["distribution_percentiles_w_m2k"] = percentiles.copy()
        surface["recommended_fixed_display_upper_w_m2k"] = float(upper)


def attach_failed_convergence(model: dict, diagnostic: Path) -> None:
    diagnostic = diagnostic.resolve()
    summary = json.loads((diagnostic / "failure_summary.json").read_text(encoding="utf-8"))
    convergence = _convergence_payload(diagnostic / "case")
    if not convergence.get("available"):
        raise ValueError(f"No preserved OpenFOAM convergence history in {diagnostic}")
    history = dict(convergence)
    history.pop("histories", None)
    pressure = summary["pressure_residual"]
    history.update(
        {
            "id": f"phi{int(summary['diameter_mm'])}_failed_cold_flow",
            "display_label": f"Ø{int(summary['diameter_mm'])} mm 실패 계산",
            "diagnostic_status": "failed",
            "failure_reason": "압력 p_rgh 잔차가 9500회 상한에서도 합격 기준보다 큼",
            "failure_reason_en": "pressure p_rgh residual remained above the acceptance gate at the 9,500-iteration cap",
            "residual_gate": float(pressure["maximum_allowed"]),
            "observed_residual": float(pressure["last_200_window_maximum"]),
            "total_cells": int(summary["mesh"]["total_cells"]),
            "hexahedron_fraction": float(summary["mesh"]["hexahedron_fraction"]),
            "yplus_area_fraction": float(summary["yplus"]["battery_area_fraction_below_1"]),
            "result_separation": "diagnostic only; not a selectable accepted CFD result",
        }
    )
    model["failed_convergence_histories"] = [history]


def upgrade(viewer: Path, *, failed_diagnostic: Path | None = None) -> None:
    viewer = viewer.resolve()
    model, _ = read_viewer_model(viewer)
    model.pop("effective_heat_transfer_coefficient", None)
    model["surface_heat_transfer_coefficient"] = {
        "status": "DIRECT_INTERFACE_DATA_WHEN_PRESENT",
        "definition": "h=abs(wallHeatFlux)/abs(Twall-Tnear-fluid)",
        "wall_heat_flux": "OpenFOAM coolant-side wallHeatFlux patch field",
        "fluid_reference_temperature": "containing coolant cell 0.35 mm outward from the interface",
        "scope": "coolant-facing surfaces of batteries C01-C23 only",
        "units": "W/(m^2*K)",
        "invalid_policy": "archived runs without direct interface probes stay unavailable; no surrogate or fill",
    }
    attach_robust_surface_htc_ranges(viewer, model)
    if failed_diagnostic is not None:
        attach_failed_convergence(model, failed_diagnostic)

    style_asset = f"style.{hashlib.sha256(CSS.encode()).hexdigest()[:12]}.css"
    app_asset = f"app.four-case-{hashlib.sha256(JAVASCRIPT.encode()).hexdigest()[:12]}.js"
    rendered = (
        HTML.replace(
            "__CAD_LINK_HIDDEN__", "" if (viewer / "cad/index.html").is_file() else "hidden"
        )
        .replace("__STYLE_ASSET__", style_asset)
        .replace("__MODEL_ASSET__", "model.js")
        .replace("__APP_ASSET__", app_asset)
    )
    (viewer / "results.html").write_text(rendered, encoding="utf-8")
    (viewer / "style.css").write_text(CSS, encoding="utf-8")
    (viewer / style_asset).write_text(CSS, encoding="utf-8")
    (viewer / "app.js").write_text(JAVASCRIPT, encoding="utf-8")
    (viewer / app_asset).write_text(JAVASCRIPT, encoding="utf-8")
    shutil.copy2(ROOT / "src/cfd_twin/workflow_assets/i18n.js", viewer / "i18n.js")
    write_four_case_model(viewer, model)

    readme = viewer / "README_VIEWER.txt"
    note = (
        "\n열전달계수는 냉각수와 맞닿은 C01-C23 배터리 표면에서만 표시합니다.\n"
        "h=abs(wallHeatFlux)/abs(Twall-Tnear-fluid)이며 OpenFOAM 냉각수측 "
        "벽면 열유속·벽면 온도·벽면 바깥 0.35 mm 냉각수 온도를 직접 사용합니다.\n"
        "가로·세로 열전달계수 단면은 제공하지 않으며, 모든 직접 표본 결과는 "
        "시간과 지름에 공통인 고정 범례를 사용합니다. 직접 표본이 없는 기존 결과는 미표시됩니다.\n"
        "현재 결과 카드의 입구·출구 질량유량은 OpenFOAM phi 면적적분의 실제값입니다.\n"
    )
    if failed_diagnostic is not None:
        note += (
            "Ø34 실패 수렴 기록은 합격 결과 선택 목록과 분리된 진단 카드에서 열 수 있으며, "
            "보존된 OpenFOAM 1–9500회 원본 로그를 사용합니다.\n"
        )
    current = readme.read_text(encoding="utf-8") if readme.is_file() else "CFD 결과 뷰어\n"
    if "h=abs(wallHeatFlux)" not in current:
        readme.write_text(current.rstrip() + "\n" + note, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("viewer", type=Path)
    parser.add_argument(
        "--failed-diagnostic",
        type=Path,
        help="Preserved failed-attempt folder containing failure_summary.json and case/runs solver logs",
    )
    args = parser.parse_args()
    upgrade(args.viewer, failed_diagnostic=args.failed_diagnostic)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
