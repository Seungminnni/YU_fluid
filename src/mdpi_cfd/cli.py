from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .case_builder import dependency_report, prepare_case_inputs
from .cad import execute_cad_source, write_cad_source_bundle
from .config import load_config
from .errors import MdpiCfdError
from .geometry import write_geometry_bundle
from .heat_source import source_from_config, write_heat_source_bundle
from .mesh import execute_mesh, write_mesh_source_bundle
from .mesh_independence import evaluate_mesh_independence
from .overlay import write_geometry_overlay
from .production_mesh import write_production_mesh_source_bundle
from .postprocess import generate_l0_animations, postprocess_l0_results
from .animation_validation import validate_animations
from .paper_evaluation import evaluate_paper_alignment
from .viewer import write_viewer_bundle


def _json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _load(args: argparse.Namespace):
    return load_config(
        args.config,
        overlay_paths=tuple(args.overlay),
        overrides=tuple(args.set_values),
    )


def _validate_config(args: argparse.Namespace) -> int:
    config = _load(args)
    result = {
        "status": "VALID",
        "case_name": config.case_name,
        "mode": config.mode,
        "source_sha256": config.source_sha256,
        "resolved_input_sha256": config.resolved_input_sha256,
        "applied_overlays": config.applied_overlays,
        "applied_overrides": config.applied_overrides,
        "geometry_sha256": config.geometry_sha256,
        "derived": config.to_dict()["derived"],
    }
    _json(result)
    return 0


def _build_geometry(args: argparse.Namespace) -> int:
    config = _load(args)
    report = write_geometry_bundle(config, args.output)
    _json(
        {
            "status": "GEOMETRY_PREVIEW_CREATED",
            "output": str(Path(args.output).resolve()),
            "production_cad_ready": report["production_cad_ready"],
            "blocking_assumptions": report["blocking_assumptions"],
            "clearances": report["clearances"],
        }
    )
    return 0


def _generate_heat_source(args: argparse.Namespace) -> int:
    config = _load(args)
    source = source_from_config(config)
    manifest = write_heat_source_bundle(config, args.output)
    samples = {
        str(time_s): source.qvol(time_s)
        for time_s in sorted(set((0.0, float(args.sample_time), *source.breakpoints())))
    }
    _json(
        {
            "status": "HEAT_SOURCE_CREATED",
            "output": str(Path(args.output).resolve()),
            "manifest": manifest,
            "samples_W_m3": samples,
        }
    )
    return 0


def _prepare_case(args: argparse.Namespace) -> int:
    config = _load(args)
    manifest = prepare_case_inputs(
        config,
        args.output,
        execute_cad=args.execute_cad,
        execute_mesh_level=args.execute_mesh,
    )
    _json(
        {
            "status": manifest["status"],
            "runnable": manifest["runnable"],
            "output": str(Path(args.output).resolve()),
            "blockers": manifest["blockers"],
            "warnings": manifest["warnings"],
        }
    )
    return 0


def _build_viewer(args: argparse.Namespace) -> int:
    config = _load(args)
    model = write_viewer_bundle(config, args.output, case_dir=args.case_dir)
    _json(
        {
            "status": model["status"],
            "output": str(Path(args.output).resolve()),
            "entrypoint": str((Path(args.output).resolve() / "index.html")),
            "production_cad_ready": model["production_cad_ready"],
        }
    )
    return 0


def _build_overlay(args: argparse.Namespace) -> int:
    config = _load(args)
    report = write_geometry_overlay(config, args.pptx, args.output)
    _json(
        {
            "status": report["status"],
            "output": str(Path(args.output).resolve()),
            "detected_cell_count": report["detected_cell_count"],
            "candidate": report["candidate"],
            "geometry_gate_g1_passed": report["geometry_gate_g1_passed"],
            "blocking_reason": report["blocking_reason"],
        }
    )
    return 0


def _build_cad_source(args: argparse.Namespace) -> int:
    config = _load(args)
    manifest = write_cad_source_bundle(config, args.output)
    _json({**manifest, "output": str(Path(args.output).resolve())})
    return 0


def _build_cad(args: argparse.Namespace) -> int:
    config = _load(args)
    result = execute_cad_source(config, args.output)
    _json({**result, "output": str(Path(args.output).resolve())})
    return 0


def _build_mesh_source(args: argparse.Namespace) -> int:
    config = _load(args)
    manifest = write_mesh_source_bundle(
        config, args.cad_dir, args.output, mesh_level=args.level
    )
    _json({**manifest, "output": str(Path(args.output).resolve())})
    return 0


def _build_mesh(args: argparse.Namespace) -> int:
    config = _load(args)
    result = execute_mesh(config, args.cad_dir, args.output, mesh_level=args.level)
    _json({**result, "output": str(Path(args.output).resolve())})
    return 0


def _build_production_mesh_source(args: argparse.Namespace) -> int:
    config = _load(args)
    manifest = write_production_mesh_source_bundle(
        config, args.cad_dir, args.output, mesh_level=args.level
    )
    _json({**manifest, "output": str(Path(args.output).resolve())})
    return 0


def _doctor(_: argparse.Namespace) -> int:
    report = dependency_report()
    _json(report)
    return 0


def _postprocess_l0(args: argparse.Namespace) -> int:
    config = _load(args)
    manifest = postprocess_l0_results(config, args.case_dir)
    _json(
        {
            "status": manifest["data_status"],
            "case_dir": str(Path(args.case_dir).resolve()),
            "publication_results_authorized": manifest[
                "publication_results_authorized"
            ],
            "generated_figure_ids": manifest["generated_figure_ids"],
            "final_summary": manifest["final_summary"],
        }
    )
    return 0


