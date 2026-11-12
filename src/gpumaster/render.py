"""Presentation layer: pure functions that turn a Fleet into lines of text.

This module imports neither curses nor any TUI framework. It returns
`list[Line]`, where a Line is a list of (text, style) spans. That keeps the
front end swappable and lets every layout be regression-tested offline against
a captured fixture.

Two width rules are load-bearing:

  * All widths are DISPLAY widths, not character counts. Block-drawing glyphs
    are East-Asian-Width "Ambiguous" and render double-width under CJK fonts,
    which would shatter the grid for a user who reads Chinese. `dwidth()`
    measures, `GLYPHS` can fall back to pure ASCII.

  * Command lines are middle-ellipsized, never truncated on the right. The two
    ComfyUI jobs on workstation-a are byte-identical except for a trailing
    `--port 8188` / `8189`; right-truncation makes them indistinguishable.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

from .model import (
    MEM_AVAIL_CRIT_FRAC,
    MEM_AVAIL_WARN_FRAC,
    Fleet,
    Gpu,
    Host,
    HostState,
    Level,
    Proc,
    gpu_alerts,
    gpu_free_mib,
    host_alerts,
    is_stale_proc,
    short_gpu_name,
    worst_level,
)

# ---------------------------------------------------------------------------
# Styles. The TUI maps these onto colour pairs; --once maps them onto nothing.
# ---------------------------------------------------------------------------
S_NORMAL = "normal"
S_DIM = "dim"
S_HEAD = "head"
S_OK = "ok"
S_WARN = "warn"
S_CRIT = "crit"
S_ACCENT = "accent"
S_SEL = "sel"

Span = tuple[str, str]
Line = list[Span]


# ---------------------------------------------------------------------------
# Glyph sets
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GlyphSet:
    name: str
    bar_full: str
    bar_parts: str      # increasing partial-cell fills, may be empty
    bar_mid: str        # second segment of a stacked bar
    bar_track: str
    spark: str
    marker_derived: str
    marker_unknown: str


GLYPHS_BLOCK = GlyphSet(
    name="block",
    bar_full="█",                       # FULL BLOCK
    bar_parts="▏▎▍▌▋▊▉",
    # The stacked UMA bar must stay readable in --once, which has no colour, so
    # the second segment is a different glyph and not merely a different style.
    bar_mid="▒",                        # MEDIUM SHADE
    bar_track="░",                      # LIGHT SHADE (EAW Narrow, safe)
    spark=" ▁▂▃▄▅▆▇█",
    marker_derived="*",
    marker_unknown="?",
)

# Braille is East-Asian-Width Narrow, so it is always single width even under a
# CJK font. Used for the history strip regardless of the chosen bar glyphs.
BRAILLE_SPARK = " ⡀⡄⡆⡇⣇⣧⣷⣿"

GLYPHS_ASCII = GlyphSet(
    name="ascii",
    bar_full="#",
    bar_parts="+",
    bar_mid="=",
    bar_track=".",
    spark="_.-=+*#%@",
    marker_derived="*",
    marker_unknown="?",
)

GLYPH_SETS = {"block": GLYPHS_BLOCK, "ascii": GLYPHS_ASCII}


# Whether the terminal renders East-Asian-Width "Ambiguous" characters as two
# columns. True under a CJK locale. Measured EAW classes for the glyphs this
# module can emit:
#     A (ambiguous, unsafe):  █ ▏▎▍▌▋▊▉ ▁▂▃▄▅▆▇ │ ─ ╭ … ·
#     N (neutral,   safe)  :  ░  and the whole braille block ⠀..⣿
# So the shade track and the braille sparkline are safe everywhere, while the
# block bars and even the HORIZONTAL ELLIPSIS are not.
_AMBIGUOUS_WIDE = False


def set_ambiguous_wide(value: bool) -> None:
    global _AMBIGUOUS_WIDE
    _AMBIGUOUS_WIDE = bool(value)


def ambiguous_wide() -> bool:
    return _AMBIGUOUS_WIDE


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    eaw = unicodedata.east_asian_width(ch)
    if eaw in ("W", "F"):
        return 2
    if eaw == "A" and _AMBIGUOUS_WIDE:
        return 2
    return 1


def dwidth(text: str) -> int:
    """Display width in terminal columns."""
    return sum(char_width(ch) for ch in text)


def ellipsis() -> str:
    """U+2026 is Ambiguous, so fall back to ASCII when that means two columns."""
    return ".." if _AMBIGUOUS_WIDE else "…"


def is_ambiguous(sample: str) -> bool:
    return any(unicodedata.east_asian_width(ch) == "A" for ch in sample)


def pad(text: str, width: int, align: str = "left") -> str:
    """Pad or trim to an exact DISPLAY width."""
    w = dwidth(text)
    if w > width:
        return trim(text, width)
    fill = " " * (width - w)
    if align == "right":
        return fill + text
    if align == "center":
        left = (width - w) // 2
        return " " * left + text + " " * (width - w - left)
    return text + fill


def trim(text: str, width: int) -> str:
    """Hard trim from the right to a display width."""
    if width <= 0:
        return ""
    out, w = [], 0
    for ch in text:
        cw = char_width(ch)
        if w + cw > width:
            break
        out.append(ch)
        w += cw
    return "".join(out)


def ellipsize(text: str, width: int) -> str:
    """Middle-ellipsize, preserving the tail.

    The tail carries the distinguishing tokens: `--port 8188` versus `8189` is
    the only difference between two otherwise identical ComfyUI command lines.
    """
    if width <= 0:
        return ""
    if dwidth(text) <= width:
        return text
    ell = ellipsis()
    ew = dwidth(ell)
    if width <= ew + 1:
        return trim(text, width)
    keep_tail = max(1, (width - ew) * 3 // 5)
    keep_head = width - ew - keep_tail
    head = trim(text, keep_head)
    tail = text[-keep_tail:] if keep_tail else ""
    while dwidth(head) + ew + dwidth(tail) > width and tail:
        tail = tail[1:]
    return f"{head}{ell}{tail}"


# ---------------------------------------------------------------------------
# Command line shortening
# ---------------------------------------------------------------------------

def shorten_command(cmd: str, home_user: str = "") -> str:
    """Collapse noisy prefixes while keeping every distinguishing token."""
    if not cmd:
        return ""
    out = cmd
    # ~/miniconda3/envs/<env>/bin/python  ->  py@<env>
    import re

    out = re.sub(
        r"\S*?/(?:mini|ana)conda3?/envs/([^/\s]+)/bin/python[0-9.]*",
        r"py@\1",
        out,
    )
    out = re.sub(r"\S*/(?:mini|ana)conda3?/bin/python[0-9.]*", "py@base", out)
    out = re.sub(r"/home/([^/\s]+)/", "~/", out)
    out = re.sub(r"^/usr/(?:local/)?(?:s?bin|lib/\S+?)/", "", out)
    out = re.sub(r"^/opt/[^/\s]+/(?:\S+?/)*", "", out)
    return out


def fit_command(cmd: str, width: int) -> str:
    """Fit a command line into `width`, shedding the least informative parts.

    Plain middle-ellipsis is not enough in a narrow column. The four ComfyUI
    jobs on workstation-b are identical except for `--port 8188..8191`, and that
    token sits in the MIDDLE, followed by 21 characters of `--disable-auto-
    launch` that distinguish nothing. Tail-preserving ellipsis keeps the noise
    and drops the signal.

    So before ellipsizing we drop bare switches (long options with no value)
    from the right, since an option carrying a value is always more
    identifying than one that does not.
    """
    if width <= 0:
        return ""
    if dwidth(cmd) <= width:
        return cmd

    tokens = cmd.split()
    if len(tokens) > 1:
        # Index the bare switches: a --flag not followed by a value.
        droppable = []
        for i, tok in enumerate(tokens):
            if i == 0 or not tok.startswith("-") or "=" in tok:
                continue
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if nxt is None or nxt.startswith("-"):
                droppable.append(i)
        # Drop from the right, most-recently-added first.
        for i in reversed(droppable):
            tokens[i] = ""
            candidate = " ".join(t for t in tokens if t)
            ell = ellipsis()
            if dwidth(candidate) + 1 + dwidth(ell) <= width:
                return candidate + " " + ell
        cmd = " ".join(t for t in tokens if t)
        if dwidth(cmd) <= width:
            return cmd
    return ellipsize(cmd, width)


def fmt_mib(v: Optional[float]) -> str:
    if v is None:
        return "-"
    if v >= 10240:
        return f"{v/1024:.1f}G"
    if v >= 1024:
        return f"{v/1024:.2f}G"
    return f"{v:.0f}M"


def fmt_kib_g(v: Optional[float]) -> str:
    if v is None:
        return "-"
    return f"{v/1048576:.0f}G"


def fmt_pct(v: Optional[float], unknown: str = "?") -> str:
    return unknown if v is None else f"{v:.0f}"


def fmt_etime(e: str) -> str:
    return e or "-"


# ---------------------------------------------------------------------------
# Bars and sparklines
# ---------------------------------------------------------------------------

def bar(frac: Optional[float], width: int, g: GlyphSet) -> str:
    """A proportional bar over a visible track.

    `None` renders as an empty track, never as 0% -- a missing value and a
    genuine zero are different states and must look different.
    """
    if width <= 0:
        return ""
    if frac is None:
        return g.bar_track * width
    frac = max(0.0, min(1.0, frac))
    units = frac * width
    full = int(units)
    out = g.bar_full * min(full, width)
    if full < width and g.bar_parts:
        rem = units - full
        if rem > 1.0 / (len(g.bar_parts) + 1):
            idx = min(len(g.bar_parts) - 1, int(rem * len(g.bar_parts)))
            out += g.bar_parts[idx]
    return pad(out, width).replace(" ", g.bar_track)


def uma_cell(gpu: Gpu, g: GlyphSet, width: int) -> str:
    """The MEMORY cell for a unified part: GPU-resident over the shared pool.

    Degrades rather than overflowing. `pad(..., 'right')` trims from the RIGHT,
    so an oversized string would silently eat the `*` that marks the value
    derived -- the one character that stops the pair being read as a
    framebuffer. `33.4G/124.0G*` is 13 against a 15-wide column; the coarse
    form exists for a pool of a TiB or more, where `fmt_mib` needs a seventh
    character on both sides and the pair reaches 16.
    """
    res, tot = gpu.gpu_resident_mib, gpu.uma_total_mib
    # Apple reads the resident figure straight from the accelerator, so it
    # carries no derived marker; GB10's per-process sum keeps it.
    mark = g.marker_derived if gpu.mem_derived else ""
    if not tot:
        # /proc/meminfo unreadable: a numerator alone still beats a ratio with
        # an invented denominator.
        return f"{fmt_mib(res)}{mark}"
    full = f"{fmt_mib(res)}/{fmt_mib(tot)}{mark}"
    if dwidth(full) <= width:
        return full
    return f"{(res or 0)/1024:.0f}/{tot/1024:.0f}G{mark}"


def stacked_bar(first: Optional[float], second: Optional[float],
                width: int, g: GlyphSet) -> Line:
    """Two-segment bar over a visible track, as spans so each carries a style.

    Used for the unified pool, where one bar has to answer two questions at
    once: how much of the pool this GPU is holding, and how much of the rest is
    already spoken for by the CPU and page cache. A single-segment bar can show
    one or the other but not the headroom between them.

    Segment sizes round to the nearest cell rather than truncating: an idle
    Spark holds ~5 GiB of the pool, which is 4.4% and truncates to nothing over
    22 cells -- a bar that reads as broken rather than as nearly empty. A
    segment under half a cell still shows as nothing, which is honest at this
    resolution; the figures beside the bar carry the precision.

    `width` is DISPLAY COLUMNS, not characters. `█` and `▒` are both
    East-Asian-Width Ambiguous and occupy two columns under a CJK font, so
    emitting `width` glyphs would draw a 22-column bar 38 columns wide. Like
    `bar()`, this measures what it emits; the bar keeps its width and loses
    resolution, which is the right way round.

    Each segment is clamped to the columns that are left, so the total is
    always exactly `width` and an over-100% input can only ever fill the bar --
    never overflow it, and never produce a negative slice.

    `None` is 0%, not "unknown". `bar()` draws an empty track for a missing
    value; a pool with no slices is a real, measured state here.
    """
    if width <= 0:
        return []
    a = max(0.0, min(1.0, first or 0.0))
    b = max(0.0, min(1.0, second or 0.0))
    if a + b > 1.0:
        b = max(0.0, 1.0 - a)

    spans: Line = []
    used = 0
    for glyph, frac, style in ((g.bar_full, a, S_ACCENT), (g.bar_mid, b, S_NORMAL)):
        cw = max(1, dwidth(glyph))
        n = min(round(frac * width / cw), (width - used) // cw)
        if n > 0:
            spans.append((glyph * n, style))
            used += n * cw
    # The track is EAW Neutral in both glyph sets, so it can always close the
    # remainder to exactly `width` even when a segment glyph could not.
    if used < width:
        spans.append((g.bar_track * (width - used), S_DIM))
    return spans


def sparkline(values: Iterable[Optional[float]], width: int, ramp: str) -> str:
    """Fixed-scale 0..100 sparkline, right-aligned to `width` samples."""
    vals = list(values)[-width:]
    if not vals:
        return ramp[0] * width
    out = []
    for v in vals:
        if v is None:
            out.append(ramp[0])
        else:
            idx = int(max(0.0, min(100.0, v)) / 100.0 * (len(ramp) - 1))
            out.append(ramp[idx])
    return (ramp[0] * (width - len(out))) + "".join(out)


def level_style(level: Level) -> str:
    return {Level.OK: S_OK, Level.WARN: S_WARN, Level.CRIT: S_CRIT}[level]


def util_style(v: Optional[float]) -> str:
    if v is None:
        return S_DIM
    if v >= 90:
        return S_CRIT
    if v >= 50:
        return S_WARN
    if v >= 5:
        return S_OK
    return S_DIM


def mem_avail_style(frac: Optional[float], ok: str = S_DIM) -> str:
    """Style for a memory-headroom fraction.

    Written against the same constants `host_alerts()` uses, so the colour of
    a row and the RAM alert for that host can never drift apart -- they are one
    threshold, spelled once.
    """
    if frac is None:
        return ok
    if frac < MEM_AVAIL_CRIT_FRAC:
        return S_CRIT
    if frac < MEM_AVAIL_WARN_FRAC:
        return S_WARN
    return ok


def temp_style(v: Optional[float]) -> str:
    if v is None:
        return S_DIM
    if v >= 87:
        return S_CRIT
    if v >= 80:
        return S_WARN
    if v >= 70:
        return S_ACCENT
    return S_NORMAL


# ---------------------------------------------------------------------------
# Column layout
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Layout:
    """Column widths for the compact GPU row, chosen for a terminal width."""

    total: int
    model: int
    bar: int
    mem: int
    power: int
    clock: int
    flags: int
    hist: int
    proc: int
    show_hist: bool
    show_proc: bool
    show_clock: bool
    show_flags: bool


def compute_layout(width: int) -> Layout:
    """Shed columns in a fixed order as the terminal narrows.

    Order of sacrifice: history strip, then the inline top process, then the
    clock pair, then the flags column. GPU utilization, memory, temperature and
    power are never dropped.
    """
    w = max(60, width)
    model = 14          # "RTX 6000 Ada" is 12; 14 keeps a gap before the bar
    bar_w = 10
    mem = 15
    power = 9
    clock = 10
    flags = 9
    fixed = 4 + model + bar_w + 5 + mem + 6 + 5 + power  # idx..power incl gaps

    show_clock = w >= 96
    show_flags = w >= 106
    rest = w - fixed - (clock + 1 if show_clock else 0) - (flags + 1 if show_flags else 0)

    show_hist = rest >= 14
    hist = 12 if show_hist else 0
    rest -= hist + 1 if show_hist else 0

    show_proc = rest >= 22
    proc = max(0, rest - 1) if show_proc else 0

    return Layout(
        total=w, model=model, bar=bar_w, mem=mem, power=power, clock=clock,
        flags=flags, hist=hist, proc=proc, show_hist=show_hist,
        show_proc=show_proc, show_clock=show_clock, show_flags=show_flags,
    )


def column_header(lay: Layout) -> Line:
    parts = [
        pad("  #", 4),
        pad("MODEL", lay.model),
        pad("GPU-UTIL", lay.bar + 5),
        pad("MEMORY", lay.mem, "right"),
        pad("TEMP", 6, "right"),
        pad("FAN", 5, "right"),
        pad("POWER", lay.power, "right"),
    ]
    if lay.show_clock:
        parts.append(" " + pad("SM CLK", lay.clock, "right"))
    if lay.show_flags:
        parts.append(" " + pad("FLAGS", lay.flags))
    if lay.show_hist:
        parts.append(" " + pad("HISTORY", lay.hist))
    if lay.show_proc:
        parts.append(" " + pad("TOP PROCESS", lay.proc))
    return [("".join(parts), S_HEAD)]


def rule(width: int, ch: str = "-", g: GlyphSet = GLYPHS_ASCII) -> Line:
    if g.name == "block" and not _AMBIGUOUS_WIDE and ch == "-":
        ch = "─"
    return [(ch * width, S_DIM)]


def clamp_line(line: Line, width: int) -> Line:
    """Trim a line of spans to an exact display width.

    Applied to every rendered line as a final backstop so that no layout bug
    can ever make the terminal wrap, which would desynchronise the whole grid.
    """
    out: Line = []
    used = 0
    for text, style in line:
        if used >= width:
            break
        w = dwidth(text)
        if used + w <= width:
            out.append((text, style))
            used += w
        else:
            out.append((trim(text, width - used), style))
            used = width
    return out


def clamp_lines(lines: list[Line], width: int) -> list[Line]:
    return [clamp_line(l, width) for l in lines]


# ---------------------------------------------------------------------------
# GPU row
# ---------------------------------------------------------------------------

def gpu_row(
    gpu: Gpu,
    lay: Layout,
    g: GlyphSet,
    history: Optional[list[Optional[float]]] = None,
    selected: bool = False,
) -> Line:
    spans: list[Span] = []
    sel_mark = ("▸" if (g.name == "block" and not _AMBIGUOUS_WIDE) else ">") if selected else " "
    spans.append((f"{sel_mark}{gpu.index:>2} ", S_HEAD if selected else S_DIM))
    spans.append((pad(short_gpu_name(gpu.name), lay.model), S_HEAD if selected else S_NORMAL))

    # --- utilization -----------------------------------------------------
    if gpu.util_trustworthy:
        frac = None if gpu.util_gpu is None else gpu.util_gpu / 100.0
        spans.append((bar(frac, lay.bar, g), util_style(gpu.util_gpu)))
        spans.append((pad(fmt_pct(gpu.util_gpu), 4, "right") + " ", util_style(gpu.util_gpu)))
    else:
        # GB10: utilization.gpu reads 0 with a 33 GB job resident, pstate is
        # pinned P0, and the clock ratio would show 80% on an idle machine.
        # Refuse to display any number rather than pick a misleading one.
        spans.append((bar(None, lay.bar, g), S_DIM))
        spans.append((pad(g.marker_unknown, 4, "right") + " ", S_DIM))

    # --- memory ----------------------------------------------------------
    if gpu.is_unified:
        # GPU-resident over the pool it shares with the CPU. The numerator is
        # the per-process NVML sum, which works on GB10; the denominator is
        # MemTotal, the only honest one this part has. `*` marks it derived and
        # the host's UMA line breaks the same pool down.
        #
        # Styled off pool headroom, the same threshold the host's RAM alert
        # uses -- NOT off resident/pool, which would stay green on a Spark
        # whose CPU side has eaten the pool.
        txt = uma_cell(gpu, g, lay.mem)
        style = mem_avail_style(gpu.pool_available_frac, ok=S_ACCENT)
    else:
        pct = gpu.mem_percent or 0
        txt = f"{fmt_mib(gpu.mem_used_mib)}/{fmt_mib(gpu.mem_total_mib)}"
        style = (
            S_CRIT if pct >= 97
            else S_WARN if pct >= 90
            else S_NORMAL if (gpu.busy or pct >= 5)
            else S_DIM
        )
    spans.append((pad(txt, lay.mem, "right"), style))

    # --- temperature / fan / power ---------------------------------------
    t = f"{gpu.temp_c:.0f}C" if gpu.temp_c is not None else "-"
    spans.append((pad(t, 6, "right"), temp_style(gpu.temp_c)))
    fan = f"{gpu.fan_percent:.0f}%" if gpu.fan_percent is not None else "-"
    spans.append((pad(fan, 5, "right"), S_WARN if gpu.fan_suspect else S_DIM))

    if gpu.power_w is None:
        pw = "-"
    elif gpu.power_limit_w:
        pw = f"{gpu.power_w:.0f}/{gpu.power_limit_w:.0f}"
    else:
        # GB10 reports no power limit at all; show the draw with an explicit
        # missing denominator rather than inventing a full-scale value.
        pw = f"{gpu.power_w:.0f}/-"
    ppct = gpu.power_percent
    pstyle = (
        S_WARN if ppct is not None and ppct >= 95
        else S_NORMAL if (gpu.busy or (ppct is not None and ppct >= 15))
        else S_DIM
    )
    spans.append((pad(pw, lay.power, "right"), pstyle))

    if lay.show_clock:
        if gpu.clock_sm is None:
            ck = "-"
        elif gpu.clock_sm_max:
            ck = f"{gpu.clock_sm:.0f}/{gpu.clock_sm_max:.0f}"
        else:
            ck = f"{gpu.clock_sm:.0f}"
        spans.append((" " + pad(ck, lay.clock, "right"), S_DIM))

    if lay.show_flags:
        badges = [s for s, _, _ in gpu.display_badges]
        if gpu.is_unified:
            badges.append("uma")
        if gpu.link_degraded:
            badges.append("LNK!")
        if gpu.fan_suspect:
            badges.append("fan?")
        alerts = gpu_alerts(gpu)
        style = level_style(worst_level(alerts)) if alerts else S_DIM
        spans.append((" " + pad(trim(" ".join(badges) or "-", lay.flags), lay.flags), style))

    if lay.show_hist:
        ramp = BRAILLE_SPARK if g.name == "block" else g.spark
        hist = history or []
        # Coloured by whatever the strip is actually plotting -- on a unified
        # part util_style(util_gpu) would dim a busy Spark to nothing.
        spans.append((" " + sparkline(hist, lay.hist, ramp),
                      util_style(gpu.history_percent)))

    if lay.show_proc:
        p = gpu.top_proc
        if p is None:
            spans.append((" " + pad("(no process)", lay.proc), S_DIM))
        else:
            user = (p.user or "?")[:8]
            cmd = shorten_command(p.display_command)
            if p.container:
                cmd = f"[{p.container}] {cmd}"
            mem = fmt_mib(p.gpu_mem_mib)
            avail = lay.proc - dwidth(user) - 1 - dwidth(mem) - 1
            body = f"{user} {fit_command(cmd, max(4, avail))} {mem}"
            spans.append((" " + pad(body, lay.proc), S_NORMAL))

    return spans


# ---------------------------------------------------------------------------
# Host header
# ---------------------------------------------------------------------------

def uma_breakdown(host: Host, g: GlyphSet) -> list[Line]:
    """One line splitting a unified pool into GPU-resident, system and free.

    This is the line that answers "will another model fit". The GPU row's
    ratio says what the GPU holds; it cannot say how much of the remainder the
    CPU and page cache have already taken.
    """
    pool = host.uma_pool()
    if pool is None:
        return []
    res, total, avail, other = pool
    if not total:
        return []
    spans: Line = [("      UMA ", S_DIM)]
    spans.extend(stacked_bar(res / total, other / total, 22, g))
    spans.append((f"  {fmt_mib(res)} GPU + {fmt_mib(other)} system"
                  f" / {fmt_mib(total)}", S_NORMAL))
    spans.append((f"   {fmt_mib(avail)} available",
                  mem_avail_style(avail / total)))
    return [spans]


def net_breakdown(host: Host, width: int) -> list[Line]:
    """One compact line summarizing high-speed RoCEv2 / >=25GbE fabric links."""
    links = host.active_net_links
    if not links:
        return []
    summary = host.hs_fabric_summary()
    if not summary:
        return []
    up_rails = sum(1 for nl in links if nl.up)
    total_rails = len(links)
    bad_mtu = [
        nl for nl in links
        if nl.up and nl.rdma_dev and nl.mtu is not None and 0 < nl.mtu < 9000
    ]
    degraded = (0 < up_rails < total_rails) or bool(bad_mtu)
    rates = [
        nl.total_gbps for nl in links
        if nl.up and nl.total_gbps is not None
    ]
    active_bw = sum(rates) >= 0.1 if rates else False
    style = (
        S_WARN if degraded
        else S_OK if active_bw
        else S_ACCENT if up_rails > 0
        else S_DIM
    )

    spans: Line = [("      ", S_DIM), (summary, style)]
    if width >= 80:
        per_rail = []
        for nl in links:
            rdma = f"/{nl.rdma_dev}" if nl.rdma_dev else ""
            if not nl.up:
                per_rail.append(f"{nl.name}{rdma}:DOWN")
            else:
                spd = f"{nl.speed_gbps}G" if nl.speed_gbps else "up"
                mtu_s = (
                    f" mtu {nl.mtu}!"
                    if (nl.rdma_dev and nl.mtu is not None and 0 < nl.mtu < 9000)
                    else ""
                )
                if nl.rx_gbps is not None and nl.tx_gbps is not None:
                    per_rail.append(
                        f"{nl.name}{rdma}:{spd}{mtu_s}"
                        f" rx {nl.rx_gbps:.1f}/tx {nl.tx_gbps:.1f}G"
                    )
                else:
                    per_rail.append(f"{nl.name}{rdma}:{spd}{mtu_s}")
        detail = "  (" + ", ".join(per_rail) + ")"
        cur_w = sum(dwidth(t) for t, _ in spans)
        if cur_w + dwidth(detail) <= width:
            spans.append((detail, S_WARN if bad_mtu else S_DIM))
        elif width - cur_w > 12:
            spans.append((ellipsize(detail, width - cur_w), S_WARN if bad_mtu else S_DIM))
    return [spans]


def host_header(host: Host, width: int, g: GlyphSet) -> list[Line]:
    """One line per host, plus a second line when something needs explaining."""
    alerts = host_alerts(host)
    lvl = worst_level(alerts)
    mark = host.state.value
    mark_style = {
        HostState.STREAMING: S_OK,
        HostState.GPU_DEAD: S_WARN,
        HostState.DOWN: S_CRIT,
        HostState.DISABLED: S_DIM,
    }[host.state]

    spans: list[Span] = [(f"[{mark}] ", mark_style)]
    spans.append((pad(host.name, 14), S_HEAD))

    if host.state is HostState.DOWN:
        spans.append((pad("HOST DOWN  ", 11), S_CRIT))
        detail = host.error_msg or "unreachable"
        if host.cached_gpu_label:
            detail = f"{host.cached_gpu_label} -- {detail}"
        spans.append((ellipsize(detail, max(10, width - 30)), S_DIM))
        return [spans]

    if host.state is HostState.DISABLED:
        spans.append(("SKIPPED  ", S_DIM))
        spans.append((ellipsize(host.error_msg or "", max(10, width - 30)), S_DIM))
        return [spans]

    if host.state is HostState.GPU_DEAD:
        # Never render this as "no GPU": the machine has GPUs, they are simply
        # unreadable right now. Fall back to the inventory from the last good
        # poll when we have one.
        label = (f"{host.cached_gpu_label} [unreadable]"
                 if host.cached_gpu_label else "GPU UNREADABLE")
        spans.append((pad(label, 18), S_WARN))
    else:
        spans.append((pad(host.gpu_label or "no GPU", 18), S_NORMAL))
    if host.arch:
        spans.append((pad(f"{host.ncpu or '?'}c", 4, "right") + " ", S_DIM))
    if host.load1 is not None:
        spans.append((pad(f"ld {host.load1:>5.2f}", 9), S_DIM))

    m = host.mem
    # A Spark's pool is shared with the GPU, so it is labelled UMA rather than
    # RAM -- on that host this number IS the GPU's headroom.
    unified = host.has_unified
    tag = "UMA" if unified else "RAM"
    if m.total_kib:
        used_s = fmt_kib_g(m.used_kib)
        tot_s = fmt_kib_g(m.total_kib)
        frac = m.available_frac or 1.0
        mstyle = mem_avail_style(frac)
        bang = "!" if frac < MEM_AVAIL_WARN_FRAC else " "
        spans.append((pad(f"{tag} {used_s}/{tot_s}{bang}", 15), mstyle))

    sw = m.swap_used_kib
    if sw is not None and m.swap_total_kib:
        gib = sw / 1048576
        sstyle = S_CRIT if gib >= 8 else S_WARN if gib >= 2 else S_DIM
        bang = "!" if gib >= 2 else " "
        spans.append((pad(f"swap {gib:.1f}/{fmt_kib_g(m.swap_total_kib)}{bang}", 14), sstyle))
    elif width >= 100:
        spans.append((" " * 14, S_DIM))

    # PSI alone is not sufficient -- workstation-b reads 0.00 while 8 GiB is
    # swapped out -- but it is the only signal that says "stalling right now".
    some = host.psi_mem.some10
    full = host.psi_mem.full10
    if some is not None:
        pstyle = S_CRIT if (full or 0) > 0 or some >= 20 else S_WARN if some >= 5 else S_DIM
        spans.append((pad(f"PSI {some:.1f}", 9), pstyle))
    elif width >= 100:
        spans.append((" " * 9, S_DIM))

    if host.latency_ms is not None:
        spans.append((pad(f"{host.latency_ms:.0f}ms", 7, "right"), S_DIM))

    lines = [spans]

    # Unified hosts get the pool broken down on its own line. It does not go
    # inline: the header is already ~95 columns and swap/PSI/latency would be
    # pushed off. Suppressed rather than truncated on a narrow terminal.
    if width >= 90 and unified:
        lines.extend(uma_breakdown(host, g))
    if host.net_links:
        lines.extend(net_breakdown(host, width))

    if host.state is HostState.GPU_DEAD:
        # The whole point of this state: the machine is alive and its CPU/RAM
        # numbers above are live. Only the GPU stack is unusable.
        msg = f"      {host.error_code}: {host.error_msg}"
        lines.append([(ellipsize(msg, width), S_WARN)])
        lines.append([
            ("      CPU/RAM/PSI above are live; auto-recovers when the driver returns", S_DIM)
        ])
    if host.pid_ns_mismatch:
        lines.append([
            (f"      PID namespace mismatch: 0 of {host.ps_requested} PIDs resolvable", S_WARN)
        ])
    return lines


# ---------------------------------------------------------------------------
# Fleet summary
# ---------------------------------------------------------------------------

def fleet_summary(
    fleet: Fleet,
    width: int,
    g: GlyphSet,
    clock: str = "",
    mode: str = "",
    host_sort: str = "config",
    filter_query: str = "",
) -> list[Line]:
    c = fleet.counts()
    gpus = fleet.gpus
    total_gpu_slots = len(gpus) + sum(
        _cached_gpu_count(h) for h in fleet.hosts if not h.online
    )
    busy = fleet.busy_count()

    l1: list[Span] = [("gpumaster ", S_HEAD)]
    if clock:
        l1.append((clock + "  ", S_DIM))
    # Without the mode a partial fleet reads as the whole one.
    if mode:
        l1.append((f"[{mode}]  ", S_ACCENT))
    l1.append((f"{c['up']} up", S_OK))
    if c["gpu_dead"]:
        l1.append((f" / {c['gpu_dead']} gpu-dead", S_WARN))
    if c["down"]:
        l1.append((f" / {c['down']} down", S_CRIT))
    if c["disabled"]:
        l1.append((f" / {c['disabled']} off", S_DIM))
    l1.append((f"   {len(gpus)}/{total_gpu_slots} GPU ({busy} busy)", S_NORMAL))

    hot = fleet.hottest()
    if hot is not None and hot.temp_c is not None:
        l1.append((f"   HOT {hot.temp_c:.0f}C {hot.host}:{hot.index}", temp_style(hot.temp_c)))
    thr = fleet.throttling()
    if thr:
        l1.append((f"   THROT {len(thr)}", S_WARN))

    # Host-level MEMORY pressure belongs in the summary: workstation-b has four
    # totally idle GPUs and 8 GiB swapped out, which no GPU column reveals.
    # Filtered to memory tags specifically -- a dead driver is already shown by
    # the host's [!] marker and must not be reported as "pressure".
    mem_tags = {"SWAP", "RAM", "PSI"}
    stressed = [
        h for h in fleet.hosts
        if h.online
        and any(a.level is Level.CRIT and a.tag in mem_tags for a in host_alerts(h))
    ]
    if stressed:
        names = ",".join(h.name for h in stressed[:3])
        l1.append((f"   PRESSURE {names}", S_CRIT))
    if host_sort and host_sort != "config":
        l1.append((f"   [sort:{host_sort}]", S_ACCENT))
    if filter_query.strip():
        l1.append((f"   [/{filter_query.strip()}]", S_ACCENT))

    def gib_group(label: str, used_mib: float, total_mib: float) -> list[Span]:
        """label, bar and `NN% used/total G` -- the fleet line's repeating unit."""
        return [
            (label, S_DIM),
            (bar(used_mib / total_mib, 10, g), S_NORMAL),
            (f" {100*used_mib/total_mib:>3.0f}%"
             f" {used_mib/1024:.0f}/{total_mib/1024:.0f}G", S_NORMAL),
        ]

    used, total = fleet.vram_totals()
    draw, limit = fleet.power_totals()
    l2: list[Span] = [("FLEET ", S_HEAD)]
    if total:
        l2.extend(gib_group("VRAM ", used, total))
        l2.append(("   ", S_NORMAL))
    if limit:
        l2.append(("PWR ", S_DIM))
        l2.append((bar(draw / limit, 10, g), S_NORMAL))
        l2.append((f" {100*draw/limit:>3.0f}% {draw/1000:.2f}/{limit/1000:.2f}kW", S_NORMAL))
    out = [l1, l2]

    # Unified parts get their own line rather than being folded into FLEET
    # VRAM. Adding a Spark's 122 GiB of system RAM to a framebuffer total would
    # corrupt the one number that says how much real VRAM the fleet has left.
    uma_res, uma_tot, n_uma = fleet.uma_totals()
    if uma_tot:
        uma = gib_group("UMA  ", uma_res, uma_tot)
        uma.append((f"   GPU-resident of unified pool"
                    f" ({n_uma} host{'s' if n_uma != 1 else ''})", S_DIM))
        if len(l2) == 1:
            # An all-Spark fleet reports neither a framebuffer nor a power
            # limit, so FLEET would otherwise be a bare label with an indented
            # continuation hanging under it.
            l2.extend(uma)
        else:
            out.append([("      ", S_DIM)] + uma)
    return out


