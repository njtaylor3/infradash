"""The five boxes. Each one draws itself onto a Canvas sized to the widget, the way btop
redraws its boxes, and records clickable hotkey labels for mouse support."""
from __future__ import annotations

import time
from collections import Counter
from datetime import datetime

from rich.text import Text
from textual.events import Click, MouseScrollDown, MouseScrollUp
from textual.widget import Widget

from ..models import Guest, fmt_uptime
from ..views import GUEST_ICON, inspector
from .draw import (Canvas, box, fmt_bits, fmt_bytes, graph, hdivider, meter, segments, vdivider)
from .theme import c, fade, gradient

LEVEL_COLOR = {"ERROR": "used_end", "WARN": "cpu_mid", "INFO": "cpu_start", "DEBUG": "inactive_fg"}
SRC_COLOR = {"fw": "available_end", "pve": "cached_mid", "task": "download_end", "you": "hi_fg"}
SEV = {"crit": ("✖", "used_end"), "warn": ("▲", "cpu_mid")}
GIB = 2**30


def _pct_color(v: float) -> str:
    return gradient("process")[int(max(0, min(100, v)))]


class BtopBox(Widget):
    can_focus = False

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.clickables: list[tuple[int, int, int, str]] = []
        self.list_geom: tuple[int, int, int] | None = None   # (first row y, visible rows, scroll)

    def render(self) -> Text:
        cv = Canvas(self.size.width, self.size.height)
        try:
            self.draw(cv)
        except Exception as e:  # noqa: BLE001 - a drawing bug must never take the dashboard down
            cv.put(2, 1, f"render error: {type(e).__name__}: {e}", c("used_end"))
        self.clickables = cv.clickables
        return cv.to_text()

    def draw(self, cv: Canvas) -> None:  # pragma: no cover - overridden
        pass

    def on_click(self, event: Click) -> None:
        for y, x0, x1, key in self.clickables:
            if event.y == y and x0 <= event.x < x1:
                self.app.press_key(key)
                return
        if self.list_geom:
            y0, n, scroll = self.list_geom
            if y0 <= event.y < y0 + n:
                self.app.click_row(self.id, scroll + event.y - y0, event.chain >= 2)

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        if self.list_geom:
            self.app.scroll_list(self.id, 3)

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        if self.list_geom:
            self.app.scroll_list(self.id, -3)


