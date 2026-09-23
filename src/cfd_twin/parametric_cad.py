"""Parametric CAD generation for the validated 23-cell reference family.

CAD export stays separate from solver launch so the generated STEP solids can
be validated and frozen first.  The workflow app may then pass that exact
bundle, together with these derived geometry overrides, to the normal
mesh/S0/S1/S2 worker.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cad import execute_cad_source
from .config import ResolvedConfig, load_config
from .geometry import geometry_report


SOURCE_ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = SOURCE_ROOT / "configs/baseline_23cell.yaml"
BASE_TANK_MM = (114.0, 121.0, 82.0)
BASE_OUTLINE_MM = (
    (4.1, 121.0), (0.0, 109.6), (0.0, 11.6), (3.9, 0.0),
    (23.0, 0.0), (27.0, 11.25), (38.0, 11.25), (42.0, 0.0),
    (61.0, 0.0), (65.0, 11.25), (76.0, 11.25), (80.0, 0.0),
    (114.0, 0.0), (114.0, 121.0), (80.0, 121.0),
    (76.0, 109.75), (65.0, 109.75), (61.0, 121.0),
    (42.0, 121.0), (38.0, 109.75), (27.0, 109.75), (23.0, 121.0),
)
BASE_LEFT_CLEARANCE_MM = 4.1
BASE_FIRST_Y_MM = 15.2
BASE_INLET_CENTER_MM = (100.3, 107.5)
BASE_OUTLET_CENTER_MM = (13.5, 13.5)
PARAMETRIC_FILES = (
    "enclosure_with_ports.step",
    "coolant.step",
    "battery_cells.step",
    "coolant.stl",
    "battery_cells.stl",
    "cad_manifest.json",
    "cad_result.json",
    "resolved_config.json",
    "geometry_report.json",
    "parametric_request.json",
)


def _polygon_area_mm2(points: tuple[tuple[float, float], ...]) -> float:
    return 0.5 * abs(sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(points, points[1:] + points[:1])
    ))


BASE_GROSS_VOLUME_L = _polygon_area_mm2(BASE_OUTLINE_MM) * BASE_TANK_MM[2] / 1e6


@dataclass(frozen=True)
class ParametricCadSpec:
    template: str
    size_mode: str
    tank_width_mm: float
    tank_length_mm: float
    tank_height_mm: float
    inlet_diameter_mm: float
    outlet_diameter_mm: float
    requested_volume_l: float | None
    gross_volume_l: float

    def to_dict(self) -> dict[str, object]:
        return {
            "template": self.template,
            "size_mode": self.size_mode,
            "tank_width_mm": self.tank_width_mm,
            "tank_length_mm": self.tank_length_mm,
            "tank_height_mm": self.tank_height_mm,
            "inlet_diameter_mm": self.inlet_diameter_mm,
            "outlet_diameter_mm": self.outlet_diameter_mm,
            "requested_volume_l": self.requested_volume_l,
            "gross_volume_l": self.gross_volume_l,
        }


def _number(value: object, label: str, lower: float, upper: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}: 숫자가 필요합니다.") from exc
    if not math.isfinite(number) or not lower <= number <= upper:
        raise ValueError(f"{label}: 허용 범위 {lower:g}–{upper:g} 밖입니다.")
    return number


def resolve_parametric_spec(value: object) -> ParametricCadSpec:
    """Validate UI values and derive X/Y/Z from volume when requested."""

    if not isinstance(value, dict):
        raise ValueError("파라메트릭 CAD 입력은 JSON 객체여야 합니다.")
    template = str(value.get("template", "reference_23cell"))
    if template != "reference_23cell":
        raise ValueError("현재는 검증된 23셀 기준 CAD 템플릿만 지원합니다.")
    size_mode = str(value.get("size_mode", "dimensions"))
    if size_mode not in {"dimensions", "volume"}:
        raise ValueError("탱크 크기 방식은 dimensions 또는 volume이어야 합니다.")

    requested_volume_l: float | None = None
    if size_mode == "volume":
        requested_volume_l = _number(value.get("tank_volume_l"), "탱크 체적", 0.93, 12.0)
        scale = (requested_volume_l / BASE_GROSS_VOLUME_L) ** (1.0 / 3.0)
        width, length, height = (dimension * scale for dimension in BASE_TANK_MM)
    else:
        width = _number(value.get("tank_width_mm"), "탱크 폭 X", 105.0, 300.0)
        length = _number(value.get("tank_length_mm"), "탱크 길이 Y", 115.0, 300.0)
        height = _number(value.get("tank_height_mm"), "탱크 높이 Z", 69.0, 300.0)

    inlet = _number(value.get("inlet_diameter_mm"), "입구 직경", 5.0, 50.0)
    outlet = _number(value.get("outlet_diameter_mm"), "출구 직경", 5.0, 50.0)
    sx, sy = width / BASE_TANK_MM[0], length / BASE_TANK_MM[1]
    gross_volume_l = BASE_GROSS_VOLUME_L * sx * sy * (height / BASE_TANK_MM[2])
    return ParametricCadSpec(
        template=template,
        size_mode=size_mode,
        tank_width_mm=width,
        tank_length_mm=length,
        tank_height_mm=height,
        inlet_diameter_mm=inlet,
        outlet_diameter_mm=outlet,
        requested_volume_l=requested_volume_l,
        gross_volume_l=gross_volume_l,
    )


def parametric_config_overrides(spec: ParametricCadSpec) -> tuple[str, ...]:
    """Return the reproducible config overrides for one validated CAD variant."""

    sx = spec.tank_width_mm / BASE_TANK_MM[0]
    sy = spec.tank_length_mm / BASE_TANK_MM[1]
    # Preserve exact 0/max boundary coordinates.  Rounding a uniformly derived
    # dimension independently from a scaled polygon point can otherwise put a
    # nominal max vertex a few picometres outside the declared box.
    outline = [
        [
            spec.tank_width_mm if x == BASE_TANK_MM[0] else 0.0 if x == 0.0 else x * sx,
            spec.tank_length_mm if y == BASE_TANK_MM[1] else 0.0 if y == 0.0 else y * sy,
        ]
        for x, y in BASE_OUTLINE_MM
    ]

    # Keep the validated 23-cell array rigid and centred as the tank changes.
    # Ports follow the user-confirmed opposite-corner placement instead of the
    # former end-cell-centre assumption. Preserve the baseline edge clearance
    # while accounting for independently configurable inlet/outlet radii.
    dx = (spec.tank_width_mm - BASE_TANK_MM[0]) / 2.0
    dy = (spec.tank_length_mm - BASE_TANK_MM[1]) / 2.0
    left_clearance = BASE_LEFT_CLEARANCE_MM + dx
    first_y = BASE_FIRST_Y_MM + dy
    inlet_x_clearance = BASE_TANK_MM[0] - BASE_INLET_CENTER_MM[0] - 19.0 / 2.0
    inlet_y_clearance = BASE_TANK_MM[1] - BASE_INLET_CENTER_MM[1] - 19.0 / 2.0
    outlet_x_clearance = BASE_OUTLET_CENTER_MM[0] - 19.0 / 2.0
    outlet_y_clearance = BASE_OUTLET_CENTER_MM[1] - 19.0 / 2.0
    inlet_radius = spec.inlet_diameter_mm / 2.0
    outlet_radius = spec.outlet_diameter_mm / 2.0
    inlet_center = [
        spec.tank_width_mm - inlet_x_clearance - inlet_radius,
        spec.tank_length_mm - inlet_y_clearance - inlet_radius,
    ]
    outlet_center = [
        outlet_x_clearance + outlet_radius,
        outlet_y_clearance + outlet_radius,
    ]

    return (
        "case_name=parametric_23cell",
        "mode=imported_23cell",
        f"geometry.enclosure.width_x={spec.tank_width_mm:.17g} mm",
        f"geometry.enclosure.length_y={spec.tank_length_mm:.17g} mm",
        f"geometry.enclosure.height_z={spec.tank_height_mm:.17g} mm",
        "geometry.enclosure.outline_polygon_mm=" + json.dumps(outline),
        f"geometry.enclosure.notch.mouth={19.0 * sx:.17g} mm",
        f"geometry.enclosure.notch.inner={11.0 * sx:.17g} mm",
        f"geometry.enclosure.notch.depth={11.25 * sy:.17g} mm",
        f"geometry.cells.left_wall_clearance={left_clearance:.17g} mm",
        f"geometry.cells.five_cell_column_first_y={first_y:.17g} mm",
        f"geometry.ports.inlet_diameter={spec.inlet_diameter_mm:.17g} mm",
        f"geometry.ports.outlet_diameter={spec.outlet_diameter_mm:.17g} mm",
        "geometry.ports.inlet_center_xy_mm=" + json.dumps(inlet_center),
        "geometry.ports.outlet_center_xy_mm=" + json.dumps(outlet_center),
        "geometry.ports.placement=corner_4mm_clearance",
        "geometry.ports.center_source=PARAMETRIC_USER_CONFIRMED_CORNER_PLACEMENT",
    )


def parametric_config(spec: ParametricCadSpec) -> ResolvedConfig:
    """Resolve the parametric variant through the project's single config path."""

    return load_config(BASE_CONFIG, overrides=parametric_config_overrides(spec))


