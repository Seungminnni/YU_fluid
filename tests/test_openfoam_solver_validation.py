from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cfd_twin.openfoam_cold_flow_validation import validate_cold_flow
from cfd_twin.openfoam_frozen_flow_validation import (
    _qvol_at_time,
    _read_qvol_samples,
    validate_frozen_flow,
)
from cfd_twin.openfoam_steady_cht_validation import validate_steady_cht


class ColdFlowValidationTests(unittest.TestCase):
    def test_dynamic_heat_source_samples_are_interpolated_in_physical_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            samples = Path(directory) / "heat_source_samples.csv"
            samples.write_text(
                "time_s,SOC,q_cell_W,qvol_W_m3\n"
                "0,1,1,100\n"
                "10,0.9,2,200\n",
                encoding="utf-8",
            )
            table = _read_qvol_samples(samples)
            self.assertEqual(_qvol_at_time(-1, table, 50), 100)
            self.assertEqual(_qvol_at_time(5, table, 50), 150)
            self.assertEqual(_qvol_at_time(11, table, 50), 200)
            self.assertEqual(_qvol_at_time(5, [], 50), 50)

    def test_compound_gate_requires_residual_mass_pressure_field_and_yplus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for iteration in range(0, 201, 10):
                rows.extend(
                    [
                        f"Time = {iteration}",
                        "Solving for fluid region coolant",
                        "smoothSolver: Solving for Ux, Initial residual = 2e-4, Final residual = 1e-6, No Iterations 2",
                        "smoothSolver: Solving for Uy, Initial residual = 2e-4, Final residual = 1e-6, No Iterations 2",
                        "smoothSolver: Solving for Uz, Initial residual = 1e-4, Final residual = 1e-6, No Iterations 2",
                        "smoothSolver: Solving for h, Initial residual = 1e-4, Final residual = 1e-6, No Iterations 2",
                        "GAMG: Solving for p_rgh, Initial residual = 1e-3, Final residual = 1e-6, No Iterations 2",
                        "smoothSolver: Solving for k, Initial residual = 1e-4, Final residual = 1e-6, No Iterations 2",
                        "smoothSolver: Solving for omega, Initial residual = 1e-6, Final residual = 1e-8, No Iterations 2",
                        "Solving for solid region batterySolid",
                        "DICPCG: Solving for h, Initial residual = 3e-4, Final residual = 1e-6, No Iterations 2",
                        "sum(inlet) of phi = -0.1129",
                        "sum(outlet) of phi = 0.1129",
                        "areaAverage(inlet) of p = 101585",
                        "areaAverage(outlet) of p = 101325",
                        "min(k) = 1e-12",
                        "max(k) = 2e-3",
                        "min(omega) = 0.1",
                        "max(omega) = 2e6",
                        "min(mag(U)) = 0",
                        "max(mag(U)) = 0.6",
                        "min(T) = 293",
                        "max(T) = 293.001",
                        "min(p) = 101000",
                        "max(p) = 101700",
                    ]
                )
            rows.append("End")
            log = root / "solver.log"
            log.write_text("\n".join(rows) + "\n", encoding="utf-8")
            yplus = root / "yplus.json"
            yplus.write_text(
                json.dumps({"status": "YPLUS_VALIDATION_PASSED"}),
                encoding="utf-8",
            )
            report = validate_cold_flow(log, yplus_report_path=yplus)
            self.assertEqual(report["status"], "COLD_FLOW_VALIDATION_PASSED")
            self.assertTrue(report["runnable_thermal_pilot"])
            self.assertEqual(report["monitor_summary"]["sample_count"], 21)

            yplus.write_text(
                json.dumps({"status": "YPLUS_VALIDATION_FAILED"}),
                encoding="utf-8",
            )
            failed = validate_cold_flow(log, yplus_report_path=yplus)
            self.assertEqual(failed["status"], "COLD_FLOW_VALIDATION_FAILED")
            self.assertFalse(failed["runnable_thermal_pilot"])

    def test_steady_cht_gate_uses_mesh_heat_and_mass_weighted_temperature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for iteration in range(0, 201, 10):
                rows.extend(
                    [
                        f"Time = {iteration}",
                        "Solving for fluid region coolant",
                        "smoothSolver: Solving for Ux, Initial residual = 2e-4, Final residual = 1e-6, No Iterations 2",
                        "smoothSolver: Solving for Uy, Initial residual = 2e-4, Final residual = 1e-6, No Iterations 2",
                        "smoothSolver: Solving for Uz, Initial residual = 1e-4, Final residual = 1e-6, No Iterations 2",
                        "smoothSolver: Solving for h, Initial residual = 1e-4, Final residual = 1e-6, No Iterations 2",
                        "GAMG: Solving for p_rgh, Initial residual = 1e-3, Final residual = 1e-6, No Iterations 2",
                        "smoothSolver: Solving for k, Initial residual = 1e-4, Final residual = 1e-6, No Iterations 2",
                        "smoothSolver: Solving for omega, Initial residual = 1e-6, Final residual = 1e-8, No Iterations 2",
                        "Solving for solid region batterySolid",
                        "DICPCG: Solving for h, Initial residual = 3e-4, Final residual = 1e-6, No Iterations 2",
                        "sum(inlet) of phi = -1",
                        "sum(outlet) of phi = 1",
                        "areaAverage(inlet) of p = 101585",
                        "areaAverage(outlet) of p = 101325",
                        "weightedAverage(inlet) of T = 300",
                        "weightedAverage(outlet) of T = 301",
                    ]
                )
                for index in range(1, 24):
                    rows.extend(
                        [
                            f"volAverage(battery_C{index:02d}) of T = 301",
                            f"max(battery_C{index:02d}) of T = 301.5",
                        ]
                    )
                rows.extend(
                    [
                        "min/max(k) = 1e-12 2e-3",
                        "min/max(omega) = 0.1 2e6",
                        "min/max(mag(U)) = 0 0.6",
                        "min/max(T) = 300 301.5",
                        "min/max(p) = 101000 101700",
                    ]
                )
            rows.append("End")
            log = root / "steady.log"
            log.write_text("\n".join(rows) + "\n", encoding="utf-8")
            cold = root / "cold.json"
            cold.write_text(
                json.dumps({"status": "COLD_FLOW_VALIDATION_PASSED"}),
                encoding="utf-8",
            )
            mesh = root / "mesh.json"
            mesh.write_text(
                json.dumps(
                    {"actual": {"battery_total_volume_m3": 1.0}}
                ),
                encoding="utf-8",
            )
            report = validate_steady_cht(
                log,
                cold_flow_report_path=cold,
                mesh_integral_report_path=mesh,
                qvol_w_m3=1.0,
                coolant_cp_j_kgk=1.0,
                initial_temperature_k=300.0,
            )
            self.assertEqual(report["status"], "STEADY_CHT_VALIDATION_PASSED")
            self.assertAlmostEqual(
                report["monitor_summary"]["mean_energy_imbalance_fraction"],
                0.0,
            )

    def test_frozen_flow_gate_requires_telemetry_frames_and_steady_equivalence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for time in range(1, 21):
                rows.extend(
                    [
                        f"Time = {time}",
                        "sum(inlet) of phi = -1",
                        "sum(outlet) of phi = 1",
                        "weightedAverage(inlet) of T = 300",
                        "weightedAverage(outlet) of T = 301",
                    ]
                )
                for index in range(1, 24):
                    rows.extend(
                        [
                            f"volAverage(battery_C{index:02d}) of T = 301",
                            f"max(battery_C{index:02d}) of T = 301.5",
                        ]
                    )
                rows.extend(
                    [
                        "min/max(k) = 1e-12 2e-3",
                        "min/max(omega) = 0.1 2e6",
                        "min/max(mag(U)) = 0 0.6",
                        "min/max(T) = 300 301.5",
                        "min/max(p) = 101000 101700",
                    ]
                )
            rows.append("End")
            log = root / "frozen.log"
            log.write_text("\n".join(rows) + "\n", encoding="utf-8")

            steady = root / "steady.json"
            steady.write_text(
                json.dumps(
                    {
                        "status": "STEADY_CHT_VALIDATION_PASSED",
                        "monitor_summary": {
                            "final_battery_tavg_k": 301.0,
                            "final_battery_tmax_k": 301.5,
                        },
                    }
                ),
                encoding="utf-8",
            )
            mesh = root / "mesh.json"
            mesh.write_text(
                json.dumps({"actual": {"battery_total_volume_m3": 1.0}}),
                encoding="utf-8",
            )
            run = root / "run"
            for processor in ("processor0", "processor1"):
                for time in range(0, 21, 5):
                    for region in ("coolant", "batterySolid"):
                        field = run / processor / str(time) / region / "T"
                        field.parent.mkdir(parents=True, exist_ok=True)
                        field.write_text("fixture", encoding="utf-8")

            report = validate_frozen_flow(
                log,
                run_dir=run,
                steady_report_path=steady,
                mesh_integral_report_path=mesh,
                qvol_w_m3=1.0,
                coolant_cp_j_kgk=1.0,
                initial_temperature_k=300.0,
                end_time_s=20.0,
                telemetry_interval_s=1.0,
                field_interval_s=5.0,
                final_window_s=10.0,
            )
            self.assertEqual(report["status"], "FROZEN_FLOW_VALIDATION_PASSED")
            self.assertEqual(
                report["time_contract"]["actual_complete_telemetry_count"], 20
            )
            self.assertEqual(
                report["time_contract"]["missing_field_frame_times_s"], []
            )
            self.assertEqual(report["time_contract"]["processor_count"], 2)
            self.assertEqual(
                report["time_contract"]["decomposed_field_frame_count"], 5
            )

            samples = root / "dynamic_heat_source.csv"
            samples.write_text(
                "time_s,qvol_W_m3\n0,1\n10,2\n20,2\n",
                encoding="utf-8",
            )
            dynamic_report = validate_frozen_flow(
                log,
                run_dir=run,
                steady_report_path=steady,
                mesh_integral_report_path=mesh,
                qvol_w_m3=1.0,
                qvol_samples_path=samples,
                coolant_cp_j_kgk=2.0,
                initial_temperature_k=300.0,
                steady_initial_temperature_k=299.0,
                end_time_s=20.0,
                telemetry_interval_s=1.0,
                field_interval_s=5.0,
                final_window_s=10.0,
            )
            self.assertEqual(
                dynamic_report["source"]["steady_reference_semantics"],
                "instantaneous_upper_bound_while_source_is_ramping",
            )
            self.assertIn(
                "storage",
                dynamic_report["source"]["transient_energy_gap_semantics"],
            )
            self.assertTrue(
                dynamic_report["final_summary"][
                    "energy_gap_includes_transient_storage"
                ]
            )
            self.assertTrue(
                dynamic_report["checks"][
                    "dynamic_final_tavg_below_instantaneous_steady_upper_bound"
                ]
            )
            self.assertTrue(
                dynamic_report["checks"][
                    "dynamic_final_tmax_below_instantaneous_steady_upper_bound"
                ]
            )
            self.assertTrue(
                dynamic_report["checks"][
                    "battery_average_temperature_trend_matches_source_contract"
                ]
            )
            self.assertEqual(
                dynamic_report["source"]["steady_reference_temperature_k"],
                299.0,
            )


if __name__ == "__main__":
    unittest.main()
