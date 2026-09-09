from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .units import positive, quantity


PAPER_REYNOLDS = 8782.0
EXPECTED_TOPOLOGY = "staggered_5_4_5_4_5"
EXPECTED_COUNTS = (5, 4, 5, 4, 5)
ALLOWED_MODES = {"paper_geometry_baseline", "viscosity_re8782_sensitivity"}


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field}: expected mapping")
    return value


def _required(mapping: dict[str, Any], key: str, prefix: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{prefix}.{key}: missing required value")
    return mapping[key]


def _millimetre_point(value: object, field: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ConfigError(f"{field}: expected [x_mm, y_mm]")
    try:
        x_m, y_m = float(value[0]) * 1e-3, float(value[1]) * 1e-3
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field}: coordinates must be numeric millimetres") from exc
    if not math.isfinite(x_m) or not math.isfinite(y_m):
        raise ConfigError(f"{field}: coordinates must be finite")
    return (x_m, y_m)


def _polygon_signed_area(points: tuple[tuple[float, float], ...]) -> float:
    return 0.5 * sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(points, points[1:] + points[:1])
    )


@dataclass(frozen=True)
class Notch:
    mouth_m: float
    inner_m: float
    depth_m: float
    columns: tuple[int, ...]


@dataclass(frozen=True)
class Enclosure:
    profile: str
    width_x_m: float
    length_y_m: float
    height_z_m: float
    outline_polygon_m: tuple[tuple[float, float], ...]
    notch: Notch
    label_24mm_status: str


@dataclass(frozen=True)
class Cells:
    count: int
    diameter_m: float
    length_m: float
    topology: str
    placement_profile: str
    column_pitch_x_m: float
    row_pitch_y_m: float
    stagger_offset_y_m: float
    left_wall_clearance_m: float
    five_cell_column_first_y_m: float
    nominal_surface_gap_m: float
    alternative_pitch_x_m: float


@dataclass(frozen=True)
class Ports:
    inlet_diameter_m: float
    outlet_diameter_m: float
    external_length_m: float
    inlet_face: str
    outlet_face: str
    inlet_center_xy_m: tuple[float, float]
    outlet_center_xy_m: tuple[float, float]
    placement: str
    inlet_column: int
    inlet_column_options: tuple[int, ...]
    placement_options: tuple[str, ...]
    center_source: str


@dataclass(frozen=True)
class Fluid:
    name: str
    density_kg_m3: float
    specific_heat_j_kgk: float
    thermal_conductivity_w_mk: float
    dynamic_viscosity_pa_s: float


@dataclass(frozen=True)
class HeatSourceSpec:
    mode: str
    qvol_w_m3: float | None
    table_path: str | None
    current_profile_path: str | None
    resistance_profile_path: str | None
    outside_policy: str


@dataclass(frozen=True)
class ElectricalMetadata:
    nominal_voltage_v: float
    capacity_ah: float
    discharge_rate_c: float


@dataclass(frozen=True)
class Battery:
    density_kg_m3: float
    specific_heat_j_kgk: float
    thermal_conductivity_w_mk: float
    initial_temperature_k: float
    heat_source: HeatSourceSpec
    electrical: ElectricalMetadata


@dataclass(frozen=True)
class Boundary:
    inlet_velocity_m_s: float
    inlet_temperature_k: float
    turbulence_intensity: float
    outlet_absolute_pressure_pa: float
    pressure_reference_absolute_pa: float
    initial_fluid_temperature_k: float
    initial_solid_temperature_k: float
    outer_wall_thermal: str
    gravity_m_s2: tuple[float, float, float]


@dataclass(frozen=True)
class FullTransientValidation:
    mesh_scope: str
    expose_in_gui: bool
    max_courant: float
    min_outer_correctors: int
    require_delta_t_half_comparison: bool


@dataclass(frozen=True)
class Solver:
    distribution: str
    version: str
    energy_field: str
    steady_algorithm: str
    transient_algorithm: str
    turbulence_model: str
    full_transient_validation: FullTransientValidation


