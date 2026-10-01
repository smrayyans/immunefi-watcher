"""Command line entry point. Run with no arguments for the interactive UI."""

from __future__ import annotations

import argparse
import csv
import json
import sys

from rich.console import Console

from . import __version__, db, prepare as prep, query, sync as syncmod, view
from .util import ago, now_utc, parse_duration, parse_ts

console = Console()


def _conn(args):
    return db.connect(args.db)


def _ensure_data(conn) -> bool:
    if conn.execute("SELECT 1 FROM programs LIMIT 1").fetchone():
        return True
    console.print("[yellow]No data yet. Run `iw sync` first.[/]")
    return False


def _prog_json(p: query.Prog, q: query.Query) -> dict:
    now = now_utc()
    return {
        "slug": p.slug, "name": p.name, "max_bounty": p.max_bounty, "pays": p.pays, "has_web": p.has_web, "pools": p.pools,
        "rewards_token": p.rewards_token, "assets": len(p.assets),
        "new_assets": [{"url": a.url, "type": a.type, "added_at": a.when.isoformat() if a.when else None}
                       for a in (q.matching_assets(p, now) if q.has_asset_filter else p.new_assets(q.effective_window, now))],
        "languages": p.languages, "ecosystems": p.ecosystems, "kyc": p.kyc, "std": p.std, "paused": p.paused,
        "updated": p.updated.isoformat() if p.updated else None, "url": view.site_url(p.slug), "mark": p.mark,
    }


def cmd_sync(args) -> int:
    conn = _conn(args)
    try:
        with console.status("Fetching Immunefi program directory..."):
            res = syncmod.sync(conn, args.source)
    except syncmod.SyncError as e:
        console.print(f"[red]sync failed:[/] {e}")
        return 1
    console.print(f"[green]ok[/] {res['programs']} programs, {res['assets']} assets via {res['source']}")
    if res["first_run"]:
        console.print("First sync: baseline stored. New-asset detection works now from Immunefi's addedAt "
                      "dates; change events (bounty, policy, removals) start with the next sync.")
    else:
        console.print(f"{res['events']} changes: " + (", ".join(f"{k}={v}" for k, v in res["by_kind"].items()) or "none"))
    return 0


def cmd_ls(args) -> int:
    conn = _conn(args)
    if not _ensure_data(conn):
        return 1
    q = query.parse(" ".join(args.query))
    for e in q.errors:
        console.print(f"[red]{e}[/]")
    rows = q.run(query.load_programs(conn))
    if args.json:
        print(json.dumps([_prog_json(p, q) for p in rows[: args.limit]], indent=2))
    elif args.csv:
        w = csv.writer(sys.stdout)
        w.writerow(["slug", "name", "max_bounty", "total_pool", "token", "assets", "languages", "ecosystems", "url"])
        for p in rows[: args.limit]:
            w.writerow([p.slug, p.name, p.max_bounty, p.total_pool, p.rewards_token, len(p.assets),
                        "|".join(p.languages), "|".join(p.ecosystems), view.site_url(p.slug)])
    else:
        console.print(view.programs_table(rows, q, args.limit))
        console.print(f"[dim]{len(rows)} programs  {' · '.join(q.notes)}[/]")
    return 0


def cmd_new(args) -> int:
    conn = _conn(args)
    if not _ensure_data(conn):
        return 1
    text = " ".join(args.query)
    if args.days and "new:" not in text and "added:" not in text:
        text += f" new:{args.days}"
    q = query.parse(text)
    for e in q.errors:
        console.print(f"[red]{e}[/]")
    rows = q.feed(query.load_programs(conn))
    if args.json:
        print(json.dumps([{"program": p.slug, "name": p.name, "max_bounty": p.max_bounty, "type": a.type,
                           "url": a.url, "description": a.description,
                           "added_at": a.when.isoformat() if a.when else None} for p, a in rows[: args.limit]], indent=2))
    else:
        console.print(view.feed_table(rows, args.limit))
        console.print(f"[dim]{len(rows)} assets added in the last {query.fmt_duration(q.effective_window)}[/]")
    return 0


def cmd_show(args) -> int:
    conn = _conn(args)
    if not _ensure_data(conn):
        return 1
    progs = query.load_programs(conn)
    hits = [p for p in progs if p.slug == args.slug] or [p for p in progs if args.slug.lower() in p.slug or args.slug.lower() in p.name.lower()]
    if not hits:
        console.print(f"[red]no program matching {args.slug!r}[/]")
        return 1
    if len(hits) > 1:
        console.print("[yellow]ambiguous:[/] " + ", ".join(p.slug for p in hits[:15]))
        return 1
    p = hits[0]
    d = query.program_data(conn, p.slug)
    win = parse_duration(args.window)
    console.rule(f"[bold]{p.name}")
    sections = {"overview": lambda: view.overview(p, d, win), "scope": lambda: view.scope(p, win),
                "rewards": lambda: view.rewards(p, d), "rules": lambda: view.rules(p, d), "known": lambda: view.known(p, d)}
    for name in (args.section or list(sections)):
        console.rule(f"[dim]{name}")
        console.print(sections[name]())
    return 0


