# Carga los dos modelos en LM Studio de forma explicita (no JIT) para que no se
# descarguen entre si. Con "unloadPreviousJITModelOnLoad" activo, LM Studio quita
# Gemma cada vez que se llama al modelo de decision y viceversa (~40 s por turno).
# Ademas fuerza Gemma al 100% en GPU (por defecto LM Studio la cargaba al ~19%).
#
# Uso:
#   powershell -ExecutionPolicy Bypass -File scripts\load-models.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\load-models.ps1 -DecisionModel jev-style-qwen3.5-2b-decision-v2
#
# Los identificadores deben coincidir con chat_model / decision_model de config.toml.
param(
    [string]$ChatModel = "google/gemma-4-12b-qat",
    # Vacío = no cargar modelo de decisión en LM Studio (StartLux va en su propio servidor:
    # scripts\start-startlux.ps1). Pon "jev-style-qwen3.5-2b-decision" si usas backend = "jev".
    [string]$DecisionModel = "",
    [string]$GemmaGpu = "max",
    [int]$GemmaContext = 8192,
    # 1 ranura: la app habla con un solo usuario. Con 4 (por defecto en LM Studio) Gemma ocupa
    # ~9 GB en vez de ~7 y no queda VRAM para StartLux.
    [int]$GemmaParallel = 1,
    [int]$DecisionContext = 2048
)

$lms = Join-Path $env:USERPROFILE ".lmstudio\bin\lms.exe"
if (-not (Test-Path $lms)) { $lms = "lms" }

function Invoke-Lms {
    # PowerShell 5.1 convierte el stderr de lms en errores; lo pasamos a texto plano.
    & $lms @args 2>&1 | ForEach-Object { "$_" } |
        Where-Object { $_.Trim() -and $_ -notmatch 'RemoteException|^Loading |\[\?25' } | Out-Host
}

$loaded = (& $lms ps 2>$null | Out-String)

$models = @()
if ($DecisionModel) { $models += @{ Key = $DecisionModel; Ctx = $DecisionContext; Gpu = "max" } }
$models += @{ Key = $ChatModel; Ctx = $GemmaContext; Gpu = $GemmaGpu; Parallel = $GemmaParallel }
foreach ($m in $models) {
    if ($loaded -match ("(?m)^" + [regex]::Escape($m.Key) + "\s")) {
        Write-Host "Descargando instancia previa de $($m.Key)..."
        Invoke-Lms unload $m.Key
    }
    Write-Host "Cargando $($m.Key) (contexto $($m.Ctx), GPU $($m.Gpu))..."
    $extra = @()
    if ($m.Parallel) { $extra = @("--parallel", $m.Parallel) }
    Invoke-Lms load $m.Key --identifier $m.Key -c $m.Ctx --gpu $m.Gpu @extra -y
}

Invoke-Lms ps
