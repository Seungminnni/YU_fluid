from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from cfd_twin import experiments as experiments
from cfd_twin.workflow_run import STEP_ROLES, _write_json


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "experiment"
        self.jobs = self.root / "jobs"
        self.config = self.root / "conditions.yaml"
        self.raw = yaml.safe_load(experiments.DEFAULT_CONFIG.read_text(encoding="utf-8"))
        for case in self.raw["cases"]:
            case["overlays"] = [str((experiments.DEFAULT_CONFIG.parent / path).resolve()) for path in case["overlays"]]
        self.raw["cases"] = self.raw["cases"][:1]
        self.write_config()
        self.enterContext(patch.object(experiments, "prepare_solver_inputs", return_value={"can_start_new_solver": True}))

    def write_config(self):
        self.config.write_text(yaml.safe_dump(self.raw), encoding="utf-8")

    def prepare(self):
        def cad(config, generated):
            for role in STEP_ROLES:
                (generated / role).write_bytes(b"ISO-10303-21;" + role.encode())
        def imported(jobs, selected, inputs, **kwargs):
            return {"inputs": inputs, "geometry": {"verified": True}, "confirmation": "confirmed", "can_start_new_solver": True}
        with patch.object(experiments, "execute_cad_source", side_effect=cad), patch.object(experiments, "prepare_imported_run", side_effect=imported):
            return experiments.prepare_batch(self.config, self.output, self.jobs, 4)

    def test_new_conditions_are_frozen_without_old_batch_or_preview(self):
        self.raw["cases"][0]["inputs"] = {"inlet_velocity_m_s": 0.6, "inlet_temperature_k": 303.0}
        self.write_config()
        batch = self.prepare()
        job = experiments.read(Path(batch["jobs"][0]["job_dir"]) / "job.json")
        self.assertEqual(job["inputs"]["inlet_velocity_m_s"], 0.6)
        self.assertEqual(job["inputs"]["inlet_temperature_k"], 303.0)
        self.assertIn("boundary.inlet_temperature=303 K", job["geometry_overrides"])
        self.assertEqual(job["wall_diagnostic_iterations"], 1500)
        self.assertFalse((Path(batch["jobs"][0]["job_dir"]) / "case").exists())
        self.raw["inputs"]["outlet_pressure_pa"] = 110000
        self.write_config()
        with self.assertRaisesRegex(ValueError, "different inputs"):
            self.prepare()
        self.assertEqual(experiments.read(Path(batch["jobs"][0]["job_dir"]) / "job.json"), job)

    def test_explicit_numerical_profile_is_frozen_and_validated(self):
        self.raw['cases'][0]['steady_numerics_profile'] = 'pressure_limited_0p5'
        self.raw['cases'][0]['outlet_velocity_profile'] = 'pressure_backflow'
        self.raw['cases'][0]['cold_flow_maximum_iterations'] = 20000
        self.write_config()
        batch = self.prepare()
        job = experiments.read(Path(batch['jobs'][0]['job_dir']) / 'job.json')
        self.assertEqual(job['steady_numerics_profile'], 'pressure_limited_0p5')
        self.assertEqual(job['outlet_velocity_profile'], 'pressure_backflow')
        self.assertEqual(job['cold_flow_maximum_iterations'], 20000)
        self.raw['cases'][0]['steady_numerics_profile'] = 'typo'
        self.write_config()
        with self.assertRaisesRegex(ValueError, 'Unknown steady_numerics_profile'):
            experiments.load_plan(self.config)

    def test_cold_flow_iteration_cap_is_validated_before_preparation(self):
        self.raw['cold_flow_maximum_iterations'] = 6001
        self.write_config()
        with self.assertRaisesRegex(ValueError, 'multiple of 500'):
            experiments.load_plan(self.config)

    def test_automatic_retry_policy_is_frozen_and_validated(self):
        self.raw["automatic_retry"] = {
            "enabled": True,
            "maximum_retries": 3,
            "cold_flow_iteration_limits": [15000, 20000],
            "steady_numerics_profiles": ["pressure_relaxation_0p15"],
        }
        self.write_config()
        policy = experiments.load_plan(self.config)["cases"][0]["automatic_retry"]
        self.assertEqual(policy["maximum_retries"], 3)
        self.assertEqual(policy["cold_flow_iteration_limits"], [15000, 20000])
        self.raw["automatic_retry"]["steady_numerics_profiles"] = ["typo"]
        self.write_config()
        with self.assertRaisesRegex(ValueError, "Unknown steady_numerics_profile"):
            experiments.load_plan(self.config)

    def test_measured_mesh_failure_selects_next_approved_profile(self):
        batch = self.prepare()
        entry = batch["jobs"][0]
        job = Path(entry["job_dir"])
        specification = batch["plan"]["cases"][0]
        (job / "case/reports/production_mesh").mkdir(parents=True)
        _write_json(job / "case/reports/production_mesh/layer_coverage.json", {"status": "failed"})
        decision = experiments.automatic_retry_decision(
            entry, specification, job,
            {"status": "failed", "stage": "mesh", "message": "mesh gate failed"},
        )
        self.assertEqual(decision["action"], "retry")
        self.assertEqual(decision["profile_index"], 1)
        self.assertEqual(decision["diagnosis"]["category"], "mesh_layers")
        self.assertEqual(
            decision["diagnosis"]["failed_reports"], ["layer_coverage.json"]
        )

    def test_cell_count_failure_is_not_mislabeled_as_quality_failure(self):
        batch = self.prepare()
        entry = batch["jobs"][0]
        job = Path(entry["job_dir"])
        report = job / "case/reports/production_mesh/mesh_target_composition.json"
        _write_json(report, {
            "status": "MESH_TARGET_VALIDATION_FAILED",
            "checks": {
                "total_cell_count_within_configured_tolerance": False,
                "hexahedron_fraction_at_least_configured_minimum": True,
            },
            "targets": {"total_cells": 800000},
            "actual": {"total_cells": 988705, "hexahedron_fraction": 0.881218},
            "relative_error": {"total_cells": 0.23588125},
            "limits": {"total_tolerance_fraction": 0.12},
        })
        diagnosis = experiments.diagnose_failure(
            job, {"status": "failed", "stage": "mesh", "message": "target failed"}
        )
        self.assertEqual(diagnosis["category"], "mesh_cell_budget")
        self.assertEqual(
            diagnosis["evidence"]["mesh_target_composition.json"]["actual"]["total_cells"],
            988705,
        )

    def test_convergence_failure_increases_cap_then_changes_numerics(self):
        batch = self.prepare()
        entry = batch["jobs"][0]
        job = Path(entry["job_dir"])
        specification = batch["plan"]["cases"][0]
        report = job / "case/runs/steady_cold_flow/reports/solver/cold_flow_validation.json"
        _write_json(report, {
            "status": "COLD_FLOW_VALIDATION_FAILED", "final_iteration": 9500,
            "checks": {"last_200_flow_residuals_below_1e-3": False},
        })
        state = {"status": "failed", "stage": "steady", "message": "absolute iteration cap reached"}
        first = experiments.automatic_retry_decision(entry, specification, job, state)
        self.assertEqual(first["job_overrides"], {"cold_flow_maximum_iterations": 20000})
        manifest = experiments.read(job / "job.json")
        manifest["cold_flow_maximum_iterations"] = 20000
        _write_json(job / "job.json", manifest)
        second = experiments.automatic_retry_decision(entry, specification, job, state)
        self.assertEqual(second["job_overrides"], {"steady_numerics_profile": "pressure_relaxation_0p15"})

    def test_continuation_abort_is_diagnosed_as_numerical_instability(self):
        batch = self.prepare()
        entry = batch["jobs"][0]
        job = Path(entry["job_dir"])
        report = job / "case/runs/steady_cold_flow/reports/solver/cold_flow_validation.json"
        _write_json(report, {
            "status": "COLD_FLOW_VALIDATION_FAILED", "final_iteration": 1500,
            # The 1500-iteration y+ value is diagnostic only. A later solver
            # abort must take precedence over this preliminary wall result.
            "checks": {"battery_yplus_gate_passed": False},
        })
        log_dir = job / "case/runs/steady_cold_flow/logs/solver"
        log_dir.mkdir(parents=True)
        (log_dir / "steady_cold_flow.log").write_text("End\n", encoding="utf-8")
        (log_dir / "steady_cold_flow_continue_1500_to_6000.log").write_text(
            "Negative initial temperature T0: -38.3\nFOAM parallel run aborting\n",
            encoding="utf-8",
        )
        decision = experiments.automatic_retry_decision(
            entry, batch["plan"]["cases"][0], job,
            {"status": "failed", "stage": "steady", "message": "Cold flow did not reach the original checkpoint"},
        )
        self.assertEqual(decision["action"], "retry")
        self.assertEqual(decision["diagnosis"]["category"], "numerical_instability")
        self.assertEqual(
            decision["job_overrides"],
            {"steady_numerics_profile": "pressure_relaxation_0p15"},
        )

    def test_unmeasured_mesh_failure_is_not_retried_blindly(self):
        batch = self.prepare()
        entry = batch["jobs"][0]
        job = Path(entry["job_dir"])
        decision = experiments.automatic_retry_decision(
            entry, batch["plan"]["cases"][0], job,
            {"status": "failed", "stage": "mesh", "message": "Docker unavailable"},
        )
        self.assertEqual(decision["action"], "stop")
        self.assertEqual(
            decision["diagnosis"]["category"],
            "infrastructure_or_unmeasured_mesh_failure",
        )

    def test_failed_mesh_is_archived_and_regenerated_in_new_job(self):
        batch = self.prepare()
        original = Path(batch["jobs"][0]["job_dir"])
        other = self.jobs / "runs" / ("f" * 32)
        other.mkdir()
        (other / "untouched").write_text("queued", encoding="utf-8")
        seen = []
        def worker(manifest):
            job = manifest.parent
            seen.append(job)
            if len(seen) == 1:
                _write_json(job / "status.json", {"status": "failed", "stage": "mesh"})
                _write_json(job / "case/reports/production_mesh/layer_coverage.json", {"status": "failed"})
                raw = job / "case/diagnostics/wall/raw"
                raw.mkdir(parents=True)
                (raw / "nSurfaceLayers").write_bytes(b"spatial layers")
                raise ValueError("mesh quality failed")
            self.assertFalse((job / "case").exists())
            self.assertNotEqual(job, original)
            _write_json(job / "status.json", {"status": "complete", "stage": "release"})
        with patch.object(experiments, "run_imported_case", side_effect=worker):
            self.assertTrue(experiments.run_batch(self.output, batch))
        self.assertEqual(len(seen), 2)
        self.assertFalse(original.exists())
        self.assertTrue((other / "untouched").exists())
        self.assertEqual((self.output / "diagnostic_attempts" / original.name / "case/diagnostics/wall/raw/nSurfaceLayers").read_bytes(), b"spatial layers")
        with patch.object(experiments, "run_imported_case") as worker:
            self.assertTrue(experiments.run_batch(self.output, batch))
            worker.assert_not_called()

    def test_docker_failure_does_not_cycle_mesh_profiles(self):
        batch = self.prepare()
        job = Path(batch["jobs"][0]["job_dir"])
        def worker(manifest):
            _write_json(job / "status.json", {"status": "failed", "stage": "mesh", "message": "image download failed"})
            raise ValueError("image download failed")
        with patch.object(experiments, "run_imported_case", side_effect=worker) as run:
            self.assertFalse(experiments.run_batch(self.output, batch))
            self.assertEqual(run.call_count, 1)
        self.assertTrue(job.exists())

    def test_running_and_published_jobs_are_never_deleted(self):
        batch = self.prepare()
        entry = batch["jobs"][0]
        job = Path(entry["job_dir"])
        _write_json(job / "status.json", {"status": "running", "stage": "mesh"})
        with self.assertRaisesRegex(ValueError, "Only failed"):
            experiments.fresh_retry(self.output, batch, entry, batch["plan"]["cases"][0], profile_index=1)
        _write_json(job / "status.json", {"status": "failed", "stage": "mesh"})
        _write_json(self.root / "viewer/four_case_manifest.json", {"runs": [{"id": f"run_{entry['id']}"}]})
        with patch.object(experiments, "SOURCE_ROOT", self.root), self.assertRaisesRegex(ValueError, "Published"):
            experiments.fresh_retry(self.output, batch, entry, batch["plan"]["cases"][0], profile_index=1)
        self.assertTrue(job.exists())

    def test_release_error_preserves_successful_solver_output(self):
        batch = self.prepare()
        job = Path(batch["jobs"][0]["job_dir"])
        _write_json(job / "status.json", {"status": "complete", "stage": "release", "release_error": "disk full"})
        with patch.object(experiments, "run_imported_case") as worker:
            self.assertFalse(experiments.run_batch(self.output, batch, retry_failed=True))
            worker.assert_not_called()
        self.assertTrue(job.exists())

    def test_profile_must_match_requested_mesh_target(self):
        self.raw["inputs"]["mesh_target_cells"] = 800000
        self.write_config()
        with self.assertRaisesRegex(ValueError, "does not match"):
            experiments.load_plan(self.config)

    def test_generated_linux_scripts_use_lf_on_every_host(self):
        from cfd_twin.production_mesh import _atomic_write as mesh_write
        from cfd_twin.openfoam_case import _atomic_write as solver_write
        for write in (mesh_write, solver_write):
            path = self.root / "Allrun"
            write(path, "#!/bin/sh\nset -e\n")
            self.assertEqual(path.read_bytes(), b"#!/bin/sh\nset -e\n")


if __name__ == "__main__":
    unittest.main()
