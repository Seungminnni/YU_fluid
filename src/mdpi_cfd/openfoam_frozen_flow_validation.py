from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

try:
    from .openfoam_cold_flow_validation import parse_cold_flow_log
except ImportError:  # Standalone copy inside a generated OpenFOAM case.
    from validate_cold_flow import parse_cold_flow_log


def _numeric_time_directories(run_dir: Path) -> dict[float, Path]:
    times: dict[float, Path] = {}
    for path in run_dir.iterdir():
        if not path.is_dir():
            continue
        try:
            time = float(path.name)
        except ValueError:
            continue
        if math.isfinite(time):
            times[time] = path
    return times


def validate_frozen_flow(
    log_path: str | Path,
    *,
    run_dir: str | Path,
    steady_report_path: str | Path,
    mesh_integral_report_path: str | Path,
    qvol_w_m3: float,
    coolant_cp_j_kgk: float,
    initial_temperature_k: float,
    steady_initial_temperature_k: float | None = None,
    end_time_s: float = 1800.0,
    telemetry_interval_s: float = 1.0,
    field_interval_s: float = 15.0,
    final_window_s: float = 60.0,
) -> dict[str, object]:
    if steady_initial_temperature_k is None:
        steady_initial_temperature_k = initial_temperature_k
    parsed = parse_cold_flow_log(log_path)
    records = parsed["records"]
    times = sorted(float(time) for time in records)
    final_time = times[-1] if times else None
    steady_report = json.loads(Path(steady_report_path).read_text(encoding="utf-8"))
    mesh_report = json.loads(
        Path(mesh_integral_report_path).read_text(encoding="utf-8")
    )
    battery_volume = float(mesh_report["actual"]["battery_total_volume_m3"])
    heat_power = qvol_w_m3 * battery_volume

    telemetry_rows = []
    for time in times:
        values = records[f"{time:g}"]
        flows = values["mass_flow_kg_s"]
        temperatures = values["mass_weighted_temperature_k"]
        batteries = values["battery_volume_temperature_k"]
        complete_batteries = len(batteries) == 23 and all(
            {"average", "maximum"}.issubset(item) for item in batteries.values()
        )
        if not (
            {"inlet", "outlet"}.issubset(flows)
            and {"inlet", "outlet"}.issubset(temperatures)
            and complete_batteries
        ):
            continue
        through_flow = 0.5 * (abs(flows["inlet"]) + abs(flows["outlet"]))
        mass_imbalance = (
            abs(flows["inlet"] + flows["outlet"]) / through_flow
            if through_flow > 0
            else math.inf
        )
        enthalpy_out = (
            coolant_cp_j_kgk
            * through_flow
            * (temperatures["outlet"] - temperatures["inlet"])
        )
        telemetry_rows.append(
            {
                "time_s": time,
                "mass_imbalance_fraction": mass_imbalance,
                "coolant_net_enthalpy_out_w": enthalpy_out,
                "energy_imbalance_fraction": abs(enthalpy_out - heat_power)
                / heat_power,
                "battery_tavg_k": sum(
                    item["average"] for item in batteries.values()
                )
                / 23,
                "battery_tmax_k": max(
                    item["maximum"] for item in batteries.values()
                ),
                "inlet_mass_weighted_temperature_k": temperatures["inlet"],
                "outlet_mass_weighted_temperature_k": temperatures["outlet"],
            }
        )

    final_rows = [
        row
        for row in telemetry_rows
        if final_time is not None and row["time_s"] >= final_time - final_window_s
    ]
    energy = [row["energy_imbalance_fraction"] for row in final_rows]
    mass = [row["mass_imbalance_fraction"] for row in final_rows]
    tavg_series = [row["battery_tavg_k"] for row in telemetry_rows]
    tmax_series = [row["battery_tmax_k"] for row in telemetry_rows]
    final_record = records.get(f"{final_time:g}") if final_time is not None else None
    latest_bounds = {} if final_record is None else final_record["field_bounds"]
    finite_fields = all(
        field in latest_bounds
        and all(math.isfinite(value) for value in latest_bounds[field].values())
        for field in ("k", "omega", "U", "T", "p")
    )
    nonnegative_turbulence = all(
        field in latest_bounds and latest_bounds[field].get("min", -1) >= 0
        for field in ("k", "omega")
    )

    root_times = _numeric_time_directories(Path(run_dir))
    expected_frame_count = int(round(end_time_s / field_interval_s)) + 1
    expected_frame_times = [
        index * field_interval_s for index in range(expected_frame_count)
    ]
    missing_frame_times = [
        time
        for time in expected_frame_times
        if not any(abs(actual - time) < 1e-8 for actual in root_times)
    ]
    missing_temperature_fields = []
    for time in expected_frame_times:
        path = next(
            (candidate for actual, candidate in root_times.items() if abs(actual - time) < 1e-8),
            None,
        )
        if path is None:
            continue
        for region in ("coolant", "batterySolid"):
            field = path / region / "T"
            if not field.is_file() and not Path(str(field) + ".gz").is_file():
                missing_temperature_fields.append(f"{path.name}/{region}/T")

    expected_telemetry_count = int(round(end_time_s / telemetry_interval_s))
    steady_summary = steady_report.get("monitor_summary", {})
    steady_tavg = steady_summary.get("final_battery_tavg_k")
    steady_tmax = steady_summary.get("final_battery_tmax_k")
    expected_tavg = (
        None
        if steady_tavg is None
        else float(steady_tavg)
        - float(steady_initial_temperature_k)
        + initial_temperature_k
    )
    expected_tmax = (
        None
        if steady_tmax is None
        else float(steady_tmax)
        - float(steady_initial_temperature_k)
        + initial_temperature_k
    )
    final_tavg = tavg_series[-1] if tavg_series else None
    final_tmax = tmax_series[-1] if tmax_series else None
    minimum_tavg_step = (
        min(b - a for a, b in zip(tavg_series, tavg_series[1:]))
        if len(tavg_series) >= 2
        else None
    )
    checks = {
        "solver_clean_end": parsed["clean_end"] and not parsed["fatal_error"],
        "steady_cht_compound_gate_passed": steady_report.get("status")
        == "STEADY_CHT_VALIDATION_PASSED",
        "reaches_configured_end_time": final_time is not None
        and abs(final_time - end_time_s) < 1e-8,
        "complete_one_second_telemetry": len(telemetry_rows)
        >= expected_telemetry_count,
        "all_configured_field_frames_reconstructed": not missing_frame_times,
        "temperature_fields_present_in_all_frames": not missing_temperature_fields,
        "final_window_has_at_least_61_samples": len(final_rows)
        >= int(round(final_window_s / telemetry_interval_s)) + 1,
        "mass_imbalance_below_0p1_percent": bool(mass) and max(mass) < 0.001,
        "mean_final_energy_imbalance_below_2_percent": bool(energy)
        and sum(energy) / len(energy) < 0.02,
        "maximum_final_energy_imbalance_below_5_percent": bool(energy)
        and max(energy) < 0.05,
        "battery_average_temperature_is_monotonic": minimum_tavg_step is not None
        and minimum_tavg_step >= -1e-6,
        "battery_temperature_rises_above_initial": final_tmax is not None
        and final_tmax > initial_temperature_k,
        "final_tavg_matches_steady_within_0p01_k": final_tavg is not None
        and expected_tavg is not None
        and abs(final_tavg - expected_tavg) < 0.01,
        "final_tmax_matches_steady_within_0p01_k": final_tmax is not None
        and expected_tmax is not None
        and abs(final_tmax - expected_tmax) < 0.01,
        "final_fields_finite": finite_fields,
        "final_turbulence_fields_nonnegative": nonnegative_turbulence,
    }
    passed = all(checks.values())
    return {
        "schema_version": 1,
        "status": (
            "FROZEN_FLOW_VALIDATION_PASSED"
            if passed
            else "FROZEN_FLOW_VALIDATION_FAILED"
        ),
        "checks": checks,
        "publication_results_authorized": False,
        "runnable_figure_generation_pilot": passed,
        "source": {
            "qvol_w_m3": qvol_w_m3,
            "mesh_integrated_battery_volume_m3": battery_volume,
            "mesh_integrated_heat_power_w": heat_power,
            "coolant_cp_j_kgk": coolant_cp_j_kgk,
            "initial_temperature_k": initial_temperature_k,
            "steady_initial_temperature_k": steady_initial_temperature_k,
            "steady_report": str(Path(steady_report_path).resolve()),
        },
        "time_contract": {
            "end_time_s": end_time_s,
            "telemetry_interval_s": telemetry_interval_s,
            "field_interval_s": field_interval_s,
            "expected_telemetry_count": expected_telemetry_count,
            "actual_complete_telemetry_count": len(telemetry_rows),
            "expected_field_frame_count": expected_frame_count,
            "actual_numeric_root_time_count": len(root_times),
            "missing_field_frame_times_s": missing_frame_times,
            "missing_temperature_fields": missing_temperature_fields,
        },
        "final_summary": {
            "final_time_s": final_time,
            "battery_tavg_k": final_tavg,
            "battery_tmax_k": final_tmax,
            "steady_battery_tavg_k": steady_tavg,
            "steady_battery_tmax_k": steady_tmax,
            "datum_shifted_expected_steady_battery_tavg_k": expected_tavg,
            "datum_shifted_expected_steady_battery_tmax_k": expected_tmax,
            "tavg_difference_from_steady_k": (
                None
                if final_tavg is None or expected_tavg is None
                else final_tavg - expected_tavg
            ),
            "tmax_difference_from_steady_k": (
                None
                if final_tmax is None or expected_tmax is None
                else final_tmax - expected_tmax
            ),
            "minimum_one_step_tavg_change_k": minimum_tavg_step,
            "maximum_mass_imbalance_fraction": max(mass) if mass else None,
            "mean_energy_imbalance_fraction": (
                sum(energy) / len(energy) if energy else None
            ),
            "maximum_energy_imbalance_fraction": max(energy) if energy else None,
        },
        "final_window_rows": final_rows,
        "final_field_bounds": latest_bounds,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate frozen-flow thermal CHT")
    parser.add_argument("--log", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--steady-report", required=True)
    parser.add_argument("--mesh-integral-report", required=True)
    parser.add_argument("--qvol", required=True, type=float)
    parser.add_argument("--coolant-cp", required=True, type=float)
    parser.add_argument("--initial-temperature", required=True, type=float)
    parser.add_argument("--steady-initial-temperature", type=float)
    parser.add_argument("--field-interval", type=float, default=15.0)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        report = validate_frozen_flow(
            args.log,
            run_dir=args.run_dir,
            steady_report_path=args.steady_report,
            mesh_integral_report_path=args.mesh_integral_report,
            qvol_w_m3=args.qvol,
            coolant_cp_j_kgk=args.coolant_cp,
            initial_temperature_k=args.initial_temperature,
            steady_initial_temperature_k=args.steady_initial_temperature,
            field_interval_s=args.field_interval,
        )
    except (OSError, UnicodeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        report = {
            "schema_version": 1,
            "status": "FROZEN_FLOW_VALIDATION_FAILED",
            "error": str(exc),
            "publication_results_authorized": False,
            "runnable_figure_generation_pilot": False,
        }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "FROZEN_FLOW_VALIDATION_PASSED" else 2


if __name__ == "__main__":
    sys.exit(main())
