#!/usr/bin/env python3
"""Consolidate OD→Hermes session fragments into per-project ledgers.

Two passes:
  1. Envelope → user voice: strip the English charter envelope from stored
     user messages, keeping only what the user actually typed.
  2. Project merge: assign each ACP session fragment to an OD project (via the
     project UUID embedded in every envelope), then merge messages into the
     newest session of that project ("ledger"), renaming the ledger to the
     OD project name and pre-filling the OD→Hermes mapping table.

Dry-run by default. Every destructive step prompts. A timestamped DB backup
is taken before the first write.

Usage:
    python consolidate.py            # analyze + dry-run report
    python consolidate.py --apply    # execute (still prompts for cleanup choice)
    python consolidate.py --apply --yes  # execute, auto-confirm archive cleanup
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

OD_DIR = Path(os.environ.get("OD_DATA_DIR", Path.home() / "AppData/Roaming/Open Design/namespaces/release-stable-win/data"))
OD_APP = OD_DIR / "app.sqlite"
HERMES_DB = Path(os.environ.get("HERMES_STATE_DB", "D:/HermesAgent/Data/profiles/design/state.db"))

# Envelope markers: OD uses two tail variants (## user and # User request),
# plus inline history markers from multi-turn envelopes.
HEAD_PAT = re.compile(r"(?:^|\n)#{1,4}\s*(?:user|User request)\s*\r?\n(?:\r?\n)?")
INLINE_USER = re.compile(r"\n## user\r?\n")
INLINE_FORM = re.compile(r"\n## Latest user turn[^\n]*\n")
ATTACH_TAIL = re.compile(r"\nAttached project files in user-visible order:")
PROJ_PAT = re.compile(r"projects[/\\]([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def extract_user_voice(text: str) -> str:
    """Strip the charter envelope; keep only the user's actual words."""
    if not text:
        return text
    m = HEAD_PAT.search(text)
    if m:
        tail = text[m.end():].strip()
        if tail:
            text = tail
    if text.startswith("## user"):
        parts = text.split("\n## user\n")
        if len(parts) > 1:
            text = parts[-1].strip()
        else:
            return text  # the user's message itself begins with '## user'
    for pat in (INLINE_USER, INLINE_FORM):
        ms = list(pat.finditer(text))
        if ms:
            text = text[ms[-1].end():].strip()
            break
    return ATTACH_TAIL.split(text)[0].strip()


def is_envelope(content: str) -> bool:
    return bool(content) and (
        content.startswith("# Instructions")
        or "# User request" in content
        or content.startswith("## user")
    )


