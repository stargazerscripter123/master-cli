"""Read standalone Antigravity CLI quotas without a running IDE language server.

Linux's native Secret Service holds the consumer login under service ``gemini``,
username ``antigravity``. Secrets stay in memory; the native CLI owns refresh and
persistence. This adapter is only used for the default, unscoped subscription.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from tokenmaster import __version__
from tokenmaster.config import get_cache_dir
from tokenmaster.providers.base import HttpError, critical_section, file_lock

API_ROOT = "https://cloudcode-pa.googleapis.com/v1internal:"


class AntigravityCliClient:
    def __init__(self, http_json: Callable[..., dict[str, Any]], timeout: float) -> None:
        self.http_json = http_json
        self.timeout = timeout

    def _access_token(self) -> Optional[str]:
        tool = shutil.which("secret-tool")
        if not tool:
            return None
        try:
            proc = subprocess.run(
                [tool, "lookup", "service", "gemini", "username", "antigravity"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=min(3.0, self.timeout),
                check=False,
            )
            if proc.returncode != 0:
                return None
            auth = json.loads(proc.stdout)
            if not isinstance(auth, dict) or auth.get("auth_method") not in (None, "consumer"):
                return None
            oauth = auth.get("token")
            if not isinstance(oauth, dict):
                return None
            token = oauth.get("access_token")
            if not isinstance(token, str) or not token.strip() or "\r" in token or "\n" in token:
                return None
            return token.strip()
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    def _refresh(self, rejected_token: str) -> Optional[str]:
        binary = shutil.which("agy")
        if not binary:
            candidate = Path.home() / ".local" / "bin" / "agy"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                binary = str(candidate)
        if not binary:
            return None

        # In SSH sessions agy deliberately chooses file-backed credentials. We
        # are reading the desktop keyring, so use that same native auth context.
        env = os.environ.copy()
        for key in ("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY"):
            env.pop(key, None)
        env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
        try:
            lock = get_cache_dir() / "antigravity_cli_refresh.lock"
            with file_lock(lock, timeout=self.timeout), critical_section():
                current = self._access_token()
                if current and current != rejected_token:
                    return current
                # `models` is a read-only native command, never a model prompt.
                # Let agy handle OAuth refresh and its own secure persistence.
                proc = subprocess.run(
                    [binary, "models"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    timeout=self.timeout,
                    check=False,
                )
                if proc.returncode != 0:
                    return None
                current = self._access_token()
                return current if current and current != rejected_token else None
        except (OSError, subprocess.SubprocessError):
            return None

    def _query(self, token: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            # Google selects Antigravity's product tier using this identifier.
            # A generic tokenmaster UA yields an unrelated onboarding response.
            "User-Agent": f"antigravity tokenmaster/{__version__}",
        }
        account = self.http_json(
            API_ROOT + "loadCodeAssist",
            method="POST",
            headers=headers,
            payload={"metadata": {"ideType": "ANTIGRAVITY"}},
            timeout=self.timeout,
        )
        if not isinstance(account, dict):
            raise RuntimeError("Invalid Antigravity account response")
        project = account.get("cloudaicompanionProject")
        if isinstance(project, dict):
            project = project.get("id")
        if not isinstance(project, str) or not project.strip():
            raise RuntimeError("Antigravity account has no quota project")

        data = self.http_json(
            API_ROOT + "fetchAvailableModels",
            method="POST",
            headers=headers,
            payload={"project": project},
            timeout=self.timeout,
        )
        models = data.get("models") if isinstance(data, dict) else None
        configs = []
        if isinstance(models, dict):
            for model_id, model in models.items():
                if not isinstance(model, dict):
                    continue
                label = model.get("displayName")
                quota = model.get("quotaInfo")
                if not isinstance(label, str) or not label.strip() or not isinstance(quota, dict):
                    continue
                reading: dict[str, Any] = {}
                fraction_key = next((key for key in ("remainingFraction", "remaining_fraction") if key in quota), None)
                if fraction_key is not None:
                    fraction = quota[fraction_key]
                    try:
                        if isinstance(fraction, bool) or not math.isfinite(float(fraction)):
                            continue
                    except (TypeError, ValueError, OverflowError):
                        continue
                    reading["remainingFraction"] = float(fraction)
                reset = quota.get("resetTime") or quota.get("reset_time")
                if isinstance(reset, str) and reset.strip():
                    reading["resetTime"] = reset.strip()
                if not reading:
                    continue
                # Undisplayed completion/utility models are not subscription
                # quota pools. Keep API omission of remainingFraction intact:
                # with a reset time it means zero, rather than 100% remaining.
                configs.append({"label": label, "modelOrAlias": model_id, "quotaInfo": reading})
        if not configs:
            raise RuntimeError("Antigravity returned no model quota data")

        tier_name = "Antigravity"
        for key in ("paidTier", "currentTier"):
            tier = account.get(key)
            if isinstance(tier, dict) and isinstance(tier.get("name"), str) and tier["name"].strip():
                tier_name = tier["name"].strip()
                break
        # Feed the existing pool/window parser the same contract as GetUserStatus.
        # Do not carry tokens, project IDs or unrelated account details forward.
        return {"userStatus": {
            "userTier": {"name": tier_name},
            "cascadeModelConfigData": {"clientModelConfigs": configs},
        }}

    def query(self) -> Optional[dict[str, Any]]:
        if not sys.platform.startswith("linux"):
            return None
        token = self._access_token()
        if not token:
            return None
        try:
            return self._query(token)
        except HttpError as exc:
            if exc.status != 401:
                raise
            refreshed = self._refresh(token)
            if not refreshed:
                raise
            return self._query(refreshed)
