"""Provider registry and parallel execution engine for TokenMaster."""

from __future__ import annotations

import copy
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from tokenmaster.config import PROVIDERS, ConfigManager, parse_provider_spec
from tokenmaster.models import ProviderReport
from tokenmaster.providers.antigravity import AntigravityProvider
from tokenmaster.providers.base import BaseProvider
from tokenmaster.providers.claude import ClaudeProvider
from tokenmaster.providers.codex import CodexProvider
from tokenmaster.providers.cursor import CursorProvider
from tokenmaster.providers.grok import GrokProvider
from tokenmaster.providers.openrouter import OpenRouterProvider

PROVIDER_CLASSES: dict[str, type[BaseProvider]] = {
    "codex": CodexProvider,
    "claude": ClaudeProvider,
    "antigravity": AntigravityProvider,
    "grok": GrokProvider,
    "cursor": CursorProvider,
    "openrouter": OpenRouterProvider,
}

# Upper bound accepted by Thread.join()/socket timeouts; larger values overflow.
_MAX_WAIT_SECONDS = min(threading.TIMEOUT_MAX, 7 * 86400.0)


@dataclass
class _Job:
    """One provider (or named subscription profile) fetch running in a daemon thread."""

    job_key: str
    pid: str
    account: Optional[str]
    instance: BaseProvider
    cfg_fingerprint: str = ""
    started: float = field(default_factory=time.perf_counter)
    thread: Optional[threading.Thread] = None
    report: Optional[ProviderReport] = None  # set exactly once, when fetch() returns


# Jobs that are still running, keyed by ``job_key`` (``pid`` or ``pid:account``).
# A subscription that outlived the deadline in one refresh (watch mode) is *joined*
# by the next refresh instead of being started a second time; overlapping fetches
# could otherwise race on token refresh/persist or on scan caches.
_INFLIGHT: dict[str, _Job] = {}
_INFLIGHT_LOCK = threading.Lock()


def _seconds(raw: object, default: float) -> float:
    """Parse a positive seconds value; non-numeric/<=0/NaN -> default; clamp to max."""
    if isinstance(raw, bool):
        return default
    try:
        val = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not (val > 0):  # also rejects NaN
        return default
    return min(val, _MAX_WAIT_SECONDS)


def resolve_deadline(config_mgr: ConfigManager, timeout: float) -> Optional[float]:
    """Global deadline in seconds; None means wait indefinitely (``deadline_seconds: 0``)."""
    raw = config_mgr.data.get("deadline_seconds")
    if raw is not None and not isinstance(raw, bool):
        try:
            if float(raw) == 0:
                return None
        except (TypeError, ValueError):
            pass
    return _seconds(raw, min(timeout * 2.5, _MAX_WAIT_SECONDS))


def _run_job(job: _Job) -> None:
    try:
        report = job.instance.fetch()
    except Exception as exc:  # noqa: BLE001 - one provider must never sink the rest
        report = ProviderReport(
            provider_id=job.pid,
            display_name=job.instance.instance_display_name,
            account=job.account,
            status="error",
            error_message=str(exc) or type(exc).__name__,
        ).finalize()
    report.fetch_ms = int((time.perf_counter() - job.started) * 1000)
    job.report = report  # single reference assignment: atomic under the GIL
    with _INFLIGHT_LOCK:
        if _INFLIGHT.get(job.job_key) is job:
            del _INFLIGHT[job.job_key]


def _timeout_report(job: _Job, deadline_s: float) -> ProviderReport:
    """Build a report for a job that missed the deadline, salvaging partial data."""
    msg = f"Timed out after {deadline_s:g}s (raise `deadline_seconds` in config)"
    partial: Optional[ProviderReport] = None
    live = job.instance.live_report
    if live is not None:
        try:
            # The fetch thread may still be mutating it; snapshot defensively.
            partial = copy.deepcopy(live)
        except Exception:  # noqa: BLE001 - concurrent mutation; fall back to bare report
            partial = None

    if partial is None:
        partial = ProviderReport(
            provider_id=job.pid,
            display_name=job.instance.instance_display_name,
            account=job.account,
        )
    has_data = (
        partial.weekly.used is not None
        or partial.weekly.used_percent is not None
        or partial.credits.used is not None
        or partial.credits.remaining is not None
        or bool(partial.sub_metrics)
    )
    partial.status = "partial" if has_data else "error"
    partial.error_message = msg + (" — showing partial data" if has_data else "")
    partial.fetch_ms = int((time.perf_counter() - job.started) * 1000)
    try:
        partial.finalize()
    except Exception:  # noqa: BLE001
        pass
    return partial


