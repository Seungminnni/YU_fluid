from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import json
import math
import os
import shutil
from pathlib import Path

from .config import ResolvedConfig
from .geometry import cell_placements, geometry_report
from .openfoam_cold_flow_validation import parse_cold_flow_log
from .openfoam_results import (
    LEGACY_DISPLAY_TEMPERATURE_OFFSET_C,
    REFERENCE_FIGURE_RANGES,
    OpenFoamResultReader,
    read_openfoam_dat,
)
from .production_mesh import SINGLE_MESH_TARGET_COUNTS
from .result_contract import figure_catalog


CURRENT_PRECISION_MESH = {
    "total_cells": 1_316_854,
    "fluid_cells": 1_061_438,
    "solid_cells": 255_416,
    "points": 1_403_119,
    "hexahedra": 1_245_779,
    "hexahedron_fraction": 1_245_779 / 1_316_854,
}


def _mesh_runs_payload(
    case: Path, *, precision_data_ready: bool, standard_data_ready: bool = False
) -> list[dict[str, object]]:
    """Describe selectable datasets without assigning one mesh's fields to another."""

    precision = dict(CURRENT_PRECISION_MESH)
    precision_zone_report = (
        case / "production/L3/reports/production_mesh/cellZones_pre_split.json"
    )
    if precision_zone_report.is_file():
        zone_report = json.loads(precision_zone_report.read_text(encoding="utf-8"))
        zone_counts = zone_report.get("zone_cell_counts", {})
        precision["total_cells"] = int(zone_report["mesh_cell_count"])
        precision["fluid_cells"] = int(zone_counts.get("coolant", 0))
        precision["solid_cells"] = sum(
            int(value)
            for name, value in zone_counts.items()
            if str(name).startswith("battery_C")
        )

    target_report_path = (
        case / "production/single/reports/production_mesh/mesh_target_composition.json"
    )
    target_report: dict[str, object] = {}
    if target_report_path.is_file():
        target_report = json.loads(target_report_path.read_text(encoding="utf-8"))
    target_actual = target_report.get("actual", {})
    mesh_ready = target_report.get("status") == "MESH_TARGET_VALIDATION_PASSED"

    return [
        {
            "id": "precision",
            "label": "정밀 결과 · 1,316,854셀",
            "state_label": "계산 완료",
            "mesh_ready": True,
            "data_ready": precision_data_ready,
            "counts": precision,
            "element_family": "hexa-dominant body-fitted",
            "note": "0–1800 s 직접 결과가 연결된 현재 정밀 메시입니다.",
        },
        {
            "id": "single",
            "label": (
                f"표준 계산 · {int(target_actual['total_cells']):,}셀"
                if mesh_ready and target_actual
                else "표준 계산 · 목표 207,390셀"
            ),
            "state_label": (
                "계산 완료"
                if standard_data_ready
                else ("메시 검증 완료" if mesh_ready else "계산 전")
            ),
            "mesh_ready": mesh_ready,
            "data_ready": standard_data_ready,
            "counts": target_actual or {
                "total_cells": SINGLE_MESH_TARGET_COUNTS["total_cells"],
                "fluid_cells": SINGLE_MESH_TARGET_COUNTS["fluid_cells"],
                "solid_cells": SINGLE_MESH_TARGET_COUNTS["solid_cells"],
                "points": SINGLE_MESH_TARGET_COUNTS["points"],
            },
            "counts_are_targets": not mesh_ready,
            "element_family": (
                f"hexa-dominant body-fitted ({100 * float(target_actual['hexahedron_fraction']):.3f}% hexahedra)"
                if mesh_ready and target_actual
                else "hexa-dominant target (≥80% hexahedra)"
            ),
            "note": (
                "0–1800 s 직접 결과가 연결된 표준 메시입니다."
                if standard_data_ready
                else "새 단일 계산 프로파일입니다. 자체 CFD 필드가 생성되기 전에는 "
                "정밀 결과를 대신 표시하지 않습니다."
            ),
        },
    ]


def _convergence_payload(case: Path) -> dict[str, object]:
    """Extract a compact, truthful residual history for the browser."""

    transient_candidates = (
        case
        / "production/single/runs/frozen_flow_actual_1s/reports/solver/transient_residuals.csv",
        case
        / "production/single/runs/full_transient_actual_1s/reports/solver/transient_residuals.csv",
        case
        / "production/L3/runs/full_transient_actual_1s/reports/solver/transient_residuals.csv",
    )
    transient_residual_path = next(
        (candidate for candidate in transient_candidates if candidate.is_file()), None
    )
    run_candidates = (
        case / "production/single/runs/steady_cht",
        case / "production/L3/runs/steady_cht",
        case / "production/L0/runs/steady_cht",
    )
    run_dir = next(
        (
            candidate
            for candidate in run_candidates
            if (candidate / "logs/solver").is_dir()
        ),
        None,
    )
    grouped: dict[str, dict[int, float]] = {}
    continuity: dict[int, float] = {}
    linear_iteration_total = 0
    source_logs: list[str] = []
    validation: dict[str, object] = {}
    axis_mode = "openfoam_outer_iteration"
    scope = "현재 정밀 결과 · 정상상태 CHT 초기화"
    transient_summary: dict[str, object] = {}
    if transient_residual_path is not None:
        with transient_residual_path.open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                iteration = int(row["global_iteration"])
                qualified = f'{row["region"]}.{row["field"]}'
                value = float(row["initial_residual"])
                grouped.setdefault(qualified, {})[iteration] = max(
                    value, grouped.get(qualified, {}).get(iteration, 0.0)
                )
                linear_iteration_total += int(row.get("linear_iterations") or 0)
        source_logs.append(str(transient_residual_path.relative_to(case)))
        summary_path = transient_residual_path.with_name(
            "transient_residuals_summary.json"
        )
        if summary_path.is_file():
            transient_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            source_logs.append(str(summary_path.relative_to(case)))
        axis_mode = (
            "transient_time_step_iteration"
            if "frozen_flow_actual_1s" in transient_residual_path.parts
            else "cumulative_pimple_outer_iteration"
        )
        scope = (
            "새 표준 결과 · 전체 과도 CHT 계산"
            if "single" in transient_residual_path.parts
            else "정밀 결과 · 전체 과도 CHT 계산"
        )
    elif run_dir is not None:
        if "single" in run_dir.parts:
            scope = "새 표준 결과 · 정상상태 CHT 초기화"
        for log_path in sorted((run_dir / "logs/solver").glob("steady_cht*.log")):
            parsed = parse_cold_flow_log(log_path)
            if not parsed["records"]:
                continue
            source_logs.append(str(log_path.relative_to(case)))
            for iteration_text, record in parsed["records"].items():
                iteration = int(round(float(iteration_text)))
                for qualified, values in record["residuals"].items():
                    if values:
                        linear_iteration_total += sum(
                            int(value.get("linear_iterations", 0)) for value in values
                        )
                        grouped.setdefault(qualified, {})[iteration] = max(
                            float(value["initial"]) for value in values
                        )
                continuity_values = record.get("continuity")
                if continuity_values is not None:
                    value = abs(float(continuity_values["global"]))
                    if value > 0:
                        continuity[iteration] = value
        validation_path = run_dir / "reports/solver/steady_cht_validation.json"
        if validation_path.is_file():
            validation = json.loads(validation_path.read_text(encoding="utf-8"))

    # A postprocessed case can retain residuals.csv after solver logs have been
    # archived. This fallback never invents points or interpolates iterations.
    if not grouped:
        residual_path = case / "results/residuals.csv"
        if residual_path.is_file():
            with residual_path.open(encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream):
                    qualified = f'{row["region"]}.{row["field"]}'
                    iteration = int(round(float(row["iteration"])))
                    value = float(row["initial_residual"])
                    grouped.setdefault(qualified, {})[iteration] = max(
                        value, grouped.get(qualified, {}).get(iteration, 0.0)
                    )
            source_logs.append(str(residual_path.relative_to(case)))
    if not grouped:
        return {"available": False}

    labels = {
        "continuity.global": "질량보존 |global|",
        "coolant.p_rgh": "압력 p_rgh",
        "coolant.Ux": "x-velocity",
        "coolant.Uy": "y-velocity",
        "coolant.Uz": "z-velocity",
        "coolant.h": "유체 energy(h)",
        "batterySolid.h": "고체 energy(h)",
        "coolant.k": "k",
        "coolant.omega": "omega",
    }
    colours = {
        "continuity.global": "#55d6c2",
        "coolant.p_rgh": "#63b3ff",
        "coolant.Ux": "#a9a0ff",
        "coolant.Uy": "#ff655a",
        "coolant.Uz": "#43a7e8",
        "coolant.h": "#ff9d2e",
        "batterySolid.h": "#ffd166",
        "coolant.k": "#8fd12a",
        "coolant.omega": "#ef9acb",
    }
    if continuity:
        grouped["continuity.global"] = continuity
    floors = validation.get("residual_floors", {})
    series = []
    all_iterations: set[int] = set()
    for key in labels:
        points = sorted(grouped.get(key, {}).items())
        if not points:
            continue
        stride = max(1, math.ceil(len(points) / 1200))
        sampled = [
            [iteration, value]
            for index, (iteration, value) in enumerate(points)
            if index % stride == 0 or index == len(points) - 1
        ]
        all_iterations.update(iteration for iteration, _ in points)
        series.append(
            {
                "key": key,
                "label": labels[key],
                "colour": colours[key],
                "points": sampled,
                "raw_point_count": len(points),
                "final": points[-1][1],
                "configured_gate": floors.get(key),
            }
        )
    final_checks = validation.get("residual_checks", {})
    primary_id = (
        "transient_energy"
        if axis_mode == "transient_time_step_iteration"
        else "full_transient"
        if axis_mode == "cumulative_pimple_outer_iteration"
        else "steady_cht"
    )
    primary_label = (
        "1초 과도 · 에너지"
        if primary_id == "transient_energy"
        else "전체 과도 CHT"
        if primary_id == "full_transient"
        else "정상 CHT · 유동+에너지"
    )
    payload = {
        "available": bool(series),
        "id": primary_id,
        "label": primary_label,
        "source": "actual OpenFOAM solver log",
        "scope": scope,
        "source_files": source_logs,
        "series": series,
        "axis_mode": axis_mode,
        "iteration_count": len(all_iterations),
        "first_iteration": min(all_iterations),
        "last_iteration": max(all_iterations),
        "linear_iteration_total": linear_iteration_total or None,
        "time_step_count": transient_summary.get("time_step_count"),
        "first_time_s": transient_summary.get("first_time_s"),
        "last_time_s": transient_summary.get("last_time_s"),
        "validation_status": validation.get("status", "SAVED_HISTORY"),
        "configured_residual_checks_passed": (
            bool(final_checks) and all(bool(value) for value in final_checks.values())
        ),
        "available_during_calculation": True,
        "live_note": (
            "During a new run these values are emitted on every solver iteration; "
            "the current static viewer contains the saved completed history."
        ),
        "iteration_axis_note": (
            "The x-axis is the cumulative PIMPLE outer-correction number, matching "
            "the transient iteration structure of the ANSYS reference. Residual "
            "values remain raw OpenFOAM residuals, not Fluent scaled residuals."
            if axis_mode == "cumulative_pimple_outer_iteration"
            else "The x-axis is the one-second transient energy-solve index. The "
            "validated velocity/pressure solution was completed separately and held "
            "fixed because this constant-property, zero-gravity model is one-way CHT."
            if axis_mode == "transient_time_step_iteration"
            else "The completed current result uses the OpenFOAM steady outer-"
            "iteration number. A new full-transient result will use the cumulative "
            "PIMPLE outer-correction axis."
        ),
        "semantics": (
            "Curves use maximum initial linear-solver residual per recorded iteration. "
            "OpenFOAM global continuity error is plotted separately and is not "
            "relabeled as a normalized Fluent continuity residual."
        ),
    }

    def _steady_history(
        *, run_name: str, history_id: str, label: str, scope_text: str,
        validation_name: str,
    ) -> dict[str, object] | None:
        run_path = case / f"production/single/runs/{run_name}"
        log_path = run_path / f"logs/solver/{run_name}.log"
        if not log_path.is_file():
            return None
        parsed = parse_cold_flow_log(log_path)
        if not parsed["records"]:
            return None

        history_grouped: dict[str, dict[int, float]] = {}
        history_continuity: dict[int, float] = {}
        history_linear_total = 0
        for iteration_text, record in parsed["records"].items():
            iteration = int(round(float(iteration_text)))
            for qualified, values in record["residuals"].items():
                if not values:
                    continue
                history_linear_total += sum(
                    int(value.get("linear_iterations", 0)) for value in values
                )
                history_grouped.setdefault(qualified, {})[iteration] = max(
                    float(value["initial"]) for value in values
                )
            continuity_values = record.get("continuity")
            if continuity_values is not None:
                continuity_value = abs(float(continuity_values["global"]))
                if continuity_value > 0:
                    history_continuity[iteration] = continuity_value
        if history_continuity:
            history_grouped["continuity.global"] = history_continuity

        validation_path = run_path / f"reports/solver/{validation_name}"
        history_validation: dict[str, object] = {}
        history_sources = [str(log_path.relative_to(case))]
        if validation_path.is_file():
            history_validation = json.loads(validation_path.read_text(encoding="utf-8"))
            history_sources.append(str(validation_path.relative_to(case)))

        history_floors = history_validation.get("residual_floors", {})
        history_series = []
        history_iterations: set[int] = set()
        for key in labels:
            points = sorted(history_grouped.get(key, {}).items())
            if not points:
                continue
            stride = max(1, math.ceil(len(points) / 1200))
            sampled = [
                [iteration, value]
                for index, (iteration, value) in enumerate(points)
                if index % stride == 0 or index == len(points) - 1
            ]
            history_iterations.update(iteration for iteration, _ in points)
            history_series.append(
                {
                    "key": key,
                    "label": labels[key],
                    "colour": colours[key],
                    "points": sampled,
                    "raw_point_count": len(points),
                    "final": points[-1][1],
                    "configured_gate": history_floors.get(key),
                }
            )
        if not history_series or not history_iterations:
            return None
        history_checks = history_validation.get("residual_checks", {})
        return {
            "available": True,
            "id": history_id,
            "label": label,
            "source": "actual OpenFOAM solver log",
            "scope": scope_text,
            "source_files": history_sources,
            "series": history_series,
            "axis_mode": "openfoam_outer_iteration",
            "iteration_count": len(history_iterations),
            "first_iteration": min(history_iterations),
            "last_iteration": max(history_iterations),
            "linear_iteration_total": history_linear_total or None,
            "time_step_count": None,
            "first_time_s": None,
            "last_time_s": None,
            "validation_status": history_validation.get("status", "SAVED_HISTORY"),
            "configured_residual_checks_passed": (
                bool(history_checks)
                and all(bool(value) for value in history_checks.values())
            ),
            "available_during_calculation": True,
            "live_note": (
                "This is a completed, saved multi-equation OpenFOAM solver history."
            ),
            "iteration_axis_note": (
                "The x-axis is the OpenFOAM steady outer-iteration number. "
                "Velocity, pressure, turbulence and energy equations are shown from "
                "the actual solver log."
            ),
            "semantics": payload["semantics"],
        }

    histories = []
    for history in (
        _steady_history(
            run_name="steady_cold_flow",
            history_id="cold_flow",
            label="유동 초기화 · U/p/k/ω",
            scope_text="새 표준 결과 · 유동 초기화",
            validation_name="cold_flow_validation.json",
        ),
        _steady_history(
            run_name="steady_cht",
            history_id="steady_cht",
            label="정상 CHT · 유동+에너지",
            scope_text="새 표준 결과 · 정상 CHT",
            validation_name="steady_cht_validation.json",
        ),
    ):
        if history is not None and history["id"] != primary_id:
            histories.append(history)
    histories.append(dict(payload))
    payload["histories"] = histories
    payload["default_history_id"] = (
        "steady_cht"
        if any(history["id"] == "steady_cht" for history in histories)
        else primary_id
    )
    return payload


def _direct_precision_xy_velocity_cut(
    config: ResolvedConfig, case: Path, *, production_profile: str = "L3"
) -> dict[str, object] | None:
    """Return a selected mesh's cell-centred XY cut without interpolation."""

    foam_file = case / f"production/{production_profile}/runs/steady_cht/steady_cht.foam"
    run_dir = foam_file.parent
    if not foam_file.is_file():
        return None
    numeric_times: list[float] = []
    for path in run_dir.iterdir():
        if not path.is_dir():
            continue
        try:
            numeric_times.append(float(path.name))
        except ValueError:
            continue
    if not numeric_times:
        return None

    try:
        import numpy as np
        import vtk
        from vtk.util.numpy_support import vtk_to_numpy
    except ImportError:
        return None

    reader = vtk.vtkOpenFOAMReader()
    reader.SetFileName(str(foam_file))
    reader.CreateCellToPointOff()
    reader.EnableAllCellArrays()
    reader.DisableAllPointArrays()
    reader.DisableAllPatchArrays()
    reader.SetPatchArrayStatus("internalMesh", 1)
    reader.UpdateInformation()
    latest_time = max(numeric_times)
    reader.UpdateTimeStep(latest_time)
    output = reader.GetOutput()
    fluid = None
    for index in range(output.GetNumberOfBlocks()):
        metadata = output.GetMetaData(index)
        name = metadata.Get(vtk.vtkCompositeDataSet.NAME()) if metadata else None
        if name != "coolant":
            continue
        parent = output.GetBlock(index)
        if hasattr(parent, "GetNumberOfBlocks"):
            for child_index in range(parent.GetNumberOfBlocks()):
                child = parent.GetBlock(child_index)
                if child is not None and child.GetNumberOfCells() > 0:
                    fluid = child
                    break
        else:
            fluid = parent
        break
    if fluid is None:
        return None

    plane = vtk.vtkPlane()
    plane.SetOrigin(0.0, 0.0, 0.5 * config.enclosure.height_z_m)
    plane.SetNormal(0.0, 0.0, 1.0)
    cutter = vtk.vtkCutter()
    cutter.SetCutFunction(plane)
    cutter.SetInputData(fluid)
    triangles = vtk.vtkTriangleFilter()
    triangles.SetInputConnection(cutter.GetOutputPort())
    triangles.Update()
    cut = triangles.GetOutput()
    velocity = cut.GetCellData().GetArray("U")
    if cut.GetPoints() is None or velocity is None:
        return None
    points = vtk_to_numpy(cut.GetPoints().GetData())[:, :2] * 1000.0
    cells = np.asarray(
        [
            [cut.GetCell(index).GetPointId(item) for item in range(3)]
            for index in range(cut.GetNumberOfCells())
        ],
        dtype=int,
    )
    speeds = np.linalg.norm(vtk_to_numpy(velocity), axis=1)
    return {
        "source": f"{production_profile} steady_cht cell-centred U on the Z-centre vtkCutter plane",
        "time": latest_time,
        "interpolation": "none; one containing OpenFOAM cell value per cut triangle",
        "points_xy_mm": np.round(points, 4).tolist(),
        "triangles": cells.tolist(),
        "velocity_m_s": np.round(speeds, 6).tolist(),
        "velocity_range_m_s": [0.0, float(np.max(speeds))],
        "triangle_count": int(len(cells)),
        "point_count": int(len(points)),
    }


