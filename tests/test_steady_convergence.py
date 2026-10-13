from __future__ import annotations

import unittest

from cfd_twin.steady_convergence import (
    continuation_decision,
    validate_maximum_iterations,
)


class SteadyConvergenceTests(unittest.TestCase):
    def test_retries_only_bounded_convergence_failures(self) -> None:
        report = {
            "status": "COLD_FLOW_VALIDATION_FAILED",
            "final_iteration": 6000,
            "checks": {
                "solver_clean_end": True,
                "battery_yplus_gate_passed": True,
                "last_200_flow_residuals_below_1e-3": False,
            },
        }
        first = continuation_decision(report, stage="cold")
        self.assertEqual(first["action"], "continue")
        self.assertEqual(first["additional_iterations"], 500)
        report["final_iteration"] = 9250
        self.assertEqual(
            continuation_decision(report, stage="cold")["additional_iterations"],
            250,
        )
        report["final_iteration"] = 9500
        self.assertEqual(continuation_decision(report, stage="cold")["action"], "fail")

    def test_non_convergence_problem_never_retries(self) -> None:
        report = {
            "status": "STEADY_CHT_VALIDATION_FAILED",
            "final_iteration": 6000,
            "checks": {
                "solver_clean_end": False,
                "last_200_flow_and_energy_residuals_below_1e-3": False,
            },
        }
        self.assertEqual(continuation_decision(report, stage="cht")["action"], "fail")
        report["checks"]["solver_clean_end"] = True
        report["checks"]["last_200_flow_and_energy_residuals_below_1e-3"] = True
        report["status"] = "STEADY_CHT_VALIDATION_PASSED"
        self.assertEqual(continuation_decision(report, stage="cht")["action"], "pass")

    def test_explicit_diagnostic_cap_continues_without_lowering_gate(self) -> None:
        report = {
            "status": "COLD_FLOW_VALIDATION_FAILED",
            "final_iteration": 9500,
            "checks": {
                "solver_clean_end": True,
                "battery_yplus_gate_passed": True,
                "last_200_flow_residuals_below_1e-3": False,
            },
        }
        decision = continuation_decision(report, stage="cold", maximum=20000)
        self.assertEqual(decision["action"], "continue")
        self.assertEqual(decision["additional_iterations"], 500)
        self.assertEqual(decision["maximum_iterations"], 20000)
        report["final_iteration"] = 20000
        decision = continuation_decision(report, stage="cold", maximum=20000)
        self.assertEqual(decision["action"], "fail")
        self.assertEqual(decision["reason"], "absolute iteration cap reached")

    def test_diagnostic_cap_is_bounded_and_reproducible(self) -> None:
        self.assertEqual(validate_maximum_iterations(None), 9500)
        self.assertEqual(validate_maximum_iterations(20000), 20000)
        for invalid in (True, 5999, 6001, 50500, "20000"):
            with self.assertRaises(ValueError):
                validate_maximum_iterations(invalid)


if __name__ == "__main__":
    unittest.main()
