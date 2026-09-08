from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mdpi_cfd.animation_validation import validate_animations
from mdpi_cfd.errors import MdpiCfdError


class AnimationValidationTests(unittest.TestCase):
    def test_missing_animation_is_never_reported_as_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            results = case / "results"
            results.mkdir()
            (results / "result_manifest.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(MdpiCfdError, "animation is missing"):
                validate_animations(case, viewer_dir=case / "viewer")


if __name__ == "__main__":
    unittest.main()
