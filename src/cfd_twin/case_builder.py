from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def _docker_command() -> str | None:
    """Locate Docker Desktop on either supported host OS without a shell."""

    found = shutil.which("docker")
    if found:
        return found
    if os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Docker/Docker/resources/bin/docker.exe"
    else:
        candidate = Path("/Applications/Docker.app/Contents/Resources/bin/docker")
    return str(candidate) if candidate.is_file() else None


def _docker_engine_version(command: str | None) -> str | None:
    if not command:
        return None
    try:
        result = subprocess.run(
            [command, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    version = result.stdout.strip() if result.returncode == 0 else ""
    return version or None


def dependency_report() -> dict[str, object]:
    """Report only dependencies used by the active production pipeline."""

    docker = _docker_command()
    docker_version = _docker_engine_version(docker)
    commands = {
        "chtMultiRegionSimpleFoam": shutil.which("chtMultiRegionSimpleFoam"),
        "chtMultiRegionFoam": shutil.which("chtMultiRegionFoam"),
        "blockMesh": shutil.which("blockMesh"),
        "snappyHexMesh": shutil.which("snappyHexMesh"),
        "checkMesh": shutil.which("checkMesh"),
        "docker": docker,
        "ffmpeg": shutil.which("ffmpeg"),
    }
    optional_modules = {
        module: importlib.util.find_spec(module) is not None
        for module in ("cadquery", "numpy", "PIL", "vtk")
    }
    host_openfoam_ready = all(
        commands[name]
        for name in (
            "chtMultiRegionSimpleFoam", "chtMultiRegionFoam", "blockMesh",
            "snappyHexMesh", "checkMesh",
        )
    )
    python_ready = all(optional_modules.values())
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "commands": commands,
        "optional_python_modules": optional_modules,
        "core_ready": True,
        "openfoam_ready": host_openfoam_ready,
        "production_python_ready": python_ready,
        "docker_engine_ready": docker_version is not None,
        "docker_server_version": docker_version,
        # Workflow jobs build and run OpenFOAM inside Docker; local OpenFOAM
        # executables are optional and must not make a ready Mac/PC look broken.
        "workflow_host_ready": python_ready and docker_version is not None,
    }
