"""Data model and derived quantities for gpumaster.

Everything here is pure: no I/O, no subprocess, no curses. `parse.py` builds
these objects out of raw remote output; `render.py` turns them into strings.

The important logic in this file is the GB10 / DGX Spark unified-memory
derivation and the alert rules, both of which are constrained by measurements
recorded in the plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

MIB = 1024.0 * 1024.0
KIB_PER_MIB = 1024.0


# --------------------------------------------------------------------------
# Clock event ("throttle") reasons -- nvml.h NVML_CLOCKS_EVENT_REASON_*.
# The bit order is NOT the display order used by `nvidia-smi -q`, which nests
# HW Thermal and HW Power Brake underneath HW Slowdown. Decode from the values.
# --------------------------------------------------------------------------
CLOCK_EVENT_BITS: list[tuple[int, str, str, bool]] = [
    # (mask, short badge, long name, is_alarm)
    (0x001, "IDLE", "GPU idle", False),
    (0x002, "APPCLK", "Applications clocks setting", False),
    (0x004, "PWR", "SW power cap", True),
    (0x008, "HW!", "HW slowdown", True),
    (0x010, "SYNC", "Sync boost", False),
    (0x020, "THRM", "SW thermal slowdown", True),
    (0x040, "THRM!", "HW thermal slowdown", True),
    (0x080, "BRAKE!", "HW power brake slowdown", True),
    (0x100, "DISP", "Display clock setting", False),
]


def pool_other(pool_used: float, gpu_resident: float) -> float:
    """Unified-pool memory held by something other than the GPU.

    By SUBTRACTION inside one pool, never by addition -- on coherent UMA the
    GPU's allocation is inside the host's used memory, so this is the shape
    that makes the double-count impossible to draw.

    Clamped at zero on purpose: the two terms come from different subsystems --
    NVML per-process accounting and the kernel's MemAvailable estimate --
    sampled at slightly different instants, and nothing guarantees
    resident <= pool used. One definition, because both the per-GPU property
    and the per-host rollup need exactly this rule.
    """
    return max(0.0, pool_used - gpu_resident)


def decode_clock_events(mask: Optional[int]) -> list[tuple[str, str, bool]]:
    """Decode the clocks_event_reasons.active bitmask into badges."""
    if mask is None:
        return []
    return [
        (short, long, alarm)
        for bit, short, long, alarm in CLOCK_EVENT_BITS
        if mask & bit
    ]


class HostState(Enum):
    """Four states, not two.

    A DGX Spark can return ssh rc=0 with a perfectly healthy /proc while
    nvidia-smi fails with rc=9. Collapsing that into "offline" throws away a
    live machine, so GPU_DEAD keeps its CPU/RAM/PSI panel rendering.
    """

    STREAMING = "+"      # everything works
    GPU_DEAD = "!"       # host up, nvidia-smi unusable
    DOWN = "x"           # ssh failed
    DISABLED = "."       # switched off in config


class MemKind(Enum):
    DISCRETE = "discrete"   # real framebuffer, used/total both meaningful
    UNIFIED = "unified"     # GB10: CPU and GPU share one pool


@dataclass(slots=True)
class Psi:
    """One /proc/pressure/<resource> resource."""

    some10: Optional[float] = None
    some60: Optional[float] = None
    some300: Optional[float] = None
    full10: Optional[float] = None
    full60: Optional[float] = None
    full300: Optional[float] = None


@dataclass(slots=True)
class NetLink:
    """One high-speed network / RoCEv2 RDMA interface from __NET__."""

    name: str
    operstate: str = "unknown"
    speed_mbps: Optional[int] = None
    mtu: Optional[int] = None
    rx_bytes: Optional[int] = None
    tx_bytes: Optional[int] = None
    rdma_dev: str = ""
    rx_gbps: Optional[float] = None
    tx_gbps: Optional[float] = None

    @property
    def up(self) -> bool:
        return self.operstate == "up"

    @property
    def speed_gbps(self) -> Optional[int]:
        if self.speed_mbps is None or self.speed_mbps < 1000:
            return None
        return self.speed_mbps // 1000

    @property
    def total_gbps(self) -> Optional[float]:
        if self.rx_gbps is None and self.tx_gbps is None:
            return None
        return (self.rx_gbps or 0.0) + (self.tx_gbps or 0.0)


@dataclass(slots=True)
class HostMem:
    total_kib: Optional[int] = None
    free_kib: Optional[int] = None
    available_kib: Optional[int] = None
    buffers_kib: Optional[int] = None
    cached_kib: Optional[int] = None
    sreclaimable_kib: Optional[int] = None
    shmem_kib: Optional[int] = None
    dirty_kib: Optional[int] = None
    swap_total_kib: Optional[int] = None
    swap_free_kib: Optional[int] = None
    disk_total_kib: Optional[int] = None
    disk_avail_kib: Optional[int] = None

    @property
    def used_kib(self) -> Optional[int]:
        """Used = total - available.

        Deliberately not total-free: MemFree on a DGX Spark can be 3 GiB while
        MemAvailable is 50 GiB, the difference being reclaimable page
        cache. Using MemFree would report an idle machine as nearly OOM.
        """
        if self.total_kib is None or self.available_kib is None:
            return None
        return self.total_kib - self.available_kib

    @property
    def swap_used_kib(self) -> Optional[int]:
        if self.swap_total_kib is None or self.swap_free_kib is None:
            return None
        return self.swap_total_kib - self.swap_free_kib

    @property
    def available_frac(self) -> Optional[float]:
        if not self.total_kib or self.available_kib is None:
            return None
        return self.available_kib / self.total_kib

    @property
    def disk_avail_gib(self) -> Optional[float]:
        if self.disk_avail_kib is None:
            return None
        return self.disk_avail_kib / (KIB_PER_MIB * KIB_PER_MIB)

    @property
    def disk_avail_frac(self) -> Optional[float]:
        if not self.disk_total_kib or self.disk_avail_kib is None:
            return None
        return self.disk_avail_kib / self.disk_total_kib


@dataclass(slots=True)
class Proc:
    """One GPU process.

    `gpu_mem_mib` comes from NVML (`-q -d PIDS`). `rss_kib` comes from ps.

    On a discrete card these are different physical memories and both are
    meaningful. On GB10 they overlap: the ComfyUI job measures 33569 MiB of GPU
    memory against 37452 MiB RSS -- the GPU allocation IS part of the RSS. They
    must never be added together on a unified host.
    """

    bus_id: str
    pid: int
    ptype: str = ""                      # C, G or C+G
    name: str = ""                       # full binary path from NVML
    gpu_mem_mib: Optional[float] = None
    user: str = ""
    cpu_percent: Optional[float] = None
    rss_kib: Optional[float] = None
    etime: str = ""
    cmdline: str = ""
    container: str = ""                  # Docker/containerd/Podman name or short ID
    resolved: bool = False               # did ps return a row for this pid?

    @property
    def display_command(self) -> str:
        return self.cmdline or self.name

    @property
    def etime_seconds(self) -> Optional[int]:
        return parse_etime_seconds(self.etime)


def parse_etime_seconds(etime: str) -> Optional[int]:
    """Parse ps `etime` (`[[DD-]HH:]MM:SS`) into total seconds, or None."""
    s = (etime or "").strip()
    if not s or s == "-":
        return None
    days = 0
    if "-" in s:
        d_str, _, s = s.partition("-")
        if not d_str.isdigit():
            return None
        days = int(d_str)
    parts = s.split(":")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        hours, mins, secs = 0, int(parts[0]), int(parts[1])
    elif len(parts) == 3 and all(p.isdigit() for p in parts):
        hours, mins, secs = int(parts[0]), int(parts[1]), int(parts[2])
    else:
        return None
    return days * 86400 + hours * 3600 + mins * 60 + secs


STALE_MEM_MIB = 4096.0
STALE_ETIME_S = 1800
STALE_CPU_PCT = 1.0


def is_stale_proc(gpu: Gpu, proc: Proc) -> bool:
    """True when a compute process holds >= 4 GiB for >= 30m at 0% GPU / < 1% CPU.

    Requires `gpu.util_trustworthy`: on GB10 `utilization.gpu` reads 0 even
    during active 33 GiB jobs, so we never accuse a Spark process of idling
    based on `util_gpu == 0`. Graphics (`G`) processes are also excluded.
    """
    if proc.ptype == "G":
        return False
    if not gpu.util_trustworthy or gpu.util_gpu is None or gpu.util_gpu > 0:
        return False
    if proc.gpu_mem_mib is None or proc.gpu_mem_mib < STALE_MEM_MIB:
        return False
    etime_s = proc.etime_seconds
    if etime_s is None or etime_s < STALE_ETIME_S:
        return False
    if proc.cpu_percent is None or proc.cpu_percent >= STALE_CPU_PCT:
        return False
    return True


@dataclass(slots=True)
class Gpu:
    host: str
    index: int
    uuid: str = ""
    name: str = ""
    bus_id: str = ""
    driver: str = ""
    vbios: str = ""
    pstate: str = ""
    temp_c: Optional[float] = None
    temp_limit_c: Optional[float] = None
    fan_percent: Optional[float] = None
    util_gpu: Optional[float] = None
    util_mem: Optional[float] = None
    util_enc: Optional[float] = None
    util_dec: Optional[float] = None
    mem_total_mib: Optional[float] = None
    mem_used_mib: Optional[float] = None
    mem_free_mib: Optional[float] = None
    mem_reserved_mib: Optional[float] = None
    power_w: Optional[float] = None
    power_limit_w: Optional[float] = None
    power_max_w: Optional[float] = None
    clock_sm: Optional[float] = None
    clock_sm_max: Optional[float] = None
    clock_mem: Optional[float] = None
    clock_mem_max: Optional[float] = None
    clock_video: Optional[float] = None
    pcie_gen: Optional[int] = None
    pcie_gen_max: Optional[int] = None
    pcie_width: Optional[int] = None
    pcie_width_max: Optional[int] = None
    clock_events: Optional[int] = None
    compute_mode: str = ""
    persistence: str = ""
    mig_mode: str = ""
    # "" for every NVIDIA row; "apple" when remote.sh's Darwin branch sampled
    # an AGX accelerator through ioreg. The one bit that tells the unified
    # branches below which of their assumptions hold.
    vendor: str = ""
    cores: Optional[int] = None   # Apple silicon GPU core count
    ane_power_w: Optional[float] = None  # Apple silicon ANE power draw (macmon)
    procs: list[Proc] = field(default_factory=list)
    # The host's unified pool, copied in by parse.parse_payload for unified
    # parts only. A Gpu knows its host by name alone, and the derived
    # quantities below -- pool_used_mib, pool_other_mib, pool_available_frac,
    # frac_of_pool -- all need the pool as their denominator, so it has to
    # travel with the GPU rather than be looked up per consumer. None means
    # "the pool size is unknown", never a fallback guess.
    uma_total_mib: Optional[float] = None      # /proc/meminfo MemTotal
    uma_available_mib: Optional[float] = None  # /proc/meminfo MemAvailable

    # ---- derived -----------------------------------------------------------

    @property
    def mem_kind(self) -> MemKind:
        """A GPU with no framebuffer total is a unified-memory part.

        NVIDIA documents `Memory-Usage: Not Supported` on DGX Spark as expected
        behaviour, and every alternative source was ruled out empirically
        (dmon fb, tegrastats, /sys/class/drm, /proc/driver/nvidia).
        """
        return MemKind.DISCRETE if self.mem_total_mib else MemKind.UNIFIED

    @property
    def is_unified(self) -> bool:
        return self.mem_kind is MemKind.UNIFIED

    @property
    def is_apple(self) -> bool:
        return self.vendor == "apple"

    @property
    def gpu_resident_mib(self) -> Optional[float]:
        """Memory the GPU is actually holding.

        Discrete: NVML's framebuffer used.
        Unified, Apple: the accelerator's own "In use system memory" counter,
        which ioreg reports directly. It arrives as memory.used.
        Unified, GB10: memory.used is [N/A], so the sum of per-process NVML
        allocations, which does work there even though the totals do not.
        In every case this is a SUBSET of host used memory, never an addition.
        """
        if not self.is_unified:
            return self.mem_used_mib
        if self.is_apple and self.mem_used_mib is not None:
            return self.mem_used_mib
        # Keyed on the vendor, not on the value being present: a future Spark
        # driver that reports memory.used=0 instead of [N/A] must not silently
        # replace the per-process sum with a zero.
        vals = [p.gpu_mem_mib for p in self.procs if p.gpu_mem_mib is not None]
        return sum(vals) if vals else 0.0

    @property
    def mem_derived(self) -> bool:
        """True when gpu_resident_mib was summed rather than read directly."""
        return self.is_unified and not (self.is_apple and self.mem_used_mib is not None)

    @property
    def mem_percent(self) -> Optional[float]:
        """Framebuffer fill. Only defined for discrete cards.

        Deliberately still None on a unified part. `uma_total_mib` is a
        denominator for the POOL, not for "this GPU's memory", and feeding it
        here would put the 90/97% VRAM alert on a number that cannot mean what
        that alert means. Pool pressure is `host_alerts()`' job -- it already
        reads MemAvailable, swap and PSI. See `frac_of_pool` for the display
        ratio.
        """
        if self.is_unified or not self.mem_total_mib or self.mem_used_mib is None:
            return None
        return 100.0 * self.mem_used_mib / self.mem_total_mib

    # ---- unified pool ------------------------------------------------------
    #
    # nvtop shows a memory bar on a Spark and it is tempting to conclude the
    # framebuffer is readable after all. It is not. nvtop's NVIDIA backend sets
    # has_unified_memory on NVML_ERROR_NOT_SUPPORTED and then opens
    # /proc/meminfo; from 3.3.1 its bar is (MemTotal - MemAvailable)/MemTotal,
    # i.e. `pool_used_mib` / `uma_total_mib` below -- host RAM, not GPU memory.
    # Measured on a DGX Spark: nvtop's denominator is MemTotal.
    # nvmlDeviceGetMemoryInfo, its _v2 form and nvmlDeviceGetBAR1MemoryInfo
    # all return NVML_ERROR_NOT_SUPPORTED when called directly on that host.

    @property
    def pool_used_mib(self) -> Optional[float]:
        """Whole-pool used = MemTotal - MemAvailable. This is nvtop's number."""
        if self.uma_total_mib is None or self.uma_available_mib is None:
            return None
        return self.uma_total_mib - self.uma_available_mib

    @property
    def pool_other_mib(self) -> Optional[float]:
        """Pool memory held by something other than this GPU."""
        used = self.pool_used_mib
        if used is None:
            return None
        return pool_other(used, self.gpu_resident_mib or 0.0)

    @property
    def pool_available_frac(self) -> Optional[float]:
        if not self.uma_total_mib or self.uma_available_mib is None:
            return None
        return self.uma_available_mib / self.uma_total_mib

    @property
    def frac_of_pool(self) -> Optional[float]:
        """GPU-resident as a fraction of the pool. Unified parts only.

        For display and history only -- explicitly NOT wired into the VRAM
        alert, see `mem_percent`.
        """
        if not self.is_unified or not self.uma_total_mib:
            return None
        return (self.gpu_resident_mib or 0.0) / self.uma_total_mib

    @property
    def pool_percent(self) -> Optional[float]:
        frac = self.frac_of_pool
        return None if frac is None else 100.0 * frac

    @property
    def history_percent(self) -> Optional[float]:
        """What the history strip plots.

        Utilization on a discrete card; pool share on a unified one, where
        utilization.gpu is not believable. Defined here, once, because the
        recorder (tui) and the colour (render) must agree on which quantity
        the strip is showing -- two copies of this branch would drift.
        """
        return self.util_gpu if self.util_trustworthy else self.pool_percent

    @property
    def util_trustworthy(self) -> bool:
        """utilization.gpu is not believable on GB10.

        Measured: a resident 33.5 GB job with utilization.gpu pinned at 0 across
        every sample, pstate pinned at P0 permanently, and clocks that never
        drop. We render '?' instead of a number, and specifically do NOT
        substitute clocks.sm/clocks.max.sm -- that ratio sits at 2405/3003=80%
        on an idle Spark and would show it as permanently busy.

        Apple silicon is unified too, but its "Device Utilization %" is the
        counter Activity Monitor's GPU History plots and it does move with
        load, so it is shown.
        """
        return not self.is_unified or self.is_apple

    @property
    def power_percent(self) -> Optional[float]:
        if not self.power_limit_w or self.power_w is None:
            return None
        return min(100.0, 100.0 * self.power_w / self.power_limit_w)

    @property
    def clock_percent(self) -> Optional[float]:
        """Exposed for the drill-down only -- never as a utilization stand-in."""
        if not self.clock_sm_max or self.clock_sm is None:
            return None
        return 100.0 * self.clock_sm / self.clock_sm_max

    @property
    def throttle_badges(self) -> list[tuple[str, str, bool]]:
        return decode_clock_events(self.clock_events)

    @property
    def display_badges(self) -> list[tuple[str, str, bool]]:
        """Badges worth a column in the compact row.

        'IDLE' (bit 0) is dropped: it is informational, it is set on most cards
        most of the time, and nvidia-smi even reports it alongside a non-zero
        utilization (measured: workstation-b GPU2 at util=10% with the idle bit
        set). Keeping it would put a badge on nearly every row.
        """
        return [b for b in self.throttle_badges if b[0] != "IDLE"]

    @property
    def is_throttling(self) -> bool:
        return any(alarm for _, _, alarm in self.throttle_badges)

    @property
    def link_degraded(self) -> bool:
        """PCIe link below maximum *while the GPU is actually working*.

        The busy gate is essential. Measured on idle discrete cards: every
        card reports gen 1 against a max of 4 or 5, and widths sit at x16, x8
        and even x1 -- that is ASPM downtraining, not broken hardware. An
        unconditional check fired on 3 of 6 healthy GeForce cards.

        Idle width cannot be told apart from a genuinely narrow slot without
        load, so we only raise it when the link is being used and still low.

        Suppressed on unified parts: GB10 reports 'gen 1 x1 / max 16' for an
        on-package C2C link, which is meaningless rather than degraded.
        """
        if self.is_unified or not self.busy:
            return False
        if self.pcie_width is None or not self.pcie_width_max:
            return False
        return self.pcie_width < self.pcie_width_max

    @property
    def fan_suspect(self) -> bool:
        """Fan reads a literal 0 while the card is hot.

        Conditional on temperature so it stays quiet on the RTX 5090s, which
        report fan=0 at 50 C without anything being wrong.
        """
        return (
            self.fan_percent is not None
            and self.fan_percent == 0
            and self.temp_c is not None
            and self.temp_c > 70
        )

    @property
    def busy(self) -> bool:
        if self.util_gpu is not None and self.util_trustworthy:
            return self.util_gpu >= 5
        # Unified parts: fall back to whether anything is resident.
        res = self.gpu_resident_mib
        return bool(res and res > 1024)

    @property
    def top_proc(self) -> Optional[Proc]:
        cands = [p for p in self.procs if p.gpu_mem_mib is not None]
        return max(cands, key=lambda p: p.gpu_mem_mib or 0) if cands else None

    @property
    def stale_procs(self) -> list[Proc]:
        return [p for p in self.procs if is_stale_proc(self, p)]


