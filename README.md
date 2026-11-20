# VtuberWarudo

Personaje VTuber de [Warudo](https://warudo.app) que conversa contigo y reacciona con el cuerpo, usando solo IA local.

- **Chat:** escribes en una web local y responde **Gemma 4 12B** (LM Studio), orquestada con **LangChain Deep Agents**. Tiene memoria del usuario y herramientas para hacer gestos cuando se los pides.
- **Animación:** el modelo de decisión **StartLux Decision v1.1 4B Instant** elige el estilo al hablar, la emoción, el gesto y la pose de mano de cada respuesta. Todo ocurre mientras el texto aparece a ritmo de habla, con las animaciones encadenadas sin cortes.
- **Modo visión:** el personaje reacciona con el cuerpo a lo que pasa en tu pantalla y a lo que dices por el micrófono (Whisper local).
- **Puente con Warudo:** un script C# de Playground dentro de Warudo (**AI Chat Bridge**) recibe las órdenes por WebSocket y mueve al personaje.

Todo funciona sin conexión, en una sola PC con una GPU de 12 GB.

---

## Índice

1. [Arquitectura](#arquitectura)
2. [Requisitos](#requisitos)
3. [Instalación](#instalación)
4. [Arranque diario](#arranque-diario)
5. [Uso](#uso)
6. [Configuración (`config.toml`)](#configuración-configtoml)
7. [Cómo funciona por dentro](#cómo-funciona-por-dentro)
8. [Estructura del proyecto](#estructura-del-proyecto)
9. [Herramientas de línea de comandos](#herramientas-de-línea-de-comandos)
10. [Tests](#tests)
11. [Solución de problemas](#solución-de-problemas)
12. [Limitaciones conocidas](#limitaciones-conocidas)
13. [Licencias de los modelos](#licencias-de-los-modelos)

---

## Arquitectura

```
                       ┌────────────────────────── PC local ──────────────────────────┐
 Navegador (chat) ⇄ WS │ FastAPI :8000 (vtuber-chat)                                   │
 http://localhost:8000 │   ├─ Deep Agent (LangGraph) ──► LM Studio :1234  Gemma 4 12B  │
                       │   │     memoria: data/memories/AGENTS.md                       │
                       │   │     historial: data/checkpoints.sqlite                     │
                       │   ├─ ReactionMiddleware ──► startlux-decide :8090 ──► llama-server :8081
                       │   │                          (StartLux 4B + visión)            │
                       │   ├─ SpeechPacer (texto a ritmo de habla)                       │
                       │   ├─ AnimationDirector (cola / coreografía de clips)            │
                       │   ├─ VisionMode: captura de pantalla + Whisper (CPU, micro)     │
                       │   └─ WarudoClient ──WS :19500──► Warudo ▸ AI Chat Bridge (C#)   │
                       └───────────────────────────────────────────────────────────────┘
```

| Pieza | Qué hace | Dónde corre |
|---|---|---|
| Gemma 4 12B QAT | Escribe las respuestas y usa herramientas | LM Studio (GPU ~75 %) |
| StartLux Decision 4B Instant | Clasifica: estilo al hablar, emoción, gesto, mano, "¿merece reaccionar?" | llama-server + `startlux-decide` (GPU) |
| Whisper small | Transcribe el micrófono (modo visión) | CPU (faster-whisper int8) |
| AI Chat Bridge | Expresiones, animaciones, poses de mano y mirada | Dentro de Warudo (Playground) |

---

## Requisitos

**Hardware probado:** Windows 11, NVIDIA RTX 3060 de 12 GB y 2 monitores. En los 12 GB caben Gemma, StartLux y Warudo, pero muy justos (ver [VRAM](#vram)).

**Software:**

| Programa | Uso |
|---|---|
| [Warudo](https://warudo.app) (Steam) | Renderiza el personaje |
| [LM Studio](https://lmstudio.ai) con el CLI `lms` | Sirve Gemma |
| [uv](https://docs.astral.sh/uv/) | Entorno Python 3.12 y dependencias |
| .NET SDK 8 (opcional) | Solo para comprobar que el script C# compila fuera de Warudo |

**Modelos:**

| Modelo | Origen | Tamaño |
|---|---|---|
| `google/gemma-4-12b-qat` (Q4_0) | LM Studio | 7,2 GB |
| `StartLux-Decision-v1.1-4B-Instant-Q4_K_M.gguf` + `mmproj-…-F16.gguf` | LM Studio / HF `startlux-models/StartLux-Decision-v1.1-4B-Instant-GGUF` | 2,7 + 0,67 GB |
| Tokenizer y config de StartLux (sin pesos) | HF `startlux-models/StartLux-Decision-v1.1-4B-Instant` | ~25 MB |
| Whisper `small` (faster-whisper) | Se descarga solo la primera vez | 464 MB |
| *(opcional)* `jev-style-qwen3.5-2b-decision` | LM Studio | 2,1 GB |

---

## Instalación

### 1. Dependencias de Python

```powershell
cd vtuberwarudo
uv sync
```

`startlux-decide` se instala desde su repositorio oficial de GitHub, porque no está en PyPI. Ya está declarado en `pyproject.toml`.

### 2. LM Studio

Descarga `google/gemma-4-12b-qat` en LM Studio. También conviene descargar ahí el GGUF de StartLux y su `mmproj`, que es como se instaló aquí (en `%USERPROFILE%\.lmstudio\models\startlux-models\…`). Si están en otra carpeta, pásala a `start-startlux.ps1` con `-ModelDir`.

### 3. llama.cpp para StartLux (`tools\llama.cpp`)

Descarga `llama-b12383-bin-win-cuda-12.4-x64.zip` de las [releases de llama.cpp](https://github.com/ggml-org/llama.cpp/releases) y descomprímelo en `tools\llama.cpp\`. No hace falta el zip `cudart`: el script usa las DLL de CUDA 12 que ya trae LM Studio.

```powershell
New-Item -ItemType Directory -Force tools\downloads | Out-Null
curl.exe -L -o tools\downloads\llama.zip https://github.com/ggml-org/llama.cpp/releases/download/b12383/llama-b12383-bin-win-cuda-12.4-x64.zip
Expand-Archive tools\downloads\llama.zip -DestinationPath tools\llama.cpp
```

### 4. Tokenizer y configuración de StartLux (`models\startlux-4b-instant`)

```powershell
uvx --from huggingface_hub hf download startlux-models/StartLux-Decision-v1.1-4B-Instant --exclude "*.safetensors" --local-dir models\startlux-4b-instant
```

### 5. Puente en Warudo

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-playground.ps1
```

Copia `warudo\AIChatBridge.cs` a `Warudo_Data\StreamingAssets\Playground\`, y Warudo lo recarga en caliente. Después, en Warudo:

1. **Assets → + → AI Chat Bridge** → elige tu **Character**. El panel debe mostrar `Listening on ws://127.0.0.1:19500`.
2. **Expresiones VRM** (happy/Joy, angry, sad/Sorrow, relaxed/Fun, surprised): si el personaje no las tiene, la app las importa sola al conectar (`[warudo] auto_import_expressions`).
3. **Guarda la escena.** Si no, al reiniciar Warudo se pierden el asset y las expresiones.

`tools/`, `models/` y `data/` están en `.gitignore`.

---

## Arranque diario

El orden importa por la VRAM:

```powershell
# 1. StartLux (llama-server :8081 + API de decisiones :8090). Déjalo abierto en su propia terminal.
powershell -ExecutionPolicy Bypass -File scripts\start-startlux.ps1

# 2. Gemma en LM Studio: 1 ranura y 75 % de capas en GPU (genera ~9 tok/s; el ritmo de habla necesita ~4)
powershell -ExecutionPolicy Bypass -File scripts\load-models.ps1 -GemmaGpu 0.75

# 3. Warudo abierto con la escena guardada (AI Chat Bridge escuchando)

# 4. La app
uv run vtuber-chat
```

Abre **http://localhost:8000**. Los tres indicadores de arriba (**LM Studio**, **StartLux** y **Warudo**) deben estar en verde. Se comprueban cada 10 s.

---

## Uso

### Chat

- **Escribir:** escribe y pulsa Enter. La respuesta aparece a ritmo de habla y el personaje gesticula mientras tanto.
- **Etiquetas:** bajo cada respuesta salen las de la reacción, por ejemplo `🗣️ excited · 😊 happy · 🎉 cheer · 👍 thumbs_up`. Una etiqueta atenuada significa que Warudo no pudo aplicarla, por ejemplo una expresión que no existe en el personaje.
- **Pedir gestos:** "¡baila!" o "hazme el signo de la paz" hacen que Gemma use sus herramientas (`perform_action`, `set_hand_pose`…).
- **Nueva conversación:** empieza otro hilo. La memoria de `AGENTS.md` se mantiene entre hilos.
- **Overlay para OBS:** `http://localhost:8000/?overlay=1` muestra solo los mensajes con fondo transparente.

### Modo visión (botón 👁)

El personaje reacciona **solo con el cuerpo** (gesto + mano + expresión, sin texto) a tu pantalla y a tu voz. El panel muestra:

- **Medidor del micrófono:** la raya marca desde dónde se considera voz.
- **`🎤 «lo que dijiste»`:** cuando Whisper entiende una frase.
- **`▶ 😲 surprised · 🤷 unsure · 🖐️ open (p=0.75)`:** una reacción y su probabilidad.
- **`· nada que reaccionar` / `· esperando`:** consultas que no dieron reacción, y por qué.

El modo visión se pausa durante los turnos de chat y no interrumpe un gesto en curso.

---

## Configuración (`config.toml`)

| Sección | Para qué |
|---|---|
| `[lmstudio]` | URL, modelo de chat, temperatura, `reasoning_effort = "none"` (desactiva el *thinking* de Gemma, que añadía ~10 s por respuesta) |
| `[decision]` | `backend = "startlux"` (con visión) o `"jev"` (solo texto, en LM Studio); `startlux_url` |
| `[vision]` | Monitor, intervalo, umbrales, micrófono, Whisper (ver abajo) |
| `[warudo]` | URL del bridge, duración de las expresiones |
| `[persona]` | Nombre y personalidad del personaje (prompt de sistema) |
| `[agent]` | `use_action_tools` (herramientas de gestos para Gemma), carpeta de datos |
| `[reaction]` | Preguntas al modelo de decisión, duración de la pose de mano |
| `[speech]` | Ritmo del texto: `chars_per_second` y pausas en comas y puntos |
| `[animation]` | `crossfade_ms` (fundido entre clips), `max_wait_ms` |
| `[talk]` | Activar la gesticulación al hablar; máscara `upper` |
| `[emotions.*]` | Emoción → nombre exacto de la expresión en Warudo |
| `[gestures.*]` | Gesto → URI de animación de Warudo (+ `mask`, `look`, `speed`) |
| `[talk_styles.*]` | Estilos al hablar (clips `…_Loop`; `_Start`/`_End` se deducen) |
| `[hand_poses.*]` | Poses de dedos (Override Hand Poses) y qué mano |

**Reglas de las tablas de opciones:**

- `label` es el texto **en inglés** que lee el modelo de decisión, y cambiarlo cambia cómo clasifica.
- El orden importa: la primera opción es la de por defecto si la decisión falla.
- Puede haber como máximo 26 opciones por tabla.

**Parámetros de `[vision]` que más se ajustan:**

| Clave | Valor | Significado |
|---|---|---|
| `monitor` | `2` | 1 = monitor derecho (x=1920), 2 = izquierdo (x=0) en este equipo |
| `interval_s` / `cooldown_s` | 4 / 6 | Cada cuánto mira la pantalla y tiempo mínimo entre reacciones |
| `react_threshold` | 0.5 | P("hay algo a lo que reaccionar"). Una pantalla vacía da ~0,03; un vídeo o una web, 0,55–0,8 |
| `speech_threshold` | 0.35 | Umbral cuando acabas de hablar (reacciona más a tu voz) |
| `scene_change_threshold` | 0.015 | Fracción de pantalla que debe cambiar para volver a consultar |
| `focus_motion` | true | Envía solo la zona que se mueve (un vídeo dentro de la web), recortada y ampliada |
| `mic_device` | `"FaceCam 1000X"` | Parte del nombre o el número que da `vtuber-cli mics` |
| `energy_threshold` | 0.004 | Volumen mínimo de voz; además se adapta a 4× el ruido de fondo |

---

## Cómo funciona por dentro

### Turno de chat

1. **Estilo al hablar (antes de Gemma, ~0,4 s):** StartLux elige cómo gesticular (`normal`, `explaining`, `excited`, `calm`, `scolding`…) a partir de tu mensaje, y empieza el clip *Start*. Se decide antes y no en paralelo porque LM Studio pondría la petición en cola detrás del procesamiento de Gemma (~8 s).
2. **Gemma genera la respuesta** (1–3 s). Puede usar herramientas: `perform_action`, `set_expression`, `set_hand_pose`, `look_at`, y `edit_file` sobre su memoria.
3. **Reacción (`ReactionMiddleware.after_agent`):** una sola petición a StartLux con tres preguntas (emoción, gesto y pose de mano).
4. **Ritmo de habla (`speech.py`):** el texto se envía al navegador a ~15 caracteres/s con pausas en la puntuación. Usa un reloj absoluto para no acumular retraso, porque en Windows cada `sleep` dura algo más de lo pedido.
5. **Coreografía (`animation.py`):** todo ocurre dentro del texto y ningún clip se corta.

```
texto:  |───────────────────────── voz ─────────────────────────|
cuerpo: | Start ⤳ Loop… ⤳ GESTO (entero) ⤳ Loop… ⤳ End           |
                          ↑ expresión y pose de mano a la vez que el gesto
```

- **`⤳` es un fundido de 600 ms:** el siguiente clip empieza antes de que acabe el actual. Así el personaje pasa de clip a clip sin volver a la animación de reposo de Warudo, que solo aparece al terminar el turno.
- **El gesto entra en cuanto se decide,** sustituyendo con el fundido al Loop de relleno. Start, End y los gestos nunca se interrumpen. Si el gesto no cabe en el tiempo que queda de texto, se acelera como máximo ×1,3.
- **Los Loops ajustan su velocidad** (×0,75–1,35) para que el *End* termine con el último carácter. Si no cabe, el gesto cierra el turno.
- **El chat no se desbloquea hasta que acaba la última animación.**

### Modo visión

1. Cada `interval_s` (o cuando Whisper termina una frase tuya) captura el monitor configurado.
2. Compara una miniatura de 64×36 con la de la última consulta y mide qué fracción de la pantalla cambió. Si apenas cambió y no hablaste, no se consulta a la GPU.
3. Si solo se mueve una zona, por ejemplo un vídeo dentro de la web, envía esa zona recortada y ampliada.
4. StartLux recibe la imagen y responde cuatro preguntas: ¿hay algo a lo que reaccionar? (sí/no), emoción, gesto y mano.
   - Las opciones excluyen *neutral* y *quieta*: si decide reaccionar, siempre se ve un gesto.
   - También excluyen el gesto anterior, para que no se repita.
5. Si la probabilidad supera el umbral y pasó el `cooldown_s`, aplica la reacción.

**Micrófono (`listen.py`):**

- **Detección de voz por energía:** el umbral se adapta al ruido de fondo, con histéresis para no trocear las frases en las sílabas suaves.
- **Pre-roll:** cada frase incluye los 300 ms anteriores a la detección, para no cortar la primera sílaba.
- **Normalización:** el volumen de cada frase se iguala antes de pasarla a Whisper.
- **Filtro de frases inventadas:** se descartan las que Whisper produce con silencio, como *«¡Suscríbete!»*.

### StartLux

`scripts\start-startlux.ps1` arranca dos procesos:

- **`llama-server`** (llama.cpp b12383, CUDA 12.4), con las flags que pide la documentación de StartLux para leer el prompt de forma bidireccional en un servidor sin parches: `--parallel 1 --ctx-checkpoints 0 -b/-ub = contexto`, junto con el `mmproj` para visión.
- **`startlux-decide serve --backend llamacpp`**, con `STARTLUX_LLAMACPP_BIDIR=server`, que expone `POST /v1/systemone`.

Datos medidos en este equipo:

| | Tiempo |
|---|---|
| 3 preguntas de texto | ~0,4 s |
| 4 preguntas con una captura | ~1,5 s |

### Puente de Warudo (`warudo/AIChatBridge.cs`)

Es un *asset* de Playground. Abre un servidor WebSocket (WebSocketSharp) en `127.0.0.1:19500` y ejecuta los comandos en el hilo principal de Unity, en `OnUpdate()`. Cada comando JSON recibe una respuesta `{"ok": true|false, "id": …}`.

| Comando | Efecto |
|---|---|
| `{"cmd":"info"}` | Personajes, expresiones, clips VRM, idle y mirada |
| `{"cmd":"expression","name":"happy","holdMs":5000}` | Expresión durante un tiempo; `""` vuelve a neutral |
| `{"cmd":"animation","uri":"…","speed":1,"mask":"upper","transition":0.6}` | Animación de un solo uso; devuelve `durationMs` |
| `{"cmd":"handPose","hand":"right","pose":"…","holdMs":4000}` | Pose de dedos (Override Hand Poses) |
| `{"cmd":"lookAt","mode":"camera"\|"away"\|"default"}` | Mirada |
| `{"cmd":"idle","uri":"…"}` | Animación de reposo por defecto |
| `{"cmd":"listResources","type":"CharacterAnimation"\|"CharacterHandPose","filter":"…"}` | Lista de animaciones o poses (URI, nombre, categoría) |
| `{"cmd":"clipInfo","uris":[…]}` | Duración de clips sin reproducirlos |
| `{"cmd":"importVrmExpressions"}` | Igual que el botón "Import VRM Expressions" del personaje |

Solo el último clip lanzado restablece el *root motion* al terminar, para que no haya saltos al encadenar clips con fundido.

---

## Estructura del proyecto

```
config.toml                  Toda la configuración (modelos, persona, visión, animaciones)
pyproject.toml / uv.lock     Dependencias (uv)
src/vtuber_warudo/
  server.py                  FastAPI: web, WebSocket del chat, /api/vision, turnos
  agent.py                   Deep Agent: modelo, prompt de personaje, memoria, perfil sin herramientas sobrantes
  tools.py                   Herramientas de gestos para Gemma
  reaction.py                Middleware: estilo al hablar (antes) y reacción (después)
  decision.py                Clientes de decisión: StartLux (/v1/systemone, imágenes) y jev (LM Studio)
  speech.py                  SpeechPacer: texto a ritmo de habla
  animation.py               AnimationDirector: cola, fundidos y coreografía Start/Loop/End
  avatar.py                  Traduce emociones, gestos y manos a comandos de Warudo
  warudo.py                  Cliente WebSocket del bridge (con confirmación de cada comando)
  vision.py                  Modo visión: captura, detección de cambios, recorte, decisiones
  listen.py                  Micrófono: detección de voz y Whisper en CPU
  cli.py                     vtuber-cli (pruebas y utilidades)
  config.py                  Carga de config.toml
web/                         Interfaz de chat (index.html, app.js, style.css)
warudo/AIChatBridge.cs       Script Playground de Warudo (+ warudo/README.md)
scripts/
  start-startlux.ps1         Arranca llama-server + startlux-decide
  load-models.ps1            Carga Gemma en LM Studio (y jev si se pide)
  install-playground.ps1     Copia el bridge a Warudo
tests/                       pytest: decisiones, animación, ritmo de habla, visión, micrófono
tools/  models/  data/       Binarios, modelos descargados y datos locales (no versionados)
```

---

## Herramientas de línea de comandos

```powershell
uv run vtuber-cli decisions                    # 15 diálogos de ejemplo → estilo, emoción, gesto, mano y latencia
uv run vtuber-cli chat                         # chat por terminal con el agente completo
uv run vtuber-cli info                         # personajes y expresiones en Warudo
uv run vtuber-cli animations [filtro] [-o f]   # las 962 animaciones de Warudo con su URI
uv run vtuber-cli handposes [filtro]           # las 20 poses de mano
uv run vtuber-cli send emotion happy           # también: gesture wave, hand peace, talk excited,
uv run vtuber-cli send animation "<uri>" 1.0   #          expression <nombre>, look camera, idle <uri>
uv run vtuber-cli mics                         # micrófonos disponibles
uv run vtuber-cli mic-test 10                  # graba, muestra el volumen y transcribe
uv run vtuber-cli vision-test "¡he ganado!"    # una decisión del modo visión con la pantalla actual
```

---

## Tests

```powershell
uv run pytest
```

Hay 42 tests y no necesitan GPU, Warudo ni LM Studio:

- **Decisiones:** formato del prompt de decisión y parseo de la letra elegida.
- **Animación:** encadenado sin huecos ni cortes, el End coincide con el final del texto, el gesto llega tarde y sustituye al Loop, aceleración de gestos que no caben.
- **Ritmo de habla:** orden de eventos y duración.
- **Visión:** pantalla quieta → no repite reacciones; la voz siempre provoca una mirada; recorte del vídeo; toda reacción mueve el cuerpo.
- **Micrófono:** frases de un micro flojo sin trocear; umbral adaptado a una sala ruidosa.

Para comprobar que el script C# compila contra los DLL reales de Warudo, crea un `.csproj` net48 que referencie `Warudo_Data\Managed\*.dll` y ejecuta `dotnet build`.

---

## Solución de problemas

### VRAM

| Proceso | Uso aproximado |
|---|---|
| Warudo + escritorio | ~1,6 GB |
| StartLux (contexto 2048, mmproj en GPU) | ~3,9 GB |
| Gemma (1 ranura) | ~8,1 GB al 100 % / ~6,1 GB al 75 % |

- **Gemma ocupa ~9 GB:** LM Studio la carga con 4 ranuras paralelas. `load-models.ps1` usa `--parallel 1`.
- **No cabe StartLux:** usa `start-startlux.ps1 -MmprojOnCpu` (libera 0,67 GB; las imágenes van más lentas) o baja `-GemmaGpu`.

### LM Studio

- **Cada respuesta tarda 40–110 s:** LM Studio descarga un modelo cuando cargas otro bajo demanda (`unloadPreviousJITModelOnLoad`). Carga los modelos con los scripts, que hacen cargas explícitas que no se descargan solas.
- **Respuestas vacías o muy lentas:** el *thinking* de Gemma está activo. Comprueba `reasoning_effort = "none"`.

### Las emociones no se ven (siempre *neutral*, sin gesto)

- **Indicador StartLux en rojo:** el servidor de decisiones no está arrancado. Cada decisión falla y se usa la opción por defecto. Arranca `scripts\start-startlux.ps1`.
  - **No basta con cargar StartLux en LM Studio:** necesita su propio servidor (lectura bidireccional del prompt). Además, en LM Studio ocupa VRAM que necesita Gemma.
  - El log del servidor avisa con `El modelo de decisión (startlux) NO responde`.
- **El personaje no tiene expresiones:** con `auto_import_expressions = true`, la app las importa del VRM al conectar con Warudo.
  - Según la versión, se llaman como en VRM 1.0 (*happy, sad, relaxed…*) o como en VRM 0.x (*Joy, Sorrow, Fun…*). La app acepta las dos convenciones.
  - **Guarda la escena** para no tener que reimportarlas cada vez.

### Warudo

- **`unknown expression "…"`:** el personaje no tiene esa expresión. `vtuber-cli info` lista las que tiene.
- **`No se pudo conectar a Warudo`:** falta el asset *AI Chat Bridge* en la escena, o no tiene personaje asignado.
- **El personaje vuelve al reposo entre animaciones:** sube `crossfade_ms`.

### Modo visión

- **No reacciona a un vídeo:**
  1. Comprueba que `monitor` es el monitor donde está el vídeo.
  2. En el log, `cambio=` debe superar `scene_change_threshold`.
  3. `vtuber-cli vision-test` muestra la probabilidad `react`.
- **Micrófono con volumen 0:**
  1. Usa `vtuber-cli mics` y `mic-test`; el micrófono predeterminado puede ser virtual, como "Steam Streaming Microphone".
  2. Elige el que capte tu voz en `mic_device`.
- **Muchas líneas "sonido sin palabras":** el micrófono capta el audio del vídeo o de los altavoces. No provoca reacciones; si molesta, sube `energy_threshold`.

### Codificación de archivos

PowerShell 5.1 escribe en ANSI con `Set-Content` y `Add-Content` y estropea las tildes. Edita los archivos del proyecto con un editor en UTF-8.

---

## Limitaciones conocidas

- **Sin voz (TTS):** el personaje no habla, el "habla" es el texto a ritmo de lectura.
- **Memoria entre conversaciones poco fiable:** Gemma no siempre decide guardar datos en `AGENTS.md`.
- **El estilo al hablar** se decide solo con tu mensaje, antes de que exista la respuesta, así que a veces no encaja con lo que contesta.
- **`llama-server` sin parches:** según la documentación de StartLux, sin el parche 0003 las decisiones con imagen se desvían ~0,02 de media respecto al modelo de referencia. Para máxima precisión, compila llama.cpp con los parches de `StartLuxLabs/StartLux-Decision`.
- **Calibración específica:** los umbrales del modo visión están calibrados para estos monitores, este micrófono y contenido tipo vídeo/web.

---

## Licencias de los modelos

- **StartLux Decision v1.1:** CC BY-NC 4.0 (uso no comercial). El paquete `startlux-decide` es Apache-2.0.
- **Gemma 4:** consulta su licencia en la ficha del modelo en LM Studio o Hugging Face.
- **Whisper (faster-whisper / Systran):** MIT.
- **Animaciones y poses de mano:** las integradas en Warudo.
