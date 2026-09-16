from __future__ import annotations

import unittest

from cfd_twin.mesh_wireframe import _sample_patch_edges


class MeshWireframeTests(unittest.TestCase):
    def test_patch_edge_sampling_is_deterministic_and_bounded(self) -> None:
        faces = [
            [index, index + 1, index + 2, index + 3]
            for index in range(0, 400, 4)
        ]
        first = _sample_patch_edges(faces, 0, len(faces), 37)
        second = _sample_patch_edges(faces, 0, len(faces), 37)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 37)
        self.assertTrue(all(a < b for a, b in first))


if __name__ == "__main__":
    unittest.main()
