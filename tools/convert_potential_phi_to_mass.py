from __future__ import annotations

import argparse
import gzip
import json
import math
import os
from pathlib import Path
import re
import tempfile


VOLUMETRIC_DIMENSIONS = "[0 3 -1 0 0 0 0]"
MASS_DIMENSIONS = "[1 0 -1 0 0 0 0]"
SCALAR = re.compile(r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)\s*$")


def convert(path: Path, density: float) -> dict[str, object]:
    if not math.isfinite(density) or density <= 0:
        raise ValueError("density must be positive and finite")
    if not path.is_file():
        raise FileNotFoundError(path)
    opener = gzip.open if path.suffix == ".gz" else open
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    os.close(temporary_fd)
    temporary = Path(temporary_name)
    list_count = 0
    scalar_count = 0
    dimensions_changed = 0
    awaiting_count = False
    awaiting_open = False
    remaining = 0
    try:
        with opener(path, "rt", encoding="utf-8", newline="") as source, gzip.open(
            temporary, "wt", encoding="utf-8", newline="\n"
        ) if path.suffix == ".gz" else open(
            temporary, "wt", encoding="utf-8", newline="\n"
        ) as destination:
            for raw in source:
                line = raw.rstrip("\r\n")
                if "dimensions" in line and VOLUMETRIC_DIMENSIONS in line:
                    line = line.replace(VOLUMETRIC_DIMENSIONS, MASS_DIMENSIONS)
                    dimensions_changed += 1
                if awaiting_open:
                    if line.strip() != "(":
                        raise ValueError("expected '(' before scalar list")
                    awaiting_open = False
                    destination.write(line + "\n")
                    continue
                if remaining:
                    match = SCALAR.fullmatch(line)
                    if match is None:
                        raise ValueError(f"expected scalar value with {remaining} entries remaining")
                    value = float(match.group(1)) * density
                    destination.write(f"{value:.12g}\n")
                    remaining -= 1
                    scalar_count += 1
                    continue
                if awaiting_count:
                    stripped = line.strip()
                    if not stripped.isdigit():
                        raise ValueError("expected scalar-list count")
                    remaining = int(stripped)
                    list_count += 1
                    awaiting_count = False
                    awaiting_open = True
                    destination.write(line + "\n")
                    continue
                destination.write(line + "\n")
                if "nonuniform List<scalar>" in line:
                    awaiting_count = True
        if dimensions_changed != 1:
            raise ValueError(f"expected one volumetric dimensions entry, found {dimensions_changed}")
        if awaiting_count or awaiting_open or remaining:
            raise ValueError("incomplete scalar list")
        if list_count < 1 or scalar_count < 1:
            raise ValueError("no scalar lists converted")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "status": "POTENTIAL_PHI_MASS_CONVERSION_PASSED",
        "path": str(path.resolve()),
        "density_kg_m3": density,
        "list_count": list_count,
        "scalar_count": scalar_count,
        "dimensions_before": VOLUMETRIC_DIMENSIONS,
        "dimensions_after": MASS_DIMENSIONS,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phi", required=True, type=Path)
    parser.add_argument("--density", required=True, type=float)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = convert(args.phi, args.density)
    rendered = json.dumps(report, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
