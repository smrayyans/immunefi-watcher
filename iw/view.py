"""Rich renderables shared by the CLI (`iw show`) and the TUI detail pane."""

from __future__ import annotations

import json

from rich.console import Group
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from .query import ASSET_TYPES, Asset, Prog
from .util import ago, fmt_money, fmt_num, now_utc, parse_ts

SITE = "https://immunefi.com/bug-bounty/{slug}/"
MARK_GLYPH = {"": " ", "hunt": "▶", "skip": "✗", "done": "✓"}
MARK_STYLE = {"hunt": "bold green", "skip": "dim red", "done": "cyan", "": ""}


def site_url(slug: str) -> str:
    return SITE.format(slug=slug)


def flags(p: Prog) -> str:
    out = []
    if p.paused:
        out.append("PAUSED")
    if p.invite:
        out.append("INVITE")
    if p.kyc:
        out.append("KYC")
    if p.std:
        out.append("STD")
    return " ".join(out)


def paid_text(p: Prog) -> Text:
    return Text("Yes", style="bold yellow") if p.paid_submission else Text("No", style="dim")


def pays_text(p: Prog) -> Text:
    """Severities the program pays for (it lists a reward row), as letters in L M H C order."""
    return Text(p.pays, style="bold") if p.pays else Text("-", style="dim")


def short_name(name: str, width: int = 28) -> str:
    return name if len(name) <= width else name[: width - 1] + "…"


WEB_MARK = "W"  # shown before the program name when it also has a web/app program


_NAME_STYLE = {"hunt": "bold green", "skip": "dim red strike", "done": "cyan"}


def name_cell(p: Prog, width: int = 24) -> Text:
    """[W ]name[ mark]: W when the program also has a web side; my mark (hunt/skip/done) colours the name and
    adds a glyph after it, so no separate mark column is needed."""
    glyph = MARK_GLYPH[p.mark] if p.mark else ""
    name = short_name(p.name, width - 2 if glyph else width)
    return Text.assemble((WEB_MARK + " ", "bold cyan") if p.has_web else "  ",
                         (name, _NAME_STYLE.get(p.mark, "bold")),
                         (f" {glyph}", MARK_STYLE[p.mark]) if glyph else "")


def pool_text(p: Prog) -> str:
    if not p.total_pool:
        return "-"
    return f"{fmt_num(p.total_pool)} {p.rewards_token}".strip()


def asset_kind(a: Asset) -> str:
    return ASSET_TYPES.get(a.type, a.type or "?")


EVENT_STYLE = {
    "asset_added": "bold green", "new_program": "bold cyan", "program_returned": "cyan",
    "unpaused": "green", "paused": "yellow", "bounty_changed": "magenta", "pool_changed": "magenta",
    "policy_changed": "yellow", "tiers_changed": "magenta", "asset_removed": "red", "program_removed": "red",
}


def event_detail(kind: str, d: dict) -> str:
    if kind == "asset_added":
        return f"[{ASSET_TYPES.get(d.get('type'), d.get('type'))}] {d.get('url', '')}"
    if kind == "new_program":
        return f"{d.get('name', '')}: max {fmt_money(d.get('max_bounty'))}, {d.get('assets', 0)} assets"
    if kind == "bounty_changed":
        return f"max bounty {fmt_money(d.get('old'))} -> {fmt_money(d.get('new'))}"
    if kind == "pool_changed":
        return f"{d.get('pool', '').replace('_pool', '')} pool {fmt_num(d.get('old'))} -> {fmt_num(d.get('new'))} {d.get('token', '')}"
    if kind == "tiers_changed":
        return f"pays {d.get('old') or '-'} -> {d.get('new') or '-'}"
    if kind == "policy_changed":
        return "changed: " + ", ".join(d.get("parts", []))
    if kind == "asset_removed":
        return f"[{ASSET_TYPES.get(d.get('type'), d.get('type'))}] {d.get('url', '')}"
    return d.get("name", "") if isinstance(d, dict) else ""


# -------------------------------------------------------------------------- text blobs

_PLACEHOLDERS = {"_blank_", ".", "-", "n/a", "none", "null"}


def to_text(v) -> str:
    """Flatten the API's str / list / dict fields into something readable."""
    if v is None:
        return ""
    if isinstance(v, str):
        return "" if v.strip().lower() in _PLACEHOLDERS else v
    if isinstance(v, list):
        return "\n".join(f"- {to_text(x)}" if not isinstance(x, str) else f"- {x}" for x in v)
    if isinstance(v, dict):
        return "\n".join(f"**{k}**: {to_text(x)}" for k, x in v.items())
    return str(v)


def _section(title: str, body) -> list:
    txt = to_text(body).strip()
    if not txt:
        return []
    return [Text(f"\n{title}", style="bold underline"), Markdown(txt)]


# -------------------------------------------------------------------------- detail panes