def _cached_gpu_count(host: Host) -> int:
    label = host.cached_gpu_label
    if not label:
        return 0
    head = label.split("x", 1)[0].strip()
    return int(head) if head.isdigit() else 1


# ---------------------------------------------------------------------------
# Process table
# ---------------------------------------------------------------------------

PROC_SORTS = ["gpumem", "cpu", "rss", "host", "time"]


def sort_procs(procs: list[tuple[Host, Gpu, Proc]], key: str) -> list[tuple[Host, Gpu, Proc]]:
    if key == "cpu":
        return sorted(procs, key=lambda t: -(t[2].cpu_percent or 0))
    if key == "rss":
        return sorted(procs, key=lambda t: -(t[2].rss_kib or 0))
    if key == "host":
        return sorted(procs, key=lambda t: (t[0].name, t[1].index, t[2].pid))
    if key == "time":
        return sorted(
            procs,
            key=lambda t: (t[2].etime_seconds if t[2].etime_seconds is not None else -1, t[2].etime),
            reverse=True,
        )
    return sorted(procs, key=lambda t: -(t[2].gpu_mem_mib or 0))


def process_table(
    rows: list[tuple[Host, Gpu, Proc]],
    width: int,
    limit: int,
    selected: int = -1,
) -> list[Line]:
    head = (
        f"  {'HOST':<13}{'GPU':>3} {'PID':>9} {'USER':<8}{'T':<5}"
        f"{'GPU-MEM':>9}{'%CPU':>7}{'RSS':>8}{'ELAPSED':>12}  COMMAND"
    )
    out: list[Line] = [[(pad(head, width), S_HEAD)]]
    fixed = dwidth(head) - len("COMMAND")
    cmd_w = max(12, width - fixed)

    for i, (host, gpu, p) in enumerate(rows[:limit]):
        stale = is_stale_proc(gpu, p)
        style = S_SEL if i == selected else (S_WARN if stale else S_NORMAL)
        raw_cmd = shorten_command(p.display_command)
        if p.container:
            raw_cmd = f"[{p.container}] {raw_cmd}"
        if stale:
            badge = " [IDLE-HOLD]"
            if cmd_w >= dwidth(badge) + 8:
                cmd = ellipsize(raw_cmd, cmd_w - dwidth(badge)) + badge
            else:
                cmd = ellipsize(f"[IDLE-HOLD] {raw_cmd}", cmd_w)
        else:
            cmd = ellipsize(raw_cmd, cmd_w)
        rss = f"{p.rss_kib/1048576:.1f}G" if p.rss_kib else "-"
        cpu = f"{p.cpu_percent:.1f}" if p.cpu_percent is not None else "-"
        mark = "" if p.resolved else "+"
        line = (
            f"  {host.name:<13}{gpu.index:>3} {p.pid:>9} {(p.user or '?')[:7]:<8}"
            f"{p.ptype:<5}{fmt_mib(p.gpu_mem_mib):>9}{cpu:>7}{rss:>8}"
            f"{fmt_etime(p.etime):>12}{mark} {cmd}"
        )
        out.append([(pad(line, width), style)])
    if len(rows) > limit:
        out.append([(f"  ... {len(rows) - limit} more of {len(rows)}", S_DIM)])
    return out


