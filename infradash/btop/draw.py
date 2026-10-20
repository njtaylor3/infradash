"""Drawing primitives ported from btop's src/btop_draw.cpp: boxes with title notches,
braille graphs (same symbol tables and algorithm) and ■ meters — drawn onto a cell canvas
that is turned into one Rich Text per box."""
from __future__ import annotations

from rich.style import Style
from rich.text import Text

from .theme import c, gradient

SUPERSCRIPT = "⁰¹²³⁴⁵⁶⁷⁸⁹"
BRAILLE_UP = (" ", "⢀", "⢠", "⢰", "⢸", "⡀", "⣀", "⣠", "⣰", "⣸", "⡄", "⣄", "⣤", "⣴", "⣼",
              "⡆", "⣆", "⣦", "⣶", "⣾", "⡇", "⣇", "⣧", "⣷", "⣿")
BRAILLE_DOWN = (" ", "⠈", "⠘", "⠸", "⢸", "⠁", "⠉", "⠙", "⠹", "⢹", "⠃", "⠋", "⠛", "⠻", "⢻",
                "⠇", "⠏", "⠟", "⠿", "⢿", "⡇", "⡏", "⡟", "⡿", "⣿")
METER = "■"


class Canvas:
    """A width×height grid of cells: (char, fg, bg, bold)."""

    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = max(0, w), max(0, h)
        self.ch = [[" "] * self.w for _ in range(self.h)]
        self.fg: list[list[str | None]] = [[None] * self.w for _ in range(self.h)]
        self.bg: list[list[str | None]] = [[None] * self.w for _ in range(self.h)]
        self.bold = [[False] * self.w for _ in range(self.h)]
        self.clickables: list[tuple[int, int, int, str]] = []   # (y, x0, x1, key)

    def put(self, x: int, y: int, s: str, fg: str | None = None, bold: bool = False,
            bg: str | None = None, maxw: int | None = None) -> int:
        if not 0 <= y < self.h:
            return x + len(s)
        if maxw is not None:
            s = s[:max(0, maxw)]
        for ch in s:
            if 0 <= x < self.w:
                self.ch[y][x] = ch
                self.fg[y][x] = fg
                self.bold[y][x] = bold
                if bg is not None:
                    self.bg[y][x] = bg
            x += 1
        return x

    def rich(self, x: int, y: int, text: Text, maxw: int | None = None) -> int:
        """Draw a Rich Text (styles resolved per character)."""
        plain = text.plain.replace("\n", " ")
        if maxw is not None:
            plain = plain[:max(0, maxw)]
        styles = [Style.parse(str(text.style)) if text.style else Style()] * len(plain)
        styles = list(styles)
        for span in text.spans:
            st = span.style if isinstance(span.style, Style) else Style.parse(str(span.style))
            for i in range(max(0, span.start), min(len(plain), span.end)):
                styles[i] = styles[i] + st
        for i, ch in enumerate(plain):
            st = styles[i]
            fg = st.color.get_truecolor().hex if st.color else None
            if st.dim and fg:
                fg = c("tree_fg")
            if st.reverse:
                self.put(x + i, y, ch, c("selected_fg"), True, c("selected_bg"))
            else:
                self.put(x + i, y, ch, fg, bool(st.bold))
        return x + len(plain)

    def fill_bg(self, x: int, y: int, w: int, bg: str, fg: str | None = None) -> None:
        if not 0 <= y < self.h:
            return
        for i in range(max(0, x), min(self.w, x + w)):
            self.bg[y][i] = bg
            if fg:
                self.fg[y][i] = fg

    def to_text(self) -> Text:
        out = Text(no_wrap=True, overflow="crop")
        for y in range(self.h):
            run, key = "", None
            for x in range(self.w):
                k = (self.fg[y][x], self.bg[y][x], self.bold[y][x])
                if k != key and run:
                    out.append(run, _style(key))
                    run = ""
                key = k
                run += self.ch[y][x]
            if run:
                out.append(run, _style(key))
            if y < self.h - 1:
                out.append("\n")
        return out


def _style(k) -> str:
    fg, bg, bold = k
    parts = []
    if bold:
        parts.append("bold")
    if fg:
        parts.append(fg)
    if bg:
        parts.append(f"on {bg}")
    return " ".join(parts)


# --- boxes ------------------------------------------------------------------------
Seg = list[tuple[str, str]]   # [(text, role)] role: hi | title | fg | inactive | graph | #rrggbb


def _role(role: str) -> str:
    return {"hi": c("hi_fg"), "title": c("title"), "fg": c("main_fg"), "inactive": c("inactive_fg"),
            "graph": c("graph_text")}.get(role, role)


def seg(*parts: str) -> Seg:
    """seg("f", "ilter") → hotkey letter highlighted, rest in title colour. Use '' to skip."""
    out: Seg = []
    for i, p in enumerate(parts):
        if p:
            out.append((p, "hi" if i % 2 == 0 else "title"))
    return out


def _seg_width(s: Seg) -> int:
    return sum(len(p[0]) for p in s) + 2


def _draw_seg(cv: Canvas, x: int, y: int, s: Seg, line: str, top: bool, key: str | None) -> int:
    left, right = ("┐", "┌") if top else ("┘", "└")
    x0 = x
    x = cv.put(x, y, left, line)
    for part in s:
        text, role = part[0], part[1]
        px = x
        x = cv.put(x, y, text, _role(role), bold=role in ("title", "hi"))
        if len(part) > 2 and part[2]:
            cv.clickables.append((y, px, x, part[2]))     # per-hotkey click target
    x = cv.put(x, y, right, line)
    if key:
        cv.clickables.append((y, x0, x, key))
    return x


