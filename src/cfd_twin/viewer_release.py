"""Validate the server viewer and publish the same files as an offline ZIP.

The project-root ``viewer/`` is the only editable publication. A ZIP is a
verified snapshot of that directory, not a second viewer to maintain by hand.
"""

from __future__ import annotations

from .file_lock import lock_file, unlock_file
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

from .multi_case_viewer import append_validated_run, dataset_paths, read_viewer_model, write_four_case_model


WINDOWS_VIEWER_LAUNCHER = r'''@echo off
setlocal
set "ROOT_DIR=%~dp0"
set "VIEWER_DIR=%ROOT_DIR%viewer"
set "SERVER_SCRIPT=%ROOT_DIR%serve-viewer.ps1"
set "PORT_FILE=%TEMP%\YU_fluid_viewer_%RANDOM%_%RANDOM%.port"

if not exist "%VIEWER_DIR%\results.html" (
  echo [ERROR] viewer\results.html was not found. Extract the entire ZIP first.
  pause
  exit /b 1
)

if not exist "%SERVER_SCRIPT%" (
  echo [ERROR] serve-viewer.ps1 was not found. Extract the entire ZIP first.
  pause
  exit /b 1
)

del /q "%PORT_FILE%" >nul 2>&1
start "YU Fluid Viewer Server" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%SERVER_SCRIPT%" -Root "%VIEWER_DIR%" -Port 0 -PortFile "%PORT_FILE%"

set /a WAIT_COUNT=0
:wait_for_port
if exist "%PORT_FILE%" goto port_ready
ping 127.0.0.1 -n 2 >nul
set /a WAIT_COUNT+=1
if %WAIT_COUNT% LSS 30 goto wait_for_port
echo [ERROR] The local viewer server could not start.
echo Close security software warnings, extract the entire ZIP, and try again.
pause
exit /b 1

:port_ready
set /p VIEWER_PORT=<"%PORT_FILE%"
del /q "%PORT_FILE%" >nul 2>&1
if "%VIEWER_PORT%"=="" (
  echo [ERROR] The local viewer server returned no port number.
  pause
  exit /b 1
)
set "VIEWER_URL=http://127.0.0.1:%VIEWER_PORT%/results.html"
set "HEALTH_URL=http://127.0.0.1:%VIEWER_PORT%/_cfd_twin_health"

set /a HEALTH_COUNT=0
:wait_for_health
powershell.exe -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing '%HEALTH_URL%' -TimeoutSec 2; if ($r.Content -eq 'YU_FLUID_VIEWER_OK') { exit 0 } else { exit 1 } } catch { exit 1 }"
if not errorlevel 1 goto open_viewer
ping 127.0.0.1 -n 2 >nul
set /a HEALTH_COUNT+=1
if %HEALTH_COUNT% LSS 15 goto wait_for_health
echo [ERROR] The local viewer server did not respond.
pause
exit /b 1

:open_viewer
start "" "%VIEWER_URL%"
endlocal
'''

