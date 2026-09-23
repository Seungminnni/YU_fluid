#!/usr/bin/env python3
"""Reflect an already-published viewer across the enclosure Y mid-plane.

This is a rigid post-processing transform.  It deliberately leaves the encoded
solver scalars untouched while moving every spatial sample and relabelling the
fixed 5-4-5-4-5 battery rows.  Consequently mesh count and quality are exactly
preserved, but the bundle must describe the result as reflected rather than as
a newly solved CFD case.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


CELL_ID_MAP = {
    "C01": "C05", "C02": "C04", "C03": "C03", "C04": "C02", "C05": "C01",
    "C06": "C09", "C07": "C08", "C08": "C07", "C09": "C06",
    "C10": "C14", "C11": "C13", "C12": "C12", "C13": "C11", "C14": "C10",
    "C15": "C18", "C16": "C17", "C17": "C16", "C18": "C15",
    "C19": "C23", "C20": "C22", "C21": "C21", "C22": "C20", "C23": "C19",
}
CELL_POSITION_LABELS = {
    "C01": "좌하단", "C05": "좌상단", "C19": "우하단", "C23": "우상단", "C12": "중앙",
}


def _read_model(path: Path) -> tuple[str, dict[str, Any]]:
    source = path.read_text(encoding="utf-8")
    match = re.search(r"^(.*?=\s*)(\{.*\})\s*;?\s*$", source, re.DOTALL)
    if not match:
        raise ValueError(f"Cannot parse viewer model: {path}")
    return match.group(1), json.loads(match.group(2))


def _write_model(path: Path, prefix: str, model: dict[str, Any]) -> None:
    path.write_text(
        prefix + json.dumps(model, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )


def _reflect_point(point: list[float], span_y: float) -> None:
    point[1] = span_y - point[1]


def _remap_cell_ids(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            CELL_ID_MAP.get(key, key): _remap_cell_ids(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_remap_cell_ids(item) for item in value]
    if isinstance(value, str):
        return CELL_ID_MAP.get(value, value)
    return value


def _reflect_vectors(vectors: Any) -> None:
    if not isinstance(vectors, list):
        return
    if len(vectors) == 3 and all(isinstance(value, (int, float)) for value in vectors):
        vectors[1] = -vectors[1]
        return
    for value in vectors:
        _reflect_vectors(value)


def _reflected_section_key(key: str) -> str:
    return {
        "xz15": "xz85", "xz33": "xz68", "xz50": "xz50",
        "xz68": "xz33", "xz85": "xz15",
    }.get(key, key)


def _reflect_flow(flow: dict[str, Any], span_y: float) -> dict[str, Any]:
    flow = _remap_cell_ids(flow)
    for streamline in flow.get("streamlines", []):
        for point in streamline.get("p", streamline.get("points", [])):
            _reflect_point(point, span_y)

    reflected_sections: dict[str, Any] = {}
    for old_key, section in flow.get("sections", {}).items():
        _reflect_point(section["origin_mm"], span_y)
        section["row_step_mm"][1] *= -1
        section["column_step_mm"][1] *= -1
        _reflect_vectors(section.get("velocity_vectors_m_s"))
        new_key = _reflected_section_key(old_key)
        if old_key.startswith("xz"):
            section["fixed_fraction"] = 1.0 - float(section["fixed_fraction"])
            section["label"] = f"Y = {section['origin_mm'][1]:.1f} mm"
        reflected_sections[new_key] = section
    flow["sections"] = reflected_sections
    flow["vertical_section_keys"] = [
        _reflected_section_key(key) for key in flow.get("vertical_section_keys", [])
    ]

    direct = flow.get("direct_xy_velocity", {})
    for point in direct.get("points_xy_mm", []):
        point[1] = span_y - point[1]
    return flow


def reflect_model(model: dict[str, Any]) -> dict[str, Any]:
    existing = model.get("spatial_transform")
    if existing:
        if existing.get("type") == "rigid_reflection" and existing.get("formula") == "y' = 121 - y":
            for featured in model.get("featured_temperature_cells", []):
                featured["position"] = CELL_POSITION_LABELS.get(featured["id"], featured.get("position", ""))
            return model
        raise ValueError(f"Model already has a spatial transform: {existing}")
    span_y = float(model["enclosure_mm"]["y"])

    for cell in model["cells"]:
        cell["y"] = span_y - cell["y"]
        cell["id"] = CELL_ID_MAP[cell["id"]]
    for port in model["ports"]:
        port["y"] = span_y - port["y"]
    for point in model["outline_mm"]:
        point[1] = span_y - point[1]
    model["outline_mm"].reverse()

    for wireframe in model.get("mesh_wireframes", {}).values():
        for point in wireframe.get("points_mm", []):
            _reflect_point(point, span_y)

    model["featured_temperature_cells"] = _remap_cell_ids(
        model.get("featured_temperature_cells", [])
    )
    for featured in model["featured_temperature_cells"]:
        featured["position"] = CELL_POSITION_LABELS.get(featured["id"], featured.get("position", ""))
    if "interactive_cfd" in model:
        model["interactive_cfd"] = _reflect_flow(model["interactive_cfd"], span_y)
    model["interactive_cfd_by_mesh"] = {
        key: _reflect_flow(flow, span_y)
        for key, flow in model.get("interactive_cfd_by_mesh", {}).items()
    }
    model["spatial_transform"] = {
        "type": "rigid_reflection",
        "axis": "y",
        "plane_y_mm": span_y / 2.0,
        "formula": f"y' = {span_y:g} - y",
        "solver_recomputed": False,
        "mesh_quality_preserved": True,
        "cell_id_map": CELL_ID_MAP,
    }
    model["calculation_note"] = (
        "기존 수렴 계산의 모든 공간 좌표와 속도 Y 성분을 탱크 Y 중앙면에서 "
        "강체 반사한 결과입니다. 셀 수와 메시 품질은 원본과 동일하며 새 계산 결과로 "
        "오인하지 않도록 반사 변환 여부를 명시합니다."
    )
    model["calculation_summary"] = (
        "<strong>Y축 반사 적용 CFD 결과</strong> — 수렴된 원본 해석장을 "
        "y′ = 121 − y로 강체 반사했습니다. 메시 수·품질과 스칼라 해석값은 보존됩니다."
    )
    model["status_label"] = "CFD 결과 · Y축 반사 적용 · 메시 품질 보존"
    return model


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("viewer", type=Path, help="Published viewer directory")
    args = parser.parse_args()
    viewer = args.viewer.resolve()
    model_files = sorted(viewer.glob("model*.js"))
    if not model_files:
        raise FileNotFoundError(f"No model*.js in {viewer}")
    for path in model_files:
        prefix, model = _read_model(path)
        _write_model(path, prefix, reflect_model(model))
        print(path)
    manifest_path = viewer / "viewer_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_path.write_text(
            json.dumps(reflect_model(manifest), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(manifest_path)


if __name__ == "__main__":
    main()
