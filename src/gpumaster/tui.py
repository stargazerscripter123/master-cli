"""curses front end.

Deliberately thin. All layout lives in `render.py` as pure functions returning
(text, style) spans; this module only maps styles to colour pairs, paints, and
handles keys. Swapping in a different front end means rewriting this file and
nothing else.
"""

from __future__ import annotations

import asyncio
import curses
import threading
import time
from collections import deque
from typing import Optional

from .config import Config, config_changed, load_config
from .model import Fleet, Gpu, Host, Proc
from .render import (
    DETAIL_PROC_LIMIT,
    GLYPH_SETS,
    HOST_SORTS,
    PROC_SORTS,
    S_ACCENT,
    S_CRIT,
    S_DIM,
    S_HEAD,
    S_NORMAL,
    S_OK,
    S_SEL,
    S_WARN,
    Line,
    clamp_lines,
    dwidth,
    ellipsize,
    trim,
    render_fleet,
    render_gpu_detail,
    sort_procs,
    visible_fleet_gpus,
)
from .transport import HostSpec

PAIRS = {
    S_NORMAL: 0,
    S_DIM: 1,
    S_HEAD: 2,
    S_OK: 3,
    S_WARN: 4,
    S_CRIT: 5,
    S_ACCENT: 6,
    S_SEL: 7,
}

HELP = [
    "gpumaster -- keys",
    "",
    "  up/down, j/k     move GPU selection",
    "  enter            expand the selected GPU",
    "  esc / q          back / clear filter / quit",
    "  /                filter by host, GPU, user, container, PID, or command",
    "  p                show or hide the process table",
    "  s                cycle process sort (gpumem, cpu, rss, host, time)",
    "  S                cycle host sort (config, util, free-mem, temp)",
    "  Tab / n / N      select process in GPU detail view",
    "  K                kill selected process in GPU detail view (confirmed)",
    "  h                hide or show idle GPUs",
    "  m                cycle the mode: default, each mode in hosts.json, all",
    "  g                cycle glyphs (block / ascii)",
    "  r                refresh now",
    "  R                clear all backoffs and retry every host",
    "  f                freeze the display (collection continues)",
    "  d                transport diagnostics",
    "  +  -             faster / slower refresh",
    "  PgUp/PgDn Home/End   scroll",
    "  ?                this help",
    "",
    "  markers:  * derived   ? not trustworthy on this part   ! over threshold",
    "            uma unified memory   + PID not resolvable by ps",
    "",
    "  on a uma row the MEMORY pair is GPU-resident over the pool it shares",
    "  with the CPU, not a framebuffer.  where utilization shows '?' (GB10) the",
    "  history strip plots that pool share instead; an Apple row plots its real",
    "  utilization.  the host's UMA line splits the same pool.",
]


class Collector:
    """Polls in a background thread so the UI never blocks on the network."""

    def __init__(self, cfg: Config, poller):
        self.cfg = cfg
        self.poller = poller
        self.lock = threading.Lock()
        self.fleet = Fleet()
        self.history: dict[str, deque] = {}
        self.generation = 0
        self.last_poll_ms = 0.0
        self.stop = threading.Event()
        self.wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while not self.stop.is_set():
                t0 = time.monotonic()
                try:
                    hosts = loop.run_until_complete(
                        self.poller.poll_all(self.cfg.active_hosts()))
                    fleet = Fleet(hosts=hosts)
                except Exception as exc:  # never let a poll kill the UI
                    fleet = Fleet()
                    fleet.error = str(exc)  # type: ignore[attr-defined]
                elapsed = (time.monotonic() - t0) * 1000.0
                with self.lock:
                    self.fleet = fleet
                    self.last_poll_ms = elapsed
                    self.generation += 1
                    self._record(fleet)
                self.wake.wait(timeout=max(0.05, self.cfg.refresh_seconds))
                self.wake.clear()
        finally:
            loop.close()

    def _record(self, fleet: Fleet) -> None:
        n = self.cfg.history_samples
        for host in fleet.hosts:
            for gpu in host.gpus:
                key = f"{host.name}:{gpu.index}"
                buf = self.history.get(key)
                if buf is None:
                    buf = deque(maxlen=n)
                    self.history[key] = buf
                # Only record a number we are willing to display -- on a
                # unified part that is pool share, not utilization. Which one
                # it is lives on the model so the recorder and the renderer
                # cannot disagree.
                buf.append(gpu.history_percent)

    def snapshot(self) -> tuple[Fleet, dict, int, float]:
        with self.lock:
            hist = {k: list(v) for k, v in self.history.items()}
            return self.fleet, hist, self.generation, self.last_poll_ms

    def poke(self) -> None:
        self.wake.set()

    def shutdown(self) -> None:
        self.stop.set()
        self.wake.set()
        if self._thread:
            self._thread.join(timeout=3.0)


