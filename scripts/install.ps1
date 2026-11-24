<#
.SYNOPSIS
  Install LabPilot for the current user (no admin rights, no global changes without explicit switches).

.DESCRIPTION
  - Finds Python 3.11+ (py launcher or python on PATH).
  - Creates an isolated environment in %LOCALAPPDATA%\labpilot\venv (rerun = upgrade).
  - Installs LabPilot from this repository with the [lab] extras (pandas, matplotlib, pyarrow).
  - Writes a small labpilot.cmd shim to %LOCALAPPDATA%\labpilot\bin.
  - Optionally downloads Playwright's Chromium (needed for screenshots) and adds the shim folder to
    your *user* PATH (only with -AddToPath).
  - Optionally installs the LabPilot skill for Codex/Claude Code (labpilot setup).

.EXAMPLE
  .\scripts\install.ps1 -Agent codex -ProjectDir C:\Labs -InstallBrowser -AddToPath
#>
[CmdletBinding()]
param(
  [ValidateSet("codex", "claude", "both", "none")] [string]$Agent = "codex",
  [ValidateSet("project", "user")] [string]$Scope = "project",
  [string]$ProjectDir = (Get-Location).Path,
  [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "labpilot"),
  [switch]$InstallBrowser,
  [switch]$AddToPath,
  [switch]$Yes
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot

function Find-Python {
  $candidates = @()
  if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in "3.13", "3.12", "3.11") { $candidates += ,@("py", "-$v") }
  }
  if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += ,@("python") }
  foreach ($c in $candidates) {
    $exe = $c[0]; $pyArgs = @(); if ($c.Count -gt 1) { $pyArgs = $c[1..($c.Count - 1)] }
    try {
      $out = & $exe @pyArgs -c "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.executable)" 2>$null
      if ($LASTEXITCODE -eq 0 -and $out) {
        $ver = [version]$out[0]
        if ($ver -ge [version]"3.11") { return $out[1] }
      }
    } catch { }
  }
  return $null
}

Write-Host "LabPilot installer (repository: $Repo)"
$py = Find-Python
if (-not $py) {
  Write-Host "Python 3.11+ was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and rerun." -ForegroundColor Red
  exit 3
}
Write-Host "Using Python: $py"

$venv = Join-Path $InstallRoot "venv"
$venvPy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  Write-Host "Creating isolated environment: $venv"
  & $py -m venv $venv
  if ($LASTEXITCODE -ne 0) { Write-Host "venv creation failed" -ForegroundColor Red; exit 3 }
}
Write-Host "Installing LabPilot and dependencies (this can take a few minutes)..."
& $venvPy -m pip install --disable-pip-version-check --upgrade pip | Out-Null
& $venvPy -m pip install --disable-pip-version-check --upgrade "$Repo[lab]"
if ($LASTEXITCODE -ne 0) { Write-Host "pip install failed" -ForegroundColor Red; exit 3 }

$bin = Join-Path $InstallRoot "bin"
New-Item -ItemType Directory -Force $bin | Out-Null
$shim = Join-Path $bin "labpilot.cmd"
Set-Content -Path $shim -Encoding ascii -Value "@echo off`r`n`"$venvPy`" -m labpilot %*"
Write-Host "Command shim: $shim"

$doBrowser = $InstallBrowser
if (-not $doBrowser -and -not $Yes) {
  $ans = Read-Host "Download Playwright's Chromium (~150 MB, needed for screenshots)? [Y/n]"
  $doBrowser = ($ans -eq "" -or $ans -match "^[Yy]")
}
if ($doBrowser) { & $venvPy -m playwright install chromium }

if ($AddToPath) {
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if (($userPath -split ";") -notcontains $bin) {
    [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $bin), "User")
    Set-Content -Path (Join-Path $InstallRoot "path-added.txt") -Value $bin
    Write-Host "Added $bin to your user PATH (restart terminals / VS Code to pick it up)."
  }
} else {
  Write-Host "Not changing PATH. Either rerun with -AddToPath, or call: $shim"
}

if ($Agent -ne "none") {
  & $shim setup --agent $Agent --scope $Scope --project-dir $ProjectDir
}
& $shim doctor
Write-Host "`nDone. In VS Code, open your lab folder and ask your agent:" -ForegroundColor Green
Write-Host "  Use LabPilot to work through the lab in this folder, run the code, collect screenshots, and prepare the original Word document with answers for my review."
