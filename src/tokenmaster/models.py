"""Data models for TokenMaster usage and credit metrics."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

DEFAULT_WEEKLY_WINDOW_SECONDS = 7 * 86400
_DURATION_RE = re.compile(
    r"^\s*(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\s*$",
    re.IGNORECASE,
)


def format_duration_short(seconds: float) -> str:
    """Format a duration in seconds into a compact human string (e.g. '1d 14h', '3h 20m', '45m')."""
    if not math.isfinite(seconds) or seconds <= 0:
        return "now"
    total_sec = max(1, int(round(seconds)))
    days, rem = divmod(total_sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{max(1, minutes)}m"


def _parse_duration_str(text: str) -> Optional[float]:
    """Parse a human duration string like '1d 14h', '⟳ 3h 20m', or 'in 2d 0h' into seconds."""
    raw = text.strip().lstrip("⟳").strip()
    if raw.lower().startswith("in "):
        raw = raw[3:].strip()
    if raw.lower() == "now":
        return 0.0
    m = _DURATION_RE.match(raw)
    if m and any(m.groups()):
        days = int(m.group(1) or 0)
        hours = int(m.group(2) or 0)
        minutes = int(m.group(3) or 0)
        secs = int(m.group(4) or 0)
        return float(days * 86400 + hours * 3600 + minutes * 60 + secs)
    return None


def parse_reset_remaining_seconds(
    resets_at: Optional[Any] = None,
    resets_in: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[float]:
    """Parse remaining seconds until reset from ``resets_at`` and/or ``resets_in``."""
    ref_now = now or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)

    if resets_at is not None and resets_at != "" and not isinstance(resets_at, bool):
        dt: Optional[datetime] = None
        is_minute_truncated_utc = False
        if isinstance(resets_at, (int, float)) and math.isfinite(resets_at):
            val = float(resets_at)
            if val > 1e12:
                val /= 1000.0
            if val >= 1e9:
                dt = datetime.fromtimestamp(val, tz=timezone.utc)
            else:
                return max(0.0, val)
        elif isinstance(resets_at, str):
            raw = resets_at.strip()
            if raw.isdigit():
                return parse_reset_remaining_seconds(int(raw), resets_in=resets_in, now=ref_now)
            try:
                if raw.endswith(" UTC"):
                    dt = datetime.strptime(raw, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                    is_minute_truncated_utc = True
                else:
                    iso_raw = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
                    dt = datetime.fromisoformat(iso_raw)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
            except ValueError:
                dt = None
                dur_from_at = _parse_duration_str(raw)
                if dur_from_at is not None:
                    return dur_from_at

        if dt is not None:
            diff = (dt - ref_now).total_seconds()
            if diff > 0:
                return float(diff)
            # Handle same-minute truncation from "%Y-%m-%d %H:%M UTC" when resets_in is a valid positive duration
            if is_minute_truncated_utc and -60.0 < diff <= 0.0 and isinstance(resets_in, str) and resets_in.strip():
                g_sec = _parse_duration_str(resets_in)
                if g_sec is not None and g_sec > 0:
                    return max(1.0, 60.0 + float(diff))
            return 0.0

    if isinstance(resets_in, str) and resets_in.strip():
        dur = _parse_duration_str(resets_in)
        if dur is not None:
            return dur

    return None


def format_reset_countdown(reset_value: Any) -> tuple[Optional[str], Optional[str]]:
    """Parse a timestamp or ISO string into (iso_str, human_countdown_str)."""
    if reset_value is None or reset_value == "" or isinstance(reset_value, bool):
        return None, None

    now = datetime.now(timezone.utc)
    dt: Optional[datetime] = None

    if isinstance(reset_value, (int, float)):
        if not math.isfinite(reset_value):
            return None, None
        # Handle milliseconds vs seconds vs relative seconds
        val = float(reset_value)
        if val > 1e12:
            val = val / 1000.0
        if val < 1e9:
            # Relative seconds from now
            total_sec = max(0, int(val))
            dt = datetime.fromtimestamp(now.timestamp() + total_sec, tz=timezone.utc)
        else:
            dt = datetime.fromtimestamp(val, tz=timezone.utc)
    elif isinstance(reset_value, str):
        raw = reset_value.strip()
        if raw.isdigit():
            return format_reset_countdown(int(raw))
        try:
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return reset_value, reset_value

    if dt is None:
        return None, None

    diff_sec = int((dt - now).total_seconds())
    iso_str = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if diff_sec <= 0:
        return iso_str, "now"

    days, rem = divmod(diff_sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    if days > 0:
        return iso_str, f"{days}d {hours}h"
    if hours > 0:
        return iso_str, f"{hours}h {minutes}m"
    return iso_str, f"{max(1, minutes)}m"


def format_amount(val: Optional[float], unit: str) -> str:
    """Format a numeric metric value with its unit."""
    if val is None:
        return "—"
    if unit == "USD" or unit == "$":
        if abs(val) > 0 and abs(val) < 0.01:
            return f"${val:.4f}"
        return f"${val:,.2f}"
    if unit == "%":
        return f"{val:.1f}%"
    if unit == "tokens":
        if val >= 1_000_000:
            return f"{val / 1_000_000:.2f}M tok"
        if val >= 1_000:
            return f"{val / 1_000:.1f}k tok"
        return f"{int(val)} tok"
    if unit in ("reqs", "queries", "steps", "credits"):
        if float(val).is_integer():
            return f"{int(val):,} {unit}"
        return f"{val:,.1f} {unit}"
    if float(val).is_integer():
        return f"{int(val):,} {unit}".strip()
    return f"{val:,.2f} {unit}".strip()


@dataclass
class UsageWindow:
    """Represents Weekly Usage and Remaining metrics, plus reset-aware pacing & ETA forecast."""

    used: Optional[float] = None
    remaining: Optional[float] = None
    limit: Optional[float] = None
    unit: str = "%"
    used_percent: Optional[float] = None
    remaining_percent: Optional[float] = None
    resets_at: Optional[str] = None
    resets_in: Optional[str] = None
    note: Optional[str] = None
    scoped_label: Optional[str] = None
    scoped_used_percent: Optional[float] = None
    scoped_remaining_percent: Optional[float] = None
    window_seconds: Optional[int] = None
    pace_used_percent: Optional[float] = None
    elapsed_percent: Optional[float] = None
    pace_ratio: Optional[float] = None
    pace_delta_percent: Optional[float] = None
    pace_status: Optional[str] = None
    projected_exhaustion_in: Optional[str] = None

    def _compute_pacing(self) -> None:
        """Compute reset-aware pacing (pace_ratio, pace_delta_percent, pace_status, projected_exhaustion_in)."""
        target_pct = (
            self.pace_used_percent
            if self.pace_used_percent is not None
            else self.used_percent
        )
        if target_pct is None or not math.isfinite(float(target_pct)):
            self.elapsed_percent = None
            self.pace_ratio = None
            self.pace_delta_percent = None
            self.pace_status = None
            self.projected_exhaustion_in = None
            return

        rem_sec = parse_reset_remaining_seconds(self.resets_at, self.resets_in)
        if rem_sec is None or rem_sec <= 0.0:
            self.elapsed_percent = None
            self.pace_ratio = None
            self.pace_delta_percent = None
            self.pace_status = None
            self.projected_exhaustion_in = None
            return

        if self.window_seconds is not None and self.window_seconds > 0:
            win_sec = float(self.window_seconds)
        elif rem_sec <= DEFAULT_WEEKLY_WINDOW_SECONDS:
            win_sec = float(DEFAULT_WEEKLY_WINDOW_SECONDS)
        elif rem_sec <= 30 * 86400:
            win_sec = float(30 * 86400)
        else:
            win_sec = float(rem_sec)

        # Guard against slight clock skew or a longer billing cycle timestamp
        if rem_sec > win_sec:
            if rem_sec <= win_sec * 1.02:
                rem_sec = win_sec
            else:
                for tier_sec in (86400.0, float(DEFAULT_WEEKLY_WINDOW_SECONDS), float(30 * 86400)):
                    if tier_sec >= rem_sec:
                        win_sec = tier_sec
                        break
                else:
                    win_sec = float(rem_sec)

        elapsed_sec = max(0.0, win_sec - rem_sec)
        elapsed_pct = min(100.0, max(0.0, (elapsed_sec / win_sec) * 100.0))
        used_pct = max(0.0, min(100.0, float(target_pct)))
        rem_pct = max(0.0, 100.0 - used_pct)
        delta_pct = used_pct - elapsed_pct

        self.elapsed_percent = round(elapsed_pct, 1)
        self.pace_delta_percent = round(delta_pct, 1)

        # Floor denominator at 2.0% of window so first-minute usage doesn't explode ratio
        eff_elapsed_pct = max(elapsed_pct, 2.0)
        ratio = used_pct / eff_elapsed_pct
        self.pace_ratio = round(ratio, 2)

        if used_pct >= 100.0 or rem_pct <= 0.0:
            self.pace_status = "overburn"
            self.projected_exhaustion_in = "now"
            return

        if used_pct <= 0.0:
            self.pace_status = "surplus"
            self.projected_exhaustion_in = None
            return

        eff_elapsed_sec = max(elapsed_sec, win_sec * 0.02)
        rate_per_sec = used_pct / eff_elapsed_sec
        sec_to_zero = rem_pct / rate_per_sec if rate_per_sec > 0 else float("inf")

        if ratio > 1.35 and delta_pct >= 10.0 and sec_to_zero < rem_sec:
            self.pace_status = "overburn"
            self.projected_exhaustion_in = format_duration_short(sec_to_zero)
        elif ratio > 1.05 and delta_pct >= 3.0:
            self.pace_status = "fast"
            self.projected_exhaustion_in = (
                format_duration_short(sec_to_zero)
                if (delta_pct >= 5.0 and sec_to_zero < rem_sec)
                else None
            )
        elif ratio < 0.85 or delta_pct <= -5.0:
            self.pace_status = "surplus"
            self.projected_exhaustion_in = None
        else:
            self.pace_status = "on_track"
            self.projected_exhaustion_in = None

    def pace_summary_str(self) -> Optional[str]:
        """Return a human-readable single-line summary of quota burn pace and exhaustion ETA."""
        if (
            self.pace_status is None
            or self.pace_ratio is None
            or self.elapsed_percent is None
            or self.pace_delta_percent is None
        ):
            return None
        base = f"{self.pace_ratio:.2f}x ({self.pace_delta_percent:+.1f}% vs {self.elapsed_percent:.0f}% elapsed)"
        if self.projected_exhaustion_in == "now":
            return f"{base} • Exhausted"
        if self.projected_exhaustion_in:
            return f"{base} • Est. exhaustion in {self.projected_exhaustion_in}"
        if self.pace_status == "surplus":
            return f"{base} • Safe until reset"
        return f"{base} • On track"

    def finalize(self) -> "UsageWindow":
        """Compute missing percentages, remaining values, and pacing metrics consistently."""
        if self.limit is not None and self.limit > 0:
            if self.used is not None and self.remaining is None:
                self.remaining = max(0.0, self.limit - self.used)
            elif self.remaining is not None and self.used is None:
                self.used = max(0.0, self.limit - self.remaining)
            if self.used is not None and self.used_percent is None:
                self.used_percent = min(100.0, max(0.0, (self.used / self.limit) * 100.0))

        if self.used_percent is not None and self.remaining_percent is None:
            self.remaining_percent = max(0.0, 100.0 - self.used_percent)
        elif self.remaining_percent is not None and self.used_percent is None:
            self.used_percent = max(0.0, 100.0 - self.remaining_percent)

        if self.scoped_used_percent is not None and self.scoped_remaining_percent is None:
            self.scoped_remaining_percent = max(0.0, 100.0 - self.scoped_used_percent)
        elif self.scoped_remaining_percent is not None and self.scoped_used_percent is None:
            self.scoped_used_percent = max(0.0, 100.0 - self.scoped_remaining_percent)

        if self.unit == "%" and self.used is None and self.used_percent is not None:
            self.used = round(self.used_percent, 1)
            self.remaining = round(self.remaining_percent or 0.0, 1)
            self.limit = 100.0

        self._compute_pacing()
        return self

    def used_str(self) -> str:
        if self.used is None and self.used_percent is None:
            return "—"
        if self.unit == "%":
            pct = self.used_percent if self.used_percent is not None else self.used
            if self.scoped_used_percent is not None:
                s_lbl = self.scoped_label or "Fable"
                return f"All: {pct:.1f}% | {s_lbl}: {self.scoped_used_percent:.1f}%"
            return f"{pct:.1f}%"
        base = format_amount(self.used, self.unit)
        if self.limit is not None and self.limit > 0:
            lim = format_amount(self.limit, self.unit)
            if self.used_percent is not None:
                return f"{base} / {lim} ({self.used_percent:.0f}%)"
            return f"{base} / {lim}"
        return base

    def remaining_str(self) -> str:
        if self.remaining is None and self.remaining_percent is None:
            return "—"
        if self.unit == "%":
            pct = self.remaining_percent if self.remaining_percent is not None else self.remaining
            if self.scoped_remaining_percent is not None:
                s_lbl = self.scoped_label or "Fable"
                return f"All: {pct:.1f}% | {s_lbl}: {self.scoped_remaining_percent:.1f}%"
            return f"{pct:.1f}%"
        base = format_amount(self.remaining, self.unit)
        if self.remaining_percent is not None:
            return f"{base} ({self.remaining_percent:.0f}%)"
        return base


@dataclass
class CreditBalance:
    """Represents Credit Usage and Credit Remaining metrics."""

    used: Optional[float] = None
    remaining: Optional[float] = None
    limit: Optional[float] = None
    unit: str = "USD"
    used_percent: Optional[float] = None
    remaining_percent: Optional[float] = None
    is_unlimited: bool = False
    note: Optional[str] = None

    def finalize(self) -> "CreditBalance":
        """Compute derived fields for credit usage & remaining."""
        if self.limit is not None and self.limit > 0:
            if self.used is not None and self.remaining is None:
                self.remaining = max(0.0, self.limit - self.used)
            elif self.remaining is not None and self.used is None:
                self.used = max(0.0, self.limit - self.remaining)
            if self.used is not None and self.used_percent is None:
                self.used_percent = min(100.0, max(0.0, (self.used / self.limit) * 100.0))

        if self.used_percent is not None and self.remaining_percent is None:
            self.remaining_percent = max(0.0, 100.0 - self.used_percent)
        elif self.remaining_percent is not None and self.used_percent is None:
            self.used_percent = max(0.0, 100.0 - self.remaining_percent)

        return self

    def used_str(self) -> str:
        if self.used is None:
            return "—"
        base = format_amount(self.used, self.unit)
        if self.limit is not None and self.limit > 0:
            lim = format_amount(self.limit, self.unit)
            if self.used_percent is not None:
                return f"{base} / {lim} ({self.used_percent:.0f}%)"
            return f"{base} / {lim}"
        return base

    def remaining_str(self) -> str:
        if self.is_unlimited:
            return "Unlimited"
        if self.remaining is None:
            return "—"
        base = format_amount(self.remaining, self.unit)
        if self.remaining_percent is not None:
            return f"{base} ({self.remaining_percent:.0f}%)"
        return base


@dataclass
class SubMetric:
    """Granular sub-window or per-model pool breakdown."""

    name: str
    used_str: str
    remaining_str: str
    used_percent: Optional[float] = None
    resets_in: Optional[str] = None


@dataclass
class ProviderReport:
    """Complete usage & credit report for a single provider (or named subscription profile)."""

    provider_id: str
    display_name: str
    status: str = "unconfigured"  # "ok", "partial", "unconfigured", "error"
    plan_tier: Optional[str] = None
    source: Optional[str] = None
    weekly: UsageWindow = field(default_factory=UsageWindow)
    credits: CreditBalance = field(default_factory=CreditBalance)
    sub_metrics: list[SubMetric] = field(default_factory=list)
    error_message: Optional[str] = None
    auth_hint: Optional[str] = None
    fetched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    fetch_ms: Optional[int] = None  # wall-clock latency of this provider's fetch()
    account: Optional[str] = None   # None for default subscription, or named profile id

    @property
    def instance_id(self) -> str:
        """Unique key for this provider subscription ('claude' or 'claude:work')."""
        acct = self.account.strip() if isinstance(self.account, str) else ""
        if acct and acct.lower() != "default":
            return f"{self.provider_id}:{acct}"
        return self.provider_id

    def finalize(self) -> "ProviderReport":
        if isinstance(self.account, str):
            acct_clean = self.account.strip()
            self.account = None if not acct_clean or acct_clean.lower() == "default" else acct_clean
        elif self.account is not None:
            self.account = None
        self.weekly.finalize()
        self.credits.finalize()
        return self

    def to_dict(self) -> dict[str, Any]:
        self.finalize()
        d = asdict(self)
        d["instance_id"] = self.instance_id
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProviderReport":
        """Reconstruct a ``ProviderReport`` from a serialized dictionary."""
        if not isinstance(data, dict):
            raise ValueError("ProviderReport.from_dict expects a dict")
        w_raw = data.get("weekly") if isinstance(data.get("weekly"), dict) else {}
        c_raw = data.get("credits") if isinstance(data.get("credits"), dict) else {}
        sm_raw = data.get("sub_metrics") if isinstance(data.get("sub_metrics"), list) else []

        def _f(v: Any) -> Optional[float]:
            if v is None or isinstance(v, (bool, dict, list)):
                return None
            try:
                fv = float(v)
                return fv if math.isfinite(fv) else None
            except (TypeError, ValueError, OverflowError):
                return None

        def _i(v: Any) -> Optional[int]:
            fv = _f(v)
            if fv is None:
                return None
            try:
                return int(fv)
            except (TypeError, ValueError, OverflowError):
                return None

        def _s(v: Any) -> Optional[str]:
            if v is None or isinstance(v, (bool, dict, list)):
                return None
            return str(v)

        weekly = UsageWindow(
            used=_f(w_raw.get("used")),
            remaining=_f(w_raw.get("remaining")),
            limit=_f(w_raw.get("limit")),
            unit=_s(w_raw.get("unit")) or "%",
            used_percent=_f(w_raw.get("used_percent")),
            remaining_percent=_f(w_raw.get("remaining_percent")),
            resets_at=_s(w_raw.get("resets_at")),
            resets_in=_s(w_raw.get("resets_in")),
            note=_s(w_raw.get("note")),
            scoped_label=_s(w_raw.get("scoped_label")),
            scoped_used_percent=_f(w_raw.get("scoped_used_percent")),
            scoped_remaining_percent=_f(w_raw.get("scoped_remaining_percent")),
            window_seconds=_i(w_raw.get("window_seconds")),
            pace_used_percent=_f(w_raw.get("pace_used_percent")),
        )
        credits = CreditBalance(
            used=_f(c_raw.get("used")),
            remaining=_f(c_raw.get("remaining")),
            limit=_f(c_raw.get("limit")),
            unit=_s(c_raw.get("unit")) or "USD",
            used_percent=_f(c_raw.get("used_percent")),
            remaining_percent=_f(c_raw.get("remaining_percent")),
            is_unlimited=(c_raw.get("is_unlimited") is True),
            note=_s(c_raw.get("note")),
        )
        sub_metrics: list[SubMetric] = []
        for item in sm_raw:
            if isinstance(item, dict):
                sub_metrics.append(
                    SubMetric(
                        name=_s(item.get("name")) or "",
                        used_str=_s(item.get("used_str")) or "—",
                        remaining_str=_s(item.get("remaining_str")) or "—",
                        used_percent=_f(item.get("used_percent")),
                        resets_in=_s(item.get("resets_in")),
                    )
                )
        pid = _s(data.get("provider_id")) or ""
        disp = _s(data.get("display_name")) or pid
        acct_val = data.get("account")
        acct_clean = acct_val.strip().lower() if isinstance(acct_val, str) else ""
        acct = acct_clean if (acct_clean and acct_clean != "default") else None
        return cls(
            provider_id=pid,
            display_name=disp,
            status=_s(data.get("status")) or "unconfigured",
            plan_tier=_s(data.get("plan_tier")),
            source=_s(data.get("source")),
            weekly=weekly,
            credits=credits,
            sub_metrics=sub_metrics,
            error_message=_s(data.get("error_message")),
            auth_hint=_s(data.get("auth_hint")),
            fetched_at=_s(data.get("fetched_at")) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            fetch_ms=_f(data.get("fetch_ms")),
            account=acct,
        ).finalize()
