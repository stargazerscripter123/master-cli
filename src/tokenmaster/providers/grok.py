"""Grok provider (~/.grok/auth.json Grok CLI proxy billing + OIDC refresh + ~/.grok/sessions + grok.com)."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from tokenmaster.cache import FileScanCache
from tokenmaster.config import get_cache_dir
from tokenmaster.models import (
    CreditBalance,
    ProviderReport,
    SubMetric,
    UsageWindow,
    format_reset_countdown,
    parse_reset_remaining_seconds,
)
from tokenmaster.providers.base import BaseProvider, atomic_write_text, critical_section, run_concurrently
from tokenmaster.warehouse import SessionLedgerRow, UsageWarehouse, normalize_day_utc

# Per-auth.json locks: OIDC refresh tokens rotate per subscription, so refreshes on the
# same auth.json are serialized without blocking independent Grok accounts.
_GROK_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_GROK_REFRESH_LOCKS_GUARD = threading.Lock()


def _grok_refresh_lock(auth_file: Path) -> threading.Lock:
    key = os.path.abspath(str(auth_file))
    with _GROK_REFRESH_LOCKS_GUARD:
        lock = _GROK_REFRESH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _GROK_REFRESH_LOCKS[key] = lock
        return lock


def _safe_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, (list, dict, bool)):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_money_usd(val: Any) -> Optional[float]:
    """Parse either a USD float or an xAI CentAmount dict {'val': cents} into USD."""
    if isinstance(val, dict) and "val" in val:
        cents = _safe_float(val.get("val"))
        return (cents / 100.0) if cents is not None else None
    return _safe_float(val)


class GrokProvider(BaseProvider):
    """Monitors Grok via Grok CLI (~/.grok/auth.json & ~/.grok/sessions), SuperGrok Cookie, or xAI API."""

    provider_id = "grok"
    display_name = "Grok (CLI / SuperGrok)"

    def _grok_home(self) -> Path:
        isolated = self._resolve_isolated_dir("grok_home")
        if isolated is not None:
            return isolated
        return self._resolve_default_env_dir("GROK_HOME", Path.home() / ".grok")

    def _load_grok_cli_auth_entries(self) -> list[tuple[str, dict[str, Any]]]:
        """Return list of (scope_key, entry_dict) from ~/.grok/auth.json."""
        auth_file = self._grok_home() / "auth.json"
        if not auth_file.exists() or not os.access(auth_file, os.R_OK):
            return []
        try:
            raw = json.loads(auth_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return []
            entries: list[tuple[str, dict[str, Any]]] = []
            # Prioritize accounts.x.ai and auth.x.ai
            for priority_key in ("https://accounts.x.ai/sign-in", "https://auth.x.ai"):
                if isinstance(raw.get(priority_key), dict):
                    entries.append((priority_key, raw[priority_key]))
            for k, v in raw.items():
                if isinstance(v, dict) and k not in ("https://accounts.x.ai/sign-in", "https://auth.x.ai"):
                    entries.append((k, v))
            if not entries and (raw.get("key") or raw.get("access_token")):
                entries.append(("root", raw))
            return entries
        except Exception:
            return []

    def _load_grok_cli_token(self) -> Optional[str]:
        """Extract session token from ~/.grok/auth.json (populated by `grok login`)."""
        for _, entry in self._load_grok_cli_auth_entries():
            tok = entry.get("key") or entry.get("access_token") or entry.get("token")
            if tok and str(tok).strip():
                return str(tok).strip()
        return None

    def _refresh_grok_oidc_token(self, scope_key: str, entry: dict[str, Any]) -> Optional[str]:
        """Attempt silent OIDC refresh using refresh_token in ~/.grok/auth.json.

        Serialized process-wide and marked critical: refresh tokens rotate, so two
        concurrent refreshes (e.g. overlapping watch cycles) could trip reuse
        detection, and abandoning the thread between the token POST and the
        persist would lose the rotated token and log the user out of the grok CLI.
        """
        auth_file = self._grok_home() / "auth.json"
        with _grok_refresh_lock(auth_file), critical_section():
            # Another thread (or the grok CLI itself) may have rotated the token
            # while we waited for the lock: adopt it instead of reusing a stale one.
            for disk_scope, disk_entry in self._load_grok_cli_auth_entries():
                if disk_scope != scope_key:
                    continue
                disk_rt = disk_entry.get("refresh_token")
                disk_key = disk_entry.get("key") or disk_entry.get("access_token")
                entry_key = entry.get("key") or entry.get("access_token")
                if disk_key and (
                    (disk_rt and disk_rt != entry.get("refresh_token"))
                    or (entry_key and disk_key != entry_key)
                ):
                    return str(disk_key).strip()
            return self._refresh_grok_oidc_token_locked(scope_key, entry)

    def _refresh_grok_oidc_token_locked(self, scope_key: str, entry: dict[str, Any]) -> Optional[str]:
        refresh_token = entry.get("refresh_token")
        if not refresh_token or not str(refresh_token).strip():
            return None
        issuer = str(
            entry.get("oidc_issuer")
            or os.environ.get("GROK_OIDC_ISSUER")
            or "https://auth.x.ai"
        ).rstrip("/")
        client_id = str(
            entry.get("oidc_client_id")
            or os.environ.get("GROK_OIDC_CLIENT_ID")
            or ""
        ).strip()

        token_endpoints: list[str] = []
        try:
            disc = self.http_json(f"{issuer}/.well-known/openid-configuration")
            if isinstance(disc, dict) and disc.get("token_endpoint"):
                token_endpoints.append(str(disc["token_endpoint"]))
        except Exception:
            pass
        for fallback_ep in (f"{issuer}/oauth2/token", f"{issuer}/oauth/token"):
            if fallback_ep not in token_endpoints:
                token_endpoints.append(fallback_ep)

        form_data: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": str(refresh_token).strip(),
        }
        if client_id:
            form_data["client_id"] = client_id
        encoded = urllib.parse.urlencode(form_data).encode("utf-8")

        for ep in token_endpoints:
            req = urllib.request.Request(
                ep,
                data=encoded,
                method="POST",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "User-Agent": "xai-grok-cli/1.0.70",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8", errors="replace"))
                if isinstance(body, dict) and body.get("access_token"):
                    new_tok = str(body["access_token"]).strip()
                    self._persist_refreshed_grok_token(
                        scope_key,
                        new_tok,
                        body.get("refresh_token"),
                        body.get("expires_in"),
                    )
                    return new_tok
            except Exception:
                continue
        return None

    def _persist_refreshed_grok_token(
        self,
        scope_key: str,
        access_token: str,
        refresh_token: Optional[str],
        expires_in: Optional[Any],
    ) -> None:
        auth_file = self._grok_home() / "auth.json"
        if not auth_file.exists():
            return
        try:
            raw = json.loads(auth_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                target = raw.get(scope_key) if scope_key != "root" else raw
                if isinstance(target, dict):
                    target["key"] = access_token
                    if refresh_token:
                        target["refresh_token"] = str(refresh_token)
                    if expires_in is not None:
                        try:
                            exp_ts = time.time() + float(expires_in)
                            # Preserve the grok CLI's own format (RFC 3339 string);
                            # writing an int where it expects a string could make the
                            # CLI reject auth.json.
                            if isinstance(target.get("expires_at"), (int, float)):
                                target["expires_at"] = int(exp_ts)
                            else:
                                target["expires_at"] = (
                                    datetime.fromtimestamp(exp_ts, tz=timezone.utc)
                                    .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                                )
                        except Exception:
                            pass
                    atomic_write_text(auth_file, json.dumps(raw, indent=2))
        except Exception:
            pass

    def _sessions_scan_cache(self) -> Optional[FileScanCache]:
        try:
            return FileScanCache(self._scoped_cache_path("grok_sessions_scan"))
        except Exception:
            return None

    def _scan_local_grok_sessions(self) -> dict[str, Any]:
        """Scan ~/.grok/sessions for 7d and 30d session activity, tokens, and costUsdTicks."""
        sessions_dir = self._grok_home() / "sessions"
        stats: dict[str, Any] = {
            "found": False,
            "sessions_7d": 0,
            "tokens_7d": 0,
            "cost_7d_usd": 0.0,
            "sessions_total": 0,
            "tokens_total": 0,
            "cost_total_usd": 0.0,
        }
        if not sessions_dir.exists() or not os.access(sessions_dir, os.R_OK):
            return stats

        stats["found"] = True
        now = time.time()
        cutoff_7d = now - 7 * 86400

        try:
            summary_files = list(sessions_dir.rglob("summary.json"))
        except Exception:
            return stats

        cache = self._sessions_scan_cache()
        keep: set[str] = set()
        ledger_rows: list[SessionLedgerRow] = []
        acct_id = self.account or "default"

        for sfile in summary_files:
            try:
                st = sfile.stat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            mtime = st.st_mtime
            keep.add(str(sfile))
            stats["sessions_total"] += 1

            summary = cache.get(sfile, st.st_mtime_ns, st.st_size) if cache is not None else None
            if summary is None:
                try:
                    raw_text = sfile.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                try:
                    raw = json.loads(raw_text)
                except Exception:
                    raw = None
                if not isinstance(raw, dict):
                    summary = {"tokens": 0, "cost_usd": 0.0, "valid": False}
                    if cache is not None:
                        cache.put(sfile, st.st_mtime_ns, st.st_size, summary)
                    continue
                tot_tok = _safe_float(raw.get("totalTokens")) or 0.0
                ticks = _safe_float(raw.get("costUsdTicks")) or _safe_float(raw.get("total_cost_usd_ticks"))
                cost_usd = (
                    (ticks / 1e10)
                    if ticks is not None
                    else (_safe_float(raw.get("total_cost_usd")) or 0.0)
                )
                mdl_raw = raw.get("model") or raw.get("modelName")
                mdl_name = str(mdl_raw).strip() if isinstance(mdl_raw, str) and mdl_raw.strip() else "grok-cli"
                summary = {
                    "tokens": int(tot_tok),
                    "cost_usd": float(cost_usd),
                    "model": mdl_name,
                    "valid": True,
                }
                if cache is not None:
                    cache.put(sfile, st.st_mtime_ns, st.st_size, summary)
            elif summary.get("valid") is False:
                continue

            tok_i = int(summary.get("tokens") or 0)
            cost_f = float(summary.get("cost_usd") or 0.0)
            mdl_s = str(summary.get("model") or "grok-cli")
            stats["tokens_total"] += tok_i
            stats["cost_total_usd"] += cost_f
            if mtime >= cutoff_7d:
                stats["sessions_7d"] += 1
                stats["tokens_7d"] += tok_i
                stats["cost_7d_usd"] += cost_f

            if tok_i > 0 or cost_f > 0:
                try:
                    rel_p = sfile.parent.relative_to(sessions_dir)
                    sid = str(rel_p).replace("\\", "/")
                    if sid in ("", "."):
                        sid = sfile.stem
                except ValueError:
                    sid = sfile.parent.name or sfile.stem
                day_s = normalize_day_utc(mtime)
                ledger_rows.append(
                    SessionLedgerRow(
                        provider_id="grok",
                        account_id=acct_id,
                        session_id=sid,
                        day_utc=day_s,
                        first_ts=mtime,
                        last_ts=mtime,
                        project="grok-cli",
                        model=mdl_s,
                        input_tokens=tok_i,
                        output_tokens=0,
                        cache_read_tokens=0,
                        cache_write_tokens=0,
                        total_tokens=tok_i,
                        estimated_usd=cost_f,
                    )
                )

        if cache is not None:
            cache.prune(keep, root=sessions_dir)
            with critical_section():
                cache.save()

        if ledger_rows:
            UsageWarehouse().upsert_sessions(ledger_rows)

        return stats

    def _query_grok_cli_proxy(self, cli_token: str) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]], Optional[str]]:
        """Query cli-chat-proxy.grok.com/v1/billing?format=credits and /v1/user?include=subscription."""
        base_url = os.environ.get("GROK_CLI_CHAT_PROXY_BASE_URL", "https://cli-chat-proxy.grok.com/v1").rstrip("/")
        headers = {
            "Authorization": f"Bearer {cli_token}",
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-grok-client-version": "1.0.70",
            "x-grok-client-mode": "xai-grok-cli",
            "x-grok-client-identifier": "grok-shell",
            "Accept": "application/json",
        }
        billing_data: Optional[dict[str, Any]] = None
        user_data: Optional[dict[str, Any]] = None
        err_msg: Optional[str] = None

        # Independent endpoints: fetch concurrently (wall time = max, not sum).
        results = run_concurrently(
            {
                "billing": lambda: self.http_json(f"{base_url}/billing?format=credits", headers=headers),
                "user": lambda: self.http_json(f"{base_url}/user?include=subscription", headers=headers),
            }
        )
        billing_res, billing_exc = results["billing"]
        user_res, user_exc = results["user"]
        if billing_exc is None:
            billing_data = billing_res
        else:
            err_msg = str(billing_exc)
        if user_exc is None:
            user_data = user_res
        elif not err_msg:
            err_msg = str(user_exc)

        return billing_data, user_data, err_msg

    def _build_cookie_header(self) -> Optional[str]:
        raw_cookie = self.cfg.get("cookie")
        sso_token = self.cfg.get("sso_token")

        if raw_cookie:
            raw_cookie = str(raw_cookie).strip()
            if "=" in raw_cookie:
                return raw_cookie
            return f"sso={raw_cookie}; sso-rw={raw_cookie}"

        if sso_token:
            sso_token = str(sso_token).strip()
            return f"sso={sso_token}; sso-rw={sso_token}"

        return None

    def _query_rate_limit(
        self, cookie: str, model_name: str = "grok-3", request_kind: str = "DEFAULT"
    ) -> Optional[dict[str, Any]]:
        headers = {
            "Cookie": cookie,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://grok.com",
            "Referer": "https://grok.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
            ),
        }
        payload = {
            "requestKind": request_kind,
            "modelName": model_name,
        }
        return self.http_json(
            "https://grok.com/rest/rate-limits",
            method="POST",
            headers=headers,
            payload=payload,
        )

    def fetch(self) -> ProviderReport:
        report = self.new_report()

        auth_entries = self._load_grok_cli_auth_entries()
        cli_token = self._load_grok_cli_token()
        local_sessions = self._scan_local_grok_sessions()
        cookie = self._build_cookie_header()
        api_key = self.cfg.get("api_key")
        sources: list[str] = []

        # Extract any cached tier from ~/.grok/auth.json
        for _, entry in auth_entries:
            cached_tier = entry.get("subscription_tier") or entry.get("subscriptionTier")
            if cached_tier:
                report.plan_tier = str(cached_tier)
                break

        # 1. Primary Method: Grok CLI (~/.grok/auth.json -> cli-chat-proxy.grok.com)
        if cli_token:
            billing_data, user_data, cli_err = self._query_grok_cli_proxy(cli_token)

            # If 401 Unauthorized, try silent OIDC refresh from ~/.grok/auth.json
            if not billing_data and not user_data and cli_err and "401" in cli_err:
                for scope_key, entry in auth_entries:
                    refreshed = self._refresh_grok_oidc_token(scope_key, entry)
                    if refreshed:
                        billing_data, user_data, cli_err = self._query_grok_cli_proxy(refreshed)
                        if billing_data or user_data:
                            break

            if cli_err and not billing_data and not user_data:
                if "401" in cli_err:
                    report.error_message = (
                        "Grok CLI token in ~/.grok/auth.json expired (7d TTL); run `grok login` to refresh"
                    )
                else:
                    report.error_message = cli_err

            if isinstance(user_data, dict) and user_data:
                sources.append("Grok CLI (user)")
                sub_tier = (
                    user_data.get("subscription_tier")
                    or user_data.get("subscriptionTier")
                    or user_data.get("tier")
                )
                if sub_tier:
                    report.plan_tier = str(sub_tier)
                if report.status == "unconfigured":
                    report.status = "partial"

            if isinstance(billing_data, dict) and billing_data:
                sources.append("Grok CLI (/v1/billing)")
                report.status = "ok"

                sub_tier = (
                    billing_data.get("subscription_tier")
                    or billing_data.get("subscriptionTier")
                )
                if sub_tier:
                    report.plan_tier = str(sub_tier)
                elif not report.plan_tier:
                    report.plan_tier = "Grok CLI"

                bcfg = (
                    billing_data.get("config")
                    if isinstance(billing_data.get("config"), dict)
                    else billing_data
                )

                usage_pct = _safe_float(bcfg.get("creditUsagePercent"))
                curr_period = (
                    bcfg.get("currentPeriod")
                    if isinstance(bcfg.get("currentPeriod"), dict)
                    else {}
                )
                resets_at, resets_in = format_reset_countdown(
                    curr_period.get("end") or bcfg.get("billingPeriodEnd")
                )

                monthly_limit = _safe_money_usd(bcfg.get("monthlyLimit"))
                on_demand_cap = _safe_money_usd(bcfg.get("onDemandCap"))
                on_demand_used = _safe_money_usd(bcfg.get("onDemandUsed"))
                prepaid_bal = _safe_money_usd(bcfg.get("prepaidBalance"))

                hist = bcfg.get("history") if isinstance(bcfg.get("history"), list) else []
                latest_hist = hist[0] if (hist and isinstance(hist[0], dict)) else {}
                inc_used = _safe_money_usd(latest_hist.get("includedUsed"))
                tot_used = _safe_money_usd(latest_hist.get("totalUsed"))

                if usage_pct is not None:
                    if 0.0 < usage_pct <= 1.0 and inc_used is not None and monthly_limit and (inc_used / monthly_limit) > 0.01:
                        usage_pct = usage_pct * 100.0
                    rem_pct = max(0.0, 100.0 - usage_pct)
                    gk_win_sec: Optional[int] = None
                    p_start = curr_period.get("start")
                    p_end = curr_period.get("end") or bcfg.get("billingPeriodEnd")
                    if isinstance(p_start, str) and isinstance(p_end, str):
                        try:
                            s_raw = p_start[:-1] + "+00:00" if p_start.endswith("Z") else p_start
                            e_raw = p_end[:-1] + "+00:00" if p_end.endswith("Z") else p_end
                            s_dt = datetime.fromisoformat(s_raw)
                            e_dt = datetime.fromisoformat(e_raw)
                            if s_dt.tzinfo is None:
                                s_dt = s_dt.replace(tzinfo=timezone.utc)
                            if e_dt.tzinfo is None:
                                e_dt = e_dt.replace(tzinfo=timezone.utc)
                            if e_dt > s_dt:
                                gk_win_sec = max(1, int((e_dt - s_dt).total_seconds()))
                        except (ValueError, TypeError):
                            pass
                    if gk_win_sec is None:
                        gk_rem_sec = parse_reset_remaining_seconds(resets_at, resets_in)
                        if gk_rem_sec is not None and gk_rem_sec <= 86400:
                            gk_win_sec = 86400
                        elif monthly_limit is not None or bcfg.get("billingPeriodEnd") is not None:
                            gk_win_sec = 30 * 86400
                    report.weekly = UsageWindow(
                        used=round(usage_pct, 1),
                        remaining=round(rem_pct, 1),
                        limit=100.0,
                        unit="%",
                        used_percent=usage_pct,
                        remaining_percent=rem_pct,
                        resets_at=resets_at,
                        resets_in=resets_in,
                        window_seconds=gk_win_sec,
                    )
                    report.sub_metrics.append(
                        SubMetric(
                            name="Grok CLI Weekly Quota",
                            used_str=f"{usage_pct:.1f}%",
                            remaining_str=f"{rem_pct:.1f}%",
                            used_percent=usage_pct,
                            resets_in=resets_in,
                        )
                    )

                prod_usage = bcfg.get("productUsage")
                if isinstance(prod_usage, list):
                    for pu in prod_usage:
                        if isinstance(pu, dict) and pu.get("product"):
                            p_name = str(pu["product"])
                            p_pct = _safe_float(pu.get("usagePercent"))
                            if p_pct is not None:
                                p_rem = max(0.0, 100.0 - p_pct)
                                report.sub_metrics.append(
                                    SubMetric(
                                        name=f"{p_name} Pool",
                                        used_str=f"{p_pct:.1f}%",
                                        remaining_str=f"{p_rem:.1f}%",
                                        used_percent=p_pct,
                                        resets_in=resets_in,
                                    )
                                )

                if prepaid_bal is not None or monthly_limit is not None or on_demand_used is not None:
                    c_used = tot_used if tot_used is not None else (inc_used if inc_used is not None else on_demand_used)
                    c_limit = monthly_limit if monthly_limit is not None else on_demand_cap
                    c_rem = prepaid_bal
                    if c_rem is None and c_limit is not None and c_used is not None:
                        c_rem = max(0.0, c_limit - c_used)

                    report.credits = CreditBalance(
                        used=round(c_used, 2) if c_used is not None else None,
                        remaining=round(c_rem, 2) if c_rem is not None else None,
                        limit=round(c_limit, 2) if c_limit is not None else None,
                        unit="USD",
                        note="Prepaid Credits" if (c_used is None and prepaid_bal is not None) else None,
                    )

                    if prepaid_bal is not None:
                        report.sub_metrics.append(
                            SubMetric(
                                name="Prepaid Credit Balance",
                                used_str=f"${c_used:.2f}" if c_used is not None else "—",
                                remaining_str=f"${prepaid_bal:.2f} left",
                            )
                        )
                    if on_demand_used is not None or on_demand_cap is not None:
                        od_u = on_demand_used or 0.0
                        od_rem = max(0.0, on_demand_cap - od_u) if on_demand_cap is not None else None
                        report.sub_metrics.append(
                            SubMetric(
                                name="Pay-as-you-go Spend",
                                used_str=f"${od_u:.2f}" + (f" / ${on_demand_cap:.2f}" if on_demand_cap else ""),
                                remaining_str=f"${od_rem:.2f} left" if od_rem is not None else "—",
                            )
                        )

        # 2. Secondary Method: grok.com/rest/rate-limits via Cookie if configured
        if cookie:
            primary_model = str(self.cfg.get("model_name") or "grok-3")
            pools_to_check = [
                (primary_model, "DEFAULT", f"{primary_model} (Default)"),
                ("grok-4", "DEFAULT", "grok-4 (Default)"),
                (primary_model, "REASONING", f"{primary_model} (Reasoning)"),
            ]
            seen_queries: set[tuple[str, str]] = set()

            for model_name, req_kind, label in pools_to_check:
                if (model_name, req_kind) in seen_queries:
                    continue
                seen_queries.add((model_name, req_kind))

                try:
                    data = self._query_rate_limit(cookie, model_name=model_name, request_kind=req_kind)
                except Exception as exc:
                    if not report.error_message:
                        report.error_message = str(exc)
                    continue

                if not isinstance(data, dict) or not data:
                    continue

                rem_q = data.get("remainingQueries")
                tot_q = data.get("totalQueries")
                unit = "queries"

                if rem_q is None and data.get("remainingTokens") is not None:
                    rem_q = data.get("remainingTokens")
                    tot_q = data.get("totalTokens")
                    unit = "tokens"

                if rem_q is None or tot_q is None:
                    continue

                rem_f = float(rem_q)
                tot_f = float(tot_q)
                used_f = max(0.0, tot_f - rem_f)
                used_pct = (used_f / tot_f * 100.0) if tot_f > 0 else 0.0
                rem_pct = max(0.0, 100.0 - used_pct)

                win_sec = data.get("windowSizeSeconds")
                wait_sec = data.get("waitTimeSeconds")
                reset_target = wait_sec if (wait_sec and int(wait_sec) > 0) else win_sec
                resets_at, resets_in = format_reset_countdown(reset_target)

                if report.weekly.used is None:
                    report.status = "ok"
                    report.plan_tier = report.plan_tier or ("SuperGrok" if tot_f >= 50 else "Grok Basic")
                    sources.append("grok.com/rest/rate-limits")
                    rl_win_sec = int(win_sec) if (win_sec and int(win_sec) > 0) else 86400
                    report.weekly = UsageWindow(
                        used=used_f,
                        remaining=rem_f,
                        limit=tot_f,
                        unit=unit,
                        used_percent=used_pct,
                        remaining_percent=rem_pct,
                        resets_at=resets_at,
                        resets_in=resets_in,
                        window_seconds=rl_win_sec,
                    )

                report.sub_metrics.append(
                    SubMetric(
                        name=label,
                        used_str=f"{int(used_f)} / {int(tot_f)} ({used_pct:.0f}%)",
                        remaining_str=f"{int(rem_f)} left ({rem_pct:.0f}%)",
                        used_percent=used_pct,
                        resets_in=resets_in,
                    )
                )

        # 3. Local ~/.grok/sessions stats
        if local_sessions["found"] and local_sessions["sessions_total"] > 0:
            sources.append("~/.grok/sessions")
            s_cnt = local_sessions["sessions_7d"] or local_sessions["sessions_total"]
            tok_m = (local_sessions["tokens_7d"] or local_sessions["tokens_total"]) / 1_000_000.0
            cost_val = local_sessions["cost_7d_usd"] or local_sessions["cost_total_usd"]
            win_lbl = "7d Local Grok CLI Sessions" if local_sessions["sessions_7d"] > 0 else "Local Grok CLI Sessions"
            cost_part = f", ${cost_val:.2f}" if cost_val > 0 else ""
            report.sub_metrics.append(
                SubMetric(
                    name=win_lbl,
                    used_str=f"{s_cnt} sessions ({tok_m:.2f}M tok{cost_part})",
                    remaining_str="—",
                )
            )
            if report.status == "unconfigured":
                report.status = "partial"
                report.plan_tier = report.plan_tier or "Grok CLI Local"

        # 4. Also check xAI API Key if configured
        if api_key:
            try:
                key_info = self.http_json(
                    "https://api.x.ai/v1/api-key",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if isinstance(key_info, dict):
                    sources.append("xAI API")
                    if report.status == "unconfigured":
                        report.status = "partial"
                    if not report.plan_tier:
                        report.plan_tier = "xAI API"
                    key_name = key_info.get("name") or "API Key active"
                    blocked = key_info.get("api_key_blocked") or key_info.get("team_blocked")
                    report.credits.note = "Blocked" if blocked else str(key_name)
            except Exception as exc:
                if not report.error_message:
                    report.error_message = str(exc)

        if sources:
            report.source = " + ".join(dict.fromkeys(sources))

        if report.status == "unconfigured":
            if self.account:
                report.auth_hint = (
                    f"Run `tokenmaster login grok {self.account}` or `tokenmaster auth grok --account {self.account}`"
                )
            else:
                report.auth_hint = (
                    "Run `grok login` (auto-reads ~/.grok/auth.json) or `tokenmaster auth grok`"
                )

        return self.apply_config_overrides(report)
