"""Colours from btop's built-in "Default" theme, and btop-style 101-step gradients."""
from __future__ import annotations

from functools import lru_cache

# Values from btop's src/btop_theme.cpp Default theme ("#cc" is shorthand for #cccccc).
DEFAULT = {
    "main_bg": "#00", "main_fg": "#cc", "title": "#ee", "hi_fg": "#b54040",
    "selected_bg": "#6a2f2f", "selected_fg": "#ee", "inactive_fg": "#40", "graph_text": "#60",
    "meter_bg": "#40", "proc_misc": "#0de756", "div_line": "#30",
    "cpu_box": "#556d59", "mem_box": "#6c6c4b", "net_box": "#5c588d", "proc_box": "#805252",
    "cpu_start": "#77ca9b", "cpu_mid": "#cbc06c", "cpu_end": "#dc4c4c",
    "free_start": "#384f21", "free_mid": "#b5e685", "free_end": "#dcff85",
    "cached_start": "#163350", "cached_mid": "#74e6fc", "cached_end": "#26c5ff",
    "used_start": "#592b26", "used_mid": "#d9626d", "used_end": "#ff4769",
    "download_start": "#291f75", "download_mid": "#4f43a3", "download_end": "#b0a9de",
    "upload_start": "#620665", "upload_mid": "#7d4180", "upload_end": "#dcafde",
    "process_start": "#80d0a3", "process_mid": "#dcd179", "process_end": "#d45454",
    "temp_start": "#4897d4", "temp_mid": "#5474e8", "temp_end": "#ff40b6",
    # Not in btop: a box colour for the events box (between net and proc).
    "event_box": "#6d5a7a",
}


def _hex(c: str) -> str:
    c = c.lstrip("#")
    if len(c) == 2:
        c = c * 3
    return "#" + c.lower()


def c(name: str) -> str:
    return _hex(DEFAULT.get(name, name if name.startswith("#") else "#cc"))


def _rgb(h: str) -> tuple[int, int, int]:
    h = _hex(h)[1:]
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(a: str, b: str, t: float) -> str:
    (r1, g1, b1), (r2, g2, b2) = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % (round(r1 + (r2 - r1) * t), round(g1 + (g2 - g1) * t), round(b1 + (b2 - b1) * t))


@lru_cache(maxsize=32)
def gradient(name: str) -> tuple[str, ...]:
    """101 colours, 0..100, start→mid→end, like btop's Theme::g()."""
    start, end = c(f"{name}_start"), c(f"{name}_end")
    mid = DEFAULT.get(f"{name}_mid")
    out = []
    for i in range(101):
        if mid:
            out.append(_mix(start, c(f"{name}_mid"), i / 50) if i <= 50 else _mix(c(f"{name}_mid"), end, (i - 50) / 50))
        else:
            out.append(_mix(start, end, i / 100))
    return tuple(out)


def fade(i: int, n: int) -> str:
    """btop's proc_gradient: list rows darken towards the bottom."""
    if n <= 1:
        return c("main_fg")
    return _mix(c("main_fg"), "#505050", min(1.0, i / max(1, n - 1)))
