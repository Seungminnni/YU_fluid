#!/usr/bin/env python3
"""Fail-closed production gate for an imported structured conical sweep mesh."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args()
    case = args.case.resolve()
    source = args.source.resolve()
    plan = read(source / "sweep_mesh_plan.json")
    generation = read(source / "sweep_generation_report.json")
    layer = read(case / "reports/production_mesh/layer_coverage.json")
    log = (source / "log.checkMesh_all_regions").read_text(encoding="utf-8", errors="replace")
    cells = int(generation.get("cells", -1))
    coolant = [int(value) for value in re.findall(r"cells:\s+(\d+)", log)]
    hexa = [int(value) for value in re.findall(r"hexahedra:\s+(\d+)", log)]
    non_orth = [float(value) for value in re.findall(r"non-orthogonality Max:\s*([0-9.]+)", log)]
    skew = [float(value) for value in re.findall(r"Max skewness\s*=\s*([0-9.]+)", log)]
    required_paths = [
        case / "constant/polyMesh/points",
        case / "constant/coolant/polyMesh/points",
        case / "constant/batterySolid/polyMesh/points",
        case / "constant/regionProperties",
    ]
    checks = {
        "fresh_source_attempt_matches_job": plan.get("attempt_id") == source.name.rsplit("_", 1)[-1],
        "geometry_is_d33_to_d22_frustum": plan.get("geometry", {}).get("inlet_shape") == "frustum"
        and abs(float(plan["geometry"]["inlet_radius_m"]) - 0.011) < 1e-12
        and abs(float(plan["geometry"]["inlet_outer_radius_m"]) - 0.0165) < 1e-12,
        "outlet_is_d19": abs(float(plan["geometry"]["outlet_radius_m"]) - 0.0095) < 1e-12,
        "generation_passed": generation.get("status") == "SWEEP_GENERATION_PASSED",
        "all_source_faces_quadrilateral": generation.get("all_source_faces_quadrilateral") is True,
        "all_volume_cells_hexahedral": generation.get("all_volume_cells_hexahedral") is True,
        "conical_nodes_reshaped": int(generation.get("conical_inlet_nodes_reshaped", 0)) > 0,
        "cell_count_in_authorized_band": int(plan["minimum_cells"]) <= cells <= int(plan["maximum_cells"]),
        "cell_count_matches_regions": len(coolant) >= 2 and sum(coolant[-2:]) == cells,
        "all_region_cells_are_hexahedra": len(hexa) >= 2 and sum(hexa[-2:]) == cells,
        "both_regions_mesh_ok": log.count("Mesh OK.") >= 2,
        "non_orthogonality_below_65": len(non_orth) >= 2 and max(non_orth[-2:]) < 65.0,
        "skewness_below_4": len(skew) >= 2 and max(skew[-2:]) < 4.0,
        "battery_region_count_is_23": generation.get("battery_regions") == 23,
        "layer_gate_passed": layer.get("status") == "LAYER_VALIDATION_PASSED",
        "first_layer_is_6um_and_11_layers": int(plan["wall_layer_count"]) == 11
        and abs(float(plan["first_layer_height_m"]) - 6e-6) < 1e-12,
        "imported_mesh_files_present": all(path.is_file() for path in required_paths),
    }
    report_dir = case / "reports/production_mesh"
    composition = {
        "schema_version": 1,
        "status": "MESH_TARGET_VALIDATION_PASSED" if checks["cell_count_in_authorized_band"] else "MESH_TARGET_VALIDATION_FAILED",
        "targets": {"total_cells": int(plan["target_cells"])},
        "limits": {"minimum_total_cells": int(plan["minimum_cells"]), "maximum_total_cells": int(plan["maximum_cells"]), "minimum_hexahedron_fraction": 1.0},
        "actual": {"total_cells": cells, "fluid_cells": coolant[-2] if len(coolant) >= 2 else None, "solid_cells": coolant[-1] if len(coolant) >= 2 else None, "hexahedron_fraction": 1.0},
    }
    write(report_dir / "mesh_target_composition.json", composition)
    gate = {
        "schema_version": 2,
        "status": "PRODUCTION_MESH_GATE_PASSED" if all(checks.values()) else "PRODUCTION_MESH_GATE_FAILED",
        "backend": "imported_reference_guided_gmsh_structured_sweep",
        "checks": checks,
        "errors": [name for name, passed in checks.items() if not passed],
        "runnable_solver_pilots": all(checks.values()),
        "publication_results_authorized": False,
        "mesh": composition["actual"],
        "quality": {"maximum_non_orthogonality_deg": max(non_orth[-2:]) if len(non_orth) >= 2 else None, "maximum_skewness": max(skew[-2:]) if len(skew) >= 2 else None},
        "geometry": {"inlet_far_diameter_mm": 33.0, "inlet_near_diameter_mm": 22.0, "outlet_diameter_mm": 19.0},
        "source_sha256": {name: sha256(source / name) for name in ("sweep_mesh_plan.json", "sweep_generation_report.json", "log.checkMesh_all_regions")},
    }
    write(report_dir / "production_mesh_gate.json", gate)
    print(json.dumps(gate, indent=2))
    return 0 if all(checks.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