def _interactive_legacy_cfd_payload(
    config: ResolvedConfig, case: Path, *, enabled: bool
) -> dict[str, object]:
    if not enabled:
        return {"enabled": False}
    foam_file = case / "production/L0/runs/frozen_flow/frozen_flow.foam"
    history_file = case / "results/per_cell_time_series.csv"
    telemetry_file = case / "results/telemetry.csv"
    if not foam_file.is_file() or not history_file.is_file() or not telemetry_file.is_file():
        return {"enabled": False}

    backend = OpenFoamResultReader(foam_file)
    fluid, _ = backend.regions(1800.0)
    all_paths = backend.streamline_paths(fluid, config)
    long_paths = [path for path in all_paths if path[4] >= 60.0]
    if not long_paths:
        return {"enabled": False}
    np = backend.np
    display_count = min(220, len(long_paths))
    selected = [
        long_paths[int(index)]
        for index in np.linspace(0, len(long_paths) - 1, display_count)
    ]
    browser_paths: list[dict[str, object]] = []
    for points, speeds, _, cumulative_mm, total_mm in selected:
        distances = np.linspace(0.0, total_mm, 72)
        coordinates = np.column_stack(
            [np.interp(distances, cumulative_mm, points[:, axis]) for axis in range(3)]
        )
        sampled_speed = np.interp(distances, cumulative_mm, speeds)
        browser_paths.append(
            {
                "p": [[round(float(value), 3) for value in row] for row in coordinates],
                "s": [round(float(value), 5) for value in sampled_speed],
                "length_mm": round(float(total_mm), 2),
            }
        )

    raw_history: dict[str, dict[int, float]] = {
        f"C{index:02d}": {
            0: config.boundary.initial_solid_temperature_k
            - 273.15
            + LEGACY_DISPLAY_TEMPERATURE_OFFSET_C
        }
        for index in range(1, 24)
    }
    with history_file.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            time_s = int(round(float(row["time_s"])))
            if time_s % 15 != 0:
                continue
            raw_history[row["cell_id"]][time_s] = (
                float(row["surface_tavg_K"])
                - 273.15
                + LEGACY_DISPLAY_TEMPERATURE_OFFSET_C
            )
    times = list(range(0, 1801, 15))
    if any(set(values) != set(times) for values in raw_history.values()):
        return {"enabled": False}
    temperature_series = {
        cell_id: [round(values[time_s], 5) for time_s in times]
        for cell_id, values in raw_history.items()
    }
    all_temperatures = [
        value for values in temperature_series.values() for value in values
    ]
    port_temperature: dict[str, dict[int, float]] = {
        "IN": {
            0: config.boundary.inlet_temperature_k
            - 273.15
            + LEGACY_DISPLAY_TEMPERATURE_OFFSET_C
        },
        "OUT": {
            0: config.boundary.initial_fluid_temperature_k
            - 273.15
            + LEGACY_DISPLAY_TEMPERATURE_OFFSET_C
        },
    }
    with telemetry_file.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            time_s = int(round(float(row["time_s"])))
            if time_s % 15 != 0:
                continue
            port_temperature["IN"][time_s] = (
                float(row["coolant_tin_K"])
                - 273.15
                + LEGACY_DISPLAY_TEMPERATURE_OFFSET_C
            )
            port_temperature["OUT"][time_s] = (
                float(row["coolant_tout_K"])
                - 273.15
                + LEGACY_DISPLAY_TEMPERATURE_OFFSET_C
            )
    if any(set(values) != set(times) for values in port_temperature.values()):
        return {"enabled": False}

    flow_root = case / "production/L0/runs/frozen_flow/postProcessing/coolant"
    inlet_mass_rows = read_openfoam_dat(
        flow_root / "coolant_inlet_mass_flow/0/surfaceFieldValue.dat"
    )
    outlet_mass_rows = read_openfoam_dat(
        flow_root / "coolant_outlet_mass_flow/0/surfaceFieldValue.dat"
    )
    inlet_pressure_rows = read_openfoam_dat(
        flow_root / "coolant_inlet_pressure_avg/0/surfaceFieldValue.dat"
    )
    outlet_pressure_rows = read_openfoam_dat(
        flow_root / "coolant_outlet_pressure_avg/0/surfaceFieldValue.dat"
    )
    inlet_area_m2 = math.pi * (config.ports.inlet_diameter_m / 2) ** 2
    outlet_area_m2 = math.pi * (config.ports.outlet_diameter_m / 2) ** 2
    inlet_mass_flow = abs(inlet_mass_rows[-1][1])
    outlet_mass_flow = abs(outlet_mass_rows[-1][1])
    inlet_pressure_pa = inlet_pressure_rows[-1][1]
    outlet_pressure_pa = outlet_pressure_rows[-1][1]
    port_flow = {
        "inlet_mass_flow_kg_s": inlet_mass_flow,
        "outlet_mass_flow_kg_s": outlet_mass_flow,
        "inlet_mean_velocity_m_s": inlet_mass_flow
        / (config.fluid.density_kg_m3 * inlet_area_m2),
        "outlet_mean_velocity_m_s": outlet_mass_flow
        / (config.fluid.density_kg_m3 * outlet_area_m2),
        "inlet_area_average_pressure_pa": inlet_pressure_pa,
        "outlet_area_average_pressure_pa": outlet_pressure_pa,
        "pressure_drop_pa": inlet_pressure_pa - outlet_pressure_pa,
        "spatial_field_semantics": "fixed converged U(x) and p(x) at 1800 s",
    }

    def sampling_plane(
        kind: str, columns: int, rows: int, *, fixed_fraction: float = 0.5
    ):
        vtk_points = backend.vtk.vtkPoints()
        coordinates: list[tuple[float, float, float]] = []
        if kind == "xy":
            fixed = config.enclosure.height_z_m * fixed_fraction
            for row in range(rows):
                y = config.enclosure.length_y_m * row / (rows - 1)
                for column in range(columns):
                    x = config.enclosure.width_x_m * column / (columns - 1)
                    coordinates.append((x, y, fixed))
        else:
            fixed = config.enclosure.length_y_m * fixed_fraction
            for row in range(rows):
                z = config.enclosure.height_z_m * row / (rows - 1)
                for column in range(columns):
                    x = config.enclosure.width_x_m * column / (columns - 1)
                    coordinates.append((x, fixed, z))
        for point in coordinates:
            vtk_points.InsertNextPoint(*point)
        samples = backend.vtk.vtkPolyData()
        samples.SetPoints(vtk_points)
        return samples, coordinates

    def probe(dataset, samples, array_name: str):
        sampler = backend.vtk.vtkProbeFilter()
        sampler.SetInputData(samples)
        sampler.SetSourceData(dataset)
        sampler.Update()
        point_data = sampler.GetOutput().GetPointData()
        array = point_data.GetArray(array_name)
        validity = point_data.GetArray("vtkValidPointMask")
        if array is None or validity is None:
            raise RuntimeError(f"VTK probe did not expose {array_name}")
        return (
            backend.vtk_to_numpy(array).copy(),
            backend.vtk_to_numpy(validity).astype(bool),
        )

    def json_values(values, validity):
        return [
            round(float(value), 5) if valid else None
            for value, valid in zip(values, validity)
        ]

    plane_specs = {
        "xy": {
            "kind": "xy",
            "columns": 37,
            "rows": 41,
            "fixed_fraction": 0.5,
            "label": "Z = 41.0 mm",
        },
        "xz15": {
            "kind": "xz",
            "columns": 37,
            "rows": 27,
            "fixed_fraction": 0.15,
            "label": "Y = 18.2 mm",
        },
        "xz33": {
            "kind": "xz",
            "columns": 37,
            "rows": 27,
            "fixed_fraction": 0.325,
            "label": "Y = 39.3 mm",
        },
        "xz50": {
            "kind": "xz",
            "columns": 37,
            "rows": 27,
            "fixed_fraction": 0.5,
            "label": "Y = 60.5 mm",
        },
        "xz68": {
            "kind": "xz",
            "columns": 37,
            "rows": 27,
            "fixed_fraction": 0.675,
            "label": "Y = 81.7 mm",
        },
        "xz85": {
            "kind": "xz",
            "columns": 37,
            "rows": 27,
            "fixed_fraction": 0.85,
            "label": "Y = 102.8 mm",
        },
    }
    plane_sources: dict[str, object] = {}
    sections: dict[str, dict[str, object]] = {}
    for name, specification in plane_specs.items():
        samples, coordinates = sampling_plane(
            specification["kind"],
            specification["columns"],
            specification["rows"],
            fixed_fraction=specification["fixed_fraction"],
        )
        plane_sources[name] = samples
        first = coordinates[0]
        horizontal = coordinates[1]
        vertical = coordinates[specification["columns"]]
        sections[name] = {
            "kind": specification["kind"],
            "columns": specification["columns"],
            "rows": specification["rows"],
            "fixed_fraction": specification["fixed_fraction"],
            "label": specification["label"],
            "origin_mm": [round(1000 * value, 4) for value in first],
            "column_step_mm": [
                round(1000 * (horizontal[axis] - first[axis]), 4)
                for axis in range(3)
            ],
            "row_step_mm": [
                round(1000 * (vertical[axis] - first[axis]), 4)
                for axis in range(3)
            ],
            "temperature_frames_c": [],
        }

    # Figure 8 uses the actual spatially varying battery-surface field.  Keep a
    # compact but genuinely field-derived sample lattice for the interactive
    # cylinders instead of painting every cylinder with its surface average.
    angular_segments = 28
    axial_segments = 12
    radius = config.cells.diameter_m / 2
    half_length = config.cells.length_m / 2
    surface_targets: dict[str, object] = {}
    for placement in cell_placements(config):
        points: list[tuple[float, float, float]] = []
        for axial in range(axial_segments):
            z_value = (
                placement.center_z_m
                - half_length
                + config.cells.length_m * (axial + 0.5) / axial_segments
            )
            for angular in range(angular_segments):
                angle = 2 * math.pi * (angular + 0.5) / angular_segments
                points.append(
                    (
                        placement.center_x_m + radius * math.cos(angle),
                        placement.center_y_m + radius * math.sin(angle),
                        z_value,
                    )
                )
        for z_value in (
            placement.center_z_m - half_length,
            placement.center_z_m + half_length,
        ):
            for angular in range(angular_segments):
                angle = 2 * math.pi * (angular + 0.5) / angular_segments
                points.append(
                    (
                        placement.center_x_m + 0.58 * radius * math.cos(angle),
                        placement.center_y_m + 0.58 * radius * math.sin(angle),
                        z_value,
                    )
                )
        surface_targets[placement.cell_id] = np.asarray(points)

    surface_point_indices: dict[str, object] = {}
    surface_temperature_frames: dict[str, list[list[float]]] = {
        cell_id: [] for cell_id in surface_targets
    }
    maximum_surface_sample_distance_mm = 0.0

    section_temperature_values: dict[str, list[float]] = {
        name: [] for name in sections
    }
    final_fluid = fluid
    for time_s in times:
        transient_fluid, transient_solid = backend.regions(float(time_s))
        if time_s == times[-1]:
            final_fluid = transient_fluid
        surface_points, _, surface_temperature = backend.surface(transient_solid)
        if not surface_point_indices:
            for placement in cell_placements(config):
                delta_xy = np.linalg.norm(
                    surface_points[:, :2]
                    - np.asarray(
                        [placement.center_x_m, placement.center_y_m]
                    ),
                    axis=1,
                )
                candidates = np.flatnonzero(
                    (delta_xy <= radius * 1.08)
                    & (
                        np.abs(surface_points[:, 2] - placement.center_z_m)
                        <= half_length * 1.03
                    )
                )
                if not len(candidates):
                    raise RuntimeError(
                        f"no solid surface points found near {placement.cell_id}"
                    )
                targets = surface_targets[placement.cell_id]
                candidate_points = surface_points[candidates]
                distances_squared = np.sum(
                    (targets[:, None, :] - candidate_points[None, :, :]) ** 2,
                    axis=2,
                )
                closest = candidates[np.argmin(distances_squared, axis=1)]
                surface_point_indices[placement.cell_id] = closest
                maximum_surface_sample_distance_mm = max(
                    maximum_surface_sample_distance_mm,
                    1000
                    * float(
                        np.sqrt(
                            distances_squared[
                                np.arange(len(targets)),
                                np.argmin(distances_squared, axis=1),
                            ]
                        ).max()
                    ),
                )
        for cell_id, indices in surface_point_indices.items():
            values = (
                surface_temperature[indices]
                - 273.15
                + LEGACY_DISPLAY_TEMPERATURE_OFFSET_C
            )
            surface_temperature_frames[cell_id].append(
                [round(float(value), 5) for value in values]
            )
        for name, section in sections.items():
            samples = plane_sources[name]
            fluid_temperature, fluid_valid = probe(transient_fluid, samples, "T")
            solid_temperature, solid_valid = probe(transient_solid, samples, "T")
            combined_valid = fluid_valid | solid_valid
            combined_temperature = np.where(
                solid_valid, solid_temperature, fluid_temperature
            ) - 273.15 + LEGACY_DISPLAY_TEMPERATURE_OFFSET_C
            section["temperature_frames_c"].append(
                json_values(combined_temperature, combined_valid)
            )
            section_temperature_values[name].extend(
                float(value)
                for value, valid in zip(combined_temperature, combined_valid)
                if valid
            )

    for name, section in sections.items():
        samples = plane_sources[name]
        velocity, velocity_valid = probe(final_fluid, samples, "U")
        speed = np.linalg.norm(velocity, axis=1)
        section["velocity_m_s"] = json_values(speed, velocity_valid)
        section["velocity_vectors_m_s"] = [
            [round(float(component), 5) for component in vector] if valid else None
            for vector, valid in zip(velocity, velocity_valid)
        ]
        temperatures = section_temperature_values[name]
        section["temperature_range_c"] = [min(temperatures), max(temperatures)]
        valid_speed = [
            float(value) for value, valid in zip(speed, velocity_valid) if valid
        ]
        section["velocity_range_m_s"] = [0.0, max(valid_speed)]

    final_cell_velocity = backend.vtk_to_numpy(
        final_fluid.GetCellData().GetArray("U")
    )
    velocity_max_m_s = float(np.linalg.norm(final_cell_velocity, axis=1).max())
    lengths = [float(path[4]) for path in selected]
    direct_xy_velocity = _direct_precision_xy_velocity_cut(config, case)
    return {
        "enabled": True,
        "semantics": (
            "OpenFOAM cell temperature T(t) with a +0.150 K display-only "
            "datum correction; moving heads follow the validated fixed "
            "velocity field U(x), not transient U(x,t)."
        ),
        "temperature_display_offset_c": LEGACY_DISPLAY_TEMPERATURE_OFFSET_C,
        "temperature_display_semantics": (
            "raw solver temperatures are preserved; viewer values equal raw degC + 0.150 K"
        ),
        "times_s": times,
        "temperature_c": temperature_series,
        "port_temperature_c": {
            name: [round(values[time_s], 5) for time_s in times]
            for name, values in port_temperature.items()
        },
        "port_flow": port_flow,
        "paper_temperature_ranges_c": {
            "cell": REFERENCE_FIGURE_RANGES["figure_09_temperature_degC"],
            "xy": REFERENCE_FIGURE_RANGES["figure_14_temperature_degC"],
            "xz": REFERENCE_FIGURE_RANGES["figure_13b_temperature_degC"],
        },
        "temperature_range_c": [min(all_temperatures), max(all_temperatures)],
        "cell_surface": {
            "angular_segments": angular_segments,
            "axial_segments": axial_segments,
            "temperature_frames_c": surface_temperature_frames,
            "temperature_range_c": [
                min(min(values) for values in section_temperature_values.values()),
                max(max(values) for values in section_temperature_values.values()),
            ],
            "sample_semantics": (
                "nearest actual OpenFOAM batterySolid surface point on a "
                "28-angle by 12-height display lattice; no per-cell flat fill"
            ),
            "maximum_sample_distance_mm": round(
                maximum_surface_sample_distance_mm, 4
            ),
        },
        "velocity_range_m_s": [0.0, velocity_max_m_s],
        "velocity_max_m_s": velocity_max_m_s,
        "velocity_scale_semantics": (
            "shared global range from every coolant cell-centred U magnitude; "
            "used by streamlines, XY sections, XZ sections, vectors, and ports"
        ),
        "streamlines": browser_paths,
        "streamline_count": len(browser_paths),
        "source_streamline_count": len(all_paths),
        "minimum_displayed_length_mm": min(lengths),
        "maximum_displayed_length_mm": max(lengths),
        "sections": sections,
        "direct_xy_velocity": direct_xy_velocity,
        "vertical_section_keys": ["xz15", "xz33", "xz50", "xz68", "xz85"],
    }


def _interactive_actual_cfd_payload(
    config: ResolvedConfig,
    case: Path,
    *,
    enabled: bool,
    mesh_id: str = "precision",
) -> dict[str, object] | None:
    """Build the browser payload from a completed, validated result package."""

    if not enabled:
        return None
    if mesh_id not in {"precision", "single"}:
        raise ValueError(f"unsupported viewer mesh id: {mesh_id}")
    production_profile = "L3" if mesh_id == "precision" else "single"
    mesh_label = "precision" if mesh_id == "precision" else "standard"
    runs_root = case / f"production/{production_profile}/runs"
    upgraded_run = runs_root / "frozen_flow_actual_1s_xy5"
    run = runs_root / "frozen_flow_actual_1s"
    if (
        mesh_id == "precision"
        and (upgraded_run / "reports/viewer/actual_1s_manifest.json").is_file()
    ):
        run = upgraded_run
    manifest_path = run / "reports/viewer/actual_1s_manifest.json"
    validation_path = run / "reports/viewer/actual_1s_validation.json"
    foam_file = runs_root / "steady_cht/steady_cht.foam"
    if (
        not manifest_path.is_file()
        or not validation_path.is_file()
        or not foam_file.is_file()
    ):
        return None

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if (
        manifest.get("status") != "ACTUAL_ONE_SECOND_SAMPLES_READY"
        or validation.get("status")
        != "ACTUAL_ONE_SECOND_BUNDLE_VALIDATION_PASSED"
    ):
        return None
    expected_times = list(range(0, 1801))
    initial_temperature_k = float(manifest["initial_temperature_k"])

    def exact_series(path: Path, initial_k: float) -> list[float]:
        values = {0: initial_k}
        for row in read_openfoam_dat(path):
            time_s = int(round(row[0]))
            if abs(row[0] - time_s) > 1e-7:
                raise ValueError(f"{path}: non-integral time {row[0]}")
            values[time_s] = row[1]
        if sorted(values) != expected_times:
            raise ValueError(
                f"{path}: expected exact 0..1800 s values; found {len(values)}"
            )
        return [round(values[time_s] - 273.15, 8) for time_s in expected_times]

    def header_measure(path: Path, name: str) -> float:
        prefix = f"# {name}"
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(prefix) and ":" in line:
                return float(line.split(":", 1)[1].strip().split()[0])
        raise ValueError(f"{path}: missing {name} header")

    solid_root = run / "postProcessing/batterySolid"
    cell_temperature: dict[str, list[float]] = {}
    cell_internal_temperature: dict[str, list[float]] = {}
    cell_mesh_maximum_temperature: dict[str, list[float]] = {}
    cell_surface_areas_m2: dict[str, float] = {}
    cell_volumes_m3: dict[str, float] = {}
    for index in range(1, 24):
        cell_id = f"C{index:02d}"
        surface_path = (
            solid_root
            / f"battery_{cell_id}_surface_Tavg/0/surfaceFieldValue.dat"
        )
        internal_path = solid_root / f"battery_{cell_id}_Tavg/0/volFieldValue.dat"
        maximum_path = solid_root / f"battery_{cell_id}_Tmax/0/volFieldValue.dat"
        cell_temperature[cell_id] = exact_series(
            surface_path, initial_temperature_k
        )
        cell_internal_temperature[cell_id] = exact_series(
            internal_path, initial_temperature_k
        )
        cell_mesh_maximum_temperature[cell_id] = exact_series(
            maximum_path, initial_temperature_k
        )
        cell_surface_areas_m2[cell_id] = header_measure(surface_path, "Area")
        cell_volumes_m3[cell_id] = header_measure(internal_path, "Volume")

    def weighted_average_series(
        values: dict[str, list[float]], weights: dict[str, float]
    ) -> list[float]:
        total_weight = sum(weights.values())
        return [
            round(
                sum(values[cell_id][time_s] * weights[cell_id] for cell_id in values)
                / total_weight,
                8,
            )
            for time_s in expected_times
        ]

    battery_surface_average = weighted_average_series(
        cell_temperature, cell_surface_areas_m2
    )
    battery_internal_average = weighted_average_series(
        cell_internal_temperature, cell_volumes_m3
    )
    battery_mesh_maximum = [
        round(
            max(values[time_s] for values in cell_mesh_maximum_temperature.values()),
            8,
        )
        for time_s in expected_times
    ]
    port_temperature = {
        "IN": exact_series(
            run
            / "postProcessing/coolant/coolant_inlet_mass_weighted_temperature/0/surfaceFieldValue.dat",
            initial_temperature_k,
        ),
        "OUT": exact_series(
            run
            / "postProcessing/coolant/coolant_outlet_mass_weighted_temperature/0/surfaceFieldValue.dat",
            initial_temperature_k,
        ),
    }

    flow_root = run / "postProcessing/coolant"
    inlet_mass = abs(
        read_openfoam_dat(
            flow_root / "coolant_inlet_mass_flow/0/surfaceFieldValue.dat"
        )[-1][1]
    )
    outlet_mass = abs(
        read_openfoam_dat(
            flow_root / "coolant_outlet_mass_flow/0/surfaceFieldValue.dat"
        )[-1][1]
    )
    inlet_pressure = read_openfoam_dat(
        flow_root / "coolant_inlet_pressure_avg/0/surfaceFieldValue.dat"
    )[-1][1]
    outlet_pressure = read_openfoam_dat(
        flow_root / "coolant_outlet_pressure_avg/0/surfaceFieldValue.dat"
    )[-1][1]
    inlet_area = math.pi * (0.5 * config.ports.inlet_diameter_m) ** 2
    outlet_area = math.pi * (0.5 * config.ports.outlet_diameter_m) ** 2
    port_flow = {
        "inlet_mass_flow_kg_s": inlet_mass,
        "outlet_mass_flow_kg_s": outlet_mass,
        "inlet_mean_velocity_m_s": inlet_mass
        / (config.fluid.density_kg_m3 * inlet_area),
        "outlet_mean_velocity_m_s": outlet_mass
        / (config.fluid.density_kg_m3 * outlet_area),
        "inlet_area_average_pressure_pa": inlet_pressure,
        "outlet_area_average_pressure_pa": outlet_pressure,
        "pressure_drop_pa": inlet_pressure - outlet_pressure,
        "spatial_field_semantics": f"validated {mesh_label}-mesh fixed field U(x), p(x)",
    }

    backend = OpenFoamResultReader(foam_file)
    numeric_times = []
    for path in foam_file.parent.iterdir():
        if path.is_dir():
            try:
                numeric_times.append(float(path.name))
            except ValueError:
                pass
    fluid, _ = backend.regions(max(numeric_times))
    all_paths = backend.streamline_paths(fluid, config)
    long_paths = [path for path in all_paths if path[4] >= 60.0]
    selected_count = min(220, len(long_paths))
    selected = [
        long_paths[int(index)]
        for index in backend.np.linspace(0, len(long_paths) - 1, selected_count)
    ]
    browser_paths = [
        {
            "p": [
                [round(float(component), 4) for component in point]
                for point in points
            ],
            "s": [round(float(value), 7) for value in speeds],
            "length_mm": round(float(total_mm), 3),
        }
        for points, speeds, _, _, total_mm in selected
    ]

    def centred_axis(length: float, spacing: float) -> list[float]:
        count = max(1, math.ceil(length / spacing))
        return [length * (index + 0.5) / count for index in range(count)]

    x_values = centred_axis(config.enclosure.width_x_m, 0.001)
    y_values = centred_axis(config.enclosure.length_y_m, 0.001)
    z_values = centred_axis(config.enclosure.height_z_m, 0.0015)
    plane_specs = {
        "xy15": ("xy", x_values, y_values, 0.15, "Z = 12.3 mm"),
        "xy33": ("xy", x_values, y_values, 0.325, "Z = 26.7 mm"),
        "xy": ("xy", x_values, y_values, 0.5, "Z = 41.0 mm"),
        "xy68": ("xy", x_values, y_values, 0.675, "Z = 55.4 mm"),
        "xy85": ("xy", x_values, y_values, 0.85, "Z = 69.7 mm"),
        "xz15": ("xz", x_values, z_values, 0.15, "Y = 18.2 mm"),
        "xz33": ("xz", x_values, z_values, 0.325, "Y = 39.3 mm"),
        "xz50": ("xz", x_values, z_values, 0.5, "Y = 60.5 mm"),
        "xz68": ("xz", x_values, z_values, 0.675, "Y = 81.7 mm"),
        "xz85": ("xz", x_values, z_values, 0.85, "Y = 102.8 mm"),
    }
    locator = backend.vtk.vtkStaticCellLocator()
    locator.SetDataSet(fluid)
    locator.BuildLocator()
    cell_velocity = backend.vtk_to_numpy(fluid.GetCellData().GetArray("U"))
    velocity_max_m_s = float(backend.np.linalg.norm(cell_velocity, axis=1).max())
    sections: dict[str, dict[str, object]] = {}
    for name, (kind, horizontal, vertical, fraction, label) in plane_specs.items():
        if name not in manifest["datasets"]:
            continue
        coordinates = (
            [
                (x, y, fraction * config.enclosure.height_z_m)
                for y in vertical
                for x in horizontal
            ]
            if kind == "xy"
            else [
                (x, fraction * config.enclosure.length_y_m, z)
                for z in vertical
                for x in horizontal
            ]
        )
        velocity_vectors: list[list[float] | None] = []
        velocity_magnitudes: list[float | None] = []
        for point in coordinates:
            cell_id = locator.FindCell(point)
            if cell_id < 0:
                velocity_vectors.append(None)
                velocity_magnitudes.append(None)
                continue
            vector = cell_velocity[cell_id]
            velocity_vectors.append(
                [round(float(component), 7) for component in vector]
            )
            velocity_magnitudes.append(
                round(float(backend.np.linalg.norm(vector)), 7)
            )
        valid_velocity = [value for value in velocity_magnitudes if value is not None]
        origin = coordinates[0]
        next_column = coordinates[1]
        next_row = coordinates[len(horizontal)]
        sections[name] = {
            "kind": kind,
            "columns": len(horizontal),
            "rows": len(vertical),
            "fixed_fraction": fraction,
            "label": label,
            "origin_mm": [round(1000 * value, 6) for value in origin],
            "column_step_mm": [
                round(1000 * (next_column[axis] - origin[axis]), 6)
                for axis in range(3)
            ],
            "row_step_mm": [
                round(1000 * (next_row[axis] - origin[axis]), 6)
                for axis in range(3)
            ],
            "actual_dataset": name,
            "temperature_range_c": manifest["datasets"][name][
                "temperature_range_c"
            ],
            "velocity_m_s": velocity_magnitudes,
            "velocity_vectors_m_s": velocity_vectors,
            "velocity_range_m_s": [0.0, max(valid_velocity)],
            "spatial_sampling": f"direct containing {mesh_label}-mesh cell; no interpolation or padding",
        }

    datasets = json.loads(json.dumps(manifest["datasets"]))
    for specification in datasets.values():
        specification["file"] = f"data/{specification['file']}"
    temperatures = [value for series in cell_temperature.values() for value in series]
    surface_spec = datasets["cell_surface"]
    return {
        "enabled": True,
        "semantics": (
            f"{mesh_label}-result OpenFOAM containing-cell T samples at every integer second; "
            "no temporal interpolation, spatial interpolation, display correction, or padding. "
            f"Moving heads follow the validated fixed {mesh_label}-result U(x)."
        ),
        "temperature_display_offset_c": 0.0,
        "temperature_display_semantics": "raw solver kelvin converted only by subtracting 273.15",
        "times_s": expected_times,
        "time_step_s": 1,
        "temporal_interpolation": False,
        "spatial_interpolation": False,
        "padding": False,
        "temperature_c": cell_temperature,
        "port_temperature_c": port_temperature,
        "temperature_graphs": {
            "time_step_s": 1,
            "outlet_mass_weighted_average_c": port_temperature["OUT"],
            "battery_mesh_maximum_c": battery_mesh_maximum,
            "battery_surface_area_average_c": battery_surface_average,
            "battery_internal_volume_average_c": battery_internal_average,
            "per_cell": {
                "surface_area_average_c": cell_temperature,
                "internal_volume_average_c": cell_internal_temperature,
                "mesh_maximum_c": cell_mesh_maximum_temperature,
            },
            "surface_area_m2": cell_surface_areas_m2,
            "volume_m3": cell_volumes_m3,
            "semantics": {
                "outlet": "coolant outlet phi-weighted average temperature",
                "battery_maximum": "maximum of batterySolid cell-mesh max(T) across C01-C23",
                "surface_average": "area-weighted mean of C01-C23 interface areaAverage(T)",
                "internal_average": "volume-weighted mean of C01-C23 cell-zone volAverage(T)",
                "per_cell": "direct C01-C23 function-object values at every integer second",
            },
        },
        "port_flow": port_flow,
        "paper_temperature_ranges_c": {
            "cell": REFERENCE_FIGURE_RANGES["figure_09_temperature_degC"],
            "xy": REFERENCE_FIGURE_RANGES["figure_14_temperature_degC"],
            "xz": REFERENCE_FIGURE_RANGES["figure_13b_temperature_degC"],
        },
        "temperature_range_c": [min(temperatures), max(temperatures)],
        "cell_surface": {
            "angular_segments": surface_spec["angular_segments"],
            "axial_segments": surface_spec["axial_segments"],
            "actual_dataset": "cell_surface",
            "temperature_range_c": surface_spec["temperature_range_c"],
            "sample_semantics": "direct containing batterySolid cell at 32 angles x 16 heights; invalid samples omitted",
        },
        "actual_one_second": {
            "validation_status": validation["status"],
            "mesh_cell_count": manifest["mesh_cell_count"],
            "initial_temperature_k": manifest["initial_temperature_k"],
            "times_s": manifest["times_s"],
            "temperature_encoding": manifest["temperature_encoding"],
            "temporal_interpolation": False,
            "spatial_interpolation": False,
            "padding": False,
            "datasets": datasets,
        },
        "velocity_range_m_s": [0.0, velocity_max_m_s],
        "velocity_max_m_s": velocity_max_m_s,
        "velocity_scale_semantics": (
            f"shared global range from every {mesh_label}-mesh coolant "
            "cell-centred U magnitude; used by streamlines, XY sections, "
            "XZ sections, vectors, and ports"
        ),
        "streamlines": browser_paths,
        "streamline_count": len(browser_paths),
        "source_streamline_count": len(all_paths),
        "minimum_displayed_length_mm": min(float(path[4]) for path in selected),
        "maximum_displayed_length_mm": max(float(path[4]) for path in selected),
        "sections": sections,
        "direct_xy_velocity": _direct_precision_xy_velocity_cut(
            config, case, production_profile=production_profile
        ),
        "horizontal_section_keys": ["xy15", "xy33", "xy", "xy68", "xy85"],
        "vertical_section_keys": ["xz15", "xz33", "xz50", "xz68", "xz85"],
    }


def _interactive_cfd_payload(
    config: ResolvedConfig, case: Path, *, enabled: bool
) -> dict[str, object]:
    actual = _interactive_actual_cfd_payload(config, case, enabled=enabled)
    if actual is not None:
        return actual
    return _interactive_legacy_cfd_payload(config, case, enabled=enabled)


