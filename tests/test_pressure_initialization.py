import gzip
from pathlib import Path
import tempfile
import unittest

from cfd_twin.openfoam_pressure_initialization import seed


TEMPLATE = """FoamFile
{{
    format ascii;
    class volScalarField;
    object {object_name};
}}
dimensions [1 -1 -2 0 0 0 0];
internalField uniform 101325;
boundaryField
{{
    outlet {{ type fixedValue; value uniform 101325; }}
    inlet {{ type fixedFluxPressure; value uniform 101325; }}
}}
"""


class PressureInitializationTests(unittest.TestCase):
    def test_kinematic_pressure_seeds_absolute_p_and_p_rgh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            potential = root / "pPotential.gz"
            with gzip.open(potential, "wt", encoding="utf-8") as stream:
                stream.write(
                    "FoamFile { format ascii; class volScalarField; object pPotential; }\n"
                    "dimensions [0 2 -2 0 0 0 0];\n"
                    "internalField nonuniform List<scalar>\n"
                    "3\n(\n-2\n0\n3.5\n)\n;\n"
                    "boundaryField { outlet { type fixedValue; value uniform 0; } }\n"
                )
            pressure = root / "p"
            pressure_rgh = root / "p_rgh"
            pressure.write_text(TEMPLATE.format(object_name="p"), encoding="utf-8")
            pressure_rgh.write_text(
                TEMPLATE.format(object_name="p_rgh"), encoding="utf-8"
            )
            report = seed(potential, pressure, pressure_rgh, 1000.0, 101325.0)
            for path in (pressure, pressure_rgh):
                rendered = path.read_text(encoding="utf-8")
                self.assertIn("internalField   nonuniform List<scalar>", rendered)
                self.assertIn("\n99325\n", rendered)
                self.assertIn("\n101325\n", rendered)
                self.assertIn("\n104825\n", rendered)
                self.assertIn("value uniform 101325", rendered)
            self.assertEqual(report["cell_count"], 3)
            self.assertEqual(report["minimum_absolute_pressure_pa"], 99325.0)
            self.assertEqual(report["maximum_absolute_pressure_pa"], 104825.0)

    def test_refuses_non_positive_absolute_pressure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            potential = root / "pPotential"
            potential.write_text(
                "dimensions [0 2 -2 0 0 0 0];\n"
                "internalField nonuniform List<scalar>\n1\n(\n-200\n)\n;\n",
                encoding="utf-8",
            )
            pressure = root / "p"
            pressure_rgh = root / "p_rgh"
            pressure.write_text(TEMPLATE.format(object_name="p"), encoding="utf-8")
            pressure_rgh.write_text(
                TEMPLATE.format(object_name="p_rgh"), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "non-positive"):
                seed(potential, pressure, pressure_rgh, 1000.0, 101325.0)


if __name__ == "__main__":
    unittest.main()
