from __future__ import annotations

import json
import math
import os
from pathlib import Path

from .errors import MdpiCfdError
from .postprocess import _ScientificBackend, read_openfoam_dat
from .result_contract import update_figure_status


LEVELS = ("L0", "L1", "L2", "L3")


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _final_cell_averages(level_dir: Path) -> dict[str, float]:
    root = level_dir / "runs/steady_cht/postProcessing/batterySolid"
    values: dict[str, float] = {}
    for index in range(1, 24):
        cell_id = f"C{index:02d}"
        path = root / f"battery_{cell_id}_Tavg/0/volFieldValue.dat"
        if not path.is_file():
            return {}
        values[cell_id] = read_openfoam_dat(path)[-1][1]
    return values


def _level_metrics(level_dir: Path) -> dict[str, object]:
    mesh = _read_json(level_dir / "reports/production_mesh/production_mesh_gate.json")
    integrals = _read_json(level_dir / "reports/production_mesh/mesh_integrals_pre_split.json")
    cold = _read_json(level_dir / "runs/steady_cold_flow/reports/solver/cold_flow_validation.json")
    yplus = _read_json(level_dir / "runs/steady_cold_flow/reports/solver/yplus.json")
    steady = _read_json(level_dir / "runs/steady_cht/reports/solver/steady_cht_validation.json")
    available = all(item is not None for item in (mesh, integrals, cold, yplus, steady))
    if not available:
        return {
            "status": "INCOMPLETE",
            "mesh_gate": None if mesh is None else mesh.get("status"),
            "cold_flow_gate": None if cold is None else cold.get("status"),
            "steady_cht_gate": None if steady is None else steady.get("status"),
            "yplus_gate": None if yplus is None else yplus.get("status"),
        }
    steady_summary = steady["monitor_summary"]
    cold_summary = cold["monitor_summary"]
    bounds = steady["final_field_bounds"]
    cells = _final_cell_averages(level_dir)
    passed = (
        mesh.get("status") == "PRODUCTION_MESH_GATE_PASSED"
        and cold.get("status") == "COLD_FLOW_VALIDATION_PASSED"
        and steady.get("status") == "STEADY_CHT_VALIDATION_PASSED"
        and yplus.get("status") == "YPLUS_VALIDATION_PASSED"
        and len(cells) == 23
    )
    return {
        "status": "LEVEL_PASSED" if passed else "LEVEL_FAILED",
        "mesh_gate": mesh.get("status"),
        "cold_flow_gate": cold.get("status"),
        "steady_cht_gate": steady.get("status"),
        "yplus_gate": yplus.get("status"),
        "cell_count": int(integrals["actual"]["cell_count"]),
        "final_iteration": int(steady["final_iteration"]),
        "effective_h_relative": float(integrals["actual"]["cell_count"]) ** (-1.0 / 3.0),
        "battery_tavg_k": float(steady_summary["final_battery_tavg_k"]),
        "battery_tmax_k": float(steady_summary["final_battery_tmax_k"]),
        "cell_average_temperature_k": cells,
        "coolant_tout_k": float(steady["monitor_rows"][-1]["outlet_mass_weighted_temperature_k"]),
        "inlet_temperature_k": float(
            steady["monitor_rows"][-1]["inlet_mass_weighted_temperature_k"]
        ),
        "pressure_drop_pa": float(steady_summary["mean_pressure_drop_pa"]),
        "velocity_max_m_s": float(bounds["U"]["max"]),
        "mass_imbalance_fraction": float(steady_summary["maximum_mass_imbalance_fraction"]),
        "energy_imbalance_fraction": float(steady_summary["mean_energy_imbalance_fraction"]),
    }