HTML = '''<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CFD 결과 뷰어</title>
  <link rel="stylesheet" href="__STYLE_ASSET__">
</head>
<body>
  <header>
    <div class="brand">
      <h1>CFD 결과 뷰어</h1>
      <span>DIGITAL TWIN · SIMULATION REPLAY</span>
    </div>
    <div id="status" class="status">GEOMETRY PREVIEW · CFD 미실행</div>
  </header>
  <main>
    <section class="viewer-panel">
      <div class="viewer-controls">
        <fieldset id="viz-modes" class="viz-modes" aria-label="결과 화면 선택">
          <legend>결과 화면</legend>
          <label><input type="radio" name="viz-mode" value="combined" checked><span>종합 화면</span></label>
          <label><input type="radio" name="viz-mode" value="cell-temperature"><span>셀 온도</span></label>
          <label><input type="radio" name="viz-mode" value="velocity-streamlines"><span>유체 유선</span></label>
          <label><input type="radio" name="viz-mode" value="xy-temperature"><span>가로 단면 온도</span></label>
          <label><input type="radio" name="viz-mode" value="xy-velocity"><span>가로 단면 유속</span></label>
          <label><input type="radio" name="viz-mode" value="xz-temperature"><span>세로 단면 온도</span></label>
          <label><input type="radio" name="viz-mode" value="xz-velocity"><span>세로 단면 유속</span></label>
        </fieldset>
        <div class="control-head">
          <div class="toolbar" aria-label="camera views">
            <span class="control-title">시점</span>
            <button data-view="px">+X</button><button data-view="nx">−X</button>
            <button data-view="py">+Y</button><button data-view="ny">−Y</button>
            <button data-view="pz">+Z</button><button data-view="nz">−Z</button>
            <button data-view="iso" class="active">등각</button>
            <button id="reset">맞춤</button>
          </div>
          <fieldset id="display-options" class="display-options" aria-label="화면 레이어">
            <legend>레이어</legend>
            <label><input id="show-cells" type="checkbox" checked> 셀</label>
            <label><input id="show-flow" type="checkbox" checked> 유선·벡터</label>
            <label><input id="show-particles" type="checkbox" checked> 이동 헤드</label>
            <label><input id="show-section" type="checkbox" checked> 단면</label>
            <label><input id="show-grid" type="checkbox"> 샘플선</label>
            <label title="실제 유효 샘플 또는 셀 표면 샘플의 경계만 부드럽게 연결하며 무효영역은 채우지 않습니다"><input id="smooth-section" type="checkbox" checked> 부드러운 색상</label>
            <label id="section-3d-option" title="가로 단면을 회전·확대 가능한 3D 평면으로 표시합니다" hidden><input id="section-3d" type="checkbox" checked> 3D 단면</label>
            <label><input id="show-enclosure" type="checkbox" checked> 외곽선</label>
            <button id="flow-only" type="button">유동만</button>
            <button id="show-all" type="button">전체</button>
          </fieldset>
        </div>
        <fieldset id="vertical-sections" class="section-positions vertical-sections" aria-label="vertical XZ section positions" hidden>
          <legend>세로 XZ 위치</legend>
          <label><input type="checkbox" name="vertical-section" value="xz15"> Y 15%</label>
          <label><input type="checkbox" name="vertical-section" value="xz33"> Y 32.5%</label>
          <label><input type="checkbox" name="vertical-section" value="xz50" checked> Y 50%</label>
          <label><input type="checkbox" name="vertical-section" value="xz68"> Y 67.5%</label>
          <label><input type="checkbox" name="vertical-section" value="xz85"> Y 85%</label>
          <button id="vertical-centre" type="button">중앙만</button>
          <button id="vertical-all" type="button">5단면</button>
          <output id="vertical-section-state">Y = 60.5 mm</output>
        </fieldset>
        <fieldset id="horizontal-sections" class="section-positions horizontal-sections" aria-label="horizontal XY section positions" hidden>
          <legend>가로 XY 위치</legend>
          <label><input type="radio" name="horizontal-section" value="xy15"> Z 15%</label>
          <label><input type="radio" name="horizontal-section" value="xy33"> Z 32.5%</label>
          <label><input type="radio" name="horizontal-section" value="xy" checked> Z 50%</label>
          <label><input type="radio" name="horizontal-section" value="xy68"> Z 67.5%</label>
          <label><input type="radio" name="horizontal-section" value="xy85"> Z 85%</label>
          <button id="horizontal-centre" type="button">중앙</button>
          <output id="horizontal-section-state">Z = 41.0 mm</output>
        </fieldset>
      </div>
      <div class="viewport">
        <div class="scene-stage">
          <canvas id="scene" aria-label="interactive 3D battery geometry"></canvas>
          <canvas id="scene-overlay" aria-hidden="true"></canvas>
          <div id="data-load-status" class="data-load-status" role="status" aria-live="polite" hidden></div>
        </div>
        <div id="flow-controls" class="flow-controls" aria-label="interactive CFD playback">
        <button id="flow-play" type="button">❚❚ 정지</button>
        <input id="flow-seek" type="range" min="0" max="1800" step="1" value="0" aria-label="simulation time in one-second display increments">
        <label for="flow-rate">배속</label>
        <select id="flow-rate" aria-label="interactive playback speed">
          <option value="1" selected>1×</option>
          <option value="5">5×</option>
          <option value="10">10×</option>
          <option value="30">30×</option>
          <option value="60">60×</option>
          <option value="180">180×</option>
        </select>
        <label for="head-rate">헤드 속도</label>
        <select id="head-rate" aria-label="moving head display speed" title="유속 수치는 그대로 유지하고 이동 헤드의 표시속도만 조절합니다">
          <option value="0.25">0.25×</option>
          <option value="0.5" selected>0.5× 보기</option>
          <option value="1">1× 실제</option>
        </select>
        <output id="flow-duration">1× 완주 30:00</output>
        <output id="flow-time">t = 0 s</output>
        <span id="flow-sampling">화면 1 s</span>
        <span id="current-mode" aria-live="polite">종합 화면</span>
        <span id="port-state" aria-live="polite">IN/OUT 면적평균</span>
        </div>
        <div id="figure-preview" class="figure-preview" hidden>
        <img id="figure-image" alt="generated figure artifact">
        <video id="figure-video" muted playsinline hidden></video>
        <div id="video-controls" class="video-controls" hidden>
          <button id="video-play" type="button">❚❚ 정지</button>
          <button id="video-prev" type="button" aria-label="previous animation frame">◀ 1프레임</button>
          <button id="video-next" type="button" aria-label="next animation frame">1프레임 ▶</button>
          <input id="video-seek" type="range" min="0" max="1000" value="0" aria-label="animation timeline">
          <label for="playback-rate">배속</label>
          <select id="playback-rate" aria-label="animation playback speed">
            <option value="1" selected>1×</option>
            <option value="5">5×</option>
            <option value="10">10×</option>
            <option value="30">30×</option>
            <option value="60">60×</option>
            <option value="180">180×</option>
          </select>
          <output id="video-duration">1× 완주 30:00</output>
          <output id="video-time">대기 중</output>
          <span id="video-semantics"></span>
        </div>
        <div id="artifact-tabs" class="artifact-tabs"></div>
        </div>
        <div id="temperature-chart-panel" class="temperature-chart-panel" hidden>
          <div class="temperature-chart-toolbar">
            <button id="temperature-chart-close" type="button">← 3D 화면</button>
            <label for="temperature-chart-metric">그래프</label>
            <select id="temperature-chart-metric">
              <option value="outlet">1. 출구 평균 온도</option>
              <option value="battery-maximum">2. 배터리 최고 온도</option>
              <option value="surface-average">3. 배터리 표면 평균 온도</option>
              <option value="internal-average">4. 배터리 내부 온도</option>
              <option value="per-cell">5. 각 배터리별 온도</option>
            </select>
            <label id="per-cell-metric-label" for="per-cell-metric" hidden>셀 기준</label>
            <select id="per-cell-metric" hidden>
              <option value="internal_volume_average_c">내부 체적평균</option>
              <option value="surface_area_average_c">표면 면적평균</option>
              <option value="mesh_maximum_c">mesh 최고값</option>
            </select>
            <button id="temperature-chart-png" type="button">PNG 추출</button>
            <button id="temperature-chart-csv" type="button">현재 CSV</button>
            <button id="temperature-chart-all-csv" type="button">전체 CSV</button>
          </div>
          <div class="temperature-chart-canvas-wrap">
            <canvas id="temperature-chart-canvas" aria-label="actual CFD temperature time-series chart"></canvas>
          </div>
          <output id="temperature-chart-status" aria-live="polite"></output>
        </div>
        <div id="convergence-chart-panel" class="convergence-chart-panel" hidden>
          <div class="convergence-chart-toolbar">
            <button id="convergence-chart-close" type="button">← 3D 화면</button>
            <strong>CFD 수렴 이력</strong>
            <label for="convergence-history-select">계산 단계</label>
            <select id="convergence-history-select" aria-label="CFD convergence calculation stage"></select>
            <span>실제 솔버 로그 · 잔차 로그 축</span>
            <button id="convergence-chart-png" type="button">PNG 추출</button>
            <button id="convergence-chart-csv" type="button">CSV 추출</button>
          </div>
          <div class="convergence-chart-canvas-wrap">
            <canvas id="convergence-chart-canvas" aria-label="actual CFD residual convergence chart"></canvas>
          </div>
          <output id="convergence-chart-status" aria-live="polite"></output>
        </div>
        <div id="canvas-note" class="canvas-note">드래그: 재생 중 3D 회전 · 휠: 확대/축소 · Ū·p̄ 실제 면적평균 · 포트는 A-04</div>
      </div>
    </section>
    <aside>
      <section id="calculation-card" class="card result-card warning">
        <div class="card-title-row">
          <h2>현재 결과</h2>
          <span id="result-badge" class="result-badge">대기</span>
        </div>
        <div class="kpi-grid" aria-label="주요 CFD 결과">
          <article><span>출구 평균 온도</span><strong id="kpi-outlet-temperature">—</strong><small id="kpi-outlet-time">현재 시점</small></article>
          <article><span>최고 셀 온도</span><strong id="kpi-cell-maximum">—</strong><small id="kpi-cell-time">23개 셀 · 현재 시점</small></article>
          <article><span>입구 유속</span><strong id="kpi-inlet-velocity">—</strong><small>면적 평균</small></article>
          <article><span>압력 강하</span><strong id="kpi-pressure-drop">—</strong><small>입구 − 출구</small></article>
        </div>
        <p id="calculation-summary" class="result-meta"><strong>CFD 미실행</strong></p>
        <p id="calculation-note" hidden></p>
      </section>
      <section class="card condition-card">
        <h2>운전 조건</h2>
        <dl id="inputs"></dl>
      </section>
      <section class="card mesh-run-card">
        <h2>계산 메시 선택</h2>
        <label for="mesh-run-select">결과 데이터</label>
        <select id="mesh-run-select" aria-label="calculation mesh result"></select>
        <dl id="mesh-run-detail"></dl>
        <p id="mesh-run-note"></p>
      </section>
      <section class="card convergence-card">
        <h2>CFD 수렴 모니터</h2>
        <dl id="convergence-summary"></dl>
        <button id="convergence-chart-open" type="button">수렴 그래프 열기</button>
        <p>과도 단계는 고정 유동이므로 에너지만 계산합니다. 유동·정상 CHT 다중 잔차는 그래프의 계산 단계에서 확인할 수 있습니다.</p>
      </section>
      <section class="card temperature-export-card">
        <h2>온도 그래프</h2>
        <dl id="temperature-graph-summary"></dl>
        <button id="temperature-chart-open" type="button">온도 그래프 열기</button>
        <p>0–1800 s · 1초 간격 · PNG/CSV 저장</p>
      </section>
      <details class="card detail-card">
        <summary>모델 정보</summary>
        <div class="detail-body"><dl id="geometry"></dl></div>
      </details>
      <details class="card detail-card">
        <summary>해석 세부정보</summary>
        <div class="detail-body"><dl id="derived"></dl></div>
      </details>
      <details class="card detail-card">
        <summary>논문 Figure 결과</summary>
        <div class="detail-body">
          <div id="figures" class="figure-grid"></div>
          <p id="figure-detail">Fig. 1 · 3D 인터랙티브 화면</p>
        </div>
      </details>
      <details class="card detail-card">
        <summary>셀 ID 보기</summary>
        <div class="detail-body"><div id="cells" class="cell-grid"></div></div>
      </details>
    </aside>
  </main>
  <div id="cell-toast" class="cell-toast" role="status" aria-live="polite" hidden></div>
  <footer id="provenance"></footer>
  <script src="__MODEL_ASSET__"></script>
  <script src="__APP_ASSET__"></script>
</body>
</html>
'''


CSS = '''
:root { color-scheme:light; --bg:#eef3f6; --panel:#ffffff; --panel-raised:#f7fafb;
  --line:#ccd8df; --text:#17232d; --muted:#61727e; --accent:#168ac1;
  --accent-soft:#e7f4fa; --cyan:#168ca5; --amber:#9b6a16; --success:#168468; }
* { box-sizing:border-box; }
html { min-width:320px; background:var(--bg); }
body { margin:0; min-width:320px; min-height:100vh; min-height:100dvh; overflow-x:hidden; color:var(--text);
  background-color:var(--bg); background-image:linear-gradient(#168ac10a 1px,transparent 1px),linear-gradient(90deg,#168ac10a 1px,transparent 1px);
  background-size:32px 32px;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
header { position:sticky; top:0; z-index:20; height:68px; display:flex; justify-content:space-between; align-items:center; padding:0 20px;
  border-bottom:1px solid var(--line); background:#f9fbfcf2; backdrop-filter:blur(12px); }
.brand { display:flex; align-items:baseline; gap:12px; min-width:0; }
h1 { margin:0; font-size:20px; font-weight:660; letter-spacing:-.02em; }
.brand span { color:#6e8491; font:600 9px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace; letter-spacing:.14em; white-space:nowrap; }
.status { max-width:60%; display:flex; align-items:center; gap:8px; padding:7px 10px; border:1px solid #b9cbd4;
  color:#425762; background:#fff; border-radius:4px; font:600 10px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace; text-align:right; }
.status::before { content:""; flex:0 0 auto; width:6px; height:6px; border-radius:50%; background:var(--success); box-shadow:0 0 8px #16846866; }
main { display:grid; grid-template-columns:minmax(0,1fr) 330px; align-items:start; gap:12px; padding:12px; min-height:calc(100dvh - 68px); }
.viewer-panel { position:sticky; top:80px; height:calc(100dvh - 92px); min-height:620px; display:flex; flex-direction:column;
  border:1px solid var(--line); border-radius:8px; overflow:hidden; background:#0c1721; box-shadow:0 8px 28px #39516114; }
.viewer-controls { position:relative; z-index:4; flex:0 0 auto; display:grid; gap:8px; padding:0 10px 9px;
  border-bottom:1px solid var(--line); background:#f8fafb; color:var(--text); }
.viewer-controls[hidden] { display:none; }
.control-head { display:flex; align-items:center; justify-content:space-between; gap:14px; min-width:0; padding-top:1px; }
.control-title { color:var(--muted); font-size:11px; font-weight:650; }
.viewport { position:relative; flex:1 1 auto; min-height:0; overflow:hidden; display:flex;
  flex-direction:column; background:#0c1721; }
.scene-stage { position:relative; flex:1 1 0; width:100%; min-width:0; min-height:0; overflow:hidden;
  overflow:clip; contain:strict; isolation:isolate; clip-path:inset(0); background:#0c1721; }
#scene,#scene-overlay { position:absolute; inset:0; width:100%; height:100%; max-width:100%; max-height:100%;
  min-width:0; min-height:0; display:block; }
#scene { cursor:grab; background:#0c1721; }
#scene-overlay { z-index:1; pointer-events:none; }
#scene:active { cursor:grabbing; }
.data-load-status { position:absolute; z-index:5; top:14px; left:50%; transform:translateX(-50%); max-width:min(760px,calc(100% - 28px));
  padding:8px 11px; border:1px solid #496f9a; border-radius:5px; background:#101a25f2; color:#d7e6f7;
  font-size:11px; font-weight:650; text-align:center; }
.data-load-status.error { border-color:#9e7535; background:#241c11f2; color:#f3d89e; }
.data-load-status[hidden] { display:none; }
.toolbar { display:flex; align-items:center; gap:5px; flex-wrap:wrap; }
.toolbar button { min-width:36px; padding:5px 8px; }
.viz-modes { display:flex; gap:0; margin:0; padding:0; overflow-x:auto; overflow-y:hidden;
  border:0; border-bottom:1px solid var(--line); color:#43545f; font-size:12px; min-width:0;
  scrollbar-width:thin; scrollbar-color:#a9bdc8 transparent; }
.viz-modes[hidden] { display:none; }
.viz-modes legend { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0,0,0,0); }
.viz-modes label { position:relative; flex:1 0 112px; min-height:43px; display:flex; align-items:center; justify-content:center; padding:7px 8px;
  border:0; border-bottom:2px solid transparent; background:transparent; white-space:nowrap; cursor:pointer; text-align:center; }
.viz-modes label:hover { color:#17232d; background:#eef4f7; }
.viz-modes label:has(input:checked) { border-bottom-color:var(--accent); color:#0a638d; background:var(--accent-soft); }
.viz-modes label:focus-within { outline:2px solid #168ac155; outline-offset:-2px; }
.viz-modes input { position:absolute; width:1px; height:1px; opacity:0; pointer-events:none; }
.display-options { display:flex; align-items:center; justify-content:flex-end; gap:6px 10px; flex-wrap:wrap;
  margin:0; padding:0; border:0; color:#4d5d67; font-size:11px; }
.display-options[hidden] { display:none; }
.display-options legend { color:var(--muted); padding:0; margin-right:2px; font-size:11px; font-weight:650; }
.display-options label { display:flex; align-items:center; gap:4px; padding:4px 2px; white-space:nowrap; cursor:pointer; }
.display-options label[hidden] { display:none; }
.display-options input { margin:0; accent-color:var(--accent); }
.display-options button { padding:5px 8px; font-size:10px; }
.vertical-sections,.horizontal-sections { display:flex; align-items:center; justify-content:center; gap:7px; flex-wrap:wrap;
  margin:0; padding:7px 9px; border:1px solid var(--line); border-radius:5px; background:#fff; color:#40515d;
  font-size:11px; }
.vertical-sections[hidden],.horizontal-sections[hidden] { display:none; }
.vertical-sections legend,.horizontal-sections legend { color:var(--cyan); padding:0 5px; font-weight:650; }
.vertical-sections label,.horizontal-sections label { display:flex; align-items:center; gap:5px; min-height:28px; padding:4px 9px;
  border:1px solid #c9d5dc; border-radius:4px; background:#f7fafb; white-space:nowrap; cursor:pointer; }
.vertical-sections label:hover,.horizontal-sections label:hover { border-color:#68a7c4; }
.vertical-sections label:has(input:checked),.horizontal-sections label:has(input:checked) { border-color:var(--accent); color:#0a638d; background:var(--accent-soft); }
.vertical-sections input,.horizontal-sections input { margin:0; accent-color:var(--cyan); }
.vertical-sections button,.horizontal-sections button { padding:5px 9px; font-size:10px; }
#vertical-section-state,#horizontal-section-state { color:var(--amber); font-variant-numeric:tabular-nums; font-weight:700; }
.flow-controls { position:relative; z-index:3; flex:0 0 auto; min-height:46px; margin:0 12px 4px;
  display:flex; align-items:center; justify-content:center; gap:9px; padding:7px 10px;
  border:1px solid #304651; border-radius:5px; background:#09121aed; color:#a9bfd0;
  font-size:11px; flex-wrap:wrap; }
.flow-controls[hidden] { display:none; }
.flow-controls button { min-width:72px; padding:5px 8px; border-color:#45606d; color:#e1edf2; background:#132532; }
.flow-controls button:hover { color:#fff; background:#1b3343; border-color:#6b94a8; }
.flow-controls button.active { color:#fff; background:#176e91; border-color:#4fb8d8; }
.flow-controls input[type="range"] { width:min(330px,36vw); accent-color:var(--cyan); }
.flow-controls select { border:1px solid #344653; border-radius:4px; color:#dcecf2; background:#121e28;
  padding:5px 7px; font-weight:700; }
#flow-time { color:var(--cyan); min-width:92px; font-variant-numeric:tabular-nums; font-weight:750; }
#flow-sampling { color:#a9bfd0; white-space:nowrap; }
#flow-duration,#video-duration { color:var(--amber); min-width:92px; font-variant-numeric:tabular-nums; font-weight:750; }
button { border:1px solid #bccbd3; border-radius:4px; color:#31434f; background:#fff;
  padding:7px 10px; font-weight:650; cursor:pointer; transition:border-color .15s,background .15s,color .15s; }
button:hover { color:#075f85; background:#eef7fb; border-color:#68a7c4; }
button.active { color:#fff; background:#167dac; border-color:#167dac; }
.canvas-note { position:relative; z-index:2; flex:0 0 auto; margin:0 12px 10px; padding:6px 9px; background:#09121ad1;
  border:1px solid #253844; border-radius:4px; color:#9fb2c0; font-size:11px; }
.figure-preview { position:absolute; inset:0; padding:18px; background:#0a1118; z-index:1; }
.figure-preview img,.figure-preview video { width:100%; height:calc(100% - 76px); object-fit:contain; display:block; }
.video-controls { min-height:34px; display:flex; align-items:center; justify-content:center; gap:8px;
  color:#a9bfd0; font-size:11px; padding-top:6px; white-space:nowrap; flex-wrap:wrap; }
.video-controls[hidden] { display:none; }
.video-controls button { padding:4px 7px; font-size:10px; }
.video-controls input[type="range"] { width:min(220px,28vw); accent-color:var(--cyan); }
.video-controls select { border:1px solid #344653; border-radius:4px; color:#dcecf2; background:#121e28;
  padding:5px 7px; font-weight:700; }
#video-time { color:var(--cyan); min-width:180px; font-variant-numeric:tabular-nums; font-weight:750; }
#video-semantics { overflow:hidden; text-overflow:ellipsis; }
.artifact-tabs { display:flex; justify-content:center; gap:7px; padding-top:8px; }
.temperature-chart-panel,.convergence-chart-panel { position:absolute; inset:0; z-index:7; display:flex; flex-direction:column; gap:8px;
  padding:12px; background:#f7fafcfa; }
.temperature-chart-panel[hidden],.convergence-chart-panel[hidden] { display:none; }
.temperature-chart-toolbar,.convergence-chart-toolbar { display:flex; align-items:center; gap:7px; flex-wrap:wrap; color:var(--muted); font-size:11px; }
.temperature-chart-toolbar button,.convergence-chart-toolbar button { padding:6px 9px; font-size:10px; }
.temperature-chart-toolbar select,.convergence-chart-toolbar select { border:1px solid #bccbd3; border-radius:4px; color:#31434f; background:#fff;
  padding:6px 8px; font-weight:700; }
.temperature-chart-canvas-wrap,.convergence-chart-canvas-wrap { flex:1 1 auto; min-height:280px; overflow:hidden; }
#temperature-chart-canvas,#convergence-chart-canvas { width:100%; height:100%; display:block; }
#temperature-chart-status,#convergence-chart-status { min-height:20px; color:#536874; font-size:11px; font-variant-numeric:tabular-nums; }
.temperature-export-card dl { margin-bottom:11px; }
#temperature-chart-open,#convergence-chart-open { width:100%; border-color:#1685b3; color:#fff; background:#1685b3; }
.mesh-run-card label { display:block; margin-bottom:5px; color:var(--muted); font-size:10px; font-weight:700; }
.mesh-run-card select { width:100%; margin-bottom:11px; padding:8px; border:1px solid #b9cbd4; border-radius:4px; color:#243946; background:#f8fbfc; font-weight:700; }
.mesh-run-card dl,.convergence-card dl { margin-bottom:11px; }
aside { min-width:0; overflow:visible; padding-right:3px; }
.card { padding:14px 15px; margin-bottom:10px; background:#fff; border:1px solid var(--line); border-radius:6px; box-shadow:0 2px 8px #3951610a; }
.card h2 { margin:0 0 11px; padding-bottom:8px; border-bottom:1px solid #e1e8ec; font-size:13px; font-weight:650; color:var(--text); }
.card p { color:var(--muted); font-size:12px; line-height:1.55; margin:5px 0; }
.card-title-row { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:11px; padding-bottom:9px; border-bottom:1px solid #e1e8ec; }
.card-title-row h2 { margin:0; padding:0; border:0; }
.result-badge { display:inline-flex; align-items:center; gap:5px; padding:4px 7px; border-radius:999px;
  color:#526873; background:#edf2f4; font-size:9px; font-weight:750; letter-spacing:.04em; }
.result-badge::before { content:""; width:6px; height:6px; border-radius:50%; background:#8ba0aa; }
.validated .result-badge { color:#126a57; background:#e7f6f0; }
.validated .result-badge::before { background:var(--success); box-shadow:0 0 7px #16846855; }
.kpi-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:7px; }
.kpi-grid article { min-width:0; padding:10px; border:1px solid #dce6eb; border-radius:5px; background:#f7fafb; }
.kpi-grid span,.kpi-grid small { display:block; overflow:hidden; color:#71838d; font-size:9px; white-space:nowrap; text-overflow:ellipsis; }
.kpi-grid strong { display:block; margin:3px 0 2px; color:#142a36; font-size:17px; line-height:1.15; font-variant-numeric:tabular-nums; letter-spacing:-.02em; }
.kpi-grid article:first-child strong { color:#087c9a; }
.kpi-grid article:nth-child(2) strong { color:#b04d1e; }
.result-meta { margin-top:10px !important; padding:8px 9px; border-radius:4px; color:#536874 !important; background:#eef4f6; }
.result-meta strong { color:#176b5a; }
.detail-card { padding:0; }
.detail-card summary { position:relative; padding:13px 38px 13px 15px; list-style:none; color:#334852; font-size:12px; font-weight:680; cursor:pointer; user-select:none; }
.detail-card summary::-webkit-details-marker { display:none; }
.detail-card summary::after { content:"+"; position:absolute; top:50%; right:15px; transform:translateY(-50%); color:#71838d; font-size:17px; font-weight:400; }
.detail-card[open] summary::after { content:"−"; }
.detail-card[open] summary { border-bottom:1px solid #e1e8ec; }
.detail-body { padding:13px 15px 15px; }
.warning { border-left:3px solid #8d6d36; }
.warning strong { color:var(--amber); }
.warning.validated { border-color:var(--line); border-left-color:var(--success); background:#f5fbf8; }
.warning.validated strong { color:#15745f; }
dl { margin:0; display:grid; grid-template-columns:minmax(76px,.72fr) minmax(0,1.28fr); gap:7px 12px; font-size:12px; }
dt { min-width:0; color:var(--muted); word-break:keep-all; overflow-wrap:normal; }
dd { min-width:0; margin:0; text-align:right; overflow-wrap:anywhere; word-break:keep-all; font-variant-numeric:tabular-nums; font-weight:700; }
.cell-grid { display:grid; grid-template-columns:repeat(8,1fr); gap:5px; }
.cell-grid button { min-width:0; padding:6px 2px; text-align:center; border:1px solid #c9d8df; border-radius:3px;
  color:#346477; background:#f4f9fb; font-size:10px; }
.cell-grid button:hover { border-color:#168ac1; color:#075f85; background:#e5f5fb; }
.cell-grid button.active { border-color:#05080a; color:#fff; background:#05080a; box-shadow:0 0 0 2px #ffffffb8; }
.cell-toast { position:fixed; z-index:50; top:82px; right:18px; max-width:min(320px,calc(100vw - 36px));
  padding:10px 13px; border:1px solid #38a5ca; border-radius:5px; color:#eaf9ff; background:#0b2230ee;
  box-shadow:0 10px 28px #07131c42; font-size:12px; font-weight:700; }
.cell-toast[hidden] { display:none; }
.figure-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:5px; }
.figure-grid button { padding:6px 2px; text-align:center; border:1px solid #d8cfbc; border-radius:3px;
  color:#846b38; background:#fffaf0; font-size:10px; }
.figure-grid button.ready { border-color:#97bccb; color:#1a627c; background:#eef8fb; }
footer { min-height:32px; padding:6px 19px 8px; color:#617d91; font-size:10px; overflow:hidden; white-space:nowrap; }
@media (max-width:1180px) {
  main { grid-template-columns:minmax(0,1fr) 300px; }
  .control-head { align-items:flex-start; flex-direction:column; gap:7px; }
  .display-options { justify-content:flex-start; }
}
@media (max-width:1050px) {
  header { height:auto; min-height:76px; gap:12px; padding:14px 18px; }
  .status { max-width:48%; text-align:right; }
  main { grid-template-columns:1fr; padding:10px; }
  .viewer-panel { position:relative; top:auto; width:100%; height:min(78dvh,760px); min-height:620px; }
  aside { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; padding-right:0; }
  .result-card { grid-column:1/-1; }
  .card { margin-bottom:0; }
}
@media (max-width:680px) {
  header { align-items:flex-start; flex-direction:column; min-height:0; gap:7px; padding:10px 12px; }
  .brand { width:100%; justify-content:space-between; gap:7px; }
  .brand span { font-size:8px; letter-spacing:.09em; }
  .status { width:100%; max-width:100%; text-align:left; }
  main { gap:8px; padding:6px; }
  .viewer-panel { height:82dvh; min-height:620px; }
  .viewer-controls { padding:0 6px 7px; }
  .viz-modes label { flex-basis:128px; }
  .control-head { width:100%; overflow-x:auto; }
  .toolbar,.display-options { min-width:max-content; }
  .flow-controls { margin:0 6px 4px; display:grid; grid-template-columns:auto minmax(110px,1fr) auto; gap:6px; }
  .flow-controls input[type="range"] { width:100%; }
  .flow-controls > label { display:none; }
  #flow-play { grid-column:1; }
  #flow-seek { grid-column:2; }
  #flow-rate { grid-column:3; }
  #head-rate { grid-column:1; grid-row:2; }
  #flow-duration,#flow-sampling,#current-mode { display:none; }
  #flow-time { grid-column:2/-1; grid-row:2; text-align:center; }
  #port-state { grid-column:1/-1; text-align:center; }
  .canvas-note { margin:0 6px 6px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  aside { grid-template-columns:1fr; }
}
'''


