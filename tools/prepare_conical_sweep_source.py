#!/usr/bin/env python3
"""Create a fresh all-hexa sweep source for the Ø33→Ø22 inlet frustum."""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = ROOT / "build/mesh_sources/phi19_600k_reference_guided_sweep_v4_ba5b0cb9e1b34028bc5350fa6dbec7c1"


def recommended_frustum_axial_intervals(plan: dict) -> tuple[int, dict]:
    """Choose a mapped-sweep count from geometry instead of cell count alone.

    For an expanding circular sweep, axial over-refinement makes the cells
    thinner than the far-end circumferential spacing and worsens
    non-orthogonality.  Keep the axial length near 1.25 times that spacing.
    This rule selects 12 intervals for the 32 mm, D33, 48-sector inlet.
    """
    geometry = plan["geometry"]
    top_port = next(section for section in plan["sections"] if section["name"] == "top_port")
    length = float(top_port["z_end_m"]) - float(geometry["tank_height_m"])
    sectors = int(plan["inlet_circumferential_intervals"])
    far_arc = 2.0 * math.pi * float(geometry["inlet_outer_radius_m"]) / sectors
    target_axial = 1.25 * far_arc
    intervals = max(8, int(round(length / target_axial)))
    return intervals, {
        "rule": "axial_cell_length_about_1p25_times_far_end_circumferential_spacing",
        "frustum_length_m": length,
        "far_end_circumferential_spacing_m": far_arc,
        "target_axial_cell_length_m": target_axial,
        "recommended_axial_intervals": intervals,
        "observed_quality_margin_target_deg": 63.5,
        "hard_max_non_orthogonality_deg": 65.0,
    }


RESHAPE_FUNCTION = r'''
def reshape_conical_inlet_nodes():
    """Expand the swept top port linearly from Ø22 at the tank to Ø33 outside."""
    centre_x, centre_y = G["inlet_center_xy_m"]
    z_start = G["tank_height_m"]
    z_end = next(item["z_end_m"] for item in PLAN["sections"] if item["name"] == "top_port")
    source_radius = G["inlet_source_radius_m"]
    near_radius = G["inlet_radius_m"]
    far_radius = G["inlet_outer_radius_m"]
    if not (z_end > z_start and far_radius > near_radius >= source_radius > 0):
        raise RuntimeError("invalid conical inlet dimensions")
    tags, coordinates, _ = gmsh.model.mesh.getNodes()
    moved = 0
    for index, tag in enumerate(tags):
        x, y, z = coordinates[3 * index:3 * index + 3]
        radius = math.hypot(x - centre_x, y - centre_y)
        if z < z_start - TOL or z > z_end + TOL or radius > source_radius + TOL:
            continue
        fraction = min(1.0, max(0.0, (z - z_start) / (z_end - z_start)))
        blend = PLAN["inlet_taper_smoothstep_blend"]
        smooth_fraction = fraction * fraction * (3.0 - 2.0 * fraction)
        tapered_fraction = (1.0 - blend) * fraction + blend * smooth_fraction
        target_radius = near_radius + tapered_fraction * (far_radius - near_radius)
        scale = target_radius / source_radius
        gmsh.model.mesh.setNode(int(tag), [centre_x + (x - centre_x) * scale,
                                           centre_y + (y - centre_y) * scale,
                                           z], [])
        moved += 1
    if moved == 0:
        raise RuntimeError("no conical inlet nodes were reshaped")
    return moved

'''


