#!/usr/bin/env python3
"""Attach a reviewed automatic-recovery plan to a fully queued batch."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time

from cfd_twin.experiments import load_plan, read
from cfd_twin.workflow_run import _write_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--batch", type=Path, required=True)
    args = parser.parse_args()

    batch_dir = args.batch.resolve()
    manifest = batch_dir / "batch.json"
    batch = read(manifest)
    old_plan = batch["plan"]
    new_plan = load_plan(args.config.resolve())
    if old_plan.get("name") != new_plan.get("name"):
        raise ValueError("Plan name changed")
    if len(old_plan["cases"]) != len(new_plan["cases"]) or len(batch["jobs"]) != len(new_plan["cases"]):
        raise ValueError("Case count changed")

    rows = []
    for old, new, entry in zip(old_plan["cases"], new_plan["cases"], batch["jobs"], strict=True):
        for key in ("name", "inputs", "geometry_overrides"):
            if old[key] != new[key]:
                raise ValueError(f"Physical or geometry input changed for {old['name']}: {key}")
        job_dir = Path(entry["job_dir"]).resolve()
        state = read(job_dir / "status.json")
        job = read(job_dir / "job.json")
        if state != {"message": "Prepared experiment; awaiting --run", "stage": "cad", "status": "queued"}:
            raise ValueError(f"Only untouched queued jobs can be upgraded: {job_dir.name} {state}")
        if job.get("mesh_profile") != new["mesh_profiles"][0]:
            raise ValueError(f"Initial mesh profile changed for {old['name']}")
        rows.append({
            "name": new["name"], "job_id": job_dir.name,
            "mesh_profiles": new["mesh_profiles"],
            "automatic_retry": new["automatic_retry"],
        })

    plan_hash = hashlib.sha256(json.dumps(new_plan, sort_keys=True).encode()).hexdigest()
    batch["plan"] = new_plan
    batch["plan_sha256"] = plan_hash
    batch["automatic_retry_enabled_at"] = time.time()
    _write_json(manifest, batch)
    report = {
        "schema_version": 1,
        "status": "AUTOMATIC_RETRY_ENABLED",
        "plan_sha256": plan_hash,
        "batch": str(batch_dir),
        "cases": rows,
        "note": "Physical inputs and acceptance gates are unchanged; only bounded fresh-run remedies were added.",
    }
    _write_json(batch_dir / "automatic_retry_plan.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