def _evaluate_mesh_independence(args: argparse.Namespace) -> int:
    report = evaluate_mesh_independence(args.case_dir)
    _json(report)
    return 0 if report["status"] != "MESH_INDEPENDENCE_FAILED" else 2


def _animate_l0(args: argparse.Namespace) -> int:
    config = _load(args)
    result = generate_l0_animations(config, args.case_dir)
    _json(result)
    return 0


def _validate_animations(args: argparse.Namespace) -> int:
    report = validate_animations(args.case_dir, viewer_dir=args.viewer_dir)
    _json(report)
    return 0 if report["status"] == "ANIMATION_VALIDATION_PASSED" else 2


def _evaluate_paper_alignment(args: argparse.Namespace) -> int:
    config = _load(args)
    report = evaluate_paper_alignment(config, args.case_dir)
    _json(report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mdpi-cfd",
        description="Traceable MDPI 23-cell CFD case preparation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_config_inputs(command: argparse.ArgumentParser) -> None:
        command.add_argument("config")
        command.add_argument(
            "--overlay",
            action="append",
            default=[],
            help="deep-merge an override YAML file; repeatable",
        )
        command.add_argument(
            "--set",
            dest="set_values",
            action="append",
            default=[],
            metavar="PATH=VALUE",
            help="override one dotted setting using a YAML value; repeatable",
        )

    validate = subparsers.add_parser("validate-config")
    add_config_inputs(validate)
    validate.set_defaults(func=_validate_config)

    geometry = subparsers.add_parser("build-geometry")
    add_config_inputs(geometry)
    geometry.add_argument("--output", required=True)
    geometry.set_defaults(func=_build_geometry)

    heat = subparsers.add_parser("generate-heat-source")
    add_config_inputs(heat)
    heat.add_argument("--output", required=True)
    heat.add_argument("--sample-time", type=float, default=1800.0)
    heat.set_defaults(func=_generate_heat_source)

    case = subparsers.add_parser("prepare-case")
    add_config_inputs(case)
    case.add_argument("--output", required=True)
    case.add_argument(
        "--execute-cad",
        action="store_true",
        help="run CadQuery export and geometry checks inside the case bundle",
    )
    case.add_argument(
        "--execute-mesh",
        choices=("smoke",),
        help="also generate a Gmsh volume mesh; this implies --execute-cad",
    )
    case.set_defaults(func=_prepare_case)

    viewer = subparsers.add_parser("build-viewer")
    add_config_inputs(viewer)
    viewer.add_argument("--output", required=True)
    viewer.add_argument("--case-dir", help="load real figure artifacts from a prepared case")
    viewer.set_defaults(func=_build_viewer)

    overlay = subparsers.add_parser("build-overlay")
    add_config_inputs(overlay)
    overlay.add_argument("--pptx", required=True)
    overlay.add_argument("--output", required=True)
    overlay.set_defaults(func=_build_overlay)

    cad_source = subparsers.add_parser("build-cad-source")
    add_config_inputs(cad_source)
    cad_source.add_argument("--output", required=True)
    cad_source.set_defaults(func=_build_cad_source)

    cad = subparsers.add_parser("build-cad")
    add_config_inputs(cad)
    cad.add_argument("--output", required=True)
    cad.set_defaults(func=_build_cad)

    for name, handler in (("build-mesh-source", _build_mesh_source), ("build-mesh", _build_mesh)):
        mesh = subparsers.add_parser(name)
        add_config_inputs(mesh)
        mesh.add_argument("--cad-dir", required=True)
        mesh.add_argument("--output", required=True)
        mesh.add_argument("--level", choices=("smoke",), default="smoke")
        mesh.set_defaults(func=handler)

    production_mesh = subparsers.add_parser("build-production-mesh-source")
    add_config_inputs(production_mesh)
    production_mesh.add_argument("--cad-dir", required=True)
    production_mesh.add_argument("--output", required=True)
    production_mesh.add_argument("--level", choices=("L0", "L1", "L2", "L3"), default="L0")
    production_mesh.set_defaults(func=_build_production_mesh_source)

    postprocess = subparsers.add_parser(
        "postprocess-l0", help="extract validated L0 OpenFOAM results and figures"
    )
    add_config_inputs(postprocess)
    postprocess.add_argument("--case-dir", required=True)
    postprocess.set_defaults(func=_postprocess_l0)

    mesh_study = subparsers.add_parser(
        "evaluate-mesh-independence",
        help="evaluate L0-L3 metrics against the final design gates",
    )
    mesh_study.add_argument("--case-dir", required=True)
    mesh_study.set_defaults(func=_evaluate_mesh_independence)

    animate = subparsers.add_parser(
        "animate-l0", help="render actual thermal and frozen-flow particle MP4 files"
    )
    add_config_inputs(animate)
    animate.add_argument("--case-dir", required=True)
    animate.set_defaults(func=_animate_l0)

    animation_check = subparsers.add_parser(
        "validate-animations", help="verify MP4 structure, temporal changes and viewer controls"
    )
    animation_check.add_argument("--case-dir", required=True)
    animation_check.add_argument("--viewer-dir", required=True)
    animation_check.set_defaults(func=_validate_animations)

    paper_check = subparsers.add_parser(
        "evaluate-paper-alignment",
        help="compare final actual fields with digitized paper references",
    )
    add_config_inputs(paper_check)
    paper_check.add_argument("--case-dir", required=True)
    paper_check.set_defaults(func=_evaluate_paper_alignment)

    doctor = subparsers.add_parser("doctor")
    doctor.set_defaults(func=_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except MdpiCfdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
