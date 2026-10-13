#!/usr/bin/env python3
"""Preserve shared CAD inputs and replace a stopped field-backfill batch."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import time

from cfd_twin.experiments import _queue, load_plan, read
from cfd_twin.workflow_run import SOURCE_ROOT, STEP_ROLES, _write_json, prepare_solver_inputs


REQUIRED_CAD_FILES = (
    "cad_result.json", "cad_manifest.json", "resolved_config.json",
    "production_surfaces/outerDomain.stl", "production_surfaces/inlet.stl",
    "production_surfaces/outlet.stl", "production_surfaces/outerWall.stl",
    *(f"production_surfaces/battery_C{i:02d}.stl" for i in range(1, 24)),
)


def hash_inventory(source: Path) -> dict[str, str]:
    result = read(source / "cad_result.json")
    if result.get("status") != "CAD_EXPORTED_GEOMETRY_CHECKS_PASSED" or result.get("cad_geometry_checks_passed") is not True:
        raise ValueError(f"Shared CAD geometry checks did not pass: {source}")
    declared = result.get("file_sha256", {})
    inventory = {}
    for relative in REQUIRED_CAD_FILES:
        candidate = source / relative
        if not candidate.is_file():
            raise ValueError(f"Shared CAD file is missing: {candidate}")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if relative.startswith("production_surfaces/") and declared.get(relative) != digest:
            raise ValueError(f"Shared CAD manifest hash mismatch: {candidate}")
        inventory[relative] = digest
    return inventory


def shared_steps(job: dict, jobs_root: Path) -> dict[str, str]:
    result = {}
    staging = (jobs_root / "staging").resolve()
    for role in STEP_ROLES:
        source = Path(job["files"][role]).resolve()
        if source.parent != staging or not source.is_file():
            raise ValueError(f"STEP is not a shared staged input: {source}")
        if hashlib.sha256(source.read_bytes()).hexdigest() != source.stem:
            raise ValueError(f"Shared STEP hash mismatch: {source}")
        result[role] = str(source)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source-batch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--shared-cad-root", type=Path,
        default=SOURCE_ROOT / "build/imported_runs/shared_cad/rerun_missing_pressure_htc_20261007",
    )
    args = parser.parse_args()

    config_path = args.config.resolve()
    source_root = args.source_batch.resolve()
    output = args.output.resolve()
    shared_root = args.shared_cad_root.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"Output must be a new empty directory: {output}")
    source_batch = read(source_root / "batch.json")
    plan = load_plan(config_path)
    if source_batch.get("plan") != plan:
        raise ValueError("Frozen source plan differs from the requested configuration")
    if len(source_batch["jobs"]) != len(plan["cases"]):
        raise ValueError("Source job count differs from the frozen plan")
    jobs_root = Path(source_batch["jobs_root"]).resolve()
    output.mkdir(parents=True, exist_ok=False)
    shared_root.mkdir(parents=True, exist_ok=True)
    plan_hash = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    batch = {
        "schema_version": 1,
        "status": "prepared",
        "plan": plan,
        "plan_sha256": plan_hash,
        "solver_mpi_ranks": int(source_batch["solver_mpi_ranks"]),
        "mesh_mpi_ranks": 1,
        "jobs_root": str(jobs_root),
        "jobs": [],
        "cad_source": "content_verified_shared_input_archive",
        "replaces": str(source_root),
    }
    _write_json(output / "preparation_started.json", {
        "time": time.time(), "plan_sha256": plan_hash,
        "reason": "fresh jobs after publish-stage failure; no failed CAD/mesh/field reused",
    })
    for old_entry, specification in zip(source_batch["jobs"], plan["cases"], strict=True):
        old_job_dir = Path(old_entry["job_dir"]).resolve()
        state = read(old_job_dir / "status.json")
        if state.get("status") not in {"failed", "queued"}:
            raise ValueError(f"Unsafe source job state: {old_job_dir.name} {state}")
        if state.get("status") == "failed" and state.get("stage") != "publish":
            raise ValueError(f"Only the reviewed publish-stage failure is supported: {state}")
        old_job = read(old_job_dir / "job.json")
        if old_job["inputs"] != specification["inputs"] or old_job["geometry_overrides"] != specification["geometry_overrides"]:
            raise ValueError(f"Frozen source settings changed: {specification['name']}")
        if not prepare_solver_inputs(jobs_root, specification["inputs"])["can_start_new_solver"]:
            raise ValueError("At least 55 GiB free disk space is required")
        source_cad = (source_root / "cad" / specification["name"]).resolve()
        if source_cad.parent != (source_root / "cad").resolve():
            raise ValueError(f"Unsafe batch-level CAD source: {source_cad}")
        source_inventory = hash_inventory(source_cad)
        frozen_cad = shared_root / specification["name"]
        if frozen_cad.exists():
            if hash_inventory(frozen_cad) != source_inventory:
                raise ValueError(f"Existing shared CAD archive differs: {frozen_cad}")
        else:
            shutil.copytree(source_cad, frozen_cad)
        archived_inventory = hash_inventory(frozen_cad)
        if archived_inventory != source_inventory:
            raise ValueError(f"Shared CAD archive verification failed: {frozen_cad}")
        prepared = {
            "inputs": old_job["inputs"],
            "geometry": old_job["geometry"],
            "confirmation": old_job["confirmation"],
        }
        entry = _queue(
            jobs_root, prepared, shared_steps(old_job, jobs_root), specification,
            retry_of=old_job_dir.name if state.get("status") == "failed" else None,
        )
        new_job_path = Path(entry["job_dir"]) / "job.json"
        new_job = read(new_job_path)
        new_job["prevalidated_cad"] = {
            "path": str(frozen_cad), "file_sha256": archived_inventory,
        }
        _write_json(new_job_path, new_job)
        entry["cad_source"] = "content_verified_shared_input_archive"
        batch["jobs"].append(entry)
    _write_json(output / "batch.json", batch)
    print(json.dumps({"output": str(output), "jobs": batch["jobs"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
