"""Loading programs from the DB and the filter language.

Filter syntax (space separated, all conditions ANDed, `a,b` means a OR b, `-key:val` negates):

    new:7d            programs/assets added within 7 days (bare `new:` = 7d)
    bounty:>=100k     max bounty        (aliases: min:100k, max:1m)
    pool:>0           any prize pool    (bare `pool:` = any pool)
    lang:solidity,rust   eco:eth   type:defi   ptype:contract   feature:triage   token:usdc
    asset:contract|web|chain    url:github.com/foo    assets:>50
    age:<90d          program launched less than 90 days ago
    updated:<7d       program record updated within 7 days
    web:no            hide programs that also have a web/app side (web:yes = only those; marked W in the list)
    pays:hc           programs that pay for High and Critical (letters L M H C; pays:l = pays lows, -pays:l = not)
    paid:no           programs where submitting is free (paid:yes = "Pay to Submit" programs)
    kyc:no  std:yes  invite:no  paused:any  mark:hunt  -mark:skip
    sort:bounty|pool|new|newest|updated|launch|assets|name
    anything else     free text matched against name, slug and tags
"""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .util import fmt_duration, now_utc, parse_duration, parse_money, parse_ts, strip_web

DEFAULT_WINDOW = timedelta(days=7)

ASSET_TYPES = {
    "smart_contract": "contract", "websites_and_applications": "web", "blockchain_dlt": "chain",
}
_ASSET_ALIASES = {
    "contract": "smart_contract", "contracts": "smart_contract", "sc": "smart_contract",
    "smart_contract": "smart_contract", "smartcontract": "smart_contract",
    "web": "websites_and_applications", "app": "websites_and_applications", "apps": "websites_and_applications",
    "websites": "websites_and_applications", "websites_and_applications": "websites_and_applications",
    "chain": "blockchain_dlt", "dlt": "blockchain_dlt", "blockchain": "blockchain_dlt",
    "blockchain_dlt": "blockchain_dlt",
}
SORTS = ("bounty", "pool", "new", "newest", "updated", "launch", "assets", "name")


@dataclass
class Asset:
    slug: str
    id: str
    url: str
    type: str
    description: str
    added_at: datetime | None
    first_seen: datetime | None

    @property
    def when(self) -> datetime | None:
        """Best-known time this asset entered scope: Immunefi's addedAt, else when we first saw it."""
        return self.added_at or self.first_seen


@dataclass
class Prog:
    slug: str
    name: str
    max_bounty: float
    pools: dict
    rewards_token: str
    launch: datetime | None
    updated: datetime | None
    paused: bool
    invite: bool
    kyc: bool
    std: bool
    languages: list
    ecosystems: list
    project_types: list
    program_types: list
    features: list
    removed: bool
    pays: str = ""        # severities the program pays for (has a reward row), e.g. "HC" (letters in L M H C order)
    has_web: bool = False  # also has a web/app program with its own payouts (not shown in this tool)
    mark: str = ""
    note: str = ""
    assets: list = field(default_factory=list)

    @property
    def total_pool(self) -> float:
        return sum(self.pools.values())

    @property
    def paid_submission(self) -> bool:
        """Immunefi's "Pay to Submit" feature: the $ badge next to the program name on its page."""
        return any("pay to submit" in str(f).lower() for f in self.features)

    @property
    def newest_asset(self) -> datetime | None:
        ts = [a.when for a in self.assets if a.when]
        return max(ts) if ts else None

    def new_assets(self, window: timedelta, now: datetime | None = None) -> list:
        cut = (now or now_utc()) - window
        return [a for a in self.assets if a.when and a.when >= cut]


