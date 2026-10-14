"""Portable, reproducible batches for new water experiments and CAD overlays."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

import yaml

from .cad import execute_cad_source
from .case_builder import _docker_command
from .config import load_config
from .file_lock import lock_file
from .geometry import geometry_report
from .parallelism import default_solver_mpi_ranks
from .production_mesh import MESH_PRESETS
from .steady_convergence import validate_maximum_iterations
from .steady_numerics import validate_profile
from .outlet_boundary import validate_profile as validate_outlet_profile
from .workflow_run import (
    BASE_CONFIG, SOURCE_ROOT, STEP_ROLES, THERMAL_OVERLAY, _operation_overrides,
    _write_json, prepare_imported_run, prepare_solver_inputs, run_imported_case,
    stage_uploaded_step,
)

DEFAULT_CONFIG = SOURCE_ROOT / "configs/experiments/frame_fit_water.yaml"

DEFAULT_AUTOMATIC_RETRY = {
    "enabled": True,
    "maximum_retries": 6,
    "cold_flow_iteration_limits": [20_000],
    "steady_numerics_profiles": [
        "pressure_relaxation_0p15",
        "pressure_limited_0p5",
        "pressure_reltol_zero",
        "pressure_relaxation_0p5",
    ],
}


def _automatic_retry_policy(raw: dict, case: dict) -> dict:
    """Freeze bounded recovery choices without changing any acceptance gate."""

    policy = copy.deepcopy(DEFAULT_AUTOMATIC_RETRY)
    for source in (raw.get("automatic_retry"), case.get("automatic_retry")):
        if source is None:
            continue
        if not isinstance(source, dict):
            raise ValueError("automatic_retry must be a mapping")
        unknown = set(source) - set(DEFAULT_AUTOMATIC_RETRY)
        if unknown:
            raise ValueError(f"Unknown automatic_retry keys: {', '.join(sorted(unknown))}")
        policy.update(source)
    if not isinstance(policy["enabled"], bool):
        raise ValueError("automatic_retry.enabled must be boolean")
    maximum = policy["maximum_retries"]
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 0 <= maximum <= 12:
        raise ValueError("automatic_retry.maximum_retries must be an integer from 0 to 12")
    limits = policy["cold_flow_iteration_limits"]
    if not isinstance(limits, list) or len(set(limits)) != len(limits):
        raise ValueError("automatic_retry.cold_flow_iteration_limits must be a unique list")
    policy["cold_flow_iteration_limits"] = sorted(
        validate_maximum_iterations(value) for value in limits
    )
    profiles = policy["steady_numerics_profiles"]
    if not isinstance(profiles, list) or len(set(profiles)) != len(profiles):
        raise ValueError("automatic_retry.steady_numerics_profiles must be a unique list")
    for profile in profiles:
        validate_profile(profile)
    return policy


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _flatten(mapping: dict, prefix: str = "") -> list[str]:
    result = []
    for key, value in mapping.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.extend(_flatten(value, name))
        else:
            result.append(name + "=" + json.dumps(value, ensure_ascii=False))
    return result


def load_plan(path: Path) -> dict:
    """Freeze YAML contents, not machine-specific overlay filenames."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("Experiment YAML requires schema_version: 1")
    cases = raw.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Experiment YAML requires a nonempty cases list")
    plan = {"name": str(raw.get("name", path.stem)), "cases": []}
    names = set()
    for case in cases:
        name = case.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", name) or name in names:
            raise ValueError("Case names must be unique letters/numbers/underscore/hyphen")
        names.add(name)
        inputs = {**raw.get("inputs", {}), **case.get("inputs", {})}
        # Validate without requiring a generated case or running Docker.
        from .workflow_run import _operating_inputs
        inputs = _operating_inputs(inputs)
        profiles = case.get("mesh_profiles", raw.get("mesh_profiles", [f"production_{inputs['mesh_target_cells']//1000}k"]))
        if not isinstance(profiles, list) or not profiles or len(set(profiles)) != len(profiles):
            raise ValueError("mesh_profiles must be a nonempty list without duplicates")
        for profile in profiles:
            if profile not in MESH_PRESETS or MESH_PRESETS[profile].target_cells != inputs["mesh_target_cells"]:
                raise ValueError(f"Mesh profile does not match requested cell count: {profile}")
        overrides = []
        for overlay in (BASE_CONFIG, THERMAL_OVERLAY, *(path.parent / p for p in raw.get("overlays", [])), *(path.parent / p for p in case.get("overlays", []))):
            value = yaml.safe_load(overlay.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError(f"Expected YAML mapping: {overlay}")
            overrides.extend(_flatten(value))
        extra = [*raw.get("overrides", []), *case.get("overrides", [])]
        if not all(isinstance(item, str) for item in extra):
            raise ValueError("overrides must be a list of key=value strings")
        overrides.extend(extra)
        overrides.extend(_operation_overrides(inputs))
        config = load_config(BASE_CONFIG, overlay_paths=(THERMAL_OVERLAY,), overrides=overrides)
        geometry_report(config)
        plan["cases"].append({"name": name, "inputs": inputs, "mesh_profiles": profiles,
                              "geometry_overrides": overrides,
                              "automatic_retry": _automatic_retry_policy(raw, case)})
        numerical_profile = case.get("steady_numerics_profile", raw.get("steady_numerics_profile"))
        validate_profile(numerical_profile)
        if numerical_profile is not None:
            plan["cases"][-1]["steady_numerics_profile"] = numerical_profile
        outlet_profile = case.get("outlet_velocity_profile", raw.get("outlet_velocity_profile"))
        validate_outlet_profile(outlet_profile)
        if outlet_profile is not None:
            plan["cases"][-1]["outlet_velocity_profile"] = outlet_profile
        cold_flow_maximum = case.get(
            "cold_flow_maximum_iterations", raw.get("cold_flow_maximum_iterations")
        )
        validate_maximum_iterations(cold_flow_maximum)
        if cold_flow_maximum is not None:
            plan["cases"][-1]["cold_flow_maximum_iterations"] = cold_flow_maximum
    return plan


def _queue(jobs: Path, prepared: dict, files: dict, specification: dict, *, profile_index: int = 0, retry_of: str | None = None) -> dict:
    run_id = uuid.uuid4().hex
    directory = jobs / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    profile = specification["mesh_profiles"][profile_index]
    manifest = {"id": run_id, "status": "queued", "files": files,
                "inputs": prepared["inputs"], "geometry": prepared["geometry"],
                "confirmation": prepared["confirmation"], "geometry_overrides": specification["geometry_overrides"],
                "mesh_profile": profile, "source": {"kind": "experiment_yaml", "name": specification["name"]}}
    if retry_of:
        manifest["source"]["retry_of"] = retry_of
    if specification.get("steady_numerics_profile") is not None:
        manifest["steady_numerics_profile"] = specification["steady_numerics_profile"]
    if specification.get("outlet_velocity_profile") is not None:
        manifest["outlet_velocity_profile"] = specification["outlet_velocity_profile"]
    if specification.get("cold_flow_maximum_iterations") is not None:
        manifest["cold_flow_maximum_iterations"] = specification[
            "cold_flow_maximum_iterations"
        ]
    if MESH_PRESETS[profile].preserve_wall_diagnostics:
        manifest["wall_diagnostic_iterations"] = 1500
    _write_json(directory / "job.json", manifest)
    _write_json(directory / "status.json", {"status": "queued", "stage": "cad", "message": "Prepared experiment; awaiting --run"})
    return {"name": specification["name"], "id": run_id, "job_dir": str(directory), "profile_index": profile_index}


def prepare_batch(config_path: Path, output: Path, jobs: Path, ranks: int) -> dict:
    plan = load_plan(config_path)
    plan_hash = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    manifest = output / "batch.json"
    if manifest.exists():
        batch = read(manifest)
        if batch.get("plan_sha256") != plan_hash or batch["solver_mpi_ranks"] != ranks or Path(batch["jobs_root"]) != jobs:
            raise ValueError("Existing batch has different inputs. Use a new --output for a new experiment.")
        return batch
    if (output / "preparation_started.json").exists():
        raise ValueError("Incomplete preparation exists. Use a new --output; existing jobs were not changed.")
    jobs.mkdir(parents=True, exist_ok=True)
    for case in plan["cases"]:
        if not prepare_solver_inputs(jobs, case["inputs"])["can_start_new_solver"]:
            raise ValueError("At least 55 GiB free disk space is required")
    _write_json(output / "preparation_started.json", {"time": time.time(), "plan_sha256": plan_hash})
    batch = {"schema_version": 1, "status": "prepared", "plan": plan, "plan_sha256": plan_hash,
             "solver_mpi_ranks": ranks, "mesh_mpi_ranks": 1, "jobs_root": str(jobs), "jobs": []}
    for specification in plan["cases"]:
        config = load_config(BASE_CONFIG, overlay_paths=(THERMAL_OVERLAY,), overrides=specification["geometry_overrides"])
        generated = output / "cad" / specification["name"]
        _write_json(generated / "geometry_report.json", geometry_report(config))
        (generated / "resolved_config.json").write_text(config.to_json(), encoding="utf-8")
        print(f"Preparing CAD: {specification['name']}", flush=True)
        execute_cad_source(config, generated)
        selected = {role: stage_uploaded_step(jobs, role, (generated / role).read_bytes()) for role in STEP_ROLES}
        prepared = prepare_imported_run(jobs, selected, specification["inputs"], geometry_overrides=specification["geometry_overrides"])
        if not prepared["can_start_new_solver"]:
            raise ValueError("Insufficient disk space after CAD generation")
        files = {role: str(jobs / "staging" / selected[role]) for role in STEP_ROLES}
        batch["jobs"].append(_queue(jobs, prepared, files, specification))
        _write_json(output / "preparation_progress.json", batch)
    _write_json(manifest, batch)
    return batch


def _retryable(job: Path, state: dict) -> bool:
    if state.get("status") != "failed":
        return False
    if state.get("stage") == "mesh":
        # Do not cycle mesh profiles for missing Docker/images or an interrupted
        # build.  A report must contain a measured failed gate, not merely exist.
        return bool(_failed_mesh_reports(job))
    report = job / "case/runs/steady_cold_flow/reports/solver/cold_flow_validation.json"
    return state.get("stage") == "steady" and report.is_file() and read(report).get("checks", {}).get("battery_yplus_gate_passed") is False


def _optional_report(path: Path) -> dict:
    try:
        value = read(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


MESH_REPORT_EXPECTATIONS = {
    "mesh_target_composition.json": "MESH_TARGET_VALIDATION_PASSED",
    "layer_coverage.json": "LAYER_VALIDATION_PASSED",
    "checkMesh_all_regions.json": "CHECKMESH_LOG_PASSED",
    "checkmesh.json": "CHECKMESH_LOG_PASSED",  # legacy report name
    "cellZones_pre_split.json": "CELLZONE_VALIDATION_PASSED",
    "cellZones_batterySolid.json": "CELLZONE_VALIDATION_PASSED",
    "interfaces_post_split.json": "INTERFACE_VALIDATION_PASSED",
    "mesh_integrals_pre_split.json": "MESH_INTEGRAL_VALIDATION_PASSED",
    "production_mesh_gate.json": "PRODUCTION_MESH_GATE_PASSED",
}


def _failed_mesh_reports(job: Path) -> dict[str, dict]:
    directory = job / "case/reports/production_mesh"
    failed: dict[str, dict] = {}
    for name, expected in MESH_REPORT_EXPECTATIONS.items():
        report = _optional_report(directory / name)
        if report and report.get("status") != expected:
            failed[name] = report
    for path in sorted(directory.glob("surfaceCheck_*.json")):
        report = _optional_report(path)
        if report and report.get("status") != "SURFACECHECK_LOG_PASSED":
            failed[path.name] = report
    return failed


def _mesh_failure_diagnosis(job: Path) -> dict:
    """Classify mesh failure by the measured gate that actually failed."""

    reports = _failed_mesh_reports(job)
    categories: set[str] = set()
    failed_checks: list[str] = []
    evidence: dict[str, object] = {}
    for name, report in reports.items():
        checks = report.get("checks") if isinstance(report.get("checks"), dict) else {}
        report_failures = sorted(key for key, value in checks.items() if value is not True)
        failed_checks.extend(f"{name}:{key}" for key in report_failures)
        if name == "mesh_target_composition.json":
            count_checks = {
                "total_cell_count_at_least_configured_minimum_if_set",
                "total_cell_count_within_configured_tolerance",
                "solid_cell_count_within_configured_tolerance_if_targeted",
                "fluid_cell_count_within_configured_tolerance_if_targeted",
                "point_count_within_configured_tolerance_if_targeted",
            }
            if set(report_failures) & count_checks:
                categories.add("mesh_cell_budget")
            if "hexahedron_fraction_at_least_configured_minimum" in report_failures:
                categories.add("mesh_composition")
            evidence[name] = {
                "targets": report.get("targets"),
                "actual": report.get("actual"),
                "relative_error": report.get("relative_error"),
                "limits": report.get("limits"),
            }
        elif name == "layer_coverage.json":
            categories.add("mesh_layers")
            evidence[name] = {
                "thresholds": report.get("thresholds"),
                "low_coverage_batteries": report.get("low_coverage_batteries", []),
                "low_thickness_batteries": report.get("low_thickness_batteries", []),
                "missing_batteries": report.get("missing_batteries", []),
                "snappy_final_illegal_face_count": report.get("snappy_final_illegal_face_count"),
            }
        elif name in {"checkMesh_all_regions.json", "checkmesh.json"}:
            categories.add("mesh_geometry_quality")
            evidence[name] = {
                "metrics": report.get("metrics"),
                "limits": report.get("limits"),
                "failed_lines": report.get("failed_lines", []),
            }
        elif name != "production_mesh_gate.json":
            categories.add("mesh_topology_or_interface")
            evidence[name] = {
                "status": report.get("status"),
                "error": report.get("error"),
            }

    if not categories and reports:
        categories.add("mesh_gate_incomplete")
    category = next(iter(categories)) if len(categories) == 1 else "mesh_multiple_gates"
    recommendations = {
        "mesh_cell_budget": (
            "adjust background/region cell allocation from measured counts; preserve layer and quality gates"
        ),
        "mesh_composition": (
            "adjust refinement transitions to recover hexahedral fraction; do not relax the composition gate"
        ),
        "mesh_layers": (
            "change only the failed battery-wall layer/refinement transition and regenerate from an empty mesh"
        ),
        "mesh_geometry_quality": (
            "locate offending faces and change local topology/refinement; keep internal and boundary skewness separate"
        ),
        "mesh_topology_or_interface": (
            "repair the measured topology, region, volume, or interface defect before any solver run"
        ),
        "mesh_gate_incomplete": "regenerate only after the missing authoritative gate is identified",
        "mesh_multiple_gates": (
            "fix hard topology/interface defects first, then layer/quality, and tune cell count last"
        ),
    }
    return {
        "category": category,
        "categories": sorted(categories),
        "failed_checks": sorted(failed_checks),
        "failed_reports": sorted(reports),
        "evidence": evidence,
        "recommendation": recommendations[category],
    }


def diagnose_failure(job: Path, state: dict) -> dict:
    """Classify a measured failure before selecting any recovery action."""

    diagnosis = {
        "schema_version": 1,
        "job_id": job.name,
        "failed_stage": state.get("stage"),
        "status_message": state.get("message"),
        "category": "unclassified",
        "retryable": False,
        "failed_checks": [],
    }
    if state.get("status") != "failed":
        diagnosis["reason"] = "job is not in failed state"
        return diagnosis
    if state.get("stage") == "mesh":
        measured = _mesh_failure_diagnosis(job)
        if measured["failed_reports"]:
            diagnosis.update(
                category=measured["category"], retryable=True,
                reason="measured production-mesh gate failure",
                categories=measured["categories"],
                evidence=measured["evidence"],
                failed_checks=measured["failed_checks"],
                failed_reports=measured["failed_reports"],
                recommendation=measured["recommendation"],
            )
        else:
            diagnosis.update(
                category="infrastructure_or_unmeasured_mesh_failure",
                reason="no measured mesh report; blind retry is disabled",
            )
        return diagnosis
    if state.get("stage") != "steady":
        diagnosis["reason"] = "only measured mesh and steady failures are recoverable"
        return diagnosis

    cold = _optional_report(
        job / "case/runs/steady_cold_flow/reports/solver/cold_flow_validation.json"
    )
    cht = _optional_report(
        job / "case/runs/steady_cht/reports/solver/steady_cht_validation.json"
    )
    report, report_stage = (cht, "cht") if cht and cht.get("status") != "STEADY_CHT_VALIDATION_PASSED" else (cold, "cold")
    checks = report.get("checks") if isinstance(report.get("checks"), dict) else {}
    failed = sorted(name for name, passed in checks.items() if passed is not True)
    diagnosis.update(report_stage=report_stage, failed_checks=failed,
                     final_iteration=report.get("final_iteration"))
    instability_checks = {
        "final_fields_finite", "final_turbulence_fields_nonnegative",
    }
    if instability_checks & set(failed):
        diagnosis.update(category="numerical_instability", retryable=True,
                         reason="computed field became non-finite or non-physical")
        return diagnosis
    log_dir = job / f"case/runs/steady_{'cht' if report_stage == 'cht' else 'cold_flow'}/logs/solver"
    log_prefix = f"steady_{'cht' if report_stage == 'cht' else 'cold_flow'}"
    # A failed continuation leaves the validation report at the preceding
    # checkpoint. Inspect every solver log for the stage so the numerical
    # abort is not hidden behind that otherwise valid checkpoint report.
    tails = []
    for log in sorted(log_dir.glob(f"{log_prefix}*.log")):
        try:
            tails.append(log.read_text(encoding="utf-8", errors="replace")[-65_536:])
        except OSError:
            continue
    tail = "\n".join(tails)
    if "Negative initial temperature" in tail or "FOAM parallel run aborting" in tail:
        diagnosis.update(category="numerical_instability", retryable=True,
                         reason="solver log records a numerical abort")
        return diagnosis

    if checks.get("battery_yplus_gate_passed") is False:
        diagnosis.update(category="wall_yplus", retryable=True,
                         reason="measured battery-wall y+ gate failure")
        return diagnosis

    convergence_tokens = (
        "iterations_completed", "residuals_below", "residual_floors_reached",
        "monitor_window", "mass_imbalance", "pressure_drop_span",
        "tmax_change", "tavg_change", "energy_imbalance",
    )
    convergence = bool(failed) and all(
        any(token in name for token in convergence_tokens) for name in failed
    )
    if convergence:
        diagnosis.update(category="convergence", retryable=True,
                         reason="only measured convergence gates failed")
    elif not report:
        diagnosis.update(category="missing_validation_report",
                         reason="steady validation report is missing")
    else:
        diagnosis["reason"] = "failure contains an unsupported safety or physics gate"
    return diagnosis


def automatic_retry_decision(entry: dict, specification: dict, job: Path, state: dict) -> dict:
    """Return one bounded, evidence-based fresh-run remedy."""

    policy = specification.get("automatic_retry", DEFAULT_AUTOMATIC_RETRY)
    diagnosis = diagnose_failure(job, state)
    decision = {"action": "stop", "diagnosis": diagnosis}
    attempts = entry.get("prior_attempts", [])
    if not policy.get("enabled", True):
        decision["reason"] = "automatic retry is disabled"
        return decision
    if len(attempts) >= int(policy.get("maximum_retries", 0)):
        decision["reason"] = "automatic retry limit reached"
        return decision
    if not diagnosis.get("retryable"):
        decision["reason"] = diagnosis.get("reason", "unclassified failure")
        return decision

    current = read(job / "job.json")
    category = diagnosis["category"]
    if category.startswith("mesh_") or category == "wall_yplus":
        next_profile = int(entry.get("profile_index", 0)) + 1
        if next_profile >= len(specification["mesh_profiles"]):
            decision["reason"] = "no further approved mesh profile is available"
            return decision
        decision.update(
            action="retry", profile_index=next_profile, job_overrides={},
            remedy="regenerate mesh and restart S0 with the next approved mesh profile",
        )
        return decision

    if category == "convergence" and diagnosis.get("report_stage") == "cold":
        current_limit = validate_maximum_iterations(
            current.get("cold_flow_maximum_iterations")
        )
        for limit in policy.get("cold_flow_iteration_limits", []):
            if limit > current_limit:
                decision.update(
                    action="retry", profile_index=int(entry.get("profile_index", 0)),
                    job_overrides={"cold_flow_maximum_iterations": limit},
                    remedy=f"restart from S0 with unchanged gates and a {limit}-iteration diagnostic cap",
                )
                return decision

    used_profiles = {
        attempt.get("steady_numerics_profile") for attempt in attempts
        if attempt.get("steady_numerics_profile") is not None
    }
    if current.get("steady_numerics_profile") is not None:
        used_profiles.add(current["steady_numerics_profile"])
    for profile in policy.get("steady_numerics_profiles", []):
        if profile not in used_profiles:
            decision.update(
                action="retry", profile_index=int(entry.get("profile_index", 0)),
                job_overrides={"steady_numerics_profile": profile},
                remedy=f"restart from S0 with approved numerical profile {profile}",
            )
            return decision
    decision["reason"] = "no unused approved numerical recovery profile is available"
    return decision


def fresh_retry(
    output: Path, batch: dict, entry: dict, specification: dict, *,
    profile_index: int, job_overrides: dict | None = None,
    diagnosis: dict | None = None,
) -> None:
    """Archive diagnostics, then delete only the failed generated job."""
    runs = Path(batch["jobs_root"]) / "runs"
    previous = Path(entry["job_dir"])
    if previous.is_symlink() or previous.resolve().parent != runs.resolve() or not re.fullmatch(r"[0-9a-f]{32}", previous.name):
        raise ValueError("Unsafe failed-job path")
    state = read(previous / "status.json")
    if state.get("status") != "failed":
        raise ValueError("Only failed jobs may be replaced")
    published = SOURCE_ROOT / "viewer/four_case_manifest.json"
    if published.exists() and any(run.get("id") == f"run_{previous.name}" for run in read(published).get("runs", [])):
        raise ValueError("Published results must never be removed")
    prior = read(previous / "job.json")
    for filename in prior["files"].values():
        source = Path(filename)
        if source.resolve().is_relative_to(previous.resolve()) or hashlib.sha256(source.read_bytes()).hexdigest() != source.stem:
            raise ValueError("Frozen STEP input changed or is inside the failed job")
    archive = output / "diagnostic_attempts" / previous.name
    archive.mkdir(parents=True, exist_ok=True)
    for name in ("job.json", "status.json"):
        shutil.copy2(previous / name, archive / name)
    for relative in (
        "case/reports/production_mesh", "case/diagnostics/wall",
        "case/diagnostics/outlet",
        "case/runs/steady_cold_flow/logs/solver",
        "case/runs/steady_cold_flow/reports/solver",
        "case/runs/steady_cht/logs/solver",
        "case/runs/steady_cht/reports/solver",
    ):
        source = previous / relative
        if source.is_dir():
            shutil.copytree(source, archive / relative, dirs_exist_ok=True)
    snappy = previous / "case/logs/production_mesh/snappyHexMesh.log"
    if snappy.is_file():
        shutil.copy2(snappy, archive / "snappyHexMesh.log")
    # Preserve the authoritative all-region quality output before removing a
    # failed generated mesh.  This is required to distinguish a local face
    # defect from a global quality or layer-coverage failure on a fresh retry.
    check_mesh = previous / "case/logs/production_mesh/checkMesh_all_regions.log"
    if check_mesh.is_file():
        shutil.copy2(check_mesh, archive / "checkMesh_all_regions.log")
    worker_log = previous / "worker.log"
    for name in ('outlet_boundary.json', 'steady_numerics.json'):
        audit = previous / 'case/reports' / name
        if audit.is_file():
            shutil.copy2(audit, archive / name)
    if worker_log.is_file():
        with worker_log.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 65536))
            (archive / "worker_tail.log").write_bytes(stream.read())
    replacement_specification = copy.deepcopy(specification)
    for key in (
        "steady_numerics_profile", "outlet_velocity_profile",
        "cold_flow_maximum_iterations",
    ):
        if prior.get(key) is not None:
            replacement_specification[key] = prior[key]
    replacement_specification.update(job_overrides or {})
    replacement = _queue(Path(batch["jobs_root"]), prior, prior["files"], replacement_specification,
                         profile_index=profile_index, retry_of=previous.name)
    history = entry.setdefault("prior_attempts", [])
    history.append({
        "id": previous.name,
        "status": state,
        "mesh_profile": prior.get("mesh_profile"),
        "steady_numerics_profile": prior.get("steady_numerics_profile"),
        "cold_flow_maximum_iterations": prior.get("cold_flow_maximum_iterations"),
        "diagnosis": diagnosis,
        "remedy": (diagnosis or {}).get("remedy"),
        "diagnostics_archive": str(archive),
        "output_deleted": False,
    })
    if diagnosis is not None:
        _write_json(archive / "failure_diagnosis.json", diagnosis)
    entry.update(replacement)
    _write_json(output / "batch.json", batch)
    shutil.rmtree(previous)
    history[-1]["output_deleted"] = True
    _write_json(output / "batch.json", batch)


