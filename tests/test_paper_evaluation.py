from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mdpi_cfd.config import load_config
from mdpi_cfd.errors import MdpiCfdError
from mdpi_cfd.paper_evaluation import evaluate_paper_alignment


ROOT = Path(__file__).resolve().parents[1]


class PaperEvaluationTests(unittest.TestCase):
    def test_mesh_independence_must_pass_before_evaluation(self) -> None:
        config = load_config(ROOT / "configs/baseline_23cell.yaml")
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            results = case / "results"
            results.mkdir()
            (results / "mesh_independence.json").write_text(
                '{"status":"MESH_INDEPENDENCE_PENDING"}', encoding="utf-8"
            )
            (results / "animation_validation.json").write_text(
                '{"status":"ANIMATION_VALIDATION_PASSED"}', encoding="utf-8"
            )
            with self.assertRaisesRegex(MdpiCfdError, "mesh independence must pass"):
                evaluate_paper_alignment(config, case)


if __name__ == "__main__":
    unittest.main()
