"""Unit tests for TokenMaster quota threshold & auth-expiry notifications (`tokenmaster notify`)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from tokenmaster.cli import main
from tokenmaster.models import CreditBalance, ProviderReport, SubMetric, UsageWindow
from tokenmaster.notify import (
    AlertManager,
    parse_thresholds,
    sanitize_alert_text,
    send_desktop_notification,
)


class TestNotifications(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_path = self.root / "alert_state.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_parse_thresholds_validation(self) -> None:
        self.assertEqual(parse_thresholds(None), [80.0, 95.0, 100.0])
        self.assertEqual(parse_thresholds("90, 75, 90, 100"), [75.0, 90.0, 100.0])
        self.assertEqual(parse_thresholds(["50,80", "95"]), [50.0, 80.0, 95.0])

        for bad in ("", "0,80", "-10", "105", "80,", "abc", True, float("nan"), []):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_thresholds(bad)

    def test_sanitize_alert_text_scrubs_secrets_and_ansi(self) -> None:
        raw = "\x1b[31mError\x1b[0m: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.sig and sk-proj-1234567890abcdef and sso=secretcookie\n\t"
        cleaned = sanitize_alert_text(raw)
        self.assertNotIn("\x1b", cleaned)
        self.assertNotIn("sk-proj-", cleaned)
        self.assertNotIn("eyJhbGci", cleaned)
        self.assertNotIn("secretcookie", cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_weekly_5h_credit_and_auth_deduplication_lifecycle(self) -> None:
        mgr = AlertManager(self.state_path)
        sent: list[tuple[str, str]] = []

        def fake_sender(title: str, msg: str) -> bool:
            sent.append((title, msg))
            return True

        r_82 = ProviderReport(
            provider_id="claude",
            account="work",
            display_name="Claude (work)",
            status="ok",
            weekly=UsageWindow(
                used_percent=82.0,
                remaining_percent=18.0,
                resets_at="2026-11-15 09:00 UTC",
                resets_in="4d 12h",
            ).finalize(),
        )
        # Poll 1: crosses 80% -> triggers 1 alert (threshold 80%)
        ev1 = mgr.process_reports([r_82], desktop_sender=fake_sender, now_ts=1794000000.0)
        self.assertEqual(len(ev1), 1)
        self.assertEqual(ev1[0].threshold, 80.0)
        self.assertEqual(ev1[0].severity, "warn")
        self.assertEqual(len(sent), 1)
        self.assertEqual(oct(self.state_path.stat().st_mode & 0o777), oct(0o600))

        # Poll 2: 84% with 1-minute jitter on resets_at -> deduplicated (0 alerts)
        r_84 = ProviderReport(
            provider_id="claude",
            account="work",
            display_name="Claude (work)",
            status="ok",
            weekly=UsageWindow(
                used_percent=84.0,
                remaining_percent=16.0,
                resets_at="2026-11-15 09:01 UTC",
                resets_in="4d 11h",
            ).finalize(),
        )
        ev2 = mgr.process_reports([r_84], desktop_sender=fake_sender, now_ts=1794000600.0)
        self.assertEqual(len(ev2), 0)
        self.assertEqual(len(sent), 1)

        # Poll 3: escalates to 96% -> triggers critical alert for 95% threshold
        r_96 = ProviderReport(
            provider_id="claude",
            account="work",
            display_name="Claude (work)",
            status="ok",
            weekly=UsageWindow(
                used_percent=96.0,
                remaining_percent=4.0,
                resets_at="2026-11-15 09:00 UTC",
                resets_in="2d 0h",
            ).finalize(),
        )
        ev3 = mgr.process_reports([r_96], desktop_sender=fake_sender, now_ts=1794001200.0)
        self.assertEqual(len(ev3), 1)
        self.assertEqual(ev3[0].threshold, 95.0)
        self.assertEqual(ev3[0].severity, "critical")

        # Poll 4: new reset cycle (resets_at moves forward by 7 days, used=81%) -> alerts again at 80%!
        r_next_cycle = ProviderReport(
            provider_id="claude",
            account="work",
            display_name="Claude (work)",
            status="ok",
            weekly=UsageWindow(
                used_percent=81.0,
                remaining_percent=19.0,
                resets_at="2026-11-22 09:00 UTC",
                resets_in="6d 20h",
            ).finalize(),
        )
        ev4 = mgr.process_reports([r_next_cycle], desktop_sender=fake_sender, now_ts=1794600000.0)
        self.assertEqual(len(ev4), 1)
        self.assertEqual(ev4[0].threshold, 80.0)

        # Poll 5: 5h rolling window alert + Auth expired on partial report (privacy-safe)
        r_5h_and_auth = ProviderReport(
            provider_id="codex",
            display_name="Codex",
            status="partial",
            error_message="HTTP 401: Bearer sk-secret-token-9999999999 expired",
            weekly=UsageWindow(used_percent=20.0, remaining_percent=80.0).finalize(),
            sub_metrics=[
                SubMetric(
                    name="5h Rolling Window",
                    used_str="96.0%",
                    remaining_str="4.0%",
                    used_percent=96.0,
                    resets_in="45m",
                )
            ],
            credits=CreditBalance(used=90.0, remaining=10.0, limit=100.0, unit="USD"),
        )
        ev5 = mgr.process_reports([r_5h_and_auth], desktop_sender=fake_sender, now_ts=1794600100.0)
        kinds = {e.kind: e for e in ev5}
        self.assertIn("auth_error", kinds)
        self.assertIn("five_hour_quota", kinds)
        self.assertIn("credit_quota", kinds)
        self.assertNotIn("sk-secret-token", kinds["auth_error"].message)

        # Auth error is deduplicated within cooldown window
        ev5_dup = mgr.process_reports([r_5h_and_auth], desktop_sender=fake_sender, now_ts=1794600200.0)
        self.assertEqual(len(ev5_dup), 0)

        # Recovery clears auth_error state so a future auth error alerts immediately
        r_recovered = ProviderReport(
            provider_id="codex",
            display_name="Codex",
            status="ok",
            weekly=UsageWindow(used_percent=20.0, remaining_percent=80.0).finalize(),
        )
        mgr.process_reports([r_recovered], desktop_sender=fake_sender, now_ts=1794600300.0)
        ev_re_err = mgr.process_reports([r_5h_and_auth], desktop_sender=fake_sender, now_ts=1794600400.0)
        self.assertTrue(any(e.kind == "auth_error" for e in ev_re_err))

    def test_send_desktop_notification_osascript_and_notify_send(self) -> None:
        mock_proc = MagicMock(returncode=0)
        with patch("tokenmaster.notify.sys.platform", "darwin"), patch(
            "tokenmaster.notify.subprocess.run", return_value=mock_proc
        ) as mock_run:
            ok = send_desktop_notification('Title "quote"', 'Body "quote" & $HOME')
            self.assertTrue(ok)
            cmd_args = mock_run.call_args[0][0]
            self.assertEqual(cmd_args[0].endswith("osascript"), True)
            self.assertIn("on run argv", cmd_args)
            self.assertEqual(cmd_args[-2], 'Title "quote"')
            self.assertEqual(cmd_args[-1], 'Body "quote" & $HOME')

        with patch("tokenmaster.notify.sys.platform", "linux"), patch(
            "tokenmaster.notify.shutil.which", return_value="/usr/bin/notify-send"
        ), patch("tokenmaster.notify.subprocess.run", return_value=mock_proc) as mock_run_linux:
            ok_l = send_desktop_notification("Linux Title", "Linux Body")
            self.assertTrue(ok_l)
            self.assertEqual(mock_run_linux.call_args[0][0][0], "/usr/bin/notify-send")

    def test_cli_notify_dry_run_reset_and_json(self) -> None:
        cache_dir = self.root / "cache"
        cfg_path = self.root / "config.json"
        fake_reports = [
            ProviderReport(
                provider_id="claude",
                display_name="Claude",
                status="ok",
                weekly=UsageWindow(
                    used_percent=88.0,
                    remaining_percent=12.0,
                    resets_at="2026-11-15 09:00 UTC",
                    resets_in="3d 2h",
                ).finalize(),
            )
        ]

        env = {
            "TOKENMASTER_CACHE_DIR": str(cache_dir),
            "TOKENMASTER_ALERT_STATE_PATH": str(cache_dir / "alert_state.json"),
            "NO_COLOR": "1",
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "tokenmaster.cli.fetch_providers", return_value=fake_reports
        ), patch("tokenmaster.notify.send_desktop_notification", return_value=True) as mock_desk:
            # 1. --dry-run evaluates alerts without writing alert_state.json or calling desktop notification
            buf_dry = io.StringIO()
            with redirect_stdout(buf_dry):
                rc_dry = main(["--config", str(cfg_path), "notify", "--dry-run", "--json"])
            self.assertEqual(rc_dry, 0)
            dry_data = json.loads(buf_dry.getvalue())
            self.assertEqual(dry_data["triggered_count"], 1)
            self.assertFalse((cache_dir / "alert_state.json").exists())
            self.assertEqual(mock_desk.call_count, 0)

            # 2. Real run triggers alert, writes alert_state.json, and emits bell with --bell
            buf_real = io.StringIO()
            with redirect_stdout(buf_real):
                rc_real = main(["--config", str(cfg_path), "notify", "--bell"])
            self.assertEqual(rc_real, 0)
            self.assertIn("\a", buf_real.getvalue())
            self.assertIn("88%", buf_real.getvalue())
            self.assertTrue((cache_dir / "alert_state.json").is_file())
            self.assertEqual(mock_desk.call_count, 1)

            # 3. Second real run is deduplicated; --quiet produces empty stdout
            buf_quiet = io.StringIO()
            with redirect_stdout(buf_quiet):
                rc_q = main(["--config", str(cfg_path), "notify", "--quiet"])
            self.assertEqual(rc_q, 0)
            self.assertEqual(buf_quiet.getvalue(), "")

            # 4. --reset-state clears state and re-triggers alert
            buf_reset = io.StringIO()
            with redirect_stdout(buf_reset):
                rc_rst = main(["--config", str(cfg_path), "notify", "--reset-state", "--no-desktop"])
            self.assertEqual(rc_rst, 0)
            self.assertIn("88%", buf_reset.getvalue())

            # 5. Invalid flags return exit code 2
            with redirect_stderr(io.StringIO()):
                self.assertEqual(
                    main(["--config", str(cfg_path), "notify", "--dry-run", "--reset-state"]),
                    2,
                )
                self.assertEqual(
                    main(["--config", str(cfg_path), "notify", "--threshold", "150"]),
                    2,
                )

    def test_notify_adversarial_edge_cases(self) -> None:
        mgr = AlertManager(self.state_path)
        sent: list[tuple[str, str]] = []

        def fake_sender(title: str, msg: str) -> bool:
            sent.append((title, msg))
            return True

        # 1. [HIGH-01] Weekly subscription with $0.00 optional add-on credits (remaining=0, limit=None/0, used=None/0)
        # must NOT trigger a false-positive CRITICAL credit_quota alert.
        r_optional_zero_credits = ProviderReport(
            provider_id="codex",
            display_name="Codex",
            status="ok",
            weekly=UsageWindow(
                used_percent=20.0,
                remaining_percent=80.0,
                resets_at="2026-11-15 09:00 UTC",
                resets_in="5d 0h",
            ).finalize(),
            credits=CreditBalance(remaining=0.0, limit=None, used=None, unit="USD"),
        )
        ev_zero = mgr.process_reports([r_optional_zero_credits], desktop_sender=fake_sender, now_ts=1794000000.0)
        self.assertEqual(ev_zero, [])

        # In contrast, OpenRouter or a credit pool with limit>0 / used>0 at remaining=0 MUST trigger 100% alert.
        r_or_exhausted = ProviderReport(
            provider_id="openrouter",
            display_name="OpenRouter",
            status="ok",
            credits=CreditBalance(remaining=0.0, limit=10.0, used=10.0, unit="USD"),
        )
        ev_or = mgr.process_reports([r_or_exhausted], desktop_sender=fake_sender, now_ts=1794000010.0)
        self.assertEqual(len(ev_or), 1)
        self.assertEqual(ev_or[0].kind, "credit_quota")
        self.assertEqual(ev_or[0].threshold, 100.0)

        # 2. [HIGH-02] Unconfigured status with concrete auth error ("OAuth token expired (HTTP 401)")
        # must NOT be silently skipped when a configured credential fails without local fallback files.
        r_unconf_401 = ProviderReport(
            provider_id="claude",
            account="work",
            display_name="Claude (work)",
            status="unconfigured",
            error_message="OAuth token expired (HTTP 401)",
        )
        ev_401 = mgr.process_reports([r_unconf_401], desktop_sender=fake_sender, now_ts=1794000020.0)
        self.assertEqual(len(ev_401), 1)
        self.assertEqual(ev_401[0].kind, "auth_error")

        # Pure setup hint ("Not found in ~/.codex/auth.json") on unconfigured provider remains silent.
        r_unconf_setup = ProviderReport(
            provider_id="grok",
            display_name="Grok",
            status="unconfigured",
            error_message="No credentials found in ~/.grok/auth.json",
        )
        self.assertEqual(
            mgr.process_reports([r_unconf_setup], desktop_sender=fake_sender, now_ts=1794000030.0),
            [],
        )

        # 3. [HIGH-03] & [MEDIUM-05] 5h Rolling Window with resets_in (resets_at=None), multiple 5h sub-metrics,
        # gradual decay without re-alerting, and new cycle re-alert after reset_epoch passes.
        r_5h_cycle1 = ProviderReport(
            provider_id="claude",
            display_name="Claude",
            status="ok",
            weekly=UsageWindow(used_percent=30.0, remaining_percent=70.0).finalize(),
            sub_metrics=[
                SubMetric(name="5h Rolling Window (Sonnet)", used_str="50%", remaining_str="50%", used_percent=50.0, resets_in="2h 0m"),
                SubMetric(name="5h Rolling Window (Opus)", used_str="96%", remaining_str="4%", used_percent=96.0, resets_in="1h 0m"),
            ],
        )
        ev_5h_1 = mgr.process_reports([r_5h_cycle1], desktop_sender=fake_sender, now_ts=1794000100.0)
        self.assertEqual(len(ev_5h_1), 1)
        self.assertEqual(ev_5h_1[0].threshold, 95.0)

        # Gradual decay 96% -> 90% -> 84% -> 81% within the SAME 5h window:
        # at 81% (drop of 15% from peak 96%), max_threshold_notified de-escalates to 80.0 (not 0.0), so no duplicate 80% alert!
        for step_idx, pct in enumerate((90.0, 84.0, 81.0), start=1):
            r_5h_decay = ProviderReport(
                provider_id="claude",
                display_name="Claude",
                status="ok",
                weekly=UsageWindow(used_percent=30.0, remaining_percent=70.0).finalize(),
                sub_metrics=[
                    SubMetric(
                        name="5h Rolling Window (Opus)",
                        used_str=f"{pct}%",
                        remaining_str=f"{100.0 - pct}%",
                        used_percent=pct,
                        resets_in=f"{60 - step_idx * 5}m",
                    )
                ],
            )
            ev_decay = mgr.process_reports(
                [r_5h_decay], desktop_sender=fake_sender, now_ts=1794000100.0 + step_idx * 300.0
            )
            self.assertEqual(ev_decay, [])

        # After the 1h reset_epoch passes (now_ts + 4000s) and a new 5h window has resets_in="4h 30m" at 82%,
        # it detects the new cycle and alerts for 80%!
        r_5h_cycle2 = ProviderReport(
            provider_id="claude",
            display_name="Claude",
            status="ok",
            weekly=UsageWindow(used_percent=30.0, remaining_percent=70.0).finalize(),
            sub_metrics=[
                SubMetric(name="5h Rolling Window (Opus)", used_str="82%", remaining_str="18%", used_percent=82.0, resets_in="4h 30m"),
            ],
        )
        ev_5h_2 = mgr.process_reports([r_5h_cycle2], desktop_sender=fake_sender, now_ts=1794004500.0)
        self.assertEqual(len(ev_5h_2), 1)
        self.assertEqual(ev_5h_2[0].threshold, 80.0)

        # 4. [MEDIUM-01] Exhaustion ETA includes pace_status="overburn"/"fast" and excludes "now".
        r_overburn = ProviderReport(
            provider_id="grok",
            display_name="Grok",
            status="ok",
            weekly=UsageWindow(
                used_percent=85.0,
                remaining_percent=15.0,
                resets_at="2026-11-15 09:00 UTC",
                resets_in="5d 0h",
                pace_status="overburn",
                projected_exhaustion_in="18h",
            ),
        )
        ev_ob = mgr.process_reports([r_overburn], desktop_sender=fake_sender, now_ts=1794005000.0)
        self.assertEqual(len(ev_ob), 1)
        self.assertIn("ETA ~", ev_ob[0].message)
        self.assertIn("to 0%", ev_ob[0].message)

        # 5. [MEDIUM-02] Word-boundary 401/403 matching (port 14010 is NOT auth_error) & status="ok" clears error state
        r_port_err = ProviderReport(
            provider_id="antigravity",
            display_name="Antigravity",
            status="error",
            error_message="Connection refused on port 14010",
        )
        ev_port = mgr.process_reports([r_port_err], desktop_sender=fake_sender, now_ts=1794006000.0)
        self.assertEqual(len(ev_port), 1)
        self.assertEqual(ev_port[0].severity, "warn")
        self.assertIn("Provider Error", ev_port[0].title)

        # 6. [MEDIUM-03] parse_thresholds rejects values that round to 0.0 (e.g. "0.001") and accepts generators/sets
        with self.assertRaises(ValueError):
            parse_thresholds("0.001")
        self.assertEqual(parse_thresholds(x for x in (90, 80)), [80.0, 90.0])

        # 7. [MEDIUM-04] dry_run and clear_state on non-existent directory do not create lockfiles or dirs
        clean_dir = self.root / "never_created" / "alert_state.json"
        dry_mgr = AlertManager(clean_dir)
        dry_mgr.clear_state()
        dry_mgr.process_reports([r_overburn], dry_run=True, now_ts=1794007000.0)
        self.assertFalse(clean_dir.parent.exists())

        # 8. [LOW-01] sanitize_alert_text scrubs xai- keys
        self.assertNotIn("xai-abcdefgh12345678", sanitize_alert_text("key xai-abcdefgh12345678 failed"))



if __name__ == "__main__":
    unittest.main()
