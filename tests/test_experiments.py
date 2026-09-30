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
        self.write_config()
        batch = self.prepare()
        job = experiments.read(Path(batch['jobs'][0]['job_dir']) / 'job.json')
        self.assertEqual(job['steady_numerics_profile'], 'pressure_limited_0p5')
        self.assertEqual(job['outlet_velocity_profile'], 'pressure_backflow')
        self.raw['cases'][0]['steady_numerics_profile'] = 'typo'
        self.write_config()
        with self.assertRaisesRegex(ValueError, 'Unknown steady_numerics_profile'):
            experiments.load_plan(self.config)

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
