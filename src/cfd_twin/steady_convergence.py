"""Deterministic decision for continuing a completed OpenFOAM steady stage.

This is run only *after* the solver has stopped and the normal physics gate
has written its JSON report. A missing report, fatal solver error, invalid
field, failed mesh/y+ gate or other unknown failure is never retried blindly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


RETRYABLE_CHECKS = {
    "cold": {
        "minimum_5000_outer_iterations_completed",
        "last_200_flow_residuals_below_1e-4",
        "pilot_derived_residual_floors_reached",
        "monitor_window_has_at_least_21_samples",
        "mass_imbalance_below_0p1_percent",
        "pressure_drop_span_below_0p5_percent",
    },
    "cht": {
        "minimum_5000_outer_iterations_completed",
        "last_200_flow_and_energy_residuals_below_1e-4",
        "pilot_derived_residual_floors_reached",
        "monitor_window_has_at_least_21_samples",
        "mass_imbalance_below_0p1_percent",
        "pressure_drop_span_below_0p5_percent",
        "battery_tmax_change_below_0p001_k",
        "battery_tavg_change_below_0p001_k",
        "mean_energy_imbalance_below_2_percent",
        "maximum_energy_imbalance_below_5_percent",
    },
}
DEFAULT_MAXIMUM_ITERATIONS = 9500
MINIMUM_MAXIMUM_ITERATIONS = 6000
MAXIMUM_DIAGNOSTIC_ITERATIONS = 50000
PASS_STATUS = {
    "cold": "COLD_FLOW_VALIDATION_PASSED",
    "cht": "STEADY_CHT_VALIDATION_PASSED",
}


def validate_maximum_iterations(value: object | None) -> int:
    """Return a bounded, reproducible S0 iteration cap.

    A larger cap is diagnostic only.  It never changes any acceptance gate or
    permits S1/S2/publication after a failed S0 report.
    """

    if value is None:
        return DEFAULT_MAXIMUM_ITERATIONS
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("cold_flow_maximum_iterations must be an integer")
    if not MINIMUM_MAXIMUM_ITERATIONS <= value <= MAXIMUM_DIAGNOSTIC_ITERATIONS:
        raise ValueError(
            "cold_flow_maximum_iterations must be between "
            f"{MINIMUM_MAXIMUM_ITERATIONS} and {MAXIMUM_DIAGNOSTIC_ITERATIONS}"
        )
    if value % 500:
        raise ValueError("cold_flow_maximum_iterations must be a multiple of 500")
    return value


def continuation_decision(
    report: dict[str, object], *, stage: str,
    maximum: int = DEFAULT_MAXIMUM_ITERATIONS,
    increment: int = 500,
) -> dict[str, object]:
    """Choose pass, bounded continuation or fail from measured gate results."""

    if stage not in RETRYABLE_CHECKS:
        raise ValueError(f"unsupported steady stage: {stage}")
    if maximum <= 0 or increment <= 0:
        raise ValueError("maximum and increment must be positive")
    checks = report.get("checks")
    final = report.get("final_iteration")
    if not isinstance(checks, dict) or isinstance(final, bool) or not isinstance(final, (int, float)):
        return {"action": "fail", "reason": "missing measured checks or iteration",
                "maximum_iterations": maximum}
    iteration = int(final)
    if iteration != final or iteration < 0:
        return {"action": "fail", "reason": "invalid final iteration",
                "maximum_iterations": maximum}
    failed = sorted(name for name, passed in checks.items() if passed is not True)
    if report.get("status") == PASS_STATUS[stage] and not failed:
        return {"action": "pass", "iteration": iteration, "failed_checks": [],
                "maximum_iterations": maximum}
    if not failed or any(name not in RETRYABLE_CHECKS[stage] for name in failed):
        return {
            "action": "fail", "iteration": iteration,
            "reason": "non-retryable or unrecognised gate failure",
            "failed_checks": failed, "maximum_iterations": maximum,
        }
    if iteration >= maximum:
        return {
            "action": "fail", "iteration": iteration,
            "reason": "absolute iteration cap reached", "failed_checks": failed,
            "maximum_iterations": maximum,
        }
    return {
        "action": "continue", "iteration": iteration,
        "additional_iterations": min(increment, maximum - iteration),
        "failed_checks": failed, "maximum_iterations": maximum,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded steady-CFD convergence gate")
    parser.add_argument("--report", required=True)
    parser.add_argument("--stage", required=True, choices=("cold", "cht"))
    parser.add_argument("--maximum", type=int, default=9500)
    parser.add_argument("--increment", type=int, default=500)
    parser.add_argument("--decision", required=True)
    args = parser.parse_args(argv)
    try:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        decision = continuation_decision(
            report, stage=args.stage, maximum=args.maximum, increment=args.increment
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        decision = {"action": "fail", "reason": str(exc)}
    Path(args.decision).parent.mkdir(parents=True, exist_ok=True)
    Path(args.decision).write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(decision, sort_keys=True))
    return {"pass": 0, "continue": 10, "fail": 2}[str(decision["action"])]


if __name__ == "__main__":
    raise SystemExit(main())
