"""검증된 형상 입력을 STEP과 snappyHexMesh용 STL로 변환한다.

탱크 외곽과 두 포트를 합친 뒤 23개 원통을 빼서 하나의 냉각수 체적을 만든다.
원통 자체는 battery solid로 보존하며, 해석 체적·면적과 CAD 측정값이 맞는지
검사한 형상만 생산 메시 단계로 넘긴다.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

from .config import ResolvedConfig
from .errors import DependencyUnavailable
from .geometry import cell_placements, geometry_report


def _cad_model(config: ResolvedConfig) -> dict[str, object]:
    """SI 입력을 CAD 작업 단위 mm와 해석식 기반 검증값으로 정리한다."""

    placements = cell_placements(config)
    report = geometry_report(config)
    cell_radius_mm = config.cells.diameter_m * 500
    cell_length_mm = config.cells.length_m * 1000
    inlet_radius_mm = config.ports.inlet_diameter_m * 500
    outlet_radius_mm = config.ports.outlet_diameter_m * 500
    port_length_mm = config.ports.external_length_m * 1000
    cell_volume_mm3 = math.pi * cell_radius_mm**2 * cell_length_mm
    inlet_volume_mm3 = math.pi * inlet_radius_mm**2 * port_length_mm
    outlet_volume_mm3 = math.pi * outlet_radius_mm**2 * port_length_mm
    enclosure_volume_mm3 = report["enclosure_profile"]["gross_extruded_volume_m3"] * 1e9
    return {
        "units": "mm",
        "enclosure_profile": config.enclosure.profile,
        "outline_mm": [[x * 1000, y * 1000] for x, y in config.enclosure.outline_polygon_m],
        "height_mm": config.enclosure.height_z_m * 1000,
        "cell_diameter_mm": config.cells.diameter_m * 1000,
        "cell_length_mm": config.cells.length_m * 1000,
        "cell_base_z_mm": (config.enclosure.height_z_m - config.cells.length_m) * 500,
        "cells": [
            {"id": item.cell_id, "x_mm": item.center_x_m * 1000, "y_mm": item.center_y_m * 1000}
            for item in placements
        ],
        "ports": {
            # ``diameter_mm`` is retained for old manifests. New CAD scripts
            # always use the independent inlet/outlet values below.
            "diameter_mm": config.ports.inlet_diameter_m * 1000,
            "inlet_diameter_mm": config.ports.inlet_diameter_m * 1000,
            "outlet_diameter_mm": config.ports.outlet_diameter_m * 1000,
            "external_length_mm": config.ports.external_length_m * 1000,
            "inlet": {
                "face": config.ports.inlet_face,
                "center_xy_mm": [value * 1000 for value in config.ports.inlet_center_xy_m],
            },
            "outlet": {
                "face": config.ports.outlet_face,
                "center_xy_mm": [value * 1000 for value in config.ports.outlet_center_xy_m],
            },
            "source": config.ports.center_source,
        },
        "geometry_sha256": config.geometry_sha256,
        "resolved_input_sha256": config.resolved_input_sha256,
        "expected": {
            "outer_with_ports_volume_mm3": enclosure_volume_mm3 + inlet_volume_mm3 + outlet_volume_mm3,
            "battery_total_volume_mm3": config.cells.count * cell_volume_mm3,
            "coolant_volume_mm3": enclosure_volume_mm3 + inlet_volume_mm3 + outlet_volume_mm3 - config.cells.count * cell_volume_mm3,
            "inlet_area_mm2": math.pi * inlet_radius_mm**2,
            "outlet_area_mm2": math.pi * outlet_radius_mm**2,
            "battery_interface_area_mm2": config.cells.count
            * (2 * math.pi * cell_radius_mm * cell_length_mm + 2 * math.pi * cell_radius_mm**2),
        },
    }


def _script(model: dict[str, object]) -> str:
    """CadQuery에서 실행될 재현 가능한 CAD 생성 스크립트를 만든다."""

    payload = json.dumps(model, indent=2, sort_keys=True)
    return f'''from __future__ import annotations

import json
import hashlib
from pathlib import Path

try:
    import cadquery as cq
except ImportError as exc:
    raise SystemExit("CadQuery is required to export STEP/STL") from exc

MODEL = json.loads(r"""{payload}""")
OUTPUT = Path(__file__).resolve().parent

outline = [tuple(point) for point in MODEL["outline_mm"]]
height = MODEL["height_mm"]
cell_radius = MODEL["cell_diameter_mm"] / 2
cell_length = MODEL["cell_length_mm"]
cell_base_z = MODEL["cell_base_z_mm"]
inlet_radius = MODEL["ports"]["inlet_diameter_mm"] / 2
outlet_radius = MODEL["ports"]["outlet_diameter_mm"] / 2
port_length = MODEL["ports"]["external_length_mm"]

# 22점 상면을 z 방향으로 압출하고 상·하 원형 포트를 합쳐 외부 용기를 만든다.
enclosure = cq.Workplane("XY").polyline(outline).close().extrude(height)
inlet_x, inlet_y = MODEL["ports"]["inlet"]["center_xy_mm"]
outlet_x, outlet_y = MODEL["ports"]["outlet"]["center_xy_mm"]
def build_port(port, radius):
    face = port["face"]
    if face == "top":
        origin_z = height
        extrusion = port_length
        end_z = height + port_length
    elif face == "bottom":
        origin_z = 0
        extrusion = -port_length
        end_z = -port_length
    else:
        raise ValueError(f"unsupported port face: {{face}}")
    center_x, center_y = port["center_xy_mm"]
    solid = (
        cq.Workplane("XY", origin=(0, 0, origin_z))
        .center(center_x, center_y).circle(radius).extrude(extrusion)
    )
    return solid, end_z

inlet, inlet_end_z = build_port(MODEL["ports"]["inlet"], inlet_radius)
outlet, outlet_end_z = build_port(MODEL["ports"]["outlet"], outlet_radius)
outer_with_ports = enclosure.union(inlet).union(outlet)

# 냉각수 영역 = 포트를 포함한 탱크 체적 - 23개 배터리 원통 체적.
cell_solids = []
coolant = outer_with_ports
for cell in MODEL["cells"]:
    solid = (
        cq.Workplane("XY", origin=(0, 0, cell_base_z))
        .center(cell["x_mm"], cell["y_mm"])
        .circle(cell_radius).extrude(cell_length)
    )
    cell_solids.append(solid.val())
    coolant = coolant.cut(solid)

cells_compound = cq.Compound.makeCompound(cell_solids)
outer_faces = outer_with_ports.faces().vals()
inlet_end_faces = [face for face in outer_faces if abs(face.Center().z - inlet_end_z) <= 1e-7]
outlet_end_faces = [face for face in outer_faces if abs(face.Center().z - outlet_end_z) <= 1e-7]
end_face_hashes = {{face.hashCode() for face in inlet_end_faces + outlet_end_faces}}
outer_wall_faces = [face for face in outer_faces if face.hashCode() not in end_face_hashes]
measured = {{
    "outer_solid_count": outer_with_ports.solids().size(),
    "coolant_solid_count": coolant.solids().size(),
    "battery_solid_count": len(cell_solids),
    "outer_with_ports_volume_mm3": outer_with_ports.val().Volume(),
    "coolant_volume_mm3": coolant.val().Volume(),
    "battery_total_volume_mm3": sum(solid.Volume() for solid in cell_solids),
    "battery_interface_area_mm2": sum(solid.Area() for solid in cell_solids),
    "inlet_area_mm2": sum(face.Area() for face in inlet_end_faces),
    "outlet_area_mm2": sum(face.Area() for face in outlet_end_faces),
}}
relative_errors = {{
    key: abs(measured[key] - MODEL["expected"][key]) / MODEL["expected"][key]
    for key in (
        "outer_with_ports_volume_mm3",
        "coolant_volume_mm3",
        "battery_total_volume_mm3",
        "battery_interface_area_mm2",
        "inlet_area_mm2",
        "outlet_area_mm2",
    )
}}
checks = {{
    "outer_is_one_solid": measured["outer_solid_count"] == 1,
    "coolant_is_one_connected_solid": measured["coolant_solid_count"] == 1,
    "battery_solid_count_is_23": measured["battery_solid_count"] == 23,
    "volumes_and_areas_within_relative_1e_6": max(relative_errors.values()) <= 1e-6,
}}
if not all(checks.values()):
    failure = {{
        "status": "CAD_GEOMETRY_CHECK_FAILED",
        "checks": checks,
        "measured": measured,
        "expected": MODEL["expected"],
        "relative_errors": relative_errors,
    }}
    (OUTPUT / "cad_result.json").write_text(json.dumps(failure, indent=2, sort_keys=True) + "\\n")
    raise SystemExit(json.dumps(failure, indent=2, sort_keys=True))

cq.exporters.export(outer_with_ports, str(OUTPUT / "enclosure_with_ports.step"))
cq.exporters.export(coolant, str(OUTPUT / "coolant.step"))
cq.exporters.export(cells_compound, str(OUTPUT / "battery_cells.step"))
cq.exporters.export(coolant, str(OUTPUT / "coolant.stl"), tolerance=0.05, angularTolerance=0.1)
cq.exporters.export(cells_compound, str(OUTPUT / "battery_cells.stl"), tolerance=0.05, angularTolerance=0.1)

# Production snappyHexMesh surfaces are exported separately in metres. STL has
# no unit metadata, so this explicit scale and the manifest unit are mandatory.
production_surface_dir = OUTPUT / "production_surfaces"
production_surface_dir.mkdir(parents=True, exist_ok=True)
production_battery_files = []
for cell, solid in zip(MODEL["cells"], cell_solids, strict=True):
    filename = f'battery_{{cell["id"]}}.stl'
    relative_path = f"production_surfaces/{{filename}}"
    cq.exporters.export(
        solid.scale(0.001),
        str(production_surface_dir / filename),
        tolerance=0.00005,
        angularTolerance=0.1,
    )
    production_battery_files.append(relative_path)
cq.exporters.export(
    outer_with_ports.val().scale(0.001),
    str(production_surface_dir / "outerDomain.stl"),
    tolerance=0.00005,
    angularTolerance=0.1,
)
for filename, faces in (
    ("inlet.stl", inlet_end_faces),
    ("outlet.stl", outlet_end_faces),
    ("outerWall.stl", outer_wall_faces),
):
    cq.exporters.export(
        cq.Compound.makeCompound(faces).scale(0.001),
        str(production_surface_dir / filename),
        tolerance=0.00005,
        angularTolerance=0.1,
    )
production_surface_files = [
    "production_surfaces/outerDomain.stl",
    "production_surfaces/inlet.stl",
    "production_surfaces/outlet.stl",
    "production_surfaces/outerWall.stl",
    *production_battery_files,
]

result = {{
    "status": "CAD_EXPORTED_GEOMETRY_CHECKS_PASSED",
    "geometry_sha256": MODEL["geometry_sha256"],
    "cad_geometry_checks_passed": True,
    "production_mesh_ready": False,
    "port_centers_xy_mm": {{
        "inlet": MODEL["ports"]["inlet"]["center_xy_mm"],
        "outlet": MODEL["ports"]["outlet"]["center_xy_mm"],
    }},
    "port_placement_source": MODEL["ports"]["source"],
    "checks": checks,
    "measured": measured,
    "expected": MODEL["expected"],
    "relative_errors": relative_errors,
    "production_surfaces": {{
        "length_unit": "m",
        "outer_domain": "production_surfaces/outerDomain.stl",
        "boundary_partition_files": [
            "production_surfaces/inlet.stl",
            "production_surfaces/outlet.stl",
            "production_surfaces/outerWall.stl",
        ],
        "battery_count": len(production_battery_files),
        "battery_files": production_battery_files,
        "intended_backend": "blockMesh+snappyHexMesh",
    }},
    "files": [
        "enclosure_with_ports.step",
        "coolant.step",
        "battery_cells.step",
        "coolant.stl",
        "battery_cells.stl",
        *production_surface_files,
    ],
}}
result["file_sha256"] = {{
    filename: hashlib.sha256((OUTPUT / filename).read_bytes()).hexdigest()
    for filename in result["files"]
}}
(OUTPUT / "cad_result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\\n")
print(json.dumps(result, indent=2, sort_keys=True))
'''


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def write_cad_source_bundle(config: ResolvedConfig, output_dir: str | Path) -> dict[str, object]:
    output = Path(output_dir).resolve()
    geometry = geometry_report(config)
    model = _cad_model(config)
    manifest = {
        "schema_version": 1,
        "status": "CAD_SOURCE_CREATED_NOT_EXECUTED",
        "backend": "CadQuery/OpenCascade",
        "units": "mm",
        "geometry_sha256": config.geometry_sha256,
        "resolved_input_sha256": config.resolved_input_sha256,
        "cell_count": config.cells.count,
        "port_diameter_mm": config.ports.inlet_diameter_m * 1000,
        "inlet_diameter_mm": config.ports.inlet_diameter_m * 1000,
        "outlet_diameter_mm": config.ports.outlet_diameter_m * 1000,
        "port_centers_xy_mm": {
            "inlet": [value * 1000 for value in config.ports.inlet_center_xy_m],
            "outlet": [value * 1000 for value in config.ports.outlet_center_xy_m],
        },
        "port_placement": config.ports.placement,
        "enclosure_profile": config.enclosure.profile,
        "port_source": config.ports.center_source,
        "geometry_definition_ready": True,
        "production_cad_ready": False,
        "required_checks": [
            "CadQuery export completes",
            "coolant is one connected solid",
            "23 battery solids are present",
            "fluid-solid interface areas match analytic cylinders",
            "inlet/outlet areas match pi*D^2/4",
            "production blockMesh/snappyHexMesh and checkMesh gates pass",
        ],
        "geometry_report_summary": {
            "minimum_cell_gap_m": geometry["clearances"]["minimum_cell_cell_surface_m"],
            "minimum_outline_gap_m": geometry["clearances"]["minimum_cell_outline_surface_m"],
        },
        "analytic_expectations": model["expected"],
    }
    _atomic_write(output / "cad_model.json", json.dumps(model, indent=2, sort_keys=True) + "\n")
    _atomic_write(output / "build_cad.py", _script(model))
    _atomic_write(output / "cad_manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def execute_cad_source(config: ResolvedConfig, output_dir: str | Path) -> dict[str, object]:
    output = Path(output_dir).resolve()
    cache_dir = output / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_dir))
    try:
        import cadquery  # noqa: F401
    except ImportError as exc:
        raise DependencyUnavailable(
            "CadQuery is unavailable; CAD source was not executed and no STEP/STL was faked"
        ) from exc
    write_cad_source_bundle(config, output)
    for filename in (
        "cad_result.json",
        "enclosure_with_ports.step",
        "coolant.step",
        "battery_cells.step",
        "coolant.stl",
        "battery_cells.stl",
        "production_surfaces/outerDomain.stl",
        "production_surfaces/inlet.stl",
        "production_surfaces/outlet.stl",
        "production_surfaces/outerWall.stl",
        *(f"production_surfaces/battery_C{index:02d}.stl" for index in range(1, 24)),
    ):
        candidate = output / filename
        if candidate.is_file():
            candidate.unlink()
    script_path = output / "build_cad.py"
    namespace = {"__file__": str(script_path), "__name__": "__main__"}
    exec(compile(script_path.read_text(encoding="utf-8"), str(script_path), "exec"), namespace)
    result = json.loads((output / "cad_result.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "cad_manifest.json").read_text(encoding="utf-8"))
    manifest.update(
        {
            "status": result["status"],
            "cad_geometry_checks_passed": result["cad_geometry_checks_passed"],
            "production_cad_ready": True,
            "port_placement_basis": config.ports.center_source,
            "result_file": "cad_result.json",
            "exported_files": result["files"],
        }
    )
    _atomic_write(output / "cad_manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return result
