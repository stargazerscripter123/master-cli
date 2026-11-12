"""High-resolution Unicode Braille (2x4 dot matrix) graph engine, bars, and formatters."""

from __future__ import annotations
from typing import Iterable, List, Sequence

# Unicode Braille dot bitmasks:
# Left column (top to bottom): 0x01, 0x02, 0x04, 0x40
# Right column (top to bottom): 0x08, 0x10, 0x20, 0x80
_LEFT_DOTS = (0x01, 0x02, 0x04, 0x40)
_RIGHT_DOTS = (0x08, 0x10, 0x20, 0x80)
_BLOCKS = " ▁▂▃▄▅▆▇█"
_HBLOCKS = ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉", "█")


def render_braille_graph(
    values: Sequence[float],
    width: int,
    height: int,
    min_val: float = 0.0,
    max_val: float = 100.0,
    invert: bool = False,
    baseline: bool = False,
) -> List[str]:
    """Renders a 2D area-filled Braille graph of `width` chars x `height` rows.

    Each character cell encodes 2 horizontal samples x 4 vertical sub-rows.
    If `invert=True`, fills downward from the top row (for mirrored bottom graphs).
    If `baseline=True`, draws a subtle 1-dot baseline along the zero axis so 0% graphs still show a floor.
    """
    if width <= 0 or height <= 0:
        return []
    n_samples = width * 2
    span = max(1e-6, max_val - min_val)
    total_subrows = height * 4

    # Pad or slice values to match 2 * width
    if len(values) < n_samples:
        padded = [min_val] * (n_samples - len(values)) + list(values)
    else:
        padded = list(values[-n_samples:])

    # Convert each sample to integer sub-row height in [0, total_subrows]
    levels: List[int] = []
    for v in padded:
        norm = max(0.0, min(1.0, (v - min_val) / span))
        lvl = int(round(norm * total_subrows))
        if v > min_val and lvl == 0:
            lvl = 1
        elif baseline and lvl == 0:
            lvl = 1
        levels.append(lvl)

    rows_out: List[str] = []
    for r in range(height):
        chars: List[str] = []
        for c in range(width):
            lvl_l = levels[c * 2]
            lvl_r = levels[c * 2 + 1]
            mask = 0
            for sub in range(4):
                if not invert:
                    # Standard upward graph: r=0 is top, r=height-1 is bottom
                    # Global subrow index from bottom (0 .. total_subrows-1)
                    pos_from_bottom = (height - 1 - r) * 4 + (3 - sub)
                    if lvl_l > pos_from_bottom:
                        mask |= _LEFT_DOTS[sub]
                    if lvl_r > pos_from_bottom:
                        mask |= _RIGHT_DOTS[sub]
                else:
                    # Inverted downward graph: r=0 is top, fills downward
                    pos_from_top = r * 4 + sub
                    if lvl_l > pos_from_top:
                        mask |= _LEFT_DOTS[sub]
                    if lvl_r > pos_from_top:
                        mask |= _RIGHT_DOTS[sub]
            chars.append(" " if mask == 0 else chr(0x2800 + mask))
        rows_out.append("".join(chars))
    return rows_out


def render_braille_sparkline(
    values: Iterable[float],
    width: int,
    min_val: float = 0.0,
    max_val: float = 100.0,
    baseline: bool = True,
) -> str:
    """Renders a 1-row high-resolution Braille sparkline (2*width samples, 4 vertical levels)."""
    seq = list(values)
    rows = render_braille_graph(seq, width=width, height=1, min_val=min_val, max_val=max_val, invert=False, baseline=baseline)
    return rows[0] if rows else ("⣀" * max(0, width) if baseline else " " * max(0, width))


def render_bar_parts(pct: float, width: int, style: str = "braille") -> tuple[str, str]:
    """Returns (filled_part, empty_track_part) of a horizontal Braille dotted bar of `width` characters."""
    if width <= 0:
        return ("", "")
    clamped = max(0.0, min(100.0, pct))
    full_dots = int(round((clamped / 100.0) * (width * 2)))
    filled: List[str] = []
    empty: List[str] = []
    for i in range(width):
        rem = full_dots - i * 2
        if rem >= 2:
            filled.append("⣿")
        elif rem == 1:
            filled.append("⡇")
        else:
            empty.append("⣀")
    return ("".join(filled), "".join(empty))


def render_bar(pct: float, width: int, style: str = "braille") -> str:
    """Renders a smooth horizontal Braille progress bar of `width` characters."""
    filled_s, empty_s = render_bar_parts(pct, width, style=style)
    return (filled_s + empty_s)[: max(0, width)]


def fmt_bytes(num_bytes: float | int, precision: int = 1) -> str:
    """Formats byte count into human-readable KiB/MiB/GiB/TiB."""
    val = float(max(0, num_bytes))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if val < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(val)}B"
            return f"{val:.{precision}f}{unit}"
        val /= 1024.0
    return f"{val:.1f}TiB"


def fmt_speed(bps: float, precision: int = 1) -> str:
    """Formats bytes/sec into human-readable B/s, KiB/s, MiB/s, GiB/s."""
    val = float(max(0.0, bps))
    for unit in ("B/s", "KiB/s", "MiB/s", "GiB/s"):
        if val < 1024.0 or unit == "GiB/s":
            if unit == "B/s":
                return f"{int(val)} B/s"
            return f"{val:.{precision}f} {unit}"
        val /= 1024.0
    return f"{val:.1f} GiB/s"


def fmt_duration(seconds: float) -> str:
    """Formats uptime seconds into Xd HH:MM:SS or HH:MM:SS."""
    sec = int(max(0, seconds))
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours:02d}:{mins:02d}:{secs:02d}"
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def fmt_freq(mhz: int) -> str:
    """Formats MHz into GHz or MHz compactly."""
    if mhz <= 0:
        return "N/A"
    if mhz >= 1000:
        return f"{mhz / 1000.0:.2f}GHz"
    return f"{mhz}MHz"
