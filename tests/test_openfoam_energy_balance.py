from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from cfd_twin.openfoam_energy_balance import build_energy_balance


class OpenFoamEnergyBalanceTests(unittest.TestCase):
    def test_builds_source_removal_storage_and_residual_terms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"

            def write_function(region: str, name: str, filename: str, values: str) -> None:
                path = run / "postProcessing" / region / name / "0" / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# Time value\n" + values, encoding="utf-8")

            write_function(
                "coolant",
                "coolant_temperature_volume_integral",
                "volFieldValue.dat",
                "1 10.2\n2 10.3\n",
            )
            write_function(
                "batterySolid",
                "batterySolid_temperature_volume_integral",
                "volFieldValue.dat",
                "1 10.3\n2 10.5\n",
            )
            for patch in ("inlet", "outlet"):
                write_function(
                    "coolant",
                    f"coolant_{patch}_mass_flow",
                    "surfaceFieldValue.dat",
                    "1 1\n2 1\n" if patch == "inlet" else "1 -1\n2 -1\n",
                )
            write_function(
                "coolant",
                "coolant_inlet_mass_weighted_temperature",
                "surfaceFieldValue.dat",
                "1 10\n2 10\n",
            )
            write_function(
                "coolant",
                "coolant_outlet_mass_weighted_temperature",
                "surfaceFieldValue.dat",
                "1 11.4\n2 11.65\n",
            )

            mesh = root / "mesh.json"
            mesh.write_text(
                json.dumps(
                    {
                        "actual": {
                            "battery_total_volume_m3": 1.0,
                            "zone_volumes_m3": {"coolant": 1.0},
                        }
                    }
                ),
                encoding="utf-8",
            )
            heat = root / "heat.csv"
            heat.write_text(
                "time_s,qvol_W_m3\n0,2\n1,2\n2,2.2\n",
                encoding="utf-8",
            )
            output_csv = root / "energy.csv"
            output_report = root / "energy.json"

            report = build_energy_balance(
                run,
                mesh,
                heat,
                output_csv,
                output_report,
                fluid_density_kg_m3=1.0,
                fluid_cp_j_kgk=1.0,
                solid_density_kg_m3=1.0,
                solid_cp_j_kgk=1.0,
                initial_temperature_k=10.0,
            )

            with output_csv.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(report["status"], "TRANSIENT_ENERGY_BALANCE_WARNING")
            self.assertFalse(report["energy_balance_gate"]["passed"])
            self.assertEqual(
                report["energy_balance_gate"]["maximum_fraction"], 0.02
            )
            self.assertEqual(report["sample_count"], 2)
            self.assertAlmostEqual(float(rows[0]["generated_W"]), 2.0)
            self.assertAlmostEqual(float(rows[0]["removed_W"]), 1.4)
            self.assertAlmostEqual(float(rows[0]["stored_W"]), 0.5)
            self.assertAlmostEqual(float(rows[0]["imbalance_fraction"]), 0.05)
            self.assertAlmostEqual(float(rows[1]["stored_W"]), 0.3)
            self.assertTrue(output_report.is_file())


if __name__ == "__main__":
    unittest.main()
