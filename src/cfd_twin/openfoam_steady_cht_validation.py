"""상수 발열 정상 CHT의 온도 안정성과 에너지수지를 검증한다.

메시에서 적분한 실제 batterySolid 체적에 q'''를 곱해 입력열을 구하고,
냉각수 입출구 엔탈피 증가와 비교한다. 마지막 구간의 온도 변화, 질량보존,
압력 안정성, 잔차까지 모두 통과해야 과도 열해석을 허용한다.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

try:
    from .openfoam_cold_flow_validation import (
        ACCEPTANCE_RESIDUAL_TARGET,
        DEFAULT_RESIDUAL_FLOORS,
        FLOW_ACCEPTANCE_FIELDS,
        MINIMUM_STEADY_ITERATIONS,
        STRICT_RESIDUAL_TARGET,
        _residual_maxima,
        _window_residual_maxima,
        parse_cold_flow_log,
    )
except ImportError:  # Standalone copy inside a generated OpenFOAM case.
    from validate_cold_flow import (
        ACCEPTANCE_RESIDUAL_TARGET,
        DEFAULT_RESIDUAL_FLOORS,
        FLOW_ACCEPTANCE_FIELDS,
        MINIMUM_STEADY_ITERATIONS,
        STRICT_RESIDUAL_TARGET,
        _residual_maxima,
        _window_residual_maxima,
        parse_cold_flow_log,
    )


def validate_steady_cht(
    log_path: str | Path,
    *,
    cold_flow_report_path: str | Path,
    mesh_integral_report_path: str | Path,
    qvol_w_m3: float,
    coolant_cp_j_kgk: float,
    initial_temperature_k: float,
    monitor_window: int = 200,
) -> dict[str, object]:
    """정상 CHT 로그와 이전 cold-flow/메시 보고서를 결합해 PASS를 판정한다."""

    parsed = parse_cold_flow_log(log_path)
    records = parsed["records"]
    times = sorted(float(time) for time in records)
    final_time = times[-1] if times else None
    start_time = None if final_time is None else final_time - monitor_window
    window_records = [
        (time, records[f"{time:g}"])
        for time in times
        if start_time is not None and time >= start_time
    ]
    final_record = records.get(f"{final_time:g}") if final_time is not None else None
    cold_report = json.loads(Path(cold_flow_report_path).read_text(encoding="utf-8"))
    mesh_report = json.loads(
        Path(mesh_integral_report_path).read_text(encoding="utf-8")
    )
    battery_volume = float(mesh_report["actual"]["battery_total_volume_m3"])
    # 입력열 [W] = 체적발열 [W/m^3] * 실제 메시 고체체적 [m^3].
    heat_power = qvol_w_m3 * battery_volume

    floors = dict(DEFAULT_RESIDUAL_FLOORS)
    # The coupled thermal solve performs two solid corrections per outer
    # iteration; both must remain below this pilot-derived floor.
    floors["batterySolid.h"] = 5e-4
    final_residuals = {} if final_record is None else _residual_maxima(final_record)
    residual_checks = {
        name: name in final_residuals and final_residuals[name] <= limit
        for name, limit in floors.items()
    }
    strict_residual_checks = {
        name: value <= STRICT_RESIDUAL_TARGET
        for name, value in final_residuals.items()
    }
    acceptance_fields = (*FLOW_ACCEPTANCE_FIELDS, "coolant.h", "batterySolid.h")
    window_residuals = _window_residual_maxima(window_records)
    acceptance_residual_checks = {
        name: name in window_residuals
        and window_residuals[name] <= ACCEPTANCE_RESIDUAL_TARGET
        for name in acceptance_fields
    }

    monitor_rows = []
    for time, values in window_records:
        flows = values["mass_flow_kg_s"]
        pressures = values["pressure_pa"]
        temperatures = values["mass_weighted_temperature_k"]
        batteries = values["battery_volume_temperature_k"]
        complete_batteries = len(batteries) == 23 and all(
            {"average", "maximum"}.issubset(item) for item in batteries.values()
        )
        if not (
            {"inlet", "outlet"}.issubset(flows)
            and {"inlet", "outlet"}.issubset(pressures)
            and {"inlet", "outlet"}.issubset(temperatures)
            and complete_batteries
        ):
            continue
        denominator = 0.5 * (abs(flows["inlet"]) + abs(flows["outlet"]))
        mass_imbalance = (
            abs(flows["inlet"] + flows["outlet"]) / denominator
            if denominator > 0
            else math.inf
        )
        # Use the common through-flow and the temperature difference.  This
        # avoids contaminating a ~7 W balance with the arbitrary enthalpy
        # datum multiplied by the tiny residual inlet/outlet mass mismatch.
        through_flow = 0.5 * (abs(flows["inlet"]) + abs(flows["outlet"]))
        enthalpy_out = (
            coolant_cp_j_kgk
            * through_flow
            * (temperatures["outlet"] - temperatures["inlet"])
        )
        monitor_rows.append(
            {
                "time": time,
                "mass_imbalance_fraction": mass_imbalance,
                "pressure_drop_pa": pressures["inlet"] - pressures["outlet"],
                "inlet_mass_weighted_temperature_k": temperatures["inlet"],
                "outlet_mass_weighted_temperature_k": temperatures["outlet"],
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
            }
        )

    pressure = [row["pressure_drop_pa"] for row in monitor_rows]
    mean_pressure = sum(pressure) / len(pressure) if pressure else None
    pressure_span = (
        (max(pressure) - min(pressure)) / abs(mean_pressure)
        if pressure and mean_pressure
        else None
    )
    mass_max = (
        max(row["mass_imbalance_fraction"] for row in monitor_rows)
        if monitor_rows
        else None
    )
    energy = [row["energy_imbalance_fraction"] for row in monitor_rows]
    tavg = [row["battery_tavg_k"] for row in monitor_rows]
    tmax = [row["battery_tmax_k"] for row in monitor_rows]
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
    # 온도 span, 질량/에너지 불균형과 솔버 종료를 모두 만족해야 PASS다.
    checks = {
        "solver_clean_end": parsed["clean_end"] and not parsed["fatal_error"],
        "cold_flow_compound_gate_passed": cold_report.get("status")
        == "COLD_FLOW_VALIDATION_PASSED",
        "final_iteration_present": final_record is not None,
        "minimum_5000_outer_iterations_completed": final_time is not None
        and final_time >= MINIMUM_STEADY_ITERATIONS,
        "last_200_flow_and_energy_residuals_below_1e-3": all(
            acceptance_residual_checks.values()
        ),
        "pilot_derived_residual_floors_reached": all(residual_checks.values()),
        "monitor_window_has_at_least_21_samples": len(monitor_rows) >= 21,
        "battery_tmax_change_below_0p001_k": bool(tmax)
        and max(tmax) - min(tmax) < 0.001,
        "battery_tavg_change_below_0p001_k": bool(tavg)
        and max(tavg) - min(tavg) < 0.001,
        "pressure_drop_span_below_0p5_percent": pressure_span is not None
        and pressure_span < 0.005,
        "mass_imbalance_below_0p1_percent": mass_max is not None
        and mass_max < 0.001,
        "mean_energy_imbalance_below_2_percent": bool(energy)
        and sum(energy) / len(energy) < 0.02,
        "maximum_energy_imbalance_below_5_percent": bool(energy)
        and max(energy) < 0.05,
        "battery_temperature_rises_above_initial": bool(tmax)
        and tmax[-1] > initial_temperature_k,
        "final_fields_finite": finite_fields,
        "final_turbulence_fields_nonnegative": nonnegative_turbulence,
    }
    passed = all(checks.values())
    return {
        "schema_version": 1,
        "status": (
            "STEADY_CHT_VALIDATION_PASSED"
            if passed
            else "STEADY_CHT_VALIDATION_FAILED"
        ),
        "checks": checks,
        "runnable_frozen_flow_thermal": passed,
        "publication_results_authorized": False,
        "final_iteration": final_time,
        "monitor_window_outer_iterations": monitor_window,
        "source": {
            "qvol_w_m3": qvol_w_m3,
            "mesh_integrated_battery_volume_m3": battery_volume,
            "mesh_integrated_heat_power_w": heat_power,
            "coolant_cp_j_kgk": coolant_cp_j_kgk,
        },
        "residual_floors": floors,
        "final_initial_residual_maxima": final_residuals,
        "residual_checks": residual_checks,
        "acceptance_residual_audit": {
            "target": ACCEPTANCE_RESIDUAL_TARGET,
            "required_fields": list(acceptance_fields),
            "window_outer_iterations": monitor_window,
            "window_maxima": window_residuals,
            "checks": acceptance_residual_checks,
        },
        "strict_residual_audit": {
            "target": STRICT_RESIDUAL_TARGET,
            "all_reported_equations_at_or_below_target": bool(strict_residual_checks)
            and all(strict_residual_checks.values()),
            "checks": strict_residual_checks,
            "policy": "record exactly; compound physics gates remain separately visible",
        },
        "monitor_summary": {
            "sample_count": len(monitor_rows),
            "maximum_mass_imbalance_fraction": mass_max,
            "mean_pressure_drop_pa": mean_pressure,
            "pressure_drop_span_fraction": pressure_span,
            "battery_tavg_span_k": max(tavg) - min(tavg) if tavg else None,
            "battery_tmax_span_k": max(tmax) - min(tmax) if tmax else None,
            "final_battery_tavg_k": tavg[-1] if tavg else None,
            "final_battery_tmax_k": tmax[-1] if tmax else None,
            "mean_energy_imbalance_fraction": (
                sum(energy) / len(energy) if energy else None
            ),
            "maximum_energy_imbalance_fraction": max(energy) if energy else None,
        },
        "monitor_rows": monitor_rows,
        "final_field_bounds": latest_bounds,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate steady OpenFOAM CHT")
    parser.add_argument("--log", required=True)
    parser.add_argument("--cold-flow-report", required=True)
    parser.add_argument("--mesh-integral-report", required=True)
    parser.add_argument("--qvol", required=True, type=float)
    parser.add_argument("--coolant-cp", required=True, type=float)
    parser.add_argument("--initial-temperature", required=True, type=float)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        report = validate_steady_cht(
            args.log,
            cold_flow_report_path=args.cold_flow_report,
            mesh_integral_report_path=args.mesh_integral_report,
            qvol_w_m3=args.qvol,
            coolant_cp_j_kgk=args.coolant_cp,
            initial_temperature_k=args.initial_temperature,
        )
    except (OSError, UnicodeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        report = {
            "schema_version": 1,
            "status": "STEADY_CHT_VALIDATION_FAILED",
            "error": str(exc),
            "runnable_frozen_flow_thermal": False,
            "publication_results_authorized": False,
        }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "STEADY_CHT_VALIDATION_PASSED" else 2


if __name__ == "__main__":
    sys.exit(main())
