"""Every nvidia-smi / ps / proc format assumption in gpumaster lives here.

Pure functions, no I/O. That makes each `--probe` capture a regression fixture
and lets the whole data layer be developed with the fleet unplugged.

Notes that cost real debugging time, kept as comments so they are not
rediscovered:

  * nvidia-smi emits BOTH `N/A` and `[N/A]` depending on the field. Both mean
    "no value" and must become None, never 0.
  * `--query-gpu` CSV separates with ", " (comma space). Splitting on "," alone
    leaves leading spaces on every field.
  * Fields are looked up BY NAME using the `fields` header the remote sent, so
    a driver that drops or reorders one degrades a single cell.
  * The process join key is pci.bus_id, taken verbatim. GB10's is
    `0000000F:01:00.0` with an uppercase hex domain; case-folding or
    zero-stripping it breaks the join.
  * ps returns rows sorted by PID regardless of the order given to -p, and
    silently drops unresolvable PIDs with rc=0. Join on PID, and compare the
    requested count against the rows received.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from .model import KIB_PER_MIB, Gpu, Host, HostMem, HostState, NetLink, Proc, Psi

NA_TOKENS = {"n/a", "[n/a]", "[not supported]", "not supported", "unknown", "-", ""}


def _clean(tok: str) -> str:
    return tok.strip()


def num(tok: Optional[str]) -> Optional[float]:
    """Parse a numeric field, mapping every not-available spelling to None."""
    if tok is None:
        return None
    t = _clean(tok)
    if t.lower() in NA_TOKENS:
        return None
    # nvidia-smi occasionally appends units even under nounits; macmon JSON may
    # emit scientific notation (e.g. 1.5258789e-5) for idle microwatt values.
    m = re.match(r"^[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?", t)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def integer(tok: Optional[str]) -> Optional[int]:
    v = num(tok)
    return int(v) if v is not None else None


def text(tok: Optional[str]) -> str:
    if tok is None:
        return ""
    t = _clean(tok)
    return "" if t.lower() in NA_TOKENS else t


def hexmask(tok: Optional[str]) -> Optional[int]:
    if tok is None:
        return None
    t = _clean(tok)
    if t.lower() in NA_TOKENS:
        return None
    try:
        return int(t, 16)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Section splitting
# --------------------------------------------------------------------------

SECTION_RE = re.compile(r"^__([A-Z]+)__(?:\t(.*))?$")


def split_sections(payload: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Split the remote output into `{SECTION: [lines]}` plus marker arguments."""
    sections: dict[str, list[str]] = {}
    args: dict[str, str] = {}
    current: Optional[str] = None
    for raw in payload.splitlines():
        m = SECTION_RE.match(raw)
        if m:
            current = m.group(1)
            sections.setdefault(current, [])
            if m.group(2) is not None:
                args[current] = m.group(2)
            continue
        if current is not None:
            sections[current].append(raw)
    return sections, args


