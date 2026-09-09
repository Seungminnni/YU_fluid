from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import EXPECTED_COUNTS, ResolvedConfig
from .errors import ConfigError


@dataclass(frozen=True)
class CellPlacement:
    cell_id: str
    column_index: int
    row_index: int
    center_x_m: float
    center_y_m: float
    center_z_m: float


def _point_in_polygon(
    point: tuple[float, float], polygon: tuple[tuple[float, float], ...]
) -> bool:
    x, y = point
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y):
            intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection_x:
                inside = not inside
        previous = current
    return inside


def _distance_to_segment(
    point: tuple[float, float],
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    px, py = point
    ax, ay = first
    bx, by = second
    dx, dy = bx - ax, by - ay
    denominator = dx * dx + dy * dy
    if denominator == 0:
        return math.hypot(px - ax, py - ay)
    fraction = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denominator))
    return math.hypot(px - (ax + fraction * dx), py - (ay + fraction * dy))


def _surface_clearance_to_outline(
    center: tuple[float, float],
    radius: float,
    polygon: tuple[tuple[float, float], ...],
) -> float:
    if not _point_in_polygon(center, polygon):
        return -math.inf
    distance = min(
        _distance_to_segment(center, first, second)
        for first, second in zip(polygon, polygon[1:] + polygon[:1])
    )
    return distance - radius


def _polygon_area(polygon: tuple[tuple[float, float], ...]) -> float:
    return 0.5 * abs(
        sum(
            first[0] * second[1] - second[0] * first[1]
            for first, second in zip(polygon, polygon[1:] + polygon[:1])
        )
    )


def cell_placements(config: ResolvedConfig) -> tuple[CellPlacement, ...]:
    cells = config.cells
    enclosure = config.enclosure
    radius = cells.diameter_m / 2
    x0 = cells.left_wall_clearance_m + radius
    y0 = cells.five_cell_column_first_y_m
    z = enclosure.height_z_m / 2
    placements: list[CellPlacement] = []
    next_id = 1
    for column, row_count in enumerate(EXPECTED_COUNTS):
        x = x0 + column * cells.column_pitch_x_m
        offset = 0.0 if row_count == max(EXPECTED_COUNTS) else cells.stagger_offset_y_m
        for row in range(row_count):
            placements.append(
                CellPlacement(
                    cell_id=f"C{next_id:02d}",
                    column_index=column,
                    row_index=row,
                    center_x_m=x,
                    center_y_m=y0 + offset + row * cells.row_pitch_y_m,
                    center_z_m=z,
                )
            )
            next_id += 1
    if len(placements) != config.cells.count:
        raise ConfigError(
            f"topology generated {len(placements)} cells, expected {config.cells.count}"
        )
    return tuple(placements)


