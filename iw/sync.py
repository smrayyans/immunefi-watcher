"""Fetch the Immunefi public directory and diff it against what is stored.

One request to /public-api/bounties.json already carries every program's full record
(scope, rewards, rules, known issues), so a sync is a single download.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter

import httpx

from .util import WEB, has_web, iso, now_utc, reward_letters, strip_web, web3_max_bounty

SOURCES = {
    "api": "https://immunefi.com/public-api/bounties.json",
    "github": "https://raw.githubusercontent.com/infosec-us-team/Immunefi-Bug-Bounty-Programs-Unofficial/main/projects.json",
}

# Parts of a program record whose text changes are worth a "policy_changed" event.
POLICY_FIELDS = [
    "description", "programOverview", "eligibilityCriteria", "outOfScopeAndRules",
    "customOutOfScopeInformation", "customProhibitedActivities", "defaultProhibitedActivities",
    "defaultOutOfScopeGeneral", "defaultOutOfScopeSmartContract", "defaultOutOfScopeWebAndApplications",
    "defaultOutOfScopeBlockchain", "defaultFeasibilityLimitations", "impacts", "impactsBody",
    "rewards", "rewardsBody", "assetsBodyV2", "prioritizedVulnerabilities", "knownIssues", "audits",
    "pocPerTypeAndSeverity", "features",
]


class SyncError(Exception):
    pass


def fetch(source: str = "auto", timeout: float = 90.0) -> tuple[list[dict], str]:
    order = {"auto": ["api", "github"], "api": ["api"], "github": ["github"]}[source]
    errors = []
    for name in order:
        try:
            r = httpx.get(
                SOURCES[name],
                timeout=httpx.Timeout(timeout, connect=15),
                headers={"User-Agent": "immunefi-watcher/0.1"},
                follow_redirects=True,
            )
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list) or not data or not all(isinstance(p, dict) and p.get("slug") for p in data):
                raise SyncError("unexpected response shape")
            return data, name
        except Exception as e:  # noqa: BLE001 - report and fall through to the next source
            errors.append(f"{name}: {e}")
    raise SyncError("; ".join(errors))


def _h(v) -> str:
    return hashlib.sha1(json.dumps(v, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _asset_id(a: dict) -> str:
    return str(a.get("id") or f"{a.get('type')}|{a.get('url')}")


def policy_parts(p: dict) -> dict:
    nw = strip_web(p)  # web/app changes are not tracked here
    return {f: _h(nw.get(f)) for f in POLICY_FIELDS}


def _row(p: dict) -> dict:
    pays, pays_known = reward_letters(strip_web(p)["rewards"])
    return {
        "pays": pays,
        "pays_known": pays_known,
        "has_web": int(has_web(p)),
        "slug": p["slug"],
        "name": p.get("project") or p["slug"],
        "max_bounty": web3_max_bounty(p),
        "primary_pool": _num(p.get("primaryPool")),
        "rewards_pool": _num(p.get("rewardsPool")),
        "allstars_pool": _num(p.get("allStarsPool")),
        "podium_pool": _num(p.get("podiumPool")),
        "rewards_token": p.get("rewardsToken") or "",
        "launch_date": p.get("launchDate"),
        "updated_date": p.get("updatedDate"),
        "is_paused": int(bool(p.get("isPaused"))),
        "invite_only": int(bool(p.get("inviteOnly"))),
        "kyc": int(bool(p.get("kyc"))),
        "immunefi_standard": int(bool(p.get("immunefiStandard"))),
        "languages": json.dumps(p.get("language") or []),
        "ecosystems": json.dumps(p.get("ecosystem") or []),
        "project_types": json.dumps(p.get("projectType") or []),
        "program_types": json.dumps(p.get("programType") or []),
        "features": json.dumps(p.get("features") or []),
        "policy_parts": json.dumps(policy_parts(p)),
        "data": json.dumps(p, separators=(",", ":")),
    }


_POOLS = ("primary_pool", "rewards_pool", "allstars_pool", "podium_pool")


def apply(conn: sqlite3.Connection, raw: list[dict], source: str) -> dict:
    """Upsert `raw` and record what changed. Returns a summary."""
    now = iso(now_utc())
    prev = {r["slug"]: r for r in conn.execute("SELECT * FROM programs")}
    active_prev = [s for s, r in prev.items() if not r["removed_at"]]
    if active_prev and len(raw) < 0.5 * len(active_prev):
        raise SyncError(f"response has {len(raw)} programs but {len(active_prev)} are tracked; refusing to apply")

    prev_assets = {
        (r["slug"], r["asset_id"]): r
        for r in conn.execute("SELECT * FROM assets WHERE removed_at IS NULL")
    }
    first_run = not prev
    events: list[tuple[str, str, dict]] = []  # (slug, kind, detail)
    seen_slugs: set[str] = set()
    seen_assets: set[tuple[str, str]] = set()
    total_assets = 0

    with conn:
        for p in raw:
            slug = p["slug"]
            seen_slugs.add(slug)
            row = _row(p)
            old = prev.get(slug)
            assets = [a for a in p.get("assets") or [] if a.get("type") != WEB]  # web/app scope is not stored

            if old is None:
                if not first_run:
                    events.append((slug, "new_program", {
                        "name": row["name"], "max_bounty": row["max_bounty"], "assets": len(assets),
                    }))
            else:
                if old["removed_at"]:
                    events.append((slug, "program_returned", {"name": row["name"]}))
                if bool(old["is_paused"]) != bool(row["is_paused"]):
                    events.append((slug, "paused" if row["is_paused"] else "unpaused", {}))
                if old["max_bounty"] != row["max_bounty"]:
                    events.append((slug, "bounty_changed", {"old": old["max_bounty"], "new": row["max_bounty"]}))
                for k in _POOLS:
                    if old[k] != row[k]:
                        events.append((slug, "pool_changed", {"pool": k, "old": old[k], "new": row[k],
                                                              "token": row["rewards_token"]}))
                if old["pays"] is not None and old["pays"] != row["pays"]:
                    events.append((slug, "tiers_changed", {"old": old["pays"], "new": row["pays"]}))
                old_parts = json.loads(old["policy_parts"] or "{}")
                new_parts = json.loads(row["policy_parts"])
                changed = [k for k, v in new_parts.items() if old_parts.get(k) not in (None, v)]
                if changed:
                    events.append((slug, "policy_changed", {"parts": changed}))

            cols = list(row)
            conn.execute(
                f"INSERT INTO programs({','.join(cols)},first_seen,last_seen,removed_at) "
                f"VALUES({','.join('?' * len(cols))},?,?,NULL) "
                f"ON CONFLICT(slug) DO UPDATE SET "
                + ",".join(f"{c}=excluded.{c}" for c in cols if c != "slug")
                + ",last_seen=excluded.last_seen,removed_at=NULL",
                [row[c] for c in cols] + [now, now],
            )

            for a in assets:
                aid = _asset_id(a)
                key = (slug, aid)
                seen_assets.add(key)
                total_assets += 1
                if key not in prev_assets and old is not None and not first_run:
                    events.append((slug, "asset_added", {
                        "url": a.get("url"), "type": a.get("type"), "added_at": a.get("addedAt"),
                        "description": (a.get("description") or "")[:200],
                    }))
                conn.execute(
                    "INSERT INTO assets(slug,asset_id,url,type,description,added_at,first_seen,last_seen,removed_at) "
                    "VALUES(?,?,?,?,?,?,?,?,NULL) "
                    "ON CONFLICT(slug,asset_id) DO UPDATE SET url=excluded.url,type=excluded.type,"
                    "description=excluded.description,added_at=excluded.added_at,"
                    "last_seen=excluded.last_seen,removed_at=NULL",
                    (slug, aid, a.get("url"), a.get("type"), a.get("description"), a.get("addedAt"), now, now),
                )

        for key, r in prev_assets.items():
            if key not in seen_assets and key[0] in seen_slugs:
                conn.execute("UPDATE assets SET removed_at=? WHERE slug=? AND asset_id=?", (now, *key))
                events.append((key[0], "asset_removed", {"url": r["url"], "type": r["type"]}))

        for slug in active_prev:
            if slug not in seen_slugs:
                conn.execute("UPDATE programs SET removed_at=? WHERE slug=?", (now, slug))
                conn.execute("UPDATE assets SET removed_at=? WHERE slug=? AND removed_at IS NULL", (now, slug))
                events.append((slug, "program_removed", {"name": prev[slug]["name"]}))

        conn.executemany(
            "INSERT INTO events(ts,slug,kind,detail) VALUES(?,?,?,?)",
            [(now, s, k, json.dumps(d)) for s, k, d in events],
        )
        conn.execute(
            "INSERT INTO sync_runs(ts,source,programs,assets,events) VALUES(?,?,?,?,?)",
            (now, source, len(raw), total_assets, len(events)),
        )

    return {
        "first_run": first_run, "source": source, "programs": len(raw), "assets": total_assets,
        "events": len(events), "by_kind": dict(Counter(k for _, k, _ in events)),
    }


def sync(conn: sqlite3.Connection, source: str = "auto") -> dict:
    raw, used = fetch(source)
    return apply(conn, raw, used)


def last_sync(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
