from __future__ import annotations

import base64
import gzip
import struct
import unittest
from unittest.mock import patch

from cfd_twin.mesh_wireframe import _sample_patch_edges, extract_complete_volume_edges


class MeshWireframeTests(unittest.TestCase):
    def test_complete_volume_contains_all_unique_edges_without_sampling(self) -> None:
        points = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
        faces = [[0, 1, 2], [0, 2, 3]]
        with patch("cfd_twin.mesh_wireframe._points", return_value=points), \
             patch("cfd_twin.mesh_wireframe._faces", return_value=faces):
            result = extract_complete_volume_edges("/unused/case")
        self.assertEqual(result["sampling"], "none")
        expected = {(0, 1), (0, 2), (0, 3), (1, 2), (2, 3)}
        for region in ("coolant", "batterySolid"):
            self.assertEqual(result["counts"][region], {"points": 4, "edges": 5, "faces": 2})
            raw = gzip.decompress(base64.b64decode(result["payload"][region]))
            self.assertEqual(struct.unpack_from("<III", raw), (4, 5, 2))
            endpoints = struct.unpack_from("<10I", raw, 12 + 4 * 12)
            self.assertEqual(set(zip(endpoints[::2], endpoints[1::2])), expected)

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
