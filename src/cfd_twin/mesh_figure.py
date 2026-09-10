"""실제 OpenFOAM ``polyMesh``에서 두 계산 메시의 중앙 단면 그림을 만든다.

이 모듈은 셀 수를 이용해 가짜 격자 무늬를 그리지 않는다. VTK의 OpenFOAM
reader로 정밀/표준 ``internalMesh``를 읽고 z=41 mm 평면과 교차하는 실제
cell polygon을 PNG로 저장한다. 생성된 두 파일은 뷰어 Figure 2의 탭으로
등록되어 결과 선택과 무관하게 서로 비교할 수 있다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _mesh_slice(case_dir: Path, z_m: float) -> tuple[Any, Any, Any]:
    """defaultRegion과 batterySolid의 실제 중앙 절단면을 반환한다."""

    from vtkmodules.vtkCommonDataModel import vtkPlane
    from vtkmodules.vtkFiltersCore import vtkCutter
    from vtkmodules.vtkIOGeometry import vtkOpenFOAMReader

    # vtkOpenFOAMReader는 .foam 파일의 내용이 아닌 부모 디렉터리를 case로
    # 사용하므로 sentinel 파일을 실제로 만들 필요가 없다.
    reader = vtkOpenFOAMReader()
    reader.SetFileName(str(case_dir / "viewer_mesh_slice.foam"))
    reader.Update()
    root = reader.GetOutput()
    volume_mesh = root.GetBlock(0).GetBlock(0)
    battery_mesh = root.GetBlock(1).GetBlock(0)

    plane = vtkPlane()
    plane.SetOrigin(0.0, 0.0, z_m)
    plane.SetNormal(0.0, 0.0, 1.0)

    def cut(grid: Any) -> Any:
        cutter = vtkCutter()
        cutter.SetInputData(grid)
        cutter.SetCutFunction(plane)
        cutter.Update()
        output = cutter.GetOutput()
        # cutter가 소멸해도 데이터가 유지되도록 별도 vtkPolyData에 복사한다.
        copy = output.NewInstance()
        copy.DeepCopy(output)
        return copy

    return volume_mesh, cut(volume_mesh), cut(battery_mesh)


def _xy_polygons(polydata: Any) -> list[list[tuple[float, float]]]:
    """VTK 절단 polygon을 millimetre 좌표의 Matplotlib polygon으로 바꾼다."""

    polygons: list[list[tuple[float, float]]] = []
    for index in range(polydata.GetNumberOfCells()):
        point_ids = polydata.GetCell(index).GetPointIds()
        polygon = [
            (
                1000.0 * polydata.GetPoint(point_ids.GetId(vertex))[0],
                1000.0 * polydata.GetPoint(point_ids.GetId(vertex))[1],
            )
            for vertex in range(point_ids.GetNumberOfIds())
        ]
        if len(polygon) >= 3:
            polygons.append(polygon)
    return polygons


def _quality_metrics(case_dir: Path) -> dict[str, float]:
    report = case_dir / "reports/production_mesh/checkMesh_all_regions.json"
    if not report.is_file():
        return {}
    return json.loads(report.read_text(encoding="utf-8")).get("metrics", {})


def render_mesh_slice(
    case_dir: Path,
    output: Path,
    *,
    label: str,
    z_m: float = 0.041,
) -> dict[str, object]:
    """한 production case의 실제 XY 중앙 메시 그림을 PNG로 저장한다."""

    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection

    volume_mesh, volume_slice, battery_slice = _mesh_slice(case_dir, z_m)
    volume_polygons = _xy_polygons(volume_slice)
    battery_polygons = _xy_polygons(battery_slice)
    quality = _quality_metrics(case_dir)

    figure, axis = plt.subplots(figsize=(10, 10), dpi=160)
    figure.patch.set_facecolor("#07131f")
    axis.set_facecolor("#07131f")
    axis.add_collection(
        PolyCollection(
            battery_polygons,
            facecolors="#a8610b",
            edgecolors="none",
            alpha=0.84,
            rasterized=True,
        )
    )
    axis.add_collection(
        PolyCollection(
            volume_polygons,
            facecolors="none",
            edgecolors="#72dcef",
            linewidths=0.10 if volume_mesh.GetNumberOfCells() > 500_000 else 0.18,
            alpha=0.82,
            rasterized=True,
        )
    )
    axis.set_xlim(-3.0, 117.0)
    axis.set_ylim(-3.0, 124.0)
    axis.set_aspect("equal", adjustable="box")
    axis.set_xlabel("X [mm]", color="#d8e8f1", fontsize=12)
    axis.set_ylabel("Y [mm]", color="#d8e8f1", fontsize=12)
    axis.tick_params(colors="#9eb5c4")
    for spine in axis.spines.values():
        spine.set_color("#638294")
    axis.set_title(
        f"Figure 2 · {label} · actual polyMesh XY centre section",
        color="#f1f7fa",
        fontsize=16,
        pad=16,
    )
    max_non_orthogonality = quality.get("max_non_orthogonality")
    max_skewness = quality.get("max_skewness")
    detail = (
        f"z = {1000 * z_m:.1f} mm  |  "
        f"{volume_mesh.GetNumberOfCells():,} cells  |  "
        f"{volume_mesh.GetNumberOfPoints():,} points"
    )
    if max_non_orthogonality is not None and max_skewness is not None:
        detail += (
            f"\nmax non-orthogonality {float(max_non_orthogonality):.3f}°  |  "
            f"max skewness {float(max_skewness):.3f}"
        )
    axis.text(
        0.5,
        -0.105,
        detail,
        transform=axis.transAxes,
        ha="center",
        va="top",
        color="#b8ccd7",
        fontsize=10,
    )
    axis.text(
        0.012,
        0.012,
        "cyan: all cell intersections · amber: batterySolid cells",
        transform=axis.transAxes,
        color="#d9e8ef",
        fontsize=9,
        bbox={"facecolor": "#07131f", "edgecolor": "#4a687a", "alpha": 0.88},
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, bbox_inches="tight", facecolor=figure.get_facecolor())
    plt.close(figure)
    return {
        "artifact": str(output),
        "cell_count": int(volume_mesh.GetNumberOfCells()),
        "point_count": int(volume_mesh.GetNumberOfPoints()),
        "slice_cell_count": int(volume_slice.GetNumberOfCells()),
        "z_m": z_m,
        "quality": quality,
    }


def write_two_mesh_figures(case_root: Path) -> dict[str, object]:
    """정밀/표준 메시 PNG를 만들고 Figure 2 결과 계약에 등록한다."""

    artifacts = case_root / "results/artifacts"
    runs = (
        (
            "precision",
            "Precision mesh · 1,316,854 cells",
            case_root / "production/L3",
            artifacts / "figure_02_precision_mesh_xy.png",
        ),
        (
            "standard",
            "Standard mesh · 216,577 cells",
            case_root / "production/single",
            artifacts / "figure_02_standard_mesh_xy.png",
        ),
    )
    metadata: dict[str, object] = {}
    artifact_paths: list[str] = []
    for run_id, label, case_dir, output in runs:
        metadata[run_id] = render_mesh_slice(case_dir, output, label=label)
        artifact_paths.append(str(output.relative_to(case_root)))

    contract_path = case_root / "results/figures/figure_02.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract.update(
        {
            "artifacts": artifact_paths,
            "status": "TWO_PRODUCTION_MESHES_READY",
            "title": "Precision and standard mesh structure",
            "synthetic_data": False,
        }
    )
    contract_metadata = dict(contract.get("metadata", {}))
    contract_metadata.update(
        {
            "mesh_panels": metadata,
            "source": "actual OpenFOAM polyMesh central XY intersections",
            "slice_z_m": 0.041,
        }
    )
    contract["metadata"] = contract_metadata
    contract_path.write_text(
        json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"status": "TWO_PRODUCTION_MESHES_READY", "artifacts": artifact_paths}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(write_two_mesh_figures(args.case_root.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
