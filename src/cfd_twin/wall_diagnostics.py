"""Preserve snappy layer fields and join them to y+ by face geometry, never ID.

Core refinement and region splitting renumber faces. A canonical polygon key
allows exact geometric correspondence and rejects missing/ambiguous matches.
The centroid is for display; it is not used for nearest-neighbour matching.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import shutil
import tarfile
import uuid
from pathlib import Path

try:
    from .openfoam_layercheck import _foam_labels, _face_zones, _scalar_internal_field
    from .openfoam_zone_validation import parse_cell_zones
    from .openfoam_mesh_integrals import _points, _faces, _boundary, _face_area
    from .openfoam_yplus_validation import parse_scalar_boundary_field, BATTERY_PATCH
except ImportError:
    from parse_layercheck import _foam_labels, _face_zones, _scalar_internal_field
    from validate_cell_zones import parse_cell_zones
    from openfoam_mesh_integrals import _points, _faces, _boundary, _face_area
    from validate_yplus import parse_scalar_boundary_field, BATTERY_PATCH


def polygon_key(vertices):
    # 0.1 nm quantisation tolerates ASCII round-off without merging neighbours.
    coordinates = sorted(tuple(round(v * 1e10) for v in xyz) for xyz in vertices)
    return hashlib.sha256(json.dumps(coordinates).encode()).hexdigest()


def write_gzip(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump(value, stream, separators=(",", ":"), allow_nan=False)


def read_gzip(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def summary(rows):
    total = sum(r["area_m2"] for r in rows)
    if not total:
        raise ValueError("Empty battery surface")
    bins = {"0": 0.0, "1-3": 0.0, "4-9": 0.0, "10+": 0.0}
    for row in rows:
        n = row["nSurfaceLayers"]
        key = "0" if n == 0 else "1-3" if n <= 3 else "4-9" if n < 10 else "10+"
        bins[key] += row["area_m2"]
    result = {
        "face_count": len(rows), "area_m2": total,
        "area_fraction_by_layer_count": {k: v / total for k, v in bins.items()},
        "area_weighted_layers": sum(r["area_m2"] * r["nSurfaceLayers"] for r in rows) / total,
    }
    if all("yPlus" in r for r in rows):
        bad = [r for r in rows if r["yPlus"] >= 1]
        bad_area = sum(r["area_m2"] for r in bad)
        result.update(
            area_fraction_yplus_below_1=1 - bad_area / total,
            high_yplus_area_m2=bad_area,
            fraction_of_high_yplus_area_without_layers=(
                sum(r["area_m2"] for r in bad if r["nSurfaceLayers"] == 0) / bad_area if bad_area else None
            ),
            area_fraction_high_yplus_by_layer_count={
                key: sum(r["area_m2"] for r in bad if (
                    "0" if r["nSurfaceLayers"] == 0 else "1-3" if r["nSurfaceLayers"] <= 3 else "4-9" if r["nSurfaceLayers"] < 10 else "10+"
                ) == key) / total for key in bins
            },
        )
    return result


def capture(case: Path):
    identity = case / "diagnostic_identity.json"
    if not identity.exists():
        # Supports source bundles generated before the identity sidecar was
        # introduced. A fresh UUID binds this capture to this case directory.
        identity.write_text(json.dumps({"attempt_id": uuid.uuid4().hex, "kind": "diagnostic_snapshot"}) + "\n")
    case_id = json.loads(identity.read_text())["attempt_id"]
    mesh = case / "constant/polyMesh"
    fields = case / "0"
    destination = case / "diagnostics/wall"
    destination.mkdir(parents=True, exist_ok=True)
    # Preserve raw fields together with the mesh they belong to before either
    # parsing or later topology changes. Uniform-zero thickness is not treated
    # as a valid local thickness measurement (v2512 internal-interface issue).
    with tarfile.open(destination / "snappy_layer_snapshot.tar.gz", "w:gz") as archive:
        archive.add(identity, arcname="diagnostic_identity.json")
        archive.add(mesh, arcname="constant/polyMesh")
        for name in ("nSurfaceLayers", "thickness", "thicknessFraction"):
            archive.add(fields / name, arcname="0/" + name)
        for name in ("snappyHexMeshDict", "blockMeshDict", "fvSchemes", "fvSolution"):
            archive.add(case / "system" / name, arcname="system/" + name)
        archive.add(case / "logs/production_mesh/snappyHexMesh.log", arcname="snappyHexMesh.log")
    owner = _foam_labels(mesh / "owner")
    neighbour = _foam_labels(mesh / "neighbour")
    n_cells = max(max(owner), max(neighbour)) + 1
    layers = _scalar_internal_field(fields / "nSurfaceLayers", n_cells)
    points, faces = _points(mesh / "points"), _faces(mesh / "faces")
    zones, interfaces = parse_cell_zones(mesh / "cellZones"), _face_zones(mesh / "faceZones")
    rows = []
    for index in range(1, 24):
        battery = f"C{index:02d}"
        solid = set(zones[f"battery_{battery}"])
        for face_id in dict.fromkeys(interfaces[f"battery_{battery}_interface"]):
            if face_id >= len(neighbour):
                raise ValueError(f"{battery}: interface face has no neighbour")
            a, b = owner[face_id], neighbour[face_id]
            if (a in solid) == (b in solid):
                raise ValueError(f"{battery}: ambiguous fluid/solid interface")
            cell = b if a in solid else a
            n = layers[cell]
            if not math.isfinite(n) or n < 0 or abs(n - round(n)) > 1e-6:
                raise ValueError(f"Invalid nSurfaceLayers {n}")
            vertices = [points[p] for p in faces[face_id]]
            rows.append({
                "battery": battery, "face_key": polygon_key(vertices),
                "pre_split_face": face_id, "pre_split_fluid_cell": cell,
                "xyz_m": [sum(p[d] for p in vertices) / len(vertices) for d in range(3)],
                "vertices_m": vertices, "area_m2": _face_area(points, faces[face_id]),
                "nSurfaceLayers": int(round(n)),
            })
    keys = {(r["battery"], r["face_key"]) for r in rows}
    if len(keys) != len(rows):
        raise ValueError("Duplicate interface polygons")
    payload = {"schema_version": 1, "case_id": case_id, "rows": rows,
               "thickness_note": "Raw thickness fields preserved; uniform zero is not a local thickness measurement."}
    write_gzip(destination / "layer_faces.json.gz", payload)
    report = {"case_id": case_id, "battery_surface": summary(rows),
              "per_battery": {f"C{i:02d}": summary([r for r in rows if r["battery"] == f"C{i:02d}"]) for i in range(1, 24)}}
    (destination / "layer_spatial_summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def join_rows(layer_rows, flow_rows):
    lookup = {(r["battery"], r["face_key"]): r for r in layer_rows}
    if len(lookup) != len(layer_rows):
        raise ValueError("Duplicate layer face key")
    result, seen = [], set()
    for row in flow_rows:
        key = (row["battery"], row["face_key"])
        if key not in lookup or key in seen:
            raise ValueError("Unmatched or duplicate flow polygon; refusing approximate mapping")
        original = lookup[key]
        if not math.isclose(original["area_m2"], row["area_m2"], rel_tol=1e-7, abs_tol=1e-16):
            raise ValueError("Matched polygon changed area")
        if not math.isfinite(row["yPlus"]) or row["yPlus"] < 0:
            raise ValueError("Invalid yPlus")
        result.append({**original, **row})
        seen.add(key)
    if seen != lookup.keys():
        raise ValueError("Some layer faces have no flow counterpart")
    return result


def compare(case: Path, iteration: str, *, diagnostic_only=True):
    source = case / "diagnostics/wall/layer_faces.json.gz"
    payload = read_gzip(source)
    identity = json.loads((case / "diagnostic_identity.json").read_text())["attempt_id"]
    if payload["case_id"] != identity:
        raise ValueError("Layer fields belong to another attempt")
    run = case / "runs/steady_cold_flow"
    mesh = run / "constant/coolant/polyMesh"
    points, faces = _points(mesh / "points"), _faces(mesh / "faces")
    field = parse_scalar_boundary_field(run / iteration / "coolant/yPlus")
    flow = []
    for patch, (start, count) in _boundary(mesh / "boundary").items():
        if not BATTERY_PATCH.fullmatch(patch):
            continue
        values = field[patch]
        if len(values) == 1:
            values = values * count
        if len(values) != count:
            raise ValueError("yPlus face count mismatch")
        battery = re.search(r"C\d{2}", patch).group()
        for face_id, value in zip(range(start, start + count), values, strict=True):
            vertices = [points[p] for p in faces[face_id]]
            flow.append({"battery": battery, "face_key": polygon_key(vertices),
                         "area_m2": _face_area(points, faces[face_id]), "yPlus": value,
                         "coolant_face": face_id})
    rows = join_rows(payload["rows"], flow)
    destination = case / "diagnostics/wall" / iteration
    write_gzip(destination / "layer_yplus_faces.json.gz", {"case_id": identity, "job_id": case.parent.name, "iteration": iteration, "rows": rows})
    yplus_source = run / iteration / "coolant/yPlus"
    if not yplus_source.exists():
        yplus_source = yplus_source.with_suffix(".gz")
    shutil.copy2(yplus_source, destination / yplus_source.name)
    report = {"case_id": identity, "job_id": case.parent.name, "iteration": iteration,
              "diagnostic_only": diagnostic_only, "mapping": "ALL_POLYGONS_MATCHED_ONE_TO_ONE",
              "battery_surface": summary(rows),
              "per_battery": {f"C{i:02d}": summary([r for r in rows if r["battery"] == f"C{i:02d}"]) for i in range(1, 24)}}
    (destination / "spatial_summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--iteration")
    args = parser.parse_args()
    result = compare(args.case, args.iteration) if args.iteration else capture(args.case)
    print(json.dumps({k: v for k, v in result.items() if k != "per_battery"}))