# ---------------------------------------------------------------------------
# Whole screen
# ---------------------------------------------------------------------------

HOST_SORTS = ["config", "util", "free-mem", "temp"]


def sort_hosts(hosts: list[Host], key: str) -> list[Host]:
    """Order hosts by the requested metric (`config`, `util`, `free-mem`, `temp`)."""
    if key == "util":
        def _util_key(h: Host) -> tuple:
            if not h.online or not h.gpus:
                return (1, 0.0, h.name)
            top = max(
                (
                    g.util_gpu
                    if (g.util_trustworthy and g.util_gpu is not None)
                    else (g.pool_percent or 0.0)
                )
                for g in h.gpus
            )
            return (0, -top, h.name)
        return sorted(hosts, key=_util_key)
    if key == "free-mem":
        def _free_key(h: Host) -> tuple:
            if not h.online or not h.gpus:
                return (1, 0.0, h.name)
            top = max((gpu_free_mib(g) or 0.0) for g in h.gpus)
            return (0, -top, h.name)
        return sorted(hosts, key=_free_key)
    if key == "temp":
        def _temp_key(h: Host) -> tuple:
            if not h.online or not h.gpus:
                return (1, 0.0, h.name)
            top = max((g.temp_c if g.temp_c is not None else -1.0) for g in h.gpus)
            return (0, -top, h.name)
        return sorted(hosts, key=_temp_key)
    return list(hosts)


