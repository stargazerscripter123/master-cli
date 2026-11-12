"""Data models for btop + nvtop unified system and GPU telemetry."""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
import os
from typing import Dict, List, Optional


def format_smart_cmdline(name: str, cmdline: str) -> str:
    """nvitop/btop-inspired smart command line condenser for Python/CUDA/App workloads."""
    raw = cmdline.strip() if cmdline else ""
    if not raw:
        return name

    parts = raw.split()
    if not parts:
        return name
    exe = parts[0]
    base = os.path.basename(exe)
    low_base = base.lower()

    # 1. Detect conda / venv / uv / Framework Python workloads first (including Python.app/Contents/MacOS/Python)
    if low_base.startswith("python") or base == "Python" or name.lower().startswith("python"):
        env_tag = "py"
        if "/envs/" in exe:
            try:
                env_name = exe.split("/envs/", 1)[1].split("/", 1)[0]
                env_tag = f"py:{env_name}"
            except Exception:
                pass
        elif "/.venv/" in exe or "/venv/" in exe:
            venv_root = exe.split("/.venv/", 1)[0] if "/.venv/" in exe else exe.split("/venv/", 1)[0]
            vparent = os.path.basename(venv_root.rstrip("/"))
            env_tag = f"py:{vparent}" if vparent else "py:venv"
        elif any(x in exe for x in ("/miniconda", "/anaconda", "/miniforge", "/conda/")):
            env_tag = "py:base"

        if len(parts) == 1:
            return f"[{env_tag}] (interactive)"

        # Shorten any .py script path in arguments (even after flags like -u or -O)
        condensed_args: List[str] = []
        shortened_script = False
        for tok in parts[1:]:
            if not shortened_script and tok.endswith(".py") and "/" in tok and not tok.startswith("-"):
                parent_dir = os.path.basename(os.path.dirname(tok))
                script_base = os.path.basename(tok)
                condensed_args.append(f"{parent_dir}/{script_base}" if parent_dir else script_base)
                shortened_script = True
            else:
                condensed_args.append(tok)
        return f"[{env_tag}] {' '.join(condensed_args)}"

    # 2. macOS .app bundle paths (handle spaces in '/Applications/Visual Studio Code.app/Contents/...')
    if ".app/Contents/" in raw:
        try:
            app_prefix = raw.split(".app/Contents/", 1)[0]
            app_name = os.path.basename(app_prefix) + ".app"
            if "/Contents/MacOS/" in raw:
                after_bin_dir = raw.rsplit("/Contents/MacOS/", 1)[-1]
            elif name and f"/{name}" in raw:
                after_bin_dir = name + raw.rsplit(f"/{name}", 1)[-1]
            else:
                after_bin_dir = raw.split(".app/Contents/", 1)[-1]

            if name and after_bin_dir.startswith(name):
                exe_disp = name
                rem_str = after_bin_dir[len(name) :].strip()
            elif " --" in after_bin_dir:
                exe_part, rem_part = after_bin_dir.split(" --", 1)
                exe_disp = os.path.basename(exe_part.strip())
                rem_str = "--" + rem_part
            else:
                toks = after_bin_dir.split()
                exe_disp = os.path.basename(toks[0]) if toks else (name or app_name)
                rem_str = " ".join(toks[1:])
            noisy_prefixes = (
                "--field-trial",
                "--shared-files",
                "--seatbelt-client",
                "--metrics-shmem",
                "--variations-seed",
                "--crashpad-handler",
                "--enable-features=",
                "--disable-features=",
            )
            clean_args = [a for a in rem_str.split() if not a.startswith(noisy_prefixes)]
            tail = " ".join(clean_args[:4])
            return f"[{app_name}] {exe_disp}" + (f" {tail}" if tail else "")
        except Exception:
            pass

    # 3. Any absolute binary path (/usr/bin/foo, /opt/homebrew/bin/foo, /Users/.../bin/foo, /System/...)
    if exe.startswith("/") and "/" in exe and base:
        tail = " ".join(parts[1:])
        return f"{base} {tail}".strip()

    return raw


@dataclass
class SensorReading:
    name: str
    category: str = "temp"   # "temp", "power", "fan"
    value: float = 0.0
    unit: str = "°C"


