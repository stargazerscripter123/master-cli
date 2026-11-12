"""Tests for the tail-reading Codex rollout scanner and its on-disk cache."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tokenmaster.cache import FileScanCache
from tokenmaster.config import ConfigManager
from tokenmaster.providers import codex as codex_mod
from tokenmaster.providers.codex import CodexProvider, summarize_rollout_tail


def _token_count_line(in_tok: int, cached: int, out_tok: int, rate_limits: Any = None) -> str:
    payload: dict[str, Any] = {
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": in_tok,
                "cached_input_tokens": cached,
                "output_tokens": out_tok,
            }
        },
    }
    if rate_limits is not None:
        payload["rate_limits"] = rate_limits
    return json.dumps({"timestamp": "2026-11-08T00:00:00Z", "type": "event_msg", "payload": payload})


def _turn_context_line(model: str) -> str:
    return json.dumps({"type": "turn_context", "payload": {"model": model, "cwd": "/tmp"}})


def _filler_line(size: int) -> str:
    return json.dumps({"type": "response_item", "payload": {"type": "message", "text": "x" * size}})


class TestSummarizeRolloutTail(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, name: str, lines: list[str]) -> Path:
        p = self.root / name
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_reads_only_tail_when_token_count_is_near_eof(self) -> None:
        lines = [
            _turn_context_line("gpt-5"),
            _filler_line(2_000_000),  # 2 MB of early content that must never be read
            _token_count_line(100, 10, 5),
            _token_count_line(300, 50, 20, {"primary": {"used_percent": 12.5}}),
            _filler_line(100),
        ]
        p = self._write("big.jsonl", lines)
        size = p.stat().st_size
        self.assertGreater(size, 2_000_000)

        s = summarize_rollout_tail(p, size)
        self.assertEqual(s["total_token_usage"]["input_tokens"], 300)
        self.assertEqual(s["total_token_usage"]["output_tokens"], 20)
        self.assertEqual(s["rate_limits"], {"primary": {"used_percent": 12.5}})
        self.assertLessEqual(s["bytes_read"], codex_mod.TAIL_CHUNK_INITIAL)

    def test_grows_tail_when_token_count_is_buried(self) -> None:
        lines = [
            _turn_context_line("gpt-5"),
            _token_count_line(999, 0, 7),
            _filler_line(200_000),  # > 64 KB of later output
        ]
        p = self._write("buried.jsonl", lines)
        s = summarize_rollout_tail(p)
        self.assertEqual(s["total_token_usage"]["input_tokens"], 999)
        self.assertEqual(s["model"], "gpt-5")
        self.assertGreater(s["bytes_read"], codex_mod.TAIL_CHUNK_INITIAL)

    def test_gives_up_beyond_cap_without_reading_whole_file(self) -> None:
        lines = [_token_count_line(5, 0, 5), _filler_line(50_000)]
        p = self._write("deep.jsonl", lines)
        with patch.object(codex_mod, "TAIL_CHUNK_INITIAL", 256), patch.object(
            codex_mod, "TAIL_CHUNK_MAX", 1024
        ):
            s = summarize_rollout_tail(p)
        self.assertIsNone(s["total_token_usage"])
        self.assertLessEqual(s["bytes_read"], 256 + 1024)

    def test_ignores_decoys_and_garbage_lines(self) -> None:
        decoy = json.dumps(
            {"type": "response_item", "payload": {"type": "message", "text": 'see "token_count" in logs'}}
        )
        lines = [
            _token_count_line(1, 0, 1),
            "{not json at all",
            decoy,
            "",
        ]
        p = self._write("decoy.jsonl", lines)
        s = summarize_rollout_tail(p)
        self.assertEqual(s["total_token_usage"]["input_tokens"], 1)

    def test_model_comes_from_last_turn_context(self) -> None:
        lines = [
            _turn_context_line("gpt-4.1"),
            _token_count_line(1, 0, 1),
            _turn_context_line("o3"),
            _token_count_line(2, 0, 2),
        ]
        p = self._write("models.jsonl", lines)
        s = summarize_rollout_tail(p)
        self.assertEqual(s["model"], "o3")
        self.assertEqual(s["total_token_usage"]["input_tokens"], 2)

    def test_empty_and_no_token_count_files(self) -> None:
        empty = self._write("empty.jsonl", [])
        self.assertIsNone(summarize_rollout_tail(empty)["total_token_usage"])
        plain = self._write("plain.jsonl", [_turn_context_line("gpt-5"), _filler_line(10)])
        s = summarize_rollout_tail(plain)
        self.assertIsNone(s["total_token_usage"])
        self.assertEqual(s["model"], "gpt-5")


class TestCodexScanWithCache(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.codex_home = self.root / ".codex"
        self.sessions = self.codex_home / "sessions" / "2026" / "11" / "08"
        self.sessions.mkdir(parents=True)
        self.cache_dir = self.root / "cache"
        self.env = patch.dict(
            os.environ,
            {"CODEX_HOME": str(self.codex_home), "TOKENMASTER_CACHE_DIR": str(self.cache_dir)},
        )
        self.env.start()
        cfg = ConfigManager(config_path=self.root / "config.json")
        self.prov = CodexProvider(cfg.provider_cfg("codex"))

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _write_session(self, name: str, in_tok: int, out_tok: int, rate_limits: Any = None) -> Path:
        p = self.sessions / name
        p.write_text(
            _turn_context_line("gpt-5") + "\n" + _token_count_line(in_tok, 0, out_tok, rate_limits) + "\n",
            encoding="utf-8",
        )
        return p

    def test_scan_aggregates_and_populates_cache(self) -> None:
        self._write_session("a.jsonl", 100, 10)
        self._write_session("b.jsonl", 200, 20, {"secondary": {"used_percent": 40.0}})
        res = self.prov._scan_local_sessions()
        self.assertEqual(res["sessions_7d"], 2)
        self.assertEqual(res["tokens_7d"], 330)
        self.assertEqual(res["output_tokens_7d"], 30)
        self.assertEqual(res["latest_rate_limits"], {"secondary": {"used_percent": 40.0}})
        self.assertGreater(res["bytes_read"], 0)
        cache_file = self.cache_dir / "codex_sessions_scan.json"
        self.assertTrue(cache_file.is_file())
        self.assertEqual(len(json.loads(cache_file.read_text())["entries"]), 2)

    def test_second_scan_hits_cache_without_reading_files(self) -> None:
        self._write_session("a.jsonl", 100, 10)
        first = self.prov._scan_local_sessions()
        with patch.object(codex_mod, "summarize_rollout_tail", side_effect=AssertionError("file was read")):
            second = self.prov._scan_local_sessions()
        self.assertEqual(second["tokens_7d"], first["tokens_7d"])
        self.assertEqual(second["bytes_read"], 0)

    def test_modified_file_invalidates_only_its_entry(self) -> None:
        a = self._write_session("a.jsonl", 100, 10)
        self._write_session("b.jsonl", 200, 20)
        self.prov._scan_local_sessions()

        with a.open("a", encoding="utf-8") as fh:
            fh.write(_token_count_line(1000, 0, 100) + "\n")
        res = self.prov._scan_local_sessions()
        self.assertEqual(res["tokens_7d"], 1100 + 220)
        # Only a.jsonl should have been re-read.
        self.assertLessEqual(res["bytes_read"], a.stat().st_size)

    def test_deleted_or_aged_files_are_pruned_from_cache(self) -> None:
        a = self._write_session("a.jsonl", 100, 10)
        old = self._write_session("old.jsonl", 5, 5)
        self.prov._scan_local_sessions()
        a.unlink()
        ancient = time.time() - 30 * 86400
        os.utime(old, (ancient, ancient))
        res = self.prov._scan_local_sessions()
        self.assertEqual(res["sessions_7d"], 0)
        cache_file = self.cache_dir / "codex_sessions_scan.json"
        self.assertEqual(json.loads(cache_file.read_text())["entries"], {})

    def test_corrupt_cache_file_is_ignored(self) -> None:
        self.cache_dir.mkdir(parents=True)
        (self.cache_dir / "codex_sessions_scan.json").write_text("{garbage", encoding="utf-8")
        self._write_session("a.jsonl", 100, 10)
        res = self.prov._scan_local_sessions()
        self.assertEqual(res["tokens_7d"], 110)
        # Rewritten as valid JSON afterwards.
        json.loads((self.cache_dir / "codex_sessions_scan.json").read_text())

    def test_unwritable_cache_dir_degrades_gracefully(self) -> None:
        self._write_session("a.jsonl", 100, 10)
        blocker = self.root / "blocker"
        blocker.write_text("not a dir")
        with patch.dict(os.environ, {"TOKENMASTER_CACHE_DIR": str(blocker / "sub")}):
            res = self.prov._scan_local_sessions()
        self.assertEqual(res["tokens_7d"], 110)


class TestFileScanCache(unittest.TestCase):
    def test_version_mismatch_discards_entries(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.json"
            c = FileScanCache(path, version=1)
            c.put(Path("/x"), 1, 2, {"k": "v"})
            self.assertTrue(c.save())
            self.assertEqual(FileScanCache(path, version=1).get(Path("/x"), 1, 2), {"k": "v"})
            self.assertIsNone(FileScanCache(path, version=2).get(Path("/x"), 1, 2))
            self.assertIsNone(FileScanCache(path, version=1).get(Path("/x"), 1, 3))

    def test_save_is_noop_when_clean(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            c = FileScanCache(Path(d) / "c.json")
            self.assertFalse(c.save())
            self.assertFalse((Path(d) / "c.json").exists())


if __name__ == "__main__":
    unittest.main()
