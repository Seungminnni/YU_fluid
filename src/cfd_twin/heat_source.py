"""배터리 체적발열 q'''(t)를 정의하고 OpenFOAM/Fluent 입력으로 변환한다.

현재 기준 계산은 기존 baseline의 상수 16,325 W/m^3를 사용한다. 시간표와
I(t)^2 R(t)/Vcell 모드는 확장용 대체모델이다. 보유 논문에 숫자로 명시된
1차 상관식은 opt-in provisional UDF로 만들 수 있지만, 공개되지 않은 최종
4차 UDF와 같다고 간주하지 않는다.
"""

from __future__ import annotations

import csv
import json
import math
import os
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

from .battery_correlation import (
    PENEKLIOGLU_FIRST_ORDER_COEFFICIENTS,
    PENEKLIOGLU_TRAINING_OUTPUT_MINIMUM_W,
    discharge_soc,
    peneklioglu_expanded_first_order_q_w,
)
from .config import ResolvedConfig
from .engineering_judgement import write_engineering_judgements
from .errors import ProfileError


class HeatSource(Protocol):
    """모든 발열모델이 제공해야 하는 공통 인터페이스."""

    mode: str

    def qvol(self, time_s: float) -> float: ...

    def breakpoints(self) -> tuple[float, ...]: ...

    def manifest(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class LinearSeries:
    """시간 CSV를 읽어 구간별 선형보간하고 범위 밖 정책을 적용한다."""

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
    """모든 시간에 동일한 W/m^3를 반환하는 현재 기준 발열모델."""

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
    """사용자 제공 time_s/qvol_W_m3 표에서 시간별 발열을 계산한다."""

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
    """Joule 대체식 q'''=I(t)^2 R(t)/Vcell을 계산한다.

    가역 엔트로피열과 반응·혼합열은 포함하지 않으므로 논문 UDF와 동일하다고
    주장할 수 없다.
    """

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


@dataclass(frozen=True)
class PenekliogluExpandedFirstOrderSource:
    """보유 논문 식 (4.1)을 시간/SOC 발열원으로 변환한 임시 모델이다.

    ``q = 1.086 D - 0.84 S - 0.01486 T + 4.41`` [W/cell]과
    ``S=S0-Dt/3600``을 사용한다. 여기의 T는 명시적으로 고정한 기준온도이며
    국부 셀 온도의 되먹임이 아니다.
    """

    c_rate: float
    initial_soc: float
    reference_temperature_k: float
    cell_volume_m3: float
    outside_policy: str = "error"
    mode: str = "peneklioglu_expanded_first_order"

    def __post_init__(self) -> None:
        if self.cell_volume_m3 <= 0 or not math.isfinite(self.cell_volume_m3):
            raise ProfileError("cell volume must be finite and positive")
        if self.outside_policy not in {"hold", "error"}:
            raise ProfileError("outside_policy must be hold or error")
        # Pure functions perform all paper-domain checks. Both endpoints must
        # also remain inside the q>0.5 W subset used to fit equation (4.1).
        endpoint_heat = (
            peneklioglu_expanded_first_order_q_w(
                self.c_rate, self.initial_soc, self.reference_temperature_k
            ),
            peneklioglu_expanded_first_order_q_w(
                self.c_rate, 0.0, self.reference_temperature_k
            ),
        )
        if min(endpoint_heat) < PENEKLIOGLU_TRAINING_OUTPUT_MINIMUM_W:
            raise ProfileError(
                "equation (4.1) may only be used where predicted q is at least 0.5 W/cell"
            )

    @property
    def discharge_end_s(self) -> float:
        return self.initial_soc * 3600.0 / self.c_rate

    def soc(self, time_s: float) -> float:
        return discharge_soc(
            time_s,
            c_rate=self.c_rate,
            initial_soc=self.initial_soc,
            outside_policy=self.outside_policy,
        )

    def qcell(self, time_s: float) -> float:
        return peneklioglu_expanded_first_order_q_w(
            self.c_rate, self.soc(time_s), self.reference_temperature_k
        )

    def qvol(self, time_s: float) -> float:
        return self.qcell(time_s) / self.cell_volume_m3

    def breakpoints(self) -> tuple[float, ...]:
        # q is linear in SOC and SOC is linear in time. Two endpoints therefore
        # reproduce the published first-order equation exactly between them.
        return (0.0, self.discharge_end_s)

    def manifest(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "status": "PROVISIONAL_OPT_IN",
            "formula_W_per_cell": "q=1.086*D-0.84*SOC-0.01486*T_ref+4.41",
            "soc_formula": "SOC(t)=SOC0-D*t/3600",
            "coefficients": PENEKLIOGLU_FIRST_ORDER_COEFFICIENTS,
            "c_rate": self.c_rate,
            "initial_soc": self.initial_soc,
            "reference_temperature_K": self.reference_temperature_k,
            "temperature_semantics": "fixed_reference_not_local_cell_feedback",
            "discharge_end_s": self.discharge_end_s,
            "q_cell_W": {
                "at_0_s": self.qcell(0.0),
                "at_discharge_end": self.qcell(self.discharge_end_s),
            },
            "qvol_W_m3": {
                "at_0_s": self.qvol(0.0),
                "at_discharge_end": self.qvol(self.discharge_end_s),
            },
            "paper_equivalence": "published_equation_4_1_only_not_final_fourth_order_udf",
            "final_fourth_order_udf_status": "BLOCKED_FULL_PRECISION_COEFFICIENTS_OR_ORIGINAL_UDF_REQUIRED",
        }


def _resolve_profile(config: ResolvedConfig, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(config.source_path).parent / candidate
    return candidate.resolve()


def source_from_config(config: ResolvedConfig) -> HeatSource:
    """YAML의 mode에 맞는 상수/시간표/Joule 발열 객체 하나를 선택한다."""

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
    if spec.mode == "peneklioglu_expanded_first_order":
        assert spec.initial_soc is not None
        assert spec.reference_temperature_k is not None
        return PenekliogluExpandedFirstOrderSource(
            c_rate=config.battery.electrical.discharge_rate_c,
            initial_soc=spec.initial_soc,
            reference_temperature_k=spec.reference_temperature_k,
            cell_volume_m3=config.derived.cell_volume_m3,
            outside_policy=spec.outside_policy,
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
    """OpenCFD v2512의 scalarSemiImplicitSource dictionary를 만든다.

    결과 파일은 batterySolid/constant/fvOptions에 놓인다. selectionMode all과
    volumeMode specific의 조합으로 23개 고체 셀 전체의 엔탈피 방정식에
    W/m^3 단위 발열을 적용한다.
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
// Dynamic modes are explicit generated models, not the unpublished final paper UDF.
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
        "final_fourth_order_udf_applied": False,
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
    write_engineering_judgements(output / "engineering_judgements.json")
    if isinstance(source, PenekliogluExpandedFirstOrderSource):
        sample_times = sorted(
            set(
                [0.0, source.discharge_end_s]
                + [
                    float(time_s)
                    for time_s in range(0, int(source.discharge_end_s) + 1, 60)
                ]
            )
        )
        rows = ["time_s,SOC,q_cell_W,qvol_W_m3"]
        rows.extend(
            f"{time_s:.12g},{source.soc(time_s):.12g},"
            f"{source.qcell(time_s):.12g},{source.qvol(time_s):.12g}"
            for time_s in sample_times
        )
        _atomic_write(output / "heat_source_samples.csv", "\n".join(rows) + "\n")
    return manifest
