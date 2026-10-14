"""Generic rendering: sparkline metric panels and an annotated tree renderer."""
from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual.widget import Widget

from .models import MetricPanel

SPARK = "▁▂▃▄▅▆▇█"
ROW_COLORS = ["deep_sky_blue1", "medium_purple1", "turquoise2", "spring_green2", "sky_blue1",
              "light_slate_blue", "aquamarine1", "steel_blue1", "medium_orchid", "dark_sea_green2"]
LEVEL_STYLE = {"ERROR": "bold red", "WARN": "yellow", "INFO": "green", "DEBUG": "dim"}
SOURCE_STYLE = {"fw": "orange1", "pve": "deep_sky_blue1", "task": "medium_purple1", "you": "bold magenta"}


def level_text(level: str) -> Text:
    return Text(level.ljust(5), style=LEVEL_STYLE.get(level, ""))


def pct_style(v: float | None, warn: float = 75, crit: float = 90) -> str:
    if v is None:
        return "dim"
    return "bold red" if v >= crit else ("yellow" if v >= warn else "green")


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
            return Text("no data", style="dim")
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
            color = ROW_COLORS[i % len(ROW_COLORS)]
            faded = bool(self.highlight) and name not in self.highlight
            mod = " dim" if faded else (" bold" if self.highlight else "")
            out.append(name[:name_w].ljust(name_w), style=color + mod)
            out.append(" ")
            out.append(sparkline(pts, spark_w, hi), style=color + (" dim" if faded else ""))
            out.append(" ")
            latest = pts[-1] if pts else 0.0
            vstyle = ""
            if self.panel.crit is not None and latest >= self.panel.crit:
                vstyle = "bold red"
            elif self.panel.warn is not None and latest >= self.panel.warn:
                vstyle = "yellow"
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
        line = Text(prefix, style="dim")
        if parent is not None:
            on_path = node.eid in path and parent.eid in path
            line.append("└─ " if last else "├─ ", style="bold magenta" if on_path else "dim")
        label = node.label.copy()
        if node.eid and node.eid == selected:
            label.stylize("reverse")
        elif node.eid in path:
            label.stylize("bold")
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
