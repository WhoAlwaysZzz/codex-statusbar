[CmdletBinding(SupportsShouldProcess)]
param(
    [switch]$RemovePath,
    [switch]$EnableAutostart
)

$ErrorActionPreference = "Stop"

if ($RemovePath -and $EnableAutostart) {
    throw "-RemovePath and -EnableAutostart cannot be used together."
}

$ScriptDir = (Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$StatusbarLauncher = Join-Path $ScriptDir "codex-stat.exe"
$WatchdogLauncher = Join-Path $ScriptDir "codex-watchdog.exe"

foreach ($launcher in @($StatusbarLauncher, $WatchdogLauncher)) {
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "Required launcher was not found: $launcher"
    }
}

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathEntries = @(
    $userPath -split ";" |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
)
if ($RemovePath) {
    $updatedEntries = @(
        $pathEntries | Where-Object {
            -not [string]::Equals($_.TrimEnd("\\"), $ScriptDir.TrimEnd("\\"), [StringComparison]::OrdinalIgnoreCase)
        }
    )
    $action = "Remove Codex Statusbar from"
} else {
    $updatedEntries = @($ScriptDir) + @(
        $pathEntries | Where-Object {
            -not [string]::Equals($_.TrimEnd("\\"), $ScriptDir.TrimEnd("\\"), [StringComparison]::OrdinalIgnoreCase)
        }
    )
    $action = "Add Codex Statusbar to"
}

$updatedPath = $updatedEntries -join ";"
$pathUpdated = $false
if ($updatedPath -ne $userPath) {
    if ($PSCmdlet.ShouldProcess("current user PATH", $action)) {
        [Environment]::SetEnvironmentVariable("Path", $updatedPath, "User")
        if (-not $RemovePath) {
            $env:Path = $ScriptDir + ";" + $env:Path
        }
        $pathUpdated = $true
        Write-Output "Updated current user PATH."
    }
} else {
    Write-Output "Current user PATH is already up to date."
}

if ($EnableAutostart) {
    if ($PSCmdlet.ShouldProcess("Windows Startup folder", "Enable Codex Statusbar autostart")) {
        & $StatusbarLauncher --enable-autostart
        if ($LASTEXITCODE -ne 0) {
            throw "Could not enable Codex Statusbar autostart."
        }
        Write-Output "Enabled Codex Statusbar autostart."
    }
}

if ($RemovePath) {
    if ($pathUpdated) {
        Write-Output "Removed this installation directory from the current user PATH."
    } elseif ($WhatIfPreference) {
        Write-Output "PATH was not changed because -WhatIf was used."
    }
} else {
    if ($pathUpdated) {
        Write-Output "Open a new terminal and run: Start-Process codex-stat"
    } elseif ($WhatIfPreference) {
        Write-Output "PATH was not changed because -WhatIf was used."
    }
}
