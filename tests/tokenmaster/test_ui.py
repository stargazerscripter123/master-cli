"""Unit tests for TokenMaster terminal UI rendering, Unicode width, and responsive layout."""

from __future__ import annotations

import io
import unittest

from tokenmaster.cli import _WATCH_USE_CONFIG_DEFAULT, build_parser
from tokenmaster.models import (
    CreditBalance,
    ProviderReport,
    SubMetric,
    UsageWindow,
    format_duration_short,
    parse_reset_remaining_seconds,
)
from tokenmaster.ui import (
    ANSI_RE,
    Style,
    _fit_col_widths,
    _format_latency_ms,
    char_width,
    format_status_dot,
    pad_cell,
    paint_watch_frame,
    render_dashboard,
    render_statusline,
    truncate_visible,
    visible_len,
)


class _TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class TestTerminalUI(unittest.TestCase):
    def test_east_asian_width_and_truncation(self) -> None:
        self.assertEqual(char_width("a"), 1)
        self.assertEqual(char_width("中"), 2)
        self.assertEqual(char_width("中文"), 4)
        self.assertEqual(char_width("\u200b"), 0)

        styled_cjk = Style.wrap("专业版Pro", Style.CYAN, enabled=True)
        # 3 CJK chars (6 cols) + 3 ASCII chars (3 cols) = 9 cols
        self.assertEqual(visible_len(styled_cjk), 9)

        trunc = truncate_visible(styled_cjk, 6)
        self.assertLessEqual(visible_len(trunc), 6)
        self.assertTrue(trunc.endswith(Style.RESET))

        padded = pad_cell(styled_cjk, 6, truncate=True)
        self.assertEqual(visible_len(padded), 6)
        self.assertEqual(pad_cell("abc", 0, truncate=True), "")

        # Ensure next span's ANSI color and trailing ZWJ do NOT bleed into ellipsis
        two_spans = Style.wrap("ab\u200d", Style.GREEN, enabled=True) + Style.wrap(
            "cd", Style.RED, enabled=True
        )
        trunc_spans = truncate_visible(two_spans, 3)
        self.assertEqual(trunc_spans, f"{Style.GREEN}ab…{Style.RESET}")
        self.assertNotIn(Style.RED, trunc_spans)
        self.assertNotIn("\u200d", trunc_spans)

    def test_table_alignment_with_cjk_and_narrow_terminal(self) -> None:
        r1 = ProviderReport(
            provider_id="codex",
            display_name="Codex (OpenAI)",
            status="ok",
            plan_tier="ChatGPT 旗舰版\nLine2",
            source="wham/usage API",
            weekly=UsageWindow(used_percent=92.5, remaining_percent=7.5, resets_in="2d 4h"),
            credits=CreditBalance(used=1250.0, remaining=250.0, limit=1500.0, unit="USD"),
            fetch_ms=999.6,
        )
        r2 = ProviderReport(
            provider_id="cursor",
            display_name="Cursor IDE",
            status="error",
            plan_tier="Cursor Ultra",
            source="cursor-agent CLI",
            weekly=UsageWindow(used_percent=0.2, remaining_percent=99.8, resets_in="26d 9h"),
            credits=CreditBalance(note="Unified w/ xAI Billing"),
            error_message="HTTP 500:\nInternal Server Error\tDetails",
            fetch_ms=-5.0,  # invalid negative latency ignored
        )

        r3 = ProviderReport(
            provider_id="claude",
            display_name="Claude (Anthropic)",
            status="partial",
            plan_tier="Claude Max 20x",
            source="local projects",
        )
        r4 = ProviderReport(
            provider_id="openrouter",
            display_name="OpenRouter",
            status="unconfigured",
        )

        for max_w in (None, 120, 95, 50):
            out = render_dashboard([r1, r2, r3, r4], show_detail=True, color_enabled=True, max_width=max_w)
            table_lines = out.splitlines()[1:9]
            self.assertEqual(len(table_lines), 8)
            for tln in table_lines:
                self.assertNotIn("│", ANSI_RE.sub("", tln))
            widths = {visible_len(line) for line in table_lines}
            self.assertEqual(len(widths), 1, f"Inconsistent row widths for max_width={max_w}: {widths}")
            if max_w is not None:
                for line in out.splitlines():
                    self.assertLessEqual(
                        visible_len(line),
                        max_w,
                        f"Line exceeded max_width={max_w}: {ANSI_RE.sub('', line)!r}",
                    )

        full_out = render_dashboard([r1, r2, r3, r4], color_enabled=False)
        self.assertNotIn("Weekly Usage", full_out)
        self.assertNotIn("Credit Usage", full_out)
        self.assertNotIn("│ Status", full_out)
        self.assertIn("Weekly Remaining", full_out)
        self.assertIn("Credit", full_out)
        self.assertNotIn("Credit Remaining", full_out)
        self.assertNotIn("Tier / Source", full_out)
        self.assertIn("● Codex", full_out)
        self.assertNotIn("Codex (OpenAI)", full_out)
        self.assertIn("✖ Cursor IDE", full_out)
        self.assertIn("◐ Claude", full_out)
        self.assertNotIn("Claude (Anthropic)", full_out)
        self.assertIn("○ OpenRouter", full_out)
        # 999.6ms rounds cleanly to 1.00s instead of "1000ms"
        self.assertIn("Fetched in 1.00s", full_out)
        self.assertIn("1/4 LIVE", full_out)
        self.assertIn("High usage: Codex (92.5%)", full_out)

        colored_out = render_dashboard([r1, r2, r3, r4], color_enabled=True)
        self.assertIn(format_status_dot("ok", color_enabled=True), colored_out)
        self.assertIn(format_status_dot("error", color_enabled=True), colored_out)
        self.assertIn(format_status_dot("partial", color_enabled=True), colored_out)
        self.assertIn(format_status_dot("unconfigured", color_enabled=True), colored_out)

        # Verify _fit_col_widths never explodes back to full width when max_total_width <= border_overhead
        shrunk_5 = _fit_col_widths([22, 15, 20, 10, 15], 15)
        self.assertEqual(shrunk_5, [1, 1, 1, 1, 1])
        shrunk_tiny = _fit_col_widths([20, 10, 15, 20, 10, 25, 15], 20)
        self.assertEqual(shrunk_tiny, [1, 1, 1, 1, 1, 1, 1])
        self.assertEqual(_format_latency_ms(999.6), "1.00s")
        self.assertEqual(_format_latency_ms(420.2), "420ms")

    def test_detail_submetrics_dynamic_column_alignment(self) -> None:
        rep = ProviderReport(
            provider_id="cursor",
            display_name="Cursor IDE",
            status="ok",
            plan_tier="Cursor Ultra",
            source="cursor-agent CLI",
            fetch_ms=880.0,
            sub_metrics=[
                SubMetric(
                    name="Plan Included Quota (Total)",  # 27 chars (> old hardcoded 26)
                    used_str="0.2% ($400.00 pool)",
                    remaining_str="99.8%",
                    used_percent=0.2,
                ),
                SubMetric(
                    name="API Pool",
                    used_str="0.3% (shared)",
                    remaining_str="99.7%",
                    used_percent=0.3,
                ),
                SubMetric(
                    name="7d Local Session Tokens",
                    used_str="22586.21M tok (~$12306.72)",  # > old hardcoded 28
                    remaining_str="—",
                    used_percent=None,
                ),
            ],
        )
        out = render_dashboard([rep], show_detail=True, color_enabled=False)
        self.assertIn("[cursor-agent CLI • 880ms]", out)

        sub_lines = [line for line in out.splitlines() if "Used:" in line and "Remaining:" in line]
        self.assertEqual(len(sub_lines), 3)
        used_offsets = {line.index("Used:") for line in sub_lines}
        rem_offsets = {line.index("Remaining:") for line in sub_lines}
        self.assertEqual(len(used_offsets), 1, f"Misaligned 'Used:' columns: {used_offsets}")
        self.assertEqual(len(rem_offsets), 1, f"Misaligned 'Remaining:' columns: {rem_offsets}")

    def test_paint_watch_frame_flicker_free_ansi(self) -> None:
        import os
        from unittest.mock import patch

        # The assertion is about the ANSI sequence. Ignore a parent NO_COLOR.
        env = os.environ.copy()
        env.pop("NO_COLOR", None)
        env.pop("FORCE_COLOR", None)
        with patch.dict(os.environ, env, clear=True):
            buf1 = _TTYBuffer()
            paint_watch_frame("line1\nline2", first_frame=True, stream=buf1)
            self.assertTrue(buf1.getvalue().startswith("\x1b[2J\x1b[H"))
            self.assertTrue(buf1.getvalue().endswith("\x1b[K\n\x1b[J"))

            buf2 = _TTYBuffer()
            paint_watch_frame("line1\nline2", first_frame=False, stream=buf2)
            self.assertTrue(buf2.getvalue().startswith("\x1b[H"))
            self.assertFalse(buf2.getvalue().startswith("\x1b[2J"))
            self.assertIn("line1\x1b[K\nline2\x1b[K\n\x1b[J", buf2.getvalue())

    def test_cli_subparser_preserves_root_flags(self) -> None:
        parser = build_parser()
        a1 = parser.parse_args(["--width", "100", "--detail", "-p", "codex", "status"])
        self.assertEqual(a1.width, 100)
        self.assertTrue(a1.detail)
        self.assertEqual(a1.provider, ["codex"])

        a2 = parser.parse_args(["--width", "90", "--no-color", "watch", "-i", "0"])
        self.assertEqual(a2.width, 90)
        self.assertTrue(a2.no_color)
        self.assertEqual(a2.interval, 0)

        a3 = parser.parse_args(["-w"])
        self.assertIs(a3.watch, _WATCH_USE_CONFIG_DEFAULT)

        a4 = parser.parse_args(["-w", "0"])
        self.assertEqual(a4.watch, 0)

        # Passing -p both before and after `status` preserves both
        a5 = parser.parse_args(["-p", "codex", "status", "-p", "claude"])
        self.assertEqual(a5.provider, ["codex"])
        self.assertEqual(a5.sub_provider, ["claude"])

    def test_config_clear_cache_and_doctor_cache_diagnostics(self) -> None:
        import json
        import os
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path
        from unittest.mock import patch
        from tokenmaster.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / "config.json"
            cache_dir = root / "cache"
            cache_dir.mkdir()

            (cache_dir / "codex_sessions_scan.json").write_text(
                json.dumps({"version": 1, "entries": {"/a": {}, "/b": {}}}),
                encoding="utf-8",
            )
            (cache_dir / "antigravity_rpc.json").write_text(
                json.dumps({"scheme": "https", "port": 51234, "csrf_token": "tok-1"}),
                encoding="utf-8",
            )
            (cache_dir / "claude_projects_scan.json").write_text("{corrupt", encoding="utf-8")
            (cache_dir / "grok_sessions_scan.json").write_text(
                json.dumps({"version": 99, "entries": {"/x": {}}}),
                encoding="utf-8",
            )
            unrelated = cache_dir / "keep_me.txt"
            unrelated.write_text("keep", encoding="utf-8")

            env = {
                "TOKENMASTER_CACHE_DIR": str(cache_dir),
                "CODEX_HOME": str(root / ".codex"),
                "CLAUDE_CONFIG_DIR": str(root / ".claude"),
                "ANTIGRAVITY_DIR": str(root / ".antigravity"),
                "GROK_HOME": str(root / ".grok"),
                "CURSOR_HOME": str(root / ".cursor"),
                "NO_COLOR": "1",
            }
            with patch.dict(os.environ, env):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = main(["--config", str(cfg_path), "doctor"])
                self.assertEqual(rc, 0)
                doc_out = buf.getvalue()
                self.assertIn("2 cached file(s)", doc_out)
                self.assertIn("STALE (https://127.0.0.1:51234 unreachable)", doc_out)
                self.assertIn("CORRUPT", doc_out)
                self.assertIn("INVALID", doc_out)

                buf_clear = io.StringIO()
                with redirect_stdout(buf_clear):
                    rc2 = main(["--config", str(cfg_path), "config", "clear-cache"])
                self.assertEqual(rc2, 0)
                self.assertIn("Cleared 4 cache file(s)", buf_clear.getvalue())
                self.assertFalse((cache_dir / "codex_sessions_scan.json").exists())
                self.assertFalse((cache_dir / "antigravity_rpc.json").exists())
                self.assertFalse((cache_dir / "claude_projects_scan.json").exists())
                self.assertFalse((cache_dir / "grok_sessions_scan.json").exists())
                self.assertTrue(unrelated.exists())

    def test_reset_aware_quota_pacing_and_exhaustion_eta(self) -> None:
        from datetime import datetime, timezone

        ref_now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
        naive_now = datetime(2026, 11, 10, 12, 0, 0)
        self.assertEqual(parse_reset_remaining_seconds(None, "now", now=ref_now), 0.0)
        self.assertEqual(parse_reset_remaining_seconds(None, "5d 0h", now=ref_now), 5 * 86400.0)
        # Booleans and offset-naive `now` are handled safely
        self.assertEqual(parse_reset_remaining_seconds(True, "5d 0h", now=naive_now), 5 * 86400.0)
        # Authoritative future resets_at wins even if resets_in was "now"
        self.assertEqual(
            parse_reset_remaining_seconds("2026-11-11 12:00 UTC", "now", now=naive_now),
            86400.0,
        )
        # Full-precision ISO timestamp that expired 10s ago returns 0.0 (no minute-truncation grace)
        self.assertEqual(
            parse_reset_remaining_seconds("2026-11-10T11:59:50Z", "1m", now=ref_now),
            0.0,
        )
        # Minute-truncated "%Y-%m-%d %H:%M UTC" with garbage resets_in does not get positive grace
        self.assertEqual(
            parse_reset_remaining_seconds("2026-11-10 12:00 UTC", "invalid", now=ref_now),
            0.0,
        )
        self.assertEqual(parse_reset_remaining_seconds(None, "3h 30m", now=ref_now), 3.5 * 3600.0)
        self.assertEqual(
            parse_reset_remaining_seconds("2026-11-11 12:00 UTC", "1d 0h", now=ref_now),
            86400.0,
        )
        # Expired cached timestamp (>60s in past) returns 0.0 even if stale resets_in string says "15m"
        self.assertEqual(
            parse_reset_remaining_seconds("2026-11-10 11:00 UTC", "15m", now=ref_now),
            0.0,
        )
        self.assertEqual(format_duration_short(0), "now")
        self.assertEqual(format_duration_short(115200), "1d 8h")

        # 1. Overburn case: 60% used after 2 days (28.6% elapsed) of a 7-day window
        w_over = UsageWindow(used_percent=60.0, resets_in="5d 0h", window_seconds=7 * 86400).finalize()
        self.assertAlmostEqual(w_over.elapsed_percent or 0.0, 28.6, places=1)
        self.assertAlmostEqual(w_over.pace_delta_percent or 0.0, 31.4, places=1)
        self.assertAlmostEqual(w_over.pace_ratio or 0.0, 2.10, places=2)
        self.assertEqual(w_over.pace_status, "overburn")
        self.assertEqual(w_over.projected_exhaustion_in, "1d 8h")
        self.assertIn("Est. exhaustion in 1d 8h", w_over.pace_summary_str() or "")

        # 2. On-track case: 50% used at 3d 12h remaining (50% elapsed)
        w_track = UsageWindow(used_percent=50.0, resets_in="3d 12h", window_seconds=7 * 86400).finalize()
        self.assertAlmostEqual(w_track.elapsed_percent or 0.0, 50.0, places=1)
        self.assertAlmostEqual(w_track.pace_ratio or 0.0, 1.00, places=2)
        self.assertEqual(w_track.pace_status, "on_track")
        self.assertIsNone(w_track.projected_exhaustion_in)
        self.assertIn("On track", w_track.pace_summary_str() or "")

        # 3. Surplus case: 15% used with 2d 0h remaining (71.4% elapsed)
        w_surplus = UsageWindow(used_percent=15.0, resets_in="2d 0h", window_seconds=7 * 86400).finalize()
        self.assertEqual(w_surplus.pace_status, "surplus")
        self.assertIsNone(w_surplus.projected_exhaustion_in)
        self.assertIn("Safe until reset", w_surplus.pace_summary_str() or "")

        # 4. Early window protection: 3% used in first hour (6d 23h left) does not trigger false overburn
        w_early = UsageWindow(used_percent=3.0, resets_in="6d 23h", window_seconds=7 * 86400).finalize()
        self.assertNotEqual(w_early.pace_status, "overburn")
        self.assertIsNone(w_early.projected_exhaustion_in)

        # 5. Dashboard renders Pace warning in Summary and Weekly Pace & ETA in --detail
        rep_over = ProviderReport(
            provider_id="claude",
            display_name="Claude (Anthropic) (work)",
            account="work",
            status="ok",
            plan_tier="Claude Max 20x",
            source="OAuth Usage API",
            weekly=w_over,
        )
        out = render_dashboard([rep_over], show_detail=True, color_enabled=False)
        self.assertIn("Pace warning: Claude [work] (2.1x, est. 1d 8h)", out)
        self.assertIn("Weekly Pace & ETA", out)
        self.assertIn("Est. exhaustion in 1d 8h", out)

        # 6. apply_config_overrides recomputes remaining, used_percent, and pacing when weekly_limit is overridden
        from tokenmaster.providers.base import BaseProvider

        class _DummyProvider(BaseProvider):
            provider_id = "codex"
            display_name = "Codex"

            def fetch(self) -> ProviderReport:
                return self.new_report()

        dummy = _DummyProvider({"weekly_limit": 200.0, "credit_limit": 500.0})
        pre_rep = ProviderReport(
            provider_id="codex",
            display_name="Codex",
            status="ok",
            weekly=UsageWindow(
                used=50.0,
                remaining=50.0,
                limit=100.0,
                unit="USD",
                used_percent=50.0,
                remaining_percent=50.0,
                pace_used_percent=50.0,
                resets_in="3d 12h",
            ),
            credits=CreditBalance(
                used=100.0,
                remaining=100.0,
                limit=200.0,
                unit="USD",
                used_percent=50.0,
                remaining_percent=50.0,
            ),
        )
        dummy.apply_config_overrides(pre_rep)
        self.assertAlmostEqual(pre_rep.weekly.remaining or 0.0, 150.0)
        self.assertAlmostEqual(pre_rep.weekly.used_percent or 0.0, 25.0)
        self.assertAlmostEqual(pre_rep.weekly.remaining_percent or 0.0, 75.0)
        self.assertEqual(pre_rep.weekly.pace_status, "surplus")
        self.assertAlmostEqual(pre_rep.credits.remaining or 0.0, 400.0)
        self.assertAlmostEqual(pre_rep.credits.used_percent or 0.0, 20.0)

    def test_provider_report_from_dict_roundtrip_and_render_statusline(self) -> None:
        rep_claude = ProviderReport(
            provider_id="claude",
            display_name="Claude (work)",
            account="work",
            status="ok",
            plan_tier="Claude Max 20x",
            source="OAuth Usage API",
            weekly=UsageWindow(
                used_percent=28.0,
                remaining_percent=72.0,
                resets_in="4d 2h",
                window_seconds=7 * 86400,
            ).finalize(),
            sub_metrics=[
                SubMetric(
                    name="5h Rolling Window",
                    used_str="16.0%",
                    remaining_str="84.0%",
                    resets_in="3h 10m",
                    used_percent=16.0,
                )
            ],
            fetch_ms=112.4,
        )
        rep_or = ProviderReport(
            provider_id="openrouter",
            display_name="OpenRouter",
            status="ok",
            plan_tier="Pay-as-you-go",
            source="OpenRouter API",
            credits=CreditBalance(used=11.80, remaining=38.20, limit=50.0, unit="USD").finalize(),
        )

        # Round-trip through to_dict() -> from_dict()
        rehydrated = ProviderReport.from_dict(rep_claude.to_dict())
        self.assertEqual(rehydrated.provider_id, "claude")
        self.assertEqual(rehydrated.account, "work")
        self.assertEqual(rehydrated.instance_id, "claude:work")
        self.assertAlmostEqual(rehydrated.weekly.remaining_percent or 0.0, 72.0)
        self.assertAlmostEqual(rehydrated.fetch_ms or 0.0, 112.4)
        self.assertEqual(len(rehydrated.sub_metrics), 1)
        self.assertEqual(rehydrated.sub_metrics[0].name, "5h Rolling Window")

        # 1. Compact format (plain & ANSI)
        compact_plain = render_statusline([rehydrated, rep_or], fmt="compact", color_enabled=False)
        self.assertIn("●CL(work):72%", compact_plain)
        self.assertIn("●OR:$38.20", compact_plain)

        # 2. Tmux format
        tmux_line = render_statusline([rehydrated, rep_or], fmt="tmux", color_enabled=False)
        self.assertIn("#[fg=green]●#[fg=default]CL(work):#[fg=green]72%#[fg=default]", tmux_line)
        self.assertIn("#[fg=green]$38.20#[fg=default]", tmux_line)

        # 3. Plain format
        plain_line = render_statusline([rehydrated], fmt="plain", color_enabled=True)
        self.assertEqual(plain_line, ANSI_RE.sub("", plain_line))
        self.assertIn("●CL(work):72%", plain_line)

        # 4. Claude Code statusLine format with session JSON context
        cc_ctx = {
            "model": {"display_name": "Opus 4.6"},
            "context_window": {"used_percentage": 41.8},
            "cost": {"total_cost_usd": 1.25},
        }
        cc_line = render_statusline(
            [rehydrated],
            fmt="claude-code",
            claude_code_ctx=cc_ctx,
            color_enabled=False,
        )
        self.assertIn("claude:work [Max 20x]", cc_line)
        self.assertIn("7d: 72.0% (⟳4d2h)", cc_line)
        self.assertIn("5h: 84.0%", cc_line)
        self.assertIn("Opus 4.6", cc_line)
        self.assertIn("Ctx: 42%", cc_line)
        self.assertIn("Sess: $1.25", cc_line)

        # 5. Width truncation on statusline
        narrow_line = render_statusline([rehydrated, rep_or], fmt="compact", color_enabled=False, max_width=14)
        self.assertLessEqual(visible_len(narrow_line), 14)

    def test_statusline_cli_snapshot_fast_path_claude_code_and_async_refresh(self) -> None:
        import json
        import os
        import tempfile
        import time
        from contextlib import redirect_stdout
        from pathlib import Path
        from unittest.mock import patch
        from tokenmaster.cli import (
            ConfigManager,
            load_status_snapshot,
            main,
            save_status_snapshot,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / "config.json"
            cache_dir = root / "cache"
            cache_dir.mkdir()

            env = {
                "TOKENMASTER_CACHE_DIR": str(cache_dir),
                "NO_COLOR": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                cfg_mgr = ConfigManager(config_path=cfg_path)
                cfg_mgr.ensure_account("claude", "work", create_profile_dir=True)

                r_def = ProviderReport(
                    provider_id="claude",
                    display_name="Claude (Anthropic)",
                    account=None,
                    status="ok",
                    weekly=UsageWindow(used_percent=80.0, remaining_percent=20.0, resets_in="1d 2h").finalize(),
                )
                r_work = ProviderReport(
                    provider_id="claude",
                    display_name="Claude (Anthropic) (work)",
                    account="work",
                    status="ok",
                    weekly=UsageWindow(used_percent=15.0, remaining_percent=85.0, resets_in="5d 6h").finalize(),
                    sub_metrics=[
                        SubMetric(name="5h Rolling Window", used_str="10.0%", remaining_str="90.0%", used_percent=10.0)
                    ],
                )
                save_status_snapshot([r_def, r_work], replace_all=True)

                loaded, age_s, hit = load_status_snapshot(cfg_mgr, selected=["claude:work"])
                self.assertTrue(hit)
                self.assertEqual(len(loaded), 1)
                self.assertEqual(loaded[0].account, "work")
                self.assertLess(age_s, 5.0)

                # Fast-path statusline hit does NOT call fetch_providers
                with patch("tokenmaster.cli.fetch_providers") as mock_fetch:
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = main(["--config", str(cfg_path), "statusline", "-p", "claude:work", "--no-color"])
                    self.assertEqual(rc, 0)
                    mock_fetch.assert_not_called()
                    self.assertIn("●CL(work):85%", buf.getvalue())

                # Claude Code mode honors TOKENMASTER_ACTIVE_PROFILE=claude:work and stdin JSON
                stdin_payload = json.dumps(
                    {
                        "model": {"display_name": "Claude Opus 4.6"},
                        "context_window": {"used_percentage": 33},
                        "cost": {"total_cost_usd": 0.42},
                    }
                )
                with (
                    patch.dict(os.environ, {"TOKENMASTER_ACTIVE_PROFILE": "claude:work"}, clear=False),
                    patch("sys.stdin", io.StringIO(stdin_payload)),
                    patch("tokenmaster.cli.fetch_providers") as mock_fetch,
                ):
                    buf_cc = io.StringIO()
                    with redirect_stdout(buf_cc):
                        rc_cc = main(["--config", str(cfg_path), "statusline", "--claude-code", "--no-color"])
                    self.assertEqual(rc_cc, 0)
                    mock_fetch.assert_not_called()
                    out_cc = buf_cc.getvalue().strip()
                    self.assertIn("claude:work", out_cc)
                    self.assertIn("7d: 85.0% (⟳5d6h)", out_cc)
                    self.assertIn("5h: 90.0%", out_cc)
                    self.assertIn("Claude Opus 4.6", out_cc)
                    self.assertIn("Ctx: 33%", out_cc)
                    self.assertIn("Sess: $0.42", out_cc)

                # Stale snapshot + --async-refresh returns cached report immediately and spawns background refresh
                snap_path = cache_dir / "status_snapshot.json"
                snap_raw = json.loads(snap_path.read_text(encoding="utf-8"))
                old_ts = time.time() - 600.0
                snap_raw["updated_at"] = old_ts
                for ent in snap_raw["entries"].values():
                    ent["updated_at"] = old_ts
                snap_path.write_text(json.dumps(snap_raw), encoding="utf-8")

                with (
                    patch("tokenmaster.cli.fetch_providers") as mock_fetch,
                    patch("tokenmaster.cli._spawn_async_statusline_refresh", return_value=True) as mock_spawn,
                ):
                    buf_json = io.StringIO()
                    with redirect_stdout(buf_json):
                        rc_j = main(
                            [
                                "--config",
                                str(cfg_path),
                                "statusline",
                                "-p",
                                "claude:work",
                                "--max-age",
                                "60",
                                "--async-refresh",
                                "--json",
                            ]
                        )
                    self.assertEqual(rc_j, 0)
                    mock_fetch.assert_not_called()
                    mock_spawn.assert_called_once()
                    parsed_j = json.loads(buf_json.getvalue())
                    self.assertTrue(parsed_j["stale"])
                    self.assertIn("claude:work", parsed_j["providers"])

                # Partial snapshot hit (e.g. only claude cached, all providers requested) returns cached reports
                # under --no-refresh and --async-refresh without calling fetch_providers
                partial_loaded, _, full_hit = load_status_snapshot(cfg_mgr, selected=None)
                self.assertFalse(full_hit)
                self.assertEqual(len(partial_loaded), 2)

                with patch("tokenmaster.cli.fetch_providers") as mock_fetch:
                    buf_nr = io.StringIO()
                    with redirect_stdout(buf_nr):
                        rc_nr = main(["--config", str(cfg_path), "statusline", "--no-refresh", "--no-color"])
                    self.assertEqual(rc_nr, 0)
                    mock_fetch.assert_not_called()
                    self.assertIn("●CL(work):85%", buf_nr.getvalue())

                # _spawn_async_statusline_refresh injects PYTHONPATH and scopes stamps per selector
                with patch("subprocess.Popen") as mock_popen:
                    from tokenmaster.cli import _spawn_async_statusline_refresh

                    ok_codex = _spawn_async_statusline_refresh(cfg_path, ["codex"])
                    self.assertTrue(ok_codex)
                    popen_kwargs = mock_popen.call_args.kwargs
                    self.assertIn("PYTHONPATH", popen_kwargs["env"])
                    # Repeat same selector within 15s -> throttled
                    self.assertFalse(_spawn_async_statusline_refresh(cfg_path, ["codex"]))
                    # Different selector (claude:work) is NOT blocked by codex stamp
                    self.assertTrue(_spawn_async_statusline_refresh(cfg_path, ["claude:work"]))

                # --claude-code --format json outputs JSON with claude_code_session
                with (
                    patch.dict(os.environ, {"TOKENMASTER_ACTIVE_PROFILE": "claude:work"}, clear=False),
                    patch("sys.stdin", io.StringIO(stdin_payload)),
                    patch("tokenmaster.cli.fetch_providers") as mock_fetch,
                ):
                    buf_cc_json = io.StringIO()
                    with redirect_stdout(buf_cc_json):
                        rc_ccj = main(
                            [
                                "--config",
                                str(cfg_path),
                                "statusline",
                                "--claude-code",
                                "--format",
                                "json",
                                "--no-refresh",
                            ]
                        )
                    self.assertEqual(rc_ccj, 0)
                    mock_fetch.assert_not_called()
                    ccj_data = json.loads(buf_cc_json.getvalue())
                    self.assertIn("claude_code_session", ccj_data)
                    self.assertIn("claude:work", ccj_data["providers"])

    def test_statusline_adversarial_edge_cases(self) -> None:
        # 1. ProviderReport.from_dict edge cases: account="default", float strings in int, booleans in strings
        malformed_dict = {
            "provider_id": "claude",
            "display_name": "Claude",
            "account": " Default ",
            "status": "ok",
            "plan_tier": True,  # rejected boolean in string field
            "weekly": {
                "used_percent": "20.0",
                "scoped_remaining_percent": 5.0,
                "scoped_label": "Opus",
                "window_seconds": "604800.0",
                "resets_at": False,
            },
            "credits": {
                "remaining": 450,
                "unit": "credits",
                "is_unlimited": "false",  # string "false" must be False, not True
            },
        }
        rep = ProviderReport.from_dict(malformed_dict)
        self.assertIsNone(rep.account)
        self.assertEqual(rep.instance_id, "claude")
        self.assertIsNone(rep.plan_tier)
        self.assertIsNone(rep.weekly.resets_at)
        self.assertEqual(rep.weekly.window_seconds, 604800)
        self.assertAlmostEqual(rep.weekly.scoped_used_percent or 0.0, 95.0)
        self.assertFalse(rep.credits.is_unlimited)

        # 2. _compact_metric_value reflects bottleneck scoped_remaining_percent (5% -> crit)
        compact_s = render_statusline([rep], fmt="compact", color_enabled=False)
        self.assertIn("●CL:5%", compact_s)

        # 3. claude-code mode displays scoped pool and handles unlimited / non-percent credits
        cc_s = render_statusline([rep], fmt="claude-code", color_enabled=False)
        self.assertIn("7d: 80.0% (Opus: 5.0%)", cc_s)

        rep_credits_only = ProviderReport(
            provider_id="antigravity",
            display_name="Antigravity",
            account="dev#1",
            status="ok",
            credits=CreditBalance(remaining=450, unit="credits"),
        )
        # Compact badge strips spaces ("450credits") and tmux escapes "#" as "##"
        self.assertEqual(render_statusline([rep_credits_only], fmt="compact", color_enabled=False), "●AG(dev#1):450credits")
        tmux_s = render_statusline([rep_credits_only], fmt="tmux", color_enabled=False)
        self.assertIn("AG(dev##1)", tmux_s)
        self.assertIn("450credits", tmux_s)
        # Tmux max_width truncates without slicing #[...] tags
        tmux_narrow = render_statusline([rep, rep_credits_only], fmt="tmux", color_enabled=False, max_width=10)
        self.assertNotIn("#[fg=green", tmux_narrow.split("#[fg=default]")[-1])

    def test_multi_subscription_tui_beautification_and_smart_pick(self) -> None:
        import os
        from unittest.mock import patch
        from tokenmaster.ui import render_progress_bar, render_remaining_bar

        # 1. Crisp horizontal gauge bars (━ / ─) instead of vertically fusing full-height blocks (█ / ░)
        rem_bar = render_remaining_bar(75.0, width=10, color_enabled=False)
        self.assertEqual(rem_bar, "━━━━━━━━──")
        self.assertNotIn("█", rem_bar)
        self.assertNotIn("░", rem_bar)

        prog_bar = render_progress_bar(25.0, width=8, color_enabled=False)
        self.assertEqual(prog_bar, "━━──────")

        # 2. Multi-subscription dashboard with 2 Codex accounts and 2 Claude accounts
        reports = [
            ProviderReport(
                provider_id="codex",
                display_name="Codex (OpenAI)",
                account=None,
                status="ok",
                plan_tier="ChatGPT ProMax",
                weekly=UsageWindow(used_percent=100.0, remaining_percent=0.0, resets_in="1d 4h"),
                credits=CreditBalance(remaining=788.5, unit="credits"),
            ),
            ProviderReport(
                provider_id="codex",
                display_name="Codex (OpenAI) [sub2]",
                account="sub2",
                status="ok",
                plan_tier="ChatGPT ProMax",
                weekly=UsageWindow(
                    used_percent=18.0,
                    remaining_percent=82.0,
                    resets_in="5d 18h",
                    window_seconds=7 * 86400,
                ),
                sub_metrics=[
                    SubMetric(
                        name="5h Rolling Window",
                        used_str="12.0%",
                        remaining_str="88.0%",
                        resets_in="4h 12m",
                        used_percent=12.0,
                    )
                ],
                credits=CreditBalance(remaining=1000.0, unit="credits"),
            ),
            ProviderReport(
                provider_id="claude",
                display_name="Claude (Anthropic)",
                account=None,
                status="ok",
                plan_tier="Claude Max 20x",
                weekly=UsageWindow(
                    used_percent=25.0,
                    remaining_percent=75.0,
                    scoped_used_percent=4.0,
                    scoped_remaining_percent=96.0,
                    scoped_label="Fable",
                    resets_in="5d 4h",
                    window_seconds=7 * 86400,
                ),
                sub_metrics=[
                    SubMetric(
                        name="5h Rolling Window",
                        used_str="8.0%",
                        remaining_str="92.0%",
                        resets_in="3h 40m",
                        used_percent=8.0,
                    )
                ],
            ),
            ProviderReport(
                provider_id="claude",
                display_name="Claude (Anthropic) [sub2]",
                account="sub2",
                status="ok",
                plan_tier="Claude Max 20x",
                weekly=UsageWindow(
                    used_percent=62.0,
                    remaining_percent=38.0,
                    scoped_used_percent=30.0,
                    scoped_remaining_percent=70.0,
                    scoped_label="Fable",
                    resets_in="6d 2h",
                    window_seconds=7 * 86400,
                ),
                sub_metrics=[
                    SubMetric(
                        name="5h Rolling Window",
                        used_str="45.0%",
                        remaining_str="55.0%",
                        resets_in="1h 55m",
                        used_percent=45.0,
                    )
                ],
            ),
            ProviderReport(
                provider_id="antigravity",
                display_name="Antigravity (Google)",
                status="ok",
                plan_tier="Google AI Ultra",
                weekly=UsageWindow(used_percent=100.0, remaining_percent=0.0, resets_in="42m"),
                credits=CreditBalance(remaining=852, unit="credits"),
            ),
        ]

        with patch.dict(os.environ, {"TOKENMASTER_ACTIVE_PROFILE": "claude:sub2"}, clear=False):
            plain_out = render_dashboard(reports, show_detail=True, color_enabled=False)
            ansi_out = render_dashboard(reports, show_detail=True, color_enabled=True)

        # Multi-subscription default account gets [default], best accounts get ★, active env gets ▸, and parentheticals are stripped
        self.assertIn("● Codex [default]", plain_out)
        self.assertIn("● Codex [sub2] ★", plain_out)
        self.assertIn("● Claude [default] ★", plain_out)
        self.assertIn("● Claude [sub2] ▸", plain_out)
        self.assertNotIn("(OpenAI)", plain_out)
        self.assertNotIn("(Anthropic)", plain_out)
        self.assertNotIn("(Google)", plain_out)
        # Single-subscription provider does not get redundant [default]
        self.assertIn("● Antigravity ", plain_out)
        self.assertNotIn("Antigravity [default]", plain_out)

        # Tier column strips vendor prefixes (ChatGPT, Claude, Google AI) and table has no internal vertical bars
        self.assertIn("   ProMax", plain_out)
        self.assertIn("   Max 20x", plain_out)
        self.assertIn("   Ultra", plain_out)
        self.assertNotIn("┬", plain_out)
        self.assertNotIn("┼", plain_out)
        self.assertNotIn("┴", plain_out)
        self.assertNotIn("ChatGPT ProMax", plain_out)
        self.assertNotIn("Claude Max 20x", plain_out)
        self.assertNotIn("Google AI Ultra", plain_out)

        # Inline secondary pools and 5h windows in Weekly Remaining (with "Total:" replacing the old bar)
        self.assertIn("Total:  82.0% · 5h: 88%", plain_out)
        self.assertIn("Total:  75.0% · Fable: 96% · 5h: 92%", plain_out)
        self.assertIn("Total:  38.0% · Fable: 70% · 5h: 55%", plain_out)

        # Inline exhaustion ETA in Reset column for overburn account (claude:sub2)
        self.assertIn("⟳ 6d 2h · ▲ ~", plain_out)

        #Exhausted provider resetting within 6h (Antigravity 42m) highlighted in bold green
        self.assertIn(f"{Style.GREEN}{Style.BOLD}⟳ 42m{Style.RESET}", ansi_out)

        # Disambiguated default account in High usage alert and Smart Pick footer
        self.assertIn("High usage: Codex [default] (100.0%)", plain_out)
        self.assertIn("⚡ Smart Pick: codex → codex:sub2 (82% left) • claude → claude:default (75% left)", plain_out)

    def test_tui_beautification_adversarial_edge_cases(self) -> None:
        import os
        from unittest.mock import patch

        # 1. Active profile is ALSO best pick -> renders both ★ and ▸; bare provider name in TOKENMASTER_ACTIVE_PROFILE
        # 2. Non-account brackets in display_name (e.g. "Claude [Max]") are preserved, not misparsed as account pills
        # 3. In-memory account=" Default " normalizes to None (default subscription)
        r_def = ProviderReport(
            provider_id="claude",
            display_name="Claude [Max]",
            account=" Default ",
            status="ok",
            weekly=UsageWindow(
                used_percent=0.04,
                remaining_percent=99.96,  # must clamp to 99.9% / 99% left, never 100.0% / 100% left
                scoped_remaining_percent=88.04,  # rounds to 88%, not 88.0%
                scoped_label="Opus",
                resets_in="5d 0h",
            ),
            sub_metrics=[
                SubMetric(name="5h Rolling Window", used_str="50.0%", remaining_str="50.0%", used_percent=50.0)
            ],
        )
        # 4. Scoped-only report with exhausted scoped pool (0% remaining, 95%+ used) + expired resets_at with stale resets_in
        r_work = ProviderReport(
            provider_id="claude",
            display_name="Claude [Max]",
            account="work",
            status="ok",
            weekly=UsageWindow(
                scoped_used_percent=96.0,
                scoped_remaining_percent=4.0,
                scoped_label="Opus",
                resets_at="2020-01-01T00:00:00Z",
                resets_in="2d 4h",
            ),
        )
        # 5. Multi-subscription credit-backed provider (OpenRouter) in Smart Pick
        or_def = ProviderReport(
            provider_id="openrouter",
            display_name="OpenRouter",
            account=None,
            status="ok",
            credits=CreditBalance(remaining=5.0, unit="USD"),
        )
        or_sub2 = ProviderReport(
            provider_id="openrouter",
            display_name="OpenRouter",
            account="sub2",
            status="ok",
            credits=CreditBalance(remaining=38.20, unit="USD"),
        )

        with patch.dict(os.environ, {"TOKENMASTER_ACTIVE_PROFILE": " claude "}, clear=False):
            out = render_dashboard([r_def, r_work, or_def, or_sub2], color_enabled=False)

        self.assertIsNone(r_def.account)
        self.assertEqual(r_def.instance_id, "claude")
        # Both ★ (best) and ▸ (active env) appear on Claude [Max] [default]
        self.assertIn("● Claude [Max] [default] ★ ▸", out)
        # Non-account bracket [Max] preserved alongside [work]
        self.assertIn("● Claude [Max] [work]", out)
        # 99.96% remaining clamps to 99.9% (not 100.0%), 88.04% formats as 88% (not 88.0%)
        self.assertIn("Total:  99.9% · Opus: 88% · 5h: 50%", out)
        # Scoped-only report renders "Total:   4.0% · Opus: 4%" in Weekly Remaining instead of "—"
        self.assertIn("Total:   4.0% · Opus: 4%", out)
        # Expired resets_at updates stale "2d 4h" to "⟳ now"
        self.assertIn("⟳ now", out)
        self.assertNotIn("⟳ 2d 4h", out)
        # High usage alert catches scoped_used_percent >= 85%
        self.assertIn("High usage: Claude [Max] [work] (Opus 96.0%)", out)
        # Smart Pick uses weekly bottleneck (88% left, not 5h 50% left) and shows credit balance for OpenRouter
        self.assertIn("claude → claude:default (88% left)", out)
        self.assertIn("openrouter → openrouter:sub2 ($38.20 left)", out)

    def test_thermalmaster_header_full_screen_and_granular_provider_badges(self) -> None:
        r_agy = ProviderReport(
            provider_id="antigravity",
            display_name="Antigravity (Google)",
            status="ok",
            plan_tier="Google AI Ultra",
            fetch_ms=55.0,
            weekly=UsageWindow(used_percent=100.0, remaining_percent=0.0, resets_in="5d 0h"),
            credits=CreditBalance(remaining=1692.0, unit="credits"),
            sub_metrics=[
                SubMetric(name="Gemini Weekly", used_str="100.0%", remaining_str="0.0%", resets_in="5d 0h", used_percent=100.0),
                SubMetric(name="Gemini 5h", used_str="100.0%", remaining_str="0.0%", resets_in="5d 0h", used_percent=100.0),
                SubMetric(name="Claude/GPT Weekly", used_str="100.0%", remaining_str="0.0%", resets_in="5d 2h", used_percent=100.0),
                SubMetric(name="Claude/GPT 5h", used_str="100.0%", remaining_str="0.0%", resets_in="5d 2h", used_percent=100.0),
            ],
        )
        r_cur = ProviderReport(
            provider_id="cursor",
            display_name="Cursor IDE",
            status="ok",
            plan_tier="Cursor Premium",
            fetch_ms=420.0,
            weekly=UsageWindow(used_percent=100.0, remaining_percent=0.0, resets_in="6d 11h"),
            credits=CreditBalance(remaining=25.0, limit=50.0, used=25.0, unit="USD"),
            sub_metrics=[
                SubMetric(name="Plan Included Quota (Total)", used_str="100.0%", remaining_str="0.0%", resets_in="6d 11h", used_percent=100.0),
                SubMetric(name="Auto Model Bucket", used_str="100.0%", remaining_str="0.0%", resets_in="6d 11h", used_percent=100.0),
                SubMetric(name="Named/API Model Bucket", used_str="0.0%", remaining_str="100.0%", resets_in="6d 11h", used_percent=0.0),
            ],
        )
        r_grok = ProviderReport(
            provider_id="grok",
            display_name="Grok (CLI / SuperGrok)",
            status="ok",
            plan_tier="GrokPro",
            fetch_ms=310.0,
            weekly=UsageWindow(used_percent=100.0, remaining_percent=0.0, resets_in="6d 11h"),
            credits=CreditBalance(remaining=22.07, unit="USD"),
            sub_metrics=[
                SubMetric(name="Grok CLI Weekly Quota", used_str="100.0%", remaining_str="0.0%", resets_in="6d 11h", used_percent=100.0),
                SubMetric(name="Cursor Pool (via xAI)", used_str="100.0%", remaining_str="0.0%", resets_in="6d 11h", used_percent=100.0),
            ],
        )
        r_claude = ProviderReport(
            provider_id="claude",
            display_name="Claude (Anthropic)",
            status="ok",
            plan_tier="Claude Max 20x",
            fetch_ms=280.0,
            weekly=UsageWindow(
                used_percent=29.0,
                remaining_percent=71.0,
                scoped_used_percent=7.0,
                scoped_remaining_percent=93.0,
                scoped_label="Fable",
                resets_in="5d 2h",
            ),
            sub_metrics=[
                SubMetric(name="5h Session Window", used_str="0.0%", remaining_str="100.0%", used_percent=0.0),
            ],
        )

        # Render with full-width expansion (max_width=160) and full-screen watch height (screen_height=36)
        # without -d (show_detail=False): Detailed Model Pools & Sub-Windows must NOT appear.
        out = render_dashboard(
            [r_claude, r_agy, r_grok, r_cur],
            show_detail=False,
            watch_interval=5,
            color_enabled=True,
            max_width=160,
            screen_height=36,
        )
        plain_lines = [ANSI_RE.sub("", ln) for ln in out.splitlines()]
        # 1. Fit to screen_height - 1 rows (35 lines) so paint_watch_frame fills 36-row viewport without scrolling
        self.assertEqual(len(plain_lines), 35)
        # 2. Top line is thermalmaster-style header with reverse-video pill, 4/4 LIVE, Fetched in, every 5s (no hostname/QUOTA/EXHAUSTED!/best)
        self.assertIn(f"{Style.BOLD}{Style.REVERSE} tokenmaster {Style.RESET}", out.splitlines()[0])
        self.assertEqual(visible_len(out.splitlines()[0]), 160)
        self.assertIn("4/4 LIVE", plain_lines[0])
        self.assertIn("Fetched in 420ms", plain_lines[0])
        self.assertIn("every 5s", plain_lines[0])
        self.assertNotIn("QUOTA:", plain_lines[0])
        self.assertNotIn("EXHAUSTED!", plain_lines[0])
        self.assertNotIn("best ", plain_lines[0])
        # 3. Table expands to exact 160 columns and has no vertical bars (neither internal nor left/right outer edges)
        table_lines = out.splitlines()[1:9]
        self.assertEqual(len(table_lines), 8)
        for tln in table_lines:
            self.assertEqual(visible_len(tln), 160)
            self.assertNotIn("│", ANSI_RE.sub("", tln))
        plain_text = "\n".join(plain_lines)
        # 4. "Total:" prefix + granular inline badges for Antigravity, Cursor, Grok, Claude, and clean Tier / Credit headers
        self.assertIn("Total:   0.0% · Gem 7d: 0% · Gem 5h: 0% · CL/GPT 7d: 0% · CL/GPT 5h: 0%", plain_text)
        self.assertIn("Total:   0.0% · Auto: 0% · API: 100%", plain_text)
        self.assertIn("Total:   0.0% · Cursor: 0%", plain_text)
        self.assertIn("Total:  71.0% · Fable: 93% · 5h: 100%", plain_text)
        self.assertNotIn("Tier / Source", plain_text)
        self.assertNotIn("Credit Remaining", plain_text)
        self.assertIn("   Max 20x", plain_text)
        self.assertIn("   Ultra", plain_text)
        self.assertIn("   Pro", plain_text)
        self.assertIn("   Premium", plain_text)
        self.assertIn("   1,692", plain_text)
        self.assertNotIn("┬", plain_text)
        self.assertNotIn("┼", plain_text)
        self.assertNotIn("┴", plain_text)
        self.assertNotIn("1,692 credits", plain_text)
        self.assertNotIn("Claude Max 20x", plain_text)
        self.assertNotIn("Google AI Ultra", plain_text)
        self.assertNotIn("GrokPro", plain_text)
        self.assertNotIn("Cursor Premium", plain_text)
        # 5. Both titles and values in each row are color-coded in Red / Yellow / Green
        self.assertIn(f"{Style.GREEN}{Style.BOLD}Total:  71.0%{Style.RESET}", out)
        self.assertIn(f"{Style.GREEN}{Style.BOLD}Fable: 93%{Style.RESET}", out)
        self.assertIn(f"{Style.GREEN}{Style.BOLD}5h: 100%{Style.RESET}", out)
        self.assertIn(f"{Style.RED}{Style.BOLD}Total:   0.0%{Style.RESET}", out)
        self.assertIn(f"{Style.RED}{Style.BOLD}Gem 7d: 0%{Style.RESET}", out)
        self.assertIn(f"{Style.RED}{Style.BOLD}Auto: 0%{Style.RESET}", out)
        self.assertIn(f"{Style.GREEN}{Style.BOLD}API: 100%{Style.RESET}", out)
        # 6. Without -d (show_detail=False), Detailed Model Pools & Sub-Windows is NOT rendered (even in watch mode)
        self.assertNotIn("📊 Detailed Model Pools & Sub-Windows", plain_text)
        self.assertIn("tokenmaster -w 5s  •  Ctrl+C quit", plain_lines[-1])
        self.assertEqual(visible_len(out.splitlines()[-1]), 160)

        # 7. With -d (show_detail=True), Detailed Model Pools & Sub-Windows IS rendered and color-coded
        out_detail = render_dashboard(
            [r_claude, r_agy, r_grok, r_cur],
            show_detail=True,
            color_enabled=True,
            max_width=160,
        )
        plain_detail = ANSI_RE.sub("", out_detail)
        self.assertIn("📊 Detailed Model Pools & Sub-Windows", plain_detail)
        self.assertIn(f"{Style.BOLD}{Style.RED}▸ Antigravity{Style.RESET}", out_detail)
        self.assertIn(f"{Style.BOLD}{Style.GREEN}▸ Claude{Style.RESET}", out_detail)

        # 8. On narrower terminals (e.g. max_width=100 and max_width=45), top-right clock HH:MM:SS
        #    remains fully visible with 2-space right buffer and Weekly Remaining compacts gracefully
        out_100 = render_dashboard(
            [r_claude, r_agy, r_grok, r_cur],
            show_detail=False,
            watch_interval=5,
            color_enabled=False,
            max_width=100,
        )
        lines_100 = out_100.splitlines()
        self.assertEqual(visible_len(lines_100[0]), 100)
        self.assertRegex(lines_100[0], r"\d{2}:\d{2}:\d{2}  $")
        for tln in lines_100[1:9]:
            self.assertEqual(visible_len(tln), 100)
        # All 4 Antigravity pools remain visible without ellipsis truncation at 100 columns
        self.assertIn("C/G 5h:0%", out_100)

        out_45 = render_dashboard(
            [r_claude, r_agy, r_grok, r_cur],
            show_detail=False,
            watch_interval=5,
            color_enabled=False,
            max_width=45,
        )
        lines_45 = out_45.splitlines()
        self.assertEqual(visible_len(lines_45[0]), 45)
        self.assertRegex(lines_45[0], r"\d{2}:\d{2}:\d{2}  $")

        # 9. Verify _clean_tier_label word boundaries & vendor prefixes, Antigravity Other Models & _inspect_pools_cache,
        #    and Cursor omitempty 0% Auto/API buckets
        from tokenmaster.providers.antigravity import AntigravityProvider
        from tokenmaster.providers.cursor import CursorProvider
        from tokenmaster.ui import _clean_tier_label

        self.assertEqual(_clean_tier_label("Air"), "Air")
        self.assertEqual(_clean_tier_label("Antigravity Pro"), "Pro")
        self.assertEqual(_clean_tier_label("Antigravity Local"), "Local")
        self.assertEqual(_clean_tier_label("GrokPro"), "Pro")
        self.assertEqual(_clean_tier_label("OpenRouter Free"), "Free")

        self.assertEqual(AntigravityProvider._classify_model_family("Imagen 3"), ("other", "Other Models"))
        self.assertEqual(
            AntigravityProvider._inspect_pools_cache({"version": 1, "families": {"gemini": {"foo": 1}}}),
            "invalid",
        )
        self.assertEqual(
            AntigravityProvider._inspect_pools_cache(
                {
                    "version": 1,
                    "families": {
                        "gemini": {"weekly": {"rem_pct": 50.0, "reset_at": None, "updated_at": 100.0}}
                    },
                }
            ),
            "ready",
        )

        cur_prov = CursorProvider({})
        cur_rep = ProviderReport(provider_id="cursor", display_name="Cursor IDE", status="ok")
        cur_prov._apply_plan_and_on_demand(cur_rep, {"totalPercentUsed": 0.0}, None, None, None, False)
        cur_sm_names = [sm.name for sm in cur_rep.sub_metrics]
        self.assertIn("Plan Included Quota (Total)", cur_sm_names)
        self.assertIn("Auto Model Bucket", cur_sm_names)
        self.assertIn("Named/API Model Bucket", cur_sm_names)


if __name__ == "__main__":
    unittest.main()
