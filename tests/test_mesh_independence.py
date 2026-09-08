from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mdpi_cfd.mesh_independence import evaluate_mesh_independence


class MeshIndependenceTests(unittest.TestCase):
    def test_incomplete_study_stays_pending_without_publication_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            report = evaluate_mesh_independence(case)
            self.assertEqual(report["status"], "MESH_INDEPENDENCE_PENDING")
            self.assertEqual(report["completed_levels"], [])
            self.assertFalse(report["publication_results_authorized"])
            self.assertTrue((case / "results/mesh_independence.json").is_file())


if __name__ == "__main__":
    unittest.main()