def host_matches_filter(host: Host, query: str) -> bool:
    q = query.strip().lower()
    if not q:
        return True
    return (
        q in host.name.lower()
        or q in host.target.lower()
        or q in host.hostname.lower()
        or any(q in t.lower() for t in host.tags)
    )


def proc_matches_filter(host: Host, gpu: Gpu, proc: Proc, query: str) -> bool:
    q = query.strip().lower()
    if not q:
        return True
    if host_matches_filter(host, q):
        return True
    if q in gpu.name.lower() or q in short_gpu_name(gpu.name).lower():
        return True
    return (
        q in (proc.user or "").lower()
        or q in (proc.container or "").lower()
        or q in proc.display_command.lower()
        or q in str(proc.pid)
    )


def gpu_matches_filter(host: Host, gpu: Gpu, query: str) -> bool:
    q = query.strip().lower()
    if not q:
        return True
    if host_matches_filter(host, q):
        return True
    if q in gpu.name.lower() or q in short_gpu_name(gpu.name).lower():
        return True
    return any(proc_matches_filter(host, gpu, p, q) for p in gpu.procs)


def visible_fleet_gpus(
    fleet: Fleet,
    hide_idle: bool = False,
    filter_query: str = "",
    host_sort: str = "config",
) -> list[tuple[Host, Gpu]]:
    """Return `(Host, Gpu)` pairs in the exact order shown by `render_fleet`."""
    out: list[tuple[Host, Gpu]] = []
    q = filter_query.strip()
    for host in sort_hosts(fleet.hosts, host_sort):
        gpus = host.gpus
        if q:
            gpus = [g for g in gpus if gpu_matches_filter(host, g, q)]
        if hide_idle:
            gpus = [g for g in gpus if g.busy]
        for g in gpus:
            out.append((host, g))
    return out


