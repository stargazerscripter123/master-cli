"""Color themes and curses color-pair manager for btop + nvtop TUI."""

from __future__ import annotations
import curses
from dataclasses import dataclass
from typing import Dict, List


@dataclass
class ThemeSpec:
    name: str
    label: str
    # 256-color codes (with 8-color fallbacks handled automatically)
    border: int
    title: int
    header_bg: int
    text: int
    dim: int
    low: int        # 0-45% gradient
    mid: int        # 45-75% gradient
    high: int       # 75-100% gradient
    gpu_main: int   # nvtop GPU utilization accent
    gpu_mem: int    # nvtop GPU memory accent
    gpu_pwr: int    # GPU power / clocks accent
    mem_main: int   # RAM accent
    disk_main: int  # Disk accent
    net_rx: int     # Download accent
    net_tx: int     # Upload accent
    proc_sel_fg: int
    proc_sel_bg: int


THEMES: List[ThemeSpec] = [
    ThemeSpec(
        name="default",
        label="btop+nvtop Default",
        border=240,
        title=81,
        header_bg=235,
        text=253,
        dim=244,
        low=48,
        mid=220,
        high=196,
        gpu_main=45,
        gpu_mem=171,
        gpu_pwr=214,
        mem_main=114,
        disk_main=141,
        net_rx=49,
        net_tx=204,
        proc_sel_fg=16,
        proc_sel_bg=81,
    ),
    ThemeSpec(
        name="nvidia",
        label="NVIDIA Green (nvtop)",
        border=239,
        title=118,
        header_bg=234,
        text=254,
        dim=243,
        low=118,
        mid=226,
        high=196,
        gpu_main=118,
        gpu_mem=51,
        gpu_pwr=220,
        mem_main=82,
        disk_main=154,
        net_rx=46,
        net_tx=208,
        proc_sel_fg=16,
        proc_sel_bg=118,
    ),
    ThemeSpec(
        name="apple",
        label="Apple Silicon Cyber",
        border=241,
        title=117,
        header_bg=235,
        text=255,
        dim=245,
        low=51,
        mid=147,
        high=204,
        gpu_main=39,
        gpu_mem=213,
        gpu_pwr=221,
        mem_main=87,
        disk_main=141,
        net_rx=45,
        net_tx=198,
        proc_sel_fg=16,
        proc_sel_bg=117,
    ),
    ThemeSpec(
        name="dracula",
        label="Dracula",
        border=61,
        title=141,
        header_bg=236,
        text=255,
        dim=103,
        low=84,
        mid=228,
        high=203,
        gpu_main=117,
        gpu_mem=212,
        gpu_pwr=215,
        mem_main=141,
        disk_main=117,
        net_rx=84,
        net_tx=212,
        proc_sel_fg=16,
        proc_sel_bg=141,
    ),
    ThemeSpec(
        name="nord",
        label="Nord",
        border=60,
        title=110,
        header_bg=236,
        text=254,
        dim=244,
        low=108,
        mid=222,
        high=174,
        gpu_main=110,
        gpu_mem=139,
        gpu_pwr=216,
        mem_main=109,
        disk_main=144,
        net_rx=108,
        net_tx=174,
        proc_sel_fg=16,
        proc_sel_bg=110,
    ),
    ThemeSpec(
        name="monokai",
        label="Monokai Pro",
        border=240,
        title=208,
        header_bg=235,
        text=254,
        dim=243,
        low=148,
        mid=221,
        high=197,
        gpu_main=81,
        gpu_mem=141,
        gpu_pwr=208,
        mem_main=148,
        disk_main=221,
        net_rx=81,
        net_tx=197,
        proc_sel_fg=16,
        proc_sel_bg=208,
    ),
    ThemeSpec(
        name="tokyo-night",
        label="Tokyo Night",
        border=60,
        title=111,
        header_bg=235,
        text=254,
        dim=103,
        low=115,
        mid=221,
        high=204,
        gpu_main=75,
        gpu_mem=177,
        gpu_pwr=215,
        mem_main=114,
        disk_main=147,
        net_rx=73,
        net_tx=204,
        proc_sel_fg=16,
        proc_sel_bg=111,
    ),
    ThemeSpec(
        name="catppuccin",
        label="Catppuccin Mocha",
        border=60,
        title=183,
        header_bg=235,
        text=255,
        dim=103,
        low=151,
        mid=223,
        high=210,
        gpu_main=117,
        gpu_mem=183,
        gpu_pwr=216,
        mem_main=151,
        disk_main=182,
        net_rx=116,
        net_tx=210,
        proc_sel_fg=16,
        proc_sel_bg=183,
    ),
]


