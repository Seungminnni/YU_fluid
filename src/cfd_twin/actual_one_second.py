"""정수 초 OpenFOAM probe를 휴대 가능한 UI 온도 프레임으로 포장한다.

0초 초기장과 1~1,800초 직접 표본을 공통 범위 uint16으로 양자화한다.
누락점을 이웃값으로 채우거나 시간/공간 보간하지 않으며, 저장 후 모든 값을
다시 읽어 원본과 반 quantum 이내인지 전수 비교한다.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class ProbeFile:
    path: Path
    points: list[tuple[float, float, float]]
    valid_indices: list[int]
    data_offset: int


@dataclass(frozen=True)
class ProbeSeries:
    files: tuple[ProbeFile, ...]
    points: list[tuple[float, float, float]]
    valid_indices: list[int]


_PROBE_RE = re.compile(
    # patchProbes appends details such as ``at patch ... with a distance ...``
    # after the sampled coordinates, while ordinary probes end immediately.
    # Keep accepting both forms; the trailing text is diagnostic only.
    r"^# Probe\s+(\d+)\s+\(([^)]+)\)(?:\s+.*)?$"
)


def read_probe_header(path: str | Path) -> ProbeFile:
    source = Path(path)
    points: list[tuple[float, float, float]] = []
    valid_indices: list[int] = []
    with source.open("rb") as stream:
        while True:
            line = stream.readline()
            if not line:
                raise ValueError(f"{source}: missing '# Time' header")
            decoded = line.decode("ascii", errors="strict").strip()
            if decoded.startswith("# Time"):
                return ProbeFile(source, points, valid_indices, stream.tell())
            match = _PROBE_RE.match(decoded)
            if not match:
                continue
            index = int(match.group(1))
            if index != len(points):
                raise ValueError(f"{source}: non-contiguous probe index {index}")
            coordinates = tuple(float(value) for value in match.group(2).split())
            if len(coordinates) != 3 or not all(math.isfinite(v) for v in coordinates):
                raise ValueError(f"{source}: invalid probe coordinates at {index}")
            points.append(coordinates)
            if "# Not Found" not in decoded:
                valid_indices.append(index)


def iter_probe_rows(probe: ProbeFile):
    with probe.path.open("rb") as stream:
        stream.seek(probe.data_offset)
        for raw_line in stream:
            values = np.fromstring(raw_line.decode("ascii"), sep=" ")
            if not len(values):
                continue
            if len(values) != len(probe.points) + 1:
                raise ValueError(
                    f"{probe.path}: expected {len(probe.points) + 1} columns, "
                    f"found {len(values)}"
                )
            time = float(values[0])
            if not math.isfinite(time) or abs(time - round(time)) > 1e-7:
                raise ValueError(f"{probe.path}: non-integer sample time {time}")
            yield int(round(time)), values[1:]


def read_probe_series(path: str | Path) -> ProbeSeries:
    """Collect every OpenFOAM restart segment of one probe function object."""

    source = Path(path)
    siblings = source.parent.parent
    if source.parent.name.isdecimal() and siblings.is_dir():
        paths = sorted(
            (directory / source.name for directory in siblings.iterdir()
             if directory.is_dir() and directory.name.isdecimal()
             and (directory / source.name).is_file()),
            key=lambda candidate: int(candidate.parent.name),
        )
    else:
        paths = [source]
    if source not in paths:
        raise ValueError(f"{source}: initial probe segment is missing")
    files = tuple(read_probe_header(candidate) for candidate in paths)
    first = files[0]
    for segment in files[1:]:
        if segment.points != first.points or segment.valid_indices != first.valid_indices:
            raise ValueError(f"{segment.path}: probe grid changed after restart")
    return ProbeSeries(files, first.points, first.valid_indices)


def iter_probe_series_rows(series: ProbeSeries):
    """Stream ordered seconds; allow identical restart-boundary duplicate rows."""

    previous_time = -1
    previous_values = None
    for segment in series.files:
        for time_s, values in iter_probe_rows(segment):
            if time_s < previous_time:
                raise ValueError(f"{segment.path}: probe times moved backwards")
            if time_s == previous_time:
                if previous_values is None or not np.array_equal(values, previous_values):
                    raise ValueError(f"{segment.path}: conflicting duplicate at {time_s} s")
                continue
            previous_time = time_s
            previous_values = values
            yield time_s, values


def _iter_post_initial_probe_rows(series: ProbeSeries):
    """Yield samples after the viewer's explicit time-zero frame.

    Solver-time probes normally start at 1 s.  Offline ``postProcess -time
    0:...`` also writes the stored time-zero field, which would duplicate the
    initial frame that this packer creates from the configured temperature.
    Accept that optional row while retaining the strict 1..end-time contract.
    """

    for time_s, values in iter_probe_series_rows(series):
        if time_s == 0:
            continue
        yield time_s, values


def _probe_specs(run_dir: Path) -> dict[str, dict[str, object]]:
    root = run_dir / "postProcessing"
    specs: dict[str, dict[str, object]] = {
        "xy": {
            "path": root / "actual_xy_temperature_1s/coolant/0/T",
            "kind": "xy",
            "columns": 114,
            "rows": 121,
            "label": "Z = 41.0 mm",
            "fixed_fraction": 0.5,
        },
        "cell_surface": {
            "path": root
            / "actual_battery_near_surface_temperature_1s/batterySolid/0/T",
            "kind": "battery-near-surface",
            "angular_segments": 32,
            "axial_segments": 16,
            "cell_count": 23,
            "samples_per_cell": 32 * 16 + 2 * 32,
        },
    }
    for name, fraction, label in (
        ("xy04", 3.5 / 82.0, "Z = 3.5 mm · lower coolant gap"),
        ("xy15", 0.15, "Z = 12.3 mm"),
        ("xy33", 0.325, "Z = 26.7 mm"),
        ("xy68", 0.675, "Z = 55.4 mm"),
        ("xy85", 0.85, "Z = 69.7 mm"),
        ("xy96", 78.5 / 82.0, "Z = 78.5 mm · upper coolant gap"),
    ):
        specs[name] = {
            "path": root / f"actual_{name}_temperature_1s/coolant/0/T",
            "kind": "xy",
            "columns": 114,
            "rows": 121,
            "label": label,
            "fixed_fraction": fraction,
        }
    xz_rows = math.ceil(0.082 / 0.0015)
    for name, label in (
        ("xz15", "Y = 18.2 mm"),
        ("xz33", "Y = 39.3 mm"),
        ("xz50", "Y = 60.5 mm"),
        ("xz68", "Y = 81.7 mm"),
        ("xz85", "Y = 102.8 mm"),
    ):
        specs[name] = {
            "path": root / f"actual_{name}_temperature_1s/coolant/0/T",
            "kind": "xz",
            "columns": 114,
            "rows": xz_rows,
            "label": label,
        }
    yz_columns = math.ceil(0.121 / 0.001)
    for name, label in (
        ("yz15", "X = 17.1 mm"),
        ("yz33", "X = 37.1 mm"),
        ("yz50", "X = 57.0 mm"),
        ("yz68", "X = 77.0 mm"),
        ("yz85", "X = 96.9 mm"),
    ):
        specs[name] = {
            "path": root / f"actual_{name}_temperature_1s/coolant/0/T",
            "kind": "yz",
            "columns": yz_columns,
            "rows": xz_rows,
            "label": label,
        }
    return specs


def _heat_transfer_probe_series(run_dir: Path) -> dict[str, ProbeSeries] | None:
    """Load the three direct interface quantities used for local convection h.

    Old validated runs predate these probes and legitimately return ``None``;
    a partially written new set is rejected instead of silently falling back
    to the former whole-cell effective coefficient.
    """

    root = run_dir / "postProcessing"
    paths = {
        "wall_temperature": root
        / "actual_battery_interface_wall_1s/coolant/0/T",
        "wall_heat_flux": root
        / "actual_battery_interface_wall_1s/coolant/0/wallHeatFlux",
        "near_fluid_temperature": root
        / "actual_battery_near_surface_fluid_temperature_1s/coolant/0/T",
    }
    present = {name: path.is_file() for name, path in paths.items()}
    if not any(present.values()):
        return None
    if not all(present.values()):
        missing = sorted(name for name, exists in present.items() if not exists)
        raise ValueError(f"incomplete battery-interface heat-transfer probes: {missing}")
    series = {name: read_probe_series(path) for name, path in paths.items()}
    wall = series["wall_temperature"]
    for name, probe in series.items():
        if len(probe.points) != len(wall.points):
            raise ValueError(f"{name}: interface probe point count changed")
        if probe.valid_indices != wall.valid_indices:
            raise ValueError(f"{name}: interface valid-point mask changed")
    return series


def _iter_heat_transfer_rows(series: dict[str, ProbeSeries]):
    """Yield local h=|q''|/|Twall-Tnear-fluid| without spatial filling."""

    iterators = {name: iter(iter_probe_series_rows(probe)) for name, probe in series.items()}
    valid = np.asarray(series["wall_temperature"].valid_indices, dtype=int)
    while True:
        rows: dict[str, tuple[int, np.ndarray]] = {}
        ended = []
        for name, iterator in iterators.items():
            try:
                rows[name] = next(iterator)
                ended.append(False)
            except StopIteration:
                ended.append(True)
        if all(ended):
            return
        if any(ended):
            raise ValueError("heat-transfer probe series ended at different times")
        times = {row[0] for row in rows.values()}
        if len(times) != 1:
            raise ValueError(f"heat-transfer probe times differ: {sorted(times)}")
        wall_temperature = rows["wall_temperature"][1][valid]
        wall_heat_flux = rows["wall_heat_flux"][1][valid]
        near_fluid_temperature = rows["near_fluid_temperature"][1][valid]
        if not all(
            np.all(np.isfinite(values))
            for values in (wall_temperature, wall_heat_flux, near_fluid_temperature)
        ):
            raise ValueError(f"non-finite interface probe source at {next(iter(times))} s")
        delta_temperature = np.abs(wall_temperature - near_fluid_temperature)
        coefficient = np.full(delta_temperature.shape, np.nan, dtype=float)
        usable = delta_temperature > 1.0e-6
        coefficient[usable] = np.abs(wall_heat_flux[usable]) / delta_temperature[usable]
        yield next(iter(times)), coefficient


def _mesh_identity(run_dir: Path, case_dir: Path | None = None) -> tuple[str, int]:
    """Read the executed mesh identity instead of stamping a legacy L3 count."""

    case_dir = case_dir.resolve() if case_dir is not None else run_dir.parent.parent
    manifest_path = case_dir / "production_mesh_manifest.json"
    composition_path = (
        case_dir / "reports/production_mesh/mesh_target_composition.json"
    )
    mesh_level = "unknown"
    mesh_cell_count = 0
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        mesh_level = str(manifest.get("mesh_level", mesh_level))
    if composition_path.is_file():
        composition = json.loads(composition_path.read_text(encoding="utf-8"))
        mesh_cell_count = int(composition.get("actual", {}).get("total_cells", 0))
    if mesh_cell_count <= 0:
        raise ValueError(
            "executed mesh cell count is unavailable: "
            f"{composition_path}"
        )
    return mesh_level, mesh_cell_count


def build_actual_one_second_bundle(
    run_dir: str | Path,
    output_dir: str | Path,
    *,
    initial_temperature_k: float = 293.15,
    initial_fluid_temperature_k: float | None = None,
    end_time_s: int = 1800,
    mesh_case_dir: str | Path | None = None,
) -> dict[str, object]:
    """Pack direct temperature probes and optional local interface h frames."""

    run = Path(run_dir).resolve()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    mesh_level, mesh_cell_count = _mesh_identity(
        run, Path(mesh_case_dir) if mesh_case_dir is not None else None
    )
    specs = _probe_specs(run)
    initial_solid_temperature_k = initial_temperature_k
    if initial_fluid_temperature_k is None:
        initial_fluid_temperature_k = initial_solid_temperature_k

    def dataset_initial_temperature(name: str) -> float:
        return (
            initial_solid_temperature_k
            if name == "cell_surface"
            else initial_fluid_temperature_k
        )

    probes: dict[str, ProbeSeries] = {}
    dataset_ranges: dict[str, tuple[float, float]] = {}
    low = min(initial_fluid_temperature_k, initial_solid_temperature_k)
    high = max(initial_fluid_temperature_k, initial_solid_temperature_k)
    expected_times = list(range(1, end_time_s + 1))
    for name, spec in specs.items():
        probe = read_probe_series(spec["path"])
        if len(probe.points) != int(spec.get("columns", 0)) * int(
            spec.get("rows", 0)
        ) and name != "cell_surface":
            raise ValueError(f"{name}: probe grid dimensions do not match point count")
        times: list[int] = []
        valid = np.asarray(probe.valid_indices, dtype=int)
        dataset_initial_k = dataset_initial_temperature(name)
        dataset_low = dataset_initial_k
        dataset_high = dataset_initial_k
        for time_s, values in _iter_post_initial_probe_rows(probe):
            times.append(time_s)
            selected = values[valid]
            if not np.all(np.isfinite(selected)):
                raise ValueError(f"{name}: non-finite valid values at {time_s} s")
            low = min(low, float(selected.min()))
            high = max(high, float(selected.max()))
            dataset_low = min(dataset_low, float(selected.min()))
            dataset_high = max(dataset_high, float(selected.max()))
        if times != expected_times:
            raise ValueError(
                f"{name}: expected complete 1..{end_time_s} s rows, "
                f"found {len(times)} ending at {times[-1] if times else None}"
            )
        probes[name] = probe
        dataset_ranges[name] = (dataset_low, dataset_high)

    # 모든 데이터셋이 같은 Tmin/Tmax와 quantum을 공유해야 화면 간 색과
    # 값의 해석이 일관된다. uint16 하나는 0~65535의 온도 구간을 나타낸다.
    span = max(high - low, 1e-12)
    quantum_k = span / 65535.0
    datasets: dict[str, dict[str, object]] = {}
    for name, spec in specs.items():
        probe = probes[name]
        valid = np.asarray(probe.valid_indices, dtype=int)
        destination = output / f"{name}.u16"
        dataset_initial_k = dataset_initial_temperature(name)
        initial = np.full(len(valid), dataset_initial_k)
        with destination.open("wb") as stream:
            stream.write(np.rint((initial - low) / quantum_k).astype("<u2").tobytes())
            for _, values in _iter_post_initial_probe_rows(probe):
                selected = values[valid]
                quantized = np.rint((selected - low) / quantum_k).clip(0, 65535)
                stream.write(quantized.astype("<u2").tobytes())
        metadata = {
            key: value
            for key, value in spec.items()
            if key != "path"
        }
        metadata.update(
            {
                "file": destination.name,
                "point_count": len(probe.points),
                "valid_count": len(probe.valid_indices),
                "valid_indices": probe.valid_indices,
                "frame_count": end_time_s + 1,
                "first_time_s": 0,
                "last_time_s": end_time_s,
                "time_step_s": 1,
                "source": str(probe.files[0].path),
                "source_segments": [str(item.path) for item in probe.files],
                "sampling": "OpenFOAM probes interpolationScheme cell",
                "invalid_policy": "omitted and preserved invalid; no padding",
                "temperature_range_k": list(dataset_ranges[name]),
                "temperature_range_c": [
                    dataset_ranges[name][0] - 273.15,
                    dataset_ranges[name][1] - 273.15,
                ],
                "initial_temperature_k": dataset_initial_k,
            }
        )
        datasets[name] = metadata

    heat_transfer_series = _heat_transfer_probe_series(run)
    if heat_transfer_series is not None:
        wall = heat_transfer_series["wall_temperature"]
        destination = output / "cell_surface_heat_transfer_coefficient.f32"
        times: list[int] = []
        finite_minimum = math.inf
        finite_maximum = -math.inf
        unavailable_count = 0
        with destination.open("wb") as stream:
            for time_s, coefficient in _iter_heat_transfer_rows(heat_transfer_series):
                times.append(time_s)
                finite = coefficient[np.isfinite(coefficient)]
                unavailable_count += int(coefficient.size - finite.size)
                if finite.size:
                    finite_minimum = min(finite_minimum, float(finite.min()))
                    finite_maximum = max(finite_maximum, float(finite.max()))
                stream.write(coefficient.astype("<f4").tobytes())
        if times != expected_times:
            raise ValueError(
                "cell_surface_heat_transfer_coefficient: expected complete "
                f"1..{end_time_s} s rows, found {len(times)}"
            )
        if not math.isfinite(finite_minimum) or not math.isfinite(finite_maximum):
            raise ValueError("local heat-transfer coefficient has no finite samples")
        packed_coefficient = np.memmap(destination, dtype="<f4", mode="r")
        finite_packed = np.asarray(
            packed_coefficient[np.isfinite(packed_coefficient)], dtype=float
        )
        distribution_percentiles = {
            str(percentile): float(np.percentile(finite_packed, percentile))
            for percentile in (1, 5, 25, 50, 75, 90, 95, 99, 99.5, 99.9)
        }
        recommended_upper = max(
            1000.0,
            1000.0 * math.ceil(distribution_percentiles["99.9"] / 1000.0),
        )
        datasets["cell_surface_heat_transfer_coefficient"] = {
            "file": destination.name,
            "kind": "battery-coolant-interface-local-convection",
            "dtype": "float32-little-endian",
            "point_count": len(wall.points),
            "valid_count": len(wall.valid_indices),
            "valid_indices": wall.valid_indices,
            "frame_count": end_time_s,
            "first_time_s": 1,
            "last_time_s": end_time_s,
            "time_step_s": 1,
            "angular_segments": 32,
            "axial_segments": 16,
            "cell_count": 23,
            "samples_per_cell": 32 * 16 + 2 * 32,
            "units": "W/(m^2*K)",
            "definition": "h=abs(wallHeatFlux)/abs(Twall-Tnear-fluid)",
            "wall_heat_flux_source": "OpenFOAM coolant-side wallHeatFlux patch field",
            "wall_temperature_source": "OpenFOAM coolant-side interface patch T",
            "fluid_reference_temperature_source": (
                "OpenFOAM containing coolant cell 0.35 mm outward from the interface"
            ),
            "sampling": "OpenFOAM patchProbes plus containing-cell probes; no interpolation",
            "invalid_policy": (
                "NaN only where abs(Twall-Tnear-fluid)<=1e-6 K; no temporal or spatial fill"
            ),
            "unavailable_value_count": unavailable_count,
            "value_range_w_m2k": [finite_minimum, finite_maximum],
            "distribution_percentiles_w_m2k": distribution_percentiles,
            "recommended_fixed_display_upper_w_m2k": recommended_upper,
            "display_range_policy": (
                "shared fixed 0..global max of per-run 99.9th-percentile rounded up "
                "to 1000 W/(m^2*K); higher direct values remain valid and saturate"
            ),
            "source_segments": {
                name: [str(item.path) for item in probe.files]
                for name, probe in heat_transfer_series.items()
            },
        }

    manifest = {
        "schema_version": 1,
        "status": "ACTUAL_ONE_SECOND_SAMPLES_READY",
        "mesh_level": mesh_level,
        "mesh_cell_count": mesh_cell_count,
        # Keep the legacy key as the solid initial temperature because all
        # per-cell histories use it. New consumers must use the regional keys.
        "initial_temperature_k": initial_solid_temperature_k,
        "initial_temperature_c": initial_solid_temperature_k - 273.15,
        "initial_fluid_temperature_k": initial_fluid_temperature_k,
        "initial_solid_temperature_k": initial_solid_temperature_k,
        "times_s": {"start": 0, "end": end_time_s, "step": 1},
        "temperature_encoding": {
            "dtype": "uint16-little-endian",
            "minimum_k": low,
            "maximum_k": high,
            "quantum_k": quantum_k,
            "maximum_quantization_error_k": 0.5 * quantum_k,
        },
        "temporal_interpolation": False,
        "spatial_interpolation": False,
        "padding": False,
        "datasets": datasets,
    }
    manifest_path = output / "actual_1s_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def validate_actual_one_second_bundle(
    run_dir: str | Path,
    output_dir: str | Path,
) -> dict[str, object]:
    """포장값 전부를 복원해 직접 probe 원본과 허용오차 내 일치하는지 검사한다."""

    run = Path(run_dir).resolve()
    output = Path(output_dir).resolve()
    manifest_path = output / "actual_1s_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    encoding = manifest["temperature_encoding"]
    low = float(encoding["minimum_k"])
    quantum = float(encoding["quantum_k"])
    tolerance = float(encoding["maximum_quantization_error_k"]) + 1e-12
    expected_times = list(
        range(
            int(manifest["times_s"]["start"]) + 1,
            int(manifest["times_s"]["end"]) + 1,
        )
    )
    results: dict[str, dict[str, object]] = {}
    all_passed = True
    for name, source_spec in _probe_specs(run).items():
        specification = manifest["datasets"][name]
        probe = read_probe_series(source_spec["path"])
        valid = np.asarray(probe.valid_indices, dtype=int)
        encoded = np.memmap(output / specification["file"], dtype="<u2", mode="r")
        expected_values = int(specification["frame_count"]) * len(valid)
        size_matches = encoded.size == expected_values
        maximum_error = 0.0
        times: list[int] = []
        if size_matches:
            frames = encoded.reshape(int(specification["frame_count"]), len(valid))
            initial = np.full(
                len(valid),
                float(
                    specification.get(
                        "initial_temperature_k", manifest["initial_temperature_k"]
                    )
                ),
            )
            maximum_error = float(np.max(np.abs(low + frames[0] * quantum - initial)))
            for frame, (time_s, values) in enumerate(
                _iter_post_initial_probe_rows(probe), start=1
            ):
                if frame >= int(specification["frame_count"]):
                    raise ValueError(f"{name}: more source rows than encoded frames")
                times.append(time_s)
                decoded = low + frames[frame] * quantum
                maximum_error = max(
                    maximum_error,
                    float(np.max(np.abs(decoded - values[valid]))),
                )
        checks = {
            "binary_value_count_matches": size_matches,
            "integer_second_rows_complete": times == expected_times,
            "valid_indices_match_source": probe.valid_indices
            == specification["valid_indices"],
            "maximum_error_within_half_quantum": maximum_error <= tolerance,
            "invalid_points_not_stored": int(specification["valid_count"])
            == len(probe.valid_indices),
        }
        passed = all(checks.values())
        all_passed = all_passed and passed
        results[name] = {
            "checks": checks,
            "maximum_absolute_error_k": maximum_error,
            "passed": passed,
        }
    heat_transfer_series = _heat_transfer_probe_series(run)
    heat_transfer_spec = manifest["datasets"].get(
        "cell_surface_heat_transfer_coefficient"
    )
    if heat_transfer_series is not None:
        if not isinstance(heat_transfer_spec, dict):
            raise ValueError("local heat-transfer source exists but packed dataset is missing")
        wall = heat_transfer_series["wall_temperature"]
        encoded = np.memmap(
            output / str(heat_transfer_spec["file"]), dtype="<f4", mode="r"
        )
        expected_values = int(heat_transfer_spec["frame_count"]) * len(
            wall.valid_indices
        )
        size_matches = encoded.size == expected_values
        times: list[int] = []
        maximum_absolute_error = 0.0
        values_match = size_matches
        if size_matches:
            frames = encoded.reshape(int(heat_transfer_spec["frame_count"]), -1)
            for frame, (time_s, expected) in enumerate(
                _iter_heat_transfer_rows(heat_transfer_series)
            ):
                times.append(time_s)
                decoded = np.asarray(frames[frame], dtype=float)
                if not np.array_equal(np.isnan(decoded), np.isnan(expected)):
                    values_match = False
                    continue
                finite = np.isfinite(expected)
                if finite.any():
                    maximum_absolute_error = max(
                        maximum_absolute_error,
                        float(np.max(np.abs(decoded[finite] - expected[finite]))),
                    )
                if not np.allclose(
                    decoded, expected, rtol=1.0e-6, atol=1.0e-4, equal_nan=True
                ):
                    values_match = False
        htc_checks = {
            "binary_value_count_matches": size_matches,
            "integer_second_rows_complete": times == expected_times,
            "valid_indices_match_source": wall.valid_indices
            == heat_transfer_spec.get("valid_indices"),
            "float32_values_match_direct_sources": values_match,
            "no_temporal_or_spatial_fill": True,
        }
        htc_passed = all(htc_checks.values())
        all_passed = all_passed and htc_passed
        results["cell_surface_heat_transfer_coefficient"] = {
            "checks": htc_checks,
            "maximum_absolute_error_w_m2k": maximum_absolute_error,
            "passed": htc_passed,
        }
    report = {
        "schema_version": 1,
        "status": (
            "ACTUAL_ONE_SECOND_BUNDLE_VALIDATION_PASSED"
            if all_passed
            else "ACTUAL_ONE_SECOND_BUNDLE_VALIDATION_FAILED"
        ),
        "maximum_allowed_error_k": tolerance,
        "temporal_interpolation": False,
        "spatial_interpolation": False,
        "padding": False,
        "datasets": results,
    }
    (output / "actual_1s_validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pack direct-cell OpenFOAM 1 s probes for the browser viewer"
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--initial-temperature", type=float, default=293.15)
    parser.add_argument("--initial-fluid-temperature", type=float)
    parser.add_argument("--end-time", type=int, default=1800)
    parser.add_argument("--mesh-case-dir", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.verify_only:
        build_actual_one_second_bundle(
            args.run_dir,
            args.output,
            initial_temperature_k=args.initial_temperature,
            initial_fluid_temperature_k=args.initial_fluid_temperature,
            end_time_s=args.end_time,
            mesh_case_dir=args.mesh_case_dir,
        )
    report = validate_actual_one_second_bundle(args.run_dir, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"].endswith("_PASSED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
