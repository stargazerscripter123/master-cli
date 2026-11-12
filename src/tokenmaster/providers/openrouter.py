"""OpenRouter provider (/api/v1/key for usage_weekly & /api/v1/credits for account balance)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from tokenmaster.models import CreditBalance, ProviderReport, SubMetric, UsageWindow, format_reset_countdown
from tokenmaster.providers.base import BaseProvider, run_concurrently


def _next_monday_utc_iso() -> str:
    """Return ISO timestamp for next Monday 00:00:00 UTC (OpenRouter's weekly reset)."""
    now = datetime.now(timezone.utc)
    days_ahead = (7 - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    next_mon = (now + timedelta(days=days_ahead)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return next_mon.isoformat()


class OpenRouterProvider(BaseProvider):
    """Monitors OpenRouter weekly USD usage/remaining and account credit usage/remaining."""

    provider_id = "openrouter"
    display_name = "OpenRouter"

    def _resolve_keys(self) -> tuple[Optional[str], Optional[str]]:
        api_key = self.cfg.get("api_key")
        mgmt_key = self.cfg.get("management_key")

        if not api_key:
            candidates: list[Path] = []
            isolated_dir = self._resolve_isolated_dir()
            if isolated_dir is not None:
                candidates.append(isolated_dir / "key")
            elif not self.account:
                candidates.extend(
                    (
                        Path.home() / ".openrouter" / "key",
                        Path.home() / ".config" / "openrouter" / "key",
                    )
                )
            for candidate in candidates:
                if candidate.exists() and os.access(candidate, os.R_OK):
                    try:
                        val = candidate.read_text(encoding="utf-8").strip()
                        if val:
                            api_key = val
                            break
                    except Exception:
                        pass

        return api_key, mgmt_key

    def fetch(self) -> ProviderReport:
        report = self.new_report()

        api_key, mgmt_key = self._resolve_keys()
        if not api_key and not mgmt_key:
            if self.account:
                report.auth_hint = (
                    f"Run `tokenmaster auth openrouter --account {self.account} --api-key <KEY>`"
                )
            else:
                report.auth_hint = (
                    "Set `OPENROUTER_API_KEY` or run `tokenmaster auth openrouter`"
                )
            return self.apply_config_overrides(report)

        sources: list[str] = []
        key_data: Optional[dict[str, Any]] = None
        credits_data: Optional[dict[str, Any]] = None

        active_key = api_key or mgmt_key
        credit_auth_key = mgmt_key or api_key

        def _get(url: str, key: str) -> Callable[[], Any]:
            return lambda: self.http_json(url, headers={"Authorization": f"Bearer {key}"})

        # /key and /credits are independent: fetch concurrently.
        tasks: dict[str, Callable[[], Any]] = {}
        if active_key:
            tasks["key"] = _get("https://openrouter.ai/api/v1/key", active_key)
        if credit_auth_key:
            tasks["credits"] = _get("https://openrouter.ai/api/v1/credits", credit_auth_key)
        results = run_concurrently(tasks)

        if "key" in results:
            res, exc = results["key"]
            if exc is None and not isinstance(res, dict):
                exc = ValueError(f"Unexpected /api/v1/key response: {type(res).__name__}")
            if exc is not None:
                report.error_message = str(exc)
            else:
                key_data = res.get("data") if isinstance(res.get("data"), dict) else res
                sources.append("/api/v1/key")

        if "credits" in results:
            res_c, exc_c = results["credits"]
            if exc_c is None and not isinstance(res_c, dict):
                exc_c = ValueError(f"Unexpected /api/v1/credits response: {type(res_c).__name__}")
            if exc_c is not None:
                if not report.error_message:
                    report.error_message = str(exc_c)
            else:
                credits_data = res_c.get("data") if isinstance(res_c.get("data"), dict) else res_c
                sources.append("/api/v1/credits")

        # 1. Parse Account Credits (/api/v1/credits)
        acct_remaining: Optional[float] = None
        if isinstance(credits_data, dict) and credits_data.get("total_credits") is not None:
            report.status = "ok"
            tot_credits = float(credits_data.get("total_credits") or 0.0)
            tot_usage = float(credits_data.get("total_usage") or 0.0)
            acct_remaining = max(0.0, tot_credits - tot_usage)
            used_pct = (tot_usage / tot_credits * 100.0) if tot_credits > 0 else 0.0

            report.credits = CreditBalance(
                used=round(tot_usage, 4 if 0 < tot_usage < 0.01 else 2),
                remaining=round(acct_remaining, 2),
                limit=round(tot_credits, 2),
                unit="USD",
                used_percent=used_pct,
                remaining_percent=max(0.0, 100.0 - used_pct),
            )

        # 2. Parse Key Weekly & All-Time Metrics (/api/v1/key)
        if isinstance(key_data, dict) and key_data:
            report.status = "ok"
            is_free = bool(key_data.get("is_free_tier"))
            report.plan_tier = "Free Tier" if is_free else "Prepaid Credits"

            usage_weekly = float(key_data.get("usage_weekly") or 0.0)
            usage_daily = float(key_data.get("usage_daily") or 0.0)
            usage_monthly = float(key_data.get("usage_monthly") or 0.0)
            usage_total = float(key_data.get("usage") or 0.0)

            key_limit = (
                float(key_data["limit"]) if key_data.get("limit") is not None else None
            )
            key_rem = (
                float(key_data["limit_remaining"])
                if key_data.get("limit_remaining") is not None
                else None
            )
            limit_reset = key_data.get("limit_reset")

            resets_at, resets_in = format_reset_countdown(_next_monday_utc_iso())

            # Determine weekly ceiling & remaining
            w_limit: Optional[float] = None
            w_rem: Optional[float] = None
            if limit_reset == "weekly" and key_limit is not None:
                w_limit = key_limit
                w_rem = key_rem if key_rem is not None else max(0.0, key_limit - usage_weekly)
            elif self.cfg.get("weekly_limit") is not None:
                w_limit = float(self.cfg["weekly_limit"])
                w_rem = max(0.0, w_limit - usage_weekly)
            elif key_rem is not None:
                w_rem = key_rem
                w_limit = usage_weekly + key_rem
            elif acct_remaining is not None:
                w_rem = acct_remaining
                w_limit = usage_weekly + acct_remaining

            report.weekly = UsageWindow(
                used=round(usage_weekly, 4 if 0 < usage_weekly < 0.01 else 2),
                remaining=round(w_rem, 2) if w_rem is not None else None,
                limit=round(w_limit, 2) if w_limit is not None else None,
                unit="USD",
                resets_at=resets_at,
                resets_in=resets_in,
            )

            # Fallback for credits if /api/v1/credits was not accessible
            if report.credits.used is None:
                report.credits = CreditBalance(
                    used=round(usage_total, 2),
                    remaining=round(key_rem, 2) if key_rem is not None else None,
                    limit=round(key_limit, 2) if key_limit is not None else None,
                    unit="USD",
                )

            report.sub_metrics.extend(
                [
                    SubMetric(
                        name="Daily Spend (UTC)",
                        used_str=f"${usage_daily:.4f}" if 0 < usage_daily < 0.01 else f"${usage_daily:.2f}",
                        remaining_str="—",
                    ),
                    SubMetric(
                        name="Monthly Spend (UTC)",
                        used_str=f"${usage_monthly:.2f}",
                        remaining_str=f"${key_rem:.2f} key cap left" if key_rem is not None else "—",
                    ),
                ]
            )

        if sources:
            report.source = " + ".join(sources)

        return self.apply_config_overrides(report)