def geometry_report(config: ResolvedConfig) -> dict[str, object]:
    placements = cell_placements(config)
    radius = config.cells.diameter_m / 2
    min_pair = math.inf
    min_pair_ids = ("", "")
    for index, first in enumerate(placements):
        for second in placements[index + 1 :]:
            distance = math.hypot(
                first.center_x_m - second.center_x_m,
                first.center_y_m - second.center_y_m,
            )
            gap = distance - 2 * radius
            if gap < min_pair:
                min_pair = gap
                min_pair_ids = (first.cell_id, second.cell_id)

    left = min(cell.center_x_m - radius for cell in placements)
    right = config.enclosure.width_x_m - max(
        cell.center_x_m + radius for cell in placements
    )
    bottom = min(cell.center_y_m - radius for cell in placements)
    top = config.enclosure.length_y_m - max(
        cell.center_y_m + radius for cell in placements
    )
    lower_z = min(cell.center_z_m - config.cells.length_m / 2 for cell in placements)
    upper_z = config.enclosure.height_z_m - max(
        cell.center_z_m + config.cells.length_m / 2 for cell in placements
    )
    if min(min_pair, left, right, bottom, top, lower_z, upper_z) <= 0:
        raise ConfigError("generated geometry contains a collision")

    outline_clearances = {
        cell.cell_id: _surface_clearance_to_outline(
            (cell.center_x_m, cell.center_y_m), radius, config.enclosure.outline_polygon_m
        )
        for cell in placements
    }
    minimum_outline_cell = min(outline_clearances, key=outline_clearances.get)
    if outline_clearances[minimum_outline_cell] <= 0:
        raise ConfigError(f"{minimum_outline_cell} intersects the measured enclosure outline")

    port_clearances = {
        "inlet": _surface_clearance_to_outline(
            config.ports.inlet_center_xy_m,
            config.ports.inlet_diameter_m / 2,
            config.enclosure.outline_polygon_m,
        ),
        "outlet": _surface_clearance_to_outline(
            config.ports.outlet_center_xy_m,
            config.ports.outlet_diameter_m / 2,
            config.enclosure.outline_polygon_m,
        ),
    }
    if min(port_clearances.values()) <= 0:
        raise ConfigError("an A-04 port opening intersects the measured enclosure outline")

    inlet_area = math.pi * config.ports.inlet_diameter_m**2 / 4
    outlet_area = math.pi * config.ports.outlet_diameter_m**2 / 4
    return {
        "schema_version": 1,
        "case_name": config.case_name,
        "geometry_sha256": config.geometry_sha256,
        "units": "m",
        "cell_count": len(placements),
        "placements": [asdict(cell) for cell in placements],
        "clearances": {
            "minimum_cell_cell_surface_m": min_pair,
            "minimum_pair": list(min_pair_ids),
            "left_wall_m": left,
            "right_wall_m": right,
            "bottom_wall_m": bottom,
            "top_wall_m": top,
            "lower_z_m": lower_z,
            "upper_z_m": upper_z,
            "paper_nominal_cell_gap_m": config.cells.nominal_surface_gap_m,
            "minimum_cell_outline_surface_m": outline_clearances[minimum_outline_cell],
            "minimum_cell_outline_id": minimum_outline_cell,
        },
        "ports": {
            "inlet_diameter_m": config.ports.inlet_diameter_m,
            "outlet_diameter_m": config.ports.outlet_diameter_m,
            "inlet_area_m2": inlet_area,
            "outlet_area_m2": outlet_area,
            "external_length_m": config.ports.external_length_m,
            "inlet_face": config.ports.inlet_face,
            "outlet_face": config.ports.outlet_face,
            "inlet_center_xy_m": list(config.ports.inlet_center_xy_m),
            "outlet_center_xy_m": list(config.ports.outlet_center_xy_m),
            "placement": config.ports.placement,
            "inlet_column": config.ports.inlet_column,
            "center_source": config.ports.center_source,
            "centers_resolved": True,
            "centers_status": "ASSUMPTION_A04",
            "opening_to_outline_clearance_m": port_clearances,
        },
        "enclosure_profile": {
            "profile": config.enclosure.profile,
            "outline_polygon_m": [list(point) for point in config.enclosure.outline_polygon_m],
            "notch": asdict(config.enclosure.notch),
            "label_24mm_status": config.enclosure.label_24mm_status,
            "footprint_area_m2": _polygon_area(config.enclosure.outline_polygon_m),
            "gross_extruded_volume_m3": _polygon_area(config.enclosure.outline_polygon_m)
            * config.enclosure.height_z_m,
        },
        "source_evidence": {
            "port_diameter": "PPT side-view labels and 300-dpi measurement",
            "port_24mm_hypothesis": "rejected_land_width_not_diameter",
            "column_pitch": "drawing_measurement_default_with_19.48557mm_alternative",
            "notch_geometry": "PPT/300-dpi contour: mouth 19, inner 11, depth 11.25 mm",
        },
        "production_cad_ready": False,
        "blocking_assumptions": [],
        "nonblocking_assumptions": [
            "A-04 port y-coordinate and inlet-column assignment require sensitivity/approval",
        ],
    }