def cmd_events(args) -> int:
    conn = _conn(args)
    cut = (now_utc() - parse_duration(args.since)).strftime("%Y-%m-%dT%H:%M:%S")
    sql = "SELECT * FROM events WHERE ts >= ?"
    params: list = [cut]
    if args.kind:
        sql += " AND kind=?"
        params.append(args.kind)
    if args.slug:
        sql += " AND slug=?"
        params.append(args.slug)
    rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", params + [args.limit]).fetchall()
    names = {r["slug"]: r["name"] for r in conn.execute("SELECT slug,name FROM programs")}
    console.print(view.events_table(rows, names))
    s = syncmod.last_sync(conn)
    console.print(f"[dim]{len(rows)} events. last sync: {(ago(parse_ts(s['ts'])) + (' ago' if ago(parse_ts(s['ts'])) != 'now' else '')) if s else 'never'}[/]")
    return 0


def cmd_mark(args) -> int:
    conn = _conn(args)
    status = "" if args.status == "clear" else args.status
    with conn:
        if status:
            conn.execute("INSERT INTO marks(slug,status,note,ts) VALUES(?,?,?,?) ON CONFLICT(slug) DO UPDATE SET "
                         "status=excluded.status,note=excluded.note,ts=excluded.ts",
                         (args.slug, status, " ".join(args.note), now_utc().isoformat()))
        else:
            conn.execute("DELETE FROM marks WHERE slug=?", (args.slug,))
    console.print(f"{args.slug}: {status or 'cleared'}")
    return 0


def cmd_prepare(args) -> int:
    conn = _conn(args)
    if not _ensure_data(conn):
        return 1
    progs = query.load_programs(conn)
    hits = [p for p in progs if p.slug == args.slug.lower()] or [
        p for p in progs if args.slug.lower() in p.slug or args.slug.lower() in p.name.lower()]
    if len(hits) != 1:
        console.print("[red]no match[/]" if not hits else "[yellow]ambiguous:[/] " + ", ".join(p.slug for p in hits[:15]))
        return 1
    st = prep.status(conn, hits[0].slug, args.dir)
    mode = "new" if args.force_new else "auto"
    if args.overwrite:
        mode = "overwrite"
        if st["scope_exists"] and not args.yes:
            console.print(f"[yellow]{st['scope_path']} already exists[/] ({st['scope_lines']} lines, "
                          f"modified {st['scope_modified']:%Y-%m-%d %H:%M}). A backup will be saved first.")
            if input("Overwrite it? [y/N] ").strip().lower() not in ("y", "yes"):
                console.print("kept existing SCOPE.md")
                return 0
    r = prep.prepare(conn, hits[0].slug, root=args.dir, mode=mode)
    console.print(f"folder: {r['folder']} ({'created' if r['created_folder'] else 'existed'})")
    if r["backup"]:
        console.print(f"backup: {r['backup']}")
    console.print(f"wrote:  {r['wrote']}" if r["wrote"] else
                  "SCOPE.md already exists, left untouched (use --force-new for SCOPE.new.md or --overwrite)")
    return 0


def cmd_tui(args) -> int:
    from .tui import run
    return run(args.db)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="iw", description="Immunefi program watcher. No arguments opens the UI.")
    ap.add_argument("--db", help="database path (default: $IW_DB or ~/.local/share/immunefi-watcher/iw.db)")
    ap.add_argument("--version", action="version", version=f"iw {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("sync", help="download the program directory and record changes")
    s.add_argument("--source", choices=["auto", "api", "github"], default="auto")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("ls", help="list programs matching a filter, e.g. iw ls new:7d bounty:>=100k lang:solidity")
    s.add_argument("query", nargs="*")
    s.add_argument("-n", "--limit", type=int, default=40)
    s.add_argument("--json", action="store_true")
    s.add_argument("--csv", action="store_true")
    s.set_defaults(fn=cmd_ls)

    s = sub.add_parser("new", help="feed of recently added assets, newest first")
    s.add_argument("query", nargs="*")
    s.add_argument("-d", "--days", default="7d", help="window, e.g. 3d, 12h, 2w (default 7d)")
    s.add_argument("-n", "--limit", type=int, default=60)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("show", help="full details for one program")
    s.add_argument("slug")
    s.add_argument("-s", "--section", action="append", choices=["overview", "scope", "rewards", "rules", "known"])
    s.add_argument("-w", "--window", default="7d")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("events", help="changes recorded between syncs")
    s.add_argument("--since", default="7d")
    s.add_argument("--kind")
    s.add_argument("--slug")
    s.add_argument("-n", "--limit", type=int, default=100)
    s.set_defaults(fn=cmd_events)

    s = sub.add_parser("mark", help="tag a program: hunt, skip, done, or clear")
    s.add_argument("slug")
    s.add_argument("status", choices=["hunt", "skip", "done", "clear"])
    s.add_argument("note", nargs="*")
    s.set_defaults(fn=cmd_mark)

    s = sub.add_parser("prepare", help="create bug-bounty/<slug>/SCOPE.md + targets.txt for a program")
    s.add_argument("slug")
    s.add_argument("--dir", help="parent directory (default: the bug-bounty folder)")
    s.add_argument("--force-new", action="store_true", help="if SCOPE.md exists, write SCOPE.new.md beside it")
    s.add_argument("--overwrite", action="store_true", help="replace SCOPE.md (asks first, saves a backup)")
    s.add_argument("-y", "--yes", action="store_true", help="skip the overwrite question")
    s.set_defaults(fn=cmd_prepare)

    s = sub.add_parser("ui", help="interactive terminal UI (default)")
    s.set_defaults(fn=cmd_tui)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        args.fn = cmd_tui
    try:
        return args.fn(args) or 0
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        return 2
    except KeyboardInterrupt:
        return 130
