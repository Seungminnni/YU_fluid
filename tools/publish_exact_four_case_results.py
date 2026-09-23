#!/usr/bin/env python3
"""Publish the two exact C05/C19 runs and assemble the final four-case viewer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cfd_twin.config import load_config
from cfd_twin.multi_case_viewer import assemble_four_case_viewer
from cfd_twin.workflow_publish import publish_completed_run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--legacy-viewer", type=Path, required=True)
    parser.add_argument("--phi23-case", type=Path, required=True)
    parser.add_argument("--phi25-case", type=Path, required=True)
    parser.add_argument("--phi23-viewer", type=Path, required=True)
    parser.add_argument("--phi25-viewer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phi23-cad-renders", type=Path, required=True)
    parser.add_argument("--phi25-cad-renders", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    common = (
        root / "configs/ppt_bernardi_udf.yaml",
    )
    phi23_config = load_config(
        root / "configs/baseline_23cell.yaml",
        overlay_paths=common
        + (root / "configs/recalculation_600k_c05_inlet_c19_outlet_phi23_20260922.yaml",),
    )
    phi25_config = load_config(
        root / "configs/baseline_23cell.yaml",
        overlay_paths=common
        + (root / "configs/recalculation_600k_c05_inlet_c19_outlet_phi25_20260922.yaml",),
    )
    publish_completed_run(
        args.phi23_case,
        args.phi23_viewer,
        config=phi23_config,
        include_reference_cad=False,
    )
    publish_completed_run(
        args.phi25_case,
        args.phi25_viewer,
        config=phi25_config,
        include_reference_cad=False,
    )
    manifest = assemble_four_case_viewer(
        args.legacy_viewer,
        args.phi23_viewer,
        args.phi25_viewer,
        args.output,
        cad_render_dirs=(args.phi23_cad_renders, args.phi25_cad_renders),
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
