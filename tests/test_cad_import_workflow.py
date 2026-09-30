"""New CAD is actually imported into the mesh/solver source, never relabeled baseline."""

from __future__ import annotations

import hashlib
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cfd_twin.cad import execute_cad_source
from cfd_twin.cad_import import STEP_ROLES, inspect_imported_steps
from cfd_twin.config import load_config
from cfd_twin.workflow_run import (
    BASE_CONFIG,
    THERMAL_OVERLAY,
    build_imported_case,
    create_imported_run,
    prepare_imported_run,
    _run_steady_stages,
    _worker_process_options,
    stage_uploaded_step,
)


class ImportedCadWorkflowTests(unittest.TestCase):
    def test_worker_detach_flags_are_host_specific(self) -> None:
        self.assertEqual(_worker_process_options("posix"), {"start_new_session": True})
        with patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), \
             patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True):
            self.assertEqual(
                _worker_process_options("nt"),
                {"creationflags": 0x08000200},
            )

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import cadquery  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("CadQuery is not installed")

    def test_changed_port_cad_propagates_to_real_solver_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "changed_cad"
            config = load_config(
                BASE_CONFIG,
                overlay_paths=(THERMAL_OVERLAY,),
                overrides=(
                    "mode=imported_23cell",
                    "geometry.ports.inlet_diameter=20 mm",
                    "geometry.ports.outlet_diameter=20 mm",
                ),
            )
            with contextlib.redirect_stdout(io.StringIO()):
                execute_cad_source(config, generated)
            selected = {
                role: stage_uploaded_step(root / "jobs", role, (generated / role).read_bytes())
                for role in STEP_ROLES
            }
            inputs = {
                "coolant": "deionized_water",
                "inlet_temperature_k": 294.0,
                "inlet_velocity_m_s": 0.45,
                "outlet_pressure_pa": 102000.0,
                "mesh_target_cells": 800000,
            }
            prepared = prepare_imported_run(root / "jobs", selected, inputs)
            self.assertEqual(
                prepared["confirmation"],
                prepare_imported_run(root / "jobs", selected, inputs)["confirmation"],
            )
            self.assertEqual(prepared["geometry"]["status"], "SUPPORTED_23CELL_PORT_VARIANT")
            self.assertIn("can_start_new_solver", prepared)
            self.assertGreater(prepared["disk_minimum_gib"], 40)
            self.assertAlmostEqual(prepared["geometry"]["inlet"]["diameter_mm"], 20.0, places=2)
            self.assertAlmostEqual(prepared["geometry"]["outlet"]["diameter_mm"], 20.0, places=2)
            job_dir = root / "case_job"
            job_dir.mkdir()
            (job_dir / "job.json").write_text(
                json.dumps({
                    "files": {role: str(root / "jobs" / "staging" / selected[role]) for role in STEP_ROLES},
                    "inputs": inputs,
                    "steady_numerics_profile": "pressure_limited_0p5",
                }), encoding="utf-8",
            )
            built = build_imported_case(job_dir)
            case = built["case"]
            self.assertEqual(
                hashlib.sha256((case / "constant/triSurface/inlet.stl").read_bytes()).hexdigest(),
                hashlib.sha256((job_dir / "cad/production_surfaces/inlet.stl").read_bytes()).hexdigest(),
            )
            self.assertIn("1e-05", (case / "profiles/frozen_flow_actual_1s/system/fvSolution").read_text())
            self.assertIn("iterations 200;", (case / "profiles/frozen_flow_actual_1s/system/fvSolution").read_text())
            self.assertIn("102000", (case / "profiles/steady/system/coolant/fvSolution").read_text())
            for stage in ('steady', 'steady_cold_flow'):
                self.assertIn('laplacian(rhorAUf,p_rgh) Gauss linear limited 0.5;',
                              (case / 'profiles' / stage / 'system/coolant/fvSchemes').read_text())

    def test_incomplete_or_fake_step_bundle_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "STEP"):
                stage_uploaded_step(root, "fake.step", b"not a CAD file")
            with self.assertRaisesRegex(ValueError, "3개"):
                prepare_imported_run(root, {}, {"coolant": "deionized_water"})

    def test_low_disk_blocks_launch_before_worker_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("cfd_twin.workflow_run.prepare_imported_run", return_value={
                "confirmation": "confirmed", "can_start_new_solver": False, "disk_free_gib": 42.0,
            }):
                with self.assertRaisesRegex(ValueError, "55 GiB"):
                    create_imported_run(root, {}, {}, "confirmed")
            self.assertFalse((root / "runs").exists())

    def test_cross_platform_steady_driver_uses_measured_stage_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            services = []

            def compose(_case, service, _output, *, environment=None):
                services.append(service)
                run_name, report_name, status = (
                    ("steady_cold_flow", "cold_flow_validation.json", "COLD_FLOW_VALIDATION_PASSED")
                    if service == "cold-flow-pilot"
                    else ("steady_cht", "steady_cht_validation.json", "STEADY_CHT_VALIDATION_PASSED")
                )
                report = case / "runs" / run_name / "reports/solver" / report_name
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(json.dumps({"status": status, "checks": {}, "final_iteration": 6000}))
                return 0

            with patch("cfd_twin.workflow_run._compose_service", side_effect=compose):
                _run_steady_stages(case, io.BytesIO())
            self.assertEqual(services, ["cold-flow-pilot", "steady-pilot"])
            self.assertEqual(
                json.loads((case / "runs/steady_cht/reports/solver/convergence_decision.json").read_text())["action"],
                "pass",
            )


if __name__ == "__main__":
    unittest.main()
