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

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"),
        "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
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

function Test-DockerEngine {
    & $script:dockerExecutable info *> $null
    return $LASTEXITCODE -eq 0
}

Write-Host "[1/6] Checking Docker engine..." -ForegroundColor Cyan
if (-not (Test-DockerEngine)) {
    $desktopCandidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"),
        "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    )
    $desktop = $desktopCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($desktop) {
        Write-Host "Docker engine is stopped. Opening Docker Desktop and waiting for it..." -ForegroundColor Yellow
        Start-Process -FilePath $desktop -WindowStyle Normal
        for ($attempt = 1; $attempt -le 45; $attempt++) {
            Start-Sleep -Seconds 2
            if (Test-DockerEngine) { break }
        }
    }
    if (-not (Test-DockerEngine)) {
        throw "Docker engine is unavailable. If Docker Desktop reports a missing 'Docker Inc.\Docker Desktop' registry key, repair the per-user Docker Desktop installation first; the project cannot create containers until the engine starts. Existing database volumes must be backed up before any reinstall."
    }
}

Write-Host "[2/6] Preparing local configuration..." -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
    Write-Host "Created .env from .env.example. Replace local secrets before shared testing." -ForegroundColor Yellow
}

# Backward-compatible local secret loading: keep the Tencent key out of the image
# while allowing the existing map_and_company/TenCentApiKey.txt file to power the unified API.
if ([string]::IsNullOrWhiteSpace($env:TENCENT_MAP_KEY)) {
    $tencentKeyFile = Join-Path $repositoryRoot "map_and_company\TenCentApiKey.txt"
    if (Test-Path -LiteralPath $tencentKeyFile) {
        $tencentKey = (Get-Content -LiteralPath $tencentKeyFile -Raw).Trim()
        if (-not [string]::IsNullOrWhiteSpace($tencentKey)) {
            $env:TENCENT_MAP_KEY = $tencentKey
            Write-Host "Tencent Maps key loaded for the backend." -ForegroundColor Green
        }
    }
}

foreach ($requiredFile in @("backend\Dockerfile", "backend\pyproject.toml", "data\china-mobile-logo.svg")) {
    if (-not (Test-Path -LiteralPath $requiredFile)) {
        throw "Required project file is missing: $requiredFile"
    }
}
if (-not (Get-ChildItem -LiteralPath "data" -Filter "*.html" -File)) {
    throw "Frontend HTML is missing from the data directory."
}

Write-Host "[3/6] Validating Compose configuration..." -ForegroundColor Cyan
Invoke-Docker -Arguments @("compose", "config", "--quiet")

Write-Host "[4/6] Building and starting frontend, API, PostgreSQL, and Redis..." -ForegroundColor Cyan
$upArguments = @("compose", "up", "-d")
if (-not $NoBuild) {
    $upArguments += "--build"
}
Invoke-Docker -Arguments $upArguments

Write-Host "[5/6] Waiting for the API health check..." -ForegroundColor Cyan
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

Write-Host "[6/6] Checking frontend assets and integrated API routes..." -ForegroundColor Cyan
$frontendUrl = "http://127.0.0.1:8000/app/"
$frontendResponse = Invoke-WebRequest -Uri $frontendUrl -UseBasicParsing -TimeoutSec 10
if ($frontendResponse.StatusCode -ne 200 -or $frontendResponse.Content -notmatch "loginWithBackend") {
    throw "Frontend page is not the expected current version: $frontendUrl"
}
$logoResponse = Invoke-WebRequest -Uri ($frontendUrl + "china-mobile-logo.svg") -UseBasicParsing -TimeoutSec 10
if ($logoResponse.StatusCode -ne 200) {
    throw "Frontend logo is unavailable."
}
$openapi = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/openapi.json" -TimeoutSec 10
$availableRoutes = @($openapi.paths.PSObject.Properties.Name)
$requiredRoutes = @(
    "/api/v1/auth/login", "/api/v1/organizations/tree", "/api/v1/customers",
    "/api/v1/opportunities/refresh", "/api/v1/visits", "/api/v1/maps/visit-route",
    "/api/v1/ai/leads/capture", "/api/v1/ai/chat", "/api/v1/documents",
    "/api/v1/reports/weekly"
)
$missingRoutes = @($requiredRoutes | Where-Object { $availableRoutes -notcontains $_ })
if ($missingRoutes.Count -gt 0) {
    throw "Backend is healthy, but these frontend routes are missing: $($missingRoutes -join ', ')"
}

Invoke-Docker -Arguments @("compose", "ps")

Write-Host ""
Write-Host "ZhiTuo frontend and backend are ready." -ForegroundColor Green
Write-Host "Application: http://127.0.0.1:8000/app/"
Write-Host "API docs: http://127.0.0.1:8000/docs"
Write-Host "Health check: http://127.0.0.1:8000/api/v1/health/ready"
Write-Host "To stop the service, double-click stop.cmd."

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:8000/app/"
}
