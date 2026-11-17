import gzip
from pathlib import Path
import tempfile
import unittest

from cfd_twin.openfoam_phi_initialization import convert


class PhiInitializationTests(unittest.TestCase):
    def test_volumetric_phi_is_scaled_and_retyped_as_mass_flux(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "phi.gz"
            with gzip.open(path, "wt", encoding="utf-8") as stream:
                stream.write(
                    "FoamFile { format ascii; class surfaceScalarField; object phi; }\n"
                    "dimensions [0 3 -1 0 0 0 0];\n"
                    "internalField nonuniform List<scalar>\n2\n(\n0.1\n-0.2\n)\n;\n"
                    "boundaryField { outlet { value nonuniform List<scalar>\n1\n(\n0.3\n)\n; } }\n"
                )
            report = convert(path, 1000.0)
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                converted = stream.read()
            self.assertIn("dimensions [1 0 -1 0 0 0 0];", converted)
            self.assertIn("\n100\n", converted)
            self.assertIn("\n-200\n", converted)
            self.assertIn("\n300\n", converted)
            self.assertEqual(report["list_count"], 2)
            self.assertEqual(report["scalar_count"], 3)

    def test_refuses_wrong_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "phi"
            path.write_text(
                "dimensions [1 0 -1 0 0 0 0];\n"
                "internalField nonuniform List<scalar>\n1\n(\n1\n)\n;\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "volumetric dimensions"):
                convert(path, 998.2)


if __name__ == "__main__":
    unittest.main()
