"""Cursor IDE & Cursor Agent CLI provider (state.vscdb + Keychain + ~/.cursor/auth.json + api2.cursor.sh)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from tokenmaster.models import CreditBalance, ProviderReport, SubMetric, UsageWindow, format_reset_countdown
from tokenmaster.providers.base import BaseProvider, atomic_write_text, critical_section, run_concurrently
from tokenmaster.providers.base import is_unauthorized as _is_unauthorized

CURSOR_OAUTH_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"

_CURSOR_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_CURSOR_REFRESH_LOCKS_GUARD = threading.Lock()


def _cursor_refresh_lock(auth_file: Path) -> threading.Lock:
    key = os.path.abspath(str(auth_file))
    with _CURSOR_REFRESH_LOCKS_GUARD:
        lock = _CURSOR_REFRESH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _CURSOR_REFRESH_LOCKS[key] = lock
        return lock


def _parse_epoch_or_iso(val: Any) -> Optional[datetime]:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        ts = float(val)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(val, str):
        raw = val.strip()
        if raw.isdigit():
            return _parse_epoch_or_iso(int(raw))
        try:
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _safe_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, (list, dict, bool)):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


class CursorProvider(BaseProvider):
    """Monitors Cursor IDE & cursor-agent CLI weekly usage/remaining and credit usage/remaining."""

    provider_id = "cursor"
    display_name = "Cursor IDE"

    def _cursor_home(self) -> Path:
        isolated = self._resolve_isolated_dir("cursor_home")
        if isolated is not None:
            return isolated
        return self._resolve_default_env_dir("CURSOR_HOME", Path.home() / ".cursor")

    def _default_state_db_path(self) -> Path:
        raw_db = self.cfg.get("state_db_path")
        if isinstance(raw_db, str) and raw_db.strip():
            return Path(raw_db.strip()).expanduser()
        if self._is_isolated_profile():
            # Isolated named profiles must never read the global Cursor IDE state.vscdb
            # unless explicitly pointed there via state_db_path.
            return self._cursor_home() / "state.vscdb"
        if sys.platform == "darwin":
            return (
                Path.home()
                / "Library"
                / "Application Support"
                / "Cursor"
                / "User"
                / "globalStorage"
                / "state.vscdb"
            )
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
            return Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
        return Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"

    def _read_sqlite_auth(self) -> dict[str, str]:
        """Extract cursorAuth keys from Cursor's state.vscdb SQLite database."""
        db_path = self._default_state_db_path()
        result: dict[str, str] = {}
        if not db_path.exists() or not os.access(db_path, os.R_OK):
            return result

        keys = (
            "cursorAuth/accessToken",
            "cursorAuth/refreshToken",
            "cursorAuth/cachedEmail",
            "cursorAuth/stripeMembershipType",
            "cursorAuth/storedServerBackendUrl",
            "cursorAuth/cachedSignUpType",
        )
        placeholders = ",".join("?" for _ in keys)
        query = f"SELECT key, value FROM ItemTable WHERE key IN ({placeholders})"

        rows: list[tuple[Any, Any]] = []
        try:
            uri = f"file:{db_path}?mode=ro"
            # closing(): sqlite3's own context manager only commits; it never closes.
            with closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as conn:
                rows = conn.execute(query, keys).fetchall()
        except Exception:
            try:
                # The temp copy holds credentials: never abandon it half-deleted at exit.
                with critical_section():
                    with tempfile.NamedTemporaryFile(suffix=".vscdb", delete=True) as tmp:
                        shutil.copy2(db_path, tmp.name)
                        with closing(sqlite3.connect(tmp.name, timeout=2.0)) as conn:
                            rows = conn.execute(query, keys).fetchall()
            except Exception:
                pass

        for k, v in rows:
            if v is not None:
                result[str(k)] = str(v)

        return result

    def _read_keychain_secret(self, service: str, account: str = "cursor-user") -> Optional[str]:
        if sys.platform != "darwin":
            return None
        for cmd in (
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            ["security", "find-generic-password", "-s", service, "-w"],
        ):
            try:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                    check=False,
                )
                if res.returncode == 0 and res.stdout.strip():
                    return res.stdout.strip()
            except Exception:
                continue
        return None

    def _write_keychain_secret(self, service: str, secret: str, account: str = "cursor-user") -> bool:
        if sys.platform != "darwin" or not secret:
            return False
        try:
            payload_hex = secret.encode("utf-8").hex()
            cmd_line = f'add-generic-password -U -a "{account}" -s "{service}" -X "{payload_hex}"\n'
            if len(cmd_line) <= 4032:
                res = subprocess.run(
                    ["security", "-i"],
                    input=cmd_line,
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                    check=False,
                )
            else:
                res = subprocess.run(
                    [
                        "security",
                        "add-generic-password",
                        "-U",
                        "-a",
                        account,
                        "-s",
                        service,
                        "-X",
                        payload_hex,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                    check=False,
                )
            return res.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _clean_auth_token(val: Any, *, is_refresh: bool = False) -> Optional[str]:
        if not val:
            return None
        s = str(val).strip()
        if not s or s.startswith("cursor-safe-storage:"):
            return None
        if is_refresh and s == "xai-identity-session":
            return None
        return s

    def _load_cursor_cli_auth(self) -> dict[str, str]:
        """Read credentials from cursor-agent CLI (~/.cursor/auth.json or macOS Keychain)."""
        self._last_auth_json_at: Optional[str] = None
        self._last_auth_json_rt: Optional[str] = None
        self._last_keychain_at: Optional[str] = None
        self._last_keychain_rt: Optional[str] = None
        out: dict[str, str] = {}
        cursor_home = self._cursor_home()
        auth_file = cursor_home / "auth.json"
        if auth_file.exists() and os.access(auth_file, os.R_OK):
            try:
                raw = json.loads(auth_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    at = self._clean_auth_token(raw.get("accessToken") or raw.get("access_token"))
                    if at:
                        out["access_token"] = at
                        out["auth_json_access_token"] = at
                        self._last_auth_json_at = at
                    rt = self._clean_auth_token(
                        raw.get("refreshToken") or raw.get("refresh_token"), is_refresh=True
                    )
                    if rt:
                        out["refresh_token"] = rt
                        out["auth_json_refresh_token"] = rt
                        self._last_auth_json_rt = rt
                    ak = self._clean_auth_token(raw.get("apiKey") or raw.get("api_key"))
                    if ak:
                        out["api_key"] = ak
            except Exception:
                pass

        # Isolated named profiles rely strictly on <profile_dir>/auth.json (populated by
        # `tokenmaster login cursor <account>` or `tokenmaster account snapshot cursor <account>`)
        # so global Keychain credentials never bleed across subscriptions.
        if self._is_isolated_profile():
            return out

        if not out.get("access_token"):
            kc_access = self._clean_auth_token(self._read_keychain_secret("cursor-access-token"))
            if kc_access:
                out["access_token"] = kc_access
                self._last_keychain_at = kc_access
        # Avoid spawning extra `/usr/bin/security` subprocesses when a valid, non-expired
        # access_token was already found; refresh_token / api_key are fetched lazily if it fails.
        need_fallbacks = not out.get("access_token") or self._is_jwt_expired(out["access_token"])
        if need_fallbacks:
            if not out.get("refresh_token"):
                kc_refresh = self._clean_auth_token(
                    self._read_keychain_secret("cursor-refresh-token"), is_refresh=True
                )
                if kc_refresh:
                    out["refresh_token"] = kc_refresh
                    out["keychain_refresh_token"] = kc_refresh
                    self._last_keychain_rt = kc_refresh
            if not out.get("api_key"):
                kc_api = self._clean_auth_token(self._read_keychain_secret("cursor-api-key"))
                if kc_api:
                    out["api_key"] = kc_api

        return out

    def _persist_refreshed_cursor_token(
        self,
        new_access_token: str,
        new_refresh_token: Optional[str] = None,
        *,
        persist_file: bool = True,
        persist_keychain: bool = False,
        used_refresh_token: Optional[str] = None,
    ) -> None:
        """Persist a refreshed accessToken (and rotated refreshToken) back to <cursor_home>/auth.json and/or Keychain."""
        if persist_file:
            auth_file = self._cursor_home() / "auth.json"
            if auth_file.is_file():
                try:
                    raw = json.loads(auth_file.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        cur_f_rt = self._clean_auth_token(
                            raw.get("refreshToken") or raw.get("refresh_token"), is_refresh=True
                        )
                        cur_f_at = self._clean_auth_token(
                            raw.get("accessToken") or raw.get("access_token")
                        )
                        # CAS guard: do not clobber a newer external auth.json token
                        if not (
                            cur_f_rt
                            and used_refresh_token
                            and cur_f_rt not in (used_refresh_token, new_refresh_token)
                            and cur_f_at
                            and not self._is_jwt_expired(cur_f_at)
                        ):
                            if "access_token" in raw and "accessToken" not in raw:
                                raw["access_token"] = new_access_token
                            else:
                                raw["accessToken"] = new_access_token
                            if new_refresh_token:
                                if "refresh_token" in raw and "refreshToken" not in raw:
                                    raw["refresh_token"] = new_refresh_token
                                else:
                                    raw["refreshToken"] = new_refresh_token
                                self._last_auth_json_rt = new_refresh_token
                            self._last_auth_json_at = new_access_token
                            atomic_write_text(auth_file, json.dumps(raw, indent=2) + "\n", mode=0o600)
                except Exception:
                    pass

        if persist_keychain and not self._is_isolated_profile() and sys.platform == "darwin":
            try:
                cur_kc_rt = self._clean_auth_token(
                    self._read_keychain_secret("cursor-refresh-token"), is_refresh=True
                )
                cur_kc_at = self._clean_auth_token(
                    self._read_keychain_secret("cursor-access-token")
                )
                # CAS guard: do not clobber a newer external Keychain login
                if not (
                    cur_kc_rt
                    and used_refresh_token
                    and cur_kc_rt not in (used_refresh_token, new_refresh_token)
                    and cur_kc_at
                    and not self._is_jwt_expired(cur_kc_at)
                ):
                    self._write_keychain_secret("cursor-access-token", new_access_token)
                    self._last_keychain_at = new_access_token
                    if new_refresh_token:
                        self._write_keychain_secret("cursor-refresh-token", new_refresh_token)
                        self._last_keychain_rt = new_refresh_token
            except Exception:
                pass

    def _refresh_access_token(
        self,
        refresh_token: str,
        backend_url: str = "https://api2.cursor.sh",
    ) -> Optional[str]:
        """Exchange refreshToken for a fresh accessToken via api2.cursor.sh/oauth/token."""
        if not refresh_token or refresh_token == "xai-identity-session":
            return None

        def _do_exchange(
            rt_to_use: str,
            should_persist_file: bool,
            should_persist_kc: bool,
        ) -> Optional[str]:
            try:
                resp = self.http_json(
                    f"{backend_url.rstrip('/')}/oauth/token",
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "x-cursor-client-version": "3.29.9",
                        "x-cursor-client-type": "ide",
                    },
                    payload={
                        "grant_type": "refresh_token",
                        "client_id": CURSOR_OAUTH_CLIENT_ID,
                        "refresh_token": rt_to_use,
                    },
                )
                if isinstance(resp, dict) and not resp.get("shouldLogout"):
                    tok = resp.get("access_token") or resp.get("accessToken")
                    if tok and str(tok).strip():
                        fresh = str(tok).strip()
                        rot_rt = self._clean_auth_token(
                            resp.get("refresh_token") or resp.get("refreshToken"),
                            is_refresh=True,
                        )
                        if should_persist_file or should_persist_kc:
                            self._persist_refreshed_cursor_token(
                                fresh,
                                rot_rt,
                                persist_file=should_persist_file,
                                persist_keychain=should_persist_kc,
                                used_refresh_token=rt_to_use,
                            )
                        return fresh
            except Exception:
                pass
            return None

        auth_file = self._cursor_home() / "auth.json"
        belongs_to_kc = bool(
            not self._is_isolated_profile()
            and sys.platform == "darwin"
            and getattr(self, "_last_keychain_rt", None) == refresh_token
        )
        if not auth_file.is_file() and not belongs_to_kc:
            return _do_exchange(refresh_token, False, False)

        with _cursor_refresh_lock(auth_file), critical_section():
            belongs_to_auth_json = False
            if auth_file.is_file():
                try:
                    disk_raw = json.loads(auth_file.read_text(encoding="utf-8"))
                    if isinstance(disk_raw, dict):
                        disk_at = self._clean_auth_token(
                            disk_raw.get("accessToken") or disk_raw.get("access_token")
                        )
                        disk_rt = self._clean_auth_token(
                            disk_raw.get("refreshToken") or disk_raw.get("refresh_token"),
                            is_refresh=True,
                        )
                        init_auth_rt = getattr(self, "_last_auth_json_rt", None)
                        init_auth_at = getattr(self, "_last_auth_json_at", None)
                        if disk_rt == refresh_token or (
                            init_auth_rt == refresh_token and disk_rt is not None
                        ):
                            belongs_to_auth_json = True
                            # If a sibling thread already refreshed accessToken (and/or rotated refreshToken)
                            # in auth.json, adopt it without making a duplicate network request.
                            if (
                                disk_at
                                and not self._is_jwt_expired(disk_at)
                                and (
                                    disk_at != init_auth_at
                                    or disk_rt != refresh_token
                                    or init_auth_at is None
                                )
                            ):
                                self._last_auth_json_at = disk_at
                                if disk_rt:
                                    self._last_auth_json_rt = disk_rt
                                return disk_at
                            if disk_rt and disk_rt != refresh_token:
                                refresh_token = disk_rt
                except Exception:
                    pass

            if belongs_to_kc:
                try:
                    kc_at = self._clean_auth_token(self._read_keychain_secret("cursor-access-token"))
                    kc_rt = self._clean_auth_token(
                        self._read_keychain_secret("cursor-refresh-token"), is_refresh=True
                    )
                    init_kc_at = getattr(self, "_last_keychain_at", None)
                    if (
                        kc_at
                        and not self._is_jwt_expired(kc_at)
                        and (kc_at != init_kc_at or (kc_rt and kc_rt != refresh_token))
                    ):
                        self._last_keychain_at = kc_at
                        if kc_rt:
                            self._last_keychain_rt = kc_rt
                        return kc_at
                    if kc_rt and kc_rt != refresh_token:
                        refresh_token = kc_rt
                except Exception:
                    pass

            return _do_exchange(refresh_token, belongs_to_auth_json, belongs_to_kc)

    @staticmethod
    def _decode_jwt_payload(token: str) -> Optional[dict[str, Any]]:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        padding = "=" * ((4 - len(payload_b64) % 4) % 4)
        try:
            raw = base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    @classmethod
    def _extract_jwt_user_id(cls, token: str) -> Optional[str]:
        """Decode JWT payload to extract the WorkOS/Auth0 user ID."""
        obj = cls._decode_jwt_payload(token)
        if not obj:
            return None
        sub = str(obj.get("sub") or "")
        if "|" in sub:
            return sub.split("|", 1)[1]
        return sub or None

    @classmethod
    def _is_jwt_expired(cls, token: str) -> bool:
        obj = cls._decode_jwt_payload(token)
        if not obj or "exp" not in obj:
            return False
        try:
            return float(obj["exp"]) < (time.time() + 60)
        except Exception:
            return False

    def _build_auth_headers(
        self, access_token: Optional[str], cookie: Optional[str], include_bearer: bool = False
    ) -> tuple[dict[str, str], Optional[str]]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Origin": "https://cursor.com",
            "Referer": "https://cursor.com/dashboard",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
            ),
        }
        user_id: Optional[str] = None

        if access_token:
            user_id = self._extract_jwt_user_id(access_token)
            if include_bearer:
                headers["Authorization"] = f"Bearer {access_token}"

        if cookie:
            c_str = str(cookie).strip()
            if "WorkosCursorSessionToken=" not in c_str and "=" not in c_str:
                c_str = f"WorkosCursorSessionToken={c_str}"
            headers["Cookie"] = c_str
        elif access_token and user_id:
            headers["Cookie"] = f"WorkosCursorSessionToken={user_id}%3A%3A{access_token}"
        elif access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        return headers, user_id

    def _apply_plan_and_on_demand(
        self,
        report: ProviderReport,
        plan_dict: dict[str, Any],
        on_demand_dict: Optional[dict[str, Any]],
        cycle_start: Any,
        cycle_end: Any,
        is_merged: bool = False,
    ) -> None:
        resets_at, resets_in = format_reset_countdown(cycle_end)

        raw_used = float(
            plan_dict.get("used")
            if plan_dict.get("used") is not None
            else (
                plan_dict.get("includedSpend")
                if plan_dict.get("includedSpend") is not None
                else (
                    plan_dict.get("included_spend")
                    if plan_dict.get("included_spend") is not None
                    else (plan_dict.get("totalSpend") or plan_dict.get("total_spend") or 0.0)
                )
            )
        )
        raw_limit = float(plan_dict.get("limit") or 0.0)
        raw_rem = float(
            plan_dict.get("remaining")
            if plan_dict.get("remaining") is not None
            else max(0.0, raw_limit - raw_used)
        )

        scale = 100.0 if raw_limit >= 100 else 1.0
        used_usd = round(raw_used / scale, 2)
        limit_usd = round(raw_limit / scale, 2)
        rem_usd = round(raw_rem / scale, 2)
        tot_pct = (
            float(plan_dict["totalPercentUsed"])
            if plan_dict.get("totalPercentUsed") is not None
            else (
                float(plan_dict["total_percent_used"])
                if plan_dict.get("total_percent_used") is not None
                else ((used_usd / limit_usd * 100.0) if limit_usd > 0 else 0.0)
            )
        )

        e_dt = _parse_epoch_or_iso(cycle_end)
        s_dt = _parse_epoch_or_iso(cycle_start)
        cycle_win_sec: Optional[int] = None
        if s_dt is not None and e_dt is not None and e_dt > s_dt:
            cycle_win_sec = max(1, int((e_dt - s_dt).total_seconds()))

        if raw_limit > 0:
            report.credits = CreditBalance(
                used=used_usd,
                remaining=rem_usd,
                limit=limit_usd,
                unit="USD",
                used_percent=tot_pct,
                remaining_percent=max(0.0, 100.0 - tot_pct),
                note="Included Plan Credits",
            )

            # Weekly pacing / allowance within billing cycle
            weekly_limit_usd = round(limit_usd / 4.0, 2)
            elapsed_weeks = 1.0
            if s_dt is not None:
                days_elapsed = max(1.0, (datetime.now(timezone.utc) - s_dt).total_seconds() / 86400.0)
                elapsed_weeks = max(1.0, days_elapsed / 7.0)

            weekly_used_est = round(used_usd / elapsed_weeks, 2)
            weekly_rem_est = round(max(0.0, weekly_limit_usd - weekly_used_est), 2)
            weekly_pct = min(
                100.0,
                (weekly_used_est / weekly_limit_usd * 100.0) if weekly_limit_usd > 0 else tot_pct,
            )

            report.weekly = UsageWindow(
                used=weekly_used_est,
                remaining=weekly_rem_est,
                limit=weekly_limit_usd,
                unit="USD",
                used_percent=weekly_pct,
                remaining_percent=max(0.0, 100.0 - weekly_pct),
                resets_at=resets_at,
                resets_in=resets_in,
                window_seconds=cycle_win_sec or (30 * 86400),
                pace_used_percent=tot_pct,
            )

            report.sub_metrics.append(
                SubMetric(
                    name="Plan Included Pool",
                    used_str=f"${used_usd:.2f} / ${limit_usd:.2f} ({tot_pct:.1f}%)",
                    remaining_str=f"${rem_usd:.2f} left",
                    used_percent=tot_pct,
                    resets_in=resets_in,
                )
            )
            auto_pct = _safe_float(
                plan_dict.get("autoPercentUsed")
                if plan_dict.get("autoPercentUsed") is not None
                else plan_dict.get("auto_percent_used")
            )
            api_pct = _safe_float(
                plan_dict.get("apiPercentUsed")
                if plan_dict.get("apiPercentUsed") is not None
                else plan_dict.get("api_percent_used")
            )
            if auto_pct is not None and api_pct is None:
                api_pct = 0.0
            elif api_pct is not None and auto_pct is None:
                auto_pct = 0.0
            if auto_pct is not None:
                auto_rem = max(0.0, 100.0 - auto_pct)
                report.sub_metrics.append(
                    SubMetric(
                        name="Auto Model Bucket",
                        used_str=f"{auto_pct:.1f}%",
                        remaining_str=f"{auto_rem:.1f}%",
                        used_percent=auto_pct,
                        resets_in=resets_in,
                    )
                )
            if api_pct is not None:
                api_rem = max(0.0, 100.0 - api_pct)
                report.sub_metrics.append(
                    SubMetric(
                        name="Named/API Model Bucket",
                        used_str=f"{api_pct:.1f}%",
                        remaining_str=f"{api_rem:.1f}%",
                        used_percent=api_pct,
                        resets_in=resets_in,
                    )
                )
        else:
            # Percentage-based weekly quota (e.g., Cursor unified / weekly percentage plans)
            rem_pct = max(0.0, 100.0 - tot_pct)
            report.weekly = UsageWindow(
                used=round(tot_pct, 1),
                remaining=round(rem_pct, 1),
                limit=100.0,
                unit="%",
                used_percent=tot_pct,
                remaining_percent=rem_pct,
                resets_at=resets_at,
                resets_in=resets_in,
                window_seconds=cycle_win_sec or (30 * 86400),
            )
            report.credits = CreditBalance(
                note="Unified w/ xAI Billing" if is_merged else "Included in Plan",
            )
            report.sub_metrics.append(
                SubMetric(
                    name="Plan Included Quota (Total)",
                    used_str=f"{tot_pct:.1f}%",
                    remaining_str=f"{rem_pct:.1f}%",
                    used_percent=tot_pct,
                    resets_in=resets_in,
                )
            )
            auto_pct = _safe_float(
                plan_dict.get("autoPercentUsed")
                if plan_dict.get("autoPercentUsed") is not None
                else plan_dict.get("auto_percent_used")
            )
            api_pct = _safe_float(
                plan_dict.get("apiPercentUsed")
                if plan_dict.get("apiPercentUsed") is not None
                else plan_dict.get("api_percent_used")
            )
            # Connect-RPC Protobuf3 JSON serialization omits 0.0 float fields (omitempty):
            # default any omitted model bucket(s) in percentage-based plan_dict to 0.0 so both
            # Auto Model Bucket and Named/API Model Bucket are always surfaced consistently.
            if auto_pct is None:
                auto_pct = 0.0
            if api_pct is None:
                api_pct = 0.0

            if auto_pct is not None:
                auto_rem = max(0.0, 100.0 - auto_pct)
                report.sub_metrics.append(
                    SubMetric(
                        name="Auto Model Bucket",
                        used_str=f"{auto_pct:.1f}%",
                        remaining_str=f"{auto_rem:.1f}%",
                        used_percent=auto_pct,
                        resets_in=resets_in,
                    )
                )
            if api_pct is not None:
                api_rem = max(0.0, 100.0 - api_pct)
                report.sub_metrics.append(
                    SubMetric(
                        name="Named/API Model Bucket",
                        used_str=f"{api_pct:.1f}%",
                        remaining_str=f"{api_rem:.1f}%",
                        used_percent=api_pct,
                        resets_in=resets_in,
                    )
                )

        if isinstance(on_demand_dict, dict):
            od_enabled = on_demand_dict.get("enabled", True)
            od_raw_used = on_demand_dict.get(
                "used",
                on_demand_dict.get(
                    "individualUsed",
                    on_demand_dict.get("individual_used", on_demand_dict.get("totalSpend")),
                ),
            )
            od_raw_limit = on_demand_dict.get(
                "limit",
                on_demand_dict.get(
                    "individualLimit",
                    on_demand_dict.get("individual_limit", on_demand_dict.get("pooledLimit")),
                ),
            )
            if od_enabled and (od_raw_used is not None or od_raw_limit is not None):
                od_used = round(float(od_raw_used or 0.0) / 100.0, 2)
                od_limit = (
                    round(float(od_raw_limit) / 100.0, 2)
                    if od_raw_limit is not None
                    else None
                )
                od_rem = (
                    round(float(on_demand_dict["remaining"]) / 100.0, 2)
                    if on_demand_dict.get("remaining") is not None
                    else (round(max(0.0, od_limit - od_used), 2) if od_limit is not None else None)
                )
                od_pct = (od_used / od_limit * 100.0) if (od_limit and od_limit > 0) else None
                report.sub_metrics.append(
                    SubMetric(
                        name="On-Demand Spend",
                        used_str=f"${od_used:.2f}" + (f" / ${od_limit:.2f}" if od_limit else ""),
                        remaining_str=f"${od_rem:.2f}" if od_rem is not None else "Unlimited",
                        used_percent=od_pct,
                    )
                )

    def _try_fetch_with_token(
        self,
        report: ProviderReport,
        token: str,
        cookie: Optional[str],
        sources: list[str],
        backend_url: str = "https://api2.cursor.sh",
    ) -> bool:
        """Attempt Connect-RPC and cursor.com API calls using a candidate token. Returns True if populated."""
        if token:
            rpc_headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "x-cursor-client-version": "3.29.9",
                "x-cursor-client-type": "ide",
            }
            base_rpc = f"{backend_url.rstrip('/')}/aiserver.v1.DashboardService"
            rpc_period: Optional[dict[str, Any]] = None
            rpc_plan: Optional[dict[str, Any]] = None
            rpc_grants: Optional[dict[str, Any]] = None

            # The three Dashboard RPCs are independent; issue them concurrently so the
            # wall time is the slowest call rather than the sum of all three.
            def _rpc(
                method: str,
                payload: dict[str, Any],
                rpc_timeout: Optional[float] = None,
            ) -> Callable[[], Any]:
                return lambda: self.http_json(
                    f"{base_rpc}/{method}",
                    method="POST",
                    headers=rpc_headers,
                    payload=payload,
                    timeout=rpc_timeout,
                )

            results = run_concurrently(
                {
                    "period": _rpc("GetCurrentPeriodUsage", {}),
                    "plan": _rpc("GetPlanInfo", {}),
                    "grants": _rpc(
                        "GetCreditGrantsBalance",
                        {"includeAllProducts": True, "includePersonalTeam": True},
                        rpc_timeout=min(self.timeout, 2.5),
                    ),
                }
            )
            period_res, period_exc = results["period"]
            if period_exc is not None:
                report.error_message = str(period_exc)
            else:
                rpc_period = period_res

            # A rejected token (401) invalidates the whole candidate: ignore whatever
            # the sibling calls returned so we fall through to the next token.
            if not _is_unauthorized(period_exc):
                plan_res, plan_exc = results["plan"]
                grants_res, grants_exc = results["grants"]
                if plan_exc is None:
                    rpc_plan = plan_res
                if grants_exc is None:
                    rpc_grants = grants_res

            if isinstance(rpc_period, dict) or isinstance(rpc_plan, dict) or isinstance(rpc_grants, dict):
                # Distinguish between Connect-RPC response vs cursor.com/api/usage-summary mock
                if isinstance(rpc_period, dict) and "individualUsage" in rpc_period:
                    sources.append("usage-summary API")
                    report.status = "ok"
                    m_type = rpc_period.get("membershipType")
                    if m_type:
                        report.plan_tier = f"Cursor {str(m_type).capitalize()}"
                    indiv = rpc_period.get("individualUsage") or {}
                    plan = indiv.get("plan") if isinstance(indiv, dict) else {}
                    on_demand = indiv.get("onDemand") if isinstance(indiv, dict) else None
                    if isinstance(plan, dict) and plan:
                        self._apply_plan_and_on_demand(
                            report,
                            plan_dict=plan,
                            on_demand_dict=on_demand,
                            cycle_start=rpc_period.get("billingCycleStart"),
                            cycle_end=rpc_period.get("billingCycleEnd"),
                        )
                        report.error_message = None
                        return True

                sources.append("api2.cursor.sh RPC")
                report.status = "ok"
                report.error_message = None

                plan_info = (
                    rpc_plan.get("planInfo") or rpc_plan.get("plan_info")
                    if isinstance(rpc_plan, dict)
                    else None
                )
                is_merged = bool(
                    isinstance(rpc_plan, dict)
                    and (rpc_plan.get("isMerged") or rpc_plan.get("is_merged"))
                )
                if isinstance(plan_info, dict):
                    p_name = plan_info.get("planName") or plan_info.get("plan_name")
                    if p_name:
                        p_str = str(p_name).strip()
                        report.plan_tier = p_str if p_str.lower().startswith("cursor") else f"Cursor {p_str}"

                plan_usage = (
                    (rpc_period.get("planUsage") or rpc_period.get("plan_usage"))
                    if isinstance(rpc_period, dict)
                    else None
                )
                spend_limit = (
                    (rpc_period.get("spendLimitUsage") or rpc_period.get("spend_limit_usage"))
                    if isinstance(rpc_period, dict)
                    else None
                )
                cycle_start = (
                    (rpc_period.get("billingCycleStart") or rpc_period.get("billing_cycle_start"))
                    if isinstance(rpc_period, dict)
                    else None
                )
                cycle_end = (
                    (rpc_period.get("billingCycleEnd") or rpc_period.get("billing_cycle_end"))
                    if isinstance(rpc_period, dict)
                    else None
                )
                if not cycle_end and isinstance(plan_info, dict):
                    cycle_end = (
                        plan_info.get("includedUsageResetsAt")
                        or plan_info.get("included_usage_resets_at")
                        or plan_info.get("billingCycleEnd")
                        or plan_info.get("billing_cycle_end")
                    )

                # Synthesize plan_usage from GetPlanInfo if GetCurrentPeriodUsage omitted 0-spend planUsage
                if not isinstance(plan_usage, dict) and isinstance(plan_info, dict):
                    inc_cents = _safe_float(
                        plan_info.get("includedAmountCents")
                        or plan_info.get("included_amount_cents")
                    )
                    if inc_cents is not None and inc_cents > 0:
                        plan_usage = {
                            "used": 0.0,
                            "limit": inc_cents,
                            "remaining": inc_cents,
                            "totalPercentUsed": 0.0,
                        }

                if isinstance(plan_usage, dict) or isinstance(spend_limit, dict):
                    self._apply_plan_and_on_demand(
                        report,
                        plan_dict=plan_usage if isinstance(plan_usage, dict) else {},
                        on_demand_dict=spend_limit if isinstance(spend_limit, dict) else None,
                        cycle_start=cycle_start,
                        cycle_end=cycle_end,
                        is_merged=is_merged,
                    )

                if isinstance(rpc_grants, dict) and (
                    rpc_grants.get("hasCreditGrants") or rpc_grants.get("has_credit_grants")
                ):
                    bal_c = _safe_float(
                        rpc_grants.get("creditBalanceCents") or rpc_grants.get("credit_balance_cents")
                    ) or 0.0
                    tot_c = _safe_float(
                        rpc_grants.get("totalCents") or rpc_grants.get("total_cents")
                    ) or 0.0
                    used_c = _safe_float(
                        rpc_grants.get("usedCents") or rpc_grants.get("used_cents")
                    ) or max(0.0, tot_c - bal_c)
                    if tot_c > 0 or bal_c > 0:
                        g_used_usd = round(used_c / 100.0, 2)
                        g_rem_usd = round(bal_c / 100.0, 2)
                        g_tot_usd = round(tot_c / 100.0, 2) if tot_c > 0 else None
                        g_used_pct = (used_c / tot_c * 100.0) if tot_c > 0 else None
                        if report.credits.remaining is None and report.credits.used is None:
                            report.credits = CreditBalance(
                                used=g_used_usd,
                                remaining=g_rem_usd,
                                limit=g_tot_usd,
                                unit="USD",
                                used_percent=g_used_pct,
                                remaining_percent=max(0.0, 100.0 - g_used_pct) if g_used_pct is not None else None,
                                note=report.credits.note or "Credit Grants",
                            )
                        report.sub_metrics.append(
                            SubMetric(
                                name="Credit Grants Balance",
                                used_str=f"${used_c / 100.0:.2f} / ${tot_c / 100.0:.2f}",
                                remaining_str=f"${bal_c / 100.0:.2f} left",
                                used_percent=g_used_pct,
                            )
                        )

                if report.weekly.used is not None or report.credits.used is not None:
                    return True

        # Only call cursor.com/api/usage-summary if we have a browser cookie or if RPC wasn't used
        if cookie:
            headers, user_id = self._build_auth_headers(token, cookie)
            summary_data: Optional[dict[str, Any]] = None
            for base_url in ("https://cursor.com", "https://www.cursor.com"):
                try:
                    summary_data = self.http_json(
                        f"{base_url}/api/usage-summary",
                        headers=headers,
                    )
                    if isinstance(summary_data, dict) and summary_data:
                        sources.append("usage-summary API")
                        break
                except Exception as exc:
                    if not report.error_message:
                        report.error_message = str(exc)

            if isinstance(summary_data, dict) and summary_data:
                report.status = "ok"
                report.error_message = None
                m_type = summary_data.get("membershipType")
                if m_type:
                    report.plan_tier = f"Cursor {str(m_type).capitalize()}"

                indiv = summary_data.get("individualUsage") or {}
                plan = indiv.get("plan") if isinstance(indiv, dict) else {}
                on_demand = indiv.get("onDemand") if isinstance(indiv, dict) else None

                if isinstance(plan, dict) and plan:
                    self._apply_plan_and_on_demand(
                        report,
                        plan_dict=plan,
                        on_demand_dict=on_demand,
                        cycle_start=summary_data.get("billingCycleStart"),
                        cycle_end=summary_data.get("billingCycleEnd"),
                    )
                    return True

        return False

    def fetch(self) -> ProviderReport:
        report = self.new_report()

        db_auth = self._read_sqlite_auth()
        cli_auth = self._load_cursor_cli_auth()

        cookie = self.cfg.get("cookie")
        membership = db_auth.get("cursorAuth/stripeMembershipType")
        backend_url = db_auth.get("cursorAuth/storedServerBackendUrl") or "https://api2.cursor.sh"
        sources: list[str] = []

        if db_auth:
            sources.append("state.vscdb")
        if any(cli_auth.get(k) for k in ("access_token", "refresh_token", "api_key")):
            sources.append("cursor-agent CLI")
        if membership:
            report.plan_tier = f"Cursor {membership.capitalize()}"

        # Collect candidate tokens in priority order, skipping Electron safeStorage blobs
        candidate_tokens: list[str] = []
        raw_candidates = [
            self.cfg.get("access_token"),
            cli_auth.get("access_token"),
            db_auth.get("cursorAuth/accessToken"),
            self.cfg.get("api_key"),
            cli_auth.get("api_key"),
        ]
        auth_json_rt = cli_auth.get("auth_json_refresh_token")
        auth_json_at = cli_auth.get("auth_json_access_token")
        refresh_tokens: list[str] = []
        for r in (cli_auth.get("refresh_token"), db_auth.get("cursorAuth/refreshToken")):
            if (
                r
                and str(r).strip()
                and str(r).strip() != "xai-identity-session"
                and not str(r).strip().startswith("cursor-safe-storage:")
                and str(r).strip() not in refresh_tokens
            ):
                refresh_tokens.append(str(r).strip())

        refresh_cache: dict[str, Optional[str]] = {}

        def _try_refresh_once(rt_val: str) -> Optional[str]:
            if rt_val not in refresh_cache:
                refresh_cache[rt_val] = self._refresh_access_token(
                    rt_val, backend_url=backend_url
                )
            return refresh_cache[rt_val]

        isolated = self._is_isolated_profile()
        kc_rt_queried = (
            isolated
            or not cli_auth.get("access_token")
            or self._is_jwt_expired(cli_auth["access_token"])
        )
        kc_api_queried = kc_rt_queried

        for tok in raw_candidates:
            if not tok or not str(tok).strip():
                continue
            t_str = str(tok).strip()
            if t_str.startswith("cursor-safe-storage:"):
                continue
            if self._is_jwt_expired(t_str):
                if not kc_rt_queried and not cli_auth.get("refresh_token"):
                    kc_rt_queried = True
                    lazy_rt = self._clean_auth_token(
                        self._read_keychain_secret("cursor-refresh-token"), is_refresh=True
                    )
                    if lazy_rt:
                        cli_auth["refresh_token"] = lazy_rt
                        self._last_keychain_rt = lazy_rt
                        if "cursor-agent CLI" not in sources:
                            sources.append("cursor-agent CLI")
                        if lazy_rt not in refresh_tokens:
                            refresh_tokens.insert(0, lazy_rt)
                for r_tok in refresh_tokens:
                    fresh = _try_refresh_once(str(r_tok).strip())
                    if fresh and fresh not in candidate_tokens:
                        candidate_tokens.append(fresh)
            if t_str not in candidate_tokens:
                candidate_tokens.append(t_str)

        if not candidate_tokens and refresh_tokens:
            for r_tok in refresh_tokens:
                fresh = _try_refresh_once(str(r_tok).strip())
                if fresh and fresh not in candidate_tokens:
                    candidate_tokens.append(fresh)

        if not candidate_tokens and not cookie:
            if db_auth:
                report.status = "partial"
                report.source = " + ".join(dict.fromkeys(sources))
            if self.account:
                report.auth_hint = (
                    f"Run `tokenmaster login cursor {self.account}` or `tokenmaster account snapshot cursor {self.account}`"
                )
            else:
                report.auth_hint = (
                    "Run `cursor-agent login` (or `cursor agent login`) to authenticate Cursor CLI"
                )
            return self.apply_config_overrides(report)

        populated = False
        for tok in candidate_tokens:
            if self._try_fetch_with_token(report, tok, cookie, sources, backend_url=backend_url):
                populated = True
                break

        # If all direct tokens failed, lazily load Keychain refresh_token / api_key fallbacks
        if not populated:
            lazy_api: Optional[str] = None
            if not kc_rt_queried and not cli_auth.get("refresh_token"):
                kc_rt_queried = True
                lazy_rt = self._clean_auth_token(
                    self._read_keychain_secret("cursor-refresh-token"), is_refresh=True
                )
                if lazy_rt:
                    cli_auth["refresh_token"] = lazy_rt
                    self._last_keychain_rt = lazy_rt
                    if "cursor-agent CLI" not in sources:
                        sources.append("cursor-agent CLI")
                    if lazy_rt not in refresh_tokens:
                        refresh_tokens.insert(0, lazy_rt)
            if not kc_api_queried and not cli_auth.get("api_key"):
                kc_api_queried = True
                lazy_api = self._clean_auth_token(self._read_keychain_secret("cursor-api-key"))
                if lazy_api:
                    cli_auth["api_key"] = lazy_api
                    if "cursor-agent CLI" not in sources:
                        sources.append("cursor-agent CLI")
            for r_tok in refresh_tokens:
                fresh_tok = _try_refresh_once(str(r_tok).strip())
                if fresh_tok and fresh_tok not in candidate_tokens:
                    candidate_tokens.append(fresh_tok)
                    if self._try_fetch_with_token(
                        report, fresh_tok, cookie, sources, backend_url=backend_url
                    ):
                        populated = True
                        break
            if not populated and lazy_api and lazy_api not in candidate_tokens:
                candidate_tokens.append(lazy_api)
                if self._try_fetch_with_token(
                    report, lazy_api, cookie, sources, backend_url=backend_url
                ):
                    populated = True

        # Cookie-only path if no token succeeded
        if not populated and cookie:
            self._try_fetch_with_token(report, "", cookie, sources, backend_url=backend_url)

        if sources:
            report.source = " + ".join(dict.fromkeys(sources))

        if report.status == "unconfigured" and candidate_tokens:
            report.status = "partial"
            report.auth_hint = (
                "Cursor token expired or rejected; run `cursor-agent login` or reopen Cursor IDE"
            )

        return self.apply_config_overrides(report)
