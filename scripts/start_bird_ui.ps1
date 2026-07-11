<#!
.SYNOPSIS
Starts the local BIRD NL2SQL UI and its API server.

.DESCRIPTION
Uses the project's Python 3.11 virtual environment when available, otherwise
falls back to the Windows Python launcher. The UI is served at
http://127.0.0.1:8765.
#>

[CmdletBinding()]
param(
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$serverScript = Join-Path $PSScriptRoot 'nl2sql_api.py'
$birdDatabaseRoot = Join-Path $repoRoot 'bird_bench\dev\dev_20240627\databases\dev_databases'
$url = "http://127.0.0.1:$Port"

function Test-BirdApi {
    try {
        $response = Invoke-WebRequest -Uri "$url/api/databases" -UseBasicParsing -TimeoutSec 1
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $serverScript)) {
    throw "Could not find the BIRD UI API server: $serverScript"
}

if (-not (Test-Path -LiteralPath $birdDatabaseRoot)) {
    Write-Warning "BIRD SQLite databases were not found at $birdDatabaseRoot. The UI will start, but no databases will be available."
}

if (Test-BirdApi) {
    Write-Host "BIRD UI is already running at $url."
} else {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) {
        throw "Port $Port is already in use, but it is not serving the BIRD API. Stop the process using it and run this script again."
    } else {
        $venvPython = Join-Path $repoRoot '.venv311\Scripts\python.exe'
        if (Test-Path -LiteralPath $venvPython) {
            $process = Start-Process -FilePath $venvPython -ArgumentList '-u', $serverScript -WorkingDirectory $repoRoot -PassThru
        } else {
            $process = Start-Process -FilePath 'py.exe' -ArgumentList '-3.11', '-u', $serverScript -WorkingDirectory $repoRoot -PassThru
        }
    }

    $ready = $false
    for ($attempt = 1; $attempt -le 60; $attempt++) {
        Start-Sleep -Milliseconds 250
        if (Test-BirdApi) {
            $ready = $true
            break
        }
    }

    if (-not $ready) {
        throw "The BIRD API server did not start on port $Port. Process ID: $($process.Id)"
    }

    Write-Host "Started BIRD UI API (PID $($process.Id)) at $url"
}

if (-not $NoBrowser) {
    Start-Process $url
}
