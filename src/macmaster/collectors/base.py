"""Unified cross-platform telemetry collector and historical time-series manager."""

from __future__ import annotations
from collections import deque
import platform
import socket
import sys
import time
from typing import Deque, Dict, List, Optional

from ..models import (
    BatteryStats,
    CpuStats,
    DiskIOStats,
    GpuDeviceStats,
    GpuProcessStat,
    MemStats,
    NetInterfaceStat,
    ProcessInfo,
    SensorReading,
    SystemSnapshot,
)
from .nvidia_gpu import NvidiaGpuCollector


class TelemetryHistory:
    """Rolling ring-buffers for all btop and nvtop historical Braille graphs."""

    def __init__(self, maxlen: int = 300) -> None:
        self.maxlen = maxlen
        self.cpu_total: Deque[float] = deque(maxlen=maxlen)
        self.cpu_user: Deque[float] = deque(maxlen=maxlen)
        self.cpu_sys: Deque[float] = deque(maxlen=maxlen)
        self.core_hist: Dict[int, Deque[float]] = {}

        # Per-GPU histories: gpu_index -> deque
        self.gpu_util: Dict[int, Deque[float]] = {}
        self.gpu_mem: Dict[int, Deque[float]] = {}
        self.gpu_power: Dict[int, Deque[float]] = {}
        self.gpu_temp: Dict[int, Deque[float]] = {}
        self.gpu_enc: Dict[int, Deque[float]] = {}
        self.gpu_dec: Dict[int, Deque[float]] = {}

        # Memory & Disk I/O histories
        self.mem_used: Deque[float] = deque(maxlen=maxlen)
        self.swap_used: Deque[float] = deque(maxlen=maxlen)
        self.disk_read_bps: Deque[float] = deque(maxlen=maxlen)
        self.disk_write_bps: Deque[float] = deque(maxlen=maxlen)

        # Network RX/TX histories per interface
        self.net_rx_bps: Dict[str, Deque[float]] = {}
        self.net_tx_bps: Dict[str, Deque[float]] = {}

        # Per-process mini sparkline histories
        self.proc_cpu: Dict[int, Deque[float]] = {}
        self.proc_gpu: Dict[int, Deque[float]] = {}

    def record(self, snap: SystemSnapshot) -> None:
        self.cpu_total.append(snap.cpu.total_pct)
        self.cpu_user.append(snap.cpu.user_pct)
        self.cpu_sys.append(snap.cpu.sys_pct)
        for c in snap.cpu.cores:
            if c.core_id not in self.core_hist:
                self.core_hist[c.core_id] = deque(maxlen=60)
            self.core_hist[c.core_id].append(c.usage_pct)

        for g in snap.gpus:
            idx = g.index
            if idx not in self.gpu_util:
                self.gpu_util[idx] = deque(maxlen=self.maxlen)
                self.gpu_mem[idx] = deque(maxlen=self.maxlen)
                self.gpu_power[idx] = deque(maxlen=self.maxlen)
                self.gpu_temp[idx] = deque(maxlen=self.maxlen)
                self.gpu_enc[idx] = deque(maxlen=self.maxlen)
                self.gpu_dec[idx] = deque(maxlen=self.maxlen)
            self.gpu_util[idx].append(g.gpu_util_pct)
            self.gpu_mem[idx].append(g.vram_pct)
            self.gpu_power[idx].append(g.power_pct or 0.0)
            self.gpu_temp[idx].append(g.temp_c or 0.0)
            self.gpu_enc[idx].append(g.enc_util_pct if g.enc_util_pct is not None else (g.renderer_util_pct or 0.0))
            self.gpu_dec[idx].append(g.dec_util_pct if g.dec_util_pct is not None else (g.tiler_util_pct or 0.0))

        self.mem_used.append(snap.mem.used_pct)
        self.swap_used.append(snap.mem.swap_pct)
        self.disk_read_bps.append(snap.disk.read_bps)
        self.disk_write_bps.append(snap.disk.write_bps)

        for ifname, nstat in snap.net.items():
            if ifname not in self.net_rx_bps:
                self.net_rx_bps[ifname] = deque(maxlen=self.maxlen)
                self.net_tx_bps[ifname] = deque(maxlen=self.maxlen)
            self.net_rx_bps[ifname].append(nstat.rx_bps)
            self.net_tx_bps[ifname].append(nstat.tx_bps)

        active_pids = set()
        for p in snap.processes:
            if p.cpu_pct > 0.1 or p.gpu_pct > 0.1 or p.pid in self.proc_cpu:
                active_pids.add(p.pid)
                if p.pid not in self.proc_cpu:
                    self.proc_cpu[p.pid] = deque(maxlen=30)
                    self.proc_gpu[p.pid] = deque(maxlen=30)
                self.proc_cpu[p.pid].append(p.cpu_pct)
                self.proc_gpu[p.pid].append(p.gpu_pct)

        # Prune exited PIDs from sparkline cache if large
        if len(self.proc_cpu) > 1000:
            dead = [k for k in self.proc_cpu if k not in active_pids]
            for k in dead:
                self.proc_cpu.pop(k, None)
                self.proc_gpu.pop(k, None)