class ThemeManager:
    """Manages active curses color pairs and gradient color lookups."""

    PAIR_BORDER = 1
    PAIR_TITLE = 2
    PAIR_TEXT = 3
    PAIR_DIM = 4
    PAIR_LOW = 5
    PAIR_MID = 6
    PAIR_HIGH = 7
    PAIR_GPU_MAIN = 8
    PAIR_GPU_MEM = 9
    PAIR_GPU_PWR = 10
    PAIR_MEM = 11
    PAIR_DISK = 12
    PAIR_NET_RX = 13
    PAIR_NET_TX = 14
    PAIR_PROC_SEL = 15
    PAIR_HEADER = 16
    PAIR_BADGE = 17
    PAIR_ALERT_BADGE = 18
    PAIR_HEADER_DIM = 19
    PAIR_HEADER_TEXT = 20

    def __init__(self, initial_theme: str = "default") -> None:
        self.idx = 0
        for i, t in enumerate(THEMES):
            if t.name == initial_theme.lower():
                self.idx = i
                break
        self.has_256 = False

    @property
    def current(self) -> ThemeSpec:
        return THEMES[self.idx]

    def cycle(self) -> ThemeSpec:
        self.idx = (self.idx + 1) % len(THEMES)
        self.apply()
        return self.current

    def apply(self) -> None:
        try:
            curses.start_color()
            curses.use_default_colors()
            self.has_256 = getattr(curses, "COLORS", 8) >= 256
        except Exception:
            return

        t = self.current
        if self.has_256:
            pairs = [
                (self.PAIR_BORDER, t.border, -1),
                (self.PAIR_TITLE, t.title, -1),
                (self.PAIR_TEXT, t.text, -1),
                (self.PAIR_DIM, t.dim, -1),
                (self.PAIR_LOW, t.low, -1),
                (self.PAIR_MID, t.mid, -1),
                (self.PAIR_HIGH, t.high, -1),
                (self.PAIR_GPU_MAIN, t.gpu_main, -1),
                (self.PAIR_GPU_MEM, t.gpu_mem, -1),
                (self.PAIR_GPU_PWR, t.gpu_pwr, -1),
                (self.PAIR_MEM, t.mem_main, -1),
                (self.PAIR_DISK, t.disk_main, -1),
                (self.PAIR_NET_RX, t.net_rx, -1),
                (self.PAIR_NET_TX, t.net_tx, -1),
                (self.PAIR_PROC_SEL, t.proc_sel_fg, t.proc_sel_bg),
                (self.PAIR_HEADER, t.title, t.header_bg),
                (self.PAIR_BADGE, t.proc_sel_fg, t.title),
                (self.PAIR_ALERT_BADGE, 255, t.high),
                (self.PAIR_HEADER_DIM, t.dim, t.header_bg),
                (self.PAIR_HEADER_TEXT, t.text, t.header_bg),
            ]
        else:
            pairs = [
                (self.PAIR_BORDER, curses.COLOR_BLUE, -1),
                (self.PAIR_TITLE, curses.COLOR_CYAN, -1),
                (self.PAIR_TEXT, curses.COLOR_WHITE, -1),
                (self.PAIR_DIM, curses.COLOR_WHITE, -1),
                (self.PAIR_LOW, curses.COLOR_GREEN, -1),
                (self.PAIR_MID, curses.COLOR_YELLOW, -1),
                (self.PAIR_HIGH, curses.COLOR_RED, -1),
                (self.PAIR_GPU_MAIN, curses.COLOR_CYAN, -1),
                (self.PAIR_GPU_MEM, curses.COLOR_MAGENTA, -1),
                (self.PAIR_GPU_PWR, curses.COLOR_YELLOW, -1),
                (self.PAIR_MEM, curses.COLOR_GREEN, -1),
                (self.PAIR_DISK, curses.COLOR_MAGENTA, -1),
                (self.PAIR_NET_RX, curses.COLOR_GREEN, -1),
                (self.PAIR_NET_TX, curses.COLOR_RED, -1),
                (self.PAIR_PROC_SEL, curses.COLOR_BLACK, curses.COLOR_CYAN),
                (self.PAIR_HEADER, curses.COLOR_CYAN, curses.COLOR_BLACK),
                (self.PAIR_BADGE, curses.COLOR_BLACK, curses.COLOR_CYAN),
                (self.PAIR_ALERT_BADGE, curses.COLOR_WHITE, curses.COLOR_RED),
                (self.PAIR_HEADER_DIM, curses.COLOR_WHITE, curses.COLOR_BLACK),
                (self.PAIR_HEADER_TEXT, curses.COLOR_WHITE, curses.COLOR_BLACK),
            ]

        for pid, fg, bg in pairs:
            try:
                curses.init_pair(pid, fg, bg)
            except Exception:
                pass

    def attr(
        self,
        pair_id: int,
        bold: bool = False,
        dim: bool = False,
        underline: bool = False,
        reverse: bool = False,
    ) -> int:
        try:
            a = curses.color_pair(pair_id)
            if bold:
                a |= curses.A_BOLD
            if dim:
                a |= curses.A_DIM
            if underline:
                a |= getattr(curses, "A_UNDERLINE", 0)
            if reverse:
                a |= getattr(curses, "A_REVERSE", 0)
            return a
        except Exception:
            return 0

    def pct_attr(self, pct: float, bold: bool = False) -> int:
        if pct >= 80.0:
            return self.attr(self.PAIR_HIGH, bold=bold)
        if pct >= 45.0:
            return self.attr(self.PAIR_MID, bold=bold)
        return self.attr(self.PAIR_LOW, bold=bold)
