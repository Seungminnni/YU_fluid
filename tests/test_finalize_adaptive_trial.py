from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.finalize_adaptive_trial import finalize


class AdaptiveTrialFinalizationTests(unittest.TestCase):
    def test_incomplete_trial_cannot_publish_or_modify_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trial, reference = root / "trial", root / "reference"
            original_run = reference / "runs/frozen_flow_actual_1s"
            for relative in (
                "constant/coolant/polyMesh/points",
                "constant/batterySolid/polyMesh/points",
                "constant/batterySolid/fvOptions",
            ):
                (trial / relative).parent.mkdir(parents=True, exist_ok=True)
                (original_run / relative).parent.mkdir(parents=True, exist_ok=True)
                (trial / relative).write_bytes(b"same evidence")
                (original_run / relative).write_bytes(b"same evidence")
            (trial / "0/coolant").mkdir(parents=True)
            (trial / "0/batterySolid").mkdir(parents=True)
            (trial / "0/coolant/T").write_text("internalField   uniform 293;\n", encoding="utf-8")
            (trial / "0/batterySolid/T").write_text("internalField   uniform 293.15;\n", encoding="utf-8")

            report = finalize(trial, reference, image="unused")
            self.assertEqual(report["status"], "ADAPTIVE_TRIAL_VALIDATION_FAILED")
            self.assertIn("solver_65_to_1800.log", report["error"])
            self.assertFalse((reference / "reports").exists())

    def test_reference_itself_cannot_be_used_as_trial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "separate directories"):
                finalize(root, root, image="unused")


if __name__ == "__main__":
    unittest.main()