WINDOWS_POWERSHELL_SERVER = r'''param(
    [Parameter(Mandatory = $true)][string]$Root,
    [int]$Port = 0,
    [Parameter(Mandatory = $true)][string]$PortFile
)

$ErrorActionPreference = "Stop"
$rootFull = [IO.Path]::GetFullPath($Root).TrimEnd("\")
$rootPrefix = $rootFull + "\"
$listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $Port)
$listener.Start()
$actualPort = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
[IO.File]::WriteAllText($PortFile, [string]$actualPort, [Text.Encoding]::ASCII)
$lastRequest = [DateTime]::UtcNow

function Get-ContentType([string]$Path) {
    switch ([IO.Path]::GetExtension($Path).ToLowerInvariant()) {
        ".html" { return "text/html; charset=utf-8" }
        ".htm"  { return "text/html; charset=utf-8" }
        ".js"   { return "application/javascript; charset=utf-8" }
        ".css"  { return "text/css; charset=utf-8" }
        ".json" { return "application/json; charset=utf-8" }
        ".svg"  { return "image/svg+xml" }
        ".png"  { return "image/png" }
        ".jpg"  { return "image/jpeg" }
        ".jpeg" { return "image/jpeg" }
        ".gif"  { return "image/gif" }
        ".webp" { return "image/webp" }
        ".ico"  { return "image/x-icon" }
        ".txt"  { return "text/plain; charset=utf-8" }
        ".csv"  { return "text/csv; charset=utf-8" }
        default  { return "application/octet-stream" }
    }
}

function Send-TextResponse($Stream, [int]$Status, [string]$Reason, [string]$Text, [bool]$HeadOnly = $false) {
    $body = [Text.Encoding]::UTF8.GetBytes($Text)
    $header = "HTTP/1.1 $Status $Reason`r`nContent-Type: text/plain; charset=utf-8`r`nContent-Length: $($body.Length)`r`nConnection: close`r`n`r`n"
    $headerBytes = [Text.Encoding]::ASCII.GetBytes($header)
    $Stream.Write($headerBytes, 0, $headerBytes.Length)
    if (-not $HeadOnly) { $Stream.Write($body, 0, $body.Length) }
}

try {
    while (([DateTime]::UtcNow - $lastRequest).TotalHours -lt 4) {
        if (-not $listener.Pending()) {
            Start-Sleep -Milliseconds 100
            continue
        }
        $client = $listener.AcceptTcpClient()
        $lastRequest = [DateTime]::UtcNow
        try {
            $client.ReceiveTimeout = 10000
            $client.SendTimeout = 10000
            $stream = $client.GetStream()
            $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::ASCII, $false, 1024, $true)
            $requestLine = $reader.ReadLine()
            while (($headerLine = $reader.ReadLine()) -ne $null -and $headerLine -ne "") { }
            if ([string]::IsNullOrWhiteSpace($requestLine)) {
                Send-TextResponse $stream 400 "Bad Request" "Bad Request"
                continue
            }
            $parts = $requestLine.Split(" ")
            if ($parts.Length -lt 2 -or ($parts[0] -ne "GET" -and $parts[0] -ne "HEAD")) {
                Send-TextResponse $stream 405 "Method Not Allowed" "Method Not Allowed"
                continue
            }
            $headOnly = $parts[0] -eq "HEAD"
            $urlPath = [Uri]::UnescapeDataString(($parts[1] -split "\?", 2)[0])
            if ($urlPath -eq "/_cfd_twin_health") {
                Send-TextResponse $stream 200 "OK" "YU_FLUID_VIEWER_OK" $headOnly
                continue
            }
            $relative = $urlPath.TrimStart("/").Replace("/", "\")
            $fullPath = [IO.Path]::GetFullPath((Join-Path $rootFull $relative))
            if ($fullPath -ne $rootFull -and -not $fullPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                Send-TextResponse $stream 403 "Forbidden" "Forbidden" $headOnly
                continue
            }
            if ([IO.Directory]::Exists($fullPath)) { $fullPath = Join-Path $fullPath "index.html" }
            if (-not [IO.File]::Exists($fullPath)) {
                Send-TextResponse $stream 404 "Not Found" "Not Found" $headOnly
                continue
            }
            $file = [IO.File]::OpenRead($fullPath)
            try {
                $contentType = Get-ContentType $fullPath
                $responseHeader = "HTTP/1.1 200 OK`r`nContent-Type: $contentType`r`nContent-Length: $($file.Length)`r`nCache-Control: no-cache`r`nConnection: close`r`n`r`n"
                $responseBytes = [Text.Encoding]::ASCII.GetBytes($responseHeader)
                $stream.Write($responseBytes, 0, $responseBytes.Length)
                if (-not $headOnly) { $file.CopyTo($stream) }
            }
            finally {
                $file.Dispose()
            }
        }
        catch {
            try { Send-TextResponse $stream 500 "Internal Server Error" "Internal Server Error" } catch { }
        }
        finally {
            $client.Close()
        }
    }
}
finally {
    $listener.Stop()
    Remove-Item -LiteralPath $PortFile -Force -ErrorAction SilentlyContinue
}
'''

