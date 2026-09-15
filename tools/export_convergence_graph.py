#!/usr/bin/env python3
"""Export saved OpenFOAM convergence histories as presentation-ready PNG/CSV."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from cfd_twin.viewer import _convergence_payload


FONT_PATH = Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf")


def _write_csv(history: dict[str, object], destination: Path) -> None:
    series = history["series"]
    iterations = sorted(
        {
            int(point[0])
            for line in series
            for point in line["points"]
        }
    )
    values = {
        line["key"]: {int(point[0]): float(point[1]) for point in line["points"]}
        for line in series
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        keys = [line["key"] for line in series]
        writer.writerow(["openfoam_outer_iteration", *keys])
        for iteration in iterations:
            writer.writerow(
                [iteration, *(values[key].get(iteration, "") for key in keys)]
            )


def _write_png(history: dict[str, object], destination: Path) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties

    font = FontProperties(fname=str(FONT_PATH)) if FONT_PATH.is_file() else None
    figure, axis = plt.subplots(figsize=(13.333, 7.5), dpi=180)
    figure.patch.set_facecolor("white")
    axis.set_facecolor("#f7fafc")
    for line in history["series"]:
        points = line["points"]
        axis.semilogy(
            [point[0] for point in points],
            [point[1] for point in points],
            color=line["colour"],
            linewidth=1.45,
            label=line["label"],
        )
    axis.set_title(
        "초기 냉간 유동 수렴 이력 · 849,284셀",
        fontproperties=font,
        fontsize=19,
        fontweight="bold",
        pad=16,
    )
    axis.set_xlabel(
        "OpenFOAM 외부 반복 횟수",
        fontproperties=font,
        fontsize=13,
        labelpad=10,
    )
    axis.set_ylabel("Initial residual / |global continuity|", fontsize=12)
    axis.grid(True, which="major", color="#cad7df", linewidth=0.7)
    axis.grid(True, which="minor", color="#e4ebef", linewidth=0.4)
    legend = axis.legend(
        loc="upper right",
        ncol=2,
        frameon=True,
        prop=font,
        fontsize=10,
    )
    legend.get_frame().set_facecolor("white")
    legend.get_frame().set_edgecolor("#bccbd3")
    axis.tick_params(colors="#344955")
    for spine in axis.spines.values():
        spine.set_color("#8ca0ab")
    figure.text(
        0.01,
        0.01,
        "Actual OpenFOAM raw initial residuals · no reference-line overlay",
        color="#5a6f7a",
        fontsize=9,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(destination, bbox_inches="tight", facecolor=figure.get_facecolor())
    plt.close(figure)


def export(case_dir: Path, output_dir: Path) -> dict[str, object]:
    convergence = _convergence_payload(case_dir)
    histories = {item["id"]: item for item in convergence.get("histories", [])}
    cold_flow = histories.get("cold_flow")
    if not cold_flow:
        raise ValueError(f"cold-flow convergence history not found under {case_dir}")
    png = output_dir / "initial_cold_flow_residuals_849284.png"
    csv_path = output_dir / "initial_cold_flow_residuals_849284.csv"
    _write_png(cold_flow, png)
    _write_csv(cold_flow, csv_path)
    manifest = {
        "status": "INITIAL_COLD_FLOW_CONVERGENCE_EXPORTED",
        "source": cold_flow["source_files"],
        "scope": cold_flow["scope"],
        "iteration_count": cold_flow["iteration_count"],
        "first_iteration": cold_flow["first_iteration"],
        "last_iteration": cold_flow["last_iteration"],
        "series": [line["key"] for line in cold_flow["series"]],
        "png": str(png.resolve()),
        "csv": str(csv_path.resolve()),
    }
    manifest_path = output_dir / "initial_cold_flow_residuals_849284.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.case_dir, args.output_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
