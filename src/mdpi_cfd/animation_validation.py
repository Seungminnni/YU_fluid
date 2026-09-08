from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import MdpiCfdError


EXPECTED = {
    "thermal_transient": {
        "filename": "actual_thermal_xy_0_1800s.mp4",
        "frames": 121,
        "fps": 12.0,
        "semantics": (
            "Figure 6 actual coolant T(x,t) every 15 s with fixed U(x); "
            "smooth point interpolation"
        ),
    },
    "frozen_flow_particles": {
        "filename": "frozen_flow_particles_fixed_U.mp4",
        "frames": 90,
        "fps": 20.0,
        "semantics": (
            "moving comet heads along inlet-fed streamlines in fixed U(x); "
            "not transient velocity"
        ),
    },
}


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _run_json(command: list[str]) -> dict[str, object]:
    completed = subprocess.run(command, check=True, text=True, capture_output=True)
    return json.loads(completed.stdout)


def _rate(value: str) -> float:
    numerator, denominator = value.split("/", 1)
    return float(numerator) / float(denominator)


def _inspect_video(path: Path, *, expected_frames: int, expected_fps: float) -> dict[str, object]:
    from PIL import Image, ImageChops, ImageDraw

    probe = _run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate,nb_frames,duration",
            "-of",
            "json",
            str(path),
        ]
    )
    stream = probe["streams"][0]
    duration = float(stream["duration"])
    fps = _rate(stream["avg_frame_rate"])
    frame_count = int(stream["nb_frames"])
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        frames = []
        for index, fraction in enumerate((0.0, 0.5, 0.9)):
            output = temporary / f"frame_{index}.png"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    f"{duration * fraction:.6f}",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-y",
                    str(output),
                ],
                check=True,
            )
            frames.append(Image.open(output).convert("RGB"))
        changed_fractions = []
        for left, right in zip(frames, frames[1:]):
            difference = ImageChops.difference(left, right).convert("L")
            histogram = difference.histogram()
            changed = sum(histogram[9:])
            changed_fractions.append(changed / (left.width * left.height))
        sheet = Image.new("RGB", (sum(frame.width for frame in frames), frames[0].height + 30), "white")
        draw = ImageDraw.Draw(sheet)
        offset = 0
        for frame, label in zip(frames, ("start", "middle", "near end")):
            sheet.paste(frame, (offset, 30))
            draw.text((offset + 8, 8), label, fill="black")
            offset += frame.width

    qa_path = path.with_name(f"animation_qa_{path.stem}.png")
    sheet.save(qa_path)
    checks = {
        "h264_codec": stream["codec_name"] == "h264",
        "expected_frame_count": frame_count == expected_frames,
        "expected_frame_rate": abs(fps - expected_fps) < 1e-6,
        "nonzero_duration": duration > 0,
        "usable_resolution": int(stream["width"]) >= 720 and int(stream["height"]) >= 640,
        "visible_temporal_change": max(changed_fractions) > 0.0001,
    }
    return {
        "path": str(path),
        "codec": stream["codec_name"],
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "duration_s": duration,
        "fps": fps,
        "frame_count": frame_count,
        "changed_pixel_fractions": changed_fractions,
        "qa_contact_sheet": str(qa_path),
        "checks": checks,
        "passed": all(checks.values()),
    }


