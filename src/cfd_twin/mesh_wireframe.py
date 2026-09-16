"""실제 OpenFOAM ``polyMesh`` 경계 격자를 웹용 3D 선분으로 축약한다.

웹 뷰어가 임의의 격자 무늬를 CAD 위에 그리지 않도록, 계산에 사용한
``constant/<region>/polyMesh``의 점·면·경계 patch를 직접 읽는다. 전체
849k 체적격자를 브라우저로 복사하는 대신 배터리 계면, 외벽, 입·출구의
실제 경계 edge를 patch별 고정 상한으로 균등 표본화한다.
"""

from __future__ import annotations

import math
import base64
import gzip
import struct
import sys
from array import array
from pathlib import Path
from typing import Callable

from .openfoam_mesh_integrals import _boundary, _faces, _points


def extract_complete_volume_edges(case_dir: str | Path) -> dict[str, object]:
    """Pack every unique 3D edge of both actual region polyMeshes, unsampled.

    Data is split from the ordinary viewer asset so that millions of lines are
    transferred/decompressed only when the user explicitly selects this mode.
    It shows the *entire* volume topology, unlike a surface wireframe or cut.
    """

    case = Path(case_dir).resolve()
    payload: dict[str, str] = {}
    counts: dict[str, dict[str, int]] = {}
    for region in ("coolant", "batterySolid"):
        mesh_dir = case / "constant" / region / "polyMesh"
        points = _points(mesh_dir / "points")
        faces = _faces(mesh_dir / "faces")
        keys: set[int] = set()
        for face in faces:
            last = face[-1]
            for current in face:
                first, second = (last, current) if last < current else (current, last)
                keys.add((first << 32) | second)
                last = current
        coordinates = array("f", (1000.0 * value for point in points for value in point))
        endpoints = array("I", (index for key in sorted(keys) for index in (key >> 32, key & 0xFFFFFFFF)))
        if sys.byteorder != "little":
            coordinates.byteswap()
            endpoints.byteswap()
        packed = struct.pack("<III", len(points), len(keys), len(faces)) + coordinates.tobytes() + endpoints.tobytes()
        payload[region] = base64.b64encode(gzip.compress(packed, compresslevel=9)).decode("ascii")
        counts[region] = {"points": len(points), "edges": len(keys), "faces": len(faces)}
    return {"source": "all unique edges of actual coolant and batterySolid polyMesh faces",
            "sampling": "none", "length_unit": "mm", "counts": counts, "payload": payload}


