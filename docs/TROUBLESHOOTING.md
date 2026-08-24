# Troubleshooting

Every failure we actually hit while building this, in the order you're likely to meet them.

## Patching has no effect at all

**You patched the install directory.** The running daemon loads from
`%APPDATA%\Open Design\launcher\channels\stable\namespaces\release-stable-win\versions\<VERSION>\payload\...`.
Prove it:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match "daemon-sidecar" } |
  Select-Object ProcessId, CreationDate, CommandLine
```

The `CommandLine` path *is* the runtime. Patch that. Also check the daemon
start time is **after** your patch mtime — an old daemon process means your
edit isn't loaded yet (fully quit OD and relaunch).

## OD crashes with an `EPIPE: broken pipe` dialog after restart

You launched OD from a bash background job (`("Open Design.exe" &)`). The
child inherits the terminal's stdout pipe; when the call returns the pipe
closes and the next `console.warn` kills the Electron main process.

Fix: kill all OD processes, then launch detached:

```powershell
Start-Process -FilePath "$env:APPDATA\Open Design\en\<hash>\Open Design.exe" `
  -WorkingDirectory "$env:APPDATA\Open Design\en\<hash>"
```

## Resume shows `json-rpc id 2: Invalid params`

Patch ④ missing — Hermes requires `mcpServers` in `session/load`.

## Resume shows `invalid session/new response` (but the JSON error contains `currentHermesSessionId`)

Good news buried in the error: the load itself worked. Patch ⑤ missing —
Hermes' load response must echo a `sessionId`.

## The mapping table stays empty (`agent_sessions` has 0 rows)

Patch ③ missing — OD only persists `openCodeSessionId`, which Hermes never
sends. Also note: capture happens when a turn **succeeds**; a cancelled turn
persists nothing.

## Sidebar still shows envelopes after patch ⑥

Three separate causes we hit, in order of likelihood:

1. **Regex covers only `## user`.** Real envelopes also use `# User request`.
   Test against a real envelope from your DB:
   ```sql
   SELECT content FROM messages WHERE role='user' AND length(content)>3000 LIMIT 1;
   ```
2. **`name 're' is not defined`** in the Hermes log — the helper uses `re`
   without importing it. Put `import re` inside the function.
3. Old rows were stored before the patch. Run `consolidate.py` pass 1 to
   translate existing history.

## Every new message first replays hours-old replies

Patch ⑦ missing (or the client-name check doesn't match — OD announces as
`open-design`; verify with a debug log of `client_info.name`).

## `sqlite3.OperationalError: database is locked` when pre-filling the OD mapping

OD is holding the DB. Either quit OD fully and re-run `consolidate.py`, or
skip pre-fill — resume failure degrades to a fresh session, which is exactly
the pre-fix behavior and loses nothing.

## `ON CONFLICT clause does not match any PRIMARY KEY` on the mapping table

`agent_sessions` has no unique constraint. Use `DELETE` + `INSERT` inside a
transaction instead of an upsert (see `consolidate.py`).

## After merging, resume feels heavy on the big project

Expected: the ledger holds the whole project history. Hermes' compressor
takes it from there; the ledger is primarily a readable archive. If a resume
is undesirable, keep chatting in the OD window as usual — a new session only
starts when you open a new OD window.

## OD updated and everything broke again

Expected too — see the maintenance matrix in [PATCHES.md](PATCHES.md).
`diagnose.py` tells you which patches are missing in 10 seconds.