@dataclass(slots=True)
class Host:
    name: str                 # display label
    target: str               # ssh target
    state: HostState = HostState.DOWN
    tags: list[str] = field(default_factory=list)
    hostname: str = ""
    arch: str = ""
    kernel: str = ""
    ncpu: Optional[int] = None
    load1: Optional[float] = None
    load5: Optional[float] = None
    load15: Optional[float] = None
    uptime_s: Optional[int] = None
    mem: HostMem = field(default_factory=HostMem)
    psi_mem: Psi = field(default_factory=Psi)
    psi_cpu: Psi = field(default_factory=Psi)
    psi_io: Psi = field(default_factory=Psi)
    net_links: list[NetLink] = field(default_factory=list)
    gpus: list[Gpu] = field(default_factory=list)
    fieldset: str = ""
    error_code: str = ""
    error_msg: str = ""
    ps_requested: int = 0
    ps_resolved: int = 0
    remote_ms: Optional[int] = None
    latency_ms: Optional[float] = None
    last_ok_wall: float = 0.0
    last_try_wall: float = 0.0
    # Inventory remembered from the last successful poll, so a host that goes
    # down can still be labelled without fabricating any live numbers.
    cached_gpu_label: str = ""

    @property
    def online(self) -> bool:
        return self.state in (HostState.STREAMING, HostState.GPU_DEAD)

    @property
    def has_unified(self) -> bool:
        return any(g.is_unified for g in self.gpus)

    def uma_pool(self) -> Optional[tuple[float, float, float, float]]:
        """(GPU-resident, pool total, pool available, other-used) MiB, or None.

        The pool is a HOST resource, so its total is taken once no matter how
        many unified GPUs the box has, while resident is summed across them.
        Defined here so the host line and the fleet rollup cannot disagree --
        they did while each filtered `host.gpus` for itself.

        None when nothing can be attributed: no unified GPU, or a driver so
        dead we have no GPU rows to attribute the pool to.
        """
        unified = [g for g in self.gpus if g.is_unified and g.uma_total_mib]
        if not unified:
            return None
        resident = sum(g.gpu_resident_mib or 0.0 for g in unified)
        total = unified[0].uma_total_mib or 0.0
        avail = unified[0].uma_available_mib or 0.0
        return resident, total, avail, pool_other(total - avail, resident)

    @property
    def gpu_label(self) -> str:
        if not self.gpus:
            return self.cached_gpu_label
        names = [short_gpu_name(g.name) for g in self.gpus]
        first = names[0]
        if all(n == first for n in names):
            return f"{len(names)}x {first}"
        return ", ".join(sorted(set(names)))

    @property
    def procs(self) -> list[Proc]:
        return [p for g in self.gpus for p in g.procs]

    @property
    def pid_ns_mismatch(self) -> bool:
        """ps resolved nothing despite NVML reporting PIDs."""
        return self.ps_requested > 0 and self.ps_resolved == 0

    @property
    def active_net_links(self) -> list[NetLink]:
        """Links that are either UP or configured for jumbo-frame cluster fabric (MTU >= 9000).

        On DGX Spark (ConnectX-7 Socket-Direct), each of the 2 physical QSFP ports
        exposes 2 netdevs (4 total). When only port 0 has a 200G DAC cable (configured
        at MTU 9000) and port 1 is intentionally left unplugged at default MTU 1500,
        filtering out down+unconfigured (MTU < 9000) ports prevents false '2/4 rails!'
        warnings while still alerting if a configured MTU 9000 rail goes down.
        """
        if not self.net_links:
            return []
        has_configured_jumbo = any((nl.mtu or 0) >= 9000 for nl in self.net_links)
        if not has_configured_jumbo:
            return self.net_links
        filtered = [nl for nl in self.net_links if nl.up or (nl.mtu or 0) >= 9000]
        return filtered or self.net_links

    def hs_fabric_summary(self) -> str:
        """Compact summary of high-speed RoCEv2 / >=25GbE links on this host."""
        links = self.active_net_links
        if not links:
            return ""
        up_links = [nl for nl in links if nl.up]
        has_rdma = any(nl.rdma_dev for nl in links)
        tag = "RDMA" if has_rdma else "NET"
        if not up_links:
            return f"{tag} 0/{len(links)} up"
        if len(up_links) < len(links):
            return f"{tag} {len(up_links)}/{len(links)} rails!"
        speeds = [nl.speed_gbps for nl in up_links if nl.speed_gbps]
        if speeds:
            if len(speeds) > 1 and len(set(speeds)) == 1:
                bw = f"{len(speeds)}x{speeds[0]}G"
            elif len(speeds) == 1:
                bw = f"{speeds[0]}G"
            else:
                bw = f"{sum(speeds)}G"
        else:
            bw = f"{len(up_links)}x"
        rates = [nl.total_gbps for nl in up_links if nl.total_gbps is not None]
        if rates:
            return f"{tag} {bw} {sum(rates):.1f}Gbps"
        return f"{tag} {bw} up"


