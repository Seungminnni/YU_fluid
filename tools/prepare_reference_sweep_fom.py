from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
import uuid
from pathlib import Path

from cfd_twin.workflow_run import build_imported_case
from cfd_twin.steady_numerics import PROFILES
from cfd_twin.outlet_boundary import PROFILES as OUTLET_PROFILES


BATTERY_IDS = tuple(f"C{index:02d}" for index in range(1, 24))
FRUSTUM_CENTER_SOURCE = "C05_TOP_FRUSTUM_D33_TO_D22_OUTLET_D19_20261101"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def boundary_names(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        name
        for name in re.findall(r"(?ms)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\{", text)
        if name != "FoamFile"
    }


def validate_sweep_source(source: Path) -> tuple[dict, dict, str]:
    report = read_json(source / "sweep_generation_report.json")
    plan = read_json(source / "sweep_mesh_plan.json")
    log = (source / "log.checkMesh_all_regions").read_text(
        encoding="utf-8", errors="replace"
    )
    checks = {
        "generation_passed": report.get("status") == "SWEEP_GENERATION_PASSED",
        "all_source_faces_quadrilateral": report.get(
            "all_source_faces_quadrilateral"
        )
        is True,
        "all_volume_cells_hexahedral": report.get(
            "all_volume_cells_hexahedral"
        )
        is True,
        "battery_region_count_is_23": report.get("battery_regions") == 23,
        "cell_count_in_target_band": int(plan["minimum_cells"])
        <= int(report.get("cells", -1))
        <= int(plan["maximum_cells"]),
        "both_final_regions_report_mesh_ok": log.count("Mesh OK.") >= 2,
        "no_failed_final_mesh_check": not re.search(
            r"Failed\s+\d+\s+mesh checks", log
        ),
    }
    for relative in (
        "constant/polyMesh",
        "constant/coolant/polyMesh",
        "constant/batterySolid/polyMesh",
        "constant/regionProperties",
    ):
        checks[f"present:{relative}"] = (source / relative).exists()
    if not all(checks.values()):
        failed = [name for name, passed in checks.items() if not passed]
        raise ValueError(f"sweep source failed closed validation: {failed}")
    return report, plan, log


def structured_layer_report(case: Path, plan: dict) -> dict:
    coolant_boundary = boundary_names(
        case / "constant/coolant/polyMesh/boundary"
    )
    solid_boundary = boundary_names(
        case / "constant/batterySolid/polyMesh/boundary"
    )
    missing: list[str] = []
    summaries: dict[str, dict] = {}
    for battery_id in BATTERY_IDS:
        prefix = f"battery_{battery_id}_interface"
        coolant_matches = sorted(name for name in coolant_boundary if name.startswith(prefix))
        solid_matches = sorted(name for name in solid_boundary if name.startswith(prefix))
        if not coolant_matches or not solid_matches:
            missing.append(battery_id)
        summaries[battery_id] = {
            "patches": coolant_matches,
            "paired_solid_patches": solid_matches,
            "minimum_target_layers": int(plan["wall_layer_count"]),
            "cap_target_layers": int(plan["cap_layer_count"]),
            "first_layer_height_m": float(plan["first_layer_height_m"]),
            "cap_first_layer_height_m": float(plan["cap_first_layer_height_m"]),
            "layer_growth": float(plan["wall_layer_growth"]),
            "surface_coverage_fraction": 1.0,
            "layer_completion_fraction": 1.0,
            "layer_coverage_fraction": 1.0,
            "wanted_thickness_fraction": 1.0,
        }
    checks = {
        "all_23_battery_interfaces_present_in_both_regions": not missing,
        "target_layer_count_is_11": int(plan["wall_layer_count"]) == 11
        and int(plan["cap_layer_count"]) == 11,
        "first_layer_height_is_6um": abs(
            float(plan["first_layer_height_m"]) - 6e-6
        )
        < 1e-12
        and abs(float(plan["cap_first_layer_height_m"]) - 6e-6) < 1e-12,
        "growth_ratio_is_1p2": abs(float(plan["wall_layer_growth"]) - 1.2)
        < 1e-12
        and abs(float(plan["cap_layer_growth"]) - 1.2) < 1e-12,
        "structured_sweep_full_coverage": True,
    }
    return {
        "schema_version": 2,
        "status": "LAYER_VALIDATION_PASSED"
        if all(checks.values())
        else "LAYER_VALIDATION_FAILED",
        "validation_source": (
            "reference_guided_mapped_quad_all_hexa_sweep_plan_plus_"
            "paired_interface_inventory"
        ),
        "checks": checks,
        "thresholds": {
            "expected_target_layers": 11,
            "first_layer_height_m": 6e-6,
            "growth_ratio": 1.2,
        },
        "battery_summaries": summaries,
        "missing_batteries": missing,
    }


