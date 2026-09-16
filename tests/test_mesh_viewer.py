"""The mesh-only export must not confuse sampled display edges with real cells."""

import base64
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cfd_twin.mesh_viewer import JAVASCRIPT as MESH_JS, write_mesh_only_viewer
from cfd_twin.viewer import HTML as SIMULATION_HTML, JAVASCRIPT as SIMULATION_JS


class MeshOnlyViewerTest(unittest.TestCase):
    def test_drag_does_not_recompute_fit_scale(self) -> None:
        self.assertIn("gl.uniform1f(uniforms.scale,baseScale*zoom)", MESH_JS)
        self.assertNotIn("gl.uniform1f(uniforms.scale,fitScale(aspect)*zoom)", MESH_JS)
        self.assertIn('document.getElementById("fit").onclick=fitView', MESH_JS)

    def test_simulation_does_not_draw_sampled_mesh_overlay(self) -> None:
        self.assertIn('id="show-mesh" type="checkbox" hidden', SIMULATION_HTML)
        self.assertIn('id="home-tab" href="../"', SIMULATION_HTML)
        self.assertIn('id="mesh-tab" href="mesh.html"', SIMULATION_HTML)
        self.assertNotIn("  drawActualMeshWireframe(basis,scale,cx,cy,w,h);", SIMULATION_JS)

    def test_portable_export_has_only_actual_mesh_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "case/reports/production_mesh/mesh_target_composition.json"
            report.parent.mkdir(parents=True)
            report.write_text(
                json.dumps(
                    {"actual": {"total_cells": 849284, "fluid_cells": 557402, "solid_cells": 291882}}
                ),
                encoding="utf-8",
            )
            data = {
                "sampling": "none", "point_count": 2,
                "points_mm": [[0, 0, 0], [1, 0, 0]],
                "modes": {
                    mode: {"label": mode, "groups": [{"kind": "fluid", "edge_count": 1, "edges": [[0, 1]]}]}
                    for mode in ("surface", "xy", "xz", "interior25", "interior50", "interior75")
                },
            }
            volume = {"payload": {"coolant": "ZGF0YQ==", "batterySolid": "ZGF0YQ=="},
                      "counts": {"coolant": {"points": 2, "edges": 1, "faces": 1},
                                 "batterySolid": {"points": 2, "edges": 1, "faces": 1}}}
            with patch("cfd_twin.mesh_viewer.extract_full_mesh_views", return_value=data), \
                 patch("cfd_twin.mesh_viewer.extract_complete_volume_edges", return_value=volume):
                summary = write_mesh_only_viewer(root / "case", root / "viewer")
            self.assertEqual(summary["cell_counts"]["total_cells"], 849284)
            html = (root / "viewer/mesh.html").read_text(encoding="utf-8")
            self.assertIn("가로 XY 단면", html)
            self.assertIn("세로 XZ 단면", html)
            self.assertIn("3D 내부 절개", html)
            self.assertIn("3D 전체 격자", html)
            self.assertIn('id="cut-x"', html)
            self.assertIn('id="mesh-home" href="../"', html)
            self.assertIn('id="save-png"', html)
            self.assertIn('canvas.toBlob(blob=>', (root / "viewer/mesh.js").read_text(encoding="utf-8"))
            self.assertNotIn("유선", html)
            self.assertNotIn("이동 헤드", html)
            self.assertNotIn("온도", html)
            packed = (root / "viewer/mesh-data.js").read_text(encoding="utf-8").split("=", 1)[1].rstrip(";\n")
            exported = json.loads(gzip.decompress(base64.b64decode(json.loads(packed))))
            self.assertEqual(exported["sampling"], "none")
            self.assertEqual(exported["cell_counts"]["total_cells"], 849284)
            self.assertEqual(exported["modes"]["xy"]["groups"][0]["edges"], [[0, 1]])
            self.assertEqual(exported["modes"]["interior50"]["groups"][0]["edges"], [[0, 1]])
            self.assertEqual(summary["complete_volume_edge_counts"]["coolant"]["edges"], 1)
            self.assertTrue((root / "viewer/mesh-volume-data.js").is_file())


if __name__ == "__main__":
    unittest.main()