@dataclass
class CoreStat:
    core_id: int
    label: str = ""          # e.g. "E0", "P0", "C0"
    usage_pct: float = 0.0
    user_pct: float = 0.0
    sys_pct: float = 0.0
    iowait_pct: float = 0.0
    freq_mhz: int = 0
    temp_c: Optional[float] = None


@dataclass
class CpuStats:
    model: str = "Unknown CPU"
    arch: str = ""
    physical_cores: int = 1
    logical_cores: int = 1
    e_cores: int = 0
    p_cores: int = 0
    total_pct: float = 0.0
    user_pct: float = 0.0
    sys_pct: float = 0.0
    idle_pct: float = 100.0
    nice_pct: float = 0.0
    iowait_pct: float = 0.0
    freq_mhz: int = 0
    freq_max_mhz: int = 0
    e_freq_mhz: int = 0
    p_freq_mhz: int = 0
    temp_c: Optional[float] = None
    temp_max_c: float = 105.0
    power_w: Optional[float] = None
    pkg_power_w: Optional[float] = None
    sys_power_w: Optional[float] = None
    dram_power_w: Optional[float] = None
    peak_sys_power_w: Optional[float] = None
    peak_cpu_power_w: Optional[float] = None
    thermal_state: str = "Nominal"
    psi_cpu_pct: Optional[float] = None
    load_avg: tuple[float, float, float] = (0.0, 0.0, 0.0)
    uptime_sec: float = 0.0
    fans_rpm: List[tuple[str, int, int, float]] = field(default_factory=list)  # (name, rpm, max_rpm, pct)
    cores: List[CoreStat] = field(default_factory=list)


@dataclass
class BatteryStats:
    present: bool = False
    percent: float = 0.0
    status: str = "Unknown"  # Charging, Discharging, Full, AC
    power_w: Optional[float] = None
    time_remaining_min: Optional[int] = None
    temp_c: Optional[float] = None
    cycle_count: Optional[int] = None


@dataclass
class GpuProcessStat:
    pid: int
    process_name: str = ""
    gpu_index: int = 0
    kind: str = "G"          # "G" (Graphic), "C" (Compute), "C+G", "M" (Metal)
    gpu_util_pct: float = 0.0
    mem_util_pct: float = 0.0
    vram_bytes: int = 0
    vram_pct: float = 0.0
    enc_util_pct: float = 0.0
    dec_util_pct: float = 0.0
    api: str = ""            # "CUDA", "Metal", "Vulkan/GL"


@dataclass
class GpuDeviceStats:
    index: int = 0
    vendor: str = "Unknown"  # "Apple" or "NVIDIA"
    name: str = "Unknown GPU"
    uuid: str = ""
    bus_id: str = ""
    driver_version: str = ""
    api_version: str = ""    # e.g. "CUDA 13.2" or "Metal 3 (40c)"
    core_count: int = 0

    # Core utilization (%)
    gpu_util_pct: float = 0.0
    mem_ctrl_util_pct: float = 0.0
    renderer_util_pct: Optional[float] = None  # Apple Silicon Renderer %
    tiler_util_pct: Optional[float] = None     # Apple Silicon Tiler %
    enc_util_pct: Optional[float] = None       # NVIDIA Encoder %
    dec_util_pct: Optional[float] = None       # NVIDIA Decoder %
    ane_util_pct: Optional[float] = None       # Apple Neural Engine %

    # Memory / VRAM (bytes)
    vram_used_bytes: int = 0
    vram_alloc_bytes: int = 0
    vram_free_bytes: int = 0
    vram_total_bytes: int = 0
    vram_pct: float = 0.0
    unified_memory: bool = False
    bar1_used_bytes: int = 0
    bar1_total_bytes: int = 0
    pb_alloc_bytes: int = 0                    # Apple Parameter Buffer size
    tiled_scene_bytes: int = 0                 # Apple Tiled Scene bytes

    # Clocks & Performance State
    gpu_clock_mhz: int = 0
    gpu_clock_max_mhz: int = 0
    mem_clock_mhz: int = 0
    mem_clock_max_mhz: int = 0
    pstate: str = ""                           # P0..P15
    throttle_reasons: str = "None"             # nvtop: Idle, PwrCap, ThermSlow, HwSlow, SwPwrCap, None
    pstate_dist: Dict[str, float] = field(default_factory=dict)
    compute_mode: str = "Default"
    mig_mode: str = ""

    # Power, Thermal, Cooling
    power_draw_w: Optional[float] = None
    power_limit_w: Optional[float] = None
    power_pct: Optional[float] = None
    peak_power_w: Optional[float] = None
    ane_power_w: Optional[float] = None
    temp_c: Optional[float] = None
    temp_slowdown_c: Optional[float] = None
    temp_shutdown_c: Optional[float] = None
    fan_speed_pct: Optional[float] = None
    fan_rpm: Optional[int] = None

    # Interconnect (PCIe / NVLink on NVIDIA)
    pcie_gen: Optional[int] = None
    pcie_gen_max: Optional[int] = None
    pcie_width: Optional[int] = None
    pcie_width_max: Optional[int] = None
    pcie_rx_kbps: Optional[float] = None
    pcie_tx_kbps: Optional[float] = None
    nvlink_active: int = 0
    ecc_errors: Optional[int] = None

    # Active GPU processes on this device
    processes: List[GpuProcessStat] = field(default_factory=list)


