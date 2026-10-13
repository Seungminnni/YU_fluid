"""발열 없는 정상 유동이 수렴했는지 복합 기준으로 판정한다.

OpenFOAM raw initial residual뿐 아니라 질량보존, 압력강하 안정성, 유한한
최종장, 비음수 난류장과 실제 배터리 표면 y+를 모두 통과해야 다음 열해석을
허용한다. 잔차 하나만 작다고 수렴으로 판정하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path


NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
DEFAULT_RESIDUAL_FLOORS = {
    "coolant.Ux": 4.0e-4,
    "coolant.Uy": 4.0e-4,
    "coolant.Uz": 2.0e-4,
    "coolant.h": 2.5e-4,
    "coolant.p_rgh": 2.5e-3,
    "coolant.k": 1.5e-4,
    "coolant.omega": 2.0e-6,
    "batterySolid.h": 5.0e-4,
}
STRICT_RESIDUAL_TARGET = 1.0e-6
# User-selected engineering acceptance band for the Ø32/Ø33/Ø34 study.
# The independent mass balance, pressure-drop stability, finite-field, y+
# and energy-balance gates remain mandatory.  The 1e-6 audit is still
# recorded separately and is never relabelled as the acceptance gate.
ACCEPTANCE_RESIDUAL_TARGET = 1.0e-3
MINIMUM_STEADY_ITERATIONS = 5000
FLOW_ACCEPTANCE_FIELDS = (
    "coolant.Ux",
    "coolant.Uy",
    "coolant.Uz",
    "coolant.p_rgh",
    "coolant.k",
    "coolant.omega",
)


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def parse_cold_flow_log(path: str | Path) -> dict[str, object]:
    """솔버 로그에서 반복별 잔차, 유량, 압력, 온도와 field bounds를 읽는다."""

    log = Path(path).resolve()
    text = log.read_text(encoding="utf-8", errors="replace")
    records: dict[float, dict[str, object]] = {}
    current_time: float | None = None
    current_region: str | None = None

    def record() -> dict[str, object] | None:
        if current_time is None:
            return None
        return records.setdefault(
            current_time,
            {
                "residuals": defaultdict(list),
                "mass_flow_kg_s": {},
                "pressure_pa": {},
                "mass_weighted_temperature_k": {},
                "battery_volume_temperature_k": defaultdict(dict),
                "field_bounds": {},
                "bounded_corrections": defaultdict(list),
                "continuity": None,
            },
        )

    for line in text.splitlines():
        time_match = re.match(rf"^Time\s*=\s*({NUMBER})\s*$", line)
        if time_match:
            current_time = float(time_match.group(1))
            current_region = None
            record()
            continue
        region_match = re.search(r"Solving for (?:fluid|solid) region (\S+)", line)
        if region_match:
            current_region = region_match.group(1)
            continue
        current = record()
        if current is None:
            continue
        residual_match = re.search(
            rf"Solving for (\w+), Initial residual = ({NUMBER}), "
            rf"Final residual = ({NUMBER}), No Iterations (\d+)",
            line,
        )
        if residual_match and current_region:
            key = f"{current_region}.{residual_match.group(1)}"
            current["residuals"][key].append(
                {
                    "initial": float(residual_match.group(2)),
                    "final": float(residual_match.group(3)),
                    "linear_iterations": int(residual_match.group(4)),
                }
            )
            continue
        continuity_match = re.search(
            rf"sum local = ({NUMBER}), global = ({NUMBER}), cumulative = ({NUMBER})",
            line,
        )
        if continuity_match:
            current["continuity"] = {
                "sum_local": float(continuity_match.group(1)),
                "global": float(continuity_match.group(2)),
                "cumulative": float(continuity_match.group(3)),
            }
            continue
        bounding_match = re.search(
            rf"\bbounding (k|omega), min: ({NUMBER}) max: ({NUMBER}) average: ({NUMBER})",
            line,
        )
        if bounding_match:
            current["bounded_corrections"][bounding_match.group(1)].append(
                {
                    "pre_bound_minimum": float(bounding_match.group(2)),
                    "pre_bound_maximum": float(bounding_match.group(3)),
                    "pre_bound_average": float(bounding_match.group(4)),
                }
            )
            continue
        flow_match = re.search(rf"sum\((inlet|outlet)\) of phi = ({NUMBER})", line)
        if flow_match:
            current["mass_flow_kg_s"][flow_match.group(1)] = float(
                flow_match.group(2)
            )
            continue
        pressure_match = re.search(
            rf"areaAverage\((inlet|outlet)\) of p = ({NUMBER})", line
        )
        if pressure_match:
            current["pressure_pa"][pressure_match.group(1)] = float(
                pressure_match.group(2)
            )
            continue
        weighted_temperature_match = re.search(
            rf"weightedAverage\((inlet|outlet)\) of T = ({NUMBER})", line
        )
        if weighted_temperature_match:
            current["mass_weighted_temperature_k"][
                weighted_temperature_match.group(1)
            ] = float(weighted_temperature_match.group(2))
            continue
        battery_temperature_match = re.search(
            rf"\b(volAverage|max)\(battery_(C\d{{2}})\) of T = ({NUMBER})",
            line,
        )
        if battery_temperature_match:
            operation = (
                "average"
                if battery_temperature_match.group(1) == "volAverage"
                else "maximum"
            )
            current["battery_volume_temperature_k"][
                battery_temperature_match.group(2)
            ][operation] = float(battery_temperature_match.group(3))
            continue
        minmax_match = re.search(
            rf"\bmin/max\((?:mag\()?(\w+)\)?\) = ({NUMBER}) ({NUMBER})\b",
            line,
        )
        if minmax_match:
            current["field_bounds"][minmax_match.group(1)] = {
                "min": float(minmax_match.group(2)),
                "max": float(minmax_match.group(3)),
            }
            continue
        bound_match = re.search(
            rf"\b(min|max)\((?:mag\()?(\w+)\)?\) = ({NUMBER})\b", line
        )
        if bound_match:
            field = bound_match.group(2)
            current["field_bounds"].setdefault(field, {})[
                bound_match.group(1)
            ] = float(bound_match.group(3))

    serialisable_records: dict[str, object] = {}
    for time, values in records.items():
        values["residuals"] = dict(values["residuals"])
        values["bounded_corrections"] = dict(values["bounded_corrections"])
        values["battery_volume_temperature_k"] = dict(
            values["battery_volume_temperature_k"]
        )
        serialisable_records[f"{time:g}"] = values
    return {
        "log": str(log),
        "fatal_error": "FOAM FATAL" in text or "MPI_ABORT" in text,
        "clean_end": bool(re.search(r"(?m)^End\s*$", text)),
        "records": serialisable_records,
    }


def _residual_maxima(record: dict[str, object]) -> dict[str, float]:
    return {
        name: max(item["initial"] for item in values)
        for name, values in record["residuals"].items()
        if values
    }


def _window_residual_maxima(
    window_records: list[tuple[float, dict[str, object]]],
) -> dict[str, float]:
    """Return the worst raw initial residual for each equation in the window."""

    maxima: dict[str, float] = {}
    for _, record in window_records:
        for name, value in _residual_maxima(record).items():
            maxima[name] = max(maxima.get(name, value), value)
    return maxima


def validate_cold_flow(
    log_path: str | Path,
    *,
    yplus_report_path: str | Path | None = None,
    monitor_window: int = 200,
    mass_imbalance_limit: float = 0.001,
    pressure_drop_span_limit: float = 0.005,
    residual_floors: dict[str, float] | None = None,
) -> dict[str, object]:
    """마지막 감시구간의 잔차·질량·압력·y+ gate를 한 번에 판정한다."""

    parsed = parse_cold_flow_log(log_path)
    records = parsed["records"]
    numeric_times = sorted(float(time) for time in records)
    final_time = numeric_times[-1] if numeric_times else None
    start_time = None if final_time is None else final_time - monitor_window
    window_records = [
        (time, records[f"{time:g}"])
        for time in numeric_times
        if start_time is not None and time >= start_time
    ]
    final_record = records.get(f"{final_time:g}") if final_time is not None else None

    floors = dict(DEFAULT_RESIDUAL_FLOORS if residual_floors is None else residual_floors)
    final_residuals = {} if final_record is None else _residual_maxima(final_record)
    residual_checks = {
        name: name in final_residuals and final_residuals[name] <= limit
        for name, limit in floors.items()
    }
    strict_residual_checks = {
        name: value <= STRICT_RESIDUAL_TARGET
        for name, value in final_residuals.items()
    }
    window_residuals = _window_residual_maxima(window_records)
    acceptance_residual_checks = {
        name: name in window_residuals
        and window_residuals[name] <= ACCEPTANCE_RESIDUAL_TARGET
        for name in FLOW_ACCEPTANCE_FIELDS
    }

    monitor_rows = []
    for time, values in window_records:
        flows = values["mass_flow_kg_s"]
        pressures = values["pressure_pa"]
        if {"inlet", "outlet"}.issubset(flows) and {
            "inlet",
            "outlet",
        }.issubset(pressures):
            denominator = 0.5 * (abs(flows["inlet"]) + abs(flows["outlet"]))
            imbalance = (
                abs(flows["inlet"] + flows["outlet"]) / denominator
                if denominator > 0
                else math.inf
            )
            monitor_rows.append(
                {
                    "time": time,
                    "inlet_mass_flow_kg_s": flows["inlet"],
                    "outlet_mass_flow_kg_s": flows["outlet"],
                    "mass_imbalance_fraction": imbalance,
                    "pressure_drop_pa": pressures["inlet"] - pressures["outlet"],
                }
            )
    pressure_drops = [row["pressure_drop_pa"] for row in monitor_rows]
    mean_drop = (
        sum(pressure_drops) / len(pressure_drops) if pressure_drops else None
    )
    pressure_span_fraction = (
        (max(pressure_drops) - min(pressure_drops)) / abs(mean_drop)
        if pressure_drops and mean_drop and math.isfinite(mean_drop)
        else None
    )
    maximum_mass_imbalance = (
        max(row["mass_imbalance_fraction"] for row in monitor_rows)
        if monitor_rows
        else None
    )

    latest_bounds = {} if final_record is None else final_record["field_bounds"]
    bounded_correction_summary = {}
    for field in ("k", "omega"):
        events = [
            event
            for _, values in window_records
            for event in values["bounded_corrections"].get(field, [])
        ]
        bounded_correction_summary[field] = {
            "event_count_in_monitor_window": len(events),
            "minimum_pre_bound_value": (
                min(event["pre_bound_minimum"] for event in events)
                if events
                else None
            ),
        }
    finite_positive_turbulence = all(
        field in latest_bounds
        and _finite(latest_bounds[field].get("min"))
        and latest_bounds[field]["min"] >= 0
        and _finite(latest_bounds[field].get("max"))
        for field in ("k", "omega")
    )
    finite_required_bounds = all(
        field in latest_bounds
        and all(_finite(value) for value in latest_bounds[field].values())
        for field in ("k", "omega", "U", "T", "p")
    )

    yplus_report = None
    if yplus_report_path is not None:
        yplus_report = json.loads(Path(yplus_report_path).read_text(encoding="utf-8"))
    # 모든 항목의 AND가 최종 PASS다. 하나라도 실패하면 열원 계산으로 넘어가지 않는다.
    checks = {
        "solver_clean_end": parsed["clean_end"] and not parsed["fatal_error"],
        "final_iteration_present": final_record is not None,
        "minimum_5000_outer_iterations_completed": final_time is not None
        and final_time >= MINIMUM_STEADY_ITERATIONS,
        "last_200_flow_residuals_below_1e-3": all(
            acceptance_residual_checks.values()
        ),
        "pilot_derived_residual_floors_reached": all(residual_checks.values()),
        "monitor_window_has_at_least_21_samples": len(monitor_rows) >= 21,
        "mass_imbalance_below_0p1_percent": (
            maximum_mass_imbalance is not None
            and maximum_mass_imbalance < mass_imbalance_limit
        ),
        "pressure_drop_span_below_0p5_percent": (
            pressure_span_fraction is not None
            and pressure_span_fraction < pressure_drop_span_limit
        ),
        "final_fields_finite": finite_required_bounds,
        "final_turbulence_fields_nonnegative": finite_positive_turbulence,
        "battery_yplus_gate_passed": (
            yplus_report is not None
            and yplus_report.get("status") == "YPLUS_VALIDATION_PASSED"
        ),
    }
    return {
        "schema_version": 1,
        "status": (
            "COLD_FLOW_VALIDATION_PASSED"
            if all(checks.values())
            else "COLD_FLOW_VALIDATION_FAILED"
        ),
        "checks": checks,
        "runnable_thermal_pilot": all(checks.values()),
        "publication_results_authorized": False,
        "log": parsed["log"],
        "final_iteration": final_time,
        "monitor_window_outer_iterations": monitor_window,
        "residual_floors": floors,
        "final_initial_residual_maxima": final_residuals,
        "residual_checks": residual_checks,
        "acceptance_residual_audit": {
            "target": ACCEPTANCE_RESIDUAL_TARGET,
            "required_fields": list(FLOW_ACCEPTANCE_FIELDS),
            "window_outer_iterations": monitor_window,
            "window_maxima": window_residuals,
            "checks": acceptance_residual_checks,
        },
        "strict_residual_audit": {
            "target": STRICT_RESIDUAL_TARGET,
            "all_reported_equations_at_or_below_target": bool(strict_residual_checks)
            and all(strict_residual_checks.values()),
            "checks": strict_residual_checks,
            "policy": "record exactly; do not relabel a miss as converged to 1e-6",
        },
        "monitor_summary": {
            "sample_count": len(monitor_rows),
            "maximum_mass_imbalance_fraction": maximum_mass_imbalance,
            "mass_imbalance_limit_fraction": mass_imbalance_limit,
            "mean_pressure_drop_pa": mean_drop,
            "pressure_drop_min_pa": min(pressure_drops) if pressure_drops else None,
            "pressure_drop_max_pa": max(pressure_drops) if pressure_drops else None,
            "pressure_drop_span_fraction": pressure_span_fraction,
            "pressure_drop_span_limit_fraction": pressure_drop_span_limit,
        },
        "final_field_bounds": latest_bounds,
        "bounded_correction_diagnostics": bounded_correction_summary,
        "monitor_rows": monitor_rows,
        "yplus_report": yplus_report,
        "residual_floor_basis": (
            "OpenFOAM-normalised equation-specific floors selected from the "
            "calibrated pilot plateau with a small repeatability margin; "
            "acceptance also requires independent 200-iteration mass-flow and "
            "pressure-drop stability plus field-bound and y+ gates."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate an OpenFOAM steady cold-flow pilot"
    )
    parser.add_argument("--log", required=True)
    parser.add_argument("--yplus-report", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        report = validate_cold_flow(
            args.log,
            yplus_report_path=args.yplus_report,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        report = {
            "schema_version": 1,
            "status": "COLD_FLOW_VALIDATION_FAILED",
            "error": str(exc),
            "runnable_thermal_pilot": False,
            "publication_results_authorized": False,
        }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "COLD_FLOW_VALIDATION_PASSED" else 2


if __name__ == "__main__":
    sys.exit(main())
