"""Unit tests for multi-subscription profile sandboxes and provider isolation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tokenmaster.config import (
    ConfigManager,
    get_account_profile_dir,
    get_profiles_dir,
    parse_provider_spec,
    validate_account_name,
)
from tokenmaster.providers import fetch_providers
from tokenmaster.providers.antigravity import AntigravityProvider
from tokenmaster.providers.claude import (
    ClaudeProvider,
    claude_keychain_service_name_for_dir,
    claude_keychain_service_names,
)
from tokenmaster.providers.codex import CodexProvider
from tokenmaster.providers.cursor import CursorProvider
from tokenmaster.providers.grok import GrokProvider
from tokenmaster.providers.openrouter import OpenRouterProvider


class TestAccountValidationAndSpecs(unittest.TestCase):
    def test_validate_account_name_accepts_and_lowercases_valid_identifiers(self) -> None:
        self.assertEqual(validate_account_name("work"), "work")
        self.assertEqual(validate_account_name("Work"), "work")
        self.assertEqual(validate_account_name("  Team-2_Prod.A  "), "team-2_prod.a")

    def test_validate_account_name_rejects_traversal_and_reserved(self) -> None:
        for bad in ("", "   ", "default", "DEFAULT", "all", "none", "auto", "AUTO", "../etc", "a/b", "a:b", ".hidden", "-dash"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_account_name(bad)

    def test_parse_provider_spec(self) -> None:
        self.assertEqual(parse_provider_spec("claude"), ("claude", None, False))
        self.assertEqual(parse_provider_spec("Claude:default"), ("claude", None, True))
        self.assertEqual(parse_provider_spec("claude:Work"), ("claude", "work", True))
        for bad_spec in ("claude:../bad", "claude:", "claude:   ", "claude:all", "claude:auto"):
            with self.subTest(bad_spec=bad_spec):
                with self.assertRaises(ValueError):
                    parse_provider_spec(bad_spec)


class TestConfigManagerMultiAccount(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env_patch = patch.dict(
            os.environ,
            {
                "TOKENMASTER_CONFIG_DIR": str(self.root / "cfg"),
                "TOKENMASTER_CACHE_DIR": str(self.root / "cache"),
                "TOKENMASTER_PROFILES_DIR": str(self.root / "profiles"),
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_account_cfg_does_not_leak_default_credentials_or_env(self) -> None:
        mgr = ConfigManager()
        mgr.set_provider_field("claude", "oauth_token", "default-secret-tok")
        mgr.set_provider_field("claude", "api_key", "sk-ant-default")

        with patch.dict(os.environ, {"CLAUDE_OAUTH_TOKEN": "env-secret-tok", "ANTHROPIC_API_KEY": "env-api-key"}):
            default_cfg = mgr.account_cfg("claude", None)
            work_cfg = mgr.account_cfg("claude", "work")

        self.assertEqual(default_cfg["oauth_token"], "env-secret-tok")
        self.assertEqual(default_cfg["api_key"], "env-api-key")
        self.assertIsNone(default_cfg["account_name"])

        self.assertIsNone(work_cfg["oauth_token"])
        self.assertIsNone(work_cfg["api_key"])
        self.assertEqual(work_cfg["account_name"], "work")
        self.assertEqual(work_cfg["profile_dir"], str(get_account_profile_dir("claude", "work")))

    def test_list_accounts_combines_config_and_autodiscovered_profiles(self) -> None:
        mgr = ConfigManager()
        pdir_work = mgr.ensure_account("codex", "work")
        self.assertTrue(pdir_work.is_dir())

        # Create an unregistered but non-empty profile directory on disk
        pdir_side = get_profiles_dir("codex") / "sideproject"
        pdir_side.mkdir(parents=True)
        (pdir_side / "auth.json").write_text('{"access_token": "tok-side"}', encoding="utf-8")

        # Create an empty directory on disk (should not be auto-discovered)
        (get_profiles_dir("codex") / "empty_dir").mkdir(parents=True)

        accts = mgr.list_accounts("codex")
        names = [a for a, _ in accts]
        self.assertEqual(names, [None, "work", "sideproject"])

        # Removing sideproject without purge should add it to ignored_profiles so it stops being tracked
        existed, _ = mgr.remove_account("codex", "sideproject", purge=False)
        self.assertTrue(existed)
        self.assertTrue(pdir_side.exists())
        self.assertEqual([a for a, _ in mgr.list_accounts("codex")], [None, "work"])

        # Re-adding sideproject re-enables it
        mgr.ensure_account("codex", "sideproject")
        self.assertEqual([a for a, _ in mgr.list_accounts("codex")], [None, "work", "sideproject"])

        # Removing with purge=True deletes the directory on disk
        mgr.remove_account("codex", "sideproject", purge=True)
        self.assertFalse(pdir_side.exists())
        self.assertEqual([a for a, _ in mgr.list_accounts("codex")], [None, "work"])


class TestClaudeMultiAccountIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env_patch = patch.dict(
            os.environ,
            {
                "TOKENMASTER_CONFIG_DIR": str(self.root / "cfg"),
                "TOKENMASTER_CACHE_DIR": str(self.root / "cache"),
                "TOKENMASTER_PROFILES_DIR": str(self.root / "profiles"),
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_claude_keychain_service_name_matches_cli_algorithm(self) -> None:
        p = Path("/home/user/.config/tokenmaster/profiles/claude/work")
        expected_hash = hashlib.sha256(
            unicodedata.normalize("NFC", str(p)).encode("utf-8")
        ).hexdigest()[:8]
        self.assertEqual(
            claude_keychain_service_name_for_dir(p),
            f"Claude Code-credentials-{expected_hash}",
        )
        isolated_svcs = claude_keychain_service_names(p, is_isolated_profile=True)
        self.assertIn(f"Claude Code-credentials-{expected_hash}", isolated_svcs)
        self.assertNotIn("Claude Code-credentials", isolated_svcs)

        default_svcs = claude_keychain_service_names(Path.home() / ".claude", is_isolated_profile=False)
        self.assertEqual(default_svcs, ["Claude Code-credentials"])

    def test_two_claude_profiles_keep_separate_scan_caches_and_credentials(self) -> None:
        dir_a = self.root / "profiles" / "claude" / "acct_a"
        dir_b = self.root / "profiles" / "claude" / "acct_b"
        (dir_a / "projects" / "p1").mkdir(parents=True)
        (dir_b / "projects" / "p2").mkdir(parents=True)

        (dir_a / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "tok-a", "subscriptionType": "max", "rateLimitTier": "20x"}}),
            encoding="utf-8",
        )
        (dir_b / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "tok-b", "subscriptionType": "pro"}}),
            encoding="utf-8",
        )

        turn_a = {
            "timestamp": "2099-01-01T00:00:00Z",
            "requestId": "req-a",
            "message": {
                "id": "msg-a",
                "model": "claude-sonnet-4",
                "usage": {"input_tokens": 1000, "output_tokens": 500},
            },
        }
        turn_b = {
            "timestamp": "2099-01-01T00:00:00Z",
            "requestId": "req-b",
            "message": {
                "id": "msg-b",
                "model": "claude-sonnet-4",
                "usage": {"input_tokens": 4000, "output_tokens": 2000},
            },
        }
        (dir_a / "projects" / "p1" / "s.jsonl").write_text(json.dumps(turn_a) + "\n", encoding="utf-8")
        (dir_b / "projects" / "p2" / "s.jsonl").write_text(json.dumps(turn_b) + "\n", encoding="utf-8")

        prov_a = ClaudeProvider({"account_name": "acct_a", "profile_dir": str(dir_a)})
        prov_b = ClaudeProvider({"account_name": "acct_b", "profile_dir": str(dir_b)})

        def fake_http(self_prov: ClaudeProvider, url: str, headers: dict | None = None, **_: object) -> dict:
            auth = (headers or {}).get("Authorization", "")
            if "tok-a" in auth:
                return {"seven_day": {"utilization": 15.0, "resets_at": "2099-01-08T00:00:00Z"}}
            if "tok-b" in auth:
                return {"seven_day": {"utilization": 72.5, "resets_at": "2099-01-08T00:00:00Z"}}
            raise AssertionError(f"Unexpected auth header: {auth}")

        with patch.object(ClaudeProvider, "http_json", new=fake_http):
            rep_a = prov_a.fetch()
            rep_b = prov_b.fetch()

        self.assertEqual(rep_a.instance_id, "claude:acct_a")
        self.assertEqual(rep_a.display_name, "Claude (Anthropic) [acct_a]")
        self.assertEqual(rep_a.plan_tier, "Claude Max 20x")
        self.assertAlmostEqual(rep_a.weekly.used_percent or 0.0, 15.0)
        self.assertAlmostEqual(rep_a.weekly.remaining_percent or 0.0, 85.0)

        self.assertEqual(rep_b.instance_id, "claude:acct_b")
        self.assertEqual(rep_b.display_name, "Claude (Anthropic) [acct_b]")
        self.assertEqual(rep_b.plan_tier, "Claude Pro")
        self.assertAlmostEqual(rep_b.weekly.used_percent or 0.0, 72.5)
        self.assertAlmostEqual(rep_b.weekly.remaining_percent or 0.0, 27.5)

        # Verify their scan caches are separate files and neither pruned the other
        cache_files = sorted(p.name for p in (self.root / "cache").glob("claude_projects_scan*.json"))
        self.assertEqual(len(cache_files), 2)


class TestCursorGrokCodexMultiAccountIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env_patch = patch.dict(
            os.environ,
            {
                "TOKENMASTER_CONFIG_DIR": str(self.root / "cfg"),
                "TOKENMASTER_CACHE_DIR": str(self.root / "cache"),
                "TOKENMASTER_PROFILES_DIR": str(self.root / "profiles"),
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_cursor_isolated_profile_ignores_global_keychain_and_persists_refresh(self) -> None:
        pdir = self.root / "profiles" / "cursor" / "work"
        pdir.mkdir(parents=True)

        # Unauthenticated isolated profile must NOT call _read_keychain_secret
        prov_empty = CursorProvider({"account_name": "work", "profile_dir": str(pdir)})
        with patch.object(CursorProvider, "_read_keychain_secret") as mock_kc:
            rep_empty = prov_empty.fetch()
            mock_kc.assert_not_called()
        self.assertEqual(rep_empty.status, "unconfigured")
        self.assertEqual(rep_empty.instance_id, "cursor:work")

        # Write an expired JWT + refreshToken into <profile_dir>/auth.json
        auth_path = pdir / "auth.json"
        auth_path.write_text(
            json.dumps({"accessToken": "old-tok", "refreshToken": "rt-work-123"}),
            encoding="utf-8",
        )
        prov_work = CursorProvider({"account_name": "work", "profile_dir": str(pdir)})

        def fake_http(
            self_prov: CursorProvider,
            url: str,
            method: str = "GET",
            headers: dict | None = None,
            payload: dict | None = None,
            **_: object,
        ) -> dict:
            if url.endswith("/oauth/token"):
                self.assertEqual((payload or {}).get("refresh_token"), "rt-work-123")
                return {"access_token": "fresh-work-tok"}
            auth = (headers or {}).get("Authorization", "")
            if auth == "Bearer fresh-work-tok":
                if "GetCurrentPeriodUsage" in url:
                    return {"planUsage": {"totalPercentUsed": 40.0}, "billingCycleEnd": "2099-01-08T00:00:00Z"}
                if "GetPlanInfo" in url:
                    return {"planInfo": {"planName": "Pro"}}
                return {}
            raise RuntimeError("HTTP 401: Unauthorized")

        with (
            patch.object(CursorProvider, "_is_jwt_expired", side_effect=lambda t: t == "old-tok"),
            patch.object(CursorProvider, "_read_keychain_secret") as mock_kc,
            patch.object(CursorProvider, "http_json", new=fake_http),
        ):
            rep_work = prov_work.fetch()
            mock_kc.assert_not_called()

        self.assertEqual(rep_work.status, "ok")
        self.assertAlmostEqual(rep_work.weekly.remaining_percent or 0.0, 60.0)
        persisted = json.loads(auth_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["accessToken"], "fresh-work-tok")

    def test_fetch_providers_orchestrates_multiple_accounts_and_selectors(self) -> None:
        mgr = ConfigManager()
        mgr.set_provider_field("openrouter", "api_key", "or-default-key")
        mgr.set_account_field("openrouter", "team", "api_key", "or-team-key")

        def fake_or_http(self_prov: OpenRouterProvider, url: str, headers: dict | None = None, **_: object) -> dict:
            auth = (headers or {}).get("Authorization", "")
            if "or-default-key" in auth:
                if url.endswith("/credits"):
                    return {"data": {"total_credits": 50.0, "total_usage": 10.0}}
                return {"data": {"usage_weekly": 2.0, "limit": 20.0, "limit_remaining": 18.0, "limit_reset": "weekly"}}
            if "or-team-key" in auth:
                if url.endswith("/credits"):
                    return {"data": {"total_credits": 200.0, "total_usage": 50.0}}
                return {"data": {"usage_weekly": 25.0, "limit": 100.0, "limit_remaining": 75.0, "limit_reset": "weekly"}}
            raise AssertionError(f"Unexpected auth: {auth}")

        with patch.object(OpenRouterProvider, "http_json", new=fake_or_http):
            all_or = fetch_providers(mgr, selected=["openrouter"])
            only_team = fetch_providers(mgr, selected=["openrouter:team"])
            only_default = fetch_providers(mgr, selected=["openrouter:default"])

        self.assertEqual([r.instance_id for r in all_or], ["openrouter", "openrouter:team"])
        self.assertAlmostEqual(all_or[0].credits.remaining or 0.0, 40.0)
        self.assertAlmostEqual(all_or[1].credits.remaining or 0.0, 150.0)

        self.assertEqual([r.instance_id for r in only_team], ["openrouter:team"])
        self.assertEqual([r.instance_id for r in only_default], ["openrouter"])

    def test_empty_or_missing_profile_dir_never_leaks_global_dirs(self) -> None:
        for empty_val in (None, "", "   "):
            c_prov = ClaudeProvider({"account_name": "work", "profile_dir": empty_val})
            self.assertEqual(c_prov._claude_dir(), get_account_profile_dir("claude", "work"))

            cx_prov = CodexProvider({"account_name": "work", "profile_dir": empty_val})
            self.assertEqual(cx_prov._codex_home(), get_account_profile_dir("codex", "work"))

            g_prov = GrokProvider({"account_name": "work", "profile_dir": empty_val})
            self.assertEqual(g_prov._grok_home(), get_account_profile_dir("grok", "work"))

            cu_prov = CursorProvider({"account_name": "work", "profile_dir": empty_val})
            self.assertEqual(cu_prov._cursor_home(), get_account_profile_dir("cursor", "work"))

            ag_prov = AntigravityProvider({"account_name": "work", "profile_dir": empty_val})
            self.assertEqual(ag_prov._antigravity_dir(), get_account_profile_dir("antigravity", "work"))

    def test_active_profile_env_var_does_not_hijack_default_account(self) -> None:
        mgr = ConfigManager()
        work_dir = mgr.ensure_account("claude", "work")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(work_dir)}):
            default_prov = ClaudeProvider(mgr.account_cfg("claude", None))
            self.assertEqual(default_prov._claude_dir(), Path.home() / ".claude")

    def test_remove_account_purge_refuses_to_delete_global_or_external_dirs(self) -> None:
        mgr = ConfigManager()
        fake_global = Path.home() / ".claude"
        with self.assertRaises(ValueError):
            mgr.set_account_field("claude", "work", "profile_dir", str(fake_global))

        # Even if config.json is tampered with directly, remove_account(purge=True) never deletes external dirs
        external_dir = self.root / "external_custom_dir"
        external_dir.mkdir(parents=True)
        (external_dir / "precious.txt").write_text("keep", encoding="utf-8")
        mgr.data["providers"]["claude"]["accounts"] = {
            "work": {"enabled": True, "profile_dir": str(external_dir)}
        }
        mgr.remove_account("claude", "work", purge=True)
        self.assertTrue((external_dir / "precious.txt").is_file())

    def test_antigravity_discovery_boundary_matching_and_default_isolation(self) -> None:
        pdir_work = get_account_profile_dir("antigravity", "work")
        pdir_work2 = get_account_profile_dir("antigravity", "work-2")
        fake_ps = (
            f"101 /Applications/Antigravity/language_server --csrf_token tok-work2 --user_data_dir {pdir_work2}\n"
            f"102 /Applications/Antigravity/language_server --csrf_token tok-default\n"
        )
        fake_lsof_101 = "language_ 101 user 10u IPv4 0x0 0t0 TCP 127.0.0.1:50101 (LISTEN)\n"
        fake_lsof_102 = "language_ 102 user 10u IPv4 0x0 0t0 TCP 127.0.0.1:50102 (LISTEN)\n"

        def fake_run(cmd: list[str], **_: object) -> MagicMock:
            m = MagicMock()
            m.returncode = 0
            if cmd[0] == "ps":
                m.stdout = fake_ps
            elif "101" in cmd:
                m.stdout = fake_lsof_101
            elif "102" in cmd:
                m.stdout = fake_lsof_102
            else:
                m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=fake_run):
            # 'work' must NOT match 'work-2' even though pdir_work is a prefix of pdir_work2
            prov_work = AntigravityProvider({"account_name": "work", "profile_dir": str(pdir_work)})
            self.assertEqual(prov_work._discover_language_server(), [])

            # 'work-2' matches PID 101
            prov_work2 = AntigravityProvider({"account_name": "work-2", "profile_dir": str(pdir_work2)})
            self.assertEqual(prov_work2._discover_language_server(), [(50101, "tok-work2")])

            # Default account skips PID 101 (which belongs to get_profiles_dir()) and only matches PID 102
            prov_default = AntigravityProvider({})
            self.assertEqual(prov_default._discover_language_server(), [(50102, "tok-default")])

    def test_include_default_false_and_disabled_accounts_respected(self) -> None:
        mgr = ConfigManager()
        mgr.set_provider_field("openrouter", "include_default", False)
        mgr.set_account_field("openrouter", "team", "enabled", False)
        self.assertEqual(fetch_providers(mgr, selected=["openrouter"]), [])

        with self.assertRaises(ValueError):
            fetch_providers(mgr, selected=["openrouter:../bad"])


class TestMultiAccountCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env_patch = patch.dict(
            os.environ,
            {
                "TOKENMASTER_CONFIG_DIR": str(self.root / "cfg"),
                "TOKENMASTER_CACHE_DIR": str(self.root / "cache"),
                "TOKENMASTER_PROFILES_DIR": str(self.root / "profiles"),
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_cli_account_add_list_quota_auth_and_masked_config(self) -> None:
        import io
        from contextlib import redirect_stdout
        from tokenmaster.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["account", "add", "openrouter", "team", "--api-key", "sk-or-v1-secret-9999"])
        self.assertEqual(rc, 0)

        with redirect_stdout(buf):
            rc = main(["quota", "openrouter:team", "--weekly-limit", "120", "--weekly-unit", "USD"])
        self.assertEqual(rc, 0)

        with redirect_stdout(buf):
            rc = main(["auth", "claude:work", "--token", "sk-ant-oat01-verysecret1234"])
        self.assertEqual(rc, 0)

        cfg_out = io.StringIO()
        with redirect_stdout(cfg_out):
            rc = main(["config", "show"])
        self.assertEqual(rc, 0)
        rendered_cfg = cfg_out.getvalue()
        self.assertNotIn("sk-or-v1-secret-9999", rendered_cfg)
        self.assertNotIn("sk-ant-oat01-verysecret1234", rendered_cfg)
        self.assertIn("sk-o...9999", rendered_cfg)
        self.assertIn("sk-a...1234", rendered_cfg)

        list_out = io.StringIO()
        with redirect_stdout(list_out):
            rc = main(["account", "list", "openrouter"])
        self.assertEqual(rc, 0)
        self.assertIn("openrouter:team", list_out.getvalue())

    def test_cli_env_named_and_default_roundtrip(self) -> None:
        import io
        from contextlib import redirect_stdout
        from tokenmaster.cli import main

        out_work = io.StringIO()
        with redirect_stdout(out_work):
            rc = main(["env", "claude", "work"])
        self.assertEqual(rc, 0)
        work_txt = out_work.getvalue()
        self.assertIn("unset CLAUDE_SECURESTORAGE_CONFIG_DIR", work_txt)
        self.assertIn("export CLAUDE_CONFIG_DIR=", work_txt)
        self.assertIn("export TOKENMASTER_ACTIVE_PROFILE=claude:work", work_txt)

        out_def = io.StringIO()
        with redirect_stdout(out_def):
            rc = main(["env", "claude", "default"])
        self.assertEqual(rc, 0)
        def_txt = out_def.getvalue()
        self.assertIn("unset CLAUDE_CONFIG_DIR", def_txt)
        self.assertIn("unset CLAUDE_SECURESTORAGE_CONFIG_DIR", def_txt)
        self.assertIn("export TOKENMASTER_ACTIVE_PROFILE=claude:default", def_txt)

    def test_cli_exec_supports_colon_positional_and_flag_syntax(self) -> None:
        from tokenmaster.cli import main

        captured_calls: list[tuple[list[str], dict[str, str]]] = []

        def fake_run(cmd: list[str], env: dict[str, str] | None = None, **_: object) -> MagicMock:
            captured_calls.append((list(cmd), dict(env or {})))
            m = MagicMock()
            m.returncode = 0
            return m

        with (
            patch("tokenmaster.cli._resolve_cli_binary", return_value="/usr/local/bin/claude"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            rc1 = main(["exec", "claude:work", "--", "-p", "hello"])
            rc2 = main(["exec", "claude", "work", "--", "-p", "world"])
            rc3 = main(["exec", "-a", "work", "claude", "--version"])
            rc4 = main(["exec", "claude:default", "--", "--help"])

        self.assertEqual((rc1, rc2, rc3, rc4), (0, 0, 0, 0))
        self.assertEqual(captured_calls[0][0], ["/usr/local/bin/claude", "-p", "hello"])
        self.assertEqual(
            captured_calls[0][1].get("CLAUDE_CONFIG_DIR"),
            str(get_account_profile_dir("claude", "work")),
        )
        self.assertEqual(captured_calls[1][0], ["/usr/local/bin/claude", "-p", "world"])
        self.assertEqual(captured_calls[2][0], ["/usr/local/bin/claude", "--version"])
        self.assertEqual(captured_calls[3][0], ["/usr/local/bin/claude", "--help"])
        self.assertNotIn("CLAUDE_CONFIG_DIR", captured_calls[3][1])

    def test_cli_login_claude_and_cursor_keychain_isolation(self) -> None:
        import io
        from contextlib import redirect_stdout
        from tokenmaster.cli import main

        pdir_claude = get_account_profile_dir("claude", "work")
        expected_svc = claude_keychain_service_name_for_dir(pdir_claude)

        def fake_claude_run(cmd: list[str], env: dict[str, str] | None = None, **_: object) -> MagicMock:
            self.assertEqual(cmd[1:], ["auth", "login"])
            self.assertEqual((env or {}).get("CLAUDE_CONFIG_DIR"), str(pdir_claude))
            self.assertNotIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", env or {})
            m = MagicMock()
            m.returncode = 0
            return m

        with (
            patch("sys.platform", "darwin"),
            patch("tokenmaster.cli._resolve_cli_binary", return_value="/opt/homebrew/bin/claude"),
            patch("subprocess.run", side_effect=fake_claude_run),
            patch(
                "tokenmaster.cli._read_macos_keychain_secret",
                side_effect=lambda svc, acct=None: (
                    json.dumps({"claudeAiOauth": {"accessToken": "kc-work-tok", "subscriptionType": "max"}})
                    if svc == expected_svc
                    else None
                ),
            ),
            redirect_stdout(io.StringIO()),
        ):
            rc = main(["login", "claude", "work"])
        self.assertEqual(rc, 0)
        cred_file = pdir_claude / ".credentials.json"
        self.assertTrue(cred_file.is_file())
        self.assertEqual(
            json.loads(cred_file.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"],
            "kc-work-tok",
        )

        # Now test Cursor login Keychain backup, pre-login clear, capture to auth.json, and restore
        pdir_cursor = get_account_profile_dir("cursor", "work")
        kc_state: dict[str, str | None] = {
            "cursor-access-token": "default-at",
            "cursor-refresh-token": None,
            "cursor-api-key": "default-stale-api-key",
        }

        def fake_cursor_login_run(cmd: list[str], env: dict[str, str] | None = None, **_: object) -> MagicMock:
            self.assertEqual(cmd[1:], ["login"])
            self.assertEqual((env or {}).get("CURSOR_HOME"), str(pdir_cursor))
            # Verify pre-existing Keychain state was cleared BEFORE cursor-agent login ran
            self.assertIsNone(kc_state["cursor-access-token"])
            self.assertIsNone(kc_state["cursor-api-key"])
            # Simulate cursor-agent login writing only access & refresh tokens
            kc_state["cursor-access-token"] = "work-new-at"
            kc_state["cursor-refresh-token"] = "work-new-rt"
            m = MagicMock()
            m.returncode = 0
            return m

        with (
            patch("sys.platform", "darwin"),
            patch("tokenmaster.cli._resolve_cli_binary", return_value="/usr/local/bin/cursor-agent"),
            patch("subprocess.run", side_effect=fake_cursor_login_run),
            patch("tokenmaster.cli._read_macos_keychain_secret", side_effect=lambda svc, acct=None: kc_state.get(svc)),
            patch("tokenmaster.cli._write_macos_keychain_secret", side_effect=lambda svc, acct, val: kc_state.__setitem__(svc, val) or True),
            patch("tokenmaster.cli._delete_macos_keychain_secret", side_effect=lambda svc: kc_state.__setitem__(svc, None)),
            redirect_stdout(io.StringIO()),
        ):
            rc_cu = main(["login", "cursor:work"])

        self.assertEqual(rc_cu, 0)
        # Verify work sandbox received ONLY the new tokens (no leaked default-stale-api-key)
        saved_cursor_auth = json.loads((pdir_cursor / "auth.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_cursor_auth["accessToken"], "work-new-at")
        self.assertEqual(saved_cursor_auth["refreshToken"], "work-new-rt")
        self.assertNotIn("apiKey", saved_cursor_auth)
        # Verify global Keychain was restored to its exact pre-login state
        self.assertEqual(kc_state["cursor-access-token"], "default-at")
        self.assertIsNone(kc_state["cursor-refresh-token"])
        self.assertEqual(kc_state["cursor-api-key"], "default-stale-api-key")

    def test_snapshot_and_remove_purge_scoped_cache_isolation(self) -> None:
        import io
        from contextlib import redirect_stdout
        from tokenmaster.cli import main

        # Set up a default Claude token and snapshot it into 'personal'
        mgr = ConfigManager()
        mgr.set_provider_field("claude", "oauth_token", "snap-tok-123")
        with (
            patch("tokenmaster.cli._write_macos_keychain_secret", return_value=True),
            redirect_stdout(io.StringIO()),
        ):
            rc = main(["account", "snapshot", "claude", "personal"])
        self.assertEqual(rc, 0)
        pdir_personal = get_account_profile_dir("claude", "personal")
        self.assertTrue((pdir_personal / ".credentials.json").is_file())

        # Create scoped cache files for both claude:personal and codex:personal
        cache_dir = self.root / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        claude_cache = cache_dir / "claude_projects_scan_personal_11111111.json"
        codex_cache = cache_dir / "codex_sessions_scan_personal_22222222.json"
        claude_cache.write_text("{}", encoding="utf-8")
        codex_cache.write_text("{}", encoding="utf-8")

        deleted_kc: list[str] = []
        with (
            patch("tokenmaster.cli._delete_macos_keychain_secret", side_effect=deleted_kc.append),
            redirect_stdout(io.StringIO()),
        ):
            rc_rm = main(["account", "remove", "claude:personal", "--purge"])
        self.assertEqual(rc_rm, 0)
        self.assertFalse(pdir_personal.exists())
        self.assertFalse(claude_cache.exists())
        # codex:personal's cache must NOT be deleted when purging claude:personal
        self.assertTrue(codex_cache.exists())
        self.assertIn(claude_keychain_service_name_for_dir(pdir_personal), deleted_kc)

    def test_active_profile_secret_env_does_not_pollute_default_or_env_default(self) -> None:
        import io
        from contextlib import redirect_stdout
        from tokenmaster.cli import main

        mgr = ConfigManager()
        mgr.set_account_field("openrouter", "team", "api_key", "sk-or-v1-team-secret")
        mgr.set_account_field("claude", "work", "oauth_token", "sk-ant-oat01-work-tok")

        # Simulate a shell where `eval "$(tokenmaster env openrouter team)"` is active
        with patch.dict(
            os.environ,
            {
                "TOKENMASTER_ACTIVE_PROFILE": "openrouter:team",
                "OPENROUTER_API_KEY": "sk-or-v1-team-secret",
                "CLAUDE_OAUTH_TOKEN": "sk-ant-oat01-work-tok",
            },
            clear=False,
        ):
            # Default account config must NOT adopt team's OPENROUTER_API_KEY from os.environ
            def_or_cfg = mgr.account_cfg("openrouter", None)
            self.assertNotEqual(def_or_cfg.get("api_key"), "sk-or-v1-team-secret")

            # Switching back to `openrouter default` must unset OPENROUTER_API_KEY
            out_def = io.StringIO()
            with redirect_stdout(out_def):
                rc = main(["env", "openrouter", "default"])
            self.assertEqual(rc, 0)
            self.assertIn("unset OPENROUTER_API_KEY", out_def.getvalue())
            self.assertNotIn("sk-or-v1-team-secret", out_def.getvalue())

            # `env claude work` must unset CLAUDE_OAUTH_TOKEN even when setting CLAUDE_CODE_OAUTH_TOKEN
            out_cw = io.StringIO()
            with redirect_stdout(out_cw):
                rc_cw = main(["env", "claude", "work"])
            self.assertEqual(rc_cw, 0)
            self.assertIn("unset CLAUDE_OAUTH_TOKEN", out_cw.getvalue())
            self.assertIn("export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-work-tok", out_cw.getvalue())

    def test_exec_trailing_account_flags_and_conflict_detection(self) -> None:
        import io
        from contextlib import redirect_stderr
        from tokenmaster.cli import main

        captured: list[tuple[list[str], dict[str, str]]] = []

        def fake_run(cmd: list[str], env: dict[str, str] | None = None, **_: object) -> MagicMock:
            captured.append((list(cmd), dict(env or {})))
            m = MagicMock()
            m.returncode = 0
            return m

        with (
            patch("tokenmaster.cli._resolve_cli_binary", return_value="/usr/local/bin/claude"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            rc1 = main(["exec", "claude", "-a", "work", "--version"])
            rc2 = main(["exec", "claude", "--account=work", "-p", "hi"])
            with redirect_stderr(io.StringIO()):
                rc_conflict = main(["exec", "claude:work", "-a", "other", "--version"])

        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        self.assertEqual(rc_conflict, 2)
        self.assertEqual(captured[0][0], ["/usr/local/bin/claude", "--version"])
        self.assertEqual(captured[1][0], ["/usr/local/bin/claude", "-p", "hi"])

    def test_inspect_cursor_safe_storage_and_control_char_profile_dir(self) -> None:
        from tokenmaster.cli import _inspect_account_status
        from tokenmaster.config import is_forbidden_profile_dir

        self.assertTrue(is_forbidden_profile_dir(Path("/tmp/bad\ndir")))
        mgr = ConfigManager()
        with (
            patch.object(CursorProvider, "_load_cursor_cli_auth", return_value={"access_token": None, "refresh_token": None, "api_key": None}),
            patch.object(CursorProvider, "_read_sqlite_auth", return_value={"cursorAuth/accessToken": "cursor-safe-storage:encrypted"}),
        ):
            _, is_ready, _ = _inspect_account_status(mgr, "cursor", None)
        self.assertFalse(is_ready)


class TestClaudeAndCursorOAuthRefresh(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env_patch = patch.dict(
            os.environ,
            {
                "TOKENMASTER_CONFIG_DIR": str(self.root / "cfg"),
                "TOKENMASTER_CACHE_DIR": str(self.root / "cache"),
                "TOKENMASTER_PROFILES_DIR": str(self.root / "profiles"),
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_claude_proactive_keychain_refresh_and_persistence(self) -> None:
        empty_cdir = self.root / "empty_claude"
        svc_name = claude_keychain_service_name_for_dir(empty_cdir)
        kc_store: dict[str, dict] = {
            svc_name: {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat01-expired-tok",
                    "refreshToken": "sk-ant-ort01-refresh-1",
                    "expiresAt": 1_500_000_000_000,  # expired epoch ms
                    "scopes": ["user:profile", "user:inference"],
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                },
                "extraTopKey": "preserve-me",
            }
        }
        http_calls: list[tuple[str, str]] = []

        def fake_http(
            self_prov: ClaudeProvider,
            url: str,
            method: str = "GET",
            headers: dict | None = None,
            payload: dict | None = None,
            **_: object,
        ) -> dict:
            http_calls.append((method, url))
            if url.endswith("/v1/oauth/token"):
                self.assertEqual((payload or {}).get("grant_type"), "refresh_token")
                self.assertEqual((payload or {}).get("refresh_token"), "sk-ant-ort01-refresh-1")
                self.assertEqual((payload or {}).get("scope"), "user:profile user:inference")
                return {
                    "access_token": "sk-ant-oat01-fresh-tok",
                    "refresh_token": "sk-ant-ort01-refresh-2",
                    "expires_in": 28800,
                    "scope": "user:profile user:inference user:sessions:claude_code",
                }
            auth = (headers or {}).get("Authorization", "")
            self.assertEqual(auth, "Bearer sk-ant-oat01-fresh-tok")
            return {"seven_day": {"utilization": 18.0, "resets_at": "2099-01-08T00:00:00Z"}}

        with (
            patch("sys.platform", "darwin"),
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(empty_cdir)}),
            patch.object(
                ClaudeProvider,
                "_read_keychain_json",
                side_effect=lambda svc: kc_store.get(svc),
            ),
            patch.object(
                ClaudeProvider,
                "_write_keychain_json",
                side_effect=lambda svc, raw: kc_store.__setitem__(svc, raw) or True,
            ),
            patch.object(ClaudeProvider, "http_json", new=fake_http),
        ):
            prov = ClaudeProvider({})
            rep = prov.fetch()

        self.assertEqual(rep.status, "ok")
        self.assertEqual(rep.plan_tier, "Claude Max 20x")
        self.assertAlmostEqual(rep.weekly.remaining_percent or 0.0, 82.0)
        # Proactive refresh should happen BEFORE /api/oauth/usage (only 2 HTTP calls total, no wasted 401)
        self.assertEqual(
            http_calls,
            [
                ("POST", "https://platform.claude.com/v1/oauth/token"),
                ("GET", "https://api.anthropic.com/api/oauth/usage"),
            ],
        )
        saved_kc = kc_store[svc_name]
        self.assertEqual(saved_kc["extraTopKey"], "preserve-me")
        self.assertEqual(saved_kc["claudeAiOauth"]["accessToken"], "sk-ant-oat01-fresh-tok")
        self.assertEqual(saved_kc["claudeAiOauth"]["refreshToken"], "sk-ant-ort01-refresh-2")
        self.assertEqual(saved_kc["claudeAiOauth"]["subscriptionType"], "max")
        self.assertGreater(saved_kc["claudeAiOauth"]["expiresAt"], 1_700_000_000_000)

    def test_claude_reactive_401_refresh_and_invalid_scope_fallback(self) -> None:
        from tokenmaster.providers.base import HttpError

        cdir = self.root / "claude_reactive"
        cdir.mkdir(parents=True)
        cred_file = cdir / ".credentials.json"
        cred_file.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-ant-oat01-revoked",
                        "refreshToken": "sk-ant-ort01-rt",
                        "expiresAt": 4_102_444_800_000,  # year 2100 (not locally expired)
                        "scopes": ["user:profile", "user:bad_scope"],
                        "subscriptionType": "pro",
                    }
                }
            ),
            encoding="utf-8",
        )

        token_post_payloads: list[dict] = []

        def fake_http(
            self_prov: ClaudeProvider,
            url: str,
            method: str = "GET",
            headers: dict | None = None,
            payload: dict | None = None,
            **_: object,
        ) -> dict:
            if url.endswith("/v1/oauth/token"):
                token_post_payloads.append(dict(payload or {}))
                if "scope" in (payload or {}):
                    raise HttpError(400, "Bad Request", '{"error": "invalid_scope"}')
                return {
                    "access_token": "sk-ant-oat01-recovered",
                    "refresh_token": "sk-ant-ort01-rt-new",
                    "expires_in": 3600,
                }
            auth = (headers or {}).get("Authorization", "")
            if auth == "Bearer sk-ant-oat01-revoked":
                raise HttpError(401, "Unauthorized")
            if auth == "Bearer sk-ant-oat01-recovered":
                return {"seven_day": {"utilization": 40.0, "resets_at": "2099-01-08T00:00:00Z"}}
            raise AssertionError(f"Unexpected auth: {auth}")

        with (
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(cdir)}),
            patch.object(ClaudeProvider, "_read_keychain_json", return_value=None),
            patch.object(ClaudeProvider, "http_json", new=fake_http),
        ):
            rep = ClaudeProvider({}).fetch()

        self.assertEqual(rep.status, "ok")
        self.assertAlmostEqual(rep.weekly.remaining_percent or 0.0, 60.0)
        self.assertEqual(len(token_post_payloads), 2)
        self.assertIn("scope", token_post_payloads[0])
        self.assertNotIn("scope", token_post_payloads[1])
        disk_saved = json.loads(cred_file.read_text(encoding="utf-8"))["claudeAiOauth"]
        self.assertEqual(disk_saved["accessToken"], "sk-ant-oat01-recovered")
        self.assertEqual(disk_saved["refreshToken"], "sk-ant-ort01-rt-new")
        # Rejected scopes must be cleared when fallback succeeds without returning new scopes
        self.assertNotIn("scopes", disk_saved)

    def test_claude_reactive_401_discovers_keychain_and_handles_manual_override(self) -> None:
        from tokenmaster.providers.base import HttpError

        cdir = self.root / "claude_kc_fallback"
        cdir.mkdir(parents=True)
        cred_file = cdir / ".credentials.json"
        # .credentials.json has a non-expired (expiresAt=None) stale token and NO refreshToken
        cred_file.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-file-stale", "subscriptionType": "max"}}),
            encoding="utf-8",
        )
        svc_name = claude_keychain_service_name_for_dir(cdir)
        # Keychain has a revoked token AND a valid refreshToken rotated by `claude` CLI
        kc_store = {
            svc_name: {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-kc-also-401",
                    "refreshToken": "sk-ant-ort01-kc-valid",
                    "expiresAt": 4_102_444_800_000,
                    "subscriptionType": "max",
                    "rateLimitTier": "20x",
                }
            }
        }

        def fake_http(
            self_prov: ClaudeProvider,
            url: str,
            method: str = "GET",
            headers: dict | None = None,
            payload: dict | None = None,
            **_: object,
        ) -> dict:
            if url.endswith("/v1/oauth/token"):
                self.assertEqual((payload or {}).get("refresh_token"), "sk-ant-ort01-kc-valid")
                return {
                    "access_token": "sk-ant-network-fresh",
                    "refresh_token": "sk-ant-ort01-kc-next",
                    "expires_in": 7200,
                }
            auth = (headers or {}).get("Authorization", "")
            if auth in ("Bearer sk-ant-manual-bad", "Bearer sk-ant-file-stale", "Bearer sk-ant-kc-also-401"):
                raise HttpError(401, "Unauthorized")
            if auth == "Bearer sk-ant-network-fresh":
                return {"seven_day": {"utilization": 22.0, "resets_at": "2099-01-08T00:00:00Z"}}
            raise AssertionError(f"Unexpected auth: {auth}")

        with (
            patch("sys.platform", "darwin"),
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(cdir)}),
            patch.object(ClaudeProvider, "_read_keychain_json", side_effect=lambda s: kc_store.get(s)),
            patch.object(
                ClaudeProvider,
                "_write_keychain_json",
                side_effect=lambda s, r: kc_store.__setitem__(s, r) or True,
            ),
            patch.object(ClaudeProvider, "http_json", new=fake_http),
        ):
            # Test with a manual cfg['oauth_token'] override that 401s, then file 401s,
            # then Keychain adopted token 401s, then network refresh with Keychain refreshToken succeeds!
            prov = ClaudeProvider({"oauth_token": "sk-ant-manual-bad"})
            rep = prov.fetch()

        self.assertEqual(rep.status, "ok")
        self.assertAlmostEqual(rep.weekly.remaining_percent or 0.0, 78.0)
        self.assertEqual(
            json.loads(cred_file.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"],
            "sk-ant-network-fresh",
        )
        self.assertEqual(
            kc_store[svc_name]["claudeAiOauth"]["accessToken"],
            "sk-ant-network-fresh",
        )

    def test_claude_refresh_adopts_sibling_rotated_token_without_network(self) -> None:
        cdir = self.root / "claude_cas"
        cdir.mkdir(parents=True)
        cred_file = cdir / ".credentials.json"
        # Disk already has a fresh token rotated by a sibling process
        cred_file.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-ant-oat01-sibling-fresh",
                        "refreshToken": "sk-ant-ort01-sibling-rt",
                        "expiresAt": 4_102_444_800_000,
                    }
                }
            ),
            encoding="utf-8",
        )
        prov = ClaudeProvider({"profile_dir": str(cdir), "account_name": "work"})
        stale_bundle = {
            "access_token": "sk-ant-oat01-stale",
            "refresh_token": "sk-ant-ort01-old-rt",
            "expires_at_ms": 1000.0,
            "scopes": None,
            "client_id": None,
            "sub_type": "max",
            "tier": None,
            "source_file": cred_file,
            "source_keychain_service": None,
        }
        with (
            patch.object(ClaudeProvider, "_read_keychain_json", return_value=None),
            patch.object(ClaudeProvider, "http_json", side_effect=AssertionError("should not call network")),
        ):
            adopted = prov._refresh_oauth_token(stale_bundle)
        self.assertEqual(adopted, "sk-ant-oat01-sibling-fresh")
        self.assertEqual(stale_bundle["refresh_token"], "sk-ant-ort01-sibling-rt")

    def test_cursor_default_keychain_refresh_persists_back_to_keychain(self) -> None:
        kc_data: dict[str, str] = {
            "cursor-access-token": "expired-kc-jwt",
            "cursor-refresh-token": "kc-rt-initial",
        }

        def fake_http(
            self_prov: CursorProvider,
            url: str,
            method: str = "GET",
            headers: dict | None = None,
            payload: dict | None = None,
            **_: object,
        ) -> dict:
            if url.endswith("/oauth/token"):
                self.assertEqual((payload or {}).get("refresh_token"), "kc-rt-initial")
                return {"access_token": "fresh-kc-jwt", "refresh_token": "kc-rt-rotated"}
            if "GetCurrentPeriodUsage" in url:
                return {"planUsage": {"totalPercentUsed": 12.5}, "billingCycleEnd": "2099-01-08T00:00:00Z"}
            if "GetPlanInfo" in url:
                return {"planInfo": {"planName": "Premium"}}
            return {}

        with (
            patch("sys.platform", "darwin"),
            patch.dict(os.environ, {"CURSOR_HOME": str(self.root / "empty_cursor")}),
            patch.object(CursorProvider, "_read_sqlite_auth", return_value={}),
            patch.object(CursorProvider, "_is_jwt_expired", side_effect=lambda t: t == "expired-kc-jwt"),
            patch.object(CursorProvider, "_read_keychain_secret", side_effect=lambda svc, account="cursor-user": kc_data.get(svc)),
            patch.object(
                CursorProvider,
                "_write_keychain_secret",
                side_effect=lambda svc, val, account="cursor-user": kc_data.__setitem__(svc, val) or True,
            ),
            patch.object(CursorProvider, "http_json", new=fake_http),
        ):
            rep = CursorProvider({}).fetch()

        self.assertEqual(rep.status, "ok")
        self.assertAlmostEqual(rep.weekly.remaining_percent or 0.0, 87.5)
        self.assertEqual(kc_data["cursor-access-token"], "fresh-kc-jwt")
        self.assertEqual(kc_data["cursor-refresh-token"], "kc-rt-rotated")

    def test_cursor_sibling_rotated_refresh_token_adopted_and_expired_claude_status(self) -> None:
        from tokenmaster.cli import _inspect_account_status

        pdir = self.root / "profiles" / "cursor" / "team"
        pdir.mkdir(parents=True)
        auth_file = pdir / "auth.json"
        auth_file.write_text(
            json.dumps({"accessToken": "expired-1", "refreshToken": "rt-1"}),
            encoding="utf-8",
        )
        prov = CursorProvider({"account_name": "team", "profile_dir": str(pdir)})
        with patch.object(CursorProvider, "_is_jwt_expired", side_effect=lambda t: t == "expired-1"):
            prov._load_cursor_cli_auth()
            # Simulate a sibling thread rotating both accessToken and refreshToken on disk before _refresh_access_token
            auth_file.write_text(
                json.dumps({"accessToken": "sibling-valid-jwt", "refreshToken": "rt-2-rotated"}),
                encoding="utf-8",
            )
            with patch.object(CursorProvider, "http_json", side_effect=AssertionError("should adopt sibling token")):
                tok = prov._refresh_access_token("rt-1")
        self.assertEqual(tok, "sibling-valid-jwt")

        # Verify _inspect_account_status reports expired-without-refreshToken Claude profile as not ready
        mgr = ConfigManager()
        c_pdir = mgr.ensure_account("claude", "exp_no_rt")
        (c_pdir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "tok-exp", "expiresAt": 1_500_000_000_000, "subscriptionType": "max"}}),
            encoding="utf-8",
        )
        with patch.object(ClaudeProvider, "_read_keychain_json", return_value=None):
            _, is_ready, detail = _inspect_account_status(mgr, "claude", "exp_no_rt")
        self.assertFalse(is_ready)
        self.assertIn("expired", detail)

    def test_claude_oauth_usage_429_rate_limit_cache_fallback_and_cooldown(self) -> None:
        from tokenmaster.providers.base import HttpError

        cdir = self.root / "claude_429"
        cdir.mkdir(parents=True)
        (cdir / ".credentials.json").write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-ant-oat01-valid",
                        "refreshToken": "sk-ant-ort01-valid",
                        "expiresAt": 4_102_444_800_000,
                        "subscriptionType": "max",
                        "rateLimitTier": "20x",
                    }
                }
            ),
            encoding="utf-8",
        )

        usage_calls = 0
        next_status = 200

        def fake_http(
            self_prov: ClaudeProvider,
            url: str,
            method: str = "GET",
            headers: dict | None = None,
            payload: dict | None = None,
            **_: object,
        ) -> dict:
            nonlocal usage_calls
            if url.endswith("/api/oauth/usage"):
                usage_calls += 1
                if next_status == 429:
                    raise HttpError(429, "Too Many Requests")
                if next_status == 401:
                    raise HttpError(401, "Unauthorized")
                return {
                    "seven_day": {"utilization": 13.0, "resets_at": "2099-01-08T00:00:00Z"},
                    "seven_day_fable": {"utilization": 23.0, "resets_at": "2099-01-08T00:00:00Z"},
                }
            raise HttpError(401, "Refresh failed")

        with (
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(cdir)}),
            patch.object(ClaudeProvider, "_read_keychain_json", return_value=None),
            patch.object(ClaudeProvider, "http_json", new=fake_http),
        ):
            prov = ClaudeProvider({})
            # 1) First call succeeds and populates snapshot cache
            rep1 = prov.fetch()
            self.assertEqual(rep1.status, "ok")
            self.assertAlmostEqual(rep1.weekly.remaining_percent or 0.0, 87.0)
            self.assertAlmostEqual(rep1.weekly.scoped_remaining_percent or 0.0, 77.0)
            self.assertEqual(usage_calls, 1)
            cache_path = prov._usage_cache_path()
            self.assertTrue(cache_path.is_file())
            orig_fetched_at = json.loads(cache_path.read_text(encoding="utf-8"))["fetched_at"]

            # 2) Second call hits HTTP 429 -> falls back to cached snapshot and sets cooldown_until
            next_status = 429
            rep2 = prov.fetch()
            self.assertEqual(rep2.status, "ok")
            self.assertIsNone(rep2.error_message)
            self.assertAlmostEqual(rep2.weekly.remaining_percent or 0.0, 87.0)
            self.assertEqual(usage_calls, 2)
            raw_after_429 = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(raw_after_429["fetched_at"], orig_fetched_at)
            self.assertGreater(raw_after_429["cooldown_until"], orig_fetched_at)

            # 3) Third call during active 60s cooldown skips network call completely
            rep3 = prov.fetch()
            self.assertEqual(rep3.status, "ok")
            self.assertAlmostEqual(rep3.weekly.remaining_percent or 0.0, 87.0)
            self.assertEqual(usage_calls, 2)

            # 4) Once cooldown expires, HTTP 401 must NOT be masked by the 429 cache
            raw_after_429["cooldown_until"] = 0.0
            cache_path.write_text(json.dumps(raw_after_429), encoding="utf-8")
            next_status = 401
            rep4 = prov.fetch()
            self.assertNotEqual(rep4.status, "ok")
            self.assertIn("401", rep4.error_message or "")


class TestSmartAccountRouter(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env_patch = patch.dict(
            os.environ,
            {
                "TOKENMASTER_CONFIG_DIR": str(self.root / "cfg"),
                "TOKENMASTER_CACHE_DIR": str(self.root / "cache"),
                "TOKENMASTER_PROFILES_DIR": str(self.root / "profiles"),
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_select_best_account_strategies_and_exhaustion_filtering(self) -> None:
        from tokenmaster.cli import select_best_account
        from tokenmaster.models import CreditBalance, ProviderReport, SubMetric, UsageWindow

        # 1) Exhausted 5h window account (98% used -> 2% rem, even though 7d has 85% left)
        rep_5h_exhausted = ProviderReport(
            provider_id="claude",
            display_name="Claude [throttled]",
            account="throttled",
            status="ok",
            weekly=UsageWindow(
                used_percent=15.0,
                remaining_percent=85.0,
                resets_at="in 2d 0h",
                window_seconds=604800,
            ),
            sub_metrics=[
                SubMetric(name="5h Rolling Window", used_str="98.0%", remaining_str="2.0%", used_percent=98.0)
            ],
        ).finalize()

        # 2) Account expiring in 1 day with 45% weekly remaining (underburn, 5h rem = 80%)
        rep_expiring_soon = ProviderReport(
            provider_id="claude",
            display_name="Claude [work]",
            account="work",
            status="ok",
            weekly=UsageWindow(
                used_percent=55.0,
                remaining_percent=45.0,
                resets_at="in 1d 0h",
                window_seconds=604800,
            ),
            # Optional extra_usage credits at $0.00 must NOT disqualify a healthy 7d subscription!
            credits=CreditBalance(remaining=0.0, limit=0.0, unit="USD"),
            sub_metrics=[
                SubMetric(name="5h Rolling Window", used_str="20.0%", remaining_str="80.0%", used_percent=20.0)
            ],
        ).finalize()

        # 3) Account resetting in 6 days with 88% weekly remaining (on_track, 5h rem = 90%)
        rep_fresh_cycle = ProviderReport(
            provider_id="claude",
            display_name="Claude [personal]",
            account="personal",
            status="ok",
            weekly=UsageWindow(
                used_percent=12.0,
                remaining_percent=88.0,
                resets_at="in 6d 0h",
                window_seconds=604800,
            ),
            sub_metrics=[
                SubMetric(name="5h Rolling Window", used_str="10.0%", remaining_str="90.0%", used_percent=10.0)
            ],
        ).finalize()

        # 4) Account in overburn (80% used in 1 day -> 20% left with 6 days remaining)
        rep_overburn = ProviderReport(
            provider_id="claude",
            display_name="Claude",
            account=None,
            status="ok",
            weekly=UsageWindow(
                used_percent=80.0,
                remaining_percent=20.0,
                resets_at="in 6d 0h",
                window_seconds=604800,
            ),
            sub_metrics=[
                SubMetric(name="5h Rolling Window", used_str="15.0%", remaining_str="85.0%", used_percent=15.0)
            ],
        ).finalize()

        reports = [rep_5h_exhausted, rep_expiring_soon, rep_fresh_cycle, rep_overburn]

        # "smart" picks `work` (45% left expiring in 1d = 45%/d burnable vs `personal` 70%/6d = 11.7%/d)
        best_smart, reason_smart = select_best_account(reports, strategy="smart")
        self.assertIsNotNone(best_smart)
        assert best_smart is not None
        self.assertEqual(best_smart.account, "work")
        self.assertIn("strategy=smart", reason_smart)

        # "most-remaining" picks `personal` (70% > 45% > 20%, while `throttled` 85% is excluded by 5h window)
        best_most, _ = select_best_account(reports, strategy="most-remaining")
        self.assertIsNotNone(best_most)
        assert best_most is not None
        self.assertEqual(best_most.account, "personal")

        # "expiring-first" picks `work` (resets in 1d 0h < 6d 0h)
        best_exp, _ = select_best_account(reports, strategy="expiring-first")
        self.assertIsNotNone(best_exp)
        assert best_exp is not None
        self.assertEqual(best_exp.account, "work")

        # Credit-only exhausted account is rejected
        rep_or_empty = ProviderReport(
            provider_id="openrouter",
            display_name="OpenRouter",
            account="empty",
            status="ok",
            credits=CreditBalance(remaining=0.0, limit=50.0, unit="USD"),
        ).finalize()
        best_none, reason_none = select_best_account([rep_5h_exhausted, rep_or_empty], strategy="smart")
        self.assertIsNone(best_none)
        self.assertIn("No available subscription accounts", reason_none)

    def test_cli_pick_env_auto_and_exec_auto(self) -> None:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from tokenmaster.cli import main
        from tokenmaster.models import ProviderReport, SubMetric, UsageWindow

        fake_reports = [
            ProviderReport(
                provider_id="claude",
                display_name="Claude",
                account=None,
                status="ok",
                weekly=UsageWindow(used_percent=65.0, remaining_percent=35.0, resets_at="in 5d 0h", window_seconds=604800),
                sub_metrics=[
                    SubMetric(name="5h Rolling Window", used_str="20.0%", remaining_str="80.0%", used_percent=20.0)
                ],
            ).finalize(),
            ProviderReport(
                provider_id="claude",
                display_name="Claude [work]",
                account="work",
                status="ok",
                weekly=UsageWindow(used_percent=15.0, remaining_percent=85.0, resets_at="in 1d 12h", window_seconds=604800),
                sub_metrics=[
                    SubMetric(name="5h Rolling Window", used_str="10.0%", remaining_str="90.0%", used_percent=10.0)
                ],
            ).finalize(),
        ]

        captured_exec: list[tuple[list[str], dict[str, str]]] = []

        def fake_run(cmd: list[str], env: dict[str, str] | None = None, **_: object) -> MagicMock:
            captured_exec.append((list(cmd), dict(env or {})))
            m = MagicMock()
            m.returncode = 0
            return m

        with (
            patch("tokenmaster.cli.fetch_providers", return_value=fake_reports),
            patch("tokenmaster.cli._resolve_cli_binary", return_value="/usr/local/bin/claude"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            # 1) `tokenmaster pick claude --quiet`
            q_out = io.StringIO()
            with redirect_stdout(q_out):
                rc_q = main(["pick", "claude", "--quiet"])
            self.assertEqual(rc_q, 0)
            self.assertEqual(q_out.getvalue().strip(), "work")

            # 2) `tokenmaster pick claude --json`
            j_out = io.StringIO()
            with redirect_stdout(j_out):
                rc_j = main(["pick", "claude", "--json"])
            self.assertEqual(rc_j, 0)
            parsed_pick = json.loads(j_out.getvalue())
            self.assertEqual(parsed_pick["selected_account"], "work")
            self.assertEqual(parsed_pick["selected_instance"], "claude:work")

            # 3) `tokenmaster env claude auto`
            env_out = io.StringIO()
            with redirect_stdout(env_out):
                rc_env = main(["env", "claude", "auto"])
            self.assertEqual(rc_env, 0)
            env_txt = env_out.getvalue()
            self.assertIn("# Auto-selected claude:work", env_txt)
            self.assertIn("export TOKENMASTER_ACTIVE_PROFILE=claude:work", env_txt)

            # 4) `tokenmaster exec claude:auto -s most-remaining -- -p hello`
            exec_out = io.StringIO()
            exec_err = io.StringIO()
            with redirect_stdout(exec_out), redirect_stderr(exec_err):
                rc_ex = main(["exec", "claude:auto", "-s", "most-remaining", "--", "-p", "hello"])
            self.assertEqual(rc_ex, 0)
            self.assertEqual(exec_out.getvalue(), "")
            self.assertIn("Auto-selected claude:work", exec_err.getvalue())
            self.assertEqual(captured_exec[0][0], ["/usr/local/bin/claude", "-p", "hello"])
            self.assertEqual(
                captured_exec[0][1].get("CLAUDE_CONFIG_DIR"),
                str(get_account_profile_dir("claude", "work")),
            )

            # 5) Invalid arguments (`pick claude:work` or invalid strategy) return exit code 2
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(["pick", "claude:work"]), 2)
                self.assertEqual(main(["pick", "claude", "-s", "bad_strat"]), 2)
                self.assertEqual(main(["exec", "claude:auto", "work", "--", "-p", "hi"]), 2)
                self.assertEqual(main(["exec", "claude", "-a", "work", "other", "--", "-p", "hi"]), 2)
                self.assertEqual(main(["exec", "claude", "-s", "bad_strat", "auto", "--", "-p", "hi"]), 2)
                self.assertEqual(main(["exec", "claude", "-s", "bad_strat", "auto", "-p", "hi"]), 2)

    def test_adversarial_edge_cases_scoped_pools_nan_now_and_openrouter(self) -> None:
        from tokenmaster.cli import select_best_account
        from tokenmaster.models import CreditBalance, ProviderReport, SubMetric, UsageWindow

        # 1) Claude account with 40% all-models remaining but 0% Fable/Sonnet scoped pool -> rejected
        rep_scoped_exhausted = ProviderReport(
            provider_id="claude",
            display_name="Claude [scoped_empty]",
            account="scoped_empty",
            status="ok",
            weekly=UsageWindow(
                used_percent=60.0,
                remaining_percent=40.0,
                scoped_label="Fable",
                scoped_used_percent=100.0,
                scoped_remaining_percent=0.0,
                resets_at="in 1d 0h",
            ),
        ).finalize()

        # 2) OpenRouter account with weekly key cap remaining (75%) but $0.00 account credits -> rejected
        rep_or_zero_cred = ProviderReport(
            provider_id="openrouter",
            display_name="OpenRouter [zero_cred]",
            account="zero_cred",
            status="ok",
            weekly=UsageWindow(used=5.0, remaining=15.0, limit=20.0, unit="USD"),
            credits=CreditBalance(used=50.0, remaining=0.0, limit=50.0, unit="USD"),
        ).finalize()

        # 3) Cursor account with 99.5% billing-cycle pace_used_percent -> rejected
        rep_cursor_exhausted = ProviderReport(
            provider_id="cursor",
            display_name="Cursor [cycle_done]",
            account="cycle_done",
            status="ok",
            weekly=UsageWindow(
                used_percent=40.0,
                remaining_percent=60.0,
                pace_used_percent=99.5,
                resets_at="in 2d 0h",
                window_seconds=30 * 86400,
            ),
        ).finalize()

        self.assertIsNone(select_best_account([rep_scoped_exhausted], strategy="smart")[0])
        self.assertIsNone(select_best_account([rep_or_zero_cred], strategy="smart")[0])
        self.assertIsNone(select_best_account([rep_cursor_exhausted], strategy="smart")[0])

        # 4) Account resetting "now" (rem_sec == 0.0) ranks first in `expiring-first` and handles NaN sub-metric
        rep_reset_now = ProviderReport(
            provider_id="claude",
            display_name="Claude [now]",
            account="now_acct",
            status="ok",
            weekly=UsageWindow(used_percent=30.0, remaining_percent=70.0, resets_in="now"),
            sub_metrics=[
                SubMetric(name="5h Rolling Window", used_str="—", remaining_str="—", used_percent=float("nan"))
            ],
        ).finalize()
        rep_reset_later = ProviderReport(
            provider_id="claude",
            display_name="Claude [later]",
            account="later_acct",
            status="ok",
            weekly=UsageWindow(used_percent=20.0, remaining_percent=80.0, resets_in="2d 0h"),
        ).finalize()
        best_exp, _ = select_best_account([rep_reset_later, rep_reset_now], strategy="expiring-first")
        self.assertIsNotNone(best_exp)
        assert best_exp is not None
        self.assertEqual(best_exp.account, "now_acct")


if __name__ == "__main__":
    unittest.main()
