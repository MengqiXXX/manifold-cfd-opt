$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot ".."))
Set-Location $root

$venv = Join-Path $root ".venv"
$py = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $py)) {
  py -m venv .venv
}

& $py -m pip install -U pip
& $py -m pip install -r monitor\requirements.txt

if (-not $env:VORTEX_ROOT) {
  $env:VORTEX_ROOT = $root.Path
}

if (-not $env:MONITOR_HOST) { $env:MONITOR_HOST = "127.0.0.1" }
if (-not $env:MONITOR_PORT) { $env:MONITOR_PORT = "8090" }

if (-not $env:PARAVIEW_PVPYTHON) {
  $candidate = Get-ChildItem "C:\Program Files\ParaView*" -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { Join-Path $_.FullName "bin\pvpython.exe" } |
    Where-Object { Test-Path $_ } |
    Select-Object -First 1

  if ($candidate) {
    $env:PARAVIEW_PVPYTHON = $candidate
  }
}

Write-Host "VORTEX_ROOT: $env:VORTEX_ROOT"
Write-Host "Monitor: http://$($env:MONITOR_HOST):$($env:MONITOR_PORT)/"
if ($env:PARAVIEW_PVPYTHON) {
  Write-Host "ParaView pvpython: $env:PARAVIEW_PVPYTHON"
} else {
  Write-Host "ParaView pvpython: (not set)"
}

& $py "monitor\run_server.py"