def validate_animations(
    case_dir: str | Path, *, viewer_dir: str | Path | None = None
) -> dict[str, object]:
    case = Path(case_dir).resolve()
    if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
        raise MdpiCfdError("ffprobe and ffmpeg are required for animation validation")
    manifest_path = case / "results/result_manifest.json"
    if not manifest_path.is_file():
        raise MdpiCfdError(f"result manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    semantics = manifest.get("animation_semantics", {})
    videos: dict[str, object] = {}
    for name, expected in EXPECTED.items():
        path = case / "results/artifacts" / expected["filename"]
        if not path.is_file():
            raise MdpiCfdError(f"animation is missing: {path}")
        result = _inspect_video(
            path,
            expected_frames=int(expected["frames"]),
            expected_fps=float(expected["fps"]),
        )
        result["semantics"] = semantics.get(name)
        result["checks"]["semantics_match_manifest"] = (
            semantics.get(name) == expected["semantics"]
        )
        result["passed"] = all(result["checks"].values())
        videos[name] = result

    viewer_checks: dict[str, bool] = {}
    if viewer_dir is not None:
        viewer = Path(viewer_dir).resolve()
        html = (viewer / "index.html").read_text(encoding="utf-8")
        script = (viewer / "app.js").read_text(encoding="utf-8")
        model_text = (viewer / "model.js").read_text(encoding="utf-8")
        viewer_model = json.loads(
            (viewer / "viewer_manifest.json").read_text(encoding="utf-8")
        )
        portable_specs = (
            viewer_model.get("interactive_cfd", {})
            .get("actual_one_second", {})
            .get("datasets", {})
        )
        temperature_graphs = viewer_model.get("interactive_cfd", {}).get(
            "temperature_graphs", {}
        )
        aggregate_graph_keys = (
            "outlet_mass_weighted_average_c",
            "battery_mesh_maximum_c",
            "battery_surface_area_average_c",
            "battery_internal_volume_average_c",
        )
        per_cell_graphs = temperature_graphs.get("per_cell", {})
        graph_data_complete = all(
            len(temperature_graphs.get(key, [])) == 1801
            for key in aggregate_graph_keys
        ) and all(
            len(per_cell_graphs.get(metric, {})) == 23
            and all(len(values) == 1801 for values in per_cell_graphs[metric].values())
            for metric in (
                "internal_volume_average_c",
                "surface_area_average_c",
                "mesh_maximum_c",
            )
        )
        portable_files_present = bool(portable_specs) and all(
            (viewer / str(specification.get("portable_file", ""))).is_file()
            and int(specification.get("byte_length", 0)) > 0
            and len(str(specification.get("sha256", ""))) == 64
            for specification in portable_specs.values()
        )
        viewer_checks = {
            "simulation_clock_video_controls": (
                'id="video-play"' in html
                and 'id="video-seek"' in html
                and "advanceVideoPlayback" in script
            ),
            "playback_rates_1_to_180": all(
                f'value="{value}"' in html
                for value in ("1", "5", "10", "30", "60", "180")
            ),
            "one_x_maps_to_one_simulation_second_per_second": (
                "simulationTime+elapsed*Number(flowRate.value)" in script
                and "videoSimulationTime+elapsed*rate" in script
                and "1800/rate" in script
            ),
            "one_second_actual_figure1_timeline": (
                'id="flow-seek" type="range" min="0" max="1800" step="1"' in html
                and "flowSeek.value=String(Math.round(simulationTime))" in script
                and "function timeSeriesValue" in script
                and "function sectionTemperature" in script
                and "function surfaceThermalValue" in script
                and "직접 셀값 · 시간보간/패딩 없음" in script
                and '"temporal_interpolation":false' in model_text
            ),
            "simulation_time_mapping": "시뮬레이션 t =" in script and "1800*fraction" in script,
            "seek_and_frame_step_controls": (
                'id="video-seek"' in html
                and 'id="video-prev"' in html
                and 'id="video-next"' in html
                and "function stepVideo" in script
            ),
            "fixed_velocity_truth_label": "과도속도 아님" in script,
            "interactive_actual_flow_scene": (
                "model.interactive_cfd" in script
                and "drawFlow(flow.streamlines" in script
                and 'id="flow-seek"' in html
                and 'id="flow-play"' in html
            ),
            "interactive_mouse_orbit_during_playback": (
                'canvas.addEventListener("pointermove"' in script
                and "requestAnimationFrame(animate)" in script
            ),
            "figure1_radio_modes": (
                html.count('name="viz-mode"') == 7
                and "selectVisualMode" in script
                and "Fig 4/7/8" in html
                and "Fig 12" in html
                and '6:"xy-temperature"' in script
                and '11:"xy-velocity"' in script
            ),
            "interactive_xy_xz_sections": (
                "drawSection(horizontalSection()" in script
                and "verticalSelection" in script
                and "for(const section of selectedVertical)drawSection" in script
                and "drawSectionVelocity" in script
            ),
            "five_selectable_horizontal_sections": (
                html.count('name="horizontal-section"') == 5
                and 'id="horizontal-centre"' in html
                and "horizontalSection" in script
                and "updateHorizontalSection" in script
            ),
            "actual_cell_surface_direct_samples": (
                "surfaceThermalValue" in script
                and "actualDatasetValue" in script
                and "cellSurface?.temperature_range_c" in script
                and "angular_segments" in model_text
                and '"actual_dataset":"cell_surface"' in model_text
            ),
            "five_selectable_vertical_sections": (
                html.count('name="vertical-section"') == 5
                and 'id="vertical-all"' in html
                and 'id="vertical-centre"' in html
                and "verticalRange" in script
            ),
            "refined_direct_section_grid": (
                "function drawActualSection" in script
                and "actualDatasetValue" in script
                and '"columns":114' in model_text
                and '"spatial_interpolation":false' in model_text
                and '"padding":false' in model_text
            ),
            "independent_layer_checkboxes": (
                all(
                    f'id="show-{name}"' in html
                    for name in ("cells", "flow", "particles", "section", "grid", "enclosure")
                )
                and 'id="flow-only"' in html
                and "setLayerVisibility" in script
            ),
            "computed_inlet_outlet_display": (
                'id="port-state"' in html
                and "flow.port_flow" in script
                and "portTemperature" in script
                and "drawPortCallout" in script
                and "area_average_pressure_pa" in script
                and "pressure_drop_pa" in script
            ),
            "dense_temperature_legend_with_paper_reference": (
                'const intervals=unit==="C"?18:9' in script
                and "PAPER REF" in script
                and "paper_temperature_ranges_c" in script
            ),
            "raw_temperature_datum_is_disclosed": (
                "temperature_display_offset_c" in script
                and "RAW T (293.15 K)" in script
                and "무보정 · K−273.15만 적용" in script
                and '"temperature_display_offset_c":0.0' in model_text
            ),
            "file_protocol_portable_actual_data": (
                'location.protocol==="file:"' in script
                and "portableDatasetBuffer" in script
                and "DecompressionStream" in script
                and portable_files_present
            ),
            "actual_temperature_graph_data": graph_data_complete,
            "temperature_graph_png_csv_exports": (
                'id="temperature-chart-open"' in html
                and 'id="temperature-chart-png"' in html
                and 'id="temperature-chart-csv"' in html
                and 'id="temperature-chart-all-csv"' in html
                and "paintTemperatureChart" in script
                and "downloadCsv" in script
                and "toBlob" in script
            ),
        }
    checks = {
        "all_videos_passed": all(item["passed"] for item in videos.values()),
        "viewer_controls_passed": bool(viewer_checks) and all(viewer_checks.values()),
    }
    report = {
        "schema_version": 1,
        "status": "ANIMATION_VALIDATION_PASSED" if all(checks.values()) else "ANIMATION_VALIDATION_FAILED",
        "checks": checks,
        "viewer_checks": viewer_checks,
        "videos": videos,
    }
    _atomic_json(case / "results/animation_validation.json", report)
    return report