def short_gpu_name(name: str) -> str:
    """'NVIDIA GeForce RTX 5090' -> 'RTX 5090'; 'NVIDIA GB10' -> 'GB10';
    'Apple M2 Ultra' -> 'M2 Ultra';
    'NVIDIA GeForce RTX 5090 Laptop GPU' -> 'RTX 5090m'.

    The mobile suffix collapses to NVIDIA's own `M` notation rather than being
    left to truncation. 'RTX 5090 Laptop GPU' is 19 columns, which fills the
    14-column model cell and -- as '1x RTX 5090 Laptop GPU' -- the 18-column
    host label *exactly*, eating the space that separates them from the next
    column. It also has to stay distinguishable from the desktop part: an
    RTX 5090 (32 GiB) and an RTX 5090 Laptop (24 GiB) would otherwise collapse
    to the same label.
    """
    n = name.replace("NVIDIA ", "").replace("GeForce ", "").replace("Apple ", "")
    n = n.replace(" Generation", "")
    return n.replace(" Laptop GPU", "m").strip()


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------

class Level(Enum):
    OK = 0
    WARN = 1
    CRIT = 2


# PSI is necessary but not sufficient. workstation-b currently sits at 9.3 of
# 124.8 GiB available with 8 GiB swapped out, and its PSI still reads 0.00 --
# PSI says "stalling now", swap-used says "already squeezed". Both are shown.
SWAP_WARN_GIB = 2.0
SWAP_CRIT_GIB = 8.0
PSI_SOME_WARN = 5.0
PSI_SOME_CRIT = 20.0
MEM_AVAIL_CRIT_FRAC = 0.10
MEM_AVAIL_WARN_FRAC = 0.20
DISK_AVAIL_WARN_GIB = 5.0
DISK_AVAIL_CRIT_GIB = 2.0
DISK_AVAIL_WARN_FRAC = 0.05
DISK_AVAIL_CRIT_FRAC = 0.02
TEMP_WARN_C = 80.0
TEMP_CRIT_C = 87.0
VRAM_WARN_FRAC = 0.90
VRAM_CRIT_FRAC = 0.97