class UnifiedCollector:
    """Orchestrates macOS (Apple Silicon) and Ubuntu (Linux + NVIDIA) collectors."""

    def __init__(self, simulate_nvidia: bool = False) -> None:
        self.sys_platform = sys.platform
        self.hostname = socket.gethostname().split(".")[0]
        self.arch = platform.machine()
        self.simulate_nvidia = simulate_nvidia
        self.history = TelemetryHistory(maxlen=300)

        if self.sys_platform == "darwin":
            from .darwin_native import DarwinNativeCollector
            self.os_name = "macOS"
            self.os_release = platform.mac_ver()[0] or platform.release()
            self.darwin = DarwinNativeCollector()
            self.linux = None
        else:
            from .linux_native import LinuxNativeCollector
            self.os_name, self.os_release = self._detect_linux_distro()
            self.darwin = None
            self.linux = LinuxNativeCollector()

        self.nvidia = NvidiaGpuCollector(simulate=simulate_nvidia)

    def _detect_linux_distro(self) -> tuple[str, str]:
        try:
            with open("/etc/os-release", "r", encoding="utf-8") as f:
                data = {}
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        data[k] = v.strip('"')
                return data.get("NAME", "Linux"), data.get("VERSION_ID", platform.release())
        except Exception:
            return "Linux", platform.release()

    def collect(self) -> SystemSnapshot:
        now = time.time()
        gpus: List[GpuDeviceStats] = []

        if self.darwin is not None:
            cpu, ior_data = self.darwin.collect_cpu_and_power()
            battery = self.darwin.collect_battery()
            apple_gpus = self.darwin.collect_gpus(ior_data)
            gpus.extend(apple_gpus)
            if self.simulate_nvidia:
                nv_gpus = self.nvidia.collect()
                for ng in nv_gpus:
                    ng.index = len(gpus)
                    for p in ng.processes:
                        p.gpu_index = ng.index
                    gpus.append(ng)
            gpu_proc_map: Dict[int, GpuProcessStat] = {}
            for g in gpus:
                for gp in g.processes:
                    if gp.pid not in gpu_proc_map or gp.gpu_util_pct > gpu_proc_map[gp.pid].gpu_util_pct:
                        gpu_proc_map[gp.pid] = gp

            mem = self.darwin.collect_memory()
            disk = self.darwin.collect_disks()
            net, primary_iface = self.darwin.collect_network()
            processes, total_threads = self.darwin.collect_processes(gpu_proc_map)
        else:
            assert self.linux is not None
            cpu = self.linux.collect_cpu()
            battery = self.linux.collect_battery()
            gpus.extend(self.nvidia.collect())
            gpu_proc_map = {}
            for g in gpus:
                for gp in g.processes:
                    if gp.pid not in gpu_proc_map or gp.gpu_util_pct > gpu_proc_map[gp.pid].gpu_util_pct:
                        gpu_proc_map[gp.pid] = gp

            mem = self.linux.collect_memory()
            disk = self.linux.collect_disks()
            net, primary_iface = self.linux.collect_network()
            processes, total_threads = self.linux.collect_processes(gpu_proc_map)

        # Backfill process names into GPU device process lists
        pid_to_proc = {p.pid: p for p in processes}
        for g in gpus:
            for gp in g.processes:
                if gp.pid in pid_to_proc and not gp.process_name:
                    gp.process_name = pid_to_proc[gp.pid].name

        # Aggregate hardware sensors & bottleneck alerts
        sensors: List[SensorReading] = []
        if self.darwin is not None:
            sensors.extend(ior_data.get("sensors", []))
        elif self.linux is not None:
            sensors.extend(self.linux.last_sensors)
        for g in gpus:
            if g.vendor == "NVIDIA":
                if g.power_draw_w is not None:
                    sensors.append(SensorReading(name=f"GPU{g.index} Power", category="power", value=g.power_draw_w, unit="W"))
                if g.temp_c is not None:
                    sensors.append(SensorReading(name=f"GPU{g.index} Temp", category="temp", value=g.temp_c, unit="°C"))
                if g.fan_speed_pct is not None:
                    sensors.append(SensorReading(name=f"GPU{g.index} Fan", category="fan", value=g.fan_speed_pct, unit="%"))

        alerts: List[str] = []
        if cpu.temp_c is not None and cpu.temp_c >= 90.0:
            alerts.append(f"CPU Temp High ({cpu.temp_c:.1f}°C)")
        if cpu.thermal_state not in ("Nominal", "Fair", ""):
            alerts.append(f"CPU Thermal {cpu.thermal_state}")
        for g in gpus:
            if g.vram_pct >= 90.0:
                alerts.append(f"GPU{g.index} VRAM {g.vram_pct:.0f}%")
            if g.temp_c is not None and g.temp_c >= 85.0:
                alerts.append(f"GPU{g.index} Temp {g.temp_c:.1f}°C")
            if g.throttle_reasons and g.throttle_reasons not in ("None", "Idle", "AppClk", "SyncBoost"):
                alerts.append(f"GPU{g.index} Throttle:{g.throttle_reasons}")
        if mem.pressure_level in ("Warn", "Critical"):
            alerts.append(f"Mem Pressure:{mem.pressure_level}")
        if mem.swap_total_bytes > 0 and mem.swap_pct >= 85.0:
            alerts.append(f"Swap High ({mem.swap_pct:.0f}%)")
        for m in disk.mounts:
            if m.used_pct >= 92.0:
                alerts.append(f"Disk {m.mount_point} {m.used_pct:.0f}%")

        snap = SystemSnapshot(
            timestamp=now,
            hostname=self.hostname,
            os_name=self.os_name,
            os_release=self.os_release,
            arch=self.arch,
            cpu=cpu,
            battery=battery,
            gpus=gpus,
            mem=mem,
            disk=disk,
            net=net,
            primary_net_iface=primary_iface,
            processes=processes,
            total_processes=len(processes),
            total_threads=total_threads,
            gpu_process_count=len(gpu_proc_map),
            sensors=sensors,
            alerts=alerts,
        )
        self.history.record(snap)
        return snap

    def inspect_process(self, pid: int) -> tuple[str, str]:
        """Inspect process CWD and GPU/AI environment variables on demand."""
        if self.darwin is not None:
            return self.darwin.inspect_process(pid)
        if self.linux is not None:
            return self.linux.inspect_process(pid)
        return "", ""
