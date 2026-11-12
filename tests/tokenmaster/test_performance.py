"""Tests for concurrency helpers, the global fetch deadline, and parallel provider I/O."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tokenmaster import providers as providers_pkg
from tokenmaster.config import ConfigManager
from tokenmaster.models import ProviderReport, SubMetric
from tokenmaster.providers import fetch_providers, resolve_deadline
from tokenmaster.providers.base import (
    BaseProvider,
    HttpError,
    atomic_write_text,
    critical_section,
    is_unauthorized,
    run_concurrently,
    wait_for_critical_sections,
)
from tokenmaster.providers.antigravity import AntigravityProvider
from tokenmaster.providers.claude import ClaudeProvider
from tokenmaster.providers.cursor import CursorProvider
from tokenmaster.providers.grok import GrokProvider
from tokenmaster.providers.openrouter import OpenRouterProvider

# Every "parallel" test uses SLEEP per call and asserts elapsed < PARALLEL_MAX, while
# sequential execution would take >= 2 * SLEEP: a wide margin against CI jitter.
SLEEP = 0.3
PARALLEL_MAX = 0.5


class TestRunConcurrently(unittest.TestCase):
    def test_runs_in_parallel(self) -> None:
        def slow(val: int) -> Any:
            return lambda: (time.sleep(SLEEP), val)[1]

        started = time.perf_counter()
        out = run_concurrently({"a": slow(1), "b": slow(2), "c": slow(3)})
        elapsed = time.perf_counter() - started
        self.assertEqual({k: v for k, (v, _) in out.items()}, {"a": 1, "b": 2, "c": 3})
        self.assertLess(elapsed, PARALLEL_MAX, "tasks should overlap, not run back-to-back")

    def test_captures_exceptions_per_task(self) -> None:
        def boom() -> Any:
            raise HttpError(401, "Unauthorized")

        out = run_concurrently({"ok": lambda: 42, "bad": boom})
        self.assertEqual(out["ok"], (42, None))
        self.assertIsNone(out["bad"][0])
        self.assertTrue(is_unauthorized(out["bad"][1]))

    def test_preserves_key_order_and_handles_trivial_inputs(self) -> None:
        self.assertEqual(run_concurrently({}), {})
        self.assertEqual(run_concurrently({"only": lambda: "x"}), {"only": ("x", None)})
        out = run_concurrently({"z": lambda: 1, "a": lambda: 2})
        self.assertEqual(list(out), ["z", "a"])

    def test_is_unauthorized(self) -> None:
        self.assertTrue(is_unauthorized(HttpError(401, "x")))
        self.assertFalse(is_unauthorized(HttpError(403, "x")))
        self.assertFalse(is_unauthorized(RuntimeError("HTTP 401")))
        self.assertFalse(is_unauthorized(None))


class TestCriticalSectionsAndAtomicWrite(unittest.TestCase):
    def test_wait_blocks_until_critical_section_exits(self) -> None:
        entered = threading.Event()

        def worker() -> None:
            with critical_section():
                entered.set()
                time.sleep(SLEEP)

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        entered.wait(2)
        self.assertFalse(wait_for_critical_sections(timeout=0.01))
        started = time.perf_counter()
        self.assertTrue(wait_for_critical_sections(timeout=5))
        self.assertGreater(time.perf_counter() - started, 0.05)
        th.join(2)

    def test_atomic_write_replaces_file_and_cleans_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "auth.json"
            path.write_text("old", encoding="utf-8")
            atomic_write_text(path, '{"new": true}')
            self.assertEqual(path.read_text(encoding="utf-8"), '{"new": true}')
            self.assertEqual(oct(path.stat().st_mode & 0o777), oct(0o600))
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["auth.json"])


# --- Fake providers for fetch_providers --------------------------------------


class _FastProvider(BaseProvider):
    provider_id = "codex"
    display_name = "Fast"

    def fetch(self) -> ProviderReport:
        report = self.new_report()
        report.status = "ok"
        return report


class _HungProvider(BaseProvider):
    """Blocks on a per-test event; publishes partial data before blocking."""

    provider_id = "claude"
    display_name = "Hung"
    release: threading.Event = threading.Event()  # replaced per test in setUp
    starts = 0

    def fetch(self) -> ProviderReport:
        type(self).starts += 1
        report = self.new_report()
        report.sub_metrics.append(SubMetric(name="local", used_str="1", remaining_str="—"))
        self.release.wait(10)
        report.status = "ok"
        return report


class _CrashProvider(BaseProvider):
    provider_id = "grok"
    display_name = "Crash"

    def fetch(self) -> ProviderReport:
        raise KeyError("boom")


class TestFetchDeadline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = ConfigManager(config_path=Path(self.tmp.name) / "config.json")
        self.cfg.data["deadline_seconds"] = 0.3
        _HungProvider.release = threading.Event()  # fresh per test: no cross-test leakage
        _HungProvider.starts = 0

    def tearDown(self) -> None:
        _HungProvider.release.set()
        # Let released daemon threads finish so _INFLIGHT is clean for the next test.
        deadline = time.monotonic() + 2
        while providers_pkg._INFLIGHT and time.monotonic() < deadline:
            time.sleep(0.01)
        self.tmp.cleanup()

    def _classes(self) -> dict[str, type[BaseProvider]]:
        return {"codex": _FastProvider, "claude": _HungProvider, "grok": _CrashProvider}

    def test_hung_provider_times_out_with_partial_data(self) -> None:
        with patch.dict(providers_pkg.PROVIDER_CLASSES, self._classes(), clear=True):
            started = time.perf_counter()
            reports = fetch_providers(self.cfg, selected=["codex", "claude", "grok"])
            elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1.5)
        by_id = {r.provider_id: r for r in reports}
        self.assertEqual([r.provider_id for r in reports], ["codex", "claude", "grok"])
        self.assertEqual(by_id["codex"].status, "ok")
        self.assertIsNotNone(by_id["codex"].fetch_ms)
        hung = by_id["claude"]
        self.assertEqual(hung.status, "partial")
        self.assertIn("Timed out", hung.error_message or "")
        self.assertEqual([m.name for m in hung.sub_metrics], ["local"])
        self.assertEqual(by_id["grok"].status, "error")
        self.assertIn("boom", by_id["grok"].error_message or "")

    def test_overlapping_refresh_joins_inflight_job_instead_of_restarting(self) -> None:
        with patch.dict(providers_pkg.PROVIDER_CLASSES, self._classes(), clear=True):
            first = fetch_providers(self.cfg, selected=["claude"])
            second = fetch_providers(self.cfg, selected=["claude"])
            self.assertEqual(_HungProvider.starts, 1, "must not start a second concurrent fetch")
            self.assertEqual(first[0].status, "partial")
            self.assertEqual(second[0].status, "partial")
            _HungProvider.release.set()
            time.sleep(0.1)
            third = fetch_providers(self.cfg, selected=["claude"])
        self.assertEqual(third[0].status, "ok")

    def test_deadline_resolution(self) -> None:
        cases = {
            None: 20.0,
            "garbage": 20.0,
            -5: 20.0,
            True: 20.0,
            float("nan"): 20.0,
            3: 3.0,
            0: None,
            "0": None,
        }
        for raw, expected in cases.items():
            self.cfg.data["deadline_seconds"] = raw
            self.assertEqual(resolve_deadline(self.cfg, 8.0), expected, raw)
        self.cfg.data["deadline_seconds"] = 1e30
        huge = resolve_deadline(self.cfg, 8.0)
        self.assertIsNotNone(huge)
        self.assertLessEqual(huge or 0, threading.TIMEOUT_MAX)

    def test_huge_deadline_does_not_overflow_join(self) -> None:
        self.cfg.data["deadline_seconds"] = 1e30
        with patch.dict(providers_pkg.PROVIDER_CLASSES, {"codex": _FastProvider}, clear=True):
            reports = fetch_providers(self.cfg, selected=["codex"])
        self.assertEqual(reports[0].status, "ok")


class TestParallelProviderIO(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._env = patch.dict(os.environ, {"TOKENMASTER_CACHE_DIR": str(self.root / "cache")})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self.tmp.cleanup()

    def test_cursor_rpcs_run_concurrently(self) -> None:
        prov = CursorProvider({}, timeout=5)
        calls: list[str] = []

        def fake_http(url: str, **_: Any) -> dict[str, Any]:
            calls.append(url.rsplit("/", 1)[-1])
            time.sleep(SLEEP)
            if url.endswith("GetCurrentPeriodUsage"):
                return {"planUsage": {"totalPercentUsed": 10.0}, "billingCycleEnd": "1894268910609"}
            if url.endswith("GetPlanInfo"):
                return {"planInfo": {"planName": "Pro"}}
            return {}

        report = ProviderReport(provider_id="cursor", display_name="Cursor")
        with patch.object(prov, "http_json", side_effect=fake_http):
            started = time.perf_counter()
            ok = prov._try_fetch_with_token(report, "tok", None, [])
            elapsed = time.perf_counter() - started

        self.assertTrue(ok)
        self.assertEqual(sorted(calls), ["GetCreditGrantsBalance", "GetCurrentPeriodUsage", "GetPlanInfo"])
        self.assertLess(elapsed, PARALLEL_MAX)
        self.assertEqual(report.plan_tier, "Cursor Pro")
        self.assertAlmostEqual(report.weekly.used_percent or 0.0, 10.0)

    def test_cursor_401_discards_sibling_results(self) -> None:
        prov = CursorProvider({}, timeout=5)

        def fake_http(url: str, **_: Any) -> dict[str, Any]:
            if url.endswith("GetCurrentPeriodUsage"):
                raise HttpError(401, "Unauthorized")
            return {"planInfo": {"planName": "Pro", "includedAmountCents": 2000}}

        report = ProviderReport(provider_id="cursor", display_name="Cursor")
        with patch.object(prov, "http_json", side_effect=fake_http):
            ok = prov._try_fetch_with_token(report, "tok", None, [])

        self.assertFalse(ok)
        self.assertIn("401", report.error_message or "")
        self.assertIsNone(report.plan_tier)

    def test_cursor_non_401_failure_still_uses_sibling_results(self) -> None:
        prov = CursorProvider({}, timeout=5)

        def fake_http(url: str, **_: Any) -> dict[str, Any]:
            if url.endswith("GetCurrentPeriodUsage"):
                raise HttpError(503, "Service Unavailable")
            if url.endswith("GetPlanInfo"):
                return {"planInfo": {"planName": "Pro", "includedAmountCents": 2000}}
            return {}

        report = ProviderReport(provider_id="cursor", display_name="Cursor")
        with patch.object(prov, "http_json", side_effect=fake_http):
            ok = prov._try_fetch_with_token(report, "tok", None, [])

        self.assertTrue(ok)
        self.assertEqual(report.plan_tier, "Cursor Pro")
        self.assertAlmostEqual(report.credits.limit or 0.0, 20.0)

    def test_grok_billing_and_user_run_concurrently(self) -> None:
        with patch.dict(os.environ, {"GROK_HOME": str(self.root / ".grok")}):
            prov = GrokProvider({}, timeout=5)

            def fake_http(url: str, **_: Any) -> dict[str, Any]:
                time.sleep(SLEEP)
                if "billing" in url:
                    raise HttpError(500, "Server Error")
                return {"subscriptionTier": "GrokPro"}

            with patch.object(prov, "http_json", side_effect=fake_http):
                started = time.perf_counter()
                billing, user, err = prov._query_grok_cli_proxy("tok")
                elapsed = time.perf_counter() - started

        self.assertLess(elapsed, PARALLEL_MAX)
        self.assertIsNone(billing)
        self.assertEqual(user, {"subscriptionTier": "GrokPro"})
        self.assertIn("500", err or "")

    def test_grok_persist_is_atomic_and_preserves_expires_at_format(self) -> None:
        grok_home = self.root / ".grok"
        grok_home.mkdir()
        scope = "https://auth.x.ai::client"
        (grok_home / "auth.json").write_text(
            json.dumps({scope: {"key": "old", "refresh_token": "rt0", "expires_at": "2026-07-20T00:01:15.262732Z"}}),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"GROK_HOME": str(grok_home)}):
            GrokProvider({})._persist_refreshed_grok_token(scope, "new", "rt1", 3600)
        saved = json.loads((grok_home / "auth.json").read_text(encoding="utf-8"))[scope]
        self.assertEqual((saved["key"], saved["refresh_token"]), ("new", "rt1"))
        self.assertIsInstance(saved["expires_at"], str)
        self.assertTrue(saved["expires_at"].endswith("Z"))
        self.assertEqual([p.name for p in grok_home.iterdir()], ["auth.json"])

    def test_grok_refresh_adopts_token_rotated_by_sibling(self) -> None:
        grok_home = self.root / ".grok"
        grok_home.mkdir()
        scope = "https://auth.x.ai::client"
        # Disk already has a newer refresh token than the entry we hold.
        (grok_home / "auth.json").write_text(
            json.dumps({scope: {"key": "fresh", "refresh_token": "rt-new"}}), encoding="utf-8"
        )
        with patch.dict(os.environ, {"GROK_HOME": str(grok_home)}):
            prov = GrokProvider({})
            with patch.object(prov, "_refresh_grok_oidc_token_locked") as network:
                tok = prov._refresh_grok_oidc_token(scope, {"key": "stale", "refresh_token": "rt-old"})
        self.assertEqual(tok, "fresh")
        network.assert_not_called()

    def test_openrouter_calls_run_concurrently(self) -> None:
        prov = OpenRouterProvider({"api_key": "sk-or-test"}, timeout=5)

        def fake_http(url: str, **_: Any) -> dict[str, Any]:
            time.sleep(SLEEP)
            if url.endswith("/key"):
                return {"data": {"usage_weekly": 1.0, "usage": 2.0}}
            return {"data": {"total_credits": 10.0, "total_usage": 2.0}}

        with patch.object(prov, "http_json", side_effect=fake_http):
            started = time.perf_counter()
            rep = prov.fetch()
            elapsed = time.perf_counter() - started
        self.assertLess(elapsed, PARALLEL_MAX)
        self.assertEqual(rep.status, "ok")
        self.assertEqual(rep.source, "/api/v1/key + /api/v1/credits")
        self.assertAlmostEqual(rep.credits.remaining or 0.0, 8.0)

    def test_openrouter_non_dict_response_is_reported_as_error(self) -> None:
        prov = OpenRouterProvider({"api_key": "sk-or-test"}, timeout=5)
        with patch.object(prov, "http_json", return_value=["unexpected"]):
            rep = prov.fetch()
        self.assertIn("Unexpected /api/v1/key response", rep.error_message or "")

    def test_claude_local_scan_overlaps_oauth_request(self) -> None:
        prov = ClaudeProvider({}, timeout=5)

        def slow_scan() -> dict[str, Any]:
            time.sleep(SLEEP)
            return {"found_files": 0, "tokens_7d": 0, "cost_7d_usd": 0.0, "tokens_30d": 0, "cost_30d_usd": 0.0}

        def slow_http(url: str, **_: Any) -> dict[str, Any]:
            time.sleep(SLEEP)
            return {"seven_day": {"utilization": 25.0}}

        with patch.object(prov, "_load_oauth_credentials", return_value=("tok", "max", None)), patch.object(
            prov, "_scan_local_projects", side_effect=slow_scan
        ), patch.object(prov, "http_json", side_effect=slow_http):
            started = time.perf_counter()
            rep = prov.fetch()
            elapsed = time.perf_counter() - started
        self.assertLess(elapsed, PARALLEL_MAX)
        self.assertEqual(rep.status, "ok")
        self.assertAlmostEqual(rep.weekly.used_percent or 0.0, 25.0)

    def test_claude_local_scan_failure_is_surfaced(self) -> None:
        prov = ClaudeProvider({}, timeout=5)
        with patch.object(prov, "_load_oauth_credentials", return_value=(None, None, None)), patch.object(
            prov, "_scan_local_projects", side_effect=OSError("disk gone")
        ):
            rep = prov.fetch()
        self.assertIn("disk gone", rep.error_message or "")


class TestIncrementalLocalScanCaches(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache_dir = self.root / "cache"
        self._env = patch.dict(os.environ, {"TOKENMASTER_CACHE_DIR": str(self.cache_dir)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self.tmp.cleanup()

    def test_claude_scan_caches_and_splits_straddling_files(self) -> None:
        from datetime import datetime, timezone
        from tokenmaster.providers import claude as claude_mod

        cdir = self.root / ".claude"
        pdir = cdir / "projects" / "demo"
        pdir.mkdir(parents=True)

        now = time.time()
        ts_recent = datetime.fromtimestamp(now - 3600, tz=timezone.utc).isoformat()
        ts_old_in_30d = datetime.fromtimestamp(now - 10 * 86400, tz=timezone.utc).isoformat()

        # File 1: entirely within 7d
        f1 = pdir / "recent.jsonl"
        f1.write_text(
            json.dumps(
                {
                    "timestamp": ts_recent,
                    "requestId": "r1",
                    "message": {
                        "id": "m1",
                        "model": "claude-sonnet-4",
                        "usage": {"input_tokens": 1000, "output_tokens": 500},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        # File 2: straddles the 7d cutoff (one turn 10d ago, one turn 1h ago)
        f2 = pdir / "straddle.jsonl"
        f2.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "timestamp": ts_old_in_30d,
                            "requestId": "r2",
                            "message": {
                                "id": "m2",
                                "model": "claude-sonnet-4",
                                "usage": {"input_tokens": 2000, "output_tokens": 0},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "timestamp": ts_recent,
                            "requestId": "r3",
                            "message": {
                                "id": "m3",
                                "model": "claude-sonnet-4",
                                "usage": {"input_tokens": 400, "output_tokens": 100},
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(cdir)}):
            prov = ClaudeProvider({})
            s1 = prov._scan_local_projects()
            self.assertEqual(s1["tokens_7d"], 1500 + 500)
            self.assertEqual(s1["tokens_30d"], 1500 + 2000 + 500)

            # Warm scan should hit cache without calling summarize_claude_jsonl
            with patch.object(claude_mod, "summarize_claude_jsonl", side_effect=AssertionError("cache miss")):
                s2 = prov._scan_local_projects()
            self.assertEqual(s1["tokens_7d"], s2["tokens_7d"])
            self.assertEqual(s1["tokens_30d"], s2["tokens_30d"])

            # Advance virtual clock by 8 days so f1's turn (was 1h ago, now 8d1h ago)
            # ages across the 7d boundary without file mtime/size changing.
            with patch.object(claude_mod.time, "time", return_value=now + 8 * 86400), patch.object(
                claude_mod, "summarize_claude_jsonl", side_effect=AssertionError("cache miss")
            ):
                s_aged = prov._scan_local_projects()
            self.assertEqual(s_aged["tokens_7d"], 0)
            self.assertEqual(s_aged["tokens_30d"], 1500 + 2000 + 500)

    def test_claude_cross_file_dedup_and_large_straddling_file(self) -> None:
        from datetime import datetime, timezone
        from tokenmaster.providers import claude as claude_mod

        cdir = self.root / ".claude_dedup"
        pdir = cdir / "projects" / "demo"
        pdir.mkdir(parents=True)

        now = time.time()
        ts_recent = datetime.fromtimestamp(now - 3600, tz=timezone.utc).isoformat()
        ts_old_in_30d = datetime.fromtimestamp(now - 10 * 86400, tz=timezone.utc).isoformat()

        # Two files containing the exact same (message.id, requestId) turn
        shared_turn = json.dumps(
            {
                "timestamp": ts_recent,
                "requestId": "req-dup",
                "message": {
                    "id": "msg-dup",
                    "model": "claude-sonnet-4",
                    "usage": {"input_tokens": 800, "output_tokens": 200},
                },
            }
        )
        (pdir / "a.jsonl").write_text(shared_turn + "\n", encoding="utf-8")
        (pdir / "b.jsonl").write_text(shared_turn + "\n", encoding="utf-8")

        # File with >MAX_CACHED_EVENTS_PER_FILE (260) turns straddling the 7d cutoff
        big_lines = []
        for idx in range(260):
            ts_val = ts_old_in_30d if idx < 10 else ts_recent
            big_lines.append(
                json.dumps(
                    {
                        "timestamp": ts_val,
                        "requestId": f"r-big-{idx}",
                        "message": {
                            "id": f"m-big-{idx}",
                            "model": "claude-sonnet-4",
                            "usage": {"input_tokens": 10, "output_tokens": 0},
                        },
                    }
                )
            )
        (pdir / "c_big.jsonl").write_text("\n".join(big_lines) + "\n", encoding="utf-8")

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(cdir)}):
            prov = ClaudeProvider({})
            # Cold scan: shared_turn counted ONCE (1000 tok), c_big has 250*10=2500 in 7d and 2600 in 30d
            s_cold = prov._scan_local_projects()
            self.assertEqual(s_cold["tokens_7d"], 1000 + 2500)
            self.assertEqual(s_cold["tokens_30d"], 1000 + 2600)

            # Warm scan: summarize_claude_jsonl must NOT be called at all (even for >256-turn c_big),
            # and _accumulate_straddling_file is called once for c_big while preserving cross-file dedup.
            with patch.object(
                claude_mod, "summarize_claude_jsonl", side_effect=AssertionError("redundant full parse")
            ), patch.object(
                prov, "_accumulate_straddling_file", wraps=prov._accumulate_straddling_file
            ) as spy_acc:
                s_warm = prov._scan_local_projects()
            self.assertEqual(spy_acc.call_count, 1)
            self.assertEqual(s_warm["tokens_7d"], 1000 + 2500)
            self.assertEqual(s_warm["tokens_30d"], 1000 + 2600)

    def test_grok_sessions_scan_cache_and_prune(self) -> None:
        grok_home = self.root / ".grok_test"
        s1_dir = grok_home / "sessions" / "s1"
        s2_dir = grok_home / "sessions" / "s2"
        s1_dir.mkdir(parents=True)
        s2_dir.mkdir(parents=True)

        f1 = s1_dir / "summary.json"
        f1.write_text(json.dumps({"totalTokens": 1234, "costUsdTicks": 50000000000}), encoding="utf-8")
        # Non-dict JSON file should also be cached as invalid and not re-read on warm scan
        f2 = s2_dir / "summary.json"
        f2.write_text("[]", encoding="utf-8")

        with patch.dict(os.environ, {"GROK_HOME": str(grok_home)}):
            prov = GrokProvider({})
            st1 = prov._scan_local_grok_sessions()
            self.assertEqual(st1["tokens_7d"], 1234)
            self.assertAlmostEqual(st1["cost_7d_usd"], 5.0)

            # Warm scan hits cache for both f1 and f2 without calling Path.read_text
            orig_read_text = Path.read_text

            def fail_on_session_read(self_path: Path, *a: Any, **kw: Any) -> str:
                if self_path.name == "summary.json":
                    raise AssertionError(f"unexpected disk read of {self_path}")
                return orig_read_text(self_path, *a, **kw)

            with patch.object(Path, "read_text", fail_on_session_read):
                st2 = prov._scan_local_grok_sessions()
            self.assertEqual(st2["tokens_7d"], 1234)

            # Deleting f1 prunes it from cache
            f1.unlink()
            st3 = prov._scan_local_grok_sessions()
            self.assertEqual(st3["tokens_7d"], 0)
            cache_raw = json.loads((self.cache_dir / "grok_sessions_scan.json").read_text(encoding="utf-8"))
            self.assertIn(str(f2), cache_raw.get("entries", {}))
            self.assertNotIn(str(f1), cache_raw.get("entries", {}))

    def test_antigravity_rpc_endpoint_cache_and_brain_cache(self) -> None:
        agy_dir = self.root / ".antigravity"
        log_dir = agy_dir / "brain" / "conv-1" / ".system_generated" / "logs"
        log_dir.mkdir(parents=True)
        tfile = log_dir / "transcript.jsonl"
        tfile.write_text('{"type": "PLANNER_RESPONSE"}\n{"type": "PLANNER_RESPONSE"}\n', encoding="utf-8")

        with patch.dict(os.environ, {"ANTIGRAVITY_DIR": str(agy_dir)}):
            prov = AntigravityProvider({})
            mock_rpc = {"userStatus": {"userTier": {"name": "Ultra"}}}
            with patch.object(prov, "_discover_language_server", return_value=[(61234, "csrf-xyz")]) as disc, patch.object(
                prov, "http_json", return_value=mock_rpc
            ):
                res1 = prov._query_language_server()
                self.assertIsNotNone(res1)
                self.assertEqual(disc.call_count, 1)

                # Second call uses cached (scheme, port, csrf_token) without running ps/lsof
                res2 = prov._query_language_server()
                self.assertIsNotNone(res2)
                self.assertEqual(disc.call_count, 1)

            # When cached endpoint fails, it is unlinked and falls back to _discover_language_server
            def probe_side_effect(scheme: str, port: int, tok: str, payload: Any, timeout: float = 2.0) -> Any:
                if port == 61234:
                    return None
                return mock_rpc

            with patch.object(prov, "_discover_language_server", return_value=[(62222, "csrf-new")]) as disc2, patch.object(
                prov, "_probe_endpoint", side_effect=probe_side_effect
            ):
                res3 = prov._query_language_server()
                self.assertIsNotNone(res3)
                self.assertEqual(disc2.call_count, 1)
                self.assertEqual(prov._load_cached_endpoint(), ("https", 62222, "csrf-new"))

            # Invalid port (bool or >65535) or newline in token is rejected by _load_cached_endpoint
            prov._rpc_cache_path().write_text(
                json.dumps({"scheme": "https", "port": True, "csrf_token": "tok"}), encoding="utf-8"
            )
            self.assertIsNone(prov._load_cached_endpoint())
            prov._rpc_cache_path().write_text(
                json.dumps({"scheme": "https", "port": 70000, "csrf_token": "tok"}), encoding="utf-8"
            )
            self.assertIsNone(prov._load_cached_endpoint())
            prov._rpc_cache_path().write_text(
                json.dumps({"scheme": "https", "port": 12345, "csrf_token": "bad\r\ntok"}), encoding="utf-8"
            )
            self.assertIsNone(prov._load_cached_endpoint())

            b1 = prov._scan_local_brain()
            self.assertEqual(b1["steps_7d"], 2)
            self.assertTrue((self.cache_dir / "antigravity_brain_scan.json").is_file())

    def test_cursor_lazy_keychain_lookup(self) -> None:
        cursor_home = self.root / ".cursor_empty"
        cursor_home.mkdir()
        with patch.dict(os.environ, {"CURSOR_HOME": str(cursor_home)}):
            prov = CursorProvider({})
            queried: list[str] = []

            def fake_kc(service: str, account: str = "cursor-user") -> Any:
                queried.append(service)
                if service == "cursor-access-token":
                    return "valid_opaque_token"
                return None

            with patch.object(prov, "_read_keychain_secret", side_effect=fake_kc):
                auth = prov._load_cursor_cli_auth()
            self.assertEqual(auth, {"access_token": "valid_opaque_token"})
            self.assertEqual(queried, ["cursor-access-token"])

    def test_cursor_lazy_keychain_fallbacks_on_safe_storage_and_401(self) -> None:
        cursor_home = self.root / ".cursor_safe"
        cursor_home.mkdir()
        # 1) cursor-safe-storage: blob in auth.json is ignored so Keychain fallbacks load
        (cursor_home / "auth.json").write_text(
            json.dumps({"accessToken": "cursor-safe-storage:encrypted_blob"}),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"CURSOR_HOME": str(cursor_home)}):
            prov = CursorProvider({})
            with patch.object(
                prov,
                "_read_keychain_secret",
                side_effect=lambda svc, account="cursor-user": {
                    "cursor-access-token": None,
                    "cursor-refresh-token": "kc-rt-1",
                    "cursor-api-key": "kc-api-1",
                }.get(svc),
            ):
                auth = prov._load_cursor_cli_auth()
            self.assertNotIn("access_token", auth)
            self.assertEqual(auth.get("refresh_token"), "kc-rt-1")
            self.assertEqual(auth.get("api_key"), "kc-api-1")

        # 2) Non-expired access_token in auth.json fails with 401 -> lazily queries Keychain
        #    cursor-refresh-token (taking priority over stale state.vscdb refreshToken) and cursor-api-key
        (cursor_home / "auth.json").write_text(
            json.dumps({"accessToken": "revoked_opaque_tok"}),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"CURSOR_HOME": str(cursor_home)}):
            prov = CursorProvider({})
            kc_calls: list[str] = []

            def fake_kc_401(service: str, account: str = "cursor-user") -> Any:
                kc_calls.append(service)
                if service == "cursor-refresh-token":
                    return "kc-rt-fresh"
                if service == "cursor-api-key":
                    return "kc-api-fallback"
                return None

            tried_tokens: list[str] = []

            def fake_try_fetch(
                report: Any, token: str, cookie: Any, sources: list[str], backend_url: str = ""
            ) -> bool:
                tried_tokens.append(token)
                if token == "kc-api-fallback":
                    report.status = "ok"
                    return True
                return False

            refreshed_order: list[str] = []

            def fake_refresh(rt: str, backend_url: str = "") -> Any:
                refreshed_order.append(rt)
                return None  # force fallback to lazy cursor-api-key

            with patch.object(
                prov,
                "_read_sqlite_auth",
                return_value={"cursorAuth/refreshToken": "stale-db-rt"},
            ), patch.object(
                prov, "_read_keychain_secret", side_effect=fake_kc_401
            ), patch.object(
                prov, "_try_fetch_with_token", side_effect=fake_try_fetch
            ), patch.object(
                prov, "_refresh_access_token", side_effect=fake_refresh
            ):
                rep = prov.fetch()

            self.assertEqual(rep.status, "ok")
            self.assertIn("cursor-refresh-token", kc_calls)
            self.assertIn("cursor-api-key", kc_calls)
            # Keychain refresh token must be tried before stale state.vscdb refresh token
            self.assertEqual(refreshed_order, ["kc-rt-fresh", "stale-db-rt"])
            self.assertEqual(tried_tokens, ["revoked_opaque_tok", "kc-api-fallback"])


if __name__ == "__main__":
    unittest.main()
