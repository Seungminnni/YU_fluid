from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cfd_twin.adaptive_publish import publish_adaptive_trial


class AdaptivePublishTests(unittest.TestCase):
    def test_validated_trial_is_linked_without_copying_or_overwriting_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trial, reference = root / "trial", root / "reference"
            review, viewer = root / "review", root / "viewer"
            for path in (
                trial / "constant",
                trial / "reports/solver",
                reference / "runs/steady_cold_flow",
                reference / "runs/steady_cht",
                reference / "reports/production_mesh",
            ):
                path.mkdir(parents=True)
            (reference / "production_mesh_manifest.json").write_text("{}", encoding="utf-8")
            (trial / "reports/solver/adaptive_solver_1_to_1800.log").write_text("End\n", encoding="utf-8")
            (trial / "reports/solver/energy_coupling_audit.json").write_text(
                json.dumps({"status": "ENERGY_COUPLING_HISTORY_EXTRACTED"}), encoding="utf-8"
            )
            (trial / "reports/solver/adaptive_trial_validation.json").write_text(
                json.dumps({
                    "status": "ADAPTIVE_TRIAL_VALIDATION_PASSED",
                    "reference": str(reference.resolve()),
                    "stages": {"reference_identity": {"status": "MATCHED_MESH_AND_DYNAMIC_HEAT_SOURCE"}},
                }),
                encoding="utf-8",
            )
            with patch(
                "cfd_twin.adaptive_publish.publish_completed_run",
                return_value={"stage": "complete"},
            ) as publisher:
                result = publish_adaptive_trial(trial, reference, review, viewer)
                publish_adaptive_trial(trial, reference, review, viewer)
            self.assertEqual(publisher.call_count, 2)
            self.assertEqual(result["execution_variant"], "adaptive_energy_coupling_trial")
            self.assertEqual((review / "runs/frozen_flow_actual_1s").resolve(), trial.resolve())
            self.assertEqual((review / "runs/steady_cht").resolve(), (reference / "runs/steady_cht").resolve())
            self.assertTrue((trial / "frozen_flow_actual_1s.foam").is_file())
            self.assertFalse((reference / "runs/steady_cht/frozen_flow_actual_1s.foam").exists())

    def test_unvalidated_trial_is_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trial, reference = root / "trial", root / "reference"
            path = trial / "reports/solver/adaptive_trial_validation.json"
            path.parent.mkdir(parents=True)
            reference.mkdir()
            path.write_text(json.dumps({"status": "ADAPTIVE_TRIAL_VALIDATION_FAILED"}), encoding="utf-8")
            with patch("cfd_twin.adaptive_publish.publish_completed_run") as publisher:
                with self.assertRaisesRegex(ValueError, "validation"):
                    publish_adaptive_trial(trial, reference, root / "review", root / "viewer")
            publisher.assert_not_called()


if __name__ == "__main__":
    unittest.main()
