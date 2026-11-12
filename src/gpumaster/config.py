"""Fleet configuration: hosts.json plus optional ~/.ssh/config discovery."""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .transport import HostSpec, Poller, SshOptions, default_control_dir

APP = "gpumaster"
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATHS = [
    PROJECT_DIR / "hosts.json",
    Path.home() / ".config" / APP / "hosts.json",
]


@dataclass(slots=True)
class Config:
    refresh_seconds: float = 1.0
    history_samples: int = 120
    hosts: list[HostSpec] = field(default_factory=list)
    ssh: SshOptions = field(default_factory=SshOptions)
    source_path: Optional[Path] = None
    mtime: float = 0.0
    warnings: list[str] = field(default_factory=list)
    # Named host sets from the "modes" object in hosts.json, in file order.
    modes: dict[str, list[str]] = field(default_factory=dict)
    # The set on screen: "default" (every host not marked hidden), "all"
    # (every host), or a key of `modes`. Chosen by --mode / --mobile /
    # --desktop / --laptop / --all and cycled by `m` in the TUI.
    mode: str = "default"

    @property
    def mode_names(self) -> list[str]:
        named = [m for m in self.modes if m not in ("default", "all")]
        return ["default"] + named + ["all"]

    @property
    def mode_label(self) -> str:
        """The mode for the screen header; empty for the default one."""
        return "" if self.mode == "default" else self.mode

    def active_hosts(self) -> list[HostSpec]:
        """The hosts to poll and draw right now, in hosts.json order.

        `hosts` always holds every entry in hosts.json, so `gpumaster check
        <laptop>` and a runtime `m` can still reach one that is filtered out.
        """
        if self.mode in self.modes:
            wanted = set(self.modes[self.mode])
            return [h for h in self.hosts if h.name in wanted or h.target in wanted]
        if self.mode == "all":
            return list(self.hosts)
        # Never hide the local machine when running gpumaster on that machine itself.
        return [h for h in self.hosts if not h.hidden or Poller.is_local_target(h)]


def find_config(explicit: Optional[Path] = None) -> Optional[Path]:
    if explicit:
        return explicit if explicit.exists() else None
    for p in DEFAULT_CONFIG_PATHS:
        if p.exists():
            return p
    return None


_SSH_HOST_RE = re.compile(r"^\s*Host\s+(.+?)\s*$", re.I)


def ssh_config_aliases(path: Optional[Path] = None) -> list[str]:
    """Host aliases from ~/.ssh/config, excluding wildcard patterns."""
    path = path or (Path.home() / ".ssh" / "config")
    out: list[str] = []
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        m = _SSH_HOST_RE.match(line)
        if not m:
            continue
        for alias in m.group(1).split():
            if any(ch in alias for ch in "*?!"):
                continue
            if alias not in out:
                out.append(alias)
    return out


def _spec_from(item, warnings: list[str]) -> Optional[HostSpec]:
    if isinstance(item, str):
        return HostSpec(name=item, target=item)
    if not isinstance(item, dict):
        warnings.append(f"ignoring host entry of type {type(item).__name__}")
        return None
    target = item.get("target") or item.get("name")
    if not target:
        warnings.append("ignoring host entry with neither name nor target")
        return None
    return HostSpec(
        name=str(item.get("name") or target),
        target=str(target),
        enabled=bool(item.get("enabled", True)),
        hidden=bool(item.get("hidden", False)),
        tags=[str(t) for t in item.get("tags", [])],
        ssh_args=[str(a) for a in item.get("ssh_args", [])],
    )


def load_config(path: Optional[Path] = None) -> Config:
    cfg = Config()
    p = find_config(path)
    if p is None:
        cfg.warnings.append(
            "no hosts.json found; run `gpumaster init` or pass --config"
        )
        return cfg

    cfg.source_path = p
    try:
        cfg.mtime = p.stat().st_mtime
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        cfg.warnings.append(f"cannot read {p}: {exc}")
        return cfg

    cfg.refresh_seconds = float(raw.get("refresh_seconds", 1.0))
    if cfg.refresh_seconds < 0.2:
        cfg.warnings.append("refresh_seconds clamped to 0.2")
        cfg.refresh_seconds = 0.2
    cfg.history_samples = int(raw.get("history_samples", 120))

    s = raw.get("ssh", {}) or {}
    cfg.ssh = SshOptions(
        connect_timeout=int(s.get("connect_timeout_seconds", 4)),
        command_timeout=float(s.get("command_timeout_seconds", 8.0)),
        control_persist=int(s.get("control_persist_seconds", 120)),
        batch_mode=bool(s.get("batch_mode", True)),
        strict_host_key_checking=bool(s.get("strict_host_key_checking", True)),
        server_alive_interval=int(s.get("server_alive_interval", 5)),
        server_alive_count=int(s.get("server_alive_count", 2)),
        multiplex=bool(s.get("multiplex", True)),
        control_dir=Path(s.get("control_dir") or default_control_dir()),
        extra_args=[str(a) for a in s.get("extra_args", [])],
    )

    seen: set[str] = set()
    for item in raw.get("hosts", []):
        spec = _spec_from(item, cfg.warnings)
        if spec and spec.name not in seen:
            seen.add(spec.name)
            cfg.hosts.append(spec)

    disc = raw.get("discover") or {}
    if disc.get("ssh_config"):
        include = disc.get("include") or ["*"]
        exclude = disc.get("exclude") or []
        for alias in ssh_config_aliases():
            if alias in seen:
                continue
            if not any(fnmatch.fnmatch(alias, pat) for pat in include):
                continue
            if any(fnmatch.fnmatch(alias, pat) for pat in exclude):
                continue
            seen.add(alias)
            cfg.hosts.append(HostSpec(name=alias, target=alias, tags=["discovered"]))

    known = {n for h in cfg.hosts for n in (h.name, h.target)}
    for mode, members in (raw.get("modes") or {}).items():
        if not isinstance(members, list):
            cfg.warnings.append(f"mode {mode!r} is not a list of hosts; ignored")
            continue
        cfg.modes[str(mode)] = [str(m) for m in members]
        for m in cfg.modes[str(mode)]:
            if m not in known:
                cfg.warnings.append(f"mode {mode!r} names unknown host {m!r}")

    if not cfg.hosts:
        cfg.warnings.append(f"{p} lists no hosts")
    return cfg


def config_changed(cfg: Config) -> bool:
    if cfg.source_path is None:
        return False
    try:
        return cfg.source_path.stat().st_mtime != cfg.mtime
    except OSError:
        return False


EXAMPLE_CONFIG = """{
  "refresh_seconds": 2.0,
  "hosts": [
    {"name": "train-a", "target": "gpu-a", "tags": ["training"]},
    {"name": "train-b", "target": "gpu-b", "tags": ["training"]},
    {"name": "spark", "target": "dgx-spark", "tags": ["gb10"]},
    {"name": "studio", "target": "mac-studio", "tags": ["apple"]},
    {"name": "laptop", "target": "mac-laptop", "tags": ["apple", "laptop"], "hidden": true}
  ],
  "modes": {
    "mobile": ["laptop", "spark", "studio"],
    "desktop": ["train-a", "train-b", "spark"],
    "laptop": ["laptop"]
  }
}
"""
