"""Quota threshold & auth-expiry notification engine with per-cycle deduplication."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from tokenmaster.config import get_cache_dir
from tokenmaster.models import ProviderReport, parse_reset_remaining_seconds
from tokenmaster.providers.base import atomic_write_text, critical_section, file_lock

ALERT_STATE_FILENAME = "alert_state.json"
ALERT_STATE_VERSION = 1

DEFAULT_THRESHOLDS: tuple[float, ...] = (80.0, 95.0, 100.0)
DEFAULT_AUTH_COOLDOWN_SECONDS: float = 21_600.0  # 6 hours

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
_SECRET_SCRUB_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|xai-[A-Za-z0-9_-]{8,}|Bearer\s+\S+|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|sso=[^\s;]+)",
    re.IGNORECASE,
)
_AUTH_HTTP_STATUS_RE = re.compile(r"\b(?:401|403)\b")
_AUTH_ERROR_KEYWORDS: tuple[str, ...] = (
    "unauthorized",
    "forbidden",
    "expired",
    "invalid_grant",
    "invalid_token",
    "re-authenticate",
    "login to refresh",
    "token rejected",
    "auth login",
)


def get_alert_state_path() -> Path:
    """Return the path to ``alert_state.json``."""
    explicit = (os.environ.get("TOKENMASTER_ALERT_STATE_PATH") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return get_cache_dir() / ALERT_STATE_FILENAME


def is_report_configured_or_errored(report: ProviderReport) -> bool:
    """Return True if a provider report is configured or has an active error message."""
    return report.status != "unconfigured" or bool((report.error_message or "").strip())


def _finite_float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        fv = float(v)
        return fv if math.isfinite(fv) else None
    except (TypeError, ValueError, OverflowError):
        return None


def parse_thresholds(raw: Any) -> list[float]:
    """Parse and validate quota usage percentage thresholds in ``(0, 100]``.

    Accepts ``None`` (returns ``[80.0, 95.0, 100.0]``), a comma-separated string
    such as ``"80,95,100"``, a number, or an iterable of strings/numbers.
    """
    if raw is None:
        return list(DEFAULT_THRESHOLDS)

    items: list[Any] = []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
        if not any(parts):
            raise ValueError("Threshold list cannot be empty")
        for p in parts:
            if not p:
                raise ValueError(f"Invalid empty threshold in '{raw}'")
            items.append(p)
    elif isinstance(raw, Iterable) and not isinstance(raw, (str, bytes, dict)):
        for elem in raw:
            if isinstance(elem, str):
                sub_parts = [p.strip() for p in elem.split(",")]
                if not any(sub_parts):
                    raise ValueError("Threshold list cannot be empty")
                for p in sub_parts:
                    if not p:
                        raise ValueError(f"Invalid empty threshold in '{elem}'")
                    items.append(p)
            else:
                items.append(elem)
    else:
        items.append(raw)

    if not items:
        raise ValueError("At least one threshold in (0, 100] is required")

    parsed: set[float] = set()
    for item in items:
        if isinstance(item, bool):
            raise ValueError("Threshold must be a number between 0 and 100")
        fv = _finite_float(item)
        if fv is None or fv <= 0.0 or fv > 100.0:
            raise ValueError(
                f"Invalid threshold '{item}': must be a finite number in (0, 100]"
            )
        rounded = round(fv, 2)
        if rounded <= 0.0 or rounded > 100.0:
            raise ValueError(
                f"Invalid threshold '{item}': must be at least 0.01 and at most 100"
            )
        parsed.add(rounded)

    return sorted(parsed)


def sanitize_alert_text(text: Any, max_len: int = 160) -> str:
    """Sanitize notification text: strip ANSI/control chars and scrub secret patterns."""
    if text is None:
        return ""
    s = _ANSI_ESCAPE_RE.sub("", str(text))
    s = _SECRET_SCRUB_RE.sub("[REDACTED]", s)
    s = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", s).strip()
    if len(s) > max_len:
        return s[: max(1, max_len - 1)].rstrip() + "…"
    return s


def _parse_resets_at_epoch(resets_at: Optional[str]) -> Optional[float]:
    """Parse ``resets_at`` string (e.g. ``2026-11-15 09:00 UTC`` or ISO-8601) into UTC epoch seconds."""
    if not isinstance(resets_at, str):
        return None
    s = resets_at.strip()
    if not s or s == "—":
        return None
    try:
        if s.endswith(" UTC"):
            dt = datetime.strptime(s[:-4].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            return dt.timestamp()
        norm = s[:-1] + "+00:00" if s.endswith("Z") else s
        dt = datetime.fromisoformat(norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _resolve_reset_epoch(
    resets_at: Optional[str],
    resets_in: Optional[str],
    now_ts: float,
) -> tuple[Optional[float], bool]:
    """Resolve ``(reset_epoch_seconds, is_coarse_countdown)`` from ``resets_at`` or ``resets_in``."""
    ep = _parse_resets_at_epoch(resets_at)
    if ep is not None:
        return ep, False
    if isinstance(resets_in, str) and resets_in.strip() and resets_in.strip() != "—":
        now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        rem_sec = parse_reset_remaining_seconds(None, resets_in, now=now_dt)
        if rem_sec is not None and rem_sec > 0.0:
            return now_ts + rem_sec, (rem_sec >= 86400.0)
    return None, False


def _fmt_pct(val: float) -> str:
    if abs(val - round(val)) < 0.05:
        return f"{int(round(val))}%"
    return f"{val:.1f}%"


@dataclass
class AlertEvent:
    """Single deduplicated quota or authentication alert event."""

    instance_id: str
    provider_id: str
    account_id: str
    kind: str  # "weekly_quota" | "five_hour_quota" | "credit_quota" | "auth_error"
    severity: str  # "warn" | "critical"
    threshold: Optional[float]
    used_percent: Optional[float]
    remaining_percent: Optional[float]
    resets_at: Optional[str]
    resets_in: Optional[str]
    title: str
    message: str
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "kind": self.kind,
            "severity": self.severity,
            "threshold": self.threshold,
            "used_percent": round(self.used_percent, 1) if self.used_percent is not None else None,
            "remaining_percent": (
                round(self.remaining_percent, 1) if self.remaining_percent is not None else None
            ),
            "resets_at": self.resets_at,
            "resets_in": self.resets_in,
            "title": self.title,
            "message": self.message,
            "timestamp": self.timestamp,
        }


def send_desktop_notification(title: str, message: str) -> bool:
    """Send a native OS desktop notification without any third-party dependencies.

    - macOS: ``osascript`` using positional ``argv`` (immune to AppleScript quote/code injection).
    - Linux: ``notify-send`` using positional arguments after ``--``.
    Returns ``True`` if a desktop notification binary succeeded.
    """
    clean_title = sanitize_alert_text(title, max_len=80) or "TokenMaster Alert"
    clean_msg = sanitize_alert_text(message, max_len=200)
    if not clean_msg:
        return False

    try:
        if sys.platform == "darwin":
            osascript_bin = shutil.which("osascript") or "/usr/bin/osascript"
            proc = subprocess.run(
                [
                    osascript_bin,
                    "-e",
                    "on run argv",
                    "-e",
                    "display notification (item 2 of argv) with title (item 1 of argv)",
                    "-e",
                    "end run",
                    "--",
                    clean_title,
                    clean_msg,
                ],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            return proc.returncode == 0

        if sys.platform.startswith("linux"):
            notify_bin = shutil.which("notify-send")
            if not notify_bin:
                return False
            proc = subprocess.run(
                [
                    notify_bin,
                    "--app-name=TokenMaster",
                    "--",
                    clean_title,
                    clean_msg,
                ],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            return proc.returncode == 0
    except Exception:
        return False

    return False


class AlertManager:
    """Stateful alert evaluator backed by ``~/.cache/tokenmaster/alert_state.json``."""

    def __init__(self, state_path: Optional[Path] = None) -> None:
        self.path: Path = (state_path or get_alert_state_path()).expanduser()
        self._lock_path: Path = self.path.parent / f".{self.path.name}.lock"

    def _load_entries_unlocked(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                isinstance(raw, dict)
                and not isinstance(raw.get("version"), bool)
                and raw.get("version") == ALERT_STATE_VERSION
                and isinstance(raw.get("entries"), dict)
            ):
                return {
                    str(k): dict(v)
                    for k, v in raw["entries"].items()
                    if isinstance(v, dict)
                }
        except Exception:
            pass
        return {}

    def _save_entries_unlocked(self, entries: dict[str, dict[str, Any]], now_ts: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": ALERT_STATE_VERSION,
            "updated_at": now_ts,
            "entries": entries,
        }
        atomic_write_text(self.path, json.dumps(payload, indent=2) + "\n", mode=0o600)

    def clear_state(self) -> bool:
        """Delete ``alert_state.json`` under lock if it exists."""
        try:
            if not self.path.exists() and not self.path.is_symlink():
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with critical_section(), file_lock(self._lock_path):
                if self.path.is_file() or self.path.is_symlink():
                    self.path.unlink()
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _is_same_quota_cycle(
        prev: dict[str, Any],
        *,
        used_pct: float,
        min_threshold: float,
        resets_at: Optional[str],
        resets_in: Optional[str],
        now_ts: float,
        window_seconds: float,
    ) -> bool:
        """Return True if ``prev`` state belongs to the same quota reset cycle."""
        prev_notified_at = _finite_float(prev.get("notified_at")) or 0.0
        if prev_notified_at > 0 and (now_ts - prev_notified_at) >= max(3600.0, window_seconds):
            return False

        # If usage dropped significantly below the lowest alert threshold (e.g. window reset or tier upgrade),
        # treat as a fresh cycle.
        prev_used = _finite_float(prev.get("last_used_percent"))
        if used_pct < max(0.0, min_threshold - 5.0):
            return False
        if prev_used is not None and (prev_used - used_pct) >= 15.0 and used_pct < min_threshold:
            return False

        cur_epoch, cur_coarse = _resolve_reset_epoch(resets_at, resets_in, now_ts)
        prev_epoch = _finite_float(prev.get("reset_epoch"))
        prev_coarse = bool(prev.get("reset_epoch_coarse", False))
        tolerance = 5400.0 if (cur_coarse or prev_coarse) else 1800.0

        if prev_epoch is not None:
            if cur_epoch is not None:
                if abs(cur_epoch - prev_epoch) > tolerance:
                    return False
                return True
            if now_ts >= prev_epoch + 60.0:
                return False

        cur_resets_str = (resets_at or "").strip()
        prev_resets_str = str(prev.get("resets_at") or "").strip()
        if cur_resets_str and prev_resets_str and cur_epoch is None and prev_epoch is None:
            if cur_resets_str != prev_resets_str:
                return False

        return True

    def _evaluate_quota_metric(
        self,
        entries: dict[str, dict[str, Any]],
        *,
        instance_id: str,
        provider_id: str,
        account_id: str,
        scope: str,
        kind: str,
        metric_label: str,
        used_pct: Optional[float],
        rem_pct: Optional[float],
        resets_at: Optional[str],
        resets_in: Optional[str],
        exhaustion_eta: Optional[str],
        pace_level: Optional[str],
        window_seconds: float,
        thresholds: list[float],
        now_ts: float,
    ) -> Optional[AlertEvent]:
        if used_pct is None or not math.isfinite(used_pct):
            return None

        clamped_used = max(0.0, min(100.0, float(used_pct)))
        clamped_rem = (
            max(0.0, min(100.0, float(rem_pct)))
            if (rem_pct is not None and math.isfinite(rem_pct))
            else max(0.0, 100.0 - clamped_used)
        )
        state_key = f"{instance_id}|{scope}"
        min_t = thresholds[0]

        prev = entries.get(state_key)
        if prev is not None and not self._is_same_quota_cycle(
            prev,
            used_pct=clamped_used,
            min_threshold=min_t,
            resets_at=resets_at,
            resets_in=resets_in,
            now_ts=now_ts,
            window_seconds=window_seconds,
        ):
            entries.pop(state_key, None)
            prev = None

        reached = [t for t in thresholds if clamped_used >= (t - 1e-6)]
        if not reached:
            if clamped_used < max(0.0, min_t - 5.0):
                entries.pop(state_key, None)
            return None

        highest_t = max(reached)
        prev_max_t = _finite_float(prev.get("max_threshold_notified")) if prev else None
        cur_epoch, cur_coarse = _resolve_reset_epoch(resets_at, resets_in, now_ts)

        if prev_max_t is not None and highest_t <= prev_max_t + 1e-6:
            if prev is not None:
                prev_used = _finite_float(prev.get("last_used_percent"))
                if (
                    prev_used is not None
                    and (prev_used - clamped_used) >= 15.0
                    and highest_t < prev_max_t - 1e-6
                ):
                    # Usage dropped significantly from a higher threshold (e.g. 96% -> 81%):
                    # de-escalate max_threshold_notified to highest_t so climbing back to 95% re-alerts.
                    prev["max_threshold_notified"] = highest_t
                    prev["last_used_percent"] = round(clamped_used, 2)
                else:
                    prev["last_used_percent"] = max(prev_used or 0.0, round(clamped_used, 2))
                if cur_epoch is not None and prev.get("reset_epoch") is None:
                    prev["reset_epoch"] = cur_epoch
                    prev["reset_epoch_coarse"] = cur_coarse
            return None

        severity = "critical" if (highest_t >= 95.0 or clamped_rem <= 5.0) else "warn"
        safe_inst = sanitize_alert_text(instance_id, max_len=40)
        title = (
            f"TokenMaster CRITICAL: {safe_inst}"
            if severity == "critical"
            else f"TokenMaster Quota Warning: {safe_inst}"
        )

        parts = [
            f"{safe_inst} {metric_label} at {_fmt_pct(clamped_used)} used ({_fmt_pct(clamped_rem)} left, threshold {_fmt_pct(highest_t)})"
        ]
        if resets_in and str(resets_in).strip() and str(resets_in).strip() != "—":
            parts.append(f"resets in {sanitize_alert_text(resets_in, max_len=24)}")
        eta_clean = str(exhaustion_eta or "").strip()
        if (
            eta_clean
            and eta_clean.lower() not in ("—", "now")
            and pace_level in ("overburn", "fast", "warn", "critical")
        ):
            parts.append(f"ETA ~{sanitize_alert_text(eta_clean, max_len=24)} to 0%")

        msg = " · ".join(parts)

        entries[state_key] = {
            "instance_id": instance_id,
            "scope": scope,
            "max_threshold_notified": highest_t,
            "last_used_percent": round(clamped_used, 2),
            "resets_at": sanitize_alert_text(resets_at, max_len=40) if resets_at else None,
            "reset_epoch": cur_epoch,
            "reset_epoch_coarse": cur_coarse,
            "notified_at": now_ts,
        }

        return AlertEvent(
            instance_id=instance_id,
            provider_id=provider_id,
            account_id=account_id,
            kind=kind,
            severity=severity,
            threshold=highest_t,
            used_percent=clamped_used,
            remaining_percent=clamped_rem,
            resets_at=sanitize_alert_text(resets_at, max_len=40) if resets_at else None,
            resets_in=sanitize_alert_text(resets_in, max_len=24) if resets_in else None,
            title=title,
            message=msg,
            timestamp=now_ts,
        )

    def _evaluate_auth_or_error(
        self,
        entries: dict[str, dict[str, Any]],
        report: ProviderReport,
        *,
        auth_cooldown_seconds: float,
        now_ts: float,
    ) -> Optional[AlertEvent]:
        instance_id = report.instance_id
        state_key = f"{instance_id}|auth_error"

        # When primary live quota succeeded (`status == "ok"`), clear any prior error state
        # and do not alert on secondary optional endpoint warnings (e.g. OpenRouter /credits 403).
        if report.status == "ok":
            entries.pop(state_key, None)
            return None

        err_lower = (report.error_message or "").strip().lower()
        hint_lower = (report.auth_hint or "").strip().lower() if report.status == "partial" else ""
        has_auth_kw = bool(
            (
                err_lower
                and (
                    _AUTH_HTTP_STATUS_RE.search(err_lower) is not None
                    or any(kw in err_lower for kw in _AUTH_ERROR_KEYWORDS)
                )
            )
            or (
                hint_lower
                and any(
                    kw in hint_lower
                    for kw in ("expired", "rejected", "re-authenticate", "login to refresh")
                )
            )
        )
        is_error_status = report.status == "error"

        if not is_error_status and not has_auth_kw and not err_lower:
            entries.pop(state_key, None)
            return None
        if not is_error_status and not has_auth_kw:
            return None

        error_kind = "auth_expired" if has_auth_kw else "fetch_error"
        prev = entries.get(state_key)
        if prev is not None:
            prev_ts = _finite_float(prev.get("notified_at")) or 0.0
            prev_kind = str(prev.get("error_kind") or "")
            # Allow escalation from fetch_error -> auth_expired immediately; otherwise honor cooldown
            if (
                prev_kind == error_kind
                and auth_cooldown_seconds > 0
                and 0.0 <= (now_ts - prev_ts) < auth_cooldown_seconds
            ):
                return None

        safe_inst = sanitize_alert_text(instance_id, max_len=40)
        acct_id = (report.account or "default").strip().lower()
        if error_kind == "auth_expired":
            severity = "critical"
            title = f"TokenMaster Auth Alert: {safe_inst}"
            msg = (
                f"{safe_inst} authentication expired or unauthorized "
                f"(run `tokenmaster login {safe_inst}` or `tokenmaster auth {safe_inst}`)"
            )
        else:
            severity = "warn"
            title = f"TokenMaster Provider Error: {safe_inst}"
            msg = f"{safe_inst} failed to fetch live usage (run `tokenmaster doctor`)"

        entries[state_key] = {
            "instance_id": instance_id,
            "scope": "auth_error",
            "error_kind": error_kind,
            "notified_at": now_ts,
        }

        return AlertEvent(
            instance_id=instance_id,
            provider_id=report.provider_id,
            account_id=acct_id,
            kind="auth_error",
            severity=severity,
            threshold=None,
            used_percent=None,
            remaining_percent=None,
            resets_at=None,
            resets_in=None,
            title=title,
            message=msg,
            timestamp=now_ts,
        )

    def process_reports(
        self,
        reports: Iterable[ProviderReport],
        *,
        thresholds: Optional[Iterable[Any] | str] = None,
        auth_cooldown_seconds: float = DEFAULT_AUTH_COOLDOWN_SECONDS,
        send_desktop: bool = True,
        desktop_sender: Optional[Callable[[str, str], bool]] = None,
        dry_run: bool = False,
        now_ts: Optional[float] = None,
    ) -> list[AlertEvent]:
        """Evaluate ``reports``, deduplicate against ``alert_state.json``, and optionally send desktop alerts."""
        norm_thresholds = parse_thresholds(thresholds)
        ts = (
            float(now_ts)
            if (now_ts is not None and not isinstance(now_ts, bool) and math.isfinite(float(now_ts)))
            else time.time()
        )
        cooldown = (
            max(0.0, float(auth_cooldown_seconds))
            if _finite_float(auth_cooldown_seconds) is not None
            else DEFAULT_AUTH_COOLDOWN_SECONDS
        )
        sender = desktop_sender or send_desktop_notification

        triggered: list[AlertEvent] = []
        use_disk_lock = (not dry_run) or self.path.exists()
        if use_disk_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_ctx = file_lock(self._lock_path) if use_disk_lock else nullcontext()

        with critical_section(), lock_ctx:
            entries = self._load_entries_unlocked()

            for r in reports:
                if not is_report_configured_or_errored(r):
                    continue
                r.finalize()
                inst_id = r.instance_id
                pid = r.provider_id
                acct = (r.account or "default").strip().lower()

                # 1. Check auth / provider error state
                auth_ev = self._evaluate_auth_or_error(
                    entries,
                    r,
                    auth_cooldown_seconds=cooldown,
                    now_ts=ts,
                )
                if auth_ev is not None:
                    triggered.append(auth_ev)

                # 2. Check Weekly Quota (strictest of primary remaining, scoped remaining, or pace used)
                w_rem_cands: list[float] = []
                for cand in (
                    _finite_float(r.weekly.remaining_percent),
                    _finite_float(r.weekly.scoped_remaining_percent),
                ):
                    if cand is not None:
                        w_rem_cands.append(max(0.0, min(100.0, cand)))
                pace_u = _finite_float(r.weekly.pace_used_percent)
                if pace_u is not None:
                    w_rem_cands.append(max(0.0, min(100.0, 100.0 - pace_u)))

                if w_rem_cands:
                    eff_w_rem = min(w_rem_cands)
                    eff_w_used = 100.0 - eff_w_rem
                    win_sec = (
                        float(r.weekly.window_seconds)
                        if (_finite_float(r.weekly.window_seconds) or 0.0) > 0
                        else float(7 * 86400)
                    )
                    w_ev = self._evaluate_quota_metric(
                        entries,
                        instance_id=inst_id,
                        provider_id=pid,
                        account_id=acct,
                        scope="weekly",
                        kind="weekly_quota",
                        metric_label="Weekly Quota",
                        used_pct=eff_w_used,
                        rem_pct=eff_w_rem,
                        resets_at=r.weekly.resets_at,
                        resets_in=r.weekly.resets_in,
                        exhaustion_eta=r.weekly.projected_exhaustion_in,
                        pace_level=r.weekly.pace_status,
                        window_seconds=win_sec,
                        thresholds=norm_thresholds,
                        now_ts=ts,
                    )
                    if w_ev is not None:
                        triggered.append(w_ev)

                # 3. Check 5h Rolling Window sub-metrics (pick highest used_percent if multiple exist;
                # skip synthetic/weekly-capped 5h entries whose reset is >6h when weekly quota already covers them)
                best_5h = None
                best_5h_used = -1.0
                for sm in r.sub_metrics:
                    lbl_low = (sm.name or "").lower()
                    if "5h" in lbl_low or "5-hour" in lbl_low or "five_hour" in lbl_low:
                        sm_used = _finite_float(sm.used_percent)
                        if sm_used is None:
                            continue
                        sm_rem_sec = parse_reset_remaining_seconds(None, sm.resets_in)
                        if (
                            sm_rem_sec is not None
                            and sm_rem_sec > 6 * 3600
                            and w_rem_cands
                            and (100.0 - min(w_rem_cands)) >= sm_used
                        ):
                            continue
                        if sm_used > best_5h_used:
                            best_5h = sm
                            best_5h_used = sm_used

                if best_5h is not None:
                    sm_rem = max(0.0, min(100.0, 100.0 - best_5h_used))
                    s5_ev = self._evaluate_quota_metric(
                        entries,
                        instance_id=inst_id,
                        provider_id=pid,
                        account_id=acct,
                        scope="5h",
                        kind="five_hour_quota",
                        metric_label="5h Window",
                        used_pct=best_5h_used,
                        rem_pct=sm_rem,
                        resets_at=None,
                        resets_in=best_5h.resets_in,
                        exhaustion_eta=None,
                        pace_level=None,
                        window_seconds=float(5 * 3600),
                        thresholds=norm_thresholds,
                        now_ts=ts,
                    )
                    if s5_ev is not None:
                        triggered.append(s5_ev)

                # 4. Check Credit Balance if configured and not unlimited
                if not r.credits.is_unlimited:
                    c_used_pct = _finite_float(r.credits.used_percent)
                    c_rem_val = _finite_float(r.credits.remaining)
                    c_lim_val = _finite_float(r.credits.limit)
                    c_used_val = _finite_float(r.credits.used)
                    if c_used_pct is None and c_rem_val is not None and c_rem_val <= 0.0:
                        has_credit_pool = (
                            (c_lim_val is not None and c_lim_val > 0.0)
                            or (c_used_val is not None and c_used_val > 0.0)
                            or not w_rem_cands
                            or r.provider_id == "openrouter"
                        )
                        if has_credit_pool:
                            c_used_pct = 100.0
                    if c_used_pct is not None:
                        c_rem_pct = max(0.0, min(100.0, 100.0 - c_used_pct))
                        c_ev = self._evaluate_quota_metric(
                            entries,
                            instance_id=inst_id,
                            provider_id=pid,
                            account_id=acct,
                            scope="credits",
                            kind="credit_quota",
                            metric_label="Credits",
                            used_pct=c_used_pct,
                            rem_pct=c_rem_pct,
                            resets_at=None,
                            resets_in=None,
                            exhaustion_eta=None,
                            pace_level=None,
                            window_seconds=float(30 * 86400),
                            thresholds=norm_thresholds,
                            now_ts=ts,
                        )
                        if c_ev is not None:
                            triggered.append(c_ev)

            if not dry_run:
                self._save_entries_unlocked(entries, ts)

        if send_desktop and not dry_run:
            for ev in triggered:
                try:
                    sender(ev.title, ev.message)
                except Exception:
                    pass

        return triggered
