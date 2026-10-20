"""Colour themes for both interfaces, and btop-style 101-step gradients.

Two themes:

* ``marathon`` (default) — the U.E.S.C. Marathon phosphor palette shared with the NTH//07
  Alacritty, Marathon Vim and marathon-shell configs: phosphor green on near-black, cyan-green
  accents, amber-green for warnings, and red kept back for severity only.
* ``btop`` — btop's built-in "Default" theme for the btop layout, and the original named
  colours for the classic layout.

``c(key)`` returns a btop palette colour, ``gradient(name)`` a 101-step gradient, and
``st(role)`` a Rich style string for the shared views (inspector, trees, classic tables).
Call ``set_theme()`` before the interface is built.
"""
from __future__ import annotations

from functools import lru_cache

# Values from btop's src/btop_theme.cpp Default theme ("#cc" is shorthand for #cccccc).
BTOP = {
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
    # Not in btop: a box colour for the events box (between net and proc), and the
    # greys and row tints infradash used to hard-code.
    "event_box": "#6d5a7a",
    "tree_fg": "#60", "fade_end": "#50", "guest_alt_bg": "#2a1a1a", "event_alt_bg": "#20181f",
}

# The NTH//07 Alacritty palette, as used across Marathon Vim and marathon-shell:
#   bg #0a0f0a  fg #39ff5a  bright-black #1a2e1a  white #a8ffa8  bright-white #d4ffd4
#   cyan #00ffaa  blue #1aff8c  yellow #b8ff70  red #ff4444 (severity only)
MARATHON = {
    "main_bg": "#0a0f0a", "main_fg": "#39ff5a", "title": "#d4ffd4", "hi_fg": "#00ffaa",
    "selected_bg": "#1a2e1a", "selected_fg": "#d4ffd4", "inactive_fg": "#2a4a2e",
    "graph_text": "#4f8a56", "meter_bg": "#1a2e1a", "proc_misc": "#70ff88", "div_line": "#1a2e1a",
    "cpu_box": "#2f8a45", "mem_box": "#6a8a2f", "net_box": "#1f8a6a", "proc_box": "#3f7a3f",
    "event_box": "#2a6a55",
    "cpu_start": "#2fa84a", "cpu_mid": "#b8ff70", "cpu_end": "#ff4444",
    "free_start": "#14301a", "free_mid": "#39ff5a", "free_end": "#b0ffb8",
    "available_start": "#163a1e", "available_mid": "#70ff88", "available_end": "#ccff88",
    "cached_start": "#0f3a2e", "cached_mid": "#00ffaa", "cached_end": "#33ffcc",
    "used_start": "#1a3a1f", "used_mid": "#b8ff70", "used_end": "#ff4444",
    "download_start": "#0f2e1c", "download_mid": "#1aff8c", "download_end": "#b0ffb8",
    "upload_start": "#0f2a2e", "upload_mid": "#00c99a", "upload_end": "#33ffcc",
    "process_start": "#39ff5a", "process_mid": "#b8ff70", "process_end": "#ff4444",
    "temp_start": "#1aff8c", "temp_mid": "#b8ff70", "temp_end": "#ff4444",
    "tree_fg": "#2f6a38", "fade_end": "#1f5a2a", "guest_alt_bg": "#0f1f12", "event_alt_bg": "#0d1a12",
}

# Semantic Rich styles for the shared views and the classic layout.
_ROLES_BTOP = {
    "ok": "green", "warn": "yellow", "crit": "bold red", "bad": "red", "dim": "dim", "strong": "bold",
    "fw": "orange1", "fw_strong": "bold orange1", "net": "cyan", "node": "deep_sky_blue1",
    "node_strong": "bold deep_sky_blue1", "path": "bold magenta",
    "src_fw": "orange1", "src_pve": "deep_sky_blue1", "src_task": "medium_purple1", "src_you": "bold magenta",
}
_ROLES_MARATHON = {
    "ok": "#39ff5a", "warn": "#b8ff70", "crit": "bold #ff4444", "bad": "#ff6666", "dim": "#4f8a56",
    "strong": "bold #d4ffd4", "fw": "#ccff88", "fw_strong": "bold #ccff88", "net": "#33ffcc",
    "node": "#44ffaa", "node_strong": "bold #44ffaa", "path": "bold #00ffaa",
    "src_fw": "#ccff88", "src_pve": "#44ffaa", "src_task": "#33ffcc", "src_you": "bold #00ffaa",
}
_ROWS_BTOP = ["deep_sky_blue1", "medium_purple1", "turquoise2", "spring_green2", "sky_blue1",
              "light_slate_blue", "aquamarine1", "steel_blue1", "medium_orchid", "dark_sea_green2"]
_ROWS_MARATHON = ["#39ff5a", "#00ffaa", "#b8ff70", "#44ffaa", "#70ff88",
                  "#33ffcc", "#ccff88", "#1aff8c", "#a8ffa8", "#00e56e"]

THEMES = {
    "marathon": (MARATHON, _ROLES_MARATHON, _ROWS_MARATHON),
    "btop": (BTOP, _ROLES_BTOP, _ROWS_BTOP),
}
DEFAULT_THEME = "marathon"

_active = DEFAULT_THEME
DEFAULT = MARATHON   # the active btop palette (kept under its old name)


def set_theme(name: str) -> None:
    global _active, DEFAULT
    if name not in THEMES:
        raise ValueError(f"unknown theme {name!r} (choose from {', '.join(THEMES)})")
    _active, DEFAULT = name, THEMES[name][0]
    gradient.cache_clear()


def theme_name() -> str:
    return _active


def st(role: str) -> str:
    """Rich style string for a semantic role ("ok", "crit", "dim", ...)."""
    return THEMES[_active][1].get(role, "")


def row_colors() -> list[str]:
    return THEMES[_active][2]


def textual_theme():
    """The classic layout's Textual theme for the active palette, or None to keep Textual's."""
    if _active != "marathon":
        return None
    from textual.theme import Theme
    p = MARATHON
    return Theme(
        name="marathon", dark=True,
        primary=p["main_fg"], secondary=p["cached_mid"], accent=p["hi_fg"],
        warning=p["cpu_mid"], error=p["used_end"], success=p["proc_misc"],
        foreground=p["main_fg"], background=p["main_bg"], surface="#0f170f", panel=p["selected_bg"],
    )


def _hex(c: str) -> str:
    c = c.lstrip("#")
    if len(c) == 2:
        c = c * 3
    return "#" + c.lower()


def c(name: str) -> str:
    return _hex(DEFAULT.get(name, name if name.startswith("#") else DEFAULT["main_fg"]))


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
    return _mix(c("main_fg"), c("fade_end"), min(1.0, i / max(1, n - 1)))
