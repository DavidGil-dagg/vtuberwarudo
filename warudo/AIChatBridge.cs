// AI Chat Bridge — Warudo Playground asset.
//
// Opens a local WebSocket server (ws://127.0.0.1:<Port>/) and lets an external
// process (the Python chat app in src/vtuber_warudo) drive a character:
// expressions, one-shot animations, idle animation and look-at.
//
// Protocol: one JSON command per text message; every command gets exactly one
// JSON reply {"ok":true|false,"id":<echoed if sent>,"cmd":<cmd>,...}.
//   {"cmd":"ping"}
//   {"cmd":"info"}
//   {"cmd":"expression","name":"Joy","holdMs":4000}          (holdMs 0/absent = stay)
//   {"cmd":"animation","uri":"character-animation://...","speed":1.0,
//        "weight":1.0,"transition":0.8,"mask":"upper"|"lower"|"full"}
//   {"cmd":"idle","uri":"character-animation://..."}
//   {"cmd":"lookAt","mode":"camera"|"away"|"default"}
//   {"cmd":"listAnimations","filter":"wave"}                 (filter optional)
//   {"cmd":"listResources","type":"CharacterHandPose","filter":""}
//   {"cmd":"handPose","hand":"left"|"right"|"both","pose":"<uri>"|"","weight":1.0,"holdMs":4000}
//
// Threading: WebSocketSharp callbacks run on worker threads and only enqueue raw
// text. Parsing, every Warudo/Unity call and the replies happen in OnUpdate()
// on the main thread.
//
// Install: copy this file to Warudo_Data\StreamingAssets\Playground\ (see
// scripts/install-playground.ps1), then in Warudo add the asset "AI Chat Bridge"
// and pick the character.

using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEngine;
using WebSocketSharp;
using WebSocketSharp.Server;
using Warudo.Core;
using Warudo.Core.Attributes;
using Warudo.Core.Scenes;
using Warudo.Plugins.Core.Assets;
using Warudo.Plugins.Core.Assets.Character;
using Warudo.Plugins.Core.Assets.Cinematography;
using Warudo.Plugins.Core.Nodes;

[AssetType(Id = "8d3f6a52-1c47-4b9e-a0d2-5e71c4b9f2a6", Title = "AI Chat Bridge", Category = "CATEGORY_INTERACTIONS")]
public class AIChatBridge : Asset
{
    [DataInput]
    [Label("Character")]
    public CharacterAsset Character;

    [DataInput]
    [Label("Look-at camera")]
    [Description("Camera used by lookAt mode \"camera\". Empty = first active camera in the scene.")]
    public CameraAsset LookAtCamera;

    [DataInput]
    [Label("Port")]
    [Description("WebSocket port on 127.0.0.1 (avoid 19190 and 19052, used by Warudo).")]
    public int Port = 19500;

    [DataInput]
    [Label("Enabled")]
    public bool Enabled = true;

    [Markdown]
    public string Status = "Stopped";

    [Trigger]
    [Label("Restart server")]
    public void RestartServerTrigger()
    {
        RestartServer();
    }

    private const int MaxCommandsPerFrame = 32;
    private const float DefaultTransitionTime = 0.8f;
    private int oneShotSeq;

    private readonly ConcurrentQueue<IncomingMessage> inbox = new ConcurrentQueue<IncomingMessage>();
    private readonly AnimationClipCache clipCache = new AnimationClipCache();

    private WebSocketServer server;
    private WebSocketSessionManager sessions;

    // Config the server was last (re)started with. OnUpdate compares it with the
    // current data inputs, so a Port/Enabled loaded from the saved scene after
    // OnCreate (or changed without firing a watcher) still restarts the server.
    private bool configApplied;
    private int appliedPort;
    private bool appliedEnabled;
    private bool destroyed;

    // Expression hold: when > 0, return to neutral at this realtime.
    private float expressionResetAt = -1f;
    private CharacterAsset expressionCharacter;

    // Hand pose hold: when > 0, release the hand pose override at this realtime.
    private float handPoseResetAt = -1f;
    private CharacterAsset handPoseCharacter;

    // Look-at state saved before the first override, restored by mode "default".
    private bool lookAtSaved;
    private CharacterAsset lookAtSavedCharacter;
    private bool savedLookAtEnabled;
    private GameObjectAsset savedLookAtTarget;

    // ------------------------------------------------------------------ lifecycle

