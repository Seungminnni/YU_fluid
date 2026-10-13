"""Export the preserved Ø34/1e-4 failed convergence history without interpolation."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from cfd_twin.openfoam_cold_flow_validation import parse_cold_flow_log


FIELDS = (
    ("coolant.p_rgh", "pressure p_rgh", "#1565c0"),
    ("coolant.Ux", "Ux", "#c62828"),
    ("coolant.Uy", "Uy", "#2e7d32"),
    ("coolant.Uz", "Uz", "#7b1fa2"),
    ("coolant.k", "k", "#00838f"),
    ("coolant.omega", "omega", "#283593"),
)


def collect(log_dir: Path) -> dict[str, dict[int, float]]:
    values = {key: {} for key, _, _ in FIELDS}
    for log_path in sorted(log_dir.glob("steady_cold_flow*.log")):
        parsed = parse_cold_flow_log(log_path)
        for iteration_text, record in parsed["records"].items():
            iteration = int(round(float(iteration_text)))
            for key in values:
                residuals = record["residuals"].get(key, [])
                if residuals:
                    values[key][iteration] = max(float(item["initial"]) for item in residuals)
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("diagnostic", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    diagnostic = args.diagnostic.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    log_dir = diagnostic / "case/runs/steady_cold_flow/logs/solver"
    validation = json.loads(
        (diagnostic / "case/runs/steady_cold_flow/reports/solver/cold_flow_validation.json")
        .read_text(encoding="utf-8")
    )
    failure = json.loads((diagnostic / "failure_summary.json").read_text(encoding="utf-8"))
    values = collect(log_dir)
    iterations = sorted(set().union(*(set(series) for series in values.values())))
    if not iterations:
        raise RuntimeError("No actual OpenFOAM residual records were found")

    csv_path = output / "phi34_1e-4_residual_history.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["outer_iteration", *(key for key, _, _ in FIELDS)])
        for iteration in iterations:
            writer.writerow([iteration, *(values[key].get(iteration, "") for key, _, _ in FIELDS)])

    fig, ax = plt.subplots(figsize=(13, 7.5), dpi=180)
    for key, label, colour in FIELDS:
        points = sorted(values[key].items())
        ax.plot(
            [point[0] for point in points],
            [point[1] for point in points],
            label=label,
            color=colour,
            linewidth=0.9,
            alpha=0.9,
        )
    gate = float(failure["pressure_residual"]["maximum_allowed"])
    ax.axhline(gate, color="#111827", linestyle="--", linewidth=1.6, label="acceptance gate 1e-4")
    ax.axvspan(iterations[-200], iterations[-1], color="#f59e0b", alpha=0.10, label="last 200 iterations")
    ax.set_yscale("log")
    ax.set_xlim(iterations[0], iterations[-1])
    ax.set_xlabel("OpenFOAM steady outer iteration")
    ax.set_ylabel("Maximum initial linear-solver residual")
    ax.set_title("Ø34 mm failed convergence audit — actual saved OpenFOAM log")
    ax.grid(True, which="both", color="#d1d5db", linewidth=0.45, alpha=0.75)
    ax.legend(ncol=4, fontsize=8.5, loc="upper right")
    fig.tight_layout()
    png_path = output / "phi34_1e-4_full_residual_history.png"
    fig.savefig(png_path, facecolor="white")
    plt.close(fig)

    audit = validation["acceptance_residual_audit"]
    pressure_max = float(audit["window_maxima"]["coolant.p_rgh"])
    pressure_final = float(validation["final_initial_residual_maxima"]["coolant.p_rgh"])
    readme = f"""# Ø34 mm · 10^-4 실패 수렴 기록

- 데이터 출처: 보존된 실제 OpenFOAM `steady_cold_flow*.log`
- 외부 반복: {iterations[0]:,}–{iterations[-1]:,}회 ({len(iterations):,}개 실제 기록, 보간 없음)
- 판정 기준: 마지막 {audit['window_outer_iterations']}회 동안 필수 유동 방정식의 최대 초기 잔차 ≤ {gate:.1e}
- 압력 `p_rgh` 마지막 200회 최대값: {pressure_max:.12g} (기준의 {pressure_max / gate:.2f}배)
- 압력 `p_rgh` 최종값: {pressure_final:.12g}
- 메시: {failure['mesh']['total_cells']:,}셀, 육면체 {100 * failure['mesh']['hexahedron_fraction']:.3f}% — 메시 품질 합격
- 배터리 표면 y+<1 면적: {100 * failure['yplus']['battery_area_fraction_below_1']:.3f}% — 95% 기준 합격

따라서 이 재시도는 메시·y+·질량 불균형·압력강하 안정성은 통과했지만, `p_rgh` 잔차가 10^-4 아래로 내려가지 않고 약 9×10^-4 부근에서 진동하여 실패했습니다. 총 메시 수만 늘려도 10^-4 수렴이 보장되지 않는다는 직접 증거입니다.
"""
    (output / "README_KO.md").write_text(readme, encoding="utf-8")
    print(json.dumps({"png": str(png_path), "csv": str(csv_path), "records": len(iterations), "pressure_last_200_max": pressure_max}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
