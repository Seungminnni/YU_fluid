from __future__ import annotations

import json
import importlib.util
import os
import platform
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from .cad import execute_cad_source, write_cad_source_bundle
from .config import ResolvedConfig
from .geometry import write_geometry_bundle
from .heat_source import write_heat_source_bundle
from .mesh import execute_mesh as execute_gmsh_mesh
from .openfoam_bridge import write_openfoam_bridge
from .production_mesh import write_production_mesh_source_bundle
from .result_contract import update_figure_status, write_result_contract


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def dependency_report() -> dict[str, object]:
    commands = {
        "chtMultiRegionSimpleFoam": shutil.which("chtMultiRegionSimpleFoam"),
        "chtMultiRegionFoam": shutil.which("chtMultiRegionFoam"),
        "blockMesh": shutil.which("blockMesh"),
        "snappyHexMesh": shutil.which("snappyHexMesh"),
        "checkMesh": shutil.which("checkMesh"),
        "docker": shutil.which("docker"),
        "ffmpeg": shutil.which("ffmpeg"),
    }
    optional_modules: dict[str, bool] = {}
    for module in ("cadquery", "gmsh", "pyvista", "PySide6", "vtk"):
        optional_modules[module] = importlib.util.find_spec(module) is not None
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "commands": commands,
        "optional_python_modules": optional_modules,
        "core_ready": True,
        "openfoam_ready": all(
            commands[name]
            for name in (
                "chtMultiRegionSimpleFoam",
                "chtMultiRegionFoam",
                "blockMesh",
                "snappyHexMesh",
                "checkMesh",
            )
        ),
        "gui_ready": optional_modules["pyvista"] and optional_modules["PySide6"],
    }


def _region_properties() -> str:
    return '''FoamFile
{
    format      ascii;
    class       dictionary;
    object      regionProperties;
}

regions
(
    fluid (coolant)
    solid (batterySolid)
);
'''


def _gravity() -> str:
    return '''FoamFile
{
    format      ascii;
    class       uniformDimensionedVectorField;
    object      g;
}

dimensions      [0 1 -2 0 0 0 0];
value           (0 0 0);
'''


def _case_readme(
    blockers: list[dict[str, str]], warnings: list[dict[str, str]]
) -> str:
    blocker_lines = "\n".join(
        f'- `{item["id"]}`: {item["reason"]}' for item in blockers
    )
    warning_lines = "\n".join(
        f'- `{item["id"]}`: {item["reason"]}' for item in warnings
    )
    return f'''# Generated CFD input bundle

The traceable input bundle is complete, but this directory is intentionally
not marked runnable yet.

Implemented and verified here:

- resolved SI configuration and provenance;
- 19 mm inlet/outlet decision;
- PPT-based 22-point enclosure, 11.25 mm notches and 23-cell placement;
- collision/clearance report and deterministic CadQuery source;
- region declaration and zero-gravity convention;
- constant/table/electrical-Joule heat source artifacts;
- Fluent UDF export for interoperability.

Current blockers:

{blocker_lines}

Recorded non-blocking assumptions:

{warning_lines}

The runner must refuse CFD execution until CAD topology, named patches,
`checkMesh`, region connectivity and source-integral checks pass. No synthetic
CFD fields may be generated to bypass these gates.
'''


