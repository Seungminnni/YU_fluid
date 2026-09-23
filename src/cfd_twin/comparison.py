"""Assemble two independently calculated CFD cases for the comparison viewer.

The assembled case contains links only.  It never copies or mutates either
solver case, so the preserved result remains recoverable and each selectable
mesh keeps its own fields, convergence history, and validation reports.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .errors import CfdTwinError


MINIMUM_COMPARISON_CELLS = 600_000


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CfdTwinError(f"required comparison artifact is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CfdTwinError(f"invalid JSON comparison artifact: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CfdTwinError(f"comparison artifact must contain a JSON object: {path}")
    return value


def _mesh_metadata(case: Path) -> dict[str, object]:
    gate_path = case / "reports/production_mesh/production_mesh_gate.json"
    target_path = case / "reports/production_mesh/mesh_target_composition.json"
    gate = _read_json(gate_path)
    target = _read_json(target_path)
    actual = target.get("actual")
    if gate.get("status") != "PRODUCTION_MESH_GATE_PASSED":
        raise CfdTwinError(f"production mesh gate has not passed: {gate_path}")
    if target.get("status") != "MESH_TARGET_VALIDATION_PASSED" or not isinstance(actual, dict):
        raise CfdTwinError(f"mesh target validation has not passed: {target_path}")
    cells = int(actual.get("total_cells", 0))
    if cells < MINIMUM_COMPARISON_CELLS:
        raise CfdTwinError(
            f"comparison mesh has {cells:,} cells; at least {MINIMUM_COMPARISON_CELLS:,} are required"
        )
    return {"total_cells": cells, "actual": actual}


def _validated_result_ready(case: Path) -> bool:
    validation = case / "runs/frozen_flow_actual_1s/reports/solver/frozen_flow_validation.json"
    energy = case / "runs/frozen_flow_actual_1s/reports/solver/energy_balance.json"
    if not validation.is_file() or not energy.is_file():
        return False
    return (
        str(_read_json(validation).get("status", "")).endswith("_PASSED")
        and _read_json(energy).get("status") == "TRANSIENT_ENERGY_BALANCE_PASSED"
    )


def _ensure_link(link: Path, target: Path) -> None:
    target = target.resolve()
    if link.is_symlink():
        if link.resolve(strict=False) == target:
            return
        link.unlink()
    elif link.exists():
        raise CfdTwinError(f"refusing to replace non-symlink comparison path: {link}")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(os.path.relpath(target, link.parent), target_is_directory=target.is_dir())


def prepare_comparison_case(
    phi19_case: Path,
    phi23_case: Path,
    output: Path,
    *,
    require_results: bool = False,
) -> dict[str, object]:
    """Create a non-destructive Ø19/Ø23 case tree consumed by the viewer."""

    phi19_case = phi19_case.resolve()
    phi23_case = phi23_case.resolve()
    output = output.resolve()
    phi19 = _mesh_metadata(phi19_case)
    phi23 = _mesh_metadata(phi23_case)
    result_ready = {
        "precision": _validated_result_ready(phi19_case),
        "single": _validated_result_ready(phi23_case),
    }
    if require_results and not all(result_ready.values()):
        missing = "Ø19" if not result_ready["precision"] else "Ø23"
        if not result_ready["precision"] and not result_ready["single"]:
            missing = "Ø19 and Ø23"
        raise CfdTwinError(f"validated transient results are not ready for {missing}")

    output.mkdir(parents=True, exist_ok=True)
    for name in ("constant", "runs", "reports", "results"):
        _ensure_link(output / name, phi19_case / name)
    _ensure_link(output / "production/L3", phi19_case)
    _ensure_link(output / "production/single", phi23_case)

    def run_metadata(
        run_id: str, diameter: int, mesh: dict[str, object], ready: bool
    ) -> dict[str, object]:
        cells = int(mesh["total_cells"])
        return {
            "id": run_id,
            "comparison_key": f"phi{diameter}",
            "label": f"Ø{diameter} mm · {cells:,}셀",
            "label_en": f"Ø{diameter} mm · {cells:,} cells",
            "state_label": "계산·검증 완료" if ready else "메시 검증 완료",
            "state_label_en": "Calculation and Validation Complete" if ready else "Mesh Validation Complete",
            "note": f"Ø{diameter} mm 포트 조건의 독립 CFD 결과입니다.",
            "note_en": f"Independent CFD result for the Ø{diameter} mm port case.",
            "inlet_diameter_mm": diameter,
            "outlet_diameter_mm": diameter,
            "total_cells": cells,
            "result_ready": ready,
        }

    manifest = {
        "status": (
            "COMPARISON_RESULTS_READY" if all(result_ready.values())
            else "COMPARISON_MESHES_READY"
        ),
        "minimum_total_cells": MINIMUM_COMPARISON_CELLS,
        "runs": [
            run_metadata("precision", 19, phi19, result_ready["precision"]),
            run_metadata("single", 23, phi23, result_ready["single"]),
        ],
    }
    destination = output / "comparison_manifest.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return manifest


def publish_comparison_results(
    phi19_case: Path,
    phi23_case: Path,
    comparison_case: Path,
    viewer_output: Path,
    *,
    phi19_config: object,
    phi23_config: object,
) -> dict[str, object]:
    """Publish, cross-check, and build the final two-result portable viewer."""

    from .viewer import write_viewer_bundle
    from .workflow_publish import _write_json, publish_completed_run

    phi19_case = phi19_case.resolve()
    phi23_case = phi23_case.resolve()
    comparison_case = comparison_case.resolve()
    viewer_output = viewer_output.resolve()
    with tempfile.TemporaryDirectory(prefix="cfd-twin-comparison-") as temporary:
        scratch = Path(temporary)
        publish_completed_run(
            phi19_case,
            scratch / "phi19",
            config=phi19_config,
            include_reference_cad=False,
        )
        publish_completed_run(
            phi23_case,
            scratch / "phi23",
            config=phi23_config,
            include_reference_cad=False,
        )

    manifest = prepare_comparison_case(
        phi19_case,
        phi23_case,
        comparison_case,
        require_results=True,
    )
    model = write_viewer_bundle(
        phi19_config,
        viewer_output,
        case_dir=comparison_case,
        include_reference_cad=True,
    )
    interactive = model.get("interactive_cfd_by_mesh", {})
    if not isinstance(interactive, dict):
        raise CfdTwinError("comparison viewer has no per-mesh CFD payload")
    for mesh_id in ("precision", "single"):
        payload = interactive.get(mesh_id, {})
        if not isinstance(payload, dict) or not payload.get("actual_one_second"):
            raise CfdTwinError(f"comparison viewer is missing verified {mesh_id} frames")
        if not (viewer_output / "data" / mesh_id / "actual_1s_validation.json").is_file():
            raise CfdTwinError(f"comparison viewer did not copy {mesh_id} validation data")
    displayed_counts = {
        str(run.get("id")): int(run.get("counts", {}).get("total_cells", 0))
        for run in model.get("mesh_runs", [])
        if isinstance(run, dict) and isinstance(run.get("counts"), dict)
    }
    expected_counts = {
        str(run["id"]): int(run["total_cells"])
        for run in manifest["runs"]
    }
    if displayed_counts != expected_counts:
        raise CfdTwinError(
            f"viewer mesh counts do not match validated cases: {displayed_counts} != {expected_counts}"
        )
    completion = {
        "stage": "complete",
        "message": "Ø19 mm와 Ø23 mm의 독립 계산·검증 결과가 비교 뷰어에 연결됐습니다.",
        "viewer": str(viewer_output / "results.html"),
        "comparison_status": manifest["status"],
        "publication_authorized": False,
    }
    for case in (phi19_case, phi23_case):
        _write_json(case / "reports/workflow_postprocess.json", completion)
    return {
        **completion,
        "case": str(comparison_case),
        "mesh_counts": expected_counts,
    }
