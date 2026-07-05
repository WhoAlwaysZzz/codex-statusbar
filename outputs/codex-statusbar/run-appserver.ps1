$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = "python"

if ($args.Count -eq 0) {
  Write-Host "Usage: .\run-appserver.ps1 <prompt> [--max-recoveries 2]"
  exit 2
}

& $Python "$ScriptDir\codex_appserver.py" @args
exit $LASTEXITCODE