_HAS_256 = False


def init_colors() -> None:
    global _HAS_256
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    _HAS_256 = getattr(curses, "COLORS", 8) >= 256
    if _HAS_256:
        try:
            curses.init_pair(PAIRS[S_DIM], 245, bg)      # neutral slate gray (#8a8a8a)
            curses.init_pair(PAIRS[S_HEAD], 81, bg)      # bright sky cyan (#5fd7ff)
            curses.init_pair(PAIRS[S_OK], 114, bg)       # emerald green (#87d787)
            curses.init_pair(PAIRS[S_WARN], 214, bg)     # warm amber gold (#ffaf00)
            curses.init_pair(PAIRS[S_CRIT], 203, bg)     # coral red (#ff5f5f)
            curses.init_pair(PAIRS[S_ACCENT], 141, bg)   # lavender violet (#af87ff)
            curses.init_pair(PAIRS[S_SEL], 16, 81)       # near-black on sky cyan
            return
        except curses.error:
            _HAS_256 = False
    curses.init_pair(PAIRS[S_DIM], curses.COLOR_BLUE, bg)
    curses.init_pair(PAIRS[S_HEAD], curses.COLOR_CYAN, bg)
    curses.init_pair(PAIRS[S_OK], curses.COLOR_GREEN, bg)
    curses.init_pair(PAIRS[S_WARN], curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIRS[S_CRIT], curses.COLOR_RED, bg)
    curses.init_pair(PAIRS[S_ACCENT], curses.COLOR_MAGENTA, bg)
    curses.init_pair(PAIRS[S_SEL], curses.COLOR_BLACK, curses.COLOR_CYAN)


def attr_for(style: str) -> int:
    if not curses.has_colors():
        return curses.A_BOLD if style in (S_HEAD, S_CRIT, S_SEL) else curses.A_NORMAL
    pair = PAIRS.get(style, 0)
    a = curses.color_pair(pair) if pair else curses.A_NORMAL
    if style in (S_HEAD, S_CRIT, S_SEL):
        a |= curses.A_BOLD
    if style == S_DIM and not _HAS_256:
        a |= curses.A_DIM
    return a


def paint(stdscr, lines: list[Line], top: int) -> None:
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    view = lines[top:top + h - 1]
    for row, line in enumerate(view):
        col = 0
        for text, style in line:
            if col >= w - 1:
                break
            chunk = trim(text, max(0, w - 1 - col))
            if not chunk:
                continue
            try:
                stdscr.addstr(row, col, chunk, attr_for(style))
            except curses.error:
                pass
            # Advance by DISPLAY width: a wide glyph occupies two cells, so
            # using len() here would overlap the next span.
            col += dwidth(chunk)
    stdscr.noutrefresh()
    curses.doupdate()


def status_line(
    cfg: Config,
    collector: Collector,
    extra: str = "",
    extra_style: str = S_WARN,
    modal: bool = False,
    width: int = 120,
) -> Line:
    max_w = max(20, width)
    if modal and extra:
        return clamp_lines(
            [[(f"  {ellipsize(extra, max_w - 2)}", extra_style)]], max_w
        )[0]
    keys = [
        ("[?]", "help "),
        ("[enter]", "expand "),
        ("[/]", "filter "),
        ("[p]", "procs "),
        ("[s/S]", "sort "),
        ("[h]", "idle "),
        ("[d]", "diag "),
        ("[f]", "freeze "),
        ("[+/-]", "rate "),
        ("[q]", "quit"),
    ]
    spans: Line = [("  ", S_DIM)]
    for k, lbl in keys:
        spans.append((k, S_ACCENT))
        spans.append((lbl, S_DIM))
    spans.append((f"   {1.0/cfg.refresh_seconds:.1f}Hz", S_HEAD))
    spans.append((f"  poll {collector.last_poll_ms:.0f}ms", S_DIM))
    if extra:
        spans.append((f"   {extra}", extra_style))
    return clamp_lines([spans], max_w)[0]