    protected override void OnCreate()
    {
        base.OnCreate();
        Watch(nameof(Port), RestartServer);
        Watch(nameof(Enabled), RestartServer);
        Watch(nameof(Character), OnCharacterChanged);
        SetActive(true);
        // The server itself is started from OnUpdate, once the scene has finished
        // deserializing this asset's data inputs.
    }

    protected override void OnDestroy()
    {
        destroyed = true;
        StopServer();
        ClearExpressionHold(exitOnCharacter: true);
        if (handPoseResetAt > 0f) ClearHandPose();
        clipCache.Clear();
        base.OnDestroy();
    }

    public override void OnUpdate()
    {
        base.OnUpdate();
        if (destroyed) return;

        if (!configApplied || appliedPort != Port || appliedEnabled != Enabled)
        {
            RestartServer();
        }

        if (expressionResetAt > 0f && Time.realtimeSinceStartup >= expressionResetAt)
        {
            ClearExpressionHold(exitOnCharacter: true);
        }

        if (handPoseResetAt > 0f && Time.realtimeSinceStartup >= handPoseResetAt)
        {
            ClearHandPose();
        }

        int processed = 0;
        IncomingMessage message;
        while (processed < MaxCommandsPerFrame && inbox.TryDequeue(out message))
        {
            processed++;
            JObject reply = Execute(message.Text);
            Reply(message.SessionId, reply);
        }
    }

    private void OnCharacterChanged()
    {
        ClearExpressionHold(exitOnCharacter: true);
        RestoreLookAt();
    }

    // ------------------------------------------------------------------ server

    private void RestartServer()
    {
        if (destroyed) return;
        StopServer();
        // Record the attempt even if it fails, so a busy port is not retried
        // every frame (use the "Restart server" button or change the port).
        configApplied = true;
        appliedPort = Port;
        appliedEnabled = Enabled;
        if (!Enabled)
        {
            SetStatus("Disabled");
            return;
        }
        if (Port <= 0 || Port > 65535)
        {
            SetStatus("Invalid port " + Port);
            return;
        }

        try
        {
            var ws = new WebSocketServer("ws://127.0.0.1:" + Port);
            ws.Log.Level = LogLevel.Error;
            ws.AddWebSocketService<BridgeBehavior>("/", b => b.Owner = this);
            ws.Start();
            server = ws;
            sessions = ws.WebSocketServices["/"].Sessions;
            SetStatus("Listening on ws://127.0.0.1:" + Port);
            Debug.Log("[AIChatBridge] Listening on ws://127.0.0.1:" + Port);
        }
        catch (Exception e)
        {
            server = null;
            sessions = null;
            SetStatus("Failed to start on port " + Port + ": " + e.Message);
            Debug.LogError("[AIChatBridge] Failed to start server on port " + Port + ": " + e);
        }
    }

    private void StopServer()
    {
        var ws = server;
        server = null;
        sessions = null;
        if (ws != null)
        {
            try
            {
                ws.Stop();
            }
            catch (Exception e)
            {
                Debug.LogWarning("[AIChatBridge] Error stopping server: " + e.Message);
            }
        }
        IncomingMessage dropped;
        while (inbox.TryDequeue(out dropped)) { }
        SetStatus("Stopped");
    }

    private void SetStatus(string status)
    {
        Status = status;
        // Ports are already cleared when OnDestroy runs.
        if (!destroyed) BroadcastDataInput(nameof(Status));
    }

    private void Reply(string sessionId, JObject reply)
    {
        var s = sessions;
        if (s == null || sessionId == null) return;
        try
        {
            s.SendToAsync(reply.ToString(Formatting.None), sessionId, null);
        }
        catch (Exception e)
        {
            Debug.LogWarning("[AIChatBridge] Could not reply to " + sessionId + ": " + e.Message);
        }
    }

    // Called from WebSocketSharp worker threads: enqueue only, never touch Unity.
    internal void Enqueue(string sessionId, string text)
    {
        inbox.Enqueue(new IncomingMessage { SessionId = sessionId, Text = text });
    }

    // ------------------------------------------------------------------ commands

