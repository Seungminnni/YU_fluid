"""Inspect and register the user-supplied GAMBIT/Fluent reference mesh.

The reference ``.msh`` is Fluent ASCII, not Gmsh MSH.  This module performs
only structural inspection and reference registration; OpenFOAM conversion
and ``checkMesh`` remain separate, explicit stages.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import zipfile
from pathlib import Path


REFERENCE_FILENAMES = (
    "20260831_1633.msh",
    "20260831_1633.jou",
    "20260831_1633.trn",
    "20260831_1633.dbs",
)
REFERENCE_MESH_SHA256 = (
    "ccb2f7e7002e9c708f9c2d5767c186966421cfef07e23d9916da72537e5d4e8c"
)
DEFAULT_REFERENCE_CONTRACT = {
    "mesh_sha256": REFERENCE_MESH_SHA256,
    "nodes": 178_660,
    "faces": 511_818,
    "cells": 166_704,
    "solid_cells": 69_920,
    "fluid_cells": 96_784,
    "solid_components": 23,
    "fluid_components": 1,
    "interface_faces_in_mixed_wall_zone": 21_712,
    "external_faces_in_mixed_wall_zone": 12_132,
    "inlet_faces": 152,
    "outlet_faces": 152,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_gambit_sweep_journal(journal_path: str | Path) -> dict[str, object]:
    """Extract the effective structured-sweep recipe from a GAMBIT journal.

    GAMBIT journals contain continuation lines and may retain commands marked
    ``/Undone``.  The latter describe abandoned edits and must not be learned
    as part of the accepted meshing workflow.
    """

    path = Path(journal_path).resolve()
    logical_lines: list[str] = []
    pending = ""
    for raw in path.read_text(encoding="ascii", errors="ignore").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical_lines.append(pending)
        pending = ""
    if pending:
        logical_lines.append(pending)

    effective = [line for line in logical_lines if "/Undone" not in line]
    split_lines = [line for line in effective if re.search(r"\bvolume\s+split\b", line, re.I)]
    face_lines = [line for line in effective if re.search(r"\bface\s+mesh\b", line, re.I)]
    edge_lines = [line for line in effective if re.search(r"\bedge\s+mesh\b", line, re.I)]
    cooper_lines = [
        line
        for line in effective
        if re.search(r"\bvolume\s+mesh\b.*\bcooper\b", line, re.I)
    ]
    interval_values: list[int] = []
    for line in edge_lines:
        match = re.search(r"\bintervals\s+(\d+)\b", line, re.I)
        if match:
            interval_values.append(int(match.group(1)))
    source_faces = sorted(
        {
            match.group(1)
            for line in cooper_lines
            if (match := re.search(r'\bsource\s+"([^"]+)"', line, re.I))
        }
    )
    methods = sorted(
        {
            match.group(1).lower()
            for line in face_lines
            if (match := re.search(r"\b(pave|map|submap)\b", line, re.I))
        }
    )
    checks = {
        "volume_partitioning_present": bool(split_lines),
        "source_face_meshing_present": bool(face_lines),
        "axial_edge_intervals_present": bool(interval_values),
        "cooper_sweep_present": bool(cooper_lines),
    }
    histogram = {
        str(value): interval_values.count(value) for value in sorted(set(interval_values))
    }
    return {
        "schema_version": 1,
        "status": (
            "GAMBIT_SWEEP_WORKFLOW_CONFIRMED"
            if all(checks.values())
            else "GAMBIT_SWEEP_WORKFLOW_INCOMPLETE"
        ),
        "source": str(path),
        "checks": checks,
        "effective_command_counts": {
            "volume_split": len(split_lines),
            "face_mesh": len(face_lines),
            "edge_mesh": len(edge_lines),
            "volume_mesh_cooper": len(cooper_lines),
        },
        "source_face_methods": methods,
        "cooper_source_faces": source_faces,
        "axial_interval_histogram": histogram,
        "axial_interval_minimum": min(interval_values) if interval_values else None,
        "axial_interval_maximum": max(interval_values) if interval_values else None,
        "ignored_undone_commands": len(logical_lines) - len(effective),
        "learned_recipe": [
            "partition the 3D domain into sweepable volumes before meshing",
            "create a quadrilateral mesh on each source/top face",
            "set explicit interval counts on axial/vertical edges",
            "sweep the source connectivity through each volume with Cooper",
            "retain conformal shared faces between fluid and battery regions",
        ],
    }


def _header_tokens(line: str) -> list[str]:
    return re.findall(r"[0-9A-Fa-f]+", line)


def _cell_zone(cell_id: int, ranges: list[dict[str, object]]) -> int | None:
    for item in ranges:
        if int(item["first"]) <= cell_id <= int(item["last"]):
            return int(item["zone_id"])
    return None


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size + 1))
        self.rank = bytearray(size + 1)

    def find(self, value: int) -> int:
        parent = self.parent
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1


def _triangle_area_centroid(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
) -> tuple[float, tuple[float, float, float]]:
    ab = tuple(b[index] - a[index] for index in range(3))
    ac = tuple(c[index] - a[index] for index in range(3))
    cross = (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )
    area = 0.5 * math.sqrt(sum(value * value for value in cross))
    return area, tuple((a[index] + b[index] + c[index]) / 3.0 for index in range(3))


def _quad_area_centroid(
    points: list[tuple[float, float, float]],
) -> tuple[float, tuple[float, float, float]]:
    first = _triangle_area_centroid(points[0], points[1], points[2])
    second = _triangle_area_centroid(points[0], points[2], points[3])
    area = first[0] + second[0]
    if area <= 0:
        return 0.0, (math.nan, math.nan, math.nan)
    centroid = tuple(
        (first[0] * first[1][index] + second[0] * second[1][index]) / area
        for index in range(3)
    )
    return area, centroid


def inspect_fluent_ascii_mesh(
    mesh_path: str | Path,
    *,
    contract: dict[str, object] | None = None,
) -> dict[str, object]:
    """Inspect a Fluent ASCII mesh without treating it as a Gmsh file."""

    path = Path(mesh_path).resolve()
    expected = dict(DEFAULT_REFERENCE_CONTRACT if contract is None else contract)
    digest = _sha256(path)
    nodes: list[tuple[float, float, float] | None] = [None]
    node_total = face_total = cell_total = 0
    node_data_remaining = 0
    current_face_zone: int | None = None
    face_data_remaining = 0
    cell_ranges: list[dict[str, object]] = []
    face_ranges: dict[int, dict[str, object]] = {}
    zone_names: dict[int, dict[str, str]] = {}
    # Fluent permits the cell declarations to appear after the face data.
    # Pre-scan only the lightweight headers so adjacency can be accumulated
    # while streaming the much larger face section on the second pass.
    with path.open("r", encoding="ascii", errors="strict") as stream:
        for raw in stream:
            line = raw.strip()
            if line.startswith("(12"):
                tokens = _header_tokens(line)
                if len(tokens) < 5:
                    raise ValueError(f"invalid Fluent cell header: {line}")
                zone_id, first, last = (int(tokens[index], 16) for index in (1, 2, 3))
                if zone_id == 0:
                    cell_total = last - first + 1
                else:
                    cell_ranges.append(
                        {
                            "zone_id": zone_id,
                            "first": first,
                            "last": last,
                            "count": last - first + 1,
                            "cell_type": int(tokens[4], 16),
                            "element_type": int(tokens[5], 16) if len(tokens) > 5 else None,
                        }
                    )
            elif line.startswith("(45"):
                match = re.match(
                    r"\(45\s*\(\s*([0-9A-Fa-f]+)\s+([^\s()]+)\s+([^\s()]+)\s*\)",
                    line,
                )
                if match:
                    zone_names[int(match.group(1), 16)] = {
                        "type": match.group(2),
                        "name": match.group(3),
                    }
    if not cell_total or not cell_ranges:
        raise ValueError("missing Fluent cell declaration")
    face_incidence: list[int] | None = [0] * (cell_total + 1)
    connectivity: _UnionFind | None = _UnionFind(cell_total)
    parsed_faces = quad_faces = 0
    mixed_material_faces_by_zone: dict[int, int] = {}
    external_faces_by_zone: dict[int, int] = {}
    area_by_zone: dict[int, float] = {}
    centroid_moment_by_zone: dict[int, list[float]] = {}

    with path.open("r", encoding="ascii", errors="strict") as stream:
        for raw in stream:
            line = raw.strip()
            if not line:
                continue
            if node_data_remaining:
                values = line.replace("(", " ").replace(")", " ").split()
                if len(values) >= 3:
                    nodes.append(tuple(float(values[index]) for index in range(3)))
                    node_data_remaining -= 1
                continue
            if face_data_remaining:
                values = line.replace("(", " ").replace(")", " ").split()
                if not values:
                    continue
                vertex_count = int(values[0], 10)
                needed = 1 + vertex_count + 2
                if len(values) < needed:
                    raise ValueError(f"truncated Fluent face row: {line[:120]}")
                node_ids = [int(value, 16) for value in values[1 : 1 + vertex_count]]
                cells = [int(value, 16) for value in values[1 + vertex_count : needed]]
                parsed_faces += 1
                quad_faces += int(vertex_count == 4)
                for cell_id in cells:
                    if cell_id:
                        if face_incidence is None:
                            raise ValueError("face data appeared before the cell declaration")
                        face_incidence[cell_id] += 1
                left_zone = _cell_zone(cells[0], cell_ranges) if cells[0] else None
                right_zone = _cell_zone(cells[1], cell_ranges) if cells[1] else None
                if cells[0] and cells[1] and left_zone == right_zone:
                    if connectivity is None:
                        raise ValueError("face data appeared before the cell declaration")
                    connectivity.union(cells[0], cells[1])
                if cells[0] and cells[1] and left_zone != right_zone:
                    mixed_material_faces_by_zone[current_face_zone] = (
                        mixed_material_faces_by_zone.get(current_face_zone, 0) + 1
                    )
                if bool(cells[0]) != bool(cells[1]):
                    external_faces_by_zone[current_face_zone] = (
                        external_faces_by_zone.get(current_face_zone, 0) + 1
                    )
                if vertex_count == 4:
                    coordinates = [nodes[node_id] for node_id in node_ids]
                    if any(point is None for point in coordinates):
                        raise ValueError("face references a missing node")
                    area, centroid = _quad_area_centroid(coordinates)  # type: ignore[arg-type]
                    area_by_zone[current_face_zone] = area_by_zone.get(current_face_zone, 0.0) + area
                    moment = centroid_moment_by_zone.setdefault(current_face_zone, [0.0, 0.0, 0.0])
                    for index in range(3):
                        moment[index] += area * centroid[index]
                face_data_remaining -= 1
                continue

            if line.startswith("(10"):
                tokens = _header_tokens(line)
                if len(tokens) < 6:
                    raise ValueError(f"invalid Fluent node header: {line}")
                zone_id, first, last = (int(tokens[index], 16) for index in (1, 2, 3))
                if zone_id == 0:
                    node_total = last - first + 1
                elif line.endswith("("):
                    node_data_remaining = last - first + 1
                continue
            if line.startswith("(12"):
                continue
            if line.startswith("(13"):
                tokens = _header_tokens(line)
                if len(tokens) < 5:
                    raise ValueError(f"invalid Fluent face header: {line}")
                zone_id, first, last = (int(tokens[index], 16) for index in (1, 2, 3))
                if zone_id == 0:
                    face_total = last - first + 1
                else:
                    current_face_zone = zone_id
                    face_data_remaining = last - first + 1
                    face_ranges[zone_id] = {
                        "zone_id": zone_id,
                        "first": first,
                        "last": last,
                        "count": face_data_remaining,
                        "boundary_condition_type": int(tokens[4], 16),
                        "face_type": int(tokens[5], 16) if len(tokens) > 5 else None,
                    }
                continue
            if line.startswith("(45"):
                match = re.match(
                    r"\(45\s*\(\s*([0-9A-Fa-f]+)\s+([^\s()]+)\s+([^\s()]+)\s*\)",
                    line,
                )
                if match:
                    zone_names[int(match.group(1), 16)] = {
                        "type": match.group(2),
                        "name": match.group(3),
                    }

    if node_data_remaining or face_data_remaining:
        raise ValueError("Fluent mesh ended inside a data section")
    if len(nodes) - 1 != node_total:
        raise ValueError(f"node declaration/data mismatch: {node_total} vs {len(nodes) - 1}")
    if parsed_faces != face_total:
        raise ValueError(f"face declaration/data mismatch: {face_total} vs {parsed_faces}")
    if face_incidence is None or connectivity is None:
        raise ValueError("missing Fluent cell declaration")

    cell_zone_by_id = {int(item["zone_id"]): item for item in cell_ranges}
    named_cell_zones = {
        zone_names[zone_id]["name"]: {**item, **zone_names[zone_id]}
        for zone_id, item in cell_zone_by_id.items()
        if zone_id in zone_names
    }
    named_face_zones = {
        zone_names[zone_id]["name"]: {
            **item,
            **zone_names[zone_id],
            "mixed_material_faces": mixed_material_faces_by_zone.get(zone_id, 0),
            "external_faces": external_faces_by_zone.get(zone_id, 0),
            "area_source_units_squared": area_by_zone.get(zone_id, 0.0),
            "area_weighted_centroid_source_units": [
                value / area_by_zone[zone_id]
                for value in centroid_moment_by_zone.get(zone_id, [0.0, 0.0, 0.0])
            ] if area_by_zone.get(zone_id, 0.0) else None,
        }
        for zone_id, item in face_ranges.items()
        if zone_id in zone_names
    }

    component_counts: dict[str, int] = {}
    component_sizes: dict[str, list[int]] = {}
    for name, item in named_cell_zones.items():
        first, last = int(item["first"]), int(item["last"])
        sizes: dict[int, int] = {}
        for cell_id in range(first, last + 1):
            root = connectivity.find(cell_id)
            sizes[root] = sizes.get(root, 0) + 1
        component_counts[name] = len(sizes)
        component_sizes[name] = sorted(sizes.values())

    coordinates = [point for point in nodes[1:] if point is not None]
    bounds = {
        "minimum_source_units": [min(point[index] for point in coordinates) for index in range(3)],
        "maximum_source_units": [max(point[index] for point in coordinates) for index in range(3)],
    }
    battery = named_cell_zones.get("battery", {})
    fluid = named_cell_zones.get("fluid", {})
    wall = named_face_zones.get("wall", {})
    inlet = named_face_zones.get("inlet", {})
    outlet = named_face_zones.get("outlet", {})
    observed = {
        "mesh_sha256": digest,
        "nodes": node_total,
        "faces": face_total,
        "cells": cell_total,
        "solid_cells": int(battery.get("count", 0)),
        "fluid_cells": int(fluid.get("count", 0)),
        "solid_components": component_counts.get("battery", 0),
        "fluid_components": component_counts.get("fluid", 0),
        "interface_faces_in_mixed_wall_zone": int(wall.get("mixed_material_faces", 0)),
        "external_faces_in_mixed_wall_zone": int(wall.get("external_faces", 0)),
        "inlet_faces": int(inlet.get("count", 0)),
        "outlet_faces": int(outlet.get("count", 0)),
    }
    checks = {
        name: observed.get(name) == value
        for name, value in expected.items()
        if name in observed
    }
    checks.update(
        all_faces_are_quadrilateral=quad_faces == face_total,
        all_cells_have_six_faces=all(value == 6 for value in face_incidence[1:]),
        all_cells_are_hexahedral=bool(cell_ranges) and all(
            item.get("element_type") == 4 for item in cell_ranges
        ),
        material_cell_counts_sum_to_total=(
            int(battery.get("count", 0)) + int(fluid.get("count", 0)) == cell_total
        ),
    )
    return {
        "schema_version": 1,
        "status": (
            "REFERENCE_MESH_VALIDATION_PASSED"
            if checks and all(checks.values())
            else "REFERENCE_MESH_VALIDATION_FAILED"
        ),
        "format": "Fluent ASCII generated by GAMBIT; not Gmsh MSH",
        "source": str(path),
        "units": {
            "status": "MM_CONSISTENT_WITH_19MM_CELLS_BUT_NOT_INFERRED_FROM_FORMAT",
            "openfoam_scale_candidate": 0.001,
            "double_scaling_must_be_prevented": True,
        },
        "contract": expected,
        "observed": observed,
        "checks": checks,
        "bounds": bounds,
        "cell_zones": named_cell_zones,
        "face_zones": named_face_zones,
        "component_counts": component_counts,
        "component_sizes": component_sizes,
        "cell_face_incidence": {
            "minimum": min(face_incidence[1:]),
            "maximum": max(face_incidence[1:]),
        },
    }


def register_reference_archive(
    archive_path: str | Path,
    output_directory: str | Path,
) -> dict[str, object]:
    """Materialize the four-file reference archive and write its audit report."""

    archive = Path(archive_path).resolve()
    output = Path(output_directory).resolve()
    source_dir = output / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        members = [item for item in bundle.infolist() if not item.is_dir()]
        by_name = {Path(item.filename).name: item for item in members}
        missing = sorted(set(REFERENCE_FILENAMES) - set(by_name))
        unexpected = sorted(set(by_name) - set(REFERENCE_FILENAMES))
        if missing or unexpected or len(members) != len(REFERENCE_FILENAMES):
            raise ValueError(
                f"reference archive members differ: missing={missing}, unexpected={unexpected}"
            )
        for name in REFERENCE_FILENAMES:
            destination = source_dir / name
            with bundle.open(by_name[name]) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
    report = inspect_fluent_ascii_mesh(source_dir / "20260831_1633.msh")
    report["sweep_workflow"] = inspect_gambit_sweep_journal(
        source_dir / "20260831_1633.jou"
    )
    provenance = {
        "schema_version": 1,
        "status": report["status"],
        "archive": str(archive),
        "archive_sha256": _sha256(archive),
        "files": {
            name: {
                "path": str(source_dir / name),
                "bytes": (source_dir / name).stat().st_size,
                "sha256": _sha256(source_dir / name),
            }
            for name in REFERENCE_FILENAMES
        },
        "mesh_report": str(output / "reference_mesh_report.json"),
        "use": "immutable structural baseline; not a substitute for OpenFOAM checkMesh or FOM validation",
    }
    (output / "reference_mesh_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "reference_mesh_provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"report": report, "provenance": provenance}
