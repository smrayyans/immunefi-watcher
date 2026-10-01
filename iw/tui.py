"""Interactive terminal UI (Textual)."""

from __future__ import annotations

import json
import shutil
import subprocess
import webbrowser

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static, TabbedContent, TabPane

from . import db, prepare as prep, query, sync as syncmod, view
from .util import ago, fmt_duration, fmt_money, now_utc, parse_ts

DEFAULT_FILTER = "paid:no"
AUTO_SYNC_HOURS = 4  # sync on launch when the last sync is older than this (no cron needed)

# Fixed widths: auto-sized DataTable columns keep the widest value ever shown (a big empty gap) and, when
# rebuilt per refresh, left stale cached rows next to freshly painted hovered rows. Fixed widths avoid both.
PROG_COLS = [("Program", 26), ("Paid sub", 8), ("Max", 7), ("Pays", 5), ("New", 5), ("Newest", 6),
             ("Assets", 6), ("Lang", 13), ("Eco", 12), ("Upd", 5), ("Flags", 14)]
FEED_COLS = [("Added", 5), ("Program", 28), ("Paid sub", 8), ("Max", 7), ("Type", 8), ("Asset", 72), ("Notes", 40)]
CENTER_COLS = {"Paid sub", "Max", "Pays", "New", "Newest", "Assets"}  # programs table only


def _c(x, label: str) -> Text:
    """Centre a cell in its fixed-width column by padding. (DataTable ignores Text.justify for cell contents.)"""
    t = Text(x) if isinstance(x, str) else x.copy()
    width = dict(PROG_COLS)[label]
    pad = max(0, width - t.cell_len) // 2
    return Text(" " * pad) + t if pad else t


EVENT_COLS = [("When", 5), ("Program", 28), ("Event", 17), ("Detail", 90)]
MARK_CYCLE = ["", "hunt", "skip", "done"]
WINDOW_CYCLE = ["1d", "3d", "7d", "14d", "30d", "90d"]
SORT_CYCLE = ["bounty", "pool", "new", "newest", "updated", "launch", "assets", "name"]
DETAIL_TABS = ["d-overview", "d-scope", "d-rewards", "d-rules", "d-known", "d-prepare"]

HELP = """\
[b]Filter bar[/b]  (press / to type, Enter or Esc to go back to the list)
  new:7d            assets added in the last 7 days        bare [b]new:[/b] = 7d
  bounty:>=100k     max bounty   (also min:100k  max:1m)
  pool:             has any prize pool       pool:>1m
  lang:solidity,rust   eco:eth   type:defi   feature:triage   token:usdc
  asset:contract|web|chain      url:github.com/foo      assets:>50
  age:<90d          launched less than 90 days ago      updated:<7d
  web:no            hide programs with a web/app side too (they carry a cyan W before the name)
  pays:hc           programs that list rewards for High and Critical (letters L M H C; -pays:l = not low)
  paid:no           free-to-submit programs  (paid:yes = "Pay to Submit", the $ badge on the program page)
  kyc:no  std:yes  invite:no  paused:any  mark:hunt  -mark:skip
  sort:bounty|pool|new|newest|updated|launch|assets|name
  -key:value negates. a,b means a OR b. Plain words match name/slug/tags.

[b]Keys[/b]
  /  filter      x  clear filter     1 2 3  views: programs / new assets / changes
  n  new-assets view                 f  toggle free-to-submit only (paid:no)
  w  cycle new-asset window (1d 3d 7d 14d 30d 90d)
  s  cycle sort                      r  sync from Immunefi now
  m  mark program: hunt > skip > done > none
  p  prepare folder: creates <slug>/ with SCOPE.md + targets.txt; asks before touching an existing SCOPE.md
  o  open asset (new-assets view) or program page in browser      O  open program page
  c  copy asset url / program url    [ ]  previous / next detail tab     z  hide/show detail pane
  ?  this help                       q  quit

[b]Notes[/b]
  "Added" is Immunefi's own addedAt for the asset, so new-asset detection works from the first sync.
  Pools can be in tokens (shown with the reward token), not necessarily dollars.
  Always confirm scope on the live program page before testing anything.
"""


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,q,question_mark", "app.pop_screen", "Close")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > VerticalScroll { width: 92; max-width: 95%; height: 85%; border: round $accent; background: $surface; padding: 1 2; }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(HELP)


