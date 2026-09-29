param([string]$Uv = 'uv')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$testProject = 'signaldesk-test-' + [guid]::NewGuid().ToString('N').Substring(0, 10)
$testCompose = Join-Path $taskRoot 'infra/compose.test.yaml'
$previousTestUrl = $env:TEST_DATABASE_URL
try {
    & docker compose -f $testCompose -p $testProject up -d --wait --wait-timeout 90
    if ($LASTEXITCODE -ne 0) { throw 'Disposable PostgreSQL failed to start.' }
    $env:TEST_DATABASE_URL = 'postgresql://signaldesk:signaldesk-test-only@127.0.0.1:5434/signaldesk_test'
    Push-Location (Join-Path $taskRoot 'backend')
    try {
        & $Uv sync --frozen
        if ($LASTEXITCODE -ne 0) { throw 'Locked backend install failed.' }
        & $Uv run --frozen pytest
        if ($LASTEXITCODE -ne 0) { throw 'Backend tests failed.' }
    } finally { Pop-Location }
} finally {
    $env:TEST_DATABASE_URL = $previousTestUrl
    # Only this invocation's unique test project; normal local volumes are untouched.
    & docker compose -f $testCompose -p $testProject down
    if ($LASTEXITCODE -ne 0) { Write-Warning "Cleanup failed for disposable project $testProject." }
}
