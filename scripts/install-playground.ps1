<#
.SYNOPSIS
    Copies the AI Chat Bridge Playground script into Warudo.

.DESCRIPTION
    Copies warudo\AIChatBridge.cs to <WarudoPath>\Warudo_Data\StreamingAssets\Playground\.
    Warudo hot-reloads Playground scripts, so no restart is needed: after copying,
    add the asset "AI Chat Bridge" in Warudo and select the character.

.PARAMETER WarudoPath
    Warudo install folder (the one containing Warudo.exe).
    Default: C:\Program Files (x86)\Steam\steamapps\common\Warudo

.EXAMPLE
    .\scripts\install-playground.ps1
    .\scripts\install-playground.ps1 -WarudoPath "C:\Program Files (x86)\Steam\steamapps\common\Warudo"
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$WarudoPath = "C:\Program Files (x86)\Steam\steamapps\common\Warudo"
)

$ErrorActionPreference = "Stop"

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
