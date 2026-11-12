"""Terminal UI renderer for TokenMaster (Rich-style Unicode tables, progress bars, and live watch)."""

from __future__ import annotations

import math
import os
import re
import shutil
import socket
import sys
import unicodedata
from datetime import datetime
from typing import Any, Iterable, Optional

from tokenmaster.models import ProviderReport

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_ZERO_WIDTH_CHARS = frozenset({"\u200b", "\u200c", "\u200d", "\ufe0e", "\ufe0f"})


def _supports_color(stream: Any = None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    target = sys.stdout if stream is None else stream
    return hasattr(target, "isatty") and bool(target.isatty())


def _sanitize_inline(text: str) -> str:
    """Replace raw control characters (newlines/tabs) with spaces so single-line cells never break."""
    if not text:
        return ""
    if "\n" in text or "\r" in text or "\t" in text:
        return re.sub(r"[\r\n\t]+", " ", text).strip()
    return text


class Style:
    """ANSI color helper with automatic fallback when NO_COLOR is set."""

    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"
    REVERSE = "\x1b[7m"
    RED = "\x1b[38;5;203m"
    GREEN = "\x1b[38;5;78m"
    YELLOW = "\x1b[38;5;221m"
    BLUE = "\x1b[38;5;75m"
    MAGENTA = "\x1b[38;5;177m"
    CYAN = "\x1b[38;5;81m"
    GRAY = "\x1b[38;5;245m"
    TRACK = "\x1b[38;5;239m"
    WHITE = "\x1b[97m"

    @classmethod
    def wrap(cls, text: str, *codes: str, enabled: Optional[bool] = None) -> str:
        use_color = _supports_color() if enabled is None else enabled
        if not use_color or not codes:
            return text
        return "".join(codes) + text + cls.RESET


def char_width(ch: str) -> int:
    """Return terminal column width (0, 1, or 2) of a Unicode character (or string)."""
    if not ch:
        return 0
    if len(ch) != 1:
        return sum(char_width(c) for c in ch)
    if ch in _ZERO_WIDTH_CHARS:
        return 0
    cat = unicodedata.category(ch)
    if cat.startswith(("M", "C")):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def visible_len(s: str) -> int:
    """Return visible terminal column width ignoring ANSI escape sequences."""
    plain = ANSI_RE.sub("", s)
    return sum(char_width(ch) for ch in plain)


def _finish_truncation(
    out: list[str], ellipsis: str, use_ellipsis: bool, has_active_ansi: bool
) -> str:
    if use_ellipsis:
        out.append(ellipsis)
    if has_active_ansi:
        out.append(Style.RESET)
    return "".join(out)


def truncate_visible(s: str, max_width: int, ellipsis: str = "…") -> str:
    """Truncate an ANSI-styled string to at most ``max_width`` visible columns."""
    if max_width <= 0:
        return ""
    if visible_len(s) <= max_width:
        return s

    ell_w = visible_len(ellipsis)
    use_ellipsis = max_width > ell_w
    budget = (max_width - ell_w) if use_ellipsis else max_width

    out: list[str] = []
    pending_ansi: list[str] = []
    used = 0
    has_active_ansi = False
    has_base_char = False

    def _consume_text_segment(segment: str) -> bool:
        nonlocal used, has_active_ansi, has_base_char
        for ch in segment:
            cw = char_width(ch)
            if cw == 0:
                # Only attach combining marks (M*) to an already-emitted base character
                # within the same styled span; skip zero-width joiners/selectors at the boundary.
                if (
                    has_base_char
                    and not pending_ansi
                    and unicodedata.category(ch).startswith("M")
                ):
                    out.append(ch)
                continue
            if used + cw > budget:
                return False
            if pending_ansi:
                for seq in pending_ansi:
                    out.append(seq)
                    if seq in (Style.RESET, "\x1b[m"):
                        has_active_ansi = False
                    elif seq.endswith("m"):
                        has_active_ansi = True
                pending_ansi.clear()
            out.append(ch)
            used += cw
            has_base_char = True
        return True

    pos = 0
    for match in ANSI_RE.finditer(s):
        start, end = match.span()
        if start > pos:
            if not _consume_text_segment(s[pos:start]):
                return _finish_truncation(out, ellipsis, use_ellipsis, has_active_ansi)
        pending_ansi.append(match.group(0))
        pos = end

    if pos < len(s):
        _consume_text_segment(s[pos:])

    return _finish_truncation(out, ellipsis, use_ellipsis, has_active_ansi)


def pad_cell(s: str, width: int, align: str = "left", truncate: bool = False) -> str:
    """Pad (and optionally truncate) an ANSI-styled string to an exact visible width."""
    if width <= 0:
        return "" if truncate else s
    if truncate and visible_len(s) > width:
        s = truncate_visible(s, width)
    vlen = visible_len(s)
    if vlen >= width:
        return s
    pad = " " * (width - vlen)
    if align == "right":
        return pad + s
    if align == "center":
        left = (width - vlen) // 2
        right = width - vlen - left
        return (" " * left) + s + (" " * right)
    return s + pad


def _fit_col_widths(col_widths: list[int], max_total_width: int) -> list[int]:
    """Shrink column widths to fit within ``max_total_width`` terminal columns."""
    n_cols = len(col_widths)
    if n_cols == 0:
        return []
    border_overhead = 3 * n_cols + 1
    avail = max(n_cols, max_total_width - border_overhead)
    if sum(col_widths) <= avail:
        return list(col_widths)

    fitted = list(col_widths)
    excess = sum(fitted) - avail
    # Phase 1: shrink wide detail columns (especially Weekly Remaining, idx=2) down toward 20 cols first
    # so compact columns (Provider, Tier, Reset, Credit) are not prematurely truncated on 80-115 col terminals.
    if n_cols > 2 and fitted[2] > 20 and excess > 0:
        cut = min(excess, fitted[2] - 20)
        fitted[2] -= cut
        excess -= cut

    # Soft minimums per column: Provider (11), Tier (6), Weekly Remaining (16), Reset (8), Credit (7)
    soft_mins = [11, 6, 16, 8, 7]
    if len(soft_mins) < n_cols:
        soft_mins.extend([6] * (n_cols - len(soft_mins)))
    # Shrink priority order: Weekly Remaining (2), Reset (3), Credit (4), Tier (1), Provider (0)
    base_order = (2, 3, 4, 1, 0)
    shrink_order = [i for i in base_order if i < n_cols] + [
        i for i in range(n_cols) if i not in base_order
    ]

    while excess > 0:
        progressed = False
        for idx in shrink_order:
            if excess <= 0:
                break
            floor = soft_mins[idx]
            if fitted[idx] > floor:
                step = min(excess, max(1, (fitted[idx] - floor + 1) // 2))
                fitted[idx] -= step
                excess -= step
                progressed = True
        if not progressed:
            break

    # Hard fallback if terminal is narrower than soft minimums: shrink down toward 1 col/cell
    while excess > 0:
        progressed = False
        for idx in shrink_order:
            if excess <= 0:
                break
            if fitted[idx] > 1:
                fitted[idx] -= 1
                excess -= 1
                progressed = True
        if not progressed:
            break

    return fitted


def _expand_col_widths(col_widths: list[int], target_total_width: int) -> list[int]:
    """Expand column widths proportionally so the table spans ``target_total_width`` columns."""
    n_cols = len(col_widths)
    if n_cols == 0:
        return []
    border_overhead = 3 * n_cols + 1
    avail = target_total_width - border_overhead
    curr_sum = sum(col_widths)
    if avail <= curr_sum:
        return list(col_widths)

    slack = avail - curr_sum
    expanded = list(col_widths)
    # Proportional expansion weights across the 5 columns:
    # Provider (20%), Tier / Source (15%), Weekly Remaining (40%), Reset (10%), Credit Remaining (15%)
    weights = [0.20, 0.15, 0.40, 0.10, 0.15]
    if len(weights) < n_cols:
        weights.extend([1.0 / n_cols] * (n_cols - len(weights)))
    total_w = sum(weights[:n_cols]) or 1.0

    allocated = 0
    for i in range(n_cols):
        add = int(slack * (weights[i] / total_w))
        expanded[i] += add
        allocated += add

    rem = slack - allocated
    order = [i for i in (2, 0, 1, 4, 3) if i < n_cols] + [
        i for i in range(n_cols) if i not in (2, 0, 1, 4, 3)
    ]
    idx = 0
    while rem > 0 and order:
        expanded[order[idx % len(order)]] += 1
        rem -= 1
        idx += 1

    return expanded


def _detect_terminal_size(stream: Any = None) -> tuple[Optional[int], Optional[int]]:
    """Return live ``(columns, lines)`` from the controlling TTY via ``os.get_terminal_size``.

    Queries the underlying file descriptors directly (ignoring potentially stale ``$COLUMNS`` /
    ``$LINES`` environment variables) so resizing a terminal session is reflected immediately.
    """
    target = sys.stdout if stream is None else stream
    try:
        if not (hasattr(target, "isatty") and target.isatty()):
            return None, None
        fds: list[int] = []
        for candidate in (
            target,
            sys.stdout,
            sys.stdin,
            sys.stderr,
            getattr(sys, "__stdout__", None),
            getattr(sys, "__stdin__", None),
            getattr(sys, "__stderr__", None),
        ):
            if candidate is not None and hasattr(candidate, "fileno"):
                try:
                    fd = int(candidate.fileno())
                    if fd >= 0 and fd not in fds:
                        fds.append(fd)
                except Exception:
                    pass
        for fallback_fd in (1, 0, 2):
            if fallback_fd not in fds:
                fds.append(fallback_fd)
        for fd in fds:
            try:
                sz = os.get_terminal_size(fd)
                if sz.columns > 0 and sz.lines > 0:
                    return sz.columns, sz.lines
            except OSError:
                continue
        if hasattr(os, "O_NOCTTY"):
            tty_fd = -1
            try:
                tty_fd = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)
                sz = os.get_terminal_size(tty_fd)
                if sz.columns > 0 and sz.lines > 0:
                    return sz.columns, sz.lines
            except OSError:
                pass
            finally:
                if tty_fd >= 0:
                    try:
                        os.close(tty_fd)
                    except OSError:
                        pass
    except Exception:
        pass
    return None, None


def render_progress_bar(
    used_percent: Optional[float],
    width: int = 10,
    color_enabled: Optional[bool] = None,
) -> str:
    """Render a crisp horizontal gauge bar colored by utilization."""
    if used_percent is None:
        return Style.wrap("─" * width, Style.TRACK, enabled=color_enabled)

    pct = max(0.0, min(100.0, float(used_percent)))
    filled = int(round((pct / 100.0) * width))
    filled = max(0, min(width, filled))
    empty = width - filled

    if pct >= 85.0:
        bar_color = Style.RED
    elif pct >= 60.0:
        bar_color = Style.YELLOW
    else:
        bar_color = Style.GREEN

    filled_part = (
        Style.wrap("━" * filled, bar_color, Style.BOLD, enabled=color_enabled) if filled else ""
    )
    empty_part = Style.wrap("─" * empty, Style.TRACK, enabled=color_enabled) if empty else ""
    return filled_part + empty_part


def render_remaining_bar(
    remaining_percent: Optional[float],
    width: int = 10,
    color_enabled: Optional[bool] = None,
) -> str:
    """Render a crisp horizontal gauge bar representing remaining quota (battery style)."""
    if remaining_percent is None:
        return Style.wrap("─" * width, Style.TRACK, enabled=color_enabled)

    pct = max(0.0, min(100.0, float(remaining_percent)))
    filled = int(round((pct / 100.0) * width))
    filled = max(0, min(width, filled))
    empty = width - filled

    if pct <= 15.0:
        bar_color = Style.RED
    elif pct <= 40.0:
        bar_color = Style.YELLOW
    else:
        bar_color = Style.GREEN

    filled_part = (
        Style.wrap("━" * filled, bar_color, Style.BOLD, enabled=color_enabled) if filled else ""
    )
    empty_part = Style.wrap("─" * empty, Style.TRACK, enabled=color_enabled) if empty else ""
    return filled_part + empty_part


def format_status_dot(status: str, color_enabled: Optional[bool] = None) -> str:
    """Return a 1-column color-coded status indicator dot/icon."""
    if status == "ok":
        return Style.wrap("●", Style.GREEN, Style.BOLD, enabled=color_enabled)
    if status == "partial":
        return Style.wrap("◐", Style.YELLOW, Style.BOLD, enabled=color_enabled)
    if status == "error":
        return Style.wrap("✖", Style.RED, Style.BOLD, enabled=color_enabled)
    return Style.wrap("○", Style.GRAY, enabled=color_enabled)


def format_status_badge(status: str, color_enabled: Optional[bool] = None) -> str:
    if status == "ok":
        return Style.wrap("● LIVE", Style.GREEN, Style.BOLD, enabled=color_enabled)
    if status == "partial":
        return Style.wrap("◐ LOCAL", Style.YELLOW, Style.BOLD, enabled=color_enabled)
    if status == "error":
        return Style.wrap("✖ ERROR", Style.RED, Style.BOLD, enabled=color_enabled)
    return Style.wrap("○ SETUP", Style.GRAY, enabled=color_enabled)


def _format_latency_ms(ms: float) -> str:
    if round(ms) >= 1000:
        return f"{ms / 1000.0:.2f}s"
    return f"{round(ms):.0f}ms"


def _valid_latency(val: Optional[float]) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0.0:
        return None
    return f


def _style_pct_color(pct: Optional[float]) -> str:
    if pct is None:
        return Style.GREEN
    if pct <= 15.0:
        return Style.RED
    if pct <= 40.0:
        return Style.YELLOW
    return Style.GREEN


def _clamp_display_pct_1d(pct: float) -> float:
    """Round ``pct`` to 1 decimal place without falsely claiming 100.0% when < 100% or 0.0% when > 0%."""
    r1 = round(float(pct), 1)
    if pct < 100.0 and r1 >= 100.0:
        return 99.9
    if pct > 0.0 and r1 <= 0.0:
        return 0.1
    return r1


def _fmt_pct_1d(pct: float, *, width: int = 0) -> str:
    r1 = _clamp_display_pct_1d(pct)
    return f"{r1:{width}.1f}%" if width > 0 else f"{r1:.1f}%"


def _fmt_pct_compact(pct: float) -> str:
    r1 = _clamp_display_pct_1d(pct)
    return f"{int(r1)}%" if float(r1).is_integer() else f"{r1:.1f}%"


def _fmt_pct_int(pct: float) -> str:
    if pct >= 100.0:
        return "100%"
    if pct <= 0.0:
        return "0%"
    ri = int(round(float(pct)))
    ri = max(1, min(99, ri))
    return f"{ri}%"


_PAREN_STRIP_RE = re.compile(r"\s*(?:\([^()]*\)|（[^（）]*）)")
_TIER_VENDOR_PREFIX_RE = re.compile(
    r"^(?:google\s+one\s+ai\b|google\s+ai\b|antigravity\b|chatgpt\b|openai\b|claude\b|anthropic\b|cursor\b|openrouter\b|super\s*grok(?=\b|pro\b|ultra\b|max\b|plus\b|free\b)|grok(?=\b|pro\b|ultra\b|max\b|plus\b|free\b)|xai\b|ai\b)\s*[-:]?\s*",
    re.IGNORECASE,
)


def _clean_tier_label(raw_tier: Optional[str]) -> str:
    """Strip redundant vendor prefixes (ChatGPT, Claude, Google AI, Grok, Cursor) from plan tier labels."""
    s = _sanitize_inline(raw_tier or "").strip()
    if not s or s == "—":
        return "—"
    cleaned = _TIER_VENDOR_PREFIX_RE.sub("", s).strip()
    if cleaned:
        return cleaned
    if s.lower() == "supergrok":
        return "Super"
    return s


def _split_provider_and_account(r: ProviderReport, *, is_multi: bool) -> tuple[str, Optional[str]]:
    """Split a report into ``(base_display_name, account_pill_or_None)``.

    1. Strips a trailing ``[acct]`` or ``(acct)`` suffix when it matches ``r.account`` (for
       named subscriptions) or ``default`` (when ``is_multi`` is True), so non-account brackets
       inside ``display_name`` (such as ``Claude [Max]``) are never misparsed as account tags.
    2. Removes parenthetical vendor annotations ``(...)`` / ``（...）`` (such as ``(OpenAI)``,
       ``(Anthropic)``, ``(Google)``, ``(CLI / SuperGrok)``) from ``base_display_name``.
    """
    disp_name = _sanitize_inline(r.display_name)
    raw_acct = _sanitize_inline(str(r.account)).strip() if r.account is not None else ""
    acct = raw_acct if (raw_acct and raw_acct.lower() != "default") else None

    target_tag: Optional[str] = acct if acct is not None else ("default" if is_multi else None)
    if target_tag is not None:
        for open_ch, close_ch in (("[", "]"), ("(", ")")):
            suffix = f" {open_ch}{target_tag}{close_ch}"
            if len(disp_name) > len(suffix) and disp_name[-len(suffix) :].lower() == suffix.lower():
                disp_name = disp_name[: -len(suffix)].rstrip()
                break

    stripped_base = _PAREN_STRIP_RE.sub("", disp_name).strip()
    if stripped_base:
        disp_name = stripped_base

    return disp_name, target_tag


def _format_report_label(r: ProviderReport, *, is_multi: bool) -> str:
    """Return a disambiguated provider label (appending ``[default]`` or ``[account]`` when needed)."""
    base_name, acct_tag = _split_provider_and_account(r, is_multi=is_multi)
    if acct_tag:
        return f"{base_name} [{acct_tag}]"
    return base_name


def _row_health_color(r: ProviderReport) -> str:
    """Return Red / Yellow / Green (or Gray when unconfigured) for a provider row's overall health."""
    if r.status == "unconfigured":
        return Style.GRAY
    if r.status == "error":
        return Style.RED

    rem_cands: list[float] = []
    for cand in (
        _finite_pct_opt(r.weekly.remaining_percent),
        _finite_pct_opt(r.weekly.scoped_remaining_percent),
    ):
        if cand is not None:
            rem_cands.append(cand)
    p_used = _finite_pct_opt(r.weekly.pace_used_percent)
    if p_used is not None:
        rem_cands.append(max(0.0, 100.0 - p_used))
    if not rem_cands:
        fh_rem = _extract_5h_remaining(r)
        if fh_rem is not None:
            rem_cands.append(fh_rem)
    if rem_cands:
        return _style_pct_color(min(rem_cands))

    c_rem_pct = _finite_pct_opt(r.credits.remaining_percent)
    if c_rem_pct is not None:
        return _style_pct_color(c_rem_pct)
    c_rem_val = _finite_float_opt(r.credits.remaining)
    if c_rem_val is not None:
        return Style.RED if c_rem_val <= 0.0 else Style.GREEN
    if r.status == "partial":
        return Style.YELLOW
    return Style.GREEN


def _format_provider_cell(
    r: ProviderReport,
    *,
    is_multi: bool,
    is_best: bool,
    is_active_env: bool,
    use_color: bool,
) -> str:
    """Format the Provider column cell with status dot, color-coded provider title, and subscription badges."""
    base_name, acct_tag = _split_provider_and_account(r, is_multi=is_multi)
    status_dot = format_status_dot(r.status, color_enabled=use_color)
    row_color = _row_health_color(r)

    styled_base = Style.wrap(base_name, Style.BOLD, row_color, enabled=use_color)
    parts = [f"{status_dot} {styled_base}"]

    if acct_tag:
        parts.append(Style.wrap(f"[{acct_tag}]", Style.BOLD, row_color, enabled=use_color))

    if is_multi and is_best:
        parts.append(Style.wrap("★", Style.BOLD, Style.GREEN, enabled=use_color))
    if is_active_env:
        parts.append(Style.wrap("▸", Style.BOLD, Style.CYAN, enabled=use_color))

    return " ".join(parts)


def _normalize_active_profile_env(raw_val: Optional[str]) -> str:
    raw = (raw_val or "").strip()
    if not raw:
        return ""
    if ":" in raw:
        pid, acct = raw.split(":", 1)
        pid_c = pid.strip().lower()
        acct_c = acct.strip().lower() or "default"
        return f"{pid_c}:{acct_c}" if pid_c else ""
    return f"{raw.lower()}:default"


_HEADER_SHORT_NAMES: dict[str, str] = {
    "codex": "Codex",
    "claude": "Claude",
    "antigravity": "Antigravity",
    "grok": "Grok",
    "cursor": "Cursor",
    "openrouter": "OpenRouter",
}


def _short_header_provider_label(r: ProviderReport, *, is_multi: bool) -> str:
    pid_low = (r.provider_id or "").strip().lower()
    base = _HEADER_SHORT_NAMES.get(pid_low)
    if not base:
        base, _ = _split_provider_and_account(r, is_multi=is_multi)
        base = base.split("(", 1)[0].strip() or base
    if is_multi:
        acct_tag = _sanitize_inline(str(r.account or "default")).strip() or "default"
        return f"{base}:{acct_tag}"
    return base


def _compose_thermalmaster_header(
    report_list: list[ProviderReport],
    *,
    provider_groups: dict[str, list[ProviderReport]],
    width: int,
    watch_interval: Optional[int],
    use_color: bool,
) -> str:
    """Compose a ``thermalmaster``-style top header bar flush across ``width`` columns.

    Left side: reverse-video bold `` tokenmaster `` pill.
    Right side: ``<live>/<total> LIVE`` + ``Fetched in <latency>`` + ``every <N>s`` + ``HH:MM:SS``.
    """
    _ = provider_groups
    left_s = Style.wrap(" tokenmaster ", Style.BOLD, Style.REVERSE, enabled=use_color)

    total_cnt = len(report_list)
    live_cnt = sum(1 for r in report_list if r.status == "ok")
    live_badge = Style.wrap(f"{live_cnt}/{total_cnt} LIVE", Style.GRAY, enabled=use_color)

    timings = [lat for r in report_list if (lat := _valid_latency(r.fetch_ms)) is not None]
    fetch_badge: Optional[str] = (
        Style.wrap(f"Fetched in {_format_latency_ms(max(timings))}", Style.GRAY, enabled=use_color)
        if timings
        else None
    )
    every_badge: Optional[str] = (
        Style.wrap(f"every {watch_interval}s", Style.GRAY, enabled=use_color)
        if watch_interval
        else None
    )
    clock_badge = Style.wrap(datetime.now().strftime("%H:%M:%S"), Style.GRAY, enabled=use_color)

    ordered_slots: list[tuple[str, str]] = [("live", live_badge)]
    if fetch_badge:
        ordered_slots.append(("fetch", fetch_badge))
    if every_badge:
        ordered_slots.append(("every", every_badge))
    ordered_slots.append(("clock", clock_badge))

    drop_priority = ("fetch", "every", "live", "clock")
    active_slots = list(ordered_slots)
    right_pad = "  "
    target_w = max(visible_len(left_s) + 2 + len(right_pad), int(width))

    while True:
        right_s = "  ".join(val for _, val in active_slots) + right_pad
        gap = target_w - visible_len(left_s) - visible_len(right_s)
        if gap >= 2 or not active_slots:
            line = left_s + (" " * max(2, gap)) + right_s if active_slots else left_s
            return truncate_visible(line, width) if width > 0 and visible_len(line) > width else line
        dropped = False
        for drop_key in drop_priority:
            for idx, (k, _) in enumerate(active_slots):
                if k == drop_key and len(active_slots) > 1:
                    active_slots.pop(idx)
                    dropped = True
                    break
            if dropped:
                break
        if not dropped:
            combined = left_s + ("  " + right_s if active_slots else "")
            return truncate_visible(combined, width) if width > 0 else combined


def _build_weekly_detail_badges(
    r: ProviderReport,
    *,
    use_color: bool,
    include_scoped: bool = True,
    include_5h: bool = True,
) -> list[str]:
    """Build inline secondary pool & sub-window badges for the ``Weekly Remaining`` column."""
    badges: list[str] = []

    def _fmt_badge(label: str, rem_pct: float) -> str:
        col = _style_pct_color(rem_pct)
        return Style.wrap(
            f"{label}: {_fmt_pct_compact(rem_pct)}",
            col,
            Style.BOLD,
            enabled=use_color,
        )

    def _sm_rem_pct(sm: Any) -> Optional[float]:
        u_pct = _finite_pct_opt(getattr(sm, "used_percent", None))
        if u_pct is not None:
            return max(0.0, min(100.0, 100.0 - u_pct))
        return None

    pid = (r.provider_id or "").strip().lower()

    if pid == "antigravity":
        agy_map = (
            ("gemini weekly", "Gem 7d"),
            ("gemini 5h", "Gem 5h"),
            ("claude/gpt weekly", "CL/GPT 7d"),
            ("claude/gpt 5h", "CL/GPT 5h"),
            ("other models weekly", "Other 7d"),
            ("other models 5h", "Other 5h"),
        )
        by_name = {
            _sanitize_inline(sm.name).strip().lower(): sm
            for sm in r.sub_metrics
            if getattr(sm, "name", None)
        }
        for key_low, short_lbl in agy_map:
            sm_match = by_name.get(key_low)
            if sm_match is not None:
                rem_v = _sm_rem_pct(sm_match)
                if rem_v is not None:
                    badges.append(_fmt_badge(short_lbl, rem_v))
        if badges:
            return badges

    if pid == "cursor":
        cursor_map = (
            ("auto model bucket", "Auto"),
            ("named/api model bucket", "API"),
        )
        by_name = {
            _sanitize_inline(sm.name).strip().lower(): sm
            for sm in r.sub_metrics
            if getattr(sm, "name", None)
        }
        for key_low, short_lbl in cursor_map:
            sm_match = by_name.get(key_low)
            if sm_match is not None:
                rem_v = _sm_rem_pct(sm_match)
                if rem_v is not None:
                    badges.append(_fmt_badge(short_lbl, rem_v))
        if include_5h:
            fh_rem = _extract_5h_remaining(r)
            if fh_rem is not None:
                badges.append(_fmt_badge("5h", fh_rem))
        return badges

    if pid == "grok":
        for sm in r.sub_metrics:
            sm_name_low = _sanitize_inline(sm.name or "").strip().lower()
            rem_v = _sm_rem_pct(sm)
            if rem_v is None:
                continue
            if "cursor pool" in sm_name_low:
                badges.append(_fmt_badge("Cursor", rem_v))
            elif sm_name_low.startswith("grok web rate limit (") and sm_name_low.endswith(")"):
                win_lbl = _sanitize_inline(sm.name)[len("Grok Web Rate Limit (") : -1].strip()
                if win_lbl:
                    badges.append(_fmt_badge(win_lbl, rem_v))
        if include_5h:
            fh_rem = _extract_5h_remaining(r)
            if fh_rem is not None:
                badges.append(_fmt_badge("5h", fh_rem))
        return badges

    scoped_pct = _finite_pct_opt(r.weekly.scoped_remaining_percent)
    scoped_lbl = _sanitize_inline(r.weekly.scoped_label or "Fable")
    if include_scoped and scoped_pct is not None:
        badges.append(_fmt_badge(scoped_lbl, scoped_pct))

    # Surface any additional rate-limit / model-scoped weekly pools in sub_metrics
    for sm in r.sub_metrics:
        sm_name = _sanitize_inline(sm.name or "").strip()
        sm_low = sm_name.lower()
        rem_v = _sm_rem_pct(sm)
        if rem_v is None:
            continue
        if sm_low.startswith("7d pool (") and sm_name.endswith(")"):
            inner_lbl = sm_name[len("7d Pool (") : -1].strip()
            if inner_lbl and inner_lbl.lower() != scoped_lbl.lower():
                badges.append(_fmt_badge(inner_lbl, rem_v))
        elif sm_low == "code review pool" and rem_v < 100.0:
            badges.append(_fmt_badge("Review", rem_v))

    if include_5h:
        fh_rem = _extract_5h_remaining(r)
        if fh_rem is not None:
            badges.append(_fmt_badge("5h", fh_rem))

    return badges


def _fit_weekly_cell_to_width(cell: str, target_width: int) -> str:
    """Progressively compact internal spacing/labels in a ``Weekly Remaining`` cell before truncating."""
    if target_width <= 0 or visible_len(cell) <= target_width:
        return cell
    cand = re.sub(r"Total:\s+", "Total: ", cell)
    if visible_len(cand) <= target_width:
        return cand
    cand = cand.replace(" · ", " ")
    if visible_len(cand) <= target_width:
        return cand
    for old_lbl, new_lbl in (
        ("CL/GPT ", "C/G "),
        ("Gem ", "G "),
        ("Other ", "Oth "),
        ("Cursor: ", "Cur:"),
        ("Fable: ", "Fab:"),
        ("Auto: ", "Aut:"),
        ("API: ", "API:"),
        ("Total: ", "Tot:"),
        (": ", ":"),
    ):
        cand = cand.replace(old_lbl, new_lbl)
        if visible_len(cand) <= target_width:
            return cand
    return cand


def _compose_watch_footer(width: int, watch_interval: int, *, use_color: bool) -> str:
    """Compose a ``thermalmaster``-style dim bottom status bar for full-screen watch mode."""
    left_txt = f"  tokenmaster -w {watch_interval}s  •  Ctrl+C quit"
    right_txt = "tokenmaster pick/exec <provider>:auto  •  tokenmaster history  "
    if width <= 0:
        return Style.wrap(left_txt + "  •  " + right_txt.strip(), Style.GRAY, enabled=use_color)
    gap = width - visible_len(left_txt) - visible_len(right_txt)
    if gap >= 2:
        full_line = left_txt + (" " * gap) + right_txt
    else:
        full_line = truncate_visible(left_txt, width)
    return Style.wrap(full_line, Style.GRAY, enabled=use_color)


def render_dashboard(
    reports: Iterable[ProviderReport],
    show_detail: bool = False,
    watch_interval: Optional[int] = None,
    color_enabled: Optional[bool] = None,
    max_width: Optional[int] = None,
    screen_height: Optional[int] = None,
) -> str:
    """Render the full TokenMaster summary table and optional sub-metric cards."""
    from tokenmaster.cli import select_best_account
    from tokenmaster.models import parse_reset_remaining_seconds

    use_color = _supports_color() if color_enabled is None else color_enabled
    report_list = [r.finalize() for r in reports]
    lines: list[str] = []

    effective_max_width: Optional[int] = None
    if max_width is not None:
        try:
            if int(max_width) > 0:
                effective_max_width = int(max_width)
        except (TypeError, ValueError):
            pass
    if effective_max_width is None:
        env_w = os.environ.get("TOKENMASTER_MAX_WIDTH")
        if env_w:
            try:
                parsed_w = int(env_w)
                if parsed_w > 0:
                    effective_max_width = parsed_w
            except ValueError:
                pass

    tty_cols, tty_lines = _detect_terminal_size()
    if effective_max_width is None and "unittest" not in sys.modules and tty_cols is not None and tty_cols > 0:
        effective_max_width = max(20, tty_cols - 1)
    effective_screen_height: Optional[int] = None
    if screen_height is not None:
        try:
            if int(screen_height) > 0:
                effective_screen_height = int(screen_height)
        except (TypeError, ValueError):
            pass
    elif watch_interval is not None and tty_lines is not None and tty_lines > 0:
        effective_screen_height = tty_lines

    # Only surface Detailed Model Pools & Sub-Windows when -d / --detail (show_detail=True) is passed.
    effective_show_detail = bool(show_detail)

    # Detect multi-subscription providers and compute smart route picks per multi-account provider
    provider_groups: dict[str, list[ProviderReport]] = {}
    for r in report_list:
        provider_groups.setdefault(r.provider_id, []).append(r)

    best_instance_by_provider: dict[str, str] = {}
    best_summary_items: list[str] = []
    for pid, grp in provider_groups.items():
        if len(grp) > 1:
            best_rep, _ = select_best_account(grp, strategy="smart")
            if best_rep is not None:
                best_instance_by_provider[pid] = best_rep.instance_id
                acct_lbl = best_rep.account or "default"
                target_lbl = f"{pid}:{acct_lbl}"
                p_used = _finite_pct_opt(best_rep.weekly.pace_used_percent)
                w_primary_cands = [
                    p
                    for p in (
                        _finite_pct_opt(best_rep.weekly.remaining_percent),
                        _finite_pct_opt(best_rep.weekly.scoped_remaining_percent),
                        max(0.0, 100.0 - p_used) if p_used is not None else None,
                    )
                    if p is not None
                ]
                fh_best = _extract_5h_remaining(best_rep)
                c_pct_best = _finite_pct_opt(best_rep.credits.remaining_percent)
                if w_primary_cands:
                    rem_lbl = f"{_fmt_pct_int(min(w_primary_cands))} left"
                elif fh_best is not None:
                    rem_lbl = f"5h {_fmt_pct_int(fh_best)} left"
                elif c_pct_best is not None:
                    rem_lbl = f"{_fmt_pct_int(c_pct_best)} left"
                elif _finite_float_opt(best_rep.credits.remaining) is not None or best_rep.credits.is_unlimited:
                    c_str_best = _sanitize_inline(best_rep.credits.remaining_str())
                    rem_lbl = f"{c_str_best} left" if c_str_best and c_str_best != "—" else "ready"
                else:
                    rem_lbl = "ready"
                best_summary_items.append(
                    Style.wrap(pid, Style.WHITE, Style.BOLD, enabled=use_color)
                    + Style.wrap(" → ", Style.GRAY, enabled=use_color)
                    + Style.wrap(target_lbl, Style.GREEN, Style.BOLD, enabled=use_color)
                    + Style.wrap(f" ({rem_lbl})", Style.GRAY, enabled=use_color)
                )

    active_env_profile = _normalize_active_profile_env(os.environ.get("TOKENMASTER_ACTIVE_PROFILE"))

    headers = [
        "Provider",
        "Tier",
        "Weekly Remaining",
        "Reset",
        "Credit",
    ]

    rows: list[list[str]] = []
    for r in report_list:
        is_multi = len(provider_groups.get(r.provider_id, ())) > 1
        is_best = best_instance_by_provider.get(r.provider_id) == r.instance_id
        pid_norm = (r.provider_id or "").strip().lower()
        acct_norm = (r.account or "default").strip().lower() or "default"
        is_active_env = bool(
            active_env_profile and active_env_profile == f"{pid_norm}:{acct_norm}"
        )
        prov_cell = _format_provider_cell(
            r,
            is_multi=is_multi,
            is_best=is_best,
            is_active_env=is_active_env,
            use_color=use_color,
        )

        tier_str = _clean_tier_label(r.plan_tier)
        if tier_str != "—":
            tier_cell = Style.wrap(
                tier_str,
                _row_health_color(r),
                Style.BOLD,
                enabled=use_color,
            )
        else:
            tier_cell = Style.wrap("—", Style.GRAY, enabled=use_color)

        # Weekly Remaining cell:
        # - Primary weekly pool is prefixed with "Total:" and color-coded (both "Total:" title and value)
        #   in Red / Yellow / Green by remaining percentage.
        # - Every secondary model pool and 5h sub-window badge has both its title and value independently
        #   color-coded in Red / Yellow / Green.
        w_rem_str = _sanitize_inline(r.weekly.remaining_str())
        w_used_str = _sanitize_inline(r.weekly.used_str())
        w_note = _sanitize_inline(r.weekly.note or "")
        rem_pct = _finite_pct_opt(r.weekly.remaining_percent)
        scoped_pct = _finite_pct_opt(r.weekly.scoped_remaining_percent)
        five_h_rem = _extract_5h_remaining(r)
        dot_sep = Style.wrap(" · ", Style.GRAY, enabled=use_color)

        if w_rem_str != "—":
            rem_color = _style_pct_color(rem_pct if rem_pct is not None else scoped_pct)
            extra_badges = _build_weekly_detail_badges(
                r,
                use_color=use_color,
                include_scoped=True,
                include_5h=True,
            )

            if rem_pct is not None and r.weekly.unit == "%":
                w_rem_cell = Style.wrap(
                    f"Total: {_fmt_pct_1d(rem_pct, width=5)}",
                    rem_color,
                    Style.BOLD,
                    enabled=use_color,
                )
            elif rem_pct is not None:
                w_rem_cell = Style.wrap(
                    f"Total: {w_rem_str}",
                    rem_color,
                    Style.BOLD,
                    enabled=use_color,
                )
            else:
                w_rem_cell = Style.wrap(
                    f"Total: {w_rem_str}",
                    rem_color,
                    Style.BOLD,
                    enabled=use_color,
                )
            if extra_badges:
                w_rem_cell += dot_sep + dot_sep.join(extra_badges)
        elif scoped_pct is not None:
            s_lbl = _sanitize_inline(r.weekly.scoped_label or "Fable")
            s_col = _style_pct_color(scoped_pct)
            w_rem_cell = Style.wrap(
                f"Total: {_fmt_pct_1d(scoped_pct, width=5)}",
                s_col,
                Style.BOLD,
                enabled=use_color,
            )
            s_badge = Style.wrap(
                f"{s_lbl}: {_fmt_pct_compact(scoped_pct)}",
                s_col,
                Style.BOLD,
                enabled=use_color,
            )
            extra_badges = [
                s_badge,
                *_build_weekly_detail_badges(
                    r,
                    use_color=use_color,
                    include_scoped=False,
                    include_5h=True,
                ),
            ]
            if extra_badges:
                w_rem_cell += dot_sep + dot_sep.join(extra_badges)
        elif five_h_rem is not None and w_used_str == "—":
            fh_col = _style_pct_color(five_h_rem)
            w_rem_cell = Style.wrap(
                f"Total: {_fmt_pct_1d(five_h_rem, width=5)}",
                fh_col,
                Style.BOLD,
                enabled=use_color,
            ) + dot_sep + Style.wrap(
                f"5h: {_fmt_pct_compact(five_h_rem)}",
                fh_col,
                Style.BOLD,
                enabled=use_color,
            )
        elif w_used_str != "—":
            fallback_txt = f"Total Used: {w_used_str}" + (f" ({w_note})" if w_note else "")
            w_rem_cell = Style.wrap(fallback_txt, _row_health_color(r), Style.BOLD, enabled=use_color)
            extra_badges = _build_weekly_detail_badges(
                r,
                use_color=use_color,
                include_scoped=True,
                include_5h=True,
            )
            if extra_badges:
                w_rem_cell += dot_sep + dot_sep.join(extra_badges)
        else:
            w_rem_cell = Style.wrap("—", Style.GRAY, enabled=use_color)

        # Reset countdown cell (with unlock highlighting for exhausted pools & inline ETA for overburn)
        resets_in = _sanitize_inline(r.weekly.resets_in or "")
        rem_sec = parse_reset_remaining_seconds(r.weekly.resets_at, r.weekly.resets_in)
        if r.weekly.resets_at and rem_sec is not None and rem_sec <= 0.0:
            resets_in = "now"
        if resets_in:
            pace_u = _finite_pct_opt(r.weekly.pace_used_percent)
            rem_cands = [
                p
                for p in (
                    rem_pct,
                    scoped_pct,
                    max(0.0, 100.0 - pace_u) if pace_u is not None else None,
                )
                if p is not None
            ]
            rem_pct_val = min(rem_cands) if rem_cands else None
            if rem_pct_val is not None and rem_pct_val <= 0.0:
                if rem_sec is not None and rem_sec <= 6 * 3600:
                    reset_base = Style.wrap(f"⟳ {resets_in}", Style.GREEN, Style.BOLD, enabled=use_color)
                else:
                    reset_base = Style.wrap(f"⟳ {resets_in}", Style.YELLOW, enabled=use_color)
            elif rem_pct_val is not None:
                reset_base = Style.wrap(f"⟳ {resets_in}", _style_pct_color(rem_pct_val), enabled=use_color)
            else:
                reset_base = Style.wrap(f"⟳ {resets_in}", _row_health_color(r), enabled=use_color)

            eta_str = _sanitize_inline(r.weekly.projected_exhaustion_in or "")
            if (
                rem_pct_val is not None
                and rem_pct_val > 0.0
                and r.weekly.pace_status in ("overburn", "fast")
                and eta_str
                and eta_str.lower() != "now"
            ):
                eta_col = Style.RED if r.weekly.pace_status == "overburn" else Style.YELLOW
                reset_cell = (
                    reset_base
                    + Style.wrap(" · ", Style.GRAY, enabled=use_color)
                    + Style.wrap(f"▲ ~{eta_str}", eta_col, Style.BOLD, enabled=use_color)
                )
            else:
                reset_cell = reset_base
        else:
            reset_cell = Style.wrap("—", Style.GRAY, enabled=use_color)

        # Credit cell (show numeric value only, stripping the "credits" unit suffix)
        c_rem_raw = _sanitize_inline(r.credits.remaining_str())
        c_rem_str = re.sub(r"\s*\bcredits?\b", "", c_rem_raw, flags=re.IGNORECASE).strip()
        if c_rem_str not in ("—", ""):
            c_rem_pct = r.credits.remaining_percent
            c_color = _style_pct_color(c_rem_pct)
            c_rem_cell = Style.wrap(c_rem_str, c_color, Style.BOLD, enabled=use_color)
        else:
            c_rem_cell = Style.wrap("—", Style.GRAY, enabled=use_color)

        rows.append(
            [
                prov_cell,
                tier_cell,
                w_rem_cell,
                reset_cell,
                c_rem_cell,
            ]
        )

    # Calculate column widths and shrink/expand to occupy the target terminal width
    col_widths = [visible_len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            col_widths[idx] = max(col_widths[idx], visible_len(cell))

    if effective_max_width is not None:
        col_widths = _fit_col_widths(col_widths, effective_max_width)
        col_widths = _expand_col_widths(col_widths, effective_max_width)

    # Draw horizontal-rule table without any vertical bars (neither internal nor left/right outer edges)
    top_border = "──" + "───".join("─" * w for w in col_widths) + "──"
    mid_border = "──" + "───".join("─" * w for w in col_widths) + "──"
    bot_border = "──" + "───".join("─" * w for w in col_widths) + "──"
    table_total_width = visible_len(top_border)

    header_target_width = (
        effective_max_width if effective_max_width is not None else table_total_width
    )
    lines.append(
        _compose_thermalmaster_header(
            report_list,
            provider_groups=provider_groups,
            width=header_target_width,
            watch_interval=watch_interval,
            use_color=use_color,
        )
    )

    lines.append(Style.wrap(top_border, Style.GRAY, enabled=use_color))
    header_cells = [
        Style.wrap(pad_cell(h, col_widths[i], truncate=True), Style.BOLD, Style.CYAN, enabled=use_color)
        for i, h in enumerate(headers)
    ]
    sep = "   "
    left_edge = "  "
    right_edge = "  "

    lines.append(left_edge + sep.join(header_cells) + right_edge)
    lines.append(Style.wrap(mid_border, Style.GRAY, enabled=use_color))

    for row in rows:
        padded = [
            pad_cell(
                _fit_weekly_cell_to_width(cell, col_widths[i]) if i == 2 else cell,
                col_widths[i],
                truncate=True,
            )
            for i, cell in enumerate(row)
        ]
        lines.append(left_edge + sep.join(padded) + right_edge)

    lines.append(Style.wrap(bot_border, Style.GRAY, enabled=use_color))

    # Compact status summary bar (auto-wraps alerts onto an indented line when wide)
    if report_list:
        total_cnt = len(report_list)
        live_cnt = sum(1 for r in report_list if r.status == "ok")
        local_cnt = sum(1 for r in report_list if r.status == "partial")
        err_cnt = sum(1 for r in report_list if r.status == "error")
        setup_cnt = sum(1 for r in report_list if r.status == "unconfigured")

        status_parts = [
            Style.wrap(
                f"{live_cnt}/{total_cnt} LIVE",
                Style.GREEN if live_cnt > 0 else Style.GRAY,
                Style.BOLD,
                enabled=use_color,
            )
        ]
        if local_cnt:
            status_parts.append(Style.wrap(f"{local_cnt} LOCAL", Style.YELLOW, enabled=use_color))
        if err_cnt:
            status_parts.append(Style.wrap(f"{err_cnt} ERROR", Style.RED, Style.BOLD, enabled=use_color))
        if setup_cnt:
            status_parts.append(Style.wrap(f"{setup_cnt} SETUP", Style.GRAY, enabled=use_color))

        alert_parts: list[str] = []
        hot_providers: list[str] = []
        hot_instance_ids: set[str] = set()
        for r in report_list:
            lbl = _format_report_label(r, is_multi=len(provider_groups.get(r.provider_id, ())) > 1)
            u_pct = _finite_pct_opt(r.weekly.used_percent)
            su_pct = _finite_pct_opt(r.weekly.scoped_used_percent)
            pu_pct = _finite_pct_opt(r.weekly.pace_used_percent)
            if u_pct is not None and u_pct >= 85.0:
                hot_providers.append(f"{lbl} ({_fmt_pct_1d(u_pct)})")
                hot_instance_ids.add(r.instance_id)
            elif su_pct is not None and su_pct >= 85.0:
                s_lbl = _sanitize_inline(r.weekly.scoped_label or "Fable")
                hot_providers.append(f"{lbl} ({s_lbl} {_fmt_pct_1d(su_pct)})")
                hot_instance_ids.add(r.instance_id)
            elif pu_pct is not None and pu_pct >= 85.0:
                hot_providers.append(f"{lbl} ({_fmt_pct_1d(pu_pct)})")
                hot_instance_ids.add(r.instance_id)
        if hot_providers:
            alert_parts.append(
                Style.wrap("High usage: " + ", ".join(hot_providers), Style.RED, enabled=use_color)
            )

        overburn_providers = [
            f"{_format_report_label(r, is_multi=len(provider_groups.get(r.provider_id, ())) > 1)} ({r.weekly.pace_ratio:.1f}x, est. {r.weekly.projected_exhaustion_in})"
            for r in report_list
            if (
                r.weekly.pace_status == "overburn"
                and r.weekly.pace_ratio is not None
                and r.weekly.projected_exhaustion_in
                and r.weekly.projected_exhaustion_in != "now"
                and r.instance_id not in hot_instance_ids
            )
        ]
        if overburn_providers:
            alert_parts.append(
                Style.wrap("Pace warning: " + ", ".join(overburn_providers), Style.YELLOW, enabled=use_color)
            )

        sep_dot = Style.wrap(" • ", Style.GRAY, enabled=use_color)
        base_summary = " " + Style.wrap("Summary:", Style.GRAY, enabled=use_color) + " " + sep_dot.join(status_parts)
        if alert_parts:
            combined_single = base_summary + sep_dot + sep_dot.join(alert_parts)
            wrap_limit = (
                table_total_width if effective_max_width is not None else max(table_total_width, 96)
            )
            if visible_len(combined_single) <= wrap_limit:
                lines.append(combined_single)
            else:
                lines.append(base_summary)
                lines.append("   " + Style.wrap("↳ ", Style.GRAY, enabled=use_color) + sep_dot.join(alert_parts))
        else:
            lines.append(base_summary)

        if best_summary_items:
            lines.append(
                " "
                + Style.wrap("⚡ Smart Pick:", Style.CYAN, Style.BOLD, enabled=use_color)
                + " "
                + sep_dot.join(best_summary_items)
                + Style.wrap("  (`tokenmaster exec <provider>:auto`)", Style.GRAY, enabled=use_color)
            )

    # Optional Detail Breakdown section
    if effective_show_detail:
        providers_with_sub = [
            r for r in report_list if r.sub_metrics or r.weekly.pace_summary_str()
        ]
        if providers_with_sub:
            name_col_w = max(
                [
                    26,
                    *(
                        visible_len(_sanitize_inline(sm.name))
                        for r in providers_with_sub
                        for sm in r.sub_metrics
                    ),
                ]
            )
            used_col_w = max(
                [
                    28,
                    *(
                        visible_len(f"Used: {_sanitize_inline(sm.used_str)}")
                        for r in providers_with_sub
                        for sm in r.sub_metrics
                    ),
                ]
            )
            if effective_max_width is not None:
                # 6 indent + name_col_w + 1 + 9 bar + used_col_w + 1 + ~18 remaining
                budget_for_cols = max(20, effective_max_width - 35)
                if name_col_w + used_col_w > budget_for_cols:
                    name_col_w = max(10, min(name_col_w, budget_for_cols // 2))
                    used_col_w = max(10, min(used_col_w, budget_for_cols - name_col_w))

            lines.append("")
            lines.append(
                Style.wrap(
                    " 📊 Detailed Model Pools & Sub-Windows",
                    Style.BOLD,
                    Style.MAGENTA,
                    enabled=use_color,
                )
            )
            for r in providers_with_sub:
                badge_items: list[str] = []
                if r.source:
                    badge_items.append(_sanitize_inline(r.source))
                lat = _valid_latency(r.fetch_ms)
                if lat is not None:
                    badge_items.append(_format_latency_ms(lat))
                src_badge = f" [{' • '.join(badge_items)}]" if badge_items else ""
                rep_lbl = _format_report_label(
                    r, is_multi=len(provider_groups.get(r.provider_id, ())) > 1
                )
                prov_hdr_color = _row_health_color(r)
                lines.append(
                    "  "
                    + Style.wrap(f"▸ {rep_lbl}", Style.BOLD, prov_hdr_color, enabled=use_color)
                    + Style.wrap(src_badge, Style.GRAY, enabled=use_color)
                )
                for sm in r.sub_metrics:
                    bar_str = (
                        render_progress_bar(sm.used_percent, width=8, color_enabled=use_color) + " "
                        if sm.used_percent is not None
                        else "         "
                    )
                    sm_reset = _sanitize_inline(sm.resets_in or "")
                    reset_part = f" (⟳ {sm_reset})" if sm_reset else ""
                    name_padded = pad_cell(_sanitize_inline(sm.name), name_col_w, truncate=True)
                    used_padded = pad_cell(
                        f"Used: {_sanitize_inline(sm.used_str)}", used_col_w, truncate=True
                    )
                    rem_str = _sanitize_inline(sm.remaining_str)
                    rem_padded = f"Remaining: {rem_str}{reset_part}"
                    if sm.used_percent is not None:
                        rem_pct = max(0.0, 100.0 - float(sm.used_percent))
                        rem_color = _style_pct_color(rem_pct)
                        title_color = rem_color
                        used_color = rem_color
                    elif rem_str == "—":
                        rem_color = Style.GRAY
                        title_color = Style.CYAN
                        used_color = Style.GRAY
                    else:
                        rem_color = Style.GREEN
                        title_color = Style.GREEN
                        used_color = Style.GREEN
                    lines.append(
                        f"      {Style.wrap(name_padded, title_color, Style.BOLD, enabled=use_color)} "
                        f"{bar_str}{Style.wrap(used_padded, used_color, enabled=use_color)} "
                        f"{Style.wrap(rem_padded, rem_color, Style.BOLD, enabled=use_color)}"
                    )
                pace_txt = r.weekly.pace_summary_str()
                if pace_txt:
                    if r.weekly.pace_status == "overburn":
                        pace_color = Style.RED
                    elif r.weekly.pace_status == "fast":
                        pace_color = Style.YELLOW
                    else:
                        pace_color = Style.GREEN
                    if r.weekly.elapsed_percent is not None:
                        el_pct = max(0.0, min(100.0, float(r.weekly.elapsed_percent)))
                        el_filled = max(0, min(8, int(round((el_pct / 100.0) * 8))))
                        el_empty = 8 - el_filled
                        pace_bar = (
                            (Style.wrap("━" * el_filled, pace_color, Style.BOLD, enabled=use_color) if el_filled else "")
                            + (Style.wrap("─" * el_empty, Style.TRACK, enabled=use_color) if el_empty else "")
                            + " "
                        )
                    else:
                        pace_bar = "         "
                    pace_label = pad_cell("Weekly Pace & ETA", name_col_w, truncate=True)
                    lines.append(
                        f"      {Style.wrap(pace_label, Style.CYAN, enabled=use_color)} "
                        f"{pace_bar}Pace: {Style.wrap(_sanitize_inline(pace_txt), pace_color, enabled=use_color)}"
                    )

    # Setup / Auth Hints footer for unconfigured, partial, or errored providers
    hints = [
        (
            _format_report_label(r, is_multi=len(provider_groups.get(r.provider_id, ())) > 1),
            _sanitize_inline(str(r.error_message or r.auth_hint)),
        )
        for r in report_list
        if r.status in ("unconfigured", "error", "partial") and (r.error_message or r.auth_hint)
    ]
    if hints:
        hint_name_w = max([22, *(visible_len(p_name) for p_name, _ in hints)])
        lines.append("")
        lines.append(
            Style.wrap(
                " 💡 Quick Setup / Diagnostics (`tokenmaster auth` or `tokenmaster quota`):",
                Style.YELLOW,
                Style.BOLD,
                enabled=use_color,
            )
        )
        for p_name, hint in hints:
            lines.append(
                f"   • {Style.wrap(pad_cell(p_name, hint_name_w), Style.WHITE, Style.BOLD, enabled=use_color)} "
                f"{Style.wrap(hint, Style.GRAY, enabled=use_color)}"
            )

    if effective_screen_height is not None and effective_screen_height >= 10:
        # Fit output to (effective_screen_height - 1) rows so paint_watch_frame's trailing newline
        # occupies the full viewport without scrolling row 1 off the top of the terminal.
        max_rows = max(6, effective_screen_height - 1)
        if watch_interval is not None:
            footer_line = _compose_watch_footer(
                header_target_width,
                watch_interval,
                use_color=use_color,
            )
            body_budget = max(5, max_rows - 1)
            if len(lines) > body_budget:
                omitted = len(lines) - (body_budget - 1)
                lines = lines[: body_budget - 1]
                lines.append(
                    Style.wrap(
                        f"  … (+{omitted} more line(s) — enlarge terminal window to view all)",
                        Style.GRAY,
                        enabled=use_color,
                    )
                )
            elif len(lines) < body_budget:
                lines.extend([""] * (body_budget - len(lines)))
            lines.append(footer_line)
        elif len(lines) < max_rows:
            lines.extend([""] * (max_rows - len(lines)))

    if effective_max_width is not None:
        lines = [
            truncate_visible(line, effective_max_width)
            if visible_len(line) > effective_max_width
            else line
            for line in lines
        ]

    return "\n".join(lines)


def paint_watch_frame(output: str, *, first_frame: bool = False, stream: Any = None) -> None:
    """Render a watch-mode frame without full-screen blanking flicker."""
    target = sys.stdout if stream is None else stream
    if _supports_color(target):
        prefix = "\x1b[2J\x1b[H" if first_frame else "\x1b[H"
        cleared_lines = "\x1b[K\n".join(output.splitlines())
        target.write(f"{prefix}{cleared_lines}\x1b[K\n\x1b[J")
        target.flush()
    else:
        if first_frame:
            os.system("cls" if os.name == "nt" else "clear")
        target.write(output + "\n")
        target.flush()


def clear_terminal() -> None:
    """Clear terminal screen for live watch mode."""
    if _supports_color():
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()
    else:
        os.system("cls" if os.name == "nt" else "clear")


SHORT_PROVIDER_LABELS: dict[str, str] = {
    "codex": "CX",
    "claude": "CL",
    "antigravity": "AG",
    "grok": "GK",
    "cursor": "CR",
    "openrouter": "OR",
}


def _extract_5h_remaining(r: ProviderReport) -> Optional[float]:
    for sm in r.sub_metrics:
        lbl = (sm.name or "").lower()
        if "5h" in lbl or "5-hour" in lbl or "five_hour" in lbl:
            if sm.used_percent is not None and not isinstance(sm.used_percent, bool):
                try:
                    fv = float(sm.used_percent)
                    if math.isfinite(fv):
                        return max(0.0, min(100.0, 100.0 - fv))
                except (TypeError, ValueError):
                    pass
    return None


def _finite_pct_opt(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        fv = float(v)
        return max(0.0, min(100.0, fv)) if math.isfinite(fv) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _finite_float_opt(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        fv = float(v)
        return fv if math.isfinite(fv) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _compact_metric_value(r: ProviderReport) -> tuple[str, str]:
    """Return ``(value_text, severity_level)`` where severity is 'ok', 'warn', 'crit', or 'muted'."""
    if r.status == "error":
        return "ERR", "crit"
    if r.status == "unconfigured":
        return "—", "muted"

    w_cands: list[float] = []
    w_rem = _finite_pct_opt(r.weekly.remaining_percent)
    if w_rem is not None:
        w_cands.append(w_rem)
    w_scoped = _finite_pct_opt(r.weekly.scoped_remaining_percent)
    if w_scoped is not None:
        w_cands.append(w_scoped)
    w_pace_used = _finite_pct_opt(r.weekly.pace_used_percent)
    if w_pace_used is not None:
        w_cands.append(max(0.0, 100.0 - w_pace_used))

    if w_cands:
        pct = min(w_cands)
        sev = "crit" if pct <= 15.0 else ("warn" if pct <= 40.0 else "ok")
        return f"{pct:.0f}%", sev

    c_rem_pct = _finite_pct_opt(r.credits.remaining_percent)
    c_rem_val = _finite_float_opt(r.credits.remaining)
    if c_rem_pct is not None:
        pct = c_rem_pct
        sev = "crit" if pct <= 15.0 else ("warn" if pct <= 40.0 else "ok")
        if c_rem_val is not None:
            return _sanitize_inline(r.credits.remaining_str().split(" ")[0]).replace(" ", ""), sev
        return f"{pct:.0f}%", sev

    if c_rem_val is not None or r.credits.is_unlimited:
        c_str = _sanitize_inline(r.credits.remaining_str()).replace(" ", "")
        if c_str and c_str != "—":
            sev = "crit" if (c_rem_val is not None and c_rem_val <= 0) else "ok"
            return c_str, sev

    w_rem_str = _sanitize_inline(r.weekly.remaining_str())
    if w_rem_str != "—":
        return w_rem_str.replace(" ", ""), "ok"

    w_used = _sanitize_inline(r.weekly.used_str())
    if w_used != "—":
        return w_used.replace(" ", ""), "muted"

    return ("LIVE" if r.status == "ok" else "LOCAL"), ("ok" if r.status == "ok" else "warn")


def render_statusline(
    reports: Iterable[ProviderReport],
    *,
    fmt: str = "compact",
    claude_code_ctx: Optional[dict[str, Any]] = None,
    color_enabled: Optional[bool] = None,
    max_width: Optional[int] = None,
) -> str:
    """Render an ultra-fast single-line status bar for Claude Code, tmux, Starship, or shell prompts."""
    mode = (fmt or "compact").strip().lower()
    use_color = (
        False
        if mode in ("plain", "tmux")
        else (_supports_color() if color_enabled is None else bool(color_enabled))
    )
    if max_width is None:
        env_w = os.environ.get("TOKENMASTER_MAX_WIDTH")
        if env_w:
            try:
                max_width = int(env_w)
            except ValueError:
                max_width = None
    report_list = [r.finalize() for r in reports]

    if mode == "claude-code":
        rep = (
            report_list[0]
            if report_list
            else ProviderReport(provider_id="claude", display_name="Claude", status="unconfigured")
        )
        dot = format_status_dot(rep.status, color_enabled=use_color)
        inst_lbl = _sanitize_inline(
            f"{rep.provider_id}:{rep.account}" if rep.account else rep.provider_id
        )
        id_seg = f"{dot} {Style.wrap(inst_lbl, Style.BOLD, Style.WHITE, enabled=use_color)}"
        if rep.plan_tier:
            short_tier = _sanitize_inline(rep.plan_tier).replace("Claude ", "")
            id_seg += " " + Style.wrap(f"[{short_tier}]", Style.CYAN, enabled=use_color)

        segments: list[str] = [id_seg]

        w_rem = _finite_pct_opt(rep.weekly.remaining_percent)
        s_rem = _finite_pct_opt(rep.weekly.scoped_remaining_percent)
        p_used = _finite_pct_opt(rep.weekly.pace_used_percent)
        primary_pct = w_rem if w_rem is not None else (s_rem if s_rem is not None else (max(0.0, 100.0 - p_used) if p_used is not None else None))
        if primary_pct is not None:
            eff_min = min([x for x in (w_rem, s_rem, (100.0 - p_used) if p_used is not None else None) if x is not None])
            w_col = Style.RED if eff_min <= 15.0 else (Style.YELLOW if eff_min <= 40.0 else Style.GREEN)
            w_val = Style.wrap(f"{primary_pct:.1f}%", w_col, Style.BOLD, enabled=use_color)
            scoped_part = ""
            if w_rem is not None and s_rem is not None:
                s_lbl = _sanitize_inline(rep.weekly.scoped_label or "Fable")
                s_col = Style.RED if s_rem <= 15.0 else (Style.YELLOW if s_rem <= 40.0 else Style.GREEN)
                scoped_part = f" ({s_lbl}: {Style.wrap(f'{s_rem:.1f}%', s_col, enabled=use_color)})"
            r_in = _sanitize_inline(rep.weekly.resets_in or "").replace(" ", "")
            r_part = f" (⟳{r_in})" if r_in else ""
            p_part = ""
            if (
                rep.weekly.pace_status == "overburn"
                and rep.weekly.pace_ratio is not None
                and math.isfinite(float(rep.weekly.pace_ratio))
            ):
                p_part = " " + Style.wrap(
                    f"⚠{float(rep.weekly.pace_ratio):.1f}x", Style.YELLOW, enabled=use_color
                )
            segments.append(f"7d: {w_val}{scoped_part}{r_part}{p_part}")
        elif _finite_float_opt(rep.credits.remaining) is not None or rep.credits.is_unlimited:
            segments.append(f"Cr: {_sanitize_inline(rep.credits.remaining_str())}")
        elif _sanitize_inline(rep.weekly.remaining_str()) != "—":
            segments.append(f"7d: {_sanitize_inline(rep.weekly.remaining_str())}")
        elif _sanitize_inline(rep.weekly.used_str()) != "—":
            segments.append(f"7d: {_sanitize_inline(rep.weekly.used_str())}")
        elif rep.status in ("error", "unconfigured"):
            st_col = Style.RED if rep.status == "error" else Style.GRAY
            segments.append(Style.wrap(rep.status.upper(), st_col, enabled=use_color))

        s5_rem = _extract_5h_remaining(rep)
        if s5_rem is not None:
            s5_col = (
                Style.RED
                if s5_rem <= 15.0
                else (Style.YELLOW if s5_rem <= 40.0 else Style.GREEN)
            )
            segments.append(
                f"5h: {Style.wrap(f'{s5_rem:.1f}%', s5_col, Style.BOLD, enabled=use_color)}"
            )

        if isinstance(claude_code_ctx, dict) and claude_code_ctx:
            raw_model = claude_code_ctx.get("model")
            model_lbl: Optional[str] = None
            if isinstance(raw_model, dict):
                model_lbl = str(
                    raw_model.get("display_name")
                    or raw_model.get("name")
                    or raw_model.get("id")
                    or ""
                ).strip() or None
            elif isinstance(raw_model, str) and raw_model.strip():
                model_lbl = raw_model.strip()
            if model_lbl:
                segments.append(
                    Style.wrap(_sanitize_inline(model_lbl), Style.MAGENTA, enabled=use_color)
                )

            cw = claude_code_ctx.get("context_window")
            ctx_pct: Optional[float] = None
            if isinstance(cw, dict):
                for k in ("used_percentage", "used_percent", "percent"):
                    v = cw.get(k)
                    if v is not None and not isinstance(v, bool):
                        try:
                            fv = float(v)
                            if math.isfinite(fv):
                                ctx_pct = max(0.0, min(100.0, fv))
                                break
                        except (TypeError, ValueError):
                            pass
                if ctx_pct is None:
                    rv = cw.get("remaining_percentage")
                    if rv is not None and not isinstance(rv, bool):
                        try:
                            frv = float(rv)
                            if math.isfinite(frv):
                                ctx_pct = max(0.0, min(100.0, 100.0 - frv))
                        except (TypeError, ValueError):
                            pass
            if ctx_pct is not None:
                c_col = (
                    Style.RED
                    if ctx_pct >= 85.0
                    else (Style.YELLOW if ctx_pct >= 65.0 else Style.CYAN)
                )
                segments.append(
                    f"Ctx: {Style.wrap(f'{ctx_pct:.0f}%', c_col, Style.BOLD, enabled=use_color)}"
                )

            cost_obj = claude_code_ctx.get("cost")
            sess_usd: Optional[float] = None
            if isinstance(cost_obj, dict):
                cv = cost_obj.get("total_cost_usd")
                if cv is not None and not isinstance(cv, bool):
                    try:
                        fcv = float(cv)
                        if math.isfinite(fcv) and fcv >= 0.0:
                            sess_usd = fcv
                    except (TypeError, ValueError):
                        pass
            if sess_usd is None:
                for ck in ("cost_usd", "total_cost_usd"):
                    cv = claude_code_ctx.get(ck)
                    if cv is not None and not isinstance(cv, bool):
                        try:
                            fcv = float(cv)
                            if math.isfinite(fcv) and fcv >= 0.0:
                                sess_usd = fcv
                                break
                        except (TypeError, ValueError):
                            pass
            if sess_usd is not None:
                segments.append(f"Sess: ${sess_usd:.2f}")

        sep = Style.wrap(" │ ", Style.GRAY, enabled=use_color)
        line = sep.join(segments)
        if max_width is not None and max_width > 0 and visible_len(line) > max_width:
            line = truncate_visible(line, max_width)
        return _sanitize_inline(line)

    # Compact / tmux / plain multi-provider badges
    badges: list[str] = []
    tmux_vis_used = 0
    for r in report_list:
        short_pid = SHORT_PROVIDER_LABELS.get(r.provider_id, r.provider_id[:2].upper())
        short_lbl = f"{short_pid}({_sanitize_inline(r.account)})" if r.account else short_pid
        val_txt, sev = _compact_metric_value(r)

        if mode == "tmux":
            dot_char = (
                "●"
                if r.status == "ok"
                else ("◐" if r.status == "partial" else ("✖" if r.status == "error" else "○"))
            )
            plain_badge = f"{dot_char}{short_lbl}:{val_txt}"
            badge_vis = visible_len(plain_badge)
            sep_vis = 1 if badges else 0
            if max_width is not None and max_width > 0 and (tmux_vis_used + sep_vis + badge_vis) > max_width:
                rem_cols = max_width - tmux_vis_used - sep_vis
                if rem_cols >= 1 and not badges:
                    trunc_plain = truncate_visible(plain_badge, rem_cols).replace("#", "##")
                    badges.append(trunc_plain)
                elif rem_cols >= 1:
                    badges.append("…")
                break
            tmux_vis_used += sep_vis + badge_vis
            dot_fg = (
                "green"
                if r.status == "ok"
                else ("yellow" if r.status == "partial" else ("red" if r.status == "error" else "colour245"))
            )
            val_fg = (
                "red"
                if sev == "crit"
                else ("yellow" if sev == "warn" else ("green" if sev == "ok" else "colour245"))
            )
            esc_lbl = short_lbl.replace("#", "##")
            esc_val = val_txt.replace("#", "##")
            badges.append(
                f"#[fg={dot_fg}]{dot_char}#[fg=default]{esc_lbl}:#[fg={val_fg}]{esc_val}#[fg=default]"
            )
        else:
            dot = format_status_dot(r.status, color_enabled=use_color)
            val_col = (
                Style.RED
                if sev == "crit"
                else (Style.YELLOW if sev == "warn" else (Style.GREEN if sev == "ok" else Style.GRAY))
            )
            styled_lbl = Style.wrap(short_lbl, Style.BOLD, Style.WHITE, enabled=use_color)
            styled_val = Style.wrap(val_txt, val_col, Style.BOLD, enabled=use_color)
            badges.append(f"{dot}{styled_lbl}:{styled_val}")

    line = " ".join(badges)
    if mode != "tmux" and max_width is not None and max_width > 0 and visible_len(line) > max_width:
        line = truncate_visible(line, max_width)
    return _sanitize_inline(line)


def _format_compact_tokens(n: Any) -> str:
    try:
        val = max(0, int(n or 0))
    except (TypeError, ValueError, OverflowError):
        return "0"
    if val >= 1_000_000_000:
        return f"{val / 1_000_000_000:.2f}B"
    if val >= 1_000_000:
        return f"{val / 1_000_000:.2f}M"
    if val >= 1_000:
        return f"{val / 1_000:.1f}K"
    return str(val)


def render_history_report(
    report_data: dict[str, Any],
    *,
    color_enabled: Optional[bool] = None,
    max_width: Optional[int] = None,
) -> str:
    """Render ``UsageWarehouse.query_history()`` results as a formatted terminal table."""
    use_color = _supports_color() if color_enabled is None else bool(color_enabled)
    if max_width is None:
        env_w = os.environ.get("TOKENMASTER_MAX_WIDTH")
        if env_w:
            try:
                max_width = int(env_w)
            except ValueError:
                max_width = None
    if max_width is not None and max_width <= 0:
        max_width = None

    mode = str(report_data.get("mode") or "daily")
    rows = report_data.get("rows") if isinstance(report_data.get("rows"), list) else []
    totals = report_data.get("totals") if isinstance(report_data.get("totals"), dict) else {}
    since_s = report_data.get("since")
    until_s = report_data.get("until")
    db_path = _sanitize_inline(str(report_data.get("warehouse_path") or ""))

    lines: list[str] = []
    mode_title = {
        "daily": "Daily Usage & Cache Ledger",
        "weekly": "Weekly (ISO) Usage & Cache Ledger",
        "monthly": "Monthly Usage & Cache Ledger",
        "projects": "Usage by Project",
        "models": "Usage by Model",
        "snapshots": "Quota & Pacing Snapshots",
    }.get(mode, "Usage History")

    range_str = ""
    if since_s and until_s:
        range_str = f" ({since_s} .. {until_s})"
    elif since_s:
        range_str = f" (since {since_s})"
    elif until_s:
        range_str = f" (until {until_s})"

    header_line = (
        Style.wrap(f"📊 TokenMaster Warehouse — {mode_title}", Style.BOLD, Style.CYAN, enabled=use_color)
        + Style.wrap(range_str, Style.GRAY, enabled=use_color)
    )
    if max_width is not None and visible_len(header_line) > max_width:
        header_line = truncate_visible(header_line, max_width)
    lines.append(header_line)

    if not rows:
        empty_msg = Style.wrap(
            "  No historical records matched in warehouse.db.", Style.GRAY, enabled=use_color
        )
        if max_width is not None and visible_len(empty_msg) > max_width:
            empty_msg = truncate_visible(empty_msg, max_width)
        lines.append(empty_msg)
        return "\n".join(lines)

    if mode == "snapshots":
        headers = ["Timestamp (UTC)", "Instance", "Weekly Rem", "5h Rem", "Credits / Pace"]
        raw_rows: list[list[str]] = []
        for r in rows:
            ts_lbl = _sanitize_inline(str(r.get("iso_utc") or "—"))
            inst_lbl = Style.wrap(
                _sanitize_inline(str(r.get("instance_id") or "")), Style.BOLD, Style.WHITE, enabled=use_color
            )
            w_pct = r.get("weekly_remaining_pct")
            w_txt = f"{float(w_pct):.1f}%" if w_pct is not None else "—"
            s5_pct = r.get("five_hour_remaining_pct")
            s5_txt = f"{float(s5_pct):.1f}%" if s5_pct is not None else "—"
            c_rem = r.get("credit_remaining")
            p_rat = r.get("pace_ratio")
            tail_parts: list[str] = []
            if c_rem is not None:
                tail_parts.append(f"${float(c_rem):.2f}")
            if p_rat is not None:
                tail_parts.append(f"{float(p_rat):.2f}x")
            tail_txt = " · ".join(tail_parts) if tail_parts else str(r.get("status") or "—")
            raw_rows.append([ts_lbl, inst_lbl, w_txt, s5_txt, tail_txt])

        col_widths = [max(visible_len(h), *(visible_len(row[i]) for row in raw_rows)) for i, h in enumerate(headers)]
        if max_width is not None:
            col_widths = _fit_col_widths(col_widths, max_width)
        top = "╭─" + "─┬─".join("─" * w for w in col_widths) + "─╮"
        sep = "├─" + "─┼─".join("─" * w for w in col_widths) + "─┤"
        bot = "╰─" + "─┴─".join("─" * w for w in col_widths) + "─╯"
        lines.append(Style.wrap(top, Style.GRAY, enabled=use_color))
        h_cells = [
            Style.wrap(pad_cell(h, col_widths[i], truncate=True), Style.BOLD, Style.CYAN, enabled=use_color)
            for i, h in enumerate(headers)
        ]
        lines.append(
            Style.wrap("│ ", Style.GRAY, enabled=use_color)
            + Style.wrap(" │ ", Style.GRAY, enabled=use_color).join(h_cells)
            + Style.wrap(" │", Style.GRAY, enabled=use_color)
        )
        lines.append(Style.wrap(sep, Style.GRAY, enabled=use_color))
        for row in raw_rows:
            r_cells = [pad_cell(row[i], col_widths[i], truncate=True) for i in range(len(headers))]
            lines.append(
                Style.wrap("│ ", Style.GRAY, enabled=use_color)
                + Style.wrap(" │ ", Style.GRAY, enabled=use_color).join(r_cells)
                + Style.wrap(" │", Style.GRAY, enabled=use_color)
            )
        lines.append(Style.wrap(bot, Style.GRAY, enabled=use_color))
        if max_width is not None:
            lines = [
                truncate_visible(line, max_width) if visible_len(line) > max_width else line
                for line in lines
            ]
        return "\n".join(lines)

    first_header = {
        "daily": "Date (UTC)",
        "weekly": "Week (ISO)",
        "monthly": "Month",
        "projects": "Project",
        "models": "Model",
    }.get(mode, "Group")
    headers = [
        first_header,
        "Sessions",
        "In / Out",
        "Cache Hit %",
        "Total Tokens",
        "Est. API Cost",
    ]

    table_rows: list[list[str]] = []
    for r in rows:
        k_lbl = Style.wrap(_sanitize_inline(str(r.get("key") or "—")), Style.BOLD, Style.WHITE, enabled=use_color)
        sess_lbl = str(int(r.get("sessions") or 0))
        in_out_lbl = (
            f"{_format_compact_tokens(r.get('input_tokens'))} / {_format_compact_tokens(r.get('output_tokens'))}"
        )
        hit_pct = r.get("cache_hit_percent")
        cr_tok = int(r.get("cache_read_tokens") or 0)
        if hit_pct is not None:
            hp = float(hit_pct)
            h_col = Style.GREEN if hp >= 70.0 else (Style.YELLOW if hp >= 30.0 else Style.GRAY)
            hit_lbl = (
                Style.wrap(f"{hp:.1f}%", h_col, Style.BOLD, enabled=use_color)
                + Style.wrap(f" ({_format_compact_tokens(cr_tok)})", Style.GRAY, enabled=use_color)
            )
        else:
            hit_lbl = Style.wrap("—", Style.GRAY, enabled=use_color)
        tot_lbl = _format_compact_tokens(r.get("total_tokens"))
        usd_val = float(r.get("estimated_usd") or 0.0)
        usd_lbl = Style.wrap(f"${usd_val:,.2f}", Style.CYAN, Style.BOLD, enabled=use_color)
        table_rows.append([k_lbl, sess_lbl, in_out_lbl, hit_lbl, tot_lbl, usd_lbl])

    # Build TOTAL footer row
    t_sess = str(int(totals.get("sessions") or 0))
    t_in_out = (
        f"{_format_compact_tokens(totals.get('input_tokens'))} / {_format_compact_tokens(totals.get('output_tokens'))}"
    )
    t_hit = totals.get("cache_hit_percent")
    t_cr = int(totals.get("cache_read_tokens") or 0)
    if t_hit is not None:
        thp = float(t_hit)
        th_col = Style.GREEN if thp >= 70.0 else (Style.YELLOW if thp >= 30.0 else Style.GRAY)
        t_hit_lbl = (
            Style.wrap(f"{thp:.1f}%", th_col, Style.BOLD, enabled=use_color)
            + Style.wrap(f" ({_format_compact_tokens(t_cr)})", Style.GRAY, enabled=use_color)
        )
    else:
        t_hit_lbl = Style.wrap("—", Style.GRAY, enabled=use_color)
    t_tot_lbl = Style.wrap(_format_compact_tokens(totals.get("total_tokens")), Style.BOLD, enabled=use_color)
    t_usd_val = float(totals.get("estimated_usd") or 0.0)
    t_usd_lbl = Style.wrap(f"${t_usd_val:,.2f}", Style.GREEN, Style.BOLD, enabled=use_color)
    total_row = [
        Style.wrap("TOTAL", Style.BOLD, Style.YELLOW, enabled=use_color),
        Style.wrap(t_sess, Style.BOLD, enabled=use_color),
        t_in_out,
        t_hit_lbl,
        t_tot_lbl,
        t_usd_lbl,
    ]

    all_measure_rows = [*table_rows, total_row]
    col_widths = [
        max(visible_len(h), *(visible_len(row[i]) for row in all_measure_rows))
        for i, h in enumerate(headers)
    ]
    if max_width is not None:
        col_widths = _fit_col_widths(col_widths, max_width)

    top = "╭─" + "─┬─".join("─" * w for w in col_widths) + "─╮"
    sep = "├─" + "─┼─".join("─" * w for w in col_widths) + "─┤"
    bot = "╰─" + "─┴─".join("─" * w for w in col_widths) + "─╯"

    lines.append(Style.wrap(top, Style.GRAY, enabled=use_color))
    h_cells = [
        Style.wrap(pad_cell(h, col_widths[i], truncate=True), Style.BOLD, Style.CYAN, enabled=use_color)
        for i, h in enumerate(headers)
    ]
    lines.append(
        Style.wrap("│ ", Style.GRAY, enabled=use_color)
        + Style.wrap(" │ ", Style.GRAY, enabled=use_color).join(h_cells)
        + Style.wrap(" │", Style.GRAY, enabled=use_color)
    )
    lines.append(Style.wrap(sep, Style.GRAY, enabled=use_color))

    for row in table_rows:
        r_cells = [pad_cell(row[i], col_widths[i], truncate=True) for i in range(len(headers))]
        lines.append(
            Style.wrap("│ ", Style.GRAY, enabled=use_color)
            + Style.wrap(" │ ", Style.GRAY, enabled=use_color).join(r_cells)
            + Style.wrap(" │", Style.GRAY, enabled=use_color)
        )

    lines.append(Style.wrap(sep, Style.GRAY, enabled=use_color))
    tot_cells = [pad_cell(total_row[i], col_widths[i], truncate=True) for i in range(len(headers))]
    lines.append(
        Style.wrap("│ ", Style.GRAY, enabled=use_color)
        + Style.wrap(" │ ", Style.GRAY, enabled=use_color).join(tot_cells)
        + Style.wrap(" │", Style.GRAY, enabled=use_color)
    )
    lines.append(Style.wrap(bot, Style.GRAY, enabled=use_color))

    if db_path:
        footer = Style.wrap(f"  Warehouse: {db_path}", Style.GRAY, enabled=use_color)
        if max_width is not None and visible_len(footer) > max_width:
            footer = truncate_visible(footer, max_width)
        lines.append(footer)

    if max_width is not None:
        lines = [
            truncate_visible(line, max_width) if visible_len(line) > max_width else line
            for line in lines
        ]

    return "\n".join(lines)
