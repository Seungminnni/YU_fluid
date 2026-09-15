"""Build a transient CHT energy balance from persisted OpenFOAM function objects."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


def _read_dat(path: Path) -> dict[int, float]:
    values: dict[int, float] = {}
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            columns = text.replace("(", " ").replace(")", " ").split()
            if len(columns) < 2:
                continue
            time_value = float(columns[0])
            time_s = int(round(time_value))
            if abs(time_value - time_s) > 1e-7:
                raise ValueError(f"{path}: expected integer-second sample, got {time_value}")
            values[time_s] = float(columns[1])
    if not values:
        raise ValueError(f"{path}: no numeric samples")
    return values


def _read_qvol(path: Path) -> dict[int, float]:
    values: dict[int, float] = {}
    with path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            time_value = float(row["time_s"])
            time_s = int(round(time_value))
            if abs(time_value - time_s) > 1e-7:
                raise ValueError(f"{path}: expected integer-second heat sample")
            values[time_s] = float(row["qvol_W_m3"])
    if sorted(values) != list(range(0, max(values) + 1)):
        raise ValueError(f"{path}: heat samples must cover every integer second from zero")
    return values


def _function_file(run: Path, region: str, name: str, filename: str) -> Path:
    path = run / "postProcessing" / region / name / "0" / filename
    if not path.is_file():
        raise ValueError(f"missing OpenFOAM function-object output: {path}")
    return path


def build_energy_balance(
    run_dir: str | Path,
    mesh_integral_report: str | Path,
    qvol_samples: str | Path,
    output_csv: str | Path,
    output_report: str | Path,
    *,
    fluid_density_kg_m3: float,
    fluid_cp_j_kgk: float,
    solid_density_kg_m3: float,
    solid_cp_j_kgk: float,
    initial_temperature_k: float,
    maximum_final_window_imbalance_fraction: float = 0.02,
) -> dict[str, object]:
    """Resolve Qsource = Qout + dE/dt + residual at every integer second."""

    if (
        not math.isfinite(maximum_final_window_imbalance_fraction)
        or maximum_final_window_imbalance_fraction < 0.0
    ):
        raise ValueError("maximum final-window imbalance must be finite and nonnegative")

    run = Path(run_dir).resolve()
    mesh = json.loads(Path(mesh_integral_report).read_text(encoding="utf-8"))
    actual = mesh["actual"]
    fluid_volume_m3 = float(actual["zone_volumes_m3"]["coolant"])
    solid_volume_m3 = float(actual["battery_total_volume_m3"])
    heat = _read_qvol(Path(qvol_samples))
    end_time_s = max(heat)

    fluid_integral = _read_dat(
        _function_file(
            run,
            "coolant",
            "coolant_temperature_volume_integral",
            "volFieldValue.dat",
        )
    )
    solid_integral = _read_dat(
        _function_file(
            run,
            "batterySolid",
            "batterySolid_temperature_volume_integral",
            "volFieldValue.dat",
        )
    )
    inlet_mass = _read_dat(
        _function_file(run, "coolant", "coolant_inlet_mass_flow", "surfaceFieldValue.dat")
    )
    outlet_mass = _read_dat(
        _function_file(run, "coolant", "coolant_outlet_mass_flow", "surfaceFieldValue.dat")
    )
    inlet_temperature = _read_dat(
        _function_file(
            run,
            "coolant",
            "coolant_inlet_mass_weighted_temperature",
            "surfaceFieldValue.dat",
        )
    )
    outlet_temperature = _read_dat(
        _function_file(
            run,
            "coolant",
            "coolant_outlet_mass_weighted_temperature",
            "surfaceFieldValue.dat",
        )
    )

    expected = set(range(1, end_time_s + 1))
    datasets = {
        "fluid_temperature_integral": fluid_integral,
        "solid_temperature_integral": solid_integral,
        "inlet_mass_flow": inlet_mass,
        "outlet_mass_flow": outlet_mass,
        "inlet_temperature": inlet_temperature,
        "outlet_temperature": outlet_temperature,
    }
    for name, values in datasets.items():
        missing = sorted(expected - set(values))
        if missing:
            raise ValueError(f"{name}: missing integer-second rows, first={missing[0]}")

    previous_energy_j = initial_temperature_k * (
        fluid_density_kg_m3 * fluid_cp_j_kgk * fluid_volume_m3
        + solid_density_kg_m3 * solid_cp_j_kgk * solid_volume_m3
    )
    rows: list[dict[str, float]] = []
    for time_s in range(1, end_time_s + 1):
        energy_j = (
            fluid_density_kg_m3 * fluid_cp_j_kgk * fluid_integral[time_s]
            + solid_density_kg_m3 * solid_cp_j_kgk * solid_integral[time_s]
        )
        stored_w = energy_j - previous_energy_j
        previous_energy_j = energy_j
        through_flow = 0.5 * (
            abs(inlet_mass[time_s]) + abs(outlet_mass[time_s])
        )
        removed_w = (
            fluid_cp_j_kgk
            * through_flow
            * (outlet_temperature[time_s] - inlet_temperature[time_s])
        )
        generated_w = heat[time_s] * solid_volume_m3
        residual_w = generated_w - removed_w - stored_w
        values = (generated_w, removed_w, stored_w, residual_w)
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"non-finite energy term at {time_s} s")
        rows.append(
            {
                "time_s": float(time_s),
                "generated_W": generated_w,
                "removed_W": removed_w,
                "stored_W": stored_w,
                "imbalance_fraction": abs(residual_w) / generated_w,
            }
        )

    csv_path = Path(output_csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "time_s",
                "generated_W",
                "removed_W",
                "stored_W",
                "imbalance_fraction",
            ),
        )
        writer.writeheader()
        writer.writerows(rows)

    final_window = rows[-60:]
    final_window_mean = sum(
        row["imbalance_fraction"] for row in final_window
    ) / len(final_window)
    final_window_maximum = max(
        row["imbalance_fraction"] for row in final_window
    )
    balance_gate_passed = (
        final_window_mean <= maximum_final_window_imbalance_fraction
    )
    report = {
        "schema_version": 1,
        "status": (
            "TRANSIENT_ENERGY_BALANCE_PASSED"
            if balance_gate_passed
            else "TRANSIENT_ENERGY_BALANCE_WARNING"
        ),
        "equation": "Q_generated = Q_removed + dE_fluid/dt + dE_solid/dt + residual",
        "storage_discretization": "backward difference over each 1 s interval",
        "source_volume_semantics": "actual mesh-integrated batterySolid volume",
        "sample_count": len(rows),
        "first_time_s": rows[0]["time_s"],
        "last_time_s": rows[-1]["time_s"],
        "fluid_volume_m3": fluid_volume_m3,
        "solid_volume_m3": solid_volume_m3,
        "final": rows[-1],
        "final_window_sample_count": len(final_window),
        "final_60s_mean_imbalance_fraction": final_window_mean,
        "final_60s_maximum_imbalance_fraction": final_window_maximum,
        "energy_balance_gate": {
            "metric": "final_60s_mean_imbalance_fraction",
            "maximum_fraction": maximum_final_window_imbalance_fraction,
            "passed": balance_gate_passed,
        },
        "output_csv": str(csv_path.resolve()),
    }
    report_path = Path(output_report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build OpenFOAM transient energy balance")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--mesh-integral-report", required=True)
    parser.add_argument("--qvol-samples", required=True)
    parser.add_argument("--fluid-density", required=True, type=float)
    parser.add_argument("--fluid-cp", required=True, type=float)
    parser.add_argument("--solid-density", required=True, type=float)
    parser.add_argument("--solid-cp", required=True, type=float)
    parser.add_argument("--initial-temperature", required=True, type=float)
    parser.add_argument(
        "--maximum-final-window-imbalance",
        type=float,
        default=0.02,
        help="maximum accepted final-window mean |residual|/Qgenerated fraction",
    )
    parser.add_argument("--output-csv", required=True)
    parser.add_argument("--output-report", required=True)
    args = parser.parse_args(argv)
    report = build_energy_balance(
        args.run_dir,
        args.mesh_integral_report,
        args.qvol_samples,
        args.output_csv,
        args.output_report,
        fluid_density_kg_m3=args.fluid_density,
        fluid_cp_j_kgk=args.fluid_cp,
        solid_density_kg_m3=args.solid_density,
        solid_cp_j_kgk=args.solid_cp,
        initial_temperature_k=args.initial_temperature,
        maximum_final_window_imbalance_fraction=args.maximum_final_window_imbalance,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
