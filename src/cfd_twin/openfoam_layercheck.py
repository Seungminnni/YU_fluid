from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from .openfoam_zone_validation import parse_cell_zones
except ImportError:  # Standalone copy inside a generated OpenFOAM case.
    try:
        from openfoam_zone_validation import parse_cell_zones
    except ImportError:
        from validate_cell_zones import parse_cell_zones


BATTERY_IDS = tuple(f"C{index:02d}" for index in range(1, 24))
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
ROW = re.compile(
    rf"^\s*(\S*?battery_(C\d{{2}})\S*)\s+(\d+)\s+(\d+)\s+({NUMBER})\s+({NUMBER})\s+({NUMBER})\s*$"
)


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*?$", "", text, flags=re.MULTILINE)


def _ascii(path: Path) -> str:
    raw = path.read_bytes()
    if b"format      binary" in raw or b"format binary" in raw:
        raise ValueError(f"{path}: binary OpenFOAM file unsupported")
    return _strip_comments(raw.decode("utf-8"))


def _named_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    pattern = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\{")
    for match in pattern.finditer(text):
        if match.group(1) == "FoamFile":
            continue
        depth = 1
        cursor = match.end()
        while cursor < len(text) and depth:
            if text[cursor] == "{":
                depth += 1
            elif text[cursor] == "}":
                depth -= 1
            cursor += 1
        if depth == 0:
            blocks[match.group(1)] = text[match.end() : cursor - 1]
    return blocks


def _foam_labels(path: Path) -> list[int]:
    text = _ascii(path)
    header_end = text.find("}")
    match = re.search(r"(\d+)\s*\((.*?)\)\s*;?\s*$", text[header_end + 1 :], re.DOTALL)
    if not match:
        raise ValueError(f"{path}: label list not found")
    declared = int(match.group(1))
    values = [int(value) for value in re.findall(r"[-+]?\d+", match.group(2))]
    if len(values) != declared:
        raise ValueError(f"{path}: declared {declared} labels but parsed {len(values)}")
    return values


def _face_zones(path: Path) -> dict[str, list[int]]:
    zones: dict[str, list[int]] = {}
    for name, block in _named_blocks(_ascii(path)).items():
        if not re.search(r"\btype\s+faceZone\s*;", block):
            continue
        match = re.search(
            r"\bfaceLabels\b\s*(?:List<label>\s*)?(\d+)\s*\((.*?)\)\s*;?",
            block,
            re.DOTALL,
        )
        if not match:
            raise ValueError(f"{name}: faceLabels not found")
        declared = int(match.group(1))
        labels = [int(value) for value in re.findall(r"[-+]?\d+", match.group(2))]
        if len(labels) != declared:
            raise ValueError(
                f"{name}: declared {declared} face labels but parsed {len(labels)}"
            )
        zones[name] = labels
    return zones


def _scalar_internal_field(path: Path, n_cells: int) -> list[float]:
    text = _ascii(path)
    uniform = re.search(rf"\binternalField\s+uniform\s+({NUMBER})\s*;", text)
    if uniform:
        return [float(uniform.group(1))] * n_cells
    nonuniform = re.search(
        rf"\binternalField\s+nonuniform\s+List<scalar>\s+(\d+)\s*\((.*?)\)\s*;",
        text,
        re.DOTALL,
    )
    if not nonuniform:
        raise ValueError(f"{path}: scalar internalField not found")
    declared = int(nonuniform.group(1))
    values = [float(value) for value in re.findall(NUMBER, nonuniform.group(2))]
    if declared != n_cells or len(values) != declared:
        raise ValueError(
            f"{path}: expected {n_cells} cell values, declared {declared}, parsed {len(values)}"
        )
    return values


def _mesh_layer_rows(
    mesh_dir: Path, field_dir: Path, expected_target_layers: int
) -> list[dict[str, object]]:
    owner = _foam_labels(mesh_dir / "owner")
    neighbour = _foam_labels(mesh_dir / "neighbour")
    n_cells = max((*owner, *neighbour), default=-1) + 1
    cell_zones = parse_cell_zones(mesh_dir / "cellZones")
    face_zones = _face_zones(mesh_dir / "faceZones")
    layers = _scalar_internal_field(field_dir / "nSurfaceLayers", n_cells)
    thickness = _scalar_internal_field(field_dir / "thickness", n_cells)
    thickness_fraction = _scalar_internal_field(
        field_dir / "thicknessFraction", n_cells
    )

    rows: list[dict[str, object]] = []
    for battery_id in BATTERY_IDS:
        cell_zone_name = f"battery_{battery_id}"
        face_zone_name = f"{cell_zone_name}_interface"
        battery_cells = set(cell_zones.get(cell_zone_name, []))
        interface_faces = list(dict.fromkeys(face_zones.get(face_zone_name, [])))
        if not battery_cells or not interface_faces:
            continue
        adjacent_fluid_cells: list[int | None] = []
        ambiguous = 0
        for face in interface_faces:
            if face >= len(neighbour):
                ambiguous += 1
                adjacent_fluid_cells.append(None)
                continue
            own = owner[face]
            nei = neighbour[face]
            own_is_battery = own in battery_cells
            nei_is_battery = nei in battery_cells
            if own_is_battery == nei_is_battery:
                ambiguous += 1
                adjacent_fluid_cells.append(None)
                continue
            adjacent_fluid_cells.append(nei if own_is_battery else own)
        # Count malformed/non-separating faces as uncovered instead of hiding
        # them from the denominator.  The later interface and checkMesh gates
        # report their exact topology diagnostics.
        layer_values = [layers[cell] if cell is not None else 0.0 for cell in adjacent_fluid_cells]
        thickness_values = [
            thickness[cell] if cell is not None else 0.0 for cell in adjacent_fluid_cells
        ]
        fraction_values = [
            thickness_fraction[cell] if cell is not None else 0.0
            for cell in adjacent_fluid_cells
        ]
        faces = len(adjacent_fluid_cells)
        average_layers = sum(layer_values) / faces if faces else 0.0
        rows.append(
            {
                "patch": face_zone_name,
                "battery_id": battery_id,
                "faces": faces,
                "target_layers": expected_target_layers,
                "average_layers": average_layers,
                "average_thickness_m": (
                    sum(thickness_values) / faces if faces else 0.0
                ),
                "average_thickness_percent": (
                    100 * sum(fraction_values) / faces if faces else 0.0
                ),
                "surface_coverage_fraction": (
                    sum(value > 0 for value in layer_values) / faces if faces else 0.0
                ),
                "ambiguous_interface_face_count": ambiguous,
                "source": "reconstructed_v2512_layer_fields",
            }
        )
    return rows


def parse_layer_log(
    path: str | Path,
    *,
    expected_target_layers: int = 5,
    minimum_layer_coverage: float = 0.8,
    minimum_thickness_fraction: float = 0.5,
    mesh_dir: str | Path | None = None,
    field_dir: str | Path | None = None,
) -> dict[str, object]:
    source = Path(path).resolve()
    text = source.read_text(encoding="utf-8", errors="replace")
    log_rows: list[dict[str, object]] = []
    for line in text.splitlines():
        match = ROW.match(line)
        if not match:
            continue
        patch_name, battery_id, faces, target, actual, thickness, percent = (
            match.groups()
        )
        log_rows.append(
            {
                "patch": patch_name,
                "battery_id": battery_id,
                "faces": int(faces),
                "target_layers": int(target),
                "average_layers": float(actual),
                "average_thickness_m": float(thickness),
                "average_thickness_percent": float(percent),
            }
        )

    rows = log_rows
    validation_source = "legacy_log_rows"
    if mesh_dir is not None or field_dir is not None:
        if mesh_dir is None or field_dir is None:
            raise ValueError("mesh_dir and field_dir must be supplied together")
        rows = _mesh_layer_rows(
            Path(mesh_dir).resolve(),
            Path(field_dir).resolve(),
            expected_target_layers,
        )
        # v2512 writes nSurfaceLayers as a useful cell field, but its
        # thickness/thicknessFraction internal fields remain uniform zero for
        # internal faceZone layers.  The final snappy layer table is therefore
        # the authoritative thickness source; combine it with the independently
        # reconstructed coolant-side coverage instead of silently reporting 0.
        latest_log_row = {str(row["battery_id"]): row for row in log_rows}
        for row in rows:
            logged = latest_log_row.get(str(row["battery_id"]))
            if logged is None:
                continue
            row["average_thickness_m"] = logged["average_thickness_m"]
            row["average_thickness_percent"] = logged[
                "average_thickness_percent"
            ]
            row["thickness_source"] = "snappy_final_layer_table"
        validation_source = "reconstructed_v2512_layer_fields_plus_log_thickness"

    summaries: dict[str, dict[str, object]] = {}
    for battery_id in BATTERY_IDS:
        selected = [row for row in rows if row["battery_id"] == battery_id]
        faces = sum(int(row["faces"]) for row in selected)
        ideal_layers = sum(
            int(row["faces"]) * int(row["target_layers"]) for row in selected
        )
        actual_layers = sum(
            int(row["faces"]) * float(row["average_layers"]) for row in selected
        )
        weighted_thickness = sum(
            int(row["faces"]) * float(row["average_thickness_percent"])
            for row in selected
        )
        surface_coverage = sum(
            int(row["faces"])
            * float(
                row.get(
                    "surface_coverage_fraction",
                    min(
                        float(row["average_layers"]) / int(row["target_layers"]),
                        1.0,
                    ),
                )
            )
            for row in selected
        )
        completion_fraction = actual_layers / ideal_layers if ideal_layers > 0 else 0.0
        surface_coverage_fraction = surface_coverage / faces if faces > 0 else 0.0
        layer_coverage = (
            surface_coverage_fraction
            if validation_source.startswith("reconstructed_v2512")
            else min(surface_coverage_fraction, completion_fraction)
        )
        summaries[battery_id] = {
            "patches": [str(row["patch"]) for row in selected],
            "face_count": faces,
            "minimum_target_layers": min(
                (int(row["target_layers"]) for row in selected), default=0
            ),
            "surface_coverage_fraction": surface_coverage_fraction,
            "layer_completion_fraction": completion_fraction,
            "layer_coverage_fraction": layer_coverage,
            "wanted_thickness_fraction": (
                weighted_thickness / (100 * faces) if faces > 0 else 0.0
            ),
        }

    missing = [name for name in BATTERY_IDS if not summaries[name]["patches"]]
    nonpositive_faces = [
        name for name in BATTERY_IDS if int(summaries[name]["face_count"]) <= 0
    ]
    below_target = [
        name
        for name in BATTERY_IDS
        if int(summaries[name]["minimum_target_layers"]) < expected_target_layers
    ]
    low_coverage = [
        name
        for name in BATTERY_IDS
        if float(summaries[name]["layer_coverage_fraction"])
        < minimum_layer_coverage
    ]
    low_thickness = [
        name
        for name in BATTERY_IDS
        if float(summaries[name]["wanted_thickness_fraction"])
        < minimum_thickness_fraction
    ]
    fatal = any(
        re.search(r"FOAM FATAL|segmentation fault", line, re.I)
        or (
            re.search(r"floating point exception", line, re.I)
            and not re.search(r"trapping enabled", line, re.I)
        )
        for line in text.splitlines()
    )
    no_layers = "No layers to generate" in text
    layer_faces_matches = re.findall(
        r"Writing\s+(\d+)\s+faces inside added layer to faceSet layerFaces", text
    )
    actual_layer_face_count = (
        int(layer_faces_matches[-1]) if layer_faces_matches else None
    )
    illegal_matches = re.findall(
        r"Finished meshing with\s+(\d+)\s+illegal faces", text
    )
    if illegal_matches:
        final_illegal_face_count = int(illegal_matches[-1])
    elif "Finished meshing without any errors" in text:
        final_illegal_face_count = 0
    else:
        final_illegal_face_count = None
    checks = {
        "all_23_battery_layer_rows_present": not missing,
        "all_battery_layer_face_counts_positive": not nonpositive_faces,
        "target_layer_count_is_at_least_expected": not below_target,
        "per_battery_layer_coverage_passes": not low_coverage,
        "per_battery_thickness_fraction_passes": not low_thickness,
        "no_fatal_diagnostic": not fatal,
        "layer_generation_was_attempted": not no_layers,
    }
    if validation_source.startswith("reconstructed_v2512"):
        checks["actual_layer_faces_positive"] = (
            actual_layer_face_count is not None and actual_layer_face_count > 0
        )
    return {
        "schema_version": 1,
        "status": "LAYER_VALIDATION_PASSED" if all(checks.values()) else "LAYER_VALIDATION_FAILED",
        "log": str(source),
        "checks": checks,
        "thresholds": {
            "expected_target_layers": expected_target_layers,
            "minimum_layer_coverage_fraction": minimum_layer_coverage,
            "minimum_wanted_thickness_fraction": minimum_thickness_fraction,
        },
        "validation_source": validation_source,
        "battery_summaries": summaries,
        "parsed_rows": rows,
        "missing_batteries": missing,
        "nonpositive_face_batteries": nonpositive_faces,
        "below_target_layer_batteries": below_target,
        "low_coverage_batteries": low_coverage,
        "low_thickness_batteries": low_thickness,
        "fatal_diagnostic_found": fatal,
        "no_layers_message_found": no_layers,
        "actual_layer_face_count": actual_layer_face_count,
        "snappy_final_illegal_face_count": final_illegal_face_count,
        "snappy_final_illegal_faces_role": (
            "PRE_SPLIT_DIAGNOSTIC_ONLY; final quality is gated after "
            "splitMeshRegions on coolant and batterySolid"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse OpenFOAM v2512 snappyHexMesh layer statistics"
    )
    parser.add_argument("--log", required=True)
    parser.add_argument("--output")
    parser.add_argument("--expected-target-layers", type=int, default=5)
    parser.add_argument("--minimum-layer-coverage", type=float, default=0.8)
    parser.add_argument("--minimum-thickness-fraction", type=float, default=0.5)
    parser.add_argument("--mesh-dir")
    parser.add_argument("--field-dir")
    args = parser.parse_args(argv)
    try:
        report = parse_layer_log(
            args.log,
            expected_target_layers=args.expected_target_layers,
            minimum_layer_coverage=args.minimum_layer_coverage,
            minimum_thickness_fraction=args.minimum_thickness_fraction,
            mesh_dir=args.mesh_dir,
            field_dir=args.field_dir,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        report = {
            "schema_version": 1,
            "status": "LAYER_VALIDATION_FAILED",
            "error": str(exc),
        }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "LAYER_VALIDATION_PASSED" else 2


if __name__ == "__main__":
    sys.exit(main())
