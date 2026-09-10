"""CadQuery/OpenCascade가 내보낸 실제 CAD 형상을 정투영 이미지로 렌더링한다."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _polydata(path: Path, *, scale: float = 1.0):
    import vtk

    reader = vtk.vtkSTLReader()
    reader.SetFileName(str(path))
    reader.Update()
    source = reader.GetOutputPort()
    if scale != 1.0:
        transform = vtk.vtkTransform()
        transform.Scale(scale, scale, scale)
        transformed = vtk.vtkTransformPolyDataFilter()
        transformed.SetTransform(transform)
        transformed.SetInputConnection(source)
        transformed.Update()
        source = transformed.GetOutputPort()
    normals = vtk.vtkPolyDataNormals()
    normals.SetInputConnection(source)
    normals.ConsistencyOn()
    normals.AutoOrientNormalsOn()
    normals.SplittingOn()
    normals.SetFeatureAngle(35)
    normals.Update()
    return normals.GetOutput()


def _actor(
    polydata,
    colour: tuple[float, float, float],
    opacity: float,
    *,
    enclosure: bool = False,
):
    import vtk

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(polydata)
    mapper.ScalarVisibilityOff()
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(*colour)
    actor.GetProperty().SetOpacity(opacity)
    if enclosure:
        actor.GetProperty().SetInterpolationToFlat()
        actor.GetProperty().SetAmbient(0.82)
        actor.GetProperty().SetDiffuse(0.18)
        actor.GetProperty().SetSpecular(0.0)
    else:
        actor.GetProperty().SetInterpolationToPhong()
        actor.GetProperty().SetAmbient(0.24)
        actor.GetProperty().SetDiffuse(0.72)
        actor.GetProperty().SetSpecular(0.18)
        actor.GetProperty().SetSpecularPower(24)
    return actor


def _render(
    enclosure,
    cells,
    output: Path,
    *,
    direction: tuple[float, float, float],
    view_up: tuple[float, float, float],
) -> None:
    import vtk

    renderer = vtk.vtkRenderer()
    renderer.SetBackground(1.0, 1.0, 1.0)
    renderer.SetUseDepthPeeling(True)
    renderer.SetMaximumNumberOfPeels(100)
    renderer.SetOcclusionRatio(0.1)
    renderer.AddActor(
        _actor(enclosure, (0.39, 0.72, 0.78), 0.16, enclosure=True)
    )
    renderer.AddActor(_actor(cells, (0.25, 0.48, 0.73), 1.0))

    window = vtk.vtkRenderWindow()
    window.SetOffScreenRendering(True)
    window.SetAlphaBitPlanes(True)
    window.SetMultiSamples(0)
    window.SetSize(1800, 1600)
    window.AddRenderer(renderer)

    bounds = enclosure.GetBounds()
    centre = (
        (bounds[0] + bounds[1]) / 2,
        (bounds[2] + bounds[3]) / 2,
        (bounds[4] + bounds[5]) / 2,
    )
    span = max(
        bounds[1] - bounds[0],
        bounds[3] - bounds[2],
        bounds[5] - bounds[4],
    )
    camera = renderer.GetActiveCamera()
    camera.ParallelProjectionOn()
    camera.SetFocalPoint(*centre)
    camera.SetPosition(
        centre[0] + direction[0] * span * 4,
        centre[1] + direction[1] * span * 4,
        centre[2] + direction[2] * span * 4,
    )
    camera.SetViewUp(*view_up)
    renderer.ResetCamera(bounds)
    camera.Zoom(1.08)
    renderer.ResetCameraClippingRange()
    window.Render()

    capture = vtk.vtkWindowToImageFilter()
    capture.SetInput(window)
    capture.SetInputBufferTypeToRGB()
    capture.ReadFrontBufferOff()
    capture.Update()
    writer = vtk.vtkPNGWriter()
    writer.SetFileName(str(output))
    writer.SetInputConnection(capture.GetOutputPort())
    writer.Write()
    window.Finalize()


def render_actual_cad(source_dir: Path, output_dir: Path) -> dict[str, object]:
    """검증된 CAD STL을 직접 읽어 상면·측면·등각 이미지를 만든다."""

    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()
    coolant_stl = source_dir / "coolant.stl"
    enclosure_stl = source_dir / "production_surfaces" / "outerDomain.stl"
    battery_stl = source_dir / "battery_cells.stl"
    source_files = [
        source_dir / "enclosure_with_ports.step",
        source_dir / "coolant.step",
        source_dir / "battery_cells.step",
        coolant_stl,
        battery_stl,
        enclosure_stl,
        source_dir / "cad_result.json",
    ]
    missing = [str(path) for path in source_files if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing CAD output: {missing}")

    output_dir.mkdir(parents=True, exist_ok=True)
    # production surface STL은 OpenFOAM용 metre 단위이므로 CAD 작업 단위인
    # millimetre로 되돌린다. 형상은 CadQuery outer_with_ports와 동일하다.
    enclosure = _polydata(enclosure_stl, scale=1000.0)
    cells = _polydata(battery_stl)
    views = {
        "actual_CAD_top_XY.png": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
        "actual_CAD_side_XZ.png": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
        "actual_CAD_isometric.png": ((1.0, -1.0, 0.85), (0.0, 0.0, 1.0)),
    }
    for filename, (direction, view_up) in views.items():
        _render(
            enclosure,
            cells,
            output_dir / filename,
            direction=direction,
            view_up=view_up,
        )

    result = json.loads((source_dir / "cad_result.json").read_text(encoding="utf-8"))
    manifest = {
        "status": "RENDERED_FROM_VERIFIED_CAD_EXPORT",
        "source": str(source_dir),
        "cad_status": result["status"],
        "cad_checks": result["checks"],
        "geometry_sha256": result["geometry_sha256"],
        "source_sha256": {
            path.name: _sha256(path)
            for path in source_files
            if path.suffix.lower() in {".step", ".stl"}
        },
        "rendered_views": list(views),
        "render_basis": (
            "production_surfaces/outerDomain.stl + battery_cells.stl "
            "exported by CadQuery/OpenCascade"
        ),
        "bounds_mm": list(enclosure.GetBounds()),
        "battery_polydata_cells": cells.GetNumberOfCells(),
    }
    (output_dir / "render_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            render_actual_cad(args.source_dir, args.output_dir),
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
