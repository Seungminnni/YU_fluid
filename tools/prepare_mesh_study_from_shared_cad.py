#!/usr/bin/env python3
"""Prepare fresh mesh-study jobs from immutable shared STEP inputs only."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time

from cfd_twin.experiments import _queue, load_plan, read
from cfd_twin.workflow_run import SOURCE_ROOT, STEP_ROLES, _write_json, prepare_solver_inputs


REQUIRED_CAD_FILES = (
    "cad_result.json", "cad_manifest.json", "resolved_config.json",
    "production_surfaces/outerDomain.stl", "production_surfaces/inlet.stl",
    "production_surfaces/outlet.stl", "production_surfaces/outerWall.stl",
    *(f"production_surfaces/battery_C{i:02d}.stl" for i in range(1, 24)),
)


def verified_prevalidated_cad(root: Path, expected_target: int) -> dict:
    name = (
        "phi19_mesh_800k_smooth_v2" if expected_target == 800_000
        else "phi19_mesh_1000k_smooth_v2"
    )
    direct = root.resolve()
    source = direct if (direct / "cad_result.json").is_file() else (direct / name).resolve()
    if source != direct and source.parent != direct:
        raise ValueError(f"Unsafe shared prevalidated CAD source: {source}")
    if not source.is_dir():
        raise ValueError(f"Shared prevalidated CAD source is missing: {source}")
    result = read(source / "cad_result.json")
    if result.get("status") != "CAD_EXPORTED_GEOMETRY_CHECKS_PASSED" or result.get("cad_geometry_checks_passed") is not True:
        raise ValueError(f"Shared CAD geometry checks did not pass: {source}")
    expected_surface_hashes = result.get("file_sha256", {})
    inventory = {}
    for relative in REQUIRED_CAD_FILES:
        candidate = source / relative
        if not candidate.is_file():
            raise ValueError(f"Shared CAD file is missing: {candidate}")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if relative.startswith("production_surfaces/") and expected_surface_hashes.get(relative) != digest:
            raise ValueError(f"Shared CAD manifest hash mismatch: {candidate}")
        inventory[relative] = digest
    return {"path": str(source), "file_sha256": inventory}


def verified_shared_source(entry: dict, expected_target: int, jobs_root: Path) -> dict:
    job_dir = Path(entry["job_dir"]).resolve()
    if job_dir.parent != (jobs_root / "runs").resolve():
        raise ValueError(f"Unsafe source job: {job_dir}")
    state = read(job_dir / "status.json")
    if state != {"message": "Prepared experiment; awaiting --run", "stage": "cad", "status": "queued"}:
        raise ValueError(f"Source is not an untouched queued CAD input: {job_dir.name} {state}")
    if (job_dir / "case").exists():
        raise ValueError(f"Source unexpectedly contains generated mesh/solver output: {job_dir.name}")
    manifest = read(job_dir / "job.json")
    if manifest["inputs"]["mesh_target_cells"] != expected_target:
        raise ValueError(f"Wrong shared CAD target for {expected_target}: {job_dir.name}")
    if manifest["geometry"].get("status") != "SUPPORTED_23CELL_PORT_VARIANT":
        raise ValueError(f"Shared CAD geometry was not validated: {job_dir.name}")
    if manifest["geometry"].get("battery_count") != 23:
        raise ValueError(f"Shared CAD does not contain 23 batteries: {job_dir.name}")
    files = {}
    for role in STEP_ROLES:
        source = Path(manifest["files"][role]).resolve()
        if source.is_relative_to(job_dir) or source.parent != (jobs_root / "staging").resolve():
            raise ValueError(f"{role} is not a shared staged input: {source}")
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != source.stem:
            raise ValueError(f"Shared STEP hash mismatch: {source}")
        files[role] = str(source)
    return {"manifest": manifest, "files": files}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-batch", type=Path, required=True)
    parser.add_argument("--prevalidated-cad-root", type=Path)
    parser.add_argument("--jobs-root", type=Path, default=SOURCE_ROOT / "build/imported_runs")
    parser.add_argument("--ranks", type=int, default=12)
    args = parser.parse_args()

    output, jobs_root = args.output.resolve(), args.jobs_root.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"Output must be a new empty directory: {output}")
    if args.ranks < 1:
        raise ValueError("MPI ranks must be positive")
    plan = load_plan(args.config.resolve())
    source_batch = read(args.source_batch.resolve() / "batch.json")
    source_by_target = {}
    for entry in source_batch["jobs"]:
        source_job = read(Path(entry["job_dir"]) / "job.json")
        source_by_target[source_job["inputs"]["mesh_target_cells"]] = entry

    plan_hash = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    output.mkdir(parents=True, exist_ok=False)
    _write_json(output / "preparation_started.json", {
        "time": time.time(), "plan_sha256": plan_hash,
        "cad_source": "hash_verified_shared_staging",
    })
    batch = {
        "schema_version": 1, "status": "prepared", "plan": plan,
        "plan_sha256": plan_hash, "solver_mpi_ranks": args.ranks,
        "mesh_mpi_ranks": 1, "jobs_root": str(jobs_root), "jobs": [],
        "cad_source": "hash_verified_shared_staging",
    }
    for specification in plan["cases"]:
        target = specification["inputs"]["mesh_target_cells"]
        if not prepare_solver_inputs(jobs_root, specification["inputs"])["can_start_new_solver"]:
            raise ValueError("At least 55 GiB free disk space is required")
        shared = verified_shared_source(source_by_target[target], target, jobs_root)
        source_manifest = shared["manifest"]
        if source_manifest["inputs"] != specification["inputs"]:
            raise ValueError(f"Operating inputs changed for {target}")
        if source_manifest["geometry_overrides"] != specification["geometry_overrides"]:
            raise ValueError(f"Geometry/physics overrides changed for {target}")
        prepared = {
            "inputs": source_manifest["inputs"],
            "geometry": source_manifest["geometry"],
            "confirmation": source_manifest["confirmation"],
        }
        entry = _queue(jobs_root, prepared, shared["files"], specification)
        if args.prevalidated_cad_root is not None:
            frozen = verified_prevalidated_cad(args.prevalidated_cad_root.resolve(), target)
            job_path = Path(entry["job_dir"]) / "job.json"
            job = read(job_path)
            job["prevalidated_cad"] = frozen
            _write_json(job_path, job)
            entry["cad_source"] = "hash_verified_prevalidated_shared_surfaces"
        batch["jobs"].append(entry)
    _write_json(output / "batch.json", batch)
    print(json.dumps({"output": str(output), "jobs": batch["jobs"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
