$ErrorActionPreference = 'Stop'
$clientDir = (Resolve-Path $PSScriptRoot).Path
$buildEnv = Join-Path $clientDir '.build-venv'
$python = Join-Path $buildEnv 'Scripts\python.exe'

if (-not (Test-Path $python)) {
    python -m venv $buildEnv
}

Push-Location $clientDir
try {
    & $python -m pip install --disable-pip-version-check -r (Join-Path $clientDir 'requirements-build.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }

    & $python -m PyInstaller --noconfirm --clean (Join-Path $clientDir 'PrivateASR.spec') --distpath (Join-Path $clientDir 'dist') --workpath (Join-Path $clientDir 'build')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
} finally {
    Pop-Location
}

Write-Host "Built portable executable: $(Join-Path $clientDir 'dist\PrivateASR.exe')"
