"""SQLite local usage warehouse (warehouse.db) for permanent session ledger & quota history."""

from __future__ import annotations

import csv
import io
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from tokenmaster.config import parse_provider_spec
from tokenmaster.models import ProviderReport
from tokenmaster.providers.base import critical_section, file_lock

WAREHOUSE_SCHEMA_VERSION = 1
WAREHOUSE_FILENAME = "warehouse.db"

VALID_HISTORY_MODES: tuple[str, ...] = (
    "daily",
    "weekly",
    "monthly",
    "projects",
    "models",
    "snapshots",
)

HISTORY_MODE_ALIASES: dict[str, str] = {
    "day": "daily",
    "days": "daily",
    "daily": "daily",
    "week": "weekly",
    "weeks": "weekly",
    "weekly": "weekly",
    "month": "monthly",
    "months": "monthly",
    "monthly": "monthly",
    "project": "projects",
    "projects": "projects",
    "model": "models",
    "models": "models",
    "snapshot": "snapshots",
    "snapshots": "snapshots",
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BUCKET_SUFFIX_RE = re.compile(r"#\d{4}-\d{2}-\d{2}#[^#]+$")
_WIN_DRIVE_SLUG_RE = re.compile(r"^[A-Za-z]--")
_CLAUDE_HOME_SLUG_RE = re.compile(
    r"^-(?:Users|home|private-var-folders|var-folders)-[^-]+(?:-(?:Library-CloudStorage-[^-]+))?(?=-|$)",
    re.IGNORECASE,
)
_KNOWN_WORKSPACE_DIRS: frozenset[str] = frozenset(
    {
        "code",
        "work",
        "workdir",
        "workspace",
        "projects",
        "project",
        "repos",
        "repo",
        "src",
        "dev",
        "git",
        "github",
        "gitlab",
        "tools",
        "apps",
        "packages",
        "services",
    }
)


def get_warehouse_path() -> Path:
    """Return the path to ``warehouse.db``.

    Priority:
    1. ``TOKENMASTER_WAREHOUSE_PATH`` (explicit file path)
    2. ``TOKENMASTER_DATA_DIR`` / ``warehouse.db``
    3. ``TOKENMASTER_CACHE_DIR`` / ``warehouse.db`` (honors isolated test sandboxes)
    4. ``XDG_DATA_HOME`` / ``tokenmaster/warehouse.db``
    5. ``~/.local/share/tokenmaster/warehouse.db``
    """
    explicit = (os.environ.get("TOKENMASTER_WAREHOUSE_PATH") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    data_dir = (os.environ.get("TOKENMASTER_DATA_DIR") or "").strip()
    if data_dir:
        return Path(data_dir).expanduser() / WAREHOUSE_FILENAME
    cache_dir = (os.environ.get("TOKENMASTER_CACHE_DIR") or "").strip()
    if cache_dir:
        return Path(cache_dir).expanduser() / WAREHOUSE_FILENAME
    xdg_data = (os.environ.get("XDG_DATA_HOME") or "").strip()
    if xdg_data:
        return Path(xdg_data).expanduser() / "tokenmaster" / WAREHOUSE_FILENAME
    return Path.home() / ".local" / "share" / "tokenmaster" / WAREHOUSE_FILENAME


def sanitize_project_name(raw_project: Any) -> str:
    """Sanitize a project directory or Claude project slug into a privacy-safe label."""
    if not isinstance(raw_project, str):
        return "unknown"
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", raw_project).strip()
    if not cleaned:
        return "unknown"

    # Handle filesystem paths (/Users/alice/work/repo -> repo; /Users/alice -> home)
    if ("/" in cleaned or "\\" in cleaned) and cleaned[0] not in ("=", "+", "@"):
        norm = cleaned.replace("\\", "/").rstrip("/")
        parts = [
            p
            for p in norm.split("/")
            if p and p not in (".", "..", "~") and not re.match(r"^[A-Za-z]:$", p)
        ]
        if not parts:
            return "unknown"
        if len(parts) <= 2 and parts[0].lower() in ("users", "home"):
            return "home"
        return parts[-1][:80]

    # Normalize Windows drive-prefixed Claude Code project slugs (e.g. "C--Users-alice-...")
    slug_cand = _WIN_DRIVE_SLUG_RE.sub("-", cleaned)

    # Handle Claude Code project directory slugs:
    # e.g. "-Users-alice-Library-CloudStorage-Drive-Working-Directory-tools-demo"
    if slug_cand.startswith("-"):
        stripped = _CLAUDE_HOME_SLUG_RE.sub("", slug_cand).strip("-")
        if stripped != slug_cand.strip("-"):
            tokens = [t for t in stripped.split("-") if t]
            if not tokens:
                return "home"
            if len(tokens) >= 3:
                return "-".join(tokens[-2:])[:80]
            if len(tokens) == 2 and tokens[0].lower() in _KNOWN_WORKSPACE_DIRS:
                return f"{tokens[0]}-{tokens[1]}"[:80]
            return tokens[-1][:80]
        tokens = [t for t in slug_cand.strip("-").split("-") if t]
        if tokens and tokens[0].lower() in ("users", "home") and len(tokens) <= 2:
            return "home"
        if tokens:
            return "-".join(tokens[-2:])[:80]

    return cleaned[:80]


def normalize_day_utc(ts_or_day: Any) -> str:
    """Convert a Unix timestamp or YYYY-MM-DD string to a UTC ``YYYY-MM-DD`` string."""
    if isinstance(ts_or_day, str):
        s = ts_or_day.strip()
        if _DATE_RE.match(s):
            try:
                date.fromisoformat(s)
                return s
            except ValueError:
                pass
    if isinstance(ts_or_day, (int, float)) and not isinstance(ts_or_day, bool) and math.isfinite(float(ts_or_day)):
        try:
            return datetime.fromtimestamp(float(ts_or_day), tz=timezone.utc).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def parse_date_arg(val: Optional[str], arg_name: str = "--since") -> Optional[str]:
    """Validate and return a ``YYYY-MM-DD`` date string."""
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    if not _DATE_RE.match(s):
        raise ValueError(f"Invalid date for {arg_name}: '{s}' (expected YYYY-MM-DD)")
    try:
        date.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"Invalid calendar date for {arg_name}: '{s}'") from exc
    return s


@dataclass
class SessionLedgerRow:
    """Single session-day-model usage record for ``session_ledger``."""

    provider_id: str
    account_id: str
    session_id: str
    day_utc: str
    first_ts: float
    last_ts: float
    project: str = "unknown"
    model: str = "unknown"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    estimated_usd: float = 0.0


def _extract_5h_rem_pct(r: ProviderReport) -> Optional[float]:
    cands: list[float] = []
    for sm in r.sub_metrics:
        lbl = (sm.name or "").lower()
        if "5h" in lbl or "5-hour" in lbl or "five_hour" in lbl:
            if sm.used_percent is not None and not isinstance(sm.used_percent, bool):
                try:
                    fv = float(sm.used_percent)
                    if math.isfinite(fv):
                        cands.append(max(0.0, min(100.0, 100.0 - fv)))
                except (TypeError, ValueError, OverflowError):
                    pass
    if not cands:
        return None
    return round(sum(cands) / len(cands), 1)


def _finite_or_none(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        fv = float(v)
        return fv if math.isfinite(fv) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_nonneg_int(v: Any) -> int:
    fv = _finite_or_none(v)
    if fv is None or fv <= 0:
        return 0
    try:
        return max(0, int(fv))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_nonneg_float(v: Any) -> float:
    fv = _finite_or_none(v)
    if fv is None or fv <= 0.0:
        return 0.0
    return float(fv)


class UsageWarehouse:
    """SQLite WAL-backed local usage warehouse surviving 30-day CLI log cleanup."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.path: Path = (db_path or get_warehouse_path()).expanduser()
        self._lock_path: Path = self.path.parent / f".{self.path.name}.lock"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        try:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._ensure_schema(conn)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self.path}{suffix}")
                if sidecar.exists():
                    try:
                        os.chmod(sidecar, 0o600)
                    except OSError:
                        pass
            return conn
        except Exception:
            conn.close()
            raise

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS warehouse_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_ledger (
                provider_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                day_utc TEXT NOT NULL,
                first_ts REAL NOT NULL,
                last_ts REAL NOT NULL,
                project TEXT NOT NULL DEFAULT 'unknown',
                model TEXT NOT NULL DEFAULT 'unknown',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_usd REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL,
                PRIMARY KEY (provider_id, account_id, session_id)
            );

            CREATE INDEX IF NOT EXISTS idx_session_ledger_day
                ON session_ledger (day_utc);
            CREATE INDEX IF NOT EXISTS idx_session_ledger_prov_day
                ON session_ledger (provider_id, account_id, day_utc);
            CREATE INDEX IF NOT EXISTS idx_session_ledger_project
                ON session_ledger (project);
            CREATE INDEX IF NOT EXISTS idx_session_ledger_model
                ON session_ledger (model);

            CREATE TABLE IF NOT EXISTS quota_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                day_utc TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                status TEXT NOT NULL,
                weekly_remaining_pct REAL,
                five_hour_remaining_pct REAL,
                credit_remaining REAL,
                pace_ratio REAL
            );

            CREATE INDEX IF NOT EXISTS idx_quota_snapshots_inst_ts
                ON quota_snapshots (instance_id, timestamp DESC);
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO warehouse_meta (key, value) VALUES ('schema_version', ?)",
            (str(WAREHOUSE_SCHEMA_VERSION),),
        )

    def upsert_sessions(self, rows: Iterable[SessionLedgerRow]) -> int:
        """Idempotently upsert session ledger records into ``warehouse.db``."""
        try:
            row_list = list(rows)
            if not row_list:
                return 0

            now_ts = time.time()
            params: list[tuple[Any, ...]] = []
            for r in row_list:
                pid = str(r.provider_id or "").strip().lower()
                if not pid:
                    continue
                acct = str(r.account_id or "").strip().lower() or "default"
                sid = str(r.session_id or "").strip()
                if not sid:
                    continue
                f_cand = _finite_or_none(r.first_ts)
                l_cand = _finite_or_none(r.last_ts)
                f_ts = f_cand if f_cand is not None else (l_cand if l_cand is not None else now_ts)
                l_ts = l_cand if l_cand is not None else f_ts
                if f_ts > l_ts:
                    f_ts, l_ts = l_ts, f_ts
                raw_day = r.day_utc.strip() if isinstance(r.day_utc, str) else ""
                if raw_day and _DATE_RE.match(raw_day):
                    try:
                        date.fromisoformat(raw_day)
                        day_str = raw_day
                    except ValueError:
                        day_str = normalize_day_utc(l_ts)
                else:
                    day_str = normalize_day_utc(l_ts)
                proj = sanitize_project_name(r.project)
                mdl = re.sub(r"[\x00-\x1f\x7f]+", " ", str(r.model or "unknown")).strip()[:80] or "unknown"
                in_t = _safe_nonneg_int(r.input_tokens)
                out_t = _safe_nonneg_int(r.output_tokens)
                cr_t = _safe_nonneg_int(r.cache_read_tokens)
                cw_t = _safe_nonneg_int(r.cache_write_tokens)
                tot_t = max(in_t + out_t + cr_t + cw_t, _safe_nonneg_int(r.total_tokens))
                usd = _safe_nonneg_float(r.estimated_usd)
                params.append(
                    (
                        pid,
                        acct,
                        sid,
                        day_str,
                        f_ts,
                        l_ts,
                        proj,
                        mdl,
                        in_t,
                        out_t,
                        cr_t,
                        cw_t,
                        tot_t,
                        usd,
                        now_ts,
                    )
                )

            if not params:
                return 0

            with critical_section(), file_lock(self._lock_path):
                conn = self._connect()
                try:
                    with conn:
                        conn.executemany(
                            """
                            INSERT INTO session_ledger (
                                provider_id, account_id, session_id, day_utc,
                                first_ts, last_ts, project, model,
                                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                                total_tokens, estimated_usd, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(provider_id, account_id, session_id) DO UPDATE SET
                                day_utc = excluded.day_utc,
                                first_ts = MIN(session_ledger.first_ts, excluded.first_ts),
                                last_ts = MAX(session_ledger.last_ts, excluded.last_ts),
                                project = excluded.project,
                                model = excluded.model,
                                input_tokens = MAX(session_ledger.input_tokens, excluded.input_tokens),
                                output_tokens = MAX(session_ledger.output_tokens, excluded.output_tokens),
                                cache_read_tokens = MAX(session_ledger.cache_read_tokens, excluded.cache_read_tokens),
                                cache_write_tokens = MAX(session_ledger.cache_write_tokens, excluded.cache_write_tokens),
                                total_tokens = MAX(session_ledger.total_tokens, excluded.total_tokens),
                                estimated_usd = MAX(session_ledger.estimated_usd, excluded.estimated_usd),
                                updated_at = excluded.updated_at
                            """,
                            params,
                        )
                    return len(params)
                finally:
                    conn.close()
        except Exception:
            return 0

    def record_quota_snapshots(
        self,
        reports: Iterable[ProviderReport],
        *,
        min_interval_seconds: float = 60.0,
        now_ts: Optional[float] = None,
    ) -> int:
        """Append live quota snapshots to ``quota_snapshots`` with deduplication."""
        rep_list = [r for r in reports if r.status != "unconfigured"]
        if not rep_list:
            return 0

        ts = float(now_ts) if (now_ts is not None and math.isfinite(float(now_ts))) else time.time()
        day_str = normalize_day_utc(ts)
        inserted = 0
        try:
            with critical_section(), file_lock(self._lock_path):
                conn = self._connect()
                try:
                    with conn:
                        for r in rep_list:
                            r.finalize()
                            inst_id = r.instance_id
                            pid = r.provider_id
                            acct = (r.account or "default").strip().lower()
                            w_cands = [
                                x
                                for x in (
                                    _finite_or_none(r.weekly.remaining_percent),
                                    _finite_or_none(r.weekly.scoped_remaining_percent),
                                    (100.0 - float(r.weekly.pace_used_percent))
                                    if _finite_or_none(r.weekly.pace_used_percent) is not None
                                    else None,
                                )
                                if x is not None
                            ]
                            w_rem = max(0.0, min(100.0, min(w_cands))) if w_cands else None
                            s5_rem = _extract_5h_rem_pct(r)
                            c_rem = _finite_or_none(r.credits.remaining)
                            pace_r = _finite_or_none(r.weekly.pace_ratio)

                            if min_interval_seconds > 0:
                                prev = conn.execute(
                                    """
                                    SELECT timestamp, status, weekly_remaining_pct,
                                           five_hour_remaining_pct, credit_remaining
                                    FROM quota_snapshots
                                    WHERE instance_id = ?
                                    ORDER BY timestamp DESC
                                    LIMIT 1
                                    """,
                                    (inst_id,),
                                ).fetchone()
                                if prev is not None:
                                    prev_ts = float(prev["timestamp"] or 0.0)
                                    if 0.0 <= (ts - prev_ts) < min_interval_seconds:
                                        same_status = prev["status"] == r.status
                                        same_w = (prev["weekly_remaining_pct"] == w_rem) or (
                                            prev["weekly_remaining_pct"] is not None
                                            and w_rem is not None
                                            and abs(float(prev["weekly_remaining_pct"]) - w_rem) < 0.05
                                        )
                                        same_5h = (prev["five_hour_remaining_pct"] == s5_rem) or (
                                            prev["five_hour_remaining_pct"] is not None
                                            and s5_rem is not None
                                            and abs(float(prev["five_hour_remaining_pct"]) - s5_rem) < 0.05
                                        )
                                        same_c = (prev["credit_remaining"] == c_rem) or (
                                            prev["credit_remaining"] is not None
                                            and c_rem is not None
                                            and abs(float(prev["credit_remaining"]) - c_rem) < 0.01
                                        )
                                        if same_status and same_w and same_5h and same_c:
                                            continue

                            conn.execute(
                                """
                                INSERT INTO quota_snapshots (
                                    timestamp, day_utc, instance_id, provider_id, account_id,
                                    status, weekly_remaining_pct, five_hour_remaining_pct,
                                    credit_remaining, pace_ratio
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (ts, day_str, inst_id, pid, acct, r.status, w_rem, s5_rem, c_rem, pace_r),
                            )
                            inserted += 1
                    return inserted
                finally:
                    conn.close()
        except Exception:
            return 0

    def inspect_status(self) -> tuple[bool, int, int]:
        """Return ``(exists_and_valid, session_row_count, snapshot_row_count)`` for ``tokenmaster doctor``."""
        if not self.path.is_file():
            return False, 0, 0
        try:
            uri_path = f"{self.path.resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri_path, uri=True, timeout=2.0)
            try:
                s_cnt = int(conn.execute("SELECT COUNT(*) FROM session_ledger").fetchone()[0])
                q_cnt = int(conn.execute("SELECT COUNT(*) FROM quota_snapshots").fetchone()[0])
                return True, s_cnt, q_cnt
            finally:
                conn.close()
        except Exception:
            return False, 0, 0

    def is_target_backfilled(self, instance_id: str) -> bool:
        """Return True if full historical backfill has completed for ``instance_id``."""
        key = f"backfilled:{(instance_id or '').strip().lower()}"
        if not self.path.is_file():
            return False
        try:
            with critical_section(), file_lock(self._lock_path):
                conn = self._connect()
                try:
                    row = conn.execute(
                        "SELECT value FROM warehouse_meta WHERE key = ?",
                        (key,),
                    ).fetchone()
                    return row is not None and str(row["value"]) == "1"
                finally:
                    conn.close()
        except Exception:
            return False

    def mark_target_backfilled(self, instance_id: str) -> None:
        """Record that full historical backfill has completed for ``instance_id``."""
        key = f"backfilled:{(instance_id or '').strip().lower()}"
        try:
            with critical_section(), file_lock(self._lock_path):
                conn = self._connect()
                try:
                    with conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO warehouse_meta (key, value) VALUES (?, '1')",
                            (key,),
                        )
                finally:
                    conn.close()
        except Exception:
            pass

    def _empty_history_result(
        self,
        norm_mode: str,
        since_norm: Optional[str],
        until_norm: Optional[str],
    ) -> dict[str, Any]:
        if norm_mode == "snapshots":
            totals: dict[str, Any] = {"snapshots": 0}
        else:
            totals = {
                "sessions": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0,
                "cache_hit_percent": None,
                "estimated_usd": 0.0,
            }
        return {
            "mode": norm_mode,
            "warehouse_path": str(self.path),
            "since": since_norm,
            "until": until_norm,
            "rows": [],
            "totals": totals,
        }

    @staticmethod
    def _build_provider_where(selected: Optional[list[str]]) -> tuple[str, list[Any]]:
        if not selected:
            return "", []
        clauses: list[str] = []
        params: list[Any] = []
        for spec in selected:
            pid, acct, explicit_acct = parse_provider_spec(spec)
            if explicit_acct:
                norm_acct = (acct or "default").strip().lower()
                clauses.append("(provider_id = ? AND account_id = ?)")
                params.extend([pid, norm_acct])
            else:
                clauses.append("(provider_id = ?)")
                params.append(pid)
        if not clauses:
            return "", []
        return "(" + " OR ".join(clauses) + ")", params

    def query_history(
        self,
        mode: str = "daily",
        *,
        selected_providers: Optional[list[str]] = None,
        since_day: Optional[str] = None,
        until_day: Optional[str] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Query aggregated historical usage or quota snapshots from ``warehouse.db``."""
        norm_mode = HISTORY_MODE_ALIASES.get((mode or "daily").strip().lower())
        if not norm_mode:
            raise ValueError(
                f"Unknown history mode '{mode}'. Choose from: {', '.join(VALID_HISTORY_MODES)}"
            )

        since_norm = parse_date_arg(since_day, "--since")
        until_norm = parse_date_arg(until_day, "--until")
        if since_norm and until_norm and since_norm > until_norm:
            raise ValueError(f"--since ({since_norm}) cannot be later than --until ({until_norm})")

        max_rows = max(1, int(limit or 100))

        where_parts: list[str] = []
        params: list[Any] = []
        prov_sql, prov_params = self._build_provider_where(selected_providers)
        if prov_sql:
            where_parts.append(prov_sql)
            params.extend(prov_params)
        if since_norm:
            where_parts.append("day_utc >= ?")
            params.append(since_norm)
        if until_norm:
            where_parts.append("day_utc <= ?")
            params.append(until_norm)

        where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

        if not self.path.is_file():
            return self._empty_history_result(norm_mode, since_norm, until_norm)

        try:
            with critical_section(), file_lock(self._lock_path):
                conn = self._connect()
                try:
                    if norm_mode == "snapshots":
                        q_rows = conn.execute(
                            f"""
                            SELECT timestamp, day_utc, instance_id, provider_id, account_id,
                                   status, weekly_remaining_pct, five_hour_remaining_pct,
                                   credit_remaining, pace_ratio
                            FROM quota_snapshots
                            {where_clause}
                            ORDER BY timestamp DESC
                            LIMIT ?
                            """,
                            [*params, max_rows],
                        ).fetchall()
                        snap_out = [
                            {
                                "timestamp": float(r["timestamp"]),
                                "iso_utc": datetime.fromtimestamp(
                                    float(r["timestamp"]), tz=timezone.utc
                                ).strftime("%Y-%m-%d %H:%M:%S UTC"),
                                "day_utc": str(r["day_utc"]),
                                "instance_id": str(r["instance_id"]),
                                "provider_id": str(r["provider_id"]),
                                "account_id": str(r["account_id"]),
                                "status": str(r["status"]),
                                "weekly_remaining_pct": _finite_or_none(r["weekly_remaining_pct"]),
                                "five_hour_remaining_pct": _finite_or_none(r["five_hour_remaining_pct"]),
                                "credit_remaining": _finite_or_none(r["credit_remaining"]),
                                "pace_ratio": _finite_or_none(r["pace_ratio"]),
                            }
                            for r in q_rows
                        ]
                        return {
                            "mode": "snapshots",
                            "warehouse_path": str(self.path),
                            "since": since_norm,
                            "until": until_norm,
                            "rows": snap_out,
                            "totals": {"snapshots": len(snap_out)},
                        }

                    raw_rows = conn.execute(
                        f"""
                        SELECT provider_id, account_id, session_id, day_utc,
                               project, model, input_tokens, output_tokens,
                               cache_read_tokens, cache_write_tokens, total_tokens,
                               estimated_usd
                        FROM session_ledger
                        {where_clause}
                        """,
                        params,
                    ).fetchall()
                finally:
                    conn.close()
        except Exception:
            return self._empty_history_result(norm_mode, since_norm, until_norm)

        buckets: dict[str, dict[str, Any]] = {}
        all_sessions: set[tuple[str, str, str]] = set()
        tot_in = 0
        tot_out = 0
        tot_cr = 0
        tot_cw = 0
        tot_tok = 0
        tot_usd = 0.0

        for r in raw_rows:
            pid = str(r["provider_id"])
            acct = str(r["account_id"] or "default")
            inst_id = f"{pid}:{acct}" if acct != "default" else pid
            sid_full = str(r["session_id"])
            base_sid = _BUCKET_SUFFIX_RE.sub("", sid_full)
            day_s = str(r["day_utc"])
            proj_s = str(r["project"] or "unknown")
            mdl_s = str(r["model"] or "unknown")

            if norm_mode == "daily":
                bkey = day_s
            elif norm_mode == "weekly":
                try:
                    iso_y, iso_w, _ = date.fromisoformat(day_s).isocalendar()
                    bkey = f"{iso_y:04d}-W{iso_w:02d}"
                except ValueError:
                    bkey = day_s[:7]
            elif norm_mode == "monthly":
                bkey = day_s[:7]
            elif norm_mode == "projects":
                bkey = proj_s
            else:  # models
                bkey = mdl_s

            in_t = max(0, int(r["input_tokens"] or 0))
            out_t = max(0, int(r["output_tokens"] or 0))
            cr_t = max(0, int(r["cache_read_tokens"] or 0))
            cw_t = max(0, int(r["cache_write_tokens"] or 0))
            t_t = max(0, int(r["total_tokens"] or 0))
            usd = max(0.0, float(r["estimated_usd"] or 0.0))

            b = buckets.get(bkey)
            if b is None:
                b = {
                    "key": bkey,
                    "_session_set": set(),
                    "_provider_set": set(),
                    "_model_set": set(),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "total_tokens": 0,
                    "estimated_usd": 0.0,
                }
                buckets[bkey] = b

            b["_session_set"].add((pid, acct, base_sid))
            b["_provider_set"].add(inst_id)
            if mdl_s and mdl_s != "unknown":
                b["_model_set"].add(mdl_s)
            b["input_tokens"] += in_t
            b["output_tokens"] += out_t
            b["cache_read_tokens"] += cr_t
            b["cache_write_tokens"] += cw_t
            b["total_tokens"] += t_t
            b["estimated_usd"] += usd

            all_sessions.add((pid, acct, base_sid))
            tot_in += in_t
            tot_out += out_t
            tot_cr += cr_t
            tot_cw += cw_t
            tot_tok += t_t
            tot_usd += usd

        finalized_rows: list[dict[str, Any]] = []
        for b in buckets.values():
            denom = b["input_tokens"] + b["cache_read_tokens"] + b["cache_write_tokens"]
            hit_pct = round((b["cache_read_tokens"] / denom) * 100.0, 1) if denom > 0 else None
            finalized_rows.append(
                {
                    "key": b["key"],
                    "sessions": len(b["_session_set"]),
                    "providers": sorted(b["_provider_set"]),
                    "models": sorted(b["_model_set"]),
                    "input_tokens": b["input_tokens"],
                    "output_tokens": b["output_tokens"],
                    "cache_read_tokens": b["cache_read_tokens"],
                    "cache_write_tokens": b["cache_write_tokens"],
                    "total_tokens": b["total_tokens"],
                    "cache_hit_percent": hit_pct,
                    "estimated_usd": round(b["estimated_usd"], 4),
                }
            )

        if norm_mode in ("daily", "weekly", "monthly"):
            # Sort chronological ascending, keeping the most recent `max_rows` periods
            finalized_rows.sort(key=lambda item: item["key"])
            if len(finalized_rows) > max_rows:
                finalized_rows = finalized_rows[-max_rows:]
        else:
            # Sort projects/models by highest cost then highest token volume
            finalized_rows.sort(
                key=lambda item: (item["estimated_usd"], item["total_tokens"], item["key"]),
                reverse=True,
            )
            if len(finalized_rows) > max_rows:
                finalized_rows = finalized_rows[:max_rows]

        tot_denom = tot_in + tot_cr + tot_cw
        tot_hit_pct = round((tot_cr / tot_denom) * 100.0, 1) if tot_denom > 0 else None

        return {
            "mode": norm_mode,
            "warehouse_path": str(self.path),
            "since": since_norm,
            "until": until_norm,
            "rows": finalized_rows,
            "totals": {
                "sessions": len(all_sessions),
                "input_tokens": tot_in,
                "output_tokens": tot_out,
                "cache_read_tokens": tot_cr,
                "cache_write_tokens": tot_cw,
                "total_tokens": tot_tok,
                "cache_hit_percent": tot_hit_pct,
                "estimated_usd": round(tot_usd, 4),
            },
        }


def _csv_safe_cell(val: Any) -> Any:
    """Prevent CSV formula injection (=, +, -, @, tab, CR, LF), including whitespace-prefixed formulas."""
    if isinstance(val, str) and val:
        if val[0] in ("=", "+", "-", "@", "\t", "\r", "\n") or val.lstrip(" \t\r\n").startswith(
            ("=", "+", "-", "@")
        ):
            return "'" + val
    return val


def format_history_csv(report_data: dict[str, Any]) -> str:
    """Serialize ``query_history`` output into RFC 4180 CSV."""
    mode = str(report_data.get("mode") or "daily")
    rows = report_data.get("rows") if isinstance(report_data.get("rows"), list) else []
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    if mode == "snapshots":
        writer.writerow(
            [
                "timestamp",
                "iso_utc",
                "day_utc",
                "instance_id",
                "status",
                "weekly_remaining_pct",
                "five_hour_remaining_pct",
                "credit_remaining",
                "pace_ratio",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r.get("timestamp"),
                    _csv_safe_cell(r.get("iso_utc")),
                    _csv_safe_cell(r.get("day_utc")),
                    _csv_safe_cell(r.get("instance_id")),
                    _csv_safe_cell(r.get("status")),
                    r.get("weekly_remaining_pct"),
                    r.get("five_hour_remaining_pct"),
                    r.get("credit_remaining"),
                    r.get("pace_ratio"),
                ]
            )
        return buf.getvalue()

    key_col = {
        "daily": "day_utc",
        "weekly": "week_iso",
        "monthly": "month_utc",
        "projects": "project",
        "models": "model",
    }.get(mode, "group")
    writer.writerow(
        [
            key_col,
            "sessions",
            "providers",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_tokens",
            "cache_hit_percent",
            "estimated_usd",
        ]
    )
    for r in rows:
        provs = ",".join(r.get("providers") or [])
        writer.writerow(
            [
                _csv_safe_cell(r.get("key")),
                r.get("sessions", 0),
                _csv_safe_cell(provs),
                r.get("input_tokens", 0),
                r.get("output_tokens", 0),
                r.get("cache_read_tokens", 0),
                r.get("cache_write_tokens", 0),
                r.get("total_tokens", 0),
                r.get("cache_hit_percent"),
                f"{float(r.get('estimated_usd') or 0.0):.4f}",
            ]
        )
    return buf.getvalue()


def compute_since_day_from_days(days: Optional[int], now: Optional[datetime] = None) -> Optional[str]:
    """Return ``YYYY-MM-DD`` UTC cutoff for ``--days N`` (``None`` when ``days <= 0``)."""
    if days is None or int(days) <= 0:
        return None
    ref = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    cutoff_dt = ref - timedelta(days=max(0, int(days) - 1))
    return cutoff_dt.strftime("%Y-%m-%d")
