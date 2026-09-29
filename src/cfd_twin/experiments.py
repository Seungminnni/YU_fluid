"""Portable, reproducible batches for new water experiments and CAD overlays."""
from __future__ import annotations

import argparse
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
from .workflow_run import (
    BASE_CONFIG, SOURCE_ROOT, STEP_ROLES, THERMAL_OVERLAY, _operation_overrides,
    _write_json, prepare_imported_run, prepare_solver_inputs, run_imported_case,
    stage_uploaded_step,
)

DEFAULT_CONFIG = SOURCE_ROOT / "configs/experiments/frame_fit_water.yaml"


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
                              "geometry_overrides": overrides})
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
        # build; actual mesh reports must exist first.
        return (job / "case/reports/production_mesh/mesh_target_composition.json").is_file() or (job / "case/reports/production_mesh/layer_coverage.json").is_file()
    report = job / "case/runs/steady_cold_flow/reports/solver/cold_flow_validation.json"
    return state.get("stage") == "steady" and report.is_file() and read(report).get("checks", {}).get("battery_yplus_gate_passed") is False


def fresh_retry(output: Path, batch: dict, entry: dict, specification: dict, *, profile_index: int) -> None:
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
    for relative in ("case/reports/production_mesh", "case/diagnostics/wall", "case/runs/steady_cold_flow/reports/solver"):
        source = previous / relative
        if source.is_dir():
            shutil.copytree(source, archive / relative, dirs_exist_ok=True)
    snappy = previous / "case/logs/production_mesh/snappyHexMesh.log"
    if snappy.is_file():
        shutil.copy2(snappy, archive / "snappyHexMesh.log")
    worker_log = previous / "worker.log"
    if worker_log.is_file():
        with worker_log.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 65536))
            (archive / "worker_tail.log").write_bytes(stream.read())
    replacement = _queue(Path(batch["jobs_root"]), prior, prior["files"], specification,
                         profile_index=profile_index, retry_of=previous.name)
    history = entry.setdefault("prior_attempts", [])
    history.append({"id": previous.name, "status": state, "diagnostics_archive": str(archive), "output_deleted": False})
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
                next_profile = entry["profile_index"] + 1
                if _retryable(job, state) and next_profile < len(specification["mesh_profiles"]):
                    fresh_retry(output, batch, entry, specification, profile_index=next_profile)
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
