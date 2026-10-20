"""Textual UI: metrics on top, topology + guests + inspector in the middle, events at the bottom."""
from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Static

from .actions import available_actions
from .btop.theme import st as sty, textual_theme
from .collector import Collector
from .dialogs import ActionMenu, ConfirmAction
from .correlate import Snapshot
from .models import LEVEL_RANK, LEVELS, Action, Event, Task, fmt_bps
from .views import GUEST_ICON, cluster_tree, inspector, legend, network_tree
from .widgets import SOURCE_STYLE, MetricPanelView, level_text, pct_style, render_tree, sparkline

SOURCES = ["all", "fw", "pve", "task", "you"]
MAX_EVENTS = 300


class InfraDash(App):
    TITLE = "infradash"
    CSS = """
    Screen { layout: vertical; }
    #metrics { height: auto; }
    MetricPanelView { width: 1fr; height: 6; border: round $accent; padding: 0 1; }
    #middle { height: 1fr; min-height: 14; }
    #mapbox { width: 50%; border: round $success; padding: 0 1; }
    #map { width: auto; }
    #right { width: 1fr; }
    #guests { height: 1fr; border: round $warning; }
    #inspbox { height: 1fr; border: round $secondary; padding: 0 1; }
    #events { height: 13; border: round $primary; }
    DataTable:focus { border: heavy $accent; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("v", "toggle_view", "Network/Cluster"),
        Binding("s", "cycle(1)", "Select ▶"),
        Binding("S", "cycle(-1)", "◀ Select", show=False),
        Binding("n", "next_issue", "Next issue"),
        Binding("x", "actions", "Actions"),
        Binding("l", "cycle_level", "Level"),
        Binding("f", "cycle_source", "Source"),
        Binding("p", "toggle_pause", "Pause"),
        Binding("escape", "clear", "Clear"),
    ]

    def __init__(self, collector: Collector, refresh_s: float = 5.0) -> None:
        super().__init__()
        self.collector = collector
        self.refresh_s = refresh_s
        self.snap: Snapshot | None = None
        self.view = "network"
        self.selected: str | None = None
        self.task_log: tuple[str, list[str]] | None = None
        self.min_level = "WARN"
        self.source = "all"
        self.paused = False
        self._event_rows: dict[str, Event] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="metrics"):
            for i in range(5):
                yield MetricPanelView(id=f"metric{i}")
        with Horizontal(id="middle"):
            with VerticalScroll(id="mapbox"):
                yield Static(id="map")
            with Vertical(id="right"):
                yield DataTable(id="guests", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="inspbox"):
                    yield Static(id="inspector")
        yield DataTable(id="events", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        theme = textual_theme()
        if theme is not None:
            self.register_theme(theme)
            self.theme = theme.name
        self.query_one("#guests", DataTable).add_columns(
            " ", "id", "name", "node", "cpu", "trend", "mem", "net ↓/↑", "network")
        self.query_one("#events", DataTable).add_columns("time", "src", "level", "about", "event")
        self.query_one("#inspbox").border_title = "Inspector"
        self.query_one("#guests", DataTable).focus()
        self.set_interval(self.refresh_s, self._tick)
        self.load()

    async def on_unmount(self) -> None:
        await self.collector.close()

    # --- loading ----------------------------------------------------------------
    def _tick(self) -> None:
        if not self.paused:
            self.load()

    @work(exclusive=True, group="poll")
    async def load(self) -> None:
        try:
            self.snap = await self.collector.poll()
        except Exception as e:  # noqa: BLE001
            self.notify(f"poll failed: {e}", severity="error", timeout=8)
            return
        for err in self.snap.raw.new_errors[:3]:
            self.notify(err, severity="warning", timeout=6)
        self.render_all()

    @work(exclusive=True, group="tasklog")
    async def load_task_log(self, task: Task) -> None:
        try:
            lines = await self.collector.task_log(task.node, task.upid)
        except Exception as e:  # noqa: BLE001
            lines = [f"could not fetch task log: {e}"]
        self.task_log = (task.upid, lines)
        self.render_all()

    # --- rendering --------------------------------------------------------------
    def render_all(self) -> None:
        try:
            self._render_all()
        except NoMatches:   # screen already torn down (quitting mid-poll)
            pass

    def _render_all(self) -> None:
        snap = self.snap
        if snap is None:
            return
        path = snap.related(self.selected)
        bits = [self.collector.name, f"view: {self.view}",
                f"selected: {snap.name(self.selected) or 'none'}",
                f"events ≥ {self.min_level} · {self.source}"]
        crit = sum(i.severity == "crit" for i in snap.issues)
        warn = len(snap.issues) - crit
        bits.append(f"✖ {crit} critical · ▲ {warn} warnings" if snap.issues else "✓ all clear")
        if not self.collector.actions_enabled:
            bits.append("read-only")
        if self.paused:
            bits.append("⏸ PAUSED")
        if snap.raw.errors:
            bits.append(f"⚠ {len(snap.raw.errors)} API errors")
        self.sub_title = "  ·  ".join(bits)

        hl = {snap.name(e) for e in path if not e.startswith(("guest:", "cluster"))}
        if self.selected and self.selected.startswith("guest:") and (g := snap.guest(self.selected)):
            hl.add(g.node)
            hl.update(st.label for name, _ in g.disks if (st := snap.storage_of(g, name)))
        for i, view in enumerate(self.query(MetricPanelView)):
            view.update_panel(snap.metrics[i] if i < len(snap.metrics) else None, hl)

        roots = network_tree(snap) if self.view == "network" else cluster_tree(snap)
        if not roots:   # only one system configured — show whichever tree exists
            roots = cluster_tree(snap) or network_tree(snap)
        self.query_one("#map", Static).update(
            render_tree(roots, self.selected, path, legend(self.view)) if roots
            else Text("waiting for data…", style=sty("dim")))
        self.query_one("#mapbox").border_title = (
            "Network topology (firewall → networks → hosts) · v: cluster view" if self.view == "network"
            else "Cluster topology (cluster → nodes → guests → networks) · v: network view")

        self._render_guests(snap)
        insp = inspector(snap, self.selected, self.task_log)
        self.query_one("#inspector", Static).update(insp)
        self.query_one("#inspbox").border_title = (
            f"Inspector · {snap.name(self.selected)}  (Esc clears · x actions)" if self.selected
            else "Attention")
        self._render_events(snap)

    def _render_guests(self, snap: Snapshot) -> None:
        table = self.query_one("#guests", DataTable)
        prev = self._cursor_key(table)
        table.clear()
        guests = sorted(snap.raw.guests, key=lambda g: g.vmid)
        sel = self.selected or ""
        if sel.startswith("node:"):
            guests = [g for g in guests if f"node:{g.node}" == sel]
        elif sel.startswith("if:"):
            guests = [g for g in guests if sel[3:] in snap.placement.get(g.id, [])]
        elif sel.startswith("st:"):
            guests = [g for g in guests if g in snap.guests_on(sel)]
        for g in guests:
            running = g.status == "running"
            dim = "" if running else sty("dim")
            nets = ", ".join(snap.name(f"if:{k}") for k in snap.placement.get(g.id, [])) or "?"
            table.add_row(
                Text(GUEST_ICON.get(g.kind, "▪"), style=sty("ok") if running else sty("dim")),
                Text(str(g.vmid), style=dim),
                Text(g.name, style=("bold " if g.id == sel else "") + dim),
                Text(g.node, style=sty("dim")),
                Text(f"{g.cpu:.0f}%" if running else g.status, style=pct_style(g.cpu) if running else sty("dim"),
                     justify="right"),
                Text(sparkline(snap.guest_cpu.get(g.id, []), 12, 100.0) if running else "",
                     style=sty("node")),
                Text(f"{g.mem:.0f}%" if running else "", style=pct_style(g.mem, 85, 95), justify="right"),
                Text(f"{fmt_bps(g.net_in)} / {fmt_bps(g.net_out)}" if running else "", style=sty("dim")),
                Text(nets, style=sty("net") if nets != "?" else sty("dim")),
                key=g.id,
            )
        scope = f" on {snap.name(sel)}" if sel.startswith(("node:", "if:", "st:")) else ""
        self.query_one("#guests").border_title = f"Guests{scope} ({len(guests)}) · Enter = inspect"
        target = sel if sel.startswith("guest:") else prev
        if target and any(g.id == target for g in guests):
            table.move_cursor(row=table.get_row_index(target), animate=False)

    def _render_events(self, snap: Snapshot) -> None:
        table = self.query_one("#events", DataTable)
        row = table.cursor_row
        prev_key = self._cursor_key(table) if row else None   # row 0 = "follow newest"
        table.clear()
        self._event_rows.clear()
        threshold = LEVEL_RANK[self.min_level]
        scoped = [e for e in snap.events_for(self.selected)
                  if self.source == "all" or e.source == self.source]
        events = [e for e in scoped if LEVEL_RANK.get(e.level, 1) >= threshold][:MAX_EVENTS]
        hidden = len(scoped) - len([e for e in scoped if LEVEL_RANK.get(e.level, 1) >= threshold])
        for i, e in enumerate(events):
            key = self._event_key(e, i)
            if key in self._event_rows:
                key = f"{key}#{i}"
            self._event_rows[key] = e
            primary = next((x for x in e.entities if x != "fw"), None) or e.iface or (
                e.entities[0] if e.entities else None)
            about = snap.name(primary)
            table.add_row(
                Text(datetime.fromtimestamp(e.ts).strftime("%H:%M:%S"), style=sty("dim")),
                Text(e.source, style=SOURCE_STYLE.get(e.source, "")),
                level_text(e.level),
                Text(about[:16], style=sty("strong") if primary and primary == self.selected else ""),
                Text(e.message[:220], style=sty("bad") if e.level == "ERROR" else ""),
                key=key,
            )
        scope = f" involving {snap.name(self.selected)}" if self.selected else ""
        self.query_one("#events").border_title = (
            f"Events ≥ {self.min_level} · source {self.source}{scope} ({len(events)})"
            + (f" · {hidden} more below {self.min_level}, press l" if hidden else "")
            + " · Enter = select what it's about")
        # Keep the cursor on the same event while new ones arrive above it, so what you
        # act on with x is what you were looking at.
        if prev_key and prev_key in self._event_rows:
            table.move_cursor(row=table.get_row_index(prev_key), animate=False)
        elif events and row:
            table.move_cursor(row=min(row, len(events) - 1), animate=False)

    @staticmethod
    def _event_key(e: Event, i: int) -> str:
        ref = e.ref
        if isinstance(ref, Task):
            return f"task:{ref.upid}:{ref.status}"
        if hasattr(ref, "key") and getattr(ref, "key"):
            return f"{e.source}:{ref.key}"
        return f"{e.source}:{e.ts}:{i}"

    @staticmethod
    def _cursor_key(table: DataTable) -> str | None:
        if table.row_count == 0:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:  # noqa: BLE001
            return None

    # --- interaction ------------------------------------------------------------
    def select(self, eid: str | None) -> None:
        self.selected = eid
        self.task_log = None
        self.render_all()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if event.data_table.id == "guests":
            self.select(key)
        elif event.data_table.id == "events":
            ev = self._event_rows.get(key)
            if not ev:
                return
            target = next((e for e in ev.entities if e != "fw"), None) or ev.iface or (
                ev.entities[0] if ev.entities else None)
            if not target:
                self.notify("This event isn't tied to a known host.", severity="warning")
                return
            self.select(target)
            if isinstance(ev.ref, Task):
                self.load_task_log(ev.ref)

    def action_cycle(self, step: int) -> None:
        if not self.snap:
            return
        order: list[str | None] = [None, *self.snap.entity_order()]
        idx = order.index(self.selected) if self.selected in order else 0
        self.select(order[(idx + step) % len(order)])

    def action_next_issue(self) -> None:
        if not self.snap or not self.snap.issues:
            self.notify("Nothing needs attention right now.")
            return
        targets = [i.entity for i in self.snap.issues if i.entity]
        if not targets:
            return
        idx = targets.index(self.selected) + 1 if self.selected in targets else 0
        self.select(targets[idx % len(targets)])

    def _focused_event(self) -> Event | None:
        table = self.query_one("#events", DataTable)
        if self.focused is not table or table.row_count == 0:
            return None
        return self._event_rows.get(self._cursor_key(table) or "")

    def action_actions(self) -> None:
        if not self.snap:
            return
        if not self.collector.actions_enabled:
            self.notify("Actions are off. Set `actions: enabled: true` in the config (and give the API "
                        "credentials write access) to turn them on.", severity="warning", timeout=8)
            return
        prov = self.collector.provider
        event = self._focused_event()
        acts = available_actions(self.snap, self.selected, event,
                                 getattr(prov, "has_pve", False), getattr(prov, "has_opn", False),
                                 getattr(prov, "block_alias", None))
        if not acts:
            what = "this event" if event else (self.snap.name(self.selected) or "nothing selected")
            self.notify(f"No actions for {what}. Select a guest or host, or focus a firewall event.",
                        severity="warning")
            return
        title = (f"Actions for firewall event: {event.message[:60]}" if event
                 else f"Actions for {self.snap.name(self.selected)}")

        def chosen(idx: int | None) -> None:
            if idx is not None:
                self.push_screen(ConfirmAction(acts[idx]), lambda ok: ok and self.execute(acts[idx]))
        self.push_screen(ActionMenu(title, acts), chosen)

    @work(exclusive=False, group="action")
    async def execute(self, action: Action) -> None:
        self.notify(f"{action.label}…", timeout=3)
        try:
            msg = await self.collector.run_action(action)
            self.notify(msg, timeout=6)
        except Exception as e:  # noqa: BLE001
            self.notify(str(e), severity="error", timeout=10)
        self.load()

    def action_toggle_view(self) -> None:
        self.view = "cluster" if self.view == "network" else "network"
        self.render_all()

    def action_cycle_level(self) -> None:
        self.min_level = LEVELS[(LEVELS.index(self.min_level) + 1) % len(LEVELS)]
        self.render_all()

    def action_cycle_source(self) -> None:
        self.source = SOURCES[(SOURCES.index(self.source) + 1) % len(SOURCES)]
        self.render_all()

    def action_toggle_pause(self) -> None:
        self.paused = not self.paused
        self.render_all()

    def action_refresh_now(self) -> None:
        self.load()

    def action_clear(self) -> None:
        self.select(None)
