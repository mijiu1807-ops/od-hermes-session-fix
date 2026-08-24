#!/usr/bin/env python3
"""OD-Hermes Session Health Check (read-only).

Counts ACP session fragments, detects which integration defects apply,
and prints a fix plan. Changes nothing.

Usage:
    python diagnose.py
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

OD_APP = Path(os.environ.get("OD_APP_DB", Path.home() / "AppData/Roaming/Open Design/namespaces/release-stable-win/data/app.sqlite"))
HERMES_DB = Path(os.environ.get("HERMES_STATE_DB", Path.home() / "D:/HermesAgent/Data/profiles/design/state.db"))


def q(db: Path, sql: str, args: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def main() -> int:
    if not HERMES_DB.exists():
        print(f"[!] Hermes state DB not found at {HERMES_DB} (set HERMES_STATE_DB)")
        return 1
    print("=" * 62)
    print("OD ↔ Hermes Session Health Check (read-only)")
    print("=" * 62)

    # --- Hermes side ---
    total, acp = q(HERMES_DB, "SELECT count(*), sum(source='acp') FROM sessions")[0]
    print(f"\n[1] Sessions: {total} total, {acp} from ACP (OpenDesign)")

    frag = q(HERMES_DB, """
        SELECT count(*) FROM sessions s
        WHERE s.source='acp'
          AND (s.title LIKE '# Instructions%'
            OR s.title LIKE 'Set up%'
            OR s.title LIKE 'Configure%'
            OR s.title LIKE 'OpenDesign%'
            OR s.title LIKE 'Open Design%'
            OR s.title LIKE 'Example%'
            OR s.title LIKE 'Review%'
            OR s.title LIKE '%ask mode%'
            OR s.title LIKE '%charter%')""")[0][0]
    env_msgs = q(HERMES_DB, """
        SELECT count(*) FROM messages m JOIN sessions s ON m.session_id=s.id
        WHERE m.role='user' AND s.source='acp'
          AND (m.content LIKE '# Instructions%' OR m.content LIKE '%# User request%'
               OR m.content LIKE '%' || char(10) || '## user%')""")[0][0]
    print(f"[2] Template-titled fragments: {frag}")
    print(f"[3] Envelope-wrapped user messages: {env_msgs}")

    # --- OD side ---
    mapping_rows = []
    if OD_APP.exists():
        mapping_rows = q(OD_APP, "SELECT conversation_id, session_id FROM agent_sessions")
        print(f"[4] OD→Hermes session mappings: {len(mapping_rows)}")
    else:
        print(f"[4] OD app DB not found at {OD_APP} (set OD_APP_DB)")

    # --- Verdict ---
    print("\n" + "-" * 62)
    issues = []
    if frag > 5:
        issues.append(f"{frag} template-named fragments → session reuse (patch ②) not active")
    if env_msgs > 0:
        issues.append(f"{env_msgs} envelopes stored raw → transcript hygiene (patch ⑥) not active")
    if acp and len(mapping_rows) < 2:
        issues.append("OD mapping table nearly empty → capture chain (patch ③) not active")
    if not issues:
        print("All clear: session reuse, transcript hygiene and mapping look healthy.")
        return 0
    print("Detected issues:")
    for i in issues:
        print(f"  - {i}")
    print("\nFix plan: see docs/PATCHES.md, then run scripts/consolidate.py for history.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
