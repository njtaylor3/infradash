"""Generic rendering: sparkline metric panels and an annotated tree renderer."""
from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual.widget import Widget

from .btop.theme import row_colors, st
from .models import MetricPanel

SPARK = "▁▂▃▄▅▆▇█"


class _Roles(dict):
    """Looks keys up through the active theme at call time, so the theme can be set late."""

    def __init__(self, roles: dict[str, str]) -> None:
        super().__init__(roles)

    def get(self, key, default=None):
        return st(dict.get(self, key)) if key in self else default

    def __getitem__(self, key):
        return st(dict.__getitem__(self, key))


LEVEL_STYLE = _Roles({"ERROR": "crit", "WARN": "warn", "INFO": "ok", "DEBUG": "dim"})
SOURCE_STYLE = _Roles({"fw": "src_fw", "pve": "src_pve", "task": "src_task", "you": "src_you"})


def level_text(level: str) -> Text:
    return Text(level.ljust(5), style=LEVEL_STYLE.get(level, ""))


def pct_style(v: float | None, warn: float = 75, crit: float = 90) -> str:
    if v is None:
        return st("dim")
    return st("crit") if v >= crit else (st("warn") if v >= warn else st("ok"))


def fmt_value(v: float, unit: str) -> str:
    if unit == "%":
        return f"{v:.0f}%"
    if unit == "ms":
        return f"{v:.1f}ms" if v < 10 else f"{v:.0f}ms"
    if unit == "Mb/s":
        return f"{v:.2f}" if v < 1 else (f"{v:.1f}" if v < 100 else f"{v:.0f}")
    return f"{v:.3g}"


def sparkline(values: list[float], width: int, hi: float | None = None) -> str:
    pts = values[-width:]
    if not pts:
        return " " * width
    top = hi or max(pts) or 1.0
    return "".join(SPARK[min(7, max(0, int(v / top * 7.999)))] for v in pts).rjust(width)


class MetricPanelView(Widget):
    MAX_ROWS = 8

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.panel: MetricPanel | None = None
        self.highlight: set[str] = set()

    def update_panel(self, panel: MetricPanel | None, highlight: set[str]) -> None:
        self.panel, self.highlight = panel, highlight
        if panel:
            self.border_title = f"{panel.title} ({panel.unit})"
            self.styles.height = max(3, min(len(panel.series), self.MAX_ROWS) + 2)
        self.refresh()

    def render(self) -> Text:
        if not self.panel or not self.panel.series:
            return Text("no data", style=st("dim"))
        rows = list(self.panel.series.items())[: self.MAX_ROWS]
        width = max(20, self.content_size.width)
        name_w = min(14, max(len(n) for n, _ in rows))
        val_w = 6
        spark_w = max(4, width - name_w - val_w - 2)
        hi = 100.0 if self.panel.unit == "%" else None
        out = Text()
        for i, (name, pts) in enumerate(rows):
            if i:
                out.append("\n")
            rows = row_colors()
            color = rows[i % len(rows)]
            faded = bool(self.highlight) and name not in self.highlight
            mod = " dim" if faded else (" bold" if self.highlight else "")
            out.append(name[:name_w].ljust(name_w), style=color + mod)
            out.append(" ")
            out.append(sparkline(pts, spark_w, hi), style=color + (" dim" if faded else ""))
            out.append(" ")
            latest = pts[-1] if pts else 0.0
            vstyle = ""
            if self.panel.crit is not None and latest >= self.panel.crit:
                vstyle = st("crit")
            elif self.panel.warn is not None and latest >= self.panel.warn:
                vstyle = st("warn")
            out.append(fmt_value(latest, self.panel.unit).rjust(val_w), style=vstyle)
        return out


@dataclass
class TNode:
    eid: str | None
    label: Text
    children: list["TNode"] = field(default_factory=list)


def render_tree(roots: list[TNode], selected: str | None, path: set[str], legend: Text) -> Text:
    out = Text()

    def walk(node: TNode, prefix: str, last: bool, parent: TNode | None) -> None:
        line = Text(prefix, style=st("dim"))
        if parent is not None:
            on_path = node.eid in path and parent.eid in path
            line.append("└─ " if last else "├─ ", style=st("path") if on_path else st("dim"))
        label = node.label.copy()
        if node.eid and node.eid == selected:
            label.stylize("reverse")
        elif node.eid in path:
            label.stylize(st("strong"))
        line.append_text(label)
        out.append_text(line)
        out.append("\n")
        child_prefix = prefix if parent is None else prefix + ("   " if last else "│  ")
        for i, c in enumerate(node.children):
            walk(c, child_prefix, i == len(node.children) - 1, node)

    for r in roots:
        walk(r, "", True, None)
    out.append("\n")
    out.append_text(legend)
    return out
