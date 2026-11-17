#!/usr/bin/env python3
"""Atomically update ROM campaign case and execution state."""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path


EXTRA_FIELDS = ("attempt_id", "container_name", "run_dir", "updated_at_utc")


def _atomic_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.replace(temporary, path)


def _atomic_json(path: Path, value: object) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--status", choices=("RUNNING", "COMPLETE", "FAILED"), required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--stage", choices=("S0", "S1", "S2"))
    args = parser.parse_args()

    completion_evidence: dict[str, object] | None = None
    if args.status == "COMPLETE":
        run_path = Path(args.run_dir)
        frozen_report_path = run_path / "reports/solver/frozen_flow_validation.json"
        energy_report_path = run_path / "reports/solver/energy_balance.json"
        solver_log_path = run_path / "logs/solver/frozen_flow_actual_1s.log"
        probe_path = (
            run_path
            / "postProcessing/actual_battery_near_surface_temperature_1s/"
            "batterySolid/0/T"
        )
        frozen_report = json.loads(frozen_report_path.read_text(encoding="utf-8"))
        energy_report = json.loads(energy_report_path.read_text(encoding="utf-8"))
        log_tail = solver_log_path.read_text(encoding="utf-8", errors="replace")[-8192:]
        probe_rows = sum(
            bool(line.strip()) and not line.lstrip().startswith("#")
            for line in probe_path.read_text(encoding="utf-8", errors="replace").splitlines()
        )
        final_time = float(frozen_report.get("final_summary", {}).get("final_time_s", -1))
        checks = {
            "clean_end": any(line.strip() == "End" for line in log_tail.splitlines()),
            "frozen_flow_validation_passed": frozen_report.get("status")
            == "FROZEN_FLOW_VALIDATION_PASSED",
            "energy_balance_passed": energy_report.get("status")
            == "TRANSIENT_ENERGY_BALANCE_PASSED",
            "final_time_is_1800_s": final_time == 1800.0,
            "one_second_probe_rows_1_to_1800": probe_rows == 1800,
        }
        if not all(checks.values()):
            raise ValueError(f"FOM completion validation failed: {checks}")
        completion_evidence = {
            "checks": checks,
            "frozen_flow_report": str(frozen_report_path),
            "energy_balance_report": str(energy_report_path),
            "solver_log": str(solver_log_path),
            "one_second_probe": str(probe_path),
            "final_time_s": final_time,
            "probe_rows": probe_rows,
        }

    csv_path = args.root / "doe_cases.csv"
    manifest_path = args.root / "campaign_manifest.json"
    with csv_path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    for field in EXTRA_FIELDS:
        if field not in fieldnames:
            fieldnames.append(field)
    matches = [row for row in rows if row["case_id"] == args.case_id]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one DOE row for {args.case_id}")
    row = matches[0]
    previous_status = row["status"]
    previous_attempt_id = row.get("attempt_id", "")
    allowed = {
        "RUNNING": {"PLANNED", "FAILED", "RUNNING"},
        "COMPLETE": {"RUNNING"},
        "FAILED": {"RUNNING"},
    }
    if row["status"] not in allowed[args.status]:
        raise ValueError(f"invalid state transition {row['status']} -> {args.status}")
    if previous_status == "RUNNING" and args.status == "RUNNING":
        if previous_attempt_id != args.attempt_id:
            raise ValueError("a running case cannot switch attempt IDs during a stage update")
    now = datetime.now(timezone.utc).isoformat()
    row.update(
        {
            "status": args.status,
            "attempt_id": args.attempt_id,
            "container_name": args.container_name,
            "run_dir": args.run_dir,
            "updated_at_utc": now,
        }
    )
    temporary_csv = csv_path.with_suffix(".csv.tmp")
    with temporary_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary_csv, csv_path)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    execution = manifest.setdefault("execution", {})
    execution.update(
        {
            "state": args.status,
            "active_case_id": args.case_id if args.status == "RUNNING" else None,
            "active_attempt_id": args.attempt_id if args.status == "RUNNING" else None,
            "container_name": args.container_name if args.status == "RUNNING" else None,
            "run_dir": args.run_dir,
            "updated_at_utc": now,
            "completed_cases": sum(item["status"] == "COMPLETE" for item in rows),
        }
    )
    planned = [item["case_id"] for item in rows if item["status"] == "PLANNED"]
    execution["next_case_id"] = planned[0] if planned else None
    _atomic_json(manifest_path, manifest)

    attempts = args.root / "fom_cases" / args.case_id / "attempts" / args.attempt_id
    attempt_manifest_path = attempts / "attempt_manifest.json"
    if args.status == "RUNNING":
        if attempt_manifest_path.is_file():
            attempt_manifest = json.loads(attempt_manifest_path.read_text(encoding="utf-8"))
        else:
            attempts.mkdir(parents=True, exist_ok=False)
            attempt_manifest = {
                "schema_version": 1,
                "case_id": args.case_id,
                "attempt_id": args.attempt_id,
                "parameters": {
                    "inlet_velocity_m_s": float(row["inlet_velocity_m_s"]),
                    "coolant_inlet_temperature_k": float(row["coolant_inlet_temperature_k"]),
                    "heat_source_multiplier": float(row["heat_source_multiplier"]),
                },
                "split": row["split"],
                "design_source": row["design_source"],
            }
        attempt_manifest["container_name"] = args.container_name
        attempt_manifest["run_dir"] = args.run_dir
        if args.stage:
            previous_stage = attempt_manifest.get("current_stage")
            stages = attempt_manifest.setdefault("stages", {})
            if previous_stage and previous_stage != args.stage:
                previous_record = stages.setdefault(previous_stage, {})
                previous_record["status"] = "PASSED"
                previous_record["completed_at_utc"] = now
            stages[args.stage] = {
                "status": "RUNNING",
                "container_name": args.container_name,
                "run_dir": args.run_dir,
                "updated_at_utc": now,
            }
            attempt_manifest["current_stage"] = args.stage
    else:
        if not attempt_manifest_path.is_file():
            raise FileNotFoundError(attempt_manifest_path)
        attempt_manifest = json.loads(attempt_manifest_path.read_text(encoding="utf-8"))
    attempt_manifest["status"] = args.status
    attempt_manifest["updated_at_utc"] = now
    if completion_evidence is not None:
        attempt_manifest["completion_evidence"] = completion_evidence
    _atomic_json(attempt_manifest_path, attempt_manifest)
    print(json.dumps({"status": args.status, "case_id": args.case_id, "attempt_id": args.attempt_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