@dataclass
class MemStats:
    total_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0
    available_bytes: int = 0
    cached_bytes: int = 0
    buffers_bytes: int = 0
    wired_bytes: int = 0
    compressed_bytes: int = 0
    app_bytes: int = 0
    shared_bytes: int = 0
    used_pct: float = 0.0
    available_pct: float = 0.0
    cached_pct: float = 0.0
    free_pct: float = 0.0

    swap_total_bytes: int = 0
    swap_used_bytes: int = 0
    swap_free_bytes: int = 0
    swap_pct: float = 0.0
    pageins: int = 0
    pageouts: int = 0
    swap_in_bps: float = 0.0
    swap_out_bps: float = 0.0
    pressure_level: str = "Normal"             # macmon: Normal / Warn / Critical
    psi_mem_pct: Optional[float] = None


@dataclass
class DiskMountStat:
    mount_point: str
    device: str
    fstype: str
    total_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0
    used_pct: float = 0.0
    read_bps: float = 0.0
    write_bps: float = 0.0


@dataclass
class DiskIOStats:
    read_bps: float = 0.0
    write_bps: float = 0.0
    read_total_bytes: int = 0
    write_total_bytes: int = 0
    read_iops: float = 0.0
    write_iops: float = 0.0
    psi_io_pct: Optional[float] = None
    mounts: List[DiskMountStat] = field(default_factory=list)


@dataclass
class NetInterfaceStat:
    name: str
    ipv4: str = ""
    ipv6: str = ""
    is_up: bool = True
    rx_bps: float = 0.0
    tx_bps: float = 0.0
    rx_peak_bps: float = 0.0
    tx_peak_bps: float = 0.0
    rx_total_bytes: int = 0
    tx_total_bytes: int = 0
    rx_pps: float = 0.0
    tx_pps: float = 0.0


@dataclass
class ProcessInfo:
    pid: int
    ppid: int = 0
    name: str = ""
    cmdline: str = ""
    smart_cmd: str = ""
    user: str = ""
    state: str = "S"
    nice: int = 0
    threads: int = 1
    cpu_pct: float = 0.0
    mem_bytes: int = 0
    mem_pct: float = 0.0
    io_read_bytes: int = 0
    io_write_bytes: int = 0
    elapsed_sec: float = 0.0
    cwd: str = ""
    gpu_env: str = ""
    group_count: int = 1

    # GPU fields (nvtop integration)
    gpu_index: Optional[int] = None
    gpu_kind: str = ""       # "G", "C", "C+G", "M"
    gpu_pct: float = 0.0
    vram_bytes: int = 0
    vram_pct: float = 0.0
    gpu_api: str = ""

    # Tree rendering helper
    tree_prefix: str = ""
    tree_depth: int = 0


@dataclass
class SystemSnapshot:
    timestamp: float
    hostname: str
    os_name: str
    os_release: str
    arch: str
    cpu: CpuStats
    battery: BatteryStats
    gpus: List[GpuDeviceStats]
    mem: MemStats
    disk: DiskIOStats
    net: Dict[str, NetInterfaceStat]
    primary_net_iface: str
    processes: List[ProcessInfo]
    total_processes: int = 0
    total_threads: int = 0
    gpu_process_count: int = 0
    sensors: List[SensorReading] = field(default_factory=list)
    alerts: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
