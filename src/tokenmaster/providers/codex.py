"""OpenAI Codex CLI provider (ChatGPT wham/usage API + ~/.codex/sessions JSONL)."""

from __future__ import annotations

import json
import os
import stat
import time
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
from tokenmaster.pricing import estimate_token_cost_usd
from tokenmaster.providers.base import BaseProvider, critical_section
from tokenmaster.warehouse import SessionLedgerRow, UsageWarehouse, normalize_day_utc

# Codex writes one cumulative ``token_count`` event per turn, so the *last* one in a
# rollout file carries the session total. Empirically it sits within a few KB of EOF.
TAIL_CHUNK_INITIAL = 64 * 1024
TAIL_CHUNK_MAX = 4 * 1024 * 1024


def _event_payload(obj: Any) -> Optional[dict[str, Any]]:
    if not isinstance(obj, dict):
        return None
    payload = obj.get("payload")
    return payload if isinstance(payload, dict) else obj


def summarize_rollout_tail(path: Path, size: Optional[int] = None) -> dict[str, Any]:
    """Extract session totals from a Codex rollout JSONL by reading only its tail.

    Returns ``{"total_token_usage", "rate_limits", "model", "bytes_read"}``.
    Reads progressively larger tails (64 KB -> 4 MB) until a ``token_count``
    event is found or the whole file / the cap has been consumed. Files whose
    last ``token_count`` is buried under more than ``TAIL_CHUNK_MAX`` bytes of
    later output are reported with no usage rather than read in full.
    """
    summary: dict[str, Any] = {
        "total_token_usage": None,
        "rate_limits": None,
        "model": None,
        "bytes_read": 0,
    }
    if size is None:
        size = path.stat().st_size
    if size <= 0:
        return summary

    chunk = TAIL_CHUNK_INITIAL
    with path.open("rb") as fh:
        while True:
            start = max(0, size - chunk)
            fh.seek(start)
            buf = fh.read(size - start)
            summary["bytes_read"] += len(buf)
            if start > 0:
                # Drop the (likely partial) first line of a mid-file read.
                nl = buf.find(b"\n")
                buf = buf[nl + 1 :] if nl != -1 else b""

            model: Optional[str] = None
            usage: Optional[tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]] = None
            for line in reversed(buf.split(b"\n")):
                # Cheap substring pre-filters before paying for json.loads.
                want_usage = usage is None and b'"token_count"' in line
                want_model = model is None and b'"model"' in line
                if not (want_usage or want_model):
                    continue
                try:
                    payload = _event_payload(json.loads(line))
                except (ValueError, UnicodeDecodeError):
                    continue
                if payload is None:
                    continue
                if want_model and payload.get("model"):
                    model = str(payload["model"])
                if want_usage and (payload.get("type") == "token_count" or "rate_limits" in payload):
                    info = payload.get("info")
                    tot = info.get("total_token_usage") if isinstance(info, dict) else None
                    rl = payload.get("rate_limits")
                    if isinstance(tot, dict) or isinstance(rl, dict):
                        usage = (
                            tot if isinstance(tot, dict) else None,
                            rl if isinstance(rl, dict) else None,
                        )
                if usage is not None and model is not None:
                    break

            if usage is not None:
                # The model is only used for cost estimation; look for it in the same
                # buffer that held the token_count and never grow the read just for it.
                summary["total_token_usage"], summary["rate_limits"] = usage
                summary["model"] = model
                return summary

            if start == 0 or chunk >= TAIL_CHUNK_MAX:
                summary["model"] = model
                return summary
            chunk = min(chunk * 4, TAIL_CHUNK_MAX)


