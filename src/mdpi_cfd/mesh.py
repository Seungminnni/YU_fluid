from __future__ import annotations

import json
import os
from pathlib import Path

from .config import ResolvedConfig
from .errors import DependencyUnavailable, MdpiCfdError


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _mesh_model(
    config: ResolvedConfig, cad_dir: str | Path, *, mesh_level: str
) -> dict[str, object]:
    levels = {
        "smoke": {
            "size_min_mm": 3.0,
            "size_max_mm": 8.0,
            "dist_min_mm": 0.8,
            "dist_max_mm": 2.5,
            "curvature_points": 6,
            "mesh_integral_relative_tolerance": 0.02,
        },
    }
    if mesh_level not in levels:
        raise MdpiCfdError(
            f"unsupported Gmsh mesh level {mesh_level!r}; only smoke is allowed. "
            "Production L0-L3 meshes use blockMesh + snappyHexMesh."
        )
    cad = Path(cad_dir).resolve()
    for filename in ("coolant.step", "battery_cells.step", "cad_result.json"):
        if not (cad / filename).is_file():
            raise MdpiCfdError(f"validated CAD input is missing: {cad / filename}")
    result = json.loads((cad / "cad_result.json").read_text(encoding="utf-8"))
    if not result.get("cad_geometry_checks_passed", False):
        raise MdpiCfdError("CAD geometry checks have not passed; refusing mesh generation")
    return {
        "schema_version": 1,
        "cad_directory": str(cad),
        "mesh_level": mesh_level,
        "geometry_sha256": config.geometry_sha256,
        "resolved_input_sha256": config.resolved_input_sha256,
        "expected_volume_count": 24,
        "expected_battery_count": 23,
        "inlet_end_z_mm": (config.enclosure.height_z_m + config.ports.external_length_m) * 1000,
        "outlet_end_z_mm": -config.ports.external_length_m * 1000,
        "expected_inlet_area_mm2": result["expected"]["inlet_area_mm2"],
        "expected_outlet_area_mm2": result["expected"]["outlet_area_mm2"],
        "expected_interface_area_mm2": result["expected"]["battery_interface_area_mm2"],
        "expected_fluid_volume_mm3": result["expected"]["coolant_volume_mm3"],
        "expected_battery_total_volume_mm3": result["expected"]["battery_total_volume_mm3"],
        "intended_use": "pipeline_smoke_test_only",
        "production_backend": "blockMesh+snappyHexMesh",
        "expected_openfoam_regions": ["coolant", "batterySolid"],
        "expected_battery_cell_zones": 23,
        **levels[mesh_level],
    }


