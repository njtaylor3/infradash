"""btop-style front end for infradash. Same collector, correlation, attention rules and
actions as the classic UI; only the presentation and key handling differ."""
from __future__ import annotations

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import Static

from ..actions import available_actions
from ..collector import Collector
from ..dialogs import ActionMenu, ConfirmAction
from ..models import LEVEL_RANK, LEVELS, Action, Event, Task
from ..views import cluster_tree, network_tree
from .boxes import ClusterBox, EventsBox, GuestsBox, MemBox, NetBox, tree_rows
from .draw import Canvas, box
from .theme import c

VIEWS = ["list", "cluster", "network"]
SORTS = ["cpu", "mem", "name", "vmid", "node", "net"]
SOURCES = ["all", "fw", "pve", "task", "you"]
HELP = [
    ("1 … 5", "Show/hide cluster, mem, net, guests, events boxes"),
    ("+ / -", "Change update interval"), ("p, space", "Pause / resume updates"),
    ("tab", "Switch between guests and events"), ("↑ ↓ PgUp PgDn Home End", "Move selection (mouse wheel too)"),
    ("enter", "Guests: show/hide details · Events: jump to what it's about"),
    ("e", "Guests view: list → cluster tree → network tree"),
    ("← → / < >", "Change sort column (list view)"), ("r", "Reverse sort"),
    ("f, /", "Filter guests (type, enter to keep, esc to clear)"),
    ("x", "Actions for the selection or the selected firewall event"),
    ("a", "Jump to the next item needing attention"), ("b / n", "Previous / next network interface"),
    ("l", "Minimum event level"), ("s", "Event source"), ("esc", "Close details / clear filter"),
    ("h, ?", "This help"), ("q", "Quit"),
]
KEY_ACTIONS = {
    "q": "quit", "escape": "escape", "h": "help", "question_mark": "help", "f1": "help",
    "1": "toggle(1)", "2": "toggle(2)", "3": "toggle(3)", "4": "toggle(4)", "5": "toggle(5)",
    "plus": "interval(500)", "minus": "interval(-500)", "p": "pause", "space": "pause",
    "tab": "switch", "shift+tab": "switch",
    "up": "move(-1)", "down": "move(1)", "pageup": "move(-10)", "pagedown": "move(10)",
    "home": "move(-1000000)", "end": "move(1000000)", "enter": "enter", "e": "view",
    "left": "sort(-1)", "right": "sort(1)", "less_than_sign": "sort(-1)", "greater_than_sign": "sort(1)",
    "r": "reverse", "f": "filter", "slash": "filter", "x": "act", "a": "next_issue",
    "b": "iface(-1)", "n": "iface(1)", "l": "level", "s": "source",
}


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "dismiss", show=False), Binding("q", "dismiss", show=False),
                Binding("h", "dismiss", show=False)]
    DEFAULT_CSS = "HelpScreen { align: center middle; } #help { width: 96; height: auto; background: #000000; }"

    def compose(self) -> ComposeResult:
        w, h = 96, len(HELP) + 4
        cv = Canvas(w, h)
        box(cv, 0, 0, w, h, c("proc_box"), "help", bottom_left=[([("esc", "hi"), (" close", "title")], None)])
        cv.put(2, 1, "Key:".ljust(26) + "Description:", c("title"), True)
        for i, (k, d) in enumerate(HELP):
            cv.put(2, 2 + i, k.ljust(26), c("hi_fg"), True)
            cv.put(28, 2 + i, d, c("main_fg"), maxw=w - 30)
        yield Static(cv.to_text(), id="help")

    def on_click(self) -> None:
        self.dismiss()


class MainScreen(Screen):
    """Tab switches boxes here; dialogs are other screens, so they keep normal Tab focus."""
    BINDINGS = [Binding("tab", "app.switch", show=False), Binding("shift+tab", "app.switch", show=False)]


