from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

try:
    from .openfoam_zone_validation import parse_cell_zones
except ImportError:  # Standalone copy inside a generated OpenFOAM case.
    try:
        from openfoam_zone_validation import parse_cell_zones
    except ImportError:
        # The executable generated-case copy is named validate_cell_zones.py.
        from validate_cell_zones import parse_cell_zones


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*?$", "", text, flags=re.MULTILINE)


def _ascii(path: Path) -> str:
    raw = path.read_bytes()
    if b"format      binary" in raw or b"format binary" in raw:
        raise ValueError(f"{path}: binary mesh file unsupported; use writeFormat ascii")
    return _strip_comments(raw.decode("utf-8"))


def _list_body(path: Path) -> tuple[int, str]:
    text = _ascii(path)
    header_end = text.find("}")
    if header_end < 0:
        raise ValueError(f"{path}: FoamFile header end not found")
    match = re.search(r"(\d+)\s*\(", text[header_end + 1 :])
    if not match:
        raise ValueError(f"{path}: list header not found")
    count = int(match.group(1))
    start = header_end + 1 + match.end()
    depth = 1
    cursor = start
    while cursor < len(text) and depth:
        if text[cursor] == "(":
            depth += 1
        elif text[cursor] == ")":
            depth -= 1
        cursor += 1
    if depth:
        raise ValueError(f"{path}: unterminated list")
    return count, text[start : cursor - 1]


def _points(path: Path) -> list[tuple[float, float, float]]:
    count, body = _list_body(path)
    number = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
    values = [
        (float(x), float(y), float(z))
        for x, y, z in re.findall(
            rf"\(\s*({number})\s+({number})\s+({number})\s*\)", body
        )
    ]
    if len(values) != count:
        raise ValueError(f"{path}: declared {count} points but parsed {len(values)}")
    return values


def _faces(path: Path) -> list[list[int]]:
    count, body = _list_body(path)
    values = [
        [int(label) for label in labels.split()]
        for _, labels in re.findall(r"(\d+)\s*\(([\d\s]+)\)", body)
    ]
    if len(values) != count:
        raise ValueError(f"{path}: declared {count} faces but parsed {len(values)}")
    for index, face in enumerate(values):
        if len(face) < 3:
            raise ValueError(f"{path}: face {index} has fewer than three points")
    return values


def _labels(path: Path) -> list[int]:
    count, body = _list_body(path)
    values = [int(value) for value in re.findall(r"[-+]?\d+", body)]
    if len(values) != count:
        raise ValueError(f"{path}: declared {count} labels but parsed {len(values)}")
    return values