def load_programs(conn: sqlite3.Connection, include_removed: bool = False) -> list[Prog]:
    marks = {r["slug"]: r for r in conn.execute("SELECT * FROM marks")}
    by_slug: dict[str, list[Asset]] = {}
    for r in conn.execute("SELECT * FROM assets WHERE removed_at IS NULL"):
        by_slug.setdefault(r["slug"], []).append(Asset(
            r["slug"], r["asset_id"], r["url"] or "", r["type"] or "", r["description"] or "",
            parse_ts(r["added_at"]), parse_ts(r["first_seen"]),
        ))
    out = []
    cols = ("slug,name,max_bounty,primary_pool,rewards_pool,allstars_pool,podium_pool,rewards_token,launch_date,"
            "updated_date,is_paused,invite_only,kyc,immunefi_standard,languages,ecosystems,project_types,"
            "program_types,features,removed_at,pays,has_web")
    for r in conn.execute(f"SELECT {cols} FROM programs"):
        if r["removed_at"] and not include_removed:
            continue
        m = marks.get(r["slug"])
        out.append(Prog(
            slug=r["slug"], name=r["name"] or r["slug"], max_bounty=r["max_bounty"] or 0,
            pools={"primary": r["primary_pool"] or 0, "rewards": r["rewards_pool"] or 0,
                   "all-stars": r["allstars_pool"] or 0, "podium": r["podium_pool"] or 0},
            rewards_token=r["rewards_token"] or "",
            launch=parse_ts(r["launch_date"]), updated=parse_ts(r["updated_date"]),
            paused=bool(r["is_paused"]), invite=bool(r["invite_only"]), kyc=bool(r["kyc"]),
            std=bool(r["immunefi_standard"]),
            languages=json.loads(r["languages"] or "[]"), ecosystems=json.loads(r["ecosystems"] or "[]"),
            project_types=json.loads(r["project_types"] or "[]"),
            program_types=json.loads(r["program_types"] or "[]"), features=json.loads(r["features"] or "[]"),
            removed=bool(r["removed_at"]), pays=r["pays"] or "", has_web=bool(r["has_web"]), mark=(m["status"] if m else ""), note=(m["note"] if m else ""),
            assets=by_slug.get(r["slug"], []),
        ))
    return out


def program_data(conn: sqlite3.Connection, slug: str) -> dict:
    r = conn.execute("SELECT data FROM programs WHERE slug=?", (slug,)).fetchone()
    return strip_web(json.loads(r["data"])) if r else {}


# --------------------------------------------------------------------------- parsing

_OP = re.compile(r"^(>=|<=|>|<|=)?(.*)$")
_TRUE = {"yes", "y", "true", "1", "on"}
_FALSE = {"no", "n", "false", "0", "off"}


def _split_op(v: str) -> tuple[str, str]:
    m = _OP.match(v)
    return (m.group(1) or ">="), m.group(2)


def _cmp(op: str, a: float, b: float) -> bool:
    return {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b, "=": a == b}[op]


def _bool(v: str) -> bool | None:
    v = v.lower()
    return True if v in _TRUE else False if v in _FALSE else None


@dataclass
class Query:
    raw: str = ""
    prog_preds: list = field(default_factory=list)   # callable(Prog) -> bool
    asset_preds: list = field(default_factory=list)  # callable(Asset) -> bool
    window: timedelta | None = None                  # from new:/added:
    sort: str | None = None
    show_paused: bool = False
    notes: list = field(default_factory=list)        # human-readable interpretation
    errors: list = field(default_factory=list)

    @property
    def effective_window(self) -> timedelta:
        return self.window or DEFAULT_WINDOW

    # -- applying -----------------------------------------------------------------
    def asset_ok(self, a: Asset, now: datetime | None = None) -> bool:
        if self.window is not None:
            cut = (now or now_utc()) - self.window
            if not (a.when and a.when >= cut):
                return False
        return all(p(a) for p in self.asset_preds)

    @property
    def has_asset_filter(self) -> bool:
        return self.window is not None or bool(self.asset_preds)

    def matching_assets(self, p: Prog, now: datetime | None = None) -> list[Asset]:
        return [a for a in p.assets if self.asset_ok(a, now)]

    def prog_ok(self, p: Prog, now: datetime | None = None) -> bool:
        if p.paused and not self.show_paused:
            return False
        if not all(f(p) for f in self.prog_preds):
            return False
        if self.has_asset_filter and not self.matching_assets(p, now):
            return False
        return True

    def run(self, progs: list[Prog]) -> list[Prog]:
        now = now_utc()
        hits = [p for p in progs if self.prog_ok(p, now)]
        sort = self.sort or ("newest" if self.has_asset_filter else "bounty")
        w = self.effective_window

        def count_new(p: Prog) -> int:
            return len(self.matching_assets(p, now)) if self.has_asset_filter else len(p.new_assets(w, now))

        keyf = {
            "bounty": lambda p: (p.max_bounty, p.total_pool),
            "pool": lambda p: (p.total_pool, p.max_bounty),
            "new": lambda p: (count_new(p), p.newest_asset or _EPOCH),
            "newest": lambda p: (p.newest_asset or _EPOCH, p.max_bounty),
            "updated": lambda p: (p.updated or _EPOCH),
            "launch": lambda p: (p.launch or _EPOCH),
            "assets": lambda p: len(p.assets),
        }
        if sort == "name":
            hits.sort(key=lambda p: p.name.lower())
        else:
            hits.sort(key=keyf[sort], reverse=True)
        hits.sort(key=lambda p: not p.pays)  # stable: programs with no published rewards ("-") always go last
        return hits

    def feed(self, progs: list[Prog]) -> list[tuple[Prog, Asset]]:
        """Flat list of (program, asset), newest first. Uses the default window when none is given."""
        now = now_utc()
        win = self.effective_window
        cut = now - win
        out = []
        for p in progs:
            if p.paused and not self.show_paused:
                continue
            if not all(f(p) for f in self.prog_preds):
                continue
            for a in p.assets:
                if a.when and a.when >= cut and all(f(a) for f in self.asset_preds):
                    out.append((p, a))
        out.sort(key=lambda t: (t[1].when, t[0].max_bounty), reverse=True)
        return out


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _any_in(values: list, needles: list[str]) -> bool:
    low = [str(v).lower() for v in values]
    return any(n in v for n in needles for v in low)


