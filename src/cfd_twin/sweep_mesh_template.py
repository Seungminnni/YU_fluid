"""Renderer for the standalone Gmsh structured-sweep generator."""

from __future__ import annotations

import json


def render_gmsh_sweep_generator(plan: dict[str, object]) -> str:
    payload = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
    return f'''#!/usr/bin/env python3
"""Generated fail-closed quad-source/hexa-sweep mesh builder."""
import json
import math
import os
import sys
from pathlib import Path

import gmsh

PLAN = json.loads({payload!r})
G = PLAN["geometry"]
os.chdir(Path(__file__).resolve().parent)
# OpenCASCADE bounding boxes are expanded by about 1e-7 m.  This tolerance
# classifies coplanar partition faces only; it is not a mesh-quality waiver.
TOL = 1e-6


def near_xy(point, centre, radius):
    return math.hypot(point[0] - centre[0], point[1] - centre[1]) < radius * 0.75


def radial_extent(dim, tag):
    box = gmsh.model.getBoundingBox(dim, tag)
    return max(box[3] - box[0], box[4] - box[1]) / 2


def surfaces_at(z):
    result = []
    for dim, tag in gmsh.model.getEntities(2):
        box = gmsh.model.getBoundingBox(dim, tag)
        if abs(box[2] - z) < TOL and abs(box[5] - z) < TOL:
            result.append((dim, tag))
    return result


def extrude_stage(source, dz, layers):
    print(f"extrude_stage start faces={{len(source)}} dz={{dz}} layers={{layers}}", flush=True)
    before = {{tag for dim, tag in gmsh.model.getEntities(3)}}
    gmsh.model.occ.extrude(source, 0, 0, dz, numElements=[layers], recombine=True)
    gmsh.model.occ.synchronize()
    after = {{tag for dim, tag in gmsh.model.getEntities(3)}}
    print(f"extrude_stage done new_volumes={{len(after - before)}}", flush=True)
    return sorted(after - before)


def add_physical(dim, tags, name):
    if not tags:
        raise RuntimeError(f"physical group {{name}} is empty")
    group = gmsh.model.addPhysicalGroup(dim, sorted(set(tags)))
    gmsh.model.setPhysicalName(dim, group, name)


def cap_footprint_weight(x, y):
    distance = min(
        math.hypot(x - item["x"], y - item["y"])
        for item in PLAN["battery_centres_m"]
    )
    wall_stack = sum(
        PLAN["first_layer_height_m"] * PLAN["wall_layer_growth"] ** index
        for index in range(PLAN["wall_layer_count"])
    )
    radius = G["battery_radius_m"] + wall_stack
    width = PLAN["cap_transition_width_m"]
    if distance <= radius:
        return 1.0
    if distance >= radius + width:
        return 0.0
    t = (distance - radius) / width
    if PLAN["cap_transition_easing"] != "linear":
        raise RuntimeError("unsupported cap transition easing")
    return 1.0 - t


def redistribute_cap_nodes(cap_heights):
    """Keep 6 um cap layers on batteries and expand them in remote coolant."""
    if PLAN.get("cap_transition_easing") == "none":
        return 0
    thin_distances = []
    cumulative = 0.0
    for height in cap_heights:
        cumulative += height
        thin_distances.append(cumulative)
    cap_total = thin_distances[-1]
    far_depth = PLAN["cap_transition_depth_m"]
    tags, coordinates, _ = gmsh.model.mesh.getNodes()
    moved = 0
    for index, node_tag in enumerate(tags):
        x = float(coordinates[3 * index])
        y = float(coordinates[3 * index + 1])
        z = float(coordinates[3 * index + 2])
        side = None
        distance = 0.0
        lower_distance = G["battery_base_m"] - z
        upper_distance = z - G["battery_top_m"]
        if -TOL <= lower_distance <= cap_total + TOL:
            side = "lower"
            distance = max(0.0, lower_distance)
        elif -TOL <= upper_distance <= cap_total + TOL:
            side = "upper"
            distance = max(0.0, upper_distance)
        if side is None or distance <= TOL:
            continue
        layer_index = min(
            range(len(thin_distances)),
            key=lambda item: abs(thin_distances[item] - distance),
        )
        thin_distance = thin_distances[layer_index]
        far_distance = far_depth * (layer_index + 1) / len(thin_distances)
        weight = cap_footprint_weight(x, y)
        target_distance = weight * thin_distance + (1.0 - weight) * far_distance
        target_z = (
            G["battery_base_m"] - target_distance
            if side == "lower"
            else G["battery_top_m"] + target_distance
        )
        if abs(target_z - z) > 1e-12:
            gmsh.model.mesh.setNode(int(node_tag), [x, y, target_z], [])
            moved += 1
    return moved


gmsh.initialize(sys.argv)
try:
    gmsh.option.setNumber("General.Terminal", 1)
    gmsh.option.setNumber("General.Verbosity", 2)
    gmsh.model.add("phi19_reference_guided_sweep")
    points = [gmsh.model.occ.addPoint(x, y, 0) for x, y in G["outline_polygon_m"]]
    edges = [gmsh.model.occ.addLine(points[i], points[(i + 1) % len(points)]) for i in range(len(points))]
    outer_loop = gmsh.model.occ.addCurveLoop(edges)
    layer_offsets = [0.0]
    layer_height = PLAN["first_layer_height_m"]
    cumulative = 0.0
    for layer_index in range(PLAN["wall_layer_count"]):
        cumulative += layer_height * PLAN["wall_layer_growth"] ** layer_index
    # Represent the full 11-layer stack as one mapped annular block.  The
    # radial transfinite curves carry the 11 graded intervals.  Creating one
    # OCC face per layer multiplied the topology into thousands of volumes
    # and triggered a native Gmsh 4.8 failure during 3-D generation.
    layer_offsets.append(cumulative)
    last_wall_height = (
        layer_height
        * PLAN["wall_layer_growth"] ** (PLAN["wall_layer_count"] - 1)
    )
    buffer_total = 0.0
    for buffer_index in range(PLAN["buffer_layer_count"]):
        buffer_total += (
            last_wall_height
            * PLAN["buffer_layer_growth"] ** (buffer_index + 1)
        )
    if PLAN["buffer_layer_count"]:
        layer_offsets.append(cumulative + buffer_total)
    battery_surfaces = []
    mapped_surfaces = []
    hole_loops = []
    circumferential_nodes = PLAN["battery_circumferential_intervals"] // 4 + 1
    for item in PLAN["battery_centres_m"]:
        centre = gmsh.model.occ.addPoint(item["x"], item["y"], 0)
        square_radius = G["battery_radius_m"] * 0.48
        square_points = [
            gmsh.model.occ.addPoint(
                item["x"] + square_radius * math.cos(math.pi / 4 + i * math.pi / 2),
                item["y"] + square_radius * math.sin(math.pi / 4 + i * math.pi / 2),
                0,
            )
            for i in range(4)
        ]
        square_lines = [
            gmsh.model.occ.addLine(square_points[i], square_points[(i + 1) % 4])
            for i in range(4)
        ]
        central = gmsh.model.occ.addPlaneSurface([
            gmsh.model.occ.addCurveLoop(square_lines)
        ])
        battery_surfaces.append(central)
        mapped_surfaces.append((central, square_lines, "central", square_points))

        ring_points = []
        ring_arcs = []
        for offset in layer_offsets:
            radius = G["battery_radius_m"] + offset
            radial_points = [
                gmsh.model.occ.addPoint(
                    item["x"] + radius * math.cos(math.pi / 4 + i * math.pi / 2),
                    item["y"] + radius * math.sin(math.pi / 4 + i * math.pi / 2),
                    0,
                )
                for i in range(4)
            ]
            arcs = [
                gmsh.model.occ.addCircleArc(radial_points[i], centre, radial_points[(i + 1) % 4])
                for i in range(4)
            ]
            ring_points.append(radial_points)
            ring_arcs.append(arcs)

        spokes = [
            gmsh.model.occ.addLine(square_points[i], ring_points[0][i])
            for i in range(4)
        ]
        for quadrant in range(4):
            boundary = [
                square_lines[quadrant],
                spokes[(quadrant + 1) % 4],
                -ring_arcs[0][quadrant],
                -spokes[quadrant],
            ]
            surface = gmsh.model.occ.addPlaneSurface([
                gmsh.model.occ.addCurveLoop(boundary)
            ])
            battery_surfaces.append(surface)
            mapped_surfaces.append((
                surface,
                boundary,
                "transition",
                [
                    square_points[quadrant],
                    square_points[(quadrant + 1) % 4],
                    ring_points[0][(quadrant + 1) % 4],
                    ring_points[0][quadrant],
                ],
            ))

        for layer in range(len(layer_offsets) - 1):
            radial_lines = [
                gmsh.model.occ.addLine(ring_points[layer][i], ring_points[layer + 1][i])
                for i in range(4)
            ]
            for quadrant in range(4):
                boundary = [
                    ring_arcs[layer][quadrant],
                    radial_lines[(quadrant + 1) % 4],
                    -ring_arcs[layer + 1][quadrant],
                    -radial_lines[quadrant],
                ]
                surface = gmsh.model.occ.addPlaneSurface([
                    gmsh.model.occ.addCurveLoop(boundary)
                ])
                battery_surfaces.append(surface)
                mapped_surfaces.append((
                    surface,
                    boundary,
                    "wall_ring" if layer == 0 else "buffer_ring",
                    [
                        ring_points[layer][quadrant],
                        ring_points[layer][(quadrant + 1) % 4],
                        ring_points[layer + 1][(quadrant + 1) % 4],
                        ring_points[layer + 1][quadrant],
                    ],
                ))
        hole_loops.append(gmsh.model.occ.addCurveLoop([
            -tag for tag in reversed(ring_arcs[-1])
        ]))

    fluid_surface = gmsh.model.occ.addPlaneSurface([outer_loop, *hole_loops])
    print("source OCC faces constructed", flush=True)
    gmsh.model.occ.synchronize()
    print("source OCC synchronized", flush=True)

    base = [(2, fluid_surface), *((2, tag) for tag in battery_surfaces)]
    if len(base) < 24:
        raise RuntimeError(
            f"source partition has only {{len(base)}} faces; battery partitions are missing"
        )
    h = PLAN["source_surface_characteristic_length_m"]
    gmsh.option.setNumber("Mesh.MeshSizeMin", min(h * 0.35, G["battery_radius_m"] / 4))
    gmsh.option.setNumber("Mesh.MeshSizeMax", h * 1.35)
    gmsh.option.setNumber("Mesh.Algorithm", PLAN["source_mesh_algorithm"])
    gmsh.option.setNumber("Mesh.Smoothing", 20)
    gmsh.option.setNumber("Mesh.RecombineAll", 1)
    gmsh.option.setNumber(
        "Mesh.RecombinationAlgorithm",
        PLAN["source_recombination_algorithm"],
    )
    for dim, tag in base:
        gmsh.model.mesh.setRecombine(dim, tag)
    for surface, boundary, kind, corners in mapped_surfaces:
        unique_curves = sorted(set(abs(tag) for tag in boundary))
        if kind == "central":
            for curve in unique_curves:
                gmsh.model.mesh.setTransfiniteCurve(curve, circumferential_nodes)
        elif kind == "transition":
            # Opposite edges of a transfinite quadrilateral must have equal
            # counts.  The square edge and circular arc are the tangential
            # pair; the two spokes are the radial pair.
            gmsh.model.mesh.setTransfiniteCurve(abs(boundary[0]), circumferential_nodes)
            gmsh.model.mesh.setTransfiniteCurve(abs(boundary[2]), circumferential_nodes)
            gmsh.model.mesh.setTransfiniteCurve(abs(boundary[1]), 6)
            gmsh.model.mesh.setTransfiniteCurve(abs(boundary[3]), 6)
        elif kind == "wall_ring":
            gmsh.model.mesh.setTransfiniteCurve(abs(boundary[0]), circumferential_nodes)
            gmsh.model.mesh.setTransfiniteCurve(abs(boundary[2]), circumferential_nodes)
            gmsh.model.mesh.setTransfiniteCurve(
                abs(boundary[1]),
                PLAN["wall_layer_count"] + 1,
                "Progression",
                PLAN["wall_layer_growth"],
            )
            gmsh.model.mesh.setTransfiniteCurve(
                abs(boundary[3]),
                PLAN["wall_layer_count"] + 1,
                "Progression",
                PLAN["wall_layer_growth"],
            )
        else:
            for curve in (abs(boundary[0]), abs(boundary[2])):
                gmsh.model.mesh.setTransfiniteCurve(curve, circumferential_nodes)
            for curve in (abs(boundary[1]), abs(boundary[3])):
                gmsh.model.mesh.setTransfiniteCurve(
                    curve,
                    PLAN["buffer_layer_count"] + 1,
                    "Progression",
                    PLAN["buffer_layer_growth"],
                )
        gmsh.model.mesh.setTransfiniteSurface(surface, "Left", corners)
        gmsh.model.mesh.setRecombine(2, surface)
    print("source meshing constraints assigned", flush=True)

    sections = {{item["name"]: item for item in PLAN["sections"]}}
    cap_heights = [
        PLAN["cap_first_layer_height_m"] * PLAN["cap_layer_growth"] ** index
        for index in range(PLAN["cap_layer_count"])
    ]
    cap_total = sum(cap_heights)
    lower_bulk_height = G["battery_base_m"] - cap_total
    if lower_bulk_height <= 0:
        raise RuntimeError("lower plenum is too short for the cap boundary layers")
    lower_volumes = extrude_stage(
        base,
        lower_bulk_height,
        PLAN["cap_bulk_intervals"],
    )
    current_z = lower_bulk_height
    # Approach the lower battery cap from the plenum with the largest layer
    # first so the final cell touching the cap has the requested 6 um height.
    for height in reversed(cap_heights):
        source = surfaces_at(current_z)
        lower_volumes.extend(extrude_stage(source, height, 1))
        current_z += height
    if abs(current_z - G["battery_base_m"]) > TOL:
        raise RuntimeError("lower cap layer stack does not end at battery base")
    lower_top = surfaces_at(G["battery_base_m"])
    body_volumes = extrude_stage(
        lower_top,
        G["battery_top_m"] - G["battery_base_m"],
        sections["battery_body"]["axial_intervals"],
    )
    body_top = surfaces_at(G["battery_top_m"])
    upper_volumes = []
    current_z = G["battery_top_m"]
    # Leave the upper cap with the 6 um cell first, then grow into the plenum.
    for height in cap_heights:
        source = surfaces_at(current_z)
        upper_volumes.extend(extrude_stage(source, height, 1))
        current_z += height
    upper_bulk_height = G["tank_height_m"] - current_z
    if upper_bulk_height <= 0:
        raise RuntimeError("upper plenum is too short for the cap boundary layers")
    upper_volumes.extend(extrude_stage(
        surfaces_at(current_z),
        upper_bulk_height,
        PLAN["cap_bulk_intervals"],
    ))

    bottom_candidates = [
        item for item in base
        if near_xy(
            gmsh.model.occ.getCenterOfMass(*item),
            G["outlet_center_xy_m"],
            G["outlet_radius_m"],
        )
        and radial_extent(*item) <= G["outlet_radius_m"] + TOL
    ]
    top_candidates = [
        item for item in surfaces_at(G["tank_height_m"])
        if near_xy(
            gmsh.model.occ.getCenterOfMass(*item),
            G["inlet_center_xy_m"],
            G["inlet_radius_m"],
        )
        and radial_extent(*item) <= G["inlet_radius_m"] + TOL
    ]
    if not bottom_candidates or not top_candidates:
        raise RuntimeError("port footprint partition is ambiguous")
    bottom_port_volumes = extrude_stage(
        bottom_candidates,
        sections["bottom_port"]["z_start_m"],
        sections["bottom_port"]["axial_intervals"],
    )
    top_port_volumes = extrude_stage(
        top_candidates,
        sections["top_port"]["z_end_m"] - sections["top_port"]["z_start_m"],
        sections["top_port"]["axial_intervals"],
    )

    print("classifying swept volumes", flush=True)
    battery_groups = {{item["id"]: [] for item in PLAN["battery_centres_m"]}}
    coolant = set(lower_volumes + upper_volumes + bottom_port_volumes + top_port_volumes)
    for tag in body_volumes:
        centre = gmsh.model.occ.getCenterOfMass(3, tag)
        matched = False
        for item in PLAN["battery_centres_m"]:
            if (
                near_xy(centre, (item["x"], item["y"]), G["battery_radius_m"])
                and radial_extent(3, tag) <= G["battery_radius_m"] + TOL
            ):
                battery_groups[item["id"]].append(tag)
                matched = True
                break
        if not matched:
            coolant.add(tag)
    print("swept volumes classified", flush=True)
    add_physical(3, list(coolant), "coolant")
    solid_tags = set()
    for name, tags in battery_groups.items():
        add_physical(3, tags, f"battery_{{name}}")
        solid_tags.update(tags)
    print("volume physical groups assigned", flush=True)

    inlet = []
    outlet = []
    outer_wall = []
    interfaces = {{name: [] for name in battery_groups}}
    tag_to_battery = {{tag: name for name, tags in battery_groups.items() for tag in tags}}
    print("classifying boundary and interface faces", flush=True)
    for dim, tag in gmsh.model.getEntities(2):
        upward, _ = gmsh.model.getAdjacencies(dim, tag)
        adjacent = set(int(value) for value in upward)
        box = gmsh.model.getBoundingBox(dim, tag)
        if len(adjacent) == 1:
            if (
                abs(box[2] - sections["bottom_port"]["z_start_m"]) < TOL
                and abs(box[5] - sections["bottom_port"]["z_start_m"]) < TOL
            ):
                outlet.append(tag)
            elif (
                abs(box[2] - sections["top_port"]["z_end_m"]) < TOL
                and abs(box[5] - sections["top_port"]["z_end_m"]) < TOL
            ):
                inlet.append(tag)
            else:
                outer_wall.append(tag)
        elif len(adjacent) == 2 and adjacent & solid_tags and adjacent & coolant:
            solid = next(iter(adjacent & solid_tags))
            interfaces[tag_to_battery[solid]].append(tag)
    print("boundary and interface faces classified", flush=True)
    add_physical(2, inlet, "inlet")
    add_physical(2, outlet, "outlet")
    add_physical(2, outer_wall, "outerWall")
    for name, tags in interfaces.items():
        # Match the production CHT case contract. splitMeshRegions appends
        # direction suffixes while the field dictionaries match this prefix.
        add_physical(2, tags, f"battery_{{name}}_interface")
    print("surface physical groups assigned", flush=True)

    gmsh.option.setNumber("Mesh.ElementOrder", 1)
    print("volume mesh generation starting", flush=True)
    gmsh.model.mesh.generate(3)
    print("volume mesh generation complete", flush=True)
    moved_cap_nodes = redistribute_cap_nodes(cap_heights)
    print(f"cap transition nodes redistributed={{moved_cap_nodes}}", flush=True)
    bad_2d = []
    for element_type in gmsh.model.mesh.getElementTypes(2):
        element_name = gmsh.model.mesh.getElementProperties(element_type)[0]
        if "Quadrilateral" not in element_name:
            bad_2d.append(element_name)
    bad_3d = []
    cell_count = 0
    for element_type in gmsh.model.mesh.getElementTypes(3):
        element_name = gmsh.model.mesh.getElementProperties(element_type)[0]
        tags, _ = gmsh.model.mesh.getElementsByType(element_type)
        cell_count += len(tags)
        if "Hexahedron" not in element_name:
            bad_3d.append(element_name)
    low = int(PLAN["target_cells"] * (1 - PLAN["target_tolerance_fraction"]))
    high = int(PLAN["target_cells"] * (1 + PLAN["target_tolerance_fraction"]))
    if bad_2d or bad_3d:
        raise RuntimeError(f"non-sweep elements rejected: 2D={{bad_2d}}, 3D={{bad_3d}}")
    if not low <= cell_count <= high:
        raise RuntimeError(f"cell target rejected: {{cell_count}} not in [{{low}}, {{high}}]")
    gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
    gmsh.option.setNumber("Mesh.Binary", 0)
    # MSH2 stores physical tags on volume elements only when SaveAll is off.
    # Saving unclassified lower-dimensional entities would rewrite the first
    # tag as zero and collapse all OpenFOAM patches/cellZones into region 0.
    gmsh.option.setNumber("Mesh.SaveAll", 0)
    gmsh.write("sweep_mesh.msh")
    with open("sweep_generation_report.json", "w", encoding="utf-8") as stream:
        json.dump(
            {{
                "status": "SWEEP_GENERATION_PASSED",
                "cells": cell_count,
                "all_source_faces_quadrilateral": True,
                "all_volume_cells_hexahedral": True,
                "battery_regions": len(battery_groups),
                "cap_transition_nodes_redistributed": moved_cap_nodes,
            }},
            stream,
            indent=2,
        )
except Exception as exc:
    with open("sweep_generation_failure.json", "w", encoding="utf-8") as stream:
        json.dump(
            {{
                "status": "SWEEP_GENERATION_FAILED",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "plan": "sweep_mesh_plan.json",
            }},
            stream,
            indent=2,
        )
    raise
finally:
    gmsh.finalize()
'''
