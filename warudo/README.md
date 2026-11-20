# AI Chat Bridge (Warudo Playground)

`AIChatBridge.cs` is a Warudo Playground asset that opens a WebSocket server on
`ws://127.0.0.1:19500/` so the Python chat app can drive a character.

## Install

```powershell
.\scripts\install-playground.ps1                      # default: C:\Program Files (x86)\Steam\steamapps\common\Warudo
.\scripts\install-playground.ps1 -WarudoPath "<Warudo folder>"
```

It copies the file to `Warudo_Data\StreamingAssets\Playground\`. Warudo hot-reloads it.
Then in Warudo: **Assets → + → AI Chat Bridge**, select the **Character**. The
asset panel shows the status (`Listening on ws://127.0.0.1:19500`). Optional:
**Look-at camera** (otherwise the first active camera is used), **Port**,
**Enabled**, **Restart server** button (use it if the port was busy).

## Protocol

One JSON object per text message. Every command gets exactly one reply:
`{"ok":true|false, "id":<echoed if sent>, "cmd":"...", ...}`; failures carry `"error"`.

| Command | Effect | Extra reply fields |
|---|---|---|
| `{"cmd":"ping"}` | – | – |
| `{"cmd":"info"}` | – | `character`, `active`, `expressions[]`, `idle`, `lookAtEnabled`, `lookAtTarget` |
| `{"cmd":"expression","name":"Joy","holdMs":4000}` | Exit all expressions, enter `name`; after `holdMs` back to neutral. `holdMs` 0/absent = stays. Name match is case-insensitive. `"neutral"`, `"none"` or `""` = just exit all. Optional `"transient":true` uses Warudo's own auto-exit instead. | `name`, `holdMs`; on unknown name `expressions[]` |
| `{"cmd":"animation","uri":"character-animation://...","speed":1.0}` | One-shot animation. Optional `weight` (0–1), `transition` (s, default 0.8), `mask`: `"upper"` / `"lower"` / `"full"` (default). | `uri`, `durationMs` |
| `{"cmd":"idle","uri":"character-animation://..."}` | Sets the character's Default Idle Animation (Warudo restarts the base animation). | `idle`, `previous` |
| `{"cmd":"lookAt","mode":"camera"}` | Enables look-at IK aimed at the camera. | `target` |
| `{"cmd":"lookAt","mode":"away"}` | Disables look-at (head follows the animation). | – |
| `{"cmd":"lookAt","mode":"default"}` | Restores the look-at settings from before the first `camera`/`away`. | `lookAtEnabled` |
| `{"cmd":"handPose","hand":"left"\|"right"\|"both","pose":"<uri>","weight":1,"holdMs":4000}` | Override Hand Poses on that hand; `pose:""` releases it; released again after `holdMs`. | `hand`, `pose` |
| `{"cmd":"listResources","type":"CharacterAnimation"\|"CharacterHandPose","filter":"…"}` | Lists Warudo resources of that type (`listAnimations` = same with `CharacterAnimation`). | `count`, `animations[]` (`uri`, `label`, `category`, `provider`) |
| `{"cmd":"clipInfo","uris":["…"]}` | Clip lengths without playing anything. | `clips{uri: {lengthMs, loop}}` |
| `{"cmd":"importVrmExpressions"}` | Same as the character's "Import VRM Expressions" button (async; poll `info`). | `before`, `importable` |

`info` also returns `characters[]` (every character in the scene with `expressions`, `expressionNames`, `vrmClips`, `importableVrmExpressions`). One-shot animations chained with a crossfade (the next one started before the previous ends) only reset root motion when the last one launched finishes.

Animation URIs are the ones Warudo shows in the animation pickers, e.g.
`character-animation://resources/Animations/AGIA/01_Idles/AGIA_Idle_generic_01`.

Threading: network callbacks only enqueue; commands run and replies are sent from
`OnUpdate()` on Unity's main thread (max 32 commands per frame).

## Quick test (no Python app)

Save as `ws_test.py` and run `uv run --with websockets python ws_test.py`:

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:19500") as ws:
        for cmd in [
            {"cmd": "ping", "id": 1},
            {"cmd": "info", "id": 2},
            {"cmd": "expression", "name": "Joy", "holdMs": 3000, "id": 3},
            {"cmd": "lookAt", "mode": "camera", "id": 4},
        ]:
            await ws.send(json.dumps(cmd))
            print(await ws.recv())

asyncio.run(main())
```
