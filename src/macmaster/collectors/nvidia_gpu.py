"""NVIDIA GPU collector (nvtop parity) via native NVML ctypes + nvidia-smi fallback."""

from __future__ import annotations
import ctypes
from ctypes import (
    c_char,
    c_int,
    c_uint,
    c_ulonglong,
    c_void_p,
    Structure,
    POINTER,
    byref,
    sizeof,
)
import math
import os
import shutil
import subprocess
import time
from typing import Dict, List, Optional

from ..models import GpuDeviceStats, GpuProcessStat


class _NvmlUtilization(Structure):
    _fields_ = [("gpu", c_uint), ("memory", c_uint)]


class _NvmlMemory(Structure):
    _fields_ = [("total", c_ulonglong), ("free", c_ulonglong), ("used", c_ulonglong)]


class _NvmlBar1Memory(Structure):
    _fields_ = [("bar1Total", c_ulonglong), ("bar1Free", c_ulonglong), ("bar1Used", c_ulonglong)]


class _NvmlPciInfo(Structure):
    _fields_ = [
        ("busIdLegacy", c_char * 16),
        ("domain", c_uint),
        ("bus", c_uint),
        ("device", c_uint),
        ("pciDeviceId", c_uint),
        ("pciSubSystemId", c_uint),
        ("busId", c_char * 32),
    ]


class _NvmlProcessInfoV2(Structure):
    _fields_ = [
        ("pid", c_uint),
        ("usedGpuMemory", c_ulonglong),
        ("gpuInstanceId", c_uint),
        ("computeInstanceId", c_uint),
    ]


class _NvmlProcessUtilizationSample(Structure):
    _fields_ = [
        ("pid", c_uint),
        ("timeStamp", c_ulonglong),
        ("smUtil", c_uint),
        ("memUtil", c_uint),
        ("encUtil", c_uint),
        ("decUtil", c_uint),
    ]


