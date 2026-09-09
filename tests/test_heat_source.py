from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cfd_twin.config import load_config
from cfd_twin.errors import ProfileError
from cfd_twin.heat_source import (
    ConstantVolumetricSource,
    ElectricalJouleSource,
    LinearSeries,
    TableVolumetricSource,
    render_fluent_udf,
    render_openfoam_fvoptions,
    source_from_config,
    write_heat_source_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "configs" / "baseline_23cell.yaml"


class HeatSourceTests(unittest.TestCase):
    def test_linear_table_interpolation_and_hold_policy(self) -> None:
        series = LinearSeries((0.0, 10.0), (100.0, 300.0), "qvol_W_m3", "memory")
        source = TableVolumetricSource(series)
        self.assertEqual(source.qvol(5.0), 200.0)
        self.assertEqual(source.qvol(20.0), 300.0)

    def test_electrical_joule_formula(self) -> None:
        current = LinearSeries((0.0, 10.0), (4.0, 6.0), "current_A", "current")
        resistance = LinearSeries((0.0, 10.0), (0.02, 0.03), "resistance_ohm", "resistance")
        source = ElectricalJouleSource(current, resistance, 2.0e-5)
        self.assertAlmostEqual(source.qvol(0.0), 16000.0)
        self.assertAlmostEqual(source.qvol(5.0), 31250.0)

    def test_out_of_range_error_policy(self) -> None:
        series = LinearSeries((1.0, 2.0), (10.0, 20.0), "qvol_W_m3", "memory", "error")
        with self.assertRaises(ProfileError):
            series.at(0.0)

    def test_malformed_csv_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            path.write_text("time_s,qvol_W_m3\n1,10\n1,20\n", encoding="utf-8")
            with self.assertRaisesRegex(ProfileError, "strictly increasing"):
                LinearSeries.from_csv(path, value_column="qvol_W_m3")

    def test_constant_outputs_match_paper_baseline(self) -> None:
        source = ConstantVolumetricSource(16325.0)
        fluent = render_fluent_udf(source)
        openfoam = render_openfoam_fvoptions(source, "h")
        self.assertIn("DEFINE_SOURCE(battery_heat_source", fluent)
        self.assertIn("return 16325", fluent)
        self.assertIn("type            scalarSemiImplicitSource;", openfoam)
        self.assertIn("volumeMode      specific;", openfoam)
        self.assertIn("h            (16325 0);", openfoam)

    def test_bundle_marks_original_udf_unavailable(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_heat_source_bundle(config, directory)
            self.assertFalse(manifest["original_paper_udf_available"])
            self.assertEqual(manifest["qvol_W_m3"], 16325.0)

    def test_dynamic_overlay_loads_profiles_relative_to_baseline(self) -> None:
        config = load_config(
            BASELINE,
            overlay_paths=(ROOT / "configs" / "electrical_joule_example.yaml",),
        )
        source = source_from_config(config)
        self.assertEqual(source.mode, "electrical_joule")
        self.assertGreater(source.qvol(0.0), 0.0)
        self.assertEqual(source.manifest()["paper_equivalence"], "joule_surrogate_not_original_udf")


if __name__ == "__main__":
    unittest.main()