JAVASCRIPT = r'''
"use strict";
const model = window.CFD_TWIN_MODEL;
const initialParams=new URLSearchParams(window.location.search);
const canvas = document.getElementById("scene");
const ctx = canvas.getContext("2d");
const sceneStage = document.querySelector(".scene-stage");
const overlayCanvas = document.getElementById("scene-overlay");
const overlayCtx = overlayCanvas.getContext("2d");
const viewerControls = document.querySelector(".viewer-controls");
const meshRuns=model.mesh_runs||[];
const requestedMeshRun=initialParams.get("mesh");
let activeMeshRunId=meshRuns.some(run=>run.id===requestedMeshRun)?requestedMeshRun:(meshRuns.find(run=>run.data_ready)?.id||meshRuns[0]?.id||"precision");
const flow=(model.interactive_cfd_by_mesh||{})[activeMeshRunId]||model.interactive_cfd||{enabled:false};
const temperatureGraphs=flow.temperature_graphs||null;
const convergenceBundle=model.convergence||{available:false};
const convergenceHistories=convergenceBundle.histories?.length?convergenceBundle.histories:[convergenceBundle];
let convergence=convergenceHistories.find(history=>history.id===convergenceBundle.default_history_id)||convergenceHistories[0]||{available:false};
const meshRunSelect=document.getElementById("mesh-run-select");
const meshRunDetail=document.getElementById("mesh-run-detail");
const meshRunNote=document.getElementById("mesh-run-note");
const sections = flow.sections || {};
const directXYVelocity = flow.direct_xy_velocity || null;
const flowControls = document.getElementById("flow-controls");
const flowPlay = document.getElementById("flow-play");
const flowSeek = document.getElementById("flow-seek");
const flowRate = document.getElementById("flow-rate");
const headRate = document.getElementById("head-rate");
const flowDuration = document.getElementById("flow-duration");
const flowTime = document.getElementById("flow-time");
const flowSampling = document.getElementById("flow-sampling");
const portState = document.getElementById("port-state");
const dataLoadStatus = document.getElementById("data-load-status");
const modeFieldset = document.getElementById("viz-modes");
const modeInputs = Array.from(document.querySelectorAll('input[name="viz-mode"]'));
const displayOptions = document.getElementById("display-options");
const smoothSectionInput = document.getElementById("smooth-section");
const section3DInput = document.getElementById("section-3d");
const section3DOption = document.getElementById("section-3d-option");
const horizontalSections = document.getElementById("horizontal-sections");
const horizontalSectionInputs = Array.from(document.querySelectorAll('input[name="horizontal-section"]'));
const horizontalSectionState = document.getElementById("horizontal-section-state");
const requestedHorizontalCut=initialParams.get("zcut");
if(requestedHorizontalCut){const requestedCutInput=horizontalSectionInputs.find(input=>input.value===requestedHorizontalCut);if(requestedCutInput){for(const input of horizontalSectionInputs)input.checked=input===requestedCutInput;}}
const verticalSections = document.getElementById("vertical-sections");
const verticalSectionInputs = Array.from(document.querySelectorAll('input[name="vertical-section"]'));
const verticalSectionState = document.getElementById("vertical-section-state");
const currentMode = document.getElementById("current-mode");
const canvasNote = document.getElementById("canvas-note");
const cellToast = document.getElementById("cell-toast");
const dashboard={
  outlet:document.getElementById("kpi-outlet-temperature"),cellMaximum:document.getElementById("kpi-cell-maximum"),
  inletVelocity:document.getElementById("kpi-inlet-velocity"),pressureDrop:document.getElementById("kpi-pressure-drop"),
  outletTime:document.getElementById("kpi-outlet-time"),cellTime:document.getElementById("kpi-cell-time")
};
const visibility={cells:true,flow:true,particles:true,section:true,grid:false,enclosure:true};
const optionInputs={
  cells:document.getElementById("show-cells"),flow:document.getElementById("show-flow"),
  particles:document.getElementById("show-particles"),section:document.getElementById("show-section"),
  grid:document.getElementById("show-grid"),enclosure:document.getElementById("show-enclosure")
};
let yaw = 2.18, pitch = 0.52, zoom = 1, dragging = false, lastX = 0, lastY = 0;
let highlightedCellId=null,cellHighlightTimer=null,cellExtremaCache={frame:-1,value:null};
const requestedMode=initialParams.get("mode");
if(requestedMode){const requestedInput=modeInputs.find(input=>input.value===requestedMode);if(requestedInput){for(const input of modeInputs)input.checked=input===requestedInput;}}
const requestedTime=Number(initialParams.get("t"));
let simulationTime = Number.isFinite(requestedTime)?Math.max(0,Math.min(1800,requestedTime)):0, flowPlaying = Boolean(flow.enabled)&&initialParams.get("autoplay")!=="0", previousFrameTime = 0, sceneRenderAccumulator = 0, overlayRenderAccumulator = 0, lastSceneFrame = -1, lastFlowClockSecond = -1;
let visualMode = modeInputs.find(input=>input.checked)?.value || "combined";
const box = model.enclosure_mm;
const centre = [box.x/2, box.y/2, box.z/2];
const actualOneSecond=flow.actual_one_second||null;
const actualDatasets=new Map();
const actualDatasetPromises=new Map();
const actualDatasetErrors=new Map();

function setDataLoadStatus(message,state="loading"){
  dataLoadStatus.textContent=message;
  dataLoadStatus.classList.toggle("error",state==="error");
  dataLoadStatus.hidden=!message;
}
function base64Bytes(payload){
  const binary=atob(payload),bytes=new Uint8Array(binary.length);
  for(let index=0;index<binary.length;index++)bytes[index]=binary.charCodeAt(index);
  return bytes;
}
async function portableDatasetBuffer(name,specification){
  window.CFD_TWIN_PACKED_DATA=window.CFD_TWIN_PACKED_DATA||{};
  if(!window.CFD_TWIN_PACKED_DATA[name])await new Promise((resolve,reject)=>{
    const script=document.createElement("script");
    script.src=specification.portable_file;
    script.onload=resolve;
    script.onerror=()=>reject(new Error(`${specification.portable_file}: local data script missing`));
    document.head.appendChild(script);
  });
  const packed=window.CFD_TWIN_PACKED_DATA[name];
  if(!packed)throw new Error(`${name}: local packed payload missing`);
  if(typeof DecompressionStream!=="function")throw new Error("이 브라우저는 로컬 CFD 압축 해제를 지원하지 않습니다. 최신 Chrome, Edge 또는 Safari를 사용하십시오.");
  const compressed=base64Bytes(packed.payload);
  const stream=new Blob([compressed]).stream().pipeThrough(new DecompressionStream("gzip"));
  const buffer=await new Response(stream).arrayBuffer();
  delete window.CFD_TWIN_PACKED_DATA[name];
  return buffer;
}
async function actualDatasetBuffer(name,specification){
  if(location.protocol==="file:")return portableDatasetBuffer(name,specification);
  const response=await fetch(specification.file,{cache:"no-store"});
  if(!response.ok)throw new Error(`${specification.file}: HTTP ${response.status}`);
  return response.arrayBuffer();
}

async function loadActualDataset(name){
  if(!actualOneSecond)return null;
  if(actualDatasets.has(name))return actualDatasets.get(name);
  if(actualDatasetPromises.has(name))return actualDatasetPromises.get(name);
  const specification=actualOneSecond.datasets?.[name];
  if(!specification)return null;
  actualDatasetErrors.delete(name);
  const promise=actualDatasetBuffer(name,specification).then(buffer=>{
    const encoded=new Uint16Array(buffer);
    const expected=specification.frame_count*specification.valid_count;
    if(encoded.length!==expected)throw new Error(`${name}: expected ${expected} samples, found ${encoded.length}`);
    const lookup=new Int32Array(specification.point_count);lookup.fill(-1);
    specification.valid_indices.forEach((pointIndex,validIndex)=>{lookup[pointIndex]=validIndex;});
    const dataset={specification,encoded,lookup};actualDatasets.set(name,dataset);return dataset;
  }).catch(error=>{
    console.error(error);actualDatasetErrors.set(name,String(error?.message||error));return null;
  });
  actualDatasetPromises.set(name,promise);return promise;
}
function actualDatasetValue(name,pointIndex){
  const dataset=actualDatasets.get(name);if(!dataset)return null;
  const validIndex=dataset.lookup[pointIndex];if(validIndex<0)return null;
  const frame=clamp(Math.round(simulationTime),0,dataset.specification.frame_count-1);
  const encoded=dataset.encoded[frame*dataset.specification.valid_count+validIndex];
  const scale=actualOneSecond.temperature_encoding;
  return scale.minimum_k+encoded*scale.quantum_k-273.15;
}

function dot(a,b){ return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]; }
function cameraBasis(){
  const cp=Math.cos(pitch), sp=Math.sin(pitch), cy=Math.cos(yaw), sy=Math.sin(yaw);
  const direction=[cp*cy,cp*sy,sp];
  const right=[-sy,cy,0];
  const up=[-sp*cy,-sp*sy,cp];
  return {direction,right,up};
}
function projected(point,basis,scale,cx,cy){
  const v=[point[0]-centre[0],point[1]-centre[1],point[2]-centre[2]];
  return {x:cx+dot(v,basis.right)*scale,y:cy-dot(v,basis.up)*scale,d:dot(v,basis.direction)};
}
function cylinderFaces(cell,segments=24,axialSegments=1){
  const faces=[], lower=cell.z-cell.length/2, upper=cell.z+cell.length/2;
  const rings=[];
  for(let axial=0;axial<=axialSegments;axial++){
    const z=lower+cell.length*axial/axialSegments,ring=[];
    for(let i=0;i<segments;i++){
      const a=2*Math.PI*i/segments;
      ring.push([cell.x+cell.radius*Math.cos(a),cell.y+cell.radius*Math.sin(a),z]);
    }
    rings.push(ring);
  }
  for(let axial=0;axial<axialSegments;axial++)for(let i=0;i<segments;i++){
    const j=(i+1)%segments;
    faces.push({points:[rings[axial][i],rings[axial][j],rings[axial+1][j],rings[axial+1][i]],kind:`${cell.kind||"cell"}-side`,id:cell.id,surfaceIndex:axial*segments+i,angularIndex:i,axialIndex:axial});
  }
  const bottomCentre=[cell.x,cell.y,lower],topCentre=[cell.x,cell.y,upper],sideCount=segments*axialSegments;
  for(let i=0;i<segments;i++){
    const j=(i+1)%segments;
    faces.push({points:[bottomCentre,rings[0][j],rings[0][i]],kind:`${cell.kind||"cell"}-cap`,id:cell.id,surfaceIndex:sideCount+i});
    faces.push({points:[topCentre,rings[axialSegments][i],rings[axialSegments][j]],kind:`${cell.kind||"cell"}-cap`,id:cell.id,surfaceIndex:sideCount+segments+i});
  }
  return faces;
}
const cellSurface=flow.cell_surface||null;
const cellFaces=model.cells.flatMap(cell=>cylinderFaces(cell,cellSurface?.angular_segments||24,cellSurface?.axial_segments||1));
const portFaces=model.ports.flatMap(port=>cylinderFaces(port,24,1));
const faces=cellFaces.concat(portFaces);
const cellById=new Map(model.cells.map(cell=>[cell.id,cell]));
const entityById=new Map([...model.cells,...model.ports].map(entity=>[entity.id,entity]));
let projectedFaceCache={key:null,faces:null};
const vertices=model.outline_mm.map(p=>[p[0],p[1],0]).concat(model.outline_mm.map(p=>[p[0],p[1],box.z]));
const n=model.outline_mm.length,edges=[];
for(let i=0;i<n;i++){const j=(i+1)%n;edges.push([i,j],[i+n,j+n],[i,i+n]);}

function clamp(value,low=0,high=1){return Math.max(low,Math.min(high,value));}
function velocityDisplayRange(){
  const range=flow.velocity_range_m_s;
  if(Array.isArray(range)&&range.length===2&&Number.isFinite(range[1])&&range[1]>range[0])return range;
  return [0,Math.max(1e-9,Number(flow.velocity_max_m_s)||0)];
}
function velocityDisplayMaximum(){return Math.max(1e-9,velocityDisplayRange()[1]);}
function jet(value){
  const t=clamp(value),r=clamp(1.5-Math.abs(4*t-3)),g=clamp(1.5-Math.abs(4*t-2)),b=clamp(1.5-Math.abs(4*t-1));
  return [Math.round(255*r),Math.round(255*g),Math.round(255*b)];
}
function activeMeshRun(){return meshRuns.find(run=>run.id===activeMeshRunId)||meshRuns[0]||null;}
function faceVisible(face,basis){
  const entity=entityById.get(face.id);if(!entity)return true;
  let normal;
  if(face.kind.endsWith("-cap")){
    normal=[0,0,face.points[0][2]>=entity.z?1:-1];
  }else{
    const centreX=face.points.reduce((sum,point)=>sum+point[0],0)/face.points.length;
    const centreY=face.points.reduce((sum,point)=>sum+point[1],0)/face.points.length;
    const length=Math.hypot(centreX-entity.x,centreY-entity.y)||1;
    normal=[(centreX-entity.x)/length,(centreY-entity.y)/length,0];
  }
  return dot(normal,basis.direction)>-0.02;
}
function projectedFaces(basis,scale,cx,cy,w,h){
  const key=[yaw,pitch,zoom,w,h,scale,cx,cy].map(value=>Number(value).toFixed(5)).join("|");
  if(projectedFaceCache.key===key&&projectedFaceCache.faces)return projectedFaceCache.faces;
  const visible=faces.filter(face=>faceVisible(face,basis)).map(face=>{
    const points=face.points.map(point=>projected(point,basis,scale,cx,cy));
    return {face,points,depth:points.reduce((sum,point)=>sum+point.d,0)/points.length};
  }).sort((a,b)=>a.depth-b.depth);
  projectedFaceCache={key,faces:visible};return visible;
}
let directXYVelocityCache=null;
const sectionRasterCache=new WeakMap();
function sectionRaster(section,field,range){
  let fields=sectionRasterCache.get(section);if(!fields){fields={};sectionRasterCache.set(section,fields);}
  let raster=fields[field];
  if(!raster){
    const canvas=document.createElement("canvas");canvas.width=section.columns;canvas.height=section.rows;
    const context=canvas.getContext("2d");raster={canvas,context,pixels:context.createImageData(section.columns,section.rows),fixedReady:false};fields[field]=raster;
  }
  if(field==="velocity"&&raster.fixedReady)return raster.canvas;
  raster.pixels.data.fill(0);
  for(let row=0;row<section.rows;row++)for(let column=0;column<section.columns;column++){
    const index=row*section.columns+column,value=field==="temperature"?sectionTemperature(section,index):section.velocity_m_s[index];
    if(!Number.isFinite(value))continue;
    const colour=jet((value-range[0])/Math.max(1e-12,range[1]-range[0])),pixel=4*(row*section.columns+(section.columns-1-column));
    raster.pixels.data[pixel]=colour[0];raster.pixels.data[pixel+1]=colour[1];raster.pixels.data[pixel+2]=colour[2];raster.pixels.data[pixel+3]=255;
  }
  raster.context.putImageData(raster.pixels,0,0);if(field==="velocity")raster.fixedReady=true;return raster.canvas;
}
function buildDirectXYVelocityCache(){
  if(directXYVelocityCache||!directXYVelocity)return directXYVelocityCache;
  const factor=8,width=Math.round(box.x*factor),height=Math.round(box.y*factor);
  const target=document.createElement("canvas");target.width=width;target.height=height;
  const targetContext=target.getContext("2d"),range=velocityDisplayRange();
  targetContext.imageSmoothingEnabled=false;
  for(let index=0;index<directXYVelocity.triangles.length;index++){
    const triangle=directXYVelocity.triangles[index],value=directXYVelocity.velocity_m_s[index];
    if(!Number.isFinite(value))continue;
    const colour=jet((value-range[0])/Math.max(1e-12,range[1]-range[0]));
    const points=triangle.map(pointIndex=>directXYVelocity.points_xy_mm[pointIndex]);
    targetContext.beginPath();
    targetContext.moveTo((box.x-points[0][0])*factor,points[0][1]*factor);
    targetContext.lineTo((box.x-points[1][0])*factor,points[1][1]*factor);
    targetContext.lineTo((box.x-points[2][0])*factor,points[2][1]*factor);
    targetContext.closePath();targetContext.fillStyle=`rgb(${colour[0]},${colour[1]},${colour[2]})`;targetContext.fill();
  }
  directXYVelocityCache=target;return target;
}
function drawXYPlaneTexture(image,section,basis,scale,cx,cy,alpha=.86){
  if(!image||!section)return false;
  const z=section.origin_mm[2];
  const topLeft=projected([box.x,0,z],basis,scale,cx,cy);
  const topRight=projected([0,0,z],basis,scale,cx,cy);
  const bottomLeft=projected([box.x,box.y,z],basis,scale,cx,cy);
  ctx.save();ctx.globalAlpha=alpha;ctx.imageSmoothingEnabled=true;ctx.imageSmoothingQuality="high";
  ctx.transform(
    (topRight.x-topLeft.x)/image.width,(topRight.y-topLeft.y)/image.width,
    (bottomLeft.x-topLeft.x)/image.height,(bottomLeft.y-topLeft.y)/image.height,
    topLeft.x,topLeft.y
  );
  ctx.drawImage(image,0,0);ctx.restore();return true;
}
function drawDirectXYVelocity3D(section,basis,scale,cx,cy){
  const image=buildDirectXYVelocityCache();
  if(!drawXYPlaneTexture(image,section,basis,scale,cx,cy,.9))return false;
  ctx.fillStyle="rgba(232,242,250,.96)";ctx.font="700 10px ui-monospace,monospace";
  ctx.fillText(`PRECISION MESH ACTUAL VALUES · ${directXYVelocity.triangle_count.toLocaleString()} CUT TRIANGLES · 3D · NO PADDING`,96,46);
  return true;
}
function directXYLayout(w,h){
  const plotLeft=96,plotRight=Math.max(plotLeft+160,w-28),plotTop=58,plotBottom=Math.max(plotTop+160,h-104);
  const plotWidth=plotRight-plotLeft,plotHeight=plotBottom-plotTop;
  const scale=Math.min(plotWidth/box.x,plotHeight/box.y)*zoom;
  const width=box.x*scale,height=box.y*scale;
  return {
    left:plotLeft+(plotWidth-width)/2,
    top:plotTop+(plotHeight-height)/2,
    width,height,scale,plotLeft,plotTop,plotRight,plotBottom
  };
}
function drawDirectXYGeometry(layout){
  ctx.strokeStyle="rgba(205,225,238,.86)";ctx.lineWidth=1.1;ctx.beginPath();
  for(let index=0;index<model.outline_mm.length;index++){
    const point=model.outline_mm[index],x=layout.left+(box.x-point[0])*layout.scale,y=layout.top+point[1]*layout.scale;
    if(index===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
  }
  ctx.closePath();ctx.stroke();
  for(const cell of model.cells){
    const x=layout.left+(box.x-cell.x)*layout.scale,y=layout.top+cell.y*layout.scale,radius=cell.radius*layout.scale;
    ctx.beginPath();ctx.arc(x,y,radius,0,2*Math.PI);ctx.fillStyle="rgba(7,16,27,1)";ctx.fill();
    ctx.strokeStyle="rgba(218,231,240,.94)";ctx.lineWidth=1;ctx.stroke();
  }
}
function drawDirectXYVelocity(w,h,showScalar=true){
  const image=buildDirectXYVelocityCache();if(!image)return false;
  const layout=directXYLayout(w,h);
  ctx.save();ctx.beginPath();ctx.rect(layout.plotLeft,layout.plotTop,layout.plotRight-layout.plotLeft,layout.plotBottom-layout.plotTop);ctx.clip();
  ctx.fillStyle="rgba(7,16,27,.98)";ctx.fillRect(layout.left,layout.top,layout.width,layout.height);
  if(showScalar){ctx.imageSmoothingEnabled=true;ctx.drawImage(image,layout.left,layout.top,layout.width,layout.height);}
  drawDirectXYGeometry(layout);
  ctx.restore();
  ctx.fillStyle="rgba(232,242,250,.96)";ctx.font="700 10px ui-monospace,monospace";
  ctx.fillText(`PRECISION MESH ACTUAL VALUES · ${directXYVelocity.triangle_count.toLocaleString()} CUT TRIANGLES · NO PADDING`,layout.plotLeft,layout.plotTop-12);
  ctx.fillStyle="rgba(145,168,188,.98)";ctx.font="650 10px system-ui";
  ctx.fillText("FROZEN-FLOW U(x) · 전체 유동장 공통 색상범위 · 이동 마커 속도는 |U|와 공통 배율에 비례",layout.plotLeft,layout.plotBottom+22);
  return true;
}
function drawDirectXYGrid(section,field,w,h,showScalar=true){
  if(!section)return false;
  const layout=directXYLayout(w,h),range=field==="temperature"?section.temperature_range_c:velocityDisplayRange();
  ctx.save();ctx.beginPath();ctx.rect(layout.plotLeft,layout.plotTop,layout.plotRight-layout.plotLeft,layout.plotBottom-layout.plotTop);ctx.clip();
  ctx.fillStyle="rgba(7,16,27,.98)";ctx.fillRect(layout.left,layout.top,layout.width,layout.height);
  if(showScalar&&range){
    const stepX=Math.abs(section.column_step_mm[0])*layout.scale,stepY=Math.abs(section.row_step_mm[1])*layout.scale;
    if(smoothSectionInput.checked){
      const fieldCanvas=sectionRaster(section,field,range);ctx.imageSmoothingEnabled=true;ctx.imageSmoothingQuality="high";
      ctx.drawImage(fieldCanvas,layout.left,layout.top,layout.width,layout.height);
    }else for(let index=0;index<section.columns*section.rows;index++){
      const value=field==="temperature"?sectionTemperature(section,index):section.velocity_m_s[index];if(!Number.isFinite(value))continue;
      const point=planePoint(section,index),x=layout.left+(box.x-point[0])*layout.scale,y=layout.top+point[1]*layout.scale;
      const colour=jet((value-range[0])/Math.max(1e-12,range[1]-range[0]));
      ctx.fillStyle=`rgb(${colour[0]},${colour[1]},${colour[2]})`;ctx.fillRect(x-stepX/2,y-stepY/2,stepX+.35,stepY+.35);
    }
    if(visibility.grid){
      ctx.strokeStyle="rgba(225,238,246,.11)";ctx.lineWidth=.3;
      for(let column=0;column<=section.columns;column++){const x=layout.left+column*layout.width/section.columns;ctx.beginPath();ctx.moveTo(x,layout.top);ctx.lineTo(x,layout.top+layout.height);ctx.stroke();}
      for(let row=0;row<=section.rows;row++){const y=layout.top+row*layout.height/section.rows;ctx.beginPath();ctx.moveTo(layout.left,y);ctx.lineTo(layout.left+layout.width,y);ctx.stroke();}
    }
  }
  drawDirectXYGeometry(layout);ctx.restore();
  ctx.fillStyle="rgba(232,242,250,.96)";ctx.font="700 10px ui-monospace,monospace";
  const renderLabel=smoothSectionInput.checked?"DISPLAY SMOOTH · VALID NEIGHBOURS ONLY":"EXACT SAMPLE TILES";
  ctx.fillText(`PRECISION MESH DIRECT SAMPLES · ${section.columns} × ${section.rows} · ${section.label} · ${renderLabel} · NO INVALID PADDING`,layout.plotLeft,layout.plotTop-12);
  return true;
}
function headDisplayScale(){return Number(headRate?.value||.5);}
function sectionMarkerPhase(speed,index){
  const displayTravelM=Math.max(.12,Math.hypot(box.x,box.y,box.z)/1000);
  return ((simulationTime*headDisplayScale()*Math.max(0,speed)/displayTravelM+index*.381966)%1+1)%1;
}
function drawDirectXYVelocityVectors(layout,movingOnly=false,targetContext=ctx){
  const section=horizontalSection();if(!section?.velocity_vectors_m_s)return;
  const limit=velocityDisplayMaximum();
  targetContext.save();targetContext.beginPath();targetContext.rect(layout.plotLeft,layout.plotTop,layout.plotRight-layout.plotLeft,layout.plotBottom-layout.plotTop);targetContext.clip();
  for(let row=2;row<section.rows-2;row+=5)for(let column=2;column<section.columns-2;column+=5){
    const index=row*section.columns+column,vector=section.velocity_vectors_m_s[index];if(!vector)continue;
    const magnitude=Math.hypot(...vector),planarMagnitude=Math.hypot(vector[0],vector[1]);
    if(magnitude<limit*.012||planarMagnitude<1e-10)continue;
    const point=planePoint(section,index),x=layout.left+(box.x-point[0])*layout.scale,y=layout.top+point[1]*layout.scale;
    const directionX=-vector[0]/planarMagnitude,directionY=vector[1]/planarMagnitude;
    const length=6+12*Math.sqrt(clamp(magnitude/limit)),endX=x+directionX*length,endY=y+directionY*length;
    if(!movingOnly){
      targetContext.strokeStyle="rgba(242,249,253,.72)";targetContext.lineWidth=.85;targetContext.beginPath();targetContext.moveTo(x,y);targetContext.lineTo(endX,endY);targetContext.stroke();
      const angle=Math.atan2(directionY,directionX),head=3.2;
      targetContext.beginPath();targetContext.moveTo(endX,endY);targetContext.lineTo(endX-head*Math.cos(angle-.55),endY-head*Math.sin(angle-.55));
      targetContext.moveTo(endX,endY);targetContext.lineTo(endX-head*Math.cos(angle+.55),endY-head*Math.sin(angle+.55));targetContext.stroke();
    }else if(visibility.particles){
      const phase=sectionMarkerPhase(magnitude,index);
      for(let tail=2;tail>=0;tail--){
        const fraction=((phase-tail*.075)%1+1)%1,mx=x+directionX*length*fraction,my=y+directionY*length*fraction;
        targetContext.beginPath();targetContext.arc(mx,my,tail===0?2.05:1.25,0,2*Math.PI);
        targetContext.fillStyle=tail===0?"rgba(255,255,255,.98)":`rgba(106,221,239,${.18+(2-tail)*.2})`;targetContext.fill();
      }
    }
  }
  targetContext.restore();
}
function timeSeriesValue(values){
  if(!values||values.length===0)return null;
  if(actualOneSecond)return values[clamp(Math.round(simulationTime),0,values.length-1)];
  const position=clamp(simulationTime/1800)*(values.length-1);
  const left=Math.floor(position),right=Math.min(values.length-1,left+1),weight=position-left;
  return values[left]+weight*(values[right]-values[left]);
}
function thermalValue(id){
  if(!flow.enabled||!flow.temperature_c[id])return null;
  return timeSeriesValue(flow.temperature_c[id]);
}
function surfaceThermalValue(id,index){
  if(actualOneSecond&&cellSurface?.actual_dataset&&Number.isInteger(index)){
    const cellIndex=Number(id.slice(1))-1;
    const samplesPerCell=actualOneSecond.datasets[cellSurface.actual_dataset].samples_per_cell;
    return actualDatasetValue(cellSurface.actual_dataset,cellIndex*samplesPerCell+index);
  }
  const frames=cellSurface?.temperature_frames_c?.[id];
  if(!frames||!Number.isInteger(index))return thermalValue(id);
  const position=clamp(simulationTime/1800)*Math.max(0,frames.length-1);
  const left=Math.floor(position),right=Math.min(frames.length-1,left+1),weight=position-left;
  const a=frames[left]?.[index],b=frames[right]?.[index];
  if(Number.isFinite(a)&&Number.isFinite(b))return a+weight*(b-a);
  return Number.isFinite(a)?a:Number.isFinite(b)?b:thermalValue(id);
}
function meanFinite(values){
  const valid=values.filter(Number.isFinite);return valid.length?valid.reduce((sum,value)=>sum+value,0)/valid.length:null;
}
function surfaceCornerTemperatures(face){
  if(!cellSurface||!Number.isInteger(face.surfaceIndex))return null;
  const segments=cellSurface.angular_segments,rows=cellSurface.axial_segments,sideCount=segments*rows;
  if(face.kind.endsWith("-side")&&Number.isInteger(face.axialIndex)){
    const axial=face.axialIndex,angular=face.angularIndex;
    const sample=(row,column)=>surfaceThermalValue(face.id,clamp(row,0,rows-1)*segments+((column%segments)+segments)%segments);
    return [
      meanFinite([sample(axial,angular),sample(axial-1,angular),sample(axial,angular-1),sample(axial-1,angular-1)]),
      meanFinite([sample(axial,angular),sample(axial-1,angular),sample(axial,angular+1),sample(axial-1,angular+1)]),
      meanFinite([sample(axial,angular),sample(axial+1,angular),sample(axial,angular+1),sample(axial+1,angular+1)]),
      meanFinite([sample(axial,angular),sample(axial+1,angular),sample(axial,angular-1),sample(axial+1,angular-1)])
    ];
  }
  if(face.kind.endsWith("-cap")){
    const top=face.surfaceIndex>=sideCount+segments,base=sideCount+(top?segments:0),angular=face.surfaceIndex-base;
    const sample=column=>surfaceThermalValue(face.id,base+((column%segments)+segments)%segments);
    const centre=meanFinite(Array.from({length:segments},(_,index)=>sample(index)));
    const before=meanFinite([sample(angular-1),sample(angular)]),after=meanFinite([sample(angular),sample(angular+1)]);
    return top?[centre,before,after]:[centre,after,before];
  }
  return null;
}
function surfaceSamplePoint(cell,surfaceIndex){
  const segments=cellSurface.angular_segments,rows=cellSurface.axial_segments,sideCount=segments*rows;
  if(surfaceIndex<sideCount){
    const axial=Math.floor(surfaceIndex/segments),angular=surfaceIndex%segments,angle=2*Math.PI*(angular+.5)/segments;
    return [cell.x+cell.radius*Math.cos(angle),cell.y+cell.radius*Math.sin(angle),cell.z-cell.length/2+cell.length*(axial+.5)/rows];
  }
  const top=surfaceIndex>=sideCount+segments,angular=surfaceIndex-sideCount-(top?segments:0),angle=2*Math.PI*(angular+.5)/segments;
  return [cell.x+.58*cell.radius*Math.cos(angle),cell.y+.58*cell.radius*Math.sin(angle),cell.z+(top?1:-1)*cell.length/2];
}
function cellSurfaceExtrema(){
  const dataset=actualDatasets.get(cellSurface?.actual_dataset);if(!dataset)return null;
  const frame=clamp(Math.round(simulationTime),0,dataset.specification.frame_count-1);
  if(cellExtremaCache.frame===frame)return cellExtremaCache.value;
  const validCount=dataset.specification.valid_count,offset=frame*validCount,scale=actualOneSecond.temperature_encoding;
  let minimum=null,maximum=null;
  for(let validIndex=0;validIndex<validCount;validIndex++){
    const pointIndex=dataset.specification.valid_indices[validIndex],encoded=dataset.encoded[offset+validIndex];
    const value=scale.minimum_k+encoded*scale.quantum_k-273.15;
    const cellIndex=Math.floor(pointIndex/dataset.specification.samples_per_cell),surfaceIndex=pointIndex%dataset.specification.samples_per_cell,cell=model.cells[cellIndex];
    const sample={value,cellId:cell.id,surfaceIndex,point:surfaceSamplePoint(cell,surfaceIndex)};
    if(!minimum||value<minimum.value)minimum=sample;if(!maximum||value>maximum.value)maximum=sample;
  }
  cellExtremaCache={frame,value:{minimum,maximum}};return cellExtremaCache.value;
}
function portTemperature(id){return timeSeriesValue(flow.port_temperature_c?.[id]);}
function portMetrics(id){
  const prefix=id==="IN"?"inlet":"outlet";
  return {
    temperatureC:portTemperature(id),
    velocityMS:flow.port_flow?.[`${prefix}_mean_velocity_m_s`],
    pressurePa:flow.port_flow?.[`${prefix}_area_average_pressure_pa`]
  };
}
function updateDashboardMetrics(inletMetrics=portMetrics("IN"),outletMetrics=portMetrics("OUT")){
  const timeIndex=clamp(Math.round(simulationTime),0,1800),timeLabel=`${timeIndex} s`;
  const outletTemperature=temperatureGraphs?.outlet_mass_weighted_average_c?.[timeIndex]??outletMetrics.temperatureC;
  const cellMaximum=temperatureGraphs?.battery_mesh_maximum_c?.[timeIndex];
  dashboard.outlet.textContent=Number.isFinite(outletTemperature)?`${outletTemperature.toFixed(3)} °C`:"—";
  dashboard.cellMaximum.textContent=Number.isFinite(cellMaximum)?`${cellMaximum.toFixed(3)} °C`:"—";
  dashboard.inletVelocity.textContent=Number.isFinite(inletMetrics.velocityMS)?`${inletMetrics.velocityMS.toFixed(3)} m/s`:"—";
  dashboard.pressureDrop.textContent=Number.isFinite(flow.port_flow?.pressure_drop_pa)?`${flow.port_flow.pressure_drop_pa.toFixed(1)} Pa`:"—";
  dashboard.outletTime.textContent=timeLabel;
  dashboard.cellTime.textContent=`23개 셀 · ${timeLabel}`;
}
function updatePortStateText(inletMetrics=portMetrics("IN"),outletMetrics=portMetrics("OUT")){
  const temperatureSemantics=actualOneSecond?"T 원본(무보정)":"T 표시보정 +"+flow.temperature_display_offset_c.toFixed(3)+" K";
  portState.textContent=`${temperatureSemantics} · IN T ${inletMetrics.temperatureC.toFixed(3)} °C / Ū ${inletMetrics.velocityMS.toFixed(3)} m/s / p̄ ${(inletMetrics.pressurePa/1000).toFixed(3)} kPa · OUT T ${outletMetrics.temperatureC.toFixed(3)} °C / Ū ${outletMetrics.velocityMS.toFixed(3)} m/s / p̄ ${(outletMetrics.pressurePa/1000).toFixed(3)} kPa · Δp ${flow.port_flow.pressure_drop_pa.toFixed(3)} Pa`;
  updateDashboardMetrics(inletMetrics,outletMetrics);
}
function drawPortCallout(anchor,id,metrics,accent,w,h){
  if(!Number.isFinite(metrics.temperatureC)||!Number.isFinite(metrics.velocityMS)||!Number.isFinite(metrics.pressurePa))return;
  const boxWidth=178,boxHeight=73,legendClearance=104;
  const preferredX=anchor.x<w/2?anchor.x+15:anchor.x-boxWidth-15;
  const left=clamp(preferredX,legendClearance,w-boxWidth-8);
  const preferredY=anchor.y<h/2?anchor.y+15:anchor.y-boxHeight-15;
  const top=clamp(preferredY,148,h-boxHeight-92);
  const targetX=anchor.x<left?left:anchor.x>left+boxWidth?left+boxWidth:anchor.x;
  const targetY=anchor.y<top?top:anchor.y>top+boxHeight?top+boxHeight:anchor.y;
  ctx.save();
  ctx.strokeStyle=accent;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(anchor.x,anchor.y);ctx.lineTo(targetX,targetY);ctx.stroke();
  ctx.fillStyle="rgba(5,14,24,.92)";ctx.fillRect(left,top,boxWidth,boxHeight);
  ctx.strokeStyle=accent;ctx.lineWidth=1;ctx.strokeRect(left+.5,top+.5,boxWidth-1,boxHeight-1);
  const temperatureLabel=actualOneSecond?"RAW T (293.15 K)":`T CAL +${flow.temperature_display_offset_c.toFixed(3)} K`;
  ctx.fillStyle=accent;ctx.font="800 10px system-ui";ctx.fillText(`${id==="IN"?"INLET":"OUTLET"} · ${temperatureLabel}`,left+8,top+15);
  ctx.fillStyle="rgba(239,247,252,.98)";ctx.font="700 10px ui-monospace,monospace";
  ctx.fillText(`T   ${metrics.temperatureC.toFixed(3)} °C`,left+8,top+32);
  ctx.fillText(`Ū   ${metrics.velocityMS.toFixed(3)} m/s`,left+8,top+48);
  ctx.fillText(`p̄   ${(metrics.pressurePa/1000).toFixed(3)} kPa`,left+8,top+64);
  ctx.restore();
}
function thermalFaceGradient(face,points,range,fallback){
  // Thousands of per-face CanvasGradient allocations landing on the exact
  // one-second boundary caused the visible periodic hitch. During playback,
  // keep the actual mesh-face value and reserve the cosmetic intra-face
  // gradient for paused inspection.
  if(flowPlaying)return fallback;
  const temperatures=surfaceCornerTemperatures(face);
  if(!smoothSectionInput.checked||!temperatures||!temperatures.every(Number.isFinite))return fallback;
  let cold=0,hot=0;for(let index=1;index<temperatures.length;index++){if(temperatures[index]<temperatures[cold])cold=index;if(temperatures[index]>temperatures[hot])hot=index;}
  if(Math.abs(temperatures[hot]-temperatures[cold])<1e-10)return fallback;
  const gradient=ctx.createLinearGradient(points[cold].x,points[cold].y,points[hot].x,points[hot].y);
  for(const [position,index] of [[0,cold],[.5,-1],[1,hot]]){
    const temperature=index<0?meanFinite(temperatures):temperatures[index],colour=jet((temperature-range[0])/Math.max(1e-9,range[1]-range[0]));
    gradient.addColorStop(position,`rgba(${colour[0]},${colour[1]},${colour[2]},.95)`);
  }
  return gradient;
}
function drawCellExtremumCallout(sample,title,accent,basis,scale,cx,cy,w,h,upper){
  if(!sample)return;
  const anchor=projected(sample.point,basis,scale,cx,cy),boxWidth=164,boxHeight=49;
  const preferredX=anchor.x<w*.58?anchor.x+14:anchor.x-boxWidth-14,left=clamp(preferredX,104,w-boxWidth-10);
  const preferredY=upper?anchor.y-boxHeight-16:anchor.y+16,top=clamp(preferredY,92,h-boxHeight-78);
  const targetX=anchor.x<left?left:anchor.x>left+boxWidth?left+boxWidth:anchor.x,targetY=anchor.y<top?top:anchor.y>top+boxHeight?top+boxHeight:anchor.y;
  ctx.save();ctx.strokeStyle=accent;ctx.lineWidth=1.2;ctx.beginPath();ctx.moveTo(anchor.x,anchor.y);ctx.lineTo(targetX,targetY);ctx.stroke();
  ctx.beginPath();ctx.arc(anchor.x,anchor.y,5,0,2*Math.PI);ctx.fillStyle="rgba(6,16,25,.95)";ctx.fill();ctx.strokeStyle=accent;ctx.lineWidth=2;ctx.stroke();
  ctx.fillStyle="rgba(5,14,24,.94)";ctx.fillRect(left,top,boxWidth,boxHeight);ctx.strokeStyle=accent;ctx.lineWidth=1;ctx.strokeRect(left+.5,top+.5,boxWidth-1,boxHeight-1);
  ctx.fillStyle=accent;ctx.font="800 10px system-ui";ctx.fillText(`${title} · ${sample.cellId}`,left+8,top+16);
  ctx.fillStyle="rgba(239,247,252,.98)";ctx.font="700 11px ui-monospace,monospace";ctx.fillText(`${sample.value.toFixed(4)} °C · t ${Math.round(simulationTime)} s`,left+8,top+35);ctx.restore();
}
function prepareFlowPathPhysics(path){
  if(path.physics)return path.physics;
  const cumulativeTimeS=[0],segmentLengthM=[];
  for(let index=1;index<path.p.length;index++){
    const lengthM=Math.hypot(...path.p[index].map((value,axis)=>value-path.p[index-1][axis]))/1000;
    const speed0=Math.max(1e-6,path.s[index-1]),speed1=Math.max(1e-6,path.s[index]),delta=speed1-speed0;
    const durationS=lengthM<=0?0:Math.abs(delta)<1e-9?lengthM/speed0:lengthM*Math.log(speed1/speed0)/delta;
    segmentLengthM.push(lengthM);cumulativeTimeS.push(cumulativeTimeS.at(-1)+durationS);
  }
  path.physics={cumulativeTimeS,segmentLengthM,transitTimeS:cumulativeTimeS.at(-1)};
  return path.physics;
}
function physicalFlowPoint(path,elapsedTimeS){
  const physics=prepareFlowPathPhysics(path),total=physics.transitTimeS;
  if(!Number.isFinite(total)||total<=0)return {point:path.p[0],speed:path.s[0]};
  const target=((elapsedTimeS%total)+total)%total,times=physics.cumulativeTimeS;
  let low=0,high=times.length-1;
  while(low+1<high){const middle=(low+high)>>1;if(times[middle]<=target)low=middle;else high=middle;}
  const left=low,right=Math.min(path.p.length-1,left+1),localTime=target-times[left],lengthM=physics.segmentLengthM[left];
  const speed0=Math.max(1e-6,path.s[left]),speed1=Math.max(1e-6,path.s[right]),delta=speed1-speed0;
  const weight=clamp(lengthM<=0?0:Math.abs(delta)<1e-9?localTime*speed0/lengthM:speed0*Math.expm1(delta*localTime/lengthM)/delta);
  const point=[0,1,2].map(axis=>path.p[left][axis]+weight*(path.p[right][axis]-path.p[left][axis]));
  return {point,speed:path.s[left]+weight*(path.s[right]-path.s[left])};
}
function drawScalarLegend(w,h,title,range,unit,referenceRange=null){
  if(!flow.enabled||!range)return;
  const [low,high]=range,x=18,y=Math.max(168,h*.19),height=Math.min(320,h*.43),width=12;
  const gradient=ctx.createLinearGradient(0,y+height,0,y);
  for(let index=0;index<=8;index++){const t=index/8,[r,g,b]=jet(t);gradient.addColorStop(t,`rgb(${r},${g},${b})`);}
  ctx.fillStyle=gradient;ctx.fillRect(x,y,width,height);ctx.strokeStyle="rgba(230,240,250,.7)";ctx.strokeRect(x,y,width,height);
  ctx.fillStyle="rgba(232,242,250,.96)";ctx.font="700 11px system-ui";ctx.fillText(title,x-1,y-24);
  const sectionSmooth=smoothSectionInput.checked&&(visualMode.startsWith("xy-")||visualMode.startsWith("xz-"));
  const scaleLabel=actualOneSecond?(sectionSmooth?"RAW SAMPLES · DISPLAY SMOOTH":"RAW CFD · NO INTERPOLATION"):`DISPLAY CAL. +${flow.temperature_display_offset_c.toFixed(3)} K`;
  ctx.fillStyle="rgba(145,168,188,.96)";ctx.font="600 9px system-ui";ctx.fillText(unit==="C"?scaleLabel:"ACTUAL CFD · GLOBAL U SCALE",x-1,y-10);
  const intervals=unit==="C"?18:9;
  ctx.font="700 9px ui-monospace,monospace";
  for(let index=0;index<=intervals;index++){
    const fraction=index/intervals,position=y+height*fraction,value=high-(high-low)*fraction;
    ctx.strokeStyle="rgba(235,243,249,.88)";ctx.lineWidth=.8;ctx.beginPath();ctx.moveTo(x+width,position);ctx.lineTo(x+width+7,position);ctx.stroke();
    ctx.fillStyle="rgba(232,242,250,.96)";ctx.fillText(value.toFixed(3),x+width+10,position+3);
    if(index<intervals)for(let minor=1;minor<5;minor++){
      const minorY=position+height/intervals*minor/5;ctx.strokeStyle="rgba(220,233,242,.58)";ctx.beginPath();ctx.moveTo(x+width,minorY);ctx.lineTo(x+width+3,minorY);ctx.stroke();
    }
  }
  ctx.fillStyle="rgba(232,242,250,.96)";ctx.font="700 10px system-ui";ctx.fillText(`[${unit}]`,x-1,y+height+18);
  if(referenceRange){ctx.fillStyle="rgba(255,189,91,.98)";ctx.font="700 9px ui-monospace,monospace";ctx.fillText(`PAPER REF ${referenceRange[0].toFixed(3)}–${referenceRange[1].toFixed(3)}`,x-1,y+height+34);}
}
function planePoint(section,index){
  const row=Math.floor(index/section.columns),column=index%section.columns;
  return [0,1,2].map(axis=>section.origin_mm[axis]+column*section.column_step_mm[axis]+row*section.row_step_mm[axis]);
}
function sectionTemperature(section,index){
  if(actualOneSecond&&section.actual_dataset)return actualDatasetValue(section.actual_dataset,index);
  const frames=section.temperature_frames_c||[],position=clamp(simulationTime/1800)*Math.max(0,frames.length-1);
  const left=Math.floor(position),right=Math.min(frames.length-1,left+1),weight=position-left;
  const a=frames[left]?.[index],b=frames[right]?.[index];
  if(Number.isFinite(a)&&Number.isFinite(b))return a+weight*(b-a);
  return Number.isFinite(a)?a:Number.isFinite(b)?b:null;
}
function sectionFieldValue(section,field,index){
  return field==="temperature"?sectionTemperature(section,index):section.velocity_m_s[index];
}
function bilinear(values,u,v){
  return values[0]*(1-u)*(1-v)+values[1]*u*(1-v)+values[2]*u*v+values[3]*(1-u)*v;
}
function bilinearPoint(points,u,v){
  return [0,1,2].map(axis=>bilinear(points.map(point=>point[axis]),u,v));
}
function drawDirectSampleTiles(section,field,basis,scale,cx,cy,range,alpha){
  const quads=[];
  for(let index=0;index<section.columns*section.rows;index++){
    const value=sectionFieldValue(section,field,index);if(!Number.isFinite(value))continue;
    const centrePoint=planePoint(section,index),columnHalf=section.column_step_mm.map(value=>.5*value),rowHalf=section.row_step_mm.map(value=>.5*value);
    const points=[
      centrePoint.map((value,axis)=>value-columnHalf[axis]-rowHalf[axis]),
      centrePoint.map((value,axis)=>value+columnHalf[axis]-rowHalf[axis]),
      centrePoint.map((value,axis)=>value+columnHalf[axis]+rowHalf[axis]),
      centrePoint.map((value,axis)=>value-columnHalf[axis]+rowHalf[axis])
    ];
    const depth=points.reduce((sum,point)=>sum+projected(point,basis,scale,cx,cy).d,0)/4;
    quads.push({points,value,depth});
  }
  quads.sort((a,b)=>a.depth-b.depth);
  for(const quad of quads){
    const normalised=(quad.value-range[0])/Math.max(1e-9,range[1]-range[0]),[r,g,b]=jet(normalised),points=quad.points.map(point=>projected(point,basis,scale,cx,cy));
    ctx.beginPath();ctx.moveTo(points[0].x,points[0].y);for(let index=1;index<points.length;index++)ctx.lineTo(points[index].x,points[index].y);ctx.closePath();
    ctx.fillStyle=`rgba(${r},${g},${b},${alpha})`;ctx.fill();
    if(visibility.grid){ctx.strokeStyle="rgba(225,238,246,.13)";ctx.lineWidth=.25;ctx.stroke();}
  }
}
function drawSection(section,field,basis,scale,cx,cy,rangeOverride=null,alpha=.82,subdivisions=3){
  if(!section)return;
  const range=rangeOverride||(field==="temperature"?section.temperature_range_c:velocityDisplayRange());
  const directlySampled=field==="velocity"||(field==="temperature"&&actualOneSecond&&section.actual_dataset);
  if(directlySampled&&!smoothSectionInput.checked){drawDirectSampleTiles(section,field,basis,scale,cx,cy,range,alpha);return;}
  const quads=[];
  for(let row=0;row<section.rows-1;row++)for(let column=0;column<section.columns-1;column++){
    const ids=[row*section.columns+column,row*section.columns+column+1,(row+1)*section.columns+column+1,(row+1)*section.columns+column];
    const values=ids.map(index=>sectionFieldValue(section,field,index));
    if(!values.every(Number.isFinite))continue;
    const points=ids.map(index=>planePoint(section,index));
    for(let subRow=0;subRow<subdivisions;subRow++)for(let subColumn=0;subColumn<subdivisions;subColumn++){
      const u0=subColumn/subdivisions,u1=(subColumn+1)/subdivisions,v0=subRow/subdivisions,v1=(subRow+1)/subdivisions;
      const subPoints=[bilinearPoint(points,u0,v0),bilinearPoint(points,u1,v0),bilinearPoint(points,u1,v1),bilinearPoint(points,u0,v1)];
      const value=bilinear(values,.5*(u0+u1),.5*(v0+v1));
      const depth=subPoints.reduce((sum,point)=>sum+projected(point,basis,scale,cx,cy).d,0)/subPoints.length;
      quads.push({points:subPoints,value,depth});
    }
  }
  quads.sort((a,b)=>a.depth-b.depth);
  for(const quad of quads){
    const normalised=(quad.value-range[0])/Math.max(1e-9,range[1]-range[0]),[r,g,b]=jet(normalised);
    const points=quad.points.map(point=>projected(point,basis,scale,cx,cy));
    ctx.beginPath();ctx.moveTo(points[0].x,points[0].y);for(let index=1;index<points.length;index++)ctx.lineTo(points[index].x,points[index].y);ctx.closePath();
    ctx.fillStyle=`rgba(${r},${g},${b},${alpha})`;ctx.fill();
    if(visibility.grid){ctx.strokeStyle="rgba(225,238,246,.15)";ctx.lineWidth=.32;ctx.stroke();}
  }
}
function horizontalSectionKey(){
  return horizontalSectionInputs.find(input=>input.checked)?.value||"xy";
}
function horizontalSection(){
  return sections[horizontalSectionKey()]||sections.xy;
}
function verticalSelection(){
  return verticalSectionInputs.filter(input=>input.checked).map(input=>sections[input.value]).filter(Boolean);
}
function verticalRange(field){
  if(field==="velocity")return velocityDisplayRange();
  const selected=verticalSelection(),ranges=selected.map(section=>field==="temperature"?section.temperature_range_c:section.velocity_range_m_s).filter(Boolean);
  if(!ranges.length)return null;
  return [Math.min(...ranges.map(range=>range[0])),Math.max(...ranges.map(range=>range[1]))];
}
function selectedSectionVelocityPeak(mode=visualMode){
  if(mode.startsWith("xy-"))return horizontalSection()?.velocity_range_m_s?.[1]??null;
  if(mode.startsWith("xz-")){
    const peaks=verticalSelection().map(section=>section.velocity_range_m_s?.[1]).filter(Number.isFinite);
    return peaks.length?Math.max(...peaks):null;
  }
  return null;
}
function drawSectionLabel(section,basis,scale,cx,cy){
  const point=projected(section.origin_mm,basis,scale,cx,cy),label=section.label||"XZ";
  ctx.save();ctx.font="700 9px ui-monospace,monospace";
  const width=ctx.measureText(label).width+8;
  ctx.fillStyle="rgba(5,14,24,.82)";ctx.fillRect(point.x+4,point.y-15,width,15);
  ctx.fillStyle="rgba(255,215,149,.98)";ctx.fillText(label,point.x+8,point.y-4);ctx.restore();
}
function drawSectionVelocity(section,basis,scale,cx,cy,movingOnly=false,targetContext=ctx){
  if(!section||!section.velocity_vectors_m_s)return;
  const limit=velocityDisplayMaximum(),stride=Math.max(2,Math.ceil(Math.max(section.rows,section.columns)/24));
  for(let row=1;row<section.rows-1;row+=stride)for(let column=1;column<section.columns-1;column+=stride){
    const index=row*section.columns+column,vector=section.velocity_vectors_m_s[index];
    if(!vector)continue;
    const magnitude=Math.hypot(...vector);if(magnitude<limit*.012)continue;
    const point=planePoint(section,index),length=3+8*Math.sqrt(clamp(magnitude/limit));
    const direction=vector.map(value=>value/magnitude),end=point.map((value,axis)=>value+direction[axis]*length);
    const a=projected(point,basis,scale,cx,cy),b=projected(end,basis,scale,cx,cy);
    if(!movingOnly){
      targetContext.strokeStyle="rgba(245,251,255,.82)";targetContext.lineWidth=.8;targetContext.beginPath();targetContext.moveTo(a.x,a.y);targetContext.lineTo(b.x,b.y);targetContext.stroke();
    }else if(visibility.particles){
      const phase=sectionMarkerPhase(magnitude,index),moving=point.map((value,axis)=>value+direction[axis]*length*phase);
      const marker=projected(moving,basis,scale,cx,cy);targetContext.beginPath();targetContext.arc(marker.x,marker.y,2,0,2*Math.PI);targetContext.fillStyle="rgba(255,255,255,.97)";targetContext.fill();
      targetContext.strokeStyle="rgba(47,157,220,.9)";targetContext.lineWidth=1;targetContext.stroke();
    }
  }
}
function drawFlow(paths,basis,scale,cx,cy){
  if(!flow.enabled)return;
  for(const path of paths){
    for(let index=1;index<path.p.length;index++){
      const a=projected(path.p[index-1],basis,scale,cx,cy),b=projected(path.p[index],basis,scale,cx,cy);
      const speed=0.5*(path.s[index-1]+path.s[index]),strength=clamp(speed/velocityDisplayMaximum());
      const [r,g,bColor]=jet(strength);
      ctx.strokeStyle=visualMode==="velocity-streamlines"
        ?`rgba(${Math.min(255,Math.round(.45*r+80))},${Math.min(255,Math.round(.45*g+90))},${Math.min(255,Math.round(.45*bColor+130))},${.58+.24*strength})`
        :"rgba(100,123,237,.72)";
      ctx.lineWidth=visualMode==="velocity-streamlines"?.8+1.25*strength:.9;ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
    }
  }
}
function drawFlowParticles(paths,basis,scale,cx,cy,targetContext=ctx){
  if(!flow.enabled||!visibility.particles)return;
  for(let index=0;index<paths.length;index++){
    const path=paths[index],physics=prepareFlowPathPhysics(path),baseTime=simulationTime*headDisplayScale()+index*.61803398875*physics.transitTimeS;
    const trailStepS=Math.min(.006,physics.transitTimeS/80);
    for(let tail=4;tail>=0;tail--){
      const sample=physicalFlowPoint(path,baseTime-tail*trailStepS),screen=projected(sample.point,basis,scale,cx,cy);
      const strength=clamp(sample.speed/velocityDisplayMaximum()),[r,g,bColor]=jet(strength);
      const alpha=.13+(4-tail)*.18,radius=1.2+(4-tail)*.62;
      targetContext.beginPath();targetContext.arc(screen.x,screen.y,radius,0,2*Math.PI);
      targetContext.fillStyle=tail===0?`rgba(255,255,255,.96)`:`rgba(${r},${g},${bColor},${alpha})`;targetContext.fill();
      if(tail===0){targetContext.strokeStyle=`rgba(${r},${g},${bColor},.95)`;targetContext.lineWidth=1.3;targetContext.stroke();}
    }
  }
}

function resize(){
  const rect=sceneStage.getBoundingClientRect(),cssPixels=Math.max(1,rect.width*rect.height);
  const ratio=Math.max(.8,Math.min(window.devicePixelRatio||1,1.75,Math.sqrt(3200000/cssPixels)));
  for(const [target,targetContext] of [[canvas,ctx],[overlayCanvas,overlayCtx]]){
    target.width=Math.max(1,Math.floor(rect.width*ratio));target.height=Math.max(1,Math.floor(rect.height*ratio));
    targetContext.setTransform(ratio,0,0,ratio,0,0);
  }
  draw();
}
function drawScene(){
  // Clear the complete backing store in device coordinates. The control rows
  // can change the CSS height before ResizeObserver updates the bitmap; using
  // CSS dimensions here left a stale lower strip that looked like a duplicate.
  ctx.save();ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,canvas.width,canvas.height);ctx.restore();
  const w=canvas.clientWidth,h=canvas.clientHeight;
  const meshRun=activeMeshRun();
  if(meshRun&&!meshRun.data_ready){
    ctx.fillStyle="#0c1721";ctx.fillRect(0,0,w,h);
    ctx.textAlign="center";ctx.fillStyle="rgba(99,215,229,.98)";ctx.font="800 18px system-ui";
    ctx.fillText(meshRun.label,w/2,h/2-34);
    ctx.fillStyle="rgba(232,242,250,.92)";ctx.font="650 13px system-ui";
    ctx.fillText(meshRun.mesh_ready?"메시는 생성됐지만 CFD 결과 필드는 아직 없습니다.":"메시 및 CFD 계산 전입니다.",w/2,h/2-5);
    ctx.fillStyle="rgba(145,168,188,.95)";ctx.font="600 11px system-ui";
    ctx.fillText("다른 메시의 결과를 이 옵션에 대신 표시하지 않습니다.",w/2,h/2+21);
    ctx.textAlign="left";return;
  }
  const basis=cameraBasis(), scale=Math.min(w/(box.x*1.65),h/(box.y*1.65))*zoom;
  const cx=w/2,cy=h/2+8;
  const thermalCells=["combined","cell-temperature","xy-temperature","xz-temperature"].includes(visualMode);
  const thermalDisplay=flow.enabled&&(thermalCells||visualMode==="xy-temperature");
  const velocityDisplay=flow.enabled&&["velocity-streamlines","xy-velocity","xz-velocity"].includes(visualMode);
  const sectionMode=visualMode.startsWith("xy-")||visualMode.startsWith("xz-");
  if(visualMode.startsWith("xy-")&&!section3DInput.checked&&horizontalSection()){
    const selectedHorizontal=horizontalSection();
    if(visualMode==="xy-temperature")drawDirectXYGrid(selectedHorizontal,"temperature",w,h,visibility.section);
    else if(smoothSectionInput.checked)drawDirectXYGrid(selectedHorizontal,"velocity",w,h,visibility.section);
    else if(horizontalSectionKey()==="xy"&&directXYVelocity)drawDirectXYVelocity(w,h,visibility.section);
    else drawDirectXYGrid(selectedHorizontal,"velocity",w,h,visibility.section);
    if(visibility.flow)drawDirectXYVelocityVectors(directXYLayout(w,h));
    if(flow.enabled){
      updatePortStateText();
      if(visibility.section&&visualMode==="xy-temperature")drawScalarLegend(w,h,"Temperature · XY",selectedHorizontal.temperature_range_c,"C",flow.paper_temperature_ranges_c?.xy);
      if(visibility.section&&visualMode==="xy-velocity")drawScalarLegend(w,h,"Velocity · shared scale",velocityDisplayRange(),"m/s");
      updateFlowClock();
    }
    return;
  }
  const sorted=projectedFaces(basis,scale,cx,cy,w,h);
  for(const item of sorted){
    const pts=item.points;
    ctx.beginPath(); ctx.moveTo(pts[0].x,pts[0].y); for(let i=1;i<pts.length;i++)ctx.lineTo(pts[i].x,pts[i].y); ctx.closePath();
    const shade=Math.max(0,Math.min(1,(item.depth+90)/180));
    const isPort=item.face.kind.startsWith("port");
    if(!isPort&&!visibility.cells)continue;
    const temperature=isPort?null:surfaceThermalValue(item.face.id,item.face.surfaceIndex);
    if(isPort&&thermalDisplay){
      const portT=portTemperature(item.face.id),range=flow.temperature_range_c,normalised=(portT-range[0])/Math.max(1e-9,range[1]-range[0]);
      const [r,g,b]=jet(normalised);ctx.fillStyle=`rgba(${r},${g},${b},.9)`;
    }
    else if(isPort&&velocityDisplay){
      const key=item.face.id==="IN"?"inlet_mean_velocity_m_s":"outlet_mean_velocity_m_s";
      const normalised=flow.port_flow[key]/velocityDisplayMaximum(),[r,g,b]=jet(normalised);
      ctx.fillStyle=`rgba(${r},${g},${b},.88)`;
    }
    else if(isPort){ctx.fillStyle=`rgba(239,147,43,${0.62+0.14*shade})`;}
    else if(thermalCells&&temperature!==null){
      const range=cellSurface?.temperature_range_c||flow.temperature_range_c,normalised=(temperature-range[0])/Math.max(1e-9,range[1]-range[0]);
      const [r,g,b]=jet(normalised);
      const fallback=`rgba(${r},${g},${b},.95)`;ctx.fillStyle=thermalFaceGradient(item.face,pts,range,fallback);
    }else if(sectionMode||visualMode==="velocity-streamlines"){
      ctx.fillStyle=`rgba(12,28,43,${.08+.07*shade})`;
    }else{ctx.fillStyle=item.face.kind.endsWith("cap")?`rgba(79,207,222,${0.66+0.13*shade})`:`rgba(32,112,145,${0.68+0.16*shade})`;}
    ctx.fill();
    const highlighted=!isPort&&item.face.id===highlightedCellId;
    ctx.strokeStyle=highlighted?"rgba(2,5,7,.99)":thermalCells&&!isPort?"rgba(225,238,246,.045)":"rgba(225,238,246,.26)";
    ctx.lineWidth=highlighted?2.4:thermalCells&&!isPort?.22:.55;ctx.stroke();
  }
  if(visibility.section){
    if(visualMode==="xy-temperature"){
      const section=horizontalSection();
      if(smoothSectionInput.checked)drawXYPlaneTexture(sectionRaster(section,"temperature",section.temperature_range_c),section,basis,scale,cx,cy,.88);
      else drawSection(section,"temperature",basis,scale,cx,cy,null,.88,1);
    }
    if(visualMode==="xy-velocity"){
      const section=horizontalSection();
      if(!smoothSectionInput.checked&&horizontalSectionKey()==="xy"&&directXYVelocity)drawDirectXYVelocity3D(section,basis,scale,cx,cy);
      else if(smoothSectionInput.checked)drawXYPlaneTexture(sectionRaster(section,"velocity",velocityDisplayRange()),section,basis,scale,cx,cy,.88);
      else drawSection(section,"velocity",basis,scale,cx,cy,velocityDisplayRange(),.88,1);
    }
    const selectedVertical=verticalSelection(),verticalAlpha=selectedVertical.length>1?.52:.82;
    const verticalSubdivisions=selectedVertical.length>1?2:3;
    if(visualMode==="xz-temperature")for(const section of selectedVertical)drawSection(section,"temperature",basis,scale,cx,cy,verticalRange("temperature"),verticalAlpha,verticalSubdivisions);
    if(visualMode==="xz-velocity")for(const section of selectedVertical)drawSection(section,"velocity",basis,scale,cx,cy,verticalRange("velocity"),verticalAlpha,verticalSubdivisions);
    if(visualMode.startsWith("xz-"))for(const section of selectedVertical)drawSectionLabel(section,basis,scale,cx,cy);
  }
  if(visibility.flow){
    if(visualMode.startsWith("xy-")&&visualMode!=="xy-velocity")drawSectionVelocity(horizontalSection(),basis,scale,cx,cy);
    if(visualMode==="xy-velocity"&&horizontalSectionKey()!=="xy")drawSectionVelocity(horizontalSection(),basis,scale,cx,cy);
    if(visualMode.startsWith("xz-"))for(const section of verticalSelection())drawSectionVelocity(section,basis,scale,cx,cy);
    if(["combined","velocity-streamlines"].includes(visualMode))drawFlow(flow.streamlines||[],basis,scale,cx,cy);
  }
  if(visibility.enclosure&&visualMode!=="xy-velocity"){
    ctx.lineWidth=1.25; ctx.strokeStyle="rgba(164,195,216,.67)";
    for(const edge of edges){
      const a=projected(vertices[edge[0]],basis,scale,cx,cy),b=projected(vertices[edge[1]],basis,scale,cx,cy);
      ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
    }
  }
  if(flow.enabled){
    const inlet=projected([model.ports[0].x,model.ports[0].y,model.ports[0].z+model.ports[0].length/2],basis,scale,cx,cy);
    const outlet=projected([model.ports[1].x,model.ports[1].y,model.ports[1].z-model.ports[1].length/2],basis,scale,cx,cy);
    const inletMetrics=portMetrics("IN"),outletMetrics=portMetrics("OUT");
    if(visualMode!=="xy-velocity"){
      drawPortCallout(inlet,"IN",inletMetrics,"rgba(255,189,91,.98)",w,h);
      drawPortCallout(outlet,"OUT",outletMetrics,"rgba(99,215,229,.98)",w,h);
    }
    if(visualMode==="cell-temperature"&&visibility.cells){
      const extrema=cellSurfaceExtrema();
      if(extrema){
        drawCellExtremumCallout(extrema.maximum,"CELL SURFACE MAX","rgba(255,119,78,.98)",basis,scale,cx,cy,w,h,true);
        drawCellExtremumCallout(extrema.minimum,"CELL SURFACE MIN","rgba(86,216,238,.98)",basis,scale,cx,cy,w,h,false);
      }
    }
    updatePortStateText(inletMetrics,outletMetrics);
    if(["combined","cell-temperature"].includes(visualMode)&&visibility.cells)drawScalarLegend(w,h,"Cell surface T",cellSurface?.temperature_range_c||flow.temperature_range_c,"C",flow.paper_temperature_ranges_c?.cell);
    if(visualMode==="velocity-streamlines"&&visibility.flow)drawScalarLegend(w,h,"Velocity · shared scale",velocityDisplayRange(),"m/s");
    if(visualMode==="xy-temperature"&&visibility.section)drawScalarLegend(w,h,"Temperature · XY",horizontalSection()?.temperature_range_c,"C",flow.paper_temperature_ranges_c?.xy);
    if(visualMode==="xy-velocity"&&visibility.section)drawScalarLegend(w,h,"Velocity · shared scale",velocityDisplayRange(),"m/s");
    if(visualMode==="xz-temperature"&&visibility.section)drawScalarLegend(w,h,"Temperature · XZ",verticalRange("temperature"),"C",flow.paper_temperature_ranges_c?.xz);
    if(visualMode==="xz-velocity"&&visibility.section)drawScalarLegend(w,h,"Velocity · shared scale",velocityDisplayRange(),"m/s");
    updateFlowClock();
  }else{ctx.fillStyle="rgba(255,189,91,.92)";ctx.font="700 11px system-ui";ctx.fillText("Ø19 mm IN / OUT — A-04 assumed centres",16,h-34);}
}
function drawDynamicOverlay(){
  overlayCtx.save();overlayCtx.setTransform(1,0,0,1,0,0);overlayCtx.clearRect(0,0,overlayCanvas.width,overlayCanvas.height);overlayCtx.restore();
  if(!flow.enabled||!visibility.flow||(activeMeshRun()?.id==="single"&&!activeMeshRun()?.data_ready))return;
  if(visualMode.startsWith("xy-")&&!section3DInput.checked&&horizontalSection()){
    drawDirectXYVelocityVectors(directXYLayout(canvas.clientWidth,canvas.clientHeight),true,overlayCtx);
    updateFlowClock();return;
  }
  if((section3DInput.checked&&visualMode.startsWith("xy-"))||visualMode.startsWith("xz-")){
    const w=canvas.clientWidth,h=canvas.clientHeight,basis=cameraBasis(),scale=Math.min(w/(box.x*1.65),h/(box.y*1.65))*zoom;
    const selected=visualMode.startsWith("xy-")?[horizontalSection()]:verticalSelection();
    for(const section of selected)drawSectionVelocity(section,basis,scale,w/2,h/2+8,true,overlayCtx);
    updateFlowClock();return;
  }
  if(!["combined","velocity-streamlines"].includes(visualMode))return;
  const w=canvas.clientWidth,h=canvas.clientHeight,basis=cameraBasis(),scale=Math.min(w/(box.x*1.65),h/(box.y*1.65))*zoom;
  drawFlowParticles(flow.streamlines||[],basis,scale,w/2,h/2+8,overlayCtx);
  updateFlowClock();
}
function draw(){drawScene();lastSceneFrame=Math.round(simulationTime);drawDynamicOverlay();}
canvas.addEventListener("pointerdown",e=>{dragging=true;lastX=e.clientX;lastY=e.clientY;canvas.setPointerCapture(e.pointerId);});
canvas.addEventListener("pointermove",e=>{if(!dragging)return;yaw+=(e.clientX-lastX)*.009;pitch=Math.max(-1.48,Math.min(1.48,pitch-(e.clientY-lastY)*.009));lastX=e.clientX;lastY=e.clientY;draw();});
canvas.addEventListener("pointerup",()=>dragging=false);
canvas.addEventListener("wheel",e=>{e.preventDefault();zoom=Math.max(.45,Math.min(3,zoom*Math.exp(-e.deltaY*.001)));draw();},{passive:false});
const views={px:[0,0],nx:[Math.PI,0],py:[Math.PI/2,0],ny:[-Math.PI/2,0],pz:[0,Math.PI/2-.001],nz:[0,-Math.PI/2+.001],iso:[2.18,.52]};
function selectView(name){
  [yaw,pitch]=views[name];document.querySelectorAll("[data-view]").forEach(button=>button.classList.toggle("active",button.dataset.view===name));draw();
}
document.querySelectorAll("[data-view]").forEach(button=>button.addEventListener("click",()=>selectView(button.dataset.view)));
document.getElementById("reset").addEventListener("click",()=>{zoom=1;selectView("iso");});
const modeDescriptions={
  combined:["종합 화면","셀 온도와 실제 U(x)에 비례해 이동하는 유동 헤드 · 단면과 동일한 U 기준"],
  "cell-temperature":["셀 온도",`배터리 직접 표면 샘플 ${cellSurface?.angular_segments||28} × ${cellSurface?.axial_segments||12}+캡 · 현재 최고/최저점 표시`],
  "velocity-streamlines":["유체 유선","전체 유체 셀 공통 U 범위로 색칠한 고정 유동장과 실제 U(x)에 비례하는 이동 헤드"],
  "xy-temperature":["가로 단면 온도","선택한 높이의 직접 셀 온도와 유동 방향을 표시"],
  "xy-velocity":["가로 단면 유속","선택한 높이의 실제 셀 중심 유속 · 전체 유동장 공통 U 범위 · 보간/패딩 없음"],
  "xz-temperature":["세로 단면 온도","선택한 세로 위치의 직접 셀 온도 단면을 표시"],
  "xz-velocity":["세로 단면 유속","선택한 세로 위치의 유속 크기와 방향 · 전체 유동장 공통 U 범위"]
};
function prepareActualMode(mode){
  if(!actualOneSecond)return;
  const names=[];
  if(["combined","cell-temperature","xz-temperature"].includes(mode)||(mode==="xy-temperature"&&section3DInput.checked))names.push("cell_surface");
  if(mode==="xy-temperature")names.push(horizontalSectionKey());
  if(mode==="xz-temperature")names.push(...verticalSectionInputs.filter(input=>input.checked).map(input=>input.value));
  const pending=names.filter(name=>!actualDatasets.has(name));
  if(!pending.length){setDataLoadStatus("");return;}
  const source=location.protocol==="file:"?"로컬 압축 데이터":"직접 셀 데이터";
  currentMode.textContent=`실제 1 s 셀값 로드 중 · ${pending.join(", ")}`;
  setDataLoadStatus(`결과 데이터 로드 중 · ${source} · ${pending.join(", ")}`);
  Promise.all(pending.map(loadActualDataset)).then(()=>{
    if(visualMode!==mode)return;
    const failed=pending.filter(name=>actualDatasetErrors.has(name));
    if(failed.length){
      currentMode.textContent=`실제값 로드 실패 · ${failed.join(", ")}`;
      const detail=actualDatasetErrors.get(failed[0]);
      setDataLoadStatus(`온도 색상 데이터 로드 실패 · ${failed.join(", ")} · viewer 폴더의 data 파일을 모두 함께 복사하십시오. (${detail})`,"error");
    }else{
      const horizontalPosition=mode.startsWith("xy-")?` · ${horizontalSection()?.label||"Z = 50%"}`:"";
      currentMode.textContent=(modeDescriptions[mode]||modeDescriptions.combined)[0]+horizontalPosition;
      setDataLoadStatus("");
    }
    draw();
  });
}
function updateCanvasNote(mode=visualMode){
  const description=modeDescriptions[mode]||modeDescriptions.combined;
  const horizontalPosition=mode.startsWith("xy-")?` · ${horizontalSection()?.label||"Z = 50%"}`:"";
  const temperatureNote=actualOneSecond?"T 원본 · 시간보간/패딩 없음":`온도 표시보정 +${flow.temperature_display_offset_c.toFixed(3)} K`;
  const sectionRenderNote=mode.startsWith("xy-")||mode.startsWith("xz-")?(smoothSectionInput.checked?" · 실제 샘플 사이 화면만 부드럽게 · 무효영역 채움 없음":" · 실제 샘플 타일 그대로"):"";
  const headScale=headDisplayScale(),headNote=visibility.particles?` · 헤드 ${headScale===1?"1× 실제속도":headScale+"× 표시속도"}`:"";
  const velocityMode=["combined","velocity-streamlines","xy-velocity","xz-velocity"].includes(mode);
  const localPeak=selectedSectionVelocityPeak(mode),velocityNote=velocityMode?` · U 공통범위 0–${velocityDisplayMaximum().toFixed(3)} m/s${Number.isFinite(localPeak)?` · 현재 단면 최고 ${localPeak.toFixed(3)} m/s`:""}`:"";
  const dimensionNote=mode.startsWith("xy-")?` · ${section3DInput.checked?"3D 회전":"평면 보기"}`:"";
  canvasNote.textContent=`${description[1]}${horizontalPosition}${sectionRenderNote}${dimensionNote}${velocityNote}${headNote} · 드래그 회전 · 휠 확대/축소 · ${temperatureNote} · Ū·p̄ 실제 면적평균`;
}
function selectVisualMode(mode){
  visualMode=mode;
  setDataLoadStatus("");
  const description=modeDescriptions[mode]||modeDescriptions.combined;
  const horizontalPosition=mode.startsWith("xy-")?` · ${horizontalSection()?.label||"Z = 50%"}`:"";
  currentMode.textContent=description[0]+horizontalPosition;
  updateCanvasNote(mode);
  prepareActualMode(mode);
  horizontalSections.hidden=!mode.startsWith("xy-");
  verticalSections.hidden=!mode.startsWith("xz-");
  section3DOption.hidden=!mode.startsWith("xy-");
  if(mode.startsWith("xy-"))selectView(section3DInput.checked?"iso":"pz");
  else if(mode.startsWith("xz-"))selectView(verticalSelection().length>1?"iso":"py");
  else selectView("iso");
  requestAnimationFrame(resize);
}
modeFieldset.hidden=!flow.enabled;
for(const input of modeInputs){
  input.disabled=!flow.enabled;
  input.addEventListener("change",()=>{if(input.checked)selectVisualMode(input.value);});
}
function setLayerVisibility(values){
  Object.assign(visibility,values);
  for(const [name,input] of Object.entries(optionInputs))input.checked=visibility[name];
  draw();
}
displayOptions.hidden=!flow.enabled;
horizontalSections.hidden=true;
verticalSections.hidden=true;
for(const [name,input] of Object.entries(optionInputs)){
  input.disabled=!flow.enabled;input.addEventListener("change",()=>{visibility[name]=input.checked;draw();});
}
smoothSectionInput.disabled=!flow.enabled;
smoothSectionInput.addEventListener("change",()=>{directXYVelocityCache=null;updateCanvasNote();draw();});
section3DInput.disabled=!flow.enabled;
section3DInput.addEventListener("change",()=>{prepareActualMode(visualMode);updateCanvasNote();selectView(section3DInput.checked?"iso":"pz");});
headRate.disabled=!flow.enabled;
headRate.addEventListener("change",()=>{updateCanvasNote();drawDynamicOverlay();});
function updateHorizontalSection({changeView=true}={}){
  let selected=horizontalSectionInputs.find(input=>input.checked&&!input.disabled);
  if(!selected){
    selected=horizontalSectionInputs.find(input=>input.value==="xy"&&!input.disabled)||horizontalSectionInputs.find(input=>!input.disabled);
    if(selected)selected.checked=true;
  }
  horizontalSectionState.textContent=selected?(sections[selected.value]?.label||selected.value):"실제 단면 데이터 없음";
  if(visualMode==="xy-temperature")prepareActualMode(visualMode);
  if(changeView&&visualMode.startsWith("xy-"))selectVisualMode(visualMode);else draw();
}
for(const input of horizontalSectionInputs){
  input.disabled=!flow.enabled||!sections[input.value];
  input.addEventListener("change",()=>{if(input.checked)updateHorizontalSection();});
}
document.getElementById("horizontal-centre").addEventListener("click",()=>{
  const centreInput=horizontalSectionInputs.find(input=>input.value==="xy"&&!input.disabled);
  if(centreInput){centreInput.checked=true;updateHorizontalSection();}
});
updateHorizontalSection({changeView:false});
function updateVerticalSections({changeView=true}={}){
  let selected=verticalSectionInputs.filter(input=>input.checked);
  if(!selected.length){
    const centreInput=verticalSectionInputs.find(input=>input.value==="xz50");
    if(centreInput){centreInput.checked=true;selected=[centreInput];}
  }
  verticalSectionState.textContent=selected.map(input=>sections[input.value]?.label||input.value).join(" · ");
  if(visualMode==="xz-temperature")prepareActualMode(visualMode);
  if(changeView&&visualMode.startsWith("xz-"))selectView(selected.length>1?"iso":"py");
  else draw();
}
for(const input of verticalSectionInputs)input.addEventListener("change",()=>updateVerticalSections());
document.getElementById("vertical-centre").addEventListener("click",()=>{
  for(const input of verticalSectionInputs)input.checked=input.value==="xz50";
  updateVerticalSections();
});
document.getElementById("vertical-all").addEventListener("click",()=>{
  for(const input of verticalSectionInputs)input.checked=true;
  updateVerticalSections();
});
updateVerticalSections({changeView:false});
document.getElementById("flow-only").addEventListener("click",()=>{
  const input=modeInputs.find(candidate=>candidate.value==="velocity-streamlines");if(input)input.checked=true;
  setLayerVisibility({cells:false,flow:true,particles:true,section:false,grid:false,enclosure:true});selectVisualMode("velocity-streamlines");
});
document.getElementById("show-all").addEventListener("click",()=>{
  const input=modeInputs.find(candidate=>candidate.value==="combined");if(input)input.checked=true;
  setLayerVisibility({cells:true,flow:true,particles:true,section:true,grid:true,enclosure:true});selectVisualMode("combined");
});
if(flow.enabled)selectVisualMode(visualMode);
flowControls.hidden=!flow.enabled;
function formatWallDuration(seconds){
  const rounded=Math.max(0,Math.round(seconds)),minutes=Math.floor(rounded/60),remainder=rounded%60;
  return `${minutes}:${String(remainder).padStart(2,"0")}`;
}
function updateFlowDuration(){
  const rate=Number(flowRate.value);flowDuration.textContent=`${rate}× 완주 ${formatWallDuration(1800/rate)}`;
}
function updateFlowClock({force=false}={}){
  const second=Math.round(simulationTime);
  if(!force&&second===lastFlowClockSecond)return;
  lastFlowClockSecond=second;
  flowSeek.value=String(second);
  flowTime.textContent=`t = ${second} s / 1800 s`;
}
if(flow.enabled){
  flowSampling.textContent=actualOneSecond?"CFD 원본 1 s · 직접 셀값 · 시간보간/패딩 없음":"화면 1 s · CFD 원본 15 s 선형보간";
  updateFlowClock({force:true});
  flowPlay.textContent=flowPlaying?"❚❚ 정지":"▶ 재생";
  flowPlay.addEventListener("click",()=>{flowPlaying=!flowPlaying;flowPlay.textContent=flowPlaying?"❚❚ 정지":"▶ 재생";draw();});
  flowSeek.addEventListener("input",()=>{simulationTime=Number(flowSeek.value);draw();});
  flowRate.addEventListener("change",updateFlowDuration);updateFlowDuration();
}
function animate(timestamp){
  if(previousFrameTime===0)previousFrameTime=timestamp;
  const elapsed=Math.min(.1,(timestamp-previousFrameTime)/1000);previousFrameTime=timestamp;
  if(flow.enabled&&flowPlaying&&figurePreview.hidden&&activeMeshRun()?.data_ready!==false){
    simulationTime=(simulationTime+elapsed*Number(flowRate.value))%1800;
    sceneRenderAccumulator+=elapsed;overlayRenderAccumulator+=elapsed;updateFlowClock();
    const actualFrame=Math.round(simulationTime);
    const timeVaryingScene=["combined","cell-temperature","xy-temperature","xz-temperature"].includes(visualMode);
    const refreshScene=timeVaryingScene&&(actualOneSecond
      ?actualFrame!==lastSceneFrame&&sceneRenderAccumulator>=1/12
      :sceneRenderAccumulator>=1/12);
    if(refreshScene){sceneRenderAccumulator=0;overlayRenderAccumulator=0;draw();}
    else if(overlayRenderAccumulator>=1/60-.002){overlayRenderAccumulator=Math.max(0,overlayRenderAccumulator-1/60);drawDynamicOverlay();}
  }
  advanceVideoPlayback(elapsed);
  requestAnimationFrame(animate);
}
function rows(element,values){ element.innerHTML=Object.entries(values).map(([key,value])=>`<dt>${key}</dt><dd>${value}</dd>`).join(""); }
function renderActiveMeshRun(){
  const run=activeMeshRun();if(!run)return;
  const counts=run.counts||{},prefix=run.counts_are_targets?"목표 ":"";
  const count=(value)=>Number.isFinite(value)?Number(value).toLocaleString():"—";
  rows(meshRunDetail,{
    "상태":run.state_label,
    "전체 셀":`${prefix}${count(counts.total_cells)}`,
    "유체 / 고체":`${prefix}${count(counts.fluid_cells)} / ${count(counts.solid_cells)}`,
    "노드":`${prefix}${count(counts.points)}`,
    "요소":run.element_family
  });
  meshRunNote.textContent=run.note;
  const waiting=run.data_ready===false;
  document.getElementById("status").textContent=waiting?`${run.label} · ${run.state_label}`:(actualOneSecond?`계산 완료 · ${run.label} · 1초 단위`:model.status_label);
  document.getElementById("result-badge").textContent=waiting?run.state_label:(model.actual_solver_data?"계산 완료":"준비 중");
  document.getElementById("calculation-summary").innerHTML=waiting
    ?`<strong>${run.label}</strong> · 자체 CFD 결과 없음`
    :`<strong>${run.label}</strong> · ${(actualOneSecond?.mesh_cell_count||counts.total_cells).toLocaleString()} cells · 0–1800 s`;
  document.getElementById("calculation-note").textContent=waiting?run.note:model.calculation_note;
  document.getElementById("calculation-card").classList.toggle("validated",model.actual_solver_data&&!waiting);
  flowPlay.disabled=waiting||!flow.enabled;flowSeek.disabled=waiting||!flow.enabled;
  draw();
}
if(meshRuns.length){
  meshRunSelect.innerHTML=meshRuns.map(run=>`<option value="${run.id}">${run.label} · ${run.state_label}</option>`).join("");
  meshRunSelect.value=activeMeshRunId;
  meshRunSelect.addEventListener("change",()=>{
    activeMeshRunId=meshRunSelect.value;
    const url=new URL(window.location.href);url.searchParams.set("mesh",activeMeshRunId);url.searchParams.set("autoplay","0");
    window.location.assign(url.toString());
  });
}else{document.querySelector(".mesh-run-card").hidden=true;}
rows(document.getElementById("geometry"),{"해석 영역":`${box.x} × ${box.y} × ${box.z} mm`,"배터리":`${model.cells.length}개 · Ø${model.cell_diameter_mm} × ${model.cell_length_mm} mm`,"입·출구":`각 Ø${model.port_diameter_mm} mm`,"셀 배열":"5–4–5–4–5 교차 배열"});
rows(document.getElementById("derived"),{
  "계산 격자":actualOneSecond?.mesh_cell_count?`${actualOneSecond.mesh_cell_count.toLocaleString()} cells`:"—",
  "Reynolds 수":model.derived.reynolds.toLocaleString(undefined,{maximumFractionDigits:1}),
  "총 발열":`${model.derived.total_heat_w.toFixed(3)} W`,
  "냉각수 밀도":`${model.inputs.fluid_density_kg_m3} kg/m³`,
  "비열 Cp":`${model.inputs.fluid_cp_J_kgK} J/(kg·K)`,
  "점도 μ":`${model.inputs.dynamic_viscosity_Pa_s} Pa·s`,
  "열전도율 k":`${model.inputs.fluid_k_W_mK} W/(m·K)`
});
const rawInletC=model.inputs.inlet_temperature_K-273.15;
rows(document.getElementById("inputs"),{
  "냉각수":"물 (Water)",
  "입구 온도":`${rawInletC.toFixed(3)} °C`,
  "입구 속도":`${model.inputs.inlet_velocity_m_s.toFixed(3)} m/s`,
  "출구 압력":flow.port_flow?`${(flow.port_flow.outlet_area_average_pressure_pa/1000).toFixed(3)} kPa`:"—",
  "해석 시간":"0–1800 s · 1 s 간격"
});
const figureGrid=document.getElementById("figures"),figureDetail=document.getElementById("figure-detail");
const figurePreview=document.getElementById("figure-preview"),figureImage=document.getElementById("figure-image"),figureVideo=document.getElementById("figure-video"),artifactTabs=document.getElementById("artifact-tabs");
const videoControls=document.getElementById("video-controls"),playbackRate=document.getElementById("playback-rate"),videoTime=document.getElementById("video-time"),videoSemantics=document.getElementById("video-semantics"),videoDuration=document.getElementById("video-duration");
const videoPlay=document.getElementById("video-play"),videoSeek=document.getElementById("video-seek"),videoPrev=document.getElementById("video-prev"),videoNext=document.getElementById("video-next");
const temperatureGraphPanel=document.getElementById("temperature-chart-panel");
const temperatureChartCanvas=document.getElementById("temperature-chart-canvas");
const temperatureChartOpen=document.getElementById("temperature-chart-open");
const temperatureChartClose=document.getElementById("temperature-chart-close");
const temperatureChartMetric=document.getElementById("temperature-chart-metric");
const perCellMetric=document.getElementById("per-cell-metric");
const perCellMetricLabel=document.getElementById("per-cell-metric-label");
const temperatureChartStatus=document.getElementById("temperature-chart-status");
const temperatureChartPng=document.getElementById("temperature-chart-png");
const temperatureChartCsv=document.getElementById("temperature-chart-csv");
const temperatureChartAllCsv=document.getElementById("temperature-chart-all-csv");
const temperatureGraphSummary=document.getElementById("temperature-graph-summary");
const convergenceGraphPanel=document.getElementById("convergence-chart-panel");
const convergenceChartCanvas=document.getElementById("convergence-chart-canvas");
const convergenceChartOpen=document.getElementById("convergence-chart-open");
const convergenceChartClose=document.getElementById("convergence-chart-close");
const convergenceChartStatus=document.getElementById("convergence-chart-status");
const convergenceChartPng=document.getElementById("convergence-chart-png");
const convergenceChartCsv=document.getElementById("convergence-chart-csv");
const convergenceSummary=document.getElementById("convergence-summary");
const convergenceHistorySelect=document.getElementById("convergence-history-select");
const temperatureMetricMeta={
  outlet:{title:"출구 평균 온도",filename:"outlet-average",key:"outlet_mass_weighted_average_c",definition:"출구 phi 질량유량 가중 평균"},
  "battery-maximum":{title:"배터리 최고 온도",filename:"battery-mesh-maximum",key:"battery_mesh_maximum_c",definition:"C01–C23 cell zone의 mesh max(T) 중 최고"},
  "surface-average":{title:"배터리 표면 평균 온도",filename:"battery-surface-average",key:"battery_surface_area_average_c",definition:"23개 셀 interface의 실제 면적가중 평균"},
  "internal-average":{title:"배터리 내부 온도",filename:"battery-internal-average",key:"battery_internal_volume_average_c",definition:"23개 batterySolid cell zone의 실제 체적가중 평균"}
};
const perCellMetricMeta={
  internal_volume_average_c:["내부 체적평균","per-cell-internal-average"],
  surface_area_average_c:["표면 면적평균","per-cell-surface-average"],
  mesh_maximum_c:["mesh 최고값","per-cell-mesh-maximum"]
};
function temperatureSeriesColour(index,count){
  if(count===1)return "#63d7e5";
  return `hsl(${(188+index*137.508)%360} 78% 64%)`;
}
function activeTemperatureGraph(){
  const metric=temperatureChartMetric.value;
  if(metric==="per-cell"){
    const source=temperatureGraphs?.per_cell?.[perCellMetric.value]||{};
    const [basis,filename]=perCellMetricMeta[perCellMetric.value];
    return {title:`각 배터리별 온도 · ${basis}`,filename,definition:`C01–C23 각각의 ${basis}`,
      series:Object.entries(source).map(([label,values],index,array)=>({label,values,colour:temperatureSeriesColour(index,array.length)}))};
  }
  const meta=temperatureMetricMeta[metric]||temperatureMetricMeta.outlet;
  return {title:meta.title,filename:meta.filename,definition:meta.definition,
    series:[{label:meta.title,values:temperatureGraphs?.[meta.key]||[],colour:temperatureSeriesColour(0,1)}]};
}
function paintTemperatureChart(context,width,height){
  const graph=activeTemperatureGraph(),series=graph.series;
  context.clearRect(0,0,width,height);context.fillStyle="#08111e";context.fillRect(0,0,width,height);
  context.fillStyle="#e9f2fa";context.font="700 18px system-ui";context.fillText(`${graph.title} · 정밀 CFD 실제값 · 1 s`,24,28);
  const values=series.flatMap(line=>line.values).filter(Number.isFinite);
  if(!values.length){context.fillStyle="#ffbd5b";context.font="700 14px system-ui";context.fillText("실제 온도 시계열 데이터가 없습니다.",24,62);return;}
  let low=Math.min(...values),high=Math.max(...values),span=Math.max(high-low,.002),padding=Math.max(span*.08,.001);
  low-=padding;high+=padding;
  const left=76,right=24,legendColumns=Math.max(1,Math.floor((width-left-right)/72));
  const legendRows=Math.ceil(series.length/legendColumns),top=48+legendRows*17,bottom=56;
  const plotWidth=Math.max(100,width-left-right),plotHeight=Math.max(100,height-top-bottom);
  context.font="700 10px ui-monospace,monospace";
  series.forEach((line,index)=>{
    const column=index%legendColumns,row=Math.floor(index/legendColumns),x=left+column*((width-left-right)/legendColumns),y=47+row*17;
    context.fillStyle=line.colour;context.fillRect(x,y-8,12,3);context.fillStyle="#dcecf7";context.fillText(line.label,x+17,y-4);
  });
  context.strokeStyle="#294158";context.lineWidth=1;context.strokeRect(left,top,plotWidth,plotHeight);
  context.font="600 11px ui-monospace,monospace";
  for(let tick=0;tick<=6;tick++){
    const fraction=tick/6,x=left+fraction*plotWidth,time=Math.round(1800*fraction);
    context.strokeStyle="#29415888";context.beginPath();context.moveTo(x,top);context.lineTo(x,top+plotHeight);context.stroke();
    context.fillStyle="#91a8bc";context.textAlign=tick===0?"left":tick===6?"right":"center";context.fillText(String(time),x,top+plotHeight+20);
  }
  for(let tick=0;tick<=6;tick++){
    const fraction=tick/6,y=top+plotHeight-fraction*plotHeight,value=low+fraction*(high-low);
    context.strokeStyle="#29415888";context.beginPath();context.moveTo(left,y);context.lineTo(left+plotWidth,y);context.stroke();
    context.fillStyle="#91a8bc";context.textAlign="right";context.fillText(value.toFixed(3),left-9,y+4);
  }
  context.textAlign="center";context.fillStyle="#dcecf7";context.font="700 12px system-ui";context.fillText("시간 [s]",left+plotWidth/2,height-16);
  context.save();context.translate(18,top+plotHeight/2);context.rotate(-Math.PI/2);context.fillText("온도 [°C]",0,0);context.restore();
  for(const line of series){
    context.strokeStyle=line.colour;context.lineWidth=series.length===1?2.4:1.15;context.globalAlpha=series.length===1?.98:.78;context.beginPath();
    line.values.forEach((value,index)=>{
      const x=left+index/Math.max(1,line.values.length-1)*plotWidth,y=top+(high-value)/(high-low)*plotHeight;
      if(index===0)context.moveTo(x,y);else context.lineTo(x,y);
    });
    context.stroke();context.globalAlpha=1;
  }
  const markerX=left+clamp(simulationTime/1800)*plotWidth;
  context.strokeStyle="#ffbd5b";context.lineWidth=1;context.beginPath();context.moveTo(markerX,top);context.lineTo(markerX,top+plotHeight);context.stroke();
  context.fillStyle="#ffbd5b";context.font="700 10px ui-monospace,monospace";context.textAlign="center";context.fillText(`${Math.round(simulationTime)} s`,markerX,top-7);
}
function updateTemperatureChartStatus(){
  const graph=activeTemperatureGraph(),last=1800;
  if(graph.series.length===1){
    const value=graph.series[0].values[last];temperatureChartStatus.textContent=`${graph.definition} · 1800 s = ${value.toFixed(6)} °C`;
  }else{
    const final=graph.series.map(line=>({label:line.label,value:line.values[last]})).sort((a,b)=>b.value-a.value);
    temperatureChartStatus.textContent=`${graph.definition} · 1800 s 최고 ${final[0].label} ${final[0].value.toFixed(6)} °C · 최저 ${final.at(-1).label} ${final.at(-1).value.toFixed(6)} °C`;
  }
}
function drawTemperatureChart(){
  if(temperatureGraphPanel.hidden)return;
  const rect=temperatureChartCanvas.getBoundingClientRect();if(rect.width<1||rect.height<1)return;
  const ratio=Math.min(window.devicePixelRatio||1,2);
  temperatureChartCanvas.width=Math.round(rect.width*ratio);temperatureChartCanvas.height=Math.round(rect.height*ratio);
  const context=temperatureChartCanvas.getContext("2d");context.setTransform(ratio,0,0,ratio,0,0);
  paintTemperatureChart(context,rect.width,rect.height);updateTemperatureChartStatus();
}
function downloadBlob(blob,filename){
  const link=document.createElement("a"),url=URL.createObjectURL(blob);link.href=url;link.download=filename;
  document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function csvValue(value){return `"${String(value).replaceAll('"','""')}"`;}
function downloadCsv(filename,headers,rowValues){
  const lines=[headers.map(csvValue).join(",")];
  for(let time=0;time<=1800;time++)lines.push(rowValues(time).map(value=>csvValue(typeof value==="number"?value.toFixed(8):value)).join(","));
  downloadBlob(new Blob(["\ufeff",lines.join("\n")],{type:"text/csv;charset=utf-8"}),filename);
}
function updateTemperatureChart(){
  const perCell=temperatureChartMetric.value==="per-cell";perCellMetric.hidden=!perCell;perCellMetricLabel.hidden=!perCell;drawTemperatureChart();
}
temperatureChartMetric.addEventListener("change",updateTemperatureChart);
perCellMetric.addEventListener("change",drawTemperatureChart);
temperatureChartOpen.addEventListener("click",()=>{
  if(!temperatureGraphs)return;
  flowPlaying=false;flowPlay.textContent="▶ 재생";videoPlaying=false;figureVideo.pause();figurePreview.hidden=true;convergenceGraphPanel.hidden=true;
  viewerControls.hidden=true;flowControls.hidden=true;canvasNote.hidden=true;temperatureGraphPanel.hidden=false;setDataLoadStatus("");
  requestAnimationFrame(drawTemperatureChart);
});
temperatureChartClose.addEventListener("click",()=>{
  temperatureGraphPanel.hidden=true;viewerControls.hidden=false;flowControls.hidden=!flow.enabled;canvasNote.hidden=false;
  modeFieldset.hidden=!flow.enabled;displayOptions.hidden=!flow.enabled;horizontalSections.hidden=!flow.enabled||!visualMode.startsWith("xy-");verticalSections.hidden=!flow.enabled||!visualMode.startsWith("xz-");resize();
});
temperatureChartPng.addEventListener("click",()=>{
  const graph=activeTemperatureGraph(),exportCanvas=document.createElement("canvas");exportCanvas.width=1600;exportCanvas.height=900;
  paintTemperatureChart(exportCanvas.getContext("2d"),1600,900);exportCanvas.toBlob(blob=>{if(blob)downloadBlob(blob,`${graph.filename}.png`);},"image/png");
});
temperatureChartCsv.addEventListener("click",()=>{
  const graph=activeTemperatureGraph();downloadCsv(`${graph.filename}.csv`,["time_s",...graph.series.map(line=>`${line.label}_C`)],time=>[time,...graph.series.map(line=>line.values[time])]);
});
temperatureChartAllCsv.addEventListener("click",()=>{
  const cells=Object.keys(temperatureGraphs?.per_cell?.internal_volume_average_c||{});
  const headers=["time_s","outlet_mass_weighted_average_C","battery_mesh_maximum_C","battery_surface_area_average_C","battery_internal_volume_average_C",...cells.map(cell=>`${cell}_internal_volume_average_C`)];
  downloadCsv("all-temperature-metrics.csv",headers,time=>[time,temperatureGraphs.outlet_mass_weighted_average_c[time],temperatureGraphs.battery_mesh_maximum_c[time],temperatureGraphs.battery_surface_area_average_c[time],temperatureGraphs.battery_internal_volume_average_c[time],...cells.map(cell=>temperatureGraphs.per_cell.internal_volume_average_c[cell][time])]);
});
if(temperatureGraphs){
  const last=1800;
  rows(temperatureGraphSummary,{"출구 평균":`${temperatureGraphs.outlet_mass_weighted_average_c[last].toFixed(4)} °C`,"배터리 최고":`${temperatureGraphs.battery_mesh_maximum_c[last].toFixed(4)} °C`,"표면 평균":`${temperatureGraphs.battery_surface_area_average_c[last].toFixed(4)} °C`,"내부 평균":`${temperatureGraphs.battery_internal_volume_average_c[last].toFixed(4)} °C`});
}else{temperatureChartOpen.disabled=true;temperatureGraphSummary.innerHTML="<dt>상태</dt><dd>실제 데이터 없음</dd>";}
function paintConvergenceChart(context,width,height){
  context.clearRect(0,0,width,height);context.fillStyle="#08111e";context.fillRect(0,0,width,height);
  context.fillStyle="#e9f2fa";context.font="700 18px system-ui";context.textAlign="left";
  context.fillText(`${convergence.scope} 수렴 이력`,24,28);
  const series=convergence.series||[],allValues=series.flatMap(line=>line.points.map(point=>point[1])).filter(value=>Number.isFinite(value)&&value>0);
  if(!allValues.length){context.fillStyle="#ffbd5b";context.font="700 14px system-ui";context.fillText("저장된 residual 데이터가 없습니다.",24,62);return;}
  const logHigh=Math.ceil(Math.log10(Math.max(...allValues))),logLow=Math.floor(Math.log10(Math.min(...allValues)));
  const first=convergence.first_iteration,last=convergence.last_iteration,span=Math.max(1,last-first);
  const left=82,right=28,columns=width>900?3:2,legendRows=Math.ceil(series.length/columns),top=50+legendRows*18,bottom=58;
  const plotWidth=Math.max(100,width-left-right),plotHeight=Math.max(100,height-top-bottom),logSpan=Math.max(1,logHigh-logLow);
  context.font="700 10px ui-monospace,monospace";
  series.forEach((line,index)=>{
    const column=index%columns,row=Math.floor(index/columns),x=left+column*plotWidth/columns,y=47+row*18;
    context.strokeStyle=line.colour;context.lineWidth=2;context.beginPath();context.moveTo(x,y-4);context.lineTo(x+13,y-4);context.stroke();
    context.fillStyle="#dcecf7";context.fillText(`${line.label} · ${line.final.toExponential(2)}`,x+18,y);
  });
  context.strokeStyle="#294158";context.lineWidth=1;context.strokeRect(left,top,plotWidth,plotHeight);
  context.font="600 11px ui-monospace,monospace";
  for(let tick=0;tick<=6;tick++){
    const fraction=tick/6,x=left+fraction*plotWidth,iteration=Math.round(first+fraction*span);
    context.strokeStyle="#29415888";context.beginPath();context.moveTo(x,top);context.lineTo(x,top+plotHeight);context.stroke();
    context.fillStyle="#91a8bc";context.textAlign=tick===0?"left":tick===6?"right":"center";context.fillText(String(iteration),x,top+plotHeight+21);
  }
  for(let exponent=logLow;exponent<=logHigh;exponent++){
    const y=top+(logHigh-exponent)/logSpan*plotHeight;
    context.strokeStyle="#29415888";context.beginPath();context.moveTo(left,y);context.lineTo(left+plotWidth,y);context.stroke();
    context.fillStyle="#91a8bc";context.textAlign="right";context.fillText(`1e${exponent}`,left-10,y+4);
  }
  for(const line of series){
    if(Number.isFinite(line.configured_gate)&&line.configured_gate>0){
      const y=top+(logHigh-Math.log10(line.configured_gate))/logSpan*plotHeight;
      if(y>=top&&y<=top+plotHeight){context.save();context.setLineDash([4,5]);context.globalAlpha=.32;context.strokeStyle=line.colour;context.beginPath();context.moveTo(left,y);context.lineTo(left+plotWidth,y);context.stroke();context.restore();}
    }
    context.strokeStyle=line.colour;context.lineWidth=1.45;context.globalAlpha=.9;context.beginPath();
    line.points.forEach((point,index)=>{
      const x=left+(point[0]-first)/span*plotWidth,y=top+(logHigh-Math.log10(Math.max(point[1],Number.MIN_VALUE)))/logSpan*plotHeight;
      if(index===0)context.moveTo(x,y);else context.lineTo(x,y);
    });
    context.stroke();context.globalAlpha=1;
  }
  const pimpleAxis=convergence.axis_mode==="cumulative_pimple_outer_iteration",timeStepAxis=convergence.axis_mode==="transient_time_step_iteration";
  const axisLabel=pimpleAxis?"누적 PIMPLE 전역 반복 횟수":timeStepAxis?"1초 과도 에너지 반복 횟수":"OpenFOAM 외부 반복 횟수";
  context.fillStyle="#dcecf7";context.font="700 12px system-ui";context.textAlign="center";context.fillText(axisLabel,left+plotWidth/2,height-16);
  context.save();context.translate(18,top+plotHeight/2);context.rotate(-Math.PI/2);context.fillText("Initial residual / |global continuity|",0,0);context.restore();
}
function drawConvergenceChart(){
  if(convergenceGraphPanel.hidden)return;
  const rect=convergenceChartCanvas.getBoundingClientRect();if(rect.width<1||rect.height<1)return;
  const ratio=Math.min(window.devicePixelRatio||1,2);
  convergenceChartCanvas.width=Math.round(rect.width*ratio);convergenceChartCanvas.height=Math.round(rect.height*ratio);
  const context=convergenceChartCanvas.getContext("2d");context.setTransform(ratio,0,0,ratio,0,0);
  paintConvergenceChart(context,rect.width,rect.height);
  const pimpleAxis=convergence.axis_mode==="cumulative_pimple_outer_iteration",timeStepAxis=convergence.axis_mode==="transient_time_step_iteration",transientAxis=pimpleAxis||timeStepAxis;
  const iterationLabel=pimpleAxis?"누적 PIMPLE 전역 반복":timeStepAxis?"1초 과도 에너지 반복":"정상상태 외부 반복";
  const linearTotal=convergence.linear_iteration_total?` · 방정식 내부 선형 반복 합계 ${convergence.linear_iteration_total.toLocaleString()}회`:"";
  const timeRange=transientAxis&&Number.isFinite(convergence.last_time_s)?` · 물리시간 ${Number(convergence.first_time_s||0).toLocaleString()}–${Number(convergence.last_time_s).toLocaleString()} s`:"";
  const comparison=pimpleAxis?" · 완전 연성 과도 반복 구조 · OpenFOAM 원본 잔차":timeStepAxis?" · 속도·압력·난류는 선행 단계에서 수렴 후 고정 · 이 단계는 energy만 계산":" · U/p/k/ω/h 실제 로그 · Fluent scaled residual과 수치 정의는 다름";
  convergenceChartStatus.textContent=`${convergence.scope} · ${iterationLabel} ${convergence.first_iteration.toLocaleString()}–${convergence.last_iteration.toLocaleString()}회${timeRange}${linearTotal}${comparison}`;
}
for(const history of convergenceHistories){
  if(!history?.available)continue;
  const option=document.createElement("option");option.value=history.id||"";option.textContent=history.label||history.scope||history.id;
  convergenceHistorySelect.appendChild(option);
}
convergenceHistorySelect.value=convergence.id||"";
convergenceHistorySelect.hidden=convergenceHistorySelect.options.length<=1;
convergenceHistorySelect.addEventListener("change",()=>{
  convergence=convergenceHistories.find(history=>history.id===convergenceHistorySelect.value)||convergence;
  renderConvergenceSummary();drawConvergenceChart();
});
convergenceChartOpen.addEventListener("click",()=>{
  if(!convergence.available)return;
  flowPlaying=false;flowPlay.textContent="▶ 재생";temperatureGraphPanel.hidden=true;figurePreview.hidden=true;
  viewerControls.hidden=true;flowControls.hidden=true;canvasNote.hidden=true;convergenceGraphPanel.hidden=false;setDataLoadStatus("");
  requestAnimationFrame(drawConvergenceChart);
});
convergenceChartClose.addEventListener("click",()=>{
  convergenceGraphPanel.hidden=true;viewerControls.hidden=false;flowControls.hidden=!flow.enabled;canvasNote.hidden=false;
  modeFieldset.hidden=!flow.enabled;displayOptions.hidden=!flow.enabled;horizontalSections.hidden=!flow.enabled||!visualMode.startsWith("xy-");verticalSections.hidden=!flow.enabled||!visualMode.startsWith("xz-");resize();
});
convergenceChartPng.addEventListener("click",()=>{
  const exportCanvas=document.createElement("canvas");exportCanvas.width=1600;exportCanvas.height=900;
  paintConvergenceChart(exportCanvas.getContext("2d"),1600,900);exportCanvas.toBlob(blob=>{if(blob)downloadBlob(blob,`cfd-residual-${convergence.id||"convergence"}.png`);},"image/png");
});
convergenceChartCsv.addEventListener("click",()=>{
  const series=convergence.series||[],iterations=[...new Set(series.flatMap(line=>line.points.map(point=>point[0])))].sort((a,b)=>a-b);
  const axisHeader=convergence.axis_mode==="cumulative_pimple_outer_iteration"?"cumulative_pimple_global_iteration":convergence.axis_mode==="transient_time_step_iteration"?"transient_energy_iteration":"openfoam_outer_iteration";
  const maps=series.map(line=>new Map(line.points)),lines=[[axisHeader,...series.map(line=>line.key)].map(csvValue).join(",")];
  for(const iteration of iterations)lines.push([iteration,...maps.map(values=>values.get(iteration)??"")].map(csvValue).join(","));
  downloadBlob(new Blob(["\ufeff",lines.join("\n")],{type:"text/csv;charset=utf-8"}),`cfd-residual-${convergence.id||"convergence"}.csv`);
});
function renderConvergenceSummary(){
  if(convergence.available){
    const pimpleAxis=convergence.axis_mode==="cumulative_pimple_outer_iteration",timeStepAxis=convergence.axis_mode==="transient_time_step_iteration";
    const validationPassed=String(convergence.validation_status||"").endsWith("_PASSED")||convergence.configured_residual_checks_passed;
    const summaryRows={"상태":validationPassed?"복합 검증 통과":"원본 로그 저장","계산 단계":convergence.label||convergence.scope};
    summaryRows[pimpleAxis?"누적 PIMPLE 반복":timeStepAxis?"1초 에너지 반복":"정상 외부 반복"]=`${convergence.first_iteration.toLocaleString()}–${convergence.last_iteration.toLocaleString()}회`;
    if(convergence.linear_iteration_total)summaryRows["내부 반복 합계"]=`${convergence.linear_iteration_total.toLocaleString()}회`;
    rows(convergenceSummary,summaryRows);convergenceChartOpen.disabled=false;
  }else{convergenceChartOpen.disabled=true;convergenceSummary.innerHTML="<dt>상태</dt><dd>로그 없음</dd>";}
}
renderConvergenceSummary();
let videoMode="none",videoPlaying=false,videoSimulationTime=0,videoFileTime=0,videoRenderAccumulator=0;
function formatSimulationTime(value){
  const seconds=Math.max(0,Math.round(value));
  const minutes=Math.floor(seconds/60),remainder=seconds%60;
  return `${minutes}:${String(remainder).padStart(2,"0")} (${seconds} s)`;
}
function videoFraction(){
  if(videoMode==="thermal")return clamp(videoSimulationTime/1800);
  return Number.isFinite(figureVideo.duration)&&figureVideo.duration>0?clamp(videoFileTime/figureVideo.duration):0;
}
function updateVideoTime(){
  const fraction=videoFraction(),rate=Number(playbackRate.value);
  videoSeek.value=String(Math.round(1000*fraction));
  if(videoMode==="thermal"){
    videoTime.textContent=`시뮬레이션 t = ${formatSimulationTime(videoSimulationTime)} / 30:00`;
    videoDuration.textContent=`${rate}× 완주 ${formatWallDuration(1800/rate)}`;
  }else if(videoMode==="fixed-flow"){
    videoTime.textContent=`시각화 위상 ${(100*fraction).toFixed(0)}%`;
    videoDuration.textContent=`${rate}× 시각화`;
  }else{videoTime.textContent="대기 중";videoDuration.textContent="";}
}
function syncVideoFrame(){
  if(!Number.isFinite(figureVideo.duration)||figureVideo.duration<=0)return;
  figureVideo.currentTime=figureVideo.duration*videoFraction();updateVideoTime();
}
function advanceVideoPlayback(elapsed){
  if(!videoPlaying||figurePreview.hidden||figureVideo.hidden||videoMode==="none")return;
  const rate=Number(playbackRate.value);
  if(videoMode==="thermal")videoSimulationTime=(videoSimulationTime+elapsed*rate)%1800;
  else if(Number.isFinite(figureVideo.duration)&&figureVideo.duration>0)videoFileTime=(videoFileTime+elapsed*rate)%figureVideo.duration;
  videoRenderAccumulator+=elapsed;
  if(videoRenderAccumulator>=1/12){videoRenderAccumulator=0;syncVideoFrame();}
}
playbackRate.addEventListener("change",updateVideoTime);
videoPlay.addEventListener("click",()=>{videoPlaying=!videoPlaying;videoPlay.textContent=videoPlaying?"❚❚ 정지":"▶ 재생";});
videoSeek.addEventListener("input",()=>{
  const fraction=Number(videoSeek.value)/1000;
  if(videoMode==="thermal")videoSimulationTime=1800*fraction;
  else if(Number.isFinite(figureVideo.duration))videoFileTime=figureVideo.duration*fraction;
  syncVideoFrame();
});
function stepVideo(direction){
  videoPlaying=false;videoPlay.textContent="▶ 재생";
  if(videoMode==="thermal")videoSimulationTime=clamp(videoSimulationTime+direction*15,0,1800);
  else{const fps=videoMode==="fixed-flow"?20:25;videoFileTime=clamp(videoFileTime+direction/fps,0,figureVideo.duration||0);}
  syncVideoFrame();
}
videoPrev.addEventListener("click",()=>stepVideo(-1));
videoNext.addEventListener("click",()=>stepVideo(1));
figureVideo.addEventListener("loadedmetadata",()=>{figureVideo.pause();syncVideoFrame();});
const figureUi={
  1:["3D 종합 화면","셀 온도와 유체 흐름을 시간에 따라 확인"],
  2:["계산 메시","해석에 사용한 메시 구조 확인"],
  3:["메시 품질","표준 메시의 셀 수와 품질 확인"],
  4:["배터리 온도 변화","시간에 따른 배터리 온도 확인"],
  6:["가로 단면 온도","선택 높이의 온도 분포 확인"],
  7:["셀 표면 온도","배터리 표면 온도 분포 확인"],
  8:["온도·유동 종합","셀 온도와 냉각수 흐름 동시 확인"],
  9:["배터리 온도와 유선","배터리별 온도와 유선 확인"],
  10:["유체 유선","냉각수 흐름 경로와 유속 확인"],
  11:["가로 단면 유속","선택 높이의 유속 분포 확인"],
  12:["세로 단면 유속","선택 위치의 세로 유속 분포 확인"],
  13:["세로 단면 온도","선택 위치의 세로 온도 분포 확인"],
  14:["가로 단면 온도","가로 온도 분포 확인"]
};
function figureSummary(item){const ui=figureUi[item.id]||["결과 화면","계산 결과 확인"];return `Fig. ${item.id} · ${ui[0]} — ${ui[1]}`;}
figureGrid.innerHTML=model.figures.map(item=>`<button data-figure="${item.id}" class="${item.status.endsWith("_READY")?"ready":""}" title="${(figureUi[item.id]||["결과 화면"])[0]}">Fig ${item.id}</button>`).join("");
const initialFigure=model.figures.find(item=>item.id===1);
if(initialFigure)figureDetail.textContent=figureSummary(initialFigure);
figureGrid.querySelectorAll("[data-figure]").forEach(button=>button.addEventListener("click",()=>{
  temperatureGraphPanel.hidden=true;
  convergenceGraphPanel.hidden=true;
  const item=model.figures.find(candidate=>String(candidate.id)===button.dataset.figure);
  figureDetail.textContent=figureSummary(item);
  const artifacts=item.viewer_artifacts||[];
  const interactiveFigureModes={4:"cell-temperature",6:"xy-temperature",7:"cell-temperature",8:"combined",9:"combined",10:"velocity-streamlines",11:"xy-velocity",12:"xz-velocity",13:"xz-temperature",14:"xy-temperature"};
  if(actualOneSecond&&interactiveFigureModes[item.id]){
    const mode=interactiveFigureModes[item.id],input=modeInputs.find(candidate=>candidate.value===mode);
    if(input)input.checked=true;
    videoPlaying=false;figureVideo.pause();figurePreview.hidden=true;viewerControls.hidden=false;canvasNote.hidden=false;flowControls.hidden=false;modeFieldset.hidden=false;displayOptions.hidden=false;
    selectVisualMode(mode);return;
  }
  if(item.id===1||artifacts.length===0){videoPlaying=false;figureVideo.pause();figurePreview.hidden=true;viewerControls.hidden=false;canvasNote.hidden=false;flowControls.hidden=!flow.enabled;modeFieldset.hidden=!flow.enabled;displayOptions.hidden=!flow.enabled;horizontalSections.hidden=!flow.enabled||!visualMode.startsWith("xy-");verticalSections.hidden=!flow.enabled||!visualMode.startsWith("xz-");draw();return;}
  const show=index=>{
    const path=artifacts[index],video=/\.(mp4|webm)(?:\?.*)?$/i.test(path);
    figureImage.hidden=video;figureVideo.hidden=!video;videoControls.hidden=!video;
    if(video){
      videoMode=/actual_thermal_xy/i.test(path)?"thermal":/frozen_flow_particles_fixed_U/i.test(path)?"fixed-flow":"generic";
      videoSemantics.textContent=videoMode==="thermal"?"Fig 6 실제 냉각수 T(x,t) + 고정 U(x) · 15 s 간격 · 1×=실제 1800 s":videoMode==="fixed-flow"?"고정 U(x)의 긴 유선을 따라 이동 헤드가 방향 표시 · 과도속도 아님":"동영상 결과";
      videoSimulationTime=0;videoFileTime=0;videoRenderAccumulator=0;videoPlaying=true;videoPlay.textContent="❚❚ 정지";
      figureVideo.pause();figureVideo.src=path;figureVideo.load();updateVideoTime();
    }else{
      videoMode="none";videoPlaying=false;figureVideo.pause();figureImage.src=path;
    }
    artifactTabs.querySelectorAll("button").forEach((tab,i)=>tab.classList.toggle("active",i===index));
  };
  artifactTabs.innerHTML=artifacts.map((path,index)=>`<button data-artifact="${index}">${/\.(mp4|webm)(?:\?.*)?$/i.test(path)?"애니메이션":"정적 결과"}</button>`).join("");
  artifactTabs.querySelectorAll("[data-artifact]").forEach(tab=>tab.addEventListener("click",()=>show(Number(tab.dataset.artifact))));
  figurePreview.hidden=false;viewerControls.hidden=true;canvasNote.hidden=true;flowControls.hidden=true;modeFieldset.hidden=true;displayOptions.hidden=true;horizontalSections.hidden=true;verticalSections.hidden=true;show(0);
}));
const cellGrid=document.getElementById("cells");
cellGrid.innerHTML=model.cells.map(cell=>`<button type="button" data-cell-id="${cell.id}" aria-label="${cell.id} 셀 강조">${cell.id}</button>`).join("");
function showCellNotification(cellId){
  highlightedCellId=cellId;clearTimeout(cellHighlightTimer);
  for(const button of cellGrid.querySelectorAll("[data-cell-id]"))button.classList.toggle("active",button.dataset.cellId===cellId);
  cellToast.textContent=`${cellId} 선택 · 3D 모델에서 잠시 강조됩니다`;cellToast.hidden=false;draw();
  cellHighlightTimer=setTimeout(()=>{
    highlightedCellId=null;cellToast.hidden=true;
    for(const button of cellGrid.querySelectorAll("[data-cell-id]"))button.classList.remove("active");draw();
  },1800);
}
cellGrid.querySelectorAll("[data-cell-id]").forEach(button=>button.addEventListener("click",()=>showCellNotification(button.dataset.cellId)));
const meshCellCount=actualOneSecond?.mesh_cell_count;
document.getElementById("status").textContent=actualOneSecond?"계산 완료 · 정밀 결과 · 1초 단위":model.status_label;
document.getElementById("result-badge").textContent=model.actual_solver_data?"계산 완료":"준비 중";
document.getElementById("calculation-summary").innerHTML=model.actual_solver_data
  ?`<strong>정밀 CFD 결과</strong> · ${meshCellCount?meshCellCount.toLocaleString()+" cells · ":""}0–1800 s`
  :model.calculation_summary;
document.getElementById("calculation-note").textContent=model.calculation_note;
document.getElementById("calculation-card").classList.toggle("validated",model.actual_solver_data);
document.getElementById("provenance").textContent=model.actual_solver_data?"검증된 CFD 결과 뷰어":"형상 미리보기";
renderActiveMeshRun();
let stageResizeFrame=0;
if(typeof ResizeObserver==="function"){
  const stageResizeObserver=new ResizeObserver(()=>{
    cancelAnimationFrame(stageResizeFrame);stageResizeFrame=requestAnimationFrame(resize);
  });
  stageResizeObserver.observe(sceneStage);
}
window.addEventListener("resize",()=>{resize();drawTemperatureChart();drawConvergenceChart();});resize();requestAnimationFrame(animate);
'''


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _portable_dataset_javascript(name: str, raw: bytes) -> str:
    """Wrap one binary field in an on-demand file:// compatible script."""

    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    payload = base64.b64encode(compressed).decode("ascii")
    packed = json.dumps(
        {
            "encoding": "gzip-base64",
            "byte_length": len(raw),
            "payload": payload,
        },
        separators=(",", ":"),
    )
    return (
        "window.CFD_TWIN_PACKED_DATA = window.CFD_TWIN_PACKED_DATA || {};\n"
        f"window.CFD_TWIN_PACKED_DATA[{json.dumps(name)}] = "
        f"{packed};\n"
    )


