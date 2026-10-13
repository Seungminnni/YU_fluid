param(
    [int]$WaitForPid = 0
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$meshBatch = Join-Path $repoRoot "build\experiments\phi19_mesh_independence_400_800_1000k_smooth_v4_20261007"
$pressureBatch = Join-Path $repoRoot "build\experiments\rerun_missing_pressure_htc_20261007"
$statusPath = Join-Path $pressureBatch "continuation_status.json"
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$runner = Join-Path $repoRoot "tools\run_experiment.py"

function Write-ContinuationStatus([string]$state, [hashtable]$extra = @{}) {
    $payload = [ordered]@{
        status = $state
        updated_at = (Get-Date).ToString("o")
        wait_for_pid = $WaitForPid
        mesh_batch = $meshBatch
        pressure_htc_batch = $pressureBatch
    }
    foreach ($item in $extra.GetEnumerator()) {
        $payload[$item.Key] = $item.Value
    }
    $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
}

Set-Location -LiteralPath $repoRoot
Write-ContinuationStatus "waiting_for_mesh_study"

if ($WaitForPid -gt 0) {
    Wait-Process -Id $WaitForPid -ErrorAction SilentlyContinue
}

$meshStatusPath = Join-Path $meshBatch "status.json"
$meshStatus = if (Test-Path -LiteralPath $meshStatusPath) {
    Get-Content -Raw -Encoding UTF8 -LiteralPath $meshStatusPath | ConvertFrom-Json
} else {
    $null
}

Write-ContinuationStatus "starting_pressure_htc_reruns" @{
    mesh_batch_status = if ($meshStatus) { $meshStatus.status } else { "unknown" }
}

& $python $runner --output $pressureBatch --run
$exitCode = $LASTEXITCODE

Write-ContinuationStatus $(if ($exitCode -eq 0) { "complete" } else { "needs_attention" }) @{
    exit_code = $exitCode
}
exit $exitCode
