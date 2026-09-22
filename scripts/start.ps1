[CmdletBinding()]
param(
    [switch]$NoBuild,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repositoryRoot

function Resolve-DockerExecutable {
    $command = Get-Command docker -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $fallback = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"
    if (Test-Path -LiteralPath $fallback) {
        return $fallback
    }

    throw "Docker CLI was not found. Install and start Docker Desktop, then run start.cmd again."
}

function Invoke-Docker {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    & $script:dockerExecutable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed: docker $($Arguments -join ' ')"
    }
}

$script:dockerExecutable = Resolve-DockerExecutable

Write-Host "[1/5] Checking Docker engine..." -ForegroundColor Cyan
& $script:dockerExecutable info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is installed, but its engine is not running."
}

Write-Host "[2/5] Preparing local configuration..." -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
    Write-Host "Created .env from .env.example. Replace local secrets before shared testing." -ForegroundColor Yellow
}

# Backward-compatible local secret loading: keep the Tencent key out of the image
# while allowing the existing data/TenCentApiKey.txt file to power the unified API.
if ([string]::IsNullOrWhiteSpace($env:TENCENT_MAP_KEY)) {
    $tencentKeyFile = Join-Path $repositoryRoot "data\TenCentApiKey.txt"
    if (Test-Path -LiteralPath $tencentKeyFile) {
        $tencentKey = (Get-Content -LiteralPath $tencentKeyFile -Raw).Trim()
        if (-not [string]::IsNullOrWhiteSpace($tencentKey)) {
            $env:TENCENT_MAP_KEY = $tencentKey
            Write-Host "Tencent Maps key loaded for the backend." -ForegroundColor Green
        }
    }
}

Write-Host "[3/5] Validating Compose configuration..." -ForegroundColor Cyan
Invoke-Docker -Arguments @("compose", "config", "--quiet")

Write-Host "[4/5] Starting API, PostgreSQL, and Redis..." -ForegroundColor Cyan
$upArguments = @("compose", "up", "-d")
if (-not $NoBuild) {
    $upArguments += "--build"
}
Invoke-Docker -Arguments $upArguments

Write-Host "[5/5] Waiting for the API health check..." -ForegroundColor Cyan
$healthUrl = "http://127.0.0.1:8000/api/v1/health/ready"
$ready = $false
for ($attempt = 1; $attempt -le 30; $attempt++) {
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
        if ($response.success -and $response.data.status -eq "ok") {
            $ready = $true
            break
        }
    }
    catch {
        Start-Sleep -Seconds 2
    }
}

if (-not $ready) {
    & $script:dockerExecutable compose ps
    & $script:dockerExecutable compose logs --tail 80 api
    throw "The API did not become healthy within 60 seconds. Review the container logs above."
}

Invoke-Docker -Arguments @("compose", "ps")

Write-Host ""
Write-Host "ZhiTuo backend is ready." -ForegroundColor Green
Write-Host "Application: http://127.0.0.1:8000/app/"
Write-Host "API docs: http://127.0.0.1:8000/docs"
Write-Host "Health check: http://127.0.0.1:8000/api/v1/health/ready"
Write-Host "To stop the service, double-click stop.cmd."

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:8000/app/"
}
