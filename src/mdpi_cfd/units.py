from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .errors import ConfigError


_NUMBER_AND_UNIT = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(.*?)\s*$"
)


@dataclass(frozen=True)
class UnitRule:
    dimension: str
    factor: float
    offset: float = 0.0


def _normalise_unit(unit: str) -> str:
    return (
        unit.strip()
        .replace("³", "^3")
        .replace("²", "^2")
        .replace("·", "*")
        .replace("Ω", "Ohm")
        .replace("℃", "degC")
        .replace("°C", "degC")
        .replace(" ", "")
    )


_RULES: dict[str, UnitRule] = {
    "m": UnitRule("length", 1.0),
    "mm": UnitRule("length", 1e-3),
    "m/s": UnitRule("velocity", 1.0),
    "s": UnitRule("time", 1.0),
    "K": UnitRule("temperature", 1.0),
    "degC": UnitRule("temperature", 1.0, 273.15),
    "Pa": UnitRule("pressure", 1.0),
    "kPa": UnitRule("pressure", 1e3),
    "kg/m^3": UnitRule("density", 1.0),
    "kg/m3": UnitRule("density", 1.0),
    "J/(kg*K)": UnitRule("specific_heat", 1.0),
    "J/kgK": UnitRule("specific_heat", 1.0),
    "W/(m*K)": UnitRule("thermal_conductivity", 1.0),
    "W/mK": UnitRule("thermal_conductivity", 1.0),
    "Pa*s": UnitRule("dynamic_viscosity", 1.0),
    "kg/(m*s)": UnitRule("dynamic_viscosity", 1.0),
    "W/m^3": UnitRule("volumetric_heat", 1.0),
    "W/m3": UnitRule("volumetric_heat", 1.0),
    "V": UnitRule("voltage", 1.0),
    "A": UnitRule("current", 1.0),
    "Ah": UnitRule("capacity", 1.0),
    "Ohm": UnitRule("resistance", 1.0),
    "mOhm": UnitRule("resistance", 1e-3),
}


def quantity(value: object, expected_dimension: str, field: str) -> float:
    """Parse a configured quantity and return its SI value."""

    if isinstance(value, bool):
        raise ConfigError(f"{field}: boolean is not a quantity")
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        match = _NUMBER_AND_UNIT.match(value)
        if not match:
            raise ConfigError(f"{field}: invalid quantity {value!r}")
        magnitude = float(match.group(1))
        unit = _normalise_unit(match.group(2))
        rule = _RULES.get(unit)
        if rule is None:
            raise ConfigError(f"{field}: unsupported unit {unit!r}")
        if rule.dimension != expected_dimension:
            raise ConfigError(
                f"{field}: expected {expected_dimension}, got {rule.dimension} ({unit})"
            )
        result = magnitude * rule.factor + rule.offset
    else:
        raise ConfigError(f"{field}: expected number or quantity string")

    if not math.isfinite(result):
        raise ConfigError(f"{field}: value must be finite")
    return result


def positive(value: float, field: str) -> float:
    if value <= 0:
        raise ConfigError(f"{field}: value must be positive")
    return value