def prepare_case_inputs(
    config: ResolvedConfig,
    output_dir: str | Path,
    *,
    execute_cad: bool = False,
    execute_mesh_level: str | None = None,
) -> dict[str, object]:
    output = Path(output_dir).resolve()
    geometry = write_geometry_bundle(config, output / "geometry")
    cad_source = write_cad_source_bundle(config, output / "cad_source")
    should_execute_cad = execute_cad or execute_mesh_level is not None
    cad_result = (
        execute_cad_source(config, output / "cad_source") if should_execute_cad else None
    )
    if should_execute_cad:
        cad_source = json.loads(
            (output / "cad_source" / "cad_manifest.json").read_text(encoding="utf-8")
        )
    mesh_result = (
        execute_gmsh_mesh(
            config,
            output / "cad_source",
            output / "mesh",
            mesh_level=execute_mesh_level,
        )
        if execute_mesh_level is not None
        else None
    )
    production_mesh_source = (
        write_production_mesh_source_bundle(
            config,
            output / "cad_source",
            output / "production" / "single",
            mesh_level="single",
        )
        if should_execute_cad
        and cad_result
        and cad_result.get("cad_geometry_checks_passed", False)
        else None
    )
    heat_source = write_heat_source_bundle(config, output / "heat_source")
    result_contract = write_result_contract(config, output / "results")
    openfoam_bridge = write_openfoam_bridge(config, output / "openfoam")
    if mesh_result is not None:
        preview_files = [
            str(Path(path).relative_to(output))
            for path in mesh_result.get("preview", {}).get("files", [])
        ]
        update_figure_status(
            output / "results", 2, "MESH_READY", artifacts=preview_files
        )
    dependencies = dependency_report()
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if config.ports.center_source.startswith("ASSUMPTION"):
        warnings.append(
            {
                "id": "A-04",
                "reason": (
                    "PPT-preferred end-cell port assignment is accepted for the baseline; "
                    "reversed/corner placements remain sensitivity profiles"
                ),
            }
        )
    if should_execute_cad and cad_result and not cad_result["cad_geometry_checks_passed"]:
        blockers.append(
            {
                "id": "CAD_VALIDATION",
                "reason": "CAD was exported but its topology/area/volume checks failed",
            }
        )
    elif not should_execute_cad and not dependencies["optional_python_modules"]["cadquery"]:
        blockers.append(
            {
                "id": "DEPENDENCY_CADQUERY",
                "reason": "CadQuery/OpenCascade is unavailable, so STEP/STL export has not run",
            }
        )
    elif not should_execute_cad:
        blockers.append(
            {
                "id": "CAD_NOT_EXECUTED",
                "reason": "CadQuery is available but --execute-cad was not requested",
            }
        )
    if not dependencies["openfoam_ready"]:
        blockers.append(
            {
                "id": "DEPENDENCY_OPENFOAM",
                "reason": "required OpenFOAM v2512 executables are unavailable",
            }
        )
    if mesh_result is None:
        blockers.append(
            {
                "id": "SMOKE_CHECKMESH",
                "reason": "smoke mesh generation/conversion/checkMesh has not completed",
            }
        )
    else:
        blockers.append(
            {
                "id": "SMOKE_CHECKMESH",
                "reason": (
                    "Gmsh smoke mesh passed mesh-element integration but "
                    "gmshToFoam/checkMesh has not run"
                ),
            }
        )
    blockers.append(
        {
            "id": "PRODUCTION_CHECKMESH",
            "reason": (
                "single 207,390-cell-target blockMesh/snappyHexMesh profile, "
                "23 non-empty battery cellZones, prism layers, count/composition "
                "and production checkMesh gates have not passed"
            ),
        }
    )
    manifest = {
        "schema_version": 1,
        "case_name": config.case_name,
        "created_utc": datetime.now(UTC).isoformat(),
        "status": "INPUT_BUNDLE_READY_NOT_RUNNABLE",
        "runnable": False,
        "runnable_smoke": False,
        "runnable_production": False,
        "blockers": blockers,
        "warnings": warnings,
        "source_config": config.source_path,
        "source_sha256": config.source_sha256,
        "applied_overlays": list(config.applied_overlays),
        "applied_overrides": list(config.applied_overrides),
        "resolved_input_sha256": config.resolved_input_sha256,
        "geometry_sha256": config.geometry_sha256,
        "geometry_production_ready": geometry["production_cad_ready"],
        "cad_geometry_ready": bool(
            cad_result and cad_result.get("cad_geometry_checks_passed", False)
        ),
        "cad_source": cad_source,
        "cad_result": cad_result,
        "mesh_result": mesh_result,
        "production_mesh_source": production_mesh_source,
        "openfoam_bridge": openfoam_bridge,
        "heat_source": heat_source,
        "result_contract": result_contract,
        "dependencies": dependencies,
        "required_solver": {
            "distribution": config.solver.distribution,
            "version": config.solver.version,
            "steady": "chtMultiRegionSimpleFoam",
            "transient": "chtMultiRegionFoam",
        },
        "pressure_convention": {
            "outlet_absolute_pressure_Pa": config.boundary.outlet_absolute_pressure_pa,
            "gauge_reference_Pa": config.boundary.pressure_reference_absolute_pa,
            "gravity_m_s2": list(config.boundary.gravity_m_s2),
            "p_rgh_equals_p_for_baseline": True,
        },
        "paper_discrepancies": {
            "reported_reynolds": config.derived.paper_reynolds,
            "table4_reynolds": config.derived.reynolds_table4,
            "implied_viscosity_Pa_s": config.derived.viscosity_implied_by_paper_re_pa_s,
            "original_dynamic_udf_available": False,
        },
    }
    _atomic_write(output / "resolved_config.json", config.to_json() + "\n")
    provenance = {
        "schema_version": 1,
        "source_priority": [
            "user_decision",
            "dimensioned_ppt",
            "paper_tables",
            "paper_figure_qualitative_pattern",
            "paper_figure_digitization",
            "assumption",
        ],
        "source_config": config.source_path,
        "source_sha256": config.source_sha256,
        "resolved_input_sha256": config.resolved_input_sha256,
        "geometry_sha256": config.geometry_sha256,
        "applied_overlays": list(config.applied_overlays),
        "applied_overrides": list(config.applied_overrides),
        "port_diameter_decision_mm": 19.0,
        "notch_depth_source_mm": 11.25,
        "original_dynamic_udf_available": False,
    }
    _atomic_write(output / "provenance.json", json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    _atomic_write(output / "case_manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    _atomic_write(output / "constant" / "regionProperties", _region_properties())
    _atomic_write(output / "constant" / "g", _gravity())
    _atomic_write(output / "README_NOT_RUNNABLE.md", _case_readme(blockers, warnings))
    return manifest
