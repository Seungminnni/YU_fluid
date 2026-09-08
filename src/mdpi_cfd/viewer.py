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
from .result_contract import figure_catalog


def _direct_l3_xy_velocity_cut(config: ResolvedConfig, case: Path) -> dict[str, object] | None:
    """Return the actual L3 cell-centred XY cut without probe interpolation."""

    foam_file = case / "production/L3/runs/steady_cht/steady_cht.foam"
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
        "source": "L3 steady_cht cell-centred U on the Z-centre vtkCutter plane",
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

    # Import lazily so the geometry-only viewer remains dependency-free at build
    # time. The extracted browser payload itself uses only plain JSON/Canvas.
    from .postprocess import (
        DISPLAY_TEMPERATURE_OFFSET_C,
        PAPER_FIGURE_RANGES,
        _ScientificBackend,
        read_openfoam_dat,
    )

    backend = _ScientificBackend(foam_file)
    fluid, _ = backend.regions(1800.0)
    all_paths = backend._streamline_paths(fluid, config)
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
            + DISPLAY_TEMPERATURE_OFFSET_C
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
                + DISPLAY_TEMPERATURE_OFFSET_C
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
            + DISPLAY_TEMPERATURE_OFFSET_C
        },
        "OUT": {
            0: config.boundary.initial_fluid_temperature_k
            - 273.15
            + DISPLAY_TEMPERATURE_OFFSET_C
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
                + DISPLAY_TEMPERATURE_OFFSET_C
            )
            port_temperature["OUT"][time_s] = (
                float(row["coolant_tout_K"])
                - 273.15
                + DISPLAY_TEMPERATURE_OFFSET_C
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
        surface_points, _, surface_temperature = backend._surface(transient_solid)
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
                + DISPLAY_TEMPERATURE_OFFSET_C
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
            ) - 273.15 + DISPLAY_TEMPERATURE_OFFSET_C
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

    lengths = [float(path[4]) for path in selected]
    direct_xy_velocity = _direct_l3_xy_velocity_cut(config, case)
    return {
        "enabled": True,
        "semantics": (
            "OpenFOAM cell temperature T(t) with a +0.150 K display-only "
            "datum correction; moving heads follow the validated fixed "
            "velocity field U(x), not transient U(x,t)."
        ),
        "temperature_display_offset_c": DISPLAY_TEMPERATURE_OFFSET_C,
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
            "cell": PAPER_FIGURE_RANGES["figure_09_temperature_degC"],
            "xy": PAPER_FIGURE_RANGES["figure_14_temperature_degC"],
            "xz": PAPER_FIGURE_RANGES["figure_13b_temperature_degC"],
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
        "velocity_max_m_s": max(max(path["s"]) for path in browser_paths),
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
    config: ResolvedConfig, case: Path, *, enabled: bool
) -> dict[str, object] | None:
    """Build the browser payload from the completed L3 1 s direct samples."""

    if not enabled:
        return None
    runs_root = case / "production/L3/runs"
    upgraded_run = runs_root / "frozen_flow_actual_1s_xy5"
    run = (
        upgraded_run
        if (upgraded_run / "reports/viewer/actual_1s_manifest.json").is_file()
        else runs_root / "frozen_flow_actual_1s"
    )
    manifest_path = run / "reports/viewer/actual_1s_manifest.json"
    validation_path = run / "reports/viewer/actual_1s_validation.json"
    foam_file = case / "production/L3/runs/steady_cht/steady_cht.foam"
    if (
        not manifest_path.is_file()
        or not validation_path.is_file()
        or not foam_file.is_file()
    ):
        return None

    from .postprocess import PAPER_FIGURE_RANGES, _ScientificBackend, read_openfoam_dat

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
        "spatial_field_semantics": "validated L3 fixed cell field U(x), p(x)",
    }

    backend = _ScientificBackend(foam_file)
    numeric_times = []
    for path in foam_file.parent.iterdir():
        if path.is_dir():
            try:
                numeric_times.append(float(path.name))
            except ValueError:
                pass
    fluid, _ = backend.regions(max(numeric_times))
    all_paths = backend._streamline_paths(fluid, config)
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
            "spatial_sampling": "direct containing L3 cell; no interpolation or padding",
        }

    datasets = json.loads(json.dumps(manifest["datasets"]))
    for specification in datasets.values():
        specification["file"] = f"data/{specification['file']}"
    temperatures = [value for series in cell_temperature.values() for value in series]
    surface_spec = datasets["cell_surface"]
    return {
        "enabled": True,
        "semantics": (
            "L3 OpenFOAM direct containing-cell T samples at every integer second; "
            "no temporal interpolation, spatial interpolation, display correction, or padding. "
            "Moving heads follow the validated fixed L3 U(x)."
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
            "cell": PAPER_FIGURE_RANGES["figure_09_temperature_degC"],
            "xy": PAPER_FIGURE_RANGES["figure_14_temperature_degC"],
            "xz": PAPER_FIGURE_RANGES["figure_13b_temperature_degC"],
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
            "initial_temperature_k": manifest["initial_temperature_k"],
            "times_s": manifest["times_s"],
            "temperature_encoding": manifest["temperature_encoding"],
            "temporal_interpolation": False,
            "spatial_interpolation": False,
            "padding": False,
            "datasets": datasets,
        },
        "velocity_max_m_s": max(max(path["s"]) for path in browser_paths),
        "streamlines": browser_paths,
        "streamline_count": len(browser_paths),
        "source_streamline_count": len(all_paths),
        "minimum_displayed_length_mm": min(float(path[4]) for path in selected),
        "maximum_displayed_length_mm": max(float(path[4]) for path in selected),
        "sections": sections,
        "direct_xy_velocity": _direct_l3_xy_velocity_cut(config, case),
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
  <title>MDPI 23-cell CFD viewer</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <header>
    <div>
      <p class="eyebrow">MDPI 23-CELL IMMERSION COOLING</p>
      <h1>형상·CFD 결과 뷰어</h1>
    </div>
    <div id="status" class="status">GEOMETRY PREVIEW · CFD 미실행</div>
  </header>
  <main>
    <section class="viewer-panel">
      <div class="viewer-controls">
        <div class="control-head">
          <div class="toolbar" aria-label="camera views">
            <span class="control-title">시점</span>
            <button data-view="px">+X</button><button data-view="nx">−X</button>
            <button data-view="py">+Y</button><button data-view="ny">−Y</button>
            <button data-view="pz">+Z</button><button data-view="nz">−Z</button>
            <button data-view="iso" class="active">ISO</button>
            <button id="reset">맞춤</button>
          </div>
          <fieldset id="display-options" class="display-options" aria-label="Figure 1 visible layers">
            <legend>레이어</legend>
            <label><input id="show-cells" type="checkbox" checked> 셀</label>
            <label><input id="show-flow" type="checkbox" checked> 유선·벡터</label>
            <label><input id="show-particles" type="checkbox" checked> 이동 헤드</label>
            <label><input id="show-section" type="checkbox" checked> 단면</label>
            <label><input id="show-grid" type="checkbox"> 샘플선</label>
            <label><input id="show-enclosure" type="checkbox" checked> 외곽선</label>
            <button id="flow-only" type="button">유동만</button>
            <button id="show-all" type="button">전체</button>
          </fieldset>
        </div>
        <fieldset id="viz-modes" class="viz-modes" aria-label="Figure 1 visualization mode">
          <legend>표시 모드</legend>
          <label><input type="radio" name="viz-mode" value="combined" checked> 종합 T(t)+U(x) · Fig 8/9</label>
          <label><input type="radio" name="viz-mode" value="cell-temperature"> 셀 T(t) · Fig 4/7/8</label>
          <label><input type="radio" name="viz-mode" value="velocity-streamlines"> U(x) 유선 · Fig 10</label>
          <label><input type="radio" name="viz-mode" value="xy-temperature"> XY 온도 T(t)+U(x) · Fig 6/14</label>
          <label><input type="radio" name="viz-mode" value="xy-velocity"> XY 속도 U(x) 고정 · Fig 11</label>
          <label><input type="radio" name="viz-mode" value="xz-temperature"> XZ 온도 · Fig 13</label>
          <label><input type="radio" name="viz-mode" value="xz-velocity"> XZ 속도 · Fig 12</label>
        </fieldset>
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
        <canvas id="scene" aria-label="interactive 3D battery geometry"></canvas>
        <div id="data-load-status" class="data-load-status" role="status" aria-live="polite" hidden></div>
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
        <output id="flow-duration">1× 완주 30:00</output>
        <output id="flow-time">t = 0 s</output>
        <span id="flow-sampling">화면 1 s</span>
        <span id="current-mode" aria-live="polite">종합: 실제 T(t) + 고정 U(x)</span>
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
        <div id="canvas-note" class="canvas-note">드래그: 재생 중 3D 회전 · 휠: 확대/축소 · Ū·p̄ 실제 면적평균 · 포트는 A-04</div>
      </div>
    </section>
    <aside>
      <section id="calculation-card" class="card warning">
        <h2>계산 상태</h2>
        <p id="calculation-summary"><strong>CFD 미실행</strong> — PPT 외곽/노치는 반영됐고 포트 y 좌표와 입구 열 배정은 A-04 기본 가정입니다.</p>
        <p id="calculation-note">이 화면의 색은 셀 구분용이며 온도장이 아닙니다.</p>
      </section>
      <section class="card">
        <h2>확정 형상</h2>
        <dl id="geometry"></dl>
      </section>
      <section class="card">
        <h2>기준 계산값</h2>
        <dl id="derived"></dl>
      </section>
      <section class="card">
        <h2>Table 2·4 계산 입력</h2>
        <dl id="inputs"></dl>
      </section>
      <section class="card temperature-export-card">
        <h2>온도 그래프 추출</h2>
        <dl id="temperature-graph-summary"></dl>
        <button id="temperature-chart-open" type="button">5개 실제 온도 그래프 보기·추출</button>
        <p>0–1800 s, 1 s 간격 실제 function-object 값입니다.</p>
      </section>
      <section class="card">
        <h2>Figure 출력 계약</h2>
        <div id="figures" class="figure-grid"></div>
        <p id="figure-detail">Fig. 5 제외 · 주황색 항목은 실제 solver field 대기 중</p>
      </section>
      <section class="card">
        <h2>셀 ID</h2>
        <div id="cells" class="cell-grid"></div>
      </section>
    </aside>
  </main>
  <footer id="provenance"></footer>
  <script src="model.js"></script>
  <script src="app.js"></script>
</body>
</html>
'''


CSS = '''
:root { color-scheme: dark; --bg:#08111e; --panel:#0e1a2a; --line:#294158;
  --text:#e9f2fa; --muted:#91a8bc; --cyan:#63d7e5; --amber:#ffbd5b; }
* { box-sizing:border-box; }
body { margin:0; min-height:100vh; background:radial-gradient(circle at 30% 0,#17324c 0,#08111e 42%);
  color:var(--text); font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif; }
header { height:86px; display:flex; justify-content:space-between; align-items:center; padding:0 28px;
  border-bottom:1px solid var(--line); background:#08111ecc; }
h1 { margin:1px 0 0; font-size:25px; letter-spacing:-.03em; }
.eyebrow { margin:0; color:var(--cyan); font-size:11px; letter-spacing:.18em; font-weight:700; }
.status { padding:9px 13px; border:1px solid #946321; color:#ffd795; background:#442e123d;
  border-radius:999px; font-size:12px; font-weight:750; }
main { display:grid; grid-template-columns:minmax(640px,1fr) 340px; gap:16px; padding:16px; height:calc(100vh - 118px); }
.viewer-panel { min-height:600px; display:flex; flex-direction:column; border:1px solid var(--line); border-radius:14px;
  overflow:hidden; background:linear-gradient(180deg,#101f31,#09131f); }
.viewer-controls { position:relative; z-index:4; flex:0 0 auto; display:grid; gap:9px; padding:11px 12px;
  border-bottom:1px solid #29465f; background:linear-gradient(180deg,#101f31 0%,#0b1725 100%);
  box-shadow:0 8px 20px #02070d52; }
.viewer-controls[hidden] { display:none; }
.control-head { display:flex; align-items:center; justify-content:space-between; gap:16px; min-width:0; }
.control-title { color:var(--muted); font-size:11px; font-weight:800; letter-spacing:.04em; }
.viewport { position:relative; flex:1 1 auto; min-height:0; overflow:hidden; }
#scene { position:absolute; inset:0; width:100%; height:100%; display:block; cursor:grab; }
#scene:active { cursor:grabbing; }
.data-load-status { position:absolute; z-index:5; top:14px; left:50%; transform:translateX(-50%); max-width:min(760px,calc(100% - 28px));
  padding:9px 12px; border:1px solid #3b8190; border-radius:7px; background:#071724f2; color:#bfeef3;
  font-size:11px; font-weight:700; text-align:center; box-shadow:0 8px 22px #02070d80; }
.data-load-status.error { border-color:#d08a32; background:#2c1c0bf2; color:#ffe0a7; }
.data-load-status[hidden] { display:none; }
.toolbar { display:flex; align-items:center; gap:5px; flex-wrap:wrap; }
.toolbar button { min-width:38px; padding:6px 8px; }
.viz-modes { display:grid; grid-template-columns:repeat(7,minmax(112px,1fr)); gap:6px; margin:0; padding:0;
  border:0; color:#cfe2ef; font-size:11px; min-width:0; }
.viz-modes[hidden] { display:none; }
.viz-modes legend { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0,0,0,0); }
.viz-modes label { min-height:34px; display:flex; align-items:center; justify-content:center; gap:5px; padding:6px 8px;
  border:1px solid #31506a; border-radius:7px; background:#0d2032; white-space:nowrap; cursor:pointer; text-align:center; }
.viz-modes label:hover { border-color:#5793a8; background:#123047; }
.viz-modes label:has(input:checked) { border-color:var(--cyan); color:#e9fdff; background:#123b4a; box-shadow:inset 0 0 0 1px #63d7e533; }
.viz-modes input { margin:0; accent-color:var(--cyan); }
.display-options { display:flex; align-items:center; justify-content:flex-end; gap:6px 10px; flex-wrap:wrap;
  margin:0; padding:0; border:0; color:#b9cedc; font-size:11px; }
.display-options[hidden] { display:none; }
.display-options legend { color:var(--muted); padding:0; margin-right:2px; font-size:11px; font-weight:800; }
.display-options label { display:flex; align-items:center; gap:4px; padding:4px 2px; white-space:nowrap; cursor:pointer; }
.display-options input { margin:0; accent-color:var(--amber); }
.display-options button { padding:5px 8px; font-size:10px; }
.vertical-sections,.horizontal-sections { display:flex; align-items:center; justify-content:center; gap:7px; flex-wrap:wrap;
  margin:0; padding:7px 9px; border:1px solid #31506a; border-radius:8px; background:#081522; color:#c7dae6;
  font-size:11px; }
.vertical-sections[hidden],.horizontal-sections[hidden] { display:none; }
.vertical-sections legend,.horizontal-sections legend { color:var(--cyan); padding:0 5px; font-weight:800; }
.vertical-sections label,.horizontal-sections label { display:flex; align-items:center; gap:5px; min-height:28px; padding:4px 9px;
  border:1px solid #2a455b; border-radius:6px; background:#0c1d2c; white-space:nowrap; cursor:pointer; }
.vertical-sections label:hover,.horizontal-sections label:hover { border-color:#5793a8; }
.vertical-sections label:has(input:checked),.horizontal-sections label:has(input:checked) { border-color:var(--cyan); color:#e8fdff; background:#123b4a; }
.vertical-sections input,.horizontal-sections input { margin:0; accent-color:var(--cyan); }
.vertical-sections button,.horizontal-sections button { padding:5px 9px; font-size:10px; }
#vertical-section-state,#horizontal-section-state { color:var(--amber); font-variant-numeric:tabular-nums; font-weight:700; }
.flow-controls { position:absolute; z-index:3; left:14px; right:14px; bottom:42px; min-height:46px;
  display:flex; align-items:center; justify-content:center; gap:9px; padding:7px 10px;
  border:1px solid #31506a; border-radius:8px; background:#07101be8; color:var(--muted);
  font-size:11px; flex-wrap:wrap; }
.flow-controls[hidden] { display:none; }
.flow-controls button { padding:5px 8px; min-width:72px; }
.flow-controls input[type="range"] { width:min(330px,36vw); accent-color:var(--cyan); }
.flow-controls select { border:1px solid #36536d; border-radius:6px; color:#dcecf7; background:#102338;
  padding:5px 7px; font-weight:700; }
#flow-time { color:var(--cyan); min-width:92px; font-variant-numeric:tabular-nums; font-weight:750; }
#flow-sampling { color:#a9bfd0; white-space:nowrap; }
#flow-duration,#video-duration { color:var(--amber); min-width:92px; font-variant-numeric:tabular-nums; font-weight:750; }
button { border:1px solid #36536d; border-radius:7px; color:#cfe2ef; background:#102338e8;
  padding:7px 10px; font-weight:700; cursor:pointer; }
button:hover,button.active { color:#06131c; background:var(--cyan); border-color:var(--cyan); }
.canvas-note { position:absolute; z-index:2; left:14px; right:14px; bottom:10px; padding:6px 9px; background:#07101bc9;
  border-radius:6px; color:var(--muted); font-size:11px; }
.figure-preview { position:absolute; inset:0; padding:18px; background:#08111e; z-index:1; }
.figure-preview img,.figure-preview video { width:100%; height:calc(100% - 76px); object-fit:contain; display:block; }
.video-controls { min-height:34px; display:flex; align-items:center; justify-content:center; gap:8px;
  color:var(--muted); font-size:11px; padding-top:6px; white-space:nowrap; flex-wrap:wrap; }
.video-controls[hidden] { display:none; }
.video-controls button { padding:4px 7px; font-size:10px; }
.video-controls input[type="range"] { width:min(220px,28vw); accent-color:var(--cyan); }
.video-controls select { border:1px solid #36536d; border-radius:6px; color:#dcecf7; background:#102338;
  padding:5px 7px; font-weight:700; }
#video-time { color:var(--cyan); min-width:180px; font-variant-numeric:tabular-nums; font-weight:750; }
#video-semantics { overflow:hidden; text-overflow:ellipsis; }
.artifact-tabs { display:flex; justify-content:center; gap:7px; padding-top:8px; }
.temperature-chart-panel { position:absolute; inset:0; z-index:7; display:flex; flex-direction:column; gap:8px;
  padding:12px; background:#08111ef7; }
.temperature-chart-panel[hidden] { display:none; }
.temperature-chart-toolbar { display:flex; align-items:center; gap:7px; flex-wrap:wrap; color:var(--muted); font-size:11px; }
.temperature-chart-toolbar button { padding:6px 9px; font-size:10px; }
.temperature-chart-toolbar select { border:1px solid #36536d; border-radius:6px; color:#dcecf7; background:#102338;
  padding:6px 8px; font-weight:700; }
.temperature-chart-canvas-wrap { flex:1 1 auto; min-height:280px; overflow:hidden; }
#temperature-chart-canvas { width:100%; height:100%; display:block; }
#temperature-chart-status { min-height:20px; color:#b9cedc; font-size:11px; font-variant-numeric:tabular-nums; }
.temperature-export-card dl { margin-bottom:11px; }
#temperature-chart-open { width:100%; border-color:#297480; color:#bdeef3; background:#102b3b; }
aside { overflow:auto; padding-right:3px; }
.card { padding:15px 16px; margin-bottom:12px; background:#0e1a2ade; border:1px solid var(--line); border-radius:12px; }
.card h2 { margin:0 0 11px; font-size:14px; color:#dcecf7; }
.card p { color:var(--muted); font-size:12px; line-height:1.55; margin:5px 0; }
.warning { border-color:#775321; background:#2d2114bf; }
.warning strong { color:var(--amber); }
.warning.validated { border-color:#246e65; background:#102f2dbf; }
.warning.validated strong { color:#80eadc; }
dl { margin:0; display:grid; grid-template-columns:1fr auto; gap:7px 12px; font-size:12px; }
dt { color:var(--muted); } dd { margin:0; font-variant-numeric:tabular-nums; font-weight:700; }
.cell-grid { display:grid; grid-template-columns:repeat(8,1fr); gap:5px; }
.cell-grid span { padding:5px 0; text-align:center; border:1px solid #31506a; border-radius:5px;
  color:#aee8ef; background:#102b3b; font-size:10px; }
.figure-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:5px; }
.figure-grid button { padding:6px 2px; text-align:center; border:1px solid #805b29; border-radius:5px;
  color:#ffd08a; background:#302313; font-size:10px; }
.figure-grid button.ready { border-color:#297480; color:#aee8ef; background:#102b3b; }
footer { height:32px; padding:2px 19px; color:#617d91; font-size:10px; overflow:hidden; white-space:nowrap; }
@media (max-width:1280px) {
  main { grid-template-columns:minmax(560px,1fr) 310px; }
  .control-head { align-items:flex-start; flex-direction:column; gap:7px; }
  .display-options { justify-content:flex-start; }
  .viz-modes { grid-template-columns:repeat(4,minmax(126px,1fr)); }
}
@media (max-width:900px) {
  header { height:auto; min-height:76px; gap:12px; padding:14px 18px; }
  .status { max-width:48%; text-align:right; }
  main { grid-template-columns:1fr; height:auto; padding:10px; }
  .viewer-panel { height:78vh; min-height:680px; }
  .viz-modes { grid-template-columns:repeat(2,minmax(138px,1fr)); }
  aside { overflow:visible; }
}
'''


JAVASCRIPT = r'''
"use strict";
const model = window.MDPI_MODEL;
const initialParams=new URLSearchParams(window.location.search);
const canvas = document.getElementById("scene");
const ctx = canvas.getContext("2d");
const viewerControls = document.querySelector(".viewer-controls");
const flow = model.interactive_cfd || {enabled:false};
const sections = flow.sections || {};
const directXYVelocity = flow.direct_xy_velocity || null;
const flowControls = document.getElementById("flow-controls");
const flowPlay = document.getElementById("flow-play");
const flowSeek = document.getElementById("flow-seek");
const flowRate = document.getElementById("flow-rate");
const flowDuration = document.getElementById("flow-duration");
const flowTime = document.getElementById("flow-time");
const flowSampling = document.getElementById("flow-sampling");
const portState = document.getElementById("port-state");
const dataLoadStatus = document.getElementById("data-load-status");
const modeFieldset = document.getElementById("viz-modes");
const modeInputs = Array.from(document.querySelectorAll('input[name="viz-mode"]'));
const displayOptions = document.getElementById("display-options");
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
const visibility={cells:true,flow:true,particles:true,section:true,grid:false,enclosure:true};
const optionInputs={
  cells:document.getElementById("show-cells"),flow:document.getElementById("show-flow"),
  particles:document.getElementById("show-particles"),section:document.getElementById("show-section"),
  grid:document.getElementById("show-grid"),enclosure:document.getElementById("show-enclosure")
};
let yaw = 2.18, pitch = 0.52, zoom = 1, dragging = false, lastX = 0, lastY = 0;
const requestedMode=initialParams.get("mode");
if(requestedMode){const requestedInput=modeInputs.find(input=>input.value===requestedMode);if(requestedInput){for(const input of modeInputs)input.checked=input===requestedInput;}}
const requestedTime=Number(initialParams.get("t"));
let simulationTime = Number.isFinite(requestedTime)?Math.max(0,Math.min(1800,requestedTime)):0, flowPlaying = Boolean(flow.enabled)&&initialParams.get("autoplay")!=="0", previousFrameTime = 0, sceneRenderAccumulator = 0, lastSceneFrame = -1;
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
  window.MDPI_PACKED_DATA=window.MDPI_PACKED_DATA||{};
  if(!window.MDPI_PACKED_DATA[name])await new Promise((resolve,reject)=>{
    const script=document.createElement("script");
    script.src=specification.portable_file;
    script.onload=resolve;
    script.onerror=()=>reject(new Error(`${specification.portable_file}: local data script missing`));
    document.head.appendChild(script);
  });
  const packed=window.MDPI_PACKED_DATA[name];
  if(!packed)throw new Error(`${name}: local packed payload missing`);
  if(typeof DecompressionStream!=="function")throw new Error("이 브라우저는 로컬 CFD 압축 해제를 지원하지 않습니다. 최신 Chrome, Edge 또는 Safari를 사용하십시오.");
  const compressed=base64Bytes(packed.payload);
  const stream=new Blob([compressed]).stream().pipeThrough(new DecompressionStream("gzip"));
  const buffer=await new Response(stream).arrayBuffer();
  delete window.MDPI_PACKED_DATA[name];
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
const vertices=model.outline_mm.map(p=>[p[0],p[1],0]).concat(model.outline_mm.map(p=>[p[0],p[1],box.z]));
const n=model.outline_mm.length,edges=[];
for(let i=0;i<n;i++){const j=(i+1)%n;edges.push([i,j],[i+n,j+n],[i,i+n]);}

function clamp(value,low=0,high=1){return Math.max(low,Math.min(high,value));}
function jet(value){
  const t=clamp(value),r=clamp(1.5-Math.abs(4*t-3)),g=clamp(1.5-Math.abs(4*t-2)),b=clamp(1.5-Math.abs(4*t-1));
  return [Math.round(255*r),Math.round(255*g),Math.round(255*b)];
}
let directXYVelocityCache=null;
function buildDirectXYVelocityCache(){
  if(directXYVelocityCache||!directXYVelocity)return directXYVelocityCache;
  const factor=8,width=Math.round(box.x*factor),height=Math.round(box.y*factor);
  const target=document.createElement("canvas");target.width=width;target.height=height;
  const targetContext=target.getContext("2d"),range=directXYVelocity.velocity_range_m_s;
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
  ctx.fillText(`L3 ACTUAL CELL VALUES · ${directXYVelocity.triangle_count.toLocaleString()} CUT TRIANGLES · NO PADDING`,layout.plotLeft,layout.plotTop-12);
  ctx.fillStyle="rgba(145,168,188,.98)";ctx.font="650 10px system-ui";
  ctx.fillText("FROZEN-FLOW U(x): 색상장은 고정 · 이동 마커는 실제 벡터 방향 표시",layout.plotLeft,layout.plotBottom+22);
  return true;
}
function drawDirectXYGrid(section,field,w,h,showScalar=true){
  if(!section)return false;
  const layout=directXYLayout(w,h),range=field==="temperature"?section.temperature_range_c:section.velocity_range_m_s;
  ctx.save();ctx.beginPath();ctx.rect(layout.plotLeft,layout.plotTop,layout.plotRight-layout.plotLeft,layout.plotBottom-layout.plotTop);ctx.clip();
  ctx.fillStyle="rgba(7,16,27,.98)";ctx.fillRect(layout.left,layout.top,layout.width,layout.height);
  if(showScalar&&range){
    const stepX=Math.abs(section.column_step_mm[0])*layout.scale,stepY=Math.abs(section.row_step_mm[1])*layout.scale;
    for(let index=0;index<section.columns*section.rows;index++){
      const value=field==="temperature"?sectionTemperature(section,index):section.velocity_m_s[index];if(!Number.isFinite(value))continue;
      const point=planePoint(section,index),x=layout.left+(box.x-point[0])*layout.scale,y=layout.top+point[1]*layout.scale;
      const colour=jet((value-range[0])/Math.max(1e-12,range[1]-range[0]));
      ctx.fillStyle=`rgb(${colour[0]},${colour[1]},${colour[2]})`;ctx.fillRect(x-stepX/2,y-stepY/2,stepX+.35,stepY+.35);
      if(visibility.grid){ctx.strokeStyle="rgba(225,238,246,.13)";ctx.lineWidth=.3;ctx.strokeRect(x-stepX/2,y-stepY/2,stepX,stepY);}
    }
  }
  drawDirectXYGeometry(layout);ctx.restore();
  ctx.fillStyle="rgba(232,242,250,.96)";ctx.font="700 10px ui-monospace,monospace";
  ctx.fillText(`L3 DIRECT CELL SAMPLES · ${section.columns} × ${section.rows} · ${section.label} · NO INTERPOLATION / PADDING`,layout.plotLeft,layout.plotTop-12);
  return true;
}
function drawDirectXYVelocityVectors(layout,movingOnly=false){
  const section=horizontalSection();if(!section?.velocity_vectors_m_s)return;
  const limit=Math.max(1e-9,section.velocity_range_m_s[1]);
  ctx.save();ctx.beginPath();ctx.rect(layout.plotLeft,layout.plotTop,layout.plotRight-layout.plotLeft,layout.plotBottom-layout.plotTop);ctx.clip();
  for(let row=2;row<section.rows-2;row+=5)for(let column=2;column<section.columns-2;column+=5){
    const index=row*section.columns+column,vector=section.velocity_vectors_m_s[index];if(!vector)continue;
    const magnitude=Math.hypot(...vector),planarMagnitude=Math.hypot(vector[0],vector[1]);
    if(magnitude<limit*.012||planarMagnitude<1e-10)continue;
    const point=planePoint(section,index),x=layout.left+(box.x-point[0])*layout.scale,y=layout.top+point[1]*layout.scale;
    const directionX=-vector[0]/planarMagnitude,directionY=vector[1]/planarMagnitude;
    const length=6+12*Math.sqrt(clamp(magnitude/limit)),endX=x+directionX*length,endY=y+directionY*length;
    if(!movingOnly){
      ctx.strokeStyle="rgba(242,249,253,.72)";ctx.lineWidth=.85;ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(endX,endY);ctx.stroke();
      const angle=Math.atan2(directionY,directionX),head=3.2;
      ctx.beginPath();ctx.moveTo(endX,endY);ctx.lineTo(endX-head*Math.cos(angle-.55),endY-head*Math.sin(angle-.55));
      ctx.moveTo(endX,endY);ctx.lineTo(endX-head*Math.cos(angle+.55),endY-head*Math.sin(angle+.55));ctx.stroke();
    }else if(visibility.particles){
      const phase=((simulationTime/8+index*.381966)%1+1)%1;
      for(let tail=2;tail>=0;tail--){
        const fraction=((phase-tail*.075)%1+1)%1,mx=x+directionX*length*fraction,my=y+directionY*length*fraction;
        ctx.beginPath();ctx.arc(mx,my,tail===0?2.05:1.25,0,2*Math.PI);
        ctx.fillStyle=tail===0?"rgba(255,255,255,.98)":`rgba(106,221,239,${.18+(2-tail)*.2})`;ctx.fill();
      }
    }
  }
  ctx.restore();
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
function surfaceBoundaryTemperatures(face){
  if(actualOneSecond)return null;
  if(!cellSurface||!face.kind.endsWith("-side")||!Number.isInteger(face.axialIndex))return null;
  const segments=cellSurface.angular_segments,rows=cellSurface.axial_segments,current=surfaceThermalValue(face.id,face.surfaceIndex);
  if(!Number.isFinite(current))return null;
  const below=face.axialIndex>0?surfaceThermalValue(face.id,(face.axialIndex-1)*segments+face.angularIndex):current;
  const above=face.axialIndex<rows-1?surfaceThermalValue(face.id,(face.axialIndex+1)*segments+face.angularIndex):current;
  return {lower:.5*(current+below),upper:.5*(current+above)};
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
function updatePortStateText(inletMetrics=portMetrics("IN"),outletMetrics=portMetrics("OUT")){
  const temperatureSemantics=actualOneSecond?"T 원본(무보정)":"T 표시보정 +"+flow.temperature_display_offset_c.toFixed(3)+" K";
  portState.textContent=`${temperatureSemantics} · IN T ${inletMetrics.temperatureC.toFixed(3)} °C / Ū ${inletMetrics.velocityMS.toFixed(3)} m/s / p̄ ${(inletMetrics.pressurePa/1000).toFixed(3)} kPa · OUT T ${outletMetrics.temperatureC.toFixed(3)} °C / Ū ${outletMetrics.velocityMS.toFixed(3)} m/s / p̄ ${(outletMetrics.pressurePa/1000).toFixed(3)} kPa · Δp ${flow.port_flow.pressure_drop_pa.toFixed(3)} Pa`;
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
function flowPoint(path,fraction){
  const wrapped=((fraction%1)+1)%1,position=wrapped*(path.p.length-1);
  const left=Math.floor(position),right=Math.min(path.p.length-1,left+1),weight=position-left;
  const point=[0,1,2].map(axis=>path.p[left][axis]+weight*(path.p[right][axis]-path.p[left][axis]));
  const speed=path.s[left]+weight*(path.s[right]-path.s[left]);
  return {point,speed};
}
function drawScalarLegend(w,h,title,range,unit,referenceRange=null){
  if(!flow.enabled||!range)return;
  const [low,high]=range,x=18,y=Math.max(168,h*.19),height=Math.min(320,h*.43),width=12;
  const gradient=ctx.createLinearGradient(0,y+height,0,y);
  for(let index=0;index<=8;index++){const t=index/8,[r,g,b]=jet(t);gradient.addColorStop(t,`rgb(${r},${g},${b})`);}
  ctx.fillStyle=gradient;ctx.fillRect(x,y,width,height);ctx.strokeStyle="rgba(230,240,250,.7)";ctx.strokeRect(x,y,width,height);
  ctx.fillStyle="rgba(232,242,250,.96)";ctx.font="700 11px system-ui";ctx.fillText(title,x-1,y-24);
  const scaleLabel=actualOneSecond?"RAW CFD · NO INTERPOLATION":`DISPLAY CAL. +${flow.temperature_display_offset_c.toFixed(3)} K`;
  ctx.fillStyle="rgba(145,168,188,.96)";ctx.font="600 9px system-ui";ctx.fillText(unit==="C"?scaleLabel:"ACTUAL CFD SCALE",x-1,y-10);
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
function bilinear(values,u,v){
  return values[0]*(1-u)*(1-v)+values[1]*u*(1-v)+values[2]*u*v+values[3]*(1-u)*v;
}
function bilinearPoint(points,u,v){
  return [0,1,2].map(axis=>bilinear(points.map(point=>point[axis]),u,v));
}
function drawActualSection(section,basis,scale,cx,cy,range,alpha){
  const quads=[];
  for(let index=0;index<section.columns*section.rows;index++){
    const value=sectionTemperature(section,index);if(!Number.isFinite(value))continue;
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
  const range=rangeOverride||(field==="temperature"?section.temperature_range_c:section.velocity_range_m_s);
  if(field==="temperature"&&actualOneSecond&&section.actual_dataset){drawActualSection(section,basis,scale,cx,cy,range,alpha);return;}
  const quads=[];
  for(let row=0;row<section.rows-1;row++)for(let column=0;column<section.columns-1;column++){
    const ids=[row*section.columns+column,row*section.columns+column+1,(row+1)*section.columns+column+1,(row+1)*section.columns+column];
    const values=ids.map(index=>field==="temperature"?sectionTemperature(section,index):section.velocity_m_s[index]);
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
  const selected=verticalSelection(),ranges=selected.map(section=>field==="temperature"?section.temperature_range_c:section.velocity_range_m_s).filter(Boolean);
  if(!ranges.length)return null;
  return [Math.min(...ranges.map(range=>range[0])),Math.max(...ranges.map(range=>range[1]))];
}
function drawSectionLabel(section,basis,scale,cx,cy){
  const point=projected(section.origin_mm,basis,scale,cx,cy),label=section.label||"XZ";
  ctx.save();ctx.font="700 9px ui-monospace,monospace";
  const width=ctx.measureText(label).width+8;
  ctx.fillStyle="rgba(5,14,24,.82)";ctx.fillRect(point.x+4,point.y-15,width,15);
  ctx.fillStyle="rgba(255,215,149,.98)";ctx.fillText(label,point.x+8,point.y-4);ctx.restore();
}
function drawSectionVelocity(section,basis,scale,cx,cy){
  if(!section||!section.velocity_vectors_m_s)return;
  const limit=Math.max(1e-9,section.velocity_range_m_s[1]);
  for(let row=1;row<section.rows-1;row+=2)for(let column=1;column<section.columns-1;column+=2){
    const index=row*section.columns+column,vector=section.velocity_vectors_m_s[index];
    if(!vector)continue;
    const magnitude=Math.hypot(...vector);if(magnitude<limit*.012)continue;
    const point=planePoint(section,index),length=3+8*Math.sqrt(clamp(magnitude/limit));
    const direction=vector.map(value=>value/magnitude),end=point.map((value,axis)=>value+direction[axis]*length);
    const a=projected(point,basis,scale,cx,cy),b=projected(end,basis,scale,cx,cy);
    ctx.strokeStyle="rgba(245,251,255,.82)";ctx.lineWidth=.8;ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
    if(visibility.particles){
      const phase=((simulationTime/210+index*.381966)%1),moving=point.map((value,axis)=>value+direction[axis]*length*phase);
      const marker=projected(moving,basis,scale,cx,cy);ctx.beginPath();ctx.arc(marker.x,marker.y,1.7,0,2*Math.PI);ctx.fillStyle="rgba(255,255,255,.96)";ctx.fill();
    }
  }
}
function drawFlow(paths,basis,scale,cx,cy){
  if(!flow.enabled)return;
  for(const path of paths){
    for(let index=1;index<path.p.length;index++){
      const a=projected(path.p[index-1],basis,scale,cx,cy),b=projected(path.p[index],basis,scale,cx,cy);
      const speed=0.5*(path.s[index-1]+path.s[index]),strength=clamp(speed/flow.velocity_max_m_s);
      const [r,g,bColor]=jet(strength);
      ctx.strokeStyle=visualMode==="velocity-streamlines"
        ?`rgba(${Math.min(255,Math.round(.45*r+80))},${Math.min(255,Math.round(.45*g+90))},${Math.min(255,Math.round(.45*bColor+130))},${.58+.24*strength})`
        :"rgba(100,123,237,.72)";
      ctx.lineWidth=visualMode==="velocity-streamlines"?.8+1.25*strength:.9;ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
    }
  }
}
function drawFlowParticles(paths,basis,scale,cx,cy){
  if(!flow.enabled||!visibility.particles)return;
  const phase=simulationTime/30;
  for(let index=0;index<paths.length;index++){
    const path=paths[index],base=phase+index*.61803398875;
    for(let tail=4;tail>=0;tail--){
      const sample=flowPoint(path,base-tail*.012),screen=projected(sample.point,basis,scale,cx,cy);
      const strength=clamp(sample.speed/flow.velocity_max_m_s),[r,g,bColor]=jet(strength);
      const alpha=.13+(4-tail)*.18,radius=1.2+(4-tail)*.62;
      ctx.beginPath();ctx.arc(screen.x,screen.y,radius,0,2*Math.PI);
      ctx.fillStyle=tail===0?`rgba(255,255,255,.96)`:`rgba(${r},${g},${bColor},${alpha})`;ctx.fill();
      if(tail===0){ctx.strokeStyle=`rgba(${r},${g},${bColor},.95)`;ctx.lineWidth=1.3;ctx.stroke();}
    }
  }
}

const sceneBaseCache=document.createElement("canvas");
function resize(){
  const ratio=Math.min(window.devicePixelRatio||1,2), rect=canvas.getBoundingClientRect();
  canvas.width=Math.max(1,Math.floor(rect.width*ratio)); canvas.height=Math.max(1,Math.floor(rect.height*ratio));
  ctx.setTransform(ratio,0,0,ratio,0,0); draw();
}
function drawScene(){
  const w=canvas.clientWidth,h=canvas.clientHeight; ctx.clearRect(0,0,w,h);
  const basis=cameraBasis(), scale=Math.min(w/(box.x*1.65),h/(box.y*1.65))*zoom;
  const cx=w/2,cy=h/2+8;
  const thermalCells=["combined","cell-temperature","xz-temperature"].includes(visualMode);
  const thermalDisplay=flow.enabled&&(thermalCells||visualMode==="xy-temperature");
  const velocityDisplay=flow.enabled&&["velocity-streamlines","xy-velocity","xz-velocity"].includes(visualMode);
  const sectionMode=visualMode.startsWith("xy-")||visualMode.startsWith("xz-");
  if(visualMode.startsWith("xy-")&&horizontalSection()){
    const selectedHorizontal=horizontalSection();
    if(visualMode==="xy-temperature")drawDirectXYGrid(selectedHorizontal,"temperature",w,h,visibility.section);
    else if(horizontalSectionKey()==="xy"&&directXYVelocity)drawDirectXYVelocity(w,h,visibility.section);
    else drawDirectXYGrid(selectedHorizontal,"velocity",w,h,visibility.section);
    if(visibility.flow)drawDirectXYVelocityVectors(directXYLayout(w,h));
    if(flow.enabled){
      updatePortStateText();
      if(visibility.section&&visualMode==="xy-temperature")drawScalarLegend(w,h,"Temperature · XY",selectedHorizontal.temperature_range_c,"C",flow.paper_temperature_ranges_c?.xy);
      if(visibility.section&&visualMode==="xy-velocity")drawScalarLegend(w,h,"XY velocity",horizontalSectionKey()==="xy"&&directXYVelocity?directXYVelocity.velocity_range_m_s:selectedHorizontal.velocity_range_m_s,"m/s");
      flowTime.textContent=`t = ${Math.round(simulationTime)} s / 1800 s`;
    }
    return;
  }
  const sorted=faces.map(face=>({face,depth:face.points.reduce((s,p)=>s+projected(p,basis,scale,cx,cy).d,0)/face.points.length})).sort((a,b)=>a.depth-b.depth);
  for(const item of sorted){
    const pts=item.face.points.map(p=>projected(p,basis,scale,cx,cy));
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
      const normalised=flow.port_flow[key]/Math.max(1e-9,flow.velocity_max_m_s),[r,g,b]=jet(normalised);
      ctx.fillStyle=`rgba(${r},${g},${b},.88)`;
    }
    else if(isPort){ctx.fillStyle=`rgba(239,147,43,${0.62+0.14*shade})`;}
    else if(thermalCells&&temperature!==null){
      const range=cellSurface?.temperature_range_c||flow.temperature_range_c,normalised=(temperature-range[0])/Math.max(1e-9,range[1]-range[0]);
      const [r,g,b]=jet(normalised);
      const boundaries=surfaceBoundaryTemperatures(item.face);
      if(boundaries){
        const lowerPoint={x:.5*(pts[0].x+pts[1].x),y:.5*(pts[0].y+pts[1].y)},upperPoint={x:.5*(pts[2].x+pts[3].x),y:.5*(pts[2].y+pts[3].y)};
        const gradient=ctx.createLinearGradient(lowerPoint.x,lowerPoint.y,upperPoint.x,upperPoint.y);
        const lowerColour=jet((boundaries.lower-range[0])/Math.max(1e-9,range[1]-range[0])),upperColour=jet((boundaries.upper-range[0])/Math.max(1e-9,range[1]-range[0]));
        gradient.addColorStop(0,`rgba(${lowerColour[0]},${lowerColour[1]},${lowerColour[2]},.95)`);
        gradient.addColorStop(.5,`rgba(${r},${g},${b},.95)`);
        gradient.addColorStop(1,`rgba(${upperColour[0]},${upperColour[1]},${upperColour[2]},.95)`);
        ctx.fillStyle=gradient;
      }else ctx.fillStyle=`rgba(${r},${g},${b},.95)`;
    }else if(sectionMode||visualMode==="velocity-streamlines"){
      ctx.fillStyle=`rgba(12,28,43,${.08+.07*shade})`;
    }else{ctx.fillStyle=item.face.kind.endsWith("cap")?`rgba(79,207,222,${0.66+0.13*shade})`:`rgba(32,112,145,${0.68+0.16*shade})`;}
    ctx.fill();
    ctx.strokeStyle=thermalCells&&!isPort?"rgba(225,238,246,.055)":"rgba(225,238,246,.26)";ctx.lineWidth=thermalCells&&!isPort?.25:.55;ctx.stroke();
  }
  if(visibility.section){
    if(visualMode==="xy-temperature")drawSection(horizontalSection(),"temperature",basis,scale,cx,cy,null,.82,3);
    if(visualMode==="xy-velocity"){
      if(horizontalSectionKey()!=="xy"||!drawDirectXYVelocity(w,h))drawSection(horizontalSection(),"velocity",basis,scale,cx,cy,null,.82,3);
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
    updatePortStateText(inletMetrics,outletMetrics);
    if(["combined","cell-temperature"].includes(visualMode)&&visibility.cells)drawScalarLegend(w,h,"Cell surface T",cellSurface?.temperature_range_c||flow.temperature_range_c,"C",flow.paper_temperature_ranges_c?.cell);
    if(visualMode==="velocity-streamlines"&&visibility.flow)drawScalarLegend(w,h,"Velocity",[0,flow.velocity_max_m_s],"m/s");
    if(visualMode==="xy-temperature"&&visibility.section)drawScalarLegend(w,h,"Temperature · XY",horizontalSection()?.temperature_range_c,"C",flow.paper_temperature_ranges_c?.xy);
    if(visualMode==="xy-velocity"&&visibility.section)drawScalarLegend(w,h,"XY velocity",horizontalSectionKey()==="xy"?directXYVelocity?.velocity_range_m_s:horizontalSection()?.velocity_range_m_s,"m/s");
    if(visualMode==="xz-temperature"&&visibility.section)drawScalarLegend(w,h,"Temperature · XZ",verticalRange("temperature"),"C",flow.paper_temperature_ranges_c?.xz);
    if(visualMode==="xz-velocity"&&visibility.section)drawScalarLegend(w,h,"XZ velocity",verticalRange("velocity"),"m/s");
    flowTime.textContent=`t = ${Math.round(simulationTime)} s / 1800 s`;
  }else{ctx.fillStyle="rgba(255,189,91,.92)";ctx.font="700 11px system-ui";ctx.fillText("Ø19 mm IN / OUT — A-04 assumed centres",16,h-34);}
}
function cacheCurrentScene(){
  sceneBaseCache.width=canvas.width;sceneBaseCache.height=canvas.height;
  sceneBaseCache.getContext("2d").drawImage(canvas,0,0);
}
function restoreCachedScene(){
  if(!sceneBaseCache.width||!sceneBaseCache.height)return;
  ctx.save();ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(sceneBaseCache,0,0);ctx.restore();
}
function drawDynamicOverlay(){
  restoreCachedScene();
  if(!flow.enabled||!visibility.flow)return;
  if(visualMode.startsWith("xy-")&&horizontalSection()){
    drawDirectXYVelocityVectors(directXYLayout(canvas.clientWidth,canvas.clientHeight),true);
    flowTime.textContent=`t = ${Math.round(simulationTime)} s / 1800 s`;return;
  }
  if(!["combined","velocity-streamlines"].includes(visualMode))return;
  const w=canvas.clientWidth,h=canvas.clientHeight,basis=cameraBasis(),scale=Math.min(w/(box.x*1.65),h/(box.y*1.65))*zoom;
  drawFlowParticles(flow.streamlines||[],basis,scale,w/2,h/2+8);
  flowTime.textContent=`t = ${Math.round(simulationTime)} s / 1800 s`;
}
function draw(){drawScene();cacheCurrentScene();lastSceneFrame=Math.round(simulationTime);drawDynamicOverlay();}
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
  combined:["종합 T(t)+U(x) · Fig 8/9","L3 직접 셀 T(x,t)와 검증된 고정 U(x) 유선을 함께 표시"],
  "cell-temperature":["셀 표면 T(x,t) · Fig 4/7/8",`L3 batterySolid 직접 셀값을 각도 ${cellSurface?.angular_segments||28} × 높이 ${cellSurface?.axial_segments||12}로 표시`],
  "velocity-streamlines":["U(x) 유선 · Fig 10","유속으로 색칠한 고정 U(x) 유선과 이동 헤드"],
  "xy-temperature":["XY 온도 T(t)+U(x) · Fig 6/14","선택한 Z 위치의 1 mm 직접 셀 T(x,t)와 고정 U(x) 이동 마커"],
  "xy-velocity":["XY 속도 · Fig 11 · 고정 U(x)","선택한 Z 위치의 실제 L3 셀 중심 U값 표시 · 중앙은 절단 삼각형, 나머지는 1 mm 직접 셀 표본 · 색상장은 고정 · 보간/패딩 없음"],
  "xz-temperature":["XZ 온도 · Fig 13","선택한 Y 위치들의 약 1 × 1.49 mm 직접 셀 T(x,t) 단면"],
  "xz-velocity":["XZ 속도 · Fig 12","선택한 Y 위치들의 고정 XZ 속도 크기와 방향 마커"]
};
function prepareActualMode(mode){
  if(!actualOneSecond)return;
  const names=[];
  if(["combined","cell-temperature"].includes(mode))names.push("cell_surface");
  if(mode==="xy-temperature")names.push(horizontalSectionKey());
  if(mode==="xz-temperature")names.push(...verticalSectionInputs.filter(input=>input.checked).map(input=>input.value));
  const pending=names.filter(name=>!actualDatasets.has(name));
  if(!pending.length){setDataLoadStatus("");return;}
  const source=location.protocol==="file:"?"로컬 압축 데이터":"직접 셀 데이터";
  currentMode.textContent=`실제 1 s 셀값 로드 중 · ${pending.join(", ")}`;
  setDataLoadStatus(`Figure 데이터 로드 중 · ${source} · ${pending.join(", ")}`);
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
function selectVisualMode(mode){
  visualMode=mode;
  setDataLoadStatus("");
  const description=modeDescriptions[mode]||modeDescriptions.combined;
  const horizontalPosition=mode.startsWith("xy-")?` · ${horizontalSection()?.label||"Z = 50%"}`:"";
  currentMode.textContent=description[0]+horizontalPosition;
  const temperatureNote=actualOneSecond?"T 원본 · 보간/패딩 없음":`온도 표시보정 +${flow.temperature_display_offset_c.toFixed(3)} K`;
  canvasNote.textContent=`${description[1]}${horizontalPosition} · 드래그 회전 · 휠 확대/축소 · ${temperatureNote} · Ū·p̄ 실제 면적평균`;
  prepareActualMode(mode);
  horizontalSections.hidden=!mode.startsWith("xy-");
  verticalSections.hidden=!mode.startsWith("xz-");
  if(mode.startsWith("xy-"))selectView("pz");
  else if(mode.startsWith("xz-"))selectView(verticalSelection().length>1?"iso":"py");
  else selectView("iso");
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
if(flow.enabled){
  flowSampling.textContent=actualOneSecond?"CFD 원본 1 s · 직접 셀값 · 시간보간/패딩 없음":"화면 1 s · CFD 원본 15 s 선형보간";
  flowSeek.value=String(simulationTime);
  flowPlay.textContent=flowPlaying?"❚❚ 정지":"▶ 재생";
  flowPlay.addEventListener("click",()=>{flowPlaying=!flowPlaying;flowPlay.textContent=flowPlaying?"❚❚ 정지":"▶ 재생";});
  flowSeek.addEventListener("input",()=>{simulationTime=Number(flowSeek.value);draw();});
  flowRate.addEventListener("change",updateFlowDuration);updateFlowDuration();
}
function animate(timestamp){
  if(previousFrameTime===0)previousFrameTime=timestamp;
  const elapsed=Math.min(.1,(timestamp-previousFrameTime)/1000);previousFrameTime=timestamp;
  if(flow.enabled&&flowPlaying&&figurePreview.hidden){
    simulationTime=(simulationTime+elapsed*Number(flowRate.value))%1800;
    flowSeek.value=String(Math.round(simulationTime));sceneRenderAccumulator+=elapsed;
    const actualFrame=Math.round(simulationTime);
    const refreshScene=actualOneSecond
      ?actualFrame!==lastSceneFrame&&sceneRenderAccumulator>=1/12
      :sceneRenderAccumulator>=1/12;
    if(refreshScene){sceneRenderAccumulator=0;draw();}
    else drawDynamicOverlay();
  }
  advanceVideoPlayback(elapsed);
  requestAnimationFrame(animate);
}
function rows(element,values){ element.innerHTML=Object.entries(values).map(([key,value])=>`<dt>${key}</dt><dd>${value}</dd>`).join(""); }
rows(document.getElementById("geometry"),{"외함":`${box.x} × ${box.y} × ${box.z} mm`,"노치 깊이":`${box.notch_depth} mm`,"배터리":`${model.cells.length} × Ø${model.cell_diameter_mm} × ${model.cell_length_mm} mm`,"포트":`Ø${model.port_diameter_mm} / Ø${model.port_diameter_mm} mm`,"배열":model.topology,"x / y pitch":`${model.pitch_mm.x} / ${model.pitch_mm.y} mm`});
rows(document.getElementById("derived"),{"Re (Table 4)":model.derived.reynolds.toLocaleString(undefined,{maximumFractionDigits:1}),"논문 Re":model.derived.paper_reynolds.toLocaleString(),"셀당 발열":`${model.derived.cell_heat_w.toFixed(6)} W`,"총 발열":`${model.derived.total_heat_w.toFixed(6)} W`,"q‴":`${model.derived.qvol_w_m3.toLocaleString()} W/m³`});
const rawInletC=model.inputs.inlet_temperature_K-273.15;
const displayedInletC=rawInletC+(flow.temperature_display_offset_c||0);
const temperatureInputRows={"입구 속도":`${model.inputs.inlet_velocity_m_s} m/s`,"입구 온도 원본":`${model.inputs.inlet_temperature_K} K = ${rawInletC.toFixed(3)} °C`};
temperatureInputRows[actualOneSecond?"Figure 1 온도 처리":"Figure 1 표시보정"]=actualOneSecond?"무보정 · K−273.15만 적용":`+${(flow.temperature_display_offset_c||0).toFixed(3)} K → ${displayedInletC.toFixed(3)} °C`;
Object.assign(temperatureInputRows,{"냉각수 밀도":`${model.inputs.fluid_density_kg_m3} kg/m³`,"동점도 입력 μ":`${model.inputs.dynamic_viscosity_Pa_s} Pa·s`,"냉각수 Cp":`${model.inputs.fluid_cp_J_kgK} J/(kg·K)`,"냉각수 k":`${model.inputs.fluid_k_W_mK} W/(m·K)`});
rows(document.getElementById("inputs"),temperatureInputRows);
const figureGrid=document.getElementById("figures"),figureDetail=document.getElementById("figure-detail");
const figurePreview=document.getElementById("figure-preview"),figureImage=document.getElementById("figure-image"),figureVideo=document.getElementById("figure-video"),artifactTabs=document.getElementById("artifact-tabs");
const videoControls=document.getElementById("video-controls"),playbackRate=document.getElementById("playback-rate"),videoTime=document.getElementById("video-time"),videoSemantics=document.getElementById("video-semantics"),videoDuration=document.getElementById("video-duration");
const videoPlay=document.getElementById("video-play"),videoSeek=document.getElementById("video-seek"),videoPrev=document.getElementById("video-prev"),videoNext=document.getElementById("video-next");
const temperatureGraphs=flow.temperature_graphs||null;
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
  context.fillStyle="#e9f2fa";context.font="700 18px system-ui";context.fillText(`${graph.title} · 실제 L3 CFD · 1 s`,24,28);
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
  flowPlaying=false;flowPlay.textContent="▶ 재생";videoPlaying=false;figureVideo.pause();figurePreview.hidden=true;
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
figureGrid.innerHTML=model.figures.map(item=>`<button data-figure="${item.id}" class="${item.status.endsWith("_READY")?"ready":""}" title="${item.status}">Fig ${item.id}</button>`).join("");
const initialFigure=model.figures.find(item=>item.id===1);
if(initialFigure)figureDetail.textContent=`Fig. 1 · ${initialFigure.title} · ${initialFigure.status} · 필요 데이터: ${initialFigure.required.join(", ")}`;
figureGrid.querySelectorAll("[data-figure]").forEach(button=>button.addEventListener("click",()=>{
  temperatureGraphPanel.hidden=true;
  const item=model.figures.find(candidate=>String(candidate.id)===button.dataset.figure);
  figureDetail.textContent=`Fig. ${item.id} · ${item.title} · ${item.status} · 필요 데이터: ${item.required.join(", ")}`;
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
document.getElementById("cells").innerHTML=model.cells.map(c=>`<span>${c.id}</span>`).join("");
document.getElementById("status").textContent=model.status_label;
document.getElementById("calculation-summary").innerHTML=model.calculation_summary;
document.getElementById("calculation-note").textContent=model.calculation_note;
document.getElementById("calculation-card").classList.toggle("validated",model.actual_solver_data);
document.getElementById("provenance").textContent=`resolved ${model.resolved_input_sha256} · geometry ${model.geometry_sha256}`;
window.addEventListener("resize",()=>{resize();drawTemperatureChart();});resize();requestAnimationFrame(animate);
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
        "window.MDPI_PACKED_DATA = window.MDPI_PACKED_DATA || {};\n"
        f"window.MDPI_PACKED_DATA[{json.dumps(name)}] = "
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
        status = "L0_CFD_RESULTS_READY"
        mesh_passed = mesh_independence.get("status") == "MESH_INDEPENDENCE_PASSED"
        status_label = (
            "L0–L3 격자 독립성 PASS · 검증 결과"
            if mesh_passed
            else "L0 CFD PASS · 논문 최종 아님"
        )
        calculation_summary = (
            "<strong>실제 CFD 검증 완료</strong> — L0 1800 s 열 과도와 "
            + ("L0–L3 격자 독립성" if mesh_passed else "L0 solver gate")
            + "를 통과했습니다."
        )
        if mesh_passed:
            comparison = mesh_independence["comparisons"]["L2_to_L3"]
            calculation_note = (
                f"L0 1800 s 체적 Tmax {final_tmax_k:.6f} K · "
                f"L2→L3 ΔTmax {float(comparison['battery_tmax_difference_k']):.6f} K · "
                "Figure 1의 이동 헤드는 검증된 고정 U(x)를 따르며 "
                "과도속도장이 아닙니다."
            )
        else:
            calculation_note = (
                f"최종 체적 Tmax {final_tmax_k:.6f} K · Figure 3의 L1–L3 "
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
    interactive_cfd = _interactive_cfd_payload(
        config,
        case if case is not None else Path("."),
        enabled=actual_solver_data and case is not None,
    )
    actual_one_second = None
    if interactive_cfd.get("enabled"):
        actual_one_second = interactive_cfd.get("actual_one_second")
        if actual_one_second and case is not None:
            runs_root = case / "production/L3/runs"
            upgraded_source = (
                runs_root / "frozen_flow_actual_1s_xy5/reports/viewer"
            )
            source_dir = (
                upgraded_source
                if (upgraded_source / "actual_1s_manifest.json").is_file()
                else runs_root / "frozen_flow_actual_1s/reports/viewer"
            )
            data_dir = output / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            for name, specification in actual_one_second["datasets"].items():
                source = source_dir / Path(specification["file"]).name
                destination = data_dir / source.name
                shutil.copy2(source, destination)
                raw = source.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                specification["file"] = f"data/{source.name}?v={digest[:12]}"
                # Keep the file:// sidecar query-free: some desktop browsers treat
                # a query string as part of a local filename. HTTP uses the hashed
                # binary URL above and therefore retains cache busting.
                specification["portable_file"] = f"data/{source.name}.js"
                specification["sha256"] = digest
                specification["byte_length"] = len(raw)
                _atomic_write(
                    data_dir / f"{source.name}.js",
                    _portable_dataset_javascript(name, raw),
                )
            source_manifest = source_dir / "actual_1s_manifest.json"
            shutil.copy2(source_manifest, data_dir / source_manifest.name)
            source_validation = source_dir / "actual_1s_validation.json"
            shutil.copy2(source_validation, data_dir / source_validation.name)
            status = "L3_ACTUAL_ONE_SECOND_RESULTS_READY"
            status_label = "L3 · 1 s 실제 셀값 · 20.000 °C 원본 · 보간/패딩 없음"
            calculation_summary = (
                "<strong>L3 실제 CFD 재계산 완료</strong> — 1,316,854셀, "
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
                "title": "Interactive T(t) + U(x) flow",
                "status": (
                    "L3_ACTUAL_ONE_SECOND_INTERACTIVE_READY"
                    if actual_one_second
                    else "L0_CFD_INTERACTIVE_READY"
                ),
                "required": [
                    "actual_T_t",
                    "actual_battery_surface_T_xt",
                    "actual_XY_XZ_T_t",
                    "five_vertical_XZ_slices",
                    "validated_fixed_U",
                    "streamline_seed",
                ],
                "time_mode": "transient_temperature_fixed_velocity",
            }
        )
    model = {
        "schema_version": 1,
        "status": status,
        "status_label": status_label,
        "actual_solver_data": actual_solver_data,
        "interactive_cfd": interactive_cfd,
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
    _atomic_write(output / "index.html", HTML)
    _atomic_write(output / "style.css", CSS)
    _atomic_write(output / "app.js", JAVASCRIPT)
    _atomic_write(
        output / "model.js",
        "window.MDPI_MODEL = "
        + json.dumps(model, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + ";\n",
    )
    _atomic_write(
        output / "viewer_manifest.json",
        json.dumps(model, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(
        output / "README_VIEWER.txt",
        "MDPI CFD viewer\n\n"
        "Keep this entire folder together and double-click index.html.\n"
        "Figure 13 and the other temperature modes load compressed direct-cell data\n"
        "from the data folder on demand, even when the page is opened as file://.\n"
        "Use '5 actual temperature graphs' to view outlet, battery maximum,\n"
        "surface average, internal average, and C01-C23 histories, then export\n"
        "the current graph as PNG/CSV or all metrics as one CSV.\n"
        "Use a current Chrome, Edge, or Safari browser. Do not copy index.html alone.\n",
    )
    return model
