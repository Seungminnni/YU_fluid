#!/usr/bin/env python3
"""Serve the current cumulative viewer without depending on archived CFD cases."""

from __future__ import annotations

import argparse
from pathlib import Path

from cfd_twin.viewer_release import validate_server_viewer
from cfd_twin.workflow_app import baseline_geometry, serve_workflow


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    viewer = root / "viewer"
    cad = root / "src/cfd_twin/assets/verified_cad_phi23"
    release = validate_server_viewer(viewer)
    if not baseline_geometry(cad)["verified"]:
        raise ValueError(f"independent reference CAD failed verification: {cad}")
    print(f"Serving {len(release['runs'])} published CFD results from {viewer}", flush=True)
    serve_workflow(
        case_dir=root / "build/published_viewer_only",
        cad_dir=cad,
        viewer_dir=viewer,
        imported_root=root / "build/imported_runs",
        port=args.port,
    )


if __name__ == "__main__":
    main()
