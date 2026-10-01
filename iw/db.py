"""SQLite storage."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def default_path() -> Path:
    env = os.environ.get("IW_DB")
    if env:
        return Path(env).expanduser()
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "immunefi-watcher" / "iw.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS programs(
  slug TEXT PRIMARY KEY,
  name TEXT, max_bounty REAL, primary_pool REAL, rewards_pool REAL, allstars_pool REAL, podium_pool REAL,
  rewards_token TEXT, launch_date TEXT, updated_date TEXT,
  is_paused INTEGER, invite_only INTEGER, kyc INTEGER, immunefi_standard INTEGER,
  languages TEXT, ecosystems TEXT, project_types TEXT, program_types TEXT, features TEXT,
  policy_parts TEXT, data TEXT, pays TEXT, pays_known TEXT, has_web INTEGER DEFAULT 0,
  first_seen TEXT, last_seen TEXT, removed_at TEXT
);
CREATE TABLE IF NOT EXISTS assets(
  slug TEXT, asset_id TEXT, url TEXT, type TEXT, description TEXT,
  added_at TEXT, first_seen TEXT, last_seen TEXT, removed_at TEXT,
  PRIMARY KEY(slug, asset_id)
);
CREATE INDEX IF NOT EXISTS assets_added ON assets(added_at);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, slug TEXT, kind TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS sync_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, programs INTEGER, assets INTEGER, events INTEGER
);
CREATE TABLE IF NOT EXISTS marks(
  slug TEXT PRIMARY KEY, status TEXT, note TEXT, ts TEXT
);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring older databases up to date and recompute derived columns from the stored records.

    v1: pays / pays_known columns.  v2/v3: has_web column, web/app data excluded from max bounty, tiers,
    policy hashes and the assets table (so no spurious change events appear on the next sync).
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 3:
        return
    import json

    from .sync import policy_parts
    from .util import WEB, has_web, reward_letters, strip_web, web3_max_bounty

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(programs)")}
    with conn:
        for c, decl in (("pays", "TEXT"), ("pays_known", "TEXT"), ("has_web", "INTEGER DEFAULT 0")):
            if c not in cols:
                conn.execute(f"ALTER TABLE programs ADD COLUMN {c} {decl}")
        for r in conn.execute("SELECT slug, data FROM programs").fetchall():
            try:
                d = json.loads(r["data"] or "{}")
            except ValueError:
                continue
            listed, known = reward_letters(strip_web(d)["rewards"])
            conn.execute(
                "UPDATE programs SET max_bounty=?, pays=?, pays_known=?, has_web=?, policy_parts=? WHERE slug=?",
                (web3_max_bounty(d), listed, known, int(has_web(d)), json.dumps(policy_parts(d)), r["slug"]))
        conn.execute("DELETE FROM assets WHERE type=?", (WEB,))
        conn.execute("PRAGMA user_version=3")
