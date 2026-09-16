"""Publish a completed baseline CFD run to the local review viewer.

Only solver output and independent validation reports are inputs.  This step
does not rerun CFD, infer missing frames, or label a single-mesh result as a
publication-approved mesh-independence study.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def publish_completed_run(
    case_dir: Path, viewer_dir: Path, *, config=None
) -> dict[str, object]:
    """Gate, pack, verify, and display one completed single-mesh run."""

    from .actual_one_second import (
        build_actual_one_second_bundle,
        validate_actual_one_second_bundle,
    )
    from .config import load_config
    from .viewer import write_viewer_bundle

    case = case_dir.resolve()
    viewer = viewer_dir.resolve()
    run = case / "runs/frozen_flow_actual_1s"
    report_dir = run / "reports/solver"
    state_path = case / "reports/workflow_postprocess.json"

    def update(stage: str, message: str) -> None:
        _write_json(state_path, {"stage": stage, "message": message})

    gates = {
        "mesh": (
            case / "reports/production_mesh/production_mesh_gate.json",
            "PRODUCTION_MESH_GATE_PASSED",
        ),
        "cold_flow": (
            case / "runs/steady_cold_flow/reports/solver/cold_flow_validation.json",
            "COLD_FLOW_VALIDATION_PASSED",
        ),
        "steady_cht": (
            case / "runs/steady_cht/reports/solver/steady_cht_validation.json",
            "STEADY_CHT_VALIDATION_PASSED",
        ),
        "transient": (
            report_dir / "frozen_flow_validation.json",
            "FROZEN_FLOW_VALIDATION_PASSED",
        ),
        "energy_balance": (
            report_dir / "energy_balance.json",
            "TRANSIENT_ENERGY_BALANCE_PASSED",
        ),
    }
    reports = {name: _read_json(path) for name, (path, _) in gates.items()}
    for name, (_, expected) in gates.items():
        if reports[name].get("status") != expected:
            raise ValueError(f"{name}: expected {expected}, got {reports[name].get('status')}")
    log_path = run / "logs/solver/frozen_flow_actual_1s.log"
    with log_path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 4096))
        log_tail = stream.read().decode("utf-8", errors="replace")
    if not re.search(r"(?m)^End\s*$", log_tail):
        raise ValueError("solver log has no clean End marker")
    summary = reports["transient"].get("final_summary", {})
    if not isinstance(summary, dict) or summary.get("final_time_s") != 1800.0:
        raise ValueError("validated transient final time is not 1800 s")

    packed_dir = run / "reports/viewer"
    if config is None:
        root = Path(__file__).resolve().parents[2]
        config = load_config(
            root / "configs/baseline_23cell.yaml",
            overlay_paths=(root / "configs/ppt_bernardi_udf.yaml",),
        )
    packed_report = _read_json(packed_dir / "actual_1s_validation.json")
    if packed_report.get("status") != "ACTUAL_ONE_SECOND_BUNDLE_VALIDATION_PASSED":
        update("packing", "1초 원본 온도 프레임을 포장하고 전수검증 중입니다.")
        build_actual_one_second_bundle(
            run,
            packed_dir,
            initial_temperature_k=config.boundary.initial_solid_temperature_k,
            initial_fluid_temperature_k=config.boundary.initial_fluid_temperature_k,
            end_time_s=1800,
            mesh_case_dir=case,
        )
        packed_report = validate_actual_one_second_bundle(run, packed_dir)
    if packed_report.get("status") != "ACTUAL_ONE_SECOND_BUNDLE_VALIDATION_PASSED":
        raise ValueError("1-second probe frame validation failed")

    update("viewer", "검증된 원본 필드와 1초 프레임을 3D 뷰어에 연결 중입니다.")
    balance = reports["energy_balance"]
    result_manifest = {
        "schema_version": 1,
        "case_name": config.case_name,
        "data_status": "CFD_PILOT_RESULTS_READY",
        "actual_solver_data": True,
        "validation_status": reports["transient"]["status"],
        "quality_status": "SINGLE_MESH_REVIEW_REQUIRED",
        "publication_authorized": False,
        "source_run": str(run),
        "source_reports": [str(path) for path, _ in gates.values()],
        "final_summary": {
            **summary,
            "complete_energy_balance_mean_fraction": balance.get(
                "final_60s_mean_imbalance_fraction"
            ),
        },
    }
    adaptive_audit = _read_json(report_dir / "energy_coupling_audit.json")
    if adaptive_audit.get("status") == "ENERGY_COUPLING_HISTORY_EXTRACTED":
        cap_failures = (
            int(adaptive_audit.get("cap_reached_count", 0))
            - int(adaptive_audit.get("cap_reached_both_h_below_target_count", 0))
            - int(adaptive_audit.get("cap_reached_after_earlier_target_count", 0))
        )
        result_manifest["execution_variant"] = "adaptive_energy_coupling_trial"
        result_manifest["energy_residual_target"] = adaptive_audit.get("target")
        result_manifest["energy_coupling_cap_failures"] = cap_failures
        if cap_failures:
            result_manifest["validation_warnings"] = [
                f"{cap_failures}개 시간단계가 결합 반복 상한에 도달하여 "
                "에너지 잔차 목표를 충족하지 못했습니다."
            ]
    _write_json(case / "results/result_manifest.json", result_manifest)
    model = write_viewer_bundle(config, viewer, case_dir=case)
    if (
        not model.get("actual_solver_data")
        or not model.get("interactive_cfd_by_mesh", {}).get("precision", {}).get(
            "actual_one_second"
        )
        or not (viewer / "index.html").is_file()
    ):
        raise ValueError("viewer build did not expose the verified precision frames")
    result = {
        "stage": "complete",
        "message": "계산·에너지 수지·1초 프레임 검증을 통과한 검토용 3D 뷰어가 준비됐습니다.",
        "viewer": str(viewer / "index.html"),
        "publication_authorized": False,
    }
    _write_json(state_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish validated baseline CFD review viewer")
    parser.add_argument("--case-dir", required=True, type=Path)
    parser.add_argument("--viewer-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = publish_completed_run(args.case_dir, args.viewer_dir)
    except Exception as exc:
        _write_json(
            args.case_dir / "reports/workflow_postprocess.json",
            {"stage": "failed", "message": str(exc)},
        )
        raise
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