def write_viewer_bundle(
    config: ResolvedConfig,
    output_dir: str | Path,
    *,
    case_dir: str | Path | None = None,
) -> dict[str, object]:
    output = Path(output_dir).resolve()
    report = geometry_report(config)
    placements = cell_placements(config)
    figures = figure_catalog()
    result_manifest: dict[str, object] | None = None
    mesh_independence: dict[str, object] = {}
    case: Path | None = None
    if case_dir is not None:
        case = Path(case_dir).resolve()
        manifest_path = case / "results" / "result_manifest.json"
        if manifest_path.is_file():
            result_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        mesh_path = case / "results" / "mesh_independence.json"
        if mesh_path.is_file():
            mesh_independence = json.loads(mesh_path.read_text(encoding="utf-8"))
        artifact_output = output / "artifacts"
        for figure in figures:
            contract_path = case / "results" / "figures" / f'figure_{int(figure["id"]):02d}.json'
            if not contract_path.is_file():
                continue
            persisted = json.loads(contract_path.read_text(encoding="utf-8"))
            figure.update(persisted)
            viewer_artifacts: list[str] = []
            for artifact in persisted.get("artifacts", []):
                source = case / artifact
                if not source.is_file() or source.suffix.lower() not in {".png", ".mp4", ".webm", ".gif"}:
                    continue
                destination = artifact_output / f'fig{int(figure["id"]):02d}_{source.name}'
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
                viewer_artifacts.append(
                    f"{destination.relative_to(output)}?v={digest}"
                )
            figure["viewer_artifacts"] = viewer_artifacts
    actual_solver_data = bool(
        result_manifest
        and result_manifest.get("data_status") == "L0_CFD_RESULTS_READY"
        and result_manifest.get("actual_solver_data") is True
    )
    if actual_solver_data:
        summary = result_manifest.get("final_summary", {})
        final_tmax_k = float(summary.get("battery_tmax_k", float("nan")))
        status = "PRECISION_RESULT_READY"
        mesh_passed = mesh_independence.get("status") == "MESH_INDEPENDENCE_PASSED"
        status_label = (
            "정밀 결과 · 역사적 격자 독립성 PASS"
            if mesh_passed
            else "검증 CFD 결과 · 논문 최종 아님"
        )
        calculation_summary = (
            "<strong>실제 CFD 검증 완료</strong> — 1800 s 열 과도와 "
            + ("역사적 격자 독립성" if mesh_passed else "solver gate")
            + "를 통과했습니다."
        )
        if mesh_passed:
            comparison = mesh_independence["comparisons"]["L2_to_L3"]
            calculation_note = (
                f"1800 s 체적 Tmax {final_tmax_k:.6f} K · "
                f"정밀도 비교 ΔTmax {float(comparison['battery_tmax_difference_k']):.6f} K · "
                "Figure 1의 이동 헤드는 검증된 고정 U(x)를 따르며 "
                "과도속도장이 아닙니다."
            )
        else:
            calculation_note = (
                f"최종 체적 Tmax {final_tmax_k:.6f} K · Figure 3의 "
                "격자 독립성 완료 전에는 논문 최종값으로 사용하지 않습니다."
            )
    else:
        status = "GEOMETRY_PREVIEW_CFD_NOT_RUN"
        status_label = "GEOMETRY PREVIEW · CFD 미실행"
        calculation_summary = (
            "<strong>CFD 미실행</strong> — PPT 외곽/노치는 반영됐고 포트 y 좌표와 "
            "입구 열 배정은 A-04 기본 가정입니다."
        )
        calculation_note = "이 화면의 색은 셀 구분용이며 온도장이 아닙니다."
    payload_case = case if case is not None else Path(".")
    precision_cfd = _interactive_actual_cfd_payload(
        config,
        payload_case,
        enabled=actual_solver_data and case is not None,
        mesh_id="precision",
    )
    if precision_cfd is None:
        precision_cfd = _interactive_legacy_cfd_payload(
            config,
            payload_case,
            enabled=actual_solver_data and case is not None,
        )
    standard_cfd = _interactive_actual_cfd_payload(
        config,
        payload_case,
        enabled=case is not None,
        mesh_id="single",
    )
    interactive_cfd_by_mesh = {
        mesh_id: payload
        for mesh_id, payload in (
            ("precision", precision_cfd),
            ("single", standard_cfd),
        )
        if payload is not None and payload.get("enabled")
    }
    interactive_cfd = interactive_cfd_by_mesh.get(
        "precision", interactive_cfd_by_mesh.get("single", {"enabled": False})
    )
    actual_one_second = None
    if interactive_cfd.get("enabled"):
        actual_one_second = interactive_cfd.get("actual_one_second")
        if actual_one_second:
            status = "PRECISION_ONE_SECOND_RESULTS_READY"
            status_label = "CFD 결과 · 1 s 실제 셀값 · 20.000 °C 원본 · 보간/패딩 없음"
            calculation_summary = (
                "<strong>CFD 계산 완료</strong> — 정밀/표준 메시 결과를 "
                "t=0–1800 s를 1 s 간격으로 직접 저장했습니다."
            )
            quantization_error = float(
                actual_one_second["temperature_encoding"][
                    "maximum_quantization_error_k"
                ]
            )
            calculation_note = (
                "Figure 1 온도는 293.15 K 원본에서 273.15만 뺀 값이며 "
                "시간/공간 보간과 무효점 패딩을 하지 않습니다. "
                f"브라우저 저장 양자화 최대오차 {quantization_error:.3e} K."
            )
        else:
            status_label += " · 화면온도 +0.150 K"
            calculation_note += (
                " · Figure 1 온도는 원본장을 보존한 채 표시값에 +0.150 K를 "
                "적용했습니다."
            )
        figure1 = next(figure for figure in figures if int(figure["id"]) == 1)
        figure1.update(
            {
                "title": "3D 종합 결과",
                "status": (
                    "PRECISION_ONE_SECOND_INTERACTIVE_READY"
                    if actual_one_second
                    else "PRECISION_INTERACTIVE_READY"
                ),
                "time_mode": "transient_temperature_fixed_velocity",
            }
        )
    if case is not None:
        for mesh_id, payload in interactive_cfd_by_mesh.items():
            packaged = payload.get("actual_one_second")
            if not packaged:
                continue
            runs_root = case / (
                "production/L3/runs"
                if mesh_id == "precision"
                else "production/single/runs"
            )
            upgraded_source = (
                runs_root / "frozen_flow_actual_1s_xy5/reports/viewer"
            )
            source_dir = (
                upgraded_source
                if mesh_id == "precision"
                and (upgraded_source / "actual_1s_manifest.json").is_file()
                else runs_root / "frozen_flow_actual_1s/reports/viewer"
            )
            data_dir = output / "data" / mesh_id
            data_dir.mkdir(parents=True, exist_ok=True)
            for name, specification in packaged["datasets"].items():
                source = source_dir / Path(specification["file"]).name
                destination = data_dir / source.name
                shutil.copy2(source, destination)
                raw = source.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                relative = destination.relative_to(output).as_posix()
                specification["file"] = f"{relative}?v={digest[:12]}"
                specification["portable_file"] = f"{relative}.js"
                specification["sha256"] = digest
                specification["byte_length"] = len(raw)
                _atomic_write(
                    data_dir / f"{source.name}.js",
                    _portable_dataset_javascript(name, raw),
                )
            shutil.copy2(
                source_dir / "actual_1s_manifest.json",
                data_dir / "actual_1s_manifest.json",
            )
            shutil.copy2(
                source_dir / "actual_1s_validation.json",
                data_dir / "actual_1s_validation.json",
            )
    mesh_runs = _mesh_runs_payload(
        case if case is not None else output / "__no_case__",
        precision_data_ready="precision" in interactive_cfd_by_mesh,
        standard_data_ready="single" in interactive_cfd_by_mesh,
    )
    convergence = (
        _convergence_payload(case) if case is not None else {"available": False}
    )
    model = {
        "schema_version": 1,
        "status": status,
        "status_label": status_label,
        "actual_solver_data": actual_solver_data,
        "interactive_cfd": interactive_cfd,
        "interactive_cfd_by_mesh": interactive_cfd_by_mesh,
        "mesh_runs": mesh_runs,
        "convergence": convergence,
        "calculation_summary": calculation_summary,
        "calculation_note": calculation_note,
        "production_cad_ready": report["production_cad_ready"],
        "blocking_assumptions": report["blocking_assumptions"],
        "nonblocking_assumptions": report["nonblocking_assumptions"],
        "enclosure_mm": {
            "x": config.enclosure.width_x_m * 1000,
            "y": config.enclosure.length_y_m * 1000,
            "z": config.enclosure.height_z_m * 1000,
            "notch_depth": config.enclosure.notch.depth_m * 1000,
        },
        "cell_diameter_mm": config.cells.diameter_m * 1000,
        "cell_length_mm": config.cells.length_m * 1000,
        "port_diameter_mm": config.ports.inlet_diameter_m * 1000,
        "topology": config.cells.topology,
        "outline_mm": [
            [x_m * 1000, y_m * 1000]
            for x_m, y_m in config.enclosure.outline_polygon_m
        ],
        "pitch_mm": {
            "x": config.cells.column_pitch_x_m * 1000,
            "y": config.cells.row_pitch_y_m * 1000,
            "offset": config.cells.stagger_offset_y_m * 1000,
        },
        "cells": [
            {
                "id": placement.cell_id,
                "x": placement.center_x_m * 1000,
                "y": placement.center_y_m * 1000,
                "z": placement.center_z_m * 1000,
                "radius": config.cells.diameter_m * 500,
                "length": config.cells.length_m * 1000,
            }
            for placement in placements
        ],
        "ports": [
            {
                "id": "IN",
                "kind": "port",
                "x": config.ports.inlet_center_xy_m[0] * 1000,
                "y": config.ports.inlet_center_xy_m[1] * 1000,
                "z": config.enclosure.height_z_m * 1000
                + config.ports.external_length_m * 500,
                "radius": config.ports.inlet_diameter_m * 500,
                "length": config.ports.external_length_m * 1000,
            },
            {
                "id": "OUT",
                "kind": "port",
                "x": config.ports.outlet_center_xy_m[0] * 1000,
                "y": config.ports.outlet_center_xy_m[1] * 1000,
                "z": -config.ports.external_length_m * 500,
                "radius": config.ports.outlet_diameter_m * 500,
                "length": config.ports.external_length_m * 1000,
            },
        ],
        "derived": {
            "reynolds": config.derived.reynolds_table4,
            "paper_reynolds": config.derived.paper_reynolds,
            "cell_heat_w": config.derived.cell_heat_w,
            "total_heat_w": config.derived.total_heat_w,
            "qvol_w_m3": config.battery.heat_source.qvol_w_m3 or 16325.0,
        },
        "inputs": {
            "inlet_velocity_m_s": config.boundary.inlet_velocity_m_s,
            "inlet_temperature_K": (
                float(actual_one_second["initial_temperature_k"])
                if actual_one_second
                else config.boundary.inlet_temperature_k
            ),
            "fluid_density_kg_m3": config.fluid.density_kg_m3,
            "dynamic_viscosity_Pa_s": config.fluid.dynamic_viscosity_pa_s,
            "fluid_cp_J_kgK": config.fluid.specific_heat_j_kgk,
            "fluid_k_W_mK": config.fluid.thermal_conductivity_w_mk,
        },
        "figures": figures,
        "resolved_input_sha256": config.resolved_input_sha256,
        "geometry_sha256": config.geometry_sha256,
    }
    model_javascript = (
        "window.CFD_TWIN_MODEL = "
        + json.dumps(model, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + ";\n"
    )
    style_asset = f"style.{hashlib.sha256(CSS.encode('utf-8')).hexdigest()[:12]}.css"
    app_asset = f"app.{hashlib.sha256(JAVASCRIPT.encode('utf-8')).hexdigest()[:12]}.js"
    model_asset = (
        f"model.{hashlib.sha256(model_javascript.encode('utf-8')).hexdigest()[:12]}.js"
    )
    rendered_html = HTML.replace("__STYLE_ASSET__", style_asset).replace(
        "__APP_ASSET__", app_asset
    ).replace("__MODEL_ASSET__", model_asset)
    for obsolete in (
        *output.glob("style.*.css"),
        *output.glob("app.*.js"),
        *output.glob("model.*.js"),
    ):
        if obsolete.name not in {style_asset, app_asset, model_asset}:
            obsolete.unlink()
    _atomic_write(output / "index.html", rendered_html)
    _atomic_write(output / "style.css", CSS)
    _atomic_write(output / "app.js", JAVASCRIPT)
    _atomic_write(output / style_asset, CSS)
    _atomic_write(output / app_asset, JAVASCRIPT)
    _atomic_write(output / "model.js", model_javascript)
    _atomic_write(output / model_asset, model_javascript)
    _atomic_write(
        output / "viewer_manifest.json",
        json.dumps(model, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(
        output / "README_VIEWER.txt",
        "CFD digital-twin result viewer\n\n"
        "Keep this entire folder together and double-click index.html.\n"
        "Figure 13 and the other temperature modes load compressed direct-cell data\n"
        "from the data folder on demand, even when the page is opened as file://.\n"
        "Use '5 actual temperature graphs' to view outlet, battery maximum,\n"
        "surface average, internal average, and C01-C23 histories, then export\n"
        "the current graph as PNG/CSV or all metrics as one CSV.\n"
        "Use a current Chrome, Edge, or Safari browser. Do not copy index.html alone.\n",
    )
    return model
