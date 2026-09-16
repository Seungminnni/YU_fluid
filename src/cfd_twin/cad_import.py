"""Import a *verified 23-cell family* STEP assembly for a new CFD case.

The imported solids, not a reconstructed parametric copy, are exported to the
OpenFOAM triSurface directory.  This deliberately supports port variations of
the validated tank and battery layout first.  A different topology is reported
as unsupported instead of silently being meshed with the baseline geometry.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
from pathlib import Path

from .config import ResolvedConfig, load_config
from .errors import CfdTwinError, DependencyUnavailable
from .geometry import cell_placements, geometry_report


STEP_ROLES = ("enclosure_with_ports.step", "coolant.step", "battery_cells.step")
PORT_TOLERANCE_MM = 0.05
GEOMETRY_TOLERANCE_MM = 0.05


def _cadquery():
    try:
        import cadquery as cq
    except ImportError as exc:
        raise DependencyUnavailable("CAD import requires the cadquery extra") from exc
    return cq


def _load_steps(files: dict[str, Path]):
    cq = _cadquery()
    objects = {}
    for role in STEP_ROLES:
        source = Path(files[role]).resolve()
        if not source.is_file() or source.suffix.lower() not in {".step", ".stp"}:
            raise CfdTwinError(f"{role}: STEP file is required")
        if source.stat().st_size > 50 * 1024 * 1024:
            raise CfdTwinError(f"{role}: file exceeds 50 MiB")
        try:
            objects[role] = cq.importers.importStep(str(source))
        except Exception as exc:
            raise CfdTwinError(f"{role}: STEP import failed: {exc}") from exc
    return cq, objects


def _port_cap(outer, z_mm: float, label: str):
    candidates = [
        face for face in outer.faces().vals()
        if face.geomType() == "PLANE"
        and abs(face.Center().z - z_mm) < GEOMETRY_TOLERANCE_MM
        and len(face.Edges()) == 1
        and face.Edges()[0].geomType() == "CIRCLE"
    ]
    if len(candidates) != 1:
        raise CfdTwinError(f"{label}: exactly one planar circular end face is required")
    face = candidates[0]
    diameter = 2 * math.sqrt(face.Area() / math.pi)
    if not 5 <= diameter <= 50:
        raise CfdTwinError(f"{label}: inferred port diameter {diameter:.3f} mm is outside 5–50 mm")
    return face, diameter


def _tank_shoulders(outer, cap_area: float) -> tuple[float, float]:
    faces = [
        face for face in outer.faces().vals()
        if face.geomType() == "PLANE"
        and abs(face.normalAt().z) > 0.99
        and face.Area() > 2 * cap_area
    ]
    levels = sorted({round(face.Center().z, 6) for face in faces})
    if len(levels) != 2:
        raise CfdTwinError("tank: top/bottom shoulder faces are ambiguous")
    return levels[0], levels[1]


def _close(label: str, actual: float, expected: float, *, tolerance: float = GEOMETRY_TOLERANCE_MM) -> None:
    if not math.isfinite(actual) or abs(actual - expected) > tolerance:
        raise CfdTwinError(f"{label}: CAD {actual:.6g} mm differs from supported family {expected:.6g} mm")


def _battery_order(batteries, config: ResolvedConfig):
    solids = list(batteries.solids().vals())
    if len(solids) != 23:
        raise CfdTwinError(f"battery: current solver requires 23 separate solids, got {len(solids)}")
    remaining = solids.copy()
    ordered = []
    diameter = config.cells.diameter_m * 1000
    length = config.cells.length_m * 1000
    for placement in cell_placements(config):
        expected_x = placement.center_x_m * 1000
        expected_y = placement.center_y_m * 1000
        match = min(
            remaining,
            key=lambda solid: (solid.BoundingBox().center.x - expected_x) ** 2
            + (solid.BoundingBox().center.y - expected_y) ** 2,
        )
        box = match.BoundingBox()
        _close(f"{placement.cell_id} x", box.center.x, expected_x)
        _close(f"{placement.cell_id} y", box.center.y, expected_y)
        _close(f"{placement.cell_id} diameter x", box.xlen, diameter)
        _close(f"{placement.cell_id} diameter y", box.ylen, diameter)
        _close(f"{placement.cell_id} height", box.zlen, length)
        expected_volume = math.pi * (diameter / 2) ** 2 * length
        if abs(match.Volume() / expected_volume - 1) > 1e-4:
            raise CfdTwinError(f"{placement.cell_id}: non-cylindrical/invalid battery solid")
        ordered.append((placement.cell_id, match))
        remaining.remove(match)
    return ordered


def inspect_imported_steps(files: dict[str, Path], base_config: ResolvedConfig) -> dict[str, object]:
    """Read geometry and return detected roles/ports without approving a solve."""

    _, objects = _load_steps(files)
    outer = objects["enclosure_with_ports.step"]
    coolant = objects["coolant.step"]
    batteries = objects["battery_cells.step"]
    if outer.solids().size() != 1 or coolant.solids().size() != 1:
        raise CfdTwinError("one connected enclosure and one connected coolant volume are required")
    if not all(solid.isValid() for obj in objects.values() for solid in obj.solids().vals()):
        raise CfdTwinError("STEP contains invalid or open solids")
    ordered = _battery_order(batteries, base_config)
    box = outer.val().BoundingBox()
    _close("tank x minimum", box.xmin, 0)
    _close("tank y minimum", box.ymin, 0)
    _close("tank width", box.xlen, base_config.enclosure.width_x_m * 1000)
    _close("tank length", box.ylen, base_config.enclosure.length_y_m * 1000)
    inlet_face, inlet_d = _port_cap(outer, box.zmax, "inlet")
    outlet_face, outlet_d = _port_cap(outer, box.zmin, "outlet")
    bottom_z, top_z = _tank_shoulders(outer, max(inlet_face.Area(), outlet_face.Area()))
    _close("tank bottom", bottom_z, 0)
    _close("tank height", top_z, base_config.enclosure.height_z_m * 1000)
    inlet_length = box.zmax - top_z
    outlet_length = bottom_z - box.zmin
    _close("port lengths", inlet_length, outlet_length)
    if not 5 <= inlet_length <= 100:
        raise CfdTwinError("port length is outside the supported 5–100 mm range")
    expected_tank_mm3 = geometry_report(base_config)["enclosure_profile"]["gross_extruded_volume_m3"] * 1e9
    core_mm3 = outer.val().Volume() - inlet_face.Area() * inlet_length - outlet_face.Area() * outlet_length
    if abs(core_mm3 / expected_tank_mm3 - 1) > 1e-4:
        raise CfdTwinError("tank core differs from the supported 23-cell tank profile")
    battery_volume = sum(solid.Volume() for _, solid in ordered)
    balance_error = abs(outer.val().Volume() - coolant.val().Volume() - battery_volume)
    if balance_error / outer.val().Volume() > 1e-5:
        raise CfdTwinError("coolant + battery volumes do not fill the enclosure")
    # Volume balance alone is insufficient: a shifted fluid cavity can have
    # exactly the same volume. Verify the imported coolant is inside the tank
    # and does not occupy any of the 23 battery solids.
    coolant_inside = coolant.val().intersect(outer.val()).Volume()
    if abs(coolant_inside - coolant.val().Volume()) / outer.val().Volume() > 1e-5:
        raise CfdTwinError("coolant solid extends outside the enclosure")
    coolant_battery_overlap = sum(coolant.val().intersect(solid).Volume() for _, solid in ordered)
    if coolant_battery_overlap / outer.val().Volume() > 1e-5:
        raise CfdTwinError("coolant and battery solids overlap")
    return {
        "status": "SUPPORTED_23CELL_PORT_VARIANT",
        "inlet": {"center_xy_mm": [inlet_face.Center().x, inlet_face.Center().y], "diameter_mm": inlet_d, "area_mm2": inlet_face.Area()},
        "outlet": {"center_xy_mm": [outlet_face.Center().x, outlet_face.Center().y], "diameter_mm": outlet_d, "area_mm2": outlet_face.Area()},
        "port_length_mm": inlet_length,
        "tank_mm": [box.xlen, box.ylen, top_z - bottom_z],
        "battery_count": len(ordered),
        "volumes_mm3": {"outer": outer.val().Volume(), "coolant": coolant.val().Volume(), "battery": battery_volume},
        "volume_balance_fraction": balance_error / outer.val().Volume(),
        "coolant_outside_fraction": abs(coolant_inside - coolant.val().Volume()) / outer.val().Volume(),
        "coolant_battery_overlap_fraction": coolant_battery_overlap / outer.val().Volume(),
        "source_sha256": {role: hashlib.sha256(Path(files[role]).read_bytes()).hexdigest() for role in STEP_ROLES},
    }


def imported_config_overrides(report: dict[str, object]) -> tuple[str, ...]:
    inlet = report["inlet"]
    outlet = report["outlet"]
    assert isinstance(inlet, dict) and isinstance(outlet, dict)
    return (
        "mode=imported_23cell",
        f"geometry.ports.inlet_diameter={inlet['diameter_mm']:.12g} mm",
        f"geometry.ports.outlet_diameter={outlet['diameter_mm']:.12g} mm",
        f"geometry.ports.external_length={report['port_length_mm']:.12g} mm",
        f"geometry.ports.inlet_center_xy_mm={json.dumps(inlet['center_xy_mm'])}",
        f"geometry.ports.outlet_center_xy_mm={json.dumps(outlet['center_xy_mm'])}",
        "geometry.ports.center_source=imported_STEP_verified",
    )


def import_step_bundle(
    files: dict[str, Path], base_config_path: Path, overlay_path: Path, output: Path
) -> dict[str, object]:
    """Verify STEP solids and export their actual surfaces for OpenFOAM."""

    base = load_config(base_config_path, overlay_paths=(overlay_path,))
    report = inspect_imported_steps(files, base)
    overrides = imported_config_overrides(report)
    config = load_config(base_config_path, overlay_paths=(overlay_path,), overrides=overrides)
    cq, objects = _load_steps(files)
    outer = objects["enclosure_with_ports.step"]
    ordered = _battery_order(objects["battery_cells.step"], config)
    box = outer.val().BoundingBox()
    inlet_face, _ = _port_cap(outer, box.zmax, "inlet")
    outlet_face, _ = _port_cap(outer, box.zmin, "outlet")
    cap_hashes = {inlet_face.hashCode(), outlet_face.hashCode()}
    walls = [face for face in outer.faces().vals() if face.hashCode() not in cap_hashes]

    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise CfdTwinError(f"import output must be empty: {output}")
    surfaces = output / "production_surfaces"
    surfaces.mkdir(parents=True, exist_ok=True)
    for role in STEP_ROLES:
        shutil.copy2(files[role], output / role)
    shapes = {
        "outerDomain.stl": outer.val(),
        "inlet.stl": cq.Compound.makeCompound([inlet_face]),
        "outlet.stl": cq.Compound.makeCompound([outlet_face]),
        "outerWall.stl": cq.Compound.makeCompound(walls),
        **{f"battery_{name}.stl": solid for name, solid in ordered},
    }
    for filename, shape in shapes.items():
        cq.exporters.export(
            shape.scale(0.001), str(surfaces / filename),
            tolerance=0.00005, angularTolerance=0.1,
        )
    files_for_hash = [*STEP_ROLES, *(f"production_surfaces/{name}" for name in shapes)]
    hashes = {name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in files_for_hash}
    result = {
        "status": "CAD_EXPORTED_GEOMETRY_CHECKS_PASSED",
        "cad_geometry_checks_passed": True,
        "geometry_sha256": config.geometry_sha256,
        "imported_step": True,
        "checks": {"closed_solids": True, "23_matched_batteries": True, "volume_balance": True, "ports_identified": True},
        "measured": {
            "outer_solid_count": 1, "coolant_solid_count": 1,
            "battery_solid_count": 23,
            "outer_with_ports_volume_mm3": report["volumes_mm3"]["outer"],
            "coolant_volume_mm3": report["volumes_mm3"]["coolant"],
            "battery_total_volume_mm3": report["volumes_mm3"]["battery"],
            "inlet_area_mm2": report["inlet"]["area_mm2"],
            "outlet_area_mm2": report["outlet"]["area_mm2"],
        },
        "production_surfaces": {
            "length_unit": "m", "outer_domain": "production_surfaces/outerDomain.stl",
            "boundary_partition_files": ["production_surfaces/inlet.stl", "production_surfaces/outlet.stl", "production_surfaces/outerWall.stl"],
            "battery_count": 23,
            "battery_files": [f"production_surfaces/battery_C{i:02d}.stl" for i in range(1, 24)],
        },
        "files": files_for_hash,
        "file_sha256": hashes,
    }
    manifest = {
        "status": "CAD_EXPORTED_GEOMETRY_CHECKS_PASSED",
        "geometry_sha256": config.geometry_sha256,
        "units": "mm", "port_source": "imported_STEP_verified",
        "port_diameter_mm": report["inlet"]["diameter_mm"],
        "inlet_diameter_mm": report["inlet"]["diameter_mm"],
        "outlet_diameter_mm": report["outlet"]["diameter_mm"],
        "input_overrides": overrides,
        "report": report,
    }
    for name, value in (("cad_result.json", result), ("cad_manifest.json", manifest)):
        (output / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "resolved_config.json").write_text(config.to_json() + "\n", encoding="utf-8")
    return {"report": report, "config": config, "cad_dir": output}