@dataclass(slots=True)
class Alert:
    level: Level
    tag: str
    detail: str


def host_alerts(host: Host) -> list[Alert]:
    out: list[Alert] = []
    if host.state is HostState.DOWN:
        out.append(Alert(Level.CRIT, "DOWN", host.error_msg or "unreachable"))
        return out
    if host.state is HostState.GPU_DEAD:
        out.append(Alert(Level.CRIT, "GPU-DEAD", host.error_msg or host.error_code))

    swap = host.mem.swap_used_kib
    if swap is not None:
        gib = swap / KIB_PER_MIB / KIB_PER_MIB
        if gib >= SWAP_CRIT_GIB:
            out.append(Alert(Level.CRIT, "SWAP", f"{gib:.1f} GiB swapped out"))
        elif gib >= SWAP_WARN_GIB:
            out.append(Alert(Level.WARN, "SWAP", f"{gib:.1f} GiB swapped out"))

    frac = host.mem.available_frac
    if frac is not None:
        if frac < MEM_AVAIL_CRIT_FRAC:
            out.append(Alert(Level.CRIT, "RAM", f"only {frac*100:.0f}% available"))
        elif frac < MEM_AVAIL_WARN_FRAC:
            out.append(Alert(Level.WARN, "RAM", f"only {frac*100:.0f}% available"))

    disk_gib = host.mem.disk_avail_gib
    disk_frac = host.mem.disk_avail_frac
    if disk_gib is not None and disk_frac is not None:
        if disk_gib < DISK_AVAIL_CRIT_GIB or (disk_frac < DISK_AVAIL_CRIT_FRAC and disk_gib < 10.0):
            out.append(
                Alert(Level.CRIT, "DISK", f"only {disk_gib:.1f} GiB ({disk_frac*100:.0f}%) free")
            )
        elif disk_gib < DISK_AVAIL_WARN_GIB or (disk_frac < DISK_AVAIL_WARN_FRAC and disk_gib < 25.0):
            out.append(
                Alert(Level.WARN, "DISK", f"only {disk_gib:.1f} GiB ({disk_frac*100:.0f}%) free")
            )

    some = host.psi_mem.some10
    full = host.psi_mem.full10
    if full is not None and full > 0:
        out.append(Alert(Level.CRIT, "PSI", f"full avg10={full:.1f}"))
    elif some is not None:
        if some >= PSI_SOME_CRIT:
            out.append(Alert(Level.CRIT, "PSI", f"some avg10={some:.1f}"))
        elif some >= PSI_SOME_WARN:
            out.append(Alert(Level.WARN, "PSI", f"some avg10={some:.1f}"))

    links = host.active_net_links
    if links:
        up_rails = sum(1 for nl in links if nl.up)
        if 0 < up_rails < len(links):
            out.append(
                Alert(Level.WARN, "RDMA", f"{up_rails}/{len(links)} rails up")
            )
        for nl in links:
            if nl.up and nl.rdma_dev and nl.mtu is not None and 0 < nl.mtu < 9000:
                out.append(
                    Alert(Level.WARN, "MTU", f"{nl.name} mtu {nl.mtu} < 9000")
                )
    return out