def render_fleet(
    fleet: Fleet,
    width: int,
    height: int,
    g: GlyphSet = GLYPHS_BLOCK,
    history: Optional[dict[str, list[Optional[float]]]] = None,
    hide_idle: bool = False,
    show_procs: bool = True,
    proc_sort: str = "gpumem",
    selected_gpu: Optional[tuple[str, int]] = None,
    clock: str = "",
    mode: str = "",
    filter_query: str = "",
    host_sort: str = "config",
) -> list[Line]:
    history = history or {}
    lay = compute_layout(width)
    out: list[Line] = []
    out.extend(fleet_summary(fleet, width, g, clock, mode, host_sort, filter_query))
    out.append(rule(width, g=g))
    out.append(column_header(lay))
    out.append(rule(width, g=g))

    q = filter_query.strip()
    hosts = sort_hosts(fleet.hosts, host_sort)
    hidden = 0
    shown_hosts = 0
    for host in hosts:
        gpus = host.gpus
        if q:
            gpus = [g_ for g_ in gpus if gpu_matches_filter(host, g_, q)]
            if not gpus and not host_matches_filter(host, q):
                continue
        shown_hosts += 1
        out.extend(host_header(host, width, g))
        if hide_idle:
            keep = [g_ for g_ in gpus if g_.busy]
            hidden += len(gpus) - len(keep)
            gpus = keep
        for gpu in gpus:
            key = f"{host.name}:{gpu.index}"
            sel = selected_gpu == (host.name, gpu.index)
            out.append(gpu_row(gpu, lay, g, history.get(key), sel))
    if q and shown_hosts == 0:
        out.append([(f"  (no hosts or GPUs match /{q})", S_DIM)])
    if hidden:
        out.append([(f"  ... {hidden} idle GPU(s) hidden  [h] to show", S_DIM)])

    if show_procs:
        out.append(rule(width, g=g))
        rows = [
            (h, gp, p)
            for h in hosts
            for gp in h.gpus
            for p in gp.procs
            if not q or proc_matches_filter(h, gp, p, q)
        ]
        rows = sort_procs(rows, proc_sort)
        remaining = max(3, height - len(out) - 2)
        out.append([
            (pad(f"PROCESSES  {len(rows)} total, sorted by {proc_sort}", width), S_ACCENT)
        ])
        out.extend(process_table(rows, width, remaining))
    return clamp_lines(out, width)