def preview_parametric_cad(value: object) -> dict[str, object]:
    """Return derived dimensions and analytic collision checks without CAD export."""

    spec = resolve_parametric_spec(value)
    config = parametric_config(spec)
    report = geometry_report(config)
    return _summary(spec, config, report)


def _summary(
    spec: ParametricCadSpec,
    config: ResolvedConfig,
    report: dict[str, object],
) -> dict[str, object]:
    clearances = report["clearances"]
    ports = report["ports"]
    assert isinstance(clearances, dict) and isinstance(ports, dict)
    return {
        "status": "PARAMETRIC_GEOMETRY_PRECHECK_PASSED",
        "template": "검증된 23셀 기준 CAD",
        "spec": spec.to_dict(),
        "derived": {
            "tank_mm": [spec.tank_width_mm, spec.tank_length_mm, spec.tank_height_mm],
            "gross_tank_volume_l": spec.gross_volume_l,
            "cell_count": config.cells.count,
            "inlet_center_xy_mm": [value * 1000 for value in config.ports.inlet_center_xy_m],
            "outlet_center_xy_mm": [value * 1000 for value in config.ports.outlet_center_xy_m],
            "minimum_cell_gap_mm": float(clearances["minimum_cell_cell_surface_m"]) * 1000,
            "minimum_outline_gap_mm": float(clearances["minimum_cell_outline_surface_m"]) * 1000,
            "inlet_outline_clearance_mm": float(ports["opening_to_outline_clearance_m"]["inlet"]) * 1000,
            "outlet_outline_clearance_mm": float(ports["opening_to_outline_clearance_m"]["outlet"]) * 1000,
        },
        "geometry_sha256": config.geometry_sha256,
        "calculation_started": False,
        "next_stage": "STEP/STL 생성 후 메시 생성",
    }


def generate_parametric_cad(root: str | Path, value: object) -> dict[str, object]:
    """Generate and validate a unique STEP/STL bundle; never start CFD."""

    spec = resolve_parametric_spec(value)
    config = parametric_config(spec)
    report = geometry_report(config)
    generation_id = uuid.uuid4().hex
    output = Path(root).resolve() / "parametric" / generation_id
    output.mkdir(parents=True, exist_ok=False)
    (output / "parametric_request.json").write_text(
        json.dumps(spec.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "resolved_config.json").write_text(config.to_json() + "\n", encoding="utf-8")
    (output / "geometry_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    result = execute_cad_source(config, output)
    summary = _summary(spec, config, report)
    files = [name for name in PARAMETRIC_FILES if (output / name).is_file()]
    summary.update({
        "id": generation_id,
        "status": "PARAMETRIC_CAD_EXPORTED_GEOMETRY_CHECKS_PASSED",
        "cad_checks": result.get("checks", {}),
        "files": files,
        "download_urls": {name: f"/parametric/{generation_id}/{name}" for name in files},
        "message": "STEP/STL 생성과 형상 검사가 끝났습니다. 메시와 S0/S1/S2 계산은 시작하지 않았습니다.",
    })
    return summary
