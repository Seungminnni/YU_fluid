from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cfd_twin.openfoam_energy_coupling_audit import audit_energy_coupling


class EnergyCouplingAuditTests(unittest.TestCase):
    def test_early_exit_cap_and_no_loop_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "solver.log"
            log.write_text(
                "Time = 1\n"
                "energyCoupling: 1/200\n"
                "Solving for fluid region coolant\n"
                "Solving for h, Initial residual = 2e-4, Final residual = 1e-8\n"
                "Solving for solid region batterySolid\n"
                "Solving for h, Initial residual = 3e-4, Final residual = 1e-8\n"
                "energyCoupling: 2/200\n"
                "Solving for fluid region coolant\n"
                "Solving for h, Initial residual = 3e-6, Final residual = 1e-8\n"
                "Solving for solid region batterySolid\n"
                "Solving for h, Initial residual = 8e-6, Final residual = 1e-8\n"
                "Time = 2\n"
                "Solving for fluid region coolant\n"
                "Solving for h, Initial residual = 4e-6, Final residual = 1e-8\n"
                "Time = 3\n"
                "energyCoupling: 200/200\n"
                "Solving for fluid region coolant\n"
                "Solving for h, Initial residual = 2e-5, Final residual = 1e-8\n"
                "Solving for solid region batterySolid\n"
                "Solving for h, Initial residual = 3e-5, Final residual = 1e-8\n",
                encoding="utf-8",
            )
            report = audit_energy_coupling(log, threshold=1e-5, expected_cap=200)
            self.assertEqual(report["time_step_count"], 3)
            self.assertEqual(report["coupling_not_entered_count"], 1)
            self.assertEqual(report["early_exit_count"], 1)
            self.assertEqual(report["cap_reached_count"], 1)
            self.assertEqual(report["early_exit_both_h_below_target_count"], 1)
            self.assertEqual(report["steps"][0]["last_inner_h_initial_residual"]["batterySolid"], 8e-6)


if __name__ == "__main__":
    unittest.main()
