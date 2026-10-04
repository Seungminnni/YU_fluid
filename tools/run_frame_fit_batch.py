#!/usr/bin/env python3
"""Prepare and sequentially solve the approved Ø28/31/34 water port variants.

All three source geometries are frozen before solving. Existing workflow gates
and cumulative publication are used without relaxing acceptance thresholds.
Restarting never silently recomputes a started or failed job.
"""
from __future__ import annotations

import argparse
from cfd_twin.file_lock import lock_file
import json
import os
from pathlib import Path
import shutil
import time
import uuid

from cfd_twin.cad import execute_cad_source
from cfd_twin.config import load_config
from cfd_twin.geometry import geometry_report
from cfd_twin.parallelism import default_solver_mpi_ranks
from cfd_twin.viewer_release import validate_server_viewer
from cfd_twin.workflow_run import (
    STEP_ROLES, prepare_imported_run, run_imported_case, stage_uploaded_step,
)

ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / "build/frame_fit_water_20260929"
JOBS = ROOT / "build/imported_runs"
PREVIEW = ROOT / "viewer/previews/frame_fit_28_31_34_20260929/preview.json"
INPUTS = {
    "coolant": "deionized_water", "inlet_temperature_k": 293.0,
    "inlet_velocity_m_s": 0.4, "outlet_pressure_pa": 101325.0,
    "mesh_target_cells": 600000,
}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def prepare():
    manifest_path = BATCH / "batch.json"
    if manifest_path.exists():
        return read_json(manifest_path)
    if (BATCH / "preparation_started.json").exists():
        raise RuntimeError("Incomplete preparation exists; inspect it before retrying.")
    write_json(BATCH / "preparation_started.json", {"time": time.time()})
    JOBS.mkdir(parents=True, exist_ok=True)
    existing = validate_server_viewer(ROOT / "viewer")
    shutil.copytree(ROOT / "viewer", BATCH / "viewer_before")
    preview = read_json(PREVIEW)
    write_json(BATCH / "approved_preview.json", preview)
    reference = load_config(
        ROOT / "configs/baseline_23cell.yaml",
        overlay_paths=(ROOT / "configs/ppt_bernardi_udf.yaml",
                       ROOT / "configs/recalculation_600k_c05_inlet_c19_outlet_phi25_20260922.yaml"),
    )
    write_json(BATCH / "reference_water_config.json", json.loads(reference.to_json()))
    batch = {"status": "prepared", "solver_mpi_ranks": default_solver_mpi_ranks(),
             "mesh_mpi_ranks": 1, "inputs": INPUTS, "existing_release": existing,
             "jobs": [], "created_at": time.time()}
    for candidate in preview["cases"]:
        diameter = candidate["diameter_mm"]
        assert diameter in (28, 31, 34)
        overrides = (
            f"case_name=frame_fit_water_phi{diameter}_20260929", "mode=imported_23cell",
            f"geometry.ports.inlet_diameter={diameter} mm",
            f"geometry.ports.outlet_diameter={diameter} mm",
            "geometry.ports.inlet_face=top", "geometry.ports.outlet_face=bottom",
            "geometry.ports.inlet_column=1", "geometry.ports.placement=end_cell_center",
            "geometry.ports.center_source=APPROVED_FRAME_FIT_PREVIEW_20260929",
            "geometry.ports.inlet_center_xy_mm=" + json.dumps(candidate["inlet"]["center_xy_mm"]),
            "geometry.ports.outlet_center_xy_mm=" + json.dumps(candidate["outlet"]["center_xy_mm"]),
        )
        config = load_config(ROOT / "configs/baseline_23cell.yaml",
                             overlay_paths=(ROOT / "configs/ppt_bernardi_udf.yaml",),
                             overrides=overrides)
        for field in ("fluid", "battery", "boundary", "solver", "cells", "enclosure"):
            if getattr(config, field) != getattr(reference, field):
                raise RuntimeError(f"Ø{diameter}: reference {field} differs")
        generation = BATCH / f"cad_phi{diameter}"
        write_json(generation / "geometry_report.json", geometry_report(config))
        write_json(generation / "resolved_config.json", json.loads(config.to_json()))
        print(f"Generating Ø{diameter} CAD", flush=True)
        execute_cad_source(config, generation)
        selected = {role: stage_uploaded_step(JOBS, role, (generation / role).read_bytes()) for role in STEP_ROLES}
        preflight = prepare_imported_run(JOBS, selected, INPUTS, geometry_overrides=overrides)
        if not preflight["can_start_new_solver"]:
            raise RuntimeError("Insufficient free disk")
        run_id = uuid.uuid4().hex
        job_dir = JOBS / "runs" / run_id
        job_dir.mkdir(parents=True, exist_ok=False)
        job = {"id": run_id, "status": "queued", "inputs": preflight["inputs"],
               "confirmation": preflight["confirmation"], "geometry": preflight["geometry"],
               "geometry_overrides": list(overrides),
               "files": {role: str(JOBS / "staging" / selected[role]) for role in STEP_ROLES},
               "source": {"kind": "approved_frame_fit", "diameter_mm": diameter,
                          "preview": str(BATCH / "approved_preview.json"),
                          "batch": str(manifest_path)}}
        write_json(job_dir / "job.json", job)
        write_json(job_dir / "status.json", {"status": "queued", "stage": "cad",
                   "message": f"Ø{diameter} 물 시안 · 순차 계산 대기"})
        batch["jobs"].append({"diameter_mm": diameter, "id": run_id, "job_dir": str(job_dir)})
        write_json(BATCH / "preparation_progress.json", batch)
        print(f"Ø{diameter} CAD verified: {run_id}", flush=True)
    write_json(manifest_path, batch)
    return batch


def run(batch):
    os.environ["OPENFOAM_MPI_RANKS"] = str(batch["solver_mpi_ranks"])
    os.environ["OPENFOAM_MESH_MPI_RANKS"] = "1"
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    failures = []
    for entry in batch["jobs"]:
        job_dir = Path(entry["job_dir"])
        state = read_json(job_dir / "status.json")
        if state["status"] == "complete" and not state.get("release_error"):
            continue
        if state["status"] != "queued":
            failures.append({**entry, "status": state})
            continue
        write_json(BATCH / "status.json", {"status": "running", "current": entry,
                   "solver_mpi_ranks": batch["solver_mpi_ranks"], "pid": os.getpid()})
        print(f"Starting Ø{entry['diameter_mm']} with {batch['solver_mpi_ranks']} solver ranks", flush=True)
        try:
            run_imported_case(job_dir / "job.json")
        except Exception as exc:
            print(f"Ø{entry['diameter_mm']} failed: {exc}", flush=True)
        state = read_json(job_dir / "status.json")
        if state["status"] != "complete" or state.get("release_error"):
            failures.append({**entry, "status": state})
        print(f"Ø{entry['diameter_mm']}: {state}", flush=True)
    write_json(BATCH / "status.json", {"status": "failed" if failures else "complete",
               "failures": failures, "finished_at": time.time(), "pid": os.getpid()})


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 or not (BATCH / "batch.json").exists():
        from cfd_twin.experiments import main
        arguments = sys.argv[1:]
        if not any(arg == "--output" or arg.startswith("--output=") for arg in arguments):
            arguments = ["--output", str(BATCH.parent / "experiments/frame_fit_water"), *arguments]
        raise SystemExit(main(arguments))

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    BATCH.mkdir(parents=True, exist_ok=True)
    with (BATCH / "batch.lock").open("a+b") as lock:
        lock_file(lock)
        batch = prepare()
        if not args.prepare_only:
            run(batch)
