from __future__ import annotations

import csv
import json
import math
import os
from pathlib import Path
from typing import Iterable

from .config import ResolvedConfig
from .errors import MdpiCfdError
from .geometry import cell_placements
from .openfoam_cold_flow_validation import parse_cold_flow_log
from .result_contract import figure_catalog, update_figure_status


VALIDATED_STATUSES = {
    "production_mesh": "PRODUCTION_MESH_GATE_PASSED",
    "cold_flow": "COLD_FLOW_VALIDATION_PASSED",
    "steady_cht": "STEADY_CHT_VALIDATION_PASSED",
    "frozen_flow": "FROZEN_FLOW_VALIDATION_PASSED",
}

PAPER_FIGURE_RANGES = {
    "figure_08_temperature_degC": [20.008, 20.155],
    "figure_09_temperature_degC": [20.008, 20.155],
    "figure_10_velocity_m_s": [0.0, 0.565],
    "figure_11_velocity_m_s": [0.0, 0.218],
    "figure_12_velocity_m_s": [0.0, 0.166],
    "figure_13a_temperature_degC": [20.008, 20.155],
    "figure_13b_temperature_degC": [20.0, 20.137],
    "figure_14_temperature_degC": [20.0, 20.151],
}

# The validated run used the rounded 293 K datum. Plotting adds the requested
# 0.150 K reference correction so figures start at exactly 20.000 degC while
# all persisted solver fields and validation metrics remain untouched.
DISPLAY_TEMPERATURE_OFFSET_C = 0.15


def read_openfoam_dat(path: str | Path) -> list[tuple[float, ...]]:
    """Read an OpenFOAM function-object table without trusting its header text."""
    rows: list[tuple[float, ...]] = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            values = tuple(float(token) for token in stripped.split())
        except ValueError as exc:
            raise MdpiCfdError(f"invalid OpenFOAM table row in {path}: {line}") from exc
        if len(values) < 2 or not all(math.isfinite(value) for value in values):
            raise MdpiCfdError(f"invalid OpenFOAM table values in {path}: {line}")
        rows.append(values)
    if not rows:
        raise MdpiCfdError(f"OpenFOAM table has no data rows: {path}")
    return rows


def _series(path: Path, column: int = 1) -> dict[float, float]:
    return {row[0]: row[column] for row in read_openfoam_dat(path)}


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def _load_required_reports(level_dir: Path) -> dict[str, dict[str, object]]:
    paths = {
        "production_mesh": level_dir / "reports/production_mesh/production_mesh_gate.json",
        "cold_flow": level_dir / "runs/steady_cold_flow/reports/solver/cold_flow_validation.json",
        "steady_cht": level_dir / "runs/steady_cht/reports/solver/steady_cht_validation.json",
        "frozen_flow": level_dir / "runs/frozen_flow/reports/solver/frozen_flow_validation.json",
    }
    reports: dict[str, dict[str, object]] = {}
    for name, path in paths.items():
        if not path.is_file():
            raise MdpiCfdError(f"required validation report is missing: {path}")
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("status") != VALIDATED_STATUSES[name]:
            raise MdpiCfdError(
                f"{name} gate is not passed: {report.get('status', 'UNKNOWN')}"
            )
        reports[name] = report
    return reports


def _collect_cell_rows(run_dir: Path) -> tuple[list[dict[str, object]], dict[str, dict[str, dict[float, float]]]]:
    root = run_dir / "postProcessing/batterySolid"
    collected: dict[str, dict[str, dict[float, float]]] = {}
    output: list[dict[str, object]] = []
    common_times: set[float] | None = None
    for index in range(1, 24):
        cell_id = f"C{index:02d}"
        prefix = f"battery_{cell_id}_surface_T"
        metrics = {
            name: _series(root / f"{prefix}{suffix}/0/surfaceFieldValue.dat")
            for name, suffix in (("minimum", "min"), ("average", "avg"), ("maximum", "max"))
        }
        times = set.intersection(*(set(values) for values in metrics.values()))
        common_times = times if common_times is None else common_times & times
        collected[cell_id] = metrics
    if common_times is None or len(common_times) < 1800:
        raise MdpiCfdError("complete C01-C23 one-second surface telemetry was not found")
    for time_s in sorted(common_times):
        for cell_id in sorted(collected):
            values = collected[cell_id]
            output.append(
                {
                    "time_s": f"{time_s:g}",
                    "cell_id": cell_id,
                    "surface_tmin_K": f"{values['minimum'][time_s]:.12g}",
                    "surface_tavg_K": f"{values['average'][time_s]:.12g}",
                    "surface_tmax_K": f"{values['maximum'][time_s]:.12g}",
                    "heat_flux_avg_W_m2": "",
                    "value_status": "ACTUAL_L0_CFD_TEMPERATURE;HEAT_FLUX_NOT_COMPUTED",
                }
            )
    return output, collected


def _collect_coolant_series(run_dir: Path) -> dict[str, dict[float, float]]:
    root = run_dir / "postProcessing/coolant"
    return {
        "inlet_mass": _series(root / "coolant_inlet_mass_flow/0/surfaceFieldValue.dat"),
        "outlet_mass": _series(root / "coolant_outlet_mass_flow/0/surfaceFieldValue.dat"),
        "tin": _series(root / "coolant_inlet_mass_weighted_temperature/0/surfaceFieldValue.dat"),
        "tout": _series(root / "coolant_outlet_mass_weighted_temperature/0/surfaceFieldValue.dat"),
        "pin": _series(root / "coolant_inlet_pressure_avg/0/surfaceFieldValue.dat"),
        "pout": _series(root / "coolant_outlet_pressure_avg/0/surfaceFieldValue.dat"),
        "umax": _series(root / "coolant_field_bounds/0/fieldMinMax.dat", 4),
    }


def _collect_residual_rows(level_dir: Path) -> list[dict[str, object]]:
    log_dir = level_dir / "runs/steady_cht/logs/solver"
    log_paths = sorted(log_dir.glob("steady_cht*.log"))
    rows: list[dict[str, object]] = []
    seen: set[tuple[float, str, str, int]] = set()
    for log_path in log_paths:
        parsed = parse_cold_flow_log(log_path)
        for time_text, record in parsed["records"].items():
            iteration = float(time_text)
            for qualified, values in record["residuals"].items():
                region, field = qualified.split(".", 1)
                for corrector, value in enumerate(values, start=1):
                    key = (iteration, region, field, corrector)
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(
                        {
                            "iteration": f"{iteration:g}",
                            "simulation_time_s": "",
                            "region": region,
                            "field": field,
                            "initial_residual": f"{value['initial']:.12g}",
                            "final_residual": f"{value['final']:.12g}",
                        }
                    )
    rows.sort(key=lambda row: (float(row["iteration"]), row["region"], row["field"]))
    return rows