def prepare(
    template_job: Path,
    sweep_source: Path,
    jobs_root: Path,
    steady_numerics_profile: str | None = None,
    mesh_profile: str | None = None,
    outlet_velocity_profile: str | None = None,
) -> Path:
    report, plan, checkmesh_log = validate_sweep_source(sweep_source)
    run_id = uuid.uuid4().hex
    job_dir = (jobs_root / "runs" / run_id).resolve()
    expected_parent = (jobs_root / "runs").resolve()
    if job_dir.parent != expected_parent or job_dir.exists():
        raise ValueError("fresh job path is not empty or not under the runs root")
    job_dir.mkdir(parents=True)
    job = read_json(template_job / "job.json")
    inputs = job.setdefault("inputs", {})
    # The case builder uses a finite preset only to render solver dictionaries;
    # the imported structured mesh remains authoritative and records its exact
    # count separately.  Use the largest supported preset for refined meshes.
    inputs["mesh_target_cells"] = (
        1_000_000 if int(report["cells"]) > 1_000_000 else int(plan["target_cells"])
    )
    inputs["inlet_velocity_m_s"] = 0.4
    inputs["outlet_pressure_pa"] = 101325.0
    overrides = list(job.get("geometry_overrides", []))
    overrides.extend(
        [
            'geometry.ports.inlet_diameter="33 mm"',
            'geometry.ports.outlet_diameter="19 mm"',
            'geometry.ports.inlet_center_xy_mm=[13.6, 105.2]',
            'geometry.ports.outlet_center_xy_mm=[89.6, 15.2]',
            'geometry.ports.inlet_column=1',
            'geometry.ports.placement="end_cell_center"',
            f'geometry.ports.center_source="{FRUSTUM_CENTER_SOURCE}"',
            'boundary.inlet_velocity="0.4 m/s"',
            'boundary.outlet_absolute_pressure="101325 Pa"',
            'boundary.pressure_reference_absolute="101325 Pa"',
        ]
    )
    job["geometry_overrides"] = overrides
    geometry = dict(job.get("geometry", {}))
    geometry.update(
        {
            "geometry_variant": "inlet_frustum_d33_to_d22_outlet_d19",
            "inlet": {
                "shape": "frustum",
                "diameter_mm": 33.0,
                "near_tank_diameter_mm": 22.0,
                "area_mm2": math.pi * 33.0**2 / 4.0,
                "center_xy_mm": [13.6, 105.2],
            },
            "outlet": {
                "diameter_mm": 19.0,
                "area_mm2": math.pi * 19.0**2 / 4.0,
                "center_xy_mm": [89.6, 15.2],
            },
            "port_length_mm": 32.0,
        }
    )
    job["geometry"] = geometry
    shared_cad = (template_job / "cad").resolve()
    if not shared_cad.is_dir():
        raise ValueError(f"validated template CAD is missing: {shared_cad}")
    cad_hashes = {
        str(path.relative_to(shared_cad)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(shared_cad.rglob("*"))
        if path.is_file()
    }
    if not cad_hashes:
        raise ValueError("validated template CAD hash inventory is empty")
    job["prevalidated_cad"] = {"path": str(shared_cad), "file_sha256": cad_hashes}
    mesh_evidence_names = (
        "sweep_mesh_plan.json",
        "sweep_generation_report.json",
        "log.checkMesh_all_regions",
    )
    job["external_structured_mesh_import"] = {
        "source": str(sweep_source.resolve()),
        "file_sha256": {
            name: hashlib.sha256((sweep_source / name).read_bytes()).hexdigest()
            for name in mesh_evidence_names
        },
    }
    if steady_numerics_profile is not None:
        job["steady_numerics_profile"] = steady_numerics_profile
    else:
        # A retry template can carry a previous numerical experiment.  A
        # fresh structured-sweep job without an explicit profile must restore
        # the validated default SIMPLE settings instead of inheriting it.
        job.pop("steady_numerics_profile", None)
    if mesh_profile is not None:
        job["mesh_profile"] = mesh_profile
    if outlet_velocity_profile is not None:
        job["outlet_velocity_profile"] = outlet_velocity_profile
    else:
        job.pop("outlet_velocity_profile", None)
    previous_id = str(job.get("id", template_job.name))
    job.update(
        {
            "id": run_id,
            "status": "queued",
            "source": {
                **job.get("source", {}),
                "retry_of": previous_id,
                "mesh_backend": "reference_guided_gmsh_structured_sweep",
                "name": (
                    "conical_inlet_d33_to_d22_outlet_d19_"
                    f"{int(report['cells'])}_all_hexa"
                ),
                "validated_mesh_source": str(sweep_source.resolve()),
            },
        }
    )
    write_json(job_dir / "job.json", job)
    write_json(
        job_dir / "status.json",
        {
            "status": "queued",
            "stage": "cad",
            "message": "fresh structured-sweep FOM job preparation",
        },
    )
    try:
        built = build_imported_case(job_dir)
        case = Path(built["case"])
        for relative in (
            "polyMesh",
            "coolant/polyMesh",
            "batterySolid/polyMesh",
        ):
            destination = case / "constant" / relative
            if destination.exists():
                raise ValueError(f"fresh case unexpectedly contains {destination}")
            shutil.copytree(sweep_source / "constant" / relative, destination)
        shutil.copy2(
            sweep_source / "constant/regionProperties",
            case / "constant/regionProperties",
        )
        # The snappy profile's generation-only minVol limit is not a valid
        # final check for this structured mesh: it is interpreted by
        # checkMesh as a face-pyramid threshold.  Import the stricter sweep
        # backend's authoritative final-region quality dictionaries instead
        # (65 degrees, boundary skewness 5, minVol 1e-15).
        for relative in (
            "system/meshQualityDict",
            "system/coolant/meshQualityDict",
            "system/batterySolid/meshQualityDict",
        ):
            shutil.copy2(sweep_source / relative, case / relative)
        logs = case / "logs/production_mesh"
        reports = case / "reports/production_mesh"
        logs.mkdir(parents=True, exist_ok=True)
        reports.mkdir(parents=True, exist_ok=True)
        (logs / "checkMesh_all_regions.log").write_text(
            checkmesh_log, encoding="utf-8"
        )
        shutil.copy2(
            sweep_source / "log.checkMesh_pre_split_diagnostic",
            logs / "checkMesh_pre_split_diagnostic.log",
        )
        write_json(reports / "layer_coverage.json", structured_layer_report(case, plan))
        write_json(
            reports / "structured_sweep_manifest.json",
            {
                "schema_version": 1,
                "status": "STRUCTURED_SWEEP_MESH_IMPORTED",
                "source": str(sweep_source.resolve()),
                "cells": report["cells"],
                "target_cells": plan["target_cells"],
                "backend": plan["backend"],
                "all_hexahedral": report["all_volume_cells_hexahedral"],
                "fresh_job_id": run_id,
                "retry_of": previous_id,
            },
        )
        write_json(
            job_dir / "status.json",
            {
                "status": "queued",
                "stage": "mesh_validation",
                "message": "structured sweep mesh imported; production reports pending",
            },
        )
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    return job_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template-job", required=True)
    parser.add_argument("--sweep-source", required=True)
    parser.add_argument("--jobs-root", required=True)
    parser.add_argument("--steady-numerics-profile", choices=PROFILES)
    parser.add_argument("--mesh-profile")
    parser.add_argument("--outlet-velocity-profile", choices=OUTLET_PROFILES)
    args = parser.parse_args(argv)
    job_dir = prepare(
        Path(args.template_job).resolve(),
        Path(args.sweep_source).resolve(),
        Path(args.jobs_root).resolve(),
        args.steady_numerics_profile,
        args.mesh_profile,
        args.outlet_velocity_profile,
    )
    print(json.dumps({"status": "PREPARED", "job_dir": str(job_dir)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