def _render_mesh_independence_figure(
    case: Path, report: dict[str, object]
) -> Path:
    """Render the paper-style L0/L3 shared-scale fields and four-grid trends."""
    from matplotlib.tri import Triangulation

    levels = report["levels"]
    temperature_cuts: dict[str, list[tuple[object, object, object]]] = {}
    all_values = []
    for level in ("L0", "L3"):
        run = case / "production" / level / "runs/steady_cht"
        marker = run / "steady_cht.foam"
        marker.touch(exist_ok=True)
        backend = _ScientificBackend(marker)
        fluid, solid = backend.regions(float(levels[level]["final_iteration"]))
        y_bounds = fluid.GetBounds()[2:4]
        y_mid = 0.5 * (float(y_bounds[0]) + float(y_bounds[1]))
        cuts = [
            backend._triangulated_cut(fluid, (0, y_mid, 0), (0, 1, 0), "T"),
            backend._triangulated_cut(solid, (0, y_mid, 0), (0, 1, 0), "T"),
        ]
        inlet_temperature_k = float(levels[level]["inlet_temperature_k"])
        for cut in cuts:
            cut[2][:] -= inlet_temperature_k
            all_values.append(cut[2])
        temperature_cuts[level] = cuts

    np = backend.np
    plt = backend.plt
    combined = np.concatenate(all_values)
    tmin, tmax = float(combined.min()), float(combined.max())
    figure = plt.figure(figsize=(13.2, 9.2), constrained_layout=True)
    grid = figure.add_gridspec(2, 2, height_ratios=(1.3, 0.8))
    field_axes = [figure.add_subplot(grid[0, 0]), figure.add_subplot(grid[0, 1])]
    colour_artist = None
    for axis, level in zip(field_axes, ("L0", "L3")):
        for points, cells, values in temperature_cuts[level]:
            triangulation = Triangulation(points[:, 0] * 1000, points[:, 2] * 1000, cells)
            colour_artist = axis.tripcolor(
                triangulation,
                values,
                shading="gouraud",
                cmap="jet",
                vmin=tmin,
                vmax=tmax,
            )
        axis.set_aspect("equal")
        axis.invert_xaxis()
        axis.set_xlabel("X [mm]")
        axis.set_ylabel("Z [mm]")
        axis.set_title(
            f"{level} · {int(levels[level]['cell_count']):,} cells · steady CHT"
        )
    figure.colorbar(
        colour_artist,
        ax=field_axes,
        shrink=0.85,
        label="Coupled fluid/solid temperature rise, ΔT = T − Tin [K]",
    )

    ordered = list(LEVELS)
    cell_counts_m = [float(levels[level]["cell_count"]) / 1_000_000 for level in ordered]
    temperature_axis = figure.add_subplot(grid[1, 0])
    for key, label, marker_style in (
        ("battery_tavg_k", "Battery Tavg", "o-"),
        ("battery_tmax_k", "Battery Tmax", "s-"),
        ("coolant_tout_k", "Coolant Tout", "^-"),
    ):
        temperature_axis.plot(
            cell_counts_m,
            [
                float(levels[level][key])
                - float(levels[level]["inlet_temperature_k"])
                for level in ordered
            ],
            marker_style,
            linewidth=1.5,
            markersize=5,
            label=label,
        )
    crosscheck = report.get("l3_20c_transient_crosscheck")
    if crosscheck and crosscheck.get("status") == "PASSED":
        l3_x = cell_counts_m[-1]
        temperature_axis.scatter(
            [l3_x, l3_x],
            [
                float(crosscheck["battery_tavg_rise_k"]),
                float(crosscheck["battery_tmax_rise_k"]),
            ],
            marker="*",
            s=90,
            facecolors="none",
            edgecolors="#111111",
            linewidths=1.2,
            zorder=5,
            label="20°C L3 rerun @ 1800 s",
        )
    temperature_axis.set_xlabel("Mesh cells [million]")
    temperature_axis.set_ylabel("Temperature rise above inlet [K]")
    temperature_axis.grid(alpha=0.25)
    temperature_axis.legend(fontsize=8)
    temperature_axis.set_title("Four-level temperature-rise convergence")

    pressure_axis = figure.add_subplot(grid[1, 1])
    pressure_axis.plot(
        cell_counts_m,
        [float(levels[level]["pressure_drop_pa"]) for level in ordered],
        "o-",
        color="#3c5488",
        linewidth=1.8,
    )
    for x_value, level in zip(cell_counts_m, ordered):
        pressure_axis.annotate(level, (x_value, float(levels[level]["pressure_drop_pa"])),
                               xytext=(0, 6), textcoords="offset points", ha="center")
    pressure_axis.set_xlabel("Mesh cells [million]")
    pressure_axis.set_ylabel("Pressure drop [Pa]")
    pressure_axis.grid(alpha=0.25)
    pressure_axis.set_title("Four-level pressure-drop convergence")
    comparison = report["comparisons"]["L2_to_L3"]
    figure.suptitle(
        "Figure 3 · Mesh independence · L0/L3 shared vertical plane and scale\n"
        f"L2→L3: ΔTmax={comparison['battery_tmax_difference_k']:.5f} K, "
        f"max ΔTcell,avg={comparison['maximum_cell_average_difference_k']:.5f} K, "
        f"Δp={100*comparison['pressure_drop_difference_fraction']:.3f}%\n"
        "Actual validated OpenFOAM fields · ΔT = T − Tin · identical XZ plane, camera and shared scale"
    )
    path = case / "results/artifacts/figure_03_mesh_independence.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, dpi=220, bbox_inches="tight", facecolor="white")
    plt.close(figure)
    return path


