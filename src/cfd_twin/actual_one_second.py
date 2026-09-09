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


_PROBE_RE = re.compile(
    r"^# Probe\s+(\d+)\s+\(([^)]+)\)(?:\s+# Not Found)?\s*$"
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
            yield int(round(float(values[0]))), values[1:]


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
        ("xy15", 0.15, "Z = 12.3 mm"),
        ("xy33", 0.325, "Z = 26.7 mm"),
        ("xy68", 0.675, "Z = 55.4 mm"),
        ("xy85", 0.85, "Z = 69.7 mm"),
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
    return specs


def _mesh_identity(run_dir: Path) -> tuple[str, int]:
    """Read the executed mesh identity instead of stamping a legacy L3 count."""

    case_dir = run_dir.parent.parent
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
    end_time_s: int = 1800,
) -> dict[str, object]:
    run = Path(run_dir).resolve()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    mesh_level, mesh_cell_count = _mesh_identity(run)
    specs = _probe_specs(run)
    probes: dict[str, ProbeFile] = {}
    dataset_ranges: dict[str, tuple[float, float]] = {}
    low = initial_temperature_k
    high = initial_temperature_k
    expected_times = list(range(1, end_time_s + 1))
    for name, spec in specs.items():
        probe = read_probe_header(spec["path"])
        if len(probe.points) != int(spec.get("columns", 0)) * int(
            spec.get("rows", 0)
        ) and name != "cell_surface":
            raise ValueError(f"{name}: probe grid dimensions do not match point count")
        times: list[int] = []
        valid = np.asarray(probe.valid_indices, dtype=int)
        dataset_low = initial_temperature_k
        dataset_high = initial_temperature_k
        for time_s, values in iter_probe_rows(probe):
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

    span = max(high - low, 1e-12)
    quantum_k = span / 65535.0
    datasets: dict[str, dict[str, object]] = {}
    for name, spec in specs.items():
        probe = probes[name]
        valid = np.asarray(probe.valid_indices, dtype=int)
        destination = output / f"{name}.u16"
        initial = np.full(len(valid), initial_temperature_k)
        with destination.open("wb") as stream:
            stream.write(np.rint((initial - low) / quantum_k).astype("<u2").tobytes())
            for _, values in iter_probe_rows(probe):
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
                "source": str(probe.path),
                "sampling": "OpenFOAM probes interpolationScheme cell",
                "invalid_policy": "omitted and preserved invalid; no padding",
                "temperature_range_k": list(dataset_ranges[name]),
                "temperature_range_c": [
                    dataset_ranges[name][0] - 273.15,
                    dataset_ranges[name][1] - 273.15,
                ],
            }
        )
        datasets[name] = metadata

    manifest = {
        "schema_version": 1,
        "status": "ACTUAL_ONE_SECOND_SAMPLES_READY",
        "mesh_level": mesh_level,
        "mesh_cell_count": mesh_cell_count,
        "initial_temperature_k": initial_temperature_k,
        "initial_temperature_c": initial_temperature_k - 273.15,
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
    """Re-read every packed value and compare it with its direct probe source."""

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
        probe = read_probe_header(source_spec["path"])
        valid = np.asarray(probe.valid_indices, dtype=int)
        encoded = np.memmap(output / specification["file"], dtype="<u2", mode="r")
        expected_values = int(specification["frame_count"]) * len(valid)
        size_matches = encoded.size == expected_values
        maximum_error = 0.0
        times: list[int] = []
        if size_matches:
            frames = encoded.reshape(int(specification["frame_count"]), len(valid))
            initial = np.full(len(valid), float(manifest["initial_temperature_k"]))
            maximum_error = float(np.max(np.abs(low + frames[0] * quantum - initial)))
            for frame, (time_s, values) in enumerate(iter_probe_rows(probe), start=1):
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
    parser.add_argument("--end-time", type=int, default=1800)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.verify_only:
        build_actual_one_second_bundle(
            args.run_dir,
            args.output,
            initial_temperature_k=args.initial_temperature,
            end_time_s=args.end_time,
        )
    report = validate_actual_one_second_bundle(args.run_dir, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"].endswith("_PASSED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
