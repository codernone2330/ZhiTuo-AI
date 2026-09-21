[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repositoryRoot

$dockerExecutable = (Get-Command docker -ErrorAction SilentlyContinue).Source
if (-not $dockerExecutable) {
    $dockerExecutable = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"
}
if (-not (Test-Path -LiteralPath $dockerExecutable)) {
    throw "Docker CLI was not found, so the project containers cannot be stopped."
}

& $dockerExecutable compose down
if ($LASTEXITCODE -ne 0) {
    throw "Failed to stop containers. Make sure Docker Desktop is running."
}

Write-Host "ZhiTuo services stopped. PostgreSQL and Redis volumes were preserved." -ForegroundColor Green
