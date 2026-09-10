"""검증된 OpenFOAM 필드를 VTK로 읽어 UI용 표면·단면·유선을 추출한다.

이 모듈은 CFD를 다시 계산하거나 임의 결과를 합성하지 않는다. 저장된 U/T
필드를 읽고, 배터리 표면 삼각형과 입구에서 시작하는 유선을 후처리한다.
"""

from __future__ import annotations

import math
from pathlib import Path

from .config import ResolvedConfig
from .errors import CfdTwinError


REFERENCE_FIGURE_RANGES = {
    "figure_08_temperature_degC": [20.008, 20.155],
    "figure_09_temperature_degC": [20.008, 20.155],
    "figure_10_velocity_m_s": [0.0, 0.565],
    "figure_11_velocity_m_s": [0.0, 0.218],
    "figure_12_velocity_m_s": [0.0, 0.166],
    "figure_13a_temperature_degC": [20.008, 20.155],
    "figure_13b_temperature_degC": [20.0, 20.137],
    "figure_14_temperature_degC": [20.0, 20.151],
}

# The archived precision run used a rounded 293 K datum. This display-only
# correction keeps that historical result comparable with a 20 degC inlet.
LEGACY_DISPLAY_TEMPERATURE_OFFSET_C = 0.15


def read_openfoam_dat(path: str | Path) -> list[tuple[float, ...]]:
    """OpenFOAM function object가 기록한 숫자 표를 유한값으로 검증해 읽는다."""

    rows: list[tuple[float, ...]] = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            values = tuple(float(token) for token in stripped.split())
        except ValueError as exc:
            raise CfdTwinError(f"invalid OpenFOAM table row in {path}: {line}") from exc
        if len(values) < 2 or not all(math.isfinite(value) for value in values):
            raise CfdTwinError(f"invalid OpenFOAM table values in {path}: {line}")
        rows.append(values)
    if not rows:
        raise CfdTwinError(f"OpenFOAM table has no data rows: {path}")
    return rows


