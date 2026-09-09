from __future__ import annotations

import csv
import json
import math
import os
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

from .config import ResolvedConfig
from .errors import ProfileError


class HeatSource(Protocol):
    mode: str

    def qvol(self, time_s: float) -> float: ...

    def breakpoints(self) -> tuple[float, ...]: ...

    def manifest(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class LinearSeries:
    times_s: tuple[float, ...]
    values: tuple[float, ...]
    value_name: str
    source_path: str
    outside_policy: str = "hold"

    @classmethod
    def from_csv(
        cls,
        path: str | Path,
        *,
        value_column: str,
        outside_policy: str = "hold",
        require_nonnegative: bool = True,
        require_positive: bool = False,
    ) -> "LinearSeries":
        source = Path(path).resolve()
        times: list[float] = []
        values: list[float] = []
        try:
            with source.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                required = {"time_s", value_column}
                if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                    raise ProfileError(
                        f"{source}: expected CSV columns {sorted(required)}"
                    )
                for line_no, row in enumerate(reader, start=2):
                    try:
                        time_s = float(row["time_s"])
                        value = float(row[value_column])
                    except (TypeError, ValueError) as exc:
                        raise ProfileError(f"{source}:{line_no}: non-numeric profile value") from exc
                    if not math.isfinite(time_s) or not math.isfinite(value):
                        raise ProfileError(f"{source}:{line_no}: values must be finite")
                    if time_s < 0:
                        raise ProfileError(f"{source}:{line_no}: time must be nonnegative")
                    if require_positive and value <= 0:
                        raise ProfileError(f"{source}:{line_no}: {value_column} must be positive")
                    if require_nonnegative and value < 0:
                        raise ProfileError(f"{source}:{line_no}: {value_column} must be nonnegative")
                    if times and time_s <= times[-1]:
                        raise ProfileError(f"{source}:{line_no}: time must be strictly increasing")
                    times.append(time_s)
                    values.append(value)
        except OSError as exc:
            raise ProfileError(f"cannot read profile {source}: {exc}") from exc
        if not times:
            raise ProfileError(f"{source}: profile has no data rows")
        if outside_policy not in {"hold", "error"}:
            raise ProfileError("outside_policy must be hold or error")
        return cls(tuple(times), tuple(values), value_column, str(source), outside_policy)

    def at(self, time_s: float) -> float:
        if not math.isfinite(time_s) or time_s < 0:
            raise ProfileError("evaluation time must be finite and nonnegative")
        if time_s < self.times_s[0]:
            if self.outside_policy == "error":
                raise ProfileError(f"time {time_s} is before profile start {self.times_s[0]}")
            return self.values[0]
        if time_s > self.times_s[-1]:
            if self.outside_policy == "error":
                raise ProfileError(f"time {time_s} is after profile end {self.times_s[-1]}")
            return self.values[-1]
        index = bisect_right(self.times_s, time_s)
        if index == 0:
            return self.values[0]
        if index == len(self.times_s):
            return self.values[-1]
        t0, t1 = self.times_s[index - 1], self.times_s[index]
        y0, y1 = self.values[index - 1], self.values[index]
        fraction = (time_s - t0) / (t1 - t0)
        return y0 + fraction * (y1 - y0)


@dataclass(frozen=True)
class ConstantVolumetricSource:
    value_w_m3: float
    mode: str = "constant_qvol"

    def qvol(self, time_s: float) -> float:
        if not math.isfinite(time_s) or time_s < 0:
            raise ProfileError("evaluation time must be finite and nonnegative")
        return self.value_w_m3

    def breakpoints(self) -> tuple[float, ...]:
        return (0.0,)

    def manifest(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "qvol_W_m3": self.value_w_m3,
            "paper_equivalence": "baseline_constant_value",
        }


@dataclass(frozen=True)
class TableVolumetricSource:
    series: LinearSeries
    mode: str = "table_qvol"

    def qvol(self, time_s: float) -> float:
        return self.series.at(time_s)

    def breakpoints(self) -> tuple[float, ...]:
        return self.series.times_s

    def manifest(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "profile": self.series.source_path,
            "outside_policy": self.series.outside_policy,
            "paper_equivalence": "user_extension_not_original_udf",
        }


@dataclass(frozen=True)
class ElectricalJouleSource:
    current: LinearSeries
    resistance: LinearSeries
    cell_volume_m3: float
    mode: str = "electrical_joule"

    def qvol(self, time_s: float) -> float:
        current_a = self.current.at(time_s)
        resistance_ohm = self.resistance.at(time_s)
        return current_a * current_a * resistance_ohm / self.cell_volume_m3

    def breakpoints(self) -> tuple[float, ...]:
        return tuple(sorted(set(self.current.times_s) | set(self.resistance.times_s)))

    def manifest(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "formula": "qvol(t)=I(t)^2*R(t)/V_cell",
            "current_profile": self.current.source_path,
            "resistance_profile": self.resistance.source_path,
            "cell_volume_m3": self.cell_volume_m3,
            "paper_equivalence": "joule_surrogate_not_original_udf",
            "omitted_terms": ["reversible_entropic_heat"],
        }


def _resolve_profile(config: ResolvedConfig, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(config.source_path).parent / candidate
    return candidate.resolve()


def source_from_config(config: ResolvedConfig) -> HeatSource:
    spec = config.battery.heat_source
    if spec.mode == "constant_qvol":
        assert spec.qvol_w_m3 is not None
        return ConstantVolumetricSource(spec.qvol_w_m3)
    if spec.mode == "table_qvol":
        assert spec.table_path is not None
        return TableVolumetricSource(
            LinearSeries.from_csv(
                _resolve_profile(config, spec.table_path),
                value_column="qvol_W_m3",
                outside_policy=spec.outside_policy,
            )
        )
    assert spec.current_profile_path is not None
    assert spec.resistance_profile_path is not None
    current = LinearSeries.from_csv(
        _resolve_profile(config, spec.current_profile_path),
        value_column="current_A",
        outside_policy=spec.outside_policy,
        require_nonnegative=False,
    )
    resistance = LinearSeries.from_csv(
        _resolve_profile(config, spec.resistance_profile_path),
        value_column="resistance_ohm",
        outside_policy=spec.outside_policy,
        require_positive=True,
    )
    return ElectricalJouleSource(current, resistance, config.derived.cell_volume_m3)


def _sample_source(source: HeatSource) -> list[tuple[float, float]]:
    times = source.breakpoints()
    if len(times) == 1:
        return [(0.0, source.qvol(0.0))]
    return [(time_s, source.qvol(time_s)) for time_s in times]


def render_openfoam_fvoptions(source: HeatSource, energy_field: str) -> str:
    """Render an OpenCFD v2512 scalarSemiImplicitSource dictionary.

    The target file belongs in the battery solid region, where selectionMode
    all applies the specific W/m3 source to every battery cell.
    """

    samples = _sample_source(source)
    if len(samples) == 1:
        source_entry = f"        {energy_field:<12} ({samples[0][1]:.12g} 0);"
    else:
        rows = " ".join(f"({t:.12g} {q:.12g})" for t, q in samples)
        source_entry = (
            f"        {energy_field}\n"
            "        {\n"
            f"            explicit table ({rows});\n"
            "            implicit none;\n"
            "        }"
        )
    return f'''FoamFile
{{
    format      ascii;
    class       dictionary;
    object      fvOptions;
}}

// Generated by cfd-twin. qvol units are W/m3.
// Dynamic modes are user-supplied surrogates, not the unpublished paper UDF.
batteryHeatSource
{{
    type            scalarSemiImplicitSource;
    active          true;
    selectionMode   all;
    volumeMode      specific;
    sources
    {{
{source_entry}
    }}
}}
'''


def _c_array(values: Iterable[float]) -> str:
    return ", ".join(f"{value:.17g}" for value in values)


def render_fluent_udf(source: HeatSource) -> str:
    samples = _sample_source(source)
    if len(samples) == 1:
        body = f"    return {samples[0][1]:.17g};"
        helpers = ""
    else:
        times = [item[0] for item in samples]
        values = [item[1] for item in samples]
        helpers = f'''#define PROFILE_COUNT {len(samples)}
static const real profile_time[PROFILE_COUNT] = {{{_c_array(times)}}};
static const real profile_qvol[PROFILE_COUNT] = {{{_c_array(values)}}};

static real interpolate_qvol(real time)
{{
    int i;
    if (time <= profile_time[0]) return profile_qvol[0];
    if (time >= profile_time[PROFILE_COUNT - 1]) return profile_qvol[PROFILE_COUNT - 1];
    for (i = 1; i < PROFILE_COUNT; ++i)
    {{
        if (time <= profile_time[i])
        {{
            real f = (time - profile_time[i - 1]) /
                     (profile_time[i] - profile_time[i - 1]);
            return profile_qvol[i - 1] + f *
                   (profile_qvol[i] - profile_qvol[i - 1]);
        }}
    }}
    return profile_qvol[PROFILE_COUNT - 1];
}}
'''
        body = "    return interpolate_qvol(CURRENT_TIME);"
    return f'''#include "udf.h"

/* Generated by cfd-twin.
 * Volumetric source unit: W/m^3.
 * This file is not the unpublished original paper UDF.
 */
{helpers}
DEFINE_SOURCE(battery_heat_source, cell, thread, dS, eqn)
{{
    dS[eqn] = 0.0;
{body}
}}
'''


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def write_heat_source_bundle(
    config: ResolvedConfig, output_dir: str | Path
) -> dict[str, object]:
    output = Path(output_dir).resolve()
    source = source_from_config(config)
    manifest = {
        **source.manifest(),
        "cell_volume_m3": config.derived.cell_volume_m3,
        "cell_count": config.cells.count,
        "constant_baseline_qvol_W_m3": 16325.0,
        "nominal_current_A": config.derived.nominal_current_a,
        "equivalent_resistance_Ohm": config.derived.equivalent_resistance_ohm,
        "equivalent_resistance_status": "derived_not_measured",
        "original_paper_udf_available": False,
    }
    _atomic_write(
        output / "heat_source_manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(
        output / "OpenFOAM" / "batterySolid" / "fvOptions",
        render_openfoam_fvoptions(source, config.solver.energy_field),
    )
    _atomic_write(output / "Fluent" / "battery_heat_source.c", render_fluent_udf(source))
    return manifest