class _ScientificBackend:
    def __init__(self, foam_file: Path, *, subtitle: str | None = None):
        matplotlib_cache = foam_file.parent / ".matplotlib-cache"
        matplotlib_cache.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache))
        os.environ.setdefault("XDG_CACHE_HOME", str(matplotlib_cache))
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import numpy as np
            import vtk
            from vtk.util.numpy_support import vtk_to_numpy
        except ImportError as exc:
            raise MdpiCfdError(
                "L0 figure generation requires the 'results' extra: "
                "python3 -m pip install -e '.[results]'"
            ) from exc
        self.plt = plt
        self.np = np
        self.vtk = vtk
        self.vtk_to_numpy = vtk_to_numpy
        self.reader = vtk.vtkOpenFOAMReader()
        self.reader.SetFileName(str(foam_file))
        self.reader.CreateCellToPointOn()
        self.reader.EnableAllCellArrays()
        self.reader.EnableAllPointArrays()
        self.reader.DisableAllPatchArrays()
        self.reader.SetPatchArrayStatus("internalMesh", 1)
        self.reader.UpdateInformation()
        self.subtitle = subtitle or (
            "Validated L0 CFD · actual solver fields · mesh independence L1–L3 pending"
        )

    def _region(self, name: str):
        output = self.reader.GetOutput()
        vtk = self.vtk
        for index in range(output.GetNumberOfBlocks()):
            metadata = output.GetMetaData(index)
            block_name = metadata.Get(vtk.vtkCompositeDataSet.NAME()) if metadata else None
            if block_name != name:
                continue
            parent = output.GetBlock(index)
            if hasattr(parent, "GetNumberOfBlocks"):
                for child_index in range(parent.GetNumberOfBlocks()):
                    child = parent.GetBlock(child_index)
                    if child is not None and child.GetNumberOfCells() > 0:
                        return child
            return parent
        raise MdpiCfdError(f"OpenFOAM reader did not expose region {name}")

    def regions(self, time_s: float):
        self.reader.UpdateTimeStep(float(time_s))
        return self._region("coolant"), self._region("batterySolid")

    def _cell_volumes(self, dataset):
        sizes = self.vtk.vtkCellSizeFilter()
        sizes.SetInputData(dataset)
        sizes.ComputeAreaOff()
        sizes.ComputeLengthOff()
        sizes.ComputeVertexCountOff()
        sizes.ComputeVolumeOn()
        sizes.Update()
        return self.vtk_to_numpy(sizes.GetOutput().GetCellData().GetArray("Volume")).copy()

    def energy_rows(
        self,
        config: ResolvedConfig,
        coolant: dict[str, dict[float, float]],
        *,
        authoritative_fluid_volume_m3: float,
        authoritative_solid_volume_m3: float,
        end_time_s: int = 1800,
        interval_s: int = 15,
    ) -> tuple[list[dict[str, object]], float]:
        np = self.np
        fluid, solid = self.regions(0.0)
        fluid_volumes = self._cell_volumes(fluid)
        solid_volumes = self._cell_volumes(solid)
        # vtkCellSizeFilter is slightly biased for OpenFOAM polyhedra. Preserve its
        # relative cell weights, but anchor both sums to the direct polyMesh gate.
        fluid_volumes *= authoritative_fluid_volume_m3 / float(fluid_volumes.sum())
        solid_volumes *= authoritative_solid_volume_m3 / float(solid_volumes.sum())
        generated_w = (
            float(config.battery.heat_source.qvol_w_m3)
            * authoritative_solid_volume_m3
        )
        previous_energy: float | None = None
        rows: list[dict[str, object]] = []
        velocity_average = math.nan
        for time_s in range(0, end_time_s + 1, interval_s):
            fluid, solid = self.regions(float(time_s))
            fluid_t = self.vtk_to_numpy(fluid.GetCellData().GetArray("T"))
            solid_t = self.vtk_to_numpy(solid.GetCellData().GetArray("T"))
            sensible_j = float(
                np.sum(
                    config.fluid.density_kg_m3
                    * config.fluid.specific_heat_j_kgk
                    * fluid_volumes
                    * (fluid_t - config.boundary.initial_fluid_temperature_k)
                )
                + np.sum(
                    config.battery.density_kg_m3
                    * config.battery.specific_heat_j_kgk
                    * solid_volumes
                    * (solid_t - config.boundary.initial_solid_temperature_k)
                )
            )
            if time_s == end_time_s:
                velocity = self.vtk_to_numpy(fluid.GetCellData().GetArray("U"))
                velocity_average = float(
                    np.sum(np.linalg.norm(velocity, axis=1) * fluid_volumes)
                    / np.sum(fluid_volumes)
                )
            if time_s == 0:
                previous_energy = sensible_j
                continue
            stored_w = (sensible_j - float(previous_energy)) / interval_s
            previous_energy = sensible_j
            mass = 0.5 * (
                abs(coolant["inlet_mass"][float(time_s)])
                + abs(coolant["outlet_mass"][float(time_s)])
            )
            removed_w = (
                mass
                * config.fluid.specific_heat_j_kgk
                * (coolant["tout"][float(time_s)] - coolant["tin"][float(time_s)])
            )
            imbalance = abs(generated_w - removed_w - stored_w) / generated_w
            rows.append(
                {
                    "time_s": f"{time_s:g}",
                    "generated_W": f"{generated_w:.12g}",
                    "removed_W": f"{removed_w:.12g}",
                    "stored_W": f"{stored_w:.12g}",
                    "imbalance_fraction": f"{imbalance:.12g}",
                }
            )
        return rows, velocity_average

    def _triangulated_cut(self, dataset, origin, normal, array_name: str):
        plane = self.vtk.vtkPlane()
        plane.SetOrigin(*origin)
        plane.SetNormal(*normal)
        cutter = self.vtk.vtkCutter()
        cutter.SetCutFunction(plane)
        cutter.SetInputData(dataset)
        triangles = self.vtk.vtkTriangleFilter()
        triangles.SetInputConnection(cutter.GetOutputPort())
        triangles.Update()
        output = triangles.GetOutput()
        points = self.vtk_to_numpy(output.GetPoints().GetData()).copy()
        cells = self.np.array(
            [
                [output.GetCell(index).GetPointId(item) for item in range(3)]
                for index in range(output.GetNumberOfCells())
            ],
            dtype=int,
        )
        if array_name == "Umag":
            values = self.np.linalg.norm(
                self.vtk_to_numpy(output.GetPointData().GetArray("U")), axis=1
            )
        else:
            values = self.vtk_to_numpy(output.GetPointData().GetArray(array_name)).copy()
        return points, cells, values

    def _surface(self, dataset):
        surface = self.vtk.vtkDataSetSurfaceFilter()
        surface.SetInputData(dataset)
        triangles = self.vtk.vtkTriangleFilter()
        triangles.SetInputConnection(surface.GetOutputPort())
        triangles.Update()
        output = triangles.GetOutput()
        points = self.vtk_to_numpy(output.GetPoints().GetData()).copy()
        cells = self.np.array(
            [
                [output.GetCell(index).GetPointId(item) for item in range(3)]
                for index in range(output.GetNumberOfCells())
            ],
            dtype=int,
        )
        temperature = self.vtk_to_numpy(output.GetPointData().GetArray("T")).copy()
        return points, cells, temperature

    def _streamline_paths(self, fluid, config: ResolvedConfig):
        output = self._streamline_output(fluid, config)
        points = self.vtk_to_numpy(output.GetPoints().GetData()).copy()
        velocity = self.vtk_to_numpy(output.GetPointData().GetArray("U")).copy()
        temperature = self.vtk_to_numpy(output.GetPointData().GetArray("T")).copy()
        paths: list[tuple[object, object, object, object, float]] = []
        for index in range(output.GetNumberOfCells()):
            cell = output.GetCell(index)
            ids = self.np.asarray(
                [cell.GetPointId(item) for item in range(cell.GetNumberOfPoints())],
                dtype=int,
            )
            if len(ids) < 5:
                continue
            path_points = points[ids]
            path_velocity = velocity[ids]
            segment_lengths = self.np.linalg.norm(
                self.np.diff(path_points, axis=0), axis=1
            )
            path_length = float(segment_lengths.sum())
            # The inlet pipe alone is about 32 mm. Retaining paths longer than
            # 35 mm ensures the rendered flow visibly enters the cell pack.
            if path_length < 0.035:
                continue
            if float(self.np.dot(path_points[1] - path_points[0], path_velocity[0])) < 0:
                ids = ids[::-1]
                path_points = points[ids]
                path_velocity = velocity[ids]
                segment_lengths = self.np.linalg.norm(
                    self.np.diff(path_points, axis=0), axis=1
                )
            cumulative_mm = self.np.concatenate(
                ([0.0], self.np.cumsum(segment_lengths) * 1000.0)
            )
            paths.append(
                (
                    path_points * 1000.0,
                    self.np.linalg.norm(path_velocity, axis=1),
                    temperature[ids] - 273.15,
                    cumulative_mm,
                    float(cumulative_mm[-1]),
                )
            )
        if not paths:
            raise MdpiCfdError("stream tracer produced no inlet-fed paths")
        return paths

    def _streamlines(self, fluid, colour_field: str, config: ResolvedConfig):
        paths = self._streamline_paths(fluid, config)
        segments: list[object] = []
        segment_values: list[float] = []
        for path_points, path_speed, path_temperature, _, _ in paths:
            values = path_temperature if colour_field == "T" else path_speed
            stride = max(1, len(path_points) // 400)
            path_points = path_points[::stride]
            values = values[::stride]
            for first, second, value in zip(
                path_points,
                path_points[1:],
                0.5 * (values[:-1] + values[1:]),
            ):
                segments.append(self.np.stack((first, second)))
                segment_values.append(float(value))
        return segments, self.np.asarray(segment_values)

    def _streamline_output(self, fluid, config: ResolvedConfig):
        fluid.GetPointData().SetActiveVectors("U")
        inlet_source = self.vtk.vtkDiskSource()
        inlet_source.SetInnerRadius(0.0008)
        inlet_source.SetOuterRadius(0.46 * config.ports.inlet_diameter_m)
        inlet_source.SetRadialResolution(7)
        inlet_source.SetCircumferentialResolution(48)
        inlet_x, inlet_y = config.ports.inlet_center_xy_m
        transform = self.vtk.vtkTransform()
        transform.Translate(
            inlet_x,
            inlet_y,
            config.enclosure.height_z_m + 0.82 * config.ports.external_length_m,
        )
        transformed_source = self.vtk.vtkTransformPolyDataFilter()
        transformed_source.SetTransform(transform)
        transformed_source.SetInputConnection(inlet_source.GetOutputPort())
        tracer = self.vtk.vtkStreamTracer()
        tracer.SetInputData(fluid)
        tracer.SetSourceConnection(transformed_source.GetOutputPort())
        tracer.SetIntegratorTypeToRungeKutta45()
        tracer.SetIntegrationDirectionToForward()
        tracer.SetMaximumPropagation(0.55)
        # vtkStreamTracer defaults to CELL_LENGTH_UNIT. The following step
        # values are metres, so LENGTH_UNIT is essential; without it every path
        # exhausts the 2,000-step default after only roughly 15 mm.
        tracer.SetIntegrationStepUnit(tracer.LENGTH_UNIT)
        tracer.SetInitialIntegrationStep(0.0004)
        tracer.SetMinimumIntegrationStep(0.00005)
        tracer.SetMaximumIntegrationStep(0.0015)
        tracer.SetMaximumNumberOfSteps(8000)
        tracer.SetComputeVorticity(False)
        tracer.Update()
        return tracer.GetOutput()

    def _subtitle(self, figure) -> None:
        figure.text(
            0.5,
            0.012,
            self.subtitle,
            ha="center",
            fontsize=8,
            color="#555555",
        )

    def _save(self, figure, path: Path) -> None:
        self._subtitle(figure)
        figure.savefig(path, dpi=220, bbox_inches="tight", facecolor="white")
        self.plt.close(figure)

    def _plot_cut(self, axis, cut, axes, *, cmap, vmin, vmax, label):
        from matplotlib.tri import Triangulation

        points, cells, values = cut
        triangulation = Triangulation(points[:, axes[0]] * 1000, points[:, axes[1]] * 1000, cells)
        artist = axis.tripcolor(
            triangulation, values, shading="gouraud", cmap=cmap, vmin=vmin, vmax=vmax
        )
        axis.set_aspect("equal")
        axis.set_xlabel(f"{label[0]} [mm]")
        axis.set_ylabel(f"{label[1]} [mm]")
        return artist

    def _temperature_colorbar(
        self,
        figure,
        artist,
        *,
        ax,
        vmin: float,
        vmax: float,
        shrink: float | None = None,
        pad: float | None = None,
        location: str | None = None,
        paper_reference: tuple[float, float] | list[float] | None = None,
    ):
        from matplotlib.ticker import AutoMinorLocator, FormatStrFormatter

        options: dict[str, object] = {}
        if shrink is not None:
            options["shrink"] = shrink
        if pad is not None:
            options["pad"] = pad
        if location is not None:
            options["location"] = location
        colorbar = figure.colorbar(artist, ax=ax, **options)
        if location == "left":
            colorbar.ax.yaxis.set_ticks_position("right")
        colorbar.set_ticks(self.np.linspace(vmin, vmax, 19))
        colorbar.ax.yaxis.set_major_formatter(FormatStrFormatter("%.3f"))
        colorbar.ax.yaxis.set_minor_locator(AutoMinorLocator(5))
        colorbar.ax.tick_params(which="major", length=4, width=0.7, labelsize=7.2)
        colorbar.ax.tick_params(which="minor", length=2, width=0.45)
        colorbar.ax.set_title("Temperature", fontsize=8.5, pad=7, loc="left")
        colorbar.ax.text(0.0, -0.055, "[C]", transform=colorbar.ax.transAxes, fontsize=8)
        if paper_reference is not None:
            colorbar.ax.text(
                0.0,
                -0.105,
                f"display cal. +{DISPLAY_TEMPERATURE_OFFSET_C:.3f} K\n"
                f"paper ref {paper_reference[0]:.3f}-{paper_reference[1]:.3f}",
                transform=colorbar.ax.transAxes,
                fontsize=6.4,
                color="#765119",
                va="top",
            )
        return colorbar

    def generate_figures(
        self,
        config: ResolvedConfig,
        cell_data: dict[str, dict[str, dict[float, float]]],
        artifact_dir: Path,
        *,
        time_s: float = 1800.0,
    ) -> dict[int, Path]:
        from matplotlib.collections import LineCollection
        from mpl_toolkits.mplot3d.art3d import Line3DCollection, Poly3DCollection
        from matplotlib.colors import Normalize

        plt = self.plt
        np = self.np
        artifact_dir.mkdir(parents=True, exist_ok=True)
        fluid, solid = self.regions(time_s)
        z_mid = config.enclosure.height_z_m / 2
        y_mid = config.enclosure.length_y_m / 2
        xy_fluid_t = self._triangulated_cut(fluid, (0, 0, z_mid), (0, 0, 1), "T")
        xy_solid_t = self._triangulated_cut(solid, (0, 0, z_mid), (0, 0, 1), "T")
        xy_speed = self._triangulated_cut(fluid, (0, 0, z_mid), (0, 0, 1), "Umag")
        zx_fluid_t = self._triangulated_cut(fluid, (0, y_mid, 0), (0, 1, 0), "T")
        zx_solid_t = self._triangulated_cut(solid, (0, y_mid, 0), (0, 1, 0), "T")
        zx_speed = self._triangulated_cut(fluid, (0, y_mid, 0), (0, 1, 0), "Umag")
        for cut in (xy_fluid_t, xy_solid_t, zx_fluid_t, zx_solid_t):
            cut[2][:] += -273.15 + DISPLAY_TEMPERATURE_OFFSET_C
        all_temperature = np.concatenate((xy_fluid_t[2], xy_solid_t[2], zx_fluid_t[2], zx_solid_t[2]))
        tmin, tmax = float(all_temperature.min()), float(all_temperature.max())
        speed_max = float(np.percentile(np.concatenate((xy_speed[2], zx_speed[2])), 99.5))
        paths: dict[int, Path] = {}

        # Figure 2: actual central-plane mesh, including both regions.
        fig, ax = plt.subplots(figsize=(8.2, 7.2))
        for cut, colour, width in ((xy_fluid_t, "#86a4b8", 0.08), (xy_solid_t, "#e46d42", 0.16)):
            from matplotlib.tri import Triangulation

            tri = Triangulation(cut[0][:, 0] * 1000, cut[0][:, 1] * 1000, cut[1])
            ax.triplot(tri, color=colour, linewidth=width)
        ax.set_aspect("equal")
        ax.set_xlabel("X [mm]")
        ax.set_ylabel("Y [mm]")
        ax.set_title("Figure 2 · L0 production mesh, XY centre section")
        paths[2] = artifact_dir / "figure_02_l0_mesh_xy.png"
        self._save(fig, paths[2])

        # Figure 4: final per-cell surface min/average/max.
        ids = sorted(cell_data)
        minima = np.array([cell_data[cell]["minimum"][time_s] - 273.15 + DISPLAY_TEMPERATURE_OFFSET_C for cell in ids])
        averages = np.array([cell_data[cell]["average"][time_s] - 273.15 + DISPLAY_TEMPERATURE_OFFSET_C for cell in ids])
        maxima = np.array([cell_data[cell]["maximum"][time_s] - 273.15 + DISPLAY_TEMPERATURE_OFFSET_C for cell in ids])
        fig, ax = plt.subplots(figsize=(11.0, 5.5))
        x = np.arange(len(ids))
        ax.fill_between(x, minima, maxima, color="#ffd166", alpha=0.28, label="surface min–max")
        ax.plot(x, averages, "o-", color="#b23a48", linewidth=1.8, markersize=4, label="surface average")
        ax.plot(x, maxima, color="#d95f02", linewidth=0.9, label="surface maximum")
        ax.set_xticks(x, ids, rotation=55)
        ax.set_ylabel("Temperature [°C]")
        ax.set_title("Figure 4 · Battery surface temperature at 1800 s")
        ax.grid(alpha=0.2)
        ax.legend(ncol=3)
        paths[4] = artifact_dir / "figure_04_cell_surface_temperature_1800s.png"
        self._save(fig, paths[4])

        # Figure 6: coupled coolant temperature and velocity at the centre plane.
        fig, axes = plt.subplots(1, 2, figsize=(13.0, 5.6), constrained_layout=True)
        artist = self._plot_cut(axes[0], xy_fluid_t, (0, 1), cmap="jet", vmin=tmin, vmax=tmax, label="XY")
        self._temperature_colorbar(fig, artist, ax=axes[0], vmin=tmin, vmax=tmax)
        axes[0].set_title("Coolant temperature")
        artist = self._plot_cut(axes[1], xy_speed, (0, 1), cmap="jet", vmin=0, vmax=speed_max, label="XY")
        fig.colorbar(artist, ax=axes[1], label="Velocity magnitude [m/s]")
        axes[1].set_title("Coolant velocity")
        fig.suptitle("Figure 6 · Fluid temperature and velocity at 1800 s")
        paths[6] = artifact_dir / "figure_06_fluid_temperature_velocity_xy_1800s.png"
        self._save(fig, paths[6])

        # Figure 7: C01-C23 surface-average histories.
        fig, ax = plt.subplots(figsize=(11.5, 6.0))
        for cell_id in ids:
            times = np.array(sorted(cell_data[cell_id]["average"]))
            values = np.array([cell_data[cell_id]["average"][time] - 273.15 + DISPLAY_TEMPERATURE_OFFSET_C for time in times])
            ax.plot(times, values, linewidth=0.8, alpha=0.72, label=cell_id)
        overall_max = np.array(
            [max(cell_data[cell]["maximum"][time] for cell in ids) - 273.15 + DISPLAY_TEMPERATURE_OFFSET_C for time in times]
        )
        ax.plot(times, overall_max, color="black", linewidth=2.2, label="overall Tmax")
        ax.set_xlabel("Time [s]")
        ax.set_ylabel("Temperature [°C]")
        ax.set_title("Figure 7 · C01-C23 battery surface-temperature histories")
        ax.grid(alpha=0.2)
        ax.legend(ncol=6, fontsize=7, loc="center left", bbox_to_anchor=(1.01, 0.5))
        paths[7] = artifact_dir / "figure_07_cell_temperature_history.png"
        self._save(fig, paths[7])

        surface_points, surface_cells, surface_t = self._surface(solid)
        surface_c = surface_t - 273.15 + DISPLAY_TEMPERATURE_OFFSET_C
        face_c = surface_c[surface_cells].mean(axis=1)

        def add_domain_wireframe(
            axis, *, include_cells: bool, include_enclosure: bool = True
        ) -> None:
            outline = np.asarray(config.enclosure.outline_polygon_m) * 1000
            closed = np.vstack((outline, outline[0]))
            if include_enclosure:
                for z_value in (0.0, config.enclosure.height_z_m * 1000):
                    axis.plot(
                        closed[:, 0], closed[:, 1], z_value,
                        color="#555555", linewidth=0.45, alpha=0.50,
                    )
                for x_value, y_value in outline:
                    axis.plot(
                        [x_value, x_value], [y_value, y_value],
                        [0, config.enclosure.height_z_m * 1000],
                        color="#777777", linewidth=0.3, alpha=0.36,
                    )
            theta = np.linspace(0, 2 * np.pi, 64)
            if include_cells:
                radius = 0.5 * config.cells.diameter_m * 1000
                bottom = 0.5 * (
                    config.enclosure.height_z_m - config.cells.length_m
                ) * 1000
                top = bottom + config.cells.length_m * 1000
                for placement in cell_placements(config):
                    x_value = placement.center_x_m * 1000 + radius * np.cos(theta)
                    y_value = placement.center_y_m * 1000 + radius * np.sin(theta)
                    for z_value in (bottom, top):
                        axis.plot(
                            x_value, y_value, z_value,
                            color="#595959", linewidth=0.34, alpha=0.38,
                        )
                    for angle in np.linspace(0, 2 * np.pi, 5)[:-1]:
                        x_edge = placement.center_x_m * 1000 + radius * np.cos(angle)
                        y_edge = placement.center_y_m * 1000 + radius * np.sin(angle)
                        axis.plot(
                            [x_edge, x_edge], [y_edge, y_edge], [bottom, top],
                            color="#777777", linewidth=0.26, alpha=0.30,
                        )
            port_radius = 0.5 * config.ports.inlet_diameter_m * 1000
            for centre, z_values in (
                (
                    config.ports.inlet_center_xy_m,
                    (
                        config.enclosure.height_z_m * 1000,
                        (config.enclosure.height_z_m + config.ports.external_length_m) * 1000,
                    ),
                ),
                (
                    config.ports.outlet_center_xy_m,
                    (-config.ports.external_length_m * 1000, 0.0),
                ),
            ):
                x_value = centre[0] * 1000 + port_radius * np.cos(theta)
                y_value = centre[1] * 1000 + port_radius * np.sin(theta)
                for z_value in z_values:
                    axis.plot(
                        x_value, y_value, z_value,
                        color="#454545", linewidth=0.48, alpha=0.58,
                    )
                for angle in np.linspace(0, 2 * np.pi, 7)[:-1]:
                    x_edge = centre[0] * 1000 + port_radius * np.cos(angle)
                    y_edge = centre[1] * 1000 + port_radius * np.sin(angle)
                    axis.plot(
                        [x_edge, x_edge], [y_edge, y_edge], list(z_values),
                        color="#555555", linewidth=0.34, alpha=0.46,
                    )

        def add_surface(axis, *, alpha: float = 1.0):
            collection = Poly3DCollection(
                surface_points[surface_cells] * 1000,
                linewidths=0.02,
                edgecolors="none",
                cmap="jet",
                norm=Normalize(tmin, tmax),
                alpha=alpha,
            )
            collection.set_array(face_c)
            axis.add_collection3d(collection)
            axis.set_xlim(0, config.enclosure.width_x_m * 1000)
            axis.set_ylim(0, config.enclosure.length_y_m * 1000)
            axis.set_zlim(0, config.enclosure.height_z_m * 1000)
            axis.set_box_aspect((114, 121, 82))
            axis.view_init(elev=28, azim=125)
            axis.set_xlabel("X [mm]")
            axis.set_ylabel("Y [mm]")
            axis.set_zlabel("Z [mm]")
            add_domain_wireframe(axis, include_cells=False)
            return collection

        # Figure 8: actual 3D battery surface temperature.
        fig = plt.figure(figsize=(9.0, 7.2))
        ax = fig.add_subplot(111, projection="3d")
        artist = add_surface(ax)
        self._temperature_colorbar(
            fig, artist, ax=ax, vmin=tmin, vmax=tmax, shrink=0.68, pad=0.08, location="left",
            paper_reference=PAPER_FIGURE_RANGES["figure_08_temperature_degC"],
        )
        ax.set_title("Figure 8 · 3D battery surface temperature at 1800 s")
        paths[8] = artifact_dir / "figure_08_battery_surface_temperature_3d_1800s.png"
        self._save(fig, paths[8])

        def streamline_figure(field: str, figure_id: int, title: str, label: str, cmap: str):
            segments, values = self._streamlines(fluid, field, config)
            fig = plt.figure(figsize=(9.2, 7.3))
            ax = fig.add_subplot(111, projection="3d")
            if field == "Umag":
                low, high = 0.0, float(np.max(values))
            else:
                low, high = float(np.percentile(values, 1)), float(np.percentile(values, 99))
            lines = Line3DCollection(
                segments, cmap=cmap, norm=Normalize(low, high),
                linewidths=0.72, alpha=0.86,
            )
            lines.set_array(values)
            ax.add_collection3d(lines)
            ax.set_xlim(0, config.enclosure.width_x_m * 1000)
            ax.set_ylim(0, config.enclosure.length_y_m * 1000)
            ax.set_zlim(-config.ports.external_length_m * 1000, (config.enclosure.height_z_m + config.ports.external_length_m) * 1000)
            ax.set_box_aspect((114, 121, 146))
            ax.view_init(elev=27, azim=125)
            ax.set_xlabel("X [mm]")
            ax.set_ylabel("Y [mm]")
            ax.set_zlabel("Z [mm]")
            add_domain_wireframe(ax, include_cells=True)
            ax.set_title(title)
            fig.colorbar(lines, ax=ax, shrink=0.68, pad=0.08, label=label)
            path = artifact_dir / f"figure_{figure_id:02d}_{field.lower()}_streamlines_1800s.png"
            self._save(fig, path)
            return path

        # Figure 9 follows the paper semantics: temperature colours the cells;
        # dark streamlines show the flow without pretending to be temperature.
        flow_segments, _ = self._streamlines(fluid, "Umag", config)
        fig = plt.figure(figsize=(9.2, 7.3))
        ax = fig.add_subplot(111, projection="3d", computed_zorder=False)
        artist = add_surface(ax, alpha=0.76)
        artist.set_zorder(1)
        lines = Line3DCollection(
            flow_segments, colors="#17114f", linewidths=0.70, alpha=0.88,
            zorder=10,
        )
        ax.add_collection3d(lines)
        self._temperature_colorbar(
            fig, artist, ax=ax, vmin=tmin, vmax=tmax, shrink=0.68, pad=0.08, location="left",
            paper_reference=PAPER_FIGURE_RANGES["figure_09_temperature_degC"],
        )
        ax.set_title("Figure 9 · Streamlines and battery temperature at 1800 s")
        paths[9] = artifact_dir / "figure_09_streamlines_battery_temperature_1800s.png"
        self._save(fig, paths[9])
        paths[10] = streamline_figure("Umag", 10, "Figure 10 · Velocity-coloured streamlines at 1800 s", "Velocity magnitude [m/s]", "jet")

        # Figures 11 and 12: horizontal and vertical velocity sections.
        for figure_id, cut, axes_index, labels, plane_name, paper_max in (
            (11, xy_speed, (0, 1), "XY", "XY centre section", PAPER_FIGURE_RANGES["figure_11_velocity_m_s"][1]),
            (12, zx_speed, (0, 2), "XZ", "XZ centre section", PAPER_FIGURE_RANGES["figure_12_velocity_m_s"][1]),
        ):
            fig, ax = plt.subplots(figsize=(8.5, 6.7))
            actual_max = float(np.percentile(cut[2], 99.5))
            artist = self._plot_cut(ax, cut, axes_index, cmap="jet", vmin=0, vmax=actual_max, label=labels)
            fig.colorbar(artist, ax=ax, label="Velocity magnitude [m/s]")
            ax.set_title(
                f"Figure {figure_id} · {plane_name} velocity at 1800 s\n"
                f"actual scale; paper upper reference {paper_max:.3f} m/s"
            )
            if figure_id == 11:
                from matplotlib.patches import Circle

                for placement in cell_placements(config):
                    ax.add_patch(
                        Circle(
                            (placement.center_x_m * 1000, placement.center_y_m * 1000),
                            config.cells.diameter_m * 500,
                            fill=False, edgecolor="#333333", linewidth=0.35, alpha=0.62,
                        )
                    )
                ax.invert_xaxis()
                ax.invert_yaxis()
            else:
                from matplotlib.patches import Rectangle

                for placement in cell_placements(config):
                    if abs(placement.center_y_m - y_mid) <= config.cells.diameter_m / 2:
                        ax.add_patch(
                            Rectangle(
                                ((placement.center_x_m - config.cells.diameter_m / 2) * 1000, 7.0),
                                config.cells.diameter_m * 1000, config.cells.length_m * 1000,
                                fill=False, edgecolor="#333333", linewidth=0.45, alpha=0.62,
                            )
                        )
            paths[figure_id] = artifact_dir / f"figure_{figure_id:02d}_velocity_{labels.lower()}_1800s.png"
            self._save(fig, paths[figure_id])

        # Figure 13: solid 3D surface plus vertical coupled-temperature section.
        fig = plt.figure(figsize=(13.5, 6.8))
        ax3d = fig.add_subplot(121, projection="3d")
        artist3d = add_surface(ax3d)
        ax3d.set_title("Battery surfaces")
        ax2d = fig.add_subplot(122)
        artist = self._plot_cut(ax2d, zx_fluid_t, (0, 2), cmap="jet", vmin=tmin, vmax=tmax, label="XZ")
        self._plot_cut(ax2d, zx_solid_t, (0, 2), cmap="jet", vmin=tmin, vmax=tmax, label="XZ")
        ax2d.set_title("Coupled vertical temperature section")
        fig.subplots_adjust(wspace=0.34)
        self._temperature_colorbar(
            fig, artist, ax=[ax3d, ax2d], vmin=tmin, vmax=tmax, shrink=0.72, location="left",
            paper_reference=PAPER_FIGURE_RANGES["figure_13a_temperature_degC"],
        )
        fig.suptitle("Figure 13 · 3D battery and vertical temperature at 1800 s")
        paths[13] = artifact_dir / "figure_13_battery_3d_temperature_xz_1800s.png"
        self._save(fig, paths[13])

        # Figure 14: common-scale coolant + solid temperature section with cell IDs.
        fig, ax = plt.subplots(figsize=(8.6, 7.5))
        artist = self._plot_cut(ax, xy_fluid_t, (0, 1), cmap="jet", vmin=tmin, vmax=tmax, label="XY")
        self._plot_cut(ax, xy_solid_t, (0, 1), cmap="jet", vmin=tmin, vmax=tmax, label="XY")
        for placement in cell_placements(config):
            ax.text(
                placement.center_x_m * 1000,
                placement.center_y_m * 1000,
                placement.cell_id,
                ha="center",
                va="center",
                fontsize=6.2,
                color="white",
                path_effects=[],
            )
        self._temperature_colorbar(
            fig, artist, ax=ax, vmin=tmin, vmax=tmax, location="left",
            paper_reference=PAPER_FIGURE_RANGES["figure_14_temperature_degC"],
        )
        ax.invert_xaxis()
        ax.invert_yaxis()
        ax.set_title("Figure 14 · Coupled fluid/solid XY temperature at 1800 s · paper view")
        paths[14] = artifact_dir / "figure_14_coupled_temperature_xy_1800s.png"
        self._save(fig, paths[14])
        return paths

    def generate_animations(
        self,
        config: ResolvedConfig,
        artifact_dir: Path,
        *,
        end_time_s: int = 1800,
        field_interval_s: int = 15,
    ) -> dict[str, Path]:
        from matplotlib.animation import FFMpegWriter, FuncAnimation
        from matplotlib.colors import Normalize
        from matplotlib.tri import Triangulation
        from mpl_toolkits.mplot3d.art3d import Line3DCollection

        plt = self.plt
        np = self.np
        artifact_dir.mkdir(parents=True, exist_ok=True)
        times = list(range(0, end_time_s + 1, field_interval_s))
        z_mid = config.enclosure.height_z_m / 2

        final_fluid, _ = self.regions(float(end_time_s))
        final_fluid_cut = self._triangulated_cut(
            final_fluid, (0, 0, z_mid), (0, 0, 1), "T"
        )
        final_speed_cut = self._triangulated_cut(
            final_fluid, (0, 0, z_mid), (0, 0, 1), "Umag"
        )
        final_values = (
            final_fluid_cut[2] - 273.15 + DISPLAY_TEMPERATURE_OFFSET_C
        )
        tmin = min(
            float(final_values.min()),
            config.boundary.initial_fluid_temperature_k
            - 273.15
            + DISPLAY_TEMPERATURE_OFFSET_C,
        )
        tmax = float(final_values.max())
        speed_max = float(np.percentile(final_speed_cut[2], 99.5))
        fluid_tri = Triangulation(
            final_fluid_cut[0][:, 0] * 1000,
            final_fluid_cut[0][:, 1] * 1000,
            final_fluid_cut[1],
        )
        fig, axes = plt.subplots(
            1, 2, figsize=(13.0, 6.5), constrained_layout=True
        )
        initial_fluid = np.full(final_fluid_cut[0].shape[0], tmin)
        fluid_artist = axes[0].tripcolor(
            fluid_tri,
            initial_fluid,
            shading="gouraud",
            cmap="jet",
            vmin=tmin,
            vmax=tmax,
        )
        axes[0].set_aspect("equal")
        axes[0].set_xlabel("X [mm]")
        axes[0].set_ylabel("Y [mm]")
        axes[0].set_title("Coolant temperature T(x,t)")
        velocity_artist = self._plot_cut(
            axes[1],
            final_speed_cut,
            (0, 1),
            cmap="jet",
            vmin=0,
            vmax=speed_max,
            label="XY",
        )
        axes[1].set_title("Coolant velocity · fixed U(x)")
        fig.colorbar(
            velocity_artist,
            ax=axes[1],
            label="Velocity magnitude [m/s]",
        )
        fig.suptitle("Figure 6 animation · coolant temperature and velocity")
        time_label = axes[0].text(
            0.02, 0.98, "t = 0 s", transform=axes[0].transAxes, va="top", ha="left",
            bbox={"facecolor": "white", "alpha": 0.82, "edgecolor": "none"},
        )
        self._temperature_colorbar(
            fig, fluid_artist, ax=axes[0], vmin=tmin, vmax=tmax,
            paper_reference=PAPER_FIGURE_RANGES["figure_14_temperature_degC"],
        )
        fig.text(
            0.5, 0.01,
            "Actual OpenFOAM T(x,t), 15 s fields · smooth point interpolation · "
            f"display +{DISPLAY_TEMPERATURE_OFFSET_C:.3f} K · U(x) fixed",
            ha="center", fontsize=8, color="#555555",
        )

        def update_thermal(frame: int):
            time_s = float(times[frame])
            fluid, _ = self.regions(time_s)
            fluid_cut = self._triangulated_cut(fluid, (0, 0, z_mid), (0, 0, 1), "T")
            if fluid_cut[1].shape != final_fluid_cut[1].shape:
                raise MdpiCfdError("slice topology changed between stored thermal frames")
            fluid_artist.set_array(
                fluid_cut[2] - 273.15 + DISPLAY_TEMPERATURE_OFFSET_C
            )
            time_label.set_text(f"t = {time_s:.0f} s")
            return fluid_artist, time_label

        thermal_path = artifact_dir / "actual_thermal_xy_0_1800s.mp4"
        animation = FuncAnimation(fig, update_thermal, frames=len(times), interval=80, blit=False)
        animation.save(thermal_path, writer=FFMpegWriter(fps=12, bitrate=2600))
        plt.close(fig)

        # Frozen U is physically stationary. Moving particles are explicitly a
        # streamline visualization and never described as transient velocity.
        fluid, _ = self.regions(float(end_time_s))
        paths = self._streamline_paths(fluid, config)
        segments: list[object] = []
        segment_speed: list[float] = []
        for path_points, path_speed, _, _, _ in paths:
            stride = max(1, len(path_points) // 220)
            line_points = path_points[::stride]
            line_speed = path_speed[::stride]
            for first, second, value in zip(
                line_points,
                line_points[1:],
                0.5 * (line_speed[:-1] + line_speed[1:]),
            ):
                segments.append(np.stack((first, second)))
                segment_speed.append(float(value))
        speed_max = max(float(np.max(path[1])) for path in paths)
        fig = plt.figure(figsize=(10.2, 7.3))
        ax = fig.add_subplot(111, projection="3d")
        norm = Normalize(0.0, speed_max)
        lines = Line3DCollection(
            segments, cmap="jet", norm=norm, linewidths=0.64, alpha=0.42
        )
        lines.set_array(np.asarray(segment_speed))
        ax.add_collection3d(lines)
        far_tail = ax.scatter(
            [], [], [], c=[], cmap="jet", norm=norm, s=10,
            alpha=0.32, depthshade=False,
        )
        near_tail = ax.scatter(
            [], [], [], c=[], cmap="jet", norm=norm, s=19,
            alpha=0.62, depthshade=False,
        )
        particles = ax.scatter(
            [], [], [], c=[], cmap="jet", norm=norm, s=34,
            edgecolors="white", linewidths=0.38, depthshade=False,
        )
        ax.set_xlim(0, config.enclosure.width_x_m * 1000)
        ax.set_ylim(0, config.enclosure.length_y_m * 1000)
        ax.set_zlim(
            -config.ports.external_length_m * 1000,
            (config.enclosure.height_z_m + config.ports.external_length_m) * 1000,
        )
        ax.set_box_aspect((114, 121, 146))
        ax.view_init(elev=27, azim=125)
        ax.set_xlabel("X [mm]")
        ax.set_ylabel("Y [mm]")
        ax.set_zlabel("Z [mm]")
        outline = np.asarray(config.enclosure.outline_polygon_m) * 1000
        closed = np.vstack((outline, outline[0]))
        for z_value in (0.0, config.enclosure.height_z_m * 1000):
            ax.plot(closed[:, 0], closed[:, 1], z_value, color="#555555", linewidth=0.45, alpha=0.52)
        for x_value, y_value in outline:
            ax.plot(
                [x_value, x_value], [y_value, y_value],
                [0, config.enclosure.height_z_m * 1000],
                color="#777777", linewidth=0.3, alpha=0.36,
            )
        ax.set_title("Coolant-flow animation · moving heads follow U(x)")
        phase_label = ax.text2D(
            0.02, 0.97, "fixed U(x) · local flow direction",
            transform=ax.transAxes,
        )
        fig.colorbar(lines, ax=ax, shrink=0.68, pad=0.08, label="Velocity magnitude [m/s]")
        fig.text(
            0.5, 0.01,
            "Long inlet-fed streamlines; bright comet heads show direction in validated fixed U(x), not U(x,t)",
            ha="center", fontsize=8, color="#555555",
        )

        frames = 90
        moving_paths = [path for path in paths if path[4] >= 60.0]
        selection_stride = max(1, math.ceil(len(moving_paths) / 150))
        moving_paths = moving_paths[::selection_stride][:150]

        def sample_path(path, fraction: float):
            path_points, path_speed, _, cumulative_mm, total_mm = path
            target = (fraction % 1.0) * total_mm
            right = min(len(cumulative_mm) - 1, int(np.searchsorted(cumulative_mm, target)))
            if right == 0:
                return path_points[0], float(path_speed[0])
            left = right - 1
            span = float(cumulative_mm[right] - cumulative_mm[left])
            weight = 0.0 if span <= 0 else (target - cumulative_mm[left]) / span
            point = path_points[left] + weight * (path_points[right] - path_points[left])
            value = float(path_speed[left] + weight * (path_speed[right] - path_speed[left]))
            return point, value

        def update_particles(frame: int):
            clouds = []
            cloud_colours = []
            for trail_offset in (-0.040, -0.020, 0.0):
                positions = []
                colours = []
                for index, path in enumerate(moving_paths):
                    fraction = frame / frames + index * 0.61803398875 + trail_offset
                    point, value = sample_path(path, fraction)
                    positions.append(point)
                    colours.append(value)
                clouds.append(np.asarray(positions))
                cloud_colours.append(np.asarray(colours))
            for artist, coordinates, colours in zip(
                (far_tail, near_tail, particles), clouds, cloud_colours
            ):
                artist._offsets3d = (
                    coordinates[:, 0], coordinates[:, 1], coordinates[:, 2]
                )
                artist.set_array(colours)
            phase_label.set_text(
                f"fixed U(x) · local flow direction · phase {frame + 1}/{frames}"
            )
            return far_tail, near_tail, particles, phase_label

        flow_path = artifact_dir / "frozen_flow_particles_fixed_U.mp4"
        animation = FuncAnimation(fig, update_particles, frames=frames, interval=50, blit=False)
        animation.save(flow_path, writer=FFMpegWriter(fps=20, bitrate=2400))
        plt.close(fig)
        return {"thermal_transient": thermal_path, "frozen_flow_particles": flow_path}


def _telemetry_rows(
    cell_data: dict[str, dict[str, dict[float, float]]],
    coolant: dict[str, dict[float, float]],
    energy_rows: list[dict[str, object]],
    velocity_average: float,
) -> list[dict[str, object]]:
    ids = sorted(cell_data)
    times = sorted(set.intersection(*(set(cell_data[cell]["average"]) for cell in ids)))
    energy_times = [float(row["time_s"]) for row in energy_rows]
    energy_values = [float(row["imbalance_fraction"]) for row in energy_rows]

    def interpolated_energy(time_s: float) -> float:
        if time_s <= energy_times[0]:
            return energy_values[0]
        if time_s >= energy_times[-1]:
            return energy_values[-1]
        right = next(index for index, value in enumerate(energy_times) if value >= time_s)
        left = right - 1
        fraction = (time_s - energy_times[left]) / (energy_times[right] - energy_times[left])
        return energy_values[left] + fraction * (energy_values[right] - energy_values[left])

    rows: list[dict[str, object]] = []
    for time_s in times:
        inlet_mass = coolant["inlet_mass"][time_s]
        outlet_mass = coolant["outlet_mass"][time_s]
        through = 0.5 * (abs(inlet_mass) + abs(outlet_mass))
        mass_imbalance = abs(inlet_mass + outlet_mass) / through
        rows.append(
            {
                "time_s": f"{time_s:g}",
                "state": "FROZEN_FLOW_VALIDATED_L0",
                "temperature_status": "ACTUAL_CFD_SURFACE",
                "battery_tmin_K": f"{min(cell_data[cell]['minimum'][time_s] for cell in ids):.12g}",
                "battery_tavg_K": f"{sum(cell_data[cell]['average'][time_s] for cell in ids) / len(ids):.12g}",
                "battery_tmax_K": f"{max(cell_data[cell]['maximum'][time_s] for cell in ids):.12g}",
                "coolant_tin_K": f"{coolant['tin'][time_s]:.12g}",
                "coolant_tout_K": f"{coolant['tout'][time_s]:.12g}",
                "velocity_avg_m_s": f"{velocity_average:.12g}",
                "velocity_max_m_s": f"{coolant['umax'][time_s]:.12g}",
                "pressure_drop_Pa": f"{coolant['pin'][time_s] - coolant['pout'][time_s]:.12g}",
                "mass_imbalance_fraction": f"{mass_imbalance:.12g}",
                "energy_imbalance_fraction": f"{interpolated_energy(time_s):.12g}",
            }
        )
    return rows


def postprocess_l0_results(config: ResolvedConfig, case_dir: str | Path) -> dict[str, object]:
    case = Path(case_dir).resolve()
    level_dir = case / "production/L0"
    reports = _load_required_reports(level_dir)
    frozen_run = level_dir / "runs/frozen_flow"
    foam_file = frozen_run / "frozen_flow.foam"
    if not foam_file.is_file():
        raise MdpiCfdError(f"OpenFOAM reader marker is missing: {foam_file}")
    result_dir = case / "results"
    figure_dir = result_dir / "figures"
    if not (result_dir / "result_manifest.json").is_file():
        raise MdpiCfdError(f"result contract is missing: {result_dir}")

    per_cell_rows, cell_data = _collect_cell_rows(frozen_run)
    coolant = _collect_coolant_series(frozen_run)
    residual_rows = _collect_residual_rows(level_dir)
    mesh_report_path = result_dir / "mesh_independence.json"
    mesh_report = (
        json.loads(mesh_report_path.read_text(encoding="utf-8"))
        if mesh_report_path.is_file()
        else {}
    )
    mesh_independence_passed = mesh_report.get("status") == "MESH_INDEPENDENCE_PASSED"
    backend = _ScientificBackend(
        foam_file,
        subtitle=(
            "Validated L0 transient CFD · actual solver fields · L0–L3 mesh independence passed"
            if mesh_independence_passed
            else None
        ),
    )
    mesh_integrals = json.loads(
        (level_dir / "reports/production_mesh/mesh_integrals_pre_split.json").read_text(
            encoding="utf-8"
        )
    )["actual"]
    energy_rows, velocity_average = backend.energy_rows(
        config,
        coolant,
        authoritative_fluid_volume_m3=float(
            mesh_integrals["zone_volumes_m3"]["coolant"]
        ),
        authoritative_solid_volume_m3=float(
            mesh_integrals["battery_total_volume_m3"]
        ),
    )
    telemetry_rows = _telemetry_rows(cell_data, coolant, energy_rows, velocity_average)

    _write_csv(
        result_dir / "per_cell_time_series.csv",
        (
            "time_s",
            "cell_id",
            "surface_tmin_K",
            "surface_tavg_K",
            "surface_tmax_K",
            "heat_flux_avg_W_m2",
            "value_status",
        ),
        per_cell_rows,
    )
    _write_csv(
        result_dir / "telemetry.csv",
        (
            "time_s",
            "state",
            "temperature_status",
            "battery_tmin_K",
            "battery_tavg_K",
            "battery_tmax_K",
            "coolant_tin_K",
            "coolant_tout_K",
            "velocity_avg_m_s",
            "velocity_max_m_s",
            "pressure_drop_Pa",
            "mass_imbalance_fraction",
            "energy_imbalance_fraction",
        ),
        telemetry_rows,
    )
    _write_csv(
        result_dir / "residuals.csv",
        ("iteration", "simulation_time_s", "region", "field", "initial_residual", "final_residual"),
        residual_rows,
    )
    _write_csv(
        result_dir / "energy_balance.csv",
        ("time_s", "generated_W", "removed_W", "stored_W", "imbalance_fraction"),
        energy_rows,
    )

    artifacts = backend.generate_figures(config, cell_data, result_dir / "artifacts")
    common_metadata = {
        "source": "validated OpenFOAM v2512 L0 fields",
        "run": "production/L0/runs/frozen_flow",
        "snapshot_time_s": 1800.0,
        "temperature_plot_unit": "degC",
        "temperature_storage_unit": "K",
        "mesh_independence_status": (
            "MESH_INDEPENDENCE_PASSED"
            if mesh_independence_passed
            else "PENDING_L1_L3"
        ),
        "publication_results_authorized": False,
        "render_style": "paper_like_jet_with_actual_result_scale",
        "paper_reference_ranges": PAPER_FIGURE_RANGES,
        "paper_range_policy": "reference_only; never force CFD values or hide clipping",
    }
    for figure_id, path in artifacts.items():
        update_figure_status(
            result_dir,
            figure_id,
            "L0_CFD_READY",
            artifacts=[str(path.relative_to(case))],
            metadata={**common_metadata, "actual_solver_data": True},
        )
    if not mesh_independence_passed:
        update_figure_status(
            result_dir,
            3,
            "AWAITING_MESH_INDEPENDENCE",
            metadata={
                "required_levels": ["L0", "L1", "L2", "L3"],
                "publication_results_authorized": False,
            },
        )
    manifest = json.loads((result_dir / "result_manifest.json").read_text(encoding="utf-8"))
    frozen_summary = reports["frozen_flow"]["final_summary"]
    manifest.update(
        {
            "state": "Completed",
            "data_status": "L0_CFD_RESULTS_READY",
            "generated_figure_ids": sorted(
                set(artifacts) | ({3} if mesh_independence_passed else set())
            ),
            "publication_results_authorized": False,
            "publication_blocker": (
                "Geometry/input assumptions A-04 remain; transient velocity was not solved"
                if mesh_independence_passed
                else "L1-L3 mesh independence is not complete"
            ),
            "mesh_independence_status": mesh_report.get(
                "status", "MESH_INDEPENDENCE_PENDING"
            ),
            "actual_solver_data": True,
            "simulation_level": "L0",
            "solver": "OpenFOAM v2512 chtMultiRegionFoam frozen-flow transient",
            "final_summary": frozen_summary,
            "postprocessing": {
                "telemetry_rows": len(telemetry_rows),
                "per_cell_rows": len(per_cell_rows),
                "residual_rows": len(residual_rows),
                "energy_balance_rows": len(energy_rows),
                "velocity_average_m_s": velocity_average,
                "energy_balance_method": "polyMesh-gate-volume-normalised sensible-energy derivative over 15 s fields",
                "heat_flux_status": "NOT_COMPUTED",
            },
            "validation_reports": {
                name: {"status": report["status"]} for name, report in reports.items()
            },
            "figure_style": {
                "palette": "jet",
                "scale": "actual L0 field range",
                "paper_reference_ranges": PAPER_FIGURE_RANGES,
                "camera": "paper-oriented preset",
            },
        }
    )
    _atomic_json(result_dir / "result_manifest.json", manifest)
    _atomic_json(
        result_dir / "figure_style_manifest.json",
        {
            "schema_version": 1,
            "palette": "jet",
            "default_scale": "actual_result_range",
            "paper_reference_ranges": PAPER_FIGURE_RANGES,
            "policy": (
                "Match palette, camera and composition to the paper while retaining "
                "the actual CFD numerical range on every colorbar."
            ),
        },
    )
    return manifest


def generate_l0_animations(config: ResolvedConfig, case_dir: str | Path) -> dict[str, object]:
    case = Path(case_dir).resolve()
    level_dir = case / "production/L0"
    _load_required_reports(level_dir)
    result_dir = case / "results"
    manifest_path = result_dir / "result_manifest.json"
    if not manifest_path.is_file():
        raise MdpiCfdError(f"result manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("data_status") != "L0_CFD_RESULTS_READY":
        raise MdpiCfdError("run postprocess-l0 before generating animations")
    foam_file = level_dir / "runs/frozen_flow/frozen_flow.foam"
    backend = _ScientificBackend(foam_file)
    animations = backend.generate_animations(config, result_dir / "artifacts")
    artifact_paths = {
        name: str(path.relative_to(case)) for name, path in animations.items()
    }
    for figure_id, animation_name in ((6, "thermal_transient"), (9, "frozen_flow_particles")):
        figure_path = result_dir / "figures" / f"figure_{figure_id:02d}.json"
        figure = json.loads(figure_path.read_text(encoding="utf-8"))
        existing = list(figure.get("artifacts", []))
        animation_path = artifact_paths[animation_name]
        if animation_path not in existing:
            existing.append(animation_path)
        figure["artifacts"] = existing
        metadata = dict(figure.get("metadata", {}))
        metadata["animation_semantics"] = (
            "figure_06_actual_coolant_T_x_t_plus_fixed_U_x_15s_fields"
            if animation_name == "thermal_transient"
            else "particles_in_fixed_frozen_U_not_transient_velocity"
        )
        figure["metadata"] = metadata
        _atomic_json(figure_path, figure)
    manifest["animations"] = artifact_paths
    manifest["animation_semantics"] = {
        "thermal_transient": (
            "Figure 6 actual coolant T(x,t) every 15 s with fixed U(x); "
            "smooth point interpolation"
        ),
        "frozen_flow_particles": (
            "moving comet heads along inlet-fed streamlines in fixed U(x); "
            "not transient velocity"
        ),
    }
    _atomic_json(manifest_path, manifest)
    return {
        "status": "L0_CFD_ANIMATIONS_READY",
        "animations": artifact_paths,
        "publication_results_authorized": False,
    }