class BtopApp(App):
    TITLE = "infradash"
    CSS = f"""
    Screen {{ background: #000000; layout: vertical; }}
    #cluster {{ height: 30%; min-height: 9; }}
    #middle {{ height: 1fr; }}
    #left {{ width: 44%; }}
    #mem {{ height: 11fr; }}
    #net {{ height: 9fr; }}
    #guests {{ width: 1fr; }}
    #events {{ height: 24%; min-height: 6; }}
    #dialog {{ border: solid {c('proc_box')}; background: #000000; }}
    OptionList {{ background: #000000; border: none; }}
    OptionList > .option-list--option-highlighted {{ background: {c('selected_bg')}; color: {c('selected_fg')}; }}
    """
    BINDINGS = [Binding(k, a, show=False, priority=True) for k, a in KEY_ACTIONS.items()
                if k not in ("tab", "shift+tab")]

    def get_default_screen(self) -> Screen:
        return MainScreen(id="_default")

    def __init__(self, collector: Collector, refresh_s: float = 2.0) -> None:
        super().__init__()
        self.collector = collector
        self.refresh_s = refresh_s
        self.snap = None
        self.paused = False
        self.shown = {1: True, 2: True, 3: True, 4: True, 5: True}
        self.focus_box = "guests"
        self.view = "list"
        self.sort_key = "cpu"
        self.reverse = False
        self.filter = ""
        self.filter_mode = False
        self.g_sel = 0
        self.g_key: str | None = None
        self.e_sel = 0
        self.e_key: str | None = None
        self.scrolls = {"guests": 0, "events": 0}
        self.detail_open = False
        self.detail_eid: str | None = None
        self.task_log: tuple[str, list[str]] | None = None
        self.min_level = "WARN"
        self.source = "all"
        self.iface_idx = 0
        self._timer = None
        self._rows_cache = None

    # --- layout ---------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield ClusterBox(id="cluster")
        with Horizontal(id="middle"):
            with Vertical(id="left"):
                yield MemBox(id="mem")
                yield NetBox(id="net")
            yield GuestsBox(id="guests")
        yield EventsBox(id="events")

    def on_mount(self) -> None:
        self._timer = self.set_interval(self.refresh_s, self._tick)
        self.set_interval(1.0, lambda: self._refresh_box("#cluster"))
        self.load()

    async def on_unmount(self) -> None:
        await self.collector.close()

    def _refresh_box(self, sel: str) -> None:
        try:
            self.query_one(sel).refresh()
        except NoMatches:
            pass

    def redraw(self) -> None:
        self._rows_cache = None
        for sel in ("#cluster", "#mem", "#net", "#guests", "#events"):
            self._refresh_box(sel)

    def apply_layout(self) -> None:
        s = self.shown
        q = self.query_one
        q("#cluster").display = s[1]
        q("#mem").display, q("#net").display = s[2], s[3]
        q("#left").display = s[2] or s[3]
        q("#guests").display = s[4]
        q("#middle").display = s[2] or s[3] or s[4]
        q("#left").styles.width = "44%" if s[4] else "100%"
        q("#events").display = s[5]
        q("#events").styles.height = "24%" if q("#middle").display else "1fr"
        q("#cluster").styles.height = "30%" if (q("#middle").display or s[5]) else "1fr"
        if not s[4] and self.focus_box == "guests" and s[5]:
            self.focus_box = "events"
        if not s[5] and self.focus_box == "events":
            self.focus_box = "guests"
        self.redraw()

    # --- data -------------------------------------------------------------------------
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
        for err in self.snap.raw.new_errors[:2]:
            self.notify(err, severity="warning", timeout=6)
        self._resync()
        self.redraw()

    @work(exclusive=True, group="tasklog")
    async def load_task_log(self, task: Task) -> None:
        try:
            lines = await self.collector.task_log(task.node, task.upid)
        except Exception as e:  # noqa: BLE001
            lines = [f"could not fetch task log: {e}"]
        self.task_log = (task.upid, lines)
        self.redraw()

    # --- row models ---------------------------------------------------------------------
    def guest_rows(self) -> list:
        if self._rows_cache is not None:
            return self._rows_cache
        snap = self.snap
        if snap is None:
            return []
        if self.view == "list":
            f = self.filter.lower()
            rows = []
            for g in snap.raw.guests:
                nets = " ".join(snap.name(f"if:{k}") for k in snap.placement.get(g.id, []))
                hay = f"{g.vmid} {g.name} {g.node} {nets} {g.status}".lower()
                if not f or f in hay:
                    rows.append((g.id, g))
            key = {"cpu": lambda r: r[1].cpu, "mem": lambda r: r[1].mem, "name": lambda r: r[1].name.lower(),
                   "vmid": lambda r: r[1].vmid, "node": lambda r: (r[1].node, r[1].vmid),
                   "net": lambda r: r[1].net_in + r[1].net_out}[self.sort_key]
            desc = self.sort_key in ("cpu", "mem", "net")
            rows.sort(key=key, reverse=desc != self.reverse)
        else:
            roots = cluster_tree(snap) if self.view == "cluster" else network_tree(snap)
            sel = self._current_eid_raw()
            rows = tree_rows(roots, sel, snap.related(sel))
        self._rows_cache = rows
        return rows

    def event_rows(self) -> list[Event]:
        snap = self.snap
        if snap is None:
            return []
        base = snap.events_for(self.detail_eid) if (self.detail_open and self.detail_eid) else snap.events
        th = LEVEL_RANK[self.min_level]
        return [e for e in base if LEVEL_RANK.get(e.level, 1) >= th
                and (self.source == "all" or e.source == self.source)][:500]

    @staticmethod
    def _event_key(e: Event) -> str:
        ref = e.ref
        if isinstance(ref, Task):
            return f"task:{ref.upid}"
        return f"{e.source}:{getattr(ref, 'key', '') or e.ts}"

    def _current_eid_raw(self) -> str | None:
        return self.g_key

    def selected_eid(self) -> str | None:
        return self.g_key

    def related(self) -> set[str]:
        return self.snap.related(self.selected_eid()) if self.snap else set()

    def _resync(self) -> None:
        """Keep selections on the same guest/event across refreshes (like btop's follow)."""
        self._rows_cache = None
        rows = self.guest_rows()
        keys = [r[0] for r in rows]
        if self.g_key in keys:
            self.g_sel = keys.index(self.g_key)
        else:
            self.g_sel = min(self.g_sel, max(0, len(rows) - 1))
            self.g_key = keys[self.g_sel] if rows else None
        events = self.event_rows()
        ekeys = [self._event_key(e) for e in events]
        # Row 0 follows the newest event only while you're not working in the events box;
        # once it has focus, the selection stays on the event you picked.
        if self.e_key in ekeys and (self.e_sel > 0 or self.focus_box == "events"):
            self.e_sel = ekeys.index(self.e_key)
        else:
            self.e_sel = min(self.e_sel, max(0, len(events) - 1))
            self.e_key = ekeys[self.e_sel] if events else None

    def scroll_for(self, which: str, visible: int) -> int:
        sel = self.g_sel if which == "guests" else self.e_sel
        n = len(self.guest_rows()) if which == "guests" else len(self.event_rows())
        s = self.scrolls[which]
        if sel < s:
            s = sel
        elif sel >= s + visible:
            s = sel - visible + 1
        s = max(0, min(s, max(0, n - visible)))
        self.scrolls[which] = s
        return s

    # --- keys -------------------------------------------------------------------------
    OWN_ACTIONS = {a.split("(")[0] for a in KEY_ACTIONS.values()}

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action not in self.OWN_ACTIONS:
            return True          # Textual's own actions (e.g. focus_next inside dialogs)
        if not isinstance(self.screen, ModalScreen) and not self.filter_mode:
            return True
        # Dialogs and filter typing get the keys instead of the dashboard.
        if self.filter_mode and action in ("escape", "enter"):
            return True
        return False

    def on_key(self, event) -> None:
        if not self.filter_mode or isinstance(self.screen, ModalScreen):
            return
        if event.key == "backspace":
            self.filter = self.filter[:-1]
        elif event.character and event.character.isprintable() and len(event.character) == 1:
            self.filter += event.character
        else:
            return
        event.stop()
        self.g_sel, self.g_key = 0, None
        self._resync()
        self.redraw()

    def press_key(self, key: str) -> None:
        """Mouse clicks on hotkey labels arrive here."""
        action = KEY_ACTIONS.get(key)
        if action and self.check_action(action.split("(")[0], ()):
            self.call_later(self.run_action, action)

    def click_row(self, box_id: str, index: int, double: bool) -> None:
        if box_id == "guests":
            self.focus_box = "guests"
            self._set_g(index)
        elif box_id == "events":
            self.focus_box = "events"
            self._set_e(index)
        if double:
            self.action_enter()
        self.redraw()

    def scroll_list(self, box_id: str, delta: int) -> None:
        if box_id == "guests":
            self._set_g(self.g_sel + delta)
        else:
            self._set_e(self.e_sel + delta)
        self.redraw()

    def _set_g(self, i: int) -> None:
        rows = self.guest_rows()
        if not rows:
            return
        self.g_sel = max(0, min(len(rows) - 1, i))
        self.g_key = rows[self.g_sel][0]
        self._rows_cache = None   # tree highlighting follows the selection
        if self.detail_open and self.g_key and self.view != "list":
            self.detail_eid = self.g_key

    def _set_e(self, i: int) -> None:
        events = self.event_rows()
        if not events:
            return
        self.e_sel = max(0, min(len(events) - 1, i))
        self.e_key = self._event_key(events[self.e_sel])

    # actions bound to keys
    def action_escape(self) -> None:
        if self.filter_mode:
            self.filter_mode, self.filter = False, ""
            self._resync()
        elif self.detail_open:
            self.detail_open, self.task_log = False, None
        elif self.filter:
            self.filter = ""
            self._resync()
        self.redraw()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_toggle(self, n: int) -> None:
        if sum(self.shown.values()) == 1 and self.shown[n]:
            return
        self.shown[n] = not self.shown[n]
        self.apply_layout()

    def action_interval(self, delta: int) -> None:
        self.refresh_s = max(1.0, min(60.0, self.refresh_s + delta / 1000))
        if self._timer:
            self._timer.stop()
        self._timer = self.set_interval(self.refresh_s, self._tick)
        self.redraw()

    def action_pause(self) -> None:
        self.paused = not self.paused
        self.redraw()

    def action_switch(self) -> None:
        if self.shown[4] and self.shown[5]:
            self.focus_box = "events" if self.focus_box == "guests" else "guests"
            self.redraw()

    def action_move(self, delta: int) -> None:
        if self.focus_box == "guests":
            self._set_g(self.g_sel + delta)
        else:
            self._set_e(self.e_sel + delta)
        self.redraw()

    def action_enter(self) -> None:
        if self.filter_mode:
            self.filter_mode = False
            self.redraw()
            return
        if self.focus_box == "guests":
            if self.detail_open and self.detail_eid == self.g_key:
                self.detail_open, self.task_log = False, None
            elif self.g_key:
                self.detail_open, self.detail_eid, self.task_log = True, self.g_key, None
            self.e_sel, self.e_key = 0, None
        else:
            events = self.event_rows()
            if not events:
                return
            ev = events[self.e_sel]
            target = next((x for x in ev.entities if x != "fw"), None) or ev.iface or (
                ev.entities[0] if ev.entities else None)
            if not target:
                self.notify("This event isn't tied to a known host.", severity="warning")
                return
            self.jump_to(target)
            if isinstance(ev.ref, Task):
                self.load_task_log(ev.ref)
        self.redraw()

    def jump_to(self, eid: str) -> None:
        """Select eid in the guests box (switching view if needed) and open its details."""
        self.view = "list" if eid.startswith("guest:") else (
            "network" if eid.startswith(("if:", "gw:", "host:")) or eid == "fw" else "cluster")
        if eid.startswith("guest:"):
            self.filter = ""
        self._rows_cache = None
        rows = self.guest_rows()
        keys = [r[0] for r in rows]
        if eid in keys:
            self._set_g(keys.index(eid))
        self.g_key = eid
        self.detail_open, self.detail_eid, self.task_log = True, eid, None
        self.focus_box = "guests"
        self.e_sel, self.e_key = 0, None
        self._resync()

    def action_view(self) -> None:
        self.view = VIEWS[(VIEWS.index(self.view) + 1) % len(VIEWS)]
        self._rows_cache = None
        self._resync()
        self.redraw()

    def action_sort(self, step: int) -> None:
        if self.view != "list":
            return
        self.sort_key = SORTS[(SORTS.index(self.sort_key) + step) % len(SORTS)]
        self._resync()
        self.redraw()

    def action_reverse(self) -> None:
        self.reverse = not self.reverse
        self._resync()
        self.redraw()

    def action_filter(self) -> None:
        self.view = "list"
        self.filter_mode = True
        self.focus_box = "guests"
        self.redraw()

    def action_next_issue(self) -> None:
        if not self.snap or not self.snap.issues:
            self.notify("Nothing needs attention right now.")
            return
        targets = [i.entity for i in self.snap.issues if i.entity]
        if not targets:
            return
        idx = targets.index(self.detail_eid) + 1 if (self.detail_open and self.detail_eid in targets) else 0
        self.jump_to(targets[idx % len(targets)])
        self.redraw()

    def action_iface(self, step: int) -> None:
        self.iface_idx += step
        self._refresh_box("#net")

    def action_level(self) -> None:
        self.min_level = LEVELS[(LEVELS.index(self.min_level) + 1) % len(LEVELS)]
        self.e_sel, self.e_key = 0, None
        self.redraw()

    def action_source(self) -> None:
        self.source = SOURCES[(SOURCES.index(self.source) + 1) % len(SOURCES)]
        self.e_sel, self.e_key = 0, None
        self.redraw()

    # --- actions (x) -----------------------------------------------------------------------
    def action_act(self) -> None:
        if not self.snap:
            return
        if not self.collector.actions_enabled:
            self.notify("Actions are off. Set `actions: enabled: true` in the config (and give the API "
                        "credentials write access) to turn them on.", severity="warning", timeout=8)
            return
        prov = self.collector.provider
        event = None
        if self.focus_box == "events":
            events = self.event_rows()
            event = events[self.e_sel] if events else None
        selected = self.detail_eid if self.detail_open else self.g_key
        acts = available_actions(self.snap, selected if event is None else None, event,
                                 getattr(prov, "has_pve", False), getattr(prov, "has_opn", False),
                                 getattr(prov, "block_alias", None))
        if not acts:
            self.notify("No actions here. Select a guest or host, or a firewall event.", severity="warning")
            return
        title = (f"Actions for event: {event.message[:60]}" if event
                 else f"Actions for {self.snap.name(selected)}")

        def chosen(idx: int | None) -> None:
            if idx is not None:
                self.push_screen(ConfirmAction(acts[idx]), lambda ok: ok and self.execute(acts[idx]))
        self.push_screen(ActionMenu(title, acts), chosen)

    @work(exclusive=False, group="action")
    async def execute(self, action: Action) -> None:
        self.notify(f"{action.label}…", timeout=3)
        try:
            self.notify(await self.collector.run_action(action), timeout=6)
        except Exception as e:  # noqa: BLE001
            self.notify(str(e), severity="error", timeout=10)
        self.load()
