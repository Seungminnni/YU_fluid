#!/usr/bin/env python3
"""Draw provisional C05/C19 port positions that fit inside the current outline.

This deliberately creates only CAD drawings, not a CFD result or production CAD.
The final port positions still require review and a fresh mesh/solver validation.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from cfd_twin.cad_case_drawings import _side, _top
from cfd_twin.config import load_config
from cfd_twin.geometry import (
    _surface_clearance_to_outline,
    cell_placements,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "viewer" / "previews" / "frame_fit_28_31_34_20260929"
DIAMETERS_MM = (28, 31, 34)
TARGET_GAP_MM = 1.0
GRID_MM = 0.25


def _nearest_center(
    diameter_mm: int,
    original_xy_mm: tuple[float, float],
    expected_cell: str,
    cells: list[dict[str, float | str]],
    outline_m: tuple[tuple[float, float], ...],
) -> tuple[float, float, float, float]:
    """Find the smallest move with >=1 mm frame clearance on a 0.25 mm grid."""

    ox, oy = original_xy_mm
    best: tuple[float, float, float, float, float] | None = None
    radius_m = diameter_mm / 2000
    for ix in range(round((ox - 16) / GRID_MM), round((ox + 16) / GRID_MM) + 1):
        x = ix * GRID_MM
        for iy in range(round((oy - 16) / GRID_MM), round((oy + 16) / GRID_MM) + 1):
            y = iy * GRID_MM
            clearance = 1000 * _surface_clearance_to_outline(
                (x / 1000, y / 1000), radius_m, outline_m
            )
            if clearance + 1e-9 < TARGET_GAP_MM:
                continue
            nearest_cell = min(
                cells,
                key=lambda cell: math.hypot(x - float(cell["x"]), y - float(cell["y"])),
            )["id"]
            if nearest_cell != expected_cell:
                continue
            displacement = math.hypot(x - ox, y - oy)
            candidate = (displacement, abs(clearance - TARGET_GAP_MM), x, y, clearance)
            if best is None or candidate < best:
                best = candidate
    if best is None:
        raise ValueError(f"No Ø{diameter_mm} mm fit near {expected_cell}")
    displacement, _, x, y, clearance = best
    return x, y, clearance, displacement


def _annotate_original(top_svg: str, original: dict[str, tuple[float, float]], outline: list[list[float]]) -> str:
    xmin = min(point[0] for point in outline)
    xmax = max(point[0] for point in outline)
    ymin = min(point[1] for point in outline)
    ymax = max(point[1] for point in outline)
    scale = min(6.0, 684.0 / (xmax - xmin), 726.0 / (ymax - ymin))
    marks = []
    for x_mm, y_mm in original.values():
        px = 60 + scale * (x_mm - xmin)
        py = 55 + scale * (ymax - y_mm)
        marks.append(
            f'<circle cx="{px:.2f}" cy="{py:.2f}" r="5" fill="white" '
            f'fill-opacity=".8" stroke="#5f6870" stroke-width="1.6" stroke-dasharray="3 2"/>'
        )
    return top_svg.replace("</svg>", "".join(marks) + "</svg>")


def main() -> None:
    config = load_config(
        ROOT / "configs" / "baseline_23cell.yaml",
        overlay_paths=(
            ROOT / "configs" / "recalculation_600k_c05_inlet_c19_outlet_phi25_20260922.yaml",
        ),
    )
    cells = [
        {
            "id": cell.cell_id,
            "x": cell.center_x_m * 1000,
            "y": cell.center_y_m * 1000,
            "z": cell.center_z_m * 1000,
            "radius": config.cells.diameter_m * 500,
            "length": config.cells.length_m * 1000,
        }
        for cell in cell_placements(config)
    ]
    outline = [[x * 1000, y * 1000] for x, y in config.enclosure.outline_polygon_m]
    original = {
        "IN": tuple(value * 1000 for value in config.ports.inlet_center_xy_m),
        "OUT": tuple(value * 1000 for value in config.ports.outlet_center_xy_m),
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    cards = []
    cases = []
    for diameter in DIAMETERS_MM:
        inlet = _nearest_center(diameter, original["IN"], "C05", cells, config.enclosure.outline_polygon_m)
        outlet = _nearest_center(diameter, original["OUT"], "C19", cells, config.enclosure.outline_polygon_m)
        ports = [
            {"id": "IN", "x": inlet[0], "y": inlet[1], "z": 98, "radius": diameter / 2, "length": 32},
            {"id": "OUT", "x": outlet[0], "y": outlet[1], "z": -16, "radius": diameter / 2, "length": 32},
        ]
        geometry = {
            "outline_mm": outline,
            "enclosure_mm": {"x": 114, "y": 121, "z": 82},
            "cells": cells,
            "ports": ports,
        }
        top_svg = _annotate_original(_top(geometry, diameter, english=False), original, outline)
        side_svg = _side(geometry, diameter, english=False)
        (OUTPUT / f"phi{diameter}_top.svg").write_text(top_svg, encoding="utf-8")
        (OUTPUT / f"phi{diameter}_side.svg").write_text(side_svg, encoding="utf-8")
        cases.append(
            {
                "diameter_mm": diameter,
                "status": "CAD_POSITION_PREVIEW_ONLY_NOT_MESHED_OR_CALCULATED",
                "inlet": {"center_xy_mm": inlet[:2], "frame_gap_mm": inlet[2], "move_from_C05_mm": inlet[3]},
                "outlet": {"center_xy_mm": outlet[:2], "frame_gap_mm": outlet[2], "move_from_C19_mm": outlet[3]},
            }
        )
        cards.append(
            f'<section class="card"><h2>Ø{diameter} mm · 입구 C05 쪽 / 출구 C19 쪽</h2>'
            f'<p>외곽 간격: 입구 {inlet[2]:.2f} mm · 출구 {outlet[2]:.2f} mm. '
            f'기존 중심에서 이동: 입구 {inlet[3]:.2f} mm · 출구 {outlet[3]:.2f} mm.</p>'
            f'<div class="views"><div>{top_svg}</div><div>{side_svg}</div></div></section>'
        )
    (OUTPUT / "preview.json").write_text(
        json.dumps({"reference": "current C05/C19 Ø25 CAD", "target_gap_mm": TARGET_GAP_MM, "cases": cases}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    html = (
        '<!doctype html><html lang="ko"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Ø28·31·34 mm 포트 위치 시안</title>'
        '<style>body{font:16px system-ui,sans-serif;background:#edf4f8;color:#193144;margin:0;padding:24px}'
        'main{max-width:1500px;margin:auto}h1{font-size:clamp(26px,4vw,40px)}'
        '.notice{background:#fff3d6;border:1px solid #d9a74a;padding:16px;border-radius:10px}'
        '.card{background:white;border:1px solid #c8dbe7;border-radius:14px;margin:20px 0;padding:20px}'
        '.views{display:grid;grid-template-columns:1fr 1fr;gap:12px}.views svg{width:100%;height:auto;border:1px solid #e2ebf1}'
        '@media(max-width:900px){.views{grid-template-columns:1fr}}'
        '</style><main><h1>Ø28 · Ø31 · Ø34 mm 포트 위치 시안</h1>'
        '<p class="notice">기존 탱크 외곽·23셀은 유지하고 입·출구 중심만 이동한 시안입니다. '
        '프레임과의 간격은 약 1 mm입니다. 이는 CFD 결과가 아니며 실제 STEP·메시 검증 전입니다. '
        '회색 점선 원은 기존 Ø25 포트 중심입니다.</p>'
        + "".join(cards) + '</main></html>'
    )
    (OUTPUT / "index.html").write_text(html, encoding="utf-8")
    print(OUTPUT / "index.html")


if __name__ == "__main__":
    main()
