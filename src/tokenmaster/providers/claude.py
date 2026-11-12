"""Claude Code / Anthropic provider (OAuth usage API + macOS Keychain + ~/.claude JSONL)."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from tokenmaster.cache import FileScanCache
from tokenmaster.models import CreditBalance, ProviderReport, SubMetric, UsageWindow, format_reset_countdown
from tokenmaster.pricing import estimate_token_cost_usd
from tokenmaster.providers.base import (
    BaseProvider,
    HttpError,
    atomic_write_text,
    critical_section,
    is_unauthorized,
    run_concurrently,
)
from tokenmaster.warehouse import (
    SessionLedgerRow,
    UsageWarehouse,
    normalize_day_utc,
    sanitize_project_name,
)

MAX_CACHED_EVENTS_PER_FILE = 256
CLAUDE_SCAN_SCHEMA = 4

CLAUDE_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_DEFAULT_OAUTH_SCOPES: tuple[str, ...] = (
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
    "user:plugins",
)
CLAUDE_EXPIRY_BUFFER_MS = 60_000.0  # Refresh 60s before expiresAt
CLAUDE_USAGE_CACHE_SCHEMA = 1
CLAUDE_USAGE_CACHE_MAX_STALE_SECONDS = 21_600.0  # Up to 6h fallback on HTTP 429 / 5xx
CLAUDE_RATE_LIMIT_COOLDOWN_SECONDS = 60.0  # Cooldown window after HTTP 429

_CLAUDE_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_CLAUDE_REFRESH_LOCKS_GUARD = threading.Lock()


def _claude_refresh_lock(source_key: str) -> threading.Lock:
    with _CLAUDE_REFRESH_LOCKS_GUARD:
        lock = _CLAUDE_REFRESH_LOCKS.get(source_key)
        if lock is None:
            lock = threading.Lock()
            _CLAUDE_REFRESH_LOCKS[source_key] = lock
        return lock


def claude_keychain_account_name() -> str:
    """Return the macOS Keychain account name used by Claude Code CLI (``YI()``)."""
    user = ""
    try:
        user = os.environ.get("USER") or getpass.getuser() or ""
    except Exception:
        user = "claude-code-user"
    if not user or not re.match(r"^[a-zA-Z0-9._-]+$", user):
        return "claude-code-user"
    return user


def claude_keychain_service_name_for_dir(config_dir: Path | str) -> str:
    """Compute the exact macOS Keychain service name used by Claude Code CLI when
    ``CLAUDE_CONFIG_DIR`` is set (``Claude Code-credentials-<sha256(nfc_path)[:8]>``).
    """
    nfc_path = unicodedata.normalize("NFC", str(config_dir))
    digest8 = hashlib.sha256(nfc_path.encode("utf-8")).hexdigest()[:8]
    return f"Claude Code-credentials-{digest8}"


def claude_keychain_service_names(
    cdir: Path,
    *,
    is_isolated_profile: bool = False,
    has_env_config_dir: bool = False,
) -> list[str]:
    """Return candidate macOS Keychain service names for ``cdir`` in priority order.

    Matches Claude Code CLI's ``kW()`` function: whenever ``CLAUDE_CONFIG_DIR`` is active
    or ``is_isolated_profile`` is True, only the hashed ``Claude Code-credentials-<sha256[:8]>``
    service names are queried and the global ``"Claude Code-credentials"`` service is
    strictly excluded so an unauthenticated profile never leaks the default token.
    """
    if is_isolated_profile or has_env_config_dir:
        services: list[str] = []
        candidates = [str(cdir), os.path.abspath(str(cdir))]
        try:
            # Resolve symlinks on parent directory (e.g. /var -> /private/var on macOS)
            # while preserving the exact leaf directory name casing.
            parent_resolved = str(cdir.parent.resolve() / cdir.name)
            candidates.append(parent_resolved)
        except OSError:
            pass
        for cand in candidates:
            svc = claude_keychain_service_name_for_dir(cand)
            if svc not in services:
                services.append(svc)
        return services
    return ["Claude Code-credentials"]


def summarize_claude_jsonl(fpath: Path, default_ts: float) -> dict[str, Any]:
    """Parse one ~/.claude/projects JSONL file into a usage summary.

    Returns ``{"schema", "min_ts", "max_ts", "tokens", "cost_usd", "events"}``.
    ``events`` holds ``[[ts, tokens, cost_usd, dedup_key], ...]`` for all unique
    turns in the file. Callers persisting to ``FileScanCache`` drop ``events``
    (setting it to ``None``) when ``len(events) > MAX_CACHED_EVENTS_PER_FILE``
    to bound cache file size while still using the in-memory ``events`` on the
    current cold scan without re-reading disk.
    """
    seen_ids: set[str] = set()
    min_ts: Optional[float] = None
    max_ts: Optional[float] = None
    total_tokens = 0
    total_cost = 0.0
    events: list[list[Any]] = []
    buckets_map: dict[tuple[str, str], list[Any]] = {}

    with fpath.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"usage"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue

            msg_id = msg.get("id") or ""
            req_id = obj.get("requestId") or ""
            dedup_key = f"{msg_id}:{req_id}" if msg_id else ""
            if dedup_key:
                if dedup_key in seen_ids:
                    continue
                seen_ids.add(dedup_key)

            entry_ts = default_ts
            ts_str = obj.get("timestamp")
            if isinstance(ts_str, str):
                try:
                    raw_ts = ts_str[:-1] + "+00:00" if ts_str.endswith("Z") else ts_str
                    entry_ts = datetime.fromisoformat(raw_ts).timestamp()
                except ValueError:
                    pass

            in_tok = int(usage.get("input_tokens") or 0)
            cache_w = int(usage.get("cache_creation_input_tokens") or 0)
            cache_r = int(usage.get("cache_read_input_tokens") or 0)
            out_tok = int(usage.get("output_tokens") or 0)
            tot_tok = in_tok + cache_w + cache_r + out_tok
            model_name = str(msg.get("model") or "claude-sonnet-4")

            cost = estimate_token_cost_usd(
                model_name,
                input_tokens=in_tok,
                cached_input_tokens=cache_r,
                cache_write_tokens=cache_w,
                output_tokens=out_tok,
            )
            min_ts = entry_ts if min_ts is None else min(min_ts, entry_ts)
            max_ts = entry_ts if max_ts is None else max(max_ts, entry_ts)
            total_tokens += tot_tok
            total_cost += cost
            events.append(
                [entry_ts, tot_tok, cost, dedup_key, in_tok, out_tok, cache_r, cache_w, model_name]
            )
            day_s = normalize_day_utc(entry_ts)
            bkey = (day_s, model_name)
            b = buckets_map.get(bkey)
            if b is None:
                buckets_map[bkey] = [
                    day_s,
                    model_name,
                    entry_ts,
                    entry_ts,
                    in_tok,
                    out_tok,
                    cache_r,
                    cache_w,
                    tot_tok,
                    cost,
                ]
            else:
                b[2] = min(float(b[2]), entry_ts)
                b[3] = max(float(b[3]), entry_ts)
                b[4] += in_tok
                b[5] += out_tok
                b[6] += cache_r
                b[7] += cache_w
                b[8] += tot_tok
                b[9] += cost

    return {
        "schema": CLAUDE_SCAN_SCHEMA,
        "min_ts": min_ts,
        "max_ts": max_ts,
        "tokens": total_tokens,
        "cost_usd": total_cost,
        "events": events,
        "buckets": list(buckets_map.values()),
        "dedup_keys": sorted(seen_ids),
    }


class ClaudeProvider(BaseProvider):
    """Monitors Claude Code weekly usage/remaining (Total + Fable) and credit (extra_usage) usage/remaining."""

    provider_id = "claude"
    display_name = "Claude (Anthropic)"

    def _claude_dir(self) -> Path:
        isolated = self._resolve_isolated_dir("config_dir")
        if isolated is not None:
            return isolated
        return self._resolve_default_env_dir("CLAUDE_CONFIG_DIR", Path.home() / ".claude")

    @staticmethod
    def _parse_oauth_fields(raw: dict[str, Any]) -> dict[str, Any]:
        oauth = raw.get("claudeAiOauth") if isinstance(raw.get("claudeAiOauth"), dict) else raw
        if not isinstance(oauth, dict):
            return {}
        at_raw = oauth.get("accessToken") or oauth.get("access_token")
        rt_raw = oauth.get("refreshToken") or oauth.get("refresh_token")
        exp_raw = oauth.get("expiresAt") if oauth.get("expiresAt") is not None else oauth.get("expires_at")
        cid_raw = oauth.get("clientId") or oauth.get("client_id")
        raw_scopes = oauth.get("scopes")
        scopes: Optional[list[str]] = None
        if isinstance(raw_scopes, list):
            scopes = [str(s).strip() for s in raw_scopes if isinstance(s, str) and s.strip()]
        elif isinstance(raw_scopes, str) and raw_scopes.strip():
            scopes = [s for s in raw_scopes.strip().split(" ") if s]

        exp_ms: Optional[float] = None
        if isinstance(exp_raw, (int, float)) and not isinstance(exp_raw, bool):
            exp_ms = float(exp_raw)
            if 0 < exp_ms < 1e11:
                exp_ms *= 1000.0
        elif isinstance(exp_raw, str) and exp_raw.strip():
            try:
                val = float(exp_raw.strip())
                exp_ms = val * 1000.0 if 0 < val < 1e11 else val
            except ValueError:
                pass

        return {
            "access_token": str(at_raw).strip() if isinstance(at_raw, str) and at_raw.strip() else None,
            "refresh_token": str(rt_raw).strip() if isinstance(rt_raw, str) and rt_raw.strip() else None,
            "expires_at_ms": exp_ms,
            "scopes": scopes,
            "client_id": str(cid_raw).strip() if isinstance(cid_raw, str) and cid_raw.strip() else None,
            "sub_type": str(oauth["subscriptionType"]) if oauth.get("subscriptionType") else None,
            "tier": str(oauth["rateLimitTier"]) if oauth.get("rateLimitTier") else None,
        }

    @staticmethod
    def _is_oauth_expired(expires_at_ms: Optional[float]) -> bool:
        if expires_at_ms is None:
            return False
        try:
            return float(expires_at_ms) <= (time.time() * 1000.0 + CLAUDE_EXPIRY_BUFFER_MS)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _read_keychain_json(service_name: str) -> Optional[dict[str, Any]]:
        if sys.platform != "darwin":
            return None
        acct = claude_keychain_account_name()
        for cmd in (
            ["security", "find-generic-password", "-a", acct, "-s", service_name, "-w"],
            ["security", "find-generic-password", "-s", service_name, "-w"],
        ):
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                    check=False,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    raw = json.loads(proc.stdout.strip())
                    if isinstance(raw, dict):
                        return raw
            except Exception:
                continue
        return None

    @staticmethod
    def _write_keychain_json(service_name: str, raw_dict: dict[str, Any]) -> bool:
        if sys.platform != "darwin":
            return False
        try:
            acct = claude_keychain_account_name()
            payload_hex = json.dumps(raw_dict).encode("utf-8").hex()
            cmd_line = f'add-generic-password -U -a "{acct}" -s "{service_name}" -X "{payload_hex}"\n'
            if len(cmd_line) <= 4032:
                proc = subprocess.run(
                    ["security", "-i"],
                    input=cmd_line,
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                    check=False,
                )
            else:
                proc = subprocess.run(
                    [
                        "security",
                        "add-generic-password",
                        "-U",
                        "-a",
                        acct,
                        "-s",
                        service_name,
                        "-X",
                        payload_hex,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                    check=False,
                )
            return proc.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _apply_oauth_updates_to_raw(
        raw: dict[str, Any],
        *,
        new_at: str,
        new_rt: str,
        new_exp_ms: Optional[float],
        new_rt_exp_ms: Optional[int],
        new_scopes: Optional[list[str]],
        clear_scopes: bool = False,
    ) -> dict[str, Any]:
        updated = dict(raw)
        if isinstance(updated.get("claudeAiOauth"), dict):
            oauth = dict(updated["claudeAiOauth"])
            target_is_nested = True
        else:
            oauth = updated
            target_is_nested = False

        if "access_token" in oauth and "accessToken" not in oauth:
            oauth["access_token"] = new_at
        else:
            oauth["accessToken"] = new_at

        if new_rt:
            if "refresh_token" in oauth and "refreshToken" not in oauth:
                oauth["refresh_token"] = new_rt
            else:
                oauth["refreshToken"] = new_rt

        if new_exp_ms is not None:
            if "expires_at" in oauth and "expiresAt" not in oauth:
                oauth["expires_at"] = int(new_exp_ms)
            else:
                oauth["expiresAt"] = int(new_exp_ms)

        if new_rt_exp_ms is not None:
            oauth["refreshTokenExpiresAt"] = int(new_rt_exp_ms)

        if new_scopes:
            oauth["scopes"] = list(new_scopes)
        elif clear_scopes:
            oauth.pop("scopes", None)

        if target_is_nested:
            updated["claudeAiOauth"] = oauth
        return updated

    def _candidate_keychain_services(self, cdir: Path) -> list[str]:
        default_home_claude = Path.home() / ".claude"
        is_custom_dir = os.path.abspath(str(cdir)) != os.path.abspath(str(default_home_claude))
        return claude_keychain_service_names(
            cdir,
            is_isolated_profile=self._is_isolated_profile(),
            has_env_config_dir=is_custom_dir,
        )

    def _load_oauth_bundle(self) -> dict[str, Any]:
        """Load complete Claude OAuth credential context (token, refreshToken, expiry, sources)."""
        cfg_tok_raw = self.cfg.get("oauth_token")
        cfg_tok: Optional[str] = (
            str(cfg_tok_raw).strip()
            if isinstance(cfg_tok_raw, str) and cfg_tok_raw.strip()
            else None
        )

        cdir = self._claude_dir()
        file_bundle: Optional[dict[str, Any]] = None
        for fname in (".credentials.json", "credentials.json"):
            fpath = cdir / fname
            if fpath.exists() and os.access(fpath, os.R_OK):
                try:
                    raw = json.loads(fpath.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        fields = self._parse_oauth_fields(raw)
                        if fields.get("access_token") or fields.get("refresh_token"):
                            file_bundle = {
                                **fields,
                                "source_file": fpath,
                                "source_keychain_service": None,
                            }
                            break
                except Exception:
                    pass

        # Query macOS Keychain when no file credentials exist, or when the file's
        # accessToken is missing/expired (since `claude` CLI on macOS writes to Keychain).
        need_keychain = sys.platform == "darwin" and (
            file_bundle is None
            or not file_bundle.get("access_token")
            or self._is_oauth_expired(file_bundle.get("expires_at_ms"))
        )
        kc_bundle: Optional[dict[str, Any]] = None
        if need_keychain:
            for service_name in self._candidate_keychain_services(cdir):
                raw_kc = self._read_keychain_json(service_name)
                if isinstance(raw_kc, dict):
                    fields = self._parse_oauth_fields(raw_kc)
                    if fields.get("access_token") or fields.get("refresh_token"):
                        kc_bundle = {
                            **fields,
                            "source_file": file_bundle.get("source_file") if file_bundle else None,
                            "source_keychain_service": service_name,
                        }
                        break

        bundle: dict[str, Any]
        if file_bundle and kc_bundle:
            file_expired = not file_bundle.get("access_token") or self._is_oauth_expired(
                file_bundle.get("expires_at_ms")
            )
            kc_expired = not kc_bundle.get("access_token") or self._is_oauth_expired(
                kc_bundle.get("expires_at_ms")
            )
            file_exp = file_bundle.get("expires_at_ms") or 0.0
            kc_exp = kc_bundle.get("expires_at_ms") or 0.0
            prefer_kc = (
                (file_expired and not kc_expired)
                or kc_exp > file_exp
                or (not file_bundle.get("refresh_token") and bool(kc_bundle.get("refresh_token")))
            )
            if prefer_kc:
                bundle = {
                    **file_bundle,
                    **{k: v for k, v in kc_bundle.items() if v is not None},
                    "expires_at_ms": kc_bundle.get("expires_at_ms"),
                    "source_file": file_bundle.get("source_file"),
                    "source_keychain_service": kc_bundle.get("source_keychain_service"),
                }
            else:
                bundle = {
                    **kc_bundle,
                    **{k: v for k, v in file_bundle.items() if v is not None},
                    "expires_at_ms": file_bundle.get("expires_at_ms"),
                    "source_file": file_bundle.get("source_file"),
                    "source_keychain_service": kc_bundle.get("source_keychain_service"),
                }
        elif kc_bundle is not None:
            bundle = kc_bundle
        elif file_bundle is not None:
            bundle = file_bundle
        else:
            bundle = {
                "access_token": None,
                "refresh_token": None,
                "expires_at_ms": None,
                "scopes": None,
                "client_id": None,
                "sub_type": None,
                "tier": None,
                "source_file": None,
                "source_keychain_service": None,
            }

        bundle["initial_store_access_token"] = bundle.get("access_token")
        bundle["initial_store_refresh_token"] = bundle.get("refresh_token")
        bundle["has_manual_override"] = bool(cfg_tok)
        bundle["failed_tokens"] = set()

        if cfg_tok:
            disc_at = bundle.get("access_token")
            if disc_at and disc_at != cfg_tok:
                bundle["discovered_access_token"] = disc_at
            bundle["access_token"] = cfg_tok

        return bundle

    def _load_oauth_credentials(self) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Return (access_token, subscription_type, rate_limit_tier)."""
        bundle = self._load_oauth_bundle()
        self._last_oauth_bundle = bundle
        return bundle.get("access_token"), bundle.get("sub_type"), bundle.get("tier")

    def _persist_refreshed_oauth(
        self,
        bundle: dict[str, Any],
        *,
        used_rt: Optional[str],
        new_at: str,
        new_rt: str,
        new_exp_ms: Optional[float],
        new_rt_exp_ms: Optional[int],
        new_scopes: Optional[list[str]],
        clear_scopes: bool = False,
    ) -> None:
        source_file: Optional[Path] = bundle.get("source_file")
        source_kc: Optional[str] = bundle.get("source_keychain_service")

        if isinstance(source_file, Path) and source_file.is_file():
            try:
                raw_file = json.loads(source_file.read_text(encoding="utf-8"))
                if isinstance(raw_file, dict):
                    f_fields = self._parse_oauth_fields(raw_file)
                    f_rt = f_fields.get("refresh_token")
                    f_at = f_fields.get("access_token")
                    # CAS guard: do not clobber a newer token written externally while in flight
                    if not (
                        used_rt
                        and f_rt
                        and f_rt not in (used_rt, new_rt)
                        and f_at
                        and not self._is_oauth_expired(f_fields.get("expires_at_ms"))
                    ):
                        updated_file = self._apply_oauth_updates_to_raw(
                            raw_file,
                            new_at=new_at,
                            new_rt=new_rt,
                            new_exp_ms=new_exp_ms,
                            new_rt_exp_ms=new_rt_exp_ms,
                            new_scopes=new_scopes,
                            clear_scopes=clear_scopes,
                        )
                        atomic_write_text(
                            source_file,
                            json.dumps(updated_file, indent=2) + "\n",
                            mode=0o600,
                        )
            except Exception:
                pass

        # If we refreshed a file on macOS and haven't checked Keychain yet, also sync any
        # matching Keychain entry for this directory so `claude` CLI and `.credentials.json` stay aligned.
        kc_services_to_check: list[str] = []
        if isinstance(source_kc, str) and source_kc:
            kc_services_to_check.append(source_kc)
        elif sys.platform == "darwin":
            kc_services_to_check.extend(self._candidate_keychain_services(self._claude_dir()))

        for svc in kc_services_to_check:
            raw_kc = self._read_keychain_json(svc)
            if not isinstance(raw_kc, dict):
                continue
            kc_fields = self._parse_oauth_fields(raw_kc)
            kc_rt = kc_fields.get("refresh_token")
            kc_at = kc_fields.get("access_token")
            if source_kc is None and used_rt and kc_rt != used_rt:
                continue
            if (
                used_rt
                and kc_rt
                and kc_rt not in (used_rt, new_rt)
                and kc_at
                and not self._is_oauth_expired(kc_fields.get("expires_at_ms"))
            ):
                continue
            updated_kc = self._apply_oauth_updates_to_raw(
                raw_kc,
                new_at=new_at,
                new_rt=new_rt,
                new_exp_ms=new_exp_ms,
                new_rt_exp_ms=new_rt_exp_ms,
                new_scopes=new_scopes,
                clear_scopes=clear_scopes,
            )
            self._write_keychain_json(svc, updated_kc)
            break

    def _refresh_oauth_token(self, bundle: dict[str, Any]) -> Optional[str]:
        """Exchange ``refreshToken`` for a fresh ``accessToken`` via platform.claude.com/v1/oauth/token.

        Serialized per Claude config directory and wrapped in ``critical_section()`` because
        Anthropic OAuth rotates ``refresh_token`` on each refresh.
        """
        bundle["_last_refresh_was_network"] = False
        cdir = self._claude_dir()
        source_kc = bundle.get("source_keychain_service")
        source_file = bundle.get("source_file")
        if source_kc or source_file or sys.platform == "darwin":
            lock_key = f"dir:{os.path.abspath(str(cdir))}"
        else:
            lock_key = f"mem:{self.instance_id}"

        with _claude_refresh_lock(lock_key), critical_section():
            # Re-read both Keychain and file under lock: if a sibling thread or `claude` CLI
            # rotated the token in Keychain or `.credentials.json`, adopt it instead of replaying
            # a consumed refreshToken.
            kc_fields: Optional[dict[str, Any]] = None
            kc_services: list[str] = []
            if isinstance(source_kc, str) and source_kc:
                kc_services.append(source_kc)
            elif sys.platform == "darwin":
                kc_services.extend(self._candidate_keychain_services(cdir))

            for svc in kc_services:
                raw_kc = self._read_keychain_json(svc)
                if isinstance(raw_kc, dict):
                    parsed_kc = self._parse_oauth_fields(raw_kc)
                    if parsed_kc.get("access_token") or parsed_kc.get("refresh_token"):
                        kc_fields = parsed_kc
                        if not source_kc:
                            source_kc = svc
                            bundle["source_keychain_service"] = svc
                        break

            f_fields: Optional[dict[str, Any]] = None
            if isinstance(source_file, Path) and source_file.is_file():
                try:
                    raw_f = json.loads(source_file.read_text(encoding="utf-8"))
                    if isinstance(raw_f, dict):
                        parsed_f = self._parse_oauth_fields(raw_f)
                        if parsed_f.get("access_token") or parsed_f.get("refresh_token"):
                            f_fields = parsed_f
                except Exception:
                    pass

            latest_fields: Optional[dict[str, Any]] = None
            latest_from_kc = False
            if kc_fields and f_fields:
                f_exp_flag = not f_fields.get("access_token") or self._is_oauth_expired(
                    f_fields.get("expires_at_ms")
                )
                kc_exp_flag = not kc_fields.get("access_token") or self._is_oauth_expired(
                    kc_fields.get("expires_at_ms")
                )
                f_exp_val = f_fields.get("expires_at_ms") or 0.0
                kc_exp_val = kc_fields.get("expires_at_ms") or 0.0
                if (
                    (f_exp_flag and not kc_exp_flag)
                    or kc_exp_val > f_exp_val
                    or (not f_fields.get("refresh_token") and bool(kc_fields.get("refresh_token")))
                ):
                    latest_fields = {**f_fields, **{k: v for k, v in kc_fields.items() if v is not None}}
                    latest_fields["expires_at_ms"] = kc_fields.get("expires_at_ms")
                    latest_from_kc = True
                else:
                    latest_fields = {**kc_fields, **{k: v for k, v in f_fields.items() if v is not None}}
                    latest_fields["expires_at_ms"] = f_fields.get("expires_at_ms")
            elif kc_fields:
                latest_fields = kc_fields
                latest_from_kc = True
            elif f_fields:
                latest_fields = f_fields

            failed_tokens: set[str] = set(bundle.get("failed_tokens") or set())
            known_tokens: set[str] = set(failed_tokens)
            for k_tok in ("access_token", "discovered_access_token", "initial_store_access_token"):
                v_tok = bundle.get(k_tok)
                if isinstance(v_tok, str) and v_tok:
                    known_tokens.add(v_tok)

            if isinstance(latest_fields, dict):
                disk_at = latest_fields.get("access_token")
                disk_rt = latest_fields.get("refresh_token")
                disk_exp = latest_fields.get("expires_at_ms")
                for meta_k in ("scopes", "client_id", "sub_type", "tier"):
                    if latest_fields.get(meta_k) is not None and not bundle.get(meta_k):
                        bundle[meta_k] = latest_fields[meta_k]

                if disk_at and disk_at not in known_tokens and not self._is_oauth_expired(disk_exp):
                    for k in ("access_token", "refresh_token", "expires_at_ms", "scopes", "sub_type", "tier"):
                        if latest_fields.get(k) is not None:
                            bundle[k] = latest_fields[k]
                    bundle["initial_store_access_token"] = disk_at
                    if disk_rt:
                        bundle["initial_store_refresh_token"] = disk_rt
                    # If Keychain had a newer rotated token than `.credentials.json`, sync `.credentials.json`
                    if latest_from_kc and isinstance(source_file, Path) and source_file.is_file():
                        self._persist_refreshed_oauth(
                            bundle,
                            used_rt=None,
                            new_at=str(disk_at),
                            new_rt=str(disk_rt or ""),
                            new_exp_ms=disk_exp,
                            new_rt_exp_ms=None,
                            new_scopes=latest_fields.get("scopes"),
                        )
                    return str(disk_at)

                if disk_rt and str(disk_rt).strip():
                    bundle["refresh_token"] = str(disk_rt).strip()

            rt_raw = bundle.get("refresh_token")
            if not rt_raw or not str(rt_raw).strip():
                return None
            rt = str(rt_raw).strip()

            client_id = str(bundle.get("client_id") or CLAUDE_OAUTH_CLIENT_ID).strip()
            scopes_list = bundle.get("scopes") or list(CLAUDE_DEFAULT_OAUTH_SCOPES)
            scope_str = " ".join(str(s).strip() for s in scopes_list if str(s).strip())

            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "claude-code/2.1.0",
            }
            payload: dict[str, Any] = {
                "grant_type": "refresh_token",
                "refresh_token": rt,
                "client_id": client_id,
            }
            if scope_str:
                payload["scope"] = scope_str

            bundle["_last_refresh_was_network"] = True
            used_scope_fallback = False
            resp: Optional[dict[str, Any]] = None
            try:
                resp = self.http_json(
                    CLAUDE_OAUTH_TOKEN_URL,
                    method="POST",
                    headers=headers,
                    payload=payload,
                )
            except HttpError as exc:
                if exc.status == 400 and "scope" in payload and "invalid_scope" in (exc.body or "").lower():
                    used_scope_fallback = True
                    fallback_payload = {
                        "grant_type": "refresh_token",
                        "refresh_token": rt,
                        "client_id": client_id,
                    }
                    try:
                        resp = self.http_json(
                            CLAUDE_OAUTH_TOKEN_URL,
                            method="POST",
                            headers=headers,
                            payload=fallback_payload,
                        )
                    except Exception:
                        return None
                else:
                    return None
            except Exception:
                return None

            if not isinstance(resp, dict):
                return None
            new_at_raw = resp.get("access_token") or resp.get("accessToken")
            if not isinstance(new_at_raw, str) or not new_at_raw.strip():
                return None
            new_at = new_at_raw.strip()

            new_rt_raw = resp.get("refresh_token") or resp.get("refreshToken")
            new_rt = str(new_rt_raw).strip() if isinstance(new_rt_raw, str) and new_rt_raw.strip() else rt

            expires_in = resp.get("expires_in")
            new_exp_ms: Optional[float] = None
            if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and expires_in > 0:
                new_exp_ms = float(int((time.time() + float(expires_in)) * 1000))

            rt_expires_in = resp.get("refresh_token_expires_in")
            new_rt_exp_ms: Optional[int] = None
            if (
                isinstance(rt_expires_in, (int, float))
                and not isinstance(rt_expires_in, bool)
                and rt_expires_in > 0
            ):
                new_rt_exp_ms = int((time.time() + float(rt_expires_in)) * 1000)

            resp_scope = resp.get("scope") if resp.get("scope") is not None else resp.get("scopes")
            new_scopes: Optional[list[str]] = None
            clear_scopes = False
            if isinstance(resp_scope, str) and resp_scope.strip():
                new_scopes = [s for s in resp_scope.strip().split(" ") if s]
            elif isinstance(resp_scope, list):
                parsed_sc = [str(s).strip() for s in resp_scope if isinstance(s, str) and str(s).strip()]
                if parsed_sc:
                    new_scopes = parsed_sc
            if new_scopes is None:
                if used_scope_fallback:
                    clear_scopes = True
                else:
                    new_scopes = bundle.get("scopes")

            self._persist_refreshed_oauth(
                bundle,
                used_rt=rt,
                new_at=new_at,
                new_rt=new_rt,
                new_exp_ms=new_exp_ms,
                new_rt_exp_ms=new_rt_exp_ms,
                new_scopes=new_scopes,
                clear_scopes=clear_scopes,
            )

            bundle["access_token"] = new_at
            bundle["refresh_token"] = new_rt
            bundle["initial_store_access_token"] = new_at
            bundle["initial_store_refresh_token"] = new_rt
            if new_exp_ms is not None:
                bundle["expires_at_ms"] = new_exp_ms
            if clear_scopes:
                bundle["scopes"] = None
            elif new_scopes is not None:
                bundle["scopes"] = new_scopes
            return new_at

    def _usage_cache_path(self) -> Path:
        cdir_abs = os.path.abspath(str(self._claude_dir()))
        default_home_claude = os.path.abspath(str(Path.home() / ".claude"))
        extra = (
            cdir_abs
            if (cdir_abs != default_home_claude and not self._is_isolated_profile())
            else None
        )
        return self._scoped_cache_path("claude_oauth_usage", extra_scope=extra)

    def _usage_cache_scope_key(self, bundle: dict[str, Any]) -> str:
        cdir_abs = os.path.abspath(str(self._claude_dir()))
        if bundle.get("has_manual_override") and bundle.get("access_token"):
            tok_hash = hashlib.sha256(str(bundle["access_token"]).encode("utf-8")).hexdigest()[:16]
            return f"manual:{cdir_abs}:{tok_hash}"
        return f"dir:{cdir_abs}"

    @staticmethod
    def _inspect_oauth_usage_cache(raw: Any) -> str:
        if not isinstance(raw, dict):
            return "invalid"
        if raw.get("schema") != CLAUDE_USAGE_CACHE_SCHEMA:
            return "invalid"
        scope_key = raw.get("scope_key")
        fetched_at = raw.get("fetched_at")
        data = raw.get("data")
        if (
            not isinstance(scope_key, str)
            or not scope_key
            or not isinstance(fetched_at, (int, float))
            or isinstance(fetched_at, bool)
            or not isinstance(data, dict)
        ):
            return "invalid"
        age = time.time() - float(fetched_at)
        if -60.0 <= age <= CLAUDE_USAGE_CACHE_MAX_STALE_SECONDS:
            return "ready"
        return "stale"

    def _load_cached_oauth_usage(
        self,
        scope_key: str,
        *,
        require_cooldown: bool = False,
    ) -> Optional[dict[str, Any]]:
        try:
            cpath = self._usage_cache_path()
            if not cpath.is_file():
                return None
            raw = json.loads(cpath.read_text(encoding="utf-8"))
            if self._inspect_oauth_usage_cache(raw) != "ready":
                return None
            if raw.get("scope_key") != scope_key:
                return None
            if require_cooldown:
                cooldown_until = raw.get("cooldown_until")
                if (
                    not isinstance(cooldown_until, (int, float))
                    or isinstance(cooldown_until, bool)
                    or float(cooldown_until) <= time.time()
                ):
                    return None
            return dict(raw["data"])
        except Exception:
            return None

    def _save_cached_oauth_usage(
        self,
        scope_key: str,
        data: dict[str, Any],
        *,
        cooldown_until: float = 0.0,
        fetched_at: Optional[float] = None,
    ) -> None:
        if not isinstance(data, dict):
            return
        try:
            cpath = self._usage_cache_path()
            cpath.parent.mkdir(parents=True, exist_ok=True)
            now = time.time()
            payload = {
                "schema": CLAUDE_USAGE_CACHE_SCHEMA,
                "scope_key": scope_key,
                "fetched_at": float(fetched_at) if fetched_at is not None else now,
                "cooldown_until": float(cooldown_until),
                "data": data,
            }
            with critical_section():
                atomic_write_text(cpath, json.dumps(payload) + "\n", mode=0o600)
        except Exception:
            pass

    def _fallback_cached_oauth_usage_on_error(
        self,
        scope_key: str,
        exc: Exception,
    ) -> Optional[dict[str, Any]]:
        if not (isinstance(exc, HttpError) and (exc.status == 429 or exc.status >= 500)):
            return None
        try:
            cpath = self._usage_cache_path()
            if not cpath.is_file():
                return None
            raw = json.loads(cpath.read_text(encoding="utf-8"))
            if self._inspect_oauth_usage_cache(raw) != "ready":
                return None
            if raw.get("scope_key") != scope_key:
                return None
            cached_data = dict(raw["data"])
            orig_fetched_at = float(raw["fetched_at"])
            if exc.status == 429:
                self._save_cached_oauth_usage(
                    scope_key,
                    cached_data,
                    cooldown_until=time.time() + CLAUDE_RATE_LIMIT_COOLDOWN_SECONDS,
                    fetched_at=orig_fetched_at,
                )
            return cached_data
        except Exception:
            return None

    def _query_oauth_usage_endpoint(self, access_token: str) -> dict[str, Any]:
        return self.http_json(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {access_token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "claude-code/2.1.0",
            },
        )

    def _fetch_oauth_usage_with_refresh(self, bundle: dict[str, Any]) -> dict[str, Any]:
        token = bundle.get("access_token")
        refresh_token = bundle.get("refresh_token")
        expires_at_ms = bundle.get("expires_at_ms")
        has_manual_override = bool(bundle.get("has_manual_override"))
        raw_failed = bundle.get("failed_tokens")
        failed_tokens: set[str] = raw_failed if isinstance(raw_failed, set) else set()
        bundle["failed_tokens"] = failed_tokens
        network_refresh_attempted = False

        # 1. Proactive refresh when accessToken is missing or already expired (unless manual override is set)
        if (
            not has_manual_override
            and (refresh_token or sys.platform == "darwin")
            and (not token or self._is_oauth_expired(expires_at_ms))
        ):
            if token:
                failed_tokens.add(token)
            fresh = self._refresh_oauth_token(bundle)
            network_refresh_attempted = bool(bundle.get("_last_refresh_was_network"))
            if fresh:
                token = fresh

        if not token:
            raise RuntimeError("Claude OAuth access token expired and refresh failed; run `claude auth login`")

        scope_key = self._usage_cache_scope_key(bundle)
        cooldown_cached = self._load_cached_oauth_usage(scope_key, require_cooldown=True)
        if cooldown_cached is not None:
            bundle["_usage_from_cache"] = True
            return cooldown_cached

        try:
            resp = self._query_oauth_usage_endpoint(token)
            if isinstance(resp, dict):
                self._save_cached_oauth_usage(scope_key, resp)
            return resp
        except Exception as exc:
            if not is_unauthorized(exc):
                fallback = self._fallback_cached_oauth_usage_on_error(scope_key, exc)
                if fallback is not None:
                    bundle["_usage_from_cache"] = True
                    return fallback
                raise
            failed_tokens.add(token)
            # 2. If a manual cfg['oauth_token'] failed with 401 and a different discovered token exists, try it
            disc_tok = bundle.get("discovered_access_token")
            if disc_tok and disc_tok not in failed_tokens and not self._is_oauth_expired(expires_at_ms):
                bundle["access_token"] = disc_tok
                bundle["has_manual_override"] = False
                token = disc_tok
                scope_key = self._usage_cache_scope_key(bundle)
                try:
                    resp = self._query_oauth_usage_endpoint(disc_tok)
                    if isinstance(resp, dict):
                        self._save_cached_oauth_usage(scope_key, resp)
                    return resp
                except Exception as disc_exc:
                    if not is_unauthorized(disc_exc):
                        fallback = self._fallback_cached_oauth_usage_on_error(scope_key, disc_exc)
                        if fallback is not None:
                            bundle["_usage_from_cache"] = True
                            return fallback
                        raise
                    failed_tokens.add(disc_tok)
            # 3. Reactive refresh on HTTP 401 (up to twice if the first pass adopted a Keychain/disk token that also 401'd)
            for _ in range(2):
                if network_refresh_attempted or not (bundle.get("refresh_token") or sys.platform == "darwin"):
                    break
                fresh = self._refresh_oauth_token(bundle)
                was_net = bool(bundle.get("_last_refresh_was_network"))
                if was_net:
                    network_refresh_attempted = True
                if not fresh or fresh in failed_tokens:
                    break
                bundle["has_manual_override"] = False
                scope_key = self._usage_cache_scope_key(bundle)
                try:
                    resp = self._query_oauth_usage_endpoint(fresh)
                    if isinstance(resp, dict):
                        self._save_cached_oauth_usage(scope_key, resp)
                    return resp
                except Exception as retry_exc:
                    if not is_unauthorized(retry_exc):
                        fallback = self._fallback_cached_oauth_usage_on_error(scope_key, retry_exc)
                        if fallback is not None:
                            bundle["_usage_from_cache"] = True
                            return fallback
                        raise
                    failed_tokens.add(fresh)
                    if network_refresh_attempted:
                        raise
            raise

    def _scan_cache(self) -> Optional[FileScanCache]:
        try:
            return FileScanCache(self._scoped_cache_path("claude_projects_scan"))
        except Exception:
            return None

    def _scan_local_projects(self, *, include_older_than_30d: bool = False) -> dict[str, Any]:
        """Scan ~/.claude/projects/**/*.jsonl for 7d and 30d token & cost metrics.

        Per-file summaries are cached by (mtime_ns, size) in ~/.cache/tokenmaster
        so unchanged session files cost a single stat(). Deduplicated per-session-day-model
        records are also persisted into ``UsageWarehouse`` (``warehouse.db``).
        """
        projects_dir = self._claude_dir() / "projects"
        stats: dict[str, Any] = {
            "found_files": 0,
            "tokens_7d": 0,
            "cost_7d_usd": 0.0,
            "tokens_30d": 0,
            "cost_30d_usd": 0.0,
        }
        if not projects_dir.exists() or not os.access(projects_dir, os.R_OK):
            return stats

        now = time.time()
        cutoff_7d = now - 7 * 86400
        cutoff_30d = now - 30 * 86400

        entries: list[tuple[float, int, int, Path]] = []
        try:
            for fpath in projects_dir.rglob("*.jsonl"):
                try:
                    st = fpath.stat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if not include_older_than_30d and st.st_mtime < cutoff_30d:
                    continue
                entries.append((st.st_mtime, st.st_mtime_ns, st.st_size, fpath))
        except Exception:
            return stats

        entries.sort(key=lambda item: str(item[3]))
        cache = self._scan_cache()
        keep: set[str] = set()
        seen_ids: set[str] = set()
        ledger_rows: list[SessionLedgerRow] = []
        acct_id = self.account or "default"

        for mtime, mtime_ns, size, fpath in entries:
            stats["found_files"] += 1
            if mtime >= cutoff_30d:
                keep.add(str(fpath))

            summary = cache.get(fpath, mtime_ns, size) if cache is not None else None
            if summary is None or summary.get("schema") != CLAUDE_SCAN_SCHEMA:
                try:
                    summary = summarize_claude_jsonl(fpath, mtime)
                except Exception:
                    continue
                if cache is not None and mtime >= cutoff_30d:
                    raw_events = summary.get("events")
                    to_cache = (
                        {**summary, "events": None}
                        if isinstance(raw_events, list) and len(raw_events) > MAX_CACHED_EVENTS_PER_FILE
                        else summary
                    )
                    cache.put(fpath, mtime_ns, size, to_cache)

            min_ts = summary.get("min_ts")
            max_ts = summary.get("max_ts")
            tok = int(summary.get("tokens") or 0)
            cost = float(summary.get("cost_usd") or 0.0)
            if min_ts is None or max_ts is None or tok <= 0:
                continue

            try:
                rel_p = fpath.relative_to(projects_dir)
                base_sid = str(rel_p.with_suffix("")).replace("\\", "/")
                raw_proj = rel_p.parts[0] if len(rel_p.parts) > 1 else "claude-code"
            except ValueError:
                base_sid = fpath.stem
                raw_proj = fpath.parent.name or "claude-code"
            proj_label = sanitize_project_name(raw_proj)

            events = summary.get("events")
            dedup_keys = summary.get("dedup_keys")
            if not isinstance(events, list) and isinstance(dedup_keys, list):
                if any(dk in seen_ids for dk in dedup_keys):
                    try:
                        events = summarize_claude_jsonl(fpath, mtime).get("events")
                    except Exception:
                        continue

            if isinstance(events, list):
                file_buckets: dict[tuple[str, str], list[Any]] = {}
                for ev in events:
                    if not isinstance(ev, (list, tuple)) or len(ev) < 3:
                        continue
                    ev_ts, ev_tok, ev_cost = float(ev[0]), int(ev[1]), float(ev[2])
                    dedup_key = str(ev[3]) if len(ev) >= 4 and ev[3] else ""
                    if dedup_key:
                        if dedup_key in seen_ids:
                            continue
                        seen_ids.add(dedup_key)
                    if ev_ts >= cutoff_30d:
                        stats["tokens_30d"] += ev_tok
                        stats["cost_30d_usd"] += ev_cost
                        if ev_ts >= cutoff_7d:
                            stats["tokens_7d"] += ev_tok
                            stats["cost_7d_usd"] += ev_cost

                    if len(ev) >= 9:
                        in_t, out_t, cr_t, cw_t, mdl_s = (
                            int(ev[4] or 0),
                            int(ev[5] or 0),
                            int(ev[6] or 0),
                            int(ev[7] or 0),
                            str(ev[8] or "claude-sonnet-4"),
                        )
                    else:
                        in_t, out_t, cr_t, cw_t, mdl_s = ev_tok, 0, 0, 0, "claude-sonnet-4"
                    day_s = normalize_day_utc(ev_ts)
                    fb = file_buckets.get((day_s, mdl_s))
                    if fb is None:
                        file_buckets[(day_s, mdl_s)] = [
                            ev_ts,
                            ev_ts,
                            in_t,
                            out_t,
                            cr_t,
                            cw_t,
                            ev_tok,
                            ev_cost,
                        ]
                    else:
                        fb[0] = min(float(fb[0]), ev_ts)
                        fb[1] = max(float(fb[1]), ev_ts)
                        fb[2] += in_t
                        fb[3] += out_t
                        fb[4] += cr_t
                        fb[5] += cw_t
                        fb[6] += ev_tok
                        fb[7] += ev_cost

                for (day_s, mdl_s), fb in file_buckets.items():
                    ledger_rows.append(
                        SessionLedgerRow(
                            provider_id="claude",
                            account_id=acct_id,
                            session_id=f"{base_sid}#{day_s}#{mdl_s}",
                            day_utc=day_s,
                            first_ts=float(fb[0]),
                            last_ts=float(fb[1]),
                            project=proj_label,
                            model=mdl_s,
                            input_tokens=int(fb[2]),
                            output_tokens=int(fb[3]),
                            cache_read_tokens=int(fb[4]),
                            cache_write_tokens=int(fb[5]),
                            total_tokens=int(fb[6]),
                            estimated_usd=float(fb[7]),
                        )
                    )
                continue

            cached_buckets = summary.get("buckets")
            if isinstance(cached_buckets, list):
                for cb in cached_buckets:
                    if isinstance(cb, (list, tuple)) and len(cb) >= 10:
                        day_s = str(cb[0])
                        mdl_s = str(cb[1] or "claude-sonnet-4")
                        ledger_rows.append(
                            SessionLedgerRow(
                                provider_id="claude",
                                account_id=acct_id,
                                session_id=f"{base_sid}#{day_s}#{mdl_s}",
                                day_utc=day_s,
                                first_ts=float(cb[2]),
                                last_ts=float(cb[3]),
                                project=proj_label,
                                model=mdl_s,
                                input_tokens=int(cb[4]),
                                output_tokens=int(cb[5]),
                                cache_read_tokens=int(cb[6]),
                                cache_write_tokens=int(cb[7]),
                                total_tokens=int(cb[8]),
                                estimated_usd=float(cb[9]),
                            )
                        )

            if max_ts < cutoff_30d:
                if isinstance(dedup_keys, list):
                    seen_ids.update(str(dk) for dk in dedup_keys if dk)
                continue

            # Cached >MAX_CACHED_EVENTS_PER_FILE file where events was omitted:
            # use aggregate totals when all turns fall in the same window, else stream once.
            if min_ts >= cutoff_7d:
                stats["tokens_30d"] += tok
                stats["cost_30d_usd"] += cost
                stats["tokens_7d"] += tok
                stats["cost_7d_usd"] += cost
                if isinstance(dedup_keys, list):
                    seen_ids.update(str(dk) for dk in dedup_keys if dk)
                continue
            if min_ts >= cutoff_30d and max_ts < cutoff_7d:
                stats["tokens_30d"] += tok
                stats["cost_30d_usd"] += cost
                if isinstance(dedup_keys, list):
                    seen_ids.update(str(dk) for dk in dedup_keys if dk)
                continue

            try:
                self._accumulate_straddling_file(
                    fpath, mtime, cutoff_7d, cutoff_30d, stats, seen_ids
                )
            except Exception:
                continue

        if cache is not None:
            cache.prune(keep, root=projects_dir)
            with critical_section():
                cache.save()

        if ledger_rows:
            UsageWarehouse().upsert_sessions(ledger_rows)

        return stats

    @staticmethod
    def _accumulate_straddling_file(
        fpath: Path,
        default_ts: float,
        cutoff_7d: float,
        cutoff_30d: float,
        stats: dict[str, Any],
        seen_ids: Optional[set[str]] = None,
    ) -> None:
        if seen_ids is None:
            seen_ids = set()
        with fpath.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                msg_id = msg.get("id") or ""
                req_id = obj.get("requestId") or ""
                dedup_key = f"{msg_id}:{req_id}"
                if msg_id and dedup_key in seen_ids:
                    continue
                if msg_id:
                    seen_ids.add(dedup_key)
                entry_ts = default_ts
                ts_str = obj.get("timestamp")
                if isinstance(ts_str, str):
                    try:
                        raw_ts = ts_str[:-1] + "+00:00" if ts_str.endswith("Z") else ts_str
                        entry_ts = datetime.fromisoformat(raw_ts).timestamp()
                    except ValueError:
                        pass
                if entry_ts < cutoff_30d:
                    continue
                in_tok = int(usage.get("input_tokens") or 0)
                cache_w = int(usage.get("cache_creation_input_tokens") or 0)
                cache_r = int(usage.get("cache_read_input_tokens") or 0)
                out_tok = int(usage.get("output_tokens") or 0)
                tot_tok = in_tok + cache_w + cache_r + out_tok
                model_name = str(msg.get("model") or "claude-sonnet-4")
                cost = estimate_token_cost_usd(
                    model_name,
                    input_tokens=in_tok,
                    cached_input_tokens=cache_r,
                    cache_write_tokens=cache_w,
                    output_tokens=out_tok,
                )
                stats["tokens_30d"] += tot_tok
                stats["cost_30d_usd"] += cost
                if entry_ts >= cutoff_7d:
                    stats["tokens_7d"] += tot_tok
                    stats["cost_7d_usd"] += cost

    @staticmethod
    def _extract_pct_and_reset(win: Any) -> tuple[Optional[float], Optional[str], Optional[str]]:
        if not isinstance(win, dict):
            return None, None, None
        raw_pct = (
            win.get("utilization")
            if win.get("utilization") is not None
            else (win.get("percent") if win.get("percent") is not None else win.get("used_percent"))
        )
        r_at, r_in = format_reset_countdown(win.get("resets_at") or win.get("reset_at"))
        if raw_pct is None:
            # If resets_at is present or utilization key exists with None, treat as 0.0%
            if "utilization" in win or "percent" in win:
                return 0.0, r_at, r_in
            return None, None, None
        try:
            pct = float(raw_pct)
        except (ValueError, TypeError):
            return None, None, None
        return pct, r_at, r_in

    def fetch(self) -> ProviderReport:
        report = self.new_report()

        self._last_oauth_bundle = None
        oauth_token, sub_type, tier = self._load_oauth_credentials()
        bundle: dict[str, Any] = (
            dict(self._last_oauth_bundle)
            if isinstance(getattr(self, "_last_oauth_bundle", None), dict)
            else {
                "access_token": oauth_token,
                "refresh_token": None,
                "expires_at_ms": None,
                "scopes": None,
                "client_id": None,
                "sub_type": sub_type,
                "tier": tier,
                "source_file": None,
                "source_keychain_service": None,
            }
        )
        has_oauth = bool(bundle.get("access_token") or bundle.get("refresh_token"))
        api_key = self.cfg.get("api_key")
        sources: list[str] = []

        # Overlap the local JSONL scan (CPU/disk) with the OAuth usage + refresh request (network).
        tasks: dict[str, Any] = {"local": self._scan_local_projects}
        if has_oauth:
            tasks["usage"] = lambda: self._fetch_oauth_usage_with_refresh(bundle)
        results = run_concurrently(tasks)
        local_stats, local_exc = results["local"]
        if not isinstance(local_stats, dict):
            local_stats = {"found_files": 0, "tokens_7d": 0, "cost_7d_usd": 0.0, "tokens_30d": 0, "cost_30d_usd": 0.0}
        if local_exc is not None:
            report.error_message = f"Local ~/.claude scan failed: {local_exc}"

        sub_type = bundle.get("sub_type") or sub_type
        tier = bundle.get("tier") or tier
        if sub_type:
            tier_suffix = ""
            if tier and "20x" in tier:
                tier_suffix = " 20x"
            elif tier and "5x" in tier:
                tier_suffix = " 5x"
            report.plan_tier = f"Claude {sub_type.capitalize()}{tier_suffix}"

        if has_oauth:
            try:
                data, usage_exc = results["usage"]
                if usage_exc is not None:
                    raise usage_exc
                if not isinstance(data, dict):
                    raise ValueError("Unexpected OAuth usage response")
                sources.append("OAuth Usage API")
                report.status = "ok"

                weekly_all_pct: Optional[float] = None
                weekly_all_reset_at: Optional[str] = None
                weekly_all_reset_in: Optional[str] = None

                fable_pct: Optional[float] = None
                fable_reset_in: Optional[str] = None
                scoped_label: str = "Fable"

                session_5h_pct: Optional[float] = None
                session_5h_reset_in: Optional[str] = None

                other_scoped: list[tuple[str, float, Optional[str]]] = []

                # 1. Parse `limits[]` array if present
                limits_arr = data.get("limits")
                if isinstance(limits_arr, list):
                    for item in limits_arr:
                        if not isinstance(item, dict):
                            continue
                        pct, r_at, r_in = self._extract_pct_and_reset(item)
                        if pct is None:
                            continue
                        kind = str(item.get("kind") or "")
                        group = str(item.get("group") or "")
                        scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
                        model_obj = scope.get("model") if isinstance(scope.get("model"), dict) else {}
                        model_name = str(
                            model_obj.get("display_name")
                            or model_obj.get("id")
                            or scope.get("name")
                            or ""
                        )

                        if kind == "weekly_all" or (group == "weekly" and not model_name and kind != "weekly_scoped"):
                            weekly_all_pct = pct
                            weekly_all_reset_at = r_at
                            weekly_all_reset_in = r_in
                        elif kind == "weekly_scoped" or (group == "weekly" and model_name):
                            if "fable" in model_name.lower() or fable_pct is None:
                                fable_pct = pct
                                fable_reset_in = r_in
                                if model_name:
                                    scoped_label = model_name
                            else:
                                other_scoped.append((f"7d {model_name} Pool", pct, r_in))
                        elif kind == "session" or group == "session":
                            session_5h_pct = pct
                            session_5h_reset_in = r_in

                # 2. Parse top-level keys (`seven_day`, `seven_day_fable`, `five_hour`, etc.)
                if weekly_all_pct is None and "seven_day" in data:
                    weekly_all_pct, weekly_all_reset_at, weekly_all_reset_in = self._extract_pct_and_reset(
                        data.get("seven_day")
                    )

                if session_5h_pct is None and "five_hour" in data:
                    if data.get("five_hour") is None:
                        session_5h_pct = 0.0
                    else:
                        session_5h_pct, _, session_5h_reset_in = self._extract_pct_and_reset(
                            data.get("five_hour")
                        )

                # Scan all top-level keys for Fable or other 7-day model pools
                for k, v in data.items():
                    if k in ("seven_day", "five_hour", "extra_usage", "limits"):
                        continue
                    k_lower = k.lower()
                    if v is None:
                        if "fable" in k_lower and fable_pct is None:
                            fable_pct = 0.0
                        continue
                    if not isinstance(v, dict):
                        continue
                    pct, _, r_in = self._extract_pct_and_reset(v)
                    if pct is None:
                        continue
                    if "fable" in k_lower:
                        fable_pct = pct
                        fable_reset_in = r_in
                        scoped_label = "Fable"
                    elif k_lower.startswith("seven_day_"):
                        pool_name = k[len("seven_day_"):].replace("_", " ").title()
                        other_scoped.append((f"7d {pool_name} Pool", pct, r_in))
                    else:
                        pool_name = k.replace("_", " ").title()
                        other_scoped.append((f"{pool_name} Pool", pct, r_in))

                # Populate main Weekly Usage & Remaining (including both Total & Fable)
                if weekly_all_pct is not None:
                    report.weekly = UsageWindow(
                        used=round(weekly_all_pct, 1),
                        remaining=round(max(0.0, 100.0 - weekly_all_pct), 1),
                        limit=100.0,
                        unit="%",
                        used_percent=weekly_all_pct,
                        remaining_percent=max(0.0, 100.0 - weekly_all_pct),
                        resets_at=weekly_all_reset_at,
                        resets_in=weekly_all_reset_in,
                        scoped_label=scoped_label if fable_pct is not None else None,
                        scoped_used_percent=fable_pct,
                        scoped_remaining_percent=max(0.0, 100.0 - fable_pct) if fable_pct is not None else None,
                        window_seconds=7 * 86400,
                    )
                    report.sub_metrics.append(
                        SubMetric(
                            name="7d Weekly (Total All)",
                            used_str=f"{weekly_all_pct:.1f}%",
                            remaining_str=f"{max(0.0, 100.0 - weekly_all_pct):.1f}%",
                            used_percent=weekly_all_pct,
                            resets_in=weekly_all_reset_in,
                        )
                    )

                if fable_pct is not None:
                    report.sub_metrics.append(
                        SubMetric(
                            name=f"7d Weekly ({scoped_label})",
                            used_str=f"{fable_pct:.1f}%",
                            remaining_str=f"{max(0.0, 100.0 - fable_pct):.1f}%",
                            used_percent=fable_pct,
                            resets_in=fable_reset_in or weekly_all_reset_in,
                        )
                    )

                if session_5h_pct is not None:
                    report.sub_metrics.append(
                        SubMetric(
                            name="5h Session Window",
                            used_str=f"{session_5h_pct:.1f}%",
                            remaining_str=f"{max(0.0, 100.0 - session_5h_pct):.1f}%",
                            used_percent=session_5h_pct,
                            resets_in=session_5h_reset_in,
                        )
                    )

                for label, s_pct, s_rin in other_scoped:
                    report.sub_metrics.append(
                        SubMetric(
                            name=label,
                            used_str=f"{s_pct:.1f}%",
                            remaining_str=f"{max(0.0, 100.0 - s_pct):.1f}%",
                            used_percent=s_pct,
                            resets_in=s_rin,
                        )
                    )

                # Extra usage credits
                extra = data.get("extra_usage")
                if isinstance(extra, dict):
                    if extra.get("is_enabled"):
                        raw_used = extra.get("used_credits")
                        raw_limit = extra.get("monthly_limit")
                        util = extra.get("utilization")
                        used_usd: Optional[float] = None
                        limit_usd: Optional[float] = None

                        if raw_limit is not None:
                            lim_f = float(raw_limit)
                            scale = 100.0 if lim_f >= 500 else 1.0
                            limit_usd = round(lim_f / scale, 2)
                            if raw_used is not None:
                                used_usd = round(float(raw_used) / scale, 2)
                        elif raw_used is not None:
                            used_usd = round(float(raw_used) / 100.0, 2)

                        report.credits = CreditBalance(
                            used=used_usd if used_usd is not None else 0.0,
                            limit=limit_usd,
                            unit="USD",
                            used_percent=float(util) if util is not None else None,
                            note="Extra Usage",
                        )
                    else:
                        report.credits.note = "Extra Usage: Off"
            except Exception as exc:
                report.error_message = str(exc)

        if local_stats["found_files"] > 0:
            sources.append("~/.claude/projects")
            tok_7d_m = local_stats["tokens_7d"] / 1_000_000.0
            cost_7d = local_stats["cost_7d_usd"]
            cost_30d = local_stats["cost_30d_usd"]

            report.sub_metrics.append(
                SubMetric(
                    name="7d Local Code Tokens",
                    used_str=f"{tok_7d_m:.2f}M tok (~${cost_7d:.2f} API eq.)",
                    remaining_str="—",
                )
            )

            if report.weekly.used is None:
                report.weekly = UsageWindow(
                    used=round(cost_7d, 2),
                    unit="USD",
                    note=f"{tok_7d_m:.2f}M tok (7d)",
                )
                report.status = "partial"

            if report.credits.used is None and not report.credits.note and cost_30d > 0:
                report.credits.note = f"~${cost_30d:.2f} (30d API eq.)"
                if report.status == "unconfigured":
                    report.status = "partial"

        if sources:
            report.source = " + ".join(sources)

        if report.status == "unconfigured":
            if api_key:
                report.status = "partial"
                report.plan_tier = "Anthropic API Key"
                report.source = "ANTHROPIC_API_KEY"
                q_target = f"claude:{self.account}" if self.account else "claude"
                report.auth_hint = (
                    f"Set credit cap via `tokenmaster quota {q_target} --credit-limit <USD>`"
                )
            elif self.account:
                report.auth_hint = (
                    f"Run `tokenmaster login claude {self.account}` or `tokenmaster auth claude --account {self.account}`"
                )
            else:
                report.auth_hint = (
                    "Login via `claude` CLI or run `tokenmaster auth claude`"
                )

        return self.apply_config_overrides(report)
