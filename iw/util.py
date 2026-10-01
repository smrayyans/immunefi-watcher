"""Small helpers: time, money, durations."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ago(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "-"
    secs = int(((now or now_utc()) - dt).total_seconds())
    if secs < 0:
        return "soon"
    if secs < 90:
        return "now"
    mins = secs // 60
    if mins < 90:
        return f"{mins}m"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h"
    days = hours // 24
    if days < 60:
        return f"{days}d"
    months = days // 30
    if months < 24:
        return f"{months}mo"
    return f"{days // 365}y"


_MONEY = re.compile(r"^\$?([0-9]*\.?[0-9]+)\s*([kmb]?)$", re.I)
_MULT = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def parse_money(s: str) -> float:
    m = _MONEY.match(s.strip().replace(",", "").replace("_", ""))
    if not m:
        raise ValueError(f"bad amount {s!r}")
    return float(m.group(1)) * _MULT[m.group(2).lower()]


def fmt_money(n: float | int | None, dash_zero: bool = True) -> str:
    if not n:
        return "-" if dash_zero else "$0"
    n = float(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            v = n / div
            return f"${v:.1f}".rstrip("0").rstrip(".") + suf if v < 100 else f"${v:.0f}{suf}"
    return f"${n:.0f}"


def fmt_num(n: float | int | None) -> str:
    """Pools can be in tokens, not dollars, so no currency sign."""
    if not n:
        return "-"
    n = float(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            v = n / div
            return (f"{v:.1f}".rstrip("0").rstrip(".") if v < 100 else f"{v:.0f}") + suf
    return f"{n:.0f}"


_DUR = re.compile(r"^(\d+(?:\.\d+)?)\s*(h|d|w|mo|y)$", re.I)
_DUR_UNIT = {"h": timedelta(hours=1), "d": timedelta(days=1), "w": timedelta(weeks=1),
             "mo": timedelta(days=30), "y": timedelta(days=365)}


def parse_duration(s: str) -> timedelta:
    m = _DUR.match(s.strip())
    if not m:
        raise ValueError(f"bad duration {s!r} (use 12h, 7d, 2w, 3mo)")
    return float(m.group(1)) * _DUR_UNIT[m.group(2).lower()]


def fmt_duration(td: timedelta) -> str:
    h = td.total_seconds() / 3600
    if h < 48:
        return f"{h:g}h"
    d = h / 24
    return f"{d:g}d"


_SEV_LETTER = (("low", "L"), ("medium", "M"), ("high", "H"), ("critical", "C"))


WEB = "websites_and_applications"


def strip_web(d: dict) -> dict:
    """Copy of a program record without anything about its web/app side (assets, rewards, impacts, PoC rules).
    This tool is for smart contract / blockchain scope only."""
    d = dict(d)
    d["assets"] = [a for a in d.get("assets") or [] if a.get("type") != WEB]
    d["rewards"] = [r for r in d.get("rewards") or [] if r.get("assetType") != WEB]
    d["impacts"] = [i for i in d.get("impacts") or [] if i.get("type") != WEB]
    d["pocPerTypeAndSeverity"] = [x for x in d.get("pocPerTypeAndSeverity") or [] if not str(x).startswith(WEB)]
    return d


def has_web(d: dict) -> bool:
    return (any(a.get("type") == WEB for a in d.get("assets") or [])
            or any(r.get("assetType") == WEB for r in d.get("rewards") or [])
            or any(i.get("type") == WEB for i in d.get("impacts") or []))


def has_web3(d: dict) -> bool:
    """Does the program have any non-web (smart contract / blockchain) assets, rewards or impacts?"""
    return (any(a.get("type") != WEB for a in d.get("assets") or [])
            or any(r.get("assetType") != WEB for r in d.get("rewards") or [])
            or any(i.get("type") != WEB for i in d.get("impacts") or []))


def web3_max_bounty(d: dict) -> float:
    """Highest published reward ignoring web/app rows. The record's top-level maxBounty is a single number
    that may come from the web side, so it is only trusted when the web side publishes no amount itself."""
    raw = float(d.get("maxBounty") or 0)
    rows = d.get("rewards") or []
    if not rows:
        return raw if has_web3(d) or not has_web(d) else 0.0
    m = float(max([r.get("maxReward") or 0 for r in rows if r.get("assetType") != WEB] or [0]))
    if m == 0 and has_web3(d) and not any((r.get("maxReward") or 0) > 0 for r in rows if r.get("assetType") == WEB):
        m = raw
    return m


def reward_letters(rewards) -> tuple[str, str]:
    """(listed, with_amounts): severities Immunefi lists a reward row for, and the subset whose
    amounts are published. Letters follow the order L M H C."""
    listed, known = set(), set()
    for r in rewards or []:
        sev = r.get("severity")
        listed.add(sev)
        if (r.get("maxReward") or 0) > 0 or (r.get("minReward") or 0) > 0 or (r.get("rewardCalculationPercentage") or 0) > 0:
            known.add(sev)
    return ("".join(l for s, l in _SEV_LETTER if s in listed), "".join(l for s, l in _SEV_LETTER if s in known))
