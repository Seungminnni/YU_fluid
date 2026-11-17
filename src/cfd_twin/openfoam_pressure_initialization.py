"""Seed absolute OpenFOAM pressure fields from potentialFoam Euler pressure."""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
from pathlib import Path
import re
import tempfile


KINEMATIC_DIMENSIONS = "[0 2 -2 0 0 0 0]"
PRESSURE_DIMENSIONS = "[1 -1 -2 0 0 0 0]"
SCALAR = re.compile(r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)\s*$")
UNIFORM_INTERNAL = re.compile(
    r"(?m)^\s*internalField\s+uniform\s+"
    r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?\s*;\s*$"
)


def _opener(path: Path):
    return gzip.open if path.suffix == ".gz" else open


def _spool_absolute_values(
    potential: Path,
    spool: Path,
    density: float,
    outlet_pressure: float,
) -> tuple[int, float, float]:
    dimensions_seen = 0
    count: int | None = None
    remaining = 0
    awaiting_count = awaiting_open = False
    minimum = math.inf
    maximum = -math.inf
    with _opener(potential)(potential, "rt", encoding="utf-8", newline="") as source, open(
        spool, "wt", encoding="utf-8", newline="\n"
    ) as destination:
        for raw in source:
            line = raw.rstrip("\r\n")
            if "dimensions" in line:
                if KINEMATIC_DIMENSIONS not in line:
                    raise ValueError("potential pressure has unexpected dimensions")
                dimensions_seen += 1
            if awaiting_open:
                if line.strip() != "(":
                    raise ValueError("expected '(' before potential pressure values")
                awaiting_open = False
                continue
            if remaining:
                match = SCALAR.fullmatch(line)
                if match is None:
                    raise ValueError(
                        f"expected potential pressure scalar with {remaining} entries remaining"
                    )
                value = outlet_pressure + density * float(match.group(1))
                if not math.isfinite(value) or value <= 0:
                    raise ValueError("potential pressure produced non-positive absolute pressure")
                destination.write(f"{value:.12g}\n")
                minimum = min(minimum, value)
                maximum = max(maximum, value)
                remaining -= 1
                continue
            if awaiting_count:
                if not line.strip().isdigit():
                    raise ValueError("expected potential pressure internal-field count")
                count = int(line.strip())
                remaining = count
                awaiting_count = False
                awaiting_open = True
                continue
            if re.match(r"^\s*internalField\s+nonuniform\s+List<scalar>\s*$", line):
                awaiting_count = True
        if dimensions_seen != 1:
            raise ValueError(f"expected one kinematic dimensions entry, found {dimensions_seen}")
        if count is None or count <= 0 or awaiting_count or awaiting_open or remaining:
            raise ValueError("incomplete or empty potential pressure internal field")
    return count, minimum, maximum


def _write_seeded_field(template: Path, spool: Path, count: int) -> None:
    if not template.is_file():
        raise FileNotFoundError(template)
    with _opener(template)(template, "rt", encoding="utf-8", newline="") as source:
        rendered = source.read()
    if PRESSURE_DIMENSIONS not in rendered:
        raise ValueError(f"target field has unexpected dimensions: {template}")
    matches = list(UNIFORM_INTERNAL.finditer(rendered))
    if len(matches) != 1:
        raise ValueError(f"expected one uniform internal field in {template}, found {len(matches)}")
    match = matches[0]
    descriptor, name = tempfile.mkstemp(
        prefix=template.name + ".", suffix=".tmp", dir=template.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    destination_open = gzip.open if template.suffix == ".gz" else open
    try:
        with destination_open(temporary, "wt", encoding="utf-8", newline="\n") as destination:
            destination.write(rendered[: match.start()])
            destination.write(
                "internalField   nonuniform List<scalar>\n"
                f"{count}\n(\n"
            )
            with open(spool, "rt", encoding="utf-8", newline="") as values:
                for line in values:
                    destination.write(line)
            destination.write(")\n;\n")
            destination.write(rendered[match.end() :])
        os.replace(temporary, template)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def seed(
    potential: Path,
    pressure: Path,
    pressure_rgh: Path,
    density: float,
    outlet_pressure: float,
) -> dict[str, object]:
    if not potential.is_file():
        raise FileNotFoundError(potential)
    if not math.isfinite(density) or density <= 0:
        raise ValueError("density must be positive and finite")
    if not math.isfinite(outlet_pressure) or outlet_pressure <= 0:
        raise ValueError("outlet pressure must be positive and finite")
    descriptor, name = tempfile.mkstemp(
        prefix="potential-pressure.", suffix=".values", dir=potential.parent
    )
    os.close(descriptor)
    spool = Path(name)
    try:
        count, minimum, maximum = _spool_absolute_values(
            potential, spool, density, outlet_pressure
        )
        _write_seeded_field(pressure, spool, count)
        _write_seeded_field(pressure_rgh, spool, count)
    finally:
        spool.unlink(missing_ok=True)
    return {
        "status": "POTENTIAL_PRESSURE_INITIALIZATION_PASSED",
        "potential": str(potential.resolve()),
        "pressure": str(pressure.resolve()),
        "pressure_rgh": str(pressure_rgh.resolve()),
        "density_kg_m3": density,
        "outlet_absolute_pressure_pa": outlet_pressure,
        "cell_count": count,
        "minimum_absolute_pressure_pa": minimum,
        "maximum_absolute_pressure_pa": maximum,
        "gravity_m_s2": [0.0, 0.0, 0.0],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--potential", required=True, type=Path)
    parser.add_argument("--pressure", required=True, type=Path)
    parser.add_argument("--pressure-rgh", required=True, type=Path)
    parser.add_argument("--density", required=True, type=float)
    parser.add_argument("--outlet-pressure", required=True, type=float)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    report = seed(
        args.potential,
        args.pressure,
        args.pressure_rgh,
        args.density,
        args.outlet_pressure,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report, indent=2) + "\n"
    args.report.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
