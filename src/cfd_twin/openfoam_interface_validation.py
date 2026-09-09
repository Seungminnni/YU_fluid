from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

try:
    from .openfoam_mesh_integrals import mesh_integrals
except ImportError:  # Standalone copy inside a generated OpenFOAM case.
    from openfoam_mesh_integrals import mesh_integrals


BATTERY_IDS = tuple(f"C{index:02d}" for index in range(1, 24))


def _interface_areas(patch_areas: dict[str, float]) -> tuple[dict[str, float], dict[str, list[str]]]:
    areas: dict[str, float] = {}
    names: dict[str, list[str]] = {}
    for battery_id in BATTERY_IDS:
        marker = f"battery_{battery_id}_interface"
        matching = sorted(name for name in patch_areas if marker in name)
        names[battery_id] = matching
        areas[battery_id] = sum(patch_areas[name] for name in matching)
    return areas, names


def validate_interfaces(
    coolant_mesh_dir: str | Path,
    solid_mesh_dir: str | Path,
    *,
    expected_each_area_m2: float,
    geometry_tolerance: float = 0.05,
    paired_area_tolerance: float = 1e-9,
) -> dict[str, object]:
    if expected_each_area_m2 <= 0:
        raise ValueError("expected interface area must be positive")
    coolant = mesh_integrals(coolant_mesh_dir)
    solid = mesh_integrals(solid_mesh_dir)
    coolant_areas, coolant_names = _interface_areas(coolant["patch_areas_m2"])
    solid_areas, solid_names = _interface_areas(solid["patch_areas_m2"])

    missing_coolant = [name for name in BATTERY_IDS if not coolant_names[name]]
    missing_solid = [name for name in BATTERY_IDS if not solid_names[name]]
    empty = [
        name
        for name in BATTERY_IDS
        if coolant_areas[name] <= 0 or solid_areas[name] <= 0
    ]
    pair_errors = {
        name: abs(coolant_areas[name] - solid_areas[name])
        / max(coolant_areas[name], solid_areas[name], sys.float_info.min)
        for name in BATTERY_IDS
    }
    geometry_errors = {
        name: max(
            abs(coolant_areas[name] - expected_each_area_m2),
            abs(solid_areas[name] - expected_each_area_m2),
        )
        / expected_each_area_m2
        for name in BATTERY_IDS
    }
    coolant_total = sum(coolant_areas.values())
    solid_total = sum(solid_areas.values())
    expected_total = expected_each_area_m2 * len(BATTERY_IDS)
    total_pair_error = abs(coolant_total - solid_total) / max(
        coolant_total, solid_total, sys.float_info.min
    )
    total_geometry_error = max(
        abs(coolant_total - expected_total), abs(solid_total - expected_total)
    ) / expected_total

    checks = {
        "all_23_coolant_interface_patches_present": not missing_coolant,
        "all_23_solid_interface_patches_present": not missing_solid,
        "all_interface_areas_positive": not empty,
        "each_paired_interface_area_matches": max(pair_errors.values(), default=math.inf)
        <= paired_area_tolerance,
        "each_interface_matches_geometry": max(
            geometry_errors.values(), default=math.inf
        )
        <= geometry_tolerance,
        "total_paired_interface_area_matches": total_pair_error
        <= paired_area_tolerance,
        "total_interface_matches_geometry": total_geometry_error
        <= geometry_tolerance,
    }
    return {
        "schema_version": 1,
        "status": (
            "INTERFACE_VALIDATION_PASSED"
            if all(checks.values())
            else "INTERFACE_VALIDATION_FAILED"
        ),
        "checks": checks,
        "tolerances": {
            "paired_area_relative": paired_area_tolerance,
            "geometry_area_relative": geometry_tolerance,
        },
        "expected": {
            "each_interface_area_m2": expected_each_area_m2,
            "total_interface_area_m2": expected_total,
        },
        "actual": {
            "coolant_each_area_m2": coolant_areas,
            "solid_each_area_m2": solid_areas,
            "coolant_total_area_m2": coolant_total,
            "solid_total_area_m2": solid_total,
            "coolant_patch_names": coolant_names,
            "solid_patch_names": solid_names,
        },
        "relative_errors": {
            "paired_each": pair_errors,
            "geometry_each": geometry_errors,
            "paired_total": total_pair_error,
            "geometry_total": total_geometry_error,
        },
        "missing_coolant_interfaces": missing_coolant,
        "missing_solid_interfaces": missing_solid,
        "nonpositive_interfaces": empty,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate 23 paired CHT interface areas from split ASCII polyMesh files"
    )
    parser.add_argument("--coolant-mesh-dir", required=True)
    parser.add_argument("--solid-mesh-dir", required=True)
    parser.add_argument("--expected-each-area", required=True, type=float)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        report = validate_interfaces(
            args.coolant_mesh_dir,
            args.solid_mesh_dir,
            expected_each_area_m2=args.expected_each_area,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        report = {
            "schema_version": 1,
            "status": "INTERFACE_VALIDATION_FAILED",
            "error": str(exc),
        }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "INTERFACE_VALIDATION_PASSED" else 2


if __name__ == "__main__":
    sys.exit(main())
