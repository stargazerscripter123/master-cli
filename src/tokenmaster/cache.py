"""Lightweight on-disk cache for per-file scan summaries.

Entries are keyed by absolute path and validated against the file's
``st_mtime_ns`` and ``st_size`` so any append or rewrite invalidates them.
The cache is strictly best-effort: every failure (unwritable dir, corrupt
JSON, version mismatch) degrades to "no cache" without raising.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

CACHE_FORMAT_VERSION = 1


class FileScanCache:
    """Persist small per-file summaries so unchanged files are never re-read."""

    def __init__(self, path: Path, version: int = CACHE_FORMAT_VERSION) -> None:
        self.path = path
        self.version = version
        self._entries: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            if not self.path.is_file():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        if (
            isinstance(raw, dict)
            and raw.get("version") == self.version
            and isinstance(raw.get("entries"), dict)
        ):
            self._entries = {
                str(k): v for k, v in raw["entries"].items() if isinstance(v, dict)
            }

    def get(self, path: Path, mtime_ns: int, size: int) -> Optional[dict[str, Any]]:
        """Return the cached summary if the file is unchanged, else None."""
        ent = self._entries.get(str(path))
        if not ent:
            return None
        if ent.get("mtime_ns") != mtime_ns or ent.get("size") != size:
            return None
        summary = ent.get("summary")
        return summary if isinstance(summary, dict) else None

    def put(self, path: Path, mtime_ns: int, size: int, summary: dict[str, Any]) -> None:
        self._entries[str(path)] = {"mtime_ns": mtime_ns, "size": size, "summary": summary}
        self._dirty = True

    def prune(self, keep: set[str], root: Optional[Path] = None) -> None:
        """Drop entries for paths not in ``keep`` (files aged out or deleted).

        When ``root`` is given, only entries under ``root`` are candidates for pruning
        so toggling an environment variable (e.g. ``CLAUDE_CONFIG_DIR``) never wipes
        cached entries belonging to another directory.
        """
        prefix: Optional[str] = None
        if root is not None:
            prefix = str(root).rstrip(os.sep) + os.sep
        stale = [
            k
            for k in self._entries
            if k not in keep and (prefix is None or k.startswith(prefix))
        ]
        for k in stale:
            del self._entries[k]
        if stale:
            self._dirty = True

    def entry_count(self) -> int:
        return len(self._entries)

    def save(self) -> bool:
        """Atomically write the cache. Returns True on success, False if skipped/failed."""
        if not self._dirty:
            return False
        tmp_path: Optional[str] = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"version": self.version, "entries": self._entries},
                separators=(",", ":"),
            )
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".tmp-", suffix=".json"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_path, self.path)
            tmp_path = None
            self._dirty = False
            return True
        except Exception:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            return False
