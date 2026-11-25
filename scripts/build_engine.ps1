# Build the receiver engine (UxPlay + Ipon Mirror patch). Installs MSYS2 with winget if missing.
# (Keep this file ASCII-only: Windows PowerShell 5.1 misreads UTF-8 files without a BOM.)
$ErrorActionPreference = "Continue"   # native tools write progress to stderr; check exit codes instead
$msys = "C:\msys64\usr\bin\bash.exe"
if (-not (Test-Path $msys)) {
    winget install --id MSYS2.MSYS2 -e --accept-source-agreements --accept-package-agreements --silent
}
$env:MSYSTEM = "UCRT64"
$env:CHERE_INVOKING = "1"
& $msys -lc "pacman -Syu --noconfirm" | Out-Host
& $msys -lc "pacman -Syu --noconfirm" | Out-Host   # run twice in case the core packages were updated
Push-Location $PSScriptRoot   # with CHERE_INVOKING=1, bash starts in this folder
try { & $msys -lc "bash ./build_engine.sh" } finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { throw "engine build failed" }
