# Arranca el modelo de decisión StartLux (con visión) con su servidor oficial:
#   llama-server (puerto 8081)  <-  startlux-decide serve (puerto 8090, API /v1/systemone)
#
# Uso:  powershell -ExecutionPolicy Bypass -File scripts\start-startlux.ps1 [-MmprojOnCpu] [-GpuLayers 99]
#
# Notas:
# - Se usa un llama-server SIN parchear en modo "server": una sola ranura, sin checkpoints de
#   contexto y un micro-batch que contiene el prompt entero (así el prompt se lee de forma
#   bidireccional, como se entrenó el modelo).
# - -c/-b/-ub 4096 en vez de 20480 para que quepa en 12 GB junto a Gemma y Warudo. Una imagen
#   cuesta como máximo 1024 tokens, así que el prompt cabe de sobra.
# - Las DLL de CUDA 12 se toman de LM Studio (no hace falta descargar cudart).
param(
    # Carpeta con el GGUF y el mmproj. Vacío = la carpeta de modelos de LM Studio
    # ("downloadsFolder" de ~\.lmstudio\settings.json) + startlux-models\...
    [string]$ModelDir = "",
    [string]$Gguf = "StartLux-Decision-v1.1-4B-Instant-Q4_K_M.gguf",
    [string]$Mmproj = "mmproj-StartLux-Decision-v1.1-4B-Instant-F16.gguf",
    [int]$GpuLayers = 99,
    # Cada pregunta relee el prompt entero (estado + 1 captura de ~600 tokens): 2048 sobra y
    # reduce el buffer de cómputo para que quepa junto a Gemma y Warudo en 12 GB.
    [int]$Context = 2048,
    [switch]$MmprojOnCpu,
    [int]$LlamaPort = 8081,
    [int]$DecidePort = 8090
)

if (-not $ModelDir) {
    $lmModels = Join-Path $env:USERPROFILE ".lmstudio\models"
    $lmSettings = Join-Path $env:USERPROFILE ".lmstudio\settings.json"
    if (Test-Path $lmSettings) {
        try {
            $folder = (Get-Content -Raw $lmSettings | ConvertFrom-Json).downloadsFolder
            if ($folder) { $lmModels = $folder }
        } catch {}
    }
    $ModelDir = Join-Path $lmModels "startlux-models\StartLux-Decision-v1.1-4B-Instant-GGUF"
}
if (-not (Test-Path (Join-Path $ModelDir $Gguf))) {
    Write-Warning "No encuentro $Gguf en '$ModelDir'. Pasa la carpeta con -ModelDir."
    exit 1
}

$root = Split-Path $PSScriptRoot -Parent
$server = Join-Path $root "tools\llama.cpp\llama-server.exe"
$config = Join-Path $root "models\startlux-4b-instant"
$logs = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force $logs | Out-Null

$cudaDlls = Join-Path $env:USERPROFILE ".lmstudio\extensions\backends\vendor\win-llama-cuda12-vendor-v2"
if (Test-Path $cudaDlls) { $env:PATH = "$cudaDlls;$env:PATH" }

$llamaArgs = @(
    "-m", (Join-Path $ModelDir $Gguf),
    "--mmproj", (Join-Path $ModelDir $Mmproj),
    "-ngl", $GpuLayers, "-fa", "on",
    "--parallel", "1", "-c", $Context, "-b", $Context, "-ub", $Context,
    "--ctx-checkpoints", "0", "--cache-ram", "0",
    "--image-min-tokens", "64", "--image-max-tokens", "1024",
    "--host", "127.0.0.1", "--port", $LlamaPort
)
if ($MmprojOnCpu) { $llamaArgs += "--no-mmproj-offload" }

Write-Host "Arrancando llama-server en :$LlamaPort ..."
$llama = Start-Process -FilePath $server -ArgumentList $llamaArgs -PassThru -NoNewWindow `
    -RedirectStandardOutput (Join-Path $logs "llama-server.out.log") `
    -RedirectStandardError (Join-Path $logs "llama-server.err.log")

# Esperar a que el modelo esté cargado
$ok = $false
for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Milliseconds 500
    if ($llama.HasExited) { break }
    try { if ((Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$LlamaPort/health" -TimeoutSec 2).StatusCode -eq 200) { $ok = $true; break } } catch {}
}
if (-not $ok) {
    Write-Warning "llama-server no arrancó. Mira $logs\llama-server.err.log"
    if (-not $llama.HasExited) { Stop-Process -Id $llama.Id -Force }
    exit 1
}

Write-Host "Arrancando startlux-decide en :$DecidePort ..."
$env:STARTLUX_LLAMACPP_BIDIR = "server"
Push-Location $root
uv run startlux-decide serve --model $config --backend llamacpp --llamacpp-url "http://127.0.0.1:$LlamaPort" --port $DecidePort
Pop-Location
Stop-Process -Id $llama.Id -Force -ErrorAction SilentlyContinue
