#!/usr/bin/env python3
"""Start one prepared experiment only after another batch completes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prerequisite", type=Path, required=True)
    parser.add_argument("--dependent", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument(
        "--recover-prerequisite", action="store_true",
        help="On needs_attention, resume once with the current diagnostic retry engine",
    )
    args = parser.parse_args()
    prerequisite = args.prerequisite.resolve()
    dependent = args.dependent.resolve()
    if not (prerequisite / "batch.json").is_file() or not (dependent / "batch.json").is_file():
        raise ValueError("Both prerequisite and dependent batches must already be prepared")
    status_path = dependent / "dependency_status.json"
    write(status_path, {
        "status": "waiting", "prerequisite": str(prerequisite),
        "dependent": str(dependent), "pid": os.getpid(),
    })
    prerequisite_recovery_used = False
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "src")
    while True:
        status = read(prerequisite / "status.json")
        state = status.get("status")
        if state == "complete":
            break
        if state == "needs_attention":
            if args.recover_prerequisite and not prerequisite_recovery_used:
                prerequisite_recovery_used = True
                write(status_path, {
                    "status": "recovering_prerequisite",
                    "prerequisite_status": status, "pid": os.getpid(),
                })
                with (prerequisite / "automatic_recovery_runner.log").open("ab") as output:
                    result = subprocess.run(
                        [sys.executable, str(root / "tools/run_experiment.py"),
                         "--output", str(prerequisite), "--run"],
                        cwd=root, env=environment, stdin=subprocess.DEVNULL,
                        stdout=output, stderr=subprocess.STDOUT, check=False,
                    )
                recovered = read(prerequisite / "status.json")
                if result.returncode == 0 and recovered.get("status") == "complete":
                    continue
                write(status_path, {
                    "status": "prerequisite_needs_attention",
                    "prerequisite_status": recovered,
                    "recovery_returncode": result.returncode,
                    "pid": os.getpid(),
                })
                return 2
            write(status_path, {
                "status": "prerequisite_needs_attention",
                "prerequisite_status": status, "pid": os.getpid(),
            })
            return 2
        time.sleep(max(10, args.poll_seconds))
    write(status_path, {"status": "starting", "pid": os.getpid(), "started_at": time.time()})
    with (dependent / "dependent_runner.log").open("ab") as output:
        result = subprocess.run(
            [sys.executable, str(root / "tools/run_experiment.py"), "--output", str(dependent), "--run"],
            cwd=root, env=environment, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, check=False,
        )
    write(status_path, {
        "status": "complete" if result.returncode == 0 else "needs_attention",
        "returncode": result.returncode, "finished_at": time.time(), "pid": os.getpid(),
    })
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