class CodexProvider(BaseProvider):
    """Monitors OpenAI Codex weekly usage/remaining and credit usage/remaining."""

    provider_id = "codex"
    display_name = "Codex (OpenAI)"

    def _codex_home(self) -> Path:
        isolated = self._resolve_isolated_dir("codex_home")
        if isolated is not None:
            return isolated
        return self._resolve_default_env_dir("CODEX_HOME", Path.home() / ".codex")

    def _load_auth(self) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Return (access_token, account_id, api_key) from config or ~/.codex/auth.json."""
        access_token = self.cfg.get("access_token")
        account_id = self.cfg.get("account_id")
        api_key = self.cfg.get("api_key")

        auth_file = self._codex_home() / "auth.json"
        if auth_file.exists() and os.access(auth_file, os.R_OK):
            try:
                raw = json.loads(auth_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    tokens = raw.get("tokens") or {}
                    if isinstance(tokens, dict):
                        access_token = access_token or tokens.get("access_token")
                        account_id = account_id or tokens.get("account_id")
                    access_token = access_token or raw.get("access_token")
                    account_id = account_id or raw.get("account_id")
                    api_key = api_key or raw.get("OPENAI_API_KEY")
            except Exception:
                pass

        return access_token, account_id, api_key

    def _scan_cache(self) -> Optional[FileScanCache]:
        """Open the per-file summary cache; None if the cache dir is unusable."""
        try:
            return FileScanCache(self._scoped_cache_path("codex_sessions_scan"))
        except Exception:
            return None

    def _scan_local_sessions(self, *, include_older_than_7d: bool = False) -> dict[str, Any]:
        """Scan ~/.codex/sessions/**/*.jsonl for latest rate_limits and 7d session totals.

        Rollout files can be hundreds of MB each; only the tail of each file is
        read (see ``summarize_rollout_tail``) and per-file summaries are cached
        by (mtime_ns, size) so unchanged files cost a single ``stat``.
        """
        sessions_dir = self._codex_home() / "sessions"
        result: dict[str, Any] = {
            "found_files": 0,
            "sessions_7d": 0,
            "latest_rate_limits": None,
            "latest_rate_limits_ts": 0.0,
            "tokens_7d": 0,
            "cached_tokens_7d": 0,
            "output_tokens_7d": 0,
            "cost_7d_usd": 0.0,
            "bytes_read": 0,
        }
        if not sessions_dir.exists() or not os.access(sessions_dir, os.R_OK):
            return result

        now = time.time()
        cutoff_7d = now - 7 * 86400

        # (mtime, mtime_ns, size, path) for every recent regular file; stat() exactly once.
        entries: list[tuple[float, int, int, Path]] = []
        try:
            for fpath in sessions_dir.rglob("*.jsonl"):
                try:
                    st = fpath.stat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if not include_older_than_7d and st.st_mtime < cutoff_7d:
                    continue
                entries.append((st.st_mtime, st.st_mtime_ns, st.st_size, fpath))
        except Exception:
            return result
        entries.sort(key=lambda e: e[0], reverse=True)

        cache = self._scan_cache()
        keep: set[str] = set()
        ledger_rows: list[SessionLedgerRow] = []
        acct_id = self.account or "default"

        for mtime, mtime_ns, size, fpath in entries:
            result["found_files"] += 1
            if mtime >= cutoff_7d:
                result["sessions_7d"] += 1
                keep.add(str(fpath))

            summary = cache.get(fpath, mtime_ns, size) if cache is not None else None
            if summary is None:
                try:
                    summary = summarize_rollout_tail(fpath, size)
                except Exception:
                    continue
                result["bytes_read"] += int(summary.get("bytes_read") or 0)
                if cache is not None and mtime >= cutoff_7d:
                    cache.put(fpath, mtime_ns, size, summary)

            rl = summary.get("rate_limits")
            if isinstance(rl, dict) and mtime >= result["latest_rate_limits_ts"]:
                result["latest_rate_limits"] = rl
                result["latest_rate_limits_ts"] = mtime

            session_total_usage = summary.get("total_token_usage")
            if isinstance(session_total_usage, dict):
                in_tok = int(session_total_usage.get("input_tokens") or 0)
                cached_tok = int(session_total_usage.get("cached_input_tokens") or 0)
                out_tok = int(session_total_usage.get("output_tokens") or 0)
                uncached_in = max(0, in_tok - cached_tok)
                mdl_name = str(summary.get("model") or "gpt-5")
                cost = estimate_token_cost_usd(
                    mdl_name,
                    input_tokens=uncached_in,
                    cached_input_tokens=cached_tok,
                    output_tokens=out_tok,
                    default_rates=(1.50, 0.25, 1.50, 6.0),
                )
                if mtime >= cutoff_7d:
                    result["tokens_7d"] += in_tok + out_tok
                    result["cached_tokens_7d"] += cached_tok
                    result["output_tokens_7d"] += out_tok
                    result["cost_7d_usd"] += cost

                if (in_tok + out_tok) > 0:
                    try:
                        rel_p = fpath.relative_to(sessions_dir)
                        sid = str(rel_p.with_suffix("")).replace("\\", "/")
                    except ValueError:
                        sid = fpath.stem
                    day_s = normalize_day_utc(mtime)
                    ledger_rows.append(
                        SessionLedgerRow(
                            provider_id="codex",
                            account_id=acct_id,
                            session_id=sid,
                            day_utc=day_s,
                            first_ts=mtime,
                            last_ts=mtime,
                            project="codex-cli",
                            model=mdl_name,
                            input_tokens=uncached_in,
                            output_tokens=out_tok,
                            cache_read_tokens=cached_tok,
                            cache_write_tokens=0,
                            total_tokens=uncached_in + cached_tok + out_tok,
                            estimated_usd=cost,
                        )
                    )

        if cache is not None:
            cache.prune(keep, root=sessions_dir)
            with critical_section():  # don't leave an orphaned .tmp-*.json at exit
                cache.save()

        if ledger_rows:
            UsageWarehouse().upsert_sessions(ledger_rows)

        return result

    @staticmethod
    def _format_plan_name(plan_type: Any) -> str:
        if not plan_type:
            return "ChatGPT"
        p = str(plan_type).lower()
        if p == "promax":
            return "ChatGPT ProMax"
        return f"ChatGPT {p.capitalize()}"

    def fetch(self) -> ProviderReport:
        report = self.new_report()

        access_token, account_id, api_key = self._load_auth()
        local_data = self._scan_local_sessions()
        sources: list[str] = []

        api_data: Optional[dict[str, Any]] = None
        if access_token:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "codex-cli/0.1.0",
            }
            if account_id:
                headers["ChatGPT-Account-Id"] = str(account_id)
            try:
                api_data = self.http_json(
                    "https://chatgpt.com/backend-api/wham/usage",
                    headers=headers,
                )
                sources.append("wham/usage API")
            except Exception as exc:
                report.error_message = str(exc)

        # 1. Populate from live wham/usage API if available
        if isinstance(api_data, dict) and api_data:
            report.status = "ok"
            if api_data.get("plan_type"):
                report.plan_tier = self._format_plan_name(api_data.get("plan_type"))

            rl = api_data.get("rate_limit") or {}
            if isinstance(rl, dict):
                primary = rl.get("primary_window") if isinstance(rl.get("primary_window"), dict) else {}
                secondary = rl.get("secondary_window") if isinstance(rl.get("secondary_window"), dict) else {}

                # Identify which window is 7d weekly vs 5h short-term based on limit_window_seconds
                pri_sec = int(primary.get("limit_window_seconds") or 0)
                sec_sec = int(secondary.get("limit_window_seconds") or 0)

                if secondary and secondary.get("used_percent") is not None:
                    weekly_win = secondary
                    short_win = primary if (primary and pri_sec <= 86400) else None
                elif primary and primary.get("used_percent") is not None:
                    weekly_win = primary
                    short_win = primary if (0 < pri_sec <= 86400) else None
                else:
                    weekly_win = {}
                    short_win = None

                if weekly_win and weekly_win.get("used_percent") is not None:
                    used_pct = float(weekly_win["used_percent"])
                    reset_raw = (
                        weekly_win.get("reset_at")
                        if weekly_win.get("reset_at") is not None
                        else weekly_win.get("reset_after_seconds")
                    )
                    resets_at, resets_in = format_reset_countdown(reset_raw)
                    win_sec = int(weekly_win.get("limit_window_seconds") or (7 * 86400))
                    report.weekly = UsageWindow(
                        used=round(used_pct, 1),
                        remaining=round(max(0.0, 100.0 - used_pct), 1),
                        limit=100.0,
                        unit="%",
                        used_percent=used_pct,
                        remaining_percent=max(0.0, 100.0 - used_pct),
                        resets_at=resets_at,
                        resets_in=resets_in,
                        window_seconds=win_sec if win_sec > 0 else (7 * 86400),
                    )
                    report.sub_metrics.append(
                        SubMetric(
                            name="7d Weekly Window",
                            used_str=f"{used_pct:.1f}%",
                            remaining_str=f"{max(0.0, 100.0 - used_pct):.1f}%",
                            used_percent=used_pct,
                            resets_in=resets_in,
                        )
                    )

                if short_win and short_win.get("used_percent") is not None:
                    p_used = float(short_win["used_percent"])
                    _, p_reset_in = format_reset_countdown(
                        short_win.get("reset_at") or short_win.get("reset_after_seconds")
                    )
                    report.sub_metrics.append(
                        SubMetric(
                            name="5h Rolling Window",
                            used_str=f"{p_used:.1f}%",
                            remaining_str=f"{max(0.0, 100.0 - p_used):.1f}%",
                            used_percent=p_used,
                            resets_in=p_reset_in,
                        )
                    )
                elif weekly_win and weekly_win.get("used_percent") is not None:
                    # OpenAI omits secondary_window when the 7d window is in primary_window
                    # (e.g. when 7d is exhausted at 100% or no 5h window is active).
                    w_u = float(weekly_win["used_percent"])
                    rl_loc = local_data.get("latest_rate_limits")
                    pri_loc = rl_loc.get("primary") if isinstance(rl_loc, dict) else None
                    loc_5h_used: Optional[float] = None
                    loc_5h_rin: Optional[str] = None
                    if isinstance(pri_loc, dict) and pri_loc.get("used_percent") is not None:
                        loc_at, loc_in = format_reset_countdown(pri_loc.get("resets_at"))
                        loc_rem_s = parse_reset_remaining_seconds(loc_at, loc_in)
                        if loc_rem_s is not None and 0.0 < loc_rem_s <= 6 * 3600:
                            loc_5h_used = float(pri_loc["used_percent"])
                            loc_5h_rin = loc_in
                    if w_u >= 100.0:
                        eff_5h_used = 100.0
                        eff_5h_rin = loc_5h_rin or report.weekly.resets_in
                    elif loc_5h_used is not None:
                        eff_5h_used = loc_5h_used
                        eff_5h_rin = loc_5h_rin
                    else:
                        eff_5h_used = 0.0
                        eff_5h_rin = None
                    report.sub_metrics.append(
                        SubMetric(
                            name="5h Rolling Window",
                            used_str=f"{eff_5h_used:.1f}%",
                            remaining_str=f"{max(0.0, 100.0 - eff_5h_used):.1f}%",
                            used_percent=eff_5h_used,
                            resets_in=eff_5h_rin,
                        )
                    )

            # Check code_review_rate_limit if present
            cr_rl = api_data.get("code_review_rate_limit")
            if isinstance(cr_rl, dict):
                cr_win = (
                    cr_rl.get("secondary_window")
                    if isinstance(cr_rl.get("secondary_window"), dict)
                    else cr_rl.get("primary_window")
                )
                if isinstance(cr_win, dict) and cr_win.get("used_percent") is not None:
                    cr_u = float(cr_win["used_percent"])
                    _, cr_rin = format_reset_countdown(
                        cr_win.get("reset_at") or cr_win.get("reset_after_seconds")
                    )
                    report.sub_metrics.append(
                        SubMetric(
                            name="Code Review Pool",
                            used_str=f"{cr_u:.1f}%",
                            remaining_str=f"{max(0.0, 100.0 - cr_u):.1f}%",
                            used_percent=cr_u,
                            resets_in=cr_rin,
                        )
                    )

            # Check additional_rate_limits if present
            add_limits = api_data.get("additional_rate_limits")
            if isinstance(add_limits, list):
                for al in add_limits:
                    if not isinstance(al, dict):
                        continue
                    al_name = str(al.get("limit_name") or al.get("model") or "Additional Pool")
                    al_rl = al.get("rate_limit") if isinstance(al.get("rate_limit"), dict) else al
                    al_win = al_rl.get("secondary_window") or al_rl.get("primary_window")
                    if isinstance(al_win, dict) and al_win.get("used_percent") is not None:
                        u_pct = float(al_win["used_percent"])
                        _, r_in = format_reset_countdown(
                            al_win.get("reset_at") or al_win.get("reset_after_seconds")
                        )
                        report.sub_metrics.append(
                            SubMetric(
                                name=al_name,
                                used_str=f"{u_pct:.1f}%",
                                remaining_str=f"{max(0.0, 100.0 - u_pct):.1f}%",
                                used_percent=u_pct,
                                resets_in=r_in,
                            )
                        )

            credits_obj = api_data.get("credits") or {}
            if isinstance(credits_obj, dict) and credits_obj:
                c_unit = "USD" if str(credits_obj.get("currency", "")).upper() == "USD" else "credits"
                if credits_obj.get("unlimited"):
                    report.credits = CreditBalance(
                        unit=c_unit,
                        is_unlimited=True,
                    )
                elif credits_obj.get("balance") is not None:
                    try:
                        bal = float(credits_obj["balance"])
                        used_val = (
                            float(credits_obj["used"])
                            if credits_obj.get("used") is not None
                            else None
                        )
                        lim_val = (
                            float(credits_obj["limit"])
                            if credits_obj.get("limit") is not None
                            else None
                        )
                        if used_val is not None and lim_val is not None:
                            c_unit = "USD"
                        report.credits = CreditBalance(
                            used=used_val,
                            remaining=round(bal, 2),
                            limit=lim_val,
                            unit=c_unit,
                            note="Codex Credits" if used_val is None else None,
                        )
                    except (ValueError, TypeError):
                        pass

        # 2. Fallback / supplement with local ~/.codex/sessions JSONL
        if local_data["found_files"] > 0:
            sources.append("~/.codex/sessions")
            rl_local = local_data.get("latest_rate_limits")
            if report.weekly.used_percent is None and isinstance(rl_local, dict):
                sec = rl_local.get("secondary") or rl_local.get("primary") or {}
                if isinstance(sec, dict) and sec.get("used_percent") is not None:
                    used_pct = float(sec["used_percent"])
                    resets_at, resets_in = format_reset_countdown(sec.get("resets_at"))
                    win_min = sec.get("window_minutes")
                    local_win_sec = (
                        int(float(win_min) * 60)
                        if isinstance(win_min, (int, float)) and float(win_min) > 0
                        else (7 * 86400 if rl_local.get("secondary") else 5 * 3600)
                    )
                    report.weekly = UsageWindow(
                        used=round(used_pct, 1),
                        remaining=round(max(0.0, 100.0 - used_pct), 1),
                        limit=100.0,
                        unit="%",
                        used_percent=used_pct,
                        remaining_percent=max(0.0, 100.0 - used_pct),
                        resets_at=resets_at,
                        resets_in=resets_in,
                        window_seconds=local_win_sec,
                    )
                    report.status = "ok"
                    report.sub_metrics.append(
                        SubMetric(
                            name="7d Weekly Window",
                            used_str=f"{used_pct:.1f}%",
                            remaining_str=f"{max(0.0, 100.0 - used_pct):.1f}%",
                            used_percent=used_pct,
                            resets_in=resets_in,
                        )
                    )
                    pri_loc = rl_local.get("primary") if isinstance(rl_local.get("secondary"), dict) else None
                    if isinstance(pri_loc, dict) and pri_loc.get("used_percent") is not None:
                        p_u = float(pri_loc["used_percent"])
                        _, p_rin = format_reset_countdown(pri_loc.get("resets_at"))
                        report.sub_metrics.append(
                            SubMetric(
                                name="5h Rolling Window",
                                used_str=f"{p_u:.1f}%",
                                remaining_str=f"{max(0.0, 100.0 - p_u):.1f}%",
                                used_percent=p_u,
                                resets_in=p_rin,
                            )
                        )

                cred_local = rl_local.get("credits")
                if (
                    report.credits.remaining is None
                    and isinstance(cred_local, dict)
                    and cred_local.get("balance") is not None
                ):
                    try:
                        bal = float(cred_local["balance"])
                        report.credits = CreditBalance(
                            remaining=round(bal, 2),
                            unit="credits",
                            note="Codex Credits",
                        )
                    except (ValueError, TypeError):
                        pass

            if local_data["sessions_7d"] > 0:
                out_m = local_data["output_tokens_7d"] / 1_000_000.0
                sess_cnt = local_data["sessions_7d"]
                report.sub_metrics.append(
                    SubMetric(
                        name="7d Local Codex Sessions",
                        used_str=f"{sess_cnt} sessions ({out_m:.2f}M output tok)",
                        remaining_str="—",
                    )
                )

        if sources:
            report.source = " + ".join(sources)

        if report.status == "unconfigured":
            if api_key and not access_token:
                report.status = "partial"
                report.plan_tier = "OpenAI API Key"
                report.source = "OPENAI_API_KEY"
                q_target = f"codex:{self.account}" if self.account else "codex"
                report.auth_hint = (
                    f"Run `codex login` for weekly quota or `tokenmaster quota {q_target} --credit-limit <USD>`"
                )
            elif self.account:
                report.auth_hint = (
                    f"Run `tokenmaster login codex {self.account}` or `tokenmaster auth codex --account {self.account}`"
                )
            else:
                report.auth_hint = (
                    "Login via `codex` (~/.codex/auth.json) or run `tokenmaster auth codex`"
                )

        return self.apply_config_overrides(report)
