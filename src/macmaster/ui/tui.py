"""Interactive btop + nvtop Unified Terminal UI (curses) — v2.0."""

from __future__ import annotations
import curses
from dataclasses import replace
import datetime
import json
import os
import signal
import time
from typing import Dict, List, Optional, Tuple

from ..collectors.base import UnifiedCollector
from ..models import (
    GpuDeviceStats,
    ProcessInfo,
    SystemSnapshot,
)
from .braille import (
    fmt_bytes,
    fmt_duration,
    fmt_freq,
    fmt_speed,
    render_bar,
    render_bar_parts,
    render_braille_graph,
    render_braille_sparkline,
)
from .themes import ThemeManager


SORT_COLUMNS = ("cpu%", "gpu%", "mem%", "vram", "time+", "pid", "name", "threads", "user")

PRESET_NAMES = (
    "Full 5-Panel",
    "AI/GPU Workstation",
    "System Classic",
    "Compact CPU+GPU+PROC",
)


def fmt_time_plus(sec: float) -> str:
    """Format process elapsed runtime (TIME+) concisely."""
    if sec <= 0:
        return "   0.0s"
    if sec < 60:
        return f"{sec:6.1f}s"
    if sec < 3600:
        m = int(sec) // 60
        s = int(sec) % 60
        return f"{m:3d}m{s:02d}s"
    if sec < 86400:
        h = int(sec) // 3600
        m = (int(sec) % 3600) // 60
        return f"{h:3d}h{m:02d}m"
    d = int(sec) // 86400
    h = (int(sec) % 86400) // 3600
    return f"{d:3d}d{h:02d}h"


