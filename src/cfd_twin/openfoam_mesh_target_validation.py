from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


CELL_TYPES = (
    "hexahedra",
    "prisms",
    "wedges",
    "pyramids",
    "tet wedges",
    "tetrahedra",
    "polyhedra",
)


def _within(actual: int, target: int | None, tolerance: float) -> bool:
    return target is None or abs(actual - target) / target <= tolerance


def _mesh_stats(text: str) -> dict[str, int]:
    points = re.search(r"(?m)^\s*points:\s+(\d+)\s*$", text)
    cells = re.search(r"(?m)^\s*cells:\s+(\d+)\s*$", text)
    if points is None or cells is None:
        raise ValueError("pre-split checkMesh log has no points/cells block")
    result = {"points": int(points.group(1)), "total_cells": int(cells.group(1))}
    for cell_type in CELL_TYPES:
        match = re.search(rf"(?m)^\s*{re.escape(cell_type)}:\s+(\d+)\s*$", text)
        result[cell_type.replace(" ", "_")] = int(match.group(1)) if match else 0
    return result


def validate_mesh_target(
    zone_report_path: str | Path,
    checkmesh_log_path: str | Path,
    *,
    target_total_cells: int,
    target_solid_cells: int | None = None,
    target_fluid_cells: int | None = None,
    target_points: int | None = None,
    minimum_total_cells: int | None = None,
    total_tolerance_fraction: float = 0.10,
    component_tolerance_fraction: float = 0.20,
    minimum_hexahedron_fraction: float = 0.80,
) -> dict[str, object]:
    if target_total_cells <= 0:
        raise ValueError("target total cells must be positive")
    if minimum_total_cells is not None and minimum_total_cells <= 0:
        raise ValueError("minimum total cells must be positive")
    if not 0 <= total_tolerance_fraction < 1:
        raise ValueError("total tolerance fraction must be in [0, 1)")
    if not 0 <= component_tolerance_fraction < 1:
        raise ValueError("component tolerance fraction must be in [0, 1)")
    if not 0 < minimum_hexahedron_fraction <= 1:
        raise ValueError("minimum hexahedron fraction must be in (0, 1]")
    zone_report = json.loads(Path(zone_report_path).read_text(encoding="utf-8"))
    if zone_report.get("status") != "CELLZONE_VALIDATION_PASSED":
        raise ValueError("cell-zone report has not passed")
    topology = _mesh_stats(
        Path(checkmesh_log_path).read_text(encoding="utf-8", errors="replace")
    )
    zones = zone_report.get("zone_cell_counts", {})
    fluid_cells = int(zones.get("coolant", 0))
    solid_cells = sum(
        int(value)
        for name, value in zones.items()
        if re.fullmatch(r"battery_C\d{2}", str(name))
    )
    total_cells = int(zone_report["mesh_cell_count"])
    if topology["total_cells"] != total_cells or fluid_cells + solid_cells != total_cells:
        raise ValueError("pre-split topology and cell-zone totals disagree")
    hexahedron_fraction = topology["hexahedra"] / total_cells
    actual = {
        "total_cells": total_cells,
        "solid_cells": solid_cells,
        "fluid_cells": fluid_cells,
        "points": topology["points"],
        "cell_types": {name: topology[name] for name in (item.replace(" ", "_") for item in CELL_TYPES)},
        "hexahedron_fraction": hexahedron_fraction,
    }
    targets = {
        "total_cells": target_total_cells,
        "solid_cells": target_solid_cells,
        "fluid_cells": target_fluid_cells,
        "points": target_points,
    }
    checks = {
        "total_cell_count_at_least_configured_minimum_if_set": (
            minimum_total_cells is None or total_cells >= minimum_total_cells
        ),
        "total_cell_count_within_configured_tolerance": _within(
            total_cells, target_total_cells, total_tolerance_fraction
        ),
        "solid_cell_count_within_configured_tolerance_if_targeted": _within(
            solid_cells, target_solid_cells, component_tolerance_fraction
        ),
        "fluid_cell_count_within_configured_tolerance_if_targeted": _within(
            fluid_cells, target_fluid_cells, component_tolerance_fraction
        ),
        "point_count_within_configured_tolerance_if_targeted": _within(
            topology["points"], target_points, component_tolerance_fraction
        ),
        "hexahedron_fraction_at_least_configured_minimum": (
            hexahedron_fraction >= minimum_hexahedron_fraction
        ),
    }
    return {
        "schema_version": 1,
        "status": (
            "MESH_TARGET_VALIDATION_PASSED"
            if all(checks.values())
            else "MESH_TARGET_VALIDATION_FAILED"
        ),
        "mesh_family": "hexa-dominant body-fitted",
        "all_hexa_claimed": False,
        "targets": targets,
        "actual": actual,
        "relative_error": {
            name: (actual[name] - target) / target
            for name, target in targets.items()
            if target is not None
        },
        "limits": {
            "total_tolerance_fraction": total_tolerance_fraction,
            "component_tolerance_fraction": component_tolerance_fraction,
            "minimum_hexahedron_fraction": minimum_hexahedron_fraction,
            "minimum_total_cells": minimum_total_cells,
        },
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate mesh count and hexa-dominant composition")
    parser.add_argument("--zone-report", required=True)
    parser.add_argument("--checkmesh-log", required=True)
    parser.add_argument("--target-total-cells", required=True, type=int)
    parser.add_argument("--target-solid-cells", type=int)
    parser.add_argument("--target-fluid-cells", type=int)
    parser.add_argument("--target-points", type=int)
    parser.add_argument("--minimum-total-cells", type=int)
    parser.add_argument("--total-tolerance-fraction", type=float, default=0.10)
    parser.add_argument("--component-tolerance-fraction", type=float, default=0.20)
    parser.add_argument("--minimum-hexahedron-fraction", type=float, default=0.80)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        report = validate_mesh_target(
            args.zone_report,
            args.checkmesh_log,
            target_total_cells=args.target_total_cells,
            target_solid_cells=args.target_solid_cells,
            target_fluid_cells=args.target_fluid_cells,
            target_points=args.target_points,
            minimum_total_cells=args.minimum_total_cells,
            total_tolerance_fraction=args.total_tolerance_fraction,
            component_tolerance_fraction=args.component_tolerance_fraction,
            minimum_hexahedron_fraction=args.minimum_hexahedron_fraction,
        )
    except (OSError, UnicodeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        report = {
            "schema_version": 1,
            "status": "MESH_TARGET_VALIDATION_FAILED",
            "error": str(exc),
        }
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    destination.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "MESH_TARGET_VALIDATION_PASSED" else 2


if __name__ == "__main__":
    sys.exit(main())
