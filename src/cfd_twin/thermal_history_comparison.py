"""Compare two same-mesh transient CHT histories at every physical second.

This report is diagnostic: a small temperature difference is evidence about
the adaptive algorithm, not a substitute for convergence or energy gates.
"""

from __future__ import annotations

import json
from pathlib import Path

from .openfoam_energy_balance import _read_function_series


def _history(run: Path, solid_volume_m3: float) -> dict[str, dict[int, float]]:
    outlet = _read_function_series(
        run, "coolant", "coolant_outlet_mass_weighted_temperature", "surfaceFieldValue.dat"
    )
    solid_integral = _read_function_series(
        run, "batterySolid", "batterySolid_temperature_volume_integral", "volFieldValue.dat"
    )
    maxima = [
        _read_function_series(run, "batterySolid", f"battery_C{index:02d}_Tmax", "volFieldValue.dat")
        for index in range(1, 24)
    ]
    common = set(outlet) & set(solid_integral)
    for series in maxima:
        common &= set(series)
    return {
        "outlet_temperature_K": {time: outlet[time] for time in common},
        "solid_volume_average_K": {time: solid_integral[time] / solid_volume_m3 for time in common},
        "battery_volume_maximum_K": {time: max(series[time] for series in maxima) for time in common},
    }


def compare_thermal_histories(
    baseline_run: str | Path,
    adaptive_run: str | Path,
    mesh_integral_report: str | Path,
    output_report: str | Path,
    *,
    end_time_s: int = 1800,
) -> dict[str, object]:
    """Require matching 1 s histories and report signed/absolute differences."""

    if end_time_s < 1:
        raise ValueError("end_time_s must be positive")
    mesh = json.loads(Path(mesh_integral_report).read_text(encoding="utf-8"))
    solid_volume = float(mesh["actual"]["battery_total_volume_m3"])
    if solid_volume <= 0:
        raise ValueError("actual solid mesh volume must be positive")
    old = _history(Path(baseline_run), solid_volume)
    new = _history(Path(adaptive_run), solid_volume)
    expected = set(range(1, end_time_s + 1))
    for name in old:
        for label, data in (("baseline", old[name]), ("adaptive", new[name])):
            missing = sorted(expected - set(data))
            if missing:
                raise ValueError(f"{label} {name}: missing {missing[0]} s sample")

    rows: list[dict[str, float]] = []
    summaries: dict[str, dict[str, float | int]] = {}
    for time_s in range(1, end_time_s + 1):
        row: dict[str, float] = {"time_s": float(time_s)}
        for name in old:
            row[f"baseline_{name}"] = old[name][time_s]
            row[f"adaptive_{name}"] = new[name][time_s]
            row[f"delta_{name}"] = new[name][time_s] - old[name][time_s]
        rows.append(row)
    for name in old:
        differences = [(time_s, new[name][time_s] - old[name][time_s]) for time_s in range(1, end_time_s + 1)]
        maximum_time, maximum_delta = max(differences, key=lambda item: abs(item[1]))
        summaries[name] = {
            "max_abs_delta_K": abs(maximum_delta),
            "max_abs_delta_time_s": maximum_time,
            "mean_abs_delta_K": sum(abs(delta) for _, delta in differences) / end_time_s,
            "final_delta_K": differences[-1][1],
        }
    result: dict[str, object] = {
        "status": "THERMAL_HISTORY_COMPARISON_EXTRACTED",
        "baseline_run": str(Path(baseline_run).resolve()),
        "adaptive_run": str(Path(adaptive_run).resolve()),
        "sample_count": end_time_s,
        "comparison_is_validation_gate": False,
        "series": summaries,
        "rows": rows,
    }
    output = Path(output_report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
