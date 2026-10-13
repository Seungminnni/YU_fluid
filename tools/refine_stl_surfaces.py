"""Conformingly subdivide long STL edges and write a named ASCII assembly.

The source OpenCASCADE STL surfaces are geometrically exact enough for this
case, but cylinder side triangles span most of the cell length.  Fluent's
faceted-input workflow preserves those triangles when it keeps body names.
This utility splits every over-length shared edge in all incident triangles,
so the closed topology and solid names are retained without reusing any mesh.
"""

from __future__ import annotations

import argparse
import math
import re
import struct
from pathlib import Path


Vec = tuple[float, float, float]
Face = tuple[int, int, int]


def safe_name(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem)


def read_binary_stl(path: Path) -> tuple[list[Vec], list[Face]]:
    data = path.read_bytes()
    if len(data) < 84:
        raise ValueError(f"{path}: not a binary STL")
    count = struct.unpack_from("<I", data, 80)[0]
    if len(data) != 84 + 50 * count:
        raise ValueError(f"{path}: unsupported/non-binary STL")
    vertices: list[Vec] = []
    vertex_ids: dict[tuple[float, float, float], int] = {}
    faces: list[Face] = []
    offset = 84
    for _ in range(count):
        values = struct.unpack_from("<12fH", data, offset)
        face_ids = []
        for raw in (values[3:6], values[6:9], values[9:12]):
            vertex = (float(raw[0]), float(raw[1]), float(raw[2]))
            key = tuple(round(value, 12) for value in vertex)
            index = vertex_ids.get(key)
            if index is None:
                index = len(vertices)
                vertices.append(vertex)
                vertex_ids[key] = index
            face_ids.append(index)
        faces.append((face_ids[0], face_ids[1], face_ids[2]))
        offset += 50
    return vertices, faces


def edge_key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def distance(a: Vec, b: Vec) -> float:
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def refine(vertices: list[Vec], faces: list[Face], max_edge: float) -> tuple[list[Vec], list[Face], int]:
    rounds = 0
    while True:
        marked: set[tuple[int, int]] = set()
        for v0, v1, v2 in faces:
            edges = ((v0, v1), (v1, v2), (v2, v0))
            longest = max(edges, key=lambda edge: distance(vertices[edge[0]], vertices[edge[1]]))
            if distance(vertices[longest[0]], vertices[longest[1]]) > max_edge * (1.0 + 1e-10):
                marked.add(edge_key(*longest))
        if not marked:
            return vertices, faces, rounds

        midpoint_ids: dict[tuple[int, int], int] = {}
        for edge in marked:
            a, b = edge
            va, vb = vertices[a], vertices[b]
            midpoint_ids[edge] = len(vertices)
            vertices.append(tuple((va[i] + vb[i]) * 0.5 for i in range(3)))

        new_faces: list[Face] = []
        for v0, v1, v2 in faces:
            e01, e12, e20 = edge_key(v0, v1), edge_key(v1, v2), edge_key(v2, v0)
            b01, b12, b20 = e01 in marked, e12 in marked, e20 in marked
            count = int(b01) + int(b12) + int(b20)
            if count == 0:
                new_faces.append((v0, v1, v2))
            elif count == 1:
                if b01:
                    m = midpoint_ids[e01]
                    new_faces.extend(((v0, m, v2), (m, v1, v2)))
                elif b12:
                    m = midpoint_ids[e12]
                    new_faces.extend(((v1, m, v0), (m, v2, v0)))
                else:
                    m = midpoint_ids[e20]
                    new_faces.extend(((v2, m, v1), (m, v0, v1)))
            elif count == 2:
                if b01 and b12:
                    m01, m12 = midpoint_ids[e01], midpoint_ids[e12]
                    new_faces.extend(((v1, m12, m01), (v0, m01, v2), (m01, m12, v2)))
                elif b12 and b20:
                    m12, m20 = midpoint_ids[e12], midpoint_ids[e20]
                    new_faces.extend(((v2, m20, m12), (v1, m12, v0), (m12, m20, v0)))
                else:
                    m20, m01 = midpoint_ids[e20], midpoint_ids[e01]
                    new_faces.extend(((v0, m01, m20), (v2, m20, v1), (m20, m01, v1)))
            else:
                m01, m12, m20 = midpoint_ids[e01], midpoint_ids[e12], midpoint_ids[e20]
                new_faces.extend(
                    (
                        (v0, m01, m20),
                        (m01, v1, m12),
                        (m20, m12, v2),
                        (m01, m12, m20),
                    )
                )
        faces = new_faces
        rounds += 1


def normal(a: Vec, b: Vec, c: Vec) -> Vec:
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length == 0.0:
        return (0.0, 0.0, 0.0)
    return (nx / length, ny / length, nz / length)


def write_solid(output, name: str, vertices: list[Vec], faces: list[Face]) -> None:
    output.write(f"solid {name}\n")
    for i0, i1, i2 in faces:
        a, b, c = vertices[i0], vertices[i1], vertices[i2]
        n = normal(a, b, c)
        output.write("  facet normal {:.12g} {:.12g} {:.12g}\n".format(*n))
        output.write("    outer loop\n")
        output.write("      vertex {:.12g} {:.12g} {:.12g}\n".format(*a))
        output.write("      vertex {:.12g} {:.12g} {:.12g}\n".format(*b))
        output.write("      vertex {:.12g} {:.12g} {:.12g}\n".format(*c))
        output.write("    endloop\n  endfacet\n")
    output.write(f"endsolid {name}\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-edge", type=float, required=True, help="maximum edge length in STL units")
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("inputs", nargs="*", type=Path)
    args = parser.parse_args()

    inputs = list(args.inputs)
    if args.input_dir is not None:
        inputs = [args.input_dir / "outerDomain.stl"] + sorted(
            args.input_dir.glob("battery_C*.stl")
        )
    if not inputs:
        parser.error("provide input STL files or --input-dir")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    total_before = 0
    total_after = 0
    with args.output.open("w", encoding="ascii", newline="\n") as output:
        for source in inputs:
            vertices, faces = read_binary_stl(source)
            before = len(faces)
            vertices, faces, rounds = refine(vertices, faces, args.max_edge)
            write_solid(output, safe_name(source), vertices, faces)
            print(f"{source.name}: {before} -> {len(faces)} facets in {rounds} rounds")
            total_before += before
            total_after += len(faces)
    print(f"wrote {args.output}: {total_before} -> {total_after} facets")


if __name__ == "__main__":
    main()
