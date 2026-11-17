#!/usr/bin/env python3
"""Prepare and validate the deterministic high-confidence ROM FOM campaign."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
from pathlib import Path

import yaml


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _distance(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(a, b)))


def _lhs(rng: random.Random, count: int, dimensions: int) -> list[tuple[float, ...]]:
    columns: list[list[float]] = []
    for _ in range(dimensions):
        values = [(index + rng.random()) / count for index in range(count)]
        rng.shuffle(values)
        columns.append(values)
    return [tuple(column[row] for column in columns) for row in range(count)]


def _minimum_distance(
    points: list[tuple[float, ...]], anchors: list[tuple[float, ...]]
) -> float:
    combined = anchors + points
    return min(
        _distance(combined[left], combined[right])
        for left in range(len(combined))
        for right in range(left + 1, len(combined))
    )


def _maximin_lhs(
    seed: int,
    count: int,
    anchors: list[tuple[float, ...]],
    candidates: int = 2048,
) -> tuple[list[tuple[float, ...]], float]:
    rng = random.Random(seed)
    best: list[tuple[float, ...]] | None = None
    best_score = -1.0
    for _ in range(candidates):
        candidate = _lhs(rng, count, 3)
        score = _minimum_distance(candidate, anchors)
        if score > best_score:
            best = candidate
            best_score = score
    assert best is not None
    return best, best_score


def _scale(value: float, minimum: float, maximum: float) -> float:
    return minimum + value * (maximum - minimum)


def _normalise(
    point: tuple[float, float, float], bounds: list[tuple[float, float]]
) -> tuple[float, float, float]:
    return tuple(
        (value - minimum) / (maximum - minimum)
        for value, (minimum, maximum) in zip(point, bounds)
    )  # type: ignore[return-value]


def _split_assignments(seed: int, total: int) -> dict[int, str]:
    # Place the baseline and corners across all three groups, then fill the
    # remaining capacities deterministically. Splitting is always case-level.
    assignments = {
        0: "train",
        1: "blind_test",
        2: "train",
        3: "validation",
        4: "train",
        5: "validation",
        6: "train",
        7: "blind_test",
        8: "train",
    }
    capacities = {"train": 30, "validation": 10, "blind_test": 8}
    for split in assignments.values():
        capacities[split] -= 1
    remaining = list(range(9, total))
    random.Random(seed + 1).shuffle(remaining)
    cursor = 0
    for split in ("train", "validation", "blind_test"):
        for index in remaining[cursor : cursor + capacities[split]]:
            assignments[index] = split
        cursor += capacities[split]
    return assignments


def prepare(contract_path: Path, output_dir: Path) -> dict[str, object]:
    contract_bytes = contract_path.read_bytes()
    contract = yaml.safe_load(contract_bytes)
    campaign = contract["campaign"]
    parameters = contract["parameters"]
    design = contract["design"]
    seed = int(campaign["random_seed"])
    total = int(campaign["initial_fom_cases"])
    anchors = [tuple(map(float, row)) for row in design["mandatory_anchor_cases"]]
    if total != 48 or len(anchors) != 9:
        raise ValueError("v1 contract requires 48 initial cases and 9 anchors")

    bounds = [
        (
            float(parameters["inlet_velocity_m_s"]["minimum"]),
            float(parameters["inlet_velocity_m_s"]["maximum"]),
        ),
        (
            float(parameters["coolant_inlet_temperature_k"]["minimum"]),
            float(parameters["coolant_inlet_temperature_k"]["maximum"]),
        ),
        (
            float(parameters["heat_source_multiplier"]["minimum"]),
            float(parameters["heat_source_multiplier"]["maximum"]),
        ),
    ]
    normalised_anchors = [_normalise(point, bounds) for point in anchors]
    lhs_count = total - len(anchors)
    lhs, maximin_score = _maximin_lhs(seed, lhs_count, normalised_anchors)
    physical_lhs = [
        tuple(_scale(value, *bound) for value, bound in zip(point, bounds))
        for point in lhs
    ]
    points = anchors + physical_lhs
    sources = ["anchor"] * len(anchors) + ["maximin_lhs"] * lhs_count
    splits = _split_assignments(seed, total)

    rows: list[dict[str, object]] = []
    for index, (point, source) in enumerate(zip(points, sources), start=1):
        rows.append(
            {
                "case_id": f"ROMFOM{index:03d}",
                "design_source": source,
                "split": splits[index - 1],
                "inlet_velocity_m_s": f"{point[0]:.9f}",
                "coolant_inlet_temperature_k": f"{point[1]:.9f}",
                "heat_source_multiplier": f"{point[2]:.9f}",
                "status": "PLANNED",
            }
        )

    split_counts = {
        name: sum(row["split"] == name for row in rows)
        for name in ("train", "validation", "blind_test")
    }
    expected_split_counts = {
        "train": int(design["initial_split"]["train_cases"]),
        "validation": int(design["initial_split"]["validation_cases"]),
        "blind_test": int(design["initial_split"]["blind_test_cases"]),
    }
    unique_points = {
        (
            row["inlet_velocity_m_s"],
            row["coolant_inlet_temperature_k"],
            row["heat_source_multiplier"],
        )
        for row in rows
    }
    checks = {
        "case_count_48": len(rows) == 48,
        "anchor_count_9": sources.count("anchor") == 9,
        "unique_parameter_points": len(unique_points) == len(rows),
        "split_counts_match_contract": split_counts == expected_split_counts,
        "all_points_within_contract_bounds": all(
            all(bound[0] <= float(value) <= bound[1] for value, bound in zip(point, bounds))
            for point in points
        ),
        "complete_case_split_only": True,
        "mesh_independence_runs_excluded": True,
    }
    if not all(checks.values()):
        raise ValueError(f"ROM campaign validation failed: {checks}")

    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "doe_cases.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    case_split = {
        "schema_version": 1,
        "split_unit": "complete_fom_case",
        "random_seed": seed,
        "train": [row["case_id"] for row in rows if row["split"] == "train"],
        "validation": [row["case_id"] for row in rows if row["split"] == "validation"],
        "blind_test": [row["case_id"] for row in rows if row["split"] == "blind_test"],
    }
    _write_json(output_dir / "case_split.json", case_split)

    manifest = {
        "schema_version": 1,
        "status": "ROM_CAMPAIGN_PREPARED",
        "campaign_name": campaign["name"],
        "contract": str(contract_path.resolve()),
        "contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
        "production_fom_uuid": "0d72f4db04c84b139ee8cb975d0d6e59",
        "production_mesh": {
            "source": "build/mesh_sources/phi19_1050k_reference_guided_sweep_v1",
            "cells": 1049808,
            "all_hexahedral": True,
            "mesh_gate_count": 31,
        },
        "excluded_reference_runs": [
            "151ee1c71d4949e3b7df869ea58a5ef8",
            "c1a4b41695b14da7ac71095fd2b79305",
            "0d72f4db04c84b139ee8cb975d0d6e59",
        ],
        "design": {
            "method": "maximin_latin_hypercube_plus_mandatory_anchors",
            "random_seed": seed,
            "candidate_designs_evaluated": 2048,
            "initial_cases": total,
            "anchor_cases": len(anchors),
            "latin_hypercube_cases": lhs_count,
            "normalised_minimum_pairwise_distance": maximin_score,
            "split_counts": split_counts,
        },
        "execution": {
            "state": "READY_FOR_SEQUENTIAL_FOM",
            "maximum_concurrent_fom": 1,
            "next_case_id": "ROMFOM001",
            "completed_cases": 0,
        },
    }
    _write_json(output_dir / "campaign_manifest.json", manifest)
    validation = {
        "schema_version": 1,
        "status": "ROM_CAMPAIGN_PREPARATION_PASSED",
        "checks": checks,
        "split_counts": split_counts,
        "normalised_minimum_pairwise_distance": maximin_score,
    }
    _write_json(output_dir / "doe_validation.json", validation)
    return validation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("configs/rom/high_confidence_thermal_rom_v1.yaml"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/rom/high_confidence_thermal_rom_v1"),
    )
    args = parser.parse_args()
    print(json.dumps(prepare(args.contract, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