    private JObject Execute(string text)
    {
        JObject request;
        try
        {
            request = JObject.Parse(text);
        }
        catch (Exception e)
        {
            return Fail(null, null, "invalid JSON: " + e.Message);
        }

        JToken id = request["id"];
        string cmd = (string)request["cmd"];
        try
        {
            switch (cmd)
            {
                case "ping": return Ok(id, cmd);
                case "info": return CmdInfo(id, cmd);
                case "expression": return CmdExpression(id, cmd, request);
                case "animation": return CmdAnimation(id, cmd, request);
                case "idle": return CmdIdle(id, cmd, request);
                case "lookAt": return CmdLookAt(id, cmd, request);
                case "listAnimations": return CmdListResources(id, cmd, request, "CharacterAnimation");
                case "listResources": return CmdListResources(id, cmd, request, (string)request["type"] ?? "CharacterAnimation");
                case "handPose": return CmdHandPose(id, cmd, request);
                case "clipInfo": return CmdClipInfo(id, cmd, request);
                case null: return Fail(id, null, "missing \"cmd\"");
                default: return Fail(id, cmd, "unknown cmd \"" + cmd + "\"");
            }
        }
        catch (Exception e)
        {
            Debug.LogError("[AIChatBridge] Error executing " + cmd + ": " + e);
            return Fail(id, cmd, e.GetType().Name + ": " + e.Message);
        }
    }

    private JObject CmdInfo(JToken id, string cmd)
    {
        var reply = Ok(id, cmd);
        // Every character in the scene, to spot a bridge pointing at the wrong one.
        var all = new JArray();
        var scene = Context.OpenedScene;
        if (scene != null)
        {
            foreach (var c in scene.GetAssets<CharacterAsset>())
            {
                if (c == null) continue;
                var names = c.Expressions == null
                    ? new string[0]
                    : c.Expressions.Where(e => e != null).Select(e => e.Name).ToArray();
                all.Add(new JObject
                {
                    ["name"] = c.Name,
                    ["id"] = c.Id.ToString(),
                    ["active"] = c.Active,
                    ["selected"] = c == Character,
                    ["expressions"] = new JArray(names.Cast<object>().ToArray()),
                    ["expressionNames"] = SafeList(() => c.ExpressionNames),
                    ["vrmClips"] = SafeList(() => c.VRMBlendShapeClipNames),
                    ["importableVrmExpressions"] = SafeList(() => c.GetEligibleVRMExpressionNames()),
                });
            }
        }
        reply["characters"] = all;
        if (Character == null)
        {
            reply["character"] = null;
            reply["expressions"] = new JArray();
            reply["idle"] = null;
            return reply;
        }
        reply["character"] = Character.Name;
        reply["active"] = Character.Active;
        reply["expressions"] = new JArray(ExpressionNames().Cast<object>().ToArray());
        reply["idle"] = Character.DefaultIdleAnimation;
        reply["lookAtEnabled"] = Character.LookAtEnabled;
        reply["lookAtTarget"] = Character.LookAtTarget != null ? Character.LookAtTarget.Name : null;
        return reply;
    }

    private JObject CmdExpression(JToken id, string cmd, JObject request)
    {
        string error;
        if (!CharacterReady(out error)) return Fail(id, cmd, error);

        string requested = ((string)request["name"] ?? "").Trim();
        int holdMs = (int?)request["holdMs"] ?? 0;
        bool transient = (bool?)request["transient"] ?? false;

        string name = ResolveExpressionName(requested);
        if (name == null)
        {
            bool isNeutral = requested.Length == 0
                || string.Equals(requested, "neutral", StringComparison.OrdinalIgnoreCase)
                || string.Equals(requested, "none", StringComparison.OrdinalIgnoreCase);
            if (!isNeutral)
            {
                var fail = Fail(id, cmd, "unknown expression \"" + requested + "\"");
                fail["expressions"] = new JArray(ExpressionNames().Cast<object>().ToArray());
                return fail;
            }
            ClearExpressionHold(exitOnCharacter: false);
            Character.ExitAllExpressions(true);
            var neutral = Ok(id, cmd);
            neutral["name"] = null;
            return neutral;
        }

        ClearExpressionHold(exitOnCharacter: false);
        Character.ExitAllExpressions(true);
        Character.EnterExpression(name, transient);
        if (holdMs > 0 && !transient)
        {
            expressionResetAt = Time.realtimeSinceStartup + holdMs / 1000f;
            expressionCharacter = Character;
        }

        var reply = Ok(id, cmd);
        reply["name"] = name;
        reply["holdMs"] = holdMs;
        return reply;
    }

