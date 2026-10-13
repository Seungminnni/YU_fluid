"""Attach direct outlet-referenced pressure sections to retained solver results."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from cfd_twin.multi_case_viewer import (
    read_viewer_model,
    refresh_four_case_app,
    refresh_four_case_manifest,
    synchronize_flow_legends,
    write_four_case_model,
)
from cfd_twin.viewer import OpenFoamResultReader, refresh_viewer_ui_assets


def _latest_numeric_time(root: Path) -> float:
    values: list[float] = []
    for path in root.iterdir():
        if not path.is_dir():
            continue
        try:
            values.append(float(path.name))
        except ValueError:
            pass
    if not values:
        raise ValueError(f"no reconstructed numeric time in {root}")
    return max(values)


def _sample_run(case: Path, payload: dict) -> dict:
    foam = case / "runs/steady_cht/steady_cht.foam"
    if not foam.is_file():
        raise FileNotFoundError(foam)
    reader = OpenFoamResultReader(foam)
    fluid, _ = reader.regions(_latest_numeric_time(foam.parent))
    pressure_array = fluid.GetCellData().GetArray("p")
    if pressure_array is None:
        raise RuntimeError(f"{foam}: coolant cell pressure p is missing")
    pressure = reader.vtk_to_numpy(pressure_array)
    velocity_array = fluid.GetCellData().GetArray("U")
    if velocity_array is None:
        raise RuntimeError(f"{foam}: coolant cell velocity U is missing")
    velocity = reader.vtk_to_numpy(velocity_array)
    locator = reader.vtk.vtkStaticCellLocator()
    locator.SetDataSet(fluid)
    locator.BuildLocator()
    thermal_foam = case / "runs/frozen_flow_actual_1s/frozen_flow_actual_1s.foam"
    if not thermal_foam.is_file():
        raise FileNotFoundError(thermal_foam)
    thermal_reader = OpenFoamResultReader(thermal_foam)
    thermal_fluid, _ = thermal_reader.regions(1800.0)
    thermal_temperature_array = thermal_fluid.GetCellData().GetArray("T")
    if thermal_temperature_array is None:
        raise RuntimeError(f"{thermal_foam}: coolant cell temperature T is missing")
    thermal_temperature = thermal_reader.vtk_to_numpy(thermal_temperature_array)
    thermal_locator = thermal_reader.vtk.vtkStaticCellLocator()
    thermal_locator.SetDataSet(thermal_fluid)
    thermal_locator.BuildLocator()
    reference = float(payload["port_flow"]["outlet_area_average_pressure_pa"])
    template = payload.get("sections", {}).get("xy")
    if not isinstance(template, dict):
        raise RuntimeError(f"{foam}: centre XY section metadata is missing")
    for name, z_mm, label in (
        ("xy04", 3.5, "Z = 3.5 mm · 하부 냉각수 간극"),
        ("xy96", 78.5, "Z = 78.5 mm · 상부 냉각수 간극"),
    ):
        payload["sections"][name] = {
            "kind": "xy",
            "columns": int(template["columns"]),
            "rows": int(template["rows"]),
            "fixed_fraction": z_mm / 82.0,
            "label": label,
            "origin_mm": [float(template["origin_mm"][0]), float(template["origin_mm"][1]), z_mm],
            "column_step_mm": list(template["column_step_mm"]),
            "row_step_mm": list(template["row_step_mm"]),
            "actual_dataset": None,
            "solid_temperature_time_s": 1800,
            "snapshot_time_s": 1800,
            "snapshot_semantics": "direct final 1800 s coolant field; selecting temperature jumps to the retained snapshot",
            "spatial_sampling": "direct containing validated coolant cell; no interpolation or padding",
        }
    payload["horizontal_section_keys"] = [
        "xy04", "xy15", "xy33", "xy", "xy68", "xy85", "xy96"
    ]
    sampled: list[float] = []
    section_counts: dict[str, int] = {}
    for name, section in payload.get("sections", {}).items():
        columns = int(section["columns"])
        rows = int(section["rows"])
        origin = section["origin_mm"]
        column_step = section["column_step_mm"]
        row_step = section["row_step_mm"]
        values: list[float | None] = []
        velocity_vectors: list[list[float] | None] = []
        velocity_magnitudes: list[float | None] = []
        fluid_temperature: list[float | None] = []
        for row in range(rows):
            for column in range(columns):
                point = tuple(
                    (
                        float(origin[axis])
                        + column * float(column_step[axis])
                        + row * float(row_step[axis])
                    )
                    / 1000.0
                    for axis in range(3)
                )
                cell_id = locator.FindCell(point)
                if cell_id < 0:
                    values.append(None)
                    if name in {"xy04", "xy96"}:
                        velocity_vectors.append(None)
                        velocity_magnitudes.append(None)
                        fluid_temperature.append(None)
                    continue
                value = float(pressure[cell_id]) - reference
                values.append(round(value, 7))
                sampled.append(value)
                if name in {"xy04", "xy96"}:
                    vector = velocity[cell_id]
                    velocity_vectors.append([round(float(component), 7) for component in vector])
                    velocity_magnitudes.append(round(float(reader.np.linalg.norm(vector)), 7))
                    thermal_id = thermal_locator.FindCell(point)
                    fluid_temperature.append(
                        round(float(thermal_temperature[thermal_id]) - 273.15, 8)
                        if thermal_id >= 0
                        else None
                    )
        valid = [value for value in values if value is not None]
        if not valid:
            raise RuntimeError(f"{foam}: pressure section {name} has no valid cells")
        section["pressure_gauge_pa"] = values
        section["pressure_range_pa"] = [min(valid), max(valid)]
        if name in {"xy04", "xy96"}:
            valid_velocity = [value for value in velocity_magnitudes if value is not None]
            valid_temperature = [value for value in fluid_temperature if value is not None]
            if not valid_velocity or not valid_temperature:
                raise RuntimeError(f"{foam}: gap section {name} has incomplete U/T data")
            section["velocity_vectors_m_s"] = velocity_vectors
            section["velocity_m_s"] = velocity_magnitudes
            section["velocity_range_m_s"] = [0.0, max(valid_velocity)]
            section["fluid_temperature_1800_c"] = fluid_temperature
            section["solid_temperature_1800_c"] = [None] * len(fluid_temperature)
            section["temperature_range_c"] = [min(valid_temperature), max(valid_temperature)]
        section_counts[name] = len(valid)
    payload.update(
        pressure_available=True,
        pressure_reference_pa=reference,
        pressure_semantics=(
            "direct steady coolant-cell absolute p minus outlet area-average p; "
            "no spatial interpolation or pressure-field synthesis"
        ),
    )
    return {
        "source": str(foam),
        "section_count": len(section_counts),
        "valid_samples_by_section": section_counts,
        "minimum_pressure_gauge_pa": min(sampled),
        "maximum_pressure_gauge_pa": max(sampled),
        "samples_outside_fixed_500_pa_legend": sum(abs(value) > 500 for value in sampled),
        "horizontal_gap_sections": ["xy04", "xy96"],
        "horizontal_gap_temperature_snapshot_s": 1800,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--viewer", type=Path, default=Path("viewer"))
    parser.add_argument("--runs-root", type=Path, default=Path("build/imported_runs/runs"))
    args = parser.parse_args()
    viewer = args.viewer.resolve()
    runs_root = args.runs_root.resolve()
    model, _ = read_viewer_model(viewer)
    report = {"schema_version": 1, "runs": {}, "unavailable_runs": []}
    for run in model.get("mesh_runs", []):
        run_id = str(run.get("id", ""))
        payload = model.get("interactive_cfd_by_mesh", {}).get(run_id)
        if not isinstance(payload, dict):
            continue
        raw_id = run_id.removeprefix("run_")
        case = runs_root / raw_id / "case"
        foam = case / "runs/steady_cht/steady_cht.foam"
        if not foam.is_file():
            report["unavailable_runs"].append(run_id)
            continue
        report["runs"][run_id] = _sample_run(case, payload)
    synchronize_flow_legends(model)
    refresh_viewer_ui_assets(viewer)
    model_asset = write_four_case_model(viewer, model)
    app_asset = refresh_four_case_app(viewer)
    refresh_four_case_manifest(viewer)
    report.update(
        model_asset=model_asset,
        app_asset=app_asset,
        fixed_velocity_legend_range_m_s=[0.0, 1.0],
        fixed_pressure_legend_range_pa=[-500.0, 500.0],
        pressure_reference="p - outlet area-average p",
    )
    (viewer / "pressure_backfill_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
