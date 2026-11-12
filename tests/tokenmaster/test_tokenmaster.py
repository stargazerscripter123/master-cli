"""Unit and integration tests for TokenMaster across all 6 providers."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

from tokenmaster.cli import main
from tokenmaster.config import ConfigManager
from tokenmaster.providers.antigravity import AntigravityProvider
from tokenmaster.providers.claude import ClaudeProvider
from tokenmaster.providers.codex import CodexProvider
from tokenmaster.providers.cursor import CursorProvider
from tokenmaster.providers.grok import GrokProvider
from tokenmaster.providers.openrouter import OpenRouterProvider
from tokenmaster.ui import render_dashboard, visible_len


def _make_fake_jwt(sub: str = "auth0|user_01JXYZ999") -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode())
        .decode()
        .rstrip("=")
    )
    return f"{header}.{payload}.sig"


class TestTokenMasterProviders(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.cfg_path = self.root / "config.json"
        self.cfg_mgr = ConfigManager(config_path=self.cfg_path)
        self._env = patch.dict(os.environ, {"TOKENMASTER_CACHE_DIR": str(self.root / "cache")})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self.tmp_dir.cleanup()

    def test_codex_provider_api_and_sessions(self) -> None:
        codex_home = self.root / ".codex"
        sessions_dir = codex_home / "sessions" / "2026" / "11" / "08"
        sessions_dir.mkdir(parents=True)

        auth_file = codex_home / "auth.json"
        auth_file.write_text(
            json.dumps(
                {
                    "tokens": {
                        "access_token": "ey_codex_token",
                        "account_id": "acct_123",
                    }
                }
            ),
            encoding="utf-8",
        )

        rollout_file = sessions_dir / "rollout-1.jsonl"
        rollout_file.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "model": "gpt-5",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 500_000,
                                "cached_input_tokens": 200_000,
                                "output_tokens": 50_000,
                            }
                        },
                        "rate_limits": {
                            "primary": {"used_percent": 15.0, "resets_at": 1800000000},
                            "secondary": {"used_percent": 38.5, "resets_at": 1800300000},
                            "credits": {"balance": "42.50"},
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        mock_wham = {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 12.0,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": 7200,
                },
                "secondary_window": {
                    "used_percent": 34.0,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 259200,
                },
            },
            "credits": {
                "has_credits": True,
                "unlimited": False,
                "balance": "85.25",
                "used": "14.75",
                "limit": "100.00",
            },
        }

        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            prov = CodexProvider(self.cfg_mgr.provider_cfg("codex"))
            with patch.object(prov, "http_json", return_value=mock_wham):
                rep = prov.fetch()

        self.assertEqual(rep.status, "ok")
        self.assertEqual(rep.plan_tier, "ChatGPT Pro")
        self.assertAlmostEqual(rep.weekly.used_percent or 0.0, 34.0)
        self.assertAlmostEqual(rep.weekly.remaining_percent or 0.0, 66.0)
        self.assertAlmostEqual(rep.credits.used or 0.0, 14.75)
        self.assertAlmostEqual(rep.credits.remaining or 0.0, 85.25)
        self.assertAlmostEqual(rep.credits.limit or 0.0, 100.0)

    def test_claude_provider_oauth_and_projects(self) -> None:
        claude_dir = self.root / ".claude"
        proj_dir = claude_dir / "projects" / "my-repo"
        proj_dir.mkdir(parents=True)

        cred_file = claude_dir / ".credentials.json"
        cred_file.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-ant-oat01-test",
                        "subscriptionType": "max",
                        "rateLimitTier": "default_claude_max_20x",
                    }
                }
            ),
            encoding="utf-8",
        )

        session_file = proj_dir / "sess1.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "requestId": "req_1",
                    "message": {
                        "id": "msg_1",
                        "model": "claude-sonnet-4-20250514",
                        "usage": {
                            "input_tokens": 100_000,
                            "cache_creation_input_tokens": 50_000,
                            "cache_read_input_tokens": 200_000,
                            "output_tokens": 20_000,
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        mock_oauth_usage = {
            "five_hour": {"utilization": 22.0, "resets_at": "2026-11-09T04:00:00Z"},
            "seven_day": {"utilization": 41.5, "resets_at": "2026-11-14T00:00:00Z"},
            "seven_day_fable": {"utilization": 15.0, "resets_at": "2026-11-14T00:00:00Z"},
            "seven_day_sonnet": {"utilization": 30.0, "resets_at": "2026-11-14T00:00:00Z"},
            "extra_usage": {
                "is_enabled": True,
                "monthly_limit": 5000,
                "used_credits": 1250,
                "utilization": 25.0,
            },
        }

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_dir)}):
            prov = ClaudeProvider(self.cfg_mgr.provider_cfg("claude"))
            with patch.object(prov, "http_json", return_value=mock_oauth_usage):
                rep = prov.fetch()

        self.assertEqual(rep.status, "ok")
        self.assertEqual(rep.plan_tier, "Claude Max 20x")
        self.assertAlmostEqual(rep.weekly.used_percent or 0.0, 41.5)
        self.assertAlmostEqual(rep.weekly.remaining_percent or 0.0, 58.5)
        self.assertAlmostEqual(rep.weekly.scoped_used_percent or 0.0, 15.0)
        self.assertIn("All: 41.5% | Fable: 15.0%", rep.weekly.used_str())
        self.assertIn("All: 58.5% | Fable: 85.0%", rep.weekly.remaining_str())
        self.assertAlmostEqual(rep.credits.used or 0.0, 12.50)
        self.assertAlmostEqual(rep.credits.remaining or 0.0, 37.50)
        self.assertAlmostEqual(rep.credits.limit or 0.0, 50.00)

    def test_antigravity_provider_connect_rpc(self) -> None:
        mock_rpc = {
            "userStatus": {
                "userTier": {
                    "name": "AI Premium",
                    "availableCredits": [
                        {
                            "creditType": "GOOGLE_ONE_AI",
                            "creditAmount": "380",
                            "minimumCreditAmountForUsage": "5",
                        }
                    ],
                    "totalCredits": 500,
                    "usedCredits": 120,
                },
                "cascadeModelConfigData": {
                    "clientModelConfigs": [
                        {
                            "label": "Gemini 4 Argon",
                            "quotaInfo": {
                                "remainingFraction": 0.75,
                                "resetTime": "2026-11-12T00:00:00Z",
                            },
                        },
                        {
                            "label": "Claude 4 Sonnet",
                            "quotaInfo": {
                                "remainingFraction": 0.65,
                                "resetTime": "2026-11-12T00:00:00Z",
                            },
                        },
                    ]
                },
            }
        }
        prov = AntigravityProvider(self.cfg_mgr.provider_cfg("antigravity"))
        with patch.object(prov, "_query_language_server", return_value=mock_rpc):
            rep = prov.fetch()

        self.assertEqual(rep.status, "ok")
        self.assertEqual(rep.plan_tier, "AI Premium")
        self.assertAlmostEqual(rep.weekly.used_percent or 0.0, 30.0)
        self.assertAlmostEqual(rep.weekly.remaining_percent or 0.0, 70.0)
        self.assertAlmostEqual(rep.credits.used or 0.0, 120.0)
        self.assertAlmostEqual(rep.credits.remaining or 0.0, 380.0)
        self.assertAlmostEqual(rep.credits.limit or 0.0, 500.0)
        sm_names = {sm.name: sm for sm in rep.sub_metrics}
        self.assertIn("Gemini Weekly", sm_names)
        self.assertIn("Gemini 5h", sm_names)
        self.assertIn("Claude/GPT Weekly", sm_names)
        self.assertIn("Claude/GPT 5h", sm_names)
        self.assertAlmostEqual(sm_names["Gemini Weekly"].used_percent or 0.0, 25.0)
        self.assertAlmostEqual(sm_names["Claude/GPT Weekly"].used_percent or 0.0, 35.0)

        # Follow-up RPC with a 5h short window for Gemini merges with cached 7d weekly window
        mock_rpc_5h = {
            "userStatus": {
                "userTier": {"name": "AI Premium"},
                "cascadeModelConfigData": {
                    "clientModelConfigs": [
                        {
                            "label": "Gemini 4 Argon",
                            "quotaInfo": {
                                "remainingFraction": 0.40,
                                "resetTime": "2026-11-08T21:00:00Z",
                            },
                        },
                        {
                            "label": "Claude 4 Sonnet",
                            "quotaInfo": {
                                "remainingFraction": 0.65,
                                "resetTime": "2026-11-12T00:00:00Z",
                            },
                        },
                    ]
                },
            }
        }
        with patch.object(prov, "_query_language_server", return_value=mock_rpc_5h), patch(
            "tokenmaster.providers.antigravity.parse_reset_remaining_seconds",
            side_effect=lambda r_at, r_in=None, **kw: 7200.0
            if (r_at and "21:00" in str(r_at)) or (r_in and "2h" in str(r_in))
            else 300000.0,
        ):
            rep2 = prov.fetch()
        sm_names2 = {sm.name: sm for sm in rep2.sub_metrics}
        # Gemini 5h is 60% used (40% remaining), while cached Gemini Weekly (25% used) is preserved
        self.assertAlmostEqual(sm_names2["Gemini 5h"].used_percent or 0.0, 60.0)
        self.assertAlmostEqual(sm_names2["Gemini Weekly"].used_percent or 0.0, 25.0)
        self.assertAlmostEqual(sm_names2["Claude/GPT Weekly"].used_percent or 0.0, 35.0)

    def test_grok_provider_cli_auth_and_billing(self) -> None:
        grok_home = self.root / ".grok"
        sess_dir = grok_home / "sessions" / "sess-01"
        sess_dir.mkdir(parents=True)

        (grok_home / "auth.json").write_text(
            json.dumps(
                {
                    "https://accounts.x.ai/sign-in": {
                        "key": "xai_cli_session_tok_123",
                    }
                }
            ),
            encoding="utf-8",
        )
        (sess_dir / "summary.json").write_text(
            json.dumps(
                {
                    "totalTokens": 2_500_000,
                    "costUsdTicks": 12_500_000_000,  # $1.25
                }
            ),
            encoding="utf-8",
        )

        mock_billing = {
            "subscription_tier": "SuperGrok Pro",
            "config": {
                "creditUsagePercent": 28.5,
                "currentPeriod": {
                    "start": "2026-11-05T00:00:00Z",
                    "end": "2026-11-12T00:00:00Z",
                },
                "monthlyLimit": 200.0,
                "prepaidBalance": 143.0,
                "history": [
                    {
                        "includedUsed": 57.0,
                        "totalUsed": 57.0,
                    }
                ],
            },
        }
        mock_user = {"subscription_tier": "SuperGrok Pro"}

        with patch.dict(os.environ, {"GROK_HOME": str(grok_home)}):
            prov = GrokProvider(self.cfg_mgr.provider_cfg("grok"))
            with patch.object(
                prov, "_query_grok_cli_proxy", return_value=(mock_billing, mock_user, None)
            ):
                rep = prov.fetch()

        self.assertEqual(rep.status, "ok")
        self.assertEqual(rep.plan_tier, "SuperGrok Pro")
        self.assertAlmostEqual(rep.weekly.used_percent or 0.0, 28.5)
        self.assertAlmostEqual(rep.weekly.remaining_percent or 0.0, 71.5)
        self.assertAlmostEqual(rep.credits.used or 0.0, 57.0)
        self.assertAlmostEqual(rep.credits.remaining or 0.0, 143.0)
        self.assertAlmostEqual(rep.credits.limit or 0.0, 200.0)

    def test_grok_provider_rate_limits_and_credits(self) -> None:
        grok_home = self.root / ".grok_empty"
        cfg = self.cfg_mgr.provider_cfg("grok")
        cfg["sso_token"] = "ey_grok_sso"
        cfg["credit_limit"] = 50.0
        cfg["credit_used"] = 12.0

        mock_rate_limit = {
            "windowSizeSeconds": 7200,
            "remainingQueries": 82,
            "totalQueries": 100,
        }
        with patch.dict(os.environ, {"GROK_HOME": str(grok_home)}):
            prov = GrokProvider(cfg)
            with patch.object(prov, "_query_rate_limit", return_value=mock_rate_limit):
                rep = prov.fetch()

        self.assertEqual(rep.status, "ok")
        self.assertEqual(rep.plan_tier, "SuperGrok")
        self.assertAlmostEqual(rep.weekly.used or 0.0, 18.0)
        self.assertAlmostEqual(rep.weekly.remaining or 0.0, 82.0)
        self.assertAlmostEqual(rep.weekly.limit or 0.0, 100.0)
        self.assertAlmostEqual(rep.credits.used or 0.0, 12.0)
        self.assertAlmostEqual(rep.credits.remaining or 0.0, 38.0)

    def test_cursor_provider_sqlite_and_usage_summary(self) -> None:
        import contextlib
        db_path = self.root / "state.vscdb"
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                "INSERT INTO ItemTable VALUES (?, ?)",
                ("cursorAuth/accessToken", _make_fake_jwt()),
            )
            conn.execute(
                "INSERT INTO ItemTable VALUES (?, ?)",
                ("cursorAuth/stripeMembershipType", "pro"),
            )
            conn.commit()

        cursor_home = self.root / ".cursor"
        cursor_home.mkdir(parents=True)

        cfg = self.cfg_mgr.provider_cfg("cursor")
        cfg["state_db_path"] = str(db_path)

        mock_summary = {
            "billingCycleStart": "2026-11-01T00:00:00.000Z",
            "billingCycleEnd": "2026-12-01T00:00:00.000Z",
            "membershipType": "pro",
            "individualUsage": {
                "plan": {
                    "enabled": True,
                    "used": 650,
                    "limit": 2000,
                    "remaining": 1350,
                    "totalPercentUsed": 32.5,
                },
                "onDemand": {
                    "enabled": True,
                    "used": 300,
                    "limit": 5000,
                    "remaining": 4700,
                },
            },
        }
        with patch.dict(os.environ, {"CURSOR_HOME": str(cursor_home)}):
            prov = CursorProvider(cfg)
            with patch.object(prov, "_read_keychain_secret", return_value=None):
                with patch.object(prov, "http_json", return_value=mock_summary):
                    rep = prov.fetch()

        self.assertEqual(rep.status, "ok")
        self.assertEqual(rep.plan_tier, "Cursor Pro")
        self.assertAlmostEqual(rep.credits.used or 0.0, 6.50)
        self.assertAlmostEqual(rep.credits.remaining or 0.0, 13.50)
        self.assertAlmostEqual(rep.credits.limit or 0.0, 20.00)
        self.assertIsNotNone(rep.weekly.used)
        self.assertIsNotNone(rep.weekly.remaining)

    def test_openrouter_provider_key_and_credits(self) -> None:
        cfg = self.cfg_mgr.provider_cfg("openrouter")
        cfg["api_key"] = "sk-or-v1-test"
        cfg["weekly_limit"] = 25.0
        prov = OpenRouterProvider(cfg)

        def fake_http(url: str, **kwargs: Any) -> dict[str, Any]:
            if url.endswith("/api/v1/key"):
                return {
                    "data": {
                        "is_free_tier": False,
                        "usage": 38.40,
                        "usage_daily": 1.25,
                        "usage_weekly": 8.50,
                        "usage_monthly": 19.80,
                        "limit": None,
                        "limit_remaining": None,
                        "limit_reset": None,
                    }
                }
            if url.endswith("/api/v1/credits"):
                return {
                    "data": {
                        "total_credits": 100.0,
                        "total_usage": 38.40,
                    }
                }
            return {}

        with patch.object(prov, "http_json", side_effect=fake_http):
            rep = prov.fetch()

        self.assertEqual(rep.status, "ok")
        self.assertAlmostEqual(rep.weekly.used or 0.0, 8.50)
        self.assertAlmostEqual(rep.weekly.remaining or 0.0, 16.50)
        self.assertAlmostEqual(rep.weekly.limit or 0.0, 25.0)
        self.assertAlmostEqual(rep.credits.used or 0.0, 38.40)
        self.assertAlmostEqual(rep.credits.remaining or 0.0, 61.60)
        self.assertAlmostEqual(rep.credits.limit or 0.0, 100.0)

    def test_cli_quota_and_json_output(self) -> None:
        rc = main(
            [
                "--config",
                str(self.cfg_path),
                "quota",
                "openrouter",
                "--weekly-limit",
                "40",
                "--weekly-used",
                "10",
                "--credit-limit",
                "200",
                "--credit-used",
                "50",
            ]
        )
        self.assertEqual(rc, 0)

        mgr = ConfigManager(config_path=self.cfg_path)
        prov = OpenRouterProvider(mgr.provider_cfg("openrouter"))
        rep = prov.fetch()
        self.assertAlmostEqual(rep.weekly.used or 0.0, 10.0)
        self.assertAlmostEqual(rep.weekly.remaining or 0.0, 30.0)
        self.assertAlmostEqual(rep.credits.used or 0.0, 50.0)
        self.assertAlmostEqual(rep.credits.remaining or 0.0, 150.0)

        rendered = render_dashboard([rep], show_detail=True, color_enabled=False)
        self.assertIn("◐ OpenRouter", rendered)
        self.assertNotIn("Weekly Usage", rendered)
        self.assertNotIn("Credit Usage", rendered)
        self.assertIn("Weekly Remaining", rendered)
        self.assertIn("Credit", rendered)
        self.assertNotIn("Credit Remaining", rendered)
        self.assertNotIn("Tier / Source", rendered)
        self.assertIn("$30.00 (75%)", rendered)
        self.assertIn("$150.00 (75%)", rendered)
        # Ensure all table lines (horizontal rules, header row, data rows) have identical visible length and no vertical bars
        out_lines = rendered.splitlines()
        table_lines = out_lines[1:6]
        self.assertEqual(len(table_lines), 5)
        for tln in table_lines:
            self.assertNotIn("│", tln)
        widths = {visible_len(line) for line in table_lines}
        self.assertEqual(len(widths), 1)


if __name__ == "__main__":
    unittest.main()