def overview(p: Prog, d: dict, window) -> Group:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()
    now = now_utc()
    new = p.new_assets(window, now)
    t.add_row("slug", f"{p.slug}  ({site_url(p.slug)})")
    t.add_row("max bounty", Text(fmt_money(p.max_bounty), style="bold"))
    t.add_row("pays", Text.assemble((p.pays, "bold"), ("   L M H C = low medium high critical",
                                                      "dim")) if p.pays else "-  (no reward table)")
    if p.has_web:
        t.add_row("web side", Text(f"{WEB_MARK}  also has a web/app program with separate payouts (not shown in this tool)",
                                   style="cyan"))
    if p.total_pool:
        parts = ", ".join(f"{k} {fmt_num(v)}" for k, v in p.pools.items() if v)
        t.add_row("pools", f"{parts} {p.rewards_token}")
    t.add_row("assets", f"{len(p.assets)}   new in window: {len(new)}   newest: {ago(p.newest_asset, now)} ago")
    t.add_row("launched", f"{p.launch:%Y-%m-%d} ({ago(p.launch, now)} ago)" if p.launch else "-")
    t.add_row("updated", f"{p.updated:%Y-%m-%d} ({ago(p.updated, now)} ago)" if p.updated else "-")
    if d.get("endDate"):
        t.add_row("ends", str(d["endDate"])[:10])
    t.add_row("language", ", ".join(p.languages) or "-")
    t.add_row("ecosystem", ", ".join(p.ecosystems) or "-")
    t.add_row("type", ", ".join(p.project_types + p.program_types) or "-")
    t.add_row("features", ", ".join(p.features) or "-")
    t.add_row("paid submission", Text("Yes (Pay to Submit)", style="bold yellow") if p.paid_submission else "No (free)")
    t.add_row("flags", flags(p) or "-")
    t.add_row("payout wallet", str(d.get("primaryPaymentWallet") or "-"))
    t.add_row("10% rule", "yes" if d.get("tenPercentEconomicRule") else "no")
    if d.get("githubUrl"):
        t.add_row("github", d["githubUrl"])
    if d.get("websiteUrl"):
        t.add_row("website", d["websiteUrl"])
    if p.mark:
        t.add_row("my mark", Text(p.mark + (f" - {p.note}" if p.note else ""), style=MARK_STYLE[p.mark]))
    body = [t]
    body += _section("Overview", d.get("programOverview") or d.get("description"))
    body += _section("Eligibility", d.get("eligibilityCriteria"))
    body += _section("PoC required for", d.get("pocPerTypeAndSeverity"))
    return Group(*body)


def scope(p: Prog, window, hl_asset: str | None = None) -> Group:
    now = now_utc()
    cut = now - window
    assets = sorted(p.assets, key=lambda a: a.when or parse_ts("1970-01-01T00:00:00Z"), reverse=True)
    t = Table(box=None, pad_edge=False, expand=True, header_style="bold dim")
    t.add_column("Added", no_wrap=True)
    t.add_column("Type", no_wrap=True)
    t.add_column("Asset", overflow="fold", ratio=3)
    t.add_column("Notes", overflow="fold", ratio=2, style="dim")
    for a in assets:
        is_new = bool(a.when and a.when >= cut)
        when = ago(a.when, now) if a.when else "-"
        style = "bold green" if is_new else ""
        if hl_asset and a.id == hl_asset:
            style = "reverse"
        t.add_row(Text(("NEW " if is_new else "") + when, style=style), asset_kind(a),
                  Text(a.url, style=style), a.description)
    head = Text(f"{len(assets)} assets in scope, {sum(1 for a in assets if a.when and a.when >= cut)} new in window",
                style="bold")
    out = [head, t]
    return Group(*out)


def rewards(p: Prog, d: dict) -> Group:
    t = Table(box=None, pad_edge=False, header_style="bold dim")
    for c in ("Severity", "Asset type", "Min", "Max", "Model"):
        t.add_column(c)
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for r in sorted(d.get("rewards") or [], key=lambda r: (order.get(r.get("severity"), 9), str(r.get("assetType")))):
        model = r.get("rewardModel") or ""
        if r.get("rewardCalculationPercentage"):
            model += f" ({r['rewardCalculationPercentage']}%)"
        fixed = model.startswith("fixed") and not r.get("minReward") and not r.get("maxReward")
        t.add_row(r.get("severity", ""), ASSET_TYPES.get(r.get("assetType"), r.get("assetType") or ""),
                  fmt_money(r.get("minReward")), "see rules" if fixed else fmt_money(r.get("maxReward")), model)
    body: list = [t]
    if p.total_pool:
        body.append(Text(f"\nPools: " + ", ".join(f"{k} {fmt_num(v)}" for k, v in p.pools.items() if v)
                         + f" {p.rewards_token}", style="bold"))
    body += _section("Rewards details", d.get("rewardsBody"))
    return Group(*body)


