"""Standalone Linux CLI quota regressions. All credentials and APIs are synthetic."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from tokenmaster.providers.antigravity import AntigravityProvider
from tokenmaster.providers.antigravity_cli import API_ROOT, AntigravityCliClient
from tokenmaster.providers.base import HttpError


def responses():
    reset = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    return (
        {"currentTier": {"name": "Antigravity"}, "paidTier": {"name": "Test Paid Tier"},
         "cloudaicompanionProject": "synthetic-project"},
        {"models": {
            "gemini-test": {"displayName": "Gemini Test", "quotaInfo": {
                "remainingFraction": 0.25, "resetTime": reset}},
            "claude-test": {"displayName": "Claude Test", "quotaInfo": {
                "remainingFraction": 0.50, "resetTime": reset}},
            "completion-helper": {"quotaInfo": {"remainingFraction": 1}},
            "unmetered": {"displayName": "No Quota Model"},
        }},
    )


class CliClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"TOKENMASTER_CACHE_DIR": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.platform = patch("tokenmaster.providers.antigravity_cli.sys.platform", "linux")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.http = MagicMock(side_effect=responses())
        self.client = AntigravityCliClient(self.http, 4.0)

    def test_keyring_contract_and_no_secret_in_command_arguments(self):
        proc = MagicMock(returncode=0, stdout=json.dumps({
            "auth_method": "consumer", "token": {"access_token": "synthetic-access",
            "refresh_token": "synthetic-refresh"}}))
        with patch("shutil.which", return_value="/usr/bin/secret-tool"), patch(
            "subprocess.run", return_value=proc
        ) as run:
            self.assertEqual(self.client._access_token(), "synthetic-access")
        self.assertEqual(run.call_args.args[0], [
            "/usr/bin/secret-tool", "lookup", "service", "gemini", "username", "antigravity"])
        self.assertNotIn("synthetic-access", str(run.call_args))
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_missing_tool_or_failed_keyring_is_unconfigured(self):
        with patch("shutil.which", return_value=None), patch("subprocess.run") as run:
            self.assertIsNone(self.client.query())
            run.assert_not_called()
        for value in ("bad JSON", "[]", '{"token": []}', '{"token": {"access_token": true}}',
                      '{"auth_method":"business","token":{"access_token":"test"}}',
                      '{"token":{"access_token":"bad\\nheader"}}'):
            with self.subTest(value=value), patch("shutil.which", return_value="secret-tool"), patch(
                "subprocess.run", return_value=MagicMock(returncode=0, stdout=value)
            ):
                self.assertIsNone(self.client.query())
        self.http.assert_not_called()

    def test_keyring_timeout_does_not_escape(self):
        with patch("shutil.which", return_value="secret-tool"), patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("secret-tool", 3)
        ):
            self.assertIsNone(self.client.query())

    def test_non_linux_never_reads_linux_keyring(self):
        with patch("tokenmaster.providers.antigravity_cli.sys.platform", "darwin"), patch.object(
            self.client, "_access_token"
        ) as read:
            self.assertIsNone(self.client.query())
            read.assert_not_called()

    def test_api_contract_normalization_and_utility_exclusion(self):
        with patch.object(self.client, "_access_token", return_value="synthetic-access"):
            result = self.client.query()
        load, models = self.http.call_args_list
        self.assertEqual(load.args[0], API_ROOT + "loadCodeAssist")
        self.assertEqual(load.kwargs["method"], "POST")
        self.assertEqual(load.kwargs["payload"], {"metadata": {"ideType": "ANTIGRAVITY"}})
        self.assertTrue(load.kwargs["headers"]["User-Agent"].startswith("antigravity "))
        self.assertEqual(load.kwargs["headers"]["Authorization"], "Bearer synthetic-access")
        self.assertEqual(models.args[0], API_ROOT + "fetchAvailableModels")
        self.assertEqual(models.kwargs["payload"], {"project": "synthetic-project"})
        status = result["userStatus"]
        self.assertEqual(status["userTier"]["name"], "Test Paid Tier")
        configs = status["cascadeModelConfigData"]["clientModelConfigs"]
        self.assertEqual([cfg["label"] for cfg in configs], ["Gemini Test", "Claude Test"])
        self.assertNotIn("synthetic-project", json.dumps(result))
        self.assertNotIn("synthetic-access", json.dumps(result))

    def test_missing_remaining_fraction_with_reset_is_exhausted(self):
        account, models = responses()
        del models["models"]["gemini-test"]["quotaInfo"]["remainingFraction"]
        self.http.side_effect = [account, models]
        data = self.client._query("synthetic-access")
        provider = AntigravityProvider({})
        with patch.object(provider, "_query_language_server", return_value=None), patch.object(
            provider, "_query_cli_status", return_value=data
        ), patch.object(provider, "_scan_local_brain", return_value={
            "brain_accessible": False, "conversations_7d": 0, "steps_7d": 0, "steps_30d": 0
        }):
            report = provider.fetch()
        weekly = next(m for m in report.sub_metrics if m.name == "Gemini Weekly")
        self.assertEqual(weekly.used_percent, 100.0)
        self.assertEqual(weekly.remaining_str, "0.0%")
        self.assertEqual(report.status, "ok")
        self.assertEqual(report.source, "Antigravity CLI Google quota API")
        self.assertEqual(report.weekly.used_percent, 75.0)
        self.assertEqual(report.credits.remaining, None)

    def test_bad_api_payload_does_not_invent_quota_or_project(self):
        for account, models in (([], {}), ({}, {}),
                                ({"cloudaicompanionProject": "synthetic"}, {"models": []}),
                                ({"cloudaicompanionProject": "synthetic"}, {"models": {
                                    "bad": {"displayName": "Unknown", "quotaInfo": {}}}})):
            with self.subTest(account=account, models=models):
                self.http.side_effect = [account, models]
                with self.assertRaises(RuntimeError):
                    self.client._query("synthetic-access")

    def test_unknown_or_invalid_quota_is_not_reported_as_full_or_exhausted(self):
        for quota in ({"remainingFraction": None}, {"remainingFraction": True},
                      {"remainingFraction": "unknown"}, {"remainingFraction": float("nan")},
                      {"remainingFraction": float("inf")}, {"resetTime": ""}, {}):
            with self.subTest(quota=quota):
                account, _ = responses()
                self.http.side_effect = [account, {"models": {
                    "bad": {"displayName": "Gemini Unknown", "quotaInfo": quota}}}]
                with self.assertRaises(RuntimeError):
                    self.client._query("synthetic-access")

    def test_unauthorized_retries_once_with_native_refreshed_token(self):
        self.http.side_effect = [HttpError(401, "expired"), *responses()]
        with patch.object(self.client, "_access_token", return_value="old"), patch.object(
            self.client, "_refresh", return_value="new"
        ) as refresh:
            self.assertIsNotNone(self.client.query())
        refresh.assert_called_once_with("old")
        self.assertEqual(self.http.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer new")

    def test_other_http_errors_do_not_refresh_or_loop(self):
        for status in (403, 429, 500):
            with self.subTest(status=status), patch.object(self.client, "_access_token", return_value="old"), patch.object(
                self.client, "_refresh"
            ) as refresh:
                self.http.side_effect = HttpError(status, "unavailable")
                with self.assertRaises(HttpError):
                    self.client.query()
                refresh.assert_not_called()
        self.http.side_effect = [HttpError(401, "expired"), HttpError(401, "still expired")]
        with patch.object(self.client, "_access_token", return_value="old"), patch.object(
            self.client, "_refresh", return_value="new"
        ) as refresh:
            with self.assertRaises(HttpError):
                self.client.query()
            refresh.assert_called_once()

    def test_refresh_uses_native_models_and_keyring_context(self):
        with patch("shutil.which", return_value="/usr/bin/agy"), patch.object(
            self.client, "_access_token", side_effect=["old", "new"]
        ), patch.dict(os.environ, {"SSH_CLIENT": "test", "SSH_CONNECTION": "test", "SSH_TTY": "test"}), patch(
            "subprocess.run", return_value=MagicMock(returncode=0)
        ) as run:
            self.assertEqual(self.client._refresh("old"), "new")
        self.assertEqual(run.call_args.args[0], ["/usr/bin/agy", "models"])
        env = run.call_args.kwargs["env"]
        self.assertFalse(any(k in env for k in ("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY")))
        self.assertEqual(env["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")
        self.assertIs(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_refresh_adopts_concurrent_native_update(self):
        with patch("shutil.which", return_value="/usr/bin/agy"), patch.object(
            self.client, "_access_token", return_value="already-refreshed"
        ), patch("subprocess.run") as run:
            self.assertEqual(self.client._refresh("old"), "already-refreshed")
            run.assert_not_called()

    def test_failed_native_refresh_keeps_auth_error(self):
        self.http.side_effect = HttpError(401, "expired")
        with patch.object(self.client, "_access_token", return_value="old"), patch.object(
            self.client, "_refresh", return_value=None
        ):
            with self.assertRaises(HttpError):
                self.client.query()


class ProviderFallbackTests(unittest.TestCase):
    def test_scoped_accounts_and_explicit_endpoints_never_use_default_keyring(self):
        with tempfile.TemporaryDirectory() as temp:
            for cfg in ({"account_name": "work", "profile_dir": temp}, {"profile_dir": temp},
                        {"port": 12345}, {"csrf_token": "synthetic"}):
                with self.subTest(cfg=cfg), patch(
                    "tokenmaster.providers.antigravity.AntigravityCliClient"
                ) as client:
                    self.assertIsNone(AntigravityProvider(cfg)._query_cli_status())
                    client.assert_not_called()
            with patch.dict(os.environ, {"ANTIGRAVITY_DIR": temp}), patch(
                "tokenmaster.providers.antigravity.AntigravityCliClient"
            ) as client:
                self.assertIsNone(AntigravityProvider({})._query_cli_status())
                client.assert_not_called()

    def test_legacy_rpc_is_preferred(self):
        provider = AntigravityProvider({})
        with patch.object(provider, "_query_language_server", return_value={"userStatus": {}}), patch.object(
            provider, "_query_cli_status"
        ) as cli, patch.object(provider, "_scan_local_brain", return_value={"brain_accessible": False}):
            report = provider.fetch()
        self.assertEqual(report.source, "Connect-RPC GetUserStatus")
        cli.assert_not_called()

    def test_api_failure_is_actionable_and_does_not_leak_error_payload(self):
        provider = AntigravityProvider({})
        with patch.object(provider, "_query_language_server", return_value=None), patch.object(
            provider, "_query_cli_status", side_effect=HttpError(403, "synthetic-secret", "synthetic-secret")
        ), patch.object(provider, "_scan_local_brain", return_value={"brain_accessible": False}):
            report = provider.fetch()
        self.assertEqual(report.status, "error")
        self.assertIn("HTTP 403", report.error_message)
        self.assertNotIn("synthetic-secret", json.dumps(report.to_dict()))
        self.assertIn("keyring", report.auth_hint)


if __name__ == "__main__":
    unittest.main()
