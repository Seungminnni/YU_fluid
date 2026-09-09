from __future__ import annotations

import argparse
import csv
import json
import os
import re
from pathlib import Path


NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"


def extract_transient_residuals(log_path: str | Path) -> dict[str, object]:
    """Extract one OpenFOAM residual sample per PIMPLE outer correction.

    ``global_iteration`` is a monotonically increasing nonlinear-iteration
    counter.  It deliberately does not sum the ``No Iterations`` values from
    the individual linear solvers.  This gives the closest truthful OpenFOAM
    equivalent of Fluent's cumulative transient iteration axis while keeping
    the raw OpenFOAM residual definition explicit.
    """

    log = Path(log_path).resolve()
    current_time: float | None = None
    current_time_step = 0
    current_outer = 0
    current_global: int | None = None
    current_region: str | None = None
    global_iteration = 0
    explicit_pimple_markers = 0
    solver_clean_end = False
    fatal_error = False
    samples: dict[tuple[int, str, str], dict[str, object]] = {}

    def activate_iteration(outer_corrector: int) -> int | None:
        nonlocal global_iteration, current_global, current_outer
        if current_time is None:
            return None
        global_iteration += 1
        current_global = global_iteration
        current_outer = outer_corrector
        return current_global

    with log.open(encoding="utf-8", errors="replace") as stream:
        lines = stream
        for line in lines:
            if re.fullmatch(r"End\s*", line):
                solver_clean_end = True
            if "FOAM FATAL" in line or "MPI_ABORT" in line:
                fatal_error = True
            time_match = re.match(rf"^Time\s*=\s*({NUMBER})\s*$", line)
            if time_match:
                current_time = float(time_match.group(1))
                current_time_step += 1
                current_outer = 0
                current_global = None
                current_region = None
                continue

            pimple_match = re.search(r"\bPIMPLE:\s*(?:iteration|Iteration)\s+(\d+)", line)
            if pimple_match:
                explicit_pimple_markers += 1
                activate_iteration(int(pimple_match.group(1)))
                current_region = None
                continue

            region_match = re.search(r"Solving for (?:fluid|solid) region (\S+)", line)
            if region_match:
                current_region = region_match.group(1)
                continue

            residual_match = re.search(
                rf"Solving for (\w+), Initial residual = ({NUMBER}), "
                rf"Final residual = ({NUMBER}), No Iterations (\d+)",
                line,
            )
            if residual_match and current_time is not None and current_region:
                if current_global is None:
                    # Some OpenFOAM builds omit the textual PIMPLE marker.  In
                    # that case each Time block is kept as one explicit sample;
                    # no unrecorded correctors are invented.
                    activate_iteration(1)
                assert current_global is not None
                field = residual_match.group(1)
                key = (current_global, current_region, field)
                initial = float(residual_match.group(2))
                final = float(residual_match.group(3))
                linear_iterations = int(residual_match.group(4))
                existing = samples.get(key)
                if existing is None:
                    samples[key] = {
                        "global_iteration": current_global,
                        "time_s": current_time,
                        "time_step_index": current_time_step,
                        "outer_corrector": current_outer,
                        "region": current_region,
                        "field": field,
                        "initial_residual": initial,
                        "final_residual": final,
                        "linear_iterations": linear_iterations,
                        "residual_definition": "OpenFOAM initial linear-solver residual",
                    }
                else:
                    # Pressure correctors can solve the same field more than once
                    # in a PIMPLE outer correction.  Plot the largest initial
                    # residual and retain the final value from the last solve.
                    existing["initial_residual"] = max(
                        float(existing["initial_residual"]), initial
                    )
                    existing["final_residual"] = final
                    existing["linear_iterations"] = int(
                        existing["linear_iterations"]
                    ) + linear_iterations

    rows = sorted(
        samples.values(),
        key=lambda row: (
            int(row["global_iteration"]),
            str(row["region"]),
            str(row["field"]),
        ),
    )
    time_values = sorted({float(row["time_s"]) for row in rows})
    return {
        "schema_version": 1,
        "status": (
            "TRANSIENT_RESIDUAL_HISTORY_EXTRACTED"
            if rows
            else "TRANSIENT_RESIDUAL_HISTORY_EMPTY"
        ),
        "source_log": str(log),
        "solver_clean_end": solver_clean_end,
        "fatal_error": fatal_error,
        "axis": "cumulative_pimple_outer_iteration",
        "residual_definition": "OpenFOAM raw initial linear-solver residual",
        "fluent_scaled_residual": False,
        "explicit_pimple_markers": explicit_pimple_markers,
        "global_iteration_count": global_iteration,
        "time_step_count": len(time_values),
        "first_time_s": time_values[0] if time_values else None,
        "last_time_s": time_values[-1] if time_values else None,
        "row_count": len(rows),
        "rows": rows,
    }


CSV_FIELDS = (
    "global_iteration",
    "time_s",
    "time_step_index",
    "outer_corrector",
    "region",
    "field",
    "initial_residual",
    "final_residual",
    "linear_iterations",
    "residual_definition",
)


def write_transient_residuals(
    report: dict[str, object], *, csv_path: str | Path, summary_path: str | Path
) -> None:
    csv_output = Path(csv_path).resolve()
    summary_output = Path(summary_path).resolve()
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    summary_output.parent.mkdir(parents=True, exist_ok=True)
    csv_temporary = csv_output.with_suffix(csv_output.suffix + ".tmp")
    with csv_temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(report["rows"])
    os.replace(csv_temporary, csv_output)

    summary = {key: value for key, value in report.items() if key != "rows"}
    summary_temporary = summary_output.with_suffix(summary_output.suffix + ".tmp")
    summary_temporary.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(summary_temporary, summary_output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--summary", required=True)
    args = parser.parse_args()
    report = extract_transient_residuals(args.log)
    write_transient_residuals(report, csv_path=args.csv, summary_path=args.summary)
    if report["status"] != "TRANSIENT_RESIDUAL_HISTORY_EXTRACTED":
        raise SystemExit("no transient residual records were extracted")


if __name__ == "__main__":
    main()