class MacMasterTUI:
    """Full-screen interactive btop + nvtop terminal dashboard."""

    def __init__(
        self,
        collector: UnifiedCollector,
        interval_sec: float = 1.0,
        gpu_only_mode: bool = False,
        theme_name: str = "default",
    ) -> None:
        self.collector = collector
        self.interval_sec = max(0.1, min(10.0, interval_sec))
        self.theme = ThemeManager(theme_name)

        # Panel visibility (1: CPU, 2: GPU, 3: MEM/DISK, 4: NET, 5: PROC)
        self.show_cpu = True
        self.show_gpu = True
        self.show_mem = True
        self.show_net = True
        self.show_proc = True
        self.preset_idx = 0
        if gpu_only_mode:
            self.show_cpu = False
            self.show_mem = False
            self.show_net = False
            self.gpu_proc_only = True
            self.preset_idx = 1
        else:
            self.gpu_proc_only = False

        self.zoomed_panel: Optional[int] = None  # 1..5 when maximized

        # GPU & Network selectors
        self.selected_gpu_idx = 0  # -1 = show all GPUs stacked, >=0 = specific GPU
        self.selected_net_iface: Optional[str] = None

        # Process table state
        self.sort_idx = 0  # default: "cpu%"
        self.sort_reverse = True
        self.tree_mode = False
        self.group_mode = False
        self.use_smart_cmd = True
        self.frozen = False
        self.filter_text = ""
        self.filter_editing = False
        self.selected_proc_row = 0
        self.selected_pid: Optional[int] = None
        self.proc_scroll_offset = 0
        self.inspect_proc = False
        self._inspect_cache: Tuple[int, float, str, str] = (0, 0.0, "", "")

        # Mouse hit-test tracking
        self._proc_header_y: int = -1
        self._proc_list_start_y: int = -1
        self._proc_list_end_y: int = -1
        self._proc_col_spans: List[Tuple[int, int, str]] = []
        self._last_mouse_click_ts: float = 0.0

        # Modals & notifications
        self.show_help = False
        self.show_sensors = False
        self.kill_prompt_pid: Optional[int] = None
        self.kill_prompt_name: str = ""
        self.status_msg: str = ""
        self.status_msg_until: float = 0.0

        self.last_snap: Optional[SystemSnapshot] = None
        self.last_collect_ts: float = 0.0
        self._VisibleProcs: List[ProcessInfo] = []

        if not gpu_only_mode and theme_name == "default" and interval_sec == 1.0:
            self._load_config()

    def _config_path(self) -> str:
        return os.path.expanduser("~/.config/macmaster/config.json")

    def _load_config(self) -> None:
        try:
            p = self._config_path()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                if "theme" in cfg:
                    self.theme = ThemeManager(str(cfg["theme"]))
                if "interval_sec" in cfg:
                    self.interval_sec = max(0.1, min(10.0, float(cfg["interval_sec"])))
                if "sort_col" in cfg and cfg["sort_col"] in SORT_COLUMNS:
                    self.sort_idx = SORT_COLUMNS.index(cfg["sort_col"])
                if "use_smart_cmd" in cfg:
                    self.use_smart_cmd = bool(cfg["use_smart_cmd"])
                if "preset_idx" in cfg:
                    self._apply_preset(int(cfg["preset_idx"]), notify=False)
        except Exception:
            pass

    def _save_config(self) -> None:
        try:
            p = self._config_path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            cfg = {
                "theme": self.theme.current.name,
                "interval_sec": self.interval_sec,
                "sort_col": SORT_COLUMNS[self.sort_idx],
                "use_smart_cmd": self.use_smart_cmd,
                "preset_idx": self.preset_idx,
            }
            with open(p, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass

    def _apply_preset(self, idx: int, notify: bool = True) -> None:
        self.preset_idx = idx % len(PRESET_NAMES)
        self.zoomed_panel = None
        if self.preset_idx == 0:  # Full 5-Panel
            self.show_cpu = self.show_gpu = self.show_mem = self.show_net = self.show_proc = True
            self.gpu_proc_only = False
        elif self.preset_idx == 1:  # AI/GPU Workstation
            self.show_cpu = self.show_gpu = self.show_proc = True
            self.show_mem = self.show_net = False
            self.gpu_proc_only = True
        elif self.preset_idx == 2:  # System Classic (btop)
            self.show_cpu = self.show_mem = self.show_net = self.show_proc = True
            self.show_gpu = False
            self.gpu_proc_only = False
        elif self.preset_idx == 3:  # Compact CPU+GPU+PROC
            self.show_cpu = self.show_gpu = self.show_proc = True
            self.show_mem = self.show_net = False
            self.gpu_proc_only = False
        if notify:
            self._save_config()
            self._notify(f"Layout Preset: {PRESET_NAMES[self.preset_idx]}")

    def run(self) -> None:
        curses.wrapper(self._main_loop)

    def _notify(self, msg: str, duration: float = 3.0) -> None:
        self.status_msg = msg
        self.status_msg_until = time.time() + duration

    def _safe_addstr(self, win, y: int, x: int, text: str, attr: int = 0, max_x: Optional[int] = None) -> None:
        h, w = win.getmaxyx()
        limit_w = min(w, max_x) if max_x is not None else w
        if y < 0 or y >= h or x >= limit_w or not text:
            return
        if x < 0:
            text = text[-x:]
            x = 0
        max_len = limit_w - x
        if max_len <= 0:
            return
        clipped = text[:max_len]
        try:
            win.addstr(y, x, clipped, attr)
        except curses.error:
            pass

    def _draw_bar(
        self,
        win,
        y: int,
        x: int,
        pct: float,
        width: int,
        fill_attr: Optional[int] = None,
        style: str = "block",
        gradient: bool = False,
        max_x: Optional[int] = None,
    ) -> None:
        """Draws a progress bar color-coded uniformly across the entire bar with a dim empty track."""
        if width <= 0:
            return
        filled_s, empty_s = render_bar_parts(pct, width, style=style)
        f_len = len(filled_s)
        if f_len > 0:
            attr = self.theme.pct_attr(pct, bold=True) if (gradient or fill_attr is None) else fill_attr
            self._safe_addstr(win, y, x, filled_s, attr, max_x=max_x)
        if empty_s:
            self._safe_addstr(win, y, x + f_len, empty_s, self.theme.attr(ThemeManager.PAIR_DIM, dim=True), max_x=max_x)

    def _draw_box(
        self,
        win,
        y: int,
        x: int,
        h: int,
        w: int,
        title: str = "",
        right_title: str = "",
        footer: str = "",
        right_footer: str = "",
        border_pair: int = ThemeManager.PAIR_BORDER,
        title_pair: int = ThemeManager.PAIR_TITLE,
        split_x: Optional[int] = None,
    ) -> None:
        if h < 2 or w < 4:
            return
        b_attr = self.theme.attr(border_pair)
        t_attr = self.theme.attr(title_pair, bold=True)
        max_bx = x + w

        top_bar = "╭" + ("─" * (w - 2)) + "╮"
        bot_bar = "╰" + ("─" * (w - 2)) + "╯"
        self._safe_addstr(win, y, x, top_bar, b_attr)
        for row in range(y + 1, y + h - 1):
            self._safe_addstr(win, row, x, "│", b_attr)
            self._safe_addstr(win, row, x + w - 1, "│", b_attr)
        self._safe_addstr(win, y + h - 1, x, bot_bar, b_attr)

        has_split = split_x is not None and (x + 8 < split_x < x + w - 8)
        if has_split:
            assert split_x is not None
            self._safe_addstr(win, y, split_x, "┬", b_attr)
            for row in range(y + 1, y + h - 1):
                self._safe_addstr(win, row, split_x, "│", b_attr)
            self._safe_addstr(win, y + h - 1, split_x, "┴", b_attr)

        end_tx = x + 2
        if title and w > 8:
            max_t = (split_x - x - 6) if (has_split and split_x is not None) else (w - 6)
            if max_t >= 2:
                t_clip = title[:max_t]
                t_lim = split_x if (has_split and split_x is not None) else (max_bx - 1)
                if t_clip[0] in ("¹", "²", "³", "⁴", "⁵") and " " in t_clip:
                    badge_part, rest_part = t_clip.split(" ", 1)
                    self._safe_addstr(
                        win,
                        y,
                        x + 2,
                        f" {badge_part} ",
                        self.theme.attr(title_pair, bold=True, reverse=True),
                        max_x=t_lim,
                    )
                    self._safe_addstr(
                        win,
                        y,
                        x + 2 + len(badge_part) + 2,
                        f" {rest_part.strip()} ",
                        t_attr,
                        max_x=t_lim,
                    )
                    end_tx = x + 2 + len(badge_part) + 2 + len(rest_part.strip()) + 2
                else:
                    self._safe_addstr(win, y, x + 2, f" {t_clip} ", t_attr, max_x=t_lim)
                    end_tx = x + 2 + len(t_clip) + 2

        if right_title:
            min_rx = (split_x + 1) if (has_split and split_x is not None) else (end_tx + 2)
            avail_rt = (x + w - 2) - min_rx - 2
            if avail_rt >= 4:
                rt_clip = right_title[:avail_rt]
                rx = x + w - len(rt_clip) - 4
                self._safe_addstr(win, y, rx, f" {rt_clip} ", self.theme.attr(ThemeManager.PAIR_DIM), max_x=max_bx - 1)

        end_fx = x + 2
        if footer and w > 10:
            max_f = (split_x - x - 6) if (has_split and split_x is not None) else (w - 8)
            if max_f >= 2:
                f_clip = footer[:max_f]
                f_lim = split_x if (has_split and split_x is not None) else (max_bx - 1)
                self._safe_addstr(win, y + h - 1, x + 2, f" {f_clip} ", self.theme.attr(ThemeManager.PAIR_DIM), max_x=f_lim)
                end_fx = x + 2 + len(f_clip) + 2

        if right_footer:
            min_rfx = (split_x + 1) if (has_split and split_x is not None) else (end_fx + 2)
            avail_rf = (x + w - 2) - min_rfx - 2
            if avail_rf >= 4:
                rf_clip = right_footer[:avail_rf]
                rfx = x + w - len(rf_clip) - 4
                self._safe_addstr(win, y + h - 1, rfx, f" {rf_clip} ", self.theme.attr(title_pair, bold=True), max_x=max_bx - 1)

    def _main_loop(self, stdscr) -> None:
        try:
            curses.curs_set(0)
        except Exception:
            pass
        stdscr.nodelay(True)
        stdscr.keypad(True)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except Exception:
            pass
        self.theme.apply()

        self.last_snap = self.collector.collect()
        self.last_collect_ts = time.perf_counter()

        while True:
            now = time.perf_counter()
            if (not self.frozen and now - self.last_collect_ts >= self.interval_sec) or self.last_snap is None:
                self.last_snap = self.collector.collect()
                self.last_collect_ts = now

            stdscr.erase()
            self._render_frame(stdscr, self.last_snap)
            stdscr.refresh()

            # Wait up to 45ms for responsive keyboard/mouse input
            curses.napms(45)
            ch = stdscr.getch()
            if ch != -1:
                if not self._handle_input(ch):
                    break

    def _handle_input(self, ch: int) -> bool:
        # 1. If kill confirmation modal is active
        if self.kill_prompt_pid is not None:
            if ch == curses.KEY_MOUSE:
                try:
                    curses.getmouse()
                except Exception:
                    pass
                return True
            if ch == getattr(curses, "KEY_RESIZE", -2):
                return True
            if ch in (ord("y"), ord("Y"), 10, 13):
                self._send_signal(self.kill_prompt_pid, signal.SIGTERM)
                self.kill_prompt_pid = None
            elif ch in (ord("9"), ord("k"), ord("K")):
                self._send_signal(self.kill_prompt_pid, signal.SIGKILL)
                self.kill_prompt_pid = None
            elif ch in (ord("i"), ord("I"), ord("2")):
                self._send_signal(self.kill_prompt_pid, signal.SIGINT)
                self.kill_prompt_pid = None
            else:
                self.kill_prompt_pid = None
                self._notify("Cancelled signal.")
            return True

        # 2. If sensors modal is active
        if self.show_sensors:
            if ch == curses.KEY_MOUSE:
                try:
                    curses.getmouse()
                except Exception:
                    pass
                return True
            if ch in (ord("q"), ord("S"), 27, 10, 13, ord(" ")):
                self.show_sensors = False
            return True

        # 3. If help modal is active
        if self.show_help:
            if ch == curses.KEY_MOUSE:
                try:
                    curses.getmouse()
                except Exception:
                    pass
                return True
            if ch in (ord("q"), ord("h"), ord("?"), 27, 10, 13, ord(" ")):
                self.show_help = False
            return True

        # 4. If live process filter input is active
        if self.filter_editing:
            if ch == curses.KEY_MOUSE:
                try:
                    curses.getmouse()
                except Exception:
                    pass
                return True
            if ch == getattr(curses, "KEY_RESIZE", -2):
                return True
            if ch in (10, 13):
                self.filter_editing = False
            elif ch == 27:  # ESC clears filter and exits filter mode
                self.filter_text = ""
                self.filter_editing = False
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.filter_text = self.filter_text[:-1]
                self.selected_proc_row = 0
                self.selected_pid = None
            elif 32 <= ch <= 126:
                self.filter_text += chr(ch)
                self.selected_proc_row = 0
                self.selected_pid = None
            return True

        # 5. Standard navigation & shortcuts
        if ch in (ord("q"), ord("Q")):
            return False
        if ch == 27:  # ESC
            if self.zoomed_panel is not None:
                self.zoomed_panel = None
            elif self.inspect_proc:
                self.inspect_proc = False
            elif self.frozen:
                self.frozen = False
                self._notify("Resumed live telemetry.")
            elif self.filter_text:
                self.filter_text = ""
            else:
                return False
            return True

        if ch in (ord("?"), ord("h"), ord("H")):
            self.show_help = not self.show_help
        elif ch == ord("S"):
            self.show_sensors = not self.show_sensors
        elif ch == ord("1"):
            self.show_cpu = not self.show_cpu
            self.zoomed_panel = None
        elif ch == ord("2"):
            self.show_gpu = not self.show_gpu
            self.zoomed_panel = None
        elif ch == ord("3"):
            self.show_mem = not self.show_mem
            self.zoomed_panel = None
        elif ch == ord("4"):
            self.show_net = not self.show_net
            self.zoomed_panel = None
        elif ch == ord("5"):
            self.show_proc = not self.show_proc
            self.zoomed_panel = None
        elif ch in (ord("p"), ord("P")):
            self._apply_preset(self.preset_idx + 1)
        elif ch in (ord("z"), ord("Z"), ord("m"), ord("M")):
            cycle_order = [None, 2, 1, 5]
            try:
                idx = cycle_order.index(self.zoomed_panel)
                self.zoomed_panel = cycle_order[(idx + 1) % len(cycle_order)]
            except ValueError:
                self.zoomed_panel = None
        elif ch == ord("g"):
            self.gpu_proc_only = not self.gpu_proc_only
            self.selected_proc_row = 0
            self.selected_pid = None
            self.proc_scroll_offset = 0
            self._notify(f"GPU Processes Only (nvtop mode): {'ON' if self.gpu_proc_only else 'OFF'}")
        elif ch == ord("G"):
            self.group_mode = not self.group_mode
            if self.group_mode:
                self.tree_mode = False
            self.selected_proc_row = 0
            self.selected_pid = None
            self.proc_scroll_offset = 0
            self._notify(f"Process Grouping by Program [G]: {'ON' if self.group_mode else 'OFF'}")
        elif ch in (ord("t"), ord("T")):
            self.tree_mode = not self.tree_mode
            if self.tree_mode:
                self.group_mode = False
            self._notify(f"Process Tree View (with subtree aggregation): {'ON' if self.tree_mode else 'OFF'}")
        elif ch in (ord("e"), ord("E")):
            self.use_smart_cmd = not self.use_smart_cmd
            self._save_config()
            self._notify(f"Smart Command Line Condenser: {'ON' if self.use_smart_cmd else 'RAW'}")
        elif ch in (ord("f"), ord("F")):
            self.frozen = not self.frozen
            self._notify("❄ Telemetry FROZEN (press 'f' to resume)" if self.frozen else "▶ Telemetry RESUMED")
        elif ch == ord("/"):
            self.filter_editing = True
        elif ch in (ord("<"), ord(","), ord("[")):
            self.sort_idx = (self.sort_idx - 1) % len(SORT_COLUMNS)
            self._save_config()
            self._notify(f"Sort by: {SORT_COLUMNS[self.sort_idx]}")
        elif ch in (ord(">"), ord("."), ord("]"), ord("s")):
            self.sort_idx = (self.sort_idx + 1) % len(SORT_COLUMNS)
            self._save_config()
            self._notify(f"Sort by: {SORT_COLUMNS[self.sort_idx]}")
        elif ch in (ord("r"), ord("R")):
            self.sort_reverse = not self.sort_reverse
            self._notify(f"Sort order: {'DESC ▼' if self.sort_reverse else 'ASC ▲'}")
        elif ch in (ord("c"), ord("C")):
            t = self.theme.cycle()
            self._save_config()
            self._notify(f"Theme: {t.label}")
        elif ch in (ord("+"), ord("=")):
            self.interval_sec = max(0.1, round(self.interval_sec - 0.1, 1))
            self._save_config()
            self._notify(f"Refresh interval: {int(self.interval_sec * 1000)}ms")
        elif ch in (ord("-"), ord("_")):
            self.interval_sec = min(10.0, round(self.interval_sec + 0.1, 1))
            self._save_config()
            self._notify(f"Refresh interval: {int(self.interval_sec * 1000)}ms")
        elif ch in (ord("a"), ord("A")):
            if self.last_snap and len(self.last_snap.gpus) > 1:
                self.selected_gpu_idx = -1 if self.selected_gpu_idx >= 0 else 0
                self._notify("All-GPUs Stacked Overview" if self.selected_gpu_idx == -1 else f"GPU 0: {self.last_snap.gpus[0].name}")
        elif ch in (ord("d"), ord("D")):
            if self.last_snap and self.last_snap.gpus:
                n_gpus = len(self.last_snap.gpus)
                if n_gpus > 1:
                    # Cycle 0 -> 1 -> ... -> n-1 -> -1 (All GPUs) -> 0
                    if self.selected_gpu_idx == -1:
                        self.selected_gpu_idx = 0
                    elif self.selected_gpu_idx + 1 < n_gpus:
                        self.selected_gpu_idx += 1
                    else:
                        self.selected_gpu_idx = -1
                    if self.selected_gpu_idx == -1:
                        self._notify(f"All {n_gpus} GPUs Stacked Overview")
                    else:
                        self._notify(f"Selected GPU {self.selected_gpu_idx}: {self.last_snap.gpus[self.selected_gpu_idx].name}")
                else:
                    self.selected_gpu_idx = 0
        elif ch in (ord("n"), ord("N"), ord("b"), ord("B")):
            if self.last_snap and self.last_snap.net:
                ifaces = list(self.last_snap.net.keys())
                cur = self.selected_net_iface or self.last_snap.primary_net_iface
                idx = ifaces.index(cur) if cur in ifaces else 0
                step = 1 if ch in (ord("n"), ord("N")) else -1
                self.selected_net_iface = ifaces[(idx + step) % len(ifaces)]
                self._notify(f"Network Interface: {self.selected_net_iface}")
        elif ch in (curses.KEY_UP, ord("k")):
            self.selected_proc_row = max(0, self.selected_proc_row - 1)
            self._sync_selected_pid()
        elif ch in (curses.KEY_DOWN, ord("j")):
            if self._VisibleProcs:
                self.selected_proc_row = min(len(self._VisibleProcs) - 1, self.selected_proc_row + 1)
                self._sync_selected_pid()
        elif ch == curses.KEY_PPAGE:
            self.selected_proc_row = max(0, self.selected_proc_row - 10)
            self._sync_selected_pid()
        elif ch == curses.KEY_NPAGE:
            if self._VisibleProcs:
                self.selected_proc_row = min(len(self._VisibleProcs) - 1, self.selected_proc_row + 10)
                self._sync_selected_pid()
        elif ch == curses.KEY_HOME:
            self.selected_proc_row = 0
            self._sync_selected_pid()
        elif ch == curses.KEY_END:
            if self._VisibleProcs:
                self.selected_proc_row = len(self._VisibleProcs) - 1
                self._sync_selected_pid()
        elif ch in (10, 13, ord(" ")):
            self.inspect_proc = not self.inspect_proc
        elif ch in (ord("K"), ord("x"), ord("X")):
            if 0 <= self.selected_proc_row < len(self._VisibleProcs):
                target = self._VisibleProcs[self.selected_proc_row]
                self.kill_prompt_pid = target.pid
                grp_tag = f" [Group {target.group_count}× lead]" if target.group_count > 1 else ""
                self.kill_prompt_name = f"{target.name}{grp_tag}"
        elif ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
                btn_up = getattr(curses, "BUTTON4_PRESSED", 0)
                btn_dn = getattr(curses, "BUTTON5_PRESSED", 0)
                btn1_mask = (
                    getattr(curses, "BUTTON1_CLICKED", 0)
                    | getattr(curses, "BUTTON1_PRESSED", 0)
                    | getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
                )
                if bstate & btn_up:
                    self.selected_proc_row = max(0, self.selected_proc_row - 3)
                    self._sync_selected_pid()
                elif bstate & btn_dn:
                    if self._VisibleProcs:
                        self.selected_proc_row = min(len(self._VisibleProcs) - 1, self.selected_proc_row + 3)
                        self._sync_selected_pid()
                elif bstate & btn1_mask:
                    now_m = time.perf_counter()
                    if now_m - self._last_mouse_click_ts < 0.12:
                        return True
                    self._last_mouse_click_ts = now_m
                    if my == 0:
                        self.show_help = not self.show_help
                    elif my == self._proc_header_y:
                        matched_col = None
                        for x0, x1, col_action in self._proc_col_spans:
                            if x0 <= mx < x1:
                                matched_col = col_action
                                break
                        if matched_col == "toggle_cmd":
                            self.use_smart_cmd = not self.use_smart_cmd
                            self._save_config()
                            self._notify(f"Smart Command Line Condenser: {'ON' if self.use_smart_cmd else 'RAW'}")
                        elif matched_col in SORT_COLUMNS:
                            new_idx = SORT_COLUMNS.index(matched_col)
                            if self.sort_idx == new_idx:
                                self.sort_reverse = not self.sort_reverse
                            else:
                                self.sort_idx = new_idx
                                self.sort_reverse = True
                            self._save_config()
                            self._notify(f"Sort by: {SORT_COLUMNS[self.sort_idx]} ({'DESC ▼' if self.sort_reverse else 'ASC ▲'})")
                        else:
                            self.sort_idx = (self.sort_idx + 1) % len(SORT_COLUMNS)
                            self._save_config()
                            self._notify(f"Sort by: {SORT_COLUMNS[self.sort_idx]}")
                    elif self._proc_list_start_y <= my <= self._proc_list_end_y and self._VisibleProcs:
                        clicked_idx = self.proc_scroll_offset + (my - self._proc_list_start_y)
                        if 0 <= clicked_idx < len(self._VisibleProcs):
                            if self.selected_proc_row == clicked_idx:
                                self.inspect_proc = not self.inspect_proc
                            else:
                                self.selected_proc_row = clicked_idx
                                self._sync_selected_pid()
            except Exception:
                pass
        return True

    def _sync_selected_pid(self) -> None:
        if 0 <= self.selected_proc_row < len(self._VisibleProcs):
            self.selected_pid = self._VisibleProcs[self.selected_proc_row].pid

    def _send_signal(self, pid: int, sig: int) -> None:
        try:
            os.kill(pid, sig)
            sig_names = {signal.SIGKILL: "SIGKILL (9)", signal.SIGTERM: "SIGTERM (15)", signal.SIGINT: "SIGINT (2)"}
            sig_name = sig_names.get(sig, f"SIG {sig}")
            self._notify(f"Sent {sig_name} to PID {pid}")
        except Exception as e:
            self._notify(f"Failed to signal PID {pid}: {e}")

    def _render_frame(self, win, snap: SystemSnapshot) -> None:
        self._proc_header_y = -1
        self._proc_list_start_y = -1
        self._proc_list_end_y = -1
        self._proc_col_spans = []

        h, w = win.getmaxyx()
        if h < 12 or w < 50:
            self._safe_addstr(win, 0, 0, f"Terminal too small ({w}x{h}). Please resize to at least 80x24.", self.theme.attr(ThemeManager.PAIR_HIGH, bold=True))
            return

        # 1. Top Header Bar (row 0)
        self._render_top_bar(win, 0, 0, w, snap)

        avail_h = h - 1
        cur_y = 1

        # Handle zoomed single-panel mode
        if self.zoomed_panel == 1:
            self._render_cpu_panel(win, cur_y, 0, avail_h, w, snap)
            self._render_modals(win, h, w, snap)
            return
        if self.zoomed_panel == 2:
            self._render_gpu_panel(win, cur_y, 0, avail_h, w, snap)
            self._render_modals(win, h, w, snap)
            return
        if self.zoomed_panel == 5:
            self._render_proc_panel(win, cur_y, 0, avail_h, w, snap)
            self._render_modals(win, h, w, snap)
            return

        has_mid = self.show_mem or self.show_net
        active_rows = sum([bool(self.show_cpu), bool(self.show_gpu), bool(has_mid), bool(self.show_proc)])
        if active_rows == 0:
            self.show_cpu = self.show_gpu = self.show_proc = True
            active_rows = 3

        cpu_h = gpu_h = mid_h = proc_h = 0
        rem_h = avail_h

        if self.show_cpu:
            cpu_h = max(7 if avail_h >= 30 else 6, min(8, int(avail_h * (0.21 if active_rows >= 4 else 0.25))))
            cpu_h = min(cpu_h, rem_h)
            rem_h -= cpu_h
        if self.show_gpu:
            gpu_h = max(7, min(7, int(avail_h * (0.18 if active_rows >= 4 else 0.28))))
            gpu_h = min(gpu_h, rem_h)
            rem_h -= gpu_h
        if has_mid:
            mid_h = max(6, min(6, int(avail_h * (0.15 if self.show_proc else 0.35))))
            mid_h = min(mid_h, rem_h)
            rem_h -= mid_h
        if self.show_proc:
            proc_h = max(6, rem_h)
        elif rem_h > 0:
            if self.show_gpu:
                gpu_h += rem_h
            elif self.show_cpu:
                cpu_h += rem_h
            elif has_mid:
                mid_h += rem_h

        if self.show_cpu and cpu_h >= 5:
            self._render_cpu_panel(win, cur_y, 0, cpu_h, w, snap)
            cur_y += cpu_h

        if self.show_gpu and gpu_h >= 5:
            self._render_gpu_panel(win, cur_y, 0, gpu_h, w, snap)
            cur_y += gpu_h

        if has_mid and mid_h >= 5:
            if self.show_mem and self.show_net:
                mem_w = int(w * 0.62)
                net_w = w - mem_w
                self._render_mem_disk_panel(win, cur_y, 0, mid_h, mem_w, snap)
                self._render_net_panel(win, cur_y, mem_w, mid_h, net_w, snap)
            elif self.show_mem:
                self._render_mem_disk_panel(win, cur_y, 0, mid_h, w, snap)
            else:
                self._render_net_panel(win, cur_y, 0, mid_h, w, snap)
            cur_y += mid_h

        if self.show_proc and proc_h >= 5:
            self._render_proc_panel(win, cur_y, 0, proc_h, w, snap)

        self._render_modals(win, h, w, snap)

    def _render_top_bar(self, win, y: int, x: int, w: int, snap: SystemSnapshot) -> None:
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        up_str = fmt_duration(snap.cpu.uptime_sec)
        bat_str = f" · {snap.battery.percent:.0f}%({snap.battery.status})" if snap.battery.present else ""

        self._safe_addstr(win, y, x, " " * w, self.theme.attr(ThemeManager.PAIR_HEADER))
        brand = " ✦ MACMASTER "
        self._safe_addstr(win, y, x, brand, self.theme.attr(ThemeManager.PAIR_BADGE, bold=True))

        host_chip = f" {snap.hostname[:18]} ({snap.os_name} {snap.arch}) "
        self._safe_addstr(win, y, x + len(brand), host_chip, self.theme.attr(ThemeManager.PAIR_HEADER_TEXT, bold=True))

        meta_x = x + len(brand) + len(host_chip)
        meta_candidates = [
            f"│ Up {up_str}{bat_str}",
            f"│ Up {up_str}",
            "",
        ]
        preset_short = ("Full", "AI/GPU", "Classic", "Compact")[self.preset_idx % 4]
        tail_candidates = [
            f" [1-5]Panels  [p]{preset_short}  [S]Sensors  [c]{self.theme.current.name}  [?]Help  {now_str} ",
            f" [1-5]Panels [p]{preset_short} [S]Sens [c]{self.theme.current.name} [?]Help {now_str} ",
            f" [p]{preset_short} [S]Sens [c]{self.theme.current.name} [?]Help {now_str} ",
            f" [p]{preset_short} [S]Sens [?]Help {now_str} ",
            f" [?]Help {now_str} ",
            f" {now_str} ",
        ]

        chosen_meta = ""
        chosen_tail = ""
        for m_cand in meta_candidates:
            for t_cand in tail_candidates:
                if meta_x + len(m_cand) + len(t_cand) <= x + w:
                    chosen_meta = m_cand
                    chosen_tail = t_cand
                    break
            if chosen_tail:
                break

        right_start_x = (x + w - len(chosen_tail)) if chosen_tail else (x + w)
        if chosen_meta:
            self._safe_addstr(win, y, meta_x, chosen_meta, self.theme.attr(ThemeManager.PAIR_HEADER), max_x=right_start_x)
        if chosen_tail:
            self._safe_addstr(win, y, right_start_x, chosen_tail, self.theme.attr(ThemeManager.PAIR_HEADER_TEXT), max_x=x + w)

        free_start = meta_x + len(chosen_meta)
        free_w = right_start_x - free_start
        if self.frozen and free_w >= 12:
            fbadge = " [FROZEN — Press f to Resume] " if free_w >= 30 else " [FROZEN] "
            bx = right_start_x - len(fbadge)
            self._safe_addstr(win, y, bx, fbadge, self.theme.attr(ThemeManager.PAIR_ALERT_BADGE, bold=True), max_x=right_start_x)
        elif time.time() < self.status_msg_until and self.status_msg and free_w >= 12:
            badge = f" ★ {self.status_msg[: max(6, free_w - 5)]} "
            bx = right_start_x - len(badge)
            self._safe_addstr(win, y, bx, badge, self.theme.attr(ThemeManager.PAIR_BADGE, bold=True), max_x=right_start_x)
        elif snap.alerts and free_w >= 14:
            abadge = f" ⚠ {snap.alerts[0][: max(8, free_w - 5)]} "
            bx = right_start_x - len(abadge)
            self._safe_addstr(win, y, bx, abadge, self.theme.attr(ThemeManager.PAIR_ALERT_BADGE, bold=True), max_x=right_start_x)

    def _render_cpu_panel(self, win, y: int, x: int, h: int, w: int, snap: SystemSnapshot) -> None:
        cpu = snap.cpu
        inner_h = h - 2
        inner_w = w - 4
        left_w = max(26, int(inner_w * 0.46))
        right_w = inner_w - left_w - 3
        gx = x + 2
        gy = y + 1
        split_x = gx + left_w + 1 if right_w > 12 else None

        topo_str = f"{cpu.e_cores}E+{cpu.p_cores}P · {cpu.logical_cores}c" if cpu.e_cores > 0 else f"{cpu.logical_cores}c"
        title = f"¹cpu  {cpu.model} ({topo_str})"
        self._draw_box(
            win,
            y,
            x,
            h,
            w,
            title=title,
            split_x=split_x,
        )

        if inner_h <= 0 or inner_w <= 10:
            return

        max_lx = gx + left_w
        usr_s = f"usr {cpu.user_pct:.0f}% · sys {cpu.sys_pct:.0f}%" if left_w >= 44 else f"u:{cpu.user_pct:.0f}% s:{cpu.sys_pct:.0f}%"
        bar_w = max(4, left_w - 17 - len(usr_s))
        self._safe_addstr(win, gy, gx, "Total  :", self.theme.attr(ThemeManager.PAIR_TEXT, bold=True), max_x=max_lx)
        self._draw_bar(win, gy, gx + 9, cpu.total_pct, bar_w, gradient=True, max_x=max_lx)
        self._safe_addstr(
            win,
            gy,
            gx + 10 + bar_w,
            f"{cpu.total_pct:5.1f}%",
            self.theme.pct_attr(cpu.total_pct, bold=True),
            max_x=max_lx,
        )
        self._safe_addstr(
            win,
            gy,
            gx + 17 + bar_w,
            usr_s,
            self.theme.attr(ThemeManager.PAIR_DIM),
            max_x=max_lx,
        )

        # Structured vitals rows on Left: Clocks/Temp/Load + Power Rails (zero border duplication, zero clipping)
        graph_start_y = gy + 1
        l1, l5, l15 = cpu.load_avg
        freq_s = (
            f"E:{cpu.e_freq_mhz / 1000.0:.2f} P:{fmt_freq(cpu.p_freq_mhz)}"
            if cpu.e_cores > 0 and cpu.e_freq_mhz >= 1000
            else (f"E:{fmt_freq(cpu.e_freq_mhz)} P:{fmt_freq(cpu.p_freq_mhz)}" if cpu.e_cores > 0 else fmt_freq(cpu.freq_mhz))
        )
        if inner_h >= 4:
            avail_v_w = max(10, left_w - 9)
            t_part = f" · {cpu.temp_c:.1f}°C" if cpu.temp_c is not None else ""
            clk_cands = [
                f"{freq_s}{t_part} · Load {l1:.1f}/{l5:.1f}/{l15:.1f}",
                f"{freq_s}{t_part} · L:{l1:.1f}/{l5:.1f}/{l15:.1f}",
                f"{freq_s}{t_part} · L:{l1:.0f}/{l5:.0f}/{l15:.0f}",
                f"{freq_s}{t_part}",
            ]
            clk_line = next((c for c in clk_cands if len(c) <= avail_v_w), clk_cands[-1])
            self._safe_addstr(win, gy + 1, gx, "Clocks :", self.theme.attr(ThemeManager.PAIR_TEXT), max_x=max_lx)
            self._safe_addstr(win, gy + 1, gx + 9, clk_line, self.theme.attr(ThemeManager.PAIR_DIM), max_x=max_lx)

            pwr_parts = []
            if cpu.power_w is not None:
                pwr_parts.append(f"CPU {cpu.power_w:.1f}W")
            if cpu.dram_power_w is not None:
                pwr_parts.append(f"DRAM {cpu.dram_power_w:.1f}W")
            if cpu.sys_power_w is not None:
                pk_s = f"(Pk {cpu.peak_sys_power_w:.0f}W)" if cpu.peak_sys_power_w else ""
                pwr_parts.append(f"Sys {cpu.sys_power_w:.1f}W{pk_s}")
            if not self.show_gpu and cpu.fans_rpm:
                pwr_parts.append(f"Fan {cpu.fans_rpm[0][1]}RPM")
            if pwr_parts:
                pwr_cands = [" · ".join(pwr_parts)]
                if cpu.sys_power_w is not None:
                    c1 = [f"CPU {cpu.power_w or 0.0:.0f}W"]
                    if cpu.dram_power_w is not None:
                        c1.append(f"DRAM {cpu.dram_power_w:.1f}W")
                    c2 = list(c1)
                    c1.append(f"Sys {cpu.sys_power_w:.0f}W" + (f"({cpu.peak_sys_power_w:.0f}W)" if cpu.peak_sys_power_w else ""))
                    c2.append(f"Sys {cpu.sys_power_w:.0f}W")
                    pwr_cands.extend([" · ".join(c1), " · ".join(c2)])
                pwr_line = next((c for c in pwr_cands if len(c) <= avail_v_w), pwr_cands[-1][:avail_v_w])
                self._safe_addstr(win, gy + 2, gx, "Power  :", self.theme.attr(ThemeManager.PAIR_TEXT), max_x=max_lx)
                self._safe_addstr(win, gy + 2, gx + 9, pwr_line, self.theme.attr(ThemeManager.PAIR_GPU_PWR), max_x=max_lx)
                graph_start_y = gy + 3
            else:
                graph_start_y = gy + 2

        graph_h = max(1, (gy + inner_h) - graph_start_y)
        g_rows = render_braille_graph(
            list(self.collector.history.cpu_total),
            width=left_w,
            height=graph_h,
            min_val=0.0,
            max_val=100.0,
            baseline=True,
        )
        for r_idx, r_str in enumerate(g_rows):
            row_pct = 100.0 - (r_idx * 100.0 / max(1, graph_h))
            self._safe_addstr(
                win,
                graph_start_y + r_idx,
                gx,
                r_str,
                self.theme.pct_attr(max(cpu.total_pct * 0.85, row_pct * 0.65), bold=(r_idx == 0 and cpu.total_pct >= 50)),
                max_x=max_lx,
            )
        if graph_h >= 2:
            self._safe_addstr(win, graph_start_y, gx, "100%", self.theme.attr(ThemeManager.PAIR_DIM))
            self._safe_addstr(win, graph_start_y + graph_h - 1, gx, "  0%", self.theme.attr(ThemeManager.PAIR_DIM))

        if right_w > 12 and cpu.cores:
            cx = gx + left_w + 3
            max_bx = x + w - 1
            n_cores = len(cpu.cores)
            min_col_w = 11 if n_cores > 24 else 15
            n_cols = max(1, (n_cores + inner_h - 1) // max(1, inner_h))
            n_cols = min(n_cols, max(1, right_w // min_col_w))
            col_w = max(min_col_w, right_w // n_cols)
            rows_per_col = min(inner_h, max(1, (n_cores + n_cols - 1) // n_cols))

            for idx, c in enumerate(cpu.cores):
                col = idx // rows_per_col
                row = idx % rows_per_col
                if col >= n_cols or row >= inner_h:
                    continue
                cell_x = cx + col * col_w
                lbl = f"{c.label:<3}"
                lbl_pair = ThemeManager.PAIR_GPU_MAIN if c.label.startswith("E") else ThemeManager.PAIR_TITLE
                self._safe_addstr(win, gy + row, cell_x, lbl, self.theme.attr(lbl_pair, bold=True), max_x=max_bx)
                if col_w >= 15:
                    cbar_w = min(10, max(3, col_w - 11))
                    self._draw_bar(win, gy + row, cell_x + 4, c.usage_pct, cbar_w, style="block", gradient=True, max_x=max_bx)
                    self._safe_addstr(
                        win,
                        gy + row,
                        cell_x + 5 + cbar_w,
                        f"{c.usage_pct:3.0f}%",
                        self.theme.pct_attr(c.usage_pct, bold=(c.usage_pct >= 50.0)),
                        max_x=max_bx,
                    )
                else:
                    cbar_w = max(2, col_w - 8)
                    self._draw_bar(win, gy + row, cell_x + 3, c.usage_pct, cbar_w, style="block", gradient=True, max_x=max_bx)
                    self._safe_addstr(
                        win,
                        gy + row,
                        cell_x + 3 + cbar_w,
                        f"{c.usage_pct:3.0f}%",
                        self.theme.pct_attr(c.usage_pct, bold=(c.usage_pct >= 50.0)),
                        max_x=max_bx,
                    )

    def _render_gpu_panel(self, win, y: int, x: int, h: int, w: int, snap: SystemSnapshot) -> None:
        gpus = snap.gpus
        if not gpus:
            self._draw_box(win, y, x, h, w, title="²gpu  No Apple or NVIDIA GPU detected")
            self._safe_addstr(
                win,
                y + h // 2,
                x + 4,
                "No Apple Silicon or NVIDIA GPU detected (Run with --simulate-nvidia to preview NVIDIA nvtop UI).",
                self.theme.attr(ThemeManager.PAIR_DIM),
            )
            return

        # Multi-GPU Stacked Overview mode when selected_gpu_idx == -1 and len(gpus) > 1
        if self.selected_gpu_idx == -1 and len(gpus) > 1:
            total_pwr = sum(g.power_draw_w or 0.0 for g in gpus)
            avg_util = sum(g.gpu_util_pct for g in gpus) / len(gpus)
            title = f"²gpu  Multi-GPU Overview ({len(gpus)} GPUs · Avg {avg_util:.1f}% · Total {total_pwr:.1f}W)"
            self._draw_box(win, y, x, h, w, title=title, title_pair=ThemeManager.PAIR_GPU_MAIN)
            inner_h = h - 2
            inner_w = w - 4
            gx = x + 2
            gy = y + 1
            rows_per_gpu = max(1, inner_h // len(gpus))
            for idx, g in enumerate(gpus):
                ry = gy + idx * rows_per_gpu
                if ry >= gy + inner_h:
                    break
                u_spark = render_braille_sparkline(self.collector.history.gpu_util.get(g.index, [g.gpu_util_pct]), width=8)
                m_spark = render_braille_sparkline(self.collector.history.gpu_mem.get(g.index, [g.vram_pct]), width=8)
                thr = f" ⚠{g.throttle_reasons}" if g.throttle_reasons and g.throttle_reasons not in ("None", "Idle") else ""
                line1 = (
                    f"[{g.index}] {g.name[:22]:<22} │ Util [{u_spark}] {render_bar(g.gpu_util_pct, 12)} {g.gpu_util_pct:5.1f}% │ "
                    f"VRAM [{m_spark}] {render_bar(g.vram_pct, 12)} {g.vram_pct:4.1f}% ({fmt_bytes(g.vram_used_bytes)}/{fmt_bytes(g.vram_total_bytes)}) │ "
                    f"{g.gpu_clock_mhz}MHz [{g.pstate}] │ {g.power_draw_w or 0.0:5.1f}W │ {g.temp_c or 0.0:4.1f}°C{thr}"
                )
                self._safe_addstr(win, ry, gx, line1[:inner_w], self.theme.attr(ThemeManager.PAIR_GPU_MAIN, bold=True), max_x=x + w - 1)
                if rows_per_gpu >= 2 and ry + 1 < gy + inner_h:
                    procs_s = ", ".join(f"{p.process_name or p.pid}({p.gpu_util_pct:.0f}%)" for p in g.processes[:5]) or "Idle"
                    self._safe_addstr(win, ry + 1, gx + 4, f"Active Procs: {procs_s}"[: inner_w - 4], self.theme.attr(ThemeManager.PAIR_DIM), max_x=x + w - 1)
            return

        g_idx = min(max(0, self.selected_gpu_idx), len(gpus) - 1)
        gpu: GpuDeviceStats = gpus[g_idx]
        title = f"²gpu  [{gpu.index}] {gpu.name}"
        r_parts = []
        if gpu.api_version:
            r_parts.append(gpu.api_version)
        if len(gpus) > 1:
            r_parts.append(f"[{g_idx + 1}/{len(gpus)} d/a]")
        r_title = " · ".join(r_parts)

        inner_h = h - 2
        inner_w = w - 4
        gx = x + 2
        gy = y + 1
        left_w = max(22, int(inner_w * 0.34))
        right_w = inner_w - left_w - 3
        rx = gx + left_w + 3
        split_x = gx + left_w + 1 if right_w >= 16 else None

        self._draw_box(
            win,
            y,
            x,
            h,
            w,
            title=title,
            right_title=r_title,
            title_pair=ThemeManager.PAIR_GPU_MAIN,
            split_x=split_x,
        )

        if inner_h <= 0 or inner_w <= 15:
            return

        util_hist = list(self.collector.history.gpu_util.get(gpu.index, [gpu.gpu_util_pct]))
        mem_hist = list(self.collector.history.gpu_mem.get(gpu.index, [gpu.vram_pct]))

        if inner_h >= 4:
            top_h = max(1, inner_h // 2)
            bot_h = max(1, inner_h - top_h)
            top_rows = render_braille_graph(util_hist, width=left_w, height=top_h, min_val=0.0, max_val=100.0, invert=False, baseline=True)
            bot_rows = render_braille_graph(mem_hist, width=left_w, height=bot_h, min_val=0.0, max_val=100.0, invert=True, baseline=True)
            for i, r_str in enumerate(top_rows):
                self._safe_addstr(win, gy + i, gx, r_str, self.theme.attr(ThemeManager.PAIR_GPU_MAIN, bold=(i == 0)), max_x=gx + left_w)
            for i, r_str in enumerate(bot_rows):
                self._safe_addstr(win, gy + top_h + i, gx, r_str, self.theme.attr(ThemeManager.PAIR_GPU_MEM, bold=(i == bot_h - 1)), max_x=gx + left_w)
            self._safe_addstr(win, gy, gx + 1, f"▲ Core {gpu.gpu_util_pct:4.1f}% ", self.theme.attr(ThemeManager.PAIR_GPU_MAIN, bold=True), max_x=gx + left_w)
            self._safe_addstr(
                win,
                gy + inner_h - 1,
                gx + 1,
                f"▼ VRAM {gpu.vram_pct:4.1f}% ",
                self.theme.attr(ThemeManager.PAIR_GPU_MEM, bold=True),
                max_x=gx + left_w,
            )
        else:
            rows = render_braille_graph(util_hist, width=left_w, height=inner_h, min_val=0.0, max_val=100.0, baseline=True)
            for i, r_str in enumerate(rows):
                self._safe_addstr(win, gy + i, gx, r_str, self.theme.attr(ThemeManager.PAIR_GPU_MAIN, bold=True), max_x=gx + left_w)

        if right_w < 16:
            return
        max_bx = x + w - 1
        bar_w = max(4, min(12, right_w - 52))
        row_cursor = gy

        # Row 1: Core Utilization + Clock + P-State
        clk_s = f"Clock {gpu.gpu_clock_mhz}/{gpu.gpu_clock_max_mhz}MHz [{gpu.pstate or 'P0'}]" if right_w >= 54 else f"{gpu.gpu_clock_mhz}/{gpu.gpu_clock_max_mhz}MHz [{gpu.pstate or 'P0'}]"
        u_attr = self.theme.pct_attr(gpu.gpu_util_pct, bold=True)
        self._safe_addstr(win, row_cursor, rx, "Core Util :", self.theme.attr(ThemeManager.PAIR_TEXT, bold=True), max_x=max_bx)
        self._draw_bar(win, row_cursor, rx + 12, gpu.gpu_util_pct, bar_w, fill_attr=u_attr, max_x=max_bx)
        self._safe_addstr(win, row_cursor, rx + 13 + bar_w, f"{gpu.gpu_util_pct:5.1f}%  {clk_s}", u_attr, max_x=max_bx)
        row_cursor += 1

        # Row 2: VRAM / Unified GPU Memory
        if row_cursor < gy + inner_h:
            lbl = "Uni VRAM  :" if gpu.unified_memory else "GPU VRAM  :"
            v_attr = self.theme.attr(ThemeManager.PAIR_HIGH, bold=True) if gpu.vram_pct >= 80.0 else self.theme.attr(ThemeManager.PAIR_GPU_MEM, bold=True)
            self._safe_addstr(win, row_cursor, rx, lbl, self.theme.attr(ThemeManager.PAIR_TEXT, bold=True), max_x=max_bx)
            self._draw_bar(win, row_cursor, rx + 12, gpu.vram_pct, bar_w, fill_attr=v_attr, max_x=max_bx)
            vram_extra = f" · Alloc {fmt_bytes(gpu.vram_alloc_bytes)}" if (gpu.vendor == "Apple" and gpu.vram_alloc_bytes > 0) else (f" · MemClk {gpu.mem_clock_mhz}MHz" if gpu.mem_clock_mhz > 0 else "")
            vram_txt = f"{gpu.vram_pct:5.1f}%  {fmt_bytes(gpu.vram_used_bytes)} / {fmt_bytes(gpu.vram_total_bytes)}{vram_extra}"
            if 13 + bar_w + len(vram_txt) > right_w:
                vram_short_extra = f" · Alloc {fmt_bytes(gpu.vram_alloc_bytes, 0)}" if (gpu.vendor == "Apple" and gpu.vram_alloc_bytes > 0) else (f" · {gpu.mem_clock_mhz}MHz" if gpu.mem_clock_mhz > 0 else "")
                vram_txt = f"{gpu.vram_pct:5.1f}% {fmt_bytes(gpu.vram_used_bytes)}/{fmt_bytes(gpu.vram_total_bytes)}{vram_short_extra}"
            self._safe_addstr(
                win,
                row_cursor,
                rx + 13 + bar_w,
                vram_txt,
                v_attr,
                max_x=max_bx,
            )
            row_cursor += 1

        if gpu.vendor == "Apple":
            # Row 3: Power + ANE + Temp + Fan
            if row_cursor < gy + inner_h:
                p_pct = gpu.power_pct or min(100.0, ((gpu.power_draw_w or 0.0) / max(1.0, gpu.power_limit_w or 60.0)) * 100.0)
                p_attr = self.theme.attr(ThemeManager.PAIR_HIGH, bold=True) if p_pct >= 80.0 else self.theme.attr(ThemeManager.PAIR_GPU_PWR, bold=True)
                gp_w = gpu.power_draw_w or 0.0
                ane_w = gpu.ane_power_w or 0.0
                pk_s = f"(Pk {gpu.peak_power_w:.0f}W)" if (gpu.peak_power_w and gpu.peak_power_w >= 1.0) else ""
                t_s = f" · {gpu.temp_c:.1f}°C" if gpu.temp_c is not None else ""
                f_s = f" · {snap.cpu.fans_rpm[0][1]}RPM" if snap.cpu.fans_rpm else ""
                self._safe_addstr(win, row_cursor, rx, "Power/Thm :", self.theme.attr(ThemeManager.PAIR_TEXT), max_x=max_bx)
                self._draw_bar(win, row_cursor, rx + 12, p_pct, bar_w, fill_attr=p_attr, max_x=max_bx)
                pwr_txt = f"{gp_w:5.2f}W/{gpu.power_limit_w or 60.0:.0f}W{pk_s} · ANE {ane_w:.2f}W{t_s}{f_s}"
                if 13 + bar_w + len(pwr_txt) > right_w:
                    pwr_txt = f"{gp_w:.1f}W/{gpu.power_limit_w or 60.0:.0f}W · ANE {ane_w:.1f}W{t_s}"
                self._safe_addstr(
                    win,
                    row_cursor,
                    rx + 13 + bar_w,
                    pwr_txt,
                    p_attr,
                    max_x=max_bx,
                )
                row_cursor += 1

            # Row 4: Renderer + Tiler + P-State distribution
            if row_cursor < gy + inner_h:
                ren = gpu.renderer_util_pct or 0.0
                til = gpu.tiler_util_pct or 0.0
                prefix_e = f"Engines   : Renderer {ren:3.0f}% · Tiler {til:3.0f}% · P-States [" if right_w >= 58 else f"Engines   : Ren {ren:.0f}% · Til {til:.0f}% · ["
                max_dist_w = max(4, right_w - len(prefix_e) - 1)
                dist_items: List[str] = []
                for k, v in list(gpu.pstate_dist.items())[:4]:
                    item = f"{k}:{v:.0f}%"
                    cand = " ".join(dist_items + [item])
                    if len(cand) <= max_dist_w:
                        dist_items.append(item)
                    else:
                        break
                dist_str = " ".join(dist_items) if dist_items else (gpu.pstate or "P0")[:max_dist_w]
                self._safe_addstr(
                    win,
                    row_cursor,
                    rx,
                    f"{prefix_e}{dist_str}]",
                    self.theme.attr(ThemeManager.PAIR_TEXT),
                    max_x=max_bx,
                )
                row_cursor += 1

            # Row 5: Metal Pipeline Memory + Active Clients
            if row_cursor < gy + inner_h:
                m_pipe_full = f"Metal Pipe: PB {fmt_bytes(gpu.pb_alloc_bytes)} · Tiled {fmt_bytes(gpu.tiled_scene_bytes)} · Active Clients: {len(gpu.processes)}"
                m_pipe_short = f"Metal Pipe: PB {fmt_bytes(gpu.pb_alloc_bytes, 0)} · Tiled {fmt_bytes(gpu.tiled_scene_bytes, 0)} · Clients: {len(gpu.processes)}"
                self._safe_addstr(
                    win,
                    row_cursor,
                    rx,
                    m_pipe_full if len(m_pipe_full) <= right_w else m_pipe_short,
                    self.theme.attr(ThemeManager.PAIR_DIM),
                    max_x=max_bx,
                )
                row_cursor += 1
        else:
            # Row 3: Power + Temp + Fan + Throttle
            if row_cursor < gy + inner_h:
                p_pct = gpu.power_pct or 0.0
                p_attr = self.theme.pct_attr(p_pct, bold=True)
                thr_lbl = f" · ⚠{gpu.throttle_reasons}" if gpu.throttle_reasons and gpu.throttle_reasons not in ("None", "Idle") else ""
                t_s = f" · {gpu.temp_c:.0f}°C" if gpu.temp_c is not None else ""
                f_s = f" · Fan {gpu.fan_speed_pct:.0f}%" if gpu.fan_speed_pct is not None else ""
                self._safe_addstr(win, row_cursor, rx, "Power/Thm :", self.theme.attr(ThemeManager.PAIR_TEXT), max_x=max_bx)
                self._draw_bar(win, row_cursor, rx + 12, p_pct, bar_w, fill_attr=p_attr, max_x=max_bx)
                nv_pwr = f"{gpu.power_draw_w or 0.0:5.1f}W/{gpu.power_limit_w or 0.0:.0f}W{t_s}{f_s}{thr_lbl}"
                if 13 + bar_w + len(nv_pwr) > right_w:
                    nv_pwr = f"{gpu.power_draw_w or 0.0:.0f}W/{gpu.power_limit_w or 0.0:.0f}W{t_s}{f_s}"
                self._safe_addstr(
                    win,
                    row_cursor,
                    rx + 13 + bar_w,
                    nv_pwr,
                    p_attr,
                    max_x=max_bx,
                )
                row_cursor += 1

            # Row 4: NV Engines (SM / MemCtrl / ENC / DEC / Mode / Driver)
            if row_cursor < gy + inner_h:
                enc = gpu.enc_util_pct or 0.0
                dec = gpu.dec_util_pct or 0.0
                mc = gpu.mem_ctrl_util_pct or 0.0
                nv_eng_full = f"NV Engines: MemCtrl {mc:3.0f}% · ENC {enc:3.0f}% · DEC {dec:3.0f}% · {gpu.compute_mode} · Drv {gpu.driver_version}"
                nv_eng_short = f"NV Engines: MC {mc:.0f}% · ENC {enc:.0f}% · DEC {dec:.0f}% · Drv {gpu.driver_version}"
                self._safe_addstr(
                    win,
                    row_cursor,
                    rx,
                    nv_eng_full if len(nv_eng_full) <= right_w else nv_eng_short,
                    self.theme.attr(ThemeManager.PAIR_TEXT),
                    max_x=max_bx,
                )
                row_cursor += 1

            # Row 5: PCIe Gen/Width/Throughput + Active Clients
            if row_cursor < gy + inner_h:
                pcie_s = f"PCIe Gen{gpu.pcie_gen or '?'}/{gpu.pcie_gen_max or '?'} x{gpu.pcie_width or '?'}/{gpu.pcie_width_max or '?'}"
                rx_s = fmt_speed((gpu.pcie_rx_kbps or 0.0) * 1024)
                tx_s = fmt_speed((gpu.pcie_tx_kbps or 0.0) * 1024)
                bus_full = f"Bus/Link  : {pcie_s} · RX {rx_s} · TX {tx_s} · Clients: {len(gpu.processes)}"
                bus_short = f"Bus/Link  : Gen{gpu.pcie_gen or '?'}/{gpu.pcie_gen_max or '?'} x{gpu.pcie_width or '?'} · RX {rx_s} · TX {tx_s} · C:{len(gpu.processes)}"
                self._safe_addstr(
                    win,
                    row_cursor,
                    rx,
                    bus_full if len(bus_full) <= right_w else bus_short,
                    self.theme.attr(ThemeManager.PAIR_DIM),
                    max_x=max_bx,
                )
                row_cursor += 1

    def _render_mem_disk_panel(self, win, y: int, x: int, h: int, w: int, snap: SystemSnapshot) -> None:
        mem = snap.mem
        dsk = snap.disk
        inner_h = h - 2
        inner_w = w - 4
        gx = x + 2
        gy = y + 1
        left_w = max(24, int(inner_w * 0.54))
        right_w = inner_w - left_w - 3
        rx = gx + left_w + 3
        split_x = gx + left_w + 1 if right_w >= 14 else None

        title = f"³mem  Memory ({fmt_bytes(mem.total_bytes)})"
        r_title = "disks  Storage & I/O"

        self._draw_box(
            win,
            y,
            x,
            h,
            w,
            title=title,
            right_title=r_title,
            title_pair=ThemeManager.PAIR_MEM,
            split_x=split_x,
        )

        if inner_h <= 0 or inner_w <= 16:
            return

        bar_w = max(4, min(8, left_w - 33))
        max_lx = gx + left_w
        p_pair = ThemeManager.PAIR_LOW if mem.pressure_level == "Normal" else (ThemeManager.PAIR_MID if mem.pressure_level == "Warn" else ThemeManager.PAIR_HIGH)

        # Clean 4-row tabular Memory layout (zero border duplication)
        w_pct = round(mem.wired_bytes * 100.0 / max(1, mem.total_bytes), 1) if mem.wired_bytes > 0 else mem.swap_pct
        w_lbl = "Wired" if mem.wired_bytes > 0 else "Swap "
        w_bytes = mem.wired_bytes if mem.wired_bytes > 0 else mem.swap_used_bytes
        w_extra = f"Cmp {fmt_bytes(mem.compressed_bytes, 0)}" if mem.compressed_bytes > 0 else f"Swp {fmt_bytes(mem.swap_used_bytes, 0)}"

        mem_rows = [
            ("Used ", mem.used_pct, mem.used_bytes, self.theme.pct_attr(mem.used_pct, bold=True), f"● {mem.pressure_level}", self.theme.attr(p_pair, bold=True)),
            ("Avail", mem.available_pct, mem.available_bytes, self.theme.attr(ThemeManager.PAIR_LOW, bold=True), f"Free {fmt_bytes(mem.free_bytes, 0)}", self.theme.attr(ThemeManager.PAIR_DIM)),
            ("Cache", mem.cached_pct, mem.cached_bytes, self.theme.attr(ThemeManager.PAIR_GPU_MAIN, bold=True), f"App  {fmt_bytes(mem.app_bytes, 0)}", self.theme.attr(ThemeManager.PAIR_DIM)),
            (w_lbl, w_pct, w_bytes, self.theme.attr(ThemeManager.PAIR_GPU_PWR, bold=True), w_extra, self.theme.attr(ThemeManager.PAIR_DIM)),
        ]

        for i, (lbl, pct, bval, attr, extra_txt, extra_attr) in enumerate(mem_rows):
            ry = gy + i
            if ry >= gy + inner_h:
                break
            self._safe_addstr(win, ry, gx, f"{lbl}:", self.theme.attr(ThemeManager.PAIR_TEXT, bold=True), max_x=max_lx)
            val_s = f"{pct:4.1f}% {fmt_bytes(bval):>7}"
            if 8 + bar_w + len(val_s) + 1 + len(extra_txt) <= left_w:
                self._draw_bar(win, ry, gx + 7, pct, bar_w, fill_attr=attr, max_x=max_lx)
                self._safe_addstr(win, ry, gx + 8 + bar_w, val_s, attr, max_x=max_lx)
                self._safe_addstr(win, ry, gx + 8 + bar_w + len(val_s) + 1, extra_txt, extra_attr, max_x=max_lx)
            else:
                val_short = f"{pct:4.1f}% {fmt_bytes(bval, 0):>5}"
                self._safe_addstr(win, ry, gx + 6, val_short, attr, max_x=max_lx)
                if 6 + len(val_short) + 1 + len(extra_txt) <= left_w:
                    self._safe_addstr(win, ry, gx + 6 + len(val_short) + 1, extra_txt, extra_attr, max_x=max_lx)

        if right_w < 14:
            return
        max_bx = x + w - 1
        d_row = gy
        # Row 1: Read Speed + IOPS
        r_cands = [
            f"Read  ▲ {fmt_speed(dsk.read_bps):>9} ({dsk.read_iops:.0f} iops)",
            f"Read  ▲ {fmt_speed(dsk.read_bps):>9} ({dsk.read_iops:.0f})",
            f"R ▲ {fmt_speed(dsk.read_bps):>9} ({dsk.read_iops:.0f})",
            f"R ▲ {fmt_speed(dsk.read_bps):>10}",
        ]
        self._safe_addstr(
            win,
            d_row,
            rx,
            next((c for c in r_cands if len(c) <= right_w), r_cands[-1]),
            self.theme.attr(ThemeManager.PAIR_DISK, bold=True),
            max_x=max_bx,
        )
        d_row += 1
        # Row 2: Write Speed + IOPS
        if d_row < gy + inner_h:
            w_cands = [
                f"Write ▼ {fmt_speed(dsk.write_bps):>9} ({dsk.write_iops:.0f} iops)",
                f"Write ▼ {fmt_speed(dsk.write_bps):>9} ({dsk.write_iops:.0f})",
                f"W ▼ {fmt_speed(dsk.write_bps):>9} ({dsk.write_iops:.0f})",
                f"W ▼ {fmt_speed(dsk.write_bps):>10}",
            ]
            self._safe_addstr(
                win,
                d_row,
                rx,
                next((c for c in w_cands if len(c) <= right_w), w_cands[-1]),
                self.theme.attr(ThemeManager.PAIR_DISK, bold=True),
                max_x=max_bx,
            )
            d_row += 1

        # Row 3+: Disk Mounts
        dbar_w = max(4, min(6, right_w - 25))
        for m in dsk.mounts[: max(1, inner_h - 3)]:
            if d_row >= gy + inner_h:
                break
            mp = m.mount_point if len(m.mount_point) <= 3 else ("…" + m.mount_point[-2:])
            m_attr = self.theme.pct_attr(m.used_pct, bold=True)
            m_val_s = f"{m.used_pct:4.1f}% {fmt_bytes(m.used_bytes, 0)}/{fmt_bytes(m.total_bytes, 1)}"
            if 6 + dbar_w + len(m_val_s) <= right_w:
                self._safe_addstr(win, d_row, rx, f"{mp:<3}:", self.theme.attr(ThemeManager.PAIR_TEXT, bold=True), max_x=max_bx)
                self._draw_bar(win, d_row, rx + 5, m.used_pct, dbar_w, fill_attr=m_attr, max_x=max_bx)
                self._safe_addstr(win, d_row, rx + 6 + dbar_w, m_val_s, m_attr, max_x=max_bx)
            else:
                m_short = f"{mp[:2]:<2}: {m.used_pct:4.1f}% {fmt_bytes(m.used_bytes, 0)}/{fmt_bytes(m.total_bytes, 0)}"
                self._safe_addstr(win, d_row, rx, m_short, m_attr, max_x=max_bx)
            d_row += 1

        # Row 4: Cumulative Read / Write Totals
        if d_row < gy + inner_h:
            tot_full = f"Total : R {fmt_bytes(dsk.read_total_bytes)} · W {fmt_bytes(dsk.write_total_bytes)}"
            tot_short = f"Tot R:{fmt_bytes(dsk.read_total_bytes, 0)} W:{fmt_bytes(dsk.write_total_bytes, 0)}"
            self._safe_addstr(
                win,
                d_row,
                rx,
                tot_full if len(tot_full) <= right_w else tot_short,
                self.theme.attr(ThemeManager.PAIR_DIM),
                max_x=max_bx,
            )

    def _render_net_panel(self, win, y: int, x: int, h: int, w: int, snap: SystemSnapshot) -> None:
        if not snap.net:
            self._draw_box(win, y, x, h, w, title="⁴net  No active interfaces")
            return

        iface = self.selected_net_iface if (self.selected_net_iface in snap.net) else snap.primary_net_iface
        nstat = snap.net.get(iface) or next(iter(snap.net.values()))
        self.selected_net_iface = nstat.name

        inner_h = h - 2
        inner_w = w - 4
        gx = x + 2
        gy = y + 1

        ip_str = nstat.ipv4 or nstat.ipv6 or "No IP"
        title = f"⁴net  {nstat.name} ({ip_str})"
        r_title = "[b/n] switch" if w >= 42 else ""
        self._draw_box(
            win,
            y,
            x,
            h,
            w,
            title=title,
            right_title=r_title,
            title_pair=ThemeManager.PAIR_NET_RX,
        )

        if inner_h <= 0 or inner_w <= 12:
            return

        max_bx = x + w - 1
        rem_net_w = max(0, inner_w - 19)
        rx_cands = [
            f"Pk {fmt_speed(nstat.rx_peak_bps, 0)} · Tot {fmt_bytes(nstat.rx_total_bytes)}",
            f"Pk {fmt_speed(nstat.rx_peak_bps, 0)} · Tot {fmt_bytes(nstat.rx_total_bytes, 0)}",
            f"Pk {fmt_speed(nstat.rx_peak_bps, 0)} Tot {fmt_bytes(nstat.rx_total_bytes, 0)}",
            f"Tot {fmt_bytes(nstat.rx_total_bytes)}",
            f"Tot {fmt_bytes(nstat.rx_total_bytes, 0)}",
            "",
        ]
        rx_tail = next((c for c in rx_cands if len(c) <= rem_net_w), "")
        # Row 1: Download speed + Peak + Total
        self._safe_addstr(
            win,
            gy,
            gx,
            f"▼ Down {fmt_speed(nstat.rx_bps):>11}",
            self.theme.attr(ThemeManager.PAIR_NET_RX, bold=True),
            max_x=max_bx,
        )
        if rx_tail:
            self._safe_addstr(
                win,
                gy,
                gx + 19,
                rx_tail,
                self.theme.attr(ThemeManager.PAIR_DIM),
                max_x=max_bx,
            )

        # Row 2: Upload speed + Peak + Total
        if inner_h >= 2:
            tx_cands = [
                f"Pk {fmt_speed(nstat.tx_peak_bps, 0)} · Tot {fmt_bytes(nstat.tx_total_bytes)}",
                f"Pk {fmt_speed(nstat.tx_peak_bps, 0)} · Tot {fmt_bytes(nstat.tx_total_bytes, 0)}",
                f"Pk {fmt_speed(nstat.tx_peak_bps, 0)} Tot {fmt_bytes(nstat.tx_total_bytes, 0)}",
                f"Tot {fmt_bytes(nstat.tx_total_bytes)}",
                f"Tot {fmt_bytes(nstat.tx_total_bytes, 0)}",
                "",
            ]
            tx_tail = next((c for c in tx_cands if len(c) <= rem_net_w), "")
            self._safe_addstr(
                win,
                gy + 1,
                gx,
                f"▲ Up   {fmt_speed(nstat.tx_bps):>11}",
                self.theme.attr(ThemeManager.PAIR_NET_TX, bold=True),
                max_x=max_bx,
            )
            if tx_tail:
                self._safe_addstr(
                    win,
                    gy + 1,
                    gx + 19,
                    tx_tail,
                    self.theme.attr(ThemeManager.PAIR_DIM),
                    max_x=max_bx,
                )

        # Rows 3..N: Full-width RX / TX Braille throughput graph
        graph_y = gy + 2
        graph_h = inner_h - 2
        if graph_h >= 1:
            rx_hist = list(self.collector.history.net_rx_bps.get(nstat.name, [nstat.rx_bps]))
            tx_hist = list(self.collector.history.net_tx_bps.get(nstat.name, [nstat.tx_bps]))
            max_rx = max(10240.0, max(rx_hist or [10240.0]))
            max_tx = max(10240.0, max(tx_hist or [10240.0]))
            if graph_h >= 2:
                top_h = max(1, graph_h // 2)
                bot_h = max(1, graph_h - top_h)
                rx_rows = render_braille_graph(rx_hist, width=inner_w, height=top_h, min_val=0.0, max_val=max_rx, invert=False, baseline=True)
                tx_rows = render_braille_graph(tx_hist, width=inner_w, height=bot_h, min_val=0.0, max_val=max_tx, invert=True, baseline=True)
                for i, r_str in enumerate(rx_rows):
                    self._safe_addstr(win, graph_y + i, gx, r_str, self.theme.attr(ThemeManager.PAIR_NET_RX, bold=(i == 0)), max_x=max_bx)
                for i, r_str in enumerate(tx_rows):
                    self._safe_addstr(win, graph_y + top_h + i, gx, r_str, self.theme.attr(ThemeManager.PAIR_NET_TX, bold=(i == bot_h - 1)), max_x=max_bx)
            else:
                rx_rows = render_braille_graph(rx_hist, width=inner_w, height=1, min_val=0.0, max_val=max_rx, baseline=True)
                if rx_rows:
                    self._safe_addstr(win, graph_y, gx, rx_rows[0], self.theme.attr(ThemeManager.PAIR_NET_RX, bold=True), max_x=max_bx)

    def _prepare_process_list(self, snap: SystemSnapshot) -> List[ProcessInfo]:
        procs = list(snap.processes)
        if self.gpu_proc_only:
            procs = [p for p in procs if p.gpu_index is not None or p.gpu_pct > 0.0 or p.vram_bytes > 0]

        if self.filter_text:
            q = self.filter_text.lower()
            procs = [
                p
                for p in procs
                if q in p.name.lower()
                or q in p.cmdline.lower()
                or q in p.smart_cmd.lower()
                or q in p.user.lower()
                or q in str(p.pid)
            ]

        # Optional Program Grouping mode (bottom --group style)
        if self.group_mode:
            grouped: Dict[str, ProcessInfo] = {}
            lead_scores: Dict[str, Tuple[float, float, int]] = {}
            for p in procs:
                key = p.name
                score = (p.gpu_pct, p.cpu_pct, p.mem_bytes)
                if key not in grouped:
                    lead_scores[key] = score
                    grouped[key] = ProcessInfo(
                        pid=p.pid,
                        ppid=p.ppid,
                        name=p.name,
                        cmdline=p.cmdline,
                        smart_cmd=p.smart_cmd,
                        user=p.user,
                        state=p.state,
                        nice=p.nice,
                        threads=p.threads,
                        cpu_pct=p.cpu_pct,
                        mem_bytes=p.mem_bytes,
                        mem_pct=p.mem_pct,
                        gpu_index=p.gpu_index,
                        gpu_kind=p.gpu_kind,
                        gpu_pct=p.gpu_pct,
                        vram_bytes=p.vram_bytes,
                        vram_pct=p.vram_pct,
                        gpu_api=p.gpu_api,
                        io_read_bytes=p.io_read_bytes,
                        io_write_bytes=p.io_write_bytes,
                        elapsed_sec=p.elapsed_sec,
                        group_count=1,
                    )
                else:
                    g = grouped[key]
                    g.group_count += 1
                    g.threads += p.threads
                    g.cpu_pct = round(g.cpu_pct + p.cpu_pct, 1)
                    g.mem_bytes += p.mem_bytes
                    g.mem_pct = round(g.mem_pct + p.mem_pct, 1)
                    g.gpu_pct = round(g.gpu_pct + p.gpu_pct, 1)
                    g.vram_bytes += p.vram_bytes
                    g.vram_pct = round(g.vram_pct + p.vram_pct, 1)
                    g.io_read_bytes += p.io_read_bytes
                    g.io_write_bytes += p.io_write_bytes
                    g.elapsed_sec = max(g.elapsed_sec, p.elapsed_sec)
                    if score > lead_scores[key]:
                        lead_scores[key] = score
                        g.pid = p.pid
                        g.ppid = p.ppid
                        g.cmdline = p.cmdline
                        g.smart_cmd = p.smart_cmd
                        g.user = p.user
                        g.state = p.state
                        g.nice = p.nice
                        if p.gpu_index is not None:
                            g.gpu_index = p.gpu_index
                            g.gpu_kind = p.gpu_kind
                            g.gpu_api = p.gpu_api
                    elif g.gpu_index is None and p.gpu_index is not None:
                        g.gpu_index = p.gpu_index
                        g.gpu_kind = p.gpu_kind
                        g.gpu_api = p.gpu_api
            procs = list(grouped.values())
            for g in procs:
                if g.group_count > 1:
                    g.tree_prefix = f"[{g.group_count}×] "
                else:
                    g.tree_prefix = ""

        sort_col = SORT_COLUMNS[self.sort_idx]
        key_fn = {
            "cpu%": lambda p: (p.cpu_pct, p.gpu_pct, p.mem_bytes),
            "gpu%": lambda p: (p.gpu_pct, p.vram_bytes, p.cpu_pct),
            "mem%": lambda p: (p.mem_pct, p.mem_bytes, p.cpu_pct),
            "vram": lambda p: (p.vram_bytes, p.gpu_pct, p.mem_bytes),
            "time+": lambda p: (p.elapsed_sec, p.cpu_pct),
            "pid": lambda p: p.pid,
            "name": lambda p: p.name.lower(),
            "threads": lambda p: (p.threads, p.cpu_pct),
            "user": lambda p: (p.user.lower(), p.cpu_pct),
        }[sort_col]

        procs.sort(key=key_fn, reverse=self.sort_reverse)

        if not self.tree_mode or self.group_mode:
            if not self.group_mode:
                procs = [replace(p, tree_prefix="") if p.tree_prefix else p for p in procs]
            return procs

        # Build parent->children tree with subtree resource aggregation
        procs = [replace(p, tree_prefix="") for p in procs]
        by_pid = {p.pid: p for p in procs}
        children: Dict[int, List[ProcessInfo]] = {}
        roots: List[ProcessInfo] = []
        for p in procs:
            if p.ppid in by_pid and p.ppid != p.pid:
                children.setdefault(p.ppid, []).append(p)
            else:
                roots.append(p)

        subtree_totals: Dict[int, Tuple[float, float, int, int]] = {}
        visiting_agg = set()

        def _agg(pid: int) -> Tuple[float, float, int, int]:
            if pid in subtree_totals:
                return subtree_totals[pid]
            if pid in visiting_agg or pid not in by_pid:
                return (0.0, 0.0, 0, 0)
            visiting_agg.add(pid)
            node = by_pid[pid]
            c_tot = node.cpu_pct
            g_tot = node.gpu_pct
            m_tot = node.mem_bytes
            v_tot = node.vram_bytes
            for ch_node in children.get(pid, []):
                sc, sg, sm, sv = _agg(ch_node.pid)
                c_tot += sc
                g_tot += sg
                m_tot += sm
                v_tot += sv
            res = (round(c_tot, 1), round(g_tot, 1), m_tot, v_tot)
            subtree_totals[pid] = res
            return res

        for p in procs:
            _agg(p.pid)

        tree_key_fn = {
            "cpu%": lambda p: (subtree_totals.get(p.pid, (p.cpu_pct, 0.0, 0, 0))[0], p.cpu_pct),
            "gpu%": lambda p: (subtree_totals.get(p.pid, (0.0, p.gpu_pct, 0, 0))[1], p.gpu_pct),
            "mem%": lambda p: (subtree_totals.get(p.pid, (0.0, 0.0, p.mem_bytes, 0))[2], p.mem_bytes),
            "vram": lambda p: (subtree_totals.get(p.pid, (0.0, 0.0, 0, p.vram_bytes))[3], p.vram_bytes),
        }.get(sort_col, key_fn)

        roots.sort(key=tree_key_fn, reverse=self.sort_reverse)
        for ch_list in children.values():
            ch_list.sort(key=tree_key_fn, reverse=self.sort_reverse)

        ordered: List[ProcessInfo] = []
        visited = set()

        def _walk(node: ProcessInfo, prefix: str, is_last: bool, depth: int) -> None:
            if node.pid in visited:
                return
            visited.add(node.pid)
            if depth == 0:
                node.tree_prefix = ""
            else:
                node.tree_prefix = prefix + ("└─ " if is_last else "├─ ")
            ordered.append(node)
            kids = children.get(node.pid, [])
            next_prefix = prefix + ("   " if is_last else "│  ") if depth > 0 else ""
            for idx, k in enumerate(kids):
                _walk(k, next_prefix, idx == len(kids) - 1, depth + 1)

        for r in roots:
            _walk(r, "", True, 0)
        return ordered

    def _render_proc_panel(self, win, y: int, x: int, h: int, w: int, snap: SystemSnapshot) -> None:
        procs = self._prepare_process_list(snap)
        self._VisibleProcs = procs

        if self.selected_pid is not None:
            for idx, p in enumerate(procs):
                if p.pid == self.selected_pid:
                    self.selected_proc_row = idx
                    break
        if procs:
            self.selected_proc_row = max(0, min(len(procs) - 1, self.selected_proc_row))
            self.selected_pid = procs[self.selected_proc_row].pid
        else:
            self.selected_proc_row = 0
            self.selected_pid = None

        sort_col = SORT_COLUMNS[self.sort_idx]
        arrow = "▼" if self.sort_reverse else "▲"
        gpu_badge = " [g:GPU-ONLY]" if self.gpu_proc_only else " [g:All]"
        grp_badge = " [G:Group:ON]" if self.group_mode else (" [t:Tree:ON]" if self.tree_mode else " [t:Tree G:Grp]")
        flt_badge = f" │ [/]:'{self.filter_text}█'" if self.filter_editing else (f" │ [/]:'{self.filter_text}'" if self.filter_text else " │ [/]Filter")

        title = f"⁵proc (btop+nvtop){gpu_badge}{grp_badge}{flt_badge}"
        r_title = f"Sort[< >]: {sort_col}{arrow} │ {len(procs)}/{snap.total_processes} ({snap.gpu_process_count} GPU)"
        footer = "Keys: [g]GPU [G]Group [t]Tree [e]SmartCmd [/]Filter [Enter]Inspect [K]Signal [f]Freeze"
        r_footer = f"Row {self.selected_proc_row + 1 if procs else 0}/{len(procs)}"
        self._draw_box(win, y, x, h, w, title=title, right_title=r_title, footer=footer, right_footer=r_footer)

        inner_h = h - 2
        inner_w = w - 4
        if inner_h <= 1 or inner_w <= 20:
            return

        gx = x + 2
        gy = y + 1
        max_bx = x + w - 1

        # Optional Process Inspector Drawer at top of process box (when Enter/Space pressed)
        if self.inspect_proc and procs and inner_h >= 7:
            sel = procs[self.selected_proc_row]
            now_t = time.time()
            if self._inspect_cache[0] != sel.pid or now_t - self._inspect_cache[1] > 2.0:
                cwd, gpu_env = self.collector.inspect_process(sel.pid)
                self._inspect_cache = (sel.pid, now_t, cwd, gpu_env)
            else:
                cwd, gpu_env = self._inspect_cache[2], self._inspect_cache[3]

            p_cpu_hist = self.collector.history.proc_cpu.get(sel.pid, [sel.cpu_pct])
            p_gpu_hist = self.collector.history.proc_gpu.get(sel.pid, [sel.gpu_pct])
            c_spark = render_braille_sparkline(p_cpu_hist, width=8)
            g_spark = render_braille_sparkline(p_gpu_hist, width=8)
            gpu_info = f"GPU {sel.gpu_index} ({sel.gpu_api}:{sel.gpu_kind}) {sel.gpu_pct:.1f}% VRAM:{fmt_bytes(sel.vram_bytes)}" if sel.gpu_index is not None else "None"
            grp_hdr = f" [Group: {sel.group_count}× procs, lead PID {sel.pid}]" if sel.group_count > 1 else ""
            self._safe_addstr(
                win,
                gy,
                gx,
                f"🔍 INSPECT PID {sel.pid} ({sel.name}){grp_hdr} │ PPID:{sel.ppid} │ User:{sel.user} │ Up:{fmt_time_plus(sel.elapsed_sec).strip()} │ Thr:{sel.threads} │ [Enter]Close [K]Signal",
                self.theme.attr(ThemeManager.PAIR_TITLE, bold=True),
                max_x=max_bx,
            )
            self._safe_addstr(
                win,
                gy + 1,
                gx,
                f"   CPU: [{c_spark}] {sel.cpu_pct:5.1f}% │ RAM: {fmt_bytes(sel.mem_bytes)} ({sel.mem_pct:.1f}%) │ GPU: [{g_spark}] {gpu_info} │ Disk R/W: {fmt_bytes(sel.io_read_bytes)}/{fmt_bytes(sel.io_write_bytes)}",
                self.theme.attr(ThemeManager.PAIR_TEXT),
                max_x=max_bx,
            )
            cwd_env_line = f"   CWD: {cwd or 'N/A'}" + (f" │ Env: {gpu_env}" if gpu_env else "")
            self._safe_addstr(win, gy + 2, gx, cwd_env_line[:inner_w], self.theme.attr(ThemeManager.PAIR_GPU_MAIN), max_x=max_bx)
            self._safe_addstr(win, gy + 3, gx, f"   Cmd: {sel.cmdline[:inner_w - 8]}", self.theme.attr(ThemeManager.PAIR_DIM), max_x=max_bx)
            self._safe_addstr(win, gy + 4, gx, "─" * inner_w, self.theme.attr(ThemeManager.PAIR_BORDER), max_x=max_bx)
            gy += 5
            inner_h -= 5

        # Table Column Header
        # Fixed widths: PID(7) Program(18) Command(flex) GPU(6) Time+(7) User(9) MemB(8) Mem%(5) VRAM(8) GPU%(8) CPU%(8)
        fixed_w = 7 + 18 + 6 + 7 + 9 + 8 + 5 + 8 + 8 + 8 + 10
        cmd_w = max(11, inner_w - fixed_w)

        self._proc_header_y = gy
        c0 = gx
        self._proc_col_spans = [
            (c0, c0 + 8, "pid"),
            (c0 + 8, c0 + 27, "name"),
            (c0 + 27, c0 + 27 + cmd_w + 1, "toggle_cmd"),
            (c0 + 28 + cmd_w, c0 + 35 + cmd_w, "gpu%"),
            (c0 + 35 + cmd_w, c0 + 43 + cmd_w, "time+"),
            (c0 + 43 + cmd_w, c0 + 53 + cmd_w, "user"),
            (c0 + 53 + cmd_w, c0 + 68 + cmd_w, "mem%"),
            (c0 + 68 + cmd_w, c0 + 77 + cmd_w, "vram"),
            (c0 + 77 + cmd_w, c0 + 86 + cmd_w, "gpu%"),
            (c0 + 86 + cmd_w, c0 + 96 + cmd_w, "cpu%"),
        ]

        cmd_mode_lbl = ("SmartCmd[e]" if self.use_smart_cmd else "RawCmd[e]")[:cmd_w]
        header_cells = [
            (f"{'PID' + (arrow if sort_col == 'pid' else ':'):>7}", "pid"),
            (f"{'Program' + (arrow if sort_col == 'name' else ':'):<18}", "name"),
            (f"{cmd_mode_lbl:<{cmd_w}}", "toggle_cmd"),
            (f"{'GPU:':>6}", "gpu_tag"),
            (f"{'TIME+' + (arrow if sort_col == 'time+' else ':'):>7}", "time+"),
            (f"{'User' + (arrow if sort_col == 'user' else ':'):<9}", "user"),
            (f"{'MemB':>8}", "mem%"),
            (f"{'Mem%' + (arrow if sort_col == 'mem%' else ''):>5}", "mem%"),
            (f"{'VRAM' + (arrow if sort_col == 'vram' else ''):>8}", "vram"),
            (f"{'GPU%' + (arrow if sort_col == 'gpu%' else ''):>8}", "gpu%"),
            (f"{'CPU%' + (arrow if sort_col == 'cpu%' else ''):>8}", "cpu%"),
        ]
        hx = gx
        for cell_txt, col_key in header_cells:
            is_active_sort = col_key == sort_col
            cell_attr = (
                self.theme.attr(ThemeManager.PAIR_BADGE, bold=True)
                if is_active_sort
                else self.theme.attr(ThemeManager.PAIR_TITLE, bold=True, underline=True)
            )
            self._safe_addstr(win, gy, hx, cell_txt, cell_attr, max_x=max_bx)
            hx += len(cell_txt) + 1

        list_h = max(1, inner_h - 1)
        if self.selected_proc_row < self.proc_scroll_offset:
            self.proc_scroll_offset = self.selected_proc_row
        elif self.selected_proc_row >= self.proc_scroll_offset + list_h:
            self.proc_scroll_offset = self.selected_proc_row - list_h + 1

        visible_slice = procs[self.proc_scroll_offset : self.proc_scroll_offset + list_h]
        self._proc_list_start_y = gy + 1
        self._proc_list_end_y = gy + len(visible_slice)
        for idx, p in enumerate(visible_slice):
            row_y = gy + 1 + idx
            abs_idx = self.proc_scroll_offset + idx
            is_sel = abs_idx == self.selected_proc_row

            prog_disp = (p.tree_prefix + p.name)[:18]
            raw_or_smart = (p.smart_cmd or p.cmdline) if self.use_smart_cmd else p.cmdline
            cmd_disp = raw_or_smart[:cmd_w]
            gpu_tag = f"{p.gpu_index}:{p.gpu_kind}" if p.gpu_index is not None else "-"
            vram_disp = fmt_bytes(p.vram_bytes) if p.vram_bytes > 0 else "-"
            time_disp = fmt_time_plus(p.elapsed_sec)

            if is_sel:
                line = (
                    f"{p.pid:>7} {prog_disp:<18} {cmd_disp:<{cmd_w}} "
                    f"{gpu_tag:>6} {time_disp:>7} {p.user[:9]:<9} "
                    f"{fmt_bytes(p.mem_bytes):>8} {p.mem_pct:>4.1f}% "
                    f"{vram_disp:>8} {p.gpu_pct:>7.1f}% {p.cpu_pct:>7.1f}%"
                )
                self._safe_addstr(win, row_y, gx, line.ljust(inner_w)[:inner_w], self.theme.attr(ThemeManager.PAIR_PROC_SEL, bold=True), max_x=max_bx)
                continue

            # Multi-column syntax highlighting for unselected rows
            rx_col = gx
            # 1. PID
            self._safe_addstr(win, row_y, rx_col, f"{p.pid:>7}", self.theme.attr(ThemeManager.PAIR_DIM), max_x=max_bx)
            rx_col += 8
            # 2. Tree/Group prefix + Program name
            if p.tree_prefix:
                pref_clip = p.tree_prefix[:18]
                name_clip = p.name[: max(0, 18 - len(pref_clip))]
                self._safe_addstr(win, row_y, rx_col, pref_clip, self.theme.attr(ThemeManager.PAIR_GPU_PWR, bold=True), max_x=max_bx)
                prog_pair = ThemeManager.PAIR_GPU_MAIN if (p.gpu_pct > 0 or p.vram_bytes > 0) else ThemeManager.PAIR_TEXT
                self._safe_addstr(win, row_y, rx_col + len(pref_clip), f"{name_clip:<{max(0, 18 - len(pref_clip))}}", self.theme.attr(prog_pair, bold=True), max_x=max_bx)
            else:
                prog_pair = ThemeManager.PAIR_GPU_MAIN if (p.gpu_pct > 0 or p.vram_bytes > 0) else ThemeManager.PAIR_TEXT
                self._safe_addstr(win, row_y, rx_col, f"{prog_disp:<18}", self.theme.attr(prog_pair, bold=True), max_x=max_bx)
            rx_col += 19
            # 3. Command
            cmd_attr = self.theme.attr(ThemeManager.PAIR_TEXT) if (p.cpu_pct >= 5.0 or p.gpu_pct > 0.0) else self.theme.attr(ThemeManager.PAIR_DIM)
            self._safe_addstr(win, row_y, rx_col, f"{cmd_disp:<{cmd_w}}", cmd_attr, max_x=max_bx)
            rx_col += cmd_w + 1
            # 4. GPU Tag
            gtag_attr = self.theme.attr(ThemeManager.PAIR_GPU_MAIN, bold=True) if p.gpu_index is not None else self.theme.attr(ThemeManager.PAIR_DIM)
            self._safe_addstr(win, row_y, rx_col, f"{gpu_tag:>6}", gtag_attr, max_x=max_bx)
            rx_col += 7
            # 5. TIME+
            t_attr = self.theme.attr(ThemeManager.PAIR_MID) if p.elapsed_sec >= 3600 else self.theme.attr(ThemeManager.PAIR_DIM)
            self._safe_addstr(win, row_y, rx_col, f"{time_disp:>7}", t_attr, max_x=max_bx)
            rx_col += 8
            # 6. User
            self._safe_addstr(win, row_y, rx_col, f"{p.user[:9]:<9}", self.theme.attr(ThemeManager.PAIR_DIM), max_x=max_bx)
            rx_col += 10
            # 7. MemB & Mem%
            m_attr = self.theme.pct_attr(p.mem_pct, bold=(p.mem_pct >= 10.0)) if p.mem_pct >= 2.0 else self.theme.attr(ThemeManager.PAIR_TEXT)
            self._safe_addstr(win, row_y, rx_col, f"{fmt_bytes(p.mem_bytes):>8} {p.mem_pct:>4.1f}%", m_attr, max_x=max_bx)
            rx_col += 15
            # 8. VRAM
            v_attr = self.theme.attr(ThemeManager.PAIR_GPU_MEM, bold=True) if p.vram_bytes > 0 else self.theme.attr(ThemeManager.PAIR_DIM)
            self._safe_addstr(win, row_y, rx_col, f"{vram_disp:>8}", v_attr, max_x=max_bx)
            rx_col += 9
            # 9. GPU%
            if p.gpu_pct > 0.0 or p.gpu_index is not None:
                g_attr = self.theme.pct_attr(p.gpu_pct, bold=True) if p.gpu_pct >= 45.0 else self.theme.attr(ThemeManager.PAIR_GPU_MAIN, bold=(p.gpu_pct > 0))
            else:
                g_attr = self.theme.attr(ThemeManager.PAIR_DIM)
            self._safe_addstr(win, row_y, rx_col, f"{p.gpu_pct:>7.1f}%", g_attr, max_x=max_bx)
            rx_col += 9
            # 10. CPU%
            c_attr = self.theme.pct_attr(p.cpu_pct, bold=(p.cpu_pct >= 20.0)) if p.cpu_pct >= 1.0 else self.theme.attr(ThemeManager.PAIR_DIM)
            self._safe_addstr(win, row_y, rx_col, f"{p.cpu_pct:>7.1f}%", c_attr, max_x=max_bx)

        # Right-Edge Proportional Scrollbar on right border when list exceeds view height
        if len(procs) > list_h and list_h >= 2:
            thumb_h = max(1, int(round((list_h / len(procs)) * list_h)))
            max_offset = max(1, len(procs) - list_h)
            thumb_top = int(round((self.proc_scroll_offset / max_offset) * (list_h - thumb_h)))
            for sr in range(thumb_top, min(list_h, thumb_top + thumb_h)):
                self._safe_addstr(win, gy + 1 + sr, x + w - 1, "┃", self.theme.attr(ThemeManager.PAIR_TITLE, bold=True))

    def _render_modals(self, win, h: int, w: int, snap: SystemSnapshot) -> None:
        if self.kill_prompt_pid is not None:
            mw, mh = min(w - 4, 68), 7
            mx, my = (w - mw) // 2, (h - mh) // 2
            for r in range(my, my + mh):
                self._safe_addstr(win, r, mx, " " * mw, self.theme.attr(ThemeManager.PAIR_TEXT))
            self._draw_box(win, my, mx, mh, mw, title="⚠ Signal Process", border_pair=ThemeManager.PAIR_HIGH, title_pair=ThemeManager.PAIR_HIGH)
            self._safe_addstr(win, my + 2, mx + 3, f"Target: PID {self.kill_prompt_pid} ({self.kill_prompt_name[:34]})", self.theme.attr(ThemeManager.PAIR_TEXT, bold=True))
            self._safe_addstr(win, my + 4, mx + 3, "[i] SIGINT(2)  [y/Enter] SIGTERM(15)  [9/K] SIGKILL(9)  [ESC] Cancel", self.theme.attr(ThemeManager.PAIR_MID, bold=True))
            return

        if self.show_sensors:
            mw, mh = min(w - 6, 86), min(h - 4, 24)
            mx, my = (w - mw) // 2, (h - mh) // 2
            for r in range(my, my + mh):
                self._safe_addstr(win, r, mx, " " * mw, self.theme.attr(ThemeManager.PAIR_TEXT))
            self._draw_box(win, my, mx, mh, mw, title="✦ Hardware Sensors & Power Rails [S]", right_title=f"{len(snap.sensors)} sensors │ [S/ESC] Close")
            row_y = my + 1
            if snap.alerts:
                alert_line = " ⚠ Active Alerts: " + " │ ".join(snap.alerts)
                self._safe_addstr(win, row_y, mx + 2, alert_line[: mw - 4], self.theme.attr(ThemeManager.PAIR_HIGH, bold=True), max_x=mx + mw - 1)
                row_y += 1
            if not snap.sensors:
                self._safe_addstr(win, row_y + 1, mx + 4, "No additional hardware sensors reported on this host.", self.theme.attr(ThemeManager.PAIR_DIM), max_x=mx + mw - 1)
            else:
                n_cols = 2 if mw >= 56 else 1
                col_w = max(18, (mw - 6) // n_cols)
                max_rows = max(1, mh - (row_y - my) - 1)
                for idx, s in enumerate(snap.sensors):
                    c = idx // max_rows
                    r = idx % max_rows
                    if c >= n_cols:
                        break
                    sx = mx + 3 + c * col_w
                    val_str = f"{s.value:6.1f} {s.unit}"
                    attr = self.theme.attr(ThemeManager.PAIR_GPU_PWR if s.category == "power" else (ThemeManager.PAIR_MID if s.category == "temp" and s.value >= 75 else ThemeManager.PAIR_TEXT))
                    self._safe_addstr(win, row_y + r, sx, f"{s.name[:col_w - 13]:<{col_w - 13}} {val_str}", attr, max_x=mx + mw - 1)
            return

        if self.show_help:
            mw, mh = min(w - 6, 78), min(h - 2, 23)
            mx, my = (w - mw) // 2, (h - mh) // 2
            for r in range(my, my + mh):
                self._safe_addstr(win, r, mx, " " * mw, self.theme.attr(ThemeManager.PAIR_TEXT))
            self._draw_box(win, my, mx, mh, mw, title="✦ MacMaster v2.0 (btop + nvtop) Shortcuts & Help", right_title="[?/ESC] Close")
            help_lines = [
                ("1 / 2 / 3 / 4 / 5", "Toggle CPU / GPU (nvtop) / MEM+DISK / NET / PROC panels"),
                ("p", "Cycle 4 Layout Presets (Full, AI/GPU Workstation, Classic, Compact)"),
                ("z or m", "Zoom / Maximize active panel (cycles All -> GPU -> CPU -> PROC)"),
                ("f", "Freeze / Pause live telemetry updates while inspecting processes"),
                ("S (Shift+s)", "Open Hardware Sensors & Power Rails drawer (Die/NAND/PMU/GPU)"),
                ("g / G (Shift+g)", "g: GPU Processes Only (nvtop) │ G: Group by Program name"),
                ("t / e", "t: Process Tree view │ e: Toggle Smart Command Line condenser"),
                ("/", "Filter processes by Name, Command, User, or PID (ESC to clear)"),
                ("< / > or s", "Cycle sort column (cpu%, gpu%, mem%, vram, time+, pid, name...)"),
                ("r", "Reverse process sort order (DESC ▼ / ASC ▲)"),
                ("Enter or Space", "Toggle Process Detail Inspector (with live CWD & GPU Env vars)"),
                ("K or x", "Send Signal (SIGINT / SIGTERM / SIGKILL) to selected process"),
                ("d / a", "d: Cycle GPU device │ a: All-GPUs Stacked Overview"),
                ("b / n", "Cycle active Network Interface (en0, eth0, utun*, lo0...)"),
                ("+ / - / c", "+/-: Refresh interval (100ms..10s) │ c: Cycle 8 Color Themes"),
                ("Mouse Click/Wheel", "Scroll list, click header to sort, click row to select/inspect"),
                ("q or ESC", "Quit MacMaster (settings auto-saved to ~/.config/macmaster)"),
            ]
            for idx, (k_str, desc) in enumerate(help_lines):
                if idx + 2 >= mh - 1:
                    break
                self._safe_addstr(win, my + 2 + idx, mx + 3, f"{k_str:<18}", self.theme.attr(ThemeManager.PAIR_TITLE, bold=True))
                self._safe_addstr(win, my + 2 + idx, mx + 22, desc[: mw - 24], self.theme.attr(ThemeManager.PAIR_TEXT))