def _boundary(path: Path) -> dict[str, tuple[int, int]]:
    _, body = _list_body(path)
    patches: dict[str, tuple[int, int]] = {}
    pattern = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\{")
    for match in pattern.finditer(body):
        depth = 1
        cursor = match.end()
        while cursor < len(body) and depth:
            if body[cursor] == "{":
                depth += 1
            elif body[cursor] == "}":
                depth -= 1
            cursor += 1
        block = body[match.end() : cursor - 1]
        n_faces = re.search(r"\bnFaces\s+(\d+)\s*;", block)
        start_face = re.search(r"\bstartFace\s+(\d+)\s*;", block)
        if n_faces and start_face:
            patches[match.group(1)] = (int(start_face.group(1)), int(n_faces.group(1)))
    return patches


def _cross(
    first: tuple[float, float, float], second: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )


def _dot(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    return sum(a * b for a, b in zip(first, second, strict=True))


def _face_area(points: list[tuple[float, float, float]], face: list[int]) -> float:
    area_vector = (0.0, 0.0, 0.0)
    for first_index, second_index in zip(face, face[1:] + face[:1]):
        cross = _cross(points[first_index], points[second_index])
        area_vector = tuple(a + b for a, b in zip(area_vector, cross, strict=True))
    return 0.5 * math.sqrt(_dot(area_vector, area_vector))


def _face_signed_volume(
    points: list[tuple[float, float, float]], face: list[int]
) -> float:
    first = points[face[0]]
    volume = 0.0
    for index in range(1, len(face) - 1):
        volume += _dot(first, _cross(points[face[index]], points[face[index + 1]])) / 6
    return volume


def mesh_integrals(mesh_dir: str | Path) -> dict[str, object]:
    mesh = Path(mesh_dir).resolve()
    points = _points(mesh / "points")
    faces = _faces(mesh / "faces")
    owner = _labels(mesh / "owner")
    neighbour = _labels(mesh / "neighbour")
    if len(owner) != len(faces):
        raise ValueError("owner count does not equal face count")
    if len(neighbour) > len(faces):
        raise ValueError("neighbour count exceeds face count")
    n_cells = max((*owner, *neighbour), default=-1) + 1
    signed_volumes = [0.0] * n_cells
    face_areas = [0.0] * len(faces)
    for face_index, face in enumerate(faces):
        signed = _face_signed_volume(points, face)
        signed_volumes[owner[face_index]] += signed
        if face_index < len(neighbour):
            signed_volumes[neighbour[face_index]] -= signed
        face_areas[face_index] = _face_area(points, face)
    cell_volumes = [abs(value) for value in signed_volumes]
    zone_path = mesh / "cellZones"
    zones = parse_cell_zones(zone_path) if zone_path.is_file() else {}
    zone_volumes = {
        name: sum(cell_volumes[label] for label in labels) for name, labels in zones.items()
    }
    patches = _boundary(mesh / "boundary")
    patch_areas = {
        name: sum(face_areas[start : start + count])
        for name, (start, count) in patches.items()
    }
    return {
        "mesh_dir": str(mesh),
        "point_count": len(points),
        "face_count": len(faces),
        "cell_count": n_cells,
        "nonpositive_cell_volume_count": sum(value <= 0 for value in cell_volumes),
        "minimum_cell_volume_m3": min(cell_volumes, default=None),
        "total_mesh_volume_m3": sum(cell_volumes),
        "zone_volumes_m3": zone_volumes,
        "patch_areas_m2": patch_areas,
    }


def validate_mesh_integrals(
    mesh_dir: str | Path,
    *,
    expected_battery_volume_m3: float,
    expected_coolant_volume_m3: float,
    expected_inlet_area_m2: float,
    expected_outlet_area_m2: float,
    total_volume_tolerance: float = 0.02,
    cell_volume_tolerance: float = 0.05,
    patch_area_tolerance: float = 0.02,
) -> dict[str, object]:
    values = mesh_integrals(mesh_dir)
    zone_volumes = values["zone_volumes_m3"]
    battery_names = [f"battery_C{index:02d}" for index in range(1, 24)]
    battery_expected_each = expected_battery_volume_m3 / 23
    battery_actual = [zone_volumes.get(name) for name in battery_names]
    battery_missing = [
        name for name, volume in zip(battery_names, battery_actual, strict=True) if volume is None
    ]
    battery_errors = {
        name: abs(float(volume) - battery_expected_each) / battery_expected_each
        for name, volume in zip(battery_names, battery_actual, strict=True)
        if volume is not None
    }
    battery_total = sum(float(volume) for volume in battery_actual if volume is not None)
    coolant_actual = zone_volumes.get("coolant")
    patch_areas = values["patch_areas_m2"]
    inlet_actual = patch_areas.get("inlet")
    outlet_actual = patch_areas.get("outlet")

    def relative(actual: float | None, expected: float) -> float | None:
        return None if actual is None else abs(actual - expected) / expected

    errors = {
        "battery_total": relative(battery_total, expected_battery_volume_m3),
        "coolant": relative(coolant_actual, expected_coolant_volume_m3),
        "inlet": relative(inlet_actual, expected_inlet_area_m2),
        "outlet": relative(outlet_actual, expected_outlet_area_m2),
    }
    checks = {
        "positive_cell_volumes": values["nonpositive_cell_volume_count"] == 0,
        "all_23_battery_zones_present": not battery_missing,
        "each_battery_volume_within_tolerance": (
            len(battery_errors) == 23
            and max(battery_errors.values(), default=math.inf) <= cell_volume_tolerance
        ),
        "battery_total_volume_within_tolerance": (
            errors["battery_total"] is not None
            and errors["battery_total"] <= total_volume_tolerance
        ),
        "coolant_volume_within_tolerance": (
            errors["coolant"] is not None and errors["coolant"] <= total_volume_tolerance
        ),
        "inlet_area_within_tolerance": (
            errors["inlet"] is not None and errors["inlet"] <= patch_area_tolerance
        ),
        "outlet_area_within_tolerance": (
            errors["outlet"] is not None and errors["outlet"] <= patch_area_tolerance
        ),
    }
    return {
        "schema_version": 1,
        "status": "MESH_INTEGRAL_VALIDATION_PASSED" if all(checks.values()) else "MESH_INTEGRAL_VALIDATION_FAILED",
        "checks": checks,
        "tolerances": {
            "total_volume_relative": total_volume_tolerance,
            "individual_battery_volume_relative": cell_volume_tolerance,
            "port_patch_area_relative": patch_area_tolerance,
        },
        "expected": {
            "battery_total_volume_m3": expected_battery_volume_m3,
            "battery_each_volume_m3": battery_expected_each,
            "coolant_volume_m3": expected_coolant_volume_m3,
            "inlet_area_m2": expected_inlet_area_m2,
            "outlet_area_m2": expected_outlet_area_m2,
        },
        "actual": {
            **values,
            "battery_total_volume_m3": battery_total,
        },
        "relative_errors": errors,
        "battery_zone_relative_errors": battery_errors,
        "missing_battery_zones": battery_missing,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Integrate an ASCII OpenFOAM polyMesh")
    parser.add_argument("--mesh-dir", required=True)
    parser.add_argument("--expected-battery-volume", required=True, type=float)
    parser.add_argument("--expected-coolant-volume", required=True, type=float)
    parser.add_argument("--expected-inlet-area", required=True, type=float)
    parser.add_argument("--expected-outlet-area", required=True, type=float)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        report = validate_mesh_integrals(
            args.mesh_dir,
            expected_battery_volume_m3=args.expected_battery_volume,
            expected_coolant_volume_m3=args.expected_coolant_volume,
            expected_inlet_area_m2=args.expected_inlet_area,
            expected_outlet_area_m2=args.expected_outlet_area,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        report = {
            "schema_version": 1,
            "status": "MESH_INTEGRAL_VALIDATION_FAILED",
            "mesh_dir": str(Path(args.mesh_dir).resolve()),
            "error": str(exc),
        }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "MESH_INTEGRAL_VALIDATION_PASSED" else 2


if __name__ == "__main__":
    sys.exit(main())