def _expand_targets(
    config_mgr: ConfigManager,
    selected: Optional[list[str]],
) -> list[tuple[str, str, Optional[str], dict[str, Any]]]:
    """Expand ``selected`` specs into ordered ``(job_key, pid, account, cfg)`` targets."""
    targets: list[tuple[str, str, Optional[str], dict[str, Any]]] = []
    seen_keys: set[str] = set()

    def _add(pid: str, acct: Optional[str], cfg: dict[str, Any]) -> None:
        jkey = f"{pid}:{acct}" if acct else pid
        if jkey not in seen_keys:
            seen_keys.add(jkey)
            targets.append((jkey, pid, acct, cfg))

    if not selected:
        for pid in PROVIDERS:
            if pid not in PROVIDER_CLASSES:
                continue
            for acct, cfg in config_mgr.list_accounts(pid, include_disabled=False):
                _add(pid, acct, cfg)
        return targets

    for raw_spec in selected:
        pid, acct, explicit_acct = parse_provider_spec(raw_spec)
        if pid not in PROVIDER_CLASSES:
            continue
        if explicit_acct:
            cfg = config_mgr.account_cfg(pid, acct)
            _add(pid, acct, cfg)
        else:
            accts = config_mgr.list_accounts(
                pid, include_disabled=False, ignore_provider_enabled=True
            )
            for a_name, a_cfg in accts:
                _add(pid, a_name, a_cfg)

    return targets


def fetch_providers(
    config_mgr: ConfigManager,
    selected: Optional[list[str]] = None,
) -> list[ProviderReport]:
    """Fetch usage and credit reports concurrently across requested providers & accounts.

    Each provider subscription runs in its own daemon thread. The whole refresh is
    bounded by a global deadline (``deadline_seconds``; default 2.5 x ``timeout_seconds``;
    ``0`` disables it). A subscription still running at the deadline is shown as timed out
    with whatever partial data it had gathered. Its thread is never killed: side
    effects that must complete run in ``critical_section()`` and are awaited at exit.
    """
    targets = _expand_targets(config_mgr, selected)
    timeout = _seconds(config_mgr.data.get("timeout_seconds"), 8.0)
    deadline_s = resolve_deadline(config_mgr, timeout)

    jobs: dict[str, _Job] = {}
    for jkey, pid, acct, p_cfg in targets:
        fp = json.dumps(p_cfg, sort_keys=True, default=str)
        with _INFLIGHT_LOCK:
            existing = _INFLIGHT.get(jkey)
            if existing is not None and (
                existing.report is None
                and existing.thread is not None
                and existing.thread.is_alive()
                and existing.cfg_fingerprint == fp
            ):
                jobs[jkey] = existing  # previous refresh with identical config still running: join it
                continue
            instance = PROVIDER_CLASSES[pid](p_cfg, timeout=timeout)
            job = _Job(
                job_key=jkey,
                pid=pid,
                account=acct,
                instance=instance,
                cfg_fingerprint=fp,
            )
            job.thread = threading.Thread(
                target=_run_job, args=(job,), name=f"tm-{jkey}", daemon=True
            )
            _INFLIGHT[jkey] = job
            job.thread.start()
        jobs[jkey] = job

    deadline_at = None if deadline_s is None else time.monotonic() + deadline_s
    for job in jobs.values():
        assert job.thread is not None
        if deadline_at is None:
            job.thread.join()
        else:
            job.thread.join(max(0.0, deadline_at - time.monotonic()))

    out: list[ProviderReport] = []
    for jkey, _, _, _ in targets:
        job = jobs.get(jkey)
        if job is None:
            continue
        report = job.report
        if report is None:
            report = _timeout_report(job, deadline_s or 0.0)
        out.append(report)
    return out
