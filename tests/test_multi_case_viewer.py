from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from cfd_twin.cad_case_drawings import write_case_drawings
from cfd_twin.multi_case_viewer import (
    add_temperature_display_variant, assemble_four_case_viewer, combine_models, read_viewer_model,
    publish_diameter_mesh_viewer, prune_archived_diameter_results,
    refresh_diameter_comparison, synchronize_flow_legends,
    synchronize_surface_heat_transfer_legend,
)
from cfd_twin.mesh_viewer import HTML as MESH_HTML


def _geometry(diameter: int, y_offset: float = 0.0) -> dict:
    return {
        "cells": [{"id": "C05", "x": 13.6, "y": 105.2 + y_offset, "z": 41, "radius": 9.5, "length": 68}],
        "ports": [
            {"id": "IN", "x": 13.6, "y": 105.2 + y_offset, "z": 98, "radius": diameter / 2, "length": 32},
            {"id": "OUT", "x": 89.6, "y": 15.2 + y_offset, "z": -16, "radius": diameter / 2, "length": 32},
        ],
        "outline_mm": [[0, 0], [114, 0], [114, 121]],
    }


def _source_model(diameter: int, cells: int) -> dict:
    geometry = _geometry(diameter)
    run = {
        "id": "precision",
        "counts": {"total_cells": cells},
        "data_ready": True,
        "mesh_ready": True,
        "inlet_diameter_mm": diameter,
        "outlet_diameter_mm": diameter,
    }
    return {
        **geometry,
        "mesh_runs": [run],
        "interactive_cfd_by_mesh": {
            "precision": {"actual_one_second": {"datasets": {"xy": {"file": "data/precision/xy.u16"}}}}
        },
        "convergence_by_mesh": {"precision": {"available": True}},
        "energy_balance_by_mesh": {"precision": {"available": True}},
        "mesh_wireframes": {"precision": {"point_count": cells}},
    }


