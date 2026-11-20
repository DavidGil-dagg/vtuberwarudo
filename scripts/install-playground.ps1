<#
.SYNOPSIS
    Copies the AI Chat Bridge Playground script into Warudo.

.DESCRIPTION
    Copies warudo\AIChatBridge.cs to <WarudoPath>\Warudo_Data\StreamingAssets\Playground\.
    Warudo hot-reloads Playground scripts, so no restart is needed: after copying,
    add the asset "AI Chat Bridge" in Warudo and select the character.

.PARAMETER WarudoPath
    Warudo install folder (the one containing Warudo.exe).
    Default: searched in every Steam library (registry SteamPath + libraryfolders.vdf).

.EXAMPLE
    .\scripts\install-playground.ps1
    .\scripts\install-playground.ps1 -WarudoPath "C:\Program Files (x86)\Steam\steamapps\common\Warudo"
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$WarudoPath = ""
)

$ErrorActionPreference = "Stop"

function Find-Warudo {
    $steam = (Get-ItemProperty -Path HKCU:\Software\Valve\Steam -ErrorAction SilentlyContinue).SteamPath
    if (-not $steam) { return $null }
    $libraries = @($steam)
    $vdf = Join-Path $steam "steamapps\libraryfolders.vdf"
    if (Test-Path -LiteralPath $vdf) {
        foreach ($m in [regex]::Matches((Get-Content -Raw -LiteralPath $vdf), '"path"\s+"([^"]+)"')) {
            $libraries += $m.Groups[1].Value -replace '\\\\', '\'
        }
    }
    foreach ($lib in $libraries | Select-Object -Unique) {
        $candidate = Join-Path $lib "steamapps\common\Warudo"
        if (Test-Path -LiteralPath (Join-Path $candidate "Warudo_Data")) { return $candidate }
    }
    return $null
}

if (-not $WarudoPath) {
    $WarudoPath = Find-Warudo
    if (-not $WarudoPath) { throw "Warudo not found in your Steam libraries. Pass -WarudoPath with your Warudo install folder." }
    Write-Host "Warudo: $WarudoPath"
}

$source = Join-Path $PSScriptRoot "..\warudo\AIChatBridge.cs" | Resolve-Path
$dataDir = Join-Path $WarudoPath "Warudo_Data"
if (-not (Test-Path -LiteralPath $dataDir -PathType Container)) {
    throw "Warudo_Data not found under '$WarudoPath'. Pass -WarudoPath with your Warudo install folder."
}

$playground = Join-Path $dataDir "StreamingAssets\Playground"
if (-not (Test-Path -LiteralPath $playground -PathType Container)) {
    if ($PSCmdlet.ShouldProcess($playground, "Create directory")) {
        New-Item -ItemType Directory -Path $playground | Out-Null
    }
}

$destination = Join-Path $playground "AIChatBridge.cs"
if ($PSCmdlet.ShouldProcess($destination, "Copy $source")) {
    Copy-Item -LiteralPath $source -Destination $destination -Force
    Write-Host "Installed: $destination"
    Write-Host "In Warudo: Assets > + > 'AI Chat Bridge', select the Character. Server: ws://127.0.0.1:19500"
}
