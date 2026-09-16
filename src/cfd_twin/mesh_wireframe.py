"""실제 OpenFOAM ``polyMesh`` 경계 격자를 웹용 3D 선분으로 축약한다.

웹 뷰어가 임의의 격자 무늬를 CAD 위에 그리지 않도록, 계산에 사용한
``constant/<region>/polyMesh``의 점·면·경계 patch를 직접 읽는다. 전체
849k 체적격자를 브라우저로 복사하는 대신 배터리 계면, 외벽, 입·출구의
실제 경계 edge를 patch별 고정 상한으로 균등 표본화한다.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Callable

from .openfoam_mesh_integrals import _boundary, _faces, _points


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
