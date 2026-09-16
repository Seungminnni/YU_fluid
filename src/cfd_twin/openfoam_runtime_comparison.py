"""Compare equal physical-time intervals from two completed OpenFOAM logs."""

from __future__ import annotations

import json
import re
from pathlib import Path


TIME_RE = re.compile(r"^Time = ([0-9]+(?:\.[0-9]+)?)\s*$")
CLOCK_RE = re.compile(r"^ExecutionTime = .*?ClockTime = ([0-9]+(?:\.[0-9]+)?) s\s*$")


def _clock_samples(log_path: Path, required_times: set[int]) -> dict[int, float]:
    samples: dict[int, float] = {}
    current_time: int | None = None
    with log_path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            time_match = TIME_RE.match(line)
            if time_match:
                value = float(time_match.group(1))
                current_time = int(value) if value.is_integer() else None
                continue
            clock_match = CLOCK_RE.match(line)
            if clock_match and current_time in required_times:
                if current_time in samples:
                    raise ValueError(f"{log_path}: duplicate clock sample at {current_time} s")
                samples[current_time] = float(clock_match.group(1))
                current_time = None
    missing = sorted(required_times - set(samples))
    if missing:
        raise ValueError(f"{log_path}: missing ClockTime at {missing[0]} s")
    return samples


def compare_runtime_windows(
    baseline_log: str | Path,
    adaptive_log: str | Path,
    output_report: str | Path,
    *,
    windows: tuple[tuple[int, int], ...] = ((1000, 1800), (1500, 1800)),
) -> dict[str, object]:
    """Report observed wall-clock reduction; never use it as a CFD gate."""

    if not windows or any(start < 0 or end <= start for start, end in windows):
        raise ValueError("runtime windows must have increasing nonnegative times")
    times = {value for window in windows for value in window}
    baseline = _clock_samples(Path(baseline_log), times)
    adaptive = _clock_samples(Path(adaptive_log), times)
    measured: list[dict[str, float | int]] = []
    for start, end in windows:
        old = baseline[end] - baseline[start]
        new = adaptive[end] - adaptive[start]
        if old <= 0 or new <= 0:
            raise ValueError(f"nonpositive wall time for {start}–{end} s")
        measured.append({
            "from_physical_time_s": start,
            "to_physical_time_s": end,
            "baseline_wall_s": old,
            "adaptive_wall_s": new,
            "observed_wall_reduction_fraction": (old - new) / old,
            "observed_speedup_factor": old / new,
        })
    result: dict[str, object] = {
        "status": "RUNTIME_WINDOWS_COMPARED",
        "comparison_is_validation_gate": False,
        "caveat": "Historical wall-clock intervals may differ in host load and cache state.",
        "baseline_log": str(Path(baseline_log).resolve()),
        "adaptive_log": str(Path(adaptive_log).resolve()),
        "windows": measured,
    }
    output = Path(output_report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
