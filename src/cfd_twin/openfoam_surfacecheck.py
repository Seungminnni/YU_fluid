from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def parse_surfacecheck_log(path: str | Path) -> dict[str, object]:
    source = Path(path).resolve()
    text = source.read_text(encoding="utf-8", errors="replace")
    closed = bool(re.search(r"Surface is closed", text, re.IGNORECASE))
    fatal_lines = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"FOAM FATAL|segmentation fault", line, re.I)
        or (
            re.search(r"floating point exception", line, re.I)
            and not re.search(r"trapping enabled", line, re.I)
        )
    ]
    illegal_lines = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"illegal triangle|degenerate triangle|self-intersection", line, re.I)
        and not re.search(r"\b(?:none|no|zero|0)\b", line, re.I)
    ]
    regions_match = re.findall(
        r"Number of unconnected parts\s*:\s*(\d+)", text, flags=re.IGNORECASE
    )
    unconnected_parts = int(regions_match[-1]) if regions_match else None
    checks = {
        "surface_closed": closed,
        "no_fatal_error": not fatal_lines,
        "no_illegal_surface_diagnostic": not illegal_lines,
        "single_connected_part_if_reported": (
            unconnected_parts is None or unconnected_parts == 1
        ),
    }
    return {
        "schema_version": 1,
        "status": "SURFACECHECK_LOG_PASSED" if all(checks.values()) else "SURFACECHECK_LOG_FAILED",
        "log": str(source),
        "checks": checks,
        "metrics": {"unconnected_parts": unconnected_parts},
        "fatal_lines": fatal_lines[:20],
        "illegal_lines": illegal_lines[:20],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Require a closed OpenFOAM triSurface")
    parser.add_argument("--log", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        report = parse_surfacecheck_log(args.log)
    except (OSError, UnicodeError, ValueError) as exc:
        report = {
            "schema_version": 1,
            "status": "SURFACECHECK_LOG_FAILED",
            "log": str(Path(args.log).resolve()),
            "error": str(exc),
        }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "SURFACECHECK_LOG_PASSED" else 2


if __name__ == "__main__":
    sys.exit(main())
