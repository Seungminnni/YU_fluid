from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from cfd_twin.openfoam_transient_residuals import (
    extract_transient_residuals,
    write_transient_residuals,
)


class OpenfoamTransientResidualTests(unittest.TestCase):
    def test_cumulative_axis_counts_pimple_outer_corrections_not_linear_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "solver.log"
            lines = []
            for time_step, time_value in enumerate((0.1, 0.2), start=1):
                lines.append(f"Time = {time_value}")
                for outer in range(1, 4):
                    lines.extend(
                        (
                            f"PIMPLE: iteration {outer}",
                            "Solving for fluid region coolant",
                            "smoothSolver: Solving for Ux, Initial residual = "
                            f"{1e-2 / (time_step * outer):.8g}, Final residual = 1e-7, No Iterations 7",
                            "GAMG: Solving for p_rgh, Initial residual = "
                            f"{2e-2 / (time_step * outer):.8g}, Final residual = 2e-7, No Iterations 5",
                            "Solving for solid region batterySolid",
                            "PCG: Solving for h, Initial residual = "
                            f"{3e-2 / (time_step * outer):.8g}, Final residual = 3e-8, No Iterations 9",
                        )
                    )
            lines.append("End")
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")

            report = extract_transient_residuals(log)

            self.assertEqual(report["status"], "TRANSIENT_RESIDUAL_HISTORY_EXTRACTED")
            self.assertEqual(report["axis"], "cumulative_pimple_outer_iteration")
            self.assertEqual(report["global_iteration_count"], 6)
            self.assertEqual(report["time_step_count"], 2)
            self.assertEqual(report["explicit_pimple_markers"], 6)
            self.assertEqual(report["row_count"], 18)
            self.assertTrue(report["solver_clean_end"])
            self.assertFalse(report["fluent_scaled_residual"])
            self.assertEqual(report["rows"][0]["global_iteration"], 1)
            self.assertEqual(report["rows"][-1]["global_iteration"], 6)

            csv_path = root / "transient_residuals.csv"
            summary_path = root / "transient_residuals_summary.json"
            write_transient_residuals(
                report, csv_path=csv_path, summary_path=summary_path
            )
            with csv_path.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 18)
            self.assertEqual(rows[0]["global_iteration"], "1")
            self.assertNotIn("rows", summary_path.read_text(encoding="utf-8"))

    def test_infers_outer_corrections_when_openfoam_omits_pimple_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "solver.log"
            lines = ["Time = 0.003"]
            for outer in range(1, 4):
                lines.extend(
                    (
                        "Solving for fluid region coolant",
                        "smoothSolver: Solving for Ux, Initial residual = "
                        f"{1e-2 / outer:.8g}, Final residual = 1e-7, No Iterations 2",
                        "Solving for solid region batterySolid",
                        "PCG: Solving for h, Initial residual = "
                        f"{2e-2 / outer:.8g}, Final residual = 1e-8, No Iterations 1",
                    )
                )
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")

            report = extract_transient_residuals(log)

            self.assertEqual(report["explicit_pimple_markers"], 0)
            self.assertEqual(report["time_step_count"], 1)
            self.assertEqual(report["global_iteration_count"], 3)
            self.assertEqual(report["row_count"], 6)
            self.assertEqual(
                sorted({row["outer_corrector"] for row in report["rows"]}),
                [1, 2, 3],
            )


if __name__ == "__main__":
    unittest.main()