@dataclass(frozen=True)
class Derived:
    cell_volume_m3: float
    cell_heat_w: float
    total_heat_w: float
    inlet_area_m2: float
    volume_flow_m3_s: float
    mass_flow_kg_s: float
    reynolds_table4: float
    paper_reynolds: float
    viscosity_implied_by_paper_re_pa_s: float
    turbulence_k_m2_s2: float
    turbulence_length_scale_m: float
    turbulence_omega_s_inv: float
    ideal_bulk_temperature_rise_k: float
    nominal_current_a: float
    equivalent_resistance_ohm: float
    diagonal_surface_gap_m: float
    right_wall_clearance_m: float
    top_wall_clearance_m: float
    bottom_wall_clearance_m: float


@dataclass(frozen=True)
class ResolvedConfig:
    schema_version: int
    case_name: str
    mode: str
    enclosure: Enclosure
    cells: Cells
    ports: Ports
    fluid: Fluid
    battery: Battery
    boundary: Boundary
    solver: Solver
    derived: Derived
    source_path: str
    source_sha256: str
    applied_overlays: tuple[str, ...]
    applied_overrides: tuple[str, ...]
    resolved_input_sha256: str
    geometry_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


def _parse_heat_source(data: dict[str, Any]) -> HeatSourceSpec:
    mode = str(_required(data, "mode", "battery.heat_source"))
    allowed = {"constant_qvol", "table_qvol", "electrical_joule"}
    if mode not in allowed:
        raise ConfigError(f"battery.heat_source.mode: expected one of {sorted(allowed)}")
    qvol = None
    table_path = None
    current_path = None
    resistance_path = None
    outside = str(data.get("outside_policy", "hold"))
    if outside not in {"hold", "error"}:
        raise ConfigError("battery.heat_source.outside_policy: expected hold or error")
    if mode == "constant_qvol":
        qvol = positive(
            quantity(_required(data, "qvol", "battery.heat_source"), "volumetric_heat", "battery.heat_source.qvol"),
            "battery.heat_source.qvol",
        )
    elif mode == "table_qvol":
        table_path = str(_required(data, "table_path", "battery.heat_source"))
    else:
        current_path = str(_required(data, "current_profile_path", "battery.heat_source"))
        resistance_path = str(_required(data, "resistance_profile_path", "battery.heat_source"))
    return HeatSourceSpec(mode, qvol, table_path, current_path, resistance_path, outside)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _deep_merge(current, value)
        else:
            base[key] = value


def _apply_override(root: dict[str, Any], expression: str) -> None:
    if "=" not in expression:
        raise ConfigError(f"override {expression!r}: expected dotted.path=value")
    dotted_path, raw_value = expression.split("=", 1)
    parts = [part.strip() for part in dotted_path.split(".")]
    if not parts or any(not part for part in parts):
        raise ConfigError(f"override {expression!r}: invalid dotted path")
    try:
        value = yaml.safe_load(raw_value)
    except yaml.YAMLError as exc:
        raise ConfigError(f"override {expression!r}: invalid YAML value") from exc
    target: dict[str, Any] = root
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            raise ConfigError(f"override {expression!r}: {part!r} is not a mapping")
        target = child
    target[parts[-1]] = value


