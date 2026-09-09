from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path


NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"


def _numbers(patterns: tuple[str, ...], text: str) -> list[float]:
    matches: list[float] = []
    for pattern in patterns:
        matches.extend(
            float(match) for match in re.findall(pattern, text, flags=re.IGNORECASE)
        )
    return matches


def _quality_error_sections(text: str) -> list[list[int]]:
    sections: list[list[int]] = []
    pattern = re.compile(
        r"Checking faces in error\s*:\s*\n((?:[^\n]*:\s*\d+\s*\n)+)",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        counts = [
            int(value)
            for value in re.findall(r":\s*(\d+)\s*$", match.group(1), flags=re.MULTILINE)
        ]
        if counts:
            sections.append(counts)
    return sections


def parse_checkmesh_log(
    path: str | Path,
    *,
    expected_mesh_ok_count: int = 1,
    max_non_orthogonality: float = 75.0,
    max_skewness: float = 20.0,
) -> dict[str, object]:
    source = Path(path).resolve()
    text = source.read_text(encoding="utf-8", errors="replace")
    mesh_ok_count = len(re.findall(r"\bMesh OK\.", text))
    fatal_lines = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"FOAM FATAL|segmentation fault", line, re.I)
        or (
            re.search(r"floating point exception", line, re.I)
            and not re.search(r"trapping enabled", line, re.I)
        )
    ]
    failed_lines = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"\*\*\*.*Failed|Failed \d+ mesh checks", line, re.I)
    ]
    non_ortho_values = _numbers(
        (
            rf"Max\s+non-orthogonality\s*=\s*({NUMBER})",
            rf"Mesh\s+non-orthogonality\s+Max\s*:\s*({NUMBER})",
        ),
        text,
    )
    non_ortho = max(non_ortho_values, default=None)
    skewness_values = _numbers((rf"Max\s+skewness\s*=\s*({NUMBER})",), text)
    skewness = max(skewness_values, default=None)
    min_volume_values = _numbers(
        (
            rf"Min(?:imum)?\s+(?:cell\s+)?volume\s*=\s*({NUMBER})",
            rf"minVol\s*[:=]\s*({NUMBER})",
        ),
        text,
    )
    min_volume = min(min_volume_values, default=None)
    min_determinant_values = _numbers(
        (
            rf"Min(?:imum)?\s+(?:cell\s+)?determinant\s*=\s*({NUMBER})",
            rf"minDeterminant\s*[:=]\s*({NUMBER})",
        ),
        text,
    )
    min_determinant = min(min_determinant_values, default=None)
    illegal_face_values = _numbers(
        (rf"Number of illegal faces\s*[:=]\s*({NUMBER})",), text
    )
    illegal_faces = max(illegal_face_values, default=None)
    quality_sections = _quality_error_sections(text)
    quality_pass_count = sum(
        len(counts) >= 8 and all(value == 0 for value in counts)
        for counts in quality_sections
    )
    user_quality_override = quality_pass_count >= expected_mesh_ok_count
    negative_volume_mentions = []
    for line in text.splitlines():
        if not re.search(r"negative volume|negative-volume", line, re.I):
            continue
        if re.search(r"\b(?:no|zero|0)\s+negative", line, re.I):
            continue
        negative_volume_mentions.append(line.strip())
    checks = {
        "expected_mesh_ok_count": (
            mesh_ok_count >= expected_mesh_ok_count or user_quality_override
        ),
        "no_fatal_error": not fatal_lines,
        "no_failed_check_lines": not failed_lines or user_quality_override,
        "minimum_volume_positive": min_volume is not None and min_volume > 0,
        "max_non_orthogonality_within_limit": (
            non_ortho is not None and non_ortho <= max_non_orthogonality
        ),
        "max_skewness_within_limit": (
            skewness is not None and skewness <= max_skewness
        ),
        "minimum_determinant_positive_if_reported": (
            min_determinant is None or min_determinant > 0
        ),
        "illegal_faces_zero_if_reported": illegal_faces is None or illegal_faces == 0,
        "no_negative_volume_diagnostic": not negative_volume_mentions,
    }
    return {
        "schema_version": 1,
        "status": "CHECKMESH_LOG_PASSED" if all(checks.values()) else "CHECKMESH_LOG_FAILED",
        "log": str(source),
        "checks": checks,
        "limits": {
            "expected_mesh_ok_count": expected_mesh_ok_count,
            "max_non_orthogonality": max_non_orthogonality,
            "max_skewness": max_skewness,
        },
        "metrics": {
            "mesh_ok_count": mesh_ok_count,
            "max_non_orthogonality": non_ortho,
            "max_skewness": skewness,
            "minimum_cell_volume_m3": min_volume,
            "minimum_determinant": min_determinant,
            "illegal_faces": None if illegal_faces is None else int(illegal_faces),
            "user_quality_pass_count": quality_pass_count,
        },
        "fatal_lines": fatal_lines[:20],
        "failed_lines": failed_lines[:20],
        "negative_volume_lines": negative_volume_mentions[:20],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse and gate an OpenFOAM checkMesh log")
    parser.add_argument("--log", required=True)
    parser.add_argument("--output")
    parser.add_argument("--expected-mesh-ok-count", type=int, default=1)
    parser.add_argument("--max-non-orthogonality", type=float, default=75.0)
    parser.add_argument("--max-skewness", type=float, default=20.0)
    args = parser.parse_args(argv)
    try:
        if args.expected_mesh_ok_count < 1:
            raise ValueError("expected mesh OK count must be positive")
        for value in (args.max_non_orthogonality, args.max_skewness):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("quality limits must be finite and positive")
        report = parse_checkmesh_log(
            args.log,
            expected_mesh_ok_count=args.expected_mesh_ok_count,
            max_non_orthogonality=args.max_non_orthogonality,
            max_skewness=args.max_skewness,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        report = {
            "schema_version": 1,
            "status": "CHECKMESH_LOG_FAILED",
            "log": str(Path(args.log).resolve()),
            "error": str(exc),
        }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "CHECKMESH_LOG_PASSED" else 2


if __name__ == "__main__":
    sys.exit(main())
