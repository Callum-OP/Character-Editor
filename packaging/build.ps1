# Builds the Character Editor desktop app.
#
#   .\packaging\build.ps1
#
# Output: packaging\dist\CharacterEditor\CharacterEditor.exe  (portable folder
# app - zip it, ship it, or feed it to build-msix.ps1 for an installable
# store-ready package).
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $here "..\backend\.venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Backend venv not found - create it first (see README setup)."
}

& $python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('PyInstaller') else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyInstaller (build-time only)..."
    & $python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller install failed" }
}

Write-Host "Generating icon/logo assets..."
& $python (Join-Path $here "gen_assets.py")
if ($LASTEXITCODE -ne 0) { throw "asset generation failed" }

Write-Host "Building with PyInstaller..."
Push-Location $here
try {
    & $python -m PyInstaller --noconfirm --clean CharacterEditor.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
} finally {
    Pop-Location
}

$exe = Join-Path $here "dist\CharacterEditor\CharacterEditor.exe"
if (Test-Path $exe) {
    Write-Host ""
    Write-Host "Done: $exe"
    Write-Host "Double-click it to launch Character Editor in its own window."
} else {
    throw "Build finished but the exe is missing."
}
