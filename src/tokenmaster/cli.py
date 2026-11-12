"""CLI entrypoint and subcommands for TokenMaster."""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from tokenmaster import __version__
from tokenmaster.cache import CACHE_FORMAT_VERSION
from tokenmaster.config import (
    PROVIDERS,
    ConfigManager,
    get_account_profile_dir,
    get_cache_dir,
    is_forbidden_profile_dir,
    parse_provider_spec,
    validate_account_name,
)
from tokenmaster.models import (
    ProviderReport,
    format_duration_short,
    parse_reset_remaining_seconds,
)
from tokenmaster.notify import (
    ALERT_STATE_FILENAME,
    ALERT_STATE_VERSION,
    DEFAULT_AUTH_COOLDOWN_SECONDS,
    AlertManager,
    get_alert_state_path,
    is_report_configured_or_errored,
    parse_thresholds,
)
from tokenmaster.providers import PROVIDER_CLASSES, _expand_targets, fetch_providers
from tokenmaster.providers.base import atomic_write_text, critical_section, file_lock
from tokenmaster.ui import (
    Style,
    _detect_terminal_size,
    clear_terminal,
    paint_watch_frame,
    render_dashboard,
    render_history_report,
    render_statusline,
)
from tokenmaster.warehouse import (
    HISTORY_MODE_ALIASES,
    VALID_HISTORY_MODES,
    UsageWarehouse,
    compute_since_day_from_days,
    format_history_csv,
    parse_date_arg,
)

_WATCH_USE_CONFIG_DEFAULT = object()

STATUS_SNAPSHOT_FILENAME = "status_snapshot.json"
STATUS_SNAPSHOT_VERSION = 1
VALID_STATUSLINE_FORMATS: tuple[str, ...] = ("compact", "tmux", "claude-code", "plain", "json")

KNOWN_CACHE_FILES: tuple[tuple[str, str], ...] = (
    ("codex", "codex_sessions_scan.json"),
    ("claude", "claude_projects_scan.json"),
    ("claude", "claude_oauth_usage.json"),
    ("antigravity", "antigravity_brain_scan.json"),
    ("antigravity", "antigravity_rpc.json"),
    ("antigravity", "antigravity_pools.json"),
    ("grok", "grok_sessions_scan.json"),
    ("all", STATUS_SNAPSHOT_FILENAME),
    ("all", ALERT_STATE_FILENAME),
)


def _parse_provider_filter(raw_list: Optional[list[str]]) -> Optional[list[str]]:
    if not raw_list:
        return None
    out: list[str] = []
    for item in raw_list:
        for part in item.split(","):
            p = part.strip()
            if p:
                pid, acct, explicit_acct = parse_provider_spec(p)
                if pid not in PROVIDERS:
                    valid = ", ".join(PROVIDERS)
                    raise ValueError(f"Unknown provider '{pid}'. Valid providers: {valid}")
                norm = f"{pid}:{acct}" if (explicit_acct and acct) else (f"{pid}:default" if explicit_acct else pid)
                if norm not in out:
                    out.append(norm)
    return out or None


def _mask_secret(val: Any) -> Any:
    if not isinstance(val, str) or len(val) < 8:
        return val
    return val[:4] + "..." + val[-4:]


def save_status_snapshot(
    reports: list[ProviderReport],
    *,
    replace_all: bool = False,
) -> Optional[Path]:
    """Atomically persist provider reports to ``~/.cache/tokenmaster/status_snapshot.json``."""
    cache_dir = get_cache_dir()
    snap_path = cache_dir / STATUS_SNAPSHOT_FILENAME
    lock_path = cache_dir / ".status_snapshot.lock"
    now_ts = time.time()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with critical_section(), file_lock(lock_path):
            existing_entries: dict[str, dict[str, Any]] = {}
            existing_order: list[str] = []
            if not replace_all and snap_path.is_file():
                try:
                    raw = json.loads(snap_path.read_text(encoding="utf-8"))
                    if (
                        isinstance(raw, dict)
                        and raw.get("version") == STATUS_SNAPSHOT_VERSION
                        and isinstance(raw.get("entries"), dict)
                    ):
                        for k, v in raw["entries"].items():
                            if isinstance(v, dict) and isinstance(v.get("report"), dict):
                                existing_entries[str(k)] = v
                        if isinstance(raw.get("order"), list):
                            for x in raw["order"]:
                                sx = str(x)
                                if sx in existing_entries and sx not in existing_order:
                                    existing_order.append(sx)
                        for k in existing_entries:
                            if k not in existing_order:
                                existing_order.append(k)
                except Exception:
                    existing_entries = {}
                    existing_order = []

            for r in reports:
                inst_id = r.instance_id
                existing_entries[inst_id] = {
                    "updated_at": now_ts,
                    "report": r.to_dict(),
                }
                if inst_id not in existing_order:
                    existing_order.append(inst_id)

            if replace_all:
                existing_order = []
                for r in reports:
                    if r.instance_id not in existing_order:
                        existing_order.append(r.instance_id)

            payload = {
                "version": STATUS_SNAPSHOT_VERSION,
                "updated_at": now_ts,
                "order": existing_order,
                "entries": existing_entries,
            }
            atomic_write_text(
                snap_path,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                mode=0o600,
            )
        UsageWarehouse().record_quota_snapshots(reports)
        return snap_path
    except Exception:
        return None


def load_status_snapshot(
    cfg_mgr: ConfigManager,
    selected: Optional[list[str]] = None,
) -> tuple[list[ProviderReport], float, bool]:
    """Load cached ``ProviderReport`` entries from ``status_snapshot.json`` in <5ms.

    Returns ``(reports, max_age_seconds, full_hit)``.
    """
    targets = _expand_targets(cfg_mgr, selected)
    if not targets:
        return [], 0.0, True

    snap_path = get_cache_dir() / STATUS_SNAPSHOT_FILENAME
    if not snap_path.is_file():
        return [], float("inf"), False

    try:
        raw = json.loads(snap_path.read_text(encoding="utf-8"))
    except Exception:
        return [], float("inf"), False

    if (
        not isinstance(raw, dict)
        or raw.get("version") != STATUS_SNAPSHOT_VERSION
        or not isinstance(raw.get("entries"), dict)
    ):
        return [], float("inf"), False

    entries = raw["entries"]
    now_ts = time.time()
    out: list[ProviderReport] = []
    max_age = 0.0
    full_hit = True
    for jkey, _, _, _ in targets:
        ent = entries.get(jkey)
        if not isinstance(ent, dict) or not isinstance(ent.get("report"), dict):
            full_hit = False
            continue
        upd = ent.get("updated_at", raw.get("updated_at"))
        if isinstance(upd, bool) or not isinstance(upd, (int, float)) or not math.isfinite(float(upd)):
            full_hit = False
            continue
        try:
            rep = ProviderReport.from_dict(ent["report"])
        except Exception:
            full_hit = False
            continue
        age = max(0.0, now_ts - float(upd))
        max_age = max(max_age, age)
        out.append(rep)

    if not out:
        return [], float("inf"), False

    return out, max_age, full_hit


def _read_claude_code_stdin() -> dict[str, Any]:
    """Read optional Claude Code statusLine JSON payload from stdin without blocking TTYs."""
    import io
    import select

    stream = sys.stdin
    if stream is None:
        return {}
    try:
        if hasattr(stream, "isatty") and stream.isatty():
            return {}
    except Exception:
        return {}

    raw_txt = ""
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, io.UnsupportedOperation):
        try:
            raw_txt = stream.read(65536)
        except Exception:
            return {}
    else:
        try:
            chunks: list[bytes] = []
            total_bytes = 0
            timeout = 0.05
            while total_bytes < 65536:
                rlist, _, _ = select.select([fd], [], [], timeout)
                if not rlist:
                    break
                chunk = os.read(fd, 65536 - total_bytes)
                if not chunk:
                    break
                chunks.append(chunk)
                total_bytes += len(chunk)
                timeout = 0.01
            if not chunks:
                return {}
            raw_txt = b"".join(chunks).decode("utf-8", errors="replace")
        except Exception:
            return {}

    raw_txt = (raw_txt or "").strip()
    if not raw_txt:
        return {}
    try:
        parsed = json.loads(raw_txt)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _resolve_claude_code_default_selector(cfg_mgr: ConfigManager) -> list[str]:
    """Resolve the active Claude subscription profile in the current shell environment."""
    active_prof = (os.environ.get("TOKENMASTER_ACTIVE_PROFILE") or "").strip()
    if active_prof.lower() == "claude":
        return ["claude:default"]
    if active_prof.lower().startswith("claude:"):
        try:
            norm = _parse_provider_filter([active_prof])
            if norm:
                return norm
        except ValueError:
            pass

    env_cdir = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if env_cdir:
        try:
            env_path = Path(env_cdir).expanduser()
            env_cands = {os.path.abspath(str(env_path))}
            try:
                env_cands.add(str(env_path.resolve()))
            except OSError:
                pass
            for acct, acfg in cfg_mgr.list_accounts(
                "claude", include_disabled=False, ignore_provider_enabled=True
            ):
                if acct is None:
                    continue
                pdir = acfg.get("profile_dir") or str(get_account_profile_dir("claude", acct))
                p_path = Path(str(pdir)).expanduser()
                p_cands = {os.path.abspath(str(p_path))}
                try:
                    p_cands.add(str(p_path.resolve()))
                except OSError:
                    pass
                if env_cands & p_cands:
                    return [f"claude:{acct}"]
        except Exception:
            pass

    return ["claude:default"]


