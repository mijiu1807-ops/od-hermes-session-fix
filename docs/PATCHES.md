# The 8 Patches — Surgical Details

All OD-side patches go in the **runtime payload**, not the install directory:

```
Runtime truth (patch here):
  %APPDATA%\Open Design\launcher\channels\stable\namespaces\
    release-stable-win\versions\<VERSION>\payload\resources\app\prebundled\daemon\chunks\

  chunk-4IN7PJG2.mjs  → agent definitions        (patch ②)
  chunk-EER2BOWK.mjs  → ACP client transport     (patches ③④)

⚠ The D:\Open Design install directory copies are used only by the CLI/MCP
  bridge. Patching them does nothing for chat sessions.
  Prove the runtime path first:
    powershell "Get-CimInstance Win32_Process | ? { $_.CommandLine -match 'daemon-sidecar' } | select CommandLine"
```

Every patch follows the same ritual: **copy `.bak-<date>` → edit → `node --check` → restart OD fully** (graceful GUI close; every OD process exits with it).

> ⚠️ Never launch OD from a bash background job `("Open Design.exe" &)`.
> Its stdout pipe dies with the terminal → Electron main process crashes
> with an EPIPE dialog. Use PowerShell `Start-Process` instead.

---

## Patch ② — Enable session reuse for the Hermes agent

`chunk-4IN7PJG2.mjs`, inside `hermesAgentDef` (the vela/`amr` definition right
next to it already has this flag — that's your reference):

```js
// after the buildArgs line, add:
resumesSessionViaAcpLoad: true,
```

Effect: OD stops forcing `session/new` per turn and instead looks up its
`agent_sessions` mapping, calling `session/load` when a prior session exists.

---

## Patch ③ — Accept the standard ACP sessionId as the durable handle

`chunk-EER2BOWK.mjs` — find the durable-session capture:

```js
durableSessionId = typeof result.openCodeSessionId === "string" ? result.openCodeSessionId : null;
```

replace with:

```js
durableSessionId = typeof result.openCodeSessionId === "string"
  ? result.openCodeSessionId
  : (typeof result.sessionId === "string" ? result.sessionId : null);
```

Effect: the conversation→session mapping table actually fills up for Hermes
(Hermes returns the standard ACP `sessionId`, not OpenCode's proprietary field).

---

## Patch ④ — Send `mcpServers` on `session/load`

Same file, the resume request:

```js
writeRpc(nextId, "session/load", { sessionId: resumeSessionId, cwd: effectiveCwd, mcpServers: mcpServers ?? [] }, "session/load");
```

Effect: Hermes' ACP schema validates `mcpServers` as required; without this
the resume dies with `json-rpc id 2: Invalid params`.

---

## Patch ⑤ — Echo `sessionId` in the load response (Hermes side)

`<hermes-agent>/venv/Lib/site-packages/acp/schema.py`:

```python
class LoadSessionResponse(BaseModel):
    ...
    session_id: Annotated[Optional[str], Field(alias="sessionId")] = None
```

`<hermes-agent>/acp_adapter/server.py` → `load_session` return:

```python
return LoadSessionResponse(..., session_id=session_id, ...)
```

Effect: OD validates the load response against the `session/new` shape and
rejects it without `sessionId`. (Hermes' acp process is spawned per turn —
this patch is live on the next message, no restart.)

---

## Patch ⑥ — Persist the user's voice, not the envelope (Hermes side)

`acp_adapter/server.py`:

```python
def _extract_user_voice(text: str) -> str:
    """Keep only what the user typed; the model still gets the full envelope."""
    import re as _re
    if not text:
        return text
    m = _re.search(r"(?:^|\n)#{1,4}\s*(?:user|User request)\s*\r?\n(?:\r?\n)?", text)
    if m:
        tail = text[m.end():].strip()
        if tail:
            return tail
    return text
```

then at the `run_conversation` call site:

```python
persist_user_message=_extract_user_voice(user_text) or "[Image attachment]",
```

Pitfalls that bit us in production:

- `import re` **inside** the function (or verify the module header) — a missing
  import surfaces as `name 're' is not defined` at runtime, killing the turn.
- The regex must cover **both** tail markers: `## user` and `# User request`.
  Covering only one silently stores envelopes for the other half of turns.
- Test with a **real** envelope pulled from your DB, not a synthetic one —
  our synthetic test passed while the real-world variant failed.

---

## Patch ⑦ — Skip history replay for clients that keep their own transcript (Hermes side)

OD renders its own history; Hermes' ACP resume replay duplicates it into the
chat window (hours-old replies re-rendered on every message).

1. `acp_adapter/session.py` → `SessionState`:

```python
skip_history_replay: bool = False
```

2. `acp_adapter/server.py` → `initialize`: remember the client

```python
self._od_client_name = client_name
```

3. `load_session` / `resume_session`, right after `update_cwd`:

```python
state.skip_history_replay = "open-design" in getattr(self, "_od_client_name", "").lower()
```

4. Wrap both replay calls:

```python
if not getattr(state, "skip_history_replay", False):
    try:
        await self._replay_session_history(state)
    except Exception:
        ...  # (indent the whole except block one level deeper)
```

Zed & co. are unaffected — they don't announce as `open-design`.

---

## Patch ① — (bonus) 16:9 image size mapping

Same chunks dir, `chunk-N7AF5FHP.mjs`, `AIHUBMIX_IMAGE_ASPECT_TO_SIZE` →
`2752x1536` for 16:9. Unrelated to sessions but lives next door.

---

## Maintenance matrix

| Event | What breaks | Action |
|---|---|---|
| OD auto-update (new version dir) | patches ②③④ gone | re-apply to the new `versions/<v>/payload` |
| Hermes update / venv reinstall | patches ⑤⑥⑦ gone | re-apply to `acp/` + `acp_adapter/` |
| Never kill the daemon process | it's the ancestor of live agent sessions and changes ports on restart | close the OD GUI instead; daemon exits with it |
