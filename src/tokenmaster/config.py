"""Configuration and local ledger management for TokenMaster."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Optional

PROVIDERS = ("codex", "claude", "antigravity", "grok", "cursor", "openrouter")

ACCOUNT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
RESERVED_ACCOUNT_NAMES = {"default", "all", "none", "auto"}

PROVIDER_DIR_KEYS: dict[str, str] = {
    "claude": "config_dir",
    "codex": "codex_home",
    "grok": "grok_home",
    "cursor": "cursor_home",
    "antigravity": "antigravity_dir",
}


def validate_account_name(name: str) -> str:
    """Validate and normalize an account/profile identifier to lowercase.

    Account names are used as directory names under ``profiles/<provider>/`` and
    in ``<provider>:<account>`` CLI selectors. Normalizing to lowercase prevents
    case-insensitive filesystem collisions (macOS APFS / Windows NTFS) between
    e.g. ``Work`` and ``work``.
    """
    if not isinstance(name, str):
        raise ValueError("Account name must be a string")
    cleaned = name.strip().lower()
    if not cleaned:
        raise ValueError("Account name cannot be empty")
    if cleaned in RESERVED_ACCOUNT_NAMES:
        raise ValueError(
            f"Account name '{cleaned}' is reserved (omit --account to target the default subscription)"
        )
    if ".." in cleaned or not ACCOUNT_NAME_RE.match(cleaned):
        raise ValueError(
            f"Invalid account name '{name.strip()}': use 1-64 chars of [a-zA-Z0-9._-], starting with alphanumeric"
        )
    return cleaned


def parse_provider_spec(spec: str) -> tuple[str, Optional[str], bool]:
    """Parse ``<provider>`` or ``<provider>:<account>`` selector.

    Returns ``(provider_id, account_name_or_none, explicit_account)``:
    - ``"claude"``         -> ``("claude", None, False)`` (all enabled accounts, or default in auth/quota)
    - ``"claude:default"`` -> ``("claude", None, True)``  (specifically the default account)
    - ``"claude:work"``    -> ``("claude", "work", True)`` (specifically the 'work' account)
    """
    raw = (spec or "").strip()
    if ":" not in raw:
        return raw.lower(), None, False
    pid_part, acct_part = raw.split(":", 1)
    pid = pid_part.strip().lower()
    acct_clean = acct_part.strip()
    if not acct_clean:
        raise ValueError(f"Empty account name in provider spec '{spec}'")
    if acct_clean.lower() == "default":
        return pid, None, True
    return pid, validate_account_name(acct_clean), True


def _find_env_in_shell_files(
    var_name: str,
    *,
    allow_os_environ: bool = True,
    excluded_values: Optional[set[str]] = None,
) -> Optional[str]:
    """Fallback: scan ~/.zshenv, ~/.zshrc, ~/.bash_profile, ~/.profile for exported env var."""
    if allow_os_environ:
        val = os.environ.get(var_name)
        if val and (not excluded_values or val not in excluded_values):
            return val
    pattern = re.compile(
        rf"^\s*(?:export\s+)?{re.escape(var_name)}\s*=\s*['\"]?([^'\"#\r\n]+)['\"]?",
        re.MULTILINE,
    )
    for rc_name in (".zshenv", ".zshrc", ".zprofile", ".bash_profile", ".bashrc", ".profile"):
        rc_path = Path.home() / rc_name
        if rc_path.exists() and os.access(rc_path, os.R_OK):
            try:
                content = rc_path.read_text(encoding="utf-8", errors="replace")
                matches = pattern.findall(content)
                if matches:
                    candidate = matches[-1].strip()
                    if (
                        candidate
                        and not candidate.startswith("$")
                        and (not excluded_values or candidate not in excluded_values)
                    ):
                        return candidate
            except Exception:
                continue
    return None

DEFAULT_CONFIG: dict[str, Any] = {
    "refresh_interval_seconds": 30,
    "timeout_seconds": 8,
    # Global wall-clock cap for one dashboard refresh; slower providers are shown as
    # timed out (with any partial data). None -> 2.5 x timeout_seconds; 0 -> no cap.
    "deadline_seconds": None,
    "providers": {
        "codex": {
            "enabled": True,
            "access_token": None,
            "account_id": None,
            "api_key": None,
            "weekly_limit": None,
            "weekly_used": None,
            "weekly_unit": "%",
            "credit_limit": None,
            "credit_used": None,
            "credit_remaining": None,
            "credit_unit": "USD",
        },
        "claude": {
            "enabled": True,
            "oauth_token": None,
            "api_key": None,
            "weekly_limit": None,
            "weekly_used": None,
            "weekly_unit": "%",
            "credit_limit": None,
            "credit_used": None,
            "credit_remaining": None,
            "credit_unit": "USD",
        },
        "antigravity": {
            "enabled": True,
            "csrf_token": None,
            "port": None,
            "weekly_limit": None,
            "weekly_used": None,
            "weekly_unit": "%",
            "credit_limit": None,
            "credit_used": None,
            "credit_remaining": None,
            "credit_unit": "credits",
        },
        "grok": {
            "enabled": True,
            "cookie": None,
            "sso_token": None,
            "api_key": None,
            "model_name": "grok-3",
            "weekly_limit": None,
            "weekly_used": None,
            "weekly_unit": "queries",
            "credit_limit": None,
            "credit_used": None,
            "credit_remaining": None,
            "credit_unit": "USD",
        },
        "cursor": {
            "enabled": True,
            "access_token": None,
            "cookie": None,
            "state_db_path": None,
            "weekly_limit": None,
            "weekly_used": None,
            "weekly_unit": "USD",
            "credit_limit": None,
            "credit_used": None,
            "credit_remaining": None,
            "credit_unit": "USD",
        },
        "openrouter": {
            "enabled": True,
            "api_key": None,
            "management_key": None,
            "weekly_limit": None,
            "weekly_used": None,
            "weekly_unit": "USD",
            "credit_limit": None,
            "credit_used": None,
            "credit_remaining": None,
            "credit_unit": "USD",
        },
    },
}


def get_config_dir() -> Path:
    """Resolve the active configuration directory with sandbox fallback."""
    env_dir = os.environ.get("TOKENMASTER_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser()

    home_cfg = Path.home() / ".config" / "tokenmaster"
    try:
        home_cfg.mkdir(parents=True, exist_ok=True)
        if os.access(home_cfg, os.R_OK | os.W_OK):
            return home_cfg
    except OSError:
        pass

    # Fallback to workspace-local .tokenmaster directory if ~/.config is not writable
    local_cfg = Path.cwd() / ".tokenmaster"
    try:
        local_cfg.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return local_cfg


def get_config_path() -> Path:
    return get_config_dir() / "config.json"


def get_cache_dir() -> Path:
    """Resolve the directory for best-effort scan caches (never required to exist)."""
    env_dir = os.environ.get("TOKENMASTER_CACHE_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else (Path.home() / ".cache")
    return base / "tokenmaster"


def get_profiles_dir(provider_id: Optional[str] = None) -> Path:
    """Resolve the root directory for isolated CLI subscription sandboxes."""
    env_dir = os.environ.get("TOKENMASTER_PROFILES_DIR")
    base = Path(env_dir).expanduser() if env_dir else (get_config_dir() / "profiles")
    base_abs = Path(os.path.abspath(str(base)))
    if provider_id:
        return base_abs / provider_id.lower()
    return base_abs


def get_account_profile_dir(provider_id: str, account: str) -> Path:
    """Return the absolute profile sandbox directory for ``(provider_id, account)``."""
    pid = provider_id.strip().lower()
    if pid not in PROVIDERS:
        raise ValueError(f"Unknown provider '{provider_id}'")
    acct = validate_account_name(account)
    return get_profiles_dir(pid) / acct


def is_path_within(path: Path, parent: Path) -> bool:
    """True if ``path`` is equal to or inside ``parent`` (comparing both abspath and resolve)."""
    try:
        p_abs = Path(os.path.abspath(str(path.expanduser())))
        par_abs = Path(os.path.abspath(str(parent.expanduser())))
        if p_abs == par_abs or par_abs in p_abs.parents:
            return True
    except Exception:
        pass
    try:
        p_res = path.expanduser().resolve()
        par_res = parent.expanduser().resolve()
        if p_res == par_res or par_res in p_res.parents:
            return True
    except Exception:
        pass
    return False


def is_forbidden_profile_dir(candidate: Path) -> bool:
    """Return True if ``candidate`` is a system/home/global CLI directory that must never
    be used as an isolated account sandbox or deleted by ``remove_account(purge=True)``.
    """
    raw_str = str(candidate)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_str):
        return True
    home = Path.home()
    forbidden = (
        Path("/"),
        home,
        Path.cwd(),
        get_config_dir(),
        get_profiles_dir(),
        home / ".claude",
        home / ".codex",
        home / ".grok",
        home / ".cursor",
        home / ".gemini",
        home / ".gemini" / "antigravity",
        home / ".openrouter",
        home / ".config",
        home / ".config" / "openrouter",
    )
    try:
        cand_abs = Path(os.path.abspath(str(candidate.expanduser())))
        cand_res = candidate.expanduser().resolve()
    except Exception:
        return True
    for f_path in forbidden:
        try:
            f_abs = Path(os.path.abspath(str(f_path.expanduser())))
            f_res = f_path.expanduser().resolve()
            if cand_abs == f_abs or cand_res == f_res:
                return True
        except Exception:
            continue
    # Also forbid any ancestor of home or get_config_dir()
    if cand_abs in home.parents or cand_res in home.expanduser().resolve().parents:
        return True
    return False


def _is_managed_profile_subdir(candidate: Path, provider_id: str, account: str) -> bool:
    """True only if ``candidate`` is the managed ``get_profiles_dir(provider_id)/<account>`` dir."""
    if candidate.is_symlink() or is_forbidden_profile_dir(candidate):
        return False
    expected = get_account_profile_dir(provider_id, account)
    try:
        return (
            Path(os.path.abspath(str(candidate))) == Path(os.path.abspath(str(expected)))
            and candidate.resolve().parent == get_profiles_dir(provider_id).resolve()
            and candidate.resolve().name.lower() == account.lower()
        )
    except Exception:
        return False


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


class ConfigManager:
    """Loads, merges, and persists TokenMaster configuration and credentials."""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self.path = config_path or get_config_path()
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cfg = _deep_merge(cfg, raw)
            except Exception:
                pass
        return cfg

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return self.path

    def provider_cfg(self, provider_id: str) -> dict[str, Any]:
        """Return merged provider config with environment variable overrides."""
        p = dict(self.data.get("providers", {}).get(provider_id, {}))

        # Record custom profile_dirs and secret values of any named accounts so the
        # default account never lets an active profile environment variable hijack it.
        excluded_dirs: list[str] = []
        excluded_secrets: set[str] = set()
        default_explicit_secrets = {
            str(p.get(k)).strip()
            for k in ("access_token", "oauth_token", "api_key", "management_key", "cookie", "sso_token", "csrf_token")
            if p.get(k)
        }
        accts_raw = p.get("accounts")
        dir_key = PROVIDER_DIR_KEYS.get(provider_id)
        if isinstance(accts_raw, dict):
            for a_name, a_val in accts_raw.items():
                if not isinstance(a_val, dict):
                    continue
                raw_d = a_val.get("profile_dir") or (a_val.get(dir_key) if dir_key else None)
                if isinstance(raw_d, str) and raw_d.strip():
                    excluded_dirs.append(os.path.abspath(str(Path(raw_d.strip()).expanduser())))
                for skey in ("access_token", "oauth_token", "api_key", "management_key", "cookie", "sso_token", "csrf_token"):
                    sval = a_val.get(skey)
                    if isinstance(sval, str) and sval.strip() and sval.strip() not in default_explicit_secrets:
                        excluded_secrets.add(sval.strip())
        p["_excluded_env_dirs"] = excluded_dirs

        active_prof = (os.environ.get("TOKENMASTER_ACTIVE_PROFILE") or "").strip().lower()
        allow_env = not (active_prof and not active_prof.endswith(":default"))

        def _env_lookup(var_name: str) -> Optional[str]:
            return _find_env_in_shell_files(
                var_name,
                allow_os_environ=allow_env,
                excluded_values=excluded_secrets or None,
            )

        if provider_id == "codex":
            p["access_token"] = _env_lookup("CODEX_ACCESS_TOKEN") or p.get("access_token")
            p["api_key"] = _env_lookup("OPENAI_API_KEY") or p.get("api_key")
        elif provider_id == "claude":
            p["oauth_token"] = (
                _env_lookup("CLAUDE_OAUTH_TOKEN")
                or _env_lookup("CLAUDE_CODE_OAUTH_TOKEN")
                or p.get("oauth_token")
            )
            p["api_key"] = _env_lookup("ANTHROPIC_API_KEY") or p.get("api_key")
        elif provider_id == "antigravity":
            p["csrf_token"] = _env_lookup("ANTIGRAVITY_CSRF_TOKEN") or p.get("csrf_token")
            if allow_env and os.environ.get("ANTIGRAVITY_PORT"):
                try:
                    p["port"] = int(os.environ["ANTIGRAVITY_PORT"])
                except ValueError:
                    pass
        elif provider_id == "grok":
            p["cookie"] = _env_lookup("GROK_COOKIE") or p.get("cookie")
            p["sso_token"] = _env_lookup("GROK_SSO_TOKEN") or p.get("sso_token")
            p["api_key"] = _env_lookup("XAI_API_KEY") or p.get("api_key")
        elif provider_id == "cursor":
            p["access_token"] = _env_lookup("CURSOR_ACCESS_TOKEN") or p.get("access_token")
            p["cookie"] = _env_lookup("CURSOR_COOKIE") or p.get("cookie")
            p["state_db_path"] = _env_lookup("CURSOR_STATE_DB") or p.get("state_db_path")
        elif provider_id == "openrouter":
            p["api_key"] = _env_lookup("OPENROUTER_API_KEY") or p.get("api_key")
            p["management_key"] = (
                _env_lookup("OPENROUTER_MANAGEMENT_KEY") or p.get("management_key")
            )

        return p

    def set_provider_field(self, provider_id: str, key: str, value: Any) -> None:
        if "providers" not in self.data:
            self.data["providers"] = {}
        if provider_id not in self.data["providers"]:
            self.data["providers"][provider_id] = {}
        self.data["providers"][provider_id][key] = value
        self.save()

    def account_cfg(self, provider_id: str, account: Optional[str] = None) -> dict[str, Any]:
        """Return isolated configuration dict for a provider's default or named subscription.

        Named accounts start from a clean provider template (never inheriting the
        default account's tokens, cookies, or shell rc environment variables) so
        multiple subscriptions stay strictly isolated.
        """
        if not account or account.strip().lower() == "default":
            cfg = self.provider_cfg(provider_id)
            cfg["account_name"] = None
            return cfg

        acct = validate_account_name(account)
        base_template = json.loads(
            json.dumps(DEFAULT_CONFIG.get("providers", {}).get(provider_id, {}))
        )
        p_raw = self.data.get("providers", {}).get(provider_id, {})
        accounts_map = p_raw.get("accounts") if isinstance(p_raw, dict) else None
        stored: dict[str, Any] = {}
        if isinstance(accounts_map, dict):
            # Match case-insensitively in case config.json was edited manually with uppercase
            for k_raw, v_raw in accounts_map.items():
                if isinstance(k_raw, str) and k_raw.strip().lower() == acct and isinstance(v_raw, dict):
                    stored = _deep_merge(stored, v_raw)
        cfg = _deep_merge(base_template, stored)
        cfg["account_name"] = acct

        # Respect explicit profile_dir or provider-specific dir key (e.g. config_dir, codex_home)
        # while rejecting empty/whitespace strings or dangerous global paths (~/.claude, ~, .).
        dir_key = PROVIDER_DIR_KEYS.get(provider_id)
        raw_dir = cfg.get("profile_dir")
        if not (isinstance(raw_dir, str) and raw_dir.strip()) and dir_key:
            raw_dir = cfg.get(dir_key)

        resolved_pdir: Optional[Path] = None
        if isinstance(raw_dir, str) and raw_dir.strip():
            cand = Path(os.path.abspath(str(Path(raw_dir.strip()).expanduser())))
            if not is_forbidden_profile_dir(cand):
                resolved_pdir = cand

        if resolved_pdir is None:
            resolved_pdir = get_account_profile_dir(provider_id, acct)

        cfg["profile_dir"] = str(resolved_pdir)
        return cfg

    def list_accounts(
        self,
        provider_id: str,
        include_disabled: bool = False,
        ignore_provider_enabled: bool = False,
    ) -> list[tuple[Optional[str], dict[str, Any]]]:
        """Return ``[(account_name_or_None, cfg_dict), ...]`` for all tracked subscriptions.

        Combines:
        1. The default subscription (``account_name=None``), unless ``include_default`` is False.
        2. Explicitly configured accounts under ``providers.<pid>.accounts`` in ``config.json``.
        3. Auto-discovered non-empty sandbox directories under ``get_profiles_dir(pid)``,
           unless explicitly removed via ``ignored_profiles``.
        """
        p_raw = self.data.get("providers", {}).get(provider_id, {})
        if not isinstance(p_raw, dict):
            p_raw = {}

        provider_enabled = bool(p_raw.get("enabled", True))
        if not provider_enabled and not include_disabled and not ignore_provider_enabled:
            return []

        out: list[tuple[Optional[str], dict[str, Any]]] = []

        include_default = bool(p_raw.get("include_default", True))
        if include_default:
            default_cfg = self.account_cfg(provider_id, None)
            if include_disabled or ignore_provider_enabled or default_cfg.get("enabled", True):
                out.append((None, default_cfg))

        accounts_map = p_raw.get("accounts")
        if not isinstance(accounts_map, dict):
            accounts_map = {}

        ignored_raw = p_raw.get("ignored_profiles")
        ignored: set[str] = (
            {str(x).strip().lower() for x in ignored_raw if isinstance(x, str)}
            if isinstance(ignored_raw, list)
            else set()
        )

        ordered_names: list[str] = []
        for raw_name in accounts_map:
            try:
                valid_name = validate_account_name(str(raw_name))
            except ValueError:
                continue
            if valid_name not in ordered_names:
                ordered_names.append(valid_name)

        # Auto-discover non-empty profile directories on disk
        prof_root = get_profiles_dir(provider_id)
        if prof_root.is_dir():
            try:
                for child in sorted(prof_root.iterdir(), key=lambda p: p.name.lower()):
                    if child.name.startswith(".") or not child.is_dir() or child.is_symlink():
                        continue
                    try:
                        valid_name = validate_account_name(child.name)
                    except ValueError:
                        continue
                    if valid_name in ordered_names or valid_name in ignored:
                        continue
                    try:
                        has_contents = any(
                            not item.name.startswith(".DS_Store") for item in child.iterdir()
                        )
                    except OSError:
                        has_contents = False
                    if has_contents:
                        ordered_names.append(valid_name)
            except OSError:
                pass

        for acct_name in ordered_names:
            acct_cfg = self.account_cfg(provider_id, acct_name)
            if not include_disabled and not acct_cfg.get("enabled", True):
                continue
            out.append((acct_name, acct_cfg))

        return out

    def ensure_account(
        self,
        provider_id: str,
        account: str,
        create_profile_dir: bool = True,
    ) -> Path:
        """Register a named subscription account in config.json and ensure its sandbox dir exists."""
        pid = provider_id.strip().lower()
        if pid not in PROVIDERS:
            raise ValueError(f"Unknown provider '{provider_id}'")
        acct = validate_account_name(account)

        p_dict = self.data.setdefault("providers", {}).setdefault(pid, {})
        accounts = p_dict.setdefault("accounts", {})
        if not isinstance(accounts, dict):
            accounts = {}
            p_dict["accounts"] = accounts
        # Consolidate any legacy mixed-case keys for this account
        for k_existing in list(accounts.keys()):
            if isinstance(k_existing, str) and k_existing.strip().lower() == acct and k_existing != acct:
                old_val = accounts.pop(k_existing)
                if isinstance(old_val, dict):
                    accounts[acct] = _deep_merge(old_val, accounts.get(acct) if isinstance(accounts.get(acct), dict) else {})
        acct_entry = accounts.setdefault(acct, {})
        if not isinstance(acct_entry, dict):
            acct_entry = {}
            accounts[acct] = acct_entry
        acct_entry.setdefault("enabled", True)

        ignored = p_dict.get("ignored_profiles")
        if isinstance(ignored, list):
            p_dict["ignored_profiles"] = [
                x for x in ignored if not (isinstance(x, str) and x.strip().lower() == acct)
            ]
            if not p_dict["ignored_profiles"]:
                p_dict.pop("ignored_profiles", None)

        dir_key = PROVIDER_DIR_KEYS.get(pid)
        raw_pdir = acct_entry.get("profile_dir")
        if not (isinstance(raw_pdir, str) and raw_pdir.strip()) and dir_key:
            raw_pdir = acct_entry.get(dir_key)

        profile_dir: Optional[Path] = None
        if isinstance(raw_pdir, str) and raw_pdir.strip():
            cand = Path(os.path.abspath(str(Path(raw_pdir.strip()).expanduser())))
            if is_forbidden_profile_dir(cand):
                raise ValueError(
                    f"Refusing to use global/system directory '{cand}' as profile_dir for '{pid}:{acct}'"
                )
            profile_dir = cand
        if profile_dir is None:
            profile_dir = get_account_profile_dir(pid, acct)

        if create_profile_dir:
            profile_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(profile_dir, 0o700)
            except OSError:
                pass
        self.save()
        return profile_dir

    def set_account_field(
        self,
        provider_id: str,
        account: Optional[str],
        key: str,
        value: Any,
    ) -> None:
        """Set a configuration field on either the default account (None) or a named account."""
        if not account or account.strip().lower() == "default":
            self.set_provider_field(provider_id, key, value)
            return
        pid = provider_id.strip().lower()
        acct = validate_account_name(account)
        if key in ("profile_dir", PROVIDER_DIR_KEYS.get(pid)) and value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"'{key}' must be a non-empty directory path")
            cand = Path(os.path.abspath(str(Path(value.strip()).expanduser())))
            if is_forbidden_profile_dir(cand):
                raise ValueError(
                    f"Refusing to set '{key}' to global/system directory '{cand}'"
                )
        self.ensure_account(pid, acct, create_profile_dir=False)
        self.data["providers"][pid]["accounts"][acct][key] = value
        self.save()

    def remove_account(
        self,
        provider_id: str,
        account: str,
        purge: bool = False,
    ) -> tuple[bool, Path]:
        """Remove a named subscription from config (and delete its managed sandbox dir if ``purge=True``).

        Returns ``(existed, profile_dir)``.
        """
        pid = provider_id.strip().lower()
        if pid not in PROVIDERS:
            raise ValueError(f"Unknown provider '{provider_id}'")
        acct = validate_account_name(account)
        managed_dir = get_account_profile_dir(pid, acct)
        profile_dir = managed_dir

        p_dict = self.data.setdefault("providers", {}).setdefault(pid, {})
        accounts = p_dict.get("accounts")
        existed = False
        if isinstance(accounts, dict):
            for k_existing in list(accounts.keys()):
                if isinstance(k_existing, str) and k_existing.strip().lower() == acct:
                    existed = True
                    entry = accounts.pop(k_existing)
                    if isinstance(entry, dict):
                        raw_stored = entry.get("profile_dir") or entry.get(PROVIDER_DIR_KEYS.get(pid, ""))
                        if isinstance(raw_stored, str) and raw_stored.strip():
                            cand = Path(os.path.abspath(str(Path(raw_stored.strip()).expanduser())))
                            if not is_forbidden_profile_dir(cand):
                                profile_dir = cand

        if profile_dir.exists() or managed_dir.exists():
            existed = True
            if purge and _is_managed_profile_subdir(profile_dir, pid, acct):
                shutil.rmtree(profile_dir, ignore_errors=True)
            # If the managed directory still exists on disk (e.g. purge=False, external dir,
            # or partial permission error), record in ignored_profiles so auto-discovery skips it.
            if managed_dir.exists():
                ignored = p_dict.setdefault("ignored_profiles", [])
                if isinstance(ignored, list):
                    norm_existing = {str(x).strip().lower() for x in ignored if isinstance(x, str)}
                    if acct not in norm_existing:
                        ignored.append(acct)

        self.save()
        return existed, profile_dir