def gpu_alerts(gpu: Gpu) -> list[Alert]:
    out: list[Alert] = []
    if gpu.temp_c is not None:
        if gpu.temp_c >= TEMP_CRIT_C:
            out.append(Alert(Level.CRIT, "TEMP", f"{gpu.temp_c:.0f}C"))
        elif gpu.temp_c >= TEMP_WARN_C:
            out.append(Alert(Level.WARN, "TEMP", f"{gpu.temp_c:.0f}C"))
    for short, long, alarm in gpu.throttle_badges:
        if alarm:
            out.append(Alert(Level.WARN, short, long))
    pct = gpu.mem_percent
    if pct is not None:
        if pct >= VRAM_CRIT_FRAC * 100:
            out.append(Alert(Level.CRIT, "VRAM", f"{pct:.0f}% full"))
        elif pct >= VRAM_WARN_FRAC * 100:
            out.append(Alert(Level.WARN, "VRAM", f"{pct:.0f}% full"))
    if gpu.link_degraded:
        out.append(
            Alert(Level.WARN, "LNK", f"x{gpu.pcie_width} of x{gpu.pcie_width_max}")
        )
    if gpu.fan_suspect:
        out.append(Alert(Level.WARN, "FAN", f"reads 0 at {gpu.temp_c:.0f}C"))
    return out