def segments(cv: Canvas, x: int, y: int, w: int, line: str, top: bool,
             left: list | None = None, right: list | None = None, center: Seg | None = None,
             start: int = 2) -> None:
    """Place title-notch segments along a horizontal border line spanning x..x+w."""
    cx = x + start
    for s, key in left or []:
        if cx + _seg_width(s) < x + w - 2:
            cx = _draw_seg(cv, cx, y, s, line, top, key)
    rx = x + w - 2
    for s, key in right or []:
        sw = _seg_width(s)
        if rx - sw > cx:
            rx -= sw
            _draw_seg(cv, rx, y, s, line, top, key)
    if center:
        sw = _seg_width(center)
        mx = x + (w - sw) // 2
        if mx > cx and mx + sw < rx:
            _draw_seg(cv, mx, y, center, line, top, None)


def box(cv: Canvas, x: int, y: int, w: int, h: int, line: str, title: str = "", num: int = 0,
        top_left: list | None = None, top_right: list | None = None, center: Seg | None = None,
        bottom_left: list | None = None, bottom_right: list | None = None) -> None:
    """Square-cornered btop box with title notches. *_left/right are lists of (Seg, key|None)."""
    if w < 4 or h < 2:
        return
    cv.put(x, y, "┌" + "─" * (w - 2) + "┐", line)
    cv.put(x, y + h - 1, "└" + "─" * (w - 2) + "┘", line)
    for yy in range(y + 1, y + h - 1):
        cv.put(x, yy, "│" + " " * (w - 2) + "│", line)
    head = []
    if title:
        parts: Seg = ([(SUPERSCRIPT[num], "hi")] if num else []) + [(title, "title")]
        head = [(parts, str(num) if num else None)]
    segments(cv, x, y, w, line, True, left=head + (top_left or []), right=top_right, center=center)
    segments(cv, x, y + h - 1, w, line, False, left=bottom_left, right=bottom_right)


def hdivider(cv: Canvas, x: int, y: int, w: int, line: str) -> None:
    cv.put(x, y, "├" + "─" * (w - 2) + "┤", line)


def vdivider(cv: Canvas, x: int, y: int, h: int, line: str) -> None:
    cv.put(x, y, "┬", line)
    for yy in range(y + 1, y + h - 1):
        cv.put(x, yy, "│", line)
    cv.put(x, y + h - 1, "┴", line)


# --- graphs and meters ----------------------------------------------------------
def graph(cv: Canvas, x: int, y: int, w: int, h: int, data: list[float], grad: str,
          max_value: float = 100.0, invert: bool = False, no_zero: bool = False) -> None:
    """btop's braille graph: two samples per character column, 4 dots per row, filled,
    coloured per row from the gradient (or per column for one-row graphs)."""
    if w <= 0 or h <= 0:
        return
    g = gradient(grad)
    vals = [max(0.0, min(100.0, v * 100 / max_value)) if max_value else 0.0 for v in data[-w * 2:]]
    if len(vals) % 2:
        vals = [0.0] + vals
    pairs = [(vals[i], vals[i + 1]) for i in range(0, len(vals), 2)]
    offset = w - len(pairs)
    table = BRAILLE_DOWN if invert else BRAILLE_UP
    mod = 0.3 if h == 1 else 0.1
    for col, (a, b) in enumerate(pairs):
        for horizon in range(h):
            hi = round(100 * (h - horizon) / h) if h > 1 else 100
            lo = round(100 * (h - horizon - 1) / h) if h > 1 else 0
            res = []
            for v in (a, b):
                cmin = 1 if (no_zero and horizon == h - 1) else 0
                if v >= hi:
                    res.append(4)
                elif v <= lo:
                    res.append(cmin)
                else:
                    res.append(max(cmin, min(4, round((v - lo) * 4 / (hi - lo) + mod))))
            sym = table[res[0] * 5 + res[1]]
            row = (h - 1 - horizon) if invert else horizon
            if h == 1:
                color = g[int(max(a, b))] if sym != " " else None
            else:
                i = row + 1
                color = g[min(100, i * 100 // h)] if invert else g[100 - (i - 1) * 100 // h]
            cv.put(x + offset + col, y + row, sym, color)


def meter(cv: Canvas, x: int, y: int, w: int, value: float, grad: str) -> None:
    g = gradient(grad)
    value = max(0.0, min(100.0, value))
    for i in range(1, w + 1):
        step = round(i * 100 / w)
        if value >= step:
            cv.put(x + i - 1, y, METER, g[step])
        else:
            cv.put(x + i - 1, y, METER * (w + 1 - i), c("meter_bg"))
            break


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024 or unit == "PiB":
            return f"{n:.0f} {unit}" if unit == "B" or n >= 100 else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def fmt_bits(bps: float) -> str:
    for unit, div in (("Gbps", 1e9), ("Mbps", 1e6), ("Kbps", 1e3)):
        if bps >= div:
            v = bps / div
            return f"{v:.1f} {unit}" if v < 100 else f"{v:.0f} {unit}"
    return f"{bps:.0f} bps"
