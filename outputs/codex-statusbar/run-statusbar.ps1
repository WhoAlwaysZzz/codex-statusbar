$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Get-Command python -ErrorAction SilentlyContinue

if (-not $Python) {
    throw "Python was not found on PATH. Install Python 3 or run this with a Python-enabled shell."
}

& $Python.Source (Join-Path $ScriptDir "codex_statusbar.py") @args
