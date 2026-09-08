from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mdpi_cfd.errors import MdpiCfdError
from mdpi_cfd.postprocess import read_openfoam_dat


class PostprocessTests(unittest.TestCase):
    def test_reads_comment_prefixed_openfoam_table(self) -> None:
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

    def test_rejects_non_numeric_table_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.dat"
            path.write_text("# header\n1 not-a-number\n", encoding="utf-8")
            with self.assertRaises(MdpiCfdError):
                read_openfoam_dat(path)


if __name__ == "__main__":
    unittest.main()
