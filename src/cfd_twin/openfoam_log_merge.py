"""Join continued OpenFOAM solver logs only when physical times are contiguous.

The solver can be safely stopped and continued from its latest time field, but
the original post-processing validators expect one chronological log. Reject
overlaps and missing one-second records instead of silently inventing them.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path


TIME_RE = re.compile(r"^Time = (\d+(?:\.\d+)?)\s*$")


def merge_solver_logs(paths: list[Path], output: Path, *, first_time: int, last_time: int) -> dict[str, object]:
    if not paths or first_time < 0 or last_time < first_time:
        raise ValueError("log paths and time range must be valid")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    expected = first_time
    counts: list[dict[str, object]] = []
    try:
        with temporary.open("w", encoding="utf-8") as merged:
            for source in paths:
                segment_start: int | None = None
                segment_last: int | None = None
                count = 0
                with source.open("r", encoding="utf-8", errors="replace") as stream:
                    for line in stream:
                        match = TIME_RE.fullmatch(line.strip())
                        if match:
                            numeric = float(match.group(1))
                            if not numeric.is_integer() or int(numeric) != expected:
                                raise ValueError(f"{source}: expected Time = {expected}, found {numeric:g}")
                            segment_start = expected if segment_start is None else segment_start
                            segment_last = expected
                            expected += 1
                            count += 1
                        merged.write(line)
                if not count or segment_last is None:
                    raise ValueError(f"{source}: no OpenFOAM time records")
                counts.append({"source": str(source), "first_time_s": segment_start, "last_time_s": segment_last, "steps": count})
        if expected != last_time + 1:
            raise ValueError(f"missing final records: expected through {last_time}, found {expected - 1}")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": "CONTIGUOUS_SOLVER_LOG_MERGED", "first_time_s": first_time,
            "last_time_s": last_time, "steps": expected - first_time,
            "output": str(output), "segments": counts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first-time", type=int, required=True)
    parser.add_argument("--last-time", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("logs", nargs="+", type=Path)
    args = parser.parse_args()
    import json

    print(json.dumps(merge_solver_logs(args.logs, args.output,
                                       first_time=args.first_time, last_time=args.last_time),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