class OpenFoamResultReader:
    """검증된 솔버 필드를 UI 데이터로 바꾸는 최소 VTK 어댑터."""

    def __init__(self, foam_file: Path):
        try:
            import numpy as np
            import vtk
            from vtk.util.numpy_support import vtk_to_numpy
        except ImportError as exc:
            raise CfdTwinError(
                "result extraction requires the 'results' extra: "
                "python3 -m pip install -e '.[results]'"
            ) from exc
        self.np = np
        self.vtk = vtk
        self.vtk_to_numpy = vtk_to_numpy
        self.reader = vtk.vtkOpenFOAMReader()
        self.reader.SetFileName(str(foam_file))
        self.reader.CreateCellToPointOn()
        self.reader.EnableAllCellArrays()
        self.reader.EnableAllPointArrays()
        self.reader.DisableAllPatchArrays()
        self.reader.SetPatchArrayStatus("internalMesh", 1)
        self.reader.UpdateInformation()

    def _region(self, name: str):
        output = self.reader.GetOutput()
        for index in range(output.GetNumberOfBlocks()):
            metadata = output.GetMetaData(index)
            block_name = (
                metadata.Get(self.vtk.vtkCompositeDataSet.NAME()) if metadata else None
            )
            if block_name != name:
                continue
            parent = output.GetBlock(index)
            if hasattr(parent, "GetNumberOfBlocks"):
                for child_index in range(parent.GetNumberOfBlocks()):
                    child = parent.GetBlock(child_index)
                    if child is not None and child.GetNumberOfCells() > 0:
                        return child
            return parent
        raise CfdTwinError(f"OpenFOAM reader did not expose region {name}")

    def regions(self, time_s: float):
        self.reader.UpdateTimeStep(float(time_s))
        return self._region("coolant"), self._region("batterySolid")

    def surface(self, dataset):
        """계산영역 외피를 삼각형화하고 각 점의 실제 T 값을 반환한다."""

        surface = self.vtk.vtkDataSetSurfaceFilter()
        surface.SetInputData(dataset)
        triangles = self.vtk.vtkTriangleFilter()
        triangles.SetInputConnection(surface.GetOutputPort())
        triangles.Update()
        output = triangles.GetOutput()
        points = self.vtk_to_numpy(output.GetPoints().GetData()).copy()
        cells = self.np.array(
            [
                [output.GetCell(index).GetPointId(item) for item in range(3)]
                for index in range(output.GetNumberOfCells())
            ],
            dtype=int,
        )
        temperature = self.vtk_to_numpy(output.GetPointData().GetArray("T")).copy()
        return points, cells, temperature

    def streamline_paths(self, fluid, config: ResolvedConfig):
        """입구 seed에서 고정 속도장 U를 따라 적분한 유선만 반환한다."""

        output = self._streamline_output(fluid, config)
        points = self.vtk_to_numpy(output.GetPoints().GetData()).copy()
        velocity = self.vtk_to_numpy(output.GetPointData().GetArray("U")).copy()
        temperature = self.vtk_to_numpy(output.GetPointData().GetArray("T")).copy()
        paths: list[tuple[object, object, object, object, float]] = []
        for index in range(output.GetNumberOfCells()):
            cell = output.GetCell(index)
            ids = self.np.asarray(
                [cell.GetPointId(item) for item in range(cell.GetNumberOfPoints())],
                dtype=int,
            )
            if len(ids) < 5:
                continue
            path_points = points[ids]
            path_velocity = velocity[ids]
            segment_lengths = self.np.linalg.norm(
                self.np.diff(path_points, axis=0), axis=1
            )
            if float(segment_lengths.sum()) < 0.035:
                continue
            if float(self.np.dot(path_points[1] - path_points[0], path_velocity[0])) < 0:
                ids = ids[::-1]
                path_points = points[ids]
                path_velocity = velocity[ids]
                segment_lengths = self.np.linalg.norm(
                    self.np.diff(path_points, axis=0), axis=1
                )
            cumulative_mm = self.np.concatenate(
                ([0.0], self.np.cumsum(segment_lengths) * 1000.0)
            )
            paths.append(
                (
                    path_points * 1000.0,
                    self.np.linalg.norm(path_velocity, axis=1),
                    temperature[ids] - 273.15,
                    cumulative_mm,
                    float(cumulative_mm[-1]),
                )
            )
        if not paths:
            raise CfdTwinError("stream tracer produced no inlet-fed paths")
        return paths

    def _streamline_output(self, fluid, config: ResolvedConfig):
        # Runge-Kutta 4/5 적분은 저장된 정상 U(x)의 공간 경로를 구한다.
        # 화면의 이동 헤드는 이 경로를 보여주는 효과이며 과도 U 재계산이 아니다.
        fluid.GetPointData().SetActiveVectors("U")
        inlet_source = self.vtk.vtkDiskSource()
        inlet_source.SetInnerRadius(0.0008)
        inlet_source.SetOuterRadius(0.46 * config.ports.inlet_diameter_m)
        inlet_source.SetRadialResolution(7)
        inlet_source.SetCircumferentialResolution(48)
        inlet_x, inlet_y = config.ports.inlet_center_xy_m
        transform = self.vtk.vtkTransform()
        transform.Translate(
            inlet_x,
            inlet_y,
            config.enclosure.height_z_m + 0.82 * config.ports.external_length_m,
        )
        transformed_source = self.vtk.vtkTransformPolyDataFilter()
        transformed_source.SetTransform(transform)
        transformed_source.SetInputConnection(inlet_source.GetOutputPort())
        tracer = self.vtk.vtkStreamTracer()
        tracer.SetInputData(fluid)
        tracer.SetSourceConnection(transformed_source.GetOutputPort())
        tracer.SetIntegratorTypeToRungeKutta45()
        tracer.SetIntegrationDirectionToForward()
        tracer.SetMaximumPropagation(0.55)
        tracer.SetIntegrationStepUnit(tracer.LENGTH_UNIT)
        tracer.SetInitialIntegrationStep(0.0004)
        tracer.SetMinimumIntegrationStep(0.00005)
        tracer.SetMaximumIntegrationStep(0.0015)
        tracer.SetMaximumNumberOfSteps(8000)
        tracer.SetComputeVorticity(False)
        tracer.Update()
        return tracer.GetOutput()
