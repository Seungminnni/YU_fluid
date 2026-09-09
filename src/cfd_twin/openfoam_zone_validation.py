from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


EXPECTED_BATTERY_ZONES = tuple(f"battery_C{index:02d}" for index in range(1, 24))


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*?$", "", text, flags=re.MULTILINE)


def _named_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    pattern = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\{")
    for match in pattern.finditer(text):
        name = match.group(1)
        if name == "FoamFile":
            continue
        depth = 1
        cursor = match.end()
        while cursor < len(text) and depth:
            if text[cursor] == "{":
                depth += 1
            elif text[cursor] == "}":
                depth -= 1
            cursor += 1
        if depth == 0:
            blocks[name] = text[match.end() : cursor - 1]
    return blocks


def _labels_from_block(block: str, zone_name: str) -> list[int]:
    match = re.search(
        r"\b(?:cellLabels|labels)\b\s*(?:List<label>\s*)?(\d+)\s*\((.*?)\)\s*;?",
        block,
        flags=re.DOTALL,
    )
    if not match:
        raise ValueError(f"{zone_name}: ASCII cell label list was not found")
    declared = int(match.group(1))
    labels = [int(value) for value in re.findall(r"[-+]?\d+", match.group(2))]
    if len(labels) != declared:
        raise ValueError(
            f"{zone_name}: declared {declared} labels but parsed {len(labels)}"
        )
    return labels


def parse_cell_zones(path: str | Path) -> dict[str, list[int]]:
    source = Path(path)
    raw = source.read_bytes()
    if b"format      binary" in raw or b"format binary" in raw:
        raise ValueError(
            f"{source}: binary cellZones is unsupported; set writeFormat ascii"
        )
    text = _strip_comments(raw.decode("utf-8"))
    zones: dict[str, list[int]] = {}
    for name, block in _named_blocks(text).items():
        if re.search(r"\btype\s+cellZone\s*;", block):
            zones[name] = _labels_from_block(block, name)
    return zones


def _n_cells_from_owner(path: Path) -> int | None:
    if not path.is_file():
        return None
    raw = path.read_bytes()
    if b"format      binary" in raw or b"format binary" in raw:
        text = raw[:4096].decode("latin-1", errors="ignore")
    else:
        text = raw.decode("utf-8", errors="ignore")
    match = re.search(r"\bnCells\s*:\s*(\d+)", text)
    return int(match.group(1)) if match else None


def validate_cell_zones(
    mesh_dir: str | Path,
    *,
    mode: str = "pre_split",
) -> dict[str, object]:
    mesh = Path(mesh_dir).resolve()
    zones = parse_cell_zones(mesh / "cellZones")
    expected = set(EXPECTED_BATTERY_ZONES)
    permitted_empty_foreign: set[str] = set()
    if mode == "pre_split":
        expected.add("coolant")
    elif mode == "battery_region":
        # splitMeshRegions preserves the source zone registry in each region.
        # The coolant zone therefore remains as an empty metadata entry in the
        # batterySolid mesh; all battery zones still have to be exact,
        # nonempty, exclusive, and cover every solid cell.
        permitted_empty_foreign.add("coolant")
    else:
        raise ValueError(f"unsupported validation mode: {mode}")

    actual = set(zones)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected - permitted_empty_foreign)
    nonempty_permitted_foreign = sorted(
        name for name in permitted_empty_foreign & actual if zones[name]
    )
    empty = sorted(name for name in expected & actual if not zones[name])

    label_owner: dict[int, str] = {}
    duplicate_labels: dict[int, list[str]] = {}
    for name in sorted(expected & actual):
        for label in zones[name]:
            previous = label_owner.get(label)
            if previous is not None:
                duplicate_labels.setdefault(label, [previous]).append(name)
            else:
                label_owner[label] = name

    n_cells = _n_cells_from_owner(mesh / "owner")
    covered = set(label_owner)
    uncovered_count: int | None = None
    out_of_range = []
    if n_cells is not None:
        uncovered_count = n_cells - len(covered)
        out_of_range = sorted(label for label in covered if not 0 <= label < n_cells)

    checks = {
        "expected_zone_names_exact": not missing and not unexpected,
        "permitted_foreign_zones_are_empty": not nonempty_permitted_foreign,
        "all_expected_zones_nonempty": not empty,
        "cell_labels_unique_across_zones": not duplicate_labels,
        "all_mesh_cells_covered": n_cells is not None and uncovered_count == 0,
        "all_cell_labels_in_range": n_cells is not None and not out_of_range,
    }
    report: dict[str, object] = {
        "schema_version": 1,
        "status": "CELLZONE_VALIDATION_PASSED" if all(checks.values()) else "CELLZONE_VALIDATION_FAILED",
        "mode": mode,
        "mesh_dir": str(mesh),
        "checks": checks,
        "expected_zones": sorted(expected),
        "actual_zones": sorted(actual),
        "missing_zones": missing,
        "unexpected_zones": unexpected,
        "permitted_empty_foreign_zones": sorted(permitted_empty_foreign),
        "nonempty_permitted_foreign_zones": nonempty_permitted_foreign,
        "empty_zones": empty,
        "zone_cell_counts": {name: len(labels) for name, labels in sorted(zones.items())},
        "mesh_cell_count": n_cells,
        "covered_cell_count": len(covered),
        "uncovered_cell_count": uncovered_count,
        "duplicate_cell_label_count": len(duplicate_labels),
        "duplicate_cell_label_examples": {
            str(label): names for label, names in list(sorted(duplicate_labels.items()))[:20]
        },
        "out_of_range_label_examples": out_of_range[:20],
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate exact OpenFOAM battery/coolant cellZone coverage"
    )
    parser.add_argument("--mesh-dir", required=True)
    parser.add_argument(
        "--mode", choices=("pre_split", "battery_region"), default="pre_split"
    )
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        report = validate_cell_zones(args.mesh_dir, mode=args.mode)
    except (OSError, UnicodeError, ValueError) as exc:
        report = {
            "schema_version": 1,
            "status": "CELLZONE_VALIDATION_FAILED",
            "mode": args.mode,
            "mesh_dir": str(Path(args.mesh_dir).resolve()),
            "error": str(exc),
        }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "CELLZONE_VALIDATION_PASSED" else 2


if __name__ == "__main__":
    sys.exit(main())