# --- ¹ cluster -----------------------------------------------------------------------------
class ClusterBox(BtopBox):
    def draw(self, cv: Canvas) -> None:
        app, w, h = self.app, cv.w, cv.h
        line, snap = c("cpu_box"), app.snap
        ms = int(app.refresh_s * 1000)
        interval = [("-", "hi", "minus"), (f" {ms}ms ", "title"), ("+", "hi", "plus")]
        paused = [[("PAUSED", "hi")]] if app.paused else []
        box(cv, 0, 0, w, h, line, "cluster", 1,
            top_left=[([(app.collector.name, "title")], None)],
            center=[(time.strftime("%H:%M:%S"), "title")],
            top_right=[(interval, None)] + [(p, None) for p in paused])
        if snap is None:
            cv.put(3, 2, "connecting…", c("graph_text"))
            return
        raw = snap.raw
        ih = h - 2
        nb = min(64, max(44, w // 3)) if w >= 90 else 0
        ab = min(58, max(38, w // 4)) if w >= 150 else 0
        nx = w - 1 - nb
        ax = nx - ab
        gw = (ax if ab else nx) - 1

        # Big graph: cluster CPU above, cluster memory below (inverted), like btop's cpu box.
        up_h = (ih + 1) // 2
        cpu = app.collector.series("agg", "cpu")
        mem = app.collector.series("agg", "mem")
        if gw > 4 and raw.nodes:
            graph(cv, 1, 1, gw, up_h, cpu, "cpu")
            graph(cv, 1, 1 + up_h, gw, ih - up_h, mem, "cached", invert=True)
            cv.put(2, 1, f"cpu {cpu[-1]:.0f}%" if cpu else "cpu", c("graph_text"))
            cv.put(2, h - 2, f"mem {mem[-1]:.0f}%" if mem else "mem", c("graph_text"))
        elif gw > 4:
            cv.put(3, 2, "no Proxmox configured", c("graph_text"))
        up = max((n.uptime for n in raw.nodes if n.online), default=0)
        if up and gw > 30:
            cv.put(gw - 12, h - 2, f"up {fmt_uptime(up)}", c("graph_text"))

        if ab:
            self.bx_attention(cv, ax, 1, ab, ih, line)
        if nb:
            self.bx_nodes(cv, nx, 1, nb, ih, line)

    def bx_attention(self, cv, x, y, w, h, line) -> None:
        snap = self.app.snap
        crit = sum(i.severity == "crit" for i in snap.issues)
        warn = len(snap.issues) - crit
        counts = [(f"✖{crit}", "used_end" if crit else "inactive"), (" ", "title"),
                  (f"▲{warn}", "cpu_mid" if warn else "inactive")]
        box(cv, x, y, w, h, line, "attention", top_right=[([(t, c(r) if r not in ("inactive", "title") else r)
                                                            for t, r in counts], None)],
            bottom_left=[([("a", "hi"), (" next", "title")], "a")])
        if not snap.issues:
            cv.put(x + 2, y + 1, "✓ all clear", c("proc_misc"), True)
            return
        now = time.time()
        for i, issue in enumerate(snap.issues[: h - 2]):
            mark, color = SEV[issue.severity]
            sel = issue.entity and issue.entity == self.app.selected_eid()
            cv.put(x + 1, y + 1 + i, mark, c(color))
            age = now - issue.since
            age_s = f"{int(age)}s" if age < 60 else fmt_uptime(age)
            cv.put(x + 3, y + 1 + i, issue.title, c("title") if sel else c("main_fg"), bool(sel), maxw=w - 5 - len(age_s))
            cv.put(x + w - 1 - len(age_s), y + 1 + i, age_s, c("graph_text"))

    def bx_nodes(self, cv, x, y, w, h, line) -> None:
        app, snap = self.app, self.app.snap
        raw = snap.raw
        online = sum(n.online for n in raw.nodes)
        q = "" if raw.quorate is None else (" quorate" if raw.quorate else " NO QUORUM")
        box(cv, x, y, w, h, line, snap.name("cluster") if raw.nodes else "firewall",
            top_right=[([(f"{online}/{len(raw.nodes)} up{q}", "title" if raw.quorate is not False else c("used_end"))], None)]
            if raw.nodes else None)
        iw = w - 2
        name_w, pct_w = 9, 4
        rest = iw - name_w - pct_w * 2 - 4
        gw_ = max(4, rest * 55 // 100)
        mw = max(3, rest - gw_)
        related = app.related()
        rows = []
        if raw.nodes:
            rows.append(("total", "agg", "cpu", "mem", True, None))
        for n in sorted(raw.nodes, key=lambda n: n.name):
            rows.append((n.name, "cpu", n.name, n.name, n.online, n.id))
        if raw.fw:
            rows.append((raw.fw.name, "cpu", raw.fw.name, raw.fw.name, True, "fw"))
        yy = y + 1
        for label, panel, key_cpu, key_mem, ok, eid in rows:
            if yy >= y + h - 1:
                break
            hot = eid in related
            cv.put(x + 1, yy, label[:name_w].ljust(name_w), c("title") if (label == "total" or hot) else c("main_fg"),
                   bold=label == "total" or hot)
            if not ok:
                cv.put(x + 1 + name_w + 1, yy, "offline", c("used_end"), True)
                yy += 1
                continue
            cpu = app.collector.series("agg" if label == "total" else "cpu", key_cpu)
            mem = app.collector.series("agg" if label == "total" else "mem", key_mem)
            gx = x + 1 + name_w + 1
            if label == "total":
                meter(cv, gx, yy, gw_, cpu[-1] if cpu else 0, "cpu")
            else:
                graph(cv, gx, yy, gw_, 1, cpu, "cpu")
            cv.put(gx + gw_ + 1, yy, f"{(cpu[-1] if cpu else 0):3.0f}%", _pct_color(cpu[-1] if cpu else 0))
            mx = gx + gw_ + 1 + pct_w + 1
            meter(cv, mx, yy, mw, mem[-1] if mem else 0, "used")
            cv.put(mx + mw + 1, yy, f"{(mem[-1] if mem else 0):3.0f}%", c("main_fg"))
            yy += 1
        for gw in raw.gateways:
            if yy >= y + h - 1:
                break
            bad = gw.status != "online"
            txt = f"{gw.name} {gw.rtt_ms or 0:.1f}ms {gw.loss_pct or 0:.0f}% loss"
            cv.put(x + 1, yy, "gw ", c("graph_text"))
            cv.put(x + 4, yy, txt, c("used_end") if bad else c("main_fg"), bad, maxw=iw - 4)
            yy += 1
        if raw.fw and raw.fw.states is not None and yy < y + h - 1:
            lim = f"/{raw.fw.states_limit // 1000}k" if raw.fw.states_limit else ""
            cv.put(x + 1, yy, f"states {raw.fw.states:,}{lim}", c("graph_text"))


# --- ² mem / disks --------------------------------------------------------------------------
class MemBox(BtopBox):
    def draw(self, cv: Canvas) -> None:
        app, w, h = self.app, cv.w, cv.h
        line, snap = c("mem_box"), app.snap
        box(cv, 0, 0, w, h, line, "mem", 2)
        if snap is None:
            return
        raw = snap.raw
        lw = max(20, w * 40 // 100) if w >= 60 else w - 1
        if lw < w - 1:
            vdivider(cv, lw, 0, h, line)
            segments(cv, lw + 1, 0, w - lw - 2, line, True, left=[([("disks", "title")], None)])
        related = app.related()

        # Memory: one block per node plus the firewall.
        rows = [(n.name, n.id, n.online) for n in sorted(raw.nodes, key=lambda n: n.name)]
        if raw.fw:
            rows.append((raw.fw.name, "fw", True))
        tall = (h - 2) >= len(rows) * 2
        yy = 1
        for name, eid, ok in rows:
            if yy >= h - 1:
                break
            series = app.collector.series("mem", name)
            v = series[-1] if series else 0
            hot = eid in related
            cv.put(2, yy, f"{name}:", c("title"), hot or tall)
            cv.put(lw - 6, yy, f"{v:4.0f}%" if ok else " down", c("main_fg") if ok else c("used_end"))
            if tall:
                graph(cv, 2, yy + 1, lw - 3, 1, series, "used")
                yy += 2
            else:
                graph(cv, 2 + len(name) + 2, yy, max(2, lw - 10 - len(name)), 1, series, "used")
                yy += 1

        if lw >= w - 1:
            return
        # Disks: health first (ZFS pools not ONLINE, Ceph), then storages fullest first.
        x0, dw = lw + 2, w - lw - 3
        yy = 1
        for z in sorted(raw.zpools, key=lambda z: (z.health == "ONLINE", -z.pct)):
            if yy >= h - 1:
                return
            ok = z.health.upper() == "ONLINE"
            hot = z.id in related or z.id == app.selected_eid()
            label = f"{z.name}@{z.node}"
            cv.put(x0, yy, label, c("title"), True, maxw=dw - 22)
            cv.put(x0 + min(len(label), dw - 22) + 1, yy, z.health, c("proc_misc") if ok else c("used_end"), not ok)
            meter(cv, x0 + dw - 15, yy, 9, z.pct, "used")
            cv.put(x0 + dw - 5, yy, f"{z.pct:3.0f}%", c("main_fg"), hot)
            yy += 1
        if raw.ceph and yy < h - 1:
            ce = raw.ceph
            col = {"HEALTH_OK": "proc_misc", "HEALTH_WARN": "cpu_mid"}.get(ce.health, "used_end")
            cv.put(x0, yy, "ceph", c("title"), True)
            cv.put(x0 + 5, yy, f"{ce.health} {ce.osds_up}/{ce.osds} osd", c(col), ce.health != "HEALTH_OK",
                   maxw=dw - 20)
            meter(cv, x0 + dw - 15, yy, 9, ce.pct, "used")
            cv.put(x0 + dw - 5, yy, f"{ce.pct:3.0f}%", c("main_fg"))
            yy += 1
        if yy < h - 1 and (raw.zpools or raw.ceph):
            cv.put(x0, yy, "─" * dw, c("div_line"))
            yy += 1
        stor = sorted(raw.storages, key=lambda s: (s.active, -s.pct))
        two = (h - 1 - yy) >= len(stor) * 2
        for st in stor:
            if yy >= h - 1:
                break
            hot = st.id in related or st.id == app.selected_eid()
            size = fmt_bytes(st.total)
            if two:
                cv.put(x0, yy, st.label, c("title"), True, maxw=dw - len(size) - 1)
                cv.put(x0 + dw - len(size), yy, size, c("main_fg"))
                yy += 1
            if not st.active:
                cv.put(x0, yy, (st.label + " " if not two else "") + "UNAVAILABLE", c("used_end"), True)
            else:
                lbl = "Used:" if two else st.label[: max(4, dw - 26)]
                cv.put(x0, yy, lbl, c("title") if hot else c("main_fg"), hot)
                cv.put(x0 + len(lbl) + 1, yy, f"{st.pct:3.0f}%", _pct_color(st.pct))
                mx = x0 + len(lbl) + 6
                mwid = max(3, dw - (mx - x0) - 10)
                meter(cv, mx, yy, mwid, st.pct, "used")
                cv.put(x0 + dw - 9, yy, fmt_bytes(st.used).rjust(9), c("main_fg"))
            yy += 1


# --- ³ net ------------------------------------------------------------------------------------
class NetBox(BtopBox):
    def draw(self, cv: Canvas) -> None:
        app, w, h = self.app, cv.w, cv.h
        line, snap = c("net_box"), app.snap
        ifaces = snap.raw.ifaces if snap else []
        iface = ifaces[app.iface_idx % len(ifaces)] if ifaces else None
        sel = [("<b", "hi", "b"), (f" {iface.name if iface else '-'} ", "title"), ("n>", "hi", "n")]
        box(cv, 0, 0, w, h, line, "net", 3,
            top_left=[([(iface.cidr or iface.device, "title")], None)] if iface else None,
            top_right=[([("auto", "title")], None), (sel, None)])
        if not iface:
            cv.put(2, 2, "no OPNsense configured" if snap else "connecting…", c("graph_text"))
            return
        ih = h - 2
        sb = min(32, max(26, w // 3)) if w >= 56 else 0
        gw = w - 2 - (sb + 1 if sb else 0)
        rx = app.collector.series("rx", iface.key)
        tx = app.collector.series("tx", iface.key)
        window = rx[-gw * 2:] + tx[-gw * 2:]
        top = max(max(window, default=0), 10_000)         # btop auto-scale, synced up/down
        up_h = (ih + 1) // 2
        graph(cv, 1, 1, gw, up_h, rx, "download", max_value=top)
        graph(cv, 1, 1 + up_h, gw, ih - up_h, tx, "upload", max_value=top, invert=True)
        cv.put(1, 1, fmt_bits(top), c("graph_text"))
        cv.put(1, h - 2, fmt_bits(top), c("graph_text"))
        if not sb:
            return
        sx = w - 1 - sb
        box(cv, sx, 1, sb, ih, line, "download", bottom_left=[([("upload", "title")], None)])
        iw = sb - 2
        blocked = sum(1 for e in snap.events_for(iface.id)
                      if e.source == "fw" and e.level != "INFO" and e.ts > time.time() - 300)
        top_rx, top_tx = max(rx, default=0), max(tx, default=0)
        lines = [
            ("▼", f"{fmt_bits(iface.rx_bps)}", "download_end"),
            ("▼", f"Top: {fmt_bits(top_rx)}", "main_fg"),
        ]
        for gw_ in snap.raw.gateways:
            if snap.gw_iface.get(gw_.id) == iface.key:
                bad = gw_.status != "online"
                lines.append(("◎", f"{gw_.name} {gw_.rtt_ms or 0:.0f}ms {gw_.loss_pct or 0:.0f}%",
                              "used_end" if bad else "main_fg"))
        lines.append(("✖", f"blocked {blocked} /5m", "used_end" if blocked else "graph_text"))
        if iface.vlan is not None:
            lines.append(("·", f"vlan {iface.vlan} {iface.device}", "graph_text"))
        low = [("▲", f"Top: {fmt_bits(top_tx)}", "main_fg"), ("▲", f"{fmt_bits(iface.tx_bps)}", "upload_end")]
        avail = ih - 2
        top_lines = lines[: max(0, avail - len(low))]
        for i, (sym, txt, col) in enumerate(top_lines):
            cv.put(sx + 1, 2 + i, sym, c(col))
            cv.put(sx + 3, 2 + i, txt, c(col), col.endswith("_end"), maxw=iw - 2)
        for i, (sym, txt, col) in enumerate(low):
            yy = 1 + ih - 1 - len(low) + i
            if yy > 1 + len(top_lines):
                cv.put(sx + 1, yy, sym, c(col))
                cv.put(sx + 3, yy, txt, c(col), col.endswith("_end"), maxw=iw - 2)


# --- ⁴ guests (btop's process box) ------------------------------------------------------------
LIST_COLS = [  # (header, width, align, drop-priority: higher drops first)
    ("Vmid:", 6, "r", 0), ("Name:", 0, "l", 0), ("Node:", 7, "l", 2), ("Type:", 5, "l", 4),
    ("Network:", 14, "l", 3), ("Net ↓/↑:", 18, "r", 5), ("Mem%:", 6, "r", 1), ("Cpu%", 12, "r", 0),
]


class GuestsBox(BtopBox):
    def draw(self, cv: Canvas) -> None:
        app, w, h = self.app, cv.w, cv.h
        line, snap = c("proc_box"), app.snap
        rows = app.guest_rows()
        n = len(rows)
        sel = app.g_sel
        detail_h = min(13, h // 2) if (app.detail_open and h >= 18) else 0
        filt = ([("f", "hi", "f"), ("ilter: ", "title"), (app.filter + ("_" if app.filter_mode else ""), "fg")]
                if (app.filter or app.filter_mode) else [("f", "hi", "f"), ("ilter", "title")])
        view = [("e", "hi", "e"), (f" {app.view}", "title")]
        sort = [("<", "hi", "left"), (f" {app.sort_key} ", "title"), (">", "hi", "right")]
        rev = [("r", "hi", "r"), ("everse", "title" if app.reverse else "inactive")]
        list_left = [(filt, None)]
        list_right = [(view, None)] + ([(rev, None), (sort, None)] if app.view == "list" else [])
        focus = app.focus_box == "guests"
        bottom_left = [([("↑", "hi", "up"), (" select ", "title"), ("↓", "hi", "down")], None),
                       ([("info ", "title"), ("↵", "hi")], "enter"),
                       ([("x", "hi"), (" actions", "title")], "x")]
        bottom_right = [([(f"{sel + 1 if n else 0}/{n}", "title")], None)]
        if detail_h:
            eid = app.detail_eid
            g = snap.guest(eid) if (snap and eid) else None
            head = ([(str(g.vmid), "title")], None) if g else ([(snap.name(eid) if snap else "", "title")], None)
            name = ([(g.name, "title")], None) if g else None
            box(cv, 0, 0, w, h, line, top_left=[head] + ([name] if name else []),
                top_right=[([("x", "hi"), (" actions", "title")], "x"), ([("hide ", "title"), ("↵", "hi")], "enter")],
                bottom_left=bottom_left, bottom_right=bottom_right)
            hdivider(cv, 0, detail_h, w, line)
            segments(cv, 0, detail_h, w, line, True,
                     left=[([("⁴", "hi"), ("guests", "title")], "4")] + list_left, right=list_right)
            self.bx_detail(cv, 1, 1, w - 2, detail_h - 1, line)
            y0 = detail_h + 1
        else:
            box(cv, 0, 0, w, h, line, "guests", 4, top_left=list_left, top_right=list_right,
                bottom_left=bottom_left, bottom_right=bottom_right)
            y0 = 1
        if snap is None:
            return
        if app.view == "list":
            self.bx_list(cv, rows, y0, focus)
        else:
            self.bx_tree(cv, rows, y0, focus)

    # list view, btop-style columns
    def bx_columns(self, width: int):
        cols = list(LIST_COLS)
        fixed = sum(cw for _, cw, _, _ in cols if cw)
        while fixed + 12 > width and any(p > 0 for *_, p in cols):
            worst = max(cols, key=lambda col: col[3])
            cols.remove(worst)
            fixed = sum(cw for _, cw, _, _ in cols if cw)
        name_w = max(8, width - fixed - len(cols))
        return [(hd, cw or name_w, al) for hd, cw, al, _ in cols]

    def bx_list(self, cv: Canvas, rows, y0: int, focus: bool) -> None:
        app, snap, w, h = self.app, self.app.snap, cv.w, cv.h
        cols = self.bx_columns(w - 3)
        x = 1
        for hd, cw, al in cols:
            cv.put(x, y0, hd.rjust(cw) if al == "r" else hd.ljust(cw), c("title"), True)
            x += cw + 1
        vis = h - 2 - y0
        scroll = app.scroll_for("guests", vis)
        self.list_geom = (y0 + 1, vis, scroll)
        for i, (eid, g) in enumerate(rows[scroll: scroll + vis]):
            yy = y0 + 1 + i
            idx = scroll + i
            running = g.status == "running"
            base = fade(i, vis) if running else c("inactive_fg")
            nets = ", ".join(snap.name(f"if:{k}") for k in snap.placement.get(g.id, [])) or "-"
            cells = {
                "Vmid:": (str(g.vmid), base), "Name:": (g.name, c("title") if running else base),
                "Node:": (g.node, base), "Type:": ("VM" if g.kind == "qemu" else "CT", base),
                "Network:": (nets, base),
                "Net ↓/↑:": (f"{fmt_bits(g.net_in)}/{fmt_bits(g.net_out)}" if running else "", base),
                "Mem%:": (f"{g.mem:.0f}" if running else "", _pct_color(g.mem) if running else base),
            }
            x = 1
            for hd, cw, al in cols:
                if hd == "Cpu%":
                    if running:
                        graph(cv, x, yy, 5, 1, app.collector.series("gcpu", g.id), "cpu")
                        cv.put(x + 6, yy, f"{g.cpu:5.1f}"[:cw - 6].rjust(cw - 6), _pct_color(g.cpu))
                    else:
                        cv.put(x, yy, g.status[:cw].rjust(cw), base)
                else:
                    txt, col = cells[hd]
                    txt = txt[:cw]
                    cv.put(x, yy, txt.rjust(cw) if al == "r" else txt.ljust(cw), col)
                x += cw + 1
            if idx == app.g_sel:
                self.bx_highlight(cv, yy, focus)
        self.bx_scrollbar(cv, y0, vis, scroll, len(rows))

    def bx_tree(self, cv: Canvas, rows, y0: int, focus: bool) -> None:
        app, w, h = self.app, cv.w, cv.h
        vis = h - 1 - y0
        scroll = app.scroll_for("guests", vis)
        self.list_geom = (y0, vis, scroll)
        for i, (eid, text) in enumerate(rows[scroll: scroll + vis]):
            yy = y0 + i
            cv.rich(2, yy, text, maxw=w - 4)
            if scroll + i == app.g_sel:
                self.bx_highlight(cv, yy, focus)
        self.bx_scrollbar(cv, y0 - 1, vis + 1, scroll, len(rows))

    def bx_highlight(self, cv: Canvas, yy: int, focus: bool) -> None:
        if focus:
            cv.fill_bg(1, yy, cv.w - 2, c("selected_bg"), c("selected_fg"))
            for x in range(1, cv.w - 1):
                cv.bold[yy][x] = True
        else:
            cv.fill_bg(1, yy, cv.w - 2, "#2a1a1a")

    def bx_scrollbar(self, cv: Canvas, y0: int, vis: int, scroll: int, n: int) -> None:
        if n <= vis or vis < 3:
            return
        x = cv.w - 2
        cv.put(x, y0, "↑", c("hi_fg"), True)
        cv.put(x, y0 + vis, "↓", c("hi_fg"), True)
        pos = y0 + 1 + int((vis - 2) * scroll / max(1, n - vis))
        cv.put(x, min(pos, y0 + vis - 1), "█", c("main_fg"))

    def bx_detail(self, cv: Canvas, x: int, y: int, w: int, h: int, line: str) -> None:
        app, snap = self.app, self.app.snap
        eid = app.detail_eid
        if snap is None or not eid:
            return
        g = snap.guest(eid)
        if g is None:
            for i, ln in enumerate(inspector(snap, eid, app.task_log).split("\n")[:h]):
                cv.rich(x + 1, y + i, ln, maxw=w - 2)
            return
        gw = max(22, w // 3)
        cpu = app.collector.series("gcpu", g.id)
        graph(cv, x + 2, y, gw - 2, h, cpu, "cpu")
        cv.put(x + 2, y, f"{g.cpu:.1f}%" if g.status == "running" else g.status, c("title"), True)
        for i, ch in enumerate("CPU"):
            cv.put(x, y + h // 2 - 1 + i, ch, c("title"), True)
        vdivider(cv, x + gw, y - 1, h + 2, line)
        rx, rw = x + gw + 2, w - gw - 3
        nets = ", ".join(snap.name(f"if:{k}") for k in snap.placement.get(g.id, [])) or "-"
        heads = [("Status:", g.status), ("Uptime:", fmt_uptime(g.uptime) if g.uptime else "-"),
                 ("Node:", g.node), ("Type:", "VM" if g.kind == "qemu" else "CT"), ("Network:", nets)]
        colw = max(10, rw // len(heads))
        for i, (hd, val) in enumerate(heads):
            cv.put(rx + i * colw, y, hd, c("title"), True, maxw=colw - 1)
            cv.put(rx + i * colw, y + 1, val, c("main_fg") if val != "stopped" else c("inactive_fg"), maxw=colw - 1)
        yy = y + 3
        if yy < y + h:
            cv.put(rx, yy, f"Memory: {g.mem:.0f}%", c("title"), True)
            mw = max(5, rw - 34)
            meter(cv, rx + 14, yy, mw, g.mem, "used")
            cv.put(rx + 15 + mw, yy, f"{g.mem / 100 * g.maxmem / GIB:.1f}/{g.maxmem / GIB:.1f} GiB", c("main_fg"))
            yy += 1
        if yy < y + h:
            cv.put(rx, yy, "Net:", c("title"), True)
            cv.put(rx + 8, yy, f"▼ {fmt_bits(g.net_in)}", c("download_end"))
            cv.put(rx + 24, yy, f"▲ {fmt_bits(g.net_out)}", c("upload_end"))
            yy += 1
        ips = ", ".join(snap.guest_ips.get(g.id, [])) or "none seen"
        info = [("IP:", ips)]
        if g.disks:
            info.append(("Disks:", ", ".join(f"{n} {s}" for n, s in g.disks)))
        fw = [e for e in snap.events_for(g.id) if e.source == "fw"]
        if fw:
            blocked = [e for e in fw if e.level != "INFO"]
            top = Counter(f"{e.ref.proto}/{e.ref.dport}" for e in blocked).most_common(2)
            info.append(("Firewall:", f"{len(fw) - len(blocked)} passed · {len(blocked)} blocked"
                         + (f" · top {', '.join(f'{k}×{v}' for k, v in top)}" if top else "")))
        for r in snap.raw.replication:
            if r.vmid == g.vmid:
                info.append(("Replic.:", f"{r.job} → {r.target} " + (f"FAILING ×{r.fail_count}" if r.fail_count else "ok")))
        for issue in (i for i in snap.issues if i.entity == g.id):
            info.append(("Issue:", issue.title))
        if app.task_log:
            info.append(("Task:", app.task_log[0][:60]))
            info += [("", ln) for ln in app.task_log[1][-4:]]
        for hd, val in info:
            if yy >= y + h:
                break
            bad = hd == "Issue:" or "FAILING" in val
            cv.put(rx, yy, hd, c("title"), True)
            cv.put(rx + 10, yy, val, c("used_end") if bad else c("main_fg"), maxw=rw - 10)
            yy += 1


# --- ⁵ events ---------------------------------------------------------------------------------
class EventsBox(BtopBox):
    def draw(self, cv: Canvas) -> None:
        app, w, h = self.app, cv.w, cv.h
        line, snap = c("event_box"), app.snap
        events = app.event_rows()
        n = len(events)
        focus = app.focus_box == "events"
        scope = ([([("for ", "title"), (snap.name(app.detail_eid), "fg")], None)]
                 if (snap and app.detail_open and app.detail_eid) else [])
        box(cv, 0, 0, w, h, line, "events", 5, top_left=scope,
            top_right=[([("l", "hi"), (f" ≥{app.min_level}", "title")], "l"),
                       ([("s", "hi"), (f" src:{app.source}", "title")], "s")],
            bottom_left=[([("↑", "hi", "up"), (" select ", "title"), ("↓", "hi", "down")], None),
                         ([("jump ", "title"), ("↵", "hi")], "enter"),
                         ([("x", "hi"), (" actions", "title")], "x"),
                         ([("tab", "hi"), (" focus", "title")], "tab")],
            bottom_right=[([(f"{app.e_sel + 1 if n else 0}/{n}", "title")], None)])
        if snap is None:
            return
        if not events:
            base = snap.events_for(app.detail_eid) if (app.detail_open and app.detail_eid) else snap.events
            hidden = sum(1 for e in base if app.source in ("all", e.source))
            msg = (f"no events ≥{app.min_level} — {hidden} below that level, press l to show them"
                   if hidden else "no events")
            cv.put(max(2, (w - len(msg)) // 2), h // 2, msg, c("graph_text"))
            return
        heads = [("Time:", 9), ("Src:", 5), ("Level:", 6), ("About:", 16), ("Event:", 0)]
        x = 1
        for hd, cw in heads:
            cv.put(x, 1, hd, c("title"), True)
            x += (cw or 0) + 1
        vis = h - 3
        scroll = app.scroll_for("events", vis)
        self.list_geom = (2, vis, scroll)
        for i, e in enumerate(events[scroll: scroll + vis]):
            yy = 2 + i
            primary = next((x_ for x_ in e.entities if x_ != "fw"), None) or e.iface or (
                e.entities[0] if e.entities else None)
            x = 1
            cv.put(x, yy, datetime.fromtimestamp(e.ts).strftime("%H:%M:%S"), c("graph_text"))
            x += 10
            cv.put(x, yy, e.source, c(SRC_COLOR.get(e.source, "main_fg")))
            x += 6
            cv.put(x, yy, e.level, c(LEVEL_COLOR.get(e.level, "main_fg")), e.level == "ERROR")
            x += 7
            cv.put(x, yy, snap.name(primary)[:16], c("main_fg"))
            x += 17
            cv.put(x, yy, e.message, c("used_end") if e.level == "ERROR" else fade(i, vis), maxw=w - x - 2)
            if scroll + i == app.e_sel:
                if focus:
                    cv.fill_bg(1, yy, w - 2, c("selected_bg"), c("selected_fg"))
                else:
                    cv.fill_bg(1, yy, w - 2, "#20181f")
        if n > vis and vis >= 3:
            cv.put(w - 2, 1, "↑", c("hi_fg"), True)
            cv.put(w - 2, 1 + vis, "↓", c("hi_fg"), True)


def tree_rows(roots, selected: str | None, path: set[str]) -> list[tuple[str | None, Text]]:
    """Flatten topology trees into selectable rows (same drawing as the classic view)."""
    out: list[tuple[str | None, Text]] = []

    def walk(node, prefix: str, last: bool, parent) -> None:
        line = Text(prefix, style="#606060")
        if parent is not None:
            on_path = node.eid in path and parent.eid in path
            line.append("└─ " if last else "├─ ", style=c("hi_fg") if on_path else "#606060")
        label = node.label.copy()
        if node.eid in path:
            label.stylize("bold")
        line.append_text(label)
        out.append((node.eid, line))
        child_prefix = prefix if parent is None else prefix + ("   " if last else "│  ")
        for i, ch in enumerate(node.children):
            walk(ch, child_prefix, i == len(node.children) - 1, node)

    for r in roots:
        walk(r, "", True, None)
    return out
