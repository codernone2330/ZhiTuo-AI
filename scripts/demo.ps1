[CmdletBinding()]
param(
    [switch]$Full,
    [switch]$NoBrowser,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

$mapPort = 8767
$qccPort = 8768
$mapHealthUrl = "http://127.0.0.1:$mapPort/api/health"
$qccHealthUrl = "http://127.0.0.1:$qccPort/api/health"
$mapUrl = "http://127.0.0.1:$mapPort/"
$qccUrl = "http://127.0.0.1:$qccPort/"

function Get-PythonPath {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "Python not found. Install Python 3 and add it to PATH."
}

function Stop-Demo {
    $targets = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "map_integration\.py|company\.py" }
    if (-not $targets) {
        Write-Host "No demo service is running." -ForegroundColor Yellow
        return
    }
    foreach ($t in $targets) {
        try {
            Stop-Process -Id $t.ProcessId -Force
            Write-Host ("Stopped PID {0}" -f $t.ProcessId) -ForegroundColor Green
        }
        catch {
            Write-Host ("Could not stop PID {0}" -f $t.ProcessId) -ForegroundColor Yellow
        }
    }
    Write-Host "Demo services stopped." -ForegroundColor Green
}

function Wait-Health([string]$Url, [int]$TimeoutSec = 30) {
    for ($i = 0; $i -lt $TimeoutSec; $i++) {
        try {
            $res = Invoke-RestMethod -Uri $Url -TimeoutSec 2
            if ($res.ok) { return $res }
        }
        catch { }
        Start-Sleep -Seconds 1
    }
    return $null
}

if ($Stop) {
    Stop-Demo
    exit 0
}

# Full mode: hand over to the Docker-based full system (start.cmd must be at project root).
if ($Full) {
    $startCmd = Join-Path $root "start.cmd"
    if (-not (Test-Path -LiteralPath $startCmd)) {
        throw "start.cmd not found at project root. Full mode requires Docker Desktop and start.cmd."
    }
    Write-Host "Starting the FULL system via start.cmd (requires Docker Desktop)..." -ForegroundColor Cyan
    & $startCmd
    exit $LASTEXITCODE
}

$python = Get-PythonPath

$hasQcc = (Test-Path -LiteralPath ".env") -or (Test-Path -LiteralPath "map_and_company\QccAppKey.txt")
if (-not $hasQcc) {
    Write-Host "WARNING: QCC credentials not found (.env or map_and_company\QccAppKey.txt); company search will be unavailable." -ForegroundColor Yellow
}
$hasTencent = (Test-Path -LiteralPath "map_and_company\TenCentApiKey.txt") -or ($env:TENCENT_MAP_KEY)
if (-not $hasTencent) {
    Write-Host "WARNING: Tencent map key not found; the map page will report an error." -ForegroundColor Yellow
}

Write-Host "[1/3] Starting the Map tool page (port $mapPort) ..." -ForegroundColor Cyan
Start-Process -FilePath $python -ArgumentList @("map_and_company\map_integration.py") -WorkingDirectory $root

Write-Host "[2/3] Starting the QCC tool page (port $qccPort) ..." -ForegroundColor Cyan
Start-Process -FilePath $python -ArgumentList @("map_and_company\company.py") -WorkingDirectory $root

Write-Host "[3/3] Waiting for services to become ready ..." -ForegroundColor Cyan
$map = Wait-Health $mapHealthUrl 30
$qcc = Wait-Health $qccHealthUrl 30

if ($map) {
    Write-Host ("  Map   OK   keyConfigured={0}   {1}" -f $map.keyConfigured, $mapUrl) -ForegroundColor Green
}
else {
    Write-Host ("  Map   FAILED to start (is port {0} free?)" -f $mapPort) -ForegroundColor Red
}

if ($qcc) {
    Write-Host ("  QCC   OK   keyConfigured={0}   {1}" -f $qcc.keyConfigured, $qccUrl) -ForegroundColor Green
}
else {
    Write-Host ("  QCC   FAILED to start (is port {0} free?)" -f $qccPort) -ForegroundColor Red
}

if (-not $NoBrowser) {
    if ($map) { Start-Process $mapUrl }
    if ($qcc) { Start-Process $qccUrl }
}

Write-Host ""
Write-Host "How to see the real effect:" -ForegroundColor Cyan
Write-Host "  Map page : fill the stations box -> click 'Generate real visit route'."
Write-Host "  QCC page : type a keyword -> click 'Search & score'."
Write-Host "  Note     : the first QCC search spends quota; repeating the same keyword uses the local cache for free."
Write-Host ""
Write-Host "To stop: run  scripts\demo.ps1 -Stop   (or close the two console windows)." -ForegroundColor Cyan