def edit_filter_key(ch: int, query: str) -> tuple[str, bool]:
    """Apply one keystroke to `/` filter input; returns `(new_query, keep_editing)`."""
    if ch == 27:  # Esc clears and exits filter editing
        return "", False
    if ch in (curses.KEY_ENTER, 10, 13):
        return query, False
    if ch in (curses.KEY_BACKSPACE, 127, 8):
        return query[:-1], True
    if 32 <= ch <= 126:
        return query + chr(ch), True
    return query, True


def diag_lines(cfg: Config, collector: Collector, fleet: Fleet, width: int) -> list[Line]:
    out: list[Line] = [[("TRANSPORT DIAGNOSTICS", S_HEAD)], [("", S_DIM)]]
    out.append([
        (f"  {'HOST':<14}{'TARGET':<16}{'STATE':<10}{'LAT':>7}{'REMOTE':>8}"
         f"{'GPUS':>5}{'PIDS':>6}{'BACKOFF':>9}  ERROR", S_HEAD)
    ])
    for h in fleet.hosts:
        wait = collector.poller.backoff_remaining(h.name)
        out.append([(
            f"  {h.name:<14}{h.target:<16}{h.state.name.lower():<10}"
            f"{(f'{h.latency_ms:.0f}ms' if h.latency_ms else '-'):>7}"
            f"{(f'{h.remote_ms}ms' if h.remote_ms else '-'):>8}"
            f"{len(h.gpus):>5}{h.ps_resolved:>3}/{h.ps_requested:<2}"
            f"{(f'{wait:.0f}s' if wait else '-'):>9}  {h.error_msg[:60]}",
            S_CRIT if not h.online else S_NORMAL,
        )])
    out.append([("", S_DIM)])
    out.append([("  ssh command template", S_HEAD)])
    if cfg.active_hosts():
        argv = collector.poller.exec_argv(cfg.active_hosts()[0])
        out.append([("    " + " ".join(argv), S_DIM)])
    out.append([("", S_DIM)])
    out.append([("  recent stderr", S_HEAD)])
    any_err = False
    for h in fleet.hosts:
        for line in collector.poller.stderr_ring(h.name)[-3:]:
            any_err = True
            out.append([(f"    {h.name}: {line[:width-20]}", S_WARN)])
    if not any_err:
        out.append([("    (none)", S_DIM)])
    out.append([("", S_DIM)])
    out.append([("  config", S_HEAD)])
    out.append([(f"    {cfg.source_path}", S_DIM)])
    out.append([("    press esc to return", S_DIM)])
    return out


def flat_gpu_list(fleet: Fleet) -> list[tuple[Host, Gpu]]:
    return [(h, g) for h in fleet.hosts for g in h.gpus]


def run_tui(cfg: Config, poller, glyphs, args) -> int:
    collector = Collector(cfg, poller)
    collector.start()
    try:
        return curses.wrapper(_main_loop, cfg, collector, glyphs, args)
    finally:
        collector.shutdown()