def _script(model: dict[str, object]) -> str:
    payload = json.dumps(model, indent=2, sort_keys=True)
    return f'''from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

try:
    import gmsh
except ImportError as exc:
    raise SystemExit("Gmsh Python API is required to generate the volume mesh") from exc

MODEL = json.loads(r"""{payload}""")
OUTPUT = Path(__file__).resolve().parent
CAD = Path(MODEL["cad_directory"])

gmsh.initialize()
try:
    gmsh.option.setNumber("General.Terminal", 1)
    gmsh.model.add("mdpi_23cell_smoke")
    fluid_imported = gmsh.model.occ.importShapes(str(CAD / "coolant.step"))
    battery_imported = gmsh.model.occ.importShapes(str(CAD / "battery_cells.step"))
    if len(fluid_imported) != 1 or len(battery_imported) != MODEL["expected_battery_count"]:
        raise RuntimeError(
            f"STEP import count mismatch: fluid={{len(fluid_imported)}}, batteries={{len(battery_imported)}}"
        )
    gmsh.model.occ.fragment(fluid_imported, battery_imported, removeObject=True, removeTool=True)
    gmsh.model.occ.synchronize()

    volumes = [tag for dim, tag in gmsh.model.getEntities(3)]
    if len(volumes) != MODEL["expected_volume_count"]:
        raise RuntimeError(f"fragmented volume count {{len(volumes)}} != 24")
    masses = {{tag: gmsh.model.occ.getMass(3, tag) for tag in volumes}}
    fluid_tag = max(volumes, key=masses.get)
    battery_tags = sorted(tag for tag in volumes if tag != fluid_tag)
    if len(battery_tags) != MODEL["expected_battery_count"]:
        raise RuntimeError("battery volume classification failed")

    surface_owners = Counter()
    for volume in volumes:
        for dim, surface in gmsh.model.getBoundary([(3, volume)], oriented=False):
            if dim == 2:
                surface_owners[surface] += 1
    interface_surfaces = sorted(surface for surface, count in surface_owners.items() if count == 2)
    external_surfaces = sorted(surface for surface, count in surface_owners.items() if count == 1)
    inlet_surfaces, outlet_surfaces, wall_surfaces = [], [], []
    for surface in external_surfaces:
        xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(2, surface)
        if abs(zmin - MODEL["inlet_end_z_mm"]) < 1e-5 and abs(zmax - MODEL["inlet_end_z_mm"]) < 1e-5:
            inlet_surfaces.append(surface)
        elif abs(zmin - MODEL["outlet_end_z_mm"]) < 1e-5 and abs(zmax - MODEL["outlet_end_z_mm"]) < 1e-5:
            outlet_surfaces.append(surface)
        else:
            wall_surfaces.append(surface)
    if len(inlet_surfaces) != 1 or len(outlet_surfaces) != 1:
        raise RuntimeError(
            f"port face classification failed: inlet={{inlet_surfaces}}, outlet={{outlet_surfaces}}"
        )
    if len(interface_surfaces) < MODEL["expected_battery_count"]:
        raise RuntimeError("too few shared battery-fluid interface surfaces")
    measured_areas = {{
        "inlet_area_mm2": sum(gmsh.model.occ.getMass(2, tag) for tag in inlet_surfaces),
        "outlet_area_mm2": sum(gmsh.model.occ.getMass(2, tag) for tag in outlet_surfaces),
        "battery_interface_area_mm2": sum(gmsh.model.occ.getMass(2, tag) for tag in interface_surfaces),
    }}
    expected_areas = {{
        "inlet_area_mm2": MODEL["expected_inlet_area_mm2"],
        "outlet_area_mm2": MODEL["expected_outlet_area_mm2"],
        "battery_interface_area_mm2": MODEL["expected_interface_area_mm2"],
    }}
    area_relative_errors = {{
        key: abs(measured_areas[key] - expected_areas[key]) / expected_areas[key]
        for key in expected_areas
    }}
    if max(area_relative_errors.values()) > 1e-6:
        raise RuntimeError(f"fragmented CAD area mismatch: {{area_relative_errors}}")

    physical = {{}}
    def add_group(dim, tags, name):
        group = gmsh.model.addPhysicalGroup(dim, tags)
        gmsh.model.setPhysicalName(dim, group, name)
        physical[name] = {{"dimension": dim, "tag": group, "entity_count": len(tags)}}

    add_group(3, [fluid_tag], "coolant")
    add_group(3, battery_tags, "batterySolid")
    add_group(2, inlet_surfaces, "inlet")
    add_group(2, outlet_surfaces, "outlet")
    add_group(2, wall_surfaces, "walls")
    add_group(2, interface_surfaces, "batteryInterfaces")

    gmsh.model.mesh.setSize(gmsh.model.getEntities(0), MODEL["size_max_mm"])
    distance = gmsh.model.mesh.field.add("Distance")
    gmsh.model.mesh.field.setNumbers(
        distance,
        "SurfacesList",
        interface_surfaces + inlet_surfaces + outlet_surfaces,
    )
    gmsh.model.mesh.field.setNumber(distance, "Sampling", 50)
    threshold = gmsh.model.mesh.field.add("Threshold")
    gmsh.model.mesh.field.setNumber(threshold, "InField", distance)
    gmsh.model.mesh.field.setNumber(threshold, "SizeMin", MODEL["size_min_mm"])
    gmsh.model.mesh.field.setNumber(threshold, "SizeMax", MODEL["size_max_mm"])
    gmsh.model.mesh.field.setNumber(threshold, "DistMin", MODEL["dist_min_mm"])
    gmsh.model.mesh.field.setNumber(threshold, "DistMax", MODEL["dist_max_mm"])
    gmsh.model.mesh.field.setAsBackgroundMesh(threshold)
    gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", MODEL["curvature_points"])
    gmsh.option.setNumber("Mesh.Algorithm3D", 10)
    gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
    gmsh.option.setNumber("Mesh.Binary", 0)
    gmsh.model.mesh.generate(3)

    node_tags, node_coordinates, _ = gmsh.model.mesh.getNodes()
    points = {{
        int(tag): (
            float(node_coordinates[3 * index]),
            float(node_coordinates[3 * index + 1]),
            float(node_coordinates[3 * index + 2]),
        )
        for index, tag in enumerate(node_tags)
    }}

    def subtract(first, second):
        return tuple(a - b for a, b in zip(first, second, strict=True))

    def cross(first, second):
        return (
            first[1] * second[2] - first[2] * second[1],
            first[2] * second[0] - first[0] * second[2],
            first[0] * second[1] - first[1] * second[0],
        )

    def dot(first, second):
        return sum(a * b for a, b in zip(first, second, strict=True))

    def entity_mesh_measure(dimension, entity_tags):
        measure = 0.0
        element_count = 0
        for entity_tag in entity_tags:
            kinds, tags_by_kind, nodes_by_kind = gmsh.model.mesh.getElements(
                dimension, entity_tag
            )
            for kind, tags, connectivity in zip(
                kinds, tags_by_kind, nodes_by_kind, strict=True
            ):
                properties = gmsh.model.mesh.getElementProperties(kind)
                node_count = int(properties[3])
                if dimension == 2 and node_count != 3:
                    raise RuntimeError(
                        f"mesh integration supports linear triangles; got {{properties[0]}}"
                    )
                if dimension == 3 and node_count != 4:
                    raise RuntimeError(
                        f"mesh integration supports linear tetrahedra; got {{properties[0]}}"
                    )
                for start in range(0, len(connectivity), node_count):
                    vertices = [
                        points[int(node)]
                        for node in connectivity[start : start + node_count]
                    ]
                    first = subtract(vertices[1], vertices[0])
                    second = subtract(vertices[2], vertices[0])
                    if dimension == 2:
                        normal = cross(first, second)
                        measure += 0.5 * dot(normal, normal) ** 0.5
                    else:
                        third = subtract(vertices[3], vertices[0])
                        measure += abs(dot(first, cross(second, third))) / 6.0
                    element_count += 1
        return measure, element_count

    fluid_mesh_volume, fluid_tetra_count = entity_mesh_measure(3, [fluid_tag])
    battery_cell_mesh_volumes = []
    battery_tetra_count = 0
    for battery_tag in battery_tags:
        cell_volume, cell_tetra_count = entity_mesh_measure(3, [battery_tag])
        battery_cell_mesh_volumes.append(cell_volume)
        battery_tetra_count += cell_tetra_count
    inlet_mesh_area, inlet_triangle_count = entity_mesh_measure(2, inlet_surfaces)
    outlet_mesh_area, outlet_triangle_count = entity_mesh_measure(2, outlet_surfaces)
    interface_mesh_area, interface_triangle_count = entity_mesh_measure(
        2, interface_surfaces
    )
    mesh_integrals_mm = {{
        "inlet_patch_area_mm2": inlet_mesh_area,
        "outlet_patch_area_mm2": outlet_mesh_area,
        "battery_interface_area_mm2": interface_mesh_area,
        "fluid_volume_mm3": fluid_mesh_volume,
        "battery_total_volume_mm3": sum(battery_cell_mesh_volumes),
        "battery_cell_volumes_mm3": battery_cell_mesh_volumes,
        "battery_cell_count": len(battery_cell_mesh_volumes),
        "fluid_tetra_count": fluid_tetra_count,
        "battery_tetra_count": battery_tetra_count,
        "inlet_triangle_count": inlet_triangle_count,
        "outlet_triangle_count": outlet_triangle_count,
        "interface_triangle_count": interface_triangle_count,
    }}
    mesh_integrals_si = {{
        "inlet_patch_area_m2": inlet_mesh_area * 1e-6,
        "outlet_patch_area_m2": outlet_mesh_area * 1e-6,
        "battery_interface_area_m2": interface_mesh_area * 1e-6,
        "fluid_volume_m3": fluid_mesh_volume * 1e-9,
        "battery_total_volume_m3": sum(battery_cell_mesh_volumes) * 1e-9,
    }}
    mesh_integral_expected_mm = {{
        "inlet_patch_area_mm2": MODEL["expected_inlet_area_mm2"],
        "outlet_patch_area_mm2": MODEL["expected_outlet_area_mm2"],
        "battery_interface_area_mm2": MODEL["expected_interface_area_mm2"],
        "fluid_volume_mm3": MODEL["expected_fluid_volume_mm3"],
        "battery_total_volume_mm3": MODEL["expected_battery_total_volume_mm3"],
    }}
    mesh_integral_relative_errors = {{
        key: abs(mesh_integrals_mm[key] - expected) / expected
        for key, expected in mesh_integral_expected_mm.items()
    }}
    mesh_integral_gate_passed = (
        len(battery_cell_mesh_volumes) == MODEL["expected_battery_count"]
        and max(mesh_integral_relative_errors.values())
        <= MODEL["mesh_integral_relative_tolerance"]
    )
    if not mesh_integral_gate_passed:
        raise RuntimeError(
            "mesh-element area/volume integration gate failed: "
            f"{{mesh_integral_relative_errors}}"
        )

    element_types, element_tags, _ = gmsh.model.mesh.getElements(3)
    element_counts = {{
        gmsh.model.mesh.getElementProperties(kind)[0]: len(tags)
        for kind, tags in zip(element_types, element_tags, strict=True)
    }}
    volume_element_tags = [int(tag) for tags in element_tags for tag in tags]
    qualities = sorted(float(value) for value in gmsh.model.mesh.getElementQualities(volume_element_tags, "minSICN"))
    minimum_jacobians = sorted(
        float(value)
        for value in gmsh.model.mesh.getElementQualities(
            volume_element_tags, "minDetJac"
        )
    )
    def percentile(fraction):
        if not qualities:
            return None
        return qualities[round((len(qualities) - 1) * fraction)]
    quality = {{
        "metric": "minSICN",
        "minimum": qualities[0] if qualities else None,
        "p01": percentile(0.01),
        "p05": percentile(0.05),
        "median": percentile(0.5),
        "mean": sum(qualities) / len(qualities) if qualities else None,
        "p95": percentile(0.95),
        "nonpositive_count": sum(value <= 0 for value in qualities),
        "minimum_jacobian": minimum_jacobians[0] if minimum_jacobians else None,
        "inverted_or_degenerate_count": sum(
            value <= 0 for value in minimum_jacobians
        ),
    }}
    gmsh.write(str(OUTPUT / "mesh.msh"))
    gmsh.write(str(OUTPUT / "mesh.vtk"))
    mesh_files = ["mesh.msh", "mesh.vtk"]
    result = {{
        "schema_version": 1,
        "status": "MESH_GENERATED_REQUIRES_OPENFOAM_CHECKMESH",
        "intended_use": MODEL["intended_use"],
        "production_backend": MODEL["production_backend"],
        "production_mesh_ready": False,
        "mesh_generated": True,
        "mesh_level": MODEL["mesh_level"],
        "geometry_sha256": MODEL["geometry_sha256"],
        "node_count": len(node_tags),
        "volume_element_count": sum(element_counts.values()),
        "volume_element_types": element_counts,
        "cad_volume_count": len(volumes),
        "fluid_volume_count": 1,
        "battery_volume_count": len(battery_tags),
        "interface_surface_count": len(interface_surfaces),
        "physical_groups": physical,
        "measured_areas_mm2": measured_areas,
        "expected_areas_mm2": expected_areas,
        "area_relative_errors": area_relative_errors,
        "mesh_integrals_mm": mesh_integrals_mm,
        "mesh_integrals_si": mesh_integrals_si,
        "mesh_integral_expected_mm": mesh_integral_expected_mm,
        "mesh_integral_relative_errors": mesh_integral_relative_errors,
        "mesh_integral_gate_passed": mesh_integral_gate_passed,
        "element_quality": quality,
        "mesh_files": mesh_files,
        "file_sha256": {{
            filename: hashlib.sha256((OUTPUT / filename).read_bytes()).hexdigest()
            for filename in mesh_files
        }},
        "openfoam_checkmesh_passed": False,
    }}
    (OUTPUT / "mesh_result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\\n")
    print(json.dumps(result, indent=2, sort_keys=True))
finally:
    gmsh.finalize()
'''