def worst_level(alerts: list[Alert]) -> Level:
    lvl = Level.OK
    for a in alerts:
        if a.level.value > lvl.value:
            lvl = a.level
    return lvl


# --------------------------------------------------------------------------
# Fleet rollup
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Fleet:
    hosts: list[Host] = field(default_factory=list)

    @property
    def gpus(self) -> list[Gpu]:
        return [g for h in self.hosts for g in h.gpus]

    @property
    def procs(self) -> list[Proc]:
        return [p for h in self.hosts for p in h.procs]

    def counts(self) -> dict[str, int]:
        c = {"up": 0, "gpu_dead": 0, "down": 0, "disabled": 0}
        for h in self.hosts:
            if h.state is HostState.STREAMING:
                c["up"] += 1
            elif h.state is HostState.GPU_DEAD:
                c["gpu_dead"] += 1
            elif h.state is HostState.DOWN:
                c["down"] += 1
            else:
                c["disabled"] += 1
        return c

    def power_totals(self) -> tuple[float, float]:
        """(draw, limit) in watts, over GPUs that report both."""
        draw = sum(g.power_w or 0.0 for g in self.gpus)
        limit = sum(g.power_limit_w or 0.0 for g in self.gpus)
        return draw, limit

    def vram_totals(self) -> tuple[float, float]:
        """(used, total) MiB over DISCRETE GPUs only.

        Unified parts are excluded: adding a Spark's share of system RAM into a
        fleet VRAM total would mix two different things.
        """
        used = sum(g.mem_used_mib or 0.0 for g in self.gpus if not g.is_unified)
        total = sum(g.mem_total_mib or 0.0 for g in self.gpus if not g.is_unified)
        return used, total

    def uma_totals(self) -> tuple[float, float, int]:
        """(GPU-resident, pool total, host count) MiB over unified hosts.

        Iterates hosts, not GPUs: the pool is a host resource, and summing it
        per GPU would count a two-GPU unified box's 128 GiB twice. `Host.uma_pool`
        does that arithmetic so this and the host line stay in step.

        Kept separate from vram_totals() rather than merged into it: one is a
        framebuffer, the other is a share of system RAM, and a combined
        "fleet VRAM" figure would answer neither question honestly.
        """
        resident = total = 0.0
        hosts = 0
        for h in self.hosts:
            pool = h.uma_pool()
            if pool is None:
                continue
            resident += pool[0]
            total += pool[1]
            hosts += 1
        return resident, total, hosts

    def hottest(self) -> Optional[Gpu]:
        cands = [g for g in self.gpus if g.temp_c is not None]
        return max(cands, key=lambda g: g.temp_c or 0) if cands else None

    def throttling(self) -> list[Gpu]:
        return [g for g in self.gpus if g.is_throttling]

    def busy_count(self) -> int:
        return sum(1 for g in self.gpus if g.busy)


