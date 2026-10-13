"""Merge binary or ASCII STL surface files into one named ASCII STL assembly.

Fluent Meshing 2024 R1 preserves the ``solid`` names in an ASCII STL.  The
production OpenCASCADE exports used by this project are binary STL files and
therefore carry only one generic header name.  This helper rewrites every
input file as a distinct named solid without changing triangle coordinates.
"""

from __future__ import annotations

import argparse
import re
import struct
from pathlib import Path


def _safe_name(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem)


def _binary_facets(data: bytes):
    if len(data) < 84:
        raise ValueError("binary STL is shorter than its 84-byte header")
    count = struct.unpack_from("<I", data, 80)[0]
    expected = 84 + 50 * count
    if len(data) != expected:
        raise ValueError(f"binary STL size mismatch: expected {expected}, got {len(data)}")
    offset = 84
    for _ in range(count):
        values = struct.unpack_from("<12fH", data, offset)
        yield values[:3], (values[3:6], values[6:9], values[9:12])
        offset += 50


def append_as_ascii(source: Path, output, name: str) -> int:
    data = source.read_bytes()
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if len(data) == 84 + 50 * count:
            output.write(f"solid {name}\n")
            for normal, vertices in _binary_facets(data):
                output.write("  facet normal {:.9g} {:.9g} {:.9g}\n".format(*normal))
                output.write("    outer loop\n")
                for vertex in vertices:
                    output.write("      vertex {:.9g} {:.9g} {:.9g}\n".format(*vertex))
                output.write("    endloop\n  endfacet\n")
            output.write(f"endsolid {name}\n")
            return count

    text = data.decode("ascii")
    facets = text.lower().count("facet normal")
    output.write(f"solid {name}\n")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(("solid ", "endsolid ")):
            continue
        output.write(line.rstrip() + "\n")
    output.write(f"endsolid {name}\n")
    return facets


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with args.output.open("w", encoding="ascii", newline="\n") as output:
        for source in args.inputs:
            count = append_as_ascii(source, output, _safe_name(source))
            print(f"{source.name}: {count} facets")
            total += count
    print(f"wrote {args.output} with {len(args.inputs)} solids and {total} facets")


if __name__ == "__main__":
    main()
