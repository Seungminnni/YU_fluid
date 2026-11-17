#!/usr/bin/env python3
"""Run S0/S1/S2 from a fresh, prevalidated imported structured-sweep mesh."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from cfd_twin.workflow_run import (
    _check_cancel,
    _compose_service,
    _gate,
    _lock_solver_file,
    _read_json,
    _run_steady_stages,
    _run_wall_diagnostic,
    _write_json,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_manifest", type=Path)
    args = parser.parse_args()
    job_manifest = args.job_manifest.resolve()
    job_dir = job_manifest.parent
    case = job_dir / "case"
    cancel = job_dir / "cancel.request"
    lock_path = job_dir.parent.parent / "active_solver.lock"
    job = _read_json(job_manifest)
    runtime_path = job_dir / "execution_runtime.json"
    started = time.time()
    runtime = {"schema_version": 1, "status": "running", "started_at_epoch_s": started, "stage_wall_seconds": {}, "semantics": "fresh conical imported mesh; S0 then S1 then frozen-flow S2"}

    def update(status: str, stage: str, message: str) -> None:
        _write_json(job_dir / "status.json", {"status": status, "stage": stage, "message": message, "pid": os.getpid()})
        _write_json(runtime_path, runtime)
        _write_json(case / "reports/execution_runtime.json", runtime)

    stage = "mesh_validation"
    try:
        _check_cancel(cancel)
        _gate(case / "reports/production_mesh/production_mesh_gate.json", "PRODUCTION_MESH_GATE_PASSED")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            _lock_solver_file(lock)
            stage = "steady"
            stage_started = time.time()
            update("running", stage, "원뿔대 새 메시 S0 유동·S1 정상 CHT 계산")
            with (job_dir / "worker.log").open("ab") as output:
                diagnostic_iterations = job.get("wall_diagnostic_iterations")
                if diagnostic_iterations is not None:
                    _run_wall_diagnostic(case, output, diagnostic_iterations, cancel_path=cancel)
                _run_steady_stages(case, output, cancel_path=cancel, cold_flow_maximum_iterations=job.get("cold_flow_maximum_iterations"))
            _gate(case / "runs/steady_cold_flow/reports/solver/cold_flow_validation.json", "COLD_FLOW_VALIDATION_PASSED")
            _gate(case / "runs/steady_cht/reports/solver/steady_cht_validation.json", "STEADY_CHT_VALIDATION_PASSED")
            runtime["stage_wall_seconds"][stage] = time.time() - stage_started
            stage = "transient"
            stage_started = time.time()
            update("running", stage, "원뿔대 S2 frozen-flow 열과도 0~1800초 계산")
            environment = os.environ.copy()
            environment.update(TRANSIENT_DELTA_T="1", ENERGY_COUPLING_ITERATIONS="200")
            with (job_dir / "worker.log").open("ab") as output:
                code = _compose_service(case, "frozen-flow-actual-1s", output, environment=environment, cancel_path=cancel)
            if code:
                raise RuntimeError(f"transient service exited with {code}")
            _gate(case / "runs/frozen_flow_actual_1s/reports/solver/frozen_flow_validation.json", "FROZEN_FLOW_VALIDATION_PASSED")
            _gate(case / "runs/frozen_flow_actual_1s/reports/solver/energy_balance.json", "TRANSIENT_ENERGY_BALANCE_PASSED")
            runtime["stage_wall_seconds"][stage] = time.time() - stage_started
        runtime.update(status="analysis_complete", total_wall_seconds=time.time() - started, finished_at_epoch_s=time.time())
        update("running", "publish", "원뿔대 FOM 검증 완료; 뷰어 자산 생성 대기")
        return 0
    except Exception as exc:
        runtime.update(status="failed", failed_stage=stage, error=str(exc), elapsed_wall_seconds=time.time() - started)
        update("failed", stage, str(exc))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
