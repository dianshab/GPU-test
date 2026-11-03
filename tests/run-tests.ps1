# Offline test suite for the remote Pearl miner controller (Windows host side).
#
# Usage:  powershell -ExecutionPolicy Bypass -File tests\run-tests.ps1

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$python = 'python'

Push-Location $root
try {
    Write-Host '== python syntax ==' -ForegroundColor Cyan
    & $python -m py_compile scripts\prl_miner.py tests\mock_krig.py tests\test_units.py tests\test_lifecycle.py
    if ($LASTEXITCODE -ne 0) { throw 'py_compile failed' }

    Write-Host '== unit tests ==' -ForegroundColor Cyan
    & $python -m unittest tests.test_units -v
    if ($LASTEXITCODE -ne 0) { throw 'unit tests failed' }

    Write-Host '== lifecycle tests (mock miner) ==' -ForegroundColor Cyan
    & $python -m unittest tests.test_lifecycle -v
    if ($LASTEXITCODE -ne 0) { throw 'lifecycle tests failed' }

    Write-Host '== PowerShell host reader tests ==' -ForegroundColor Cyan
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here 'test_host_status.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'host status tests failed' }

    Write-Host ''
    Write-Host 'ALL TESTS PASSED' -ForegroundColor Green
    $script:Failed = $false
} catch {
    Write-Host ('FAILED: ' + $_.Exception.Message) -ForegroundColor Red
    $script:Failed = $true
} finally {
    Pop-Location
}

if ($script:Failed) { exit 1 }
exit 0