WINDOWS_VIEWER_README = '''CFD 결과 뷰어 실행 방법

1. ZIP 파일 전체를 먼저 압축 해제합니다.
2. OPEN_3D_RESULTS.cmd 또는 바로열기_3D결과.cmd를 더블클릭합니다.
3. 브라우저에서 결과 화면이 자동으로 열립니다.

Python 설치는 필요하지 않습니다. Windows 기본 PowerShell 로컬 서버를 사용합니다.
results.html을 ZIP 내부나 file:// 주소로 직접 열지 마십시오.
대용량 계산 데이터와 브라우저 보안 정책 때문에 로컬 HTTP 서버가 필요합니다.
'''


def _initialize_publication(viewer: Path, source: Path, **run_options) -> None:
    """Build and validate the first cumulative viewer before exposing any files."""
    markers = ("results.html", "model.js", "four_case_manifest.json", "data")
    if any((viewer / name).exists() for name in markers):
        raise ValueError("Existing incomplete publication must be repaired before adding results")
    with tempfile.TemporaryDirectory(prefix=".first-viewer-", dir=viewer.parent) as temporary:
        staged = Path(temporary) / "viewer"
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns("data", ".release.lock"))
        model, _ = read_viewer_model(source)
        model["mesh_runs"] = []
        model.pop("archived_mesh_runs", None)
        model.pop("interactive_cfd", None)
        for key in list(model):
            if key.endswith("_by_mesh") or key == "mesh_wireframes":
                model[key] = {}
        write_four_case_model(staged, model)
        # append_validated_run checks source validation, datasets and geometry.
        append_validated_run(staged, source, **run_options)
        run_id = run_options["run_id"]
        model, _ = read_viewer_model(staged)
        model["interactive_cfd"] = model["interactive_cfd_by_mesh"][run_id]
        write_four_case_model(staged, model)
        cad = staged / "cad"
        cad.mkdir(exist_ok=True)
        reference = Path(__file__).with_name("assets") / "cad_reference"
        html = (reference / "index.html").read_text(encoding="utf-8")
        html = html.replace("cad_exact_phi23_", f"cad_{run_id}_")
        # Only show measured CAD projections when the source actually has them.
        for view in ("top_XY", "side_XZ"):
            name = f"actual_{run_id}_{view}.png"
            if (cad / name).is_file():
                html = html.replace(f"actual_phi23_{view}.png", name)
            else:
                html = re.sub(r"<details>.*?" + re.escape(f"actual_phi23_{view}.png") + r".*?</details>", "", html)
        (cad / "index.html").write_text(html, encoding="utf-8")
        shutil.copy2(reference / "style.css", cad / "style.css")
        i18n = Path(__file__).with_name("workflow_assets") / "i18n.js"
        shutil.copy2(i18n, staged / "i18n.js")
        results = staged / "results.html"
        results.write_text(results.read_text(encoding="utf-8").replace('href="cad/index.html" hidden', 'href="cad/index.html"'), encoding="utf-8")
        mesh = staged / "mesh.html"
        html = mesh.read_text(encoding="utf-8")
        html = html.replace('<script src="mesh-data.js"></script>', '<script src="four_case_data.js"></script>')
        html = html.replace('<script src="mesh-volume-data.js"></script>', '')
        if 'id="mesh-case-select"' not in html:
            html = html.replace('<h1>메시 보기</h1>', '<h1>메시 보기</h1><select id="mesh-case-select" aria-label="결과 선택"></select>')
        mesh.write_text(html, encoding="utf-8")
        validate_server_viewer(staged)
        # The release lock file stays in place on Windows. Publish the manifest
        # last; a missing manifest cannot be advertised as a validated result.
        items = sorted(staged.iterdir(), key=lambda item: item.name == "four_case_manifest.json")
        collisions = [item.name for item in items if (viewer / item.name).exists()]
        if collisions:
            raise ValueError(f"First publication would overwrite existing files: {collisions}")
        installed = []
        try:
            for item in items:
                destination = viewer / item.name
                os.replace(item, destination)
                installed.append(destination)
        except Exception:
            for destination in reversed(installed):
                os.replace(destination, staged / destination.name)
            raise