    private JObject CmdAnimation(JToken id, string cmd, JObject request)
    {
        string error;
        if (!CharacterReady(out error)) return Fail(id, cmd, error);

        string uri = (string)request["uri"];
        if (string.IsNullOrWhiteSpace(uri)) return Fail(id, cmd, "missing \"uri\"");

        AnimationClip clip = clipCache.Get(uri);
        if (clip == null) return Fail(id, cmd, "animation not found: " + uri);

        float speed = (float?)request["speed"] ?? 1f;
        if (speed <= 0f) speed = 1f;
        float weight = Mathf.Clamp01((float?)request["weight"] ?? 1f);
        float transition = Mathf.Max(0f, (float?)request["transition"] ?? DefaultTransitionTime);
        string mask = ((string)request["mask"] ?? "full").ToLowerInvariant();

        CharacterAsset.AnimationMaskedBodyPart[] parts;
        switch (mask)
        {
            case "upper":
                parts = new[]
                {
                    CharacterAsset.AnimationMaskedBodyPart.LeftArm,
                    CharacterAsset.AnimationMaskedBodyPart.RightArm,
                    CharacterAsset.AnimationMaskedBodyPart.LeftFingers,
                    CharacterAsset.AnimationMaskedBodyPart.RightFingers,
                    CharacterAsset.AnimationMaskedBodyPart.Head,
                };
                break;
            case "lower":
                parts = new[]
                {
                    CharacterAsset.AnimationMaskedBodyPart.Root,
                    CharacterAsset.AnimationMaskedBodyPart.Body,
                    CharacterAsset.AnimationMaskedBodyPart.LeftLeg,
                    CharacterAsset.AnimationMaskedBodyPart.RightLeg,
                    CharacterAsset.AnimationMaskedBodyPart.LeftFoot,
                    CharacterAsset.AnimationMaskedBodyPart.RightFoot,
                };
                break;
            case "full":
                parts = null;
                break;
            default:
                return Fail(id, cmd, "unknown mask \"" + mask + "\" (use upper, lower or full)");
        }

        CharacterAsset character = Character;
        // Clips encadenados con crossfade: el siguiente puede empezar antes de que acabe
        // este. Solo el último clip lanzado restablece transición y root motion al
        // terminar; si no, el final de un clip anterior daría un salto en mitad del nuevo.
        int seq = ++oneShotSeq;
        character.OverrideOverlappingAnimationTransitionTime(transition);
        character.PlayOneShotAnimation(clip, parts, weight, speed, false, () =>
        {
            if (character == null || seq != oneShotSeq) return;
            character.OverrideOverlappingAnimationTransitionTime(transition);
            character.ResetRootMotion();
        });

        var reply = Ok(id, cmd);
        reply["uri"] = uri;
        reply["durationMs"] = Mathf.RoundToInt(clip.length / speed * 1000f);
        return reply;
    }

    private JObject CmdIdle(JToken id, string cmd, JObject request)
    {
        if (Character == null) return Fail(id, cmd, "no character selected in AI Chat Bridge");

        string uri = (string)request["uri"];
        if (string.IsNullOrWhiteSpace(uri)) return Fail(id, cmd, "missing \"uri\"");
        if (clipCache.Get(uri) == null) return Fail(id, cmd, "animation not found: " + uri);

        string previous = Character.DefaultIdleAnimation;
        // Going through the data input fires CharacterAsset's own watcher, which
        // restarts the base animation, and updates the editor UI.
        Character.SetDataInput("DefaultIdleAnimation", uri, true);

        var reply = Ok(id, cmd);
        reply["idle"] = uri;
        reply["previous"] = previous;
        return reply;
    }

    private JObject CmdLookAt(JToken id, string cmd, JObject request)
    {
        if (Character == null) return Fail(id, cmd, "no character selected in AI Chat Bridge");

        string mode = ((string)request["mode"] ?? "").ToLowerInvariant();
        switch (mode)
        {
            case "camera":
            {
                GameObjectAsset target = FindLookAtCamera();
                if (target == null) return Fail(id, cmd, "no active camera found in the scene");
                SaveLookAt();
                Character.SetDataInput("LookAtTarget", target, true);
                Character.SetDataInput("LookAtEnabled", true, true);
                var reply = Ok(id, cmd);
                reply["mode"] = mode;
                reply["target"] = target.Name;
                return reply;
            }
            case "away":
            {
                SaveLookAt();
                Character.SetDataInput("LookAtEnabled", false, true);
                var reply = Ok(id, cmd);
                reply["mode"] = mode;
                return reply;
            }
            case "default":
            {
                RestoreLookAt();
                var reply = Ok(id, cmd);
                reply["mode"] = mode;
                reply["lookAtEnabled"] = Character.LookAtEnabled;
                return reply;
            }
            default:
                return Fail(id, cmd, "unknown mode \"" + mode + "\" (use camera, away or default)");
        }
    }

