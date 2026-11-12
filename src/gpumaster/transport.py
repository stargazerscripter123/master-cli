"""SSH transport: concurrent agentless polling with connection multiplexing.

Nothing is installed on the GPU nodes. `remote.sh` is piped to `bash -s` over a
multiplexed OpenSSH connection, and every host is polled concurrently so a slow
or dead one never blocks the others.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .model import Host, HostState
from .parse import REMEDIES, TERMINAL_ERRORS, parse_payload, ssh_error_kind

_LOCAL_IDS_CACHE: Optional[set[str]] = None
_SSH_CONFIG_HOSTNAMES_CACHE: Optional[dict[str, str]] = None


def _ssh_config_hostnames(path: Optional[Path] = None) -> dict[str, str]:
    """Map literal SSH Host aliases in ~/.ssh/config to their lowercased HostName."""
    global _SSH_CONFIG_HOSTNAMES_CACHE
    if path is None and _SSH_CONFIG_HOSTNAMES_CACHE is not None:
        return _SSH_CONFIG_HOSTNAMES_CACHE
    p = path or (Path.home() / ".ssh" / "config")
    mapping: dict[str, str] = {}
    try:
        text = p.read_text(errors="replace")
    except OSError:
        if path is None:
            _SSH_CONFIG_HOSTNAMES_CACHE = mapping
        return mapping
    cur_aliases: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m_host = re.match(r"^Host\s+(.+)$", line, re.I)
        if m_host:
            cur_aliases = [
                a.lower() for a in m_host.group(1).split()
                if not any(ch in a for ch in "*?!")
            ]
            continue
        m_hn = re.match(r"^HostName\s+(\S+)$", line, re.I)
        if m_hn and cur_aliases:
            hn = m_hn.group(1).strip().lower()
            for a in cur_aliases:
                mapping.setdefault(a, hn)
    if path is None:
        _SSH_CONFIG_HOSTNAMES_CACHE = mapping
    return mapping


def _local_identities() -> set[str]:
    """Return lowercased hostnames, interface IPs, and Darwin chip aliases for this machine."""
    global _LOCAL_IDS_CACHE
    if _LOCAL_IDS_CACHE is not None:
        return _LOCAL_IDS_CACHE

    ids: set[str] = {"localhost", "localhost.", "127.0.0.1", "::1"}
    for raw_hn in (socket.gethostname(), os.uname().nodename):
        hn = (raw_hn or "").strip().lower()
        if hn:
            ids.add(hn)
            ids.add(hn.split(".", 1)[0])

    # Collect local interface IP addresses (including Tailscale utun/tailscale0 IPs)
    if_cmd = ["ifconfig"] if sys.platform == "darwin" else ["ip", "-o", "addr"]
    try:
        cp = subprocess.run(if_cmd, capture_output=True, text=True, timeout=1.0)
        if cp.returncode == 0:
            for m in re.finditer(r"\binet6?\s+([0-9a-fA-F:.]+)", cp.stdout):
                ip = m.group(1).split("%", 1)[0].split("/", 1)[0].lower()
                if ip:
                    ids.add(ip)
    except (OSError, subprocess.SubprocessError):
        pass

    # A fleet alias is this machine when it matches the OS hostname, an
    # interface address, or a short name derived from the Apple silicon chip
    # (generation plus a tier letter: Max, Ultra or Pro).
    if sys.platform == "darwin":
        try:
            cp = subprocess.run(
                ["/usr/sbin/ioreg", "-r", "-d", "1", "-c", "IOAccelerator"],
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            if cp.returncode == 0:
                m_chip = re.search(r'"model"\s*=\s*"Apple\s+M(\d+)(?:\s+(Max|Ultra|Pro))?"', cp.stdout, re.I)
                if m_chip:
                    gen = m_chip.group(1)
                    tier = (m_chip.group(2) or "").lower()
                    suffix = {"max": "x", "ultra": "u", "pro": "p"}.get(tier, "")
                    if suffix:
                        ids.update({
                            f"m{gen}{suffix}",
                            f"mac-m{gen}{suffix}",
                            f"m{gen}{tier}",
                            f"mac-m{gen}{tier}",
                        })
                    else:
                        ids.update({f"m{gen}", f"mac-m{gen}"})
        except (OSError, subprocess.SubprocessError):
            pass

    _LOCAL_IDS_CACHE = ids
    return ids

# macOS caps AF_UNIX socket paths at 104 bytes. %C expands to a 40-character
# hash, so the directory prefix has to stay well under that.
#
# tempfile.gettempdir() on macOS is a long per-user directory under
# /var/folders. Using it for the ssh control socket pushes ControlPath past
# the 104-byte AF_UNIX limit, and ssh then fails with rc=255 for every host.
CONTROL_HASH_LEN = 40
CONTROL_PATH_MAX = 104


def default_control_dir() -> Path:
    return Path(f"/tmp/gm.{os.getuid()}")


def control_path_len(control_dir: Path) -> int:
    return len(str(control_dir)) + len("/cm-") + CONTROL_HASH_LEN


def check_control_dir(control_dir: Path) -> Optional[str]:
    """Return an error message if this directory cannot host a control socket."""
    n = control_path_len(control_dir)
    if n >= CONTROL_PATH_MAX:
        return (
            f"ControlPath would be {n} bytes, over the {CONTROL_PATH_MAX}-byte "
            f"limit for UNIX sockets:\n    {control_dir}/cm-%C\n"
            f"Use a shorter directory, e.g. --control-dir {default_control_dir()}"
        )
    return None


@dataclass(slots=True)
class SshOptions:
    connect_timeout: int = 4
    command_timeout: float = 8.0
    control_persist: int = 120
    batch_mode: bool = True
    strict_host_key_checking: bool = True
    server_alive_interval: int = 5
    server_alive_count: int = 2
    multiplex: bool = True
    control_dir: Path = field(default_factory=default_control_dir)
    extra_args: list[str] = field(default_factory=list)


@dataclass(slots=True)
class HostSpec:
    name: str
    target: str
    enabled: bool = True
    # Left out of the "default" mode: a laptop that is reachable over Tailscale
    # but asleep or away most of the time would otherwise sit on the default
    # screen on a backoff countdown. A named mode that lists it still shows it.
    hidden: bool = False
    tags: list[str] = field(default_factory=list)
    ssh_args: list[str] = field(default_factory=list)


class Poller:
    """Polls every host concurrently and returns fully parsed Host objects."""

    def __init__(self, options: SshOptions, script_path: Path):
        self.opt = options
        self.script = script_path.read_text()
        self._control_ready = False
        # Backoff and cached inventory survive across polls.
        self._fail_count: dict[str, int] = {}
        self._next_try: dict[str, float] = {}
        self._cached_label: dict[str, str] = {}
        self._last_ok: dict[str, float] = {}
        # Why a host last failed, kept so the backoff countdown can report the
        # reason instead of replacing it.
        self._last_error: dict[str, tuple[str, str]] = {}
        self._stderr_ring: dict[str, list[str]] = {}
        # (host_name, if_name) -> (monotonic_ts, rx_bytes, tx_bytes)
        self._net_counters: dict[tuple[str, str], tuple[float, int, int]] = {}

    # -- setup ------------------------------------------------------------

    def preflight(self) -> Optional[str]:
        """Validate the environment before any connection is attempted."""
        if not self.opt.multiplex:
            return None
        err = check_control_dir(self.opt.control_dir)
        if err:
            return err
        try:
            self.opt.control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.opt.control_dir, 0o700)
        except OSError as exc:
            return f"cannot create control directory {self.opt.control_dir}: {exc}"
        self._control_ready = True
        return None

    def ssh_argv(self, spec: HostSpec) -> list[str]:
        o = self.opt
        argv = [
            "ssh", "-T",
            "-o", "LogLevel=ERROR",
            "-o", f"ConnectTimeout={o.connect_timeout}",
            "-o", f"ServerAliveInterval={o.server_alive_interval}",
            "-o", f"ServerAliveCountMax={o.server_alive_count}",
        ]
        if o.batch_mode:
            argv += ["-o", "BatchMode=yes"]
        if o.strict_host_key_checking:
            argv += ["-o", "StrictHostKeyChecking=yes"]
        if o.multiplex and self._control_ready:
            argv += [
                "-o", "ControlMaster=auto",
                "-o", f"ControlPersist={o.control_persist}s",
                "-o", f"ControlPath={o.control_dir}/cm-%C",
            ]
        argv += o.extra_args
        argv += spec.ssh_args
        argv += [spec.target, "bash -s"]
        return argv

    @staticmethod
    def is_local_target(spec: HostSpec) -> bool:
        """True when `spec` targets this machine without custom per-host SSH args."""
        if spec.ssh_args:
            return False
        t = (spec.target or "").strip().lower()
        if not t or "@" in t:
            return False
        if any(tag.lower() == "local" for tag in spec.tags):
            return True
        local_ids = _local_identities()
        candidates = {t, t.split(".", 1)[0]}
        n = (spec.name or "").strip().lower()
        if n:
            candidates.add(n)
        ssh_hn = _ssh_config_hostnames().get(t)
        if ssh_hn:
            candidates.add(ssh_hn)
            candidates.add(ssh_hn.split(".", 1)[0])
        return bool(candidates & local_ids)

    def exec_argv(self, spec: HostSpec) -> list[str]:
        """Return `['bash', '-s']` for local targets, or `ssh_argv(spec)` for remote."""
        if self.is_local_target(spec):
            return ["bash", "-s"]
        return self.ssh_argv(spec)

    # -- polling ----------------------------------------------------------

    def backoff_remaining(self, name: str) -> float:
        return max(0.0, self._next_try.get(name, 0.0) - time.monotonic())

    def _note_failure(self, name: str, terminal: bool,
                      code: str = "", msg: str = "") -> None:
        n = self._fail_count.get(name, 0) + 1
        self._fail_count[name] = n
        if msg:
            self._last_error[name] = (code, msg)
        if terminal:
            # An auth failure or a changed host key will not fix itself; stop
            # hammering and let the user act on the printed remedy.
            delay = 300.0
        else:
            delay = min(60.0, 2.0 ** min(n, 6))
        self._next_try[name] = time.monotonic() + delay

    def _note_success(self, name: str) -> None:
        self._fail_count.pop(name, None)
        self._next_try.pop(name, None)
        self._last_error.pop(name, None)
        self._last_ok[name] = time.time()

    def _update_net_rates(self, host: Host, now_mono: float) -> None:
        """Compute rx_gbps / tx_gbps from monotonic byte-counter deltas."""
        for nl in host.net_links:
            if nl.rx_bytes is None or nl.tx_bytes is None:
                continue
            key = (host.name, nl.name)
            prev = self._net_counters.get(key)
            self._net_counters[key] = (now_mono, nl.rx_bytes, nl.tx_bytes)
            if prev is None:
                continue
            prev_ts, prev_rx, prev_tx = prev
            dt = now_mono - prev_ts
            if dt <= 0.0:
                continue
            drx = nl.rx_bytes - prev_rx
            dtx = nl.tx_bytes - prev_tx
            if drx < 0 or dtx < 0:
                continue
            nl.rx_gbps = (drx * 8.0) / dt / 1e9
            nl.tx_gbps = (dtx * 8.0) / dt / 1e9

    async def poll_one(self, spec: HostSpec) -> Host:
        host = Host(name=spec.name, target=spec.target, tags=list(spec.tags))
        host.cached_gpu_label = self._cached_label.get(spec.name, "")
        host.last_ok_wall = self._last_ok.get(spec.name, 0.0)
        host.last_try_wall = time.time()

        if not spec.enabled:
            host.state = HostState.DISABLED
            host.error_msg = "disabled in config"
            return host

        wait = self.backoff_remaining(spec.name)
        if wait > 0:
            # Lead with WHY, not just when. Reporting only "retry in 2s" left a
            # whole fleet of down hosts on screen with nothing to act on -- the
            # reason had already scrolled past in the one frame before the
            # first backoff started.
            code, msg = self._last_error.get(spec.name, ("", ""))
            host.state = HostState.DOWN
            host.error_code = code or "backoff"
            host.error_msg = (f"{msg}  [retry in {wait:.0f}s]" if msg
                              else f"retry in {wait:.0f}s")
            return host

        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.exec_argv(spec),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            host.state = HostState.DOWN
            host.error_code = "spawn-failed"
            host.error_msg = str(exc)
            self._note_failure(spec.name, False, host.error_code, host.error_msg)
            return host

        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(self.script.encode()),
                timeout=self.opt.command_timeout,
            )
            rc = proc.returncode or 0
        except asyncio.TimeoutError:
            # Never leave a wedged child behind.
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            host.state = HostState.DOWN
            host.error_code = "timeout"
            host.error_msg = f"no response in {self.opt.command_timeout:.0f}s"
            self._note_failure(spec.name, False, host.error_code, host.error_msg)
            return host

        now_mono = time.monotonic()
        latency = (now_mono - started) * 1000.0
        out = out_b.decode("utf-8", "replace")
        err = err_b.decode("utf-8", "replace")

        if err.strip():
            ring = self._stderr_ring.setdefault(spec.name, [])
            ring.extend(err.strip().splitlines())
            del ring[:-20]

        parse_payload(host, out, err, rc, latency)

        if host.state is HostState.DOWN:
            kind = ssh_error_kind(err)
            terminal = kind in TERMINAL_ERRORS
            remedy = REMEDIES.get(kind)
            if remedy:
                host.error_msg = f"{host.error_msg}  [fix: {remedy.replace('<host>', spec.target)}]"
            self._note_failure(spec.name, terminal, host.error_code, host.error_msg)
        else:
            self._note_success(spec.name)
            self._update_net_rates(host, now_mono)
            if host.gpus:
                self._cached_label[spec.name] = host.gpu_label
            host.last_ok_wall = self._last_ok.get(spec.name, 0.0)
        return host

    async def poll_all(self, specs: list[HostSpec]) -> list[Host]:
        # The control directory lives in /tmp and is shared by every gpumaster
        # process for this user: an older shutdown() rmtree'd it on exit, and
        # macOS periodic jobs prune /tmp. Once it is gone, every connection
        # dies with `unix_listener: cannot bind ... No such file or directory`
        # (rc=255, whole fleet HOST DOWN, observed live 2026-09-02) -- so
        # re-assert it before each sweep instead of trusting preflight() once.
        if self.opt.multiplex and self._control_ready and not self.opt.control_dir.is_dir():
            try:
                self.opt.control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(self.opt.control_dir, 0o700)
            except OSError:
                pass  # the per-host ssh errors will say why; retried next sweep
        return list(await asyncio.gather(*(self.poll_one(s) for s in specs)))

    def stderr_ring(self, name: str) -> list[str]:
        return list(self._stderr_ring.get(name, []))

    def kill_remote_proc(
        self, spec: HostSpec, pid: int, sig: str = "TERM"
    ) -> tuple[bool, str]:
        """Send `sig` (`TERM`, `KILL`, `INT`, `HUP`) to `pid` (> 1) on `spec`."""
        import subprocess

        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            return False, f"refusing to signal invalid PID {pid!r}"
        sig_u = sig.upper()
        if sig_u not in ("TERM", "KILL", "INT", "HUP"):
            return False, f"unsupported signal {sig!r}"
        if self.opt.multiplex and self._control_ready and not self.opt.control_dir.is_dir():
            try:
                self.opt.control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(self.opt.control_dir, 0o700)
            except OSError:
                pass
        cmd = f"kill -s {sig_u} {pid}\n"
        try:
            cp = subprocess.run(
                self.exec_argv(spec),
                input=cmd,
                capture_output=True,
                text=True,
                timeout=max(2.0, self.opt.command_timeout),
            )
        except subprocess.TimeoutExpired:
            return False, f"kill timed out on {spec.name} PID {pid}"
        except OSError as exc:
            return False, f"kill spawn failed on {spec.name}: {exc}"
        if cp.returncode == 0:
            return True, f"sent SIG{sig_u} to {spec.name} PID {pid}"
        detail = (cp.stderr.strip() or cp.stdout.strip() or f"rc={cp.returncode}").splitlines()[0]
        return False, f"kill failed on {spec.name} PID {pid}: {detail}"

    def shutdown(self) -> None:
        """Deliberately leave the control masters and their directory alone.

        This used to unlink every cm-* socket and rmtree the control
        directory. But the directory is shared by every gpumaster process for
        the same user, so a one-shot `gpumaster free` / `--json` exiting
        deleted the sockets out from under a long-running TUI: every later
        poll in that TUI failed hard with `unix_listener: cannot bind ...
        No such file or directory` (rc=255) and the whole fleet sat at
        HOST DOWN until a restart (observed live 2026-09-02).

        No cleanup is needed. A master exits on its own ControlPersist
        seconds after its last client and removes its own socket; a stale
        socket left by a crash makes `ControlMaster=auto` fall back to a
        plain direct connection, not fail.
        """
        return


def probe_capabilities(spec: HostSpec, options: SshOptions, script_path: Path) -> dict:
    """Blocking one-shot preflight for `gpumaster check <host>`."""
    import subprocess

    poller = Poller(options, script_path)
    poller.preflight()
    argv = poller.exec_argv(spec)

    started = time.monotonic()
    try:
        cp = subprocess.run(
            argv, input=script_path.read_text(), capture_output=True,
            text=True, timeout=options.command_timeout + 4,
        )
        rc, out, err = cp.returncode, cp.stdout, cp.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", "timed out"
    latency = (time.monotonic() - started) * 1000.0

    host = Host(name=spec.name, target=spec.target)
    parse_payload(host, out, err, rc, latency)
    return {
        "host": host,
        "rc": rc,
        "stderr": err.strip(),
        "latency_ms": latency,
        "ssh_argv": argv,
    }
