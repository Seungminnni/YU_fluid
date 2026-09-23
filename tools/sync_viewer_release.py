#!/usr/bin/env python3
"""Publish one validated experiment to both the local server and offline ZIP.

With no --source-viewer, rebuild the ZIP from the already published server
viewer. New experiments use --source-viewer and a unique --run-id after the
1800 s CFD result has passed its independent publication gates.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cfd_twin.viewer_release import publish_experiment_and_demo


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-viewer", type=Path, default=root / "viewer")
    parser.add_argument("--demo-zip", type=Path, default=root / "YU_fluid_데모.zip")
    parser.add_argument("--source-viewer", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--label")
    parser.add_argument("--label-en")
    args = parser.parse_args()
    result = publish_experiment_and_demo(
        args.server_viewer,
        args.demo_zip,
        source_viewer=args.source_viewer,
        run_id=args.run_id,
        label=args.label,
        label_en=args.label_en,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