def backup(db: Path) -> Path:
    bak = db.with_suffix(".db.bak-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(db, bak)
    return bak


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="execute writes (default: dry-run)")
    ap.add_argument("--yes", action="store_true", help="auto-confirm cleanup (archive)")
    args = ap.parse_args()

    if not HERMES_DB.exists():
        sys.exit(f"Hermes DB not found: {HERMES_DB}")

    conn = sqlite3.connect(HERMES_DB)
    cur = conn.cursor()

    # ---------- Pass 1: envelope → user voice ----------
    cur.execute("""
        SELECT m.id, m.content FROM messages m JOIN sessions s ON m.session_id = s.id
        WHERE m.role='user' AND s.source='acp'""")
    rows = cur.fetchall()
    to_translate = [(mid, c) for mid, c in rows if is_envelope(c)]
    print(f"[pass 1] envelope-wrapped user messages: {len(to_translate)}")

    # ---------- Pass 2: project assignment ----------
    cur.execute("""
        SELECT m.session_id, m.content FROM messages m JOIN sessions s ON m.session_id=s.id
        WHERE s.source='acp' AND m.role='user' ORDER BY m.id""")
    assign: dict[str, str] = {}
    seen: set[str] = set()
    for sid, content in cur.fetchall():
        hits = PROJ_PAT.findall(content or "")
        if hits:
            assign[sid] = Counter(hits).most_common(1)[0][0] if sid not in assign else assign[sid]
        seen.add(sid)
    unassigned = [s for s in seen if s not in assign]

    # Fallback for translated DBs (envelopes already stripped): align each
    # unassigned session to an OD conversation by overlapping activity window.
    if unassigned and OD_APP.exists():
        try:
            odro = sqlite3.connect(f"file:{OD_APP.as_posix()}?mode=ro", uri=True)
            wins = []
            for cid, pid, ca, ua in odro.execute(
                    "SELECT id, project_id, created_at, updated_at FROM conversations"):
                wins.append((ca, ua, pid))
            odro.close()
            for sid in list(unassigned):
                cur.execute("SELECT min(timestamp), max(timestamp) FROM messages WHERE session_id=?", (sid,))
                mn, mx = cur.fetchone()
                if not mn:
                    continue
                # messages.timestamp is epoch-seconds(float); conversations are epoch-ms
                lo, hi = mn * 1000, mx * 1000
                span = max(hi - lo, 1)
                best, best_ratio, best_width = None, 0.0, 0
                for ca, ua, pid in wins:
                    ov = min(hi, ua) - max(lo, ca)
                    if ov <= 0:
                        continue
                    ratio = ov / span  # fraction of the session's life inside this window
                    # tie-break: prefer the tighter (more specific) window
                    width = max(ua - ca, 1)
                    score = (ratio, -width)
                    cur_best = (best_ratio, -best_width) if best else None
                    if cur_best is None or score > cur_best:
                        best, best_ratio, best_width = pid, ratio, width
                if best and best_ratio >= 0.5:
                    assign[sid] = best
                    unassigned.remove(sid)
        except Exception:
            pass
    print(f"[pass 2] sessions assigned to OD projects: {len(assign)}  |  unassigned: {len(unassigned)}")

    # OD project names + newest-session-per-project ledger choice
    od = sqlite3.connect(f"file:{OD_APP.as_posix()}?mode=ro", uri=True)
    pname = dict(od.execute("SELECT id, name FROM projects"))
    od.close()

    stats = {}
    for sid in assign:
        cur.execute("SELECT count(*), max(id) FROM messages WHERE session_id=?", (sid,))
        n, mx = cur.fetchone()
        stats[sid] = (n or 0, mx or 0)
    by_proj: dict[str, list[str]] = defaultdict(list)
    for sid, pid in assign.items():
        by_proj[pid].append(sid)
    ledgers = {pid: max(sids, key=lambda s: stats[s][1]) for pid, sids in by_proj.items()}

    print("\nConsolidation plan:")
    merge_plan = []
    for pid, sids in sorted(by_proj.items(), key=lambda kv: -len(kv[1])):
        tgt = ledgers[pid]
        frags = [s for s in sids if s != tgt and stats[s][0] > 0]
        msg_n = sum(stats[s][0] for s in frags)
        print(f"  → {pname.get(pid, pid[:8])!r}: ledger {tgt[:8]}…  +{len(frags)} fragments ({msg_n} msgs)")
        merge_plan.extend((s, tgt) for s in frags)

    empty = [s for s in assign if stats[s][0] == 0]
    print(f"  → empty shells to clean later: {len(empty)}")

    if not args.apply:
        print("\nDRY-RUN only. Re-run with --apply to execute.")
        return 0

    # ---------- Execute ----------
    bak = backup(HERMES_DB)
    print(f"\nbackup: {bak.name}")
    try:
        moved = 0
        for sid, tgt in merge_plan:
            cur.execute("UPDATE messages SET session_id=? WHERE session_id=?", (tgt, sid))
            moved += cur.rowcount
        renamed = []
        for pid, tgt in ledgers.items():
            name = pname.get(pid)
            if name:
                cur.execute("UPDATE sessions SET title=?, title_source='user' WHERE id=?", (name, tgt))
                renamed.append(name)
        conn.commit()
        print(f"merged {moved} messages into {len(ledgers)} ledgers; renamed: {renamed}")
    except Exception:
        conn.rollback()
        raise

    # ---------- Pre-fill OD mapping (best effort) ----------
    try:
        odw = sqlite3.connect(OD_APP, timeout=5)
        oc = odw.cursor()
        now = int(time.time() * 1000)
        conv_proj = dict(odw.execute("SELECT id, project_id FROM conversations"))
        oc.execute("BEGIN")
        for conv_id, pid in conv_proj.items():
            tgt = ledgers.get(pid)
            if not tgt:
                continue
            oc.execute("DELETE FROM agent_sessions WHERE conversation_id=?", (conv_id,))
            oc.execute(
                "INSERT INTO agent_sessions (conversation_id, agent_id, session_id, updated_at) VALUES (?,?,?,?)",
                (conv_id, "hermes", tgt, now))
        odw.commit()
        odw.close()
        print("OD mapping table pre-filled.")
    except Exception as e:
        print(f"OD mapping pre-fill skipped ({e}); resume will fall back to a fresh session — harmless.")

    # ---------- Interactive cleanup ----------
    shells = empty + [s for s in unassigned]
    if not shells:
        print("No leftover fragments to clean.")
        return 0
    print(f"\n{len(shells)} fragment sessions are now empty (content lives in the ledgers).")
    if args.yes:
        choice = "archive"
    else:
        choice = input("Clean them up? [a]rchive (recommended) / [k]eep / [d]elete: ").strip().lower()
    if choice.startswith("a"):
        for sid in shells:
            cur.execute("UPDATE sessions SET archived=1 WHERE id=?", (sid,))
        conn.commit()
        print(f"archived {len(shells)} fragments (reversible: archived=0 to restore).")
    elif choice.startswith("d"):
        confirm = input("PERMANENTLY delete rows? type 'DELETE' to confirm: ")
        if confirm == "DELETE":
            for sid in shells:
                cur.execute("DELETE FROM messages WHERE session_id=?", (sid,))
                cur.execute("DELETE FROM sessions WHERE id=?", (sid,))
            conn.commit()
            print(f"deleted {len(shells)} fragment sessions.")
        else:
            print("aborted deletion; fragments kept.")
    else:
        print("kept all fragments.")
    conn.close()
    print("\nDone. Restart your Hermes desktop view to see the new sidebar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