def check_docker() -> None:
    docker = _docker_command()
    if not docker:
        raise ValueError("Docker Desktop is required. Start it in Linux-container mode.")
    for arguments in (("info", "--format", "{{.OSType}}"), ("compose", "version")):
        result = subprocess.run([docker, *arguments], capture_output=True, text=True, timeout=30, check=False)
        if result.returncode or (arguments[0] == "info" and result.stdout.strip() != "linux"):
            raise ValueError(f"Docker Linux engine / Compose is not ready: {result.stderr.strip()}")


def run_batch(output: Path, batch: dict, *, retry_failed: bool = False) -> bool:
    os.environ.update(OPENFOAM_MPI_RANKS=str(batch["solver_mpi_ranks"]), OPENFOAM_MESH_MPI_RANKS="1",
                      OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    for entry, specification in zip(batch["jobs"], batch["plan"]["cases"], strict=True):
        while True:
            job = Path(entry["job_dir"])
            state = read(job / "status.json")
            if state.get("status") == "complete" and not state.get("release_error"):
                break
            if state.get("status") == "failed":
                decision = automatic_retry_decision(entry, specification, job, state)
                _write_json(output / "retry_decisions" / f"{job.name}.json", decision)
                if decision["action"] == "retry":
                    fresh_retry(
                        output, batch, entry, specification,
                        profile_index=decision["profile_index"],
                        job_overrides=decision.get("job_overrides"),
                        diagnosis={**decision["diagnosis"], "remedy": decision["remedy"]},
                    )
                    continue
                if retry_failed:
                    fresh_retry(output, batch, entry, specification, profile_index=entry["profile_index"])
                    retry_failed = False  # one explicit retry, never an endless failure loop
                    continue
            if state.get("status") != "queued":
                _write_json(output / "status.json", {"status": "needs_attention", "current": entry, "detail": state})
                return False
            if not prepare_solver_inputs(Path(batch["jobs_root"]), specification["inputs"])["can_start_new_solver"]:
                raise ValueError("At least 55 GiB free disk space is required before each solve")
            _write_json(output / "status.json", {"status": "running", "current": entry, "pid": os.getpid()})
            print(f"Solving {entry['name']} ({entry['id']}) with {batch['solver_mpi_ranks']} MPI ranks", flush=True)
            try:
                run_imported_case(job / "job.json")
            except Exception as exc:
                print(f"{entry['name']}: {exc}", flush=True)
                # The worker records the failure before raising.
                if read(job / "status.json").get("status") == "queued":
                    raise
    _write_json(output / "status.json", {"status": "complete", "finished_at": time.time(), "jobs": batch["jobs"]})
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Experiment YAML; default: configs/experiments/frame_fit_water.yaml")
    parser.add_argument("--output", type=Path, required=True, help="New directory, or the same directory to resume queued work")
    parser.add_argument("--jobs-root", type=Path, default=SOURCE_ROOT / "build/imported_runs")
    parser.add_argument("--ranks", type=int, help="Solver MPI processes; defaults to platform detection (Windows: 4)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prepare-only", action="store_true", help="Generate and verify CAD/input manifests only (default)")
    action.add_argument("--run", action="store_true", help="Run mesh, S0, S1, S2, validation and cumulative publication")
    parser.add_argument("--retry-failed", action="store_true", help="Archive/delete a failed job and start once with a new UUID")
    args = parser.parse_args(argv)
    if args.ranks is not None and args.ranks < 1:
        parser.error("--ranks must be positive")
    if args.retry_failed and not args.run:
        parser.error("--retry-failed requires --run")
    output, jobs = args.output.resolve(), args.jobs_root.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (output / "batch.lock").open("a+b") as stream:
        lock_file(stream)
        if args.run:
            check_docker()
        manifest = output / "batch.json"
        existing = read(manifest) if manifest.exists() else None
        ranks = args.ranks or (existing["solver_mpi_ranks"] if existing else int(os.environ.get("OPENFOAM_MPI_RANKS", default_solver_mpi_ranks())))
        if ranks < 1:
            parser.error("Solver MPI ranks must be positive")
        if existing and args.config is None:
            batch = existing
            if Path(batch["jobs_root"]) != jobs or batch["solver_mpi_ranks"] != ranks:
                parser.error("A prepared batch freezes jobs-root and ranks. Use a new --output.")
        else:
            batch = prepare_batch((args.config or DEFAULT_CONFIG).resolve(), output, jobs, ranks)
        print(f"Prepared {len(batch['jobs'])} cases: {manifest}", flush=True)
        if args.run:
            return 0 if run_batch(output, batch, retry_failed=args.retry_failed) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
