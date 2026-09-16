"""Audit the *executed* fluid/solid h solves in each transient coupling loop.

The OpenFOAM loopControl limit is adaptive, but reaching its limit is not a
convergence claim. This read-only report distinguishes early exits from
limit-bound steps and preserves the raw initial residuals for both regions.
Additional PIMPLE outer-corrector solves after energyCoupling are deliberately
not mislabelled as part of the inner coupling loop.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


TIME_RE = re.compile(r"^Time\s*=\s*([0-9.eE+-]+)\s*$")
COUPLING_RE = re.compile(r"^energyCoupling:\s*(\d+)/(\d+)\s*$")
RESIDUAL_RE = re.compile(r"Solving for h, Initial residual =\s*([0-9.eE+-]+)")


def audit_energy_coupling(log: Path, *, threshold: float, expected_cap: int) -> dict[str, object]:
    if not 0 < threshold < 1 or expected_cap < 1:
        raise ValueError("positive residual threshold below 1 and iteration cap are required")
    steps: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    region: str | None = None
    awaiting: set[str] = set()
    active_pair: dict[str, float] = {}

    def finish_pair() -> None:
        nonlocal active_pair
        if current is not None and len(active_pair) == 2:
            current["last_inner_h_initial_residual"] = active_pair.copy()
            if (
                "first_both_h_below_target_iteration" not in current
                and all(value <= threshold for value in active_pair.values())
            ):
                current["first_both_h_below_target_iteration"] = current["coupling_iterations"]
        active_pair = {}

    def finish_step() -> None:
        finish_pair()
        if current is not None:
            if current["coupling_iterations"] is not None:
                pair = current.get("last_inner_h_initial_residual", {})
                current["both_inner_h_below_target"] = (
                    isinstance(pair, dict) and set(pair) == {"coolant", "batterySolid"}
                    and all(value <= threshold for value in pair.values())
                )
                current["early_exit"] = current["coupling_iterations"] < current["coupling_cap"]
            else:
                current["early_exit"] = None
                current["both_inner_h_below_target"] = None
            steps.append(current)

    with log.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.strip()
            match = TIME_RE.match(line)
            if match:
                finish_step()
                current = {"time_s": float(match.group(1)), "coupling_iterations": None, "coupling_cap": None}
                region = None
                awaiting = set()
                continue
            match = COUPLING_RE.match(line)
            if match and current is not None:
                finish_pair()
                current["coupling_iterations"] = int(match.group(1))
                current["coupling_cap"] = int(match.group(2))
                awaiting = {"coolant", "batterySolid"}
                region = None
                continue
            if line.startswith("Solving for fluid region coolant"):
                region = "coolant"
            elif line.startswith("Solving for solid region batterySolid"):
                region = "batterySolid"
            elif region in awaiting:
                match = RESIDUAL_RE.search(line)
                if match:
                    active_pair[region] = float(match.group(1))
                    awaiting.remove(region)
    finish_step()
    if not any(step["coupling_iterations"] is not None for step in steps):
        raise ValueError("no energyCoupling iterations found in solver log")
    loop_steps = [step for step in steps if step["coupling_iterations"] is not None]
    if any(step["coupling_cap"] != expected_cap or "last_inner_h_initial_residual" not in step for step in loop_steps):
        raise ValueError("some time steps have an unexpected cap or incomplete fluid/solid h pair")
    return {
        "status": "ENERGY_COUPLING_HISTORY_EXTRACTED",
        "source_log": str(log),
        "residual_definition": "OpenFOAM raw initial linear-solver residual inside energyCoupling; not final outer-corrector residual",
        "target": threshold,
        "cap": expected_cap,
        "time_step_count": len(steps),
        "coupling_loop_time_step_count": len(loop_steps),
        "coupling_not_entered_count": len(steps) - len(loop_steps),
        "early_exit_count": sum(bool(step["early_exit"]) for step in loop_steps),
        "cap_reached_count": sum(not step["early_exit"] for step in loop_steps),
        "early_exit_both_h_below_target_count": sum(bool(step["early_exit"] and step["both_inner_h_below_target"]) for step in loop_steps),
        "cap_reached_both_h_below_target_count": sum(bool(not step["early_exit"] and step["both_inner_h_below_target"]) for step in loop_steps),
        "cap_reached_after_earlier_target_count": sum(
            bool(
                not step["early_exit"]
                and step.get("first_both_h_below_target_iteration", expected_cap) < expected_cap
            )
            for step in loop_steps
        ),
        "steps": steps,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--expected-cap", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_energy_coupling(args.log, threshold=args.threshold, expected_cap=args.expected_cap)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "steps"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