def to_clipboard(text: str) -> bool:
    """Copy via the system clipboard tool when there is one (reliable in Konsole), else report False."""
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if shutil.which(cmd[0]):
            try:
                pr = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                pr.stdin.write(text.encode())
                pr.stdin.close()  # the tool keeps serving the clipboard in the background; do not wait
                return True
            except OSError:
                continue
    return False


class ScopeExistsScreen(ModalScreen[str]):
    """Asked before anything touches an existing SCOPE.md. Escape or Enter keeps the file."""

    BINDINGS = [
        Binding("escape", "choose('keep')", "Keep"),
        Binding("k", "choose('keep')", "Keep"),
        Binding("n", "choose('new')", "Save as SCOPE.new.md"),
        Binding("o", "choose('overwrite')", "Overwrite"),
    ]
    DEFAULT_CSS = """
    ScopeExistsScreen { align: center middle; }
    ScopeExistsScreen > Vertical { width: 84; max-width: 96%; height: auto; border: heavy $warning;
                                   background: $surface; padding: 1 2; }
    ScopeExistsScreen Horizontal { height: auto; margin-top: 1; }
    ScopeExistsScreen Button { margin-right: 2; }
    """

    def __init__(self, info: dict, name: str):
        super().__init__()
        self.info, self.pname = info, name

    def compose(self) -> ComposeResult:
        i = self.info
        mod = f"{i['scope_modified']:%Y-%m-%d %H:%M}" if i["scope_modified"] else "?"
        with Vertical():
            yield Static(Text.assemble(
                ("SCOPE.md already exists\n\n", "bold yellow"),
                (f"{self.pname}\n", "bold"), (f"{i['scope_path']}\n", "dim"),
                (f"{i['scope_lines']} lines, {i['scope_bytes']:,} bytes, last modified {mod}\n\n", ""),
                ("Overwriting replaces it, including any rejection rules or notes you wrote by hand. "
                 "The current file is first copied to SCOPE.md.bak-<time> in the same folder.\n", ""),
            ))
            with Horizontal():
                yield Button("Keep existing (k)", id="keep", variant="primary")
                yield Button("Save as SCOPE.new.md (n)", id="new")
                yield Button("Overwrite (o)", id="overwrite", variant="error")

    def on_mount(self) -> None:
        self.query_one("#keep", Button).focus()  # the safe choice is the default

    def on_button_pressed(self, ev: Button.Pressed) -> None:
        self.dismiss(ev.button.id)

    def action_choose(self, choice: str) -> None:
        self.dismiss(choice)


