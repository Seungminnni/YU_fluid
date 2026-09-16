from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cfd_twin.errors import CfdTwinError
from cfd_twin.openfoam_results import read_openfoam_dat, read_openfoam_dat_series


class OpenFoamResultTests(unittest.TestCase):
    def test_reads_comment_prefixed_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "surfaceFieldValue.dat"
            path.write_text(
                "# Time areaAverage(T)\n1 2.93001e2\n2\t2.93002e2\n",
                encoding="utf-8",
            )
            self.assertEqual(
                read_openfoam_dat(path),
                [(1.0, 293.001), (2.0, 293.002)],
            )

    def test_rejects_non_numeric_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.dat"
            path.write_text("# header\n1 not-a-number\n", encoding="utf-8")
            with self.assertRaises(CfdTwinError):
                read_openfoam_dat(path)

    def test_merges_restart_segments_and_rejects_conflicting_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "Tavg" / "0" / "surfaceFieldValue.dat"
            second = root / "Tavg" / "2" / "surfaceFieldValue.dat"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_text("# Time T\n1 293.1\n2 293.2\n", encoding="ascii")
            second.write_text("# Time T\n2 293.2\n3 293.3\n", encoding="ascii")
            self.assertEqual(
                read_openfoam_dat_series(first),
                [(1.0, 293.1), (2.0, 293.2), (3.0, 293.3)],
            )
            second.write_text("# Time T\n2 293.4\n3 293.3\n", encoding="ascii")
            with self.assertRaisesRegex(CfdTwinError, "conflicting restart row"):
                read_openfoam_dat_series(first)


if __name__ == "__main__":
    unittest.main()