class MultiCaseViewerTests(unittest.TestCase):
    def test_velocity_and_pressure_legends_are_fixed_across_runs(self) -> None:
        model = {
            "mesh_runs": [
                {"id": "preserved", "asset_source_id": "preserved"},
                {"id": "direct", "asset_source_id": "direct"},
            ],
            "inputs_by_mesh": {
                "preserved": {"fluid_density_kg_m3": 1000.0},
                "direct": {"fluid_density_kg_m3": 1000.0},
            },
            "interactive_cfd_by_mesh": {
                "preserved": {
                    "sections": {"xy": {
                        "velocity_m_s": [0.1, 0.2],
                        "velocity_vectors_m_s": [[0.0, 0.0, 0.1], [0.0, 0.0, 0.2]],
                        "column_step_mm": [1.0, 0.0, 0.0],
                        "row_step_mm": [0.0, 1.0, 0.0],
                    }}
                },
                "direct": {
                    "sections": {
                        "xy": {
                            "velocity_m_s": [0.1, 0.3],
                            "velocity_vectors_m_s": [[0.0, 0.0, 0.1], [0.0, 0.0, 0.3]],
                            "column_step_mm": [1.0, 0.0, 0.0],
                            "row_step_mm": [0.0, 1.0, 0.0],
                            "pressure_gauge_pa": [-12.0, 84.0],
                        }
                    }
                },
            }
        }
        fixed = synchronize_flow_legends(model)
        self.assertEqual(fixed["velocity"], [0.0, 0.30000000000000004])
        self.assertAlmostEqual(fixed["mass_flow"][1], 0.0003)
        self.assertEqual(fixed["pressure"], [-500.0, 500.0])
        self.assertAlmostEqual(
            model["interactive_cfd_by_mesh"]["direct"]["fixed_mass_flow_legend_range_kg_s"][1],
            0.0003,
        )
        self.assertNotIn("fixed_mass_flux_legend_range_kg_m2_s", model["interactive_cfd_by_mesh"]["direct"])
        self.assertIn("99.9th-percentile", model["flow_legend_contract"]["velocity_legend_policy"])
        self.assertIn("99.9th-percentile", model["flow_legend_contract"]["mass_flow_legend_policy"])
        self.assertFalse(model["interactive_cfd_by_mesh"]["preserved"]["pressure_available"])
        self.assertTrue(model["interactive_cfd_by_mesh"]["direct"]["pressure_available"])
        self.assertEqual(
            model["interactive_cfd_by_mesh"]["direct"]["fixed_pressure_legend_range_pa"],
            [-500.0, 500.0],
        )

    def test_mass_flow_uses_normal_velocity_and_rendered_patch_area(self) -> None:
        model = {
            "inputs_by_mesh": {"case": {"fluid_density_kg_m3": 1000.0}},
            "interactive_cfd_by_mesh": {
                "case": {
                    "sections": {
                        "xy": {
                            "velocity_m_s": [10.0, 0.2],
                            "velocity_vectors_m_s": [[10.0, 0.0, 0.0], [0.0, 0.0, 0.2]],
                            "column_step_mm": [2.0, 0.0, 0.0],
                            "row_step_mm": [0.0, 3.0, 0.0],
                            "pressure_gauge_pa": [0.0, 0.0],
                        }
                    }
                }
            },
        }
        fixed = synchronize_flow_legends(model)
        # Tangential 10 m/s contributes zero. The normal sample is
        # 1000 kg/m3 * 0.2 m/s * 6e-6 m2 = 0.0012 kg/s, rounded upward.
        self.assertAlmostEqual(fixed["mass_flow"][1], 0.002)

    def test_surface_heat_transfer_legend_is_fixed_across_runs(self) -> None:
        model = {
            "interactive_cfd_by_mesh": {
                "old": {"actual_one_second": {"datasets": {}}},
                "phi32": {
                    "cell_surface_heat_transfer": {
                        "actual_dataset": "cell_surface_heat_transfer_coefficient",
                        "value_range_w_m2k": [12.0, 742.0],
                    }
                },
                "phi33": {
                    "cell_surface_heat_transfer": {
                        "actual_dataset": "cell_surface_heat_transfer_coefficient",
                        "value_range_w_m2k": [9.0, 1120.0],
                    }
                },
            }
        }
        fixed = synchronize_surface_heat_transfer_legend(model)
        self.assertEqual(fixed, [0.0, 2000.0])
        self.assertNotIn(
            "cell_surface_heat_transfer",
            model["interactive_cfd_by_mesh"]["old"],
        )
        for run_id in ("phi32", "phi33"):
            self.assertEqual(
                model["interactive_cfd_by_mesh"][run_id]
                ["cell_surface_heat_transfer"]["fixed_legend_range_w_m2k"],
                fixed,
            )

    def test_publishes_independent_mesh_assets_for_each_diameter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            viewer = root / "viewer"
            viewer.mkdir()
            (viewer / "mesh.html").write_text(MESH_HTML, encoding="utf-8")
            run_ids = ("precision", "exact_phi21", "exact_phi23", "exact_phi25")
            (viewer / "four_case_manifest.json").write_text(
                json.dumps({"runs": [{"id": run_id, "data_ready": True} for run_id in run_ids]}),
                encoding="utf-8",
            )
            sources = {}
            for run_id in run_ids:
                source = root / run_id
                source.mkdir()
                (source / "mesh-data.js").write_text(f"boundary-{run_id}", encoding="utf-8")
                (source / "mesh-volume-data.js").write_text(f"volume-{run_id}", encoding="utf-8")
                sources[run_id] = source

            publish_diameter_mesh_viewer(viewer, sources)

            html = (viewer / "mesh.html").read_text(encoding="utf-8")
            javascript = (viewer / "mesh.js").read_text(encoding="utf-8")
            self.assertIn('id="mesh-case-select"', html)
            self.assertIn('src="four_case_data.js"', html)
            self.assertNotIn('src="mesh-data.js"', html)
            self.assertIn('selectedMeshAssetId==="precision"', javascript)
            self.assertIn('`mesh-volume-data-${selectedMeshAssetId}.js`', javascript)
            for run_id in run_ids:
                self.assertEqual(
                    (viewer / f"mesh-data-{run_id}.js").read_text(encoding="utf-8"),
                    f"boundary-{run_id}",
                )
                self.assertEqual(
                    (viewer / f"mesh-volume-data-{run_id}.js").read_text(encoding="utf-8"),
                    f"volume-{run_id}",
                )

    def test_refresh_keeps_multi_case_mesh_selector(self) -> None:
        from cfd_twin.viewer import refresh_viewer_ui_assets

        with tempfile.TemporaryDirectory() as directory:
            viewer = Path(directory)
            (viewer / "results.html").write_text(
                '<script src="model.js"></script>', encoding="utf-8"
            )
            (viewer / "model.js").write_text("window.CFD_TWIN_MODEL = {};\n", encoding="utf-8")
            (viewer / "four_case_data.js").write_text("window.CFD_FOUR_CASE_MANIFEST={};\n", encoding="utf-8")
            (viewer / "four_case_manifest.json").write_text(
                json.dumps({"runs": [{"id": "precision", "data_ready": True}]}),
                encoding="utf-8",
            )
            for stem in ("mesh-data", "mesh-volume-data"):
                (viewer / f"{stem}-precision.js").write_text("mesh", encoding="utf-8")

            refresh_viewer_ui_assets(viewer)

            html = (viewer / "mesh.html").read_text(encoding="utf-8")
            self.assertIn('id="mesh-case-select"', html)
            self.assertIn('src="four_case_data.js"', html)
            self.assertNotIn('src="mesh-data.js"', html)

    def test_comparison_legend_variant_reuses_validated_water_data(self) -> None:
        model = _source_model(25, 624_788)
        model.update(actual_solver_data=True, validation_status="FROZEN_FLOW_VALIDATION_PASSED")
        model["mesh_runs"][0]["label"] = "Ø25 mm"
        model["mesh_runs"][0]["label_en"] = "Ø25 mm"
        model["mesh_runs"][0]["temperature_legend_range_c"] = [19.908, 21.044]
        add_temperature_display_variant(
            model, source_id="precision", variant_id="water_phi25_compare",
            label="Ø25 mm · 물 · 비교 범례", label_en="Ø25 mm · Water · comparison scale",
            range_c=(19.5, 23.0),
        )
        self.assertEqual([run["id"] for run in model["mesh_runs"]], ["precision", "water_phi25_compare"])
        variant = model["mesh_runs"][1]
        self.assertEqual(variant["temperature_legend_range_c"], [19.5, 23.0])
        self.assertEqual(variant["asset_source_id"], "precision")
        self.assertEqual(model["mesh_runs"][0]["temperature_legend_range_c"], [19.908, 21.044])
        self.assertEqual(
            model["interactive_cfd_by_mesh"]["water_phi25_compare"],
            model["interactive_cfd_by_mesh"]["precision"],
        )
        self.assertIsNot(
            model["interactive_cfd_by_mesh"]["water_phi25_compare"],
            model["interactive_cfd_by_mesh"]["precision"],
        )
        add_temperature_display_variant(
            model, source_id="precision", variant_id="water_phi25_compare",
            label="Ø25 mm · 물 · 비교 범례", label_en="Ø25 mm · Water · comparison scale",
            range_c=(19.5, 23.0),
        )
        self.assertEqual(len(model["mesh_runs"]), 2)

    def test_combines_four_runs_without_mutating_sources(self) -> None:
        legacy = _source_model(19, 611_313)
        legacy["mesh_runs"].append(
            {"id": "single", "counts": {"total_cells": 621_273}, "data_ready": True, "mesh_ready": True}
        )
        legacy["interactive_cfd_by_mesh"]["single"] = copy.deepcopy(
            legacy["interactive_cfd_by_mesh"]["precision"]
        )
        legacy["convergence_by_mesh"]["single"] = {"available": True}
        legacy["energy_balance_by_mesh"]["single"] = {"available": True}
        legacy["mesh_wireframes"]["single"] = {"point_count": 621_273}
        phi23 = _source_model(23, 621_948)
        phi25 = _source_model(25, 624_788)

        combined, source_ids = combine_models(legacy, phi23, phi25)

        self.assertEqual([run["id"] for run in combined["mesh_runs"]], ["precision", "single", "exact_phi23", "exact_phi25"])
        self.assertEqual([run["label"] for run in combined["mesh_runs"]], [
            "Ø19 mm · 611,313셀", "Ø23 mm · 621,273셀",
            "Ø23 mm · 621,948셀", "Ø25 mm · 624,788셀",
        ])
        self.assertEqual(source_ids, {"exact_phi23": "precision", "exact_phi25": "precision"})
        self.assertEqual(combined["geometry_by_mesh"]["single"]["ports"][0]["radius"], 11.5)
        self.assertEqual(combined["geometry_by_mesh"]["exact_phi25"]["ports"][0]["radius"], 12.5)
        self.assertIsNone(combined["spatial_transform_by_mesh"]["exact_phi23"])
        self.assertIsNone(combined["spatial_transform_by_mesh"]["exact_phi25"])
        self.assertEqual(
            combined["interactive_cfd_by_mesh"]["exact_phi23"]["actual_one_second"]["datasets"]["xy"]["file"],
            "data/exact_phi23/xy.u16",
        )
        self.assertNotIn("geometry_by_mesh", legacy)

    def test_assembles_portable_assets_and_switchable_geometry(self) -> None:
        legacy = _source_model(19, 611_313)
        legacy["mesh_runs"].append(
            {"id": "single", "counts": {"total_cells": 621_273}, "data_ready": True, "mesh_ready": True}
        )
        for key in ("interactive_cfd_by_mesh", "convergence_by_mesh", "energy_balance_by_mesh", "mesh_wireframes"):
            legacy[key]["single"] = copy.deepcopy(legacy[key]["precision"])
        sources = (legacy, _source_model(23, 621_948), _source_model(25, 624_788))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            viewers = []
            for index, model in enumerate(sources):
                viewer = root / f"viewer-{index}"
                viewer.mkdir()
                (viewer / "results.html").write_text(
                    '<script src="model.js"></script><script src="app.js"></script>',
                    encoding="utf-8",
                )
                (viewer / "model.js").write_text(
                    "window.CFD_TWIN_MODEL = " + json.dumps(model) + ";\n",
                    encoding="utf-8",
                )
                (viewer / "app.js").write_text("// fixture\n", encoding="utf-8")
                for run_id in {str(run["id"]) for run in model["mesh_runs"]}:
                    data = viewer / "data" / run_id
                    data.mkdir(parents=True)
                    (data / "xy.u16").write_bytes(bytes([index]))
                viewers.append(viewer)

            output = root / "four"
            manifest = assemble_four_case_viewer(*viewers, output)

            self.assertEqual(manifest["status"], "FOUR_CASE_VIEWER_ASSEMBLED")
            self.assertTrue((output / "data/exact_phi23/xy.u16").is_file())
            self.assertTrue((output / "data/exact_phi25/xy.u16").is_file())
            result_html = (output / "results.html").read_text(encoding="utf-8")
            self.assertIn("model.four-case-", result_html)
            self.assertIn("app.four-case-", result_html)
            self.assertIn("model.geometry_by_mesh", (output / "app.js").read_text(encoding="utf-8"))

            current = refresh_diameter_comparison(output)
            self.assertEqual(current["status"], "THREE_CASE_VIEWER_ASSEMBLED")
            self.assertEqual([run["id"] for run in current["runs"]], ["precision", "exact_phi23", "exact_phi25"])
            self.assertTrue((output / "data/single/xy.u16").is_file())
            current_model, _ = read_viewer_model(output)
            self.assertEqual([run["id"] for run in current_model["archived_mesh_runs"]], ["single"])
            self.assertIn("cad_exact_phi25_top.svg", write_case_drawings(current_model, output))
            self.assertIn("OUT Ø25", (output / "cad/cad_exact_phi25_top.svg").read_text(encoding="utf-8"))
            self.assertIn("CAD TOP", (output / "cad/cad_exact_phi25_top.en.svg").read_text(encoding="utf-8"))

            phi21_viewer = root / "viewer-21"
            phi21_viewer.mkdir()
            (phi21_viewer / "results.html").write_text('<script src="model.js"></script>', encoding="utf-8")
            (phi21_viewer / "model.js").write_text(
                "window.CFD_TWIN_MODEL = " + json.dumps(_source_model(21, 614_037)) + ";\n",
                encoding="utf-8",
            )
            (phi21_viewer / "data/precision").mkdir(parents=True)
            (phi21_viewer / "data/precision/xy.u16").write_bytes(b"21")
            updated = refresh_diameter_comparison(output, exact_phi21_viewer=phi21_viewer)
            self.assertEqual(updated["status"], "FOUR_CASE_VIEWER_ASSEMBLED")
            self.assertEqual([run["id"] for run in updated["runs"]],
                             ["precision", "exact_phi21", "exact_phi23", "exact_phi25"])
            self.assertTrue((output / "data/exact_phi21/xy.u16").is_file())

            pruned = prune_archived_diameter_results(output)
            pruned_model, _ = read_viewer_model(output)
            self.assertFalse(pruned["legacy_results_preserved"])
            self.assertNotIn("archived_mesh_runs", pruned_model)
            self.assertNotIn("single", pruned_model["interactive_cfd_by_mesh"])
            self.assertEqual(pruned_model["four_case_comparison"]["archived_run_ids"], [])


if __name__ == "__main__":
    unittest.main()