def render_gpu_detail(host: Host, gpu: Gpu, width: int, g: GlyphSet,
                      history: Optional[list[Optional[float]]] = None,
                      selected_proc: int = -1) -> list[Line]:
    """Full nvtop-style panel for one GPU."""
    out: list[Line] = []
    out.append([(f"GPU  {host.name} #{gpu.index}  {gpu.name}", S_HEAD)])
    out.append([(f"     uuid {gpu.uuid}", S_DIM)])
    out.append([(f"     bus  {gpu.bus_id}   driver {gpu.driver}   vbios {gpu.vbios}", S_DIM)])
    out.append(rule(width, g=g))

    def kv(k: str, v: str, style: str = S_NORMAL) -> None:
        out.append([(f"  {k:<26}", S_DIM), (v, style)])

    # '?' means "reported but not trustworthy". On Apple silicon the sensors
    # below are simply not sampled (powermetrics needs root), which the
    # compact row already shows as '-'; the panel must agree with it.
    na = "-" if gpu.is_apple else "?"

    if gpu.is_unified:
        res = gpu.gpu_resident_mib or 0.0
        tot = gpu.uma_total_mib
        kv("memory kind", "unified (coherent UMA, shared with CPU)", S_ACCENT)
        if tot:
            other = gpu.pool_other_mib or 0.0
            out.append([(f"  {'pool':<26}", S_DIM)]
                       + stacked_bar(res / tot, other / tot, 12, g)
                       + [(f"  gpu {fmt_mib(res)}  other {fmt_mib(other)}"
                           f"  free {fmt_mib(gpu.uma_available_mib)}", S_NORMAL)])
        if gpu.is_apple:
            kv("GPU resident", f"{fmt_mib(res)}   = IOAccelerator 'In use system memory'")
            if gpu.mem_reserved_mib is not None:
                kv("driver reserved", f"{fmt_mib(gpu.mem_reserved_mib)}"
                                      "   = 'Alloc system memory' (superset of resident)")
        else:
            kv("GPU resident *", f"{fmt_mib(res)}   = sum of per-process NVML allocations")
        if tot:
            pool_pct = 100.0 * (gpu.pool_used_mib or 0.0) / tot
            kv("pool used *", f"{fmt_mib(gpu.pool_used_mib)} of {fmt_mib(tot)}"
                              f"  ({pool_pct:.0f}%)   = MemTotal - MemAvailable")
        kv("host available", fmt_kib_g(host.mem.available_kib) + "   (MemAvailable)")
        kv("host total", fmt_kib_g(host.mem.total_kib))
        out.append([
            ("  note", S_DIM),
            ("  GPU resident is a SUBSET of host used memory, not an addition:",
             S_WARN),
        ])
        out.append([
            ("        that memory is the process RSS. Adding them double-counts.", S_DIM)
        ])
        if gpu.is_apple:
            if gpu.cores:
                kv("GPU cores", str(gpu.cores))
            if gpu.ane_power_w is not None:
                kv("ANE power", f"{gpu.ane_power_w:.1f}W   (macmon rootless IOReport)")
            kv("framebuffer total",
               "none: memory is on-package and shared with the CPU", S_DIM)
            if (
                gpu.temp_c is not None
                or gpu.power_w is not None
                or gpu.clock_sm is not None
            ):
                kv("not sampled",
                   "per-process GPU time (powermetrics needs root; temp/power via macmon)",
                   S_DIM)
            else:
                kv("not sampled",
                   "temperature, power, per-process GPU time (powermetrics needs root)",
                   S_DIM)
        else:
            # Answering "but nvtop shows a bar" at the exact place someone goes
            # looking for it. On a DGX Spark, nvtop's MEM denominator is MemTotal.
            out.append([
                ("        nvtop's MEM bar on this part is this same /proc/meminfo pool,",
                 S_DIM)
            ])
            out.append([
                ("        not VRAM -- NVML has no framebuffer number to give either tool.",
                 S_DIM)
            ])
            kv("framebuffer total",
               "not reported by this part (NVML: NOT_SUPPORTED)", S_DIM)
    else:
        kv("memory used", f"{fmt_mib(gpu.mem_used_mib)} / {fmt_mib(gpu.mem_total_mib)}"
                          f"  ({fmt_pct(gpu.mem_percent)}%)")
        kv("memory free", fmt_mib(gpu.mem_free_mib))
        kv("memory reserved", fmt_mib(gpu.mem_reserved_mib))

    if gpu.util_trustworthy:
        kv("utilization gpu / mem", f"{fmt_pct(gpu.util_gpu)}% / {fmt_pct(gpu.util_mem, na)}%")
    else:
        kv("utilization gpu", "? not trustworthy on this part", S_WARN)
        out.append([
            ("        raw utilization.gpu reads ", S_DIM),
            (f"{fmt_pct(gpu.util_gpu, '-')}%", S_DIM),
            ("  while SM clock sits at ", S_DIM),
            (f"{fmt_pct(gpu.clock_percent)}%", S_DIM),
            (" of max --", S_DIM),
        ])
        out.append([
            ("        shown side by side deliberately; the clock ratio is NOT "
             "substituted for utilization.", S_DIM)
        ])
    kv("encoder / decoder", f"{fmt_pct(gpu.util_enc, na)}% / {fmt_pct(gpu.util_dec, na)}%")
    # temperature.gpu.tlimit is the margin to the thermal limit, not an
    # absolute temperature -- GB10 reports 36 while sitting at 59 C. Labelling
    # it "tlimit" next to the current temperature reads as a limit of 36 C.
    marg = (f"{gpu.temp_limit_c:.0f}C below throttle"
            if gpu.temp_limit_c is not None else "margin not reported")
    kv("temperature", f"{fmt_pct(gpu.temp_c, na)}C   ({marg})", temp_style(gpu.temp_c))
    kv("fan", f"{fmt_pct(gpu.fan_percent)}%" if gpu.fan_percent is not None
       else "not reported")
    kv("power draw / limit",
       f"{fmt_pct(gpu.power_w, na)}W / "
       f"{fmt_pct(gpu.power_limit_w, 'not reported')}"
       + ("W" if gpu.power_limit_w else ""))
    kv("clocks sm / max", f"{fmt_pct(gpu.clock_sm, na)} / {fmt_pct(gpu.clock_sm_max, na)} MHz")
    kv("clocks mem / video", f"{fmt_pct(gpu.clock_mem, na)} / {fmt_pct(gpu.clock_video, na)} MHz")
    if gpu.is_apple:
        kv("pstate", "not exposed on Apple silicon", S_DIM)
        kv("interconnect", "on-package unified memory fabric  (no PCIe link)")
    elif gpu.is_unified:
        kv("pstate", gpu.pstate + "   (pinned P0 on this part)")
        kv("interconnect", "C2C on-package  (PCIe gen/width readings are meaningless)")
    else:
        kv("pstate", gpu.pstate)
        kv("pcie", f"gen {gpu.pcie_gen}/{gpu.pcie_gen_max}  "
                   f"x{gpu.pcie_width}/{gpu.pcie_width_max}")
    if host.active_net_links:
        links = host.active_net_links
        up_rails = sum(1 for nl in links if nl.up)
        bad_mtu = any(
            nl.up and nl.rdma_dev and nl.mtu is not None and 0 < nl.mtu < 9000
            for nl in links
        )
        fstyle = S_WARN if (0 < up_rails < len(links) or bad_mtu) else S_NORMAL
        rails_s = ", ".join(
            f"{nl.name}{'/' + nl.rdma_dev if nl.rdma_dev else ''} "
            f"{(str(nl.speed_gbps) + 'G') if (nl.up and nl.speed_gbps) else nl.operstate}"
            f"{(' mtu=' + str(nl.mtu)) if nl.mtu else ''}"
            for nl in links
        )
        kv("cluster fabric", f"{host.hs_fabric_summary()}   [{rails_s}]", fstyle)
    if host.mem.disk_total_kib and host.mem.disk_avail_kib is not None:
        d_alerts = [a for a in host_alerts(host) if a.tag == "DISK"]
        d_style = (
            S_CRIT if any(a.level is Level.CRIT for a in d_alerts)
            else (S_WARN if d_alerts else S_NORMAL)
        )
        kv(
            "root disk (/)",
            f"{fmt_kib_g(host.mem.disk_avail_kib)} free of "
            f"{fmt_kib_g(host.mem.disk_total_kib)} "
            f"({(host.mem.disk_avail_frac or 0.0) * 100:.0f}% avail)",
            d_style,
        )
    kv("compute mode", gpu.compute_mode)
    kv("persistence", gpu.persistence)

    out.append(rule(width, g=g))
    out.append([("  clock event reasons", S_DIM)])
    active = {s for s, _, _ in gpu.throttle_badges}
    for bit, short, long, alarm in _all_bits():
        on = short in active
        style = (S_WARN if alarm else S_NORMAL) if on else S_DIM
        out.append([(f"    [{'x' if on else ' '}] {short:<8} {long}", style)])

    out.append(rule(width, g=g))
    stale = gpu.stale_procs
    proc_hdr: Line = [(f"  processes ({len(gpu.procs)})", S_DIM)]
    if stale:
        proc_hdr.append((f"   -- {len(stale)} stale idle allocation(s) [IDLE-HOLD]", S_WARN))
    if gpu.procs and selected_proc >= 0:
        proc_hdr.append(("   [Tab/n] select  [K] kill", S_DIM))
    out.append(proc_hdr)
    rows = [(host, gpu, p) for p in gpu.procs]
    out.extend(process_table(rows, width, DETAIL_PROC_LIMIT, selected=selected_proc))
    return clamp_lines(out, width)