def _svg(config: ResolvedConfig, placements: tuple[CellPlacement, ...]) -> str:
    scale = 6.0
    width_mm = config.enclosure.width_x_m * 1000
    height_mm = config.enclosure.length_y_m * 1000
    canvas_w = width_mm * scale
    canvas_h = height_mm * scale
    radius = config.cells.diameter_m * 1000 * scale / 2
    circles: list[str] = []
    labels: list[str] = []
    for cell in placements:
        x = cell.center_x_m * 1000 * scale
        y = canvas_h - cell.center_y_m * 1000 * scale
        circles.append(
            f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{radius:.3f}" '
            'fill="#dcecff" stroke="#17324d" stroke-width="1.5" />'
        )
        labels.append(
            f'<text x="{x:.3f}" y="{y + 3:.3f}" text-anchor="middle" '
            'font-family="sans-serif" font-size="8">'
            f"{cell.cell_id}</text>"
        )
    outline = " ".join(
        f"{x_m * 1000 * scale:.3f},{canvas_h - y_m * 1000 * scale:.3f}"
        for x_m, y_m in config.enclosure.outline_polygon_m
    )
    port_circles = []
    for label, center in (
        ("IN", config.ports.inlet_center_xy_m),
        ("OUT", config.ports.outlet_center_xy_m),
    ):
        x = center[0] * 1000 * scale
        y = canvas_h - center[1] * 1000 * scale
        port_circles.append(
            f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{radius:.3f}" '
            'fill="none" stroke="#e66b00" stroke-width="2" stroke-dasharray="7 4" />'
            f'<text x="{x:.3f}" y="{y - radius - 4:.3f}" text-anchor="middle" '
            'font-family="sans-serif" font-size="9" fill="#b24700">'
            f'{label} A-04</text>'
        )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{canvas_w:.0f}" height="{canvas_h:.0f}"
     viewBox="0 0 {canvas_w:.3f} {canvas_h:.3f}">
  <rect width="100%" height="100%" fill="white" />
  <polygon points="{outline}" fill="none" stroke="#111" stroke-width="2" />
  {''.join(circles)}
  {''.join(labels)}
  {''.join(port_circles)}
  <text x="8" y="16" font-family="sans-serif" font-size="10" fill="#a00">
    Port centres shown as A-04 assumptions; diameter is fixed at 19 mm.
  </text>
</svg>
'''


def _vtk_cylinders(
    config: ResolvedConfig, placements: tuple[CellPlacement, ...], segments: int = 32
) -> str:
    radius = config.cells.diameter_m / 2
    half_length = config.cells.length_m / 2
    points: list[tuple[float, float, float]] = []
    polygons: list[list[int]] = []
    cell_values: list[int] = []
    for cell_index, cell in enumerate(placements, start=1):
        base = len(points)
        for z in (cell.center_z_m - half_length, cell.center_z_m + half_length):
            for segment in range(segments):
                angle = 2 * math.pi * segment / segments
                points.append(
                    (
                        cell.center_x_m + radius * math.cos(angle),
                        cell.center_y_m + radius * math.sin(angle),
                        z,
                    )
                )
        bottom_center = len(points)
        points.append((cell.center_x_m, cell.center_y_m, cell.center_z_m - half_length))
        top_center = len(points)
        points.append((cell.center_x_m, cell.center_y_m, cell.center_z_m + half_length))
        for segment in range(segments):
            nxt = (segment + 1) % segments
            polygons.append([base + segment, base + nxt, base + segments + nxt, base + segments + segment])
            cell_values.append(cell_index)
            polygons.append([bottom_center, base + nxt, base + segment])
            cell_values.append(cell_index)
            polygons.append([top_center, base + segments + segment, base + segments + nxt])
            cell_values.append(cell_index)
    polygon_size = sum(len(poly) + 1 for poly in polygons)
    lines = [
        "# vtk DataFile Version 3.0",
        "23-cell battery surfaces",
        "ASCII",
        "DATASET POLYDATA",
        f"POINTS {len(points)} float",
    ]
    lines.extend(f"{x:.9g} {y:.9g} {z:.9g}" for x, y, z in points)
    lines.append(f"POLYGONS {len(polygons)} {polygon_size}")
    lines.extend(f"{len(poly)} " + " ".join(map(str, poly)) for poly in polygons)
    lines.extend(
        [
            f"CELL_DATA {len(polygons)}",
            "SCALARS cell_index int 1",
            "LOOKUP_TABLE default",
        ]
    )
    lines.extend(str(value) for value in cell_values)
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def write_geometry_bundle(
    config: ResolvedConfig, output_dir: str | Path
) -> dict[str, object]:
    output = Path(output_dir).resolve()
    placements = cell_placements(config)
    report = geometry_report(config)
    _atomic_write(
        output / "geometry_report.json",
        json.dumps(report, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(output / "top_view.svg", _svg(config, placements))
    _atomic_write(output / "battery_surfaces.vtk", _vtk_cylinders(config, placements))
    return report
