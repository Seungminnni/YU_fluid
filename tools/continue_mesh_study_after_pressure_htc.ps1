param(
    [int]$WaitForPid = 0
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$pressureBatch = Join-Path $repoRoot "build\experiments\rerun_missing_pressure_htc_20261007"
$meshBatch = Join-Path $repoRoot "build\experiments\phi19_mesh_independence_400_800_1000k_smooth_v5_20261007"
$statusPath = Join-Path $meshBatch "continuation_status.json"
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$runner = Join-Path $repoRoot "tools\run_experiment.py"
$releaseSync = Join-Path $repoRoot "tools\sync_viewer_release.py"
$releaseZip = Join-Path $repoRoot "distribution\YU_fluid_32_33_34mm_압력분포_속도압력범례통일_바로실행_20261006.zip"

function Write-ContinuationStatus([string]$state, [hashtable]$extra = @{}) {
    $payload = [ordered]@{
        status = $state
        updated_at = (Get-Date).ToString("o")
        wait_for_pid = $WaitForPid
        pressure_htc_batch = $pressureBatch
        mesh_batch = $meshBatch
    }
    foreach ($item in $extra.GetEnumerator()) {
        $payload[$item.Key] = $item.Value
    }
    $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
}

Set-Location -LiteralPath $repoRoot
Write-ContinuationStatus "waiting_for_pressure_htc_reruns"

if ($WaitForPid -gt 0) {
    Wait-Process -Id $WaitForPid -ErrorAction SilentlyContinue
}

$pressureStatusPath = Join-Path $pressureBatch "status.json"
$pressureStatus = if (Test-Path -LiteralPath $pressureStatusPath) {
    Get-Content -Raw -Encoding UTF8 -LiteralPath $pressureStatusPath | ConvertFrom-Json
} else {
    $null
}

Write-ContinuationStatus "starting_mesh_study_v5" @{
    pressure_htc_batch_status = if ($pressureStatus) { $pressureStatus.status } else { "unknown" }
}

& $python $runner --output $meshBatch --run
$meshExitCode = $LASTEXITCODE

# Publish only cases that passed the normal workflow gates. Failed attempts
# remain excluded by the viewer source and retirement rules.
Write-ContinuationStatus "packaging_verified_results" @{
    mesh_exit_code = $meshExitCode
}
& $python $releaseSync --demo-zip $releaseZip
$releaseExitCode = $LASTEXITCODE
$exitCode = if ($meshExitCode -ne 0) { $meshExitCode } else { $releaseExitCode }

Write-ContinuationStatus $(if ($exitCode -eq 0) { "complete" } else { "needs_attention" }) @{
    exit_code = $exitCode
    mesh_exit_code = $meshExitCode
    release_exit_code = $releaseExitCode
    release_zip = $releaseZip
}
exit $exitCode
