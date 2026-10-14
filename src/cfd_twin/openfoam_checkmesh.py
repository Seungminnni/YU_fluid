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


def _skewness_error_rows(text: str) -> list[dict[str, float | int]]:
    """Extract separate internal/boundary limits from quality tables.

    OpenFOAM prints one combined maximum skewness value.  The detailed table
    carries the two applied limits, so retain those without mislabelling the
    combined maximum as an internal-face maximum.
    """

    rows: list[dict[str, float | int]] = []
    pattern = re.compile(
        rf"faces\s+with\s+skewness\s*>\s*({NUMBER})\s*"
        rf"\(internal\)\s*or\s*({NUMBER})\s*\(boundary\)\s*:\s*(\d+)",
        flags=re.IGNORECASE,
    )
    for internal, boundary, count in pattern.findall(text):
        rows.append(
            {
                "internal_limit": float(internal),
                "boundary_limit": float(boundary),
                "offending_face_count": int(count),
            }
        )
    return rows


def _face_set_labels(path: Path) -> list[int]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    matches = re.findall(r"(?s)\b\d+\s*\(\s*([\d\s]+?)\s*\)", text)
    if not matches:
        return []
    return [int(value) for value in re.findall(r"\d+", matches[-1])]


def _boundary_ranges(path: Path) -> list[dict[str, int | str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    result: list[dict[str, int | str]] = []
    for name, body in re.findall(r"(?ms)^\s*([\w.-]+)\s*\{(.*?)^\s*\}", text):
        count = re.search(r"\bnFaces\s+(\d+)\s*;", body)
        start = re.search(r"\bstartFace\s+(\d+)\s*;", body)
        if count and start:
            result.append(
                {"patch": name, "start": int(start.group(1)), "count": int(count.group(1))}
            )
    return result


def _audit_face_sets(mesh_dirs: tuple[str | Path, ...]) -> dict[str, object]:
    regions: dict[str, object] = {}
    all_skew_scopes: list[str] = []
    for raw in mesh_dirs:
        mesh_dir = Path(raw).resolve()
        if not (mesh_dir / "boundary").is_file():
            continue
        ranges = _boundary_ranges(mesh_dir / "boundary")
        if not ranges:
            continue
        first_boundary = min(int(item["start"]) for item in ranges)

        def classify(labels: list[int]) -> dict[str, object]:
            by_patch: dict[str, int] = {}
            internal = 0
            unmatched = 0
            for label in labels:
                if label < first_boundary:
                    internal += 1
                    continue
                matched = False
                for item in ranges:
                    start = int(item["start"])
                    if start <= label < start + int(item["count"]):
                        patch = str(item["patch"])
                        by_patch[patch] = by_patch.get(patch, 0) + 1
                        matched = True
                        break
                if not matched:
                    unmatched += 1
            return {
                "total": len(labels),
                "internal": internal,
                "boundary": sum(by_patch.values()),
                "unmatched": unmatched,
                "boundary_patches": dict(sorted(by_patch.items())),
            }

        skew = classify(_face_set_labels(mesh_dir / "sets" / "skewFaces"))
        quality = classify(_face_set_labels(mesh_dir / "sets" / "meshQualityFaces"))
        if int(skew["internal"]):
            all_skew_scopes.append("internal")
        if int(skew["boundary"]):
            all_skew_scopes.append("boundary")
        regions[mesh_dir.parent.name] = {
            "mesh_dir": str(mesh_dir),
            "first_boundary_face": first_boundary,
            "skew_faces": skew,
            "mesh_quality_faces": quality,
        }
    if not all_skew_scopes:
        scope = "not_available"
    elif set(all_skew_scopes) == {"internal"}:
        scope = "internal"
    elif set(all_skew_scopes) == {"boundary"}:
        scope = "boundary"
    else:
        scope = "mixed_internal_and_boundary"
    return {"max_skewness_scope_inferred_from_skew_face_sets": scope, "regions": regions}


def parse_checkmesh_log(
    path: str | Path,
    *,
    expected_mesh_ok_count: int = 1,
    max_non_orthogonality: float = 75.0,
    max_skewness: float = 20.0,
    max_internal_skewness: float = 4.0,
    max_boundary_skewness: float | None = None,
    mesh_dirs: tuple[str | Path, ...] = (),
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
    skewness_error_rows = _skewness_error_rows(text)
    observed_internal_limits = sorted(
        {float(row["internal_limit"]) for row in skewness_error_rows}
    )
    observed_boundary_limits = sorted(
        {float(row["boundary_limit"]) for row in skewness_error_rows}
    )
    high_skew_face_count = sum(
        int(row["offending_face_count"]) for row in skewness_error_rows
    )
    boundary_limit = (
        max_skewness if max_boundary_skewness is None else max_boundary_skewness
    )
    face_set_audit = _audit_face_sets(mesh_dirs)
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
        "observed_internal_skewness_limit_not_looser": (
            not observed_internal_limits
            or max(observed_internal_limits) <= max_internal_skewness
        ),
        "observed_boundary_skewness_limit_not_looser": (
            not observed_boundary_limits
            or max(observed_boundary_limits) <= boundary_limit
        ),
        "minimum_determinant_positive_if_reported": (
            min_determinant is None or min_determinant > 0
        ),
        "illegal_faces_zero_if_reported": illegal_faces is None or illegal_faces == 0,
        "no_negative_volume_diagnostic": not negative_volume_mentions,
    }
    return {
        "schema_version": 2,
        "status": "CHECKMESH_LOG_PASSED" if all(checks.values()) else "CHECKMESH_LOG_FAILED",
        "log": str(source),
        "checks": checks,
        "limits": {
            "expected_mesh_ok_count": expected_mesh_ok_count,
            "max_non_orthogonality": max_non_orthogonality,
            "max_skewness": max_skewness,
            "max_internal_skewness": max_internal_skewness,
            "max_boundary_skewness": boundary_limit,
        },
        "metrics": {
            "mesh_ok_count": mesh_ok_count,
            "max_non_orthogonality": non_ortho,
            "max_skewness": skewness,
            "max_skewness_scope": (
                face_set_audit["max_skewness_scope_inferred_from_skew_face_sets"]
                if face_set_audit["max_skewness_scope_inferred_from_skew_face_sets"]
                != "not_available"
                else "combined_internal_and_boundary; no face-set scope was available"
            ),
            "observed_internal_skewness_limits": observed_internal_limits,
            "observed_boundary_skewness_limits": observed_boundary_limits,
            "high_skew_face_count_from_quality_tables": high_skew_face_count,
            "skewness_quality_rows": skewness_error_rows,
            "minimum_cell_volume_m3": min_volume,
            "minimum_determinant": min_determinant,
            "illegal_faces": None if illegal_faces is None else int(illegal_faces),
            "user_quality_pass_count": quality_pass_count,
        },
        "fatal_lines": fatal_lines[:20],
        "failed_lines": failed_lines[:20],
        "negative_volume_lines": negative_volume_mentions[:20],
        "face_set_audit": face_set_audit,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse and gate an OpenFOAM checkMesh log")
    parser.add_argument("--log", required=True)
    parser.add_argument("--output")
    parser.add_argument("--expected-mesh-ok-count", type=int, default=1)
    parser.add_argument("--max-non-orthogonality", type=float, default=75.0)
    parser.add_argument("--max-skewness", type=float, default=20.0)
    parser.add_argument("--max-internal-skewness", type=float, default=4.0)
    parser.add_argument("--max-boundary-skewness", type=float)
    parser.add_argument("--mesh-dir", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.expected_mesh_ok_count < 1:
            raise ValueError("expected mesh OK count must be positive")
        for value in (
            args.max_non_orthogonality,
            args.max_skewness,
            args.max_internal_skewness,
            args.max_boundary_skewness,
        ):
            if value is None:
                continue
            if not math.isfinite(value) or value <= 0:
                raise ValueError("quality limits must be finite and positive")
        report = parse_checkmesh_log(
            args.log,
            expected_mesh_ok_count=args.expected_mesh_ok_count,
            max_non_orthogonality=args.max_non_orthogonality,
            max_skewness=args.max_skewness,
            max_internal_skewness=args.max_internal_skewness,
            max_boundary_skewness=args.max_boundary_skewness,
            mesh_dirs=tuple(args.mesh_dir),
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
