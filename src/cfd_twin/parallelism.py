"""Detect compute-capable CPU tiers; measured MPI counts may override them."""

from __future__ import annotations

import platform
import subprocess


def default_solver_mpi_ranks() -> int:
    """Count macOS Super + Performance cores; keep the four-rank fallback.

    M5 Pro reports Super at perflevel0 and Performance at perflevel1, so
    Both tiers contribute to parallel throughput. This is the available-core
    default, not a claim that the maximum count is fastest for every mesh.
    It does not select physical-core affinity in Docker's VM. Benchmarked
    OPENFOAM_MPI_RANKS overrides this default at execution.
    """
    if platform.system() != "Darwin":
        return 4
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "hw"], capture_output=True, text=True,
            check=True, timeout=5,
        )
        values = dict(
            line.split(": ", 1) for line in result.stdout.splitlines() if ": " in line
        )
        total = 0
        for key, name in values.items():
            if key.startswith("hw.perflevel") and key.endswith(".name") and name in {"Super", "Performance"}:
                count = int(values[key.removesuffix(".name") + ".physicalcpu"])
                if count > 0:
                    total += count
        if total:
            return total
    except (OSError, subprocess.SubprocessError, KeyError, ValueError):
        pass
    return 4