def _kv(lines: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        if "\t" not in line:
            continue
        k, _, v = line.partition("\t")
        out[k] = v
    return out


# --------------------------------------------------------------------------
# Individual sections
# --------------------------------------------------------------------------

def parse_host(lines: list[str], host: Host) -> None:
    kv = _kv(lines)
    host.hostname = kv.get("hostname", "")
    host.arch = kv.get("arch", "")
    host.kernel = kv.get("kernel", "")
    host.ncpu = integer(kv.get("ncpu"))
    host.uptime_s = integer(kv.get("uptime"))

    load = kv.get("loadavg", "").split()
    if len(load) >= 3:
        host.load1, host.load5, host.load15 = (num(load[0]), num(load[1]), num(load[2]))

    m = HostMem()
    m.total_kib = integer(kv.get("mem.MemTotal"))
    m.free_kib = integer(kv.get("mem.MemFree"))
    m.available_kib = integer(kv.get("mem.MemAvailable"))
    m.buffers_kib = integer(kv.get("mem.Buffers"))
    m.cached_kib = integer(kv.get("mem.Cached"))
    m.sreclaimable_kib = integer(kv.get("mem.SReclaimable"))
    m.shmem_kib = integer(kv.get("mem.Shmem"))
    m.dirty_kib = integer(kv.get("mem.Dirty"))
    m.swap_total_kib = integer(kv.get("mem.SwapTotal"))
    m.swap_free_kib = integer(kv.get("mem.SwapFree"))
    dt = integer(kv.get("disk.total"))
    da = integer(kv.get("disk.avail"))
    m.disk_total_kib = dt if (dt is not None and dt > 0) else None
    m.disk_avail_kib = da if (da is not None and da >= 0) else None
    host.mem = m


def parse_psi(lines: list[str], host: Host) -> None:
    """Lines look like `memory.some\tavg10=0.00\tavg60=0.00\tavg300=0.00`."""
    buckets = {"memory": Psi(), "cpu": Psi(), "io": Psi()}
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        key = parts[0]
        res, _, kind = key.partition(".")
        psi = buckets.get(res)
        if psi is None or kind not in ("some", "full"):
            continue
        vals = {}
        for p in parts[1:]:
            name, _, v = p.partition("=")
            vals[name] = num(v)
        if kind == "some":
            psi.some10, psi.some60, psi.some300 = (
                vals.get("avg10"), vals.get("avg60"), vals.get("avg300"))
        else:
            psi.full10, psi.full60, psi.full300 = (
                vals.get("avg10"), vals.get("avg60"), vals.get("avg300"))
    host.psi_mem = buckets["memory"]
    host.psi_cpu = buckets["cpu"]
    host.psi_io = buckets["io"]


def parse_net(lines: list[str], host: Host) -> None:
    """Parse `__NET__` rows: `if\\t<name>\\t<operstate>\\t<speed>\\t<mtu>\\t<rx>\\t<tx>\\t<rdma>`."""
    links: list[NetLink] = []
    for line in lines:
        if not line.startswith("if\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        name = _clean(parts[1])
        if not name:
            continue
        operstate = _clean(parts[2]).lower() or "unknown"
        raw_spd = integer(parts[3])
        # Linux sysfs reports -1, 65535 (uint16 -1), or 4294967295 (uint32 -1)
        # when link speed is unknown or carrier is down.
        speed_mbps = (
            raw_spd
            if raw_spd is not None
            and 0 < raw_spd <= 1600000
            and raw_spd != 65535
            else None
        )
        raw_mtu = integer(parts[4])
        mtu = raw_mtu if raw_mtu is not None and raw_mtu > 0 else None
        raw_rx = integer(parts[5])
        rx_bytes = raw_rx if raw_rx is not None and raw_rx >= 0 else None
        raw_tx = integer(parts[6])
        tx_bytes = raw_tx if raw_tx is not None and raw_tx >= 0 else None
        rdma_dev = _clean(parts[7]) if len(parts) >= 8 else ""
        links.append(
            NetLink(
                name=name,
                operstate=operstate,
                speed_mbps=speed_mbps,
                mtu=mtu,
                rx_bytes=rx_bytes,
                tx_bytes=tx_bytes,
                rdma_dev=rdma_dev,
            )
        )
    host.net_links = links


# Map query-gpu field name -> (Gpu attribute, converter).
GPU_FIELD_MAP: dict[str, tuple[str, str]] = {
    "index": ("index", "int"),
    "uuid": ("uuid", "text"),
    "name": ("name", "text"),
    "pci.bus_id": ("bus_id", "raw"),        # raw: join key, keep byte-exact
    "driver_version": ("driver", "text"),
    "vbios_version": ("vbios", "text"),
    "pstate": ("pstate", "text"),
    "temperature.gpu": ("temp_c", "num"),
    "temperature.gpu.tlimit": ("temp_limit_c", "num"),
    "fan.speed": ("fan_percent", "num"),
    "utilization.gpu": ("util_gpu", "num"),
    "utilization.memory": ("util_mem", "num"),
    "utilization.encoder": ("util_enc", "num"),
    "utilization.decoder": ("util_dec", "num"),
    "memory.total": ("mem_total_mib", "num"),
    "memory.used": ("mem_used_mib", "num"),
    "memory.free": ("mem_free_mib", "num"),
    "memory.reserved": ("mem_reserved_mib", "num"),
    "power.draw": ("power_w", "num"),
    "power.draw.instant": ("power_w", "num"),
    "power.limit": ("power_limit_w", "num"),
    "enforced.power.limit": ("power_limit_w", "num"),
    "power.max_limit": ("power_max_w", "num"),
    "clocks.sm": ("clock_sm", "num"),
    "clocks.max.sm": ("clock_sm_max", "num"),
    "clocks.mem": ("clock_mem", "num"),
    "clocks.max.mem": ("clock_mem_max", "num"),
    "clocks.video": ("clock_video", "num"),
    "pcie.link.gen.current": ("pcie_gen", "int"),
    "pcie.link.gen.max": ("pcie_gen_max", "int"),
    "pcie.link.width.current": ("pcie_width", "int"),
    "pcie.link.width.max": ("pcie_width_max", "int"),
    "clocks_event_reasons.active": ("clock_events", "hex"),
    "compute_mode": ("compute_mode", "text"),
    "persistence_mode": ("persistence", "text"),
    "mig.mode.current": ("mig_mode", "text"),
    # Apple silicon (remote.sh Darwin branch). Absent from every nvidia-smi
    # field set, so NVIDIA rows keep vendor="", cores=None, ane_power_w=None.
    "vendor": ("vendor", "text"),
    "gpu.cores": ("cores", "int"),
    "ane.power": ("ane_power_w", "num"),
}

_CONV = {"int": integer, "num": num, "text": text, "hex": hexmask, "raw": _clean}


def parse_gpus(lines: list[str], host: Host) -> list[Gpu]:
    kv_lines = [l for l in lines if l.startswith(("fieldset\t", "fields\t"))]
    kv = _kv(kv_lines)
    host.fieldset = kv.get("fieldset", "")
    field_names = [f.strip() for f in kv.get("fields", "").split(",") if f.strip()]

    gpus: list[Gpu] = []
    for line in lines:
        if not line.startswith("row\t"):
            continue
        # ", " is the real separator; splitting on "," leaves leading spaces.
        cells = line[4:].split(", ")
        gpu = Gpu(host=host.name, index=-1)
        for name, cell in zip(field_names, cells):
            mapped = GPU_FIELD_MAP.get(name)
            if mapped is None:
                continue
            attr, kind = mapped
            value = _CONV[kind](cell)
            # power.draw.instant and enforced.power.limit share a target with
            # their non-instant counterparts; never let a None overwrite a real
            # value that an earlier alias already supplied.
            if value is None and getattr(gpu, attr, None) is not None:
                continue
            setattr(gpu, attr, value)
        if gpu.index is None or gpu.index < 0:
            continue
        gpus.append(gpu)
    return gpus


# `nvidia-smi -q -d PIDS` shape:
#     GPU 00000000:16:00.0
#         Processes
#             Process ID                        : 4595
#                 Type                          : G
#                 Name                          : /usr/lib/xorg/Xorg
#                 Used GPU Memory               : 4 MiB
_BUS_RE = re.compile(r"^GPU\s+(\S+)\s*$")
_FIELD_RE = re.compile(r"^\s*(Process ID|Type|Name|Used GPU Memory)\s*:\s*(.*?)\s*$")


def parse_pids(lines: list[str]) -> list[Proc]:
    """Parse the `-q -d PIDS` dump into Proc rows keyed by bus_id."""
    procs: list[Proc] = []
    bus = ""
    cur: Optional[Proc] = None
    for raw in lines:
        # remote.sh prefixes every line with '|' so blank lines survive.
        line = raw[1:] if raw.startswith("|") else raw
        mb = _BUS_RE.match(line.strip())
        if mb:
            bus = mb.group(1)
            continue
        mf = _FIELD_RE.match(line)
        if not mf:
            continue
        key, val = mf.group(1), mf.group(2)
        if key == "Process ID":
            pid = integer(val)
            if pid is None:
                cur = None
                continue
            cur = Proc(bus_id=bus, pid=pid)
            procs.append(cur)
        elif cur is None:
            continue
        elif key == "Type":
            cur.ptype = text(val)
        elif key == "Name":
            cur.name = val.strip()
        elif key == "Used GPU Memory":
            cur.gpu_mem_mib = num(val)
    return procs


_PS_RE = re.compile(
    r"^\s*(\d+)\s+(\S+)\s+([\d.]+)\s+(\d+)\s+(\S+)\s+(.*)$"
)


def parse_ps(lines: list[str]) -> tuple[dict[int, tuple[str, float, float, str, str]], int]:
    """Returns ({pid: (user, pcpu, rss_kib, etime, args)}, requested_count)."""
    out: dict[int, tuple[str, float, float, str, str]] = {}
    requested = 0
    for line in lines:
        if line.startswith("requested\t"):
            requested = integer(line.split("\t", 1)[1]) or 0
            continue
        if not line.startswith("row\t"):
            continue
        m = _PS_RE.match(line[4:])
        if not m:
            continue
        pid = int(m.group(1))
        out[pid] = (
            m.group(2),
            float(m.group(3)),
            float(m.group(4)),
            m.group(5),
            m.group(6).strip(),
        )
    return out, requested


def parse_cgroups(lines: list[str]) -> dict[int, str]:
    """Parse `cg\\t<pid>\\t<container>` lines inside `__PS__`."""
    out: dict[int, str] = {}
    for line in lines:
        if not line.startswith("cg\t"):
            continue
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        pid = integer(parts[1])
        cname = _clean(parts[2])
        if pid is not None and cname:
            out[pid] = cname
    return out


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def parse_payload(
    host: Host,
    stdout: str,
    stderr: str = "",
    returncode: int = 0,
    latency_ms: Optional[float] = None,
) -> Host:
    """Fill `host` from one remote sample. Mutates and returns it."""
    host.latency_ms = latency_ms

    # ssh itself failed: the host is down and there is nothing else to read.
    if returncode != 0 and not stdout.strip():
        host.state = HostState.DOWN
        host.error_code = f"ssh-{returncode}"
        host.error_msg = classify_ssh_error(stderr)
        host.gpus = []
        host.net_links = []
        return host

    sections, args = split_sections(stdout)
    if "HOST" not in sections:
        host.state = HostState.DOWN
        host.error_code = "no-payload"
        host.error_msg = (stderr.strip().splitlines() or ["empty response"])[0]
        host.gpus = []
        host.net_links = []
        return host

    parse_host(sections["HOST"], host)
    parse_psi(sections.get("PSI", []), host)
    parse_net(sections.get("NET", []), host)
    host.remote_ms = integer(args.get("END"))

    if "ERROR" in sections:
        kv = _kv(sections["ERROR"])
        host.error_code = kv.get("code", "unknown")
        host.error_msg = kv.get("msg", "")
        # The host answered with a healthy /proc block; only its GPU stack is
        # unusable. Keep it visible with live CPU/RAM/PSI.
        host.state = HostState.GPU_DEAD
        host.gpus = []
        return host

    host.error_code = ""
    host.error_msg = ""
    gpus = parse_gpus(sections.get("GPU", []), host)
    procs = parse_pids(sections.get("PIDS", []))
    ps_lines = sections.get("PS", [])
    ps_map, requested = parse_ps(ps_lines)
    cg_map = parse_cgroups(ps_lines)

    host.ps_requested = requested
    host.ps_resolved = len(ps_map)

    # A unified part has no framebuffer, so its only honest denominator is the
    # pool it shares with the CPU. Joining the two here is the same job as the
    # bus_id join below -- assembly is this module's work, and this is the one
    # place where host.mem and the GPU rows are both in scope. Live polling,
    # --replay and `gpumaster check` all funnel through this function.
    #
    # A snapshot, not a reference: the pool numbers can drift from host.mem if
    # one is mutated afterwards. Nothing does, outside tests.
    for g in gpus:
        if not g.is_unified:
            continue
        if host.mem.total_kib:
            g.uma_total_mib = host.mem.total_kib / KIB_PER_MIB
        if host.mem.available_kib is not None:
            g.uma_available_mib = host.mem.available_kib / KIB_PER_MIB

    for p in procs:
        row = ps_map.get(p.pid)
        if row:
            p.user, p.cpu_percent, p.rss_kib, p.etime, p.cmdline = row
            p.resolved = True
        if p.pid in cg_map:
            p.container = cg_map[p.pid]

    # Join processes to GPUs on the exact bus_id string.
    by_bus: dict[str, Gpu] = {g.bus_id: g for g in gpus}
    for p in procs:
        g = by_bus.get(p.bus_id)
        if g is not None:
            g.procs.append(p)
    for g in gpus:
        g.procs.sort(key=lambda p: -(p.gpu_mem_mib or 0))

    host.gpus = gpus
    host.state = HostState.STREAMING
    if gpus:
        host.cached_gpu_label = host.gpu_label
    return host


_SSH_PATTERNS = [
    (re.compile(r"Permission denied", re.I), "AUTH_FAIL"),
    (re.compile(r"REMOTE HOST IDENTIFICATION HAS CHANGED", re.I), "HOSTKEY_CHANGED"),
    (re.compile(r"Host key verification failed", re.I), "HOSTKEY_FAIL"),
    (re.compile(r"Connection refused", re.I), "REFUSED"),
    (re.compile(r"No route to host", re.I), "NO_ROUTE"),
    (re.compile(r"Connection timed out|Operation timed out", re.I), "TIMEOUT"),
    (re.compile(r"ControlPath too long", re.I), "CONTROLPATH_TOO_LONG"),
    (re.compile(r"Could not resolve hostname", re.I), "DNS_FAIL"),
    # The shared control-socket dir in /tmp vanished mid-run (old gpumaster
    # shutdown(), macOS /tmp pruning). Transient: poll_all re-creates it.
    (re.compile(r"unix_listener: cannot bind", re.I), "MUX_BIND"),
]

# Failures that will never fix themselves by retrying sooner.
TERMINAL_ERRORS = {"AUTH_FAIL", "HOSTKEY_CHANGED", "HOSTKEY_FAIL", "CONTROLPATH_TOO_LONG"}

REMEDIES = {
    "AUTH_FAIL": "ssh-copy-id <host>   (or check IdentityFile / ssh-agent)",
    "HOSTKEY_CHANGED": "ssh-keygen -R <host>",
    "HOSTKEY_FAIL": "ssh <host> true   (accept the fingerprint once)",
    "CONTROLPATH_TOO_LONG": "use a shorter --control-dir, e.g. /tmp/gm.$UID",
    "MUX_BIND": "control dir vanished; recreated automatically on the next poll",
}


def classify_ssh_error(stderr: str) -> str:
    first = ""
    for line in stderr.splitlines():
        s = line.strip()
        if s and not s.startswith("Warning: Permanently added"):
            first = s
            break
    return first or "ssh failed"


def ssh_error_kind(stderr: str) -> str:
    for rx, kind in _SSH_PATTERNS:
        if rx.search(stderr):
            return kind
    return "UNKNOWN"