def _publish_mesh_independence_result(case: Path, report: dict[str, object]) -> None:
    figure_path = _render_mesh_independence_figure(case, report)
    relative_path = str(figure_path.relative_to(case))
    report["figure_03"] = {
        "status": "MESH_INDEPENDENCE_FIGURE_READY",
        "artifact": relative_path,
        "actual_solver_data": True,
    }
    figure_contract = case / "results/figures/figure_03.json"
    if figure_contract.is_file():
        update_figure_status(
            case / "results",
            3,
            "MESH_INDEPENDENCE_READY",
            artifacts=[relative_path],
            metadata={
                "actual_solver_data": True,
                "levels": list(LEVELS),
                "comparison": report["comparisons"]["L2_to_L3"],
                "field_panels": ["L0 steady CHT", "L3 steady CHT"],
                "field_view": (
                    "identical XZ centre plane, camera and shared actual ΔT scale; "
                    "temperature datum removed by subtracting each run's measured inlet"
                ),
                "temperature_quantity": "actual temperature rise ΔT = T − Tin [K]",
                "l3_20c_transient_crosscheck": report.get(
                    "l3_20c_transient_crosscheck"
                ),
                "publication_results_authorized": False,
            },
        )
    manifest_path = case / "results/result_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["mesh_independence_status"] = "MESH_INDEPENDENCE_PASSED"
        manifest["mesh_independence_figure"] = relative_path
        manifest["publication_results_authorized"] = False
        manifest["publication_blocker"] = (
            "Geometry/input assumptions A-04 remain; transient velocity was not solved "
            "and is represented only by particles in the validated fixed U(x) field"
        )
        _atomic_json(manifest_path, manifest)


def _observed_order(level_metrics: dict[str, dict[str, object]], key: str) -> dict[str, object]:
    available = [level for level in LEVELS if level_metrics[level].get("status") == "LEVEL_PASSED"]
    if len(available) < 3:
        return {"status": "INSUFFICIENT_LEVELS"}
    last = available[-3:]
    h1, h2, h3 = (float(level_metrics[level]["effective_h_relative"]) for level in last)
    q1, q2, q3 = (float(level_metrics[level][key]) for level in last)
    d12, d23 = q1 - q2, q2 - q3
    if d12 == 0 or d23 == 0 or d12 * d23 <= 0:
        return {"status": "NON_MONOTONIC_OR_FLAT", "levels": last}
    target = d12 / d23

    def residual(order: float) -> float:
        return (h1**order - h2**order) / (h2**order - h3**order) - target

    lower, upper = 0.05, 10.0
    low_value, high_value = residual(lower), residual(upper)
    if low_value * high_value > 0:
        return {"status": "ORDER_NOT_BRACKETED", "levels": last}
    for _ in range(100):
        middle = 0.5 * (lower + upper)
        value = residual(middle)
        if low_value * value <= 0:
            upper = middle
            high_value = value
        else:
            lower = middle
            low_value = value
    order = 0.5 * (lower + upper)
    ratio = h2 / h3
    extrapolated = q3 + (q3 - q2) / (ratio**order - 1.0)
    gci = 1.25 * abs((q3 - q2) / q3) / (ratio**order - 1.0)
    return {
        "status": "COMPUTED",
        "levels": last,
        "observed_order": order,
        "richardson_extrapolated": extrapolated,
        "fine_grid_gci_fraction": gci,
    }