    // Clip lengths without playing anything: {"cmd":"clipInfo","uris":["...", ...]}
    // -> {"clips":{"<uri>":{"lengthMs":1234,"loop":false}|null}}
    private JObject CmdClipInfo(JToken id, string cmd, JObject request)
    {
        var uris = request["uris"] as JArray;
        if (uris == null) return Fail(id, cmd, "missing \"uris\" array");
        var clips = new JObject();
        foreach (var token in uris)
        {
            string uri = (string)token;
            if (string.IsNullOrWhiteSpace(uri)) continue;
            AnimationClip clip = clipCache.Get(uri);
            clips[uri] = clip == null
                ? null
                : new JObject { ["lengthMs"] = Mathf.RoundToInt(clip.length * 1000f), ["loop"] = clip.isLooping };
        }
        var reply = Ok(id, cmd);
        reply["clips"] = clips;
        return reply;
    }

    // Overrides the finger pose of one or both hands (Character > Override Hand Poses).
    // {"cmd":"handPose","hand":"left"|"right"|"both","pose":"<CharacterHandPose uri>"|"","weight":1,"holdMs":4000}
    // pose "" disables the override. holdMs > 0 disables it again after that time.
    private JObject CmdHandPose(JToken id, string cmd, JObject request)
    {
        string error;
        if (!CharacterReady(out error)) return Fail(id, cmd, error);

        string hand = ((string)request["hand"] ?? "both").ToLowerInvariant();
        if (hand != "left" && hand != "right" && hand != "both")
            return Fail(id, cmd, "unknown hand \"" + hand + "\" (use left, right or both)");
        string pose = ((string)request["pose"] ?? "").Trim();
        float weight = Mathf.Clamp01((float?)request["weight"] ?? 1f);
        int holdMs = (int?)request["holdMs"] ?? 0;
        bool highPriority = (bool?)request["highPriority"] ?? true;

        bool left = hand != "right", right = hand != "left";
        // Releasing first avoids leaving the other hand posed from a previous command.
        ApplyHandPose(Character, left, right, pose, weight, highPriority);
        handPoseResetAt = pose.Length > 0 && holdMs > 0 ? Time.realtimeSinceStartup + holdMs / 1000f : -1f;
        handPoseCharacter = Character;

        var reply = Ok(id, cmd);
        reply["hand"] = hand;
        reply["pose"] = pose.Length > 0 ? pose : null;
        return reply;
    }

    private static void ApplyHandPose(CharacterAsset character, bool left, bool right, string pose, float weight, bool highPriority)
    {
        var data = character.OverrideHandPoses;
        if (data == null) return;
        bool enable = !string.IsNullOrEmpty(pose);
        foreach (var h in new CharacterAsset.OverrideHandPosesData.HandPoseData[] { left ? data.LeftHand : null, right ? data.RightHand : null })
        {
            if (h == null) continue;
            if (enable)
            {
                h.SetDataInput(nameof(h.Pose), pose, true);
                h.SetDataInput(nameof(h.Weight), weight, true);
                h.SetDataInput(nameof(h.HighPriority), highPriority, true);
            }
            h.SetDataInput(nameof(h.Enabled), enable, true);
        }
    }

    private void ClearHandPose()
    {
        var target = handPoseCharacter;
        handPoseResetAt = -1f;
        handPoseCharacter = null;
        if (target != null && target.Active) ApplyHandPose(target, true, true, "", 1f, true);
    }