def transform_generator(source: str) -> str:
    source, count = re.subn(
        r"^PLAN = json\.loads\(.*\)$",
        'PLAN = json.loads(Path("sweep_mesh_plan.json").read_text(encoding="utf-8"))',
        source,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise RuntimeError("embedded plan assignment was not found exactly once")
    source = source.replace(
        "    mapped_surfaces = []\n    hole_loops = []",
        "    mapped_surfaces = []\n    inlet_mapped_surface_tags = set()\n    hole_loops = []",
        1,
    )
    near_helper = '''def near_xy(point, centre, radius):
    return math.hypot(point[0] - centre[0], point[1] - centre[1]) < radius * 0.75
'''
    near_replacement = near_helper + '''

def near_port_xy(point, centre, radius):
    return math.hypot(point[0] - centre[0], point[1] - centre[1]) <= radius + TOL
'''
    if source.count(near_helper) != 1:
        raise RuntimeError("near-xy helper marker is ambiguous")
    source = source.replace(near_helper, near_replacement)
    inlet_selector = '''        if near_xy(
            gmsh.model.occ.getCenterOfMass(*item),
            G["inlet_center_xy_m"],
            G["inlet_radius_m"],
        )'''
    inlet_selector_replacement = inlet_selector.replace("if near_xy(", "if near_port_xy(")
    if source.count(inlet_selector) != 1:
        raise RuntimeError("inlet selector marker is ambiguous")
    source = source.replace(inlet_selector, inlet_selector_replacement)
    loop_marker = '    for item in PLAN["battery_centres_m"]:\n        centre = gmsh.model.occ.addPoint(item["x"], item["y"], 0)'
    loop_replacement = '    for item in PLAN["battery_centres_m"]:\n        is_inlet = item["id"] == PLAN["inlet_battery_id"]\n        item_surface_start = len(battery_surfaces)\n        centre = gmsh.model.occ.addPoint(item["x"], item["y"], 0)'
    if source.count(loop_marker) != 1:
        raise RuntimeError("battery source-loop marker is ambiguous")
    source = source.replace(loop_marker, loop_replacement)
    hole_marker = '''        hole_loops.append(gmsh.model.occ.addCurveLoop([
            -tag for tag in reversed(ring_arcs[-1])
        ]))'''
    hole_replacement = '''        hole_boundary_arcs = ring_arcs[-1]
        if is_inlet:
            port_radius = G["inlet_radius_m"]
            port_points = [
                gmsh.model.occ.addPoint(
                    item["x"] + port_radius * math.cos(math.pi / 4 + i * math.pi / 2),
                    item["y"] + port_radius * math.sin(math.pi / 4 + i * math.pi / 2),
                    0,
                )
                for i in range(4)
            ]
            port_arcs = [
                gmsh.model.occ.addCircleArc(port_points[i], centre, port_points[(i + 1) % 4])
                for i in range(4)
            ]
            port_radial_lines = [
                gmsh.model.occ.addLine(ring_points[-1][i], port_points[i])
                for i in range(4)
            ]
            for quadrant in range(4):
                boundary = [
                    ring_arcs[-1][quadrant],
                    port_radial_lines[(quadrant + 1) % 4],
                    -port_arcs[quadrant],
                    -port_radial_lines[quadrant],
                ]
                surface = gmsh.model.occ.addPlaneSurface([
                    gmsh.model.occ.addCurveLoop(boundary)
                ])
                battery_surfaces.append(surface)
                mapped_surfaces.append((
                    surface,
                    boundary,
                    "port_ring",
                    [
                        ring_points[-1][quadrant],
                        ring_points[-1][(quadrant + 1) % 4],
                        port_points[(quadrant + 1) % 4],
                        port_points[quadrant],
                    ],
                ))
            hole_boundary_arcs = port_arcs
        if is_inlet:
            inlet_mapped_surface_tags.update(battery_surfaces[item_surface_start:])
        hole_loops.append(gmsh.model.occ.addCurveLoop([
            -tag for tag in reversed(hole_boundary_arcs)
        ]))'''
    if source.count(hole_marker) != 1:
        raise RuntimeError("source-hole marker is ambiguous")
    source = source.replace(hole_marker, hole_replacement)
    wall_marker = '        elif kind == "wall_ring":\n'
    port_constraints = '''        elif kind == "port_ring":
            gmsh.model.mesh.setTransfiniteCurve(abs(boundary[0]), circumferential_nodes)
            gmsh.model.mesh.setTransfiniteCurve(abs(boundary[2]), circumferential_nodes)
            gmsh.model.mesh.setTransfiniteCurve(abs(boundary[1]), 3)
            gmsh.model.mesh.setTransfiniteCurve(abs(boundary[3]), 3)
'''
    if source.count(wall_marker) != 1:
        raise RuntimeError("wall-ring constraint marker is ambiguous")
    source = source.replace(wall_marker, port_constraints + wall_marker)
    constraints_start = '    for surface, boundary, kind, corners in mapped_surfaces:\n'
    constraints_end = '    print("source meshing constraints assigned", flush=True)'
    start_index = source.index(constraints_start)
    end_index = source.index(constraints_end, start_index)
    constraint_block = source[start_index:end_index]
    constraint_block = constraint_block.replace(
        "circumferential_nodes", "local_circumferential_nodes"
    )
    constraint_block = constraint_block.replace(
        constraints_start,
        constraints_start
        + '        local_circumferential_nodes = (PLAN["inlet_circumferential_intervals"] // 4 + 1 if surface in inlet_mapped_surface_tags else circumferential_nodes)\n',
        1,
    )
    source = source[:start_index] + constraint_block + source[end_index:]
    marker = "gmsh.initialize(sys.argv)"
    if source.count(marker) != 1:
        raise RuntimeError("gmsh initialization marker is ambiguous")
    source = source.replace(marker, RESHAPE_FUNCTION + marker)
    generate = '    gmsh.model.mesh.generate(3)\n    print("volume mesh generation complete", flush=True)'
    replacement = generate + '\n    conical_nodes_reshaped = reshape_conical_inlet_nodes()\n    print(f"conical inlet nodes reshaped={conical_nodes_reshaped}", flush=True)'
    if source.count(generate) != 1:
        raise RuntimeError("mesh generation marker is ambiguous")
    source = source.replace(generate, replacement)
    band = '''    low = int(PLAN["target_cells"] * (1 - PLAN["target_tolerance_fraction"]))
    high = int(PLAN["target_cells"] * (1 + PLAN["target_tolerance_fraction"]))'''
    explicit_band = '''    low = int(PLAN["minimum_cells"])
    high = int(PLAN["maximum_cells"])'''
    if source.count(band) != 1:
        raise RuntimeError("cell-band marker is ambiguous")
    source = source.replace(band, explicit_band)
    report_marker = '                "cap_transition_nodes_redistributed": moved_cap_nodes,'
    report_replacement = report_marker + '''
                "conical_inlet_nodes_reshaped": conical_nodes_reshaped,
                "inlet_near_diameter_m": 2 * G["inlet_radius_m"],
                "inlet_far_diameter_m": 2 * G["inlet_outer_radius_m"],'''
    if source.count(report_marker) != 1:
        raise RuntimeError("report marker is ambiguous")
    return source.replace(report_marker, report_replacement)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--attempt-id", default=uuid.uuid4().hex)
    parser.add_argument(
        "--refined-pressure-profile",
        action="store_true",
        help=(
            "Refine the battery-cap/plenum cross-section for the D33, 0.4 m/s "
            "pressure-conditioning retry without changing the 6 um, 11-layer stack."
        ),
    )
    parser.add_argument(
        "--refined-pressure-axial-profile",
        action="store_true",
        help=(
            "Extend the pressure-path refinement by splitting the 68 mm battery "
            "body from 12 to 24 axial intervals.  The D33-to-D22 frustum, 6 um "
            "first layer, 11 layers, and all physical boundary conditions stay fixed."
        ),
    )
    args = parser.parse_args()
    if args.refined_pressure_profile and args.refined_pressure_axial_profile:
        parser.error("select only one refined pressure profile")
    profile_name = (
        "phi19_conical_refined_pressure_axial_all_hexa"
        if args.refined_pressure_axial_profile
        else
        "phi19_conical_refined_pressure_all_hexa"
        if args.refined_pressure_profile
        else "phi19_conical_660k_all_hexa"
    )
    target = ROOT / "build/mesh_sources" / f"{profile_name}_{args.attempt_id}"
    if target.exists():
        raise FileExistsError(f"fresh target already exists: {target}")
    target.mkdir(parents=True)
    for name in ("Dockerfile", "run_sweep_mesh.sh", "set_openfoam_patch_types.py"):
        shutil.copy2(args.template / name, target / name)
    shutil.copytree(args.template / "system", target / "system")
    shutil.copytree(args.template / "constant", target / "constant")
    plan = json.loads((args.template / "sweep_mesh_plan.json").read_text(encoding="utf-8"))
    plan.update({
        "target_cells": 660000,
        "minimum_cells": 600000,
        "maximum_cells": 690000,
        "estimated_cells": 662670,
        "estimated_target_error_fraction": abs(662670 - 660000) / 660000,
        "attempt_id": args.attempt_id,
        "variant": "inlet_frustum_d33_to_d22_outlet_d19",
    })
    plan["geometry"].update({
        "inlet_source_radius_m": 0.011,
        "inlet_radius_m": 0.011,
        "inlet_outer_radius_m": 0.0165,
        "inlet_shape": "frustum",
    })
    plan["inlet_battery_id"] = "C05"
    plan["inlet_circumferential_intervals"] = 48
    frustum_intervals, mesh_design_rule = recommended_frustum_axial_intervals(plan)
    # Preserve the requested straight Ø33 -> Ø22 frustum exactly.  The
    # previous 20% smoothstep blend slightly curved the wall and still left
    # the first taper cells close to the non-orthogonality gate.  Instead,
    # refine the axial discretisation of the straight taper and recover the
    # cell budget from the battery-body axial spacing.  This changes only
    # the mesh; the physical inlet boundary remains a fixed 0.4 m/s over the
    # actual D33 inlet face.
    plan["inlet_taper_smoothstep_blend"] = 0.0
    for section in plan["sections"]:
        if section["name"] == "top_port":
            section["axial_intervals"] = frustum_intervals
        elif section["name"] == "battery_body":
            section["axial_intervals"] = 12
        elif section["name"] == "lower_plenum":
            section["axial_intervals"] = 13
    plan["estimated_cells"] = 667055
    plan["estimated_target_error_fraction"] = abs(667055 - 660000) / 660000
    plan["mesh_design_rule"] = mesh_design_rule
    plan["mesh_correction"] = {
        "reason": "reduce pressure-sensitive non-orthogonality at the straight frustum/tank junction",
        "top_port_axial_intervals": frustum_intervals,
        "battery_body_axial_intervals": 12,
        "lower_plenum_axial_intervals": 13,
        "geometry_changed": False,
        "boundary_condition_changed": False,
        "inlet_velocity_m_s": 0.4,
    }
    if args.refined_pressure_profile or args.refined_pressure_axial_profile:
        # Keep the validated 6 um/11-layer wall-normal stack.  Reduce the
        # extreme aspect ratio by splitting the long tangential direction
        # around every battery cap and by reducing the unstructured core
        # target size.  The conical port remains at 12 axial intervals: the
        # recorded 16/24/32 interval trials increased non-orthogonality.
        plan.update({
            "target_cells": 1200000,
            "minimum_cells": 750000,
            "maximum_cells": 1600000,
            "estimated_cells": 1200000,
            "estimated_target_error_fraction": 0.0,
            "variant": "inlet_frustum_d33_to_d22_outlet_d19_refined_pressure_path",
            "battery_circumferential_intervals": 72,
            "inlet_circumferential_intervals": 72,
            "source_surface_characteristic_length_m": (
                float(plan["source_surface_characteristic_length_m"]) * 0.75
            ),
        })
        plan["mesh_design_rule"]["recommended_axial_intervals"] = frustum_intervals
        plan["mesh_correction"].update({
            "reason": (
                "split long tangential cells around all battery caps and the upper "
                "plenum pressure path while preserving the validated wall-normal stack"
            ),
            "battery_circumferential_intervals": 72,
            "inlet_circumferential_intervals": 72,
            "source_surface_size_scale": 0.75,
            "target_max_aspect_ratio": 200.0,
            "cell_count_constraint_relaxed_by_user": True,
        })
    if args.refined_pressure_axial_profile:
        for section in plan["sections"]:
            if section["name"] == "battery_body":
                section["axial_intervals"] = 24
        plan.update({
            "target_cells": 2100000,
            "minimum_cells": 1500000,
            "maximum_cells": 3000000,
            "estimated_cells": 2100000,
            "estimated_target_error_fraction": 0.0,
            "variant": (
                "inlet_frustum_d33_to_d22_outlet_d19_"
                "refined_pressure_axial_path"
            ),
        })
        plan["mesh_correction"].update({
            "reason": (
                "retain the passed circumferential refinement and split the long "
                "battery-body sweep cells that left max aspect ratio 425.323"
            ),
            "battery_body_axial_intervals": 24,
            "previous_battery_body_axial_intervals": 12,
            "observed_previous_max_aspect_ratio": 425.323,
            "target_max_aspect_ratio": 220.0,
        })
    (target / "sweep_mesh_plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    generator = (args.template / "generate_sweep_mesh.py").read_text(encoding="utf-8")
    (target / "generate_sweep_mesh.py").write_text(transform_generator(generator), encoding="utf-8")
    (target / "attempt_id.txt").write_text(args.attempt_id + "\n", encoding="utf-8")
    manifest = {
        "status": "CONICAL_SWEEP_SOURCE_PREPARED",
        "attempt_id": args.attempt_id,
        "template": str(args.template),
        "target": str(target),
        "geometry": {"inlet_far_diameter_mm": 33, "inlet_near_diameter_mm": 22, "outlet_diameter_mm": 19},
        "boundary_conditions": {"inlet_type": "fixedValue", "inlet_velocity_m_s": 0.4},
        "cell_gate": {
            "target": plan["target_cells"],
            "minimum": plan["minimum_cells"],
            "maximum": plan["maximum_cells"],
        },
        "layer_gate": {"first_layer_um": 6, "layers": 11},
    }
    if args.refined_pressure_profile:
        manifest["quality_targets"] = {
            "all_hexa": True,
            "max_aspect_ratio": 200.0,
            "max_non_orthogonality_deg": 65.0,
        }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
