#!/usr/bin/env python3
"""Archive diagnostics and remove only superseded mesh-study job outputs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil


ALLOWED_STATUSES = {"failed", "cancelled", "calibration_blocked", "queued"}


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def retire(batch_dir: Path, replacement_batch: Path) -> dict:
    manifest = batch_dir / "batch.json"
    batch = read(manifest)
    runs = (Path(batch["jobs_root"]) / "runs").resolve()
    archive_root = batch_dir / "diagnostic_attempts"
    retired = []
    for entry in batch["jobs"]:
        job = Path(entry["job_dir"])
        resolved = job.resolve()
        if resolved.parent != runs or not re.fullmatch(r"[0-9a-f]{32}", resolved.name):
            raise ValueError(f"Unsafe job path: {job}")
        state = read(job / "status.json")
        if state.get("status") not in ALLOWED_STATUSES:
            raise ValueError(f"Job is not safe to retire: {job.name} {state}")
        if state.get("status") == "queued" and (job / "case").exists():
            raise ValueError(f"Queued job unexpectedly has generated output: {job.name}")
        archive = archive_root / job.name
        if archive.exists():
            archived_job = archive / "job.json"
            archived_status = archive / "status.json"
            if not archived_job.is_file() or not archived_status.is_file():
                raise ValueError(f"Incomplete diagnostic archive cannot be resumed: {archive}")
            if read(archived_job).get("id") != job.name:
                raise ValueError(f"Diagnostic archive belongs to another job: {archive}")
        else:
            archive.mkdir(parents=True)
        for name in ("job.json", "status.json", "execution_runtime.json"):
            source = job / name
            if source.is_file():
                shutil.copy2(source, archive / name)
        for relative in (
            "case/reports/production_mesh",
            "case/reports/steady_numerics.json",
            "case/reports/outlet_boundary.json",
            "case/diagnostics/wall",
            "case/diagnostics/outlet",
            "case/runs/steady_cold_flow/reports/solver",
        ):
            source = job / relative
            target = archive / relative
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        for relative in (
            "case/logs/production_mesh/snappyHexMesh.log",
            "case/runs/steady_cold_flow/logs/solver/steady_cold_flow.log",
        ):
            source = job / relative
            if source.is_file():
                target = archive / (relative.replace("/", "_") + ".tail")
                with source.open("rb") as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - 131072))
                    target.write_bytes(stream.read())
        history = entry.setdefault("prior_attempts", [])
        history.append({
            "id": job.name,
            "status": state,
            "diagnostics_archive": str(archive.resolve()),
            "output_deleted": False,
            "replacement_batch": str(replacement_batch.resolve()),
        })
        write(manifest, batch)
        shutil.rmtree(job)
        history[-1]["output_deleted"] = True
        entry["retired"] = True
        entry["retired_job_dir"] = str(resolved)
        write(manifest, batch)
        retired.append({"id": resolved.name, "archive": str(archive), "output_deleted": True})
    result = {"status": "retired", "replacement_batch": str(replacement_batch), "jobs": retired}
    write(batch_dir / "retirement_20261007.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--replacement-batch", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(retire(args.batch.resolve(), args.replacement_batch.resolve()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
