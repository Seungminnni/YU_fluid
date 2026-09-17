from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from cfd_twin.workflow_app import (
    BASELINE_INPUTS,
    baseline_geometry,
    inspect_cad_bytes,
    prepare_inputs,
    workflow_status,
)
from cfd_twin.workflow_publish import publish_completed_run


class WorkflowAppTests(unittest.TestCase):
    def test_uploaded_cad_needs_exact_content_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cad = Path(directory)
            known = b"ISO-10303-21;\nDATA;\nENDSEC;"
            (cad / "cad_result.json").write_text(
                json.dumps(
                    {
                        "file_sha256": {
                            "coolant.step": hashlib.sha256(known).hexdigest()
                        }
                    }
                ),
                encoding="utf-8",
            )
            same = inspect_cad_bytes("renamed.step", known, cad)
            self.assertEqual(same["recognized_baseline_role"], "coolant.step")
            self.assertTrue(same["usable_for_current_mesh"])
            changed = inspect_cad_bytes("coolant.step", known + b"X", cad)
            self.assertIsNone(changed["recognized_baseline_role"])
            self.assertFalse(changed["usable_for_current_mesh"])

    def test_uploaded_cad_rejects_wrong_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "STEP 헤더"):
                inspect_cad_bytes("shape.step", b"not a STEP file", Path(directory))
            with self.assertRaisesRegex(ValueError, "STEP"):
                inspect_cad_bytes("shape.exe", b"bytes", Path(directory))

    def test_confirm_does_not_start_new_solver(self) -> None:
        current = {**BASELINE_INPUTS, "cad_mode": "baseline"}
        attached = prepare_inputs(current, run_active=True)
        self.assertTrue(attached["can_view_current_run"])
        self.assertFalse(attached["can_start_new_solver"])
        modified = prepare_inputs(
            {**current, "inlet_velocity_m_s": 0.5}, run_active=True
        )
        self.assertEqual(modified["status"], "DRAFT_ONLY")
        self.assertFalse(modified["can_view_current_run"])
        with self.assertRaisesRegex(ValueError, "검증되지 않은 CAD"):
            prepare_inputs({**current, "cad_mode": "unverified_upload"}, run_active=True)

    def test_progress_is_read_from_log_not_invented(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cad = root / "cad"
            case = root / "case"
            viewer = root / "viewer"
            cad.mkdir()
            (cad / "cad_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "CAD_EXPORTED_GEOMETRY_CHECKS_PASSED",
                        "port_diameter_mm": 19,
                    }
                ),
                encoding="utf-8",
            )
            fixture_bytes = b"ISO-10303-21;"
            hashes = {
                filename: hashlib.sha256(fixture_bytes).hexdigest()
                for filename in ("enclosure_with_ports.step", "coolant.step", "battery_cells.step")
            }
            (cad / "cad_result.json").write_text(
                json.dumps({"status": "CAD_EXPORTED_GEOMETRY_CHECKS_PASSED", "file_sha256": hashes}),
                encoding="utf-8",
            )
            for filename in ("enclosure_with_ports.step", "coolant.step", "battery_cells.step"):
                (cad / filename).write_bytes(fixture_bytes)
            log = case / "runs/frozen_flow_actual_1s/logs/solver/frozen_flow_actual_1s.log"
            log.parent.mkdir(parents=True)
            log.write_text(
                "Time = 321\nenergyCoupling: 42/100\n", encoding="utf-8"
            )
            status = workflow_status(case, cad, viewer)
            self.assertEqual(status["transient"]["time_s"], 321.0)
            self.assertEqual(status["transient"]["energy_coupling_iteration"], 42)
            self.assertAlmostEqual(status["transient"]["physical_time_percent"], 321 / 18)
            self.assertFalse(status["viewer_ready"])
            self.assertEqual(status["stages"][4]["status"], "running")
            self.assertTrue(baseline_geometry(cad)["verified"])
            (cad / "coolant.step").write_bytes(fixture_bytes + b"changed")
            self.assertFalse(baseline_geometry(cad)["verified"])

    def test_clean_end_without_validation_is_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory) / "case"
            log = case / "runs/frozen_flow_actual_1s/logs/solver/frozen_flow_actual_1s.log"
            log.parent.mkdir(parents=True)
            log.write_text("Time = 1800\nEnd\n", encoding="utf-8")
            status = workflow_status(case, Path(directory) / "cad", Path(directory) / "viewer")
            self.assertEqual(status["stages"][4]["status"], "needs_check")
            self.assertFalse(status["viewer_ready"])

    def test_validated_solver_results_show_numbers_before_viewer_packaging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory) / "case"
            run = case / "runs/frozen_flow_actual_1s"
            log = run / "logs/solver/frozen_flow_actual_1s.log"
            log.parent.mkdir(parents=True)
            log.write_text("Time = 1800\nenergyCoupling: 100/100\nEnd\n", encoding="utf-8")
            report_dir = run / "reports/solver"
            report_dir.mkdir(parents=True)
            (report_dir / "frozen_flow_validation.json").write_text(
                json.dumps({
                    "status": "FROZEN_FLOW_VALIDATION_PASSED",
                    "final_summary": {"battery_tmax_k": 294.1343, "battery_tavg_k": 293.6437},
                    "final_window_rows": [{"outlet_mass_weighted_temperature_k": 293.1163}],
                }),
                encoding="utf-8",
            )
            (report_dir / "energy_balance.json").write_text(
                json.dumps({
                    "status": "TRANSIENT_ENERGY_BALANCE_PASSED",
                    "final": {"imbalance_fraction": 0.0021},
                }),
                encoding="utf-8",
            )
            status = workflow_status(case, Path(directory) / "cad", Path(directory) / "viewer")
            self.assertEqual(status["stages"][4]["status"], "passed")
            self.assertEqual(status["stages"][5]["status"], "pending")
            self.assertFalse(status["viewer_ready"])
            self.assertTrue(status["result_summary"]["energy_balance_passed"])
            self.assertEqual(status["result_summary"]["battery_tmax_k"], 294.1343)

    def test_pilot_viewer_requires_validated_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = root / "case"
            viewer = root / "viewer"
            (case / "results").mkdir(parents=True)
            viewer.mkdir()
            (viewer / "index.html").write_text("viewer", encoding="utf-8")
            (viewer / "results.html").write_text("results", encoding="utf-8")
            manifest = {
                "data_status": "CFD_PILOT_RESULTS_READY",
                "actual_solver_data": True,
                "validation_status": "FROZEN_FLOW_VALIDATION_FAILED",
            }
            path = case / "results/result_manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertFalse(workflow_status(case, root / "cad", viewer)["viewer_ready"])
            manifest["validation_status"] = "FROZEN_FLOW_VALIDATION_PASSED"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertFalse(workflow_status(case, root / "cad", viewer)["viewer_ready"])
            (case / "reports").mkdir()
            (case / "reports/workflow_postprocess.json").write_text(
                json.dumps({"stage": "complete"}), encoding="utf-8"
            )
            self.assertTrue(workflow_status(case, root / "cad", viewer)["viewer_ready"])

    def test_postprocess_rejects_case_without_all_solver_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "mesh"):
                publish_completed_run(root / "case", root / "viewer")


if __name__ == "__main__":
    unittest.main()
