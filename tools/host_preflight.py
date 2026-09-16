"""Reproducible Mac/Windows preflight for the source CAD CFD workflow.

This intentionally does not run the expensive mesh or S0/S1/S2 solvers.
It proves that the host can generate the real reference CAD and both mesh
source bundles, and that Docker Compose accepts each bundle's configuration.
The JSON report must not be mistaken for an end-to-end CFD validation.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

from cfd_twin.cad import execute_cad_source
from cfd_twin.case_builder import _docker_command, dependency_report
from cfd_twin.config import load_config
from cfd_twin.production_mesh import write_production_mesh_source_bundle


ROOT = Path(__file__).resolve().parents[1]


def verify(output: Path) -> dict[str, object]:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, object] = {
        "status": "INCOMPLETE",
        "scope": "host dependencies, baseline CAD, 700k/800k source bundles, Docker Compose syntax",
        "full_mesh_and_cfd_verified": False,
        "output": str(output),
        "host": dependency_report(),
        "profiles": {},
    }
    report_path = output / "host_preflight.json"

    def save() -> None:
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    save()
    try:
        host = report["host"]
        if not isinstance(host, dict) or not host.get("workflow_host_ready"):
            raise RuntimeError("Python CAD/VTK dependencies or Docker Engine are unavailable")
        config = load_config(
            ROOT / "configs/baseline_23cell.yaml",
            overlay_paths=(ROOT / "configs/ppt_bernardi_udf.yaml",),
        )
        cad_dir = output / "cad"
        cad_console = io.StringIO()
        with contextlib.redirect_stdout(cad_console):
            cad_result = execute_cad_source(config, cad_dir)
        (output / "cad_generation_stdout.txt").write_text(cad_console.getvalue(), encoding="utf-8")
        report["cad"] = cad_result
        if not cad_result.get("cad_geometry_checks_passed"):
            raise RuntimeError("generated CAD did not pass its geometry checks")
        docker = _docker_command()
        if docker is None:
            raise RuntimeError("Docker Desktop command disappeared after host check")

        profiles = report["profiles"]
        assert isinstance(profiles, dict)
        for profile in ("production_700k", "production_800k"):
            case_dir = output / profile
            manifest = write_production_mesh_source_bundle(
                config, cad_dir, case_dir, mesh_level=profile,
            )
            result = subprocess.run(
                [docker, "compose", "-f", str(case_dir / "compose.yaml"), "config", "-q"],
                cwd=case_dir, capture_output=True, text=True, check=False, timeout=30,
            )
            profiles[profile] = {
                "source_bundle_created": True,
                "target_cells": manifest["target_cell_count"],
                "compose_valid": result.returncode == 0,
                "compose_error": result.stderr[-2000:] if result.returncode else "",
            }
            save()
            if result.returncode:
                raise RuntimeError(f"{profile}: Docker Compose rejected generated config")
        report["status"] = "HOST_PREFLIGHT_PASSED"
    except Exception as exc:
        report["status"] = "HOST_PREFLIGHT_FAILED"
        report["error"] = str(exc)
    save()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify(args.output)
    except FileExistsError:
        parser.error("output directory already exists; choose a new directory to preserve prior evidence")
    print(json.dumps({
        "status": report["status"],
        "report": str(Path(args.output).resolve() / "host_preflight.json"),
        "profiles": report.get("profiles", {}),
        "full_mesh_and_cfd_verified": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "HOST_PREFLIGHT_PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