class NvidiaGpuCollector:
    """Collects full nvtop-equivalent metrics from NVIDIA GPUs via NVML or nvidia-smi."""

    def __init__(self, simulate: bool = False) -> None:
        self.simulate = simulate
        self.nvml = None
        self.initialized = False
        self._last_util_ts: Dict[int, int] = {}
        self.peak_power_map: Dict[int, float] = {}
        self._init_nvml()

    def _init_nvml(self) -> None:
        if self.simulate:
            return
        candidates = [
            "libnvidia-ml.so.1",
            "libnvidia-ml.so",
            "/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1",
            "/usr/lib/aarch64-linux-gnu/libnvidia-ml.so.1",
            "/usr/lib64/libnvidia-ml.so.1",
            "/usr/lib/wsl/lib/libnvidia-ml.so.1",
        ]
        for path in candidates:
            try:
                lib = ctypes.cdll.LoadLibrary(path)
                res = lib.nvmlInit_v2()
                if res == 0:
                    self.nvml = lib
                    self.initialized = True
                    break
            except Exception:
                continue

    def is_available(self) -> bool:
        if self.simulate:
            return True
        if self.initialized and self.nvml is not None:
            return True
        return shutil.which("nvidia-smi") is not None

    def collect(self) -> List[GpuDeviceStats]:
        if self.simulate:
            return self._collect_simulated()
        if self.initialized and self.nvml is not None:
            try:
                return self._collect_nvml()
            except Exception:
                pass
        if shutil.which("nvidia-smi"):
            try:
                return self._collect_smi()
            except Exception:
                pass
        return []

    def _fn(self, *names: str):
        if not self.nvml:
            return None
        for name in names:
            fn = getattr(self.nvml, name, None)
            if fn is not None:
                return fn
        return None

    def _collect_nvml(self) -> List[GpuDeviceStats]:
        lib = self.nvml
        if lib is None:
            return []

        cnt = c_uint(0)
        fn_cnt = self._fn("nvmlDeviceGetCount_v2", "nvmlDeviceGetCount")
        if not fn_cnt or fn_cnt(byref(cnt)) != 0 or cnt.value == 0:
            return []

        # Driver & CUDA version
        drv_str = ""
        buf = ctypes.create_string_buffer(80)
        if lib.nvmlSystemGetDriverVersion(buf, 80) == 0:
            drv_str = buf.value.decode("utf-8", "ignore")

        cuda_ver_str = "CUDA"
        cuda_ver = c_int(0)
        fn_cuda = self._fn("nvmlSystemGetCudaDriverVersion_v2", "nvmlSystemGetCudaDriverVersion")
        if fn_cuda and fn_cuda(byref(cuda_ver)) == 0 and cuda_ver.value > 0:
            maj = cuda_ver.value // 1000
            min_v = (cuda_ver.value % 1000) // 10
            cuda_ver_str = f"CUDA {maj}.{min_v}"

        devices: List[GpuDeviceStats] = []
        fn_handle = self._fn("nvmlDeviceGetHandleByIndex_v2", "nvmlDeviceGetHandleByIndex")
        if not fn_handle:
            return []

        for idx in range(cnt.value):
            handle = c_void_p()
            if fn_handle(c_uint(idx), byref(handle)) != 0:
                continue

            nbuf = ctypes.create_string_buffer(96)
            name = f"NVIDIA GPU {idx}"
            if lib.nvmlDeviceGetName(handle, nbuf, 96) == 0:
                name = nbuf.value.decode("utf-8", "ignore")

            ubuf = ctypes.create_string_buffer(96)
            uuid = ""
            if lib.nvmlDeviceGetUUID(handle, ubuf, 96) == 0:
                uuid = ubuf.value.decode("utf-8", "ignore")

            bus_id = ""
            pci = _NvmlPciInfo()
            fn_pci = self._fn("nvmlDeviceGetPciInfo_v3", "nvmlDeviceGetPciInfo_v2", "nvmlDeviceGetPciInfo")
            if fn_pci and fn_pci(handle, byref(pci)) == 0:
                bus_id = (pci.busId or pci.busIdLegacy).decode("utf-8", "ignore")

            dev = GpuDeviceStats(
                index=idx,
                vendor="NVIDIA",
                name=name,
                uuid=uuid,
                bus_id=bus_id,
                driver_version=drv_str,
                api_version=cuda_ver_str,
                unified_memory=False,
            )

            # Utilization rates
            util = _NvmlUtilization()
            if lib.nvmlDeviceGetUtilizationRates(handle, byref(util)) == 0:
                dev.gpu_util_pct = float(util.gpu)
                dev.mem_ctrl_util_pct = float(util.memory)

            # Memory
            mem = _NvmlMemory()
            if lib.nvmlDeviceGetMemoryInfo(handle, byref(mem)) == 0:
                dev.vram_total_bytes = int(mem.total)
                dev.vram_free_bytes = int(mem.free)
                dev.vram_used_bytes = int(mem.used)
                dev.vram_alloc_bytes = int(mem.used)
                if mem.total > 0:
                    dev.vram_pct = round(mem.used * 100.0 / mem.total, 1)

            # BAR1 Memory
            bar1 = _NvmlBar1Memory()
            fn_bar1 = self._fn("nvmlDeviceGetBAR1MemoryInfo")
            if fn_bar1 and fn_bar1(handle, byref(bar1)) == 0:
                dev.bar1_used_bytes = int(bar1.bar1Used)
                dev.bar1_total_bytes = int(bar1.bar1Total)

            # Clocks (0 = Graphics, 1 = SM, 2 = Memory)
            clk = c_uint(0)
            if lib.nvmlDeviceGetClockInfo(handle, c_uint(0), byref(clk)) == 0:
                dev.gpu_clock_mhz = int(clk.value)
            if lib.nvmlDeviceGetMaxClockInfo(handle, c_uint(0), byref(clk)) == 0:
                dev.gpu_clock_max_mhz = int(clk.value)
            if lib.nvmlDeviceGetClockInfo(handle, c_uint(2), byref(clk)) == 0:
                dev.mem_clock_mhz = int(clk.value)
            if lib.nvmlDeviceGetMaxClockInfo(handle, c_uint(2), byref(clk)) == 0:
                dev.mem_clock_max_mhz = int(clk.value)

            # Performance State (P0..P15)
            pstate = c_uint(0)
            fn_pstate = self._fn("nvmlDeviceGetPerformanceState", "nvmlDeviceGetPowerState")
            if fn_pstate and fn_pstate(handle, byref(pstate)) == 0:
                dev.pstate = f"P{pstate.value}"

            # Compute Mode
            cmode = c_uint(0)
            fn_cmode = self._fn("nvmlDeviceGetComputeMode")
            if fn_cmode and fn_cmode(handle, byref(cmode)) == 0:
                modes = {0: "Default", 1: "Exclusive_Thread", 2: "Prohibited", 3: "Exclusive_Process"}
                dev.compute_mode = modes.get(cmode.value, "Default")

            # Temperature & Thresholds
            temp = c_uint(0)
            if lib.nvmlDeviceGetTemperature(handle, c_uint(0), byref(temp)) == 0:
                dev.temp_c = float(temp.value)
            fn_thresh = self._fn("nvmlDeviceGetTemperatureThreshold")
            if fn_thresh:
                t_slow = c_uint(0)
                t_shut = c_uint(0)
                if fn_thresh(handle, c_uint(1), byref(t_slow)) == 0:
                    dev.temp_slowdown_c = float(t_slow.value)
                if fn_thresh(handle, c_uint(0), byref(t_shut)) == 0:
                    dev.temp_shutdown_c = float(t_shut.value)

            # Fan Speed
            fan = c_uint(0)
            if lib.nvmlDeviceGetFanSpeed(handle, byref(fan)) == 0:
                dev.fan_speed_pct = float(fan.value)

            # Power Draw & Limit (milliwatts -> Watts)
            pwr = c_uint(0)
            if lib.nvmlDeviceGetPowerUsage(handle, byref(pwr)) == 0:
                dev.power_draw_w = round(pwr.value / 1000.0, 1)
                self.peak_power_map[idx] = max(self.peak_power_map.get(idx, 0.0), dev.power_draw_w)
                dev.peak_power_w = self.peak_power_map[idx]
            plim = c_uint(0)
            fn_plim = self._fn("nvmlDeviceGetEnforcedPowerLimit", "nvmlDeviceGetPowerManagementLimit")
            if fn_plim and fn_plim(handle, byref(plim)) == 0 and plim.value > 0:
                dev.power_limit_w = round(plim.value / 1000.0, 1)
                if dev.power_draw_w is not None and dev.power_limit_w > 0:
                    dev.power_pct = round(dev.power_draw_w * 100.0 / dev.power_limit_w, 1)

            # Clock Throttle Reasons bitmask (nvitop / nvtop parity)
            fn_thr = self._fn("nvmlDeviceGetCurrentClocksThrottleReasons", "nvmlDeviceGetCurrentClocksEventReasons")
            if fn_thr:
                reasons = c_ulonglong(0)
                if fn_thr(handle, byref(reasons)) == 0:
                    rval = int(reasons.value)
                    active_reasons = []
                    if rval & 0x080:
                        active_reasons.append("PwrBrake")
                    if rval & 0x040:
                        active_reasons.append("HwTherm")
                    if rval & 0x020:
                        active_reasons.append("ThermSlow")
                    if rval & 0x008:
                        active_reasons.append("HwSlow")
                    if rval & 0x004:
                        active_reasons.append("SwPwrCap")
                    if rval & 0x010:
                        active_reasons.append("SyncBoost")
                    if rval & 0x002:
                        active_reasons.append("AppClk")
                    if not active_reasons and (rval & 0x001):
                        active_reasons.append("Idle")
                    dev.throttle_reasons = ",".join(active_reasons) if active_reasons else "None"

            # Optional MIG & NVLink status
            fn_mig = self._fn("nvmlDeviceGetMigMode")
            if fn_mig:
                cur_mig = c_uint(0)
                pend_mig = c_uint(0)
                if fn_mig(handle, byref(cur_mig), byref(pend_mig)) == 0 and cur_mig.value == 1:
                    dev.mig_mode = "Enabled"
            fn_nvl = self._fn("nvmlDeviceGetNvLinkState")
            if fn_nvl:
                nvl_st = c_uint(0)
                if fn_nvl(handle, c_uint(0), byref(nvl_st)) == 0 and nvl_st.value == 1:
                    dev.nvlink_active = True

            # Encoder & Decoder Utilization
            enc_u = c_uint(0)
            samp_p = c_uint(0)
            if lib.nvmlDeviceGetEncoderUtilization(handle, byref(enc_u), byref(samp_p)) == 0:
                dev.enc_util_pct = float(enc_u.value)
            dec_u = c_uint(0)
            if lib.nvmlDeviceGetDecoderUtilization(handle, byref(dec_u), byref(samp_p)) == 0:
                dev.dec_util_pct = float(dec_u.value)

            # PCIe Gen, Width & Throughput (KB/s)
            val = c_uint(0)
            if lib.nvmlDeviceGetCurrPcieLinkGeneration(handle, byref(val)) == 0:
                dev.pcie_gen = int(val.value)
            if lib.nvmlDeviceGetMaxPcieLinkGeneration(handle, byref(val)) == 0:
                dev.pcie_gen_max = int(val.value)
            if lib.nvmlDeviceGetCurrPcieLinkWidth(handle, byref(val)) == 0:
                dev.pcie_width = int(val.value)
            if lib.nvmlDeviceGetMaxPcieLinkWidth(handle, byref(val)) == 0:
                dev.pcie_width_max = int(val.value)
            fn_tp = self._fn("nvmlDeviceGetPcieThroughput")
            if fn_tp:
                tx_kb = c_uint(0)
                rx_kb = c_uint(0)
                if fn_tp(handle, c_uint(0), byref(tx_kb)) == 0:
                    dev.pcie_tx_kbps = float(tx_kb.value)
                if fn_tp(handle, c_uint(1), byref(rx_kb)) == 0:
                    dev.pcie_rx_kbps = float(rx_kb.value)

            # Per-process GPU utilization & VRAM
            dev.processes = self._collect_nvml_processes(handle, idx, dev.vram_total_bytes)
            devices.append(dev)

        return devices

    def _collect_nvml_processes(
        self, handle: c_void_p, gpu_idx: int, total_vram: int
    ) -> List[GpuProcessStat]:
        proc_map: Dict[int, GpuProcessStat] = {}

        # 1. Compute and Graphics running processes (VRAM + Kind C/G)
        for kind_code, fn_names in [
            ("C", ("nvmlDeviceGetComputeRunningProcesses_v3", "nvmlDeviceGetComputeRunningProcesses_v2", "nvmlDeviceGetComputeRunningProcesses")),
            ("G", ("nvmlDeviceGetGraphicsRunningProcesses_v3", "nvmlDeviceGetGraphicsRunningProcesses_v2", "nvmlDeviceGetGraphicsRunningProcesses")),
        ]:
            fn = self._fn(*fn_names)
            if not fn:
                continue
            count = c_uint(0)
            fn(handle, byref(count), None)
            if count.value > 0:
                arr = (_NvmlProcessInfoV2 * (count.value + 8))()
                count = c_uint(count.value + 8)
                if fn(handle, byref(count), byref(arr)) == 0:
                    for i in range(count.value):
                        p = arr[i]
                        pid = int(p.pid)
                        if pid <= 0:
                            continue
                        vram = int(p.usedGpuMemory)
                        # NVML_VALUE_NOT_AVAILABLE = -1 ull
                        if vram > (1 << 60):
                            vram = 0
                        if pid in proc_map:
                            existing = proc_map[pid]
                            if kind_code not in existing.kind:
                                existing.kind = "C+G"
                            existing.vram_bytes = max(existing.vram_bytes, vram)
                            existing.vram_pct = round(existing.vram_bytes * 100.0 / total_vram, 1) if total_vram > 0 else 0.0
                        else:
                            proc_map[pid] = GpuProcessStat(
                                pid=pid,
                                gpu_index=gpu_idx,
                                kind=kind_code,
                                vram_bytes=vram,
                                vram_pct=round(vram * 100.0 / total_vram, 1) if total_vram > 0 else 0.0,
                                api="CUDA" if kind_code == "C" else "GL/VK",
                            )

        # 2. Per-process SM / Mem / Enc / Dec utilization samples
        fn_putil = self._fn("nvmlDeviceGetProcessUtilization")
        if fn_putil:
            last_ts = c_ulonglong(self._last_util_ts.get(gpu_idx, 0))
            scount = c_uint(0)
            fn_putil(handle, None, byref(scount), last_ts)
            if scount.value > 0 and scount.value < 2048:
                sarr = (_NvmlProcessUtilizationSample * scount.value)()
                if fn_putil(handle, byref(sarr), byref(scount), last_ts) == 0:
                    max_ts = self._last_util_ts.get(gpu_idx, 0)
                    for i in range(scount.value):
                        s = sarr[i]
                        if s.timeStamp > max_ts:
                            max_ts = int(s.timeStamp)
                        pid = int(s.pid)
                        if pid <= 0:
                            continue
                        sm = float(s.smUtil) if s.smUtil <= 100 else 0.0
                        mem = float(s.memUtil) if s.memUtil <= 100 else 0.0
                        enc = float(s.encUtil) if s.encUtil <= 100 else 0.0
                        dec = float(s.decUtil) if s.decUtil <= 100 else 0.0
                        if pid not in proc_map and (sm > 0 or mem > 0):
                            proc_map[pid] = GpuProcessStat(
                                pid=pid,
                                gpu_index=gpu_idx,
                                kind="C",
                                api="CUDA",
                            )
                        if pid in proc_map:
                            proc_map[pid].gpu_util_pct = max(proc_map[pid].gpu_util_pct, sm)
                            proc_map[pid].mem_util_pct = max(proc_map[pid].mem_util_pct, mem)
                            proc_map[pid].enc_util_pct = max(proc_map[pid].enc_util_pct, enc)
                            proc_map[pid].dec_util_pct = max(proc_map[pid].dec_util_pct, dec)
                    self._last_util_ts[gpu_idx] = max_ts

        return list(proc_map.values())

    def _collect_smi(self) -> List[GpuDeviceStats]:
        """Fallback parser using nvidia-smi CSV query when NVML library is not directly loadable."""
        fields = [
            "index",
            "name",
            "uuid",
            "pci.bus_id",
            "driver_version",
            "utilization.gpu",
            "utilization.memory",
            "utilization.encoder",
            "utilization.decoder",
            "memory.total",
            "memory.used",
            "memory.free",
            "clocks.current.graphics",
            "clocks.max.graphics",
            "clocks.current.memory",
            "clocks.max.memory",
            "pstate",
            "compute_mode",
            "temperature.gpu",
            "fan.speed",
            "power.draw",
            "power.limit",
            "pcie.link.gen.current",
            "pcie.link.gen.max",
            "pcie.link.width.current",
            "pcie.link.width.max",
        ]
        out = subprocess.check_output(
            ["nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"],
            text=True,
            timeout=2.0,
            stderr=subprocess.DEVNULL,
        )
        devices: List[GpuDeviceStats] = []
        uuid_to_idx: Dict[str, int] = {}

        def _f(v: str) -> Optional[float]:
            v = v.strip()
            if not v or "N/A" in v or "Not Supported" in v:
                return None
            try:
                return float(v)
            except ValueError:
                return None

        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < len(fields):
                continue
            idx = int(_f(parts[0]) or 0)
            uuid = parts[2]
            uuid_to_idx[uuid] = idx
            mem_tot = int((_f(parts[9]) or 0) * 1024 * 1024)
            mem_used = int((_f(parts[10]) or 0) * 1024 * 1024)
            mem_free = int((_f(parts[11]) or 0) * 1024 * 1024)
            p_draw = _f(parts[20])
            p_lim = _f(parts[21])
            g_util = _f(parts[5]) or 0.0
            t_c = _f(parts[18])
            p_st = parts[16]
            if p_draw is not None:
                self.peak_power_map[idx] = max(self.peak_power_map.get(idx, 0.0), p_draw)
            thr_reason = "None"
            if t_c is not None and t_c >= 85.0:
                thr_reason = "ThermSlow"
            elif p_draw is not None and p_lim is not None and p_lim > 0 and p_draw >= p_lim * 0.95:
                thr_reason = "SwPwrCap"
            elif g_util < 5.0 and p_st in ("P8", "P10", "P12"):
                thr_reason = "Idle"
            dev = GpuDeviceStats(
                index=idx,
                vendor="NVIDIA",
                name=parts[1],
                uuid=uuid,
                bus_id=parts[3],
                driver_version=parts[4],
                api_version="CUDA",
                gpu_util_pct=g_util,
                mem_ctrl_util_pct=_f(parts[6]) or 0.0,
                enc_util_pct=_f(parts[7]),
                dec_util_pct=_f(parts[8]),
                vram_total_bytes=mem_tot,
                vram_used_bytes=mem_used,
                vram_alloc_bytes=mem_used,
                vram_free_bytes=mem_free,
                vram_pct=round(mem_used * 100.0 / mem_tot, 1) if mem_tot > 0 else 0.0,
                gpu_clock_mhz=int(_f(parts[12]) or 0),
                gpu_clock_max_mhz=int(_f(parts[13]) or 0),
                mem_clock_mhz=int(_f(parts[14]) or 0),
                mem_clock_max_mhz=int(_f(parts[15]) or 0),
                pstate=p_st,
                compute_mode=parts[17],
                throttle_reasons=thr_reason,
                temp_c=t_c,
                fan_speed_pct=_f(parts[19]),
                power_draw_w=p_draw,
                power_limit_w=p_lim,
                power_pct=round(p_draw * 100.0 / p_lim, 1) if (p_draw and p_lim and p_lim > 0) else None,
                peak_power_w=self.peak_power_map.get(idx),
                pcie_gen=int(_f(parts[22]) or 0) or None,
                pcie_gen_max=int(_f(parts[23]) or 0) or None,
                pcie_width=int(_f(parts[24]) or 0) or None,
                pcie_width_max=int(_f(parts[25]) or 0) or None,
            )
            devices.append(dev)

        # Also query pmon for per-process GPU SM% and VRAM
        try:
            pmon_out = subprocess.check_output(
                ["nvidia-smi", "pmon", "-c", "1", "-s", "um"],
                text=True,
                timeout=1.5,
                stderr=subprocess.DEVNULL,
            )
            for line in pmon_out.strip().splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                cols = line.split()
                if len(cols) >= 8 and cols[1] != "-":
                    g_idx = int(cols[0])
                    pid = int(cols[1])
                    kind = cols[2]
                    sm_u = float(cols[3]) if cols[3] != "-" else 0.0
                    mem_u = float(cols[4]) if cols[4] != "-" else 0.0
                    enc_u = float(cols[5]) if cols[5] != "-" else 0.0
                    dec_u = float(cols[6]) if cols[6] != "-" else 0.0
                    fb_mb = int(cols[7]) if cols[7] != "-" else 0
                    vram_b = fb_mb * 1024 * 1024
                    if 0 <= g_idx < len(devices):
                        tot_v = devices[g_idx].vram_total_bytes
                        devices[g_idx].processes.append(
                            GpuProcessStat(
                                pid=pid,
                                process_name=cols[8] if len(cols) > 8 else "",
                                gpu_index=g_idx,
                                kind=kind,
                                gpu_util_pct=sm_u,
                                mem_util_pct=mem_u,
                                enc_util_pct=enc_u,
                                dec_util_pct=dec_u,
                                vram_bytes=vram_b,
                                vram_pct=round(vram_b * 100.0 / tot_v, 1) if tot_v > 0 else 0.0,
                                api="CUDA" if "C" in kind else "GL/VK",
                            )
                        )
        except Exception:
            pass

        return devices

    def _collect_simulated(self) -> List[GpuDeviceStats]:
        """Simulated dual-NVIDIA GPU telemetry for cross-platform UI verification."""
        t = time.time()
        u0 = round(68.0 + 24.0 * math.sin(t * 0.7), 1)
        u1 = round(42.0 + 30.0 * math.cos(t * 0.5), 1)
        tot = 80 * 1024**3  # 80 GiB H100
        used0 = int(tot * (0.64 + 0.05 * math.sin(t * 0.2)))
        used1 = int(tot * (0.38 + 0.04 * math.cos(t * 0.3)))
        my_pid = os.getpid()
        return [
            GpuDeviceStats(
                index=0,
                vendor="NVIDIA",
                name="NVIDIA H100 80GB HBM3",
                uuid="GPU-9a8b7c6d-0000-1111-2222-333344445555",
                bus_id="00000000:17:00.0",
                driver_version="560.35.03",
                api_version="CUDA 12.6",
                gpu_util_pct=u0,
                mem_ctrl_util_pct=round(u0 * 0.72, 1),
                enc_util_pct=0.0,
                dec_util_pct=12.0,
                vram_total_bytes=tot,
                vram_used_bytes=used0,
                vram_alloc_bytes=used0,
                vram_free_bytes=tot - used0,
                vram_pct=round(used0 * 100.0 / tot, 1),
                bar1_used_bytes=256 * 1024**2,
                bar1_total_bytes=128 * 1024**3,
                gpu_clock_mhz=1830,
                gpu_clock_max_mhz=1980,
                mem_clock_mhz=2619,
                mem_clock_max_mhz=2619,
                pstate="P0",
                compute_mode="Default",
                throttle_reasons="SwPwrCap",
                power_draw_w=round(485.0 + 65.0 * math.sin(t * 0.7), 1),
                power_limit_w=700.0,
                power_pct=round((485.0 + 65.0 * math.sin(t * 0.7)) * 100.0 / 700.0, 1),
                peak_power_w=550.0,
                nvlink_active=True,
                temp_c=round(66.0 + 4.0 * math.sin(t * 0.4), 1),
                temp_slowdown_c=85.0,
                temp_shutdown_c=90.0,
                fan_speed_pct=58.0,
                pcie_gen=5,
                pcie_gen_max=5,
                pcie_width=16,
                pcie_width_max=16,
                pcie_rx_kbps=round(1420000 + 320000 * math.sin(t), 0),
                pcie_tx_kbps=round(980000 + 210000 * math.cos(t), 0),
                processes=[
                    GpuProcessStat(
                        pid=my_pid,
                        process_name="python3",
                        gpu_index=0,
                        kind="C",
                        gpu_util_pct=u0,
                        mem_util_pct=round(u0 * 0.6, 1),
                        vram_bytes=used0,
                        vram_pct=round(used0 * 100.0 / tot, 1),
                        api="CUDA",
                    )
                ],
            ),
            GpuDeviceStats(
                index=1,
                vendor="NVIDIA",
                name="NVIDIA H100 80GB HBM3",
                uuid="GPU-9a8b7c6d-0001-1111-2222-333344445555",
                bus_id="00000000:CA:00.0",
                driver_version="560.35.03",
                api_version="CUDA 12.6",
                gpu_util_pct=u1,
                mem_ctrl_util_pct=round(u1 * 0.65, 1),
                enc_util_pct=4.0,
                dec_util_pct=0.0,
                vram_total_bytes=tot,
                vram_used_bytes=used1,
                vram_alloc_bytes=used1,
                vram_free_bytes=tot - used1,
                vram_pct=round(used1 * 100.0 / tot, 1),
                gpu_clock_mhz=1620,
                gpu_clock_max_mhz=1980,
                mem_clock_mhz=2619,
                mem_clock_max_mhz=2619,
                pstate="P0",
                compute_mode="Default",
                throttle_reasons="None",
                power_draw_w=round(340.0 + 50.0 * math.cos(t * 0.5), 1),
                power_limit_w=700.0,
                power_pct=round((340.0 + 50.0 * math.cos(t * 0.5)) * 100.0 / 700.0, 1),
                peak_power_w=390.0,
                nvlink_active=True,
                temp_c=round(59.0 + 3.0 * math.cos(t * 0.4), 1),
                temp_slowdown_c=85.0,
                temp_shutdown_c=90.0,
                fan_speed_pct=49.0,
                pcie_gen=5,
                pcie_gen_max=5,
                pcie_width=16,
                pcie_width_max=16,
                pcie_rx_kbps=round(820000 + 180000 * math.cos(t), 0),
                pcie_tx_kbps=round(610000 + 140000 * math.sin(t), 0),
            ),
        ]