def idle_gpus(fleet: Fleet, util_max: float = 5.0, mem_frac_max: float = 0.10) -> list[Gpu]:
    """GPUs worth grabbing, for `gpumaster free`.

    A GPU on a host that is itself memory-starved is excluded: workstation-b has
    four completely idle cards but only 9.3 of 124.8 GiB of RAM available, so
    sending a job there would fail for reasons the GPU columns never show.
    """
    out: list[Gpu] = []
    starved = set()
    for h in fleet.hosts:
        frac = h.mem.available_frac
        if frac is not None and frac < MEM_AVAIL_WARN_FRAC:
            starved.add(h.name)
    for g in fleet.gpus:
        if g.host in starved:
            continue
        if g.util_trustworthy and g.util_gpu is not None and g.util_gpu > util_max:
            continue
        if not g.is_unified:
            if g.mem_total_mib and g.mem_used_mib is not None:
                if g.mem_used_mib / g.mem_total_mib > mem_frac_max:
                    continue
        elif not g.util_trustworthy:
            # GB10: utilization reads '?', so residency is the only busy
            # signal there. NOT applied to Apple silicon -- its utilization was
            # already judged above, and its resident counter carries the
            # desktop's own baseline (WindowServer alone holds 2-3 GiB on an
            # idle Mac Studio), which would exclude every Mac forever.
            res = g.gpu_resident_mib or 0.0
            if res > 1024:
                continue
        out.append(g)
    return out


