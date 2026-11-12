"""Unit tests for TokenMaster SQLite local usage warehouse (warehouse.db) and `tokenmaster history`."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tokenmaster.cli import ConfigManager, main
from tokenmaster.models import CreditBalance, ProviderReport, SubMetric, UsageWindow
from tokenmaster.providers.claude import ClaudeProvider
from tokenmaster.providers.codex import CodexProvider
from tokenmaster.providers.grok import GrokProvider
from tokenmaster.ui import ANSI_RE, render_history_report, visible_len
from tokenmaster.warehouse import (
    SessionLedgerRow,
    UsageWarehouse,
    format_history_csv,
    sanitize_project_name,
)


class TestUsageWarehouse(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "warehouse.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sanitize_project_name_strips_home_paths_and_slugs(self) -> None:
        self.assertEqual(
            sanitize_project_name("-Users-alice-Library-CloudStorage-Drive-Working-Directory-tools-demo"),
            "tools-demo",
        )
        self.assertEqual(sanitize_project_name("/Users/alice/projects/my-agent"), "my-agent")
        self.assertEqual(sanitize_project_name("-home-ubuntu-code-demo"), "code-demo")
        self.assertEqual(sanitize_project_name("  \n\t  "), "unknown")

    def test_upsert_sessions_idempotency_and_aggregations(self) -> None:
        wh = UsageWarehouse(self.db_path)
        r1 = SessionLedgerRow(
            provider_id="claude",
            account_id="work",
            session_id="proj-a/sess-1#2026-11-08#claude-opus-4-6",
            day_utc="2026-11-08",
            first_ts=1794096000.0,
            last_ts=1794099600.0,
            project="/Users/alice/work/tokenmaster",
            model="claude-opus-4-6",
            input_tokens=10_000,
            output_tokens=5_000,
            cache_read_tokens=80_000,
            cache_write_tokens=10_000,
            total_tokens=105_000,
            estimated_usd=1.50,
        )
        r2 = SessionLedgerRow(
            provider_id="codex",
            account_id="default",
            session_id="2026/11/09/rollout-1",
            day_utc="2026-11-09",
            first_ts=1794182400.0,
            last_ts=1794186000.0,
            project="codex-cli",
            model="gpt-5",
            input_tokens=20_000,
            output_tokens=4_000,
            cache_read_tokens=20_000,
            cache_write_tokens=0,
            total_tokens=44_000,
            estimated_usd=0.60,
        )
        self.assertEqual(wh.upsert_sessions([r1, r2]), 2)
        self.assertEqual(oct(self.db_path.stat().st_mode & 0o777), oct(0o600))

        # Re-upserting r1 with smaller numbers preserves monotonic max; larger numbers update
        r1_smaller = SessionLedgerRow(
            provider_id="claude",
            account_id="work",
            session_id="proj-a/sess-1#2026-11-08#claude-opus-4-6",
            day_utc="2026-11-08",
            first_ts=1794096000.0,
            last_ts=1794099600.0,
            project="tokenmaster",
            model="claude-opus-4-6",
            input_tokens=1_000,
            output_tokens=500,
            cache_read_tokens=8_000,
            cache_write_tokens=1_000,
            total_tokens=10_500,
            estimated_usd=0.15,
        )
        wh.upsert_sessions([r1_smaller])

        daily = wh.query_history("daily")
        self.assertEqual(len(daily["rows"]), 2)
        self.assertEqual(daily["rows"][0]["key"], "2026-11-08")
        self.assertEqual(daily["rows"][0]["total_tokens"], 105_000)
        # Cache hit % for 2026-11-08: 80,000 / (10,000 + 80,000 + 10,000) = 80.0%
        self.assertAlmostEqual(daily["rows"][0]["cache_hit_percent"] or 0.0, 80.0, places=1)
        self.assertEqual(daily["totals"]["sessions"], 2)
        self.assertEqual(daily["totals"]["total_tokens"], 149_000)
        self.assertAlmostEqual(daily["totals"]["estimated_usd"], 2.10, places=2)

        # Weekly ISO aggregation
        weekly = wh.query_history("weekly")
        self.assertGreaterEqual(len(weekly["rows"]), 1)
        self.assertTrue(weekly["rows"][0]["key"].startswith("2026-W"))

        # Monthly aggregation
        monthly = wh.query_history("monthly")
        self.assertEqual(len(monthly["rows"]), 1)
        self.assertEqual(monthly["rows"][0]["key"], "2026-11")

        # Projects aggregation
        projects = wh.query_history("projects")
        self.assertEqual([r["key"] for r in projects["rows"]], ["tokenmaster", "codex-cli"])

        # Models aggregation + provider filter (claude:work)
        models_filtered = wh.query_history("models", selected_providers=["claude:work"])
        self.assertEqual(len(models_filtered["rows"]), 1)
        self.assertEqual(models_filtered["rows"][0]["key"], "claude-opus-4-6")

        # CSV export & formula injection protection
        r_formula = SessionLedgerRow(
            provider_id="claude",
            account_id="default",
            session_id="evil-1",
            day_utc="2026-11-09",
            first_ts=1794182400.0,
            last_ts=1794182400.0,
            project="=cmd|' /C calc'!A0",
            model="@evil-model",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            estimated_usd=0.01,
        )
        wh.upsert_sessions([r_formula])
        csv_proj = format_history_csv(wh.query_history("projects"))
        self.assertIn("'=cmd|", csv_proj)

    def test_survives_30_day_cli_log_deletion_and_renders_cli_history(self) -> None:
        cfg_path = self.root / "config.json"
        cache_dir = self.root / "cache"
        cache_dir.mkdir()
        claude_dir = self.root / ".claude"
        proj_dir = claude_dir / "projects" / "-Users-alice-work-tokenmaster"
        proj_dir.mkdir(parents=True)
        codex_dir = self.root / ".codex"
        codex_sess_dir = codex_dir / "sessions" / "2026" / "11" / "08"
        codex_sess_dir.mkdir(parents=True)
        grok_dir = self.root / ".grok"
        grok_sess_dir = grok_dir / "sessions" / "g-sess-1"
        grok_sess_dir.mkdir(parents=True)

        now = time.time()
        ts_iso = datetime.fromtimestamp(now - 3600, tz=timezone.utc).isoformat()
        claude_jsonl = proj_dir / "sess-abc.jsonl"
        claude_jsonl.write_text(
            json.dumps(
                {
                    "timestamp": ts_iso,
                    "requestId": "req-1",
                    "message": {
                        "id": "msg-1",
                        "model": "claude-sonnet-4",
                        "usage": {
                            "input_tokens": 2000,
                            "cache_read_input_tokens": 8000,
                            "cache_creation_input_tokens": 0,
                            "output_tokens": 1000,
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        codex_jsonl = codex_sess_dir / "rollout-1.jsonl"
        codex_jsonl.write_text(
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-5"}})
            + "\n"
            + json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 5000,
                                "cached_input_tokens": 3000,
                                "output_tokens": 1000,
                            }
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        grok_summary = grok_sess_dir / "summary.json"
        grok_summary.write_text(
            json.dumps({"totalTokens": 4000, "costUsdTicks": 2000000000, "model": "grok-4"}),
            encoding="utf-8",
        )

        env = {
            "TOKENMASTER_CACHE_DIR": str(cache_dir),
            "TOKENMASTER_WAREHOUSE_PATH": str(self.db_path),
            "CLAUDE_CONFIG_DIR": str(claude_dir),
            "CODEX_HOME": str(codex_dir),
            "GROK_HOME": str(grok_dir),
            "NO_COLOR": "1",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg_mgr = ConfigManager(config_path=cfg_path)
            ClaudeProvider(cfg_mgr.provider_cfg("claude"))._scan_local_projects()
            CodexProvider(cfg_mgr.provider_cfg("codex"))._scan_local_sessions()
            GrokProvider(cfg_mgr.provider_cfg("grok"))._scan_local_grok_sessions()

            # Simulate 30-day CLI log cleanup deleting all raw session files AND clearing scan caches!
            claude_jsonl.unlink()
            codex_jsonl.unlink()
            grok_summary.unlink()
            for cfile in cache_dir.glob("*.json"):
                cfile.unlink()

            # Query via `tokenmaster history` — all 3 sessions remain permanently preserved in warehouse.db!
            buf_json = io.StringIO()
            with redirect_stdout(buf_json):
                rc = main(["--config", str(cfg_path), "history", "models", "--days", "0", "--no-sync", "--json"])
            self.assertEqual(rc, 0)
            hist_data = json.loads(buf_json.getvalue())
            models_found = {r["key"]: r for r in hist_data["rows"]}
            self.assertIn("claude-sonnet-4", models_found)
            self.assertIn("gpt-5", models_found)
            self.assertIn("grok-4", models_found)
            self.assertAlmostEqual(models_found["claude-sonnet-4"]["cache_hit_percent"] or 0.0, 80.0, places=1)
            self.assertAlmostEqual(models_found["gpt-5"]["cache_hit_percent"] or 0.0, 60.0, places=1)

            # Render terminal table with width constraint
            table_str = render_history_report(hist_data, color_enabled=False, max_width=95)
            self.assertIn("claude-sonnet-4", table_str)
            self.assertIn("TOTAL", table_str)
            for line in table_str.splitlines():
                self.assertLessEqual(visible_len(line), 95)

            # Record quota snapshots and query `tokenmaster history snapshots`
            wh = UsageWarehouse(self.db_path)
            rep = ProviderReport(
                provider_id="claude",
                display_name="Claude",
                status="ok",
                weekly=UsageWindow(used_percent=25.0, remaining_percent=75.0, resets_in="4d 0h").finalize(),
                sub_metrics=[SubMetric(name="5h Rolling Window", used_str="10%", remaining_str="90%", used_percent=10.0)],
                credits=CreditBalance(remaining=42.5, unit="USD"),
            )
            self.assertEqual(wh.record_quota_snapshots([rep], now_ts=1794200000.0), 1)
            # Duplicate within 60s is throttled
            self.assertEqual(wh.record_quota_snapshots([rep], now_ts=1794200020.0), 0)
            # Changed remaining_percent within 60s is recorded
            rep.weekly.remaining_percent = 70.0
            self.assertEqual(wh.record_quota_snapshots([rep], now_ts=1794200030.0), 1)

            buf_snap = io.StringIO()
            with redirect_stdout(buf_snap):
                rc_s = main(["--config", str(cfg_path), "history", "snapshots", "--days", "0", "--no-color"])
            self.assertEqual(rc_s, 0)
            self.assertIn("75.0%", buf_snap.getvalue())
            self.assertIn("70.0%", buf_snap.getvalue())

            # Export to file
            export_file = self.root / "history_export.csv"
            buf_exp = io.StringIO()
            with redirect_stdout(buf_exp):
                rc_e = main(
                    [
                        "--config",
                        str(cfg_path),
                        "history",
                        "daily",
                        "--days",
                        "0",
                        "--no-sync",
                        "--export",
                        "csv",
                        "-o",
                        str(export_file),
                    ]
                )
            self.assertEqual(rc_e, 0)
            self.assertTrue(export_file.is_file())
            self.assertIn("day_utc,sessions,providers", export_file.read_text(encoding="utf-8"))

            # Invalid date range returns exit code 2
            err_buf = io.StringIO()
            with redirect_stderr(err_buf):
                rc_bad = main(
                    ["--config", str(cfg_path), "history", "daily", "--since", "2026-11-10", "--until", "2026-11-01"]
                )
            self.assertEqual(rc_bad, 2)

    def test_warehouse_adversarial_edge_cases(self) -> None:
        # 1. Privacy sanitization: Windows slugs, bare home dirs, hyphenated usernames
        self.assertEqual(sanitize_project_name("/Users/alice"), "home")
        self.assertEqual(sanitize_project_name("/home/alice"), "home")
        self.assertEqual(sanitize_project_name("-Users-alice"), "home")
        self.assertEqual(sanitize_project_name("-home-alice"), "home")
        self.assertEqual(sanitize_project_name("C--Users-alice"), "home")
        self.assertEqual(sanitize_project_name("C--Users-alice-projects-webapp"), "projects-webapp")
        self.assertEqual(sanitize_project_name("-Users-john-doe-repo"), "repo")

        # 2. Whitespace-prefixed CSV formula injection protection
        csv_text = format_history_csv(
            {
                "mode": "projects",
                "rows": [
                    {
                        "key": " =cmd|' /C calc'!A0",
                        "sessions": 1,
                        "providers": ["claude"],
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_read_tokens": 0,
                        "cache_write_tokens": 0,
                        "total_tokens": 15,
                        "cache_hit_percent": 0.0,
                        "estimated_usd": 0.01,
                    }
                ],
            }
        )
        self.assertIn("'\x20=cmd|", csv_text)

        # 3. Session filenames containing '#' are not collapsed across distinct files
        wh = UsageWarehouse(self.db_path)
        wh.upsert_sessions(
            [
                SessionLedgerRow(
                    provider_id="claude",
                    account_id="default",
                    session_id="proj/fix#123#2026-01-10#claude-sonnet-4",
                    day_utc="2026-01-10",
                    first_ts=1768000000.0,
                    last_ts=1768000100.0,
                    project="my-repo",
                    model="claude-sonnet-4",
                    input_tokens=100,
                    output_tokens=50,
                    total_tokens=150,
                    estimated_usd=0.01,
                ),
                SessionLedgerRow(
                    provider_id="claude",
                    account_id="default",
                    session_id="proj/fix#456#2026-01-10#claude-sonnet-4",
                    day_utc="2026-01-10",
                    first_ts=1768000200.0,
                    last_ts=1768000300.0,
                    project="my-repo",
                    model="claude-sonnet-4",
                    input_tokens=200,
                    output_tokens=50,
                    total_tokens=250,
                    estimated_usd=0.02,
                ),
            ]
        )
        q_hash = wh.query_history("daily", since_day="2026-01-01", until_day="2026-01-15")
        self.assertEqual(q_hash["totals"]["sessions"], 2)

        # 4. Empty snapshots query returns {"snapshots": 0}, corrupt DB does not crash
        missing_wh = UsageWarehouse(self.root / "nonexistent.db")
        empty_snap = missing_wh.query_history("snapshots")
        self.assertEqual(empty_snap["totals"], {"snapshots": 0})

        corrupt_db = self.root / "corrupt?test#1.db"
        corrupt_db.write_bytes(b"not a valid sqlite file")
        corrupt_wh = UsageWarehouse(corrupt_db)
        self.assertEqual(corrupt_wh.inspect_status(), (False, 0, 0))
        self.assertEqual(corrupt_wh.query_history("daily")["rows"], [])

        # 5. Backfill after partial scan + >256-turn cross-file dedup + CLI date/export validation
        cache_dir = self.root / "cache_adv"
        claude_dir = self.root / ".claude_adv"
        codex_dir = self.root / ".codex_adv"
        grok_dir = self.root / ".grok_adv"
        cfg_path = self.root / "config_adv.json"
        adv_db = cache_dir / "warehouse.db"

        now = time.time()
        old_45d = now - 45 * 86400
        old_20d = now - 20 * 86400
        recent_1h = now - 3600

        # Claude: old (>30d) file + recent shared turn + >256-turn file containing the shared turn + trailing dup file
        c_proj = claude_dir / "projects" / "demo"
        c_proj.mkdir(parents=True)
        c_old = c_proj / "00_old.jsonl"
        c_old.write_text(
            json.dumps(
                {
                    "timestamp": datetime.fromtimestamp(old_45d, tz=timezone.utc).isoformat(),
                    "requestId": "req-old-45d",
                    "message": {
                        "id": "msg-old-45d",
                        "model": "claude-opus-4-6",
                        "usage": {"input_tokens": 500, "output_tokens": 500},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        os.utime(c_old, (old_45d, old_45d))

        shared_line = json.dumps(
            {
                "timestamp": datetime.fromtimestamp(recent_1h, tz=timezone.utc).isoformat(),
                "requestId": "req-shared",
                "message": {
                    "id": "msg-shared",
                    "model": "claude-sonnet-4",
                    "usage": {"input_tokens": 1000, "output_tokens": 0},
                },
            }
        )
        (c_proj / "01_a.jsonl").write_text(shared_line + "\n", encoding="utf-8")
        big_lines = [shared_line]
        for idx in range(260):
            big_lines.append(
                json.dumps(
                    {
                        "timestamp": datetime.fromtimestamp(recent_1h, tz=timezone.utc).isoformat(),
                        "requestId": f"req-big-{idx}",
                        "message": {
                            "id": f"msg-big-{idx}",
                            "model": "claude-sonnet-4",
                            "usage": {"input_tokens": 10, "output_tokens": 0},
                        },
                    }
                )
            )
        (c_proj / "02_big.jsonl").write_text("\n".join(big_lines) + "\n", encoding="utf-8")
        # 03_z.jsonl duplicates msg-big-0 from 02_big.jsonl
        (c_proj / "03_z.jsonl").write_text(big_lines[1] + "\n", encoding="utf-8")

        # Codex: old (20d > 7d) session file
        codex_sess_dir = codex_dir / "sessions" / "2026"
        codex_sess_dir.mkdir(parents=True)
        codex_old = codex_sess_dir / "old_rollout.jsonl"
        codex_old.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 4000,
                                "cached_input_tokens": 1000,
                                "output_tokens": 1000,
                            }
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        os.utime(codex_old, (old_20d, old_20d))

        # Grok: root-level summary.json in ~/.grok/sessions/summary.json
        grok_sess_dir = grok_dir / "sessions"
        grok_sess_dir.mkdir(parents=True)
        (grok_sess_dir / "summary.json").write_text(
            json.dumps({"totalTokens": 777, "total_cost_usd": 0.05, "model": "grok-3"}),
            encoding="utf-8",
        )

        env = {
            "TOKENMASTER_CACHE_DIR": str(cache_dir),
            "TOKENMASTER_WAREHOUSE_PATH": str(adv_db),
            "CLAUDE_CONFIG_DIR": str(claude_dir),
            "CODEX_HOME": str(codex_dir),
            "GROK_HOME": str(grok_dir),
            "NO_COLOR": "1",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg_mgr = ConfigManager(config_path=cfg_path)
            # Step A: Simulate `tokenmaster status` scanning only <30d Claude and <7d Codex
            s_c1 = ClaudeProvider(cfg_mgr.provider_cfg("claude"))._scan_local_projects(include_older_than_30d=False)
            s_c2 = ClaudeProvider(cfg_mgr.provider_cfg("claude"))._scan_local_projects(include_older_than_30d=False)
            # Warm scan must match cold scan (1000 from 01_a + 260*10 from 02_big, 03_z deduplicated)
            self.assertEqual(s_c1["tokens_7d"], 3600)
            self.assertEqual(s_c2["tokens_7d"], 3600)
            CodexProvider(cfg_mgr.provider_cfg("codex"))._scan_local_sessions(include_older_than_7d=False)

            # Now run `tokenmaster history models --days 0 --json`:
            # Even though warehouse.db already had rows from Step A, it must backfill >30d Claude (claude-opus-4-6),
            # >7d Codex (gpt-5), and Grok (grok-3)!
            buf_m = io.StringIO()
            with redirect_stdout(buf_m):
                rc_m = main(["--config", str(cfg_path), "history", "models", "--days", "0", "--json"])
            self.assertEqual(rc_m, 0)
            m_data = json.loads(buf_m.getvalue())
            by_model = {r["key"]: r for r in m_data["rows"]}
            self.assertIn("claude-opus-4-6", by_model)
            self.assertIn("claude-sonnet-4", by_model)
            self.assertEqual(by_model["claude-sonnet-4"]["total_tokens"], 3600)
            self.assertIn("gpt-5", by_model)
            self.assertIn("grok-3", by_model)

            # Verify FileScanCache was NOT polluted with >30d Claude or >7d Codex files
            claude_cache_raw = json.loads((cache_dir / "claude_projects_scan.json").read_text(encoding="utf-8"))
            self.assertNotIn(str(c_old), claude_cache_raw.get("files", {}))
            codex_cache_file = cache_dir / "codex_sessions_scan.json"
            if codex_cache_file.exists():
                codex_cache_raw = json.loads(codex_cache_file.read_text(encoding="utf-8"))
                self.assertNotIn(str(codex_old), codex_cache_raw.get("files", {}))

            # Verify `--until` >30d ago works without `--days`
            old_until_day = datetime.fromtimestamp(old_45d + 86400, tz=timezone.utc).strftime("%Y-%m-%d")
            buf_u = io.StringIO()
            with redirect_stdout(buf_u):
                rc_u = main(
                    ["--config", str(cfg_path), "history", "daily", "--until", old_until_day, "--no-sync", "--json"]
                )
            self.assertEqual(rc_u, 0)
            u_data = json.loads(buf_u.getvalue())
            self.assertEqual(len(u_data["rows"]), 1)

            # Verify `--days` + `--since` conflict and `--csv` + `--json` conflict return exit code 2
            with redirect_stderr(io.StringIO()):
                self.assertEqual(
                    main(["--config", str(cfg_path), "history", "--days", "7", "--since", "2026-01-01"]),
                    2,
                )
                self.assertEqual(
                    main(["--config", str(cfg_path), "history", "--csv", "--json"]),
                    2,
                )

            # Verify narrow max_width (15) and non-positive max_width (0) in render_history_report
            narrow = render_history_report(m_data, color_enabled=False, max_width=15)
            for line in narrow.splitlines():
                self.assertLessEqual(visible_len(line), 15)
            zero_w = render_history_report(m_data, color_enabled=False, max_width=0)
            self.assertIn("claude-sonnet-4", zero_w)


if __name__ == "__main__":
    unittest.main()
