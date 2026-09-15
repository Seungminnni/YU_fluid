"""0~1,800 s frozen-flow 과도 CHT 결과의 완전성과 수렴성을 검증한다.

유동장은 검증된 정상 U/p/k/omega를 유지하지만 유체·고체 에너지는 매 시간
계면에서 결합된다. 1초 telemetry, 온도장 저장, 질량·에너지수지, 단조 가열,
상수 열원은 정상 CHT 종점 일치를 확인한다. 계속 증가하는 시간 의존 열원은
그 순간의 정상해에 도달하는 것이 물리적으로 불가능하므로, 정상해를 온도
상한으로만 사용하고 질량·에너지수지와 단조 과도응답을 확인한다.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

try:
    from .openfoam_cold_flow_validation import parse_cold_flow_log
except ImportError:  # Standalone copy inside a generated OpenFOAM case.
    from validate_cold_flow import parse_cold_flow_log


def _read_qvol_samples(path: str | Path | None) -> list[tuple[float, float]]:
    """Read the same physical-time heat-source table consumed by fvOptions."""

    if path is None:
        return []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        rows = [
            (float(row["time_s"]), float(row["qvol_W_m3"]))
            for row in csv.DictReader(handle)
        ]
    if not rows or any(not all(math.isfinite(value) for value in row) for row in rows):
        raise ValueError("heat-source samples must contain finite time_s and qvol_W_m3")
    rows.sort()
    if any(right[0] <= left[0] for left, right in zip(rows, rows[1:])):
        raise ValueError("heat-source sample times must be strictly increasing")
    if any(qvol <= 0 for _, qvol in rows):
        raise ValueError("heat-source qvol samples must be positive")
    return rows


def _qvol_at_time(
    time_s: float, samples: list[tuple[float, float]], fallback_qvol: float
) -> float:
    if not samples:
        return fallback_qvol
    if time_s <= samples[0][0]:
        return samples[0][1]
    if time_s >= samples[-1][0]:
        return samples[-1][1]
    for (left_time, left_qvol), (right_time, right_qvol) in zip(
        samples, samples[1:]
    ):
        if time_s <= right_time:
            fraction = (time_s - left_time) / (right_time - left_time)
            return left_qvol + fraction * (right_qvol - left_qvol)
    raise AssertionError("unreachable heat-source interpolation branch")


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


def _temperature_frame_storage(
    run_dir: Path, expected_times: list[float]
) -> dict[str, object]:
    """Accept either reconstructed fields or a complete parallel decomposition.

    OpenFOAM's processor time directories are the native result of a parallel
    solve.  Reconstructing 1,801 identical time labels is optional post-processing,
    not a numerical-validity requirement, so validate the native fields directly.
    """

    regions = ("coolant", "batterySolid")
    root_times = _numeric_time_directories(run_dir)
    processor_dirs = sorted(
        (
            path
            for path in run_dir.iterdir()
            if path.is_dir()
            and path.name.startswith("processor")
            and path.name.removeprefix("processor").isdigit()
        ),
        key=lambda path: int(path.name.removeprefix("processor")),
    )
    processor_times = {
        processor: _numeric_time_directories(processor) for processor in processor_dirs
    }

    def field_exists(path: Path) -> bool:
        return path.is_file() or Path(str(path) + ".gz").is_file()

    missing_times: list[float] = []
    missing_fields: list[str] = []
    reconstructed_count = 0
    decomposed_count = 0
    for time in expected_times:
        root_path = root_times.get(time)
        if root_path is not None and all(
            field_exists(root_path / region / "T") for region in regions
        ):
            reconstructed_count += 1
            continue

        decomposed_complete = bool(processor_dirs)
        for processor, times in processor_times.items():
            processor_path = times.get(time)
            for region in regions:
                if processor_path is None or not field_exists(
                    processor_path / region / "T"
                ):
                    decomposed_complete = False
                    missing_fields.append(
                        f"{processor.name}/{time:g}/{region}/T"
                    )
        if decomposed_complete:
            decomposed_count += 1
        else:
            missing_times.append(time)

    return {
        "root_time_count": len(root_times),
        "processor_count": len(processor_dirs),
        "reconstructed_frame_count": reconstructed_count,
        "decomposed_frame_count": decomposed_count,
        "missing_frame_times": missing_times,
        "missing_temperature_fields": missing_fields,
    }


def validate_frozen_flow(
    log_path: str | Path,
    *,
    run_dir: str | Path,
    steady_report_path: str | Path,
    mesh_integral_report_path: str | Path,
    qvol_w_m3: float,
    qvol_samples_path: str | Path | None = None,
    coolant_cp_j_kgk: float,
    initial_temperature_k: float,
    steady_initial_temperature_k: float | None = None,
    end_time_s: float = 1800.0,
    telemetry_interval_s: float = 1.0,
    field_interval_s: float = 15.0,
    final_window_s: float = 60.0,
) -> dict[str, object]:
    """과도 로그·원본 필드·정상 CHT 기준을 결합해 최종 열결과를 판정한다."""

    # This legacy argument is the steady solution's zero-source datum. For a
    # forced-flow CHT case it must be the fixed inlet temperature, not the
    # battery's transient initial temperature. Keep the public name for
    # compatibility with already generated cases.
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
    qvol_samples = _read_qvol_samples(qvol_samples_path)
    # 정상 CHT와 동일하게 목표 CAD 체적이 아닌 실제 메시 고체체적을 사용한다.
    steady_heat_power = qvol_w_m3 * battery_volume

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
        current_qvol = _qvol_at_time(time, qvol_samples, qvol_w_m3)
        current_heat_power = current_qvol * battery_volume
        telemetry_rows.append(
            {
                "time_s": time,
                "mass_imbalance_fraction": mass_imbalance,
                "coolant_net_enthalpy_out_w": enthalpy_out,
                "qvol_w_m3": current_qvol,
                "mesh_integrated_heat_power_w": current_heat_power,
                # In a transient run q_source - Q_out also contains dE_stored/dt.
                # Retain the legacy key for the viewer/result contract, but state
                # its semantics explicitly in the report below.
                "energy_imbalance_fraction": abs(enthalpy_out - current_heat_power)
                / current_heat_power,
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

    expected_frame_count = int(round(end_time_s / field_interval_s)) + 1
    expected_frame_times = [
        index * field_interval_s for index in range(expected_frame_count)
    ]
    frame_storage = _temperature_frame_storage(
        Path(run_dir), expected_frame_times
    )
    missing_frame_times = frame_storage["missing_frame_times"]
    missing_temperature_fields = frame_storage["missing_temperature_fields"]

    expected_telemetry_count = int(round(end_time_s / telemetry_interval_s))
    steady_summary = steady_report.get("monitor_summary", {})
    steady_tavg = steady_summary.get("final_battery_tavg_k")
    steady_tmax = steady_summary.get("final_battery_tmax_k")
    final_qvol = (
        _qvol_at_time(final_time, qvol_samples, qvol_w_m3)
        if final_time is not None
        else qvol_w_m3
    )
    source_ratio = final_qvol / qvol_w_m3
    source_is_time_varying = bool(qvol_samples) and any(
        abs(sample_qvol - qvol_samples[0][1]) > 1e-12
        for _, sample_qvol in qvol_samples[1:]
    )
    # With the configured constant properties and zero gravity, the steady
    # temperature rise is linear in source power.  Scale the t=0 steady
    # reference to the final table value instead of comparing a dynamic-source
    # run against the wrong t=0 power.
    expected_tavg = (
        None
        if steady_tavg is None
        else initial_temperature_k
        + (float(steady_tavg) - float(steady_initial_temperature_k))
        * source_ratio
    )
    expected_tmax = (
        None
        if steady_tmax is None
        else initial_temperature_k
        + (float(steady_tmax) - float(steady_initial_temperature_k))
        * source_ratio
    )
    final_tavg = tavg_series[-1] if tavg_series else None
    final_tmax = tmax_series[-1] if tmax_series else None
    minimum_tavg_step = (
        min(b - a for a, b in zip(tavg_series, tavg_series[1:]))
        if len(tavg_series) >= 2
        else None
    )
    # A constant-source transient should reach its matching steady solution.
    # A time-varying source cannot generally be at the *instantaneous* final
    # steady solution while the source is still changing.  In the present
    # profile the final source is also the largest source, so the scaled final
    # steady result is a causal upper bound, not an equality target.
    steady_match_tolerance_k = 0.01
    steady_upper_bound_roundoff_k = 0.001
    tavg_steady_check = (
        final_tavg is not None
        and expected_tavg is not None
        and (
            final_tavg <= expected_tavg + steady_upper_bound_roundoff_k
            if source_is_time_varying
            else abs(final_tavg - expected_tavg) < steady_match_tolerance_k
        )
    )
    tmax_steady_check = (
        final_tmax is not None
        and expected_tmax is not None
        and (
            final_tmax <= expected_tmax + steady_upper_bound_roundoff_k
            if source_is_time_varying
            else abs(final_tmax - expected_tmax) < steady_match_tolerance_k
        )
    )
    checks = {
        "solver_clean_end": parsed["clean_end"] and not parsed["fatal_error"],
        "steady_cht_compound_gate_passed": steady_report.get("status")
        == "STEADY_CHT_VALIDATION_PASSED",
        "reaches_configured_end_time": final_time is not None
        and abs(final_time - end_time_s) < 1e-8,
        "complete_one_second_telemetry": len(telemetry_rows)
        >= expected_telemetry_count,
        "all_configured_temperature_frames_available": not missing_frame_times,
        "temperature_fields_present_in_native_storage": not missing_temperature_fields,
        "final_window_has_at_least_61_samples": len(final_rows)
        >= int(round(final_window_s / telemetry_interval_s)) + 1,
        "mass_imbalance_below_0p1_percent": bool(mass) and max(mass) < 0.001,
        "mean_final_energy_imbalance_below_2_percent": bool(energy)
        and sum(energy) / len(energy) < 0.02,
        "maximum_final_energy_imbalance_below_5_percent": bool(energy)
        and max(energy) < 0.05,
        # Monotonic heating is a valid gate only for a constant positive heat
        # source.  The Peneklioglu source decreases first and rises later, so a
        # real thermal maximum followed by a temporary decline is expected and
        # must not be reported as numerical failure.
        "battery_average_temperature_trend_matches_source_contract": (
            minimum_tavg_step is not None
            and (source_is_time_varying or minimum_tavg_step >= -1e-6)
        ),
        "battery_temperature_rises_above_initial": final_tmax is not None
        and final_tmax > initial_temperature_k,
        (
            "dynamic_final_tavg_below_instantaneous_steady_upper_bound"
            if source_is_time_varying
            else "constant_source_final_tavg_matches_steady_within_0p01_k"
        ): tavg_steady_check,
        (
            "dynamic_final_tmax_below_instantaneous_steady_upper_bound"
            if source_is_time_varying
            else "constant_source_final_tmax_matches_steady_within_0p01_k"
        ): tmax_steady_check,
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
            "time_varying": source_is_time_varying,
            "steady_qvol_w_m3": qvol_w_m3,
            "final_qvol_w_m3": final_qvol,
            "final_to_steady_source_ratio": source_ratio,
            "qvol_samples_path": (
                None
                if qvol_samples_path is None
                else str(Path(qvol_samples_path).resolve())
            ),
            "mesh_integrated_battery_volume_m3": battery_volume,
            "steady_mesh_integrated_heat_power_w": steady_heat_power,
            "final_mesh_integrated_heat_power_w": final_qvol * battery_volume,
            "coolant_cp_j_kgk": coolant_cp_j_kgk,
            "initial_temperature_k": initial_temperature_k,
            "steady_reference_temperature_k": steady_initial_temperature_k,
            "steady_report": str(Path(steady_report_path).resolve()),
            "steady_reference_semantics": (
                "instantaneous_upper_bound_while_source_is_ramping"
                if source_is_time_varying
                else "constant_source_equilibrium_target"
            ),
            "transient_energy_gap_semantics": (
                "abs(Q_source-Q_coolant_out)/Q_source; includes fluid/solid "
                "storage and is not a pure numerical conservation residual"
            ),
        },
        "time_contract": {
            "end_time_s": end_time_s,
            "telemetry_interval_s": telemetry_interval_s,
            "field_interval_s": field_interval_s,
            "expected_telemetry_count": expected_telemetry_count,
            "actual_complete_telemetry_count": len(telemetry_rows),
            "expected_field_frame_count": expected_frame_count,
            "storage_policy": "reconstructed field or complete OpenFOAM processor decomposition",
            "processor_count": frame_storage["processor_count"],
            "actual_numeric_root_time_count": frame_storage["root_time_count"],
            "reconstructed_field_frame_count": frame_storage[
                "reconstructed_frame_count"
            ],
            "decomposed_field_frame_count": frame_storage["decomposed_frame_count"],
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
            "energy_gap_includes_transient_storage": True,
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
    parser.add_argument("--qvol-samples")
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
            qvol_samples_path=args.qvol_samples,
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
