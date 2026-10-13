#!/usr/bin/env python3
"""Report which published CFD cases expose pressure, mass flux and surface HTC."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--viewer", type=Path, default=Path("viewer"))
    args = parser.parse_args()
    viewer = args.viewer.resolve()
    published = read(viewer / "four_case_manifest.json")
    pressure_report = read(viewer / "pressure_backfill_report.json")
    pressure_runs = set(pressure_report.get("runs", {}))
    rows = []
    for run in published.get("runs", []):
        run_id = str(run["id"])
        asset_id = str(run.get("asset_source_id") or run_id)
        manifest_path = viewer / "data" / asset_id / "actual_1s_manifest.json"
        actual = read(manifest_path) if manifest_path.is_file() else {}
        datasets = actual.get("datasets", {})
        section_names = (
            "xy", "xy15", "xy33", "xy68", "xy85",
            "xz15", "xz33", "xz50", "xz68", "xz85",
            "yz15", "yz33", "yz50", "yz68", "yz85",
        )
        velocity_sections = all(name in datasets for name in section_names)
        pressure_available = run_id in pressure_runs or asset_id in pressure_runs
        htc = datasets.get("cell_surface_heat_transfer_coefficient")
        htc_available = isinstance(htc, dict) and htc.get("frame_count") == 1800
        rows.append({
            "id": run_id,
            "asset_source_id": asset_id,
            "label": run.get("label"),
            "calculation_complete": bool(run.get("data_ready")),
            "mass_flux_sections_available": velocity_sections,
            "mass_flux_semantics": "viewer-derived |rho*U| from computed velocity sections and configured coolant density",
            "pressure_sections_available": pressure_available,
            "surface_heat_transfer_available": htc_available,
            "surface_heat_transfer_frames": htc.get("frame_count") if htc_available else 0,
        })
    report = {
        "schema_version": 1,
        "status": "VIEWER_FIELD_AVAILABILITY_AUDITED",
        "runs": rows,
        "counts": {
            "published": len(rows),
            "mass_flux": sum(row["mass_flux_sections_available"] for row in rows),
            "pressure": sum(row["pressure_sections_available"] for row in rows),
            "surface_heat_transfer": sum(row["surface_heat_transfer_available"] for row in rows),
        },
        "note": "Unavailable pressure/HTC fields are not synthesized; retained raw fields or a fresh calculation are required.",
    }
    output = viewer / "field_availability.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