def write_mesh_source_bundle(
    config: ResolvedConfig,
    cad_dir: str | Path,
    output_dir: str | Path,
    *,
    mesh_level: str = "smoke",
) -> dict[str, object]:
    output = Path(output_dir).resolve()
    model = _mesh_model(config, cad_dir, mesh_level=mesh_level)
    manifest = {
        **model,
        "status": "MESH_SOURCE_CREATED_NOT_EXECUTED",
        "backend": "Gmsh/OpenCASCADE",
        "intended_use": "pipeline_smoke_test_only",
        "production_backend": "blockMesh+snappyHexMesh",
        "production_mesh_ready": False,
        "mesh_generated": False,
        "openfoam_checkmesh_passed": False,
    }
    _atomic_write(output / "mesh_model.json", json.dumps(model, indent=2, sort_keys=True) + "\n")
    _atomic_write(output / "build_mesh.py", _script(model))
    _atomic_write(output / "mesh_manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def execute_mesh(
    config: ResolvedConfig,
    cad_dir: str | Path,
    output_dir: str | Path,
    *,
    mesh_level: str = "smoke",
) -> dict[str, object]:
    try:
        import gmsh  # noqa: F401
    except ImportError as exc:
        raise DependencyUnavailable(
            "Gmsh is unavailable; mesh source was not executed and no mesh was faked"
        ) from exc
    output = Path(output_dir).resolve()
    write_mesh_source_bundle(config, cad_dir, output, mesh_level=mesh_level)
    for filename in ("mesh_result.json", "mesh.msh", "mesh.vtk"):
        candidate = output / filename
        if candidate.is_file():
            candidate.unlink()
    script_path = output / "build_mesh.py"
    namespace = {"__file__": str(script_path), "__name__": "__main__"}
    exec(compile(script_path.read_text(encoding="utf-8"), str(script_path), "exec"), namespace)
    result = json.loads((output / "mesh_result.json").read_text(encoding="utf-8"))
    try:
        previews = render_mesh_slices(output / "mesh.vtk", output / "previews")
    except ImportError:
        previews = {"status": "PREVIEW_SKIPPED_VTK_UNAVAILABLE", "files": []}
    result["preview"] = previews
    _atomic_write(output / "mesh_result.json", json.dumps(result, indent=2, sort_keys=True) + "\n")
    manifest = json.loads((output / "mesh_manifest.json").read_text(encoding="utf-8"))
    manifest.update(result)
    _atomic_write(output / "mesh_manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return result


def _slice_svg(
    polydata, axes: tuple[int, int], title: str
) -> tuple[
    str,
    int,
    list[tuple[float, float, float, float]],
    list[list[tuple[float, float]]],
]:
    import vtk

    edges = vtk.vtkExtractEdges()
    edges.SetInputData(polydata)
    edges.Update()
    data = edges.GetOutput()
    bounds = data.GetBounds()
    first_min, first_max = bounds[axes[0] * 2], bounds[axes[0] * 2 + 1]
    second_min, second_max = bounds[axes[1] * 2], bounds[axes[1] * 2 + 1]
    span_first = max(first_max - first_min, 1e-9)
    span_second = max(second_max - second_min, 1e-9)
    canvas_w, canvas_h, padding = 960.0, 760.0, 54.0
    scale = min((canvas_w - 2 * padding) / span_first, (canvas_h - 2 * padding) / span_second)
    x_offset = (canvas_w - span_first * scale) / 2
    y_offset = (canvas_h - span_second * scale) / 2
    lines = data.GetLines()
    ids = vtk.vtkIdList()
    path_parts: list[str] = []
    segments: list[tuple[float, float, float, float]] = []
    battery_polygons: list[list[tuple[float, float]]] = []
    entities = polydata.GetCellData().GetArray("CellEntityIds")
    polygons = polydata.GetPolys()
    polygon_ids = vtk.vtkIdList()
    polygon_index = 0
    polygons.InitTraversal()
    while polygons.GetNextCell(polygon_ids):
        if entities is not None and round(entities.GetTuple1(polygon_index)) == 2:
            projected_polygon: list[tuple[float, float]] = []
            for index in range(polygon_ids.GetNumberOfIds()):
                point = polydata.GetPoint(polygon_ids.GetId(index))
                projected_polygon.append(
                    (
                        x_offset + (point[axes[0]] - first_min) * scale,
                        canvas_h - y_offset - (point[axes[1]] - second_min) * scale,
                    )
                )
            battery_polygons.append(projected_polygon)
        polygon_index += 1
    line_count = 0
    lines.InitTraversal()
    while lines.GetNextCell(ids):
        if ids.GetNumberOfIds() < 2:
            continue
        for index in range(ids.GetNumberOfIds() - 1):
            first = data.GetPoint(ids.GetId(index))
            second = data.GetPoint(ids.GetId(index + 1))
            x1 = x_offset + (first[axes[0]] - first_min) * scale
            y1 = canvas_h - y_offset - (first[axes[1]] - second_min) * scale
            x2 = x_offset + (second[axes[0]] - first_min) * scale
            y2 = canvas_h - y_offset - (second[axes[1]] - second_min) * scale
            path_parts.append(f"M{x1:.2f},{y1:.2f}L{x2:.2f},{y2:.2f}")
            segments.append((x1, y1, x2, y2))
            line_count += 1
    battery_paths = " ".join(
        "M" + "L".join(f"{x:.2f},{y:.2f}" for x, y in polygon) + "Z"
        for polygon in battery_polygons
    )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{canvas_w:.0f}" height="{canvas_h:.0f}" viewBox="0 0 {canvas_w:.0f} {canvas_h:.0f}">
<rect width="100%" height="100%" fill="#08111e"/>
<path d="{battery_paths}" fill="#7a4d1d" stroke="none" opacity="0.82"/>
<path d="{' '.join(path_parts)}" fill="none" stroke="#78cddd" stroke-width="0.48" opacity="0.82"/>
<text x="24" y="32" fill="#e9f2fa" font-family="sans-serif" font-size="18" font-weight="700">{title}</text>
<text x="24" y="54" fill="#91a8bc" font-family="sans-serif" font-size="12">actual Gmsh tetra section · no CFD field</text>
</svg>
'''
    return svg, line_count, segments, battery_polygons


def render_mesh_slices(mesh_vtk: str | Path, output_dir: str | Path) -> dict[str, object]:
    import vtk

    source = Path(mesh_vtk).resolve()
    if not source.is_file():
        raise MdpiCfdError(f"mesh VTK is missing: {source}")
    reader = vtk.vtkUnstructuredGridReader()
    reader.SetFileName(str(source))
    reader.Update()
    grid = reader.GetOutput()
    if grid.GetNumberOfCells() == 0:
        raise MdpiCfdError("mesh VTK contains no cells")
    tetrahedra = vtk.vtkExtractCellsByType()
    tetrahedra.SetInputData(grid)
    tetrahedra.AddCellType(vtk.VTK_TETRA)
    tetrahedra.Update()
    volume_grid = tetrahedra.GetOutput()
    bounds = volume_grid.GetBounds()
    specifications = (
        ("mesh_xy.svg", (0.0, 0.0, 1.0), (0, 1), "Figure 2 · XY mesh section"),
        ("mesh_zx.svg", (0.0, 1.0, 0.0), (0, 2), "Figure 2 · ZX mesh section"),
    )
    output = Path(output_dir).resolve()
    files: list[str] = []
    counts: dict[str, int] = {}
    for filename, normal, axes, title in specifications:
        origin = [
            (bounds[index * 2] + bounds[index * 2 + 1]) / 2 for index in range(3)
        ]
        plane = vtk.vtkPlane()
        plane.SetOrigin(*origin)
        plane.SetNormal(*normal)
        cutter = vtk.vtkCutter()
        cutter.SetCutFunction(plane)
        cutter.SetInputData(volume_grid)
        cutter.Update()
        svg, line_count, segments, battery_polygons = _slice_svg(
            cutter.GetOutput(), axes, title
        )
        _atomic_write(output / filename, svg)
        files.append(str((output / filename).resolve()))
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            pass
        else:
            raster = Image.new("RGB", (960, 760), "#08111e")
            draw = ImageDraw.Draw(raster)
            for polygon in battery_polygons:
                draw.polygon(polygon, fill="#7a4d1d")
            for x1, y1, x2, y2 in segments:
                draw.line((x1, y1, x2, y2), fill="#78cddd", width=1)
            png_path = output / filename.replace(".svg", ".png")
            raster.save(png_path)
            files.append(str(png_path.resolve()))
        counts[filename] = line_count
    return {
        "status": "MESH_SLICE_PREVIEWS_CREATED",
        "files": files,
        "line_segment_counts": counts,
        "source_dataset_cell_count": grid.GetNumberOfCells(),
        "source_tetra_count": volume_grid.GetNumberOfCells(),
        "synthetic_data": False,
    }