def evaluate_mesh_independence(case_dir: str | Path) -> dict[str, object]:
    case = Path(case_dir).resolve()
    metrics = {
        level: _level_metrics(case / "production" / level) for level in LEVELS
    }
    completed = [level for level in LEVELS if metrics[level]["status"] == "LEVEL_PASSED"]
    checks: dict[str, bool] = {}
    comparisons: dict[str, object] = {}
    if all(level in completed for level in ("L2", "L3")):
        l2, l3 = metrics["L2"], metrics["L3"]
        cell_differences = {
            cell_id: abs(
                float(l3["cell_average_temperature_k"][cell_id])
                - float(l2["cell_average_temperature_k"][cell_id])
            )
            for cell_id in l3["cell_average_temperature_k"]
        }
        comparisons["L2_to_L3"] = {
            "battery_tmax_difference_k": abs(float(l3["battery_tmax_k"]) - float(l2["battery_tmax_k"])),
            "maximum_cell_average_difference_k": max(cell_differences.values()),
            "cell_average_differences_k": cell_differences,
            "coolant_tout_difference_k": abs(float(l3["coolant_tout_k"]) - float(l2["coolant_tout_k"])),
            "pressure_drop_difference_fraction": abs(float(l3["pressure_drop_pa"]) - float(l2["pressure_drop_pa"])) / abs(float(l3["pressure_drop_pa"])),
        }
        comparison = comparisons["L2_to_L3"]
        checks = {
            "all_four_levels_passed": len(completed) == 4,
            "l2_l3_tmax_within_0p02_k": comparison["battery_tmax_difference_k"] <= 0.02,
            "l2_l3_all_cell_averages_within_0p02_k": comparison["maximum_cell_average_difference_k"] <= 0.02,
            "l2_l3_tout_within_0p01_k": comparison["coolant_tout_difference_k"] <= 0.01,
            "l2_l3_pressure_drop_within_2_percent": comparison["pressure_drop_difference_fraction"] <= 0.02,
        }
    status = (
        "MESH_INDEPENDENCE_PASSED"
        if checks and all(checks.values())
        else "MESH_INDEPENDENCE_FAILED"
        if checks
        else "MESH_INDEPENDENCE_PENDING"
    )
    rerun_validation = _read_json(
        case
        / "production/L3/runs/frozen_flow_actual_1s_xy5/reports/solver"
        / "frozen_flow_validation.json"
    )
    l3_20c_crosscheck = None
    if (
        rerun_validation
        and rerun_validation.get("status") == "FROZEN_FLOW_VALIDATION_PASSED"
        and metrics["L3"].get("status") == "LEVEL_PASSED"
    ):
        initial_temperature_k = float(
            rerun_validation["source"]["initial_temperature_k"]
        )
        summary = rerun_validation["final_summary"]
        average_rise = float(summary["battery_tavg_k"]) - initial_temperature_k
        maximum_rise = float(summary["battery_tmax_k"]) - initial_temperature_k
        steady_average_rise = float(metrics["L3"]["battery_tavg_k"]) - float(
            metrics["L3"]["inlet_temperature_k"]
        )
        steady_maximum_rise = float(metrics["L3"]["battery_tmax_k"]) - float(
            metrics["L3"]["inlet_temperature_k"]
        )
        l3_20c_crosscheck = {
            "status": "PASSED",
            "source": "L3 frozen-flow actual 1 s run at 1800 s",
            "inlet_temperature_k": initial_temperature_k,
            "battery_tavg_rise_k": average_rise,
            "battery_tmax_rise_k": maximum_rise,
            "tavg_rise_difference_from_steady_k": abs(
                average_rise - steady_average_rise
            ),
            "tmax_rise_difference_from_steady_k": abs(
                maximum_rise - steady_maximum_rise
            ),
        }
    report = {
        "schema_version": 1,
        "status": status,
        "publication_results_authorized": False,
        "completed_levels": completed,
        "required_levels": list(LEVELS),
        "levels": metrics,
        "comparisons": comparisons,
        "l3_20c_transient_crosscheck": l3_20c_crosscheck,
        "checks": checks,
        "convergence_analysis": {
            "battery_tavg": _observed_order(metrics, "battery_tavg_k"),
            "battery_tmax": _observed_order(metrics, "battery_tmax_k"),
            "pressure_drop": _observed_order(metrics, "pressure_drop_pa"),
        },
        "thresholds": {
            "l2_l3_tmax_difference_k": 0.02,
            "l2_l3_maximum_cell_average_difference_k": 0.02,
            "l2_l3_coolant_tout_difference_k": 0.01,
            "l2_l3_pressure_drop_difference_fraction": 0.02,
        },
    }
    if status == "MESH_INDEPENDENCE_PASSED":
        try:
            _publish_mesh_independence_result(case, report)
        except Exception as exc:  # keep the numerical gate distinct from rendering
            report["figure_03"] = {
                "status": "MESH_INDEPENDENCE_FIGURE_FAILED",
                "error": f"{type(exc).__name__}: {exc}",
            }
    _atomic_json(case / "results/mesh_independence.json", report)
    return report