DETAIL_PROC_LIMIT = 20


def _all_bits():
    from .model import CLOCK_EVENT_BITS
    return CLOCK_EVENT_BITS


def render_free(
    fleet: Fleet,
    width: int,
    min_mem_gib: float = 0.0,
    count: int = 1,
) -> list[Line]:
    """`gpumaster free` -- where should I send the next job?"""
    from .model import MEM_AVAIL_WARN_FRAC, gpu_free_mib, select_free_gpus

    out: list[Line] = []
    by_host = select_free_gpus(fleet, min_mem_gib=min_mem_gib, count=count)
    free = [g for gpus in by_host.values() for g in gpus]
    total_free_mib = 0.0
    for gpu in free:
        if gpu.is_unified:
            continue
        fr_mib = gpu_free_mib(gpu)
        if fr_mib is not None:
            total_free_mib += fr_mib

    filt = ""
    if min_mem_gib > 0.0 or count > 1:
        filt = f"   [filter: >= {max(1, count)}x {max(0.0, min_mem_gib):g} GiB]"
    out.append([("IDLE GPUS", S_HEAD),
                (f"   {len(free)} GPU(s), {total_free_mib/1024:.1f} GiB free VRAM{filt}", S_NORMAL)])
    hosts = {h.name: h for h in fleet.hosts}
    for name, gpus in by_host.items():
        h = hosts[name]
        idxs = ",".join(str(g.index) for g in gpus)
        gpu0 = gpus[0]
        if gpu0.is_unified:
            per = f"{fmt_kib_g(h.mem.available_kib)} unified headroom"
        else:
            fr = min((gpu_free_mib(g) or 0.0) for g in gpus)
            per = f"{fr/1024:.1f} GiB free each"
        load = f"ld {h.load1:.2f}" if h.load1 is not None else ""
        out.append([
            (f"  {name:<14}", S_ACCENT),
            (f"{idxs:<8}", S_NORMAL),
            (f"{short_gpu_name(gpu0.name):<16}", S_NORMAL),
            (f"{per:<26}", S_OK),
            (f"{load}", S_DIM),
        ])

    skipped = [
        h for h in fleet.hosts
        if h.online and (h.mem.available_frac or 1.0) < MEM_AVAIL_WARN_FRAC
    ]
    for h in skipped:
        idle_here = sum(1 for g in h.gpus if not g.busy)
        if idle_here:
            out.append([
                (f"  {h.name:<14}", S_DIM),
                (f"{idle_here} idle GPU(s) EXCLUDED -- host has only "
                 f"{(h.mem.available_frac or 0)*100:.0f}% RAM available", S_WARN),
            ])
    free_set = {(g.host, g.index) for g in free}
    for h in fleet.hosts:
        if not h.online:
            continue
        for g in h.gpus:
            if (h.name, g.index) in free_set:
                continue
            sp = g.stale_procs
            if sp:
                top_s = sp[0]
                out.append([
                    (f"  {h.name:<14}", S_DIM),
                    (f"#{g.index:<7}", S_WARN),
                    (f"IDLE-HOLD by PID {top_s.pid} ({top_s.user or '?'})"
                     f" -- {fmt_mib(top_s.gpu_mem_mib)} held for {fmt_etime(top_s.etime)}"
                     f" at 0% GPU", S_WARN),
                ])
    if not free:
        out.append([("  none", S_DIM)])
    return clamp_lines(out, width)


def lines_to_text(lines: list[Line]) -> list[str]:
    return ["".join(t for t, _ in line) for line in lines]


ANSI_SGR = {
    S_NORMAL: "",
    S_DIM: "\x1b[2;34m",
    S_HEAD: "\x1b[1;36m",
    S_OK: "\x1b[32m",
    S_WARN: "\x1b[33m",
    S_CRIT: "\x1b[1;31m",
    S_ACCENT: "\x1b[35m",
    S_SEL: "\x1b[30;46m",
}
ANSI_RESET = "\x1b[0m"


def lines_to_ansi(lines: list[Line]) -> list[str]:
    """Convert `(text, style)` spans into ANSI-colored terminal lines (`curl /fleet.ansi`)."""
    out: list[str] = []
    for line in lines:
        parts: list[str] = []
        for text, style in line:
            if not text:
                continue
            code = ANSI_SGR.get(style, "")
            if code:
                parts.append(f"{code}{text}{ANSI_RESET}")
            else:
                parts.append(text)
        out.append("".join(parts))
    return out


