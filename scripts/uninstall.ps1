<#
.SYNOPSIS
  Remove LabPilot-owned files only: skill copies it installed, its MCP registrations, its venv, shim and PATH entry.
  Lab folders, run workspaces (.labpilot/) and outputs are never touched.
#>
[CmdletBinding()]
param(
  [ValidateSet("project", "user")] [string]$Scope = "project",
  [string]$ProjectDir = (Get-Location).Path,
  [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "labpilot")
)
$ErrorActionPreference = "Stop"
$shim = Join-Path $InstallRoot "bin\labpilot.cmd"
if (Test-Path $shim) { & $shim uninstall --agent both --scope $Scope --project-dir $ProjectDir }

$marker = Join-Path $InstallRoot "path-added.txt"
if (Test-Path $marker) {
  $bin = (Get-Content $marker -Raw).Trim()
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $kept = ($userPath -split ";") | Where-Object { $_ -and $_ -ne $bin }
  [Environment]::SetEnvironmentVariable("Path", ($kept -join ";"), "User")
  Write-Host "Removed $bin from user PATH"
}
foreach ($p in "venv", "bin", "mnt", "path-added.txt") {
  $target = Join-Path $InstallRoot $p
  if (Test-Path $target) { Remove-Item -Recurse -Force -Confirm:$false $target; Write-Host "Removed $target" }
}
Write-Host "LabPilot removed. Your lab folders and outputs were left untouched."
