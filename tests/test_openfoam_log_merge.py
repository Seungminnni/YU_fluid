from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cfd_twin.openfoam_log_merge import merge_solver_logs


class SolverLogMergeTests(unittest.TestCase):
    def test_contiguous_segments_keep_all_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, output = (root / name for name in ("first.log", "second.log", "joined.log"))
            first.write_text("start\nTime = 1\na\nTime = 2\nb\n", encoding="utf-8")
            second.write_text("restart\nTime = 3\nc\nTime = 4\nEnd\n", encoding="utf-8")
            result = merge_solver_logs([first, second], output, first_time=1, last_time=4)
            self.assertEqual(result["steps"], 4)
            self.assertEqual(result["segments"][1]["first_time_s"], 3)
            self.assertIn("Time = 4\nEnd\n", output.read_text(encoding="utf-8"))

    def test_gap_or_overlap_cannot_replace_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, output = (root / name for name in ("first.log", "second.log", "joined.log"))
            first.write_text("Time = 1\n", encoding="utf-8")
            output.write_text("previous valid result\n", encoding="utf-8")
            for invalid_time in (1, 3):
                second.write_text(f"Time = {invalid_time}\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "expected Time = 2"):
                    merge_solver_logs([first, second], output, first_time=1, last_time=2)
                self.assertEqual(output.read_text(encoding="utf-8"), "previous valid result\n")
                self.assertFalse((root / "joined.log.tmp").exists())


if __name__ == "__main__":
    unittest.main()
