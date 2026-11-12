"""Google Antigravity provider (Local Language Server Connect-RPC + ~/.gemini/antigravity logs)."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from tokenmaster.cache import FileScanCache
from tokenmaster.config import get_profiles_dir
from tokenmaster.models import (
    CreditBalance,
    ProviderReport,
    SubMetric,
    UsageWindow,
    format_reset_countdown,
    parse_reset_remaining_seconds,
)
from tokenmaster.providers.antigravity_cli import AntigravityCliClient
from tokenmaster.providers.base import (
    BaseProvider,
    HttpError,
    atomic_write_text,
    critical_section,
    file_lock,
    run_concurrently,
)

ANTIGRAVITY_POOLS_CACHE_VERSION = 1


def _safe_float(val: Any) -> Optional[float]:
    """Convert scalar value to float safely, returning None for lists/dicts/invalid."""
    if val is None or isinstance(val, (list, dict, bool)):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


class AntigravityProvider(BaseProvider):
    """Monitors Google Antigravity weekly model quotas and AI credit balance."""

    provider_id = "antigravity"
    display_name = "Antigravity (Google)"

    def _antigravity_dir(self) -> Path:
        isolated = self._resolve_isolated_dir("antigravity_dir")
        if isolated is not None:
            return isolated
        return self._resolve_default_env_dir("ANTIGRAVITY_DIR", Path.home() / ".gemini" / "antigravity")

    def _discover_language_server(self) -> list[tuple[int, str]]:
        """Discover (port, csrf_token) pairs from running Antigravity language_server processes."""
        candidates: list[tuple[int, str]] = []

        cfg_port = self.cfg.get("port")
        cfg_token = self.cfg.get("csrf_token")
        if cfg_port and cfg_token:
            candidates.append((int(cfg_port), str(cfg_token)))
            if self._is_isolated_profile():
                return candidates

        # For an isolated profile without explicit port+csrf_token, strictly require
        # the full resolved profile directory path on the command line with path boundaries
        # (so .../profiles/antigravity/work never matches .../profiles/antigravity/work-2).
        # Conversely, the default account skips any process belonging to get_profiles_dir().
        isolated = self._is_isolated_profile()
        profile_dir_regex: Optional[re.Pattern[str]] = None
        profiles_root_prefix = str(get_profiles_dir())
        if isolated:
            target_dir = os.path.abspath(str(self._antigravity_dir()))
            profile_dir_regex = re.compile(
                rf"(?<![a-zA-Z0-9._/-]){re.escape(target_dir)}(?![a-zA-Z0-9._-])"
            )

        try:
            proc = subprocess.run(
                ["ps", "-ax", "-o", "pid=,command="],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if proc.returncode != 0:
                return candidates

            for line in proc.stdout.splitlines():
                if "language_server" not in line and "antigravity" not in line.lower():
                    continue
                if "--csrf_token" not in line:
                    continue
                if profile_dir_regex is not None:
                    if not profile_dir_regex.search(line):
                        continue
                elif profiles_root_prefix and profiles_root_prefix in line:
                    continue

                m_pid = re.match(r"^\s*(\d+)\s+(.*)$", line)
                if not m_pid:
                    continue
                pid = m_pid.group(1)
                cmd = m_pid.group(2)

                m_csrf = re.search(r"--csrf_token[=\s]+([^\s]+)", cmd)
                if not m_csrf:
                    continue
                csrf_token = m_csrf.group(1).strip("'\"")

                ports: list[int] = []
                try:
                    lsof_proc = subprocess.run(
                        ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", pid],
                        capture_output=True,
                        text=True,
                        timeout=3.0,
                        check=False,
                    )
                    if lsof_proc.returncode == 0:
                        for lline in lsof_proc.stdout.splitlines():
                            m_port = re.search(r":(\d+)\s+\(LISTEN\)", lline)
                            if m_port:
                                p_int = int(m_port.group(1))
                                if p_int not in ports:
                                    ports.append(p_int)
                except Exception:
                    pass

                m_ext_port = re.search(r"--extension_server_port[=\s]+(\d+)", cmd)
                if m_ext_port:
                    ep = int(m_ext_port.group(1))
                    if ep not in ports:
                        ports.append(ep)

                for p in ports:
                    pair = (p, csrf_token)
                    if pair not in candidates:
                        candidates.append(pair)
        except Exception:
            pass

        return candidates

    def _rpc_cache_path(self) -> Path:
        extra = str(self.cfg.get("port") or "")
        return self._scoped_cache_path("antigravity_rpc", extra_scope=extra or None)

    def _load_cached_endpoint(self) -> Optional[tuple[str, int, str]]:
        """Return cached (scheme, port, csrf_token) from last successful RPC, or None."""
        try:
            p = self._rpc_cache_path()
            if not p.is_file():
                return None
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw_scheme = raw.get("scheme")
                raw_port = raw.get("port")
                raw_token = raw.get("csrf_token")
                if (
                    isinstance(raw_scheme, str)
                    and raw_scheme in ("https", "http")
                    and isinstance(raw_port, int)
                    and not isinstance(raw_port, bool)
                    and 1 <= raw_port <= 65535
                    and isinstance(raw_token, str)
                    and raw_token
                    and "\r" not in raw_token
                    and "\n" not in raw_token
                ):
                    return raw_scheme, raw_port, raw_token
        except Exception:
            return None
        return None

    def _clear_cached_endpoint(self) -> None:
        try:
            with critical_section():
                self._rpc_cache_path().unlink(missing_ok=True)
        except Exception:
            pass

    def _save_cached_endpoint(self, scheme: str, port: int, csrf_token: str) -> None:
        try:
            p = self._rpc_cache_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"scheme": scheme, "port": port, "csrf_token": csrf_token})
            with critical_section():
                atomic_write_text(p, payload, mode=0o600)
        except Exception:
            pass

    def _pools_cache_path(self) -> Path:
        extra = str(self.cfg.get("port") or "")
        return self._scoped_cache_path("antigravity_pools", extra_scope=extra or None)

    @staticmethod
    def _validate_pool_window_entry(entry: Any) -> Optional[dict[str, Any]]:
        if not isinstance(entry, dict):
            return None
        raw_rem = _safe_float(entry.get("rem_pct"))
        if raw_rem is None or not math.isfinite(raw_rem):
            return None
        rem_pct = max(0.0, min(100.0, raw_rem))
        reset_at = entry.get("reset_at")
        if reset_at is not None and not isinstance(reset_at, str):
            reset_at = None
        upd = _safe_float(entry.get("updated_at"))
        return {
            "rem_pct": rem_pct,
            "reset_at": reset_at.strip() if isinstance(reset_at, str) and reset_at.strip() else None,
            "updated_at": upd if (upd is not None and math.isfinite(upd)) else 0.0,
        }

    @classmethod
    def _inspect_pools_cache(cls, raw: Any) -> str:
        """Return 'ready', 'stale', or 'invalid' for doctor diagnostics."""
        if (
            not isinstance(raw, dict)
            or isinstance(raw.get("version"), bool)
            or raw.get("version") != ANTIGRAVITY_POOLS_CACHE_VERSION
            or not isinstance(raw.get("families"), dict)
        ):
            return "invalid"
        families = raw["families"]
        has_any_valid = False
        has_unexpired = False
        for fval in families.values():
            if not isinstance(fval, dict) or not fval:
                return "invalid"
            if any(k not in ("weekly", "five_hour") for k in fval):
                return "invalid"
            f_has_valid = False
            for wkey in ("weekly", "five_hour"):
                if wkey in fval and fval[wkey] is not None:
                    v_ent = cls._validate_pool_window_entry(fval[wkey])
                    if v_ent is None:
                        return "invalid"
                    f_has_valid = True
                    has_any_valid = True
                    r_at = v_ent.get("reset_at")
                    rem_s = parse_reset_remaining_seconds(r_at, None) if r_at else None
                    if rem_s is None or rem_s > 0.0:
                        has_unexpired = True
            if not f_has_valid:
                return "invalid"
        if not has_any_valid:
            return "ready" if len(families) == 0 else "invalid"
        return "ready" if has_unexpired else "stale"

    def _load_pools_cache(self) -> dict[str, dict[str, Any]]:
        try:
            p = self._pools_cache_path()
            if not p.is_file():
                return {}
            raw = json.loads(p.read_text(encoding="utf-8"))
            if self._inspect_pools_cache(raw) == "invalid":
                return {}
            out: dict[str, dict[str, Any]] = {}
            for fkey, fval in raw.get("families", {}).items():
                if not isinstance(fkey, str) or not isinstance(fval, dict):
                    continue
                cleaned_f: dict[str, Any] = {}
                for wkey in ("weekly", "five_hour"):
                    v_ent = self._validate_pool_window_entry(fval.get(wkey))
                    if v_ent is not None:
                        cleaned_f[wkey] = v_ent
                if cleaned_f:
                    out[fkey] = cleaned_f
            return out
        except Exception:
            return {}

    def _save_pools_cache(self, families: dict[str, dict[str, Any]]) -> None:
        try:
            p = self._pools_cache_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            lock_path = p.with_suffix(".lock")
            payload = json.dumps(
                {
                    "version": ANTIGRAVITY_POOLS_CACHE_VERSION,
                    "updated_at": time.time(),
                    "families": families,
                },
                indent=2,
            )
            with file_lock(lock_path), critical_section():
                atomic_write_text(p, payload + "\n", mode=0o600)
        except Exception:
            pass

    @staticmethod
    def _classify_model_family(label: str, model_or_alias: Any = None) -> tuple[str, str]:
        """Classify an Antigravity model config into ``(family_key, family_display_name)``."""
        alias_txt = ""
        if isinstance(model_or_alias, dict):
            alias_txt = str(model_or_alias.get("model") or model_or_alias.get("alias") or "")
        elif model_or_alias is not None:
            alias_txt = str(model_or_alias)
        combined = f"{label} {alias_txt}".lower()
        if "gemini" in combined:
            return "gemini", "Gemini"
        if any(
            kw in combined
            for kw in ("claude", "gpt", "anthropic", "openai", "sonnet", "opus", "haiku", "o1", "o3", "o4")
        ):
            return "claude_gpt", "Claude/GPT"
        return "other", "Other Models"

    @staticmethod
    def _parse_quota_dict(quota: dict[str, Any]) -> tuple[float, Optional[str], Optional[str], Optional[float]]:
        """Return ``(rem_pct, reset_at, reset_in, rem_seconds)`` from a quotaInfo dict."""
        rem_frac = _safe_float(
            quota.get("remainingFraction")
            if quota.get("remainingFraction") is not None
            else quota.get("remaining_fraction")
        )
        raw_reset = (
            quota.get("resetTime")
            if quota.get("resetTime") is not None
            else quota.get("reset_time")
        )
        if rem_frac is None:
            rem_frac = 0.0 if raw_reset else 1.0
        rem_pct = max(0.0, min(100.0, rem_frac * 100.0))
        r_at, r_in = format_reset_countdown(raw_reset)
        rem_sec = parse_reset_remaining_seconds(r_at, r_in)
        return rem_pct, r_at, r_in, rem_sec

    def _probe_endpoint(
        self, scheme: str, port: int, csrf_token: str, payload: dict[str, Any], timeout: float = 2.0
    ) -> Optional[dict[str, Any]]:
        headers = {
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "X-Codeium-Csrf-Token": csrf_token,
        }
        url = f"{scheme}://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetUserStatus"
        try:
            res = self.http_json(
                url,
                method="POST",
                headers=headers,
                payload=payload,
                verify_ssl=(scheme != "https"),
                timeout=timeout,
            )
            if isinstance(res, dict) and ("userStatus" in res or "cascadeModelConfigData" in res):
                return res
        except Exception:
            return None
        return None

    def _query_language_server(self) -> Optional[dict[str, Any]]:
        """Query local Connect-RPC GetUserStatus endpoint across discovered ports."""
        payload = {
            "metadata": {
                "ideName": "antigravity",
                "extensionName": "antigravity",
                "locale": "en",
            }
        }
        # Fast path: try the last working (scheme, port, csrf_token) before spawning ps + lsof.
        if not (self.cfg.get("port") and self.cfg.get("csrf_token")):
            cached = self._load_cached_endpoint()
            if cached is not None:
                c_scheme, c_port, c_tok = cached
                res = self._probe_endpoint(c_scheme, c_port, c_tok, payload, timeout=1.0)
                if res is not None:
                    return res
                self._clear_cached_endpoint()

        candidates = self._discover_language_server()
        for port, csrf_token in candidates:
            for scheme in ("https", "http"):
                res = self._probe_endpoint(scheme, port, csrf_token, payload, timeout=2.5)
                if res is not None:
                    self._save_cached_endpoint(scheme, port, csrf_token)
                    return res
        return None

    def _brain_scan_cache(self) -> Optional[FileScanCache]:
        try:
            return FileScanCache(self._scoped_cache_path("antigravity_brain_scan"))
        except Exception:
            return None

    def _query_cli_status(self) -> Optional[dict[str, Any]]:
        # An explicit endpoint or profile must never fall back to another
        # subscription in the default system keyring.
        if self._is_isolated_profile() or self.cfg.get("port") or self.cfg.get("csrf_token"):
            return None
        default_dir = Path.home() / ".gemini" / "antigravity"
        if self._antigravity_dir().resolve() != default_dir.resolve():
            return None
        return AntigravityCliClient(self.http_json, self.timeout).query()

    def _scan_local_brain(self) -> dict[str, Any]:
        """Scan ~/.gemini/antigravity/brain for 7d/30d local agent steps and conversations."""
        agy_root = self._antigravity_dir()
        brain_dir = agy_root / "brain"

        stats: dict[str, Any] = {
            "brain_accessible": False,
            "conversations_7d": 0,
            "steps_7d": 0,
            "steps_30d": 0,
        }
        if not brain_dir.exists() or not os.access(brain_dir, os.R_OK):
            return stats

        stats["brain_accessible"] = True
        now = time.time()
        cutoff_7d = now - 7 * 86400
        cutoff_30d = now - 30 * 86400

        try:
            conv_dirs = [d for d in brain_dir.iterdir() if d.is_dir()]
        except Exception:
            return stats

        cache = self._brain_scan_cache()
        keep: set[str] = set()

        for cdir in conv_dirs:
            tfile = cdir / ".system_generated" / "logs" / "transcript.jsonl"
            try:
                st = tfile.stat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or not os.access(tfile, os.R_OK):
                continue
            mtime = st.st_mtime
            if mtime < cutoff_30d:
                continue

            keep.add(str(tfile))
            if mtime >= cutoff_7d:
                stats["conversations_7d"] += 1

            summary = cache.get(tfile, st.st_mtime_ns, st.st_size) if cache is not None else None
            if summary is None:
                step_count = 0
                try:
                    with tfile.open("r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            if '"PLANNER_RESPONSE"' in line:
                                step_count += 1
                except Exception:
                    continue
                summary = {"steps": step_count}
                if cache is not None:
                    cache.put(tfile, st.st_mtime_ns, st.st_size, summary)

            steps = int(summary.get("steps") or 0)
            stats["steps_30d"] += steps
            if mtime >= cutoff_7d:
                stats["steps_7d"] += steps

        if cache is not None:
            cache.prune(keep, root=brain_dir)
            with critical_section():
                cache.save()

        return stats

    def fetch(self) -> ProviderReport:
        report = self.new_report()

        results = run_concurrently(
            {
                "rpc": self._query_language_server,
                "brain": self._scan_local_brain,
            }
        )
        rpc_data, _ = results["rpc"]
        brain_stats, _ = results["brain"]
        if not isinstance(brain_stats, dict):
            brain_stats = {"brain_accessible": False, "conversations_7d": 0, "steps_7d": 0, "steps_30d": 0}
        sources: list[str] = []
        quota_source = "Connect-RPC GetUserStatus"
        if not isinstance(rpc_data, dict):
            try:
                rpc_data = self._query_cli_status()
                quota_source = "Antigravity CLI Google quota API"
            except (HttpError, RuntimeError) as exc:
                report.status = "partial" if brain_stats["brain_accessible"] else "error"
                detail = f" (HTTP {exc.status})" if isinstance(exc, HttpError) else ""
                report.error_message = f"Antigravity CLI quota query failed{detail}"
                report.auth_hint = "Check the native agy sign-in and access to the unlocked Linux keyring"

        if isinstance(rpc_data, dict):
            sources.append(quota_source)
            report.status = "ok"
            user_status = (
                rpc_data.get("userStatus")
                if isinstance(rpc_data.get("userStatus"), dict)
                else rpc_data
            )

            # Tier / Plan info
            user_tier = user_status.get("userTier") if isinstance(user_status.get("userTier"), dict) else {}
            plan_status = user_status.get("planStatus") if isinstance(user_status.get("planStatus"), dict) else {}
            plan_info = plan_status.get("planInfo") if isinstance(plan_status.get("planInfo"), dict) else {}
            tier_name = (
                user_tier.get("name")
                or plan_info.get("planName")
                or "Antigravity Pro"
            )
            report.plan_tier = str(tier_name)

            # Parse model quotas from cascadeModelConfigData.clientModelConfigs
            cascade = (
                user_status.get("cascadeModelConfigData")
                if isinstance(user_status.get("cascadeModelConfigData"), dict)
                else {}
            )
            configs = cascade.get("clientModelConfigs") if isinstance(cascade.get("clientModelConfigs"), list) else []
            if configs:
                earliest_reset_at: Optional[str] = None
                earliest_reset_in: Optional[str] = None
                earliest_rem_sec: Optional[float] = None
                seen_labels: set[str] = set()

                cached_pools = self._load_pools_cache()
                fam_names: dict[str, str] = {}
                discovered_fams: list[str] = []
                fam_weekly_obs: dict[str, list[tuple[float, Optional[str], Optional[str], Optional[float], str]]] = {}
                fam_five_h_obs: dict[str, list[tuple[float, Optional[str], Optional[str], Optional[float], str]]] = {}

                def _record_earliest(r_at: Optional[str], r_in: Optional[str], r_sec: Optional[float]) -> None:
                    nonlocal earliest_reset_at, earliest_reset_in, earliest_rem_sec
                    if not r_in:
                        return
                    if earliest_reset_in is None:
                        earliest_reset_at = r_at
                        earliest_reset_in = r_in
                        earliest_rem_sec = r_sec
                    elif (
                        r_sec is not None
                        and r_sec > 0.0
                        and (earliest_rem_sec is None or earliest_rem_sec <= 0.0 or r_sec < earliest_rem_sec)
                    ):
                        earliest_reset_at = r_at
                        earliest_reset_in = r_in
                        earliest_rem_sec = r_sec

                for cfg in configs:
                    if not isinstance(cfg, dict):
                        continue
                    quota = cfg.get("quotaInfo")
                    explicit_w = cfg.get("weeklyQuotaInfo") or (
                        quota.get("weeklyQuotaInfo") if isinstance(quota, dict) else None
                    )
                    explicit_5h = (
                        cfg.get("fiveHourQuotaInfo")
                        or cfg.get("hourlyQuotaInfo")
                        or (
                            (quota.get("fiveHourQuotaInfo") or quota.get("hourlyQuotaInfo"))
                            if isinstance(quota, dict)
                            else None
                        )
                    )
                    if not isinstance(quota, dict) and not isinstance(explicit_w, dict) and not isinstance(explicit_5h, dict):
                        continue

                    label = str(cfg.get("label") or cfg.get("modelOrAlias") or "Model Pool")
                    if label in seen_labels:
                        continue
                    seen_labels.add(label)

                    fam_key, fam_name = self._classify_model_family(label, cfg.get("modelOrAlias"))
                    if fam_key not in fam_names:
                        fam_names[fam_key] = fam_name
                        discovered_fams.append(fam_key)
                        fam_weekly_obs[fam_key] = []
                        fam_five_h_obs[fam_key] = []

                    if isinstance(explicit_w, dict) or isinstance(explicit_5h, dict):
                        if isinstance(explicit_w, dict):
                            w_rem, w_at, w_in, w_sec = self._parse_quota_dict(explicit_w)
                            fam_weekly_obs[fam_key].append((w_rem, w_at, w_in, w_sec, label))
                            _record_earliest(w_at, w_in, w_sec)
                        if isinstance(explicit_5h, dict):
                            f_rem, f_at, f_in, f_sec = self._parse_quota_dict(explicit_5h)
                            fam_five_h_obs[fam_key].append((f_rem, f_at, f_in, f_sec, label))
                            _record_earliest(f_at, f_in, f_sec)
                        continue

                    assert isinstance(quota, dict)
                    rem_pct, r_at, r_in, rem_sec = self._parse_quota_dict(quota)
                    _record_earliest(r_at, r_in, rem_sec)

                    cw_entry = (cached_pools.get(fam_key) or {}).get("weekly")
                    cw_reset_at = cw_entry.get("reset_at") if isinstance(cw_entry, dict) else None
                    cw_sec = parse_reset_remaining_seconds(cw_reset_at, None) if cw_reset_at else None
                    matches_cached_weekly = bool(
                        cw_sec is not None
                        and cw_sec > 0.0
                        and rem_sec is not None
                        and abs(rem_sec - cw_sec) <= 120.0
                    )

                    if rem_sec is not None and 0.0 < rem_sec <= 6 * 3600 and not matches_cached_weekly:
                        fam_five_h_obs[fam_key].append((rem_pct, r_at, r_in, rem_sec, label))
                    else:
                        fam_weekly_obs[fam_key].append((rem_pct, r_at, r_in, rem_sec, label))

                ordered_fams = [k for k in ("gemini", "claude_gpt") if k in fam_names] + [
                    k for k in discovered_fams if k not in ("gemini", "claude_gpt")
                ]

                now_ts = time.time()
                updated_pools: dict[str, dict[str, Any]] = dict(cached_pools)
                family_used_pcts: list[float] = []
                divergent_model_metrics: list[SubMetric] = []

                def _pick_earliest_obs(
                    obs_list: list[tuple[float, Optional[str], Optional[str], Optional[float], str]],
                ) -> tuple[Optional[str], Optional[str]]:
                    best_at: Optional[str] = None
                    best_in: Optional[str] = None
                    best_sec: Optional[float] = None
                    for _, o_at, o_in, o_sec, _ in obs_list:
                        if not o_in:
                            continue
                        if best_in is None or (
                            o_sec is not None
                            and o_sec > 0.0
                            and (best_sec is None or best_sec <= 0.0 or o_sec < best_sec)
                        ):
                            best_at = o_at
                            best_in = o_in
                            best_sec = o_sec
                    return best_at, best_in

                for fam_key in ordered_fams:
                    fam_name = fam_names[fam_key]
                    w_obs = fam_weekly_obs.get(fam_key, [])
                    f5_obs = fam_five_h_obs.get(fam_key, [])
                    prev_fam = dict(updated_pools.get(fam_key) or {})

                    cw = self._validate_pool_window_entry(prev_fam.get("weekly"))
                    cw_sec = (
                        parse_reset_remaining_seconds(cw.get("reset_at"), None)
                        if (cw and cw.get("reset_at"))
                        else None
                    )
                    valid_cw = cw is not None and cw_sec is not None and cw_sec > 0.0

                    c5 = self._validate_pool_window_entry(prev_fam.get("five_hour"))
                    c5_sec = (
                        parse_reset_remaining_seconds(c5.get("reset_at"), None)
                        if (c5 and c5.get("reset_at"))
                        else None
                    )
                    valid_c5 = c5 is not None and c5_sec is not None and 0.0 < c5_sec <= 6 * 3600

                    if w_obs and f5_obs:
                        w_rem_pct = sum(x[0] for x in w_obs) / len(w_obs)
                        w_r_at, w_r_in = _pick_earliest_obs(w_obs)
                        f5_rem_pct = sum(x[0] for x in f5_obs) / len(f5_obs)
                        f5_r_at, f5_r_in = _pick_earliest_obs(f5_obs)
                        prev_fam["weekly"] = {
                            "rem_pct": round(w_rem_pct, 2),
                            "reset_at": w_r_at,
                            "updated_at": now_ts,
                        }
                        prev_fam["five_hour"] = {
                            "rem_pct": round(f5_rem_pct, 2),
                            "reset_at": f5_r_at,
                            "updated_at": now_ts,
                        }
                        eff_fam_rem = min(w_rem_pct, f5_rem_pct)
                    elif w_obs:
                        w_rem_pct = sum(x[0] for x in w_obs) / len(w_obs)
                        w_r_at, w_r_in = _pick_earliest_obs(w_obs)
                        prev_fam["weekly"] = {
                            "rem_pct": round(w_rem_pct, 2),
                            "reset_at": w_r_at,
                            "updated_at": now_ts,
                        }
                        if w_rem_pct <= 0.0:
                            f5_rem_pct = 0.0
                            f5_r_at = w_r_at
                            f5_r_in = w_r_in
                            prev_fam.pop("five_hour", None)
                        elif valid_c5 and c5 is not None and float(c5["rem_pct"]) >= w_rem_pct:
                            f5_rem_pct = float(c5["rem_pct"])
                            f5_r_at = c5.get("reset_at")
                            _, f5_r_in = format_reset_countdown(f5_r_at)
                        else:
                            # GetUserStatus returns the active bottleneck window; since it returned
                            # the 7d weekly window (>5h) with >0% remaining, the 5h window is clear.
                            f5_rem_pct = 100.0
                            f5_r_at = None
                            f5_r_in = None
                            prev_fam.pop("five_hour", None)
                        eff_fam_rem = w_rem_pct
                    else:
                        f5_rem_pct = sum(x[0] for x in f5_obs) / len(f5_obs)
                        f5_r_at, f5_r_in = _pick_earliest_obs(f5_obs)
                        prev_fam["five_hour"] = {
                            "rem_pct": round(f5_rem_pct, 2),
                            "reset_at": f5_r_at,
                            "updated_at": now_ts,
                        }
                        if valid_cw and cw is not None:
                            w_rem_pct = max(f5_rem_pct, float(cw["rem_pct"]))
                            w_r_at = cw.get("reset_at")
                            _, w_r_in = format_reset_countdown(w_r_at)
                        else:
                            w_rem_pct = f5_rem_pct
                            w_r_at = f5_r_at
                            w_r_in = f5_r_in
                        eff_fam_rem = f5_rem_pct

                    updated_pools[fam_key] = prev_fam
                    w_used_pct = max(0.0, 100.0 - w_rem_pct)
                    f5_used_pct = max(0.0, 100.0 - f5_rem_pct)
                    family_used_pcts.append(max(0.0, 100.0 - eff_fam_rem))

                    report.sub_metrics.append(
                        SubMetric(
                            name=f"{fam_name} Weekly",
                            used_str=f"{w_used_pct:.1f}%",
                            remaining_str=f"{w_rem_pct:.1f}%",
                            used_percent=w_used_pct,
                            resets_in=w_r_in,
                        )
                    )
                    report.sub_metrics.append(
                        SubMetric(
                            name=f"{fam_name} 5h",
                            used_str=f"{f5_used_pct:.1f}%",
                            remaining_str=f"{f5_rem_pct:.1f}%",
                            used_percent=f5_used_pct,
                            resets_in=f5_r_in,
                        )
                    )

                    all_fam_obs = [*w_obs, *f5_obs]
                    if len(all_fam_obs) > 1:
                        obs_rems = [x[0] for x in all_fam_obs]
                        if max(obs_rems) - min(obs_rems) > 0.1:
                            for m_rem, _, m_in, _, m_lbl in all_fam_obs:
                                m_used = max(0.0, 100.0 - m_rem)
                                divergent_model_metrics.append(
                                    SubMetric(
                                        name=m_lbl,
                                        used_str=f"{m_used:.1f}%",
                                        remaining_str=f"{m_rem:.1f}%",
                                        used_percent=m_used,
                                        resets_in=m_in,
                                    )
                                )

                report.sub_metrics.extend(divergent_model_metrics)
                if updated_pools:
                    self._save_pools_cache(updated_pools)

                if family_used_pcts:
                    avg_used = sum(family_used_pcts) / len(family_used_pcts)
                    ag_rem_sec = parse_reset_remaining_seconds(earliest_reset_at, earliest_reset_in)
                    if ag_rem_sec is not None and ag_rem_sec > 0:
                        if ag_rem_sec <= 6 * 3600:
                            ag_win_sec = 5 * 3600
                        elif ag_rem_sec <= 86400:
                            ag_win_sec = 86400
                        else:
                            ag_win_sec = 7 * 86400
                    else:
                        ag_win_sec = 5 * 3600
                    report.weekly = UsageWindow(
                        used=round(avg_used, 1),
                        remaining=round(max(0.0, 100.0 - avg_used), 1),
                        limit=100.0,
                        unit="%",
                        used_percent=avg_used,
                        remaining_percent=max(0.0, 100.0 - avg_used),
                        resets_at=earliest_reset_at,
                        resets_in=earliest_reset_in,
                        window_seconds=ag_win_sec,
                    )

            # Parse AI Credits from userTier.availableCredits (can be list of dicts or scalar) or planStatus
            avail_credits: Optional[float] = None
            total_credits: Optional[float] = None
            used_credits: Optional[float] = None
            credit_note: Optional[str] = None

            raw_avail = user_tier.get("availableCredits")
            if isinstance(raw_avail, list):
                bucket_sum = 0.0
                bucket_types: list[str] = []
                for item in raw_avail:
                    if isinstance(item, dict):
                        amt = _safe_float(
                            item.get("creditAmount")
                            if item.get("creditAmount") is not None
                            else item.get("amount")
                        )
                        if amt is not None:
                            bucket_sum += amt
                        else:
                            # Go omitempty omits creditAmount when balance is 0
                            bucket_sum += 0.0
                        ctype = item.get("creditType")
                        if ctype:
                            bucket_types.append(str(ctype))
                    else:
                        v = _safe_float(item)
                        if v is not None:
                            bucket_sum += v
                avail_credits = bucket_sum
                if bucket_types:
                    credit_note = ", ".join(bucket_types)
            else:
                avail_credits = _safe_float(raw_avail) or _safe_float(user_tier.get("creditBalance"))

            total_credits = _safe_float(user_tier.get("totalCredits")) or _safe_float(
                user_tier.get("monthlyCredits")
            )
            used_credits = _safe_float(user_tier.get("usedCredits"))

            if avail_credits is None and plan_status:
                avail_credits = _safe_float(plan_status.get("availablePromptCredits"))
                total_credits = total_credits or _safe_float(plan_info.get("monthlyPromptCredits"))
                used_credits = used_credits or _safe_float(plan_status.get("usedPromptCredits"))

            if avail_credits is not None or used_credits is not None or total_credits is not None:
                report.credits = CreditBalance(
                    used=used_credits,
                    remaining=avail_credits,
                    limit=total_credits,
                    unit="credits",
                    note=credit_note,
                )

        if brain_stats["brain_accessible"]:
            sources.append("~/.gemini/antigravity/brain")
            steps_7d = brain_stats["steps_7d"]
            convs_7d = brain_stats["conversations_7d"]
            report.sub_metrics.append(
                SubMetric(
                    name="7d Local Agent Activity",
                    used_str=f"{steps_7d} steps ({convs_7d} convs)",
                    remaining_str="—",
                )
            )
            if report.status == "unconfigured":
                report.status = "partial"
                report.plan_tier = "Antigravity Local"
                report.weekly = UsageWindow(
                    used=float(steps_7d),
                    unit="steps",
                    note=f"{convs_7d} active convs (7d)",
                )
                q_target = f"antigravity:{self.account}" if self.account else "antigravity"
                report.auth_hint = (
                    f"Open Antigravity IDE/CLI for live RPC quota or set `tokenmaster quota {q_target}`"
                )

        if sources:
            report.source = " + ".join(sources)

        if report.status == "unconfigured":
            q_target = f"antigravity:{self.account}" if self.account else "antigravity"
            report.auth_hint = (
                f"Launch Antigravity IDE/CLI or run `tokenmaster quota {q_target} --weekly-limit 100`"
            )

        return self.apply_config_overrides(report)
