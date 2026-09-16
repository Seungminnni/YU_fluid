"""Validate a completed, isolated 849k adaptive CHT trial without touching baseline results.

This is a reproducibility tool for the 0-1800 s trial that was continued in
five solver segments. It refuses missing physical times, keeps every report in
the trial directory, and never publishes results to the reference web viewer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from cfd_twin.case_builder import _docker_command
from cfd_twin.openfoam_log_merge import merge_solver_logs


SEGMENTS = (
    "solver.log", "solver_1_to_10.log", "solver_10_to_40.log",
    "solver_40_to_65.log", "solver_65_to_1800.log",
)


def _run(command: list[str], log_path: Path) -> int:
    with log_path.open("w", encoding="utf-8") as output:
        return subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, check=False).returncode


def _energy_balance_passed(report_path: Path) -> bool:
    """The balance CLI exits zero even when its numerical gate is a warning."""

    report = json.loads(report_path.read_text(encoding="utf-8"))
    gate = report.get("energy_balance_gate", {})
    return report.get("status") == "TRANSIENT_ENERGY_BALANCE_PASSED" and gate.get("passed") is True


def finalize(trial: Path, reference: Path, *, image: str) -> dict[str, object]:
    trial = trial.resolve()
    reference = reference.resolve()
    if trial == reference or reference.is_relative_to(trial) or trial.is_relative_to(reference):
        raise ValueError("trial and preserved reference case must be separate directories")
    reports = trial / "reports/solver"
    reports.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {"status": "INCOMPLETE", "trial": str(trial), "reference": str(reference), "stages": {}}
    summary = reports / "adaptive_trial_validation.json"

    def save() -> None:
        summary.write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    save()
    try:
        reference_run = reference / "runs/frozen_flow_actual_1s"
        for relative in (
            "constant/coolant/polyMesh/points",
            "constant/batterySolid/polyMesh/points",
            "constant/batterySolid/fvOptions",
        ):
            candidate = trial / relative
            original = reference_run / relative
            if hashlib.sha256(candidate.read_bytes()).digest() != hashlib.sha256(original.read_bytes()).digest():
                raise RuntimeError(f"trial and preserved reference differ: {relative}")
        if "internalField   uniform 293;" not in (trial / "0/coolant/T").read_text(encoding="utf-8"):
            raise RuntimeError("trial fluid was not initially 293 K")
        if "internalField   uniform 293.15;" not in (trial / "0/batterySolid/T").read_text(encoding="utf-8"):
            raise RuntimeError("trial battery was not initially 293.15 K")
        result["stages"]["reference_identity"] = {"status": "MATCHED_MESH_AND_DYNAMIC_HEAT_SOURCE"}
        save()
        final_log = trial / SEGMENTS[-1]
        with final_log.open("rb") as stream:
            stream.seek(max(0, stream.seek(0, 2) - 4096))
            tail = stream.read().decode("utf-8", errors="replace")
        if "\nEnd\n" not in tail:
            raise RuntimeError("last solver segment has no clean OpenFOAM End marker")
        merged = reports / "adaptive_solver_1_to_1800.log"
        result["stages"]["merge"] = merge_solver_logs(
            [trial / name for name in SEGMENTS], merged, first_time=1, last_time=1800,
        )
        save()

        if not (trial / "1800/coolant/T").is_file() or not (trial / "1800/batterySolid/T").is_file():
            docker = _docker_command()
            if docker is None:
                raise RuntimeError("Docker Desktop is required to reconstruct final OpenFOAM fields")
            command = [
                docker, "run", "--rm", "-v", f"{trial}:/case", image,
                "/bin/bash", "-lc",
                "source /usr/lib/openfoam/openfoam2512/etc/bashrc && "
                "reconstructPar -case /case -allRegions -latestTime",
            ]
            code = _run(command, reports / "reconstruct_final.log")
            result["stages"]["reconstruct"] = {"exit_code": code}
            save()
            if code:
                raise RuntimeError("reconstructPar failed; see reconstruct_final.log")

        steady_report = reference / "runs/steady_cht/reports/solver/steady_cht_validation.json"
        mesh_report = reference / "reports/production_mesh/mesh_integrals_pre_split.json"
        source_samples = reference / "heat_source_reference/heat_source_samples.csv"
        for path in (steady_report, mesh_report, source_samples):
            if not path.is_file():
                raise FileNotFoundError(path)

        calls = (
            ("residuals", "cfd_twin.openfoam_transient_residuals", [
                "--log", str(merged), "--csv", str(reports / "transient_residuals.csv"),
                "--summary", str(reports / "transient_residuals_summary.json"),
            ]),
            ("coupling_audit", "cfd_twin.openfoam_energy_coupling_audit", [
                "--log", str(merged), "--threshold", "1e-5", "--expected-cap", "200",
                "--output", str(reports / "energy_coupling_audit.json"),
            ]),
            ("frozen_flow_validation", "cfd_twin.openfoam_frozen_flow_validation", [
                "--log", str(merged), "--run-dir", str(trial),
                "--steady-report", str(steady_report),
                "--mesh-integral-report", str(mesh_report),
                "--qvol", "78699.3333524", "--qvol-samples", str(source_samples),
                "--coolant-cp", "4182", "--initial-temperature", "293.15",
                "--steady-initial-temperature", "293", "--field-interval", "1",
                "--output", str(reports / "frozen_flow_validation.json"),
            ]),
            ("energy_balance", "cfd_twin.openfoam_energy_balance", [
                "--run-dir", str(trial), "--mesh-integral-report", str(mesh_report),
                "--qvol-samples", str(source_samples),
                "--fluid-density", "998.2", "--fluid-cp", "4182",
                "--solid-density", "2000", "--solid-cp", "900",
                "--initial-fluid-temperature", "293",
                "--initial-solid-temperature", "293.15",
                "--maximum-final-window-imbalance", "0.02",
                "--output-csv", str(reports / "energy_balance.csv"),
                "--output-report", str(reports / "energy_balance.json"),
            ]),
        )
        failed: list[str] = []
        for name, module, arguments in calls:
            code = _run([sys.executable, "-m", module, *arguments], reports / f"{name}.log")
            result["stages"][name] = {"exit_code": code}
            if name == "coupling_audit" and code == 0:
                audit = json.loads((reports / "energy_coupling_audit.json").read_text(encoding="utf-8"))
                result["stages"][name]["no_inner_loop_outer_h_gate_passed"] = audit[
                    "coupling_not_entered_outer_h_gate_passed"
                ]
                if not audit["coupling_not_entered_outer_h_gate_passed"]:
                    failed.append(name)
            if name == "energy_balance" and code == 0:
                balance_passed = _energy_balance_passed(reports / "energy_balance.json")
                result["stages"][name]["numerical_gate_passed"] = balance_passed
                if not balance_passed:
                    failed.append(name)
            if code:
                failed.append(name)
            save()
        if failed:
            raise RuntimeError("validation stages failed: " + ", ".join(failed))
        result["status"] = "ADAPTIVE_TRIAL_VALIDATION_PASSED"
    except Exception as exc:
        result["status"] = "ADAPTIVE_TRIAL_VALIDATION_FAILED"
        result["error"] = str(exc)
    save()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-dir", type=Path, required=True)
    parser.add_argument("--reference-case-dir", type=Path, required=True)
    parser.add_argument("--docker-image", default="cfd-twin-adaptive-v2512:pilot")
    args = parser.parse_args()
    result = finalize(args.trial_dir, args.reference_case_dir, image=args.docker_image)
    print(json.dumps({"status": result["status"],
                      "report": str(args.trial_dir.resolve() / "reports/solver/adaptive_trial_validation.json"),
                      "error": result.get("error")}, ensure_ascii=False))
    return 0 if result["status"] == "ADAPTIVE_TRIAL_VALIDATION_PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