def parse(text: str) -> Query:
    q = Query(raw=text)
    try:
        toks = shlex.split(text)
    except ValueError:  # unbalanced quote while typing
        toks = text.split()

    for tok in toks:
        neg = False
        t = tok
        if t.startswith("-") and ":" in t and len(t) > 1:
            neg, t = True, t[1:]
        if ":" not in t:
            term = t.lower()
            if not term:
                continue
            q.prog_preds.append(_with_neg(
                lambda p, term=term: term in p.name.lower() or term in p.slug
                or _any_in(p.ecosystems + p.languages + p.project_types + p.features, [term]), neg))
            q.notes.append(f"text~{term}")
            continue
        key, _, val = t.partition(":")
        key = key.lower()
        try:
            _compile(q, key, val, neg)
        except ValueError as e:
            q.errors.append(str(e))
    return q


def _with_neg(fn, neg: bool):
    return (lambda x: not fn(x)) if neg else fn


def _compile(q: Query, key: str, val: str, neg: bool) -> None:
    vals = [v.strip().lower() for v in val.split(",") if v.strip()]
    n = "!" if neg else ""

    if key in ("new", "added"):
        q.window = parse_duration(val) if val else DEFAULT_WINDOW
        q.notes.append(f"asset added <{fmt_duration(q.window)} ago")
    elif key in ("bounty", "min", "max"):
        if not val:
            raise ValueError(f"{key}: needs an amount, e.g. {key}:100k")
        op, num = _split_op(val)
        op = {"min": ">=", "max": "<="}.get(key, op) if not re.match(r"^(>=|<=|>|<|=)", val) else op
        amt = parse_money(num)
        q.prog_preds.append(_with_neg(lambda p: _cmp(op, p.max_bounty, amt), neg))
        q.notes.append(f"{n}bounty {op} {amt:,.0f}")
    elif key == "pool":
        if not val or val.lower() in _TRUE:
            q.prog_preds.append(_with_neg(lambda p: p.total_pool > 0, neg))
            q.notes.append(f"{n}has pool")
        else:
            op, num = _split_op(val)
            amt = parse_money(num)
            q.prog_preds.append(_with_neg(lambda p: _cmp(op, p.total_pool, amt), neg))
            q.notes.append(f"{n}pool {op} {amt:,.0f}")
    elif key == "assets":
        op, num = _split_op(val)
        cnt = float(num)
        q.prog_preds.append(_with_neg(lambda p: _cmp(op, len(p.assets), cnt), neg))
        q.notes.append(f"{n}assets {op} {cnt:g}")
    elif key in ("lang", "language"):
        q.prog_preds.append(_with_neg(lambda p: _any_in(p.languages, vals), neg))
        q.notes.append(f"{n}lang {'|'.join(vals)}")
    elif key in ("eco", "ecosystem", "chain"):
        q.prog_preds.append(_with_neg(lambda p: _any_in(p.ecosystems, vals), neg))
        q.notes.append(f"{n}eco {'|'.join(vals)}")
    elif key in ("type", "category"):
        q.prog_preds.append(_with_neg(lambda p: _any_in(p.project_types, vals), neg))
        q.notes.append(f"{n}type {'|'.join(vals)}")
    elif key in ("ptype", "program"):
        q.prog_preds.append(_with_neg(lambda p: _any_in(p.program_types, vals), neg))
        q.notes.append(f"{n}program {'|'.join(vals)}")
    elif key == "feature":
        q.prog_preds.append(_with_neg(lambda p: _any_in(p.features, vals), neg))
        q.notes.append(f"{n}feature {'|'.join(vals)}")
    elif key == "token":
        q.prog_preds.append(_with_neg(lambda p: any(v in p.rewards_token.lower() for v in vals), neg))
        q.notes.append(f"{n}token {'|'.join(vals)}")
    elif key == "web":
        b = _bool(val) if val else True
        if b is None:
            raise ValueError("web: expected yes/no")
        if neg:
            b = not b
        q.prog_preds.append(lambda p, b=b: p.has_web == b)
        q.notes.append(f"web side={'yes' if b else 'no'}")
    elif key == "pays":
        letters = set(val.upper())
        if not letters or not letters <= set("LMHC"):
            raise ValueError("pays: use letters L M H C, e.g. pays:hc")
        q.prog_preds.append(_with_neg(lambda p: letters <= set(p.pays), neg))
        q.notes.append(f"{n}pays {''.join(sorted(letters, key='LMHC'.index))}")
    elif key in ("paid", "pts"):
        b = _bool(val) if val else True
        if b is None:
            raise ValueError(f"{key}: expected yes/no")
        if neg:
            b = not b
        q.prog_preds.append(lambda p, b=b: p.paid_submission == b)
        q.notes.append(f"paid submission={'yes' if b else 'no'}")
    elif key in ("kyc", "std", "invite", "paused"):
        b = _bool(val) if val else True
        if key == "paused" and val.lower() == "any":
            q.show_paused = True
            q.notes.append("incl. paused")
            return
        if b is None:
            raise ValueError(f"{key}: expected yes/no")
        if neg:
            b = not b
        attr = {"kyc": "kyc", "std": "std", "invite": "invite", "paused": "paused"}[key]
        if key == "paused":
            q.show_paused = True
        q.prog_preds.append(lambda p, attr=attr, b=b: getattr(p, attr) == b)
        q.notes.append(f"{key}={'yes' if b else 'no'}")
    elif key == "age":
        op, num = _split_op(val)
        td = parse_duration(num)
        q.prog_preds.append(_with_neg(
            lambda p: p.launch is not None and _cmp(op, (now_utc() - p.launch).total_seconds(), td.total_seconds()),
            neg))
        q.notes.append(f"{n}age {op} {fmt_duration(td)}")
    elif key == "updated":
        op, num = _split_op(val)
        td = parse_duration(num)
        # "<7d" must mean "updated less than 7 days ago"; default operator is "within".
        op = "<" if op == ">=" and not re.match(r"^(>=|<=|>|<|=)", val) else op
        q.prog_preds.append(_with_neg(
            lambda p: p.updated is not None and _cmp(op, (now_utc() - p.updated).total_seconds(), td.total_seconds()),
            neg))
        q.notes.append(f"{n}updated {op} {fmt_duration(td)} ago")
    elif key in ("asset", "atype"):
        types = set()
        for v in vals:
            if v not in _ASSET_ALIASES:
                raise ValueError(f"asset: unknown type {v!r} (contract, web, chain)")
            types.add(_ASSET_ALIASES[v])
        q.asset_preds.append(_with_neg(lambda a: a.type in types, neg))
        q.notes.append(f"{n}asset {'|'.join(ASSET_TYPES[t] for t in types)}")
    elif key == "url":
        q.asset_preds.append(_with_neg(
            lambda a: any(v in a.url.lower() or v in a.description.lower() for v in vals), neg))
        q.notes.append(f"{n}asset ~{'|'.join(vals)}")
    elif key == "mark":
        want = [("" if v in ("none", "-") else v) for v in vals]
        q.prog_preds.append(_with_neg(lambda p: p.mark in want, neg))
        q.notes.append(f"{n}mark {'|'.join(vals)}")
    elif key == "sort":
        s = val.lower()
        if s not in SORTS:
            raise ValueError(f"sort: one of {', '.join(SORTS)}")
        q.sort = s
        q.notes.append(f"sort {s}")
    else:
        raise ValueError(f"unknown filter {key!r}")