def load_config(
    path: str | Path,
    *,
    overlay_paths: tuple[str | Path, ...] = (),
    overrides: tuple[str, ...] = (),
) -> ResolvedConfig:
    source_path = Path(path).resolve()
    raw_bytes = source_path.read_bytes()
    try:
        root = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    root = _mapping(root, "root")

    resolved_overlay_paths: list[str] = []
    for overlay_path in overlay_paths:
        candidate = Path(overlay_path)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = candidate.resolve()
        try:
            overlay = yaml.safe_load(candidate.read_bytes())
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid overlay YAML {candidate}: {exc}") from exc
        _deep_merge(root, _mapping(overlay, str(candidate)))
        resolved_overlay_paths.append(str(candidate))
    for expression in overrides:
        _apply_override(root, expression)

    resolved_bytes = json.dumps(
        root, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")

    schema_version = int(_required(root, "schema_version", "root"))
    if schema_version != 3:
        raise ConfigError(f"schema_version: expected 3, got {schema_version}")
    case_name = str(_required(root, "case_name", "root"))
    mode = str(_required(root, "mode", "root"))
    if mode not in ALLOWED_MODES:
        raise ConfigError(f"mode: expected one of {sorted(ALLOWED_MODES)}, got {mode!r}")

    geometry = _mapping(_required(root, "geometry", "root"), "geometry")
    enclosure_raw = _mapping(_required(geometry, "enclosure", "geometry"), "geometry.enclosure")
    width_x_m = positive(quantity(_required(enclosure_raw, "width_x", "geometry.enclosure"), "length", "geometry.enclosure.width_x"), "geometry.enclosure.width_x")
    length_y_m = positive(quantity(_required(enclosure_raw, "length_y", "geometry.enclosure"), "length", "geometry.enclosure.length_y"), "geometry.enclosure.length_y")
    height_z_m = positive(quantity(_required(enclosure_raw, "height_z", "geometry.enclosure"), "length", "geometry.enclosure.height_z"), "geometry.enclosure.height_z")
    polygon_raw = _required(enclosure_raw, "outline_polygon_mm", "geometry.enclosure")
    if not isinstance(polygon_raw, list) or len(polygon_raw) < 3:
        raise ConfigError("geometry.enclosure.outline_polygon_mm: expected at least 3 points")
    outline_polygon_m = tuple(
        _millimetre_point(point, f"geometry.enclosure.outline_polygon_mm[{index}]")
        for index, point in enumerate(polygon_raw)
    )
    for index, (x_m, y_m) in enumerate(outline_polygon_m):
        if not (-1e-12 <= x_m <= width_x_m + 1e-12 and -1e-12 <= y_m <= length_y_m + 1e-12):
            raise ConfigError(f"geometry.enclosure.outline_polygon_mm[{index}]: outside enclosure bounds")
    if _polygon_signed_area(outline_polygon_m) <= 0:
        raise ConfigError("geometry.enclosure.outline_polygon_mm: expected counter-clockwise polygon")
    notch_raw = _mapping(_required(enclosure_raw, "notch", "geometry.enclosure"), "geometry.enclosure.notch")
    notch_columns_raw = _required(notch_raw, "columns", "geometry.enclosure.notch")
    if not isinstance(notch_columns_raw, list):
        raise ConfigError("geometry.enclosure.notch.columns: expected list")
    notch = Notch(
        mouth_m=positive(quantity(_required(notch_raw, "mouth", "geometry.enclosure.notch"), "length", "geometry.enclosure.notch.mouth"), "geometry.enclosure.notch.mouth"),
        inner_m=positive(quantity(_required(notch_raw, "inner", "geometry.enclosure.notch"), "length", "geometry.enclosure.notch.inner"), "geometry.enclosure.notch.inner"),
        depth_m=positive(quantity(_required(notch_raw, "depth", "geometry.enclosure.notch"), "length", "geometry.enclosure.notch.depth"), "geometry.enclosure.notch.depth"),
        columns=tuple(int(value) for value in notch_columns_raw),
    )
    enclosure = Enclosure(
        profile=str(_required(enclosure_raw, "profile", "geometry.enclosure")),
        width_x_m=width_x_m,
        length_y_m=length_y_m,
        height_z_m=height_z_m,
        outline_polygon_m=outline_polygon_m,
        notch=notch,
        label_24mm_status=str(_required(enclosure_raw, "label_24mm_status", "geometry.enclosure")),
    )

    cells_raw = _mapping(_required(geometry, "cells", "geometry"), "geometry.cells")
    count = int(_required(cells_raw, "count", "geometry.cells"))
    cells = Cells(
        count=count,
        diameter_m=positive(quantity(_required(cells_raw, "diameter", "geometry.cells"), "length", "geometry.cells.diameter"), "geometry.cells.diameter"),
        length_m=positive(quantity(_required(cells_raw, "length", "geometry.cells"), "length", "geometry.cells.length"), "geometry.cells.length"),
        topology=str(_required(cells_raw, "topology", "geometry.cells")),
        placement_profile=str(_required(cells_raw, "placement_profile", "geometry.cells")),
        column_pitch_x_m=positive(quantity(_required(cells_raw, "column_pitch_x", "geometry.cells"), "length", "geometry.cells.column_pitch_x"), "geometry.cells.column_pitch_x"),
        row_pitch_y_m=positive(quantity(_required(cells_raw, "row_pitch_y", "geometry.cells"), "length", "geometry.cells.row_pitch_y"), "geometry.cells.row_pitch_y"),
        stagger_offset_y_m=quantity(_required(cells_raw, "stagger_offset_y", "geometry.cells"), "length", "geometry.cells.stagger_offset_y"),
        left_wall_clearance_m=positive(quantity(_required(cells_raw, "left_wall_clearance", "geometry.cells"), "length", "geometry.cells.left_wall_clearance"), "geometry.cells.left_wall_clearance"),
        five_cell_column_first_y_m=positive(quantity(_required(cells_raw, "five_cell_column_first_y", "geometry.cells"), "length", "geometry.cells.five_cell_column_first_y"), "geometry.cells.five_cell_column_first_y"),
        nominal_surface_gap_m=positive(quantity(_required(cells_raw, "paper_nominal_surface_gap", "geometry.cells"), "length", "geometry.cells.paper_nominal_surface_gap"), "geometry.cells.paper_nominal_surface_gap"),
        alternative_pitch_x_m=positive(quantity(_required(cells_raw, "regular_triangular_alternative_pitch_x", "geometry.cells"), "length", "geometry.cells.regular_triangular_alternative_pitch_x"), "geometry.cells.regular_triangular_alternative_pitch_x"),
    )
    if count != 23 or cells.topology != EXPECTED_TOPOLOGY:
        raise ConfigError("paper baseline requires 23 cells and staggered_5_4_5_4_5 topology")

    ports_raw = _mapping(_required(geometry, "ports", "geometry"), "geometry.ports")
    inlet_column_options_raw = _required(ports_raw, "inlet_column_options", "geometry.ports")
    placement_options_raw = _required(ports_raw, "placement_options", "geometry.ports")
    if not isinstance(inlet_column_options_raw, list) or not isinstance(placement_options_raw, list):
        raise ConfigError("geometry.ports options must be lists")
    ports = Ports(
        inlet_diameter_m=positive(quantity(_required(ports_raw, "inlet_diameter", "geometry.ports"), "length", "geometry.ports.inlet_diameter"), "geometry.ports.inlet_diameter"),
        outlet_diameter_m=positive(quantity(_required(ports_raw, "outlet_diameter", "geometry.ports"), "length", "geometry.ports.outlet_diameter"), "geometry.ports.outlet_diameter"),
        external_length_m=positive(quantity(_required(ports_raw, "external_length", "geometry.ports"), "length", "geometry.ports.external_length"), "geometry.ports.external_length"),
        inlet_face=str(_required(ports_raw, "inlet_face", "geometry.ports")),
        outlet_face=str(_required(ports_raw, "outlet_face", "geometry.ports")),
        inlet_center_xy_m=_millimetre_point(_required(ports_raw, "inlet_center_xy_mm", "geometry.ports"), "geometry.ports.inlet_center_xy_mm"),
        outlet_center_xy_m=_millimetre_point(_required(ports_raw, "outlet_center_xy_mm", "geometry.ports"), "geometry.ports.outlet_center_xy_mm"),
        placement=str(_required(ports_raw, "placement", "geometry.ports")),
        inlet_column=int(_required(ports_raw, "inlet_column", "geometry.ports")),
        inlet_column_options=tuple(int(value) for value in inlet_column_options_raw),
        placement_options=tuple(str(value) for value in placement_options_raw),
        center_source=str(_required(ports_raw, "center_source", "geometry.ports")),
    )
    if ports.inlet_face != "top" or ports.outlet_face != "bottom":
        raise ConfigError("final-design ports require inlet_face=top and outlet_face=bottom")
    if ports.inlet_column not in ports.inlet_column_options:
        raise ConfigError("geometry.ports.inlet_column must be listed in inlet_column_options")
    if ports.placement not in ports.placement_options:
        raise ConfigError("geometry.ports.placement must be listed in placement_options")
    for label, actual, expected in (
        ("cell diameter", cells.diameter_m, 0.019),
        ("cell length", cells.length_m, 0.068),
        ("inlet diameter", ports.inlet_diameter_m, 0.019),
        ("outlet diameter", ports.outlet_diameter_m, 0.019),
    ):
        if not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-12):
            raise ConfigError(f"final-design {label} must be {expected} m")
    notch_expectations = {
        "pptx_measured_v1": (0.019, 0.011, 0.01125),
        "paper_fig8_digitized": (0.023, 0.0097, 0.0115),
    }
    expected_notch = notch_expectations.get(enclosure.profile)
    if expected_notch is None:
        raise ConfigError(f"unsupported geometry.enclosure.profile {enclosure.profile!r}")
    for label, actual, expected in zip(
        ("notch mouth", "notch inner width", "notch depth"),
        (enclosure.notch.mouth_m, enclosure.notch.inner_m, enclosure.notch.depth_m),
        expected_notch,
        strict=True,
    ):
        if not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-12):
            raise ConfigError(f"{enclosure.profile} {label} must be {expected} m")

    fluid_raw = _mapping(_required(root, "fluid", "root"), "fluid")
    fluid = Fluid(
        name=str(_required(fluid_raw, "name", "fluid")),
        density_kg_m3=positive(quantity(_required(fluid_raw, "density", "fluid"), "density", "fluid.density"), "fluid.density"),
        specific_heat_j_kgk=positive(quantity(_required(fluid_raw, "specific_heat", "fluid"), "specific_heat", "fluid.specific_heat"), "fluid.specific_heat"),
        thermal_conductivity_w_mk=positive(quantity(_required(fluid_raw, "thermal_conductivity", "fluid"), "thermal_conductivity", "fluid.thermal_conductivity"), "fluid.thermal_conductivity"),
        dynamic_viscosity_pa_s=positive(quantity(_required(fluid_raw, "dynamic_viscosity", "fluid"), "dynamic_viscosity", "fluid.dynamic_viscosity"), "fluid.dynamic_viscosity"),
    )

    battery_raw = _mapping(_required(root, "battery", "root"), "battery")
    electrical_raw = _mapping(_required(battery_raw, "electrical_metadata", "battery"), "battery.electrical_metadata")
    electrical = ElectricalMetadata(
        nominal_voltage_v=positive(quantity(_required(electrical_raw, "nominal_voltage", "battery.electrical_metadata"), "voltage", "battery.electrical_metadata.nominal_voltage"), "battery.electrical_metadata.nominal_voltage"),
        capacity_ah=positive(quantity(_required(electrical_raw, "capacity", "battery.electrical_metadata"), "capacity", "battery.electrical_metadata.capacity"), "battery.electrical_metadata.capacity"),
        discharge_rate_c=positive(float(_required(electrical_raw, "discharge_rate_c", "battery.electrical_metadata")), "battery.electrical_metadata.discharge_rate_c"),
    )
    battery = Battery(
        density_kg_m3=positive(quantity(_required(battery_raw, "density", "battery"), "density", "battery.density"), "battery.density"),
        specific_heat_j_kgk=positive(quantity(_required(battery_raw, "specific_heat", "battery"), "specific_heat", "battery.specific_heat"), "battery.specific_heat"),
        thermal_conductivity_w_mk=positive(quantity(_required(battery_raw, "thermal_conductivity", "battery"), "thermal_conductivity", "battery.thermal_conductivity"), "battery.thermal_conductivity"),
        initial_temperature_k=positive(quantity(_required(battery_raw, "initial_temperature", "battery"), "temperature", "battery.initial_temperature"), "battery.initial_temperature"),
        heat_source=_parse_heat_source(_mapping(_required(battery_raw, "heat_source", "battery"), "battery.heat_source")),
        electrical=electrical,
    )

    boundary_raw = _mapping(_required(root, "boundary", "root"), "boundary")
    gravity_raw = _required(boundary_raw, "gravity", "boundary")
    if not isinstance(gravity_raw, list) or len(gravity_raw) != 3:
        raise ConfigError("boundary.gravity: expected three components")
    intensity = float(_required(boundary_raw, "turbulence_intensity", "boundary"))
    if not 0 < intensity < 1:
        raise ConfigError("boundary.turbulence_intensity: expected fraction in (0,1)")
    boundary = Boundary(
        inlet_velocity_m_s=positive(quantity(_required(boundary_raw, "inlet_velocity", "boundary"), "velocity", "boundary.inlet_velocity"), "boundary.inlet_velocity"),
        inlet_temperature_k=positive(quantity(_required(boundary_raw, "inlet_temperature", "boundary"), "temperature", "boundary.inlet_temperature"), "boundary.inlet_temperature"),
        turbulence_intensity=intensity,
        outlet_absolute_pressure_pa=positive(quantity(_required(boundary_raw, "outlet_absolute_pressure", "boundary"), "pressure", "boundary.outlet_absolute_pressure"), "boundary.outlet_absolute_pressure"),
        pressure_reference_absolute_pa=positive(quantity(_required(boundary_raw, "pressure_reference_absolute", "boundary"), "pressure", "boundary.pressure_reference_absolute"), "boundary.pressure_reference_absolute"),
        initial_fluid_temperature_k=positive(quantity(_required(boundary_raw, "initial_fluid_temperature", "boundary"), "temperature", "boundary.initial_fluid_temperature"), "boundary.initial_fluid_temperature"),
        initial_solid_temperature_k=positive(quantity(_required(boundary_raw, "initial_solid_temperature", "boundary"), "temperature", "boundary.initial_solid_temperature"), "boundary.initial_solid_temperature"),
        outer_wall_thermal=str(_required(boundary_raw, "outer_wall_thermal", "boundary")),
        gravity_m_s2=tuple(float(v) for v in gravity_raw),
    )

    solver_raw = _mapping(_required(root, "solver", "root"), "solver")
    ft_raw = _mapping(_required(solver_raw, "full_transient_validation", "solver"), "solver.full_transient_validation")
    ft = FullTransientValidation(
        mesh_scope=str(_required(ft_raw, "mesh_scope", "solver.full_transient_validation")),
        expose_in_gui=bool(_required(ft_raw, "expose_in_gui", "solver.full_transient_validation")),
        max_courant=positive(float(_required(ft_raw, "max_courant", "solver.full_transient_validation")), "solver.full_transient_validation.max_courant"),
        min_outer_correctors=int(_required(ft_raw, "min_outer_correctors", "solver.full_transient_validation")),
        require_delta_t_half_comparison=bool(_required(ft_raw, "require_delta_t_half_comparison", "solver.full_transient_validation")),
    )
    if ft.max_courant > 5 or ft.min_outer_correctors < 3 or ft.expose_in_gui:
        raise ConfigError("full transient validation must use Co<=5, >=3 outer correctors and remain hidden from GUI")
    solver = Solver(
        distribution=str(_required(solver_raw, "distribution", "solver")),
        version=str(_required(solver_raw, "version", "solver")),
        energy_field=str(_required(solver_raw, "energy_field", "solver")),
        steady_algorithm=str(_required(solver_raw, "steady_algorithm", "solver")),
        transient_algorithm=str(_required(solver_raw, "transient_algorithm", "solver")),
        turbulence_model=str(_required(solver_raw, "turbulence_model", "solver")),
        full_transient_validation=ft,
    )

    radius = cells.diameter_m / 2
    cell_volume = math.pi * radius**2 * cells.length_m
    qvol = battery.heat_source.qvol_w_m3 if battery.heat_source.qvol_w_m3 is not None else 16325.0
    cell_heat = qvol * cell_volume
    inlet_area = math.pi * ports.inlet_diameter_m**2 / 4
    volume_flow = inlet_area * boundary.inlet_velocity_m_s
    mass_flow = volume_flow * fluid.density_kg_m3
    re = fluid.density_kg_m3 * boundary.inlet_velocity_m_s * ports.inlet_diameter_m / fluid.dynamic_viscosity_pa_s
    k = 1.5 * (boundary.inlet_velocity_m_s * intensity) ** 2
    length_scale = 0.07 * ports.inlet_diameter_m
    omega = math.sqrt(k) / (0.09**0.25 * length_scale)
    array_width = (len(EXPECTED_COUNTS) - 1) * cells.column_pitch_x_m + cells.diameter_m
    array_height = (max(EXPECTED_COUNTS) - 1) * cells.row_pitch_y_m + cells.diameter_m
    right_clearance = enclosure.width_x_m - cells.left_wall_clearance_m - array_width
    bottom_clearance = cells.five_cell_column_first_y_m - radius
    top_clearance = enclosure.length_y_m - (
        cells.five_cell_column_first_y_m
        + (max(EXPECTED_COUNTS) - 1) * cells.row_pitch_y_m
        + radius
    )
    diagonal_gap = math.hypot(cells.column_pitch_x_m, cells.stagger_offset_y_m) - cells.diameter_m
    if min(right_clearance, bottom_clearance, top_clearance, diagonal_gap) <= 0:
        raise ConfigError("cell layout intersects the enclosure or another cell")
    nominal_current = electrical.capacity_ah * electrical.discharge_rate_c
    eq_r = cell_heat / nominal_current**2
    derived = Derived(
        cell_volume_m3=cell_volume,
        cell_heat_w=cell_heat,
        total_heat_w=cell_heat * count,
        inlet_area_m2=inlet_area,
        volume_flow_m3_s=volume_flow,
        mass_flow_kg_s=mass_flow,
        reynolds_table4=re,
        paper_reynolds=PAPER_REYNOLDS,
        viscosity_implied_by_paper_re_pa_s=fluid.density_kg_m3 * boundary.inlet_velocity_m_s * ports.inlet_diameter_m / PAPER_REYNOLDS,
        turbulence_k_m2_s2=k,
        turbulence_length_scale_m=length_scale,
        turbulence_omega_s_inv=omega,
        ideal_bulk_temperature_rise_k=cell_heat * count / (mass_flow * fluid.specific_heat_j_kgk),
        nominal_current_a=nominal_current,
        equivalent_resistance_ohm=eq_r,
        diagonal_surface_gap_m=diagonal_gap,
        right_wall_clearance_m=right_clearance,
        top_wall_clearance_m=top_clearance,
        bottom_wall_clearance_m=bottom_clearance,
    )

    geometry_payload = json.dumps(
        {"enclosure": asdict(enclosure), "cells": asdict(cells), "ports": asdict(ports)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return ResolvedConfig(
        schema_version=schema_version,
        case_name=case_name,
        mode=mode,
        enclosure=enclosure,
        cells=cells,
        ports=ports,
        fluid=fluid,
        battery=battery,
        boundary=boundary,
        solver=solver,
        derived=derived,
        source_path=str(source_path),
        source_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        applied_overlays=tuple(resolved_overlay_paths),
        applied_overrides=tuple(overrides),
        resolved_input_sha256=hashlib.sha256(resolved_bytes).hexdigest(),
        geometry_sha256=hashlib.sha256(geometry_payload).hexdigest(),
    )
