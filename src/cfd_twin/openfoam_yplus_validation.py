from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

try:
    from .openfoam_mesh_integrals import (
        _ascii,
        _boundary,
        _face_area,
        _faces,
        _points,
    )
except ImportError:  # Standalone copy inside a generated OpenFOAM case.
    from openfoam_mesh_integrals import (
        _ascii,
        _boundary,
        _face_area,
        _faces,
        _points,
    )


NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
BATTERY_PATCH = re.compile(
    r"^battery_C\d{2}_interface(?:_coolant_to_batterySolid)?$"
)


def _balanced_block(text: str, opening_brace: int) -> str:
    depth = 1
    cursor = opening_brace + 1
    while cursor < len(text) and depth:
        if text[cursor] == "{":
            depth += 1
        elif text[cursor] == "}":
            depth -= 1
        cursor += 1
    if depth:
        raise ValueError("unterminated OpenFOAM dictionary block")
    return text[opening_brace + 1 : cursor - 1]


def parse_scalar_boundary_field(path: str | Path) -> dict[str, list[float]]:
    text = _ascii(Path(path))
    boundary_match = re.search(r"\bboundaryField\s*\{", text)
    if not boundary_match:
        raise ValueError("boundaryField block not found")
    opening = text.find("{", boundary_match.start())
    body = _balanced_block(text, opening)
    patches: dict[str, list[float]] = {}
    patch_pattern = re.compile(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\{")
    for match in patch_pattern.finditer(body):
        block_opening = body.find("{", match.start())
        block = _balanced_block(body, block_opening)
        uniform = re.search(rf"\bvalue\s+uniform\s+({NUMBER})\s*;", block)
        if uniform:
            patches[match.group(1)] = [float(uniform.group(1))]
            continue
        nonuniform = re.search(
            rf"\bvalue\s+nonuniform\s+List<scalar>\s+(\d+)\s*\((.*?)\)\s*;",
            block,
            flags=re.DOTALL,
        )
        if nonuniform:
            declared = int(nonuniform.group(1))
            values = [float(value) for value in re.findall(NUMBER, nonuniform.group(2))]
            if len(values) != declared:
                raise ValueError(
                    f"{match.group(1)} declares {declared} values but contains {len(values)}"
                )
            patches[match.group(1)] = values
    return patches


def _weighted_quantile(
    values_and_weights: list[tuple[float, float]], quantile: float
) -> float | None:
    if not values_and_weights:
        return None
    ordered = sorted(values_and_weights)
    total = sum(weight for _, weight in ordered)
    target = quantile * total
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= target:
            return value
    return ordered[-1][0]


def _summary(values_and_weights: list[tuple[float, float]]) -> dict[str, float | int | None]:
    if not values_and_weights:
        return {
            "face_count": 0,
            "area_m2": 0.0,
            "minimum": None,
            "area_weighted_mean": None,
            "area_weighted_p50": None,
            "area_weighted_p95": None,
            "area_weighted_p99": None,
            "maximum": None,
            "area_fraction_below_1": None,
        }
    total_area = sum(weight for _, weight in values_and_weights)
    return {
        "face_count": len(values_and_weights),
        "area_m2": total_area,
        "minimum": min(value for value, _ in values_and_weights),
        "area_weighted_mean": sum(
            value * weight for value, weight in values_and_weights
        )
        / total_area,
        "area_weighted_p50": _weighted_quantile(values_and_weights, 0.50),
        "area_weighted_p95": _weighted_quantile(values_and_weights, 0.95),
        "area_weighted_p99": _weighted_quantile(values_and_weights, 0.99),
        "maximum": max(value for value, _ in values_and_weights),
        "area_fraction_below_1": sum(
            weight for value, weight in values_and_weights if value < 1.0
        )
        / total_area,
    }


def validate_yplus(
    mesh_dir: str | Path,
    field_path: str | Path,
    *,
    minimum_battery_area_fraction_below_1: float = 0.95,
) -> dict[str, object]:
    mesh = Path(mesh_dir).resolve()
    field = Path(field_path).resolve()
    points = _points(mesh / "points")
    faces = _faces(mesh / "faces")
    face_areas = [_face_area(points, face) for face in faces]
    boundary = _boundary(mesh / "boundary")
    field_values = parse_scalar_boundary_field(field)
    battery_names = sorted(name for name in boundary if BATTERY_PATCH.fullmatch(name))

    errors: list[str] = []
    if len(battery_names) != 23:
        errors.append(f"expected 23 coolant-side battery patches, found {len(battery_names)}")
    per_patch: dict[str, dict[str, float | int | None]] = {}
    battery_values: list[tuple[float, float]] = []
    for name in battery_names:
        start, count = boundary[name]
        values = field_values.get(name)
        if values is None:
            errors.append(f"missing yPlus values for {name}")
            continue
        if len(values) == 1:
            values = values * count
        if len(values) != count:
            errors.append(f"{name}: expected {count} values, found {len(values)}")
            continue
        weighted = list(zip(values, face_areas[start : start + count], strict=True))
        per_patch[name] = _summary(weighted)
        battery_values.extend(weighted)

    all_finite_nonnegative = all(
        math.isfinite(value) and value >= 0 for value, _ in battery_values
    )
    battery_summary = _summary(battery_values)
    fraction = battery_summary["area_fraction_below_1"]
    checks = {
        "exactly_23_battery_patches": len(battery_names) == 23,
        "all_patch_values_match_mesh_faces": not errors,
        "all_battery_yplus_finite_nonnegative": all_finite_nonnegative,
        "at_least_95_percent_battery_area_below_1": (
            fraction is not None
            and fraction >= minimum_battery_area_fraction_below_1
        ),
    }
    return {
        "schema_version": 1,
        "status": (
            "YPLUS_VALIDATION_PASSED"
            if all(checks.values())
            else "YPLUS_VALIDATION_FAILED"
        ),
        "mesh_dir": str(mesh),
        "field_path": str(field),
        "thresholds": {
            "battery_yplus": 1.0,
            "minimum_battery_area_fraction_below_threshold": minimum_battery_area_fraction_below_1,
        },
        "checks": checks,
        "battery_surface": battery_summary,
        "per_battery_patch": per_patch,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate area-weighted battery-wall y+ from an ASCII OpenFOAM field"
    )
    parser.add_argument("--mesh-dir", required=True)
    parser.add_argument("--field", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        report = validate_yplus(args.mesh_dir, args.field)
    except (OSError, UnicodeError, ValueError) as exc:
        report = {
            "schema_version": 1,
            "status": "YPLUS_VALIDATION_FAILED",
            "error": str(exc),
        }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "YPLUS_VALIDATION_PASSED" else 2


if __name__ == "__main__":
    sys.exit(main())
