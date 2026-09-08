from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REQUIRED_REPORTS = {
    "checkMesh_all_regions.json": "CHECKMESH_LOG_PASSED",
    "cellZones_pre_split.json": "CELLZONE_VALIDATION_PASSED",
    "cellZones_batterySolid.json": "CELLZONE_VALIDATION_PASSED",
    "mesh_integrals_pre_split.json": "MESH_INTEGRAL_VALIDATION_PASSED",
    "interfaces_post_split.json": "INTERFACE_VALIDATION_PASSED",
    "layer_coverage.json": "LAYER_VALIDATION_PASSED",
    "surfaceCheck_outerDomain.json": "SURFACECHECK_LOG_PASSED",
    **{
        f"surfaceCheck_battery_C{index:02d}.json": "SURFACECHECK_LOG_PASSED"
        for index in range(1, 24)
    },
}


def aggregate_production_gate(report_dir: str | Path) -> dict[str, object]:
    directory = Path(report_dir).resolve()
    observed: dict[str, str | None] = {}
    errors: dict[str, str] = {}
    for name, expected_status in REQUIRED_REPORTS.items():
        path = directory / name
        if not path.is_file():
            observed[name] = None
            errors[name] = "missing report"
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            observed[name] = None
            errors[name] = f"invalid report: {exc}"
            continue
        status = data.get("status")
        observed[name] = status if isinstance(status, str) else None
        if status != expected_status:
            errors[name] = f"expected {expected_status}, found {status!r}"
    passed = not errors
    return {
        "schema_version": 1,
        "status": (
            "PRODUCTION_MESH_GATE_PASSED"
            if passed
            else "PRODUCTION_MESH_GATE_FAILED"
        ),
        "report_dir": str(directory),
        "required_report_count": len(REQUIRED_REPORTS),
        "observed_statuses": observed,
        "errors": errors,
        "runnable_solver_pilots": passed,
        "publication_results_authorized": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate all OpenFOAM production-mesh blocker reports"
    )
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    report = aggregate_production_gate(args.report_dir)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "PRODUCTION_MESH_GATE_PASSED" else 2


if __name__ == "__main__":
    sys.exit(main())
