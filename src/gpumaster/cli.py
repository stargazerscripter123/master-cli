#!/usr/bin/env python3
"""gpumaster -- one screen for every GPU in the fleet, over plain SSH.

Nothing is installed on the GPU nodes: `remote.sh` is piped to `bash -s` over a
multiplexed OpenSSH connection. Requirements on a node are ssh, bash and
either nvidia-smi + Linux /proc, or -- on an Apple-silicon Mac -- the stock
ioreg / sysctl / vm_stat.

    gpumaster                 live TUI
    gpumaster --once          one static screen to stdout, then exit
    gpumaster --json          one structured snapshot as JSON
    gpumaster free            which GPUs are worth grabbing right now
    gpumaster serve           zero-dependency HTTP / ANSI / JSON / Prometheus server
    gpumaster --mobile        one mode from hosts.json; also --desktop, --laptop
    gpumaster --all           every host, hidden ones included
    gpumaster --mode NAME     any mode in hosts.json (no flag: "default")
    gpumaster check <host>    one-shot capability preflight for one machine
    gpumaster --probe <host>  raw remote payload (save it as a test fixture)
    gpumaster --replay DIR    render from captured fixtures, no network
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent

from .config import EXAMPLE_CONFIG, Config, load_config  # noqa: E402
from .model import (  # noqa: E402
    Fleet,
    Host,
    HostState,
    gpu_alerts,
    gpu_free_mib,
    host_alerts,
    is_stale_proc,
    select_free_gpus,
)
from .parse import parse_payload  # noqa: E402
from .render import (  # noqa: E402
    GLYPH_SETS,
    GLYPHS_ASCII,
    GLYPHS_BLOCK,
    fleet_to_prometheus,
    lines_to_ansi,
    lines_to_text,
    render_fleet,
    render_free,
    set_ambiguous_wide,
)
from .transport import HostSpec, Poller, probe_capabilities  # noqa: E402

SCRIPT = HERE / "remote.sh"
VERSION = "0.3.0"


# ---------------------------------------------------------------------------
# Environment guards
# ---------------------------------------------------------------------------

def check_interpreter() -> Optional[str]:
    if sys.version_info < (3, 10):
        return f"gpumaster needs Python 3.10+, running {sys.version.split()[0]}"
    # /usr/bin/python3 on this Mac is an xcode-select shim that aborts with a
    # dyld symbol error. Fail with an explanation rather than a stack trace.
    if sys.executable.startswith("/usr/bin/python") and sys.platform == "darwin":
        return (
            "/usr/bin/python3 on macOS is an xcode-select shim and is often "
            "broken; run with /opt/homebrew/bin/python3 instead"
        )
    return None


def cjk_locale() -> bool:
    ctype = (os.environ.get("LC_ALL") or os.environ.get("LC_CTYPE")
             or os.environ.get("LANG") or "")
    return any(tag in ctype.lower() for tag in ("zh", "ja", "ko", "cjk"))


def pick_glyphs(name: str):
    """Choose bar glyphs and tell the renderer how wide Ambiguous chars are.

    Block-drawing characters (and, less obviously, U+2026 HORIZONTAL ELLIPSIS)
    are East-Asian-Width Ambiguous: single-width in a Western font, double-width
    under a CJK one. Getting that wrong shifts every column on the line.
    U+2591 LIGHT SHADE and the braille block are Neutral and always safe.
    """
    wide = cjk_locale()
    if name == "block":
        wide = False          # explicit override: trust the user's terminal
    elif name == "ascii":
        wide = True           # be conservative when asked for the safe set
    set_ambiguous_wide(wide)

    if name in GLYPH_SETS:
        return GLYPH_SETS[name]
    return GLYPHS_ASCII if cjk_locale() else GLYPHS_BLOCK


def terminal_size(default=(150, 50)) -> tuple[int, int]:
    try:
        sz = os.get_terminal_size()
        return sz.columns, sz.lines
    except OSError:
        return default


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def build_poller(cfg: Config, multiplex: Optional[bool] = None,
                 control_dir: Optional[Path] = None) -> tuple[Poller, Optional[str]]:
    opts = cfg.ssh
    if multiplex is not None:
        opts.multiplex = multiplex
    if control_dir is not None:
        opts.control_dir = control_dir
    poller = Poller(opts, SCRIPT)
    return poller, poller.preflight()


def collect_once(
    cfg: Config,
    poller: Poller,
    specs: Optional[list[HostSpec]] = None,
) -> Fleet:
    hosts = asyncio.run(poller.poll_all(specs if specs is not None else cfg.active_hosts()))
    return Fleet(hosts=hosts)


def _read_text(path: Path) -> str:
    """Read a capture without ever raising on odd bytes.

    A capture directory can pick up junk -- notably macOS AppleDouble `._name`
    files, which are binary, match `*.txt`, and made this crash with a
    UnicodeDecodeError after a tarball round trip.
    """
    try:
        return path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""


def load_replay(directory: Path, cfg: Optional[Config] = None) -> Fleet:
    """Rebuild a Fleet from `--probe` captures so layout work needs no network."""
    captures = {
        p.stem: p for p in directory.glob("*.txt")
        if not p.name.startswith("._")
    }
    # Follow the configured host order when it is available; a plain glob sorts
    # alphabetically and would put the down host first.
    if cfg and cfg.hosts:
        order = [s.target for s in cfg.active_hosts() if s.target in captures]
        order += [n for n in sorted(captures) if n not in order]
    else:
        order = sorted(captures)

    fleet = Fleet()
    for name in order:
        txt = captures[name]
        err_file = txt.with_suffix(".stderr")
        err = _read_text(err_file) if err_file.exists() else ""
        out = _read_text(txt)
        rc = 0 if out.strip() else 255
        fleet.hosts.append(parse_payload(Host(name=name, target=name), out, err, rc, 0.0))
    return fleet


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def fleet_to_json(fleet: Fleet) -> list[dict]:
    out = []
    for h in fleet.hosts:
        m = h.mem
        out.append({
            "name": h.name,
            "target": h.target,
            "state": h.state.name.lower(),
            "online": h.online,
            "hostname": h.hostname,
            "arch": h.arch,
            "kernel": h.kernel,
            "ncpu": h.ncpu,
            "load": [h.load1, h.load5, h.load15],
            "uptime_seconds": h.uptime_s,
            "error": ({"code": h.error_code, "message": h.error_msg}
                      if h.error_code else None),
            "latency_ms": h.latency_ms,
            "remote_ms": h.remote_ms,
            "memory_kib": {
                "total": m.total_kib, "available": m.available_kib,
                "used": m.used_kib, "free": m.free_kib,
                "cached": m.cached_kib, "buffers": m.buffers_kib,
                "swap_total": m.swap_total_kib, "swap_used": m.swap_used_kib,
            },
            "disk_root_kib": {
                "total": m.disk_total_kib,
                "available": m.disk_avail_kib,
            },
            "psi": {
                "memory": {"some_avg10": h.psi_mem.some10,
                           "full_avg10": h.psi_mem.full10},
                "cpu": {"some_avg10": h.psi_cpu.some10},
                "io": {"some_avg10": h.psi_io.some10,
                       "full_avg10": h.psi_io.full10},
            },
            "net_links": [{
                "name": nl.name,
                "operstate": nl.operstate,
                "up": nl.up,
                "speed_mbps": nl.speed_mbps,
                "speed_gbps": nl.speed_gbps,
                "mtu": nl.mtu,
                "rx_bytes": nl.rx_bytes,
                "tx_bytes": nl.tx_bytes,
                "rdma_dev": nl.rdma_dev or None,
                "rx_gbps": nl.rx_gbps,
                "tx_gbps": nl.tx_gbps,
                "total_gbps": nl.total_gbps,
            } for nl in h.net_links],
            "alerts": [{"level": a.level.name, "tag": a.tag, "detail": a.detail}
                       for a in host_alerts(h)],
            "gpus": [{
                "index": g.index,
                "name": g.name,
                "uuid": g.uuid,
                "bus_id": g.bus_id,
                "memory_kind": g.mem_kind.value,
                # On a unified part this is derived from per-process NVML sums
                # and is a SUBSET of host used memory, never an addition to it.
                "gpu_resident_mib": g.gpu_resident_mib,
                "gpu_resident_derived": g.mem_derived,
                # Framebuffer figures, straight from NVML. Null on a unified
                # part, where NVML returns NOT_SUPPORTED -- including
                # memory_percent, which means framebuffer occupancy only.
                "memory_total_mib": g.mem_total_mib,
                "memory_used_mib": g.mem_used_mib,
                "memory_percent": g.mem_percent,
                # The shared pool, unified parts only. `source` travels with
                # the numbers so a consumer cannot mistake the denominator for
                # a framebuffer. gpu_resident + other_used == used <= total.
                "unified_pool_mib": None if not g.is_unified else {
                    "total": g.uma_total_mib,
                    "used": g.pool_used_mib,
                    "available": g.uma_available_mib,
                    "gpu_resident": g.gpu_resident_mib,
                    "other_used": g.pool_other_mib,
                    "source": ("sysctl hw.memsize / vm_stat free+inactive+speculative"
                               if g.is_apple else "/proc/meminfo MemTotal/MemAvailable"),
                },
                "unified_pool_percent": g.pool_percent,
                # null rather than 0 when the value is not believable.
                "utilization_gpu": g.util_gpu if g.util_trustworthy else None,
                "utilization_gpu_raw": g.util_gpu,
                "utilization_trustworthy": g.util_trustworthy,
                "utilization_memory": g.util_mem if g.util_trustworthy else None,
                "temperature_c": g.temp_c,
                "thermal_margin_c": g.temp_limit_c,
                "fan_percent": g.fan_percent,
                "power_w": g.power_w,
                "power_limit_w": g.power_limit_w,
                "ane_power_w": g.ane_power_w,
                "clock_sm_mhz": g.clock_sm,
                "clock_sm_max_mhz": g.clock_sm_max,
                "pstate": g.pstate,
                "pcie": None if g.is_unified else {
                    "gen": g.pcie_gen, "gen_max": g.pcie_gen_max,
                    "width": g.pcie_width, "width_max": g.pcie_width_max,
                },
                "clock_events": [{"tag": s, "name": n, "alarm": a}
                                 for s, n, a in g.throttle_badges],
                "alerts": [{"level": a.level.name, "tag": a.tag, "detail": a.detail}
                           for a in gpu_alerts(g)],
                "processes": [{
                    "pid": p.pid, "type": p.ptype, "user": p.user or None,
                    "container": p.container or None,
                    "gpu_memory_mib": p.gpu_mem_mib,
                    "cpu_percent": p.cpu_percent,
                    "rss_kib": p.rss_kib,
                    "elapsed": p.etime or None,
                    "elapsed_seconds": p.etime_seconds,
                    "stale": is_stale_proc(g, p),
                    "command": p.display_command,
                    "resolved": p.resolved,
                } for p in g.procs],
            } for g in h.gpus],
        })
    return out


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def _find_spec(cfg: Config, target: str) -> HostSpec:
    for h in cfg.hosts:
        if h.name == target or h.target == target:
            return h
    return HostSpec(name=target, target=target)


def cmd_probe(cfg: Config, target: str, poller: Poller) -> int:
    import subprocess

    spec = _find_spec(cfg, target)
    cp = subprocess.run(poller.exec_argv(spec), input=SCRIPT.read_text(),
                        capture_output=True, text=True)
    sys.stdout.write(cp.stdout)
    if cp.stderr.strip():
        sys.stderr.write(cp.stderr)
    return cp.returncode


def cmd_check(cfg: Config, target: str) -> int:
    spec = _find_spec(cfg, target)
    res = probe_capabilities(spec, cfg.ssh, SCRIPT)
    h: Host = res["host"]

    print(f"{spec.name}  ->  {spec.target}")
    print(f"  ssh            rc={res['rc']}  {res['latency_ms']:.0f} ms round trip")
    if h.state is HostState.DOWN:
        print(f"  UNREACHABLE    {h.error_msg}")
        for line in (res["stderr"] or "").splitlines()[:4]:
            print(f"                 {line}")
        return 1

    print(f"  host           {h.hostname}  {h.arch}  {h.ncpu} cpu  kernel {h.kernel}")
    m = h.mem
    if m.total_kib:
        print(f"  memory         {(m.used_kib or 0)/1048576:.0f}/"
              f"{m.total_kib/1048576:.0f} GiB used, "
              f"{(m.available_kib or 0)/1048576:.0f} GiB available, "
              f"{(m.swap_used_kib or 0)/1048576:.1f} GiB swapped")
    print(f"  PSI            {'yes' if h.psi_mem.some10 is not None else 'NO'}"
          f"   (memory some avg10={h.psi_mem.some10})")
    if h.net_links:
        rails = ", ".join(
            f"{nl.name}{'/' + nl.rdma_dev if nl.rdma_dev else ''} "
            f"{(str(nl.speed_gbps) + 'G') if (nl.up and nl.speed_gbps) else nl.operstate}"
            f"{(' mtu=' + str(nl.mtu)) if nl.mtu else ''}"
            for nl in h.net_links
        )
        print(f"  fabric         {h.hs_fabric_summary()}  ({rails})")

    if h.state is HostState.GPU_DEAD:
        tool = "ioreg" if h.error_code == "no-accelerator" else "nvidia-smi"
        print(f"  {tool:<14} FAILED: {h.error_code}")
        print(f"                 {h.error_msg}")
        print("  verdict        host usable for CPU/RAM only; renders as [!]")
        return 2

    tool = "ioreg" if h.fieldset == "apple" else "nvidia-smi"
    print(f"  {tool:<14} ok, field set '{h.fieldset}'")
    print(f"  remote sample  {h.remote_ms} ms")
    if h.fieldset == "apple":
        print("  processes      not sampled (per-process GPU time needs powermetrics, i.e. root)")
    else:
        print(f"  processes      {h.ps_requested} PIDs from NVML, "
              f"{h.ps_resolved} resolved by ps"
              + ("   *** PID namespace mismatch ***" if h.pid_ns_mismatch else ""))
    print(f"  GPUs           {len(h.gpus)}: {h.gpu_label}")
    for g in h.gpus:
        if not g.is_unified:
            kind = f"{g.mem_total_mib:.0f} MiB framebuffer"
        elif g.uma_total_mib:
            kind = (f"unified (UMA), {g.uma_total_mib/1024:.1f} GiB pool, "
                    f"{(g.gpu_resident_mib or 0)/1024:.1f} GiB GPU-resident")
        else:
            kind = "unified (UMA)"
        print(f"    [{g.index}] {g.name}  {kind}  driver {g.driver}")
    if h.has_unified and any(g.is_apple for g in h.gpus):
        has_mm = any(
            g.temp_c is not None or g.power_w is not None or g.clock_sm is not None
            for g in h.gpus
        )
        print("  note           Apple silicon: GPU utilization and resident memory are")
        print("                 read from ioreg's IOAccelerator counters, the pool from")
        if has_mm:
            print("                 sysctl/vm_stat, and temp/power/clocks from macmon.")
            print("                 Per-process GPU attribution still needs powermetrics (root).")
        else:
            print("                 sysctl/vm_stat. Temperature, power and per-process GPU")
            print("                 attribution need powermetrics (root) and are not shown")
            print("                 (optional: `brew install macmon` unlocks rootless temp/power).")
        tag = "apple"
    elif h.has_unified:
        print("  note           unified-memory part: there is no framebuffer. NVML")
        print("                 returns NOT_SUPPORTED for memory info and BAR1, so")
        print("                 GPU memory is the sum of per-process NVML sums and")
        print("                 the pool total/used come from /proc/meminfo -- the")
        print("                 same numbers nvtop shows here. utilization.gpu is '?'")
        tag = "gb10"
    else:
        tag = " ".join(spec.tags)
    print("\n  suggested hosts.json entry:")
    print(f'    {{"name": "{spec.name}", "target": "{spec.target}"'
          + (f', "tags": ["{tag}"]' if tag else "") + "}")
    return 0


def cmd_free(
    fleet: Fleet,
    width: int,
    min_mem_gib: float = 0.0,
    count: int = 1,
    fmt: str = "table",
    host_filter: str = "",
) -> int:
    import shlex

    if host_filter:
        matched = [
            h for h in fleet.hosts
            if h.name == host_filter or h.target == host_filter
        ]
        if not matched:
            print(f"gpumaster free: unknown host {host_filter!r}", file=sys.stderr)
            return 2
        fleet = Fleet(hosts=matched)

    req_count = max(1, count)
    cuda_only = fmt in ("env", "ssh")
    selected = select_free_gpus(
        fleet, min_mem_gib=min_mem_gib, count=req_count, cuda_only=cuda_only
    )
    if fmt == "json":
        hosts_map = {h.name: h for h in fleet.hosts}
        payload = []
        for hname, gpus in selected.items():
            h = hosts_map.get(hname)
            chosen = sorted(
                sorted(gpus, key=lambda g: (-(gpu_free_mib(g) or 0.0), g.index))[:req_count],
                key=lambda g: g.index,
            )
            is_apple_host = bool(chosen) and all(g.is_apple for g in chosen)
            payload.append({
                "host": hname,
                "target": h.target if h else hname,
                "cuda_visible_devices": (
                    None if is_apple_host else ",".join(str(g.index) for g in chosen)
                ),
                "available_indices": [g.index for g in gpus],
                "gpus": [{
                    "index": g.index,
                    "name": g.name,
                    "free_mib": gpu_free_mib(g),
                    "unified": g.is_unified,
                } for g in gpus],
            })
        print(json.dumps(payload, indent=2))
        return 0 if selected else 1

    if fmt in ("env", "ssh"):
        if not selected:
            print(
                f"gpumaster free: no CUDA host has >= {req_count} idle GPU(s) "
                f"with >= {max(0.0, min_mem_gib):g} GiB free",
                file=sys.stderr,
            )
            return 1
        best_host, best_gpus = next(iter(selected.items()))
        chosen = sorted(
            sorted(best_gpus, key=lambda g: (-(gpu_free_mib(g) or 0.0), g.index))[:req_count],
            key=lambda g: g.index,
        )
        devs = ",".join(str(g.index) for g in chosen)
        if fmt == "env":
            print(f"CUDA_VISIBLE_DEVICES={devs}")
            return 0
        h_obj = next((h for h in fleet.hosts if h.name == best_host), None)
        target = h_obj.target if h_obj else best_host
        print(f"ssh {shlex.quote(target)} CUDA_VISIBLE_DEVICES={devs}")
        return 0

    for line in lines_to_text(
        render_free(fleet, width, min_mem_gib=min_mem_gib, count=req_count)
    ):
        print(line.rstrip())
    if not selected and (min_mem_gib > 0.0 or count > 1):
        return 1
    return 0


def print_screen(fleet: Fleet, width: int, height: int, glyphs, args,
                 mode: str = "") -> None:
    lines = render_fleet(fleet, width, height, glyphs,
                         hide_idle=args.hide_idle,
                         show_procs=not args.no_procs,
                         clock=time.strftime("%H:%M:%S"),
                         mode=mode)
    for line in lines_to_text(lines):
        print(line.rstrip())


def build_serve_response(
    path: str,
    fleet: Fleet,
    width: int,
    height: int,
    glyphs,
    args,
    mode: str = "",
    history: Optional[dict] = None,
    refresh_seconds: float = 2.0,
) -> tuple[int, str, bytes]:
    """Pure HTTP response builder for `gpumaster serve` (unit-testable without sockets)."""
    import html as html_mod

    clean_path = path.split("?", 1)[0]
    if clean_path == "/fleet.txt":
        lines = render_fleet(
            fleet,
            width,
            height,
            glyphs,
            hide_idle=getattr(args, "hide_idle", False),
            show_procs=not getattr(args, "no_procs", False),
            history=history,
            clock=time.strftime("%H:%M:%S"),
            mode=mode,
        )
        body = ("\n".join(line.rstrip() for line in lines_to_text(lines)) + "\n").encode("utf-8")
        return 200, "text/plain; charset=utf-8", body

    if clean_path == "/fleet.ansi":
        lines = render_fleet(
            fleet,
            width,
            height,
            glyphs,
            hide_idle=getattr(args, "hide_idle", False),
            show_procs=not getattr(args, "no_procs", False),
            history=history,
            clock=time.strftime("%H:%M:%S"),
            mode=mode,
        )
        body = ("\n".join(lines_to_ansi(lines)) + "\n").encode("utf-8")
        return 200, "text/plain; charset=utf-8", body

    if clean_path == "/api/fleet.json":
        body = (json.dumps(fleet_to_json(fleet), indent=2) + "\n").encode("utf-8")
        return 200, "application/json; charset=utf-8", body

    if clean_path == "/metrics":
        body = fleet_to_prometheus(fleet).encode("utf-8")
        return 200, "text/plain; version=0.0.4; charset=utf-8", body

    if clean_path in ("/", "/index.html"):
        lines = render_fleet(
            fleet,
            width,
            height,
            glyphs,
            hide_idle=getattr(args, "hide_idle", False),
            show_procs=not getattr(args, "no_procs", False),
            history=history,
            clock=time.strftime("%H:%M:%S"),
            mode=mode,
        )
        txt = html_mod.escape("\n".join(line.rstrip() for line in lines_to_text(lines)))
        refresh_int = max(1, int(round(refresh_seconds)))
        doc = (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            f"<meta http-equiv=\"refresh\" content=\"{refresh_int}\">"
            "<title>gpumaster</title>"
            "<style>"
            "body{background:#0d1117;color:#e6edf3;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;margin:16px;}"
            "nav{margin-bottom:12px;font-size:13px;}nav a{color:#58a6ff;margin-right:16px;text-decoration:none;}"
            "pre{font-size:13px;line-height:1.35;overflow-x:auto;}"
            "</style></head><body>"
            "<nav>"
            "<strong>gpumaster</strong> &middot; "
            "<a href=\"/fleet.txt\">/fleet.txt</a>"
            "<a href=\"/fleet.ansi\">/fleet.ansi</a>"
            "<a href=\"/api/fleet.json\">/api/fleet.json</a>"
            "<a href=\"/metrics\">/metrics (Prometheus)</a>"
            "</nav>"
            f"<pre>{txt}</pre>"
            "</body></html>\n"
        )
        return 200, "text/html; charset=utf-8", doc.encode("utf-8")

    return 404, "text/plain; charset=utf-8", b"not found\n"


def cmd_serve(
    cfg: Config,
    poller: Optional[Poller],
    glyphs,
    width: int,
    height: int,
    args,
    static_fleet: Optional[Fleet] = None,
) -> int:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from .tui import Collector

    collector: Optional[Collector] = None
    if static_fleet is None:
        collector = Collector(cfg, poller)
        collector.start()
    try:
        if collector is not None:
            # Wait briefly for the first poll so immediate scrapes get populated data
            deadline = time.monotonic() + min(2.0, cfg.ssh.command_timeout)
            while time.monotonic() < deadline:
                if collector.snapshot()[2] > 0:
                    break
                time.sleep(0.05)

        class Handler(BaseHTTPRequestHandler):
            def _respond(self, send_body: bool) -> None:
                if collector is not None:
                    fleet, hist, _gen, _ms = collector.snapshot()
                else:
                    fleet, hist = static_fleet or Fleet(), None
                status, ctype, body = build_serve_response(
                    self.path,
                    fleet,
                    width,
                    height,
                    glyphs,
                    args,
                    mode=cfg.mode_label,
                    history=hist,
                    refresh_seconds=cfg.refresh_seconds,
                )
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if send_body:
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            def do_GET(self) -> None:
                self._respond(send_body=True)

            def do_HEAD(self) -> None:
                self._respond(send_body=False)

            def log_message(self, format: str, *log_args) -> None:
                return  # keep stdout/stderr clean

        try:
            httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
            httpd.daemon_threads = True
        except OSError as exc:
            print(f"gpumaster serve: cannot bind {args.bind}:{args.port}: {exc}", file=sys.stderr)
            return 1

        print(
            f"gpumaster serve listening on http://{args.bind}:{args.port} "
            "(/, /fleet.txt, /fleet.ansi, /api/fleet.json, /metrics)",
            file=sys.stderr,
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
    finally:
        if collector is not None:
            collector.shutdown()
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="gpumaster", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="tui",
                   choices=["tui", "free", "check", "init", "serve"],
                   help="sub-command (default: tui)")
    p.add_argument("arg", nargs="?", help="argument for the sub-command")
    p.add_argument("-c", "--config", type=Path, help="path to hosts.json")
    p.add_argument("-i", "--interval", type=float, help="refresh seconds")
    p.add_argument("--once", action="store_true", help="print one screen and exit")
    p.add_argument("--json", action="store_true", help="print one JSON snapshot")
    p.add_argument("--probe", metavar="HOST", help="dump one raw remote payload")
    p.add_argument("--replay", metavar="DIR", type=Path,
                   help="render from captured fixtures instead of the network")
    p.add_argument("--width", type=int, help="force a render width")
    p.add_argument("--height", type=int, help="force a render height")
    p.add_argument("--glyphs", default="auto", choices=["auto", "block", "ascii"])
    p.add_argument("--no-controlmaster", action="store_true",
                   help="disable SSH connection multiplexing")
    p.add_argument("--control-dir", type=Path, help="directory for control sockets")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--mode", metavar="NAME",
                       help="show one host set from \"modes\" in hosts.json")
    for name in ("mobile", "desktop", "laptop", "all"):
        modes.add_argument(f"--{name}", dest="mode", action="store_const",
                           const=name, help=f"same as --mode {name}")
    p.add_argument("--hide-idle", action="store_true", help="hide idle GPUs")
    p.add_argument("--no-procs", action="store_true", help="hide the process table")
    p.add_argument("--min-mem", type=float, default=0.0, metavar="GIB",
                   help="minimum free memory per GPU in GiB (for `gpumaster free`)")
    p.add_argument("-n", "--count", type=int, default=1, metavar="N",
                   help="number of free GPUs required on one host (for `gpumaster free`)")
    p.add_argument("--format", dest="free_format", default="table",
                   choices=["table", "env", "ssh", "json"],
                   help="output format for `gpumaster free` (default: table)")
    p.add_argument("--bind", default="127.0.0.1", metavar="ADDR",
                   help="bind address for `gpumaster serve` (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765, metavar="PORT",
                   help="TCP port for `gpumaster serve` (default: 8765)")
    p.add_argument("--version", action="version", version=f"gpumaster {VERSION}")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    err = check_interpreter()
    if err:
        print(f"gpumaster: {err}", file=sys.stderr)
        return 2

    if args.min_mem < 0:
        print("gpumaster: --min-mem must be >= 0", file=sys.stderr)
        return 2
    if args.count < 1:
        print("gpumaster: --count must be >= 1", file=sys.stderr)
        return 2
    if args.port < 1 or args.port > 65535:
        print("gpumaster: --port must be between 1 and 65535", file=sys.stderr)
        return 2

    if args.command == "init":
        dest = args.config or (Path.home() / ".config" / "gpumaster" / "hosts.json")
        if dest.exists():
            print(f"{dest} already exists", file=sys.stderr)
            return 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(EXAMPLE_CONFIG)
        print(f"wrote {dest}")
        return 0

    cfg = load_config(args.config)
    for w in cfg.warnings:
        print(f"gpumaster: {w}", file=sys.stderr)
    if args.interval:
        cfg.refresh_seconds = max(0.2, args.interval)
    if args.mode:
        if args.mode not in cfg.mode_names:
            print(f"gpumaster: no mode {args.mode!r} in hosts.json; modes: "
                  + ", ".join(cfg.mode_names), file=sys.stderr)
            return 2
        cfg.mode = args.mode

    glyphs = pick_glyphs(args.glyphs)
    tw, th = terminal_size()
    width = args.width or tw
    height = args.height or th

    # ---- offline paths --------------------------------------------------
    if args.replay:
        fleet = load_replay(args.replay, cfg)
        if args.command == "serve":
            return cmd_serve(cfg, None, glyphs, width, height, args, static_fleet=fleet)
        if args.command == "free":
            fmt = "json" if args.json else args.free_format
            return cmd_free(fleet, width, args.min_mem, args.count, fmt, args.arg or "")
        if args.json:
            print(json.dumps(fleet_to_json(fleet), indent=2))
            return 0
        print_screen(fleet, width, height, glyphs, args, cfg.mode_label)
        return 0

    free_specs: Optional[list[HostSpec]] = None
    if args.command == "free" and args.arg:
        free_specs = [
            h for h in cfg.hosts
            if h.name == args.arg or h.target == args.arg
        ]
        if not free_specs:
            print(f"gpumaster free: unknown host {args.arg!r}", file=sys.stderr)
            return 2

    # An explicit host argument (check, --probe, free <host>) reaches it whatever the mode.
    if (
        not cfg.active_hosts()
        and args.command != "check"
        and not args.probe
        and not free_specs
    ):
        if cfg.hosts:
            print(f"gpumaster: mode {cfg.mode!r} has no hosts; modes: "
                  + ", ".join(cfg.mode_names), file=sys.stderr)
        else:
            print("gpumaster: no hosts configured; try `gpumaster init`", file=sys.stderr)
        return 2

    if args.command == "check":
        if not args.arg:
            print("usage: gpumaster check <host>", file=sys.stderr)
            return 2
        return cmd_check(cfg, args.arg)

    # ---- transport ------------------------------------------------------
    poller, perr = build_poller(
        cfg,
        False if args.no_controlmaster else None,
        args.control_dir,
    )
    if perr:
        # The macOS 104-byte ControlPath limit is a hard ssh failure on every
        # host, so refuse to start rather than connect nowhere.
        print(f"gpumaster: {perr}", file=sys.stderr)
        return 2

    try:
        if args.probe:
            return cmd_probe(cfg, args.probe, poller)

        if args.command == "serve":
            return cmd_serve(cfg, poller, glyphs, width, height, args)

        if args.command == "free" or args.once or args.json:
            fleet = collect_once(cfg, poller, specs=free_specs)
            if args.command == "free":
                fmt = "json" if args.json else args.free_format
                return cmd_free(fleet, width, args.min_mem, args.count, fmt, args.arg or "")
            if args.json:
                print(json.dumps(fleet_to_json(fleet), indent=2))
                return 0
            print_screen(fleet, width, height, glyphs, args, cfg.mode_label)
            return 0

        from .tui import run_tui
        return run_tui(cfg, poller, glyphs, args)
    finally:
        poller.shutdown()


if __name__ == "__main__":
    sys.exit(main())
