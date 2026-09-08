from __future__ import annotations

import base64
import io
import json
import math
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import EXPECTED_COUNTS, ResolvedConfig
from .errors import DependencyUnavailable, MdpiCfdError
from .geometry import cell_placements


@dataclass(frozen=True)
class Component:
    size: int
    min_x: int
    min_y: int
    max_x: int
    max_y: int

    @property
    def width(self) -> int:
        return self.max_x - self.min_x + 1

    @property
    def height(self) -> int:
        return self.max_y - self.min_y + 1

    @property
    def center(self) -> tuple[float, float]:
        return ((self.min_x + self.max_x) / 2, (self.min_y + self.max_y) / 2)


def _components(image) -> list[Component]:
    grayscale = image.convert("L")
    width, height = grayscale.size
    pixels = grayscale.load()
    dark = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            if pixels[x, y] < 100:
                dark[y * width + x] = 1
    seen = bytearray(width * height)
    components: list[Component] = []
    for start, value in enumerate(dark):
        if not value or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        size = 0
        min_x, min_y, max_x, max_y = width, height, 0, 0
        while stack:
            index = stack.pop()
            y, x = divmod(index, width)
            size += 1
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            for neighbour in (index - 1, index + 1, index - width, index + width):
                if neighbour < 0 or neighbour >= width * height:
                    continue
                if seen[neighbour] or not dark[neighbour]:
                    continue
                neighbour_y, neighbour_x = divmod(neighbour, width)
                if abs(neighbour_x - x) + abs(neighbour_y - y) != 1:
                    continue
                seen[neighbour] = 1
                stack.append(neighbour)
        if size > 100:
            components.append(Component(size, min_x, min_y, max_x, max_y))
    return components