def rules(p: Prog, d: dict) -> Group:
    body: list = []
    body += _section("Out of scope and rules", d.get("outOfScopeAndRules"))
    body += _section("Out of scope (program specific)", d.get("customOutOfScopeInformation"))
    for k, title in (("defaultOutOfScopeGeneral", "Out of scope: general"),
                     ("defaultOutOfScopeSmartContract", "Out of scope: smart contracts"),
                     ("defaultOutOfScopeWebAndApplications", "Out of scope: web and apps"),
                     ("defaultOutOfScopeBlockchain", "Out of scope: blockchain")):
        body += _section(title, d.get(k))
    body += _section("Prohibited activities", d.get("customProhibitedActivities") or d.get("defaultProhibitedActivities"))
    body += _section("Feasibility limitations", d.get("defaultFeasibilityLimitations"))
    body += _section("Prioritized vulnerabilities", d.get("prioritizedVulnerabilities"))
    imp = d.get("impacts") or []
    if imp:
        t = Table(box=None, pad_edge=False, header_style="bold dim")
        for c in ("Severity", "Type", "Impact"):
            t.add_column(c, overflow="fold")
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        for i in sorted(imp, key=lambda i: order.get(i.get("severity"), 9)):
            t.add_row(i.get("severity", ""), ASSET_TYPES.get(i.get("type"), i.get("type") or ""), i.get("title", ""))
        body += [Text("\nImpacts in scope", style="bold underline"), t]
    body += _section("Impacts notes", d.get("impactsBody"))
    return Group(*body) if body else Group(Text("(no rules text in the API record)", style="dim"))


def known(p: Prog, d: dict) -> Group:
    body: list = []
    ki = d.get("knownIssues") or []
    if ki:
        body.append(Text(f"{len(ki)} known issues", style="bold underline"))
        for k in ki:
            line = Text()
            if k.get("link"):
                line.append(str(k["link"]) + "\n", style="cyan")
            body += [line, Markdown(to_text(k.get("description")) or "(no description)"), Text("")]
    else:
        body.append(Text("No known issues listed in the API record.\n", style="dim"))
    au = d.get("audits") or []
    if au:
        t = Table(box=None, pad_edge=False, header_style="bold dim")
        for c in ("Date", "Auditor", "Report"):
            t.add_column(c, overflow="fold")
        for a in sorted(au, key=lambda a: a.get("date") or "", reverse=True):
            t.add_row(str(a.get("date") or "")[:10], a.get("auditor") or "", a.get("url") or "")
        body += [Text("Audits", style="bold underline"), t]
    return Group(*body)


# -------------------------------------------------------------------------- tables for the CLI

def programs_table(progs: list[Prog], q, limit: int | None = None) -> Table:
    now = now_utc()
    win = q.effective_window
    t = Table(box=None, pad_edge=False, header_style="bold dim")
    t.add_column("Program", style="bold", no_wrap=True)
    t.add_column("Paid sub")
    t.add_column("Max", justify="right")
    t.add_column("Pays")
    t.add_column(f"New({int(win.total_seconds() // 86400) or int(win.total_seconds() // 3600)}"
                 f"{'d' if win.total_seconds() >= 86400 else 'h'})", justify="right")
    t.add_column("Newest", justify="right")
    t.add_column("Assets", justify="right")
    t.add_column("Lang")
    t.add_column("Eco")
    t.add_column("Upd", justify="right")
    t.add_column("Flags", style="dim")
    for p in progs[:limit]:
        n = len(q.matching_assets(p, now)) if q.has_asset_filter else len(p.new_assets(win, now))
        t.add_row(name_cell(p), paid_text(p), fmt_money(p.max_bounty), pays_text(p),
                  Text(f"+{n}" if n else "", style="bold green"), ago(p.newest_asset, now), str(len(p.assets)),
                  ",".join(p.languages)[:18], ",".join(p.ecosystems)[:14], ago(p.updated, now), flags(p))
    return t


def feed_table(rows: list, limit: int | None = None) -> Table:
    now = now_utc()
    t = Table(box=None, pad_edge=False, header_style="bold dim")
    t.add_column("Added", justify="right", no_wrap=True)
    t.add_column("Program", style="bold", no_wrap=True)
    t.add_column("Max", justify="right")
    t.add_column("Type")
    t.add_column("Asset", overflow="fold")
    for p, a in rows[:limit]:
        t.add_row(ago(a.when, now), name_cell(p), fmt_money(p.max_bounty), asset_kind(a), a.url)
    return t


def events_table(rows, names: dict) -> Table:
    t = Table(box=None, pad_edge=False, header_style="bold dim")
    t.add_column("When", justify="right")
    t.add_column("Program", style="bold", no_wrap=True)
    t.add_column("Event")
    t.add_column("Detail", overflow="fold")
    now = now_utc()
    for r in rows:
        d = json.loads(r["detail"] or "{}")
        t.add_row(ago(parse_ts(r["ts"]), now), names.get(r["slug"], r["slug"]),
                  Text(r["kind"], style=EVENT_STYLE.get(r["kind"], "")), event_detail(r["kind"], d))
    return t
