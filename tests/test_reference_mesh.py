from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from cfd_twin.reference_mesh import inspect_fluent_ascii_mesh, inspect_gambit_sweep_journal


TWO_CELL_FLUENT = """(0 \"test\")
(2 3)
(10 (0 1 C 1 3))
(10 (1 1 C 1 3)(
0 0 0
1 0 0
1 1 0
0 1 0
0 0 1
1 0 1
1 1 1
0 1 1
2 0 0
2 1 0
2 0 1
2 1 1
))
(12 (0 1 2 0))
(12 (2 1 1 1 4))
(12 (3 2 2 1 4))
(13(0 1 B 0))
(13(4 1 1 3 0)(
4 2 3 7 6 1 2
))
(13(5 2 9 3 0)(
4 1 2 6 5 1 0
4 4 8 7 3 1 0
4 1 4 3 2 1 0
4 5 6 7 8 1 0
4 2 9 B 6 2 0
4 3 7 C A 2 0
4 2 3 A 9 2 0
4 6 B C 7 2 0
))
(13(6 A A 5 0)(
4 9 A C B 2 0
))
(13(7 B B A 0)(
4 1 5 8 4 1 0
))
(45 (2 solid battery)())
(45 (3 fluid fluid)())
(45 (4 wall wall)())
(45 (5 wall side_wall)())
(45 (6 pressure-outlet outlet)())
(45 (7 velocity-inlet inlet)())
"""


class ReferenceMeshTests(unittest.TestCase):
    def test_extracts_only_effective_gambit_sweep_commands(self) -> None:
        journal_text = '''volume split "volume.1" face "face.1"
face mesh "face.1" pave \\
  size 1
edge mesh "edge.1" successive ratio1 1 intervals 15
volume mesh "volume.1" cooper source "face.1"
/Undone volume mesh "volume.bad" cooper source "face.bad"
'''
        with tempfile.TemporaryDirectory(dir=Path.cwd() / "build") as directory:
            journal = Path(directory) / "reference.jou"
            journal.write_text(journal_text, encoding="ascii")
            report = inspect_gambit_sweep_journal(journal)
            self.assertEqual(report["status"], "GAMBIT_SWEEP_WORKFLOW_CONFIRMED")
            self.assertEqual(report["effective_command_counts"]["volume_mesh_cooper"], 1)
            self.assertEqual(report["axial_interval_histogram"], {"15": 1})
            self.assertEqual(report["ignored_undone_commands"], 1)

    def test_inspects_fluent_ascii_topology_and_mixed_wall(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd() / "build") as directory:
            mesh = Path(directory) / "reference.msh"
            mesh.write_text(TWO_CELL_FLUENT, encoding="ascii")
            contract = {
                "mesh_sha256": hashlib.sha256(mesh.read_bytes()).hexdigest(),
                "nodes": 12,
                "faces": 11,
                "cells": 2,
                "solid_cells": 1,
                "fluid_cells": 1,
                "solid_components": 1,
                "fluid_components": 1,
                "interface_faces_in_mixed_wall_zone": 1,
                "external_faces_in_mixed_wall_zone": 0,
                "inlet_faces": 1,
                "outlet_faces": 1,
            }
            report = inspect_fluent_ascii_mesh(mesh, contract=contract)
            self.assertEqual(report["status"], "REFERENCE_MESH_VALIDATION_PASSED")
            self.assertTrue(report["checks"]["all_faces_are_quadrilateral"])
            self.assertTrue(report["checks"]["all_cells_have_six_faces"])
            self.assertEqual(report["face_zones"]["inlet"]["area_source_units_squared"], 1.0)
            self.assertEqual(report["component_sizes"]["battery"], [1])


if __name__ == "__main__":
    unittest.main()