class IW(App):
    TITLE = "immunefi-watcher"
    CSS = """
    #filter { margin: 0 0; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    #main { height: 1fr; }
    #views { width: 3fr; }
    #detail { width: 2fr; border-left: tall $primary 40%; }
    DataTable { height: 1fr; }
    .detailbody { padding: 0 1; }
    """
    BINDINGS = [
        Binding("slash", "focus_filter", "Filter"),
        Binding("x", "clear_filter", "Clear"),
        Binding("1", "view('v-programs')", "Programs"),
        Binding("2,n", "view('v-feed')", "New assets"),
        Binding("3", "view('v-events')", "Changes"),
        Binding("f", "toggle_free", "Free/All"),
        Binding("w", "cycle_window", "Window"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("r", "sync", "Sync"),
        Binding("m", "mark", "Mark"),
        Binding("p", "prepare", "Prepare"),
        Binding("o", "open", "Open"),
        Binding("O", "open_program", "Open page", show=False),
        Binding("c", "copy", "Copy"),
        Binding("left_square_bracket", "detail_tab(-1)", "◀ tab", show=False),
        Binding("right_square_bracket", "detail_tab(1)", "tab ▶", show=False),
        Binding("z", "toggle_detail", "Detail", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, db_path: str | None = None):
        super().__init__()
        self.db_path = db_path
        self.conn = db.connect(db_path)
        self.progs: list[query.Prog] = []
        self.by_slug: dict[str, query.Prog] = {}
        self.q = query.parse("")
        self.shown: list[query.Prog] = []
        self.feed_rows: list = []
        self.current_slug: str | None = None
        self.current_asset: str | None = None
        self.event_rows: dict[str, str] = {}
        self.syncing = False

    # ---------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="filter:  new:7d bounty:>=100k lang:solidity pool: asset:contract sort:newest   (? for help)",
                    id="filter")
        yield Static("", id="status")
        with Horizontal(id="main"):
            with TabbedContent(initial="v-programs", id="views"):
                with TabPane("Programs [1]", id="v-programs"):
                    yield DataTable(id="t-programs", cursor_type="row", zebra_stripes=True)
                with TabPane("New assets [2]", id="v-feed"):
                    yield DataTable(id="t-feed", cursor_type="row", zebra_stripes=True)
                with TabPane("Changes [3]", id="v-events"):
                    yield DataTable(id="t-events", cursor_type="row", zebra_stripes=True)
            with TabbedContent(initial="d-overview", id="detail"):
                for tid, title in zip(DETAIL_TABS, ("Overview", "Scope", "Rewards", "Rules", "Known issues", "Prepare folder")):
                    with TabPane(title, id=tid):
                        with VerticalScroll():
                            yield Static("", id=f"b-{tid}", classes="detailbody")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "Immunefi program browser"
        self.query_one("#filter", Input).value = DEFAULT_FILTER  # free-to-submit programs by default; f toggles
        for tid, cols in (("#t-programs", PROG_COLS), ("#t-feed", FEED_COLS), ("#t-events", EVENT_COLS)):
            tbl = self.query_one(tid, DataTable)
            for label, width in cols:
                tbl.add_column(_c(label, label) if tid == "#t-programs" and label in CENTER_COLS else label, width=width)
        self.reload()
        last = syncmod.last_sync(self.conn)
        age = (now_utc() - parse_ts(last["ts"])).total_seconds() / 3600 if last and parse_ts(last["ts"]) else None
        if not self.progs:
            self.notify("No data yet, running first sync...", timeout=4)
            self.action_sync()
        elif age is None or age > AUTO_SYNC_HOURS:
            self.notify("Data is stale, syncing in the background...", timeout=3)
            self.action_sync()
        self.query_one("#t-programs").focus()

    # ---------------------------------------------------------------- data
    def reload(self) -> None:
        self.progs = query.load_programs(self.conn)
        self.by_slug = {p.slug: p for p in self.progs}
        self.refresh_views()

    def refresh_views(self) -> None:
        text = self.query_one("#filter", Input).value
        self.q = query.parse(text)
        self.shown = self.q.run(self.progs)
        self.feed_rows = self.q.feed(self.progs)
        now = now_utc()
        win = self.q.effective_window

        t = self.query_one("#t-programs", DataTable)
        t.clear()
        for p in self.shown:
            n = len(self.q.matching_assets(p, now)) if self.q.has_asset_filter else len(p.new_assets(win, now))
            t.add_row(
                view.name_cell(p, 24),
                _c(view.paid_text(p), "Paid sub"), _c(fmt_money(p.max_bounty), "Max"), _c(view.pays_text(p), "Pays"),
                _c(Text(f"+{n}" if n else "", style="bold green"), "New"), _c(ago(p.newest_asset, now), "Newest"),
                _c(str(len(p.assets)), "Assets"),
                ",".join(p.languages)[:13], ",".join(p.ecosystems)[:12], ago(p.updated, now), view.flags(p),
                key=p.slug,
            )

        f = self.query_one("#t-feed", DataTable)
        f.clear()
        for i, (p, a) in enumerate(self.feed_rows[:2000]):
            f.add_row(Text(ago(a.when, now), style="bold green"), view.name_cell(p, 26),
                      view.paid_text(p), fmt_money(p.max_bounty), view.asset_kind(a), a.url, a.description[:60],
                      key=f"{i}")

        e = self.query_one("#t-events", DataTable)
        e.clear()
        self.event_rows = {}
        names = {p.slug: p.name for p in self.progs}
        for r in self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 500"):
            d = json.loads(r["detail"] or "{}")
            key = str(r["id"])
            self.event_rows[key] = r["slug"]
            e.add_row(ago(parse_ts(r["ts"]), now), Text(names.get(r["slug"], r["slug"]), style="bold"),
                      Text(r["kind"], style=view.EVENT_STYLE.get(r["kind"], "")),
                      view.event_detail(r["kind"], d), key=key)

        self.update_status()
        if self.shown and self._active() == "v-programs":
            self.show_detail(self.shown[0].slug)
        elif self._active() == "v-feed" and self.feed_rows:
            self.show_detail(self.feed_rows[0][0].slug, self.feed_rows[0][1].id)
        elif not self.shown:
            self.clear_detail()

    def update_status(self) -> None:
        last = syncmod.last_sync(self.conn)
        sync_txt = f"synced {ago(parse_ts(last['ts']))} ago via {last['source']}" if last else "never synced (press r)"
        if last and ago(parse_ts(last["ts"])) == "now":
            sync_txt = f"synced just now via {last['source']}"
        win = fmt_duration(self.q.effective_window) + ("" if self.q.window else " (default)")
        bits = [f"{len(self.shown)}/{len(self.progs)} programs", f"{len(self.feed_rows)} new assets", f"window {win}",
                f"sort {self.q.sort or 'auto'}", sync_txt]
        line = Text(" · ".join(bits))
        if self.q.notes:
            line.append("   ")
            line.append(" ".join(f"[{n}]" for n in self.q.notes), style="cyan")
        for err in self.q.errors:
            line.append(f"   ! {err}", style="bold red")
        if self.syncing:
            line.append("   syncing...", style="bold yellow")
        self.query_one("#status", Static).update(line)

    def _active(self) -> str:
        return self.query_one("#views", TabbedContent).active

    # ---------------------------------------------------------------- detail pane
    def clear_detail(self) -> None:
        self.current_slug = None
        for tid in DETAIL_TABS:
            self.query_one(f"#b-{tid}", Static).update(Text("(nothing selected)", style="dim"))

    def show_detail(self, slug: str, asset_id: str | None = None) -> None:
        p = self.by_slug.get(slug)
        if not p:
            return
        self.current_slug, self.current_asset = slug, asset_id
        d = query.program_data(self.conn, slug)
        win = self.q.effective_window
        panes = {
            "d-overview": view.overview(p, d, win),
            "d-scope": view.scope(p, win, asset_id),
            "d-rewards": view.rewards(p, d),
            "d-rules": view.rules(p, d),
            "d-known": view.known(p, d),
            "d-prepare": self._prepare_preview(p),
        }
        for tid, body in panes.items():
            self.query_one(f"#b-{tid}", Static).update(body)

    _TABLE_VIEW = {"t-programs": "v-programs", "t-feed": "v-feed", "t-events": "v-events"}

    def _is_current(self, ev: DataTable.RowHighlighted) -> bool:
        """Highlight events are queued and also come from hidden tables being rebuilt.
        Keep only ones from the visible table that still match its cursor."""
        t = ev.control
        if self._TABLE_VIEW.get(t.id) != self._active():
            return False
        if ev.row_key is None or ev.row_key.value is None or t.row_count == 0:
            return False
        try:
            return t.coordinate_to_cell_key(t.cursor_coordinate).row_key == ev.row_key
        except Exception:  # noqa: BLE001
            return False

    # ---------------------------------------------------------------- prepare folder
    def _prepare_preview(self, p: query.Prog) -> Text:
        """What the tab shows when you merely select a program. Nothing is written until the tab is opened or p is pressed."""
        st = prep.status(self.conn, p.slug)
        t = Text()
        t.append(f"{p.name}\n\n", style="bold")
        t.append("Target folder  ", style="dim")
        t.append(st["folder"] + "\n")
        t.append("Folder exists  ", style="dim")
        t.append(("yes" if st["folder_exists"] else "no") + "\n")
        t.append("SCOPE.md       ", style="dim")
        t.append("exists, you will be asked before anything is overwritten\n\n" if st["scope_exists"]
                 else "not created yet\n\n", style="yellow" if st["scope_exists"] else "")
        t.append("Opening this tab (or pressing p) creates the folder and writes SCOPE.md + targets.txt "
                 "for the selected program.", style="cyan")
        return t

    def _do_prepare(self) -> None:
        if not self.current_slug:
            return
        p = self.by_slug[self.current_slug]
        st = prep.status(self.conn, p.slug)
        if st["scope_exists"]:
            self.push_screen(ScopeExistsScreen(st, p.name), lambda choice, slug=p.slug: self._finish_prepare(slug, choice))
        else:
            self._finish_prepare(p.slug, "auto")

    def _finish_prepare(self, slug: str, choice: str | None) -> None:
        p = self.by_slug[slug]
        mode = {"new": "new", "overwrite": "overwrite"}.get(choice or "", "auto")
        try:
            r = prep.prepare(self.conn, slug, mode=mode)
        except Exception as e:  # noqa: BLE001
            self.query_one("#b-d-prepare", Static).update(Text(f"prepare failed: {e}", style="bold red"))
            self.notify(f"prepare failed: {e}", severity="error", timeout=6)
            return
        t = Text()
        t.append(f"{p.name}\n\n", style="bold")
        t.append("Folder  ", style="dim")
        t.append(r["folder"] + ("  (created)" if r["created_folder"] else "  (already existed)") + "\n")
        if r["backup"]:
            t.append("Backup  ", style="dim")
            t.append(r["backup"] + "\n", style="yellow")
        if r["wrote"]:
            t.append("Wrote   ", style="dim")
            t.append(r["wrote"] + "\n", style="bold green")
        elif r["scope_existed"]:
            t.append("SCOPE.md already exists, left untouched.\n", style="yellow")
            t.append("Press p to choose: overwrite (with backup) or save as SCOPE.new.md.\n", style="cyan")
        for f in r["files"]:
            if f != r["wrote"]:
                t.append("Wrote   ", style="dim")
                t.append(f + "\n")
        t.append(f"\nContents: {r['assets']} assets, {r['impacts']} impacts, {r['known_issues']} known issues, "
                 "rewards, out of scope, rules, audits, economics.\n", style="dim")
        t.append(f"\ncd {r['folder']}\n", style="bold")
        self.query_one("#b-d-prepare", Static).update(t)
        self.notify(("wrote " + r["wrote"]) if r["wrote"] else "SCOPE.md kept as is", timeout=4)

    @on(TabbedContent.TabActivated, "#detail")
    def _detail_tab(self, ev: TabbedContent.TabActivated) -> None:
        if ev.pane.id == "d-prepare":
            self._do_prepare()

    def action_prepare(self) -> None:
        if not self.current_slug:
            return
        tc = self.query_one("#detail", TabbedContent)
        if tc.active == "d-prepare":
            self._do_prepare()
        else:
            tc.active = "d-prepare"  # TabActivated runs the prepare

    @on(DataTable.RowHighlighted, "#t-programs")
    def _hl_prog(self, ev: DataTable.RowHighlighted) -> None:
        if self._is_current(ev):
            self.show_detail(ev.row_key.value)

    @on(DataTable.RowHighlighted, "#t-feed")
    def _hl_feed(self, ev: DataTable.RowHighlighted) -> None:
        if self._is_current(ev):
            p, a = self.feed_rows[int(ev.row_key.value)]
            self.show_detail(p.slug, a.id)

    @on(DataTable.RowHighlighted, "#t-events")
    def _hl_event(self, ev: DataTable.RowHighlighted) -> None:
        if self._is_current(ev) and ev.row_key.value in self.event_rows:
            self.show_detail(self.event_rows[ev.row_key.value])

    @on(TabbedContent.TabActivated, "#views")
    def _view_changed(self, ev: TabbedContent.TabActivated) -> None:
        table = {"v-programs": "#t-programs", "v-feed": "#t-feed", "v-events": "#t-events"}[ev.pane.id]
        self.query_one(table).focus()
        self.refresh_views()

    # ---------------------------------------------------------------- filter bar
    @on(Input.Changed, "#filter")
    def _filter_changed(self, ev: Input.Changed) -> None:
        self.refresh_views()

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self, ev: Input.Submitted) -> None:
        self._focus_table()

    def _focus_table(self) -> None:
        table = {"v-programs": "#t-programs", "v-feed": "#t-feed", "v-events": "#t-events"}[self._active()]
        self.query_one(table).focus()

    def on_key(self, event) -> None:
        if event.key == "escape" and isinstance(self.focused, Input):
            self._focus_table()
            event.stop()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_clear_filter(self) -> None:
        if isinstance(self.focused, Input):
            return
        self.query_one("#filter", Input).value = ""

    def _set_token(self, key: str, value: str | None) -> None:
        """Replace or remove `key:...` in the filter text, so the bar stays the single source of truth."""
        inp = self.query_one("#filter", Input)
        parts = [t for t in inp.value.split() if not t.lower().startswith(f"{key}:")]
        if value:
            parts.append(f"{key}:{value}")
        inp.value = " ".join(parts)

    # ---------------------------------------------------------------- actions
    def action_view(self, pane: str) -> None:
        self.query_one("#views", TabbedContent).active = pane

    def action_cycle_window(self) -> None:
        cur = fmt_duration(self.q.effective_window)
        nxt = WINDOW_CYCLE[(WINDOW_CYCLE.index(cur) + 1) % len(WINDOW_CYCLE)] if cur in WINDOW_CYCLE else "7d"
        self._set_token("new", nxt)
        self._set_token("added", None)

    def action_toggle_free(self) -> None:
        """Toggle `paid:no` in the filter bar: only programs that are free to submit to."""
        on = any(t.lower() == "paid:no" for t in self.query_one("#filter", Input).value.split())
        self._set_token("paid", None if on else "no")
        self.notify("showing all programs" if on else "free-to-submit programs only", timeout=2)

    def action_cycle_sort(self) -> None:
        cur = self.q.sort or ("newest" if self.q.has_asset_filter else "bounty")
        self._set_token("sort", SORT_CYCLE[(SORT_CYCLE.index(cur) + 1) % len(SORT_CYCLE)])

    def action_detail_tab(self, step: int) -> None:
        tc = self.query_one("#detail", TabbedContent)
        tc.active = DETAIL_TABS[(DETAIL_TABS.index(tc.active) + step) % len(DETAIL_TABS)]

    def action_toggle_detail(self) -> None:
        d = self.query_one("#detail")
        d.display = not d.display

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_mark(self) -> None:
        if not self.current_slug:
            return
        p = self.by_slug[self.current_slug]
        new = MARK_CYCLE[(MARK_CYCLE.index(p.mark) + 1) % len(MARK_CYCLE)]
        with self.conn:
            if new:
                self.conn.execute(
                    "INSERT INTO marks(slug,status,note,ts) VALUES(?,?,?,?) ON CONFLICT(slug) DO UPDATE SET "
                    "status=excluded.status,ts=excluded.ts", (p.slug, new, "", now_utc().isoformat()))
            else:
                self.conn.execute("DELETE FROM marks WHERE slug=?", (p.slug,))
        slug = p.slug
        self.reload()
        self.notify(f"{p.name}: {new or 'unmarked'}", timeout=2)
        self._restore_cursor(slug)

    def _restore_cursor(self, slug: str) -> None:
        if self._active() == "v-programs":
            t = self.query_one("#t-programs", DataTable)
            for i, p in enumerate(self.shown):
                if p.slug == slug:
                    t.move_cursor(row=i)
                    break

    def _selected_asset(self):
        if self._active() != "v-feed":
            return None
        t = self.query_one("#t-feed", DataTable)
        if t.row_count == 0:
            return None
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        return self.feed_rows[int(key)][1]

    def action_open(self) -> None:
        a = self._selected_asset()
        if a and a.url.startswith("http"):
            webbrowser.open(a.url)
            self.notify(f"opened {a.url[:70]}", timeout=2)
        else:
            self.action_open_program()

    def action_open_program(self) -> None:
        if self.current_slug:
            url = view.site_url(self.current_slug)
            webbrowser.open(url)
            self.notify(f"opened {url}", timeout=2)

    def _copy(self, text: str) -> None:
        self.copy_to_clipboard(text)  # terminal escape sequence, works in some terminals
        to_clipboard(text)           # system tool, works in Konsole under Wayland/X11
        self.notify(f"copied: {text[:70]}", timeout=2)

    def action_copy(self) -> None:
        a = self._selected_asset()
        text = a.url if a else (view.site_url(self.current_slug) if self.current_slug else "")
        if text:
            self._copy(text)

    # ---------------------------------------------------------------- sync
    def action_sync(self) -> None:
        if self.syncing:
            return
        self.syncing = True
        self.update_status()
        self._do_sync()

    @work(thread=True, exclusive=True)
    def _do_sync(self) -> None:
        conn = db.connect(self.db_path)  # sqlite connections are per-thread
        try:
            res = syncmod.sync(conn, "auto")
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._sync_done, None, str(e))
        else:
            self.call_from_thread(self._sync_done, res, None)
        finally:
            conn.close()

    def _sync_done(self, res: dict | None, err: str | None) -> None:
        self.syncing = False
        if err:
            self.notify(f"sync failed: {err}", severity="error", timeout=8)
            self.update_status()
            return
        self.reload()
        if res["first_run"]:
            self.notify(f"first sync: {res['programs']} programs, {res['assets']} assets", timeout=5)
        else:
            by = res["by_kind"]
            msg = ", ".join(f"{v} {k}" for k, v in by.items()) or "no changes"
            self.notify(f"synced: {msg}", timeout=8 if by else 3)


def run(db_path: str | None = None) -> int:
    IW(db_path).run()
    return 0