def _prom_esc(val: str) -> str:
    return (
        val.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def fleet_to_prometheus(fleet: Fleet) -> str:
    """Export fleet telemetry in Prometheus text exposition format (0.0.4).

    Metric families are grouped so `# HELP` and `# TYPE` appear once per family,
    and missing/untrusted readings (`None`, or GB10 utilization) are omitted
    rather than fabricated as 0.
    """
    families: list[tuple[str, str, str, list[str]]] = [
        ("gpumaster_host_online", "gauge", "1 if the host is reachable (streaming or gpu_dead), 0 if down/disabled.", []),
        ("gpumaster_host_load1", "gauge", "Host 1-minute load average.", []),
        ("gpumaster_host_psi_cpu_some10", "gauge", "Host Linux PSI CPU some avg10 percentage.", []),
        ("gpumaster_host_psi_memory_some10", "gauge", "Host Linux PSI memory some avg10 percentage.", []),
        ("gpumaster_host_memory_total_kib", "gauge", "Host total system memory in KiB.", []),
        ("gpumaster_host_memory_available_kib", "gauge", "Host available system memory in KiB.", []),
        ("gpumaster_host_disk_total_kib", "gauge", "Host root filesystem total space in KiB.", []),
        ("gpumaster_host_disk_available_kib", "gauge", "Host root filesystem available space in KiB.", []),
        ("gpumaster_host_uma_pool_total_mib", "gauge", "Unified memory host pool total in MiB.", []),
        ("gpumaster_host_uma_pool_available_mib", "gauge", "Unified memory host pool available in MiB.", []),
        ("gpumaster_net_link_up", "gauge", "1 if high-speed / RDMA network interface is up, 0 otherwise.", []),
        ("gpumaster_net_link_speed_gbps", "gauge", "High-speed / RDMA network link speed in Gbps.", []),
        ("gpumaster_net_link_rx_gbps", "gauge", "Live receive throughput on high-speed / RDMA link in Gbps.", []),
        ("gpumaster_net_link_tx_gbps", "gauge", "Live transmit throughput on high-speed / RDMA link in Gbps.", []),
        ("gpumaster_gpu_utilization_percent", "gauge", "Trustworthy GPU compute utilization percent (omitted on GB10).", []),
        ("gpumaster_gpu_memory_used_mib", "gauge", "Discrete GPU framebuffer memory used in MiB (omitted on unified GPUs).", []),
        ("gpumaster_gpu_memory_total_mib", "gauge", "Discrete GPU framebuffer memory total in MiB (omitted on unified GPUs).", []),
        ("gpumaster_gpu_resident_mib", "gauge", "GPU resident memory in MiB (framebuffer on discrete, pool subset on unified).", []),
        ("gpumaster_gpu_temperature_celsius", "gauge", "GPU temperature in degrees Celsius.", []),
        ("gpumaster_gpu_power_watts", "gauge", "GPU power draw in watts.", []),
        ("gpumaster_gpu_ane_power_watts", "gauge", "Apple Neural Engine power draw in watts.", []),
        ("gpumaster_gpu_clock_sm_mhz", "gauge", "GPU SM / shader clock in MHz.", []),
        ("gpumaster_gpu_stale_processes", "gauge", "Count of stale idle GPU allocations (IDLE-HOLD) on this GPU.", []),
    ]
    by_name = {name: samples for name, _, _, samples in families}

    for h in fleet.hosts:
        hl = f'host="{_prom_esc(h.name)}"'
        st = _prom_esc(h.state.name.lower())
        by_name["gpumaster_host_online"].append(
            f'gpumaster_host_online{{{hl},state="{st}"}} {1 if h.online else 0}'
        )
        if h.load1 is not None:
            by_name["gpumaster_host_load1"].append(f"gpumaster_host_load1{{{hl}}} {h.load1}")
        if h.psi_cpu.some10 is not None:
            by_name["gpumaster_host_psi_cpu_some10"].append(
                f"gpumaster_host_psi_cpu_some10{{{hl}}} {h.psi_cpu.some10}"
            )
        if h.psi_mem.some10 is not None:
            by_name["gpumaster_host_psi_memory_some10"].append(
                f"gpumaster_host_psi_memory_some10{{{hl}}} {h.psi_mem.some10}"
            )
        if h.mem.total_kib is not None:
            by_name["gpumaster_host_memory_total_kib"].append(
                f"gpumaster_host_memory_total_kib{{{hl}}} {h.mem.total_kib}"
            )
        if h.mem.available_kib is not None:
            by_name["gpumaster_host_memory_available_kib"].append(
                f"gpumaster_host_memory_available_kib{{{hl}}} {h.mem.available_kib}"
            )
        if h.mem.disk_total_kib is not None:
            by_name["gpumaster_host_disk_total_kib"].append(
                f"gpumaster_host_disk_total_kib{{{hl}}} {h.mem.disk_total_kib}"
            )
        if h.mem.disk_avail_kib is not None:
            by_name["gpumaster_host_disk_available_kib"].append(
                f"gpumaster_host_disk_available_kib{{{hl}}} {h.mem.disk_avail_kib}"
            )
        pool = h.uma_pool()
        if pool is not None:
            _res, tot_mib, avail_mib, _other = pool
            by_name["gpumaster_host_uma_pool_total_mib"].append(
                f"gpumaster_host_uma_pool_total_mib{{{hl}}} {tot_mib:.2f}"
            )
            by_name["gpumaster_host_uma_pool_available_mib"].append(
                f"gpumaster_host_uma_pool_available_mib{{{hl}}} {avail_mib:.2f}"
            )
        for nl in h.net_links:
            nlabel = (
                f'{hl},interface="{_prom_esc(nl.name)}",'
                f'rdma_dev="{_prom_esc(nl.rdma_dev)}"'
            )
            by_name["gpumaster_net_link_up"].append(
                f"gpumaster_net_link_up{{{nlabel}}} {1 if nl.up else 0}"
            )
            if nl.speed_gbps is not None:
                by_name["gpumaster_net_link_speed_gbps"].append(
                    f"gpumaster_net_link_speed_gbps{{{nlabel}}} {nl.speed_gbps}"
                )
            if nl.rx_gbps is not None:
                by_name["gpumaster_net_link_rx_gbps"].append(
                    f"gpumaster_net_link_rx_gbps{{{nlabel}}} {nl.rx_gbps:.4f}"
                )
            if nl.tx_gbps is not None:
                by_name["gpumaster_net_link_tx_gbps"].append(
                    f"gpumaster_net_link_tx_gbps{{{nlabel}}} {nl.tx_gbps:.4f}"
                )
        for g in h.gpus:
            gl = (
                f'{hl},index="{g.index}",name="{_prom_esc(g.name)}",'
                f'uuid="{_prom_esc(g.uuid)}"'
            )
            if g.util_trustworthy and g.util_gpu is not None:
                by_name["gpumaster_gpu_utilization_percent"].append(
                    f"gpumaster_gpu_utilization_percent{{{gl}}} {g.util_gpu}"
                )
            if not g.is_unified:
                if g.mem_used_mib is not None:
                    by_name["gpumaster_gpu_memory_used_mib"].append(
                        f"gpumaster_gpu_memory_used_mib{{{gl}}} {g.mem_used_mib}"
                    )
                if g.mem_total_mib is not None:
                    by_name["gpumaster_gpu_memory_total_mib"].append(
                        f"gpumaster_gpu_memory_total_mib{{{gl}}} {g.mem_total_mib}"
                    )
            if g.gpu_resident_mib is not None:
                mk = _prom_esc(g.mem_kind.value)
                by_name["gpumaster_gpu_resident_mib"].append(
                    f'gpumaster_gpu_resident_mib{{{gl},memory_kind="{mk}"}} {g.gpu_resident_mib}'
                )
            if g.temp_c is not None:
                by_name["gpumaster_gpu_temperature_celsius"].append(
                    f"gpumaster_gpu_temperature_celsius{{{gl}}} {g.temp_c}"
                )
            if g.power_w is not None:
                by_name["gpumaster_gpu_power_watts"].append(
                    f"gpumaster_gpu_power_watts{{{gl}}} {g.power_w}"
                )
            if g.ane_power_w is not None:
                by_name["gpumaster_gpu_ane_power_watts"].append(
                    f"gpumaster_gpu_ane_power_watts{{{gl}}} {g.ane_power_w}"
                )
            if g.clock_sm is not None:
                by_name["gpumaster_gpu_clock_sm_mhz"].append(
                    f"gpumaster_gpu_clock_sm_mhz{{{gl}}} {g.clock_sm}"
                )
            by_name["gpumaster_gpu_stale_processes"].append(
                f"gpumaster_gpu_stale_processes{{{gl}}} {len(g.stale_procs)}"
            )

    lines_out: list[str] = []
    for name, mtype, help_text, samples in families:
        if not samples:
            continue
        lines_out.append(f"# HELP {name} {help_text}")
        lines_out.append(f"# TYPE {name} {mtype}")
        lines_out.extend(samples)
    return "\n".join(lines_out) + "\n"