def gpu_free_mib(gpu: Gpu) -> Optional[float]:
    """Free memory on one GPU in MiB (framebuffer free on discrete, pool available on UMA)."""
    if gpu.is_unified:
        return gpu.uma_available_mib
    if gpu.mem_free_mib is not None:
        return gpu.mem_free_mib
    if gpu.mem_total_mib is not None and gpu.mem_used_mib is not None:
        return max(0.0, gpu.mem_total_mib - gpu.mem_used_mib)
    return None


def select_free_gpus(
    fleet: Fleet,
    min_mem_gib: float = 0.0,
    count: int = 1,
    util_max: float = 5.0,
    mem_frac_max: float = 0.10,
    cuda_only: bool = False,
) -> dict[str, list[Gpu]]:
    """Filter `idle_gpus(fleet)` by minimum free GiB and minimum GPU count per host.

    Returns `{host_name: [Gpu, ...]}` ordered from best host to worst:
      1. Most qualifying idle GPUs on the host
      2. Most free memory per GPU
      3. Lowest host 1-minute load average
    """
    req_mib = max(0.0, min_mem_gib) * 1024.0
    req_count = max(1, count)
    free = idle_gpus(fleet, util_max=util_max, mem_frac_max=mem_frac_max)
    by_host: dict[str, list[Gpu]] = {}
    for g in free:
        if cuda_only and g.is_apple:
            continue
        avail = gpu_free_mib(g)
        if req_mib > 0.0 and (avail is None or avail < req_mib):
            continue
        by_host.setdefault(g.host, []).append(g)

    hosts_map = {h.name: h for h in fleet.hosts}
    qualified: dict[str, list[Gpu]] = {}
    for hname, gpus in by_host.items():
        if len(gpus) < req_count:
            continue
        # Unified pool is shared across all unified GPUs on the host; require
        # the pool to fit `req_count` allocations of `req_mib` without double-counting.
        if req_mib > 0.0 and any(g.is_unified for g in gpus):
            pool_avail = next((g.uma_available_mib for g in gpus if g.is_unified), None)
            if pool_avail is None or pool_avail < req_count * req_mib:
                continue
        qualified[hname] = sorted(gpus, key=lambda g: g.index)

    def host_rank(item: tuple[str, list[Gpu]]) -> tuple:
        hname, gpus = item
        h = hosts_map.get(hname)
        load1 = h.load1 if (h and h.load1 is not None) else 9999.0
        top_free = max((gpu_free_mib(g) or 0.0) for g in gpus)
        return (-len(gpus), -top_free, load1, hname)

    return dict(sorted(qualified.items(), key=host_rank))