class _PageAssets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"script", "link", "img"}:
            return
        values = dict(attrs)
        path = values.get("src") or values.get("href")
        if path:
            self.paths.append(path)


def _local_file(viewer: Path, parent: Path, raw: str) -> Path | None:
    url = urlsplit(raw)
    if url.scheme or url.netloc or not url.path or url.path.startswith("/"):
        return None
    target = (parent / unquote(url.path)).resolve()
    if not target.is_relative_to(viewer):
        raise ValueError(f"viewer asset escapes publication: {raw}")
    return target


def validate_server_viewer(viewer_dir: str | Path) -> dict[str, object]:
    """Check active results, portable data, CAD, mesh and HTML dependencies."""
    viewer = Path(viewer_dir).resolve()
    required = ("index.html", "results.html", "mesh.html", "cad/index.html",
                "four_case_manifest.json", "four_case_data.js", "model.js")
    for name in required:
        if not (viewer / name).is_file():
            raise FileNotFoundError(viewer / name)
    model, _ = read_viewer_model(viewer)
    manifest = json.loads((viewer / "four_case_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("solver_validation_passed") is not True or manifest.get("energy_balance_passed") is not True:
        raise ValueError("published viewer is missing solver or energy-balance validation")
    runs = model.get("mesh_runs", [])
    model_ids = [str(run["id"]) for run in runs if run.get("data_ready")]
    manifest_ids = [str(run["id"]) for run in manifest.get("runs", []) if run.get("data_ready")]
    if not model_ids or model_ids != manifest_ids:
        raise ValueError(f"server result selection differs from portal: {model_ids} != {manifest_ids}")
    declared = {path.split("/", 2)[1] for path in dataset_paths(model) if path.count("/") >= 2}
    unexpected = declared - set(model_ids)
    if unexpected:
        raise ValueError(f"model references data for an inactive result: {sorted(unexpected)}")
    for raw in dataset_paths(model):
        target = _local_file(viewer, viewer, raw)
        if target is None or not target.is_file() or target.stat().st_size == 0:
            raise FileNotFoundError(viewer / raw)
    for run in runs:
        if not run.get("data_ready"):
            continue
        run_id = str(run["id"])
        if not re.fullmatch(r"[a-z][a-z0-9_]*", run_id):
            raise ValueError(f"unsafe run id: {run_id}")
        asset_id = str(run.get("asset_source_id") or run_id)
        if asset_id not in model_ids or not re.fullmatch(r"[a-z][a-z0-9_]*", asset_id):
            raise ValueError(f"invalid asset source for {run_id}: {asset_id}")
        if run.get("display_variant_of") and run["display_variant_of"] != asset_id:
            raise ValueError(f"display variant source differs from assets for {run_id}")
        for stem in ("mesh-data", "mesh-volume-data"):
            target = viewer / f"{stem}-{asset_id}.js"
            if not target.is_file() or target.stat().st_size == 0:
                raise FileNotFoundError(target)
        for view in ("top", "side"):
            for suffix in (".svg", ".en.svg"):
                target = viewer / "cad" / f"cad_{asset_id}_{view}{suffix}"
                if not target.is_file():
                    raise FileNotFoundError(target)
    for page in (viewer / "index.html", viewer / "results.html", viewer / "mesh.html", viewer / "cad/index.html"):
        assets = _PageAssets()
        assets.feed(page.read_text(encoding="utf-8"))
        for raw in assets.paths:
            target = _local_file(viewer, page.parent, raw)
            if target is not None and not target.is_file():
                raise FileNotFoundError(target)
    return {"runs": model_ids, "dataset_files": len(dataset_paths(model))}


def prune_old_viewer_assets(viewer_dir: str | Path) -> list[str]:
    """Remove only old content-hashed UI snapshots, never CFD payloads."""
    viewer = Path(viewer_dir).resolve()
    html = (viewer / "results.html").read_text(encoding="utf-8")
    active = set(re.findall(r'["\']([^"\']+\.(?:js|css))["\']', html))
    removed: list[str] = []
    for pattern in ("app.*.js", "model.*.js", "style.*.css"):
        for path in viewer.glob(pattern):
            if path.name in active:
                continue
            if not path.is_file():
                continue
            path.unlink()
            removed.append(path.name)
    return sorted(removed)


@contextmanager
def release_lock(viewer_dir: str | Path):
    viewer = Path(viewer_dir).resolve()
    viewer.mkdir(parents=True, exist_ok=True)
    with (viewer / ".release.lock").open("a+b") as lock:
        lock_file(lock, blocking=True)
        try:
            yield
        finally:
            unlock_file(lock)


def package_demo_viewer(viewer_dir: str | Path, zip_path: str | Path) -> dict[str, object]:
    """Atomically replace one ZIP after validating its exact server source."""
    viewer = Path(viewer_dir).resolve()
    destination = Path(zip_path).resolve()
    if destination.is_relative_to(viewer):
        raise ValueError("ZIP output must be outside the server viewer")
    report = validate_server_viewer(viewer)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    count = 0
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            archive.writestr(
                "index.html",
                '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=viewer/index.html">'
                '<a href="viewer/index.html">Open CFD viewer</a>\n',
            )
            archive.writestr("OPEN_3D_RESULTS.cmd", WINDOWS_VIEWER_LAUNCHER)
            archive.writestr("바로열기_3D결과.cmd", WINDOWS_VIEWER_LAUNCHER)
            archive.writestr("serve-viewer.ps1", WINDOWS_POWERSHELL_SERVER)
            archive.writestr("실행방법.txt", WINDOWS_VIEWER_README)
            for path in sorted(viewer.rglob("*")):
                if not path.is_file() or path.name.startswith(".") or path.name.endswith(".tmp"):
                    continue
                if path.is_symlink():
                    raise ValueError(f"viewer contains a symlink: {path}")
                archive.write(path, "viewer/" + path.relative_to(viewer).as_posix())
                count += 1
        with ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("ZIP integrity check failed")
            if "viewer/results.html" not in archive.namelist():
                raise ValueError("ZIP has no results page")
        os.replace(temporary, destination)
        os.chmod(destination, 0o644)
    finally:
        temporary.unlink(missing_ok=True)
    return {**report, "zip": str(destination), "files": count, "bytes": destination.stat().st_size}


def publish_experiment_and_demo(
    viewer_dir: str | Path,
    zip_path: str | Path,
    *,
    source_viewer: str | Path | None = None,
    run_id: str | None = None,
    label: str | None = None,
    label_en: str | None = None,
) -> dict[str, object]:
    """Synchronize one verified server publication and its offline demo."""
    viewer = Path(viewer_dir).resolve()
    with release_lock(viewer):
        if source_viewer is not None:
            if not run_id:
                raise ValueError("run_id is required when adding a source viewer")
            if not (viewer / "results.html").is_file():
                _initialize_publication(viewer, Path(source_viewer).resolve(), run_id=run_id, label=label, label_en=label_en)
            current, _ = read_viewer_model(viewer)
            if not any(str(run.get("id")) == run_id for run in current.get("mesh_runs", [])):
                append_validated_run(
                    viewer, Path(source_viewer), run_id=run_id, label=label, label_en=label_en
                )
        elif run_id is not None:
            raise ValueError("run_id requires a source viewer")
        report = validate_server_viewer(viewer)
        removed = prune_old_viewer_assets(viewer)
        packaged = package_demo_viewer(viewer, zip_path)
        return {**report, **packaged, "removed_old_assets": removed}
