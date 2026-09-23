from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cfd_twin.comparison import prepare_comparison_case, publish_comparison_results
from cfd_twin.cli import build_parser
from cfd_twin.errors import CfdTwinError


def _case(root: Path, cells: int, *, results: bool = False) -> Path:
    case = root
    reports = case / "reports/production_mesh"
    reports.mkdir(parents=True)
    (reports / "production_mesh_gate.json").write_text(
        json.dumps({"status": "PRODUCTION_MESH_GATE_PASSED"}), encoding="utf-8"
    )
    (reports / "mesh_target_composition.json").write_text(
        json.dumps({
            "status": "MESH_TARGET_VALIDATION_PASSED",
            "actual": {"total_cells": cells},
        }),
        encoding="utf-8",
    )
    for name in ("constant", "runs", "results"):
        (case / name).mkdir(exist_ok=True)
    if results:
        solver = case / "runs/frozen_flow_actual_1s/reports/solver"
        solver.mkdir(parents=True)
        (solver / "frozen_flow_validation.json").write_text(
            json.dumps({"status": "FROZEN_FLOW_VALIDATION_PASSED"}), encoding="utf-8"
        )
        (solver / "energy_balance.json").write_text(
            json.dumps({"status": "TRANSIENT_ENERGY_BALANCE_PASSED"}), encoding="utf-8"
        )
    return case


class ComparisonCaseTests(unittest.TestCase):
    def test_publish_cli_accepts_ordered_repeated_overlays(self) -> None:
        args = build_parser().parse_args([
            "publish-comparison-results",
            "baseline.yaml",
            "--phi19-overlay", "physics.yaml",
            "--phi19-overlay", "phi19.yaml",
            "--phi23-overlay", "physics.yaml",
            "--phi23-overlay", "phi23.yaml",
            "--phi19-case", "case19",
            "--phi23-case", "case23",
            "--comparison-case", "comparison",
            "--viewer-output", "viewer",
        ])
        self.assertEqual(args.phi19_overlay, ["physics.yaml", "phi19.yaml"])
        self.assertEqual(args.phi23_overlay, ["physics.yaml", "phi23.yaml"])

    def test_assembles_two_independent_validated_cases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            phi19 = _case(root / "phi19", 603_634, results=True)
            phi23 = _case(root / "phi23", 611_869, results=True)
            output = root / "comparison"
            manifest = prepare_comparison_case(phi19, phi23, output, require_results=True)

            self.assertEqual(manifest["status"], "COMPARISON_RESULTS_READY")
            self.assertEqual([run["total_cells"] for run in manifest["runs"]], [603_634, 611_869])
            self.assertEqual((output / "production/L3").resolve(), phi19.resolve())
            self.assertEqual((output / "production/single").resolve(), phi23.resolve())
            self.assertEqual((output / "runs").resolve(), (phi19 / "runs").resolve())

    def test_rejects_mesh_below_hard_600k_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(CfdTwinError, "at least 600,000"):
                prepare_comparison_case(
                    _case(root / "phi19", 599_999),
                    _case(root / "phi23", 611_869),
                    root / "comparison",
                )

    def test_require_results_rejects_incomplete_case(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(CfdTwinError, "Ø19 and Ø23"):
                prepare_comparison_case(
                    _case(root / "phi19", 603_634),
                    _case(root / "phi23", 611_869),
                    root / "comparison",
                    require_results=True,
                )

    def test_publish_cross_checks_both_viewer_payloads_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            phi19 = _case(root / "phi19", 603_634, results=True)
            phi23 = _case(root / "phi23", 611_869, results=True)
            for case in (phi19, phi23):
                (case / "results/result_manifest.json").write_text(json.dumps({
                    "actual_solver_data": True,
                    "validation_status": "FROZEN_FLOW_VALIDATION_PASSED",
                }), encoding="utf-8")
            viewer = root / "viewer"

            def build(_config: object, output: Path, **_kwargs: object) -> dict[str, object]:
                for mesh_id in ("precision", "single"):
                    data = output / "data" / mesh_id
                    data.mkdir(parents=True)
                    (data / "actual_1s_validation.json").write_text("{}", encoding="utf-8")
                return {
                    "interactive_cfd_by_mesh": {
                        "precision": {"actual_one_second": {"verified": True}},
                        "single": {"actual_one_second": {"verified": True}},
                    },
                    "mesh_runs": [
                        {"id": "precision", "counts": {"total_cells": 603_634}},
                        {"id": "single", "counts": {"total_cells": 611_869}},
                    ],
                }

            with patch("cfd_twin.workflow_publish.publish_completed_run"), patch(
                "cfd_twin.viewer.write_viewer_bundle", side_effect=build,
            ):
                result = publish_comparison_results(
                    phi19,
                    phi23,
                    root / "comparison",
                    viewer,
                    phi19_config=object(),
                    phi23_config=object(),
                )
            self.assertEqual(result["comparison_status"], "COMPARISON_RESULTS_READY")
            self.assertEqual(result["mesh_counts"], {"precision": 603_634, "single": 611_869})
            self.assertEqual(
                json.loads((phi19 / "reports/workflow_postprocess.json").read_text())["stage"],
                "complete",
            )


if __name__ == "__main__":
    unittest.main()