def _main_loop(stdscr, cfg: Config, collector: Collector, glyphs, args) -> int:
    curses.curs_set(0)
    init_colors()
    stdscr.nodelay(True)
    stdscr.keypad(True)

    view = "fleet"          # fleet | detail | help | diag
    top = 0
    sel = 0
    hide_idle = bool(args.hide_idle)
    show_procs = not args.no_procs
    proc_sort = "gpumem"
    host_sort = "config"
    filter_query = ""
    filter_editing = False
    detail_proc_sel = 0
    confirm_kill: Optional[tuple[Host, Gpu, Proc]] = None
    frozen = False
    glyph_name = glyphs.name
    toast = ""
    toast_until = 0.0
    last_gen = -1
    frozen_lines: list[Line] = []

    while True:
        h, w = stdscr.getmaxyx()
        fleet, history, gen, _ = collector.snapshot()
        gpus = visible_fleet_gpus(
            fleet,
            hide_idle=hide_idle,
            filter_query=filter_query,
            host_sort=host_sort,
        )
        if gpus:
            sel = max(0, min(sel, len(gpus) - 1))
        elif view == "detail":
            view = "fleet"
            confirm_kill = None

        if toast and time.time() > toast_until:
            toast = ""

        if view == "help":
            lines = [[(l, S_NORMAL if not l.startswith("gpumaster") else S_HEAD)]
                     for l in HELP]
        elif view == "diag":
            lines = diag_lines(cfg, collector, fleet, w)
        elif view == "detail" and gpus:
            host, gpu = gpus[sel]
            shown_procs = min(DETAIL_PROC_LIMIT, len(gpu.procs))
            if shown_procs:
                detail_proc_sel = max(0, min(detail_proc_sel, shown_procs - 1))
                sel_proc = detail_proc_sel
            else:
                detail_proc_sel = 0
                sel_proc = -1
                confirm_kill = None
            lines = render_gpu_detail(
                host, gpu, w, GLYPH_SETS[glyph_name],
                history.get(f"{host.name}:{gpu.index}"),
                selected_proc=sel_proc,
            )
        elif frozen and frozen_lines:
            lines = frozen_lines
        elif gen == 0:
            # Before the first poll lands, say so rather than rendering an
            # empty fleet that reads as "everything is down".
            lines = [
                [("gpumaster", S_HEAD), ("   connecting...", S_DIM)],
                [("", S_DIM)],
            ] + [[(f"  {s.name:<14} {s.target}", S_DIM)] for s in cfg.active_hosts()]
        else:
            selkey = (gpus[sel][0].name, gpus[sel][1].index) if gpus else None
            lines = render_fleet(
                fleet, w, h - 1, GLYPH_SETS[glyph_name], history=history,
                hide_idle=hide_idle, show_procs=show_procs, proc_sort=proc_sort,
                selected_gpu=selkey, clock=time.strftime("%H:%M:%S"),
                mode=cfg.mode_label, filter_query=filter_query,
                host_sort=host_sort,
            )
            if frozen:
                frozen_lines = lines

        lines = clamp_lines(lines, w - 1)
        if confirm_kill is not None:
            kh, _kg, kp = confirm_kill
            status = status_line(
                cfg, collector,
                f"KILL {kh.name} PID {kp.pid} ({kp.user or '?'}: {kp.name})? "
                f"[y/t]=SIGTERM [k]=SIGKILL [any]=cancel",
                S_CRIT,
                modal=True,
                width=w - 1,
            )
        elif filter_editing:
            status = status_line(
                cfg, collector,
                f"filter: /{filter_query}_  [enter]apply [esc]clear",
                S_ACCENT,
                modal=True,
                width=w - 1,
            )
        else:
            status = status_line(
                cfg, collector, toast or ("FROZEN" if frozen else ""), width=w - 1
            )
        max_top = max(0, len(lines) - (h - 1))
        top = min(top, max_top)
        paint(stdscr, lines[top:top + h - 1] + [status], 0)

        if gen != last_gen:
            last_gen = gen
            if config_changed(cfg):
                new = load_config(cfg.source_path)
                if not new.warnings:
                    cfg.hosts = new.hosts
                    cfg.modes = new.modes
                    if cfg.mode not in cfg.mode_names:
                        cfg.mode = "default"
                    cfg.refresh_seconds = new.refresh_seconds
                    cfg.mtime = new.mtime
                    toast = "config reloaded"
                    toast_until = time.time() + 3

        # Sleep in short slices so keys stay responsive between polls.
        deadline = time.monotonic() + min(0.25, cfg.refresh_seconds)
        while time.monotonic() < deadline:
            try:
                ch = stdscr.getch()
            except curses.error:
                ch = -1
            if ch == -1:
                time.sleep(0.02)
                continue
            if ch == curses.KEY_RESIZE:
                break

            if confirm_kill is not None:
                if ch in (ord("y"), ord("Y"), ord("t"), ord("T"), ord("k"), ord("K")):
                    sig = "KILL" if ch in (ord("k"), ord("K")) else "TERM"
                    khost, _kgpu, kproc = confirm_kill
                    spec = next(
                        (s for s in cfg.hosts if s.name == khost.name or s.target == khost.target),
                        HostSpec(name=khost.name, target=khost.target),
                    )
                    _ok, msg = collector.poller.kill_remote_proc(spec, kproc.pid, sig)
                    toast = msg
                    toast_until = time.time() + 4
                    confirm_kill = None
                    collector.poke()
                else:
                    confirm_kill = None
                    toast = "kill cancelled"
                    toast_until = time.time() + 2
                break

            if filter_editing:
                filter_query, filter_editing = edit_filter_key(ch, filter_query)
                sel = 0
                top = 0
                break

            if ch in (ord("q"),):
                if view != "fleet":
                    view = "fleet"
                    top = 0
                    break
                return 0
            if ch == 27:  # esc
                if view != "fleet":
                    view = "fleet"
                    top = 0
                elif filter_query:
                    filter_query = ""
                    sel = 0
                    top = 0
                    toast = "filter cleared"
                    toast_until = time.time() + 2
                break
            if ch == ord("/"):
                view = "fleet"
                filter_editing = True
                break
            if ch in (curses.KEY_DOWN, ord("j")):
                sel = min(sel + 1, max(0, len(gpus) - 1))
                detail_proc_sel = 0
                break
            if ch in (curses.KEY_UP, ord("k")):
                sel = max(sel - 1, 0)
                detail_proc_sel = 0
                break
            if view == "detail" and gpus and ch in (9, ord("n")):
                _, dgpu = gpus[sel]
                n_shown = min(DETAIL_PROC_LIMIT, len(dgpu.procs))
                if n_shown:
                    detail_proc_sel = (detail_proc_sel + 1) % n_shown
                break
            if view == "detail" and gpus and ch == ord("N"):
                _, dgpu = gpus[sel]
                n_shown = min(DETAIL_PROC_LIMIT, len(dgpu.procs))
                if n_shown:
                    detail_proc_sel = (detail_proc_sel - 1) % n_shown
                break
            if view == "detail" and gpus and ch == ord("K"):
                dhost, dgpu = gpus[sel]
                n_shown = min(DETAIL_PROC_LIMIT, len(dgpu.procs))
                if n_shown:
                    idx = max(0, min(detail_proc_sel, n_shown - 1))
                    confirm_kill = (dhost, dgpu, dgpu.procs[idx])
                else:
                    toast = "no GPU process to kill"
                    toast_until = time.time() + 2
                break
            if ch in (curses.KEY_NPAGE,):
                top = min(max_top, top + (h - 2)); break
            if ch in (curses.KEY_PPAGE,):
                top = max(0, top - (h - 2)); break
            if ch == curses.KEY_HOME:
                top = 0; break
            if ch == curses.KEY_END:
                top = max_top; break
            if ch in (curses.KEY_ENTER, 10, 13):
                view = "detail" if view != "detail" else "fleet"
                detail_proc_sel = 0
                top = 0; break
            if ch == ord("?"):
                view = "help" if view != "help" else "fleet"
                top = 0; break
            if ch == ord("d"):
                view = "diag" if view != "diag" else "fleet"
                top = 0; break
            if ch == ord("p"):
                show_procs = not show_procs; break
            if ch == ord("h"):
                hide_idle = not hide_idle; break
            if ch == ord("m"):
                # The next poll picks the new host set up; a laptop lands as
                # [x] until it answers, which is the honest state for a
                # machine that is asleep.
                names = cfg.mode_names
                cfg.mode = names[(names.index(cfg.mode) + 1) % len(names)]
                n = len(cfg.active_hosts())
                toast = f"mode: {cfg.mode} ({n} host{'s' if n != 1 else ''})"
                toast_until = time.time() + 2
                collector.poke()
                sel = 0; top = 0; break
            if ch == ord("s"):
                proc_sort = PROC_SORTS[(PROC_SORTS.index(proc_sort) + 1) % len(PROC_SORTS)]
                toast = f"sort: {proc_sort}"; toast_until = time.time() + 2; break
            if ch == ord("S"):
                host_sort = HOST_SORTS[(HOST_SORTS.index(host_sort) + 1) % len(HOST_SORTS)]
                toast = f"host sort: {host_sort}"
                toast_until = time.time() + 2
                sel = 0
                break
            if ch == ord("g"):
                names = list(GLYPH_SETS)
                glyph_name = names[(names.index(glyph_name) + 1) % len(names)]
                toast = f"glyphs: {glyph_name}"; toast_until = time.time() + 2; break
            if ch == ord("f"):
                frozen = not frozen
                frozen_lines = [] if not frozen else frozen_lines
                break
            if ch == ord("r"):
                collector.poke(); break
            if ch == ord("R"):
                collector.poller._fail_count.clear()
                collector.poller._next_try.clear()
                collector.poke()
                toast = "retrying every host"; toast_until = time.time() + 2; break
            if ch in (ord("+"), ord("=")):
                cfg.refresh_seconds = max(0.2, cfg.refresh_seconds / 2)
                toast = f"{1/cfg.refresh_seconds:.1f}Hz"; toast_until = time.time() + 2; break
            if ch == ord("-"):
                cfg.refresh_seconds = min(30.0, cfg.refresh_seconds * 2)
                toast = f"{1/cfg.refresh_seconds:.1f}Hz"; toast_until = time.time() + 2; break
