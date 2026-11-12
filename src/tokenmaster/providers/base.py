"""Base provider interface and HTTP / config overlay utilities."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import ssl
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from tokenmaster.config import (
    PROVIDER_DIR_KEYS,
    get_account_profile_dir,
    get_cache_dir,
    get_profiles_dir,
    is_forbidden_profile_dir,
    is_path_within,
    validate_account_name,
)
from tokenmaster.models import ProviderReport


class HttpError(Exception):
    """HTTP error wrapper with status code and body."""

    def __init__(self, status: int, reason: str, body: str = "") -> None:
        super().__init__(f"HTTP {status}: {reason}")
        self.status = status
        self.reason = reason
        self.body = body


def is_unauthorized(exc: Optional[BaseException]) -> bool:
    """True if ``exc`` is an HTTP 401 (rejected / expired credential)."""
    return isinstance(exc, HttpError) and exc.status == 401


# --- Critical sections -------------------------------------------------------
# Provider threads are daemons so a hung network call can never block exit. That
# is only safe if they are not killed half-way through a side effect that must
# complete (persisting a rotated OAuth refresh token, replacing a cache file,
# deleting a temp copy of a credential DB). Such code runs inside
# ``critical_section()``; an atexit hook waits (bounded) for them to finish.
# atexit callbacks run *before* daemon threads are torn down, so this works.

_CRITICAL_COND = threading.Condition()
_CRITICAL_ACTIVE = 0
CRITICAL_EXIT_WAIT_SECONDS = 20.0


@contextmanager
def critical_section() -> Iterator[None]:
    """Mark a block that must not be abandoned at interpreter exit."""
    global _CRITICAL_ACTIVE
    with _CRITICAL_COND:
        _CRITICAL_ACTIVE += 1
    try:
        yield
    finally:
        with _CRITICAL_COND:
            _CRITICAL_ACTIVE -= 1
            _CRITICAL_COND.notify_all()


def wait_for_critical_sections(timeout: float = CRITICAL_EXIT_WAIT_SECONDS) -> bool:
    """Block until no critical section is active (or ``timeout``). True if drained."""
    with _CRITICAL_COND:
        return _CRITICAL_COND.wait_for(lambda: _CRITICAL_ACTIVE == 0, timeout=timeout)


atexit.register(wait_for_critical_sections)


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write ``text`` to ``path`` atomically (tmp file in same dir + os.replace)."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_FILE_THREAD_LOCKS: dict[str, threading.Lock] = {}
_FILE_THREAD_LOCKS_GUARD = threading.Lock()


def _get_file_thread_lock(key: str) -> threading.Lock:
    with _FILE_THREAD_LOCKS_GUARD:
        lock = _FILE_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FILE_THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def file_lock(lock_path: Path, timeout: float = 5.0) -> Iterator[None]:
    """Cross-thread and cross-process advisory lock on ``lock_path`` with bounded timeout."""
    import time as _time

    try:
        import fcntl as _fcntl
    except ImportError:
        _fcntl = None  # type: ignore[assignment]

    key = os.path.abspath(str(lock_path))
    t_lock = _get_file_thread_lock(key)
    t_lock.acquire()
    fd: Optional[int] = None
    locked_flock = False
    try:
        if _fcntl is not None:
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
                deadline = _time.monotonic() + max(0.05, float(timeout))
                while True:
                    try:
                        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                        locked_flock = True
                        break
                    except BlockingIOError:
                        if _time.monotonic() >= deadline:
                            break
                        _time.sleep(0.02)
            except OSError:
                pass
        yield
    finally:
        if fd is not None:
            if locked_flock and _fcntl is not None:
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass
        t_lock.release()


def run_concurrently(
    tasks: dict[str, Callable[[], Any]],
) -> dict[str, tuple[Any, Optional[Exception]]]:
    """Run independent blocking callables in parallel threads.

    Returns ``{name: (result, None)}`` on success or ``{name: (None, exc)}`` if the
    callable raised, so callers can apply per-task error policies. Network I/O
    releases the GIL, so N independent HTTPS requests take ~max() instead of sum().

    Daemon threads are used (not ``ThreadPoolExecutor``) so that if the caller is
    itself abandoned by the global fetch deadline, a still-blocked request can never
    hold up interpreter exit.
    """
    if not tasks:
        return {}
    if len(tasks) == 1:
        (name, fn), = tasks.items()
        try:
            return {name: (fn(), None)}
        except Exception as exc:  # noqa: BLE001 - surfaced to caller
            return {name: (None, exc)}

    out: dict[str, tuple[Any, Optional[Exception]]] = {}

    def _worker(name: str, fn: Callable[[], Any]) -> None:
        try:
            out[name] = (fn(), None)
        except Exception as exc:  # noqa: BLE001 - surfaced to caller
            out[name] = (None, exc)

    threads = [
        threading.Thread(target=_worker, args=(name, fn), name=f"tm-io-{name}", daemon=True)
        for name, fn in tasks.items()
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    # Preserve the caller's key order.
    return {name: out.get(name, (None, RuntimeError("task did not complete"))) for name in tasks}


class BaseProvider:
    """Abstract base provider for TokenMaster."""

    provider_id: str = ""
    display_name: str = ""

    def __init__(self, cfg: dict[str, Any], timeout: float = 8.0) -> None:
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.timeout = timeout
        raw_acct = self.cfg.get("account_name")
        self.account: Optional[str] = (
            validate_account_name(str(raw_acct))
            if raw_acct and str(raw_acct).strip() and str(raw_acct).strip().lower() != "default"
            else None
        )
        # The report currently being built by fetch(); lets the orchestrator salvage
        # partial data if the global deadline expires mid-fetch.
        self.live_report: Optional[ProviderReport] = None

    @property
    def instance_id(self) -> str:
        """Unique key for this provider subscription ('claude' or 'claude:work')."""
        return f"{self.provider_id}:{self.account}" if self.account else self.provider_id

    @property
    def instance_display_name(self) -> str:
        """Human-readable label including subscription account tag when present."""
        if self.account:
            return f"{self.display_name} [{self.account}]"
        return self.display_name

    def _resolve_isolated_dir(self, *extra_cfg_keys: str) -> Optional[Path]:
        """Resolve the isolated directory for this provider instance, if any.

        Checks ``profile_dir``, the provider's canonical directory key in ``PROVIDER_DIR_KEYS``,
        and any ``extra_cfg_keys`` (ignoring empty/whitespace strings and rejecting global
        home/CLI directories when ``self.account`` is set). When ``self.account`` is set
        and no valid custom directory is configured, always returns
        ``get_account_profile_dir(provider_id, account)`` so a named subscription can never
        fall through to ``os.environ`` or ``~``.
        """
        keys: list[str] = ["profile_dir"]
        p_key = PROVIDER_DIR_KEYS.get(self.provider_id)
        if p_key and p_key not in keys:
            keys.append(p_key)
        for k in extra_cfg_keys:
            if k and k not in keys:
                keys.append(k)

        for key in keys:
            raw = self.cfg.get(key)
            if isinstance(raw, str) and raw.strip():
                cand = Path(os.path.abspath(str(Path(raw.strip()).expanduser())))
                if not self.account or not is_forbidden_profile_dir(cand):
                    return cand

        if self.account and self.provider_id:
            return get_account_profile_dir(self.provider_id, self.account)
        return None

    def _resolve_default_env_dir(self, env_var: str, default_path: Path) -> Path:
        """Resolve a CLI home directory for the default account, ignoring ``os.environ``
        values that point inside ``get_profiles_dir()`` or match a configured named
        account's ``profile_dir``.
        """
        raw_env = os.environ.get(env_var)
        if isinstance(raw_env, str) and raw_env.strip():
            cand = Path(raw_env.strip()).expanduser()
            if is_path_within(cand, get_profiles_dir()):
                return default_path
            excluded = self.cfg.get("_excluded_env_dirs")
            if isinstance(excluded, list):
                for ex in excluded:
                    if isinstance(ex, str) and ex.strip() and is_path_within(cand, Path(ex)):
                        return default_path
            return cand
        return default_path

    def _is_isolated_profile(self) -> bool:
        """True if this provider instance represents an isolated subscription profile."""
        return self._resolve_isolated_dir() is not None

    def _scoped_cache_path(self, base_stem: str, extra_scope: Optional[str] = None) -> Path:
        """Return a cache file path in ``get_cache_dir()``, isolated per account/profile.

        Default accounts keep their canonical filename (e.g. ``claude_projects_scan.json``)
        while named/profile-scoped accounts get a deterministic suffix so concurrent
        accounts never race or ``prune()`` each other's cached file entries.
        """
        isolated_dir = self._resolve_isolated_dir()
        scope_val = extra_scope or (str(isolated_dir) if isolated_dir is not None else None) or self.account
        if not scope_val:
            return get_cache_dir() / f"{base_stem}.json"
        digest = hashlib.sha256(str(scope_val).encode("utf-8")).hexdigest()[:8]
        safe_acct = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(self.account or "profile"))[:24]
        return get_cache_dir() / f"{base_stem}_{safe_acct}_{digest}.json"

    def new_report(self) -> ProviderReport:
        """Create this provider's report and expose it as ``live_report``."""
        report = ProviderReport(
            provider_id=self.provider_id,
            display_name=self.instance_display_name,
            account=self.account,
        )
        self.live_report = report
        return report

    def http_json(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[dict[str, str]] = None,
        payload: Optional[dict[str, Any]] = None,
        verify_ssl: bool = True,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Perform an HTTP request and parse JSON response."""
        req_headers = {
            "Accept": "application/json",
            "User-Agent": "tokenmaster/0.1.0",
        }
        if headers:
            req_headers.update(headers)

        data: Optional[bytes] = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")

        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

        ctx: Optional[ssl.SSLContext] = None
        if not verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        try:
            with urllib.request.urlopen(
                req, timeout=timeout or self.timeout, context=ctx
            ) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise HttpError(exc.code, str(exc.reason), body) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Network error: {exc.reason}") from exc

    def apply_config_overrides(self, report: ProviderReport) -> ProviderReport:
        """Apply user-configured budgets/quotas or manual ledger values from config.json."""
        w_limit = self.cfg.get("weekly_limit")
        w_used = self.cfg.get("weekly_used")
        w_unit = self.cfg.get("weekly_unit")

        w_changed = False
        if w_limit is not None:
            report.weekly.limit = float(w_limit)
            w_changed = True
            if w_unit:
                report.weekly.unit = str(w_unit)
        if w_used is not None and report.weekly.used is None:
            report.weekly.used = float(w_used)
            w_changed = True
            if w_unit:
                report.weekly.unit = str(w_unit)
        if w_changed and report.weekly.used is not None:
            report.weekly.remaining = None
            report.weekly.used_percent = None
            report.weekly.remaining_percent = None
            report.weekly.pace_used_percent = None

        c_limit = self.cfg.get("credit_limit")
        c_used = self.cfg.get("credit_used")
        c_rem = self.cfg.get("credit_remaining")
        c_unit = self.cfg.get("credit_unit")

        c_changed = False
        if c_limit is not None:
            report.credits.limit = float(c_limit)
            c_changed = True
            if c_unit:
                report.credits.unit = str(c_unit)
        if c_used is not None and report.credits.used is None:
            report.credits.used = float(c_used)
            c_changed = True
            if c_unit:
                report.credits.unit = str(c_unit)
        if c_rem is not None and report.credits.remaining is None:
            report.credits.remaining = float(c_rem)
            if c_unit:
                report.credits.unit = str(c_unit)
        elif c_changed and report.credits.used is not None:
            report.credits.remaining = None
            report.credits.used_percent = None
            report.credits.remaining_percent = None

        report.finalize()
        if (
            report.status == "unconfigured"
            and (
                report.weekly.used is not None
                or report.weekly.remaining is not None
                or report.credits.used is not None
                or report.credits.remaining is not None
            )
        ):
            report.status = "partial"
            report.source = report.source or "Manual Config Quota"

        return report

    def fetch(self) -> ProviderReport:
        raise NotImplementedError