def _sample_patch_edges(
    faces: list[list[int]], start: int, count: int, maximum_edges: int
) -> list[tuple[int, int]]:
    """patch 전체에서 순서가 재현 가능한 실제 edge 표본을 반환한다."""

    if count <= 0 or maximum_edges <= 0:
        return []
    estimated_edges_per_face = 4
    face_budget = max(1, maximum_edges // estimated_edges_per_face)
    stride = max(1, math.ceil(count / face_budget))
    edges: set[tuple[int, int]] = set()
    stop = min(len(faces), start + count)
    for face_index in range(start, stop, stride):
        face = faces[face_index]
        for first, second in zip(face, face[1:] + face[:1], strict=True):
            edges.add((first, second) if first < second else (second, first))
    ordered = sorted(edges)
    if len(ordered) <= maximum_edges:
        return ordered
    edge_stride = len(ordered) / maximum_edges
    return [ordered[min(len(ordered) - 1, int(index * edge_stride))] for index in range(maximum_edges)]


def _append_region_groups(
    mesh_dir: Path,
    *,
    patch_selector: Callable[[str], str | None],
    point_index: dict[tuple[float, float, float], int],
    packed_points: list[list[float]],
    groups: list[dict[str, object]],
    maximum_edges_per_patch: int,
) -> None:
    points = _points(mesh_dir / "points")
    faces = _faces(mesh_dir / "faces")
    patches = _boundary(mesh_dir / "boundary")
    for patch_name, (start, count) in patches.items():
        kind = patch_selector(patch_name)
        if kind is None:
            continue
        source_edges = _sample_patch_edges(
            faces, start, count, maximum_edges_per_patch
        )
        packed_edges: list[list[int]] = []
        for first, second in source_edges:
            edge: list[int] = []
            for source_index in (first, second):
                point_m = points[source_index]
                key = tuple(round(value, 9) for value in point_m)
                target_index = point_index.get(key)
                if target_index is None:
                    target_index = len(packed_points)
                    point_index[key] = target_index
                    packed_points.append([round(1000.0 * value, 6) for value in point_m])
                edge.append(target_index)
            packed_edges.append(edge)
        groups.append(
            {
                "patch": patch_name,
                "kind": kind,
                "source_face_count": count,
                "sampled_edge_count": len(packed_edges),
                "edges": packed_edges,
            }
        )


def extract_mesh_wireframe(case_dir: str | Path) -> dict[str, object]:
    """한 split multi-region case의 실제 표면 메시를 압축 JSON 구조로 만든다."""

    case = Path(case_dir).resolve()
    coolant = case / "constant/coolant/polyMesh"
    solid = case / "constant/batterySolid/polyMesh"
    if not coolant.is_dir() or not solid.is_dir():
        raise FileNotFoundError("split coolant/batterySolid polyMesh is unavailable")

    point_index: dict[tuple[float, float, float], int] = {}
    packed_points: list[list[float]] = []
    groups: list[dict[str, object]] = []

    def coolant_kind(name: str) -> str | None:
        if name == "outerWall":
            return "enclosure"
        if name in {"inlet", "outlet"}:
            return "port"
        return None

    def solid_kind(name: str) -> str | None:
        if name.startswith("battery_C") and "interface" in name:
            return "battery"
        return None

    _append_region_groups(
        coolant,
        patch_selector=coolant_kind,
        point_index=point_index,
        packed_points=packed_points,
        groups=groups,
        maximum_edges_per_patch=3000,
    )
    _append_region_groups(
        solid,
        patch_selector=solid_kind,
        point_index=point_index,
        packed_points=packed_points,
        groups=groups,
        maximum_edges_per_patch=350,
    )
    sampled_edge_count = sum(int(group["sampled_edge_count"]) for group in groups)
    return {
        "source": "actual OpenFOAM split-region polyMesh boundary edges",
        "synthetic": False,
        "length_unit": "mm",
        "point_count": len(packed_points),
        "sampled_edge_count": sampled_edge_count,
        "points_mm": packed_points,
        "groups": groups,
        "sampling": {
            "enclosure_or_port_maximum_edges_per_patch": 3000,
            "battery_maximum_edges_per_patch": 350,
            "policy": "deterministic uniform face stride followed by deterministic edge stride",
        },
    }


def mesh_wireframes_for_viewer(case_root: str | Path) -> dict[str, dict[str, object]]:
    """직접 case와 기존 production/L3·single 배치를 모두 탐색한다."""

    root = Path(case_root).resolve()
    candidates = {
        "precision": (root / "production/L3", root),
        "single": (root / "production/single",),
    }
    payload: dict[str, dict[str, object]] = {}
    for mesh_id, paths in candidates.items():
        for path in paths:
            if (path / "constant/coolant/polyMesh").is_dir():
                try:
                    payload[mesh_id] = extract_mesh_wireframe(path)
                except (OSError, UnicodeError, ValueError):
                    continue
                payload[mesh_id]["case_relative_path"] = (
                    "." if path == root else path.relative_to(root).as_posix()
                )
                break
    return payload


def extract_full_mesh_views(case_dir: str | Path) -> dict[str, object]:
    """Export real boundary edges and exact internal-cell cuts, without thinning.

    The existing simulation overlay deliberately samples about 13k edges. This
    separate mesh-only view instead retains the full real boundary topology and
    the cell intersections at central XY/XZ planes. Three 3D cutaways combine
    the clipped exterior with actual YZ intersections of the fluid/solid volume
    cells. A 3D exterior surface is not misrepresented as all internal edges of
    the 849k volume cells.
    """

    from .openfoam_results import OpenFoamResultReader

    case = Path(case_dir).resolve()
    points_mm: list[list[float]] = []
    point_index: dict[tuple[float, float, float], int] = {}

    def register(point: tuple[float, float, float]) -> int:
        key = tuple(round(1000.0 * value, 6) for value in point)
        found = point_index.get(key)
        if found is None:
            found = len(points_mm)
            point_index[key] = found
            points_mm.append(list(key))
        return found

    modes: dict[str, dict[str, object]] = {}
    outer_groups: list[dict[str, object]] = []
    for region, selector in (
        ("coolant", lambda name: "port" if name in {"inlet", "outlet"} else "fluid" if name == "outerWall" else None),
        ("batterySolid", lambda name: "solid" if name.startswith("battery_C") and "interface" in name else None),
    ):
        mesh_dir = case / "constant" / region / "polyMesh"
        source_points = _points(mesh_dir / "points")
        source_faces = _faces(mesh_dir / "faces")
        for patch_name, (start, count) in _boundary(mesh_dir / "boundary").items():
            kind = selector(patch_name)
            if kind is None:
                continue
            edges: set[tuple[int, int]] = set()
            for face in source_faces[start : start + count]:
                for first, second in zip(face, face[1:] + face[:1]):
                    edges.add((min(first, second), max(first, second)))
            packed_edges = [
                [register(source_points[first]), register(source_points[second])]
                for first, second in sorted(edges)
            ]
            outer_groups.append(
                {
                    "name": patch_name,
                    "kind": kind,
                    "region": region,
                    "source_face_count": count,
                    "edge_count": len(packed_edges),
                    "edges": packed_edges,
                }
            )
        del source_faces, source_points
    modes["surface"] = {"label": "3D 경계", "groups": outer_groups}

    # Keeping every volume edge visible at once would obscure the interior.
    # Retain the genuine 3D boundary on one side of each exact volume cut and
    # show all cell/plane intersections on the newly exposed face instead.
    outer_x = [point[0] for point in points_mm]
    x_min, x_max = min(outer_x), max(outer_x)

    def clipped_shell(cut_mm: float) -> list[dict[str, object]]:
        clipped_groups: list[dict[str, object]] = []
        for group in outer_groups:
            kept: list[list[int]] = []
            for first, second in group["edges"]:
                a, b = points_mm[first], points_mm[second]
                a_inside, b_inside = a[0] >= cut_mm, b[0] >= cut_mm
                if a_inside and b_inside:
                    kept.append([first, second])
                elif a_inside != b_inside:
                    fraction = (cut_mm - a[0]) / (b[0] - a[0])
                    crossing_mm = [a[index] + fraction * (b[index] - a[index]) for index in range(3)]
                    crossing = register(tuple(value / 1000.0 for value in crossing_mm))
                    kept.append([first, crossing] if a_inside else [crossing, second])
            if kept:
                clipped_groups.append(
                    {
                        "name": group["name"],
                        "kind": group["kind"] + "_shell",
                        "region": group["region"],
                        "edge_count": len(kept),
                        "edges": kept,
                    }
                )
        return clipped_groups

    foam_file = case / "runs/steady_cht/steady_cht.foam"
    backend = OpenFoamResultReader(foam_file)
    numeric_times = []
    for item in foam_file.parent.iterdir():
        if item.is_dir():
            try:
                numeric_times.append(float(item.name))
            except ValueError:
                continue
    if not numeric_times:
        raise ValueError(f"no steady-CHT field time in {foam_file.parent}")
    fluid, solid = backend.regions(max(numeric_times))
    for mode, normal, origin, label in (
        ("xy", (0, 0, 1), (0, 0, 0.041), "XY · Z = 41.0 mm"),
        ("xz", (0, 1, 0), (0, 0.0605, 0), "XZ · Y = 60.5 mm"),
        *(
            (
                f"interior{percent}",
                (1, 0, 0),
                ((x_min + (x_max - x_min) * percent / 100.0) / 1000.0, 0, 0),
                f"3D 내부 절개 · X = {x_min + (x_max - x_min) * percent / 100.0:.1f} mm",
            )
            for percent in (25, 50, 75)
        ),
    ):
        cut_groups = []
        for region, dataset, kind in (
            ("coolant", fluid, "fluid"),
            ("batterySolid", solid, "solid"),
        ):
            plane = backend.vtk.vtkPlane()
            plane.SetNormal(*normal)
            plane.SetOrigin(*origin)
            cutter = backend.vtk.vtkCutter()
            cutter.SetInputData(dataset)
            cutter.SetCutFunction(plane)
            cutter.Update()
            unique = backend.vtk.vtkExtractEdges()
            unique.SetInputConnection(cutter.GetOutputPort())
            unique.Update()
            output = unique.GetOutput()
            raw_points = output.GetPoints()
            if raw_points is None or not output.GetNumberOfCells():
                raise ValueError(f"{mode} cut missing {region} mesh edges")
            local_index = [register(raw_points.GetPoint(index)) for index in range(output.GetNumberOfPoints())]
            edges = []
            for index in range(output.GetNumberOfCells()):
                cell = output.GetCell(index)
                for segment in range(cell.GetNumberOfPoints() - 1):
                    edges.append(
                        [
                            local_index[cell.GetPointId(segment)],
                            local_index[cell.GetPointId(segment + 1)],
                        ]
                    )
            cut_groups.append(
                {"name": region, "kind": kind, "region": region, "edge_count": len(edges), "edges": edges}
            )
        modes[mode] = {
            "label": label,
            "groups": (clipped_shell(origin[0] * 1000.0) + cut_groups) if mode.startswith("interior") else cut_groups,
        }

    return {
        "source": "actual OpenFOAM polyMesh boundary faces and VTK exact volume-cell cuts",
        "sampling": "none",
        "length_unit": "mm",
        "point_count": len(points_mm),
        "points_mm": points_mm,
        "modes": modes,
    }
