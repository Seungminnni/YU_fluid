from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cfd_twin.workflow_app import (
    BASELINE_INPUTS,
    baseline_geometry,
    inspect_cad_bytes,
    prepare_parametric_run,
    prepare_inputs,
    start_parametric_run,
    workflow_status,
)
from cfd_twin.workflow_publish import publish_completed_run


class WorkflowAppTests(unittest.TestCase):
    PARAMETRIC_PARAMETERS = {
        "template": "reference_23cell",
        "size_mode": "dimensions",
        "tank_width_mm": 125,
        "tank_length_mm": 135,
        "tank_height_mm": 90,
        "inlet_diameter_mm": 20,
        "outlet_diameter_mm": 16,
    }
    SOLVER_INPUTS = {
        "coolant": "deionized_water",
        "inlet_temperature_k": 294,
        "inlet_velocity_m_s": 0.45,
        "outlet_pressure_pa": 102000,
        "mesh_target_cells": 800000,
    }

    def test_parametric_prepare_includes_real_solver_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "cfd_twin.workflow_app.prepare_solver_inputs",
            return_value={
                "inputs": self.SOLVER_INPUTS,
                "disk_free_gib": 120.0,
                "disk_minimum_gib": 55,
                "can_start_new_solver": True,
                "confirmation": "frozen-parametric-preview",
            },
        ):
            prepared = prepare_parametric_run(Path(directory), {
                "parameters": self.PARAMETRIC_PARAMETERS,
                "inputs": self.SOLVER_INPUTS,
            })
        self.assertEqual(prepared["status"], "READY_FOR_EXPLICIT_CONFIRMATION")
        self.assertEqual(prepared["inputs"]["mesh_target_cells"], 800000)
        self.assertTrue(prepared["can_start_new_solver"])
        self.assertEqual(prepared["derived"]["tank_mm"], [125.0, 135.0, 90.0])

    def test_600k_solver_class_is_accepted(self) -> None:
        inputs = {**self.SOLVER_INPUTS, "mesh_target_cells": 600000}
        prepared = prepare_inputs(inputs | {"cad_mode": "baseline"}, run_active=False)
        self.assertEqual(prepared["inputs"]["mesh_target_cells"], 600000)

    def test_parametric_start_freezes_generated_steps_and_launches_real_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def generate(target: Path, _parameters: object) -> dict[str, object]:
                generation_id = "a" * 32
                output = target / "parametric" / generation_id
                output.mkdir(parents=True)
                for index, role in enumerate((
                    "enclosure_with_ports.step", "coolant.step", "battery_cells.step",
                )):
                    (output / role).write_bytes(
                        b"ISO-10303-21;\nDATA;\n" + str(index).encode() + b"\nENDSEC;"
                    )
                return {
                    "id": generation_id,
                    "geometry_sha256": "geometry-hash",
                    "derived": {"tank_mm": [125, 135, 90], "gross_tank_volume_l": 1.2},
                    "download_urls": {},
                }

            with patch(
                "cfd_twin.workflow_app.prepare_parametric_run",
                return_value={
                    "can_start_new_solver": True,
                    "disk_free_gib": 120.0,
                    "disk_minimum_gib": 55,
                    "confirmation": "frozen-parametric-preview",
                },
            ), patch(
                "cfd_twin.workflow_app.generate_parametric_cad", side_effect=generate,
            ), patch(
                "cfd_twin.workflow_app.prepare_imported_run",
                return_value={"confirmation": "frozen-confirmation"},
            ) as prepare, patch(
                "cfd_twin.workflow_app.create_imported_run",
                return_value={"id": "b" * 32, "status": "queued"},
            ) as create:
                started = start_parametric_run(root, {
                    "parameters": self.PARAMETRIC_PARAMETERS,
                    "inputs": self.SOLVER_INPUTS,
                    "confirmation": "frozen-parametric-preview",
                    "geometry_confirmed": True,
                })

            selected = prepare.call_args.args[1]
            self.assertEqual(set(selected), {
                "enclosure_with_ports.step", "coolant.step", "battery_cells.step",
            })
            self.assertEqual(len(set(selected.values())), 3)
            overrides = prepare.call_args.kwargs["geometry_overrides"]
            self.assertIn("geometry.enclosure.width_x=125 mm", overrides)
            self.assertEqual(create.call_args.args[3], "frozen-confirmation")
            self.assertEqual(create.call_args.kwargs["source_metadata"]["kind"], "parametric_template")
            self.assertEqual(started["status"], "queued")
            self.assertTrue(started["parametric"]["calculation_started"])

    def test_parametric_start_checks_disk_before_exporting_cad(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "cfd_twin.workflow_app.prepare_parametric_run",
            return_value={
                "can_start_new_solver": False,
                "disk_free_gib": 40.0,
                "disk_minimum_gib": 55,
                "confirmation": "frozen-parametric-preview",
            },
        ), patch("cfd_twin.workflow_app.generate_parametric_cad") as generate:
            with self.assertRaisesRegex(ValueError, "55 GiB"):
                start_parametric_run(Path(directory), {
                    "parameters": self.PARAMETRIC_PARAMETERS,
                    "inputs": self.SOLVER_INPUTS,
                    "confirmation": "frozen-parametric-preview",
                    "geometry_confirmed": True,
                })
            generate.assert_not_called()

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
            self.assertEqual(status["result_slots"], [{
                "name": "현재 계산 결과",
                "name_en": "Current Calculation",
                "ready": False,
                "battery_tmax_k": None,
                "outlet_temperature_k": None,
            }])

    def test_published_results_stay_available_after_raw_case_is_archived(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            viewer = root / "viewer"
            (viewer / "cad").mkdir(parents=True)
            for name in ("index.html", "results.html", "mesh.html", "cad/index.html"):
                (viewer / name).write_text("viewer", encoding="utf-8")
            published = {
                "status": "FOUR_CASE_VIEWER_ASSEMBLED",
                "solver_validation_passed": True,
                "energy_balance_passed": True,
                "runs": [{
                    "id": "precision", "label": "Ø19", "label_en": "Ø19",
                    "data_ready": True, "cells": 611313,
                    "battery_tmax_k": 294.0, "outlet_temperature_k": 293.2,
                }],
            }
            (viewer / "four_case_manifest.json").write_text(json.dumps(published), encoding="utf-8")
            status = workflow_status(root / "archived_case", root / "cad", viewer)
            self.assertTrue(status["viewer_ready"])
            self.assertEqual(status["mesh_cells"], 611313)
            self.assertEqual(status["transient"]["time_s"], 1800.0)
            self.assertEqual(status["result_slots"][0]["name"], "Ø19")
            published["energy_balance_passed"] = False
            (viewer / "four_case_manifest.json").write_text(json.dumps(published), encoding="utf-8")
            self.assertFalse(workflow_status(root / "archived_case", root / "cad", viewer)["viewer_ready"])

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

    def test_comparison_viewer_requires_both_independent_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = root / "case"
            second = case / "production/single"
            viewer = root / "viewer"
            viewer.mkdir(parents=True)
            (viewer / "index.html").write_text("viewer", encoding="utf-8")
            (viewer / "results.html").write_text("results", encoding="utf-8")

            def write_result(target: Path, maximum: float) -> None:
                solver = target / "runs/frozen_flow_actual_1s/reports/solver"
                solver.mkdir(parents=True)
                (solver / "frozen_flow_validation.json").write_text(json.dumps({
                    "status": "FROZEN_FLOW_VALIDATION_PASSED",
                    "final_summary": {"battery_tmax_k": maximum},
                    "final_window_rows": [{"outlet_mass_weighted_temperature_k": 293.2}],
                }), encoding="utf-8")
                (solver / "energy_balance.json").write_text(json.dumps({
                    "status": "TRANSIENT_ENERGY_BALANCE_PASSED",
                }), encoding="utf-8")
                (target / "results").mkdir(parents=True)
                (target / "results/result_manifest.json").write_text(json.dumps({
                    "data_status": "L0_CFD_RESULTS_READY",
                    "actual_solver_data": True,
                    "validation_status": "FROZEN_FLOW_VALIDATION_PASSED",
                }), encoding="utf-8")

            write_result(case, 294.0)
            write_result(second, 295.0)
            (case / "comparison_manifest.json").write_text(json.dumps({
                "status": "COMPARISON_RESULTS_READY",
                "runs": [
                    {"id": "precision", "label": "Ø19", "label_en": "Ø19"},
                    {"id": "single", "label": "Ø23", "label_en": "Ø23"},
                ],
            }), encoding="utf-8")

            status = workflow_status(case, root / "cad", viewer)
            self.assertTrue(status["viewer_ready"])
            self.assertTrue(status["comparison_mode"])
            self.assertEqual([slot["battery_tmax_k"] for slot in status["result_slots"]], [294.0, 295.0])
            (second / "results/result_manifest.json").unlink()
            self.assertFalse(workflow_status(case, root / "cad", viewer)["viewer_ready"])

    def test_postprocess_rejects_case_without_all_solver_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "mesh"):
                publish_completed_run(root / "case", root / "viewer")


if __name__ == "__main__":
    unittest.main()