    // Lists Warudo resources of a type (default "CharacterAnimation"; also
    // "CharacterHandPose", ...), the same entries shown in the editor pickers.
    // Optional "filter" (case-insensitive substring of uri/label/category).
    private JObject CmdListResources(JToken id, string cmd, JObject request, string type)
    {
        string filter = ((string)request["filter"] ?? "").Trim();
        var list = new JArray();
        var results = Context.ResourceManager.ProvideResources(type);
        if (results != null)
        {
            foreach (var result in results)
            {
                if (result == null || result.resources == null) continue;
                foreach (var res in result.resources)
                {
                    if (res == null || res.uri == null) continue;
                    string uri = res.uri.ToString();
                    if (filter.Length > 0
                        && uri.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0
                        && (res.label ?? "").IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0
                        && (res.category ?? "").IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0)
                    {
                        continue;
                    }
                    list.Add(new JObject
                    {
                        ["uri"] = uri,
                        ["label"] = res.label,
                        ["category"] = res.category,
                        ["provider"] = result.providerName,
                    });
                }
            }
        }
        var reply = Ok(id, cmd);
        reply["count"] = list.Count;
        reply["animations"] = list;
        return reply;
    }

    // ------------------------------------------------------------------ helpers

    private bool CharacterReady(out string error)
    {
        if (Character == null)
        {
            error = "no character selected in AI Chat Bridge";
            return false;
        }
        if (!Character.Active)
        {
            error = "character \"" + Character.Name + "\" is not active/loaded";
            return false;
        }
        error = null;
        return true;
    }

    private IEnumerable<string> ExpressionNames()
    {
        if (Character == null || Character.Expressions == null) return Enumerable.Empty<string>();
        return Character.Expressions.Where(e => e != null).Select(e => e.Name);
    }

    private string ResolveExpressionName(string requested)
    {
        if (string.IsNullOrEmpty(requested)) return null;
        var names = ExpressionNames().ToList();
        return names.FirstOrDefault(n => n == requested)
            ?? names.FirstOrDefault(n => string.Equals(n, requested, StringComparison.OrdinalIgnoreCase));
    }

    private void ClearExpressionHold(bool exitOnCharacter)
    {
        var target = expressionCharacter;
        bool wasHolding = expressionResetAt > 0f;
        expressionResetAt = -1f;
        expressionCharacter = null;
        if (exitOnCharacter && wasHolding && target != null && target.Active)
        {
            target.ExitAllExpressions(true);
        }
    }

    private GameObjectAsset FindLookAtCamera()
    {
        if (LookAtCamera != null && LookAtCamera.Active) return LookAtCamera;
        var scene = Context.OpenedScene;
        if (scene == null) return null;
        return scene.GetAssets<CameraAsset>().FirstOrDefault(c => c != null && c.Active);
    }

    private void SaveLookAt()
    {
        if (lookAtSaved && lookAtSavedCharacter == Character) return;
        lookAtSaved = true;
        lookAtSavedCharacter = Character;
        savedLookAtEnabled = Character.LookAtEnabled;
        savedLookAtTarget = Character.LookAtTarget;
    }

    private void RestoreLookAt()
    {
        if (!lookAtSaved) return;
        var target = lookAtSavedCharacter;
        lookAtSaved = false;
        lookAtSavedCharacter = null;
        if (target == null) return;
        target.SetDataInput("LookAtTarget", savedLookAtTarget, true);
        target.SetDataInput("LookAtEnabled", savedLookAtEnabled, true);
        savedLookAtTarget = null;
    }

    private static JArray SafeList(Func<IEnumerable<string>> get)
    {
        try
        {
            var items = get();
            return items == null ? new JArray() : new JArray(items.Where(s => s != null).Cast<object>().ToArray());
        }
        catch (Exception e)
        {
            return new JArray("error: " + e.Message);
        }
    }

    private static JObject Ok(JToken id, string cmd)
    {
        var o = new JObject { ["ok"] = true };
        if (id != null) o["id"] = id.DeepClone();
        if (cmd != null) o["cmd"] = cmd;
        return o;
    }

    private static JObject Fail(JToken id, string cmd, string error)
    {
        var o = new JObject { ["ok"] = false };
        if (id != null) o["id"] = id.DeepClone();
        if (cmd != null) o["cmd"] = cmd;
        o["error"] = error;
        return o;
    }

    // ------------------------------------------------------------------ nested types

    private class IncomingMessage
    {
        public string SessionId;
        public string Text;
    }

    // Nested so the Playground file only declares one top-level (Asset) class.
    public class BridgeBehavior : WebSocketBehavior
    {
        public AIChatBridge Owner;

        protected override void OnMessage(MessageEventArgs e)
        {
            var owner = Owner;
            if (owner == null || !e.IsText) return;
            owner.Enqueue(ID, e.Data);
        }
    }
}