def _cluster_columns(circles: list[Component]) -> list[list[Component]]:
    columns: list[list[Component]] = []
    for circle in sorted(circles, key=lambda item: item.center[0]):
        if not columns or abs(circle.center[0] - columns[-1][0].center[0]) > 20:
            columns.append([circle])
        else:
            columns[-1].append(circle)
    for column in columns:
        column.sort(key=lambda item: item.center[1], reverse=True)
    return columns


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def write_geometry_overlay(
    config: ResolvedConfig,
    pptx_path: str | Path,
    output_dir: str | Path,
) -> dict[str, object]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise DependencyUnavailable("Pillow is required for PPT overlay analysis") from exc

    source = Path(pptx_path).resolve()
    try:
        with zipfile.ZipFile(source) as archive:
            image_bytes = archive.read("ppt/media/image4.png")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise MdpiCfdError(f"cannot extract ppt/media/image4.png from {source}: {exc}") from exc
    image = Image.open(io.BytesIO(image_bytes))
    width, height = image.size
    components = _components(image)
    circles = [
        component
        for component in components
        if 80 <= component.width <= 140
        and 80 <= component.height <= 140
        and 0.8 <= component.width / component.height <= 1.2
    ]
    columns = _cluster_columns(circles)
    if len(circles) != 23 or tuple(len(column) for column in columns) != EXPECTED_COUNTS:
        raise MdpiCfdError(
            "PPT circle detection failed: expected 23 circles in 5-4-5-4-5 columns"
        )
    outline_candidates = [
        component
        for component in components
        if component.min_x < width / 2 and component.width > 500 and component.height > 600
    ]
    if not outline_candidates:
        raise MdpiCfdError("PPT top-view enclosure outline was not detected")
    outline = max(outline_candidates, key=lambda item: item.size)
    scale_x = (outline.max_x - outline.min_x) / (config.enclosure.width_x_m * 1000)
    scale_y = (outline.max_y - outline.min_y) / (config.enclosure.length_y_m * 1000)

    placements_by_column: list[list[object]] = []
    placements = cell_placements(config)
    for column_index in range(5):
        placements_by_column.append(
            sorted(
                (item for item in placements if item.column_index == column_index),
                key=lambda item: item.center_y_m,
            )
        )

    matches: list[dict[str, object]] = []
    overlay_elements: list[str] = []
    errors_mm: list[float] = []
    radius_x = config.cells.diameter_m * 500 * scale_x
    radius_y = config.cells.diameter_m * 500 * scale_y
    for placement_column, detected_column in zip(placements_by_column, columns, strict=True):
        for placement, detected in zip(placement_column, detected_column, strict=True):
            predicted_x = outline.min_x + placement.center_x_m * 1000 * scale_x
            predicted_y = outline.max_y - placement.center_y_m * 1000 * scale_y
            detected_x, detected_y = detected.center
            error_mm = math.hypot(
                (predicted_x - detected_x) / scale_x,
                (predicted_y - detected_y) / scale_y,
            )
            errors_mm.append(error_mm)
            matches.append(
                {
                    "cell_id": placement.cell_id,
                    "detected_px": [detected_x, detected_y],
                    "predicted_px": [predicted_x, predicted_y],
                    "center_error_mm": error_mm,
                }
            )
            overlay_elements.append(
                f'<ellipse cx="{predicted_x:.3f}" cy="{predicted_y:.3f}" '
                f'rx="{radius_x:.3f}" ry="{radius_y:.3f}" />'
            )
            overlay_elements.append(
                f'<path d="M {detected_x - 4:.3f} {detected_y:.3f} H {detected_x + 4:.3f} '
                f'M {detected_x:.3f} {detected_y - 4:.3f} V {detected_y + 4:.3f}" />'
            )

    column_centers = [sum(item.center[0] for item in column) / len(column) for column in columns]
    x_pitches = [
        (second - first) / scale_x
        for first, second in zip(column_centers, column_centers[1:])
    ]
    five_row = columns[0]
    y_pitches = [
        (first.center[1] - second.center[1]) / scale_y
        for first, second in zip(five_row, five_row[1:])
    ]
    offset_px = abs(columns[1][0].center[1] - columns[0][0].center[1])
    diameters_mm = [
        0.5 * (circle.width / scale_x + circle.height / scale_y) for circle in circles
    ]
    rms_error = math.sqrt(sum(value * value for value in errors_mm) / len(errors_mm))
    report = {
        "schema_version": 1,
        "status": "OVERLAY_CREATED_REQUIRES_G1_APPROVAL",
        "source_pptx": str(source),
        "source_member": "ppt/media/image4.png",
        "source_image_px": [width, height],
        "threshold_grayscale": 100,
        "detected_cell_count": len(circles),
        "detected_topology": [len(column) for column in columns],
        "enclosure_outline_bbox_px": [
            outline.min_x,
            outline.min_y,
            outline.max_x,
            outline.max_y,
        ],
        "calibration_px_per_mm": {"x": scale_x, "y": scale_y},
        "digitized": {
            "mean_cell_diameter_mm": sum(diameters_mm) / len(diameters_mm),
            "column_pitch_x_mm": x_pitches,
            "row_pitch_y_mm": y_pitches,
            "stagger_offset_y_mm": offset_px / scale_y,
        },
        "candidate": {
            "placement_profile": config.cells.placement_profile,
            "column_pitch_x_mm": config.cells.column_pitch_x_m * 1000,
            "row_pitch_y_mm": config.cells.row_pitch_y_m * 1000,
            "stagger_offset_y_mm": config.cells.stagger_offset_y_m * 1000,
            "rms_center_error_mm": rms_error,
            "max_center_error_mm": max(errors_mm),
        },
        "matches": matches,
        "port_centers_resolved": True,
        "port_centers_status": "ASSUMPTION_A04",
        "notch_depth_mm": config.enclosure.notch.depth_m * 1000,
        "notch_depth_resolved": True,
        "notch_profile_fully_constrained": True,
        "geometry_gate_g1_passed": False,
        "blocking_reason": "A-04 port y-coordinate and inlet-column assignment require sensitivity/approval",
    }
    encoded = base64.b64encode(image_bytes).decode("ascii")
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
 viewBox="0 0 {width} {height}">
 <image width="{width}" height="{height}" href="data:image/png;base64,{encoded}"/>
 <g fill="none" stroke="#ff006e" stroke-width="2.2">{''.join(overlay_elements)}</g>
 <rect x="18" y="16" width="600" height="60" rx="8" fill="#07111ddd"/>
 <text x="32" y="41" fill="#ff70ae" font-family="sans-serif" font-size="16" font-weight="700">
   Candidate {config.cells.placement_profile}: magenta ellipse = generated CAD</text>
 <text x="32" y="63" fill="#63d7e5" font-family="sans-serif" font-size="13">
   Cross = detected drawing centre · notch depth 11.25 mm · remaining A-04 unresolved</text>
</svg>
'''
    output = Path(output_dir).resolve()
    _atomic_write(output / "top_overlay.svg", svg)
    _atomic_write(
        output / "overlay_report.json",
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return report