def _spawn_async_statusline_refresh(
    cfg_path: Path,
    selected: Optional[list[str]],
) -> bool:
    """Spawn a detached background process to refresh ``status_snapshot.json`` without blocking."""
    cache_dir = get_cache_dir()
    stamp_path = cache_dir / ".status_snapshot_refresh.stamp"
    lock_path = cache_dir / ".status_snapshot_refresh.lock"
    selector_key = ",".join(selected) if selected else "all"
    now_ts = time.time()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with critical_section(), file_lock(lock_path):
            stamps: dict[str, float] = {}
            if stamp_path.is_file():
                try:
                    raw = json.loads(stamp_path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        for k, v in raw.items():
                            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
                                stamps[str(k)] = float(v)
                except Exception:
                    stamps = {}

            # An active "all" refresh also covers any subset selector; a subset selector
            # only throttles the same subset selector.
            all_ts = stamps.get("all")
            if all_ts is not None and 0.0 <= (now_ts - all_ts) < 15.0:
                return False
            sel_ts = stamps.get(selector_key)
            if sel_ts is not None and 0.0 <= (now_ts - sel_ts) < 15.0:
                return False

            stamps[selector_key] = now_ts
            # Prune expired stamp entries older than 5 minutes
            stamps = {k: v for k, v in stamps.items() if abs(now_ts - v) < 300.0}
            atomic_write_text(stamp_path, json.dumps(stamps) + "\n", mode=0o600)

        src_dir = str(Path(__file__).resolve().parents[1])
        child_env = dict(os.environ)
        existing_py = child_env.get("PYTHONPATH", "").strip()
        child_env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing_py}" if existing_py else src_dir

        cmd = [
            sys.executable,
            "-m",
            "tokenmaster.cli",
            "--config",
            str(cfg_path),
            "statusline",
            "--refresh",
        ]
        if selected:
            cmd.extend(["-p", ",".join(selected)])
        subprocess.Popen(
            cmd,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return True
    except Exception:
        return False


def cmd_statusline(args: argparse.Namespace, cfg_mgr: ConfigManager) -> int:
    combined_providers: list[str] = []
    for attr in ("provider", "sub_provider"):
        val = getattr(args, attr, None)
        if isinstance(val, (list, tuple)):
            combined_providers.extend(val)

    claude_code_flag = bool(getattr(args, "claude_code", False))
    as_json_flag = bool(getattr(args, "json", False))
    raw_fmt = str(getattr(args, "format", None) or "compact").strip().lower()
    if raw_fmt not in VALID_STATUSLINE_FORMATS:
        sys.stderr.write(
            f"Error: Unknown statusline format '{raw_fmt}'. Choose from: {', '.join(VALID_STATUSLINE_FORMATS)}\n"
        )
        return 2

    if as_json_flag or raw_fmt == "json":
        fmt = "json"
    elif claude_code_flag:
        fmt = "claude-code"
    else:
        fmt = raw_fmt

    is_claude_code_mode = claude_code_flag or raw_fmt == "claude-code"
    explicit_provider_filter = bool(combined_providers)
    try:
        selected = _parse_provider_filter(combined_providers or None)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    if is_claude_code_mode and not selected:
        selected = _resolve_claude_code_default_selector(cfg_mgr)

    max_age_raw = getattr(args, "max_age", 180)
    try:
        max_age = float(180 if max_age_raw is None else max_age_raw)
        if not math.isfinite(max_age) or max_age < 0.0:
            raise ValueError("max_age must be >= 0")
    except (TypeError, ValueError):
        sys.stderr.write("Error: --max-age must be a non-negative number of seconds\n")
        return 2

    force_refresh = bool(getattr(args, "refresh", False))
    no_refresh = bool(getattr(args, "no_refresh", False))
    async_refresh = bool(getattr(args, "async_refresh", False))
    include_all = bool(getattr(args, "all", False))
    color_enabled = False if getattr(args, "no_color", False) else None
    max_width = getattr(args, "width", None)

    if force_refresh and no_refresh:
        sys.stderr.write("Error: Cannot combine --refresh and --no-refresh\n")
        return 2

    claude_code_ctx = _read_claude_code_stdin() if is_claude_code_mode else {}

    cached_reports, age_s, cache_hit = load_status_snapshot(cfg_mgr, selected=selected)
    if force_refresh:
        reports = fetch_providers(cfg_mgr, selected=selected)
        save_status_snapshot(reports, replace_all=(selected is None))
        age_s = 0.0
        is_stale = False
    elif no_refresh:
        reports = cached_reports
        is_stale = (not cache_hit) or (age_s > max_age)
    elif cache_hit and age_s <= max_age:
        reports = cached_reports
        is_stale = False
    elif cached_reports and ((not cache_hit) or (age_s > max_age)) and async_refresh:
        _spawn_async_statusline_refresh(cfg_mgr.path, selected)
        reports = cached_reports
        is_stale = True
    else:
        reports = fetch_providers(cfg_mgr, selected=selected)
        save_status_snapshot(reports, replace_all=(selected is None))
        age_s = 0.0
        is_stale = False

    if not explicit_provider_filter and not include_all and not is_claude_code_mode:
        configured = [r for r in reports if r.status != "unconfigured"]
        display_reports = configured if configured else reports
    else:
        display_reports = reports

    if fmt == "json":
        payload: dict[str, Any] = {
            "version": __version__,
            "age_seconds": round(age_s, 1) if math.isfinite(age_s) else None,
            "stale": is_stale,
            "providers": {r.instance_id: r.to_dict() for r in display_reports},
        }
        if claude_code_ctx:
            payload["claude_code_session"] = claude_code_ctx
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    render_fmt = "claude-code" if is_claude_code_mode else fmt
    line = render_statusline(
        display_reports,
        fmt=render_fmt,
        claude_code_ctx=claude_code_ctx,
        color_enabled=color_enabled,
        max_width=max_width,
    )
    print(line)
    return 0


def cmd_status(args: argparse.Namespace, cfg_mgr: ConfigManager) -> int:
    combined_providers: list[str] = []
    for attr in ("provider", "sub_provider"):
        val = getattr(args, attr, None)
        if isinstance(val, (list, tuple)):
            combined_providers.extend(val)
    try:
        selected = _parse_provider_filter(combined_providers or None)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    watch_val = getattr(args, "watch", None)
    show_detail = bool(getattr(args, "detail", False))
    as_json = bool(getattr(args, "json", False))
    color_enabled = False if getattr(args, "no_color", False) else None
    max_width = getattr(args, "width", None)
    if max_width is None:
        env_w = (os.environ.get("TOKENMASTER_MAX_WIDTH") or "").strip()
        if env_w:
            try:
                parsed_env_w = int(env_w)
                if parsed_env_w > 0:
                    max_width = parsed_env_w
            except ValueError:
                pass
    notify_flag = bool(getattr(args, "notify", False))

    combined_thresholds: list[str] = []
    for t_attr in ("notify_thresholds", "sub_thresholds"):
        t_val = getattr(args, t_attr, None)
        if isinstance(t_val, (list, tuple)):
            combined_thresholds.extend(str(x) for x in t_val)
        elif isinstance(t_val, str) and t_val.strip():
            combined_thresholds.append(t_val.strip())

    notify_thresholds: Optional[list[float]] = None
    if notify_flag or combined_thresholds:
        try:
            notify_thresholds = parse_thresholds(combined_thresholds or None)
        except ValueError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 2

    if watch_val is not None and not as_json:
        import importlib
        import signal
        import threading
        import tokenmaster.ui as ui_mod

        cfg_default_interval = max(1, int(cfg_mgr.data.get("refresh_interval_seconds", 30)))
        interval = (
            cfg_default_interval
            if watch_val is _WATCH_USE_CONFIG_DEFAULT
            else max(1, int(watch_val))
        )
        is_tty = hasattr(sys.stdout, "isatty") and bool(sys.stdout.isatty())
        use_alt_screen = is_tty and (color_enabled is not False) and not os.environ.get("NO_COLOR")
        first_frame = True
        last_reports: list[ProviderReport] = []
        last_dims: tuple[Optional[int], Optional[int]] = (None, None)
        ui_file = getattr(ui_mod, "__file__", None)
        last_ui_mtime: Optional[int] = None
        if ui_file:
            try:
                last_ui_mtime = os.stat(ui_file).st_mtime_ns
            except OSError:
                last_ui_mtime = None
        resize_event = threading.Event()
        prev_winch: Any = None
        has_sigwinch = hasattr(signal, "SIGWINCH")
        if is_tty and has_sigwinch:
            try:
                prev_winch = signal.getsignal(signal.SIGWINCH)
                signal.signal(signal.SIGWINCH, lambda _s, _f: resize_event.set())
            except Exception:
                has_sigwinch = False
        alt_screen_active = False
        if use_alt_screen:
            sys.stdout.write("\x1b[?1049h\x1b[?25l")
            sys.stdout.flush()
            alt_screen_active = True
        try:
            while True:
                if ui_file:
                    try:
                        cur_mtime = os.stat(ui_file).st_mtime_ns
                        if last_ui_mtime is not None and cur_mtime != last_ui_mtime:
                            importlib.reload(ui_mod)
                            last_ui_mtime = cur_mtime
                            last_dims = (None, None)
                    except Exception:
                        pass
                reports = fetch_providers(cfg_mgr, selected=selected)
                last_reports = reports
                save_status_snapshot(reports, replace_all=(selected is None))
                if notify_flag:
                    AlertManager().process_reports(
                        reports,
                        thresholds=notify_thresholds,
                        send_desktop=True,
                    )
                resize_event.clear()
                frame_width = max_width
                frame_height: Optional[int] = None
                if is_tty:
                    tty_cols, tty_lines = ui_mod._detect_terminal_size()
                    if tty_cols is None or tty_cols <= 0:
                        term_sz = shutil.get_terminal_size((120, 30))
                        tty_cols, tty_lines = term_sz.columns, term_sz.lines
                    if frame_width is None and tty_cols and tty_cols > 0:
                        frame_width = max(20, tty_cols - 1)
                    if tty_lines and tty_lines > 0:
                        frame_height = tty_lines
                cur_dims = (frame_width, frame_height)
                need_full_clear = first_frame or (last_dims != (None, None) and cur_dims != last_dims)
                last_dims = cur_dims
                output = ui_mod.render_dashboard(
                    last_reports,
                    show_detail=show_detail,
                    watch_interval=interval,
                    color_enabled=color_enabled,
                    max_width=frame_width,
                    screen_height=frame_height,
                )
                ui_mod.paint_watch_frame(output, first_frame=need_full_clear)
                first_frame = False

                deadline = time.monotonic() + interval
                while True:
                    rem_sleep = deadline - time.monotonic()
                    if rem_sleep <= 0:
                        break
                    fired = resize_event.wait(timeout=min(0.15, rem_sleep))
                    if fired:
                        resize_event.clear()
                    reloaded_ui = False
                    if ui_file:
                        try:
                            cur_mtime = os.stat(ui_file).st_mtime_ns
                            if last_ui_mtime is not None and cur_mtime != last_ui_mtime:
                                importlib.reload(ui_mod)
                                last_ui_mtime = cur_mtime
                                reloaded_ui = True
                        except Exception:
                            pass
                    if is_tty:
                        tty_cols, tty_lines = ui_mod._detect_terminal_size()
                        if tty_cols is None or tty_cols <= 0:
                            term_sz = shutil.get_terminal_size((120, 30))
                            tty_cols, tty_lines = term_sz.columns, term_sz.lines
                        cand_w = (
                            max_width
                            if max_width is not None
                            else (max(20, tty_cols - 1) if tty_cols and tty_cols > 0 else None)
                        )
                        cand_h = tty_lines if tty_lines and tty_lines > 0 else None
                        if (cand_w, cand_h) != last_dims or fired or reloaded_ui:
                            last_dims = (cand_w, cand_h)
                            output = ui_mod.render_dashboard(
                                last_reports,
                                show_detail=show_detail,
                                watch_interval=interval,
                                color_enabled=color_enabled,
                                max_width=cand_w,
                                screen_height=cand_h,
                            )
                            ui_mod.paint_watch_frame(output, first_frame=True)
        except KeyboardInterrupt:
            if alt_screen_active:
                sys.stdout.write("\x1b[?25h\x1b[?1049l")
                sys.stdout.flush()
                alt_screen_active = False
                if last_reports:
                    tty_cols, _ = ui_mod._detect_terminal_size()
                    if tty_cols is None or tty_cols <= 0:
                        tty_cols = shutil.get_terminal_size((120, 30)).columns
                    final_w = (
                        max_width
                        if max_width is not None
                        else (max(20, tty_cols - 1) if tty_cols and tty_cols > 0 else None)
                    )
                    print(
                        ui_mod.render_dashboard(
                            last_reports,
                            show_detail=show_detail,
                            color_enabled=color_enabled,
                            max_width=final_w,
                        )
                    )
            print("\nStopped TokenMaster watch.", flush=True)
            return 0
        finally:
            if is_tty and has_sigwinch and prev_winch is not None:
                try:
                    signal.signal(signal.SIGWINCH, prev_winch)
                except Exception:
                    pass
            if alt_screen_active:
                sys.stdout.write("\x1b[?25h\x1b[?1049l")
                sys.stdout.flush()
                alt_screen_active = False

    reports = fetch_providers(cfg_mgr, selected=selected)
    save_status_snapshot(reports, replace_all=(selected is None))
    if notify_flag:
        AlertManager().process_reports(
            reports,
            thresholds=notify_thresholds,
            send_desktop=True,
        )
    if as_json:
        payload = {
            "version": __version__,
            "providers": {r.instance_id: r.to_dict() for r in reports},
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    eff_width = max_width
    if eff_width is None and hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
        tty_cols, _ = _detect_terminal_size()
        if tty_cols is not None and tty_cols > 0:
            eff_width = max(20, tty_cols - 1)
        else:
            term_cols = shutil.get_terminal_size((0, 0)).columns
            if term_cols > 0:
                eff_width = max(20, term_cols - 1)

    output = render_dashboard(
        reports,
        show_detail=show_detail,
        color_enabled=color_enabled,
        max_width=eff_width,
    )
    print(output)
    return 0


def _sync_local_sessions_to_warehouse(
    cfg_mgr: ConfigManager,
    selected: Optional[list[str]] = None,
    *,
    force_full: bool = False,
) -> None:
    """Scan local session directories for Claude, Codex, and Grok into ``warehouse.db``.

    Targets that have not yet completed a full historical backfill (or when
    ``force_full=True``) scan all files including ``>30d`` Claude / ``>7d`` Codex
    and record ``backfilled:<instance_id>`` in ``warehouse_meta``. Already-backfilled
    targets run a fast incremental scan using ``FileScanCache``.
    """
    from tokenmaster.providers.claude import ClaudeProvider
    from tokenmaster.providers.codex import CodexProvider
    from tokenmaster.providers.grok import GrokProvider

    wh = UsageWarehouse()
    _, wh_sessions, _ = wh.inspect_status()
    targets = _expand_targets(cfg_mgr, selected)
    for jkey, pid, _, pcfg in targets:
        if pid not in ("claude", "codex", "grok"):
            continue
        try:
            already_backfilled = (
                (not force_full) and (wh_sessions > 0) and wh.is_target_backfilled(jkey)
            )
            full_scan = not already_backfilled
            if pid == "claude":
                ClaudeProvider(pcfg)._scan_local_projects(include_older_than_30d=full_scan)
            elif pid == "codex":
                CodexProvider(pcfg)._scan_local_sessions(include_older_than_7d=full_scan)
            elif pid == "grok":
                GrokProvider(pcfg)._scan_local_grok_sessions()
            if full_scan:
                wh.mark_target_backfilled(jkey)
        except Exception:
            continue


def cmd_history(args: argparse.Namespace, cfg_mgr: ConfigManager) -> int:
    combined_providers: list[str] = []
    for attr in ("provider", "sub_provider"):
        val = getattr(args, attr, None)
        if isinstance(val, (list, tuple)):
            combined_providers.extend(val)
    try:
        selected = _parse_provider_filter(combined_providers or None)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    raw_mode = str(getattr(args, "history_mode", None) or "daily").strip().lower()
    norm_mode = HISTORY_MODE_ALIASES.get(raw_mode)
    if not norm_mode:
        sys.stderr.write(
            f"Error: Unknown history view '{raw_mode}'. Choose from: {', '.join(VALID_HISTORY_MODES)}\n"
        )
        return 2

    raw_since = getattr(args, "since", None)
    raw_until = getattr(args, "until", None)
    raw_days = getattr(args, "days", None)
    raw_limit = getattr(args, "limit", 100)
    force_sync = bool(getattr(args, "sync", False))
    no_sync = bool(getattr(args, "no_sync", False))
    if force_sync and no_sync:
        sys.stderr.write("Error: Cannot combine --sync and --no-sync\n")
        return 2

    if raw_since is not None and raw_days is not None:
        sys.stderr.write("Error: Cannot combine --days and --since\n")
        return 2

    try:
        since_norm = parse_date_arg(raw_since, "--since")
        until_norm = parse_date_arg(raw_until, "--until")
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    try:
        limit_val = int(100 if raw_limit is None else raw_limit)
        if limit_val <= 0:
            raise ValueError("limit must be > 0")
    except (TypeError, ValueError):
        sys.stderr.write("Error: --limit must be a positive integer\n")
        return 2

    if raw_days is not None:
        try:
            days_val = int(raw_days)
            if days_val < 0:
                raise ValueError("days must be >= 0")
        except (TypeError, ValueError):
            sys.stderr.write("Error: --days must be a non-negative integer (0 = all time)\n")
            return 2
    else:
        days_val = 30 if not (since_norm or until_norm) else 0

    if since_norm:
        eff_since: Optional[str] = since_norm
    elif days_val > 0:
        anchor_dt = (
            datetime.fromisoformat(until_norm).replace(tzinfo=timezone.utc)
            if until_norm
            else None
        )
        eff_since = compute_since_day_from_days(days_val, now=anchor_dt)
    else:
        eff_since = None

    export_fmt = str(getattr(args, "export_fmt", None) or "").strip().lower()
    csv_flag = bool(getattr(args, "csv", False))
    as_json = bool(getattr(args, "json", False))
    if export_fmt and export_fmt not in ("csv", "json"):
        sys.stderr.write("Error: --export supports 'csv' or 'json'\n")
        return 2
    chosen_formats: set[str] = set()
    if export_fmt:
        chosen_formats.add(export_fmt)
    if csv_flag:
        chosen_formats.add("csv")
    if as_json:
        chosen_formats.add("json")
    if len(chosen_formats) > 1:
        sys.stderr.write("Error: Conflicting export format flags (--csv / --json / --export)\n")
        return 2
    export_fmt = next(iter(chosen_formats), "")

    wh = UsageWarehouse()
    if norm_mode != "snapshots" and not no_sync:
        _sync_local_sessions_to_warehouse(cfg_mgr, selected=selected, force_full=force_sync)

    try:
        report_data = wh.query_history(
            norm_mode,
            selected_providers=selected,
            since_day=eff_since,
            until_day=until_norm,
            limit=limit_val,
        )
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    out_file = getattr(args, "output", None)
    if export_fmt == "csv":
        text_out = format_history_csv(report_data)
    elif export_fmt == "json":
        text_out = json.dumps(report_data, indent=2, ensure_ascii=False) + "\n"
    else:
        color_enabled = False if getattr(args, "no_color", False) else None
        max_width = getattr(args, "width", None)
        text_out = (
            render_history_report(
                report_data,
                color_enabled=color_enabled,
                max_width=max_width,
            )
            + "\n"
        )

    if out_file:
        out_path = Path(str(out_file)).expanduser()
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(out_path, text_out, mode=0o600)
            print(f"✔ Exported history ({norm_mode}) to {out_path}")
            return 0
        except OSError as exc:
            sys.stderr.write(f"Error: Failed to write output file '{out_path}': {exc}\n")
            return 1

    sys.stdout.write(text_out)
    return 0


def cmd_notify(args: argparse.Namespace, cfg_mgr: ConfigManager) -> int:
    """Single-shot or cron/launchd quota threshold & auth-expiry check + OAuth keep-alive."""
    combined_providers: list[str] = []
    for attr in ("provider", "sub_provider"):
        val = getattr(args, attr, None)
        if isinstance(val, (list, tuple)):
            combined_providers.extend(val)
    try:
        selected = _parse_provider_filter(combined_providers or None)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    combined_thresholds: list[str] = []
    for t_attr in ("notify_thresholds", "sub_thresholds"):
        t_val = getattr(args, t_attr, None)
        if isinstance(t_val, (list, tuple)):
            combined_thresholds.extend(str(x) for x in t_val)
        elif isinstance(t_val, str) and t_val.strip():
            combined_thresholds.append(t_val.strip())

    try:
        thresholds = parse_thresholds(combined_thresholds or None)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    raw_cooldown = getattr(args, "auth_cooldown", DEFAULT_AUTH_COOLDOWN_SECONDS)
    try:
        if isinstance(raw_cooldown, bool):
            raise ValueError("auth cooldown cannot be boolean")
        cooldown_val = float(
            DEFAULT_AUTH_COOLDOWN_SECONDS if raw_cooldown is None else raw_cooldown
        )
        if not math.isfinite(cooldown_val) or cooldown_val < 0.0:
            raise ValueError("auth cooldown must be >= 0")
    except (TypeError, ValueError):
        sys.stderr.write("Error: --auth-cooldown must be a non-negative number of seconds\n")
        return 2

    dry_run = bool(getattr(args, "dry_run", False))
    reset_state = bool(getattr(args, "reset_state", False))
    if dry_run and reset_state:
        sys.stderr.write("Error: Cannot combine --dry-run and --reset-state\n")
        return 2

    no_desktop = bool(getattr(args, "no_desktop", False))
    bell_flag = bool(getattr(args, "bell", False))
    quiet_flag = bool(getattr(args, "quiet", False))
    as_json = bool(getattr(args, "json", False))
    use_color = not bool(getattr(args, "no_color", False))

    mgr = AlertManager()
    if reset_state:
        mgr.clear_state()

    # Fetch live reports (also performs background OAuth token refresh keep-alive & warehouse snapshot)
    reports = fetch_providers(cfg_mgr, selected=selected)
    if not dry_run:
        save_status_snapshot(reports, replace_all=(selected is None))

    events = mgr.process_reports(
        reports,
        thresholds=thresholds,
        auth_cooldown_seconds=cooldown_val,
        send_desktop=(not no_desktop and not dry_run),
        dry_run=dry_run,
    )

    configured_reports = [r for r in reports if is_report_configured_or_errored(r)]
    if as_json:
        payload = {
            "version": __version__,
            "checked_instances": [r.instance_id for r in configured_reports],
            "thresholds": thresholds,
            "dry_run": dry_run,
            "state_path": str(mgr.path),
            "triggered_count": len(events),
            "alerts": [ev.to_dict() for ev in events],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if not events:
        if not quiet_flag:
            t_str = ", ".join(f"{int(t) if t == int(t) else t}%" for t in thresholds)
            msg = Style.wrap(
                f"✔ Checked {len(configured_reports)} subscription(s) — no new alerts (thresholds: {t_str})",
                Style.GREEN,
                enabled=use_color,
            )
            print(msg)
        return 0

    if bell_flag:
        sys.stdout.write("\a")
    for ev in events:
        if ev.severity == "critical":
            badge = Style.wrap("🚨 CRITICAL", Style.RED, Style.BOLD, enabled=use_color)
        else:
            badge = Style.wrap("⚠ WARNING", Style.YELLOW, Style.BOLD, enabled=use_color)
        print(f"{badge}  {ev.message}")

    return 0


def _resolve_target_provider_and_account(
    raw_target: Optional[str],
    explicit_account_arg: Optional[str] = None,
    *,
    allowed_providers: tuple[str, ...] = PROVIDERS,
    require_provider: bool = True,
    require_account: bool = False,
    allow_auto: bool = False,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve ``(provider_id, account_or_None)`` from ``<provider>[:<account>]`` and ``--account``."""
    if not raw_target or not raw_target.strip():
        if require_provider:
            raise ValueError(f"Provider is required. Choose from: {', '.join(allowed_providers)}")
        if explicit_account_arg and explicit_account_arg.strip():
            raw_a = explicit_account_arg.strip()
            if raw_a.lower() == "default":
                acct_only: Optional[str] = None
            elif allow_auto and raw_a.lower() == "auto":
                acct_only = "auto"
            else:
                acct_only = validate_account_name(raw_a)
            return None, acct_only
        return None, None

    raw_clean = raw_target.strip()
    if ":" in raw_clean:
        pid_part, acct_part = raw_clean.split(":", 1)
        pid_cand = pid_part.strip().lower()
        acct_cand = acct_part.strip()
        if allow_auto and acct_cand.lower() == "auto":
            pid = pid_cand
            spec_acct: Optional[str] = "auto"
            spec_explicit = True
        else:
            pid, spec_acct, spec_explicit = parse_provider_spec(raw_target)
    else:
        pid, spec_acct, spec_explicit = parse_provider_spec(raw_target)

    if pid not in allowed_providers:
        raise ValueError(
            f"Unknown provider '{pid}'. Choose from: {', '.join(allowed_providers)}"
        )

    arg_acct: Optional[str] = None
    arg_explicit = False
    if explicit_account_arg is not None and str(explicit_account_arg).strip():
        arg_explicit = True
        raw_a = str(explicit_account_arg).strip()
        if raw_a.lower() == "default":
            arg_acct = None
        elif allow_auto and raw_a.lower() == "auto":
            arg_acct = "auto"
        else:
            arg_acct = validate_account_name(raw_a)

    if spec_explicit and arg_explicit and spec_acct != arg_acct:
        raise ValueError(
            f"Conflicting account selectors: '{raw_target}' vs '{explicit_account_arg}'"
        )

    final_acct = arg_acct if arg_explicit else spec_acct
    if require_account and not final_acct:
        raise ValueError(
            f"A named subscription account is required (e.g. `{pid} work` or `{pid}:work`)"
        )
    return pid, final_acct


def cmd_auth(args: argparse.Namespace, cfg_mgr: ConfigManager) -> int:
    raw_provider = getattr(args, "target_provider", None)
    raw_account = getattr(args, "account", None)
    try:
        provider, account = _resolve_target_provider_and_account(
            raw_provider,
            raw_account,
            require_provider=False,
        )
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    target_label = f"{provider}:{account}" if (provider and account) else (provider or "")

    # Non-interactive flag updates if provided
    updated = False
    if provider:
        if getattr(args, "api_key", None):
            cfg_mgr.set_account_field(provider, account, "api_key", args.api_key.strip())
            updated = True
        if getattr(args, "management_key", None):
            cfg_mgr.set_account_field(provider, account, "management_key", args.management_key.strip())
            updated = True
        if getattr(args, "token", None):
            field_name = "oauth_token" if provider == "claude" else "access_token"
            cfg_mgr.set_account_field(provider, account, field_name, args.token.strip())
            updated = True
        if getattr(args, "cookie", None):
            cfg_mgr.set_account_field(provider, account, "cookie", args.cookie.strip())
            updated = True
        if getattr(args, "sso", None):
            cfg_mgr.set_account_field(provider, account, "sso_token", args.sso.strip())
            updated = True
        if getattr(args, "csrf_token", None):
            cfg_mgr.set_account_field(provider, account, "csrf_token", args.csrf_token.strip())
            updated = True
        if getattr(args, "port", None) is not None:
            cfg_mgr.set_account_field(provider, account, "port", int(args.port))
            updated = True

        if updated:
            if account:
                cfg_mgr.ensure_account(provider, account, create_profile_dir=True)
            print(f"✔ Updated authentication settings for '{target_label}' in {cfg_mgr.path}")
            return 0

    # Interactive wizard
    targets = [provider] if provider else list(PROVIDERS)
    print(Style.wrap("🔐 TokenMaster Interactive Auth Setup", Style.CYAN, Style.BOLD))
    print(f"Config file: {cfg_mgr.path} (Press Enter to keep existing / skip)\n")

    prompts: dict[str, list[tuple[str, str, bool]]] = {
        "codex": [
            ("access_token", "Codex ChatGPT Access Token (leave blank to auto-read auth.json)", True),
            ("api_key", "OpenAI API Key (optional)", True),
        ],
        "claude": [
            ("oauth_token", "Claude OAuth Access Token (leave blank to auto-read .credentials.json / Keychain)", True),
            ("api_key", "Anthropic API Key (optional)", True),
        ],
        "antigravity": [
            ("csrf_token", "Antigravity Language Server CSRF Token (leave blank for auto-discovery)", True),
        ],
        "grok": [
            ("cookie", "Grok (grok.com) Cookie / `sso` token (leave blank to auto-read auth.json)", True),
            ("api_key", "xAI API Key (optional)", True),
        ],
        "cursor": [
            ("access_token", "Cursor Access Token (leave blank to auto-read cursor-agent / state.vscdb)", True),
            ("cookie", "Cursor WorkosCursorSessionToken Cookie (optional)", True),
        ],
        "openrouter": [
            ("api_key", "OpenRouter API Key (sk-or-v1-...)", True),
            ("management_key", "OpenRouter Management Key for /api/v1/credits (optional if same as API key)", True),
        ],
    }

    try:
        for pid in targets:
            hdr = f"{pid.upper()} [{account}]" if account else pid.upper()
            print(Style.wrap(f"▸ [{hdr}]", Style.BOLD, Style.WHITE))
            curr_dict = (
                cfg_mgr.data.get("providers", {}).get(pid, {}).get("accounts", {}).get(account, {})
                if account
                else cfg_mgr.data.get("providers", {}).get(pid, {})
            )
            if not isinstance(curr_dict, dict):
                curr_dict = {}
            for key, label, is_secret in prompts.get(pid, []):
                curr = curr_dict.get(key)
                curr_hint = f" [current: {_mask_secret(curr)}]" if curr else ""
                prompt_text = f"  {label}{curr_hint}: "
                val = getpass.getpass(prompt_text) if (is_secret and sys.stdin.isatty()) else input(prompt_text)
                if val.strip():
                    cfg_mgr.set_account_field(pid, account, key, val.strip())
                    if account:
                        cfg_mgr.ensure_account(pid, account, create_profile_dir=True)
                    saved_lbl = f"{pid}:{account}.{key}" if account else f"{pid}.{key}"
                    print(f"    ✔ Saved {saved_lbl}")
    except (KeyboardInterrupt, EOFError):
        print("\nSetup aborted.")
        return 1

    cfg_mgr.save()
    print(f"\n✔ Configuration saved to {cfg_mgr.path}")
    return 0


def cmd_quota(args: argparse.Namespace, cfg_mgr: ConfigManager) -> int:
    try:
        provider, account = _resolve_target_provider_and_account(
            getattr(args, "target_provider", None),
            getattr(args, "account", None),
            require_provider=True,
        )
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    assert provider is not None
    target_label = f"{provider}:{account}" if account else provider

    if getattr(args, "reset", False):
        for k in (
            "weekly_limit",
            "weekly_used",
            "credit_limit",
            "credit_used",
            "credit_remaining",
        ):
            cfg_mgr.set_account_field(provider, account, k, None)
        print(f"✔ Cleared custom quota/credit overrides for '{target_label}'.")
        return 0

    changed = False
    field_map = {
        "weekly_limit": getattr(args, "weekly_limit", None),
        "weekly_used": getattr(args, "weekly_used", None),
        "weekly_unit": getattr(args, "weekly_unit", None),
        "credit_limit": getattr(args, "credit_limit", None),
        "credit_used": getattr(args, "credit_used", None),
        "credit_remaining": getattr(args, "credit_remaining", None),
        "credit_unit": getattr(args, "credit_unit", None),
    }
    for k, val in field_map.items():
        if val is not None:
            cfg_mgr.set_account_field(provider, account, k, val)
            changed = True

    if not changed:
        sys.stderr.write(
            f"No quota flags specified. Example: `tokenmaster quota {target_label} --weekly-limit 50 --credit-limit 100`\n"
        )
        return 2

    print(f"✔ Updated quota/credit settings for '{target_label}' in {cfg_mgr.path}")
    return 0


def _clear_known_caches() -> tuple[int, Path]:
    cache_dir = get_cache_dir()
    removed = 0
    stems = tuple(fname[:-5] for _, fname in KNOWN_CACHE_FILES if fname.endswith(".json"))
    with critical_section():
        for _, fname in KNOWN_CACHE_FILES:
            cpath = get_alert_state_path() if fname == ALERT_STATE_FILENAME else (cache_dir / fname)
            try:
                if cpath.is_file() or cpath.is_symlink():
                    cpath.unlink()
                    removed += 1
            except OSError:
                pass
        if cache_dir.is_dir():
            try:
                for child in list(cache_dir.iterdir()):
                    cname = child.name
                    if not cname.endswith(".json"):
                        continue
                    if any(cname.startswith(f"{stem}_") for stem in stems):
                        try:
                            if child.is_file() or child.is_symlink():
                                child.unlink()
                                removed += 1
                        except OSError:
                            pass
            except OSError:
                pass
    return removed, cache_dir


SECRET_CONFIG_KEYS = (
    "access_token",
    "oauth_token",
    "api_key",
    "management_key",
    "cookie",
    "sso_token",
    "csrf_token",
)


def cmd_config(args: argparse.Namespace, cfg_mgr: ConfigManager) -> int:
    action = getattr(args, "config_action", "show") or "show"
    if getattr(args, "clear_cache", False) or action == "clear-cache":
        removed, cache_dir = _clear_known_caches()
        print(f"✔ Cleared {removed} cache file(s) in {cache_dir}")
        return 0
    if action == "path":
        print(cfg_mgr.path)
        return 0
    if action == "init":
        path = cfg_mgr.save()
        print(f"✔ Initialized TokenMaster config at {path}")
        return 0

    # Show masked config (including nested multi-account secrets)
    masked = json.loads(json.dumps(cfg_mgr.data))
    for pid, pdata in masked.get("providers", {}).items():
        if isinstance(pdata, dict):
            for skey in SECRET_CONFIG_KEYS:
                if pdata.get(skey):
                    pdata[skey] = _mask_secret(pdata[skey])
            accts = pdata.get("accounts")
            if isinstance(accts, dict):
                for _, adata in accts.items():
                    if isinstance(adata, dict):
                        for skey in SECRET_CONFIG_KEYS:
                            if adata.get(skey):
                                adata[skey] = _mask_secret(adata[skey])
    print(f"# Config file: {cfg_mgr.path}")
    print(json.dumps(masked, indent=2, ensure_ascii=False))
    return 0


def cmd_doctor(cfg_mgr: ConfigManager) -> int:
    from tokenmaster.providers.antigravity import AntigravityProvider
    from tokenmaster.providers.claude import ClaudeProvider
    from tokenmaster.providers.codex import CodexProvider
    from tokenmaster.providers.cursor import CursorProvider
    from tokenmaster.providers.grok import GrokProvider

    print(Style.wrap("🩺 TokenMaster Environment & Credential Doctor", Style.CYAN, Style.BOLD))
    print(f"Active Config File: {cfg_mgr.path}")
    cache_dir = get_cache_dir()
    print(f"Active Cache Dir  : {cache_dir}\n")

    codex_prov = CodexProvider(cfg_mgr.account_cfg("codex", None))
    claude_prov = ClaudeProvider(cfg_mgr.account_cfg("claude", None))
    agy_prov = AntigravityProvider(cfg_mgr.account_cfg("antigravity", None))
    grok_prov = GrokProvider(cfg_mgr.account_cfg("grok", None))
    cursor_prov = CursorProvider(cfg_mgr.account_cfg("cursor", None))

    codex_dir = codex_prov._codex_home()
    claude_dir = claude_prov._claude_dir()
    agy_root = agy_prov._antigravity_dir()
    grok_dir = grok_prov._grok_home()

    claude_cred_path = next(
        (
            p
            for p in (
                claude_dir / ".credentials.json",
                claude_dir / "credentials.json",
            )
            if p.exists()
        ),
        claude_dir / ".credentials.json",
    )
    checks = [
        ("codex", "~/.codex/auth.json", codex_dir / "auth.json"),
        ("codex", "~/.codex/sessions", codex_dir / "sessions"),
        ("claude", "~/.claude/.credentials.json", claude_cred_path),
        ("claude", "~/.claude/projects", claude_dir / "projects"),
        ("antigravity", "~/.gemini/antigravity/brain", agy_root / "brain"),
        ("grok", "~/.grok/auth.json", grok_dir / "auth.json"),
        ("grok", "~/.grok/sessions", grok_dir / "sessions"),
        ("cursor", "Cursor state.vscdb", cursor_prov._default_state_db_path()),
    ]

    for pid, label, path in checks:
        exists = path.exists()
        readable = os.access(path, os.R_OK) if exists else False
        if exists and readable:
            badge = Style.wrap("✔ READABLE", Style.GREEN, Style.BOLD)
        elif exists and not readable:
            badge = Style.wrap("⚠ BLOCKED (Sandbox / Permissions)", Style.YELLOW, Style.BOLD)
        else:
            badge = Style.wrap("○ NOT FOUND", Style.GRAY)
        print(f"  [{pid:<11}] {label:<32} {badge}")

    # Check Claude macOS Keychain / OAuth
    c_bundle = claude_prov._load_oauth_bundle()
    c_tok = c_bundle.get("access_token")
    c_rt = c_bundle.get("refresh_token")
    c_sub = c_bundle.get("sub_type")
    c_exp_ms = c_bundle.get("expires_at_ms")
    if c_tok or c_rt:
        c_expired = not c_tok or claude_prov._is_oauth_expired(c_exp_ms)
        if c_expired and c_rt:
            c_state = f"{c_sub or 'OAuth'}, access token expired — auto-refresh ready"
            c_badge = Style.wrap(f"✔ DETECTED ({c_state})", Style.GREEN, Style.BOLD)
        elif c_expired and not c_rt:
            c_badge = Style.wrap(
                f"◐ EXPIRED ({c_sub or 'OAuth'}, no refreshToken — run `claude auth login`)",
                Style.YELLOW,
                Style.BOLD,
            )
        else:
            c_suffix = ", refreshToken=yes" if c_rt else ""
            c_badge = Style.wrap(f"✔ DETECTED ({c_sub or 'OAuth'}{c_suffix})", Style.GREEN, Style.BOLD)
    else:
        c_badge = Style.wrap("○ NOT FOUND", Style.GRAY)
    print(f"  [{'claude':<11}] {'macOS Keychain / OAuth Token':<32} {c_badge}")

    # Check Grok CLI token
    g_entries = grok_prov._load_grok_cli_auth_entries()
    if g_entries:
        _, g_ent = g_entries[0]
        g_mode = g_ent.get("auth_mode") or "token"
        g_exp = g_ent.get("expires_at")
        g_badge = Style.wrap(f"✔ DETECTED (mode={g_mode}, exp={g_exp})", Style.GREEN, Style.BOLD)
    else:
        g_badge = Style.wrap("○ NOT FOUND", Style.GRAY)
    print(f"  [{'grok':<11}] {'Grok CLI Auth (accounts.x.ai)':<32} {g_badge}")

    # Check Cursor CLI / Keychain & state.vscdb token
    cur_cli = cursor_prov._load_cursor_cli_auth()
    cur_db = cursor_prov._read_sqlite_auth()
    cli_keys = [k for k in ("access_token", "refresh_token", "api_key") if cur_cli.get(k)]
    if cur_cli.get("access_token") or cur_cli.get("api_key"):
        cur_badge = Style.wrap(f"✔ DETECTED (cursor-agent CLI: {cli_keys})", Style.GREEN, Style.BOLD)
    elif cur_db.get("cursorAuth/accessToken"):
        db_tok = cur_db["cursorAuth/accessToken"]
        email = cur_db.get("cursorAuth/cachedEmail") or "signed-in"
        if db_tok.startswith("cursor-safe-storage:"):
            cur_badge = Style.wrap(
                f"◐ IDE SafeStorage ({email}) — run `cursor-agent login` for CLI token",
                Style.YELLOW,
                Style.BOLD,
            )
        else:
            cur_badge = Style.wrap(f"✔ DETECTED (state.vscdb: {email})", Style.GREEN, Style.BOLD)
    else:
        cur_badge = Style.wrap("○ NOT FOUND", Style.GRAY)
    print(f"  [{'cursor':<11}] {'cursor-agent / IDE Auth State':<32} {cur_badge}")

    # Check Antigravity Language Server RPC discovery (prefer fast cached endpoint probe first)
    cached_rpc = agy_prov._load_cached_endpoint()
    cached_rpc_live = False
    ls_candidates = [] if cached_rpc is not None else agy_prov._discover_language_server()
    if cached_rpc is not None:
        c_scheme, c_port, c_tok = cached_rpc
        probe_res = agy_prov._probe_endpoint(
            c_scheme,
            c_port,
            c_tok,
            {"metadata": {"ideName": "antigravity", "extensionName": "antigravity", "locale": "en"}},
            timeout=1.0,
        )
        if probe_res is not None:
            cached_rpc_live = True
            a_badge = Style.wrap(f"✔ ACTIVE (port: {c_port}, cached)", Style.GREEN, Style.BOLD)
        else:
            ls_candidates = agy_prov._discover_language_server()
            if ls_candidates:
                ports_str = ", ".join(str(p) for p, _ in ls_candidates)
                a_badge = Style.wrap(f"✔ ACTIVE (ports: {ports_str})", Style.GREEN, Style.BOLD)
            else:
                a_badge = Style.wrap("○ NOT DETECTED", Style.GRAY)
    elif ls_candidates:
        ports_str = ", ".join(str(p) for p, _ in ls_candidates)
        a_badge = Style.wrap(f"✔ ACTIVE (ports: {ports_str})", Style.GREEN, Style.BOLD)
    else:
        a_badge = Style.wrap("○ NOT DETECTED", Style.GRAY)
    print(f"  [{'antigravity':<11}] {'Language Server Connect-RPC':<32} {a_badge}")

    print("\nLocal Scan & RPC Caches:")
    for pid, fname in KNOWN_CACHE_FILES:
        cpath = get_alert_state_path() if fname == ALERT_STATE_FILENAME else (cache_dir / fname)
        if cpath.is_file() and os.access(cpath, os.R_OK):
            try:
                raw = json.loads(cpath.read_text(encoding="utf-8"))
                if fname == "antigravity_rpc.json":
                    valid_ep = agy_prov._load_cached_endpoint()
                    if valid_ep is not None:
                        ep_scheme, ep_port, _ = valid_ep
                        if cached_rpc_live:
                            c_status = Style.wrap(
                                f"✔ READY ({ep_scheme}://127.0.0.1:{ep_port})",
                                Style.GREEN,
                                Style.BOLD,
                            )
                        else:
                            c_status = Style.wrap(
                                f"◐ STALE ({ep_scheme}://127.0.0.1:{ep_port} unreachable)",
                                Style.YELLOW,
                                Style.BOLD,
                            )
                    else:
                        c_status = Style.wrap("⚠ INVALID", Style.YELLOW, Style.BOLD)
                elif fname == "antigravity_pools.json":
                    p_state = agy_prov._inspect_pools_cache(raw)
                    if p_state == "ready":
                        fams = raw.get("families", {}) if isinstance(raw.get("families"), dict) else {}
                        p_cnt = sum(
                            sum(1 for k in ("weekly", "five_hour") if isinstance(fv, dict) and fv.get(k) is not None)
                            for fv in fams.values()
                        )
                        upd_ts = raw.get("updated_at")
                        age_txt = (
                            f"{max(0, int(time.time() - float(upd_ts)))}s ago"
                            if isinstance(upd_ts, (int, float)) and not isinstance(upd_ts, bool) and math.isfinite(float(upd_ts))
                            else "ready"
                        )
                        c_status = Style.wrap(
                            f"✔ READY ({p_cnt} model pool(s), {age_txt})",
                            Style.GREEN,
                            Style.BOLD,
                        )
                    elif p_state == "stale":
                        c_status = Style.wrap(
                            "◐ STALE (expired pool windows)",
                            Style.YELLOW,
                            Style.BOLD,
                        )
                    else:
                        c_status = Style.wrap("⚠ INVALID", Style.YELLOW, Style.BOLD)
                elif fname == "claude_oauth_usage.json":
                    cache_state = claude_prov._inspect_oauth_usage_cache(raw)
                    if cache_state == "ready":
                        age_s = max(0, int(time.time() - float(raw.get("fetched_at", 0.0))))
                        c_status = Style.wrap(
                            f"✔ READY (snapshot {age_s}s ago)",
                            Style.GREEN,
                            Style.BOLD,
                        )
                    elif cache_state == "stale":
                        c_status = Style.wrap(
                            "◐ STALE (expired snapshot)",
                            Style.YELLOW,
                            Style.BOLD,
                        )
                    else:
                        c_status = Style.wrap("⚠ INVALID", Style.YELLOW, Style.BOLD)
                elif fname == STATUS_SNAPSHOT_FILENAME:
                    if (
                        isinstance(raw, dict)
                        and raw.get("version") == STATUS_SNAPSHOT_VERSION
                        and isinstance(raw.get("entries"), dict)
                    ):
                        upd_ts = raw.get("updated_at")
                        age_txt = (
                            f"{max(0, int(time.time() - float(upd_ts)))}s ago"
                            if isinstance(upd_ts, (int, float)) and not isinstance(upd_ts, bool) and math.isfinite(float(upd_ts))
                            else "ready"
                        )
                        c_status = Style.wrap(
                            f"✔ READY ({len(raw['entries'])} instance(s), {age_txt})",
                            Style.GREEN,
                            Style.BOLD,
                        )
                    else:
                        c_status = Style.wrap("⚠ INVALID", Style.YELLOW, Style.BOLD)
                elif fname == ALERT_STATE_FILENAME:
                    if (
                        isinstance(raw, dict)
                        and not isinstance(raw.get("version"), bool)
                        and raw.get("version") == ALERT_STATE_VERSION
                        and isinstance(raw.get("entries"), dict)
                        and all(isinstance(v, dict) for v in raw["entries"].values())
                    ):
                        c_status = Style.wrap(
                            f"✔ READY ({len(raw['entries'])} active alert state(s))",
                            Style.GREEN,
                            Style.BOLD,
                        )
                    else:
                        c_status = Style.wrap("⚠ INVALID", Style.YELLOW, Style.BOLD)
                elif (
                    isinstance(raw, dict)
                    and raw.get("version") == CACHE_FORMAT_VERSION
                    and isinstance(raw.get("entries"), dict)
                    and all(isinstance(v, dict) for v in raw["entries"].values())
                ):
                    detail = f"{len(raw['entries'])} cached file(s)"
                    c_status = Style.wrap(f"✔ READY ({detail})", Style.GREEN, Style.BOLD)
                else:
                    c_status = Style.wrap("⚠ INVALID", Style.YELLOW, Style.BOLD)
            except Exception:
                c_status = Style.wrap("⚠ CORRUPT", Style.YELLOW, Style.BOLD)
        else:
            c_status = Style.wrap("○ COLD", Style.GRAY)
        print(f"  [{pid:<11}] {fname:<32} {c_status}")

    wh = UsageWarehouse()
    wh_ok, wh_sessions, wh_snaps = wh.inspect_status()
    if wh_ok:
        wh_badge = Style.wrap(
            f"✔ READY ({wh_sessions} session row(s), {wh_snaps} snapshot(s))",
            Style.GREEN,
            Style.BOLD,
        )
    elif wh.path.exists():
        wh_badge = Style.wrap("⚠ CORRUPT", Style.YELLOW, Style.BOLD)
    else:
        wh_badge = Style.wrap("○ COLD", Style.GRAY)
    print(f"  [{'warehouse':<11}] {wh.path.name:<32} {wh_badge}")

    print("\nConfigured Keys / Overrides:")
    for pid in PROVIDERS:
        pcfg = cfg_mgr.provider_cfg(pid)
        has_secret = any(
            pcfg.get(k)
            for k in ("access_token", "oauth_token", "api_key", "management_key", "cookie", "sso_token", "csrf_token")
        )
        has_quota = any(
            pcfg.get(k) is not None
            for k in ("weekly_limit", "weekly_used", "credit_limit", "credit_used", "credit_remaining")
        )
        status_parts = []
        if has_secret:
            status_parts.append("Auth Token/Key Configured")
        if has_quota:
            status_parts.append("Custom Quota Configured")
        summary = ", ".join(status_parts) if status_parts else "Auto-discovery only"
        print(f"  • {pid:<12}: {summary}")

    named_profiles: list[tuple[str, str, dict[str, Any]]] = []
    for pid in PROVIDERS:
        for acct, acfg in cfg_mgr.list_accounts(pid, include_disabled=True, ignore_provider_enabled=True):
            if acct is not None:
                named_profiles.append((pid, acct, acfg))

    if named_profiles:
        print("\nNamed Subscription Profiles:")
        for pid, acct, acfg in named_profiles:
            pdir, is_ready, detail = _inspect_account_status(cfg_mgr, pid, acct, acfg)
            enabled_flag = acfg.get("enabled", True)
            if not enabled_flag:
                badge = Style.wrap(f"○ DISABLED ({detail})", Style.GRAY)
            elif is_ready:
                badge = Style.wrap(f"✔ READY ({detail})", Style.GREEN, Style.BOLD)
            else:
                badge = Style.wrap(f"○ UNCONFIGURED ({detail})", Style.YELLOW)
            inst_id = f"{pid}:{acct}"
            print(f"  [{inst_id:<18}] {str(pdir):<44} {badge}")

    return 0


def _inspect_account_status(
    cfg_mgr: ConfigManager,
    pid: str,
    acct: Optional[str],
    acct_cfg: Optional[dict[str, Any]] = None,
) -> tuple[Path, bool, str]:
    """Inspect a provider account's directory and credential readiness without network I/O."""
    from tokenmaster.providers.antigravity import AntigravityProvider
    from tokenmaster.providers.claude import ClaudeProvider
    from tokenmaster.providers.codex import CodexProvider
    from tokenmaster.providers.cursor import CursorProvider
    from tokenmaster.providers.grok import GrokProvider
    from tokenmaster.providers.openrouter import OpenRouterProvider

    cfg = acct_cfg if isinstance(acct_cfg, dict) else cfg_mgr.account_cfg(pid, acct)
    if pid == "claude":
        prov = ClaudeProvider(cfg)
        cdir = prov._claude_dir()
        bundle = prov._load_oauth_bundle()
        tok = bundle.get("access_token")
        rt = bundle.get("refresh_token")
        sub = bundle.get("sub_type") or "active"
        expired = not tok or prov._is_oauth_expired(bundle.get("expires_at_ms"))
        if (tok and not expired) or rt:
            return cdir, True, f"OAuth ({sub})"
        if cfg.get("api_key"):
            return cdir, True, "API Key"
        if tok and expired:
            return cdir, False, f"OAuth ({sub}, expired — no refreshToken)"
        return cdir, False, "no credentials"
    if pid == "codex":
        prov_cx = CodexProvider(cfg)
        cdir = prov_cx._codex_home()
        atok, _, akey = prov_cx._load_auth()
        if atok:
            return cdir, True, "auth.json / OAuth"
        if akey:
            return cdir, True, "OPENAI_API_KEY"
        return cdir, False, "no credentials"
    if pid == "grok":
        prov_gk = GrokProvider(cfg)
        cdir = prov_gk._grok_home()
        gtok = prov_gk._load_grok_cli_token()
        if gtok:
            return cdir, True, "auth.json (Grok CLI)"
        if prov_gk._build_cookie_header():
            return cdir, True, "Cookie / SSO"
        if cfg.get("api_key"):
            return cdir, True, "xAI API Key"
        return cdir, False, "no credentials"
    if pid == "cursor":
        prov_cu = CursorProvider(cfg)
        cdir = prov_cu._cursor_home()
        cli_a = prov_cu._load_cursor_cli_auth()
        if cli_a.get("access_token") or cli_a.get("refresh_token") or cli_a.get("api_key"):
            return cdir, True, "cursor-agent CLI auth"
        if cfg.get("access_token") or cfg.get("cookie"):
            return cdir, True, "config token/cookie"
        if not prov_cu._is_isolated_profile():
            db_a = prov_cu._read_sqlite_auth()
            if prov_cu._clean_auth_token(db_a.get("cursorAuth/accessToken")):
                return cdir, True, "Cursor IDE state.vscdb"
        return cdir, False, "no credentials"
    if pid == "openrouter":
        prov_or = OpenRouterProvider(cfg)
        pdir = prov_or._resolve_isolated_dir() or (Path.home() / ".config" / "openrouter")
        ak, mk = prov_or._resolve_keys()
        if ak or mk:
            return pdir, True, "API / Management Key"
        return pdir, False, "no key"
    if pid == "antigravity":
        prov_ag = AntigravityProvider(cfg)
        adir = prov_ag._antigravity_dir()
        if cfg.get("port") and cfg.get("csrf_token"):
            return adir, True, f"port {cfg['port']}"
        if (adir / "brain").is_dir():
            return adir, True, "local brain logs"
        return adir, False, "auto-discovery"
    return get_account_profile_dir(pid, acct or "default"), False, "unknown"


def _combine_account_args(args: argparse.Namespace) -> Optional[str]:
    pos_acct = getattr(args, "account_pos", None)
    flag_acct = getattr(args, "account", None)
    pos_str = str(pos_acct).strip() if pos_acct is not None else ""
    flag_str = str(flag_acct).strip() if flag_acct is not None else ""
    if pos_str and flag_str and pos_str.lower() != flag_str.lower():
        raise ValueError(
            f"Conflicting account arguments '{pos_str}' and '--account {flag_str}'"
        )
    return flag_str if flag_str else (pos_str if pos_str else None)


def _profile_export_vars(
    cfg_mgr: ConfigManager,
    pid: str,
    acct: Optional[str],
) -> tuple[Path, dict[str, str], list[str]]:
    """Compute ``(profile_dir, set_env_vars, unset_env_vars)`` for a provider account."""
    cfg = (
        cfg_mgr.account_cfg(pid, acct)
        if acct
        else dict(cfg_mgr.data.get("providers", {}).get(pid, {}))
    )
    custom_default_dir_keys = {
        "claude": "config_dir",
        "codex": "codex_home",
        "grok": "grok_home",
        "cursor": "cursor_home",
        "antigravity": "antigravity_dir",
    }
    default_dirs = {
        "claude": Path.home() / ".claude",
        "codex": Path.home() / ".codex",
        "grok": Path.home() / ".grok",
        "cursor": Path.home() / ".cursor",
        "antigravity": Path.home() / ".gemini" / "antigravity",
        "openrouter": Path.home() / ".config" / "openrouter",
    }

    custom_default_dir: Optional[Path] = None
    if acct:
        raw_pdir = cfg.get("profile_dir")
        cand = (
            Path(os.path.abspath(str(Path(str(raw_pdir)).expanduser())))
            if isinstance(raw_pdir, str) and raw_pdir.strip()
            else None
        )
        if cand is not None and not is_forbidden_profile_dir(cand):
            profile_dir = cand
        else:
            profile_dir = get_account_profile_dir(pid, acct)
    else:
        cfg_dir_key = custom_default_dir_keys.get(pid)
        raw_cfg_dir = cfg.get(cfg_dir_key) if cfg_dir_key else None
        if isinstance(raw_cfg_dir, str) and raw_cfg_dir.strip():
            cand_def = Path(os.path.abspath(str(Path(raw_cfg_dir.strip()).expanduser())))
            if not is_forbidden_profile_dir(cand_def):
                custom_default_dir = cand_def
                profile_dir = custom_default_dir
            else:
                profile_dir = default_dirs.get(pid, Path.home())
        else:
            profile_dir = default_dirs.get(pid, Path.home())

    set_vars: dict[str, str] = {
        "TOKENMASTER_ACTIVE_PROFILE": f"{pid}:{acct}" if acct else f"{pid}:default",
    }
    unset_vars: list[str] = []

    if pid == "claude":
        if acct or custom_default_dir is not None:
            set_vars["CLAUDE_CONFIG_DIR"] = str(profile_dir)
        else:
            unset_vars.append("CLAUDE_CONFIG_DIR")
        unset_vars.extend(["CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDE_OAUTH_TOKEN"])
        if cfg.get("oauth_token"):
            set_vars["CLAUDE_CODE_OAUTH_TOKEN"] = str(cfg["oauth_token"])
        else:
            unset_vars.append("CLAUDE_CODE_OAUTH_TOKEN")
        if cfg.get("api_key"):
            set_vars["ANTHROPIC_API_KEY"] = str(cfg["api_key"])
        else:
            unset_vars.append("ANTHROPIC_API_KEY")
    elif pid == "codex":
        if acct or custom_default_dir is not None:
            set_vars["CODEX_HOME"] = str(profile_dir)
        else:
            unset_vars.append("CODEX_HOME")
        if cfg.get("access_token"):
            set_vars["CODEX_ACCESS_TOKEN"] = str(cfg["access_token"])
        else:
            unset_vars.append("CODEX_ACCESS_TOKEN")
        if cfg.get("api_key"):
            set_vars["OPENAI_API_KEY"] = str(cfg["api_key"])
        else:
            unset_vars.append("OPENAI_API_KEY")
    elif pid == "grok":
        if acct or custom_default_dir is not None:
            set_vars["GROK_HOME"] = str(profile_dir)
        else:
            unset_vars.append("GROK_HOME")
        if cfg.get("api_key"):
            set_vars["XAI_API_KEY"] = str(cfg["api_key"])
        else:
            unset_vars.extend(["XAI_API_KEY", "GROK_COOKIE", "GROK_SSO_TOKEN"])
    elif pid == "cursor":
        if acct or custom_default_dir is not None:
            set_vars["CURSOR_HOME"] = str(profile_dir)
        else:
            unset_vars.append("CURSOR_HOME")
        unset_vars.extend(["CURSOR_ACCESS_TOKEN", "CURSOR_COOKIE", "CURSOR_STATE_DB"])
    elif pid == "antigravity":
        if acct or custom_default_dir is not None:
            set_vars["ANTIGRAVITY_DIR"] = str(profile_dir)
        else:
            unset_vars.append("ANTIGRAVITY_DIR")
    elif pid == "openrouter":
        if cfg.get("api_key"):
            set_vars["OPENROUTER_API_KEY"] = str(cfg["api_key"])
        else:
            unset_vars.append("OPENROUTER_API_KEY")
        if cfg.get("management_key"):
            set_vars["OPENROUTER_MANAGEMENT_KEY"] = str(cfg["management_key"])
        else:
            unset_vars.append("OPENROUTER_MANAGEMENT_KEY")

    return profile_dir, set_vars, unset_vars


def _build_profile_child_env(
    cfg_mgr: ConfigManager,
    pid: str,
    acct: Optional[str],
) -> tuple[Path, dict[str, str]]:
    """Build a sanitized ``os.environ`` copy for spawning an isolated CLI child process."""
    profile_dir, set_vars, unset_vars = _profile_export_vars(cfg_mgr, pid, acct)
    env = dict(os.environ)
    for u_key in unset_vars:
        env.pop(u_key, None)
    env.update(set_vars)
    return profile_dir, env


def _read_macos_keychain_secret(service: str, account: Optional[str] = None) -> Optional[str]:
    if sys.platform != "darwin":
        return None
    cmds = (
        [["security", "find-generic-password", "-s", service, "-a", account, "-w"]]
        if account
        else []
    ) + [["security", "find-generic-password", "-s", service, "-w"]]
    for cmd in cmds:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0, check=False)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            continue
    return None


def _write_macos_keychain_secret(service: str, account: str, secret: str) -> bool:
    if sys.platform != "darwin" or not secret:
        return False
    try:
        res = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", secret],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


def _delete_macos_keychain_secret(service: str) -> None:
    if sys.platform != "darwin":
        return
    for _ in range(4):
        try:
            res = subprocess.run(
                ["security", "delete-generic-password", "-s", service],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if getattr(res, "returncode", 1) != 0:
                break
        except Exception:
            break


def snapshot_account_credentials(
    cfg_mgr: ConfigManager,
    provider_id: str,
    account: str,
) -> tuple[bool, Path, str]:
    """Snapshot the currently active default credentials for ``provider_id`` into ``account``'s sandbox.

    Returns ``(ok, profile_dir, message)``.
    """
    from tokenmaster.providers.antigravity import AntigravityProvider
    from tokenmaster.providers.claude import (
        ClaudeProvider,
        claude_keychain_account_name,
        claude_keychain_service_name_for_dir,
        claude_keychain_service_names,
    )
    from tokenmaster.providers.codex import CodexProvider
    from tokenmaster.providers.cursor import CursorProvider
    from tokenmaster.providers.grok import GrokProvider
    from tokenmaster.providers.openrouter import OpenRouterProvider

    pid = provider_id.strip().lower()
    acct = validate_account_name(account)
    default_cfg = cfg_mgr.account_cfg(pid, None)

    if pid == "claude":
        c_prov = ClaudeProvider(default_cfg)
        src_dir = c_prov._claude_dir()
        raw_cred_json: Optional[str] = None
        if default_cfg.get("oauth_token"):
            raw_cred_json = json.dumps(
                {"claudeAiOauth": {"accessToken": str(default_cfg["oauth_token"]).strip()}},
                indent=2,
            )
        if not raw_cred_json:
            for fname in (".credentials.json", "credentials.json"):
                fpath = src_dir / fname
                if fpath.is_file() and os.access(fpath, os.R_OK):
                    try:
                        txt = fpath.read_text(encoding="utf-8").strip()
                        parsed = json.loads(txt) if txt else None
                        if isinstance(parsed, dict) and (
                            (isinstance(parsed.get("claudeAiOauth"), dict) and parsed["claudeAiOauth"].get("accessToken"))
                            or parsed.get("accessToken")
                            or parsed.get("oauth_token")
                        ):
                            raw_cred_json = txt
                            break
                    except Exception:
                        pass
        if not raw_cred_json:
            for svc_cand in claude_keychain_service_names(src_dir, is_isolated_profile=c_prov._is_isolated_profile()):
                kc_raw = _read_macos_keychain_secret(svc_cand)
                if kc_raw:
                    try:
                        parsed_kc = json.loads(kc_raw)
                        if isinstance(parsed_kc, dict) and (
                            (isinstance(parsed_kc.get("claudeAiOauth"), dict) and parsed_kc["claudeAiOauth"].get("accessToken"))
                            or parsed_kc.get("accessToken")
                            or parsed_kc.get("oauth_token")
                        ):
                            raw_cred_json = kc_raw
                            break
                    except Exception:
                        pass
        if not raw_cred_json:
            tok, sub, tier = c_prov._load_oauth_credentials()
            if tok:
                raw_cred_json = json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": tok,
                            "subscriptionType": sub,
                            "rateLimitTier": tier,
                        }
                    },
                    indent=2,
                )
        if not raw_cred_json and default_cfg.get("api_key"):
            profile_dir = cfg_mgr.ensure_account(pid, acct, create_profile_dir=True)
            cfg_mgr.set_account_field(pid, acct, "api_key", default_cfg["api_key"])
            return True, profile_dir, "Snapshotted Anthropic API key into account config"
        if not raw_cred_json:
            return False, get_account_profile_dir(pid, acct), "No active default Claude credentials found to snapshot"

        profile_dir = cfg_mgr.ensure_account(pid, acct, create_profile_dir=True)
        dest_file = profile_dir / ".credentials.json"
        with critical_section():
            atomic_write_text(dest_file, raw_cred_json.rstrip() + "\n", mode=0o600)
            svc = claude_keychain_service_name_for_dir(profile_dir)
            _write_macos_keychain_secret(svc, claude_keychain_account_name(), raw_cred_json)
        return True, profile_dir, f"Saved Claude OAuth credentials to {dest_file}"

    if pid == "codex":
        cx_prov = CodexProvider(default_cfg)
        src_auth = cx_prov._codex_home() / "auth.json"
        payload_txt: Optional[str] = None
        if src_auth.is_file() and os.access(src_auth, os.R_OK):
            try:
                txt = src_auth.read_text(encoding="utf-8").strip()
                parsed_cx = json.loads(txt) if txt else None
                if isinstance(parsed_cx, dict) and (
                    (isinstance(parsed_cx.get("tokens"), dict) and parsed_cx["tokens"].get("access_token"))
                    or parsed_cx.get("access_token")
                    or parsed_cx.get("OPENAI_API_KEY")
                    or parsed_cx.get("api_key")
                ):
                    payload_txt = txt
            except Exception:
                pass
        if not payload_txt:
            atok, aid, akey = cx_prov._load_auth()
            if atok or akey:
                obj: dict[str, Any] = {}
                if atok:
                    obj["tokens"] = {"access_token": atok, "account_id": aid}
                    obj["access_token"] = atok
                    if aid:
                        obj["account_id"] = aid
                if akey:
                    obj["OPENAI_API_KEY"] = akey
                payload_txt = json.dumps(obj, indent=2)
        if not payload_txt:
            return False, get_account_profile_dir(pid, acct), "No active default Codex credentials found to snapshot"

        profile_dir = cfg_mgr.ensure_account(pid, acct, create_profile_dir=True)
        dest_file = profile_dir / "auth.json"
        with critical_section():
            atomic_write_text(dest_file, payload_txt.rstrip() + "\n", mode=0o600)
        return True, profile_dir, f"Saved Codex credentials to {dest_file}"

    if pid == "grok":
        gk_prov = GrokProvider(default_cfg)
        src_auth = gk_prov._grok_home() / "auth.json"
        if src_auth.is_file() and os.access(src_auth, os.R_OK) and gk_prov._load_grok_cli_token():
            try:
                txt = src_auth.read_text(encoding="utf-8").strip()
                if txt and isinstance(json.loads(txt), dict):
                    profile_dir = cfg_mgr.ensure_account(pid, acct, create_profile_dir=True)
                    dest_file = profile_dir / "auth.json"
                    with critical_section():
                        atomic_write_text(dest_file, txt.rstrip() + "\n", mode=0o600)
                    return True, profile_dir, f"Saved Grok CLI auth.json to {dest_file}"
            except Exception:
                pass
        if default_cfg.get("cookie") or default_cfg.get("sso_token") or default_cfg.get("api_key"):
            profile_dir = cfg_mgr.ensure_account(pid, acct, create_profile_dir=True)
            for k in ("cookie", "sso_token", "api_key"):
                if default_cfg.get(k):
                    cfg_mgr.set_account_field(pid, acct, k, default_cfg[k])
            return True, profile_dir, "Snapshotted Grok cookie/API key into account config"
        return False, get_account_profile_dir(pid, acct), "No active default Grok credentials found to snapshot"

    if pid == "cursor":
        cu_prov = CursorProvider(default_cfg)
        cli_a = cu_prov._load_cursor_cli_auth()
        db_a = cu_prov._read_sqlite_auth()
        at = (
            cli_a.get("access_token")
            or cu_prov._clean_auth_token(default_cfg.get("access_token"))
            or cu_prov._clean_auth_token(db_a.get("cursorAuth/accessToken"))
        )
        rt = (
            cli_a.get("refresh_token")
            or cu_prov._clean_auth_token(db_a.get("cursorAuth/refreshToken"), is_refresh=True)
        )
        ak = cli_a.get("api_key") or cu_prov._clean_auth_token(default_cfg.get("api_key"))
        if not (at or rt or ak or default_cfg.get("cookie")):
            return False, get_account_profile_dir(pid, acct), "No active default Cursor credentials found to snapshot"

        profile_dir = cfg_mgr.ensure_account(pid, acct, create_profile_dir=True)
        if at or rt or ak:
            auth_obj: dict[str, str] = {}
            if at:
                auth_obj["accessToken"] = at
            if rt:
                auth_obj["refreshToken"] = rt
            if ak:
                auth_obj["apiKey"] = ak
            dest_file = profile_dir / "auth.json"
            with critical_section():
                atomic_write_text(dest_file, json.dumps(auth_obj, indent=2) + "\n", mode=0o600)
        if default_cfg.get("cookie"):
            cfg_mgr.set_account_field(pid, acct, "cookie", default_cfg["cookie"])
        return True, profile_dir, f"Saved Cursor credentials to {profile_dir / 'auth.json'}"

    if pid == "openrouter":
        or_prov = OpenRouterProvider(default_cfg)
        ak, mk = or_prov._resolve_keys()
        if not ak and not mk:
            return False, get_account_profile_dir(pid, acct), "No active default OpenRouter keys found to snapshot"
        profile_dir = cfg_mgr.ensure_account(pid, acct, create_profile_dir=True)
        if ak:
            cfg_mgr.set_account_field(pid, acct, "api_key", ak)
            with critical_section():
                atomic_write_text(profile_dir / "key", ak + "\n", mode=0o600)
        if mk:
            cfg_mgr.set_account_field(pid, acct, "management_key", mk)
        return True, profile_dir, f"Saved OpenRouter credentials for openrouter:{acct}"

    if pid == "antigravity":
        ag_prov = AntigravityProvider(default_cfg)
        port = default_cfg.get("port")
        csrf = default_cfg.get("csrf_token")
        if not (port and csrf):
            cached = ag_prov._load_cached_endpoint()
            if cached:
                _, port, csrf = cached
            else:
                cands = ag_prov._discover_language_server()
                if cands:
                    port, csrf = cands[0]
        if not (port and csrf):
            return False, get_account_profile_dir(pid, acct), "No active Antigravity RPC endpoint found to snapshot"
        profile_dir = cfg_mgr.ensure_account(pid, acct, create_profile_dir=True)
        cfg_mgr.set_account_field(pid, acct, "port", int(port))
        cfg_mgr.set_account_field(pid, acct, "csrf_token", str(csrf))
        return True, profile_dir, f"Saved Antigravity RPC endpoint (port {port}) for antigravity:{acct}"

    return False, get_account_profile_dir(pid, acct), f"Unsupported provider '{pid}'"


def _resolve_cli_binary(provider: str) -> str:
    search_path = (
        os.environ.get("PATH", "")
        + f":{Path.home() / '.local' / 'bin'}:/opt/homebrew/bin:/usr/local/bin"
    )
    if provider == "grok":
        return shutil.which("grok", path=search_path) or "/opt/homebrew/bin/grok"
    if provider == "cursor":
        return (
            shutil.which("cursor-agent", path=search_path)
            or str(Path.home() / ".local" / "bin" / "cursor-agent")
        )
    if provider == "codex":
        return shutil.which("codex", path=search_path) or "codex"
    if provider == "claude":
        return shutil.which("claude", path=search_path) or "/opt/homebrew/bin/claude"
    raise ValueError(f"Provider '{provider}' does not have an interactive CLI binary")


def cmd_login(args: argparse.Namespace, cfg_mgr: Optional[ConfigManager] = None) -> int:
    from tokenmaster.providers.claude import claude_keychain_service_name_for_dir

    mgr = cfg_mgr or ConfigManager()
    raw_target = getattr(args, "target_provider", None)
    try:
        combined_acct = _combine_account_args(args)
        target, account = _resolve_target_provider_and_account(
            raw_target,
            combined_acct,
            allowed_providers=("grok", "cursor", "codex", "claude"),
            require_provider=True,
        )
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    assert target is not None

    binary = _resolve_cli_binary(target)
    cmd = [binary, "auth", "login"] if target == "claude" else [binary, "login"]

    if not account:
        _, default_child_env = _build_profile_child_env(mgr, target, None)
        print(Style.wrap(f"🔐 Launching `{' '.join(cmd)}` (default subscription) ...", Style.CYAN, Style.BOLD))
        try:
            res = subprocess.run(cmd, env=default_child_env, check=False)
            return res.returncode
        except FileNotFoundError:
            sys.stderr.write(f"Error: CLI binary not found for '{target}': {cmd[0]}\n")
            return 1
        except KeyboardInterrupt:
            sys.stderr.write("\nLogin aborted.\n")
            return 130

    # Isolated profile sandbox login
    profile_dir = mgr.ensure_account(target, account, create_profile_dir=True)
    _, child_env = _build_profile_child_env(mgr, target, account)

    print(
        Style.wrap(
            f"🔐 Launching isolated `{' '.join(cmd)}` for [{target}:{account}]\n"
            f"   Sandbox Dir: {profile_dir}",
            Style.CYAN,
            Style.BOLD,
        )
    )

    # For Cursor: cursor-agent login writes to global macOS Keychain services
    # (cursor-access-token, cursor-refresh-token, cursor-api-key). Preserve the
    # pre-login Keychain state, clear those services before running `cursor-agent login`
    # so only newly written tokens are captured into <profile_dir>/auth.json, and
    # restore the pre-login Keychain state in `finally`.
    cursor_kc_services = ("cursor-access-token", "cursor-refresh-token", "cursor-api-key")
    prev_cursor_kc: dict[str, Optional[str]] = {}

    try:
        if target == "cursor" and sys.platform == "darwin":
            with critical_section():
                for svc in cursor_kc_services:
                    prev_cursor_kc[svc] = _read_macos_keychain_secret(svc, "cursor-user")
                    _delete_macos_keychain_secret(svc)
        res = subprocess.run(cmd, env=child_env, check=False)
    except FileNotFoundError:
        sys.stderr.write(f"Error: CLI binary not found for '{target}': {cmd[0]}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\nLogin aborted.\n")
        return 130
    finally:
        if target == "cursor" and sys.platform == "darwin":
            # If login succeeded, capture newly written Keychain tokens into <profile_dir>/auth.json
            if "res" in locals() and res.returncode == 0:
                post_at = _read_macos_keychain_secret("cursor-access-token", "cursor-user")
                post_rt = _read_macos_keychain_secret("cursor-refresh-token", "cursor-user")
                post_ak = _read_macos_keychain_secret("cursor-api-key", "cursor-user")
                auth_obj: dict[str, str] = {}
                if post_at:
                    auth_obj["accessToken"] = post_at
                if post_rt and post_rt != "xai-identity-session":
                    auth_obj["refreshToken"] = post_rt
                if post_ak:
                    auth_obj["apiKey"] = post_ak
                if auth_obj:
                    auth_file = profile_dir / "auth.json"
                    with critical_section():
                        atomic_write_text(auth_file, json.dumps(auth_obj, indent=2) + "\n", mode=0o600)
            # Always restore pre-login Keychain state so default Cursor account is untouched
            with critical_section():
                for svc, prev_val in prev_cursor_kc.items():
                    _delete_macos_keychain_secret(svc)
                    if prev_val:
                        _write_macos_keychain_secret(svc, "cursor-user", prev_val)

    if res.returncode == 0 and target == "claude" and sys.platform == "darwin":
        # Mirror the profile-scoped Keychain credential into <profile_dir>/.credentials.json
        svc_name = claude_keychain_service_name_for_dir(profile_dir)
        kc_json = _read_macos_keychain_secret(svc_name)
        if kc_json:
            try:
                if isinstance(json.loads(kc_json), dict):
                    with critical_section():
                        atomic_write_text(
                            profile_dir / ".credentials.json",
                            kc_json.rstrip() + "\n",
                            mode=0o600,
                        )
            except Exception:
                pass

    if res.returncode == 0:
        print(
            Style.wrap(
                f"✔ Authenticated [{target}:{account}] in sandbox {profile_dir}",
                Style.GREEN,
                Style.BOLD,
            )
        )
    return res.returncode


VALID_PICK_STRATEGIES: tuple[str, ...] = ("smart", "most-remaining", "expiring-first")


def _finite_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    try:
        fval = float(val)
    except (TypeError, ValueError):
        return None
    return fval if math.isfinite(fval) else None


def _finite_pct(val: Any) -> Optional[float]:
    fval = _finite_float(val)
    if fval is None:
        return None
    return max(0.0, min(100.0, fval))


def _short_window_remaining_pct(r: ProviderReport) -> Optional[float]:
    """Return 5-hour short-window remaining percentage if present in ``r.sub_metrics``."""
    cands: list[float] = []
    for sm in r.sub_metrics:
        lbl = (getattr(sm, "name", None) or getattr(sm, "label", None) or "").lower()
        if "5h" in lbl or "5-hour" in lbl or "five_hour" in lbl:
            rem_pct = _finite_pct(getattr(sm, "remaining_percent", None))
            if rem_pct is not None:
                cands.append(rem_pct)
                continue
            used_pct = _finite_pct(sm.used_percent)
            if used_pct is not None:
                cands.append(max(0.0, min(100.0, 100.0 - used_pct)))
    if not cands:
        return None
    return max(cands)


def select_best_account(
    reports: list[ProviderReport],
    strategy: str = "smart",
) -> tuple[Optional[ProviderReport], str]:
    """Select the optimal subscription account from ``reports`` for a single provider.

    Returns ``(best_report_or_None, human_readable_reason)``.
    """
    strat = (strategy or "smart").strip().lower()
    if strat not in VALID_PICK_STRATEGIES:
        raise ValueError(
            f"Unknown pick strategy '{strategy}'. Choose from: {', '.join(VALID_PICK_STRATEGIES)}"
        )
    if not reports:
        return None, "No subscription accounts configured or enabled"

    viable: list[tuple[tuple[Any, ...], ProviderReport, str]] = []
    skipped_reasons: list[str] = []

    for r in reports:
        acct_lbl = f"{r.provider_id}:{r.account}" if r.account else f"{r.provider_id}:default"
        if r.status not in ("ok", "partial"):
            skipped_reasons.append(f"{acct_lbl}={r.status}")
            continue

        w_rem = _finite_pct(r.weekly.remaining_percent)
        scoped_rem = _finite_pct(r.weekly.scoped_remaining_percent)
        pace_used = _finite_pct(r.weekly.pace_used_percent)
        pace_rem = (100.0 - pace_used) if pace_used is not None else None
        s5_rem = _short_window_remaining_pct(r)
        cred_rem_pct = _finite_pct(r.credits.remaining_percent)
        cred_rem_opt = _finite_float(r.credits.remaining)
        cred_lim_opt = _finite_float(r.credits.limit)

        if w_rem is not None and w_rem <= 1.0:
            skipped_reasons.append(f"{acct_lbl}=weekly exhausted ({w_rem:.1f}% rem)")
            continue
        if scoped_rem is not None and scoped_rem <= 1.0:
            s_lbl = r.weekly.scoped_label or "scoped"
            skipped_reasons.append(f"{acct_lbl}={s_lbl} pool exhausted ({scoped_rem:.1f}% rem)")
            continue
        if pace_rem is not None and pace_rem <= 1.0:
            skipped_reasons.append(f"{acct_lbl}=plan cycle exhausted ({pace_rem:.1f}% rem)")
            continue
        if s5_rem is not None and s5_rem <= 5.0:
            skipped_reasons.append(f"{acct_lbl}=5h window exhausted ({s5_rem:.1f}% rem)")
            continue

        weekly_pct_candidates = [p for p in (w_rem, scoped_rem, pace_rem) if p is not None]
        eff_weekly_rem: Optional[float] = min(weekly_pct_candidates) if weekly_pct_candidates else None

        # Check credit exhaustion when no weekly % pool exists, or on credit-backed OpenRouter,
        # or when weekly absolute remaining is 0 with a positive limit.
        if eff_weekly_rem is None:
            w_abs_rem = _finite_float(r.weekly.remaining)
            w_abs_lim = _finite_float(r.weekly.limit)
            if w_abs_rem is not None and w_abs_rem <= 0.0 and w_abs_lim is not None and w_abs_lim > 0.0:
                skipped_reasons.append(f"{acct_lbl}=weekly quota exhausted")
                continue

        if not r.credits.is_unlimited and (
            eff_weekly_rem is None
            or r.provider_id == "openrouter"
            or (cred_lim_opt is not None and cred_lim_opt > 0.0 and eff_weekly_rem is None)
        ):
            if cred_rem_pct is not None and cred_rem_pct <= 1.0:
                skipped_reasons.append(
                    f"{acct_lbl}=credits exhausted ({cred_rem_pct:.1f}% rem)"
                )
                continue
            if cred_rem_opt is not None and cred_rem_opt <= 0.0:
                skipped_reasons.append(f"{acct_lbl}=credits exhausted ($0.00 rem)")
                continue

        is_live = 1 if r.status == "ok" else 0
        eff_rem_pct: Optional[float] = (
            eff_weekly_rem if eff_weekly_rem is not None else cred_rem_pct
        )
        s5_val = float(s5_rem) if s5_rem is not None else 100.0
        rem_sec = parse_reset_remaining_seconds(r.weekly.resets_at, r.weekly.resets_in)
        if rem_sec is not None and (not math.isfinite(rem_sec) or rem_sec < 0.0):
            rem_sec = None

        comfortable_headroom = (
            1
            if (s5_rem is None or s5_rem >= 10.0)
            and (eff_rem_pct is None or eff_rem_pct >= 5.0)
            else 0
        )
        has_pct = 1 if eff_rem_pct is not None else 0
        cred_rem = cred_rem_opt if cred_rem_opt is not None else 0.0

        if strat == "most-remaining":
            sort_key: tuple[Any, ...] = (
                is_live,
                comfortable_headroom,
                has_pct,
                round(eff_rem_pct or 0.0, 4),
                round(s5_val, 4),
                -(rem_sec if rem_sec is not None else float("inf")),
                round(cred_rem, 4),
            )
        elif strat == "expiring-first":
            has_reset = 1 if rem_sec is not None else 0
            sort_key = (
                is_live,
                comfortable_headroom,
                has_reset,
                -(rem_sec if rem_sec is not None else float("inf")),
                round(eff_rem_pct or 0.0, 4),
                round(s5_val, 4),
                round(cred_rem, 4),
            )
        else:
            # "smart": prioritize quota expiring soon that is not overburning, weighted by 5h headroom
            not_overburn = 0 if r.weekly.pace_status == "overburn" else 1
            if eff_rem_pct is not None:
                win_days = max(1.0, float(r.weekly.window_seconds or 604800) / 86400.0)
                days_left = (
                    max(0.25, rem_sec / 86400.0)
                    if rem_sec is not None
                    else max(1.0, win_days * 0.5)
                )
                daily_allowance = eff_rem_pct / days_left
                s5_factor = 0.5 + 0.5 * (max(0.0, min(100.0, s5_val)) / 100.0)
                pace_factor = (
                    0.5
                    if r.weekly.pace_status == "overburn"
                    else (1.15 if r.weekly.pace_status == "surplus" else 1.0)
                )
                smart_score = daily_allowance * s5_factor * pace_factor
            else:
                smart_score = 0.0
            sort_key = (
                is_live,
                comfortable_headroom,
                not_overburn,
                has_pct,
                round(smart_score, 6),
                round(eff_rem_pct or 0.0, 4),
                round(s5_val, 4),
                round(cred_rem, 4),
            )

        reason_parts: list[str] = []
        if eff_weekly_rem is not None:
            reason_parts.append(f"{eff_weekly_rem:.1f}% weekly rem")
        elif eff_rem_pct is not None:
            reason_parts.append(f"{eff_rem_pct:.1f}% credit rem")
        elif cred_rem_opt is not None:
            reason_parts.append(f"credits {r.credits.remaining_str()}")
        if s5_rem is not None:
            reason_parts.append(f"5h rem {s5_rem:.0f}%")
        if rem_sec is not None:
            reason_parts.append(f"resets in {format_duration_short(rem_sec)}")
        if r.weekly.pace_status:
            reason_parts.append(f"pace={r.weekly.pace_status}")
        if not reason_parts:
            reason_parts.append(f"status={r.status}")
        reason_parts.append(f"strategy={strat}")
        viable.append((sort_key, r, ", ".join(reason_parts)))

    if not viable:
        detail = "; ".join(skipped_reasons) if skipped_reasons else "no healthy accounts"
        return None, f"No available subscription accounts ({detail})"

    viable.sort(key=lambda item: item[0], reverse=True)
    _, best_report, best_reason = viable[0]
    return best_report, best_reason


def _resolve_auto_account(
    cfg_mgr: ConfigManager,
    pid: str,
    strategy: str = "smart",
) -> tuple[Optional[ProviderReport], str]:
    """Fetch all enabled accounts for ``pid`` and pick the optimal one."""
    reports = fetch_providers(cfg_mgr, selected=[pid])
    save_status_snapshot(reports, replace_all=False)
    return select_best_account(reports, strategy=strategy)


def cmd_pick(args: argparse.Namespace, cfg_mgr: ConfigManager) -> int:
    raw_target = str(getattr(args, "target_provider", None) or "").strip()
    strategy = str(getattr(args, "strategy", None) or "smart").strip().lower()
    as_json = bool(getattr(args, "json", False))
    quiet = bool(getattr(args, "quiet", False))

    try:
        if strategy not in VALID_PICK_STRATEGIES:
            raise ValueError(
                f"Unknown pick strategy '{strategy}'. Choose from: {', '.join(VALID_PICK_STRATEGIES)}"
            )
        pid, acct = _resolve_target_provider_and_account(
            raw_target,
            None,
            allowed_providers=PROVIDERS,
            require_provider=True,
            require_account=False,
            allow_auto=True,
        )
        if acct not in (None, "auto") or (
            ":" in raw_target and raw_target.split(":", 1)[1].strip().lower() == "default"
        ):
            raise ValueError(
                f"`tokenmaster pick` selects across all accounts of a provider; pass `{pid}` instead of `{raw_target}`"
            )
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    assert pid is not None

    reports = fetch_providers(cfg_mgr, selected=[pid])
    save_status_snapshot(reports, replace_all=False)
    best, reason = select_best_account(reports, strategy=strategy)

    if as_json:
        selected_acct = (best.account or "default") if best is not None else None
        selected_inst = (
            (f"{pid}:{best.account}" if best.account else f"{pid}:default")
            if best is not None
            else None
        )
        payload = {
            "provider": pid,
            "strategy": strategy,
            "selected_account": selected_acct,
            "selected_instance": selected_inst,
            "reason": reason,
            "candidates": [r.to_dict() for r in reports],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if best is not None else 1

    if best is None:
        sys.stderr.write(f"Error: Could not pick an account for '{pid}': {reason}\n")
        return 1

    acct_name = best.account or "default"
    inst_lbl = f"{pid}:{acct_name}"
    if quiet:
        print(acct_name)
    else:
        print(
            Style.wrap(f"⚡ Selected {inst_lbl}", Style.GREEN, Style.BOLD)
            + f" ({reason})"
        )
    return 0


def cmd_env(args: argparse.Namespace, cfg_mgr: ConfigManager) -> int:
    raw_target = getattr(args, "target_provider", None)
    strategy = str(getattr(args, "strategy", None) or "smart").strip().lower()
    try:
        if strategy not in VALID_PICK_STRATEGIES:
            raise ValueError(
                f"Unknown pick strategy '{strategy}'. Choose from: {', '.join(VALID_PICK_STRATEGIES)}"
            )
        combined_acct = _combine_account_args(args)
        pid, acct = _resolve_target_provider_and_account(
            raw_target,
            combined_acct,
            require_provider=True,
            require_account=False,
            allow_auto=True,
        )
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    assert pid is not None

    auto_comment: Optional[str] = None
    if acct == "auto":
        best, reason = _resolve_auto_account(cfg_mgr, pid, strategy=strategy)
        if best is None:
            sys.stderr.write(f"Error: Could not auto-select account for '{pid}': {reason}\n")
            return 1
        acct = best.account
        chosen_lbl = f"{pid}:{acct}" if acct else f"{pid}:default"
        safe_reason = reason.replace("\r", " ").replace("\n", " ")
        auto_comment = f"# Auto-selected {chosen_lbl} ({safe_reason})"

    profile_dir, set_vars, unset_vars = _profile_export_vars(cfg_mgr, pid, acct)
    lbl = f"{pid}:{acct}" if acct else f"{pid}:default"
    safe_pdir_comment = str(profile_dir).replace("\r", " ").replace("\n", " ")
    lines: list[str] = []
    if auto_comment:
        lines.append(auto_comment)
    lines.append(f"# TokenMaster environment for {lbl} ({safe_pdir_comment})")
    for u_key in unset_vars:
        lines.append(f"unset {u_key}")
    for k, v in set_vars.items():
        lines.append(f"export {k}={shlex.quote(v)}")
    print("\n".join(lines))
    return 0


def cmd_exec(args: argparse.Namespace, cfg_mgr: ConfigManager) -> int:
    raw_target = str(getattr(args, "target_provider", None) or "").strip()
    flag_acct = getattr(args, "account", None)
    flag_acct_str = str(flag_acct).strip() if flag_acct is not None else ""
    strategy = str(getattr(args, "strategy", None) or "smart").strip().lower()
    explicit_pos = getattr(args, "account_pos", None)
    pos_acct: Optional[str] = str(explicit_pos).strip() if (explicit_pos is not None and str(explicit_pos).strip()) else None
    rem_args = list(getattr(args, "cli_args", None) or [])

    # Also consume leading `-a <acct>`, `--account <acct>`, `-a=<acct>`, `--account=<acct>`,
    # `--strategy <strat>`, `--strategy=<strat>`, `-s <strat>`, and/or a single positional
    # account token before `--` in `rem_args`.
    has_colon_acct = ":" in raw_target
    colon_is_auto = has_colon_acct and raw_target.split(":", 1)[1].strip().lower() == "auto"
    has_dashdash = "--" in rem_args
    pre_dash_tokens = rem_args[: rem_args.index("--")] if has_dashdash else rem_args
    any_auto_in_rem = any(
        t.strip().lower() in ("auto", "-a=auto", "--account=auto")
        for t in pre_dash_tokens
    )
    while rem_args and rem_args[0] != "--":
        tok = rem_args[0]
        if tok in ("-a", "--account"):
            if len(rem_args) < 2 or rem_args[1] == "--":
                sys.stderr.write(f"Error: Option '{tok}' requires an account name\n")
                return 2
            val = rem_args[1].strip()
            if flag_acct_str and flag_acct_str.lower() != val.lower():
                sys.stderr.write(
                    f"Error: Conflicting account arguments '{flag_acct_str}' and '{tok} {val}'\n"
                )
                return 2
            flag_acct_str = val
            rem_args = rem_args[2:]
            continue
        if tok.startswith("--account=") or tok.startswith("-a="):
            val = tok.split("=", 1)[1].strip()
            if not val:
                sys.stderr.write("Error: Empty account name in '--account='\n")
                return 2
            if flag_acct_str and flag_acct_str.lower() != val.lower():
                sys.stderr.write(
                    f"Error: Conflicting account arguments '{flag_acct_str}' and '{tok}'\n"
                )
                return 2
            flag_acct_str = val
            rem_args = rem_args[1:]
            continue
        if tok == "--strategy" or (
            tok == "-s"
            and (
                has_dashdash
                or colon_is_auto
                or flag_acct_str.lower() == "auto"
                or (pos_acct or "").lower() == "auto"
                or any_auto_in_rem
                or (len(rem_args) >= 2 and rem_args[1].strip().lower() in VALID_PICK_STRATEGIES)
            )
        ):
            if len(rem_args) < 2 or rem_args[1] == "--":
                sys.stderr.write(f"Error: Option '{tok}' requires a strategy name\n")
                return 2
            strategy = rem_args[1].strip().lower()
            rem_args = rem_args[2:]
            continue
        if tok.startswith("--strategy=") or (
            tok.startswith("-s=")
            and (
                has_dashdash
                or colon_is_auto
                or flag_acct_str.lower() == "auto"
                or (pos_acct or "").lower() == "auto"
                or any_auto_in_rem
                or tok.split("=", 1)[1].strip().lower() in VALID_PICK_STRATEGIES
            )
        ):
            strategy = tok.split("=", 1)[1].strip().lower()
            rem_args = rem_args[1:]
            continue
        if not tok.startswith("-"):
            if not has_colon_acct and not flag_acct_str and not pos_acct:
                pos_acct = rem_args.pop(0).strip()
                continue
            if has_dashdash:
                extra_pos = rem_args.pop(0).strip()
                if pos_acct and pos_acct.lower() != extra_pos.lower():
                    sys.stderr.write(
                        f"Error: Conflicting account arguments '{pos_acct}' and '{extra_pos}'\n"
                    )
                    return 2
                pos_acct = extra_pos
                continue
        if has_dashdash:
            sys.stderr.write(f"Error: Unexpected option '{tok}' before '--'\n")
            return 2
        break

    if rem_args and rem_args[0] == "--":
        rem_args = rem_args[1:]

    if strategy not in VALID_PICK_STRATEGIES:
        sys.stderr.write(
            f"Error: Unknown pick strategy '{strategy}'. Choose from: {', '.join(VALID_PICK_STRATEGIES)}\n"
        )
        return 2

    if pos_acct and flag_acct_str and pos_acct.strip().lower() != flag_acct_str.lower():
        sys.stderr.write(
            f"Error: Conflicting account arguments '{pos_acct}' and '--account {flag_acct_str}'\n"
        )
        return 2
    combined_acct = flag_acct_str if flag_acct_str else pos_acct

    explicit_default = False
    if has_colon_acct and raw_target.split(":", 1)[1].strip().lower() == "default":
        explicit_default = True
    if combined_acct and combined_acct.strip().lower() == "default":
        explicit_default = True

    try:
        pid, acct = _resolve_target_provider_and_account(
            raw_target,
            combined_acct,
            allowed_providers=("claude", "codex", "grok", "cursor"),
            require_provider=True,
            require_account=not explicit_default,
            allow_auto=True,
        )
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    assert pid is not None

    if acct == "auto":
        best, reason = _resolve_auto_account(cfg_mgr, pid, strategy=strategy)
        if best is None:
            sys.stderr.write(f"Error: Could not auto-select account for '{pid}': {reason}\n")
            return 1
        acct = best.account
        chosen_lbl = f"{pid}:{acct}" if acct else f"{pid}:default"
        sys.stderr.write(
            Style.wrap(f"⚡ Auto-selected {chosen_lbl}", Style.CYAN, Style.BOLD)
            + f" ({reason})\n"
        )

    if acct:
        cfg_mgr.ensure_account(pid, acct, create_profile_dir=True)
    _, child_env = _build_profile_child_env(cfg_mgr, pid, acct)
    binary = _resolve_cli_binary(pid)

    cmd = [binary, *rem_args]
    try:
        res = subprocess.run(cmd, env=child_env, check=False)
        return res.returncode
    except FileNotFoundError:
        sys.stderr.write(f"Error: CLI binary not found for '{pid}': {binary}\n")
        return 1
    except KeyboardInterrupt:
        return 130


def cmd_account(args: argparse.Namespace, cfg_mgr: ConfigManager) -> int:
    import re
    from tokenmaster.config import _is_managed_profile_subdir
    from tokenmaster.providers.claude import claude_keychain_service_name_for_dir

    action = getattr(args, "account_action", None) or "list"

    if action == "list":
        filter_prov = getattr(args, "target_provider", None)
        target_pids: list[str] = []
        if filter_prov:
            try:
                pid, _, _ = parse_provider_spec(filter_prov)
            except ValueError as exc:
                sys.stderr.write(f"Error: {exc}\n")
                return 2
            if pid not in PROVIDERS:
                sys.stderr.write(f"Error: Unknown provider '{pid}'. Valid: {', '.join(PROVIDERS)}\n")
                return 2
            target_pids = [pid]
        else:
            target_pids = list(PROVIDERS)

        print(Style.wrap("🗂  TokenMaster Subscription Profiles", Style.CYAN, Style.BOLD))
        print(f"Config File : {cfg_mgr.path}\n")
        for pid in target_pids:
            accts = cfg_mgr.list_accounts(pid, include_disabled=True, ignore_provider_enabled=True)
            for acct, acfg in accts:
                inst_id = f"{pid}:{acct}" if acct else f"{pid} (default)"
                pdir, is_ready, detail = _inspect_account_status(cfg_mgr, pid, acct, acfg)
                enabled = acfg.get("enabled", True) and cfg_mgr.data.get("providers", {}).get(pid, {}).get("enabled", True)
                if not enabled:
                    badge = Style.wrap(f"○ DISABLED ({detail})", Style.GRAY)
                elif is_ready:
                    badge = Style.wrap(f"✔ READY ({detail})", Style.GREEN, Style.BOLD)
                else:
                    badge = Style.wrap(f"○ UNCONFIGURED ({detail})", Style.YELLOW)
                print(f"  {inst_id:<22} {str(pdir):<46} {badge}")
        return 0

    if action == "add":
        try:
            combined_acct = _combine_account_args(args)
            pid, acct = _resolve_target_provider_and_account(
                getattr(args, "target_provider", None),
                combined_acct,
                require_provider=True,
                require_account=True,
            )
        except ValueError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 2
        assert pid is not None and acct is not None

        try:
            if getattr(args, "profile_dir", None):
                cfg_mgr.set_account_field(pid, acct, "profile_dir", args.profile_dir.strip())
            profile_dir = cfg_mgr.ensure_account(pid, acct, create_profile_dir=True)
            if getattr(args, "api_key", None):
                cfg_mgr.set_account_field(pid, acct, "api_key", args.api_key.strip())
            if getattr(args, "management_key", None):
                cfg_mgr.set_account_field(pid, acct, "management_key", args.management_key.strip())
            if getattr(args, "token", None):
                field_name = "oauth_token" if pid == "claude" else "access_token"
                cfg_mgr.set_account_field(pid, acct, field_name, args.token.strip())
            if getattr(args, "cookie", None):
                cfg_mgr.set_account_field(pid, acct, "cookie", args.cookie.strip())
            if getattr(args, "sso", None):
                cfg_mgr.set_account_field(pid, acct, "sso_token", args.sso.strip())
            if getattr(args, "csrf_token", None):
                cfg_mgr.set_account_field(pid, acct, "csrf_token", args.csrf_token.strip())
            if getattr(args, "port", None) is not None:
                cfg_mgr.set_account_field(pid, acct, "port", int(args.port))
        except ValueError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 2

        print(f"✔ Registered subscription '{pid}:{acct}' (sandbox: {profile_dir})")
        if getattr(args, "login", False):
            login_ns = argparse.Namespace(
                target_provider=pid,
                account_pos=acct,
                account=acct,
            )
            return cmd_login(login_ns, cfg_mgr)
        return 0

    if action == "snapshot":
        try:
            combined_acct = _combine_account_args(args)
            pid, acct = _resolve_target_provider_and_account(
                getattr(args, "target_provider", None),
                combined_acct,
                require_provider=True,
                require_account=True,
            )
        except ValueError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 2
        assert pid is not None and acct is not None

        ok, pdir, msg = snapshot_account_credentials(cfg_mgr, pid, acct)
        if not ok:
            sys.stderr.write(f"Error: {msg}\n")
            return 1
        print(f"✔ Snapshotted '{pid}' default credentials into '{pid}:{acct}' ({pdir})\n  {msg}")
        return 0

    if action == "remove":
        try:
            combined_acct = _combine_account_args(args)
            pid, acct = _resolve_target_provider_and_account(
                getattr(args, "target_provider", None),
                combined_acct,
                require_provider=True,
                require_account=True,
            )
        except ValueError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 2
        assert pid is not None and acct is not None
        purge = bool(getattr(args, "purge", False))
        existed, pdir = cfg_mgr.remove_account(pid, acct, purge=purge)
        is_managed_pdir = _is_managed_profile_subdir(pdir, pid, acct)
        if purge:
            # Clean only scoped scan/RPC cache files belonging to this (pid, acct)
            cache_dir = get_cache_dir()
            safe_acct = re.sub(r"[^a-zA-Z0-9_.-]", "_", acct)[:24]
            pid_stems = [
                fname[:-5]
                for k_pid, fname in KNOWN_CACHE_FILES
                if k_pid == pid and fname.endswith(".json")
            ]
            if cache_dir.is_dir() and pid_stems:
                with critical_section():
                    for stem in pid_stems:
                        for child in list(cache_dir.glob(f"{stem}_{safe_acct}_*.json")):
                            try:
                                child.unlink(missing_ok=True)
                            except OSError:
                                pass
            if pid == "claude" and is_managed_pdir:
                _delete_macos_keychain_secret(claude_keychain_service_name_for_dir(pdir))
        if not existed:
            sys.stderr.write(f"Error: Subscription '{pid}:{acct}' was not found.\n")
            return 1
        if purge and is_managed_pdir:
            suffix = f" and purged {pdir}"
        elif purge:
            suffix = f" (preserved external directory {pdir})"
        else:
            suffix = f" (preserved files in {pdir})"
        print(f"✔ Removed subscription '{pid}:{acct}'{suffix}")
        return 0

    if action == "env":
        return cmd_env(args, cfg_mgr)

    if action == "exec":
        return cmd_exec(args, cfg_mgr)

    if action == "pick":
        return cmd_pick(args, cfg_mgr)

    sys.stderr.write(f"Error: Unknown account action '{action}'\n")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tokenmaster",
        description=(
            "TokenMaster: Monitor Weekly Usage & Remaining and Credit Usage & Remaining "
            "for Codex, Claude, Antigravity, Grok, Cursor, and OpenRouter."
        ),
    )
    parser.add_argument(
        "-v", "--version", action="version", version=f"tokenmaster {__version__}"
    )
    parser.add_argument(
        "-p",
        "--provider",
        action="append",
        help="Filter by provider(s), comma-separated (e.g. -p codex,claude,openrouter)",
    )
    parser.add_argument(
        "-d",
        "--detail",
        action="store_true",
        help="Show detailed per-model pools, 5h windows, and local token counts",
    )
    parser.add_argument(
        "-w",
        "--watch",
        nargs="?",
        const=_WATCH_USE_CONFIG_DEFAULT,
        type=int,
        metavar="SECONDS",
        help="Live auto-refresh dashboard every N seconds (default: 30s)",
    )
    parser.add_argument(
        "-j",
        "--json",
        action="store_true",
        help="Output metrics as structured JSON",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color output",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=None,
        metavar="COLS",
        help="Fit dashboard table within COLS terminal columns",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Custom path to config.json",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Trigger deduplicated desktop/terminal alerts when quota crosses thresholds or auth expires",
    )
    parser.add_argument(
        "--threshold",
        "--notify-threshold",
        dest="notify_thresholds",
        action="append",
        default=None,
        metavar="PCT_LIST",
        help="Comma-separated used-%% alert thresholds (default: 80,95,100)",
    )

    subparsers = parser.add_subparsers(dest="subcommand")

    # status subcommand (use SUPPRESS defaults so flags passed before `status` are not overwritten)
    p_status = subparsers.add_parser("status", help="Show usage and credit summary table (default)")
    p_status.add_argument("-p", "--provider", dest="sub_provider", action="append", default=argparse.SUPPRESS, help="Filter by provider(s)")
    p_status.add_argument("-d", "--detail", action="store_true", default=argparse.SUPPRESS, help="Show detailed sub-metrics")
    p_status.add_argument("-w", "--watch", nargs="?", const=_WATCH_USE_CONFIG_DEFAULT, type=int, default=argparse.SUPPRESS, metavar="SECONDS")
    p_status.add_argument("-j", "--json", action="store_true", default=argparse.SUPPRESS, help="Output JSON")
    p_status.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS)
    p_status.add_argument("--width", type=int, default=argparse.SUPPRESS, metavar="COLS")
    p_status.add_argument("--notify", action="store_true", default=argparse.SUPPRESS, help="Enable threshold notifications")
    p_status.add_argument(
        "--threshold",
        "--notify-threshold",
        dest="sub_thresholds",
        action="append",
        default=argparse.SUPPRESS,
        metavar="PCT_LIST",
    )

    # statusline subcommand
    p_sl = subparsers.add_parser(
        "statusline",
        aliases=["sl"],
        help="Ultra-fast single-line status string for Claude Code statusLine, tmux, Starship, or shell prompts",
    )
    p_sl.add_argument(
        "-p",
        "--provider",
        dest="sub_provider",
        action="append",
        default=argparse.SUPPRESS,
        help="Filter by provider(s) or provider:account (e.g. -p claude,codex or -p claude:work)",
    )
    p_sl.add_argument(
        "--format",
        type=str,
        default="compact",
        help="Output format: compact (default), tmux, plain, claude-code, or json",
    )
    p_sl.add_argument(
        "--claude-code",
        action="store_true",
        help="Claude Code statusLine mode (reads session JSON from stdin and fuses with active Claude profile)",
    )
    p_sl.add_argument(
        "--max-age",
        type=float,
        default=180.0,
        metavar="SECONDS",
        help="Max age in seconds for cached status_snapshot.json before refreshing (default: 180)",
    )
    p_sl.add_argument(
        "--refresh",
        action="store_true",
        help="Force live provider query and update status_snapshot.json",
    )
    p_sl.add_argument(
        "--no-refresh",
        action="store_true",
        help="Read only from status_snapshot.json without querying providers",
    )
    p_sl.add_argument(
        "--async-refresh",
        action="store_true",
        help="When snapshot is stale, return cached line immediately and refresh in background",
    )
    p_sl.add_argument(
        "--all",
        action="store_true",
        help="Include unconfigured providers even when no explicit -p filter is given",
    )
    p_sl.add_argument("-j", "--json", action="store_true", default=argparse.SUPPRESS, help="Output JSON")
    p_sl.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help="Disable ANSI color")
    p_sl.add_argument("--width", type=int, default=argparse.SUPPRESS, metavar="COLS", help="Max output width")

    # history subcommand
    p_hist = subparsers.add_parser(
        "history",
        aliases=["hist"],
        help="Query SQLite local usage warehouse (daily, weekly, monthly, projects, models, snapshots) & Cache Hit %%",
    )
    p_hist.add_argument(
        "history_mode",
        nargs="?",
        default="daily",
        help="Aggregation mode: daily (default), weekly, monthly, projects, models, or snapshots",
    )
    p_hist.add_argument(
        "-p",
        "--provider",
        dest="sub_provider",
        action="append",
        default=argparse.SUPPRESS,
        help="Filter by provider(s) or provider:account (e.g. -p claude,codex or -p claude:work)",
    )
    p_hist.add_argument(
        "--days",
        type=int,
        default=None,
        help="Look back N days in UTC (default: 30 when --since is omitted; 0 = all time)",
    )
    p_hist.add_argument("--since", type=str, default=None, metavar="YYYY-MM-DD", help="Start date in UTC (inclusive)")
    p_hist.add_argument("--until", type=str, default=None, metavar="YYYY-MM-DD", help="End date in UTC (inclusive)")
    p_hist.add_argument("--limit", type=int, default=100, help="Maximum rows to return (default: 100)")
    p_hist.add_argument(
        "--sync",
        action="store_true",
        help="Scan all local CLI session files on disk into warehouse.db before querying",
    )
    p_hist.add_argument(
        "--no-sync",
        action="store_true",
        help="Query warehouse.db directly without triggering an initial local session scan",
    )
    p_hist.add_argument(
        "--export",
        dest="export_fmt",
        type=str,
        default=None,
        help="Export format: csv or json",
    )
    p_hist.add_argument("--csv", action="store_true", help="Output RFC 4180 CSV")
    p_hist.add_argument("-o", "--output", type=str, default=None, metavar="FILE", help="Write output to file path")
    p_hist.add_argument("-j", "--json", action="store_true", default=argparse.SUPPRESS, help="Output JSON")
    p_hist.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help="Disable ANSI color")
    p_hist.add_argument("--width", type=int, default=argparse.SUPPRESS, metavar="COLS", help="Max table width")

    # watch subcommand
    p_watch = subparsers.add_parser("watch", help="Live auto-refreshing usage dashboard")
    p_watch.add_argument("-i", "--interval", type=int, default=argparse.SUPPRESS, help="Refresh interval in seconds")
    p_watch.add_argument("-p", "--provider", dest="sub_provider", action="append", default=argparse.SUPPRESS, help="Filter by provider(s)")
    p_watch.add_argument("-d", "--detail", action="store_true", default=argparse.SUPPRESS, help="Show detailed sub-metrics")
    p_watch.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS)
    p_watch.add_argument("--width", type=int, default=argparse.SUPPRESS, metavar="COLS")
    p_watch.add_argument("--notify", action="store_true", default=argparse.SUPPRESS, help="Enable threshold notifications")
    p_watch.add_argument(
        "--threshold",
        "--notify-threshold",
        dest="sub_thresholds",
        action="append",
        default=argparse.SUPPRESS,
        metavar="PCT_LIST",
    )

    # notify subcommand
    p_notify = subparsers.add_parser(
        "notify",
        aliases=["alerts"],
        help="Check quota thresholds & auth expiry, send deduplicated local notifications, and keep OAuth tokens alive",
    )
    p_notify.add_argument(
        "-p",
        "--provider",
        dest="sub_provider",
        action="append",
        default=argparse.SUPPRESS,
        help="Filter by provider(s) or provider:account (e.g. -p claude,codex or -p claude:work)",
    )
    p_notify.add_argument(
        "-t",
        "--threshold",
        "--notify-threshold",
        dest="sub_thresholds",
        action="append",
        default=argparse.SUPPRESS,
        metavar="PCT_LIST",
        help="Comma-separated used-%% alert thresholds (default: 80,95,100)",
    )
    p_notify.add_argument(
        "--auth-cooldown",
        type=float,
        default=DEFAULT_AUTH_COOLDOWN_SECONDS,
        metavar="SECONDS",
        help="Cooldown in seconds before repeating an unresolved auth/error alert (default: 21600)",
    )
    p_notify.add_argument(
        "--no-desktop",
        action="store_true",
        help="Do not invoke OS desktop notification binary (osascript / notify-send); print to stdout only",
    )
    p_notify.add_argument(
        "--bell",
        action="store_true",
        help="Emit terminal bell (\\a) when an alert triggers",
    )
    p_notify.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate alerts without sending OS notifications or updating alert_state.json",
    )
    p_notify.add_argument(
        "--reset-state",
        action="store_true",
        help="Clear alert_state.json deduplication state before evaluating",
    )
    p_notify.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Print nothing when no new alerts trigger (ideal for cron / launchd)",
    )
    p_notify.add_argument("-j", "--json", action="store_true", default=argparse.SUPPRESS, help="Output JSON")
    p_notify.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help="Disable ANSI color")

    # auth subcommand
    p_auth = subparsers.add_parser("auth", help="Configure API keys, OAuth tokens, or cookies")
    p_auth.add_argument(
        "target_provider",
        nargs="?",
        help="Provider (or provider:account) to configure",
    )
    p_auth.add_argument("-a", "--account", type=str, default=None, help="Named subscription account (e.g. work)")
    p_auth.add_argument("--api-key", type=str, help="Set API key")
    p_auth.add_argument("--management-key", type=str, help="Set Management key (OpenRouter)")
    p_auth.add_argument("--token", type=str, help="Set OAuth or Access Token")
    p_auth.add_argument("--cookie", type=str, help="Set session Cookie header (Grok / Cursor)")
    p_auth.add_argument("--sso", type=str, help="Set Grok `sso` cookie value")
    p_auth.add_argument("--csrf-token", type=str, help="Set Antigravity CSRF token")
    p_auth.add_argument("--port", type=int, help="Set Antigravity localhost RPC port")

    # quota subcommand
    p_quota = subparsers.add_parser(
        "quota", help="Set custom weekly limits, credit budgets, or manual usage overrides"
    )
    p_quota.add_argument(
        "target_provider",
        help="Provider (or provider:account) to configure",
    )
    p_quota.add_argument("-a", "--account", type=str, default=None, help="Named subscription account (e.g. work)")
    p_quota.add_argument("--weekly-limit", type=float, help="Set weekly limit")
    p_quota.add_argument("--weekly-used", type=float, help="Set manual weekly used amount")
    p_quota.add_argument("--weekly-unit", type=str, help="Weekly unit (%%, USD, queries, reqs, tokens, steps)")
    p_quota.add_argument("--credit-limit", type=float, help="Set total credit pool / budget limit")
    p_quota.add_argument("--credit-used", type=float, help="Set manual credit used amount")
    p_quota.add_argument("--credit-remaining", type=float, help="Set manual credit remaining amount")
    p_quota.add_argument("--credit-unit", type=str, help="Credit unit (USD, credits)")
    p_quota.add_argument("--reset", action="store_true", help="Clear custom quota overrides")

    # config subcommand
    p_cfg = subparsers.add_parser("config", help="View, initialize, or clear cache for TokenMaster")
    p_cfg.add_argument(
        "config_action",
        nargs="?",
        choices=("show", "path", "init", "clear-cache"),
        default="show",
    )
    p_cfg.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear local scan and RPC discovery caches",
    )

    # doctor subcommand
    subparsers.add_parser("doctor", help="Inspect local credential files and environment status")

    # login subcommand
    p_login = subparsers.add_parser(
        "login",
        help="Authenticate provider (or isolated named account) via its native CLI (grok, cursor, codex, claude)",
    )
    p_login.add_argument(
        "target_provider",
        help="Provider CLI to log in (e.g. claude, codex, grok, cursor, or claude:work)",
    )
    p_login.add_argument(
        "account_pos",
        nargs="?",
        default=None,
        help="Optional subscription account name to isolate in its own sandbox (e.g. work, personal)",
    )
    p_login.add_argument(
        "-a",
        "--account",
        type=str,
        default=None,
        help="Named subscription account to isolate in its own sandbox",
    )

    # pick subcommand
    p_pick = subparsers.add_parser(
        "pick",
        help="Automatically pick the optimal subscription account for a provider based on live quota & reset horizon",
    )
    p_pick.add_argument("target_provider", help="Provider to select an account for (e.g. claude, codex)")
    p_pick.add_argument(
        "-s",
        "--strategy",
        type=str,
        default="smart",
        help="Selection strategy: smart (default), most-remaining, or expiring-first",
    )
    p_pick.add_argument("-j", "--json", action="store_true", default=argparse.SUPPRESS, help="Output selection as JSON")
    p_pick.add_argument("-q", "--quiet", action="store_true", help="Print only the selected account name")

    # env subcommand
    p_env = subparsers.add_parser(
        "env",
        help="Print shell `export` statements to activate a subscription's isolated CLI environment",
    )
    p_env.add_argument("target_provider", help="Provider or provider:account (e.g. claude:work or claude:auto)")
    p_env.add_argument("account_pos", nargs="?", default=None, help="Subscription account name (or 'auto')")
    p_env.add_argument("-a", "--account", type=str, default=None, help="Subscription account name (or 'auto')")
    p_env.add_argument(
        "-s",
        "--strategy",
        type=str,
        default="smart",
        help="Strategy when account is 'auto': smart (default), most-remaining, or expiring-first",
    )

    # exec subcommand
    p_exec = subparsers.add_parser(
        "exec",
        help="Run a provider's native CLI (claude, codex, grok, cursor-agent) inside an isolated subscription sandbox",
    )
    p_exec.add_argument("target_provider", help="Provider or provider:account (e.g. claude, claude:work, or claude:auto)")
    p_exec.add_argument("-a", "--account", type=str, default=None, help="Subscription account name (or 'auto')")
    p_exec.add_argument(
        "-s",
        "--strategy",
        type=str,
        default="smart",
        help="Strategy when account is 'auto': smart (default), most-remaining, or expiring-first",
    )
    p_exec.add_argument("cli_args", nargs=argparse.REMAINDER, help="Account name (if not in provider:account) followed by CLI arguments")

    # account subcommand
    p_acct = subparsers.add_parser(
        "account",
        aliases=["accounts", "profile", "profiles"],
        help="Manage multiple isolated subscriptions per provider (list, add, snapshot, remove, pick, env, exec)",
    )
    acct_sub = p_acct.add_subparsers(dest="account_action")

    p_acct_list = acct_sub.add_parser("list", help="List all tracked subscriptions and sandbox paths")
    p_acct_list.add_argument("target_provider", nargs="?", default=None, help="Optional provider filter")

    p_acct_add = acct_sub.add_parser("add", help="Add or update a named subscription sandbox")
    p_acct_add.add_argument("target_provider", help="Provider or provider:account (e.g. claude or claude:work)")
    p_acct_add.add_argument("account_pos", nargs="?", default=None, help="Subscription account name")
    p_acct_add.add_argument("-a", "--account", type=str, default=None, help="Subscription account name")
    p_acct_add.add_argument("--profile-dir", type=str, default=None, help="Custom sandbox directory path")
    p_acct_add.add_argument("--api-key", type=str, help="Set API key")
    p_acct_add.add_argument("--management-key", type=str, help="Set Management key (OpenRouter)")
    p_acct_add.add_argument("--token", type=str, help="Set OAuth or Access Token")
    p_acct_add.add_argument("--cookie", type=str, help="Set session Cookie")
    p_acct_add.add_argument("--sso", type=str, help="Set Grok sso token")
    p_acct_add.add_argument("--csrf-token", type=str, help="Set Antigravity CSRF token")
    p_acct_add.add_argument("--port", type=int, help="Set Antigravity RPC port")
    p_acct_add.add_argument("--login", action="store_true", help="Immediately launch native CLI login in this sandbox")

    p_acct_snap = acct_sub.add_parser(
        "snapshot",
        help="Snapshot current default credentials into an isolated named subscription profile",
    )
    p_acct_snap.add_argument("target_provider", help="Provider or provider:account (e.g. claude or claude:personal)")
    p_acct_snap.add_argument("account_pos", nargs="?", default=None, help="Target subscription account name")
    p_acct_snap.add_argument("-a", "--account", type=str, default=None, help="Target subscription account name")

    p_acct_rm = acct_sub.add_parser("remove", help="Stop tracking a named subscription (and optionally purge its sandbox)")
    p_acct_rm.add_argument("target_provider", help="Provider or provider:account (e.g. claude:work)")
    p_acct_rm.add_argument("account_pos", nargs="?", default=None, help="Subscription account name")
    p_acct_rm.add_argument("-a", "--account", type=str, default=None, help="Subscription account name")
    p_acct_rm.add_argument("--purge", action="store_true", help="Also delete the managed sandbox directory on disk")

    p_acct_pick = acct_sub.add_parser("pick", help="Automatically pick the optimal subscription account for a provider")
    p_acct_pick.add_argument("target_provider", help="Provider to select an account for (e.g. claude, codex)")
    p_acct_pick.add_argument(
        "-s",
        "--strategy",
        type=str,
        default="smart",
        help="Selection strategy: smart (default), most-remaining, or expiring-first",
    )
    p_acct_pick.add_argument("-j", "--json", action="store_true", default=argparse.SUPPRESS, help="Output selection as JSON")
    p_acct_pick.add_argument("-q", "--quiet", action="store_true", help="Print only the selected account name")

    p_acct_env = acct_sub.add_parser("env", help="Print shell export commands for a subscription sandbox")
    p_acct_env.add_argument("target_provider", help="Provider or provider:account")
    p_acct_env.add_argument("account_pos", nargs="?", default=None, help="Subscription account name (or 'auto')")
    p_acct_env.add_argument("-a", "--account", type=str, default=None, help="Subscription account name (or 'auto')")
    p_acct_env.add_argument(
        "-s",
        "--strategy",
        type=str,
        default="smart",
        help="Strategy when account is 'auto': smart (default), most-remaining, or expiring-first",
    )

    p_acct_exec = acct_sub.add_parser("exec", help="Execute native CLI inside a subscription sandbox")
    p_acct_exec.add_argument("target_provider", help="Provider or provider:account")
    p_acct_exec.add_argument("-a", "--account", type=str, default=None, help="Subscription account name (or 'auto')")
    p_acct_exec.add_argument(
        "-s",
        "--strategy",
        type=str,
        default="smart",
        help="Strategy when account is 'auto': smart (default), most-remaining, or expiring-first",
    )
    p_acct_exec.add_argument("cli_args", nargs=argparse.REMAINDER, help="Account name (if not in provider:account) followed by CLI arguments")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cfg_path = Path(args.config).expanduser() if getattr(args, "config", None) else None
    cfg_mgr = ConfigManager(config_path=cfg_path)

    sub = getattr(args, "subcommand", None)
    if sub in (None, "status"):
        return cmd_status(args, cfg_mgr)
    if sub in ("statusline", "sl"):
        return cmd_statusline(args, cfg_mgr)
    if sub in ("history", "hist"):
        return cmd_history(args, cfg_mgr)
    if sub in ("notify", "alerts"):
        return cmd_notify(args, cfg_mgr)
    if sub == "watch":
        if hasattr(args, "interval"):
            args.watch = args.interval
        elif getattr(args, "watch", None) is None:
            args.watch = _WATCH_USE_CONFIG_DEFAULT
        args.json = False
        return cmd_status(args, cfg_mgr)
    if sub == "auth":
        return cmd_auth(args, cfg_mgr)
    if sub == "quota":
        return cmd_quota(args, cfg_mgr)
    if sub == "config":
        return cmd_config(args, cfg_mgr)
    if sub == "doctor":
        return cmd_doctor(cfg_mgr)
    if sub == "login":
        return cmd_login(args, cfg_mgr)
    if sub == "pick":
        return cmd_pick(args, cfg_mgr)
    if sub == "env":
        return cmd_env(args, cfg_mgr)
    if sub == "exec":
        return cmd_exec(args, cfg_mgr)
    if sub in ("account", "accounts", "profile", "profiles"):
        return cmd_account(args, cfg_mgr)

    return cmd_status(args, cfg_mgr)


if __name__ == "__main__":
    sys.exit(main())
