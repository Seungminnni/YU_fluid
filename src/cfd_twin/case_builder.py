from __future__ import annotations

import importlib.util
import platform
import shutil
import sys


def dependency_report() -> dict[str, object]:
    """Report only dependencies used by the active production pipeline."""

    commands = {
        "chtMultiRegionSimpleFoam": shutil.which("chtMultiRegionSimpleFoam"),
        "chtMultiRegionFoam": shutil.which("chtMultiRegionFoam"),
        "blockMesh": shutil.which("blockMesh"),
        "snappyHexMesh": shutil.which("snappyHexMesh"),
        "checkMesh": shutil.which("checkMesh"),
        "docker": shutil.which("docker"),
        "ffmpeg": shutil.which("ffmpeg"),
    }
    optional_modules = {
        module: importlib.util.find_spec(module) is not None
        for module in ("cadquery", "numpy", "PIL", "vtk")
    }
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "commands": commands,
        "optional_python_modules": optional_modules,
        "core_ready": True,
        "openfoam_ready": all(
            commands[name]
            for name in (
                "chtMultiRegionSimpleFoam",
                "chtMultiRegionFoam",
                "blockMesh",
                "snappyHexMesh",
                "checkMesh",
            )
        ),
        "production_python_ready": all(optional_modules.values()),
    }
