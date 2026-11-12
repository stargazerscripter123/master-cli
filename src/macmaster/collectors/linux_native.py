"""Native Ubuntu / Linux collector reading /proc and /sys with zero external dependencies."""

from __future__ import annotations
import fcntl
import glob
import os
import platform
import pwd
import socket
import struct
import time
from typing import Dict, List, Optional, Tuple

from ..models import (
    BatteryStats,
    CoreStat,
    CpuStats,
    DiskIOStats,
    DiskMountStat,
    GpuProcessStat,
    MemStats,
    NetInterfaceStat,
    ProcessInfo,
    SensorReading,
    format_smart_cmdline,
)


class LinuxNativeCollector:
    """High-speed native Linux (/proc + /sys) collector for Ubuntu."""

    def __init__(self) -> None:
        self.page_size = os.sysconf("SC_PAGE_SIZE")
        self.clk_tck = os.sysconf("SC_CLK_TCK") or 100
        self.total_mem = self._read_total_mem()
        self.arch = platform.machine()
        self.cpu_model, self.phys_cores, self.log_cores = self._read_cpu_metadata()

        # State for deltas
        self.prev_cpu_ticks: Dict[str, Tuple[int, ...]] = {}
        self.prev_rapl: Tuple[int, float] = (0, 0.0)
        self.prev_disk_stats: Tuple[int, int, int, int, float] = (0, 0, 0, 0, 0.0)
        self.prev_dev_io: Dict[str, Tuple[int, int, float]] = {}
        self.prev_net_stats: Dict[str, Tuple[int, int, int, int, float]] = {}
        self.net_peaks: Dict[str, Tuple[float, float]] = {}
        self.prev_proc_cpu: Dict[int, Tuple[int, float]] = {}
        self.uid_cache: Dict[int, str] = {}
        self.peak_cpu_power_w: float = 0.0
        self.last_uptime_sec: float = 0.0
        self.last_sensors: List[SensorReading] = []

        self._prime_deltas()

    def _read_total_mem(self) -> int:
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) * 1024
        except Exception:
            pass
        return os.sysconf("SC_PHYS_PAGES") * self.page_size

    def _read_cpu_metadata(self) -> Tuple[str, int, int]:
        model = ""
        phys_ids = set()
        core_ids = set()
        log_count = 0
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                cur_phys = "0"
                for line in f:
                    if ":" not in line:
                        continue
                    k, v = [x.strip() for x in line.split(":", 1)]
                    if k in ("model name", "Hardware", "Processor") and not model:
                        model = v
                    elif k == "processor":
                        log_count += 1
                    elif k == "physical id":
                        cur_phys = v
                        phys_ids.add(v)
                    elif k == "core id":
                        core_ids.add((cur_phys, v))
        except Exception:
            pass
        log_c = log_count or os.cpu_count() or 1
        phys_c = len(core_ids) if core_ids else log_c
        return model or platform.processor() or "Linux CPU", phys_c, log_c

    def _prime_deltas(self) -> None:
        self.prev_cpu_ticks = self._read_proc_stat()
        rb, wb, rop, wop, dev_map = self._read_raw_diskstats()
        now = time.perf_counter()
        self.prev_disk_stats = (rb, wb, rop, wop, now)
        self.prev_dev_io = {k: (v[0], v[1], now) for k, v in dev_map.items()}
        self.prev_net_stats = self._read_raw_net()
        rapl = self._read_rapl_uj()
        if rapl is not None:
            self.prev_rapl = (rapl, now)
        self.collect_processes({})

    def _read_proc_stat(self) -> Dict[str, Tuple[int, ...]]:
        out: Dict[str, Tuple[int, ...]] = {}
        try:
            with open("/proc/stat", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("cpu"):
                        parts = line.split()
                        out[parts[0]] = tuple(int(x) for x in parts[1:9])
        except Exception:
            pass
        return out

    def _read_core_freqs(self, num_cores: int) -> Tuple[List[int], int]:
        freqs: List[int] = []
        max_mhz = 0
        for i in range(num_cores):
            p_cur = f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq"
            p_max = f"/sys/devices/system/cpu/cpu{i}/cpufreq/cpuinfo_max_freq"
            mhz = 0
            try:
                if os.path.exists(p_cur):
                    with open(p_cur, "r", encoding="utf-8") as f:
                        mhz = int(f.read().strip()) // 1000
                if os.path.exists(p_max):
                    with open(p_max, "r", encoding="utf-8") as f:
                        max_mhz = max(max_mhz, int(f.read().strip()) // 1000)
            except Exception:
                pass
            freqs.append(mhz)

        if not any(freqs):
            try:
                with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                    idx = 0
                    for line in f:
                        if line.startswith("cpu MHz") and ":" in line:
                            val = int(float(line.split(":", 1)[1].strip()))
                            if idx < len(freqs):
                                freqs[idx] = val
                            idx += 1
            except Exception:
                pass
        return freqs, max_mhz or (max(freqs) if freqs else 0)

    def _read_psi_avg10(self, resource: str) -> Optional[float]:
        """Read Linux Pressure Stall Information (PSI) avg10 for cpu/memory/io."""
        p = f"/proc/pressure/{resource}"
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("some "):
                            for tok in line.split():
                                if tok.startswith("avg10="):
                                    return round(float(tok.split("=", 1)[1]), 2)
        except Exception:
            pass
        return None

    def _read_hwmon_temps_and_fans(self) -> Tuple[Optional[float], List[Tuple[str, int, int, float]], List[SensorReading]]:
        temps: List[float] = []
        fans: List[Tuple[str, int, int, float]] = []
        sensors: List[SensorReading] = []
        for hw_dir in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            hw_name = ""
            try:
                name_p = os.path.join(hw_dir, "name")
                if os.path.exists(name_p):
                    with open(name_p, "r", encoding="utf-8") as f:
                        hw_name = f.read().strip()
            except Exception:
                pass
            for tp in sorted(glob.glob(os.path.join(hw_dir, "temp*_input"))):
                try:
                    with open(tp, "r", encoding="utf-8") as f:
                        v = round(int(f.read().strip()) / 1000.0, 1)
                    if 10.0 < v < 125.0:
                        lbl = hw_name or os.path.basename(hw_dir)
                        lbl_p = tp.replace("_input", "_label")
                        if os.path.exists(lbl_p):
                            try:
                                with open(lbl_p, "r", encoding="utf-8") as lf:
                                    lbl = f"{hw_name}: {lf.read().strip()}"
                            except Exception:
                                pass
                        sensors.append(SensorReading(name=lbl, category="temp", value=v, unit="°C"))
                        if hw_name in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz", "thinkpad"):
                            temps.append(v)
                except Exception:
                    continue
            for fp in sorted(glob.glob(os.path.join(hw_dir, "fan*_input"))):
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        rpm = int(f.read().strip())
                    mx_p = fp.replace("_input", "_max")
                    mx = 5000
                    if os.path.exists(mx_p):
                        try:
                            with open(mx_p, "r", encoding="utf-8") as mf:
                                mx = max(1000, int(mf.read().strip()))
                        except Exception:
                            pass
                    pct = round(min(100.0, rpm * 100.0 / mx), 1)
                    fan_name = f"Fan {len(fans)}"
                    fans.append((fan_name, rpm, mx, pct))
                    sensors.append(SensorReading(name=f"{hw_name or 'System'} {fan_name}", category="fan", value=float(rpm), unit="RPM"))
                except Exception:
                    continue

        if not temps:
            for tz in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
                try:
                    with open(tz, "r", encoding="utf-8") as f:
                        v = round(int(f.read().strip()) / 1000.0, 1)
                        if 10.0 < v < 125.0:
                            temps.append(v)
                            tdir = os.path.dirname(tz)
                            tname = os.path.basename(tdir)
                            tp_file = os.path.join(tdir, "type")
                            if os.path.exists(tp_file):
                                with open(tp_file, "r", encoding="utf-8") as tf:
                                    tname = tf.read().strip()
                            sensors.append(SensorReading(name=tname, category="temp", value=v, unit="°C"))
                except Exception:
                    continue

        cpu_temp = round(max(temps), 1) if temps else None
        return cpu_temp, fans, sensors

    def _read_rapl_uj(self) -> Optional[int]:
        for p in (
            "/sys/class/powercap/intel-rapl:0/energy_uj",
            "/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj",
        ):
            try:
                if os.path.exists(p) and os.access(p, os.R_OK):
                    with open(p, "r", encoding="utf-8") as f:
                        return int(f.read().strip())
            except Exception:
                continue
        return None

    def collect_cpu(self) -> CpuStats:
        cur_stat = self._read_proc_stat()
        prev_stat = self.prev_cpu_ticks
        self.prev_cpu_ticks = cur_stat

        def _calc_pcts(cur_t: Tuple[int, ...], prev_t: Tuple[int, ...]) -> Tuple[float, float, float, float, float, float]:
             deltas = [max(0, c - p) for c, p in zip(cur_t, prev_t)]
             tot = sum(deltas)
             if tot <= 0:
                 return 0.0, 0.0, 0.0, 100.0, 0.0, 0.0
             user = deltas[0] * 100.0 / tot
             nice = deltas[1] * 100.0 / tot
             sys_p = (deltas[2] + (deltas[5] if len(deltas) > 5 else 0) + (deltas[6] if len(deltas) > 6 else 0)) * 100.0 / tot
             idle = deltas[3] * 100.0 / tot
             iowait = (deltas[4] if len(deltas) > 4 else 0) * 100.0 / tot
             active = min(100.0, max(0.0, 100.0 - idle - iowait))
             return round(active, 1), round(user, 1), round(sys_p, 1), round(idle, 1), round(nice, 1), round(iowait, 1)

        t_cur = cur_stat.get("cpu", (0, 0, 0, 100, 0, 0, 0, 0))
        t_prev = prev_stat.get("cpu", t_cur)
        total_pct, user_pct, sys_pct, idle_pct, nice_pct, iowait_pct = _calc_pcts(t_cur, t_prev)

        freqs, max_mhz = self._read_core_freqs(self.log_cores)
        cpu_temp, fans, sensors = self._read_hwmon_temps_and_fans()

        # RAPL CPU package power
        cpu_power_w = None
        now = time.perf_counter()
        rapl_uj = self._read_rapl_uj()
        if rapl_uj is not None:
            p_uj, p_t = self.prev_rapl
            if p_t > 0 and rapl_uj >= p_uj:
                cpu_power_w = round((rapl_uj - p_uj) / 1e6 / max(1e-3, now - p_t), 1)
                self.peak_cpu_power_w = max(self.peak_cpu_power_w, cpu_power_w)
                sensors.insert(0, SensorReading(name="CPU Package (RAPL)", category="power", value=cpu_power_w, unit="W"))
            self.prev_rapl = (rapl_uj, now)
        self.last_sensors = sensors

        cores: List[CoreStat] = []
        for idx in range(self.log_cores):
            key = f"cpu{idx}"
            if key not in cur_stat:
                continue
            c_cur = cur_stat[key]
            c_prev = prev_stat.get(key, c_cur)
            c_act, c_usr, c_sys, _, _, c_io = _calc_pcts(c_cur, c_prev)
            cores.append(
                CoreStat(
                    core_id=idx,
                    label=f"C{idx}",
                    usage_pct=c_act,
                    user_pct=c_usr,
                    sys_pct=c_sys,
                    iowait_pct=c_io,
                    freq_mhz=freqs[idx] if idx < len(freqs) else 0,
                    temp_c=cpu_temp,
                )
            )

        uptime = 0.0
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as f:
                uptime = float(f.read().split()[0])
        except Exception:
            pass
        self.last_uptime_sec = uptime

        try:
            l1, l5, l15 = os.getloadavg()
        except Exception:
            l1 = l5 = l15 = 0.0

        avg_freq = int(round(sum(freqs) / len(freqs))) if any(freqs) else 0
        psi_cpu = self._read_psi_avg10("cpu")
        t_state = "Nominal"
        if cpu_temp is not None:
            if cpu_temp >= 95.0:
                t_state = "Critical"
            elif cpu_temp >= 87.0:
                t_state = "Serious"
            elif cpu_temp >= 78.0:
                t_state = "Fair"

        return CpuStats(
            model=self.cpu_model,
            arch=self.arch,
            physical_cores=self.phys_cores,
            logical_cores=self.log_cores,
            total_pct=total_pct,
            user_pct=user_pct,
            sys_pct=sys_pct,
            idle_pct=idle_pct,
            nice_pct=nice_pct,
            iowait_pct=iowait_pct,
            freq_mhz=avg_freq,
            freq_max_mhz=max_mhz,
            temp_c=cpu_temp,
            power_w=cpu_power_w,
            pkg_power_w=cpu_power_w,
            peak_cpu_power_w=self.peak_cpu_power_w if self.peak_cpu_power_w > 0 else cpu_power_w,
            thermal_state=t_state,
            psi_cpu_pct=psi_cpu,
            load_avg=(round(l1, 2), round(l5, 2), round(l15, 2)),
            uptime_sec=uptime,
            fans_rpm=fans,
            cores=cores,
        )

    def collect_battery(self) -> BatteryStats:
        bats = sorted(glob.glob("/sys/class/power_supply/BAT*"))
        if not bats:
            return BatteryStats(present=False)
        bdir = bats[0]
        try:
            with open(os.path.join(bdir, "capacity"), "r", encoding="utf-8") as f:
                pct = float(f.read().strip())
            status = "Unknown"
            st_p = os.path.join(bdir, "status")
            if os.path.exists(st_p):
                with open(st_p, "r", encoding="utf-8") as f:
                    status = f.read().strip()
            pwr_w = None
            pw_p = os.path.join(bdir, "power_now")
            if os.path.exists(pw_p):
                with open(pw_p, "r", encoding="utf-8") as f:
                    pwr_w = round(int(f.read().strip()) / 1e6, 1)
            return BatteryStats(present=True, percent=pct, status=status, power_w=pwr_w)
        except Exception:
            return BatteryStats(present=False)

    def collect_memory(self) -> MemStats:
        info: Dict[str, int] = {}
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if ":" in line:
                        k, rest = line.split(":", 1)
                        parts = rest.split()
                        if parts:
                            info[k.strip()] = int(parts[0]) * 1024
        except Exception:
            pass

        tot = info.get("MemTotal", self.total_mem)
        free = info.get("MemFree", 0)
        buffers = info.get("Buffers", 0)
        cached = info.get("Cached", 0) + info.get("SReclaimable", 0)
        shared = info.get("Shmem", 0)
        avail = info.get("MemAvailable", free + buffers + cached)
        used = max(0, tot - avail)
        sw_tot = info.get("SwapTotal", 0)
        sw_free = info.get("SwapFree", 0)
        sw_used = max(0, sw_tot - sw_free)
        used_pct = round(used * 100.0 / max(1, tot), 1)
        psi_mem = self._read_psi_avg10("memory")
        p_level = "Normal"
        if (psi_mem is not None and psi_mem >= 25.0) or used_pct >= 94.0:
            p_level = "Critical"
        elif (psi_mem is not None and psi_mem >= 5.0) or used_pct >= 84.0:
            p_level = "Warn"

        return MemStats(
            total_bytes=tot,
            used_bytes=used,
            free_bytes=free,
            available_bytes=avail,
            cached_bytes=cached,
            buffers_bytes=buffers,
            shared_bytes=shared,
            app_bytes=used,
            used_pct=used_pct,
            available_pct=round(avail * 100.0 / max(1, tot), 1),
            cached_pct=round(cached * 100.0 / max(1, tot), 1),
            free_pct=round(free * 100.0 / max(1, tot), 1),
            swap_total_bytes=sw_tot,
            swap_used_bytes=sw_used,
            swap_free_bytes=sw_free,
            swap_pct=round(sw_used * 100.0 / sw_tot, 1) if sw_tot > 0 else 0.0,
            pressure_level=p_level,
            psi_mem_pct=psi_mem,
        )

    def _read_raw_diskstats(self) -> Tuple[int, int, int, int, Dict[str, Tuple[int, int]]]:
        rb = wb = rop = wop = 0
        dev_map: Dict[str, Tuple[int, int]] = {}
        try:
            with open("/proc/diskstats", "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 14:
                        continue
                    dev = parts[2]
                    if dev.startswith(("loop", "ram", "dm-", "sr", "zram")):
                        continue
                    r_ios = int(parts[3])
                    r_sec = int(parts[5])
                    w_ios = int(parts[7])
                    w_sec = int(parts[9])
                    dev_rb = r_sec * 512
                    dev_wb = w_sec * 512
                    dev_map[dev] = (dev_rb, dev_wb)
                    # Sum only whole-disk block devices (nvme0n1, sda, vda, xvda)
                    if (
                        (dev.startswith("nvme") and "p" not in dev[4:])
                        or (dev.startswith(("sd", "vd", "xvd", "hd")) and not dev[-1].isdigit())
                    ):
                        rb += dev_rb
                        wb += dev_wb
                        rop += r_ios
                        wop += w_ios
        except Exception:
            pass
        return rb, wb, rop, wop, dev_map

    def collect_disks(self) -> DiskIOStats:
        now = time.perf_counter()
        rb, wb, rop, wop, dev_map = self._read_raw_diskstats()
        prb, pwb, prop, pwop, pt = self.prev_disk_stats
        dt = max(1e-3, now - pt)
        self.prev_disk_stats = (rb, wb, rop, wop, now)

        r_bps = max(0.0, (rb - prb) / dt)
        w_bps = max(0.0, (wb - pwb) / dt)
        r_iops = max(0.0, (rop - prop) / dt)
        w_iops = max(0.0, (wop - pwop) / dt)

        mounts: List[DiskMountStat] = []
        seen_devs = set()
        ignore_fs = {
            "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "securityfs", "cgroup", "cgroup2",
            "pstore", "efivarfs", "bpf", "autofs", "hugetlbfs", "mqueue", "debugfs", "tracefs",
            "fusectl", "configfs", "ramfs", "squashfs", "overlay", "nsfs",
        }
        try:
            with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    dev, mnt, fstype = parts[0], parts[1], parts[2]
                    if fstype in ignore_fs or mnt.startswith(("/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/docker")):
                        continue
                    if dev in seen_devs and dev.startswith("/dev/"):
                        continue
                    try:
                        st = os.statvfs(mnt)
                    except Exception:
                        continue
                    tot = st.f_blocks * st.f_frsize
                    if tot <= 0:
                        continue
                    seen_devs.add(dev)
                    free = st.f_bavail * st.f_frsize
                    used = max(0, (st.f_blocks - st.f_bfree) * st.f_frsize)
                    short_dev = os.path.basename(dev)
                    d_rbps = d_wbps = 0.0
                    if short_dev in dev_map:
                        crb, cwb = dev_map[short_dev]
                        p_drb, p_dwb, p_dt = self.prev_dev_io.get(short_dev, (crb, cwb, now - 1.0))
                        ddt = max(1e-3, now - p_dt)
                        d_rbps = max(0.0, (crb - p_drb) / ddt)
                        d_wbps = max(0.0, (cwb - p_dwb) / ddt)
                    mounts.append(
                        DiskMountStat(
                            mount_point=mnt,
                            device=dev,
                            fstype=fstype,
                            total_bytes=tot,
                            used_bytes=used,
                            free_bytes=free,
                            used_pct=round(used * 100.0 / tot, 1),
                            read_bps=d_rbps,
                            write_bps=d_wbps,
                        )
                    )
        except Exception:
            pass

        self.prev_dev_io = {k: (v[0], v[1], now) for k, v in dev_map.items()}
        return DiskIOStats(
            read_bps=r_bps,
            write_bps=w_bps,
            read_total_bytes=rb,
            write_total_bytes=wb,
            read_iops=round(r_iops, 1),
            write_iops=round(w_iops, 1),
            psi_io_pct=self._read_psi_avg10("io"),
            mounts=mounts,
        )

    def _read_raw_net(self) -> Dict[str, Tuple[int, int, int, int, float]]:
        now = time.perf_counter()
        out: Dict[str, Tuple[int, int, int, int, float]] = {}
        try:
            with open("/proc/net/dev", "r", encoding="utf-8") as f:
                for line in f.readlines()[2:]:
                    if ":" not in line:
                        continue
                    name, rest = line.split(":", 1)
                    name = name.strip()
                    cols = rest.split()
                    if len(cols) >= 10:
                        ib, ipk = int(cols[0]), int(cols[1])
                        ob, opk = int(cols[8]), int(cols[9])
                        out[name] = (ib, ob, ipk, opk, now)
        except Exception:
            pass
        return out

    def _get_iface_ipv4(self, ifname: str) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            res = fcntl.ioctl(
                s.fileno(),
                0x8915,  # SIOCGIFADDR
                struct.pack("256s", ifname[:15].encode("utf-8")),
            )
            s.close()
            return socket.inet_ntoa(res[20:24])
        except Exception:
            return ""

    def collect_network(self) -> Tuple[Dict[str, NetInterfaceStat], str]:
        cur_net = self._read_raw_net()
        prev_net = self.prev_net_stats
        self.prev_net_stats = cur_net

        stats: Dict[str, NetInterfaceStat] = {}
        for name, (ib, ob, ipk, opk, now) in cur_net.items():
            pib, pob, pipk, popk, pt = prev_net.get(name, (ib, ob, ipk, opk, now - 1.0))
            dt = max(1e-3, now - pt)
            rx_bps = max(0.0, (ib - pib) / dt)
            tx_bps = max(0.0, (ob - pob) / dt)
            rx_pps = max(0.0, (ipk - pipk) / dt)
            tx_pps = max(0.0, (opk - popk) / dt)
            pk_rx, pk_tx = self.net_peaks.get(name, (0.0, 0.0))
            pk_rx = max(pk_rx, rx_bps)
            pk_tx = max(pk_tx, tx_bps)
            self.net_peaks[name] = (pk_rx, pk_tx)
            ip4 = self._get_iface_ipv4(name)
            stats[name] = NetInterfaceStat(
                name=name,
                ipv4=ip4,
                is_up=True,
                rx_bps=rx_bps,
                tx_bps=tx_bps,
                rx_peak_bps=pk_rx,
                tx_peak_bps=pk_tx,
                rx_total_bytes=ib,
                tx_total_bytes=ob,
                rx_pps=round(rx_pps, 1),
                tx_pps=round(tx_pps, 1),
            )

        primary = "eth0"
        if primary not in stats:
            candidates = sorted(
                stats.values(),
                key=lambda s: (bool(s.ipv4 and s.name != "lo"), s.rx_total_bytes + s.tx_total_bytes),
                reverse=True,
            )
            if candidates:
                primary = candidates[0].name
        return stats, primary

    def _get_username(self, uid: int) -> str:
        if uid not in self.uid_cache:
            try:
                self.uid_cache[uid] = pwd.getpwuid(uid).pw_name
            except Exception:
                self.uid_cache[uid] = str(uid)
        return self.uid_cache[uid]

    def collect_processes(self, gpu_proc_map: Dict[int, GpuProcessStat]) -> Tuple[List[ProcessInfo], int]:
        now = time.perf_counter()
        if self.last_uptime_sec <= 0.0:
            try:
                with open("/proc/uptime", "r", encoding="utf-8") as uf:
                    self.last_uptime_sec = float(uf.read().split()[0])
            except Exception:
                pass
        procs: List[ProcessInfo] = []
        new_proc_cpu: Dict[int, Tuple[int, float]] = {}
        total_threads = 0

        try:
            entries = [int(x) for x in os.listdir("/proc") if x.isdigit()]
        except Exception:
            return [], 0

        for pid in entries:
            pdir = f"/proc/{pid}"
            try:
                with open(f"{pdir}/stat", "r", encoding="utf-8", errors="ignore") as f:
                    stat_str = f.read()
                rparen = stat_str.rfind(")")
                lparen = stat_str.find("(")
                if lparen == -1 or rparen == -1:
                    continue
                comm = stat_str[lparen + 1 : rparen]
                fields = stat_str[rparen + 2 :].split()
                state = fields[0]
                ppid = int(fields[1])
                utime = int(fields[11])
                stime = int(fields[12])
                nice = int(fields[16])
                threads = max(1, int(fields[17]))
                starttime_ticks = int(fields[19])
                rss_pages = int(fields[21])
                mem_bytes = max(0, rss_pages * self.page_size)
                st_info = os.stat(pdir)
                user = self._get_username(st_info.st_uid)
            except Exception:
                continue

            elapsed_sec = max(0.0, self.last_uptime_sec - (starttime_ticks / self.clk_tck)) if self.last_uptime_sec > 0 else 0.0
            tot_ticks = utime + stime
            new_proc_cpu[pid] = (tot_ticks, now)
            cpu_pct = 0.0
            if pid in self.prev_proc_cpu:
                p_ticks, p_t = self.prev_proc_cpu[pid]
                dt = max(1e-3, now - p_t)
                cpu_pct = max(0.0, (tot_ticks - p_ticks) * 100.0 / (self.clk_tck * dt))

            cmdline = comm
            try:
                with open(f"{pdir}/cmdline", "rb") as cf:
                    raw_cmd = cf.read(2048)
                    if raw_cmd:
                        cmdline = raw_cmd.replace(b"\x00", b" ").decode("utf-8", "ignore").strip() or comm
            except Exception:
                pass

            total_threads += threads
            gp = gpu_proc_map.get(pid)
            pinfo = ProcessInfo(
                pid=pid,
                ppid=ppid,
                name=comm,
                cmdline=cmdline,
                smart_cmd=format_smart_cmdline(comm, cmdline),
                user=user,
                state=state,
                nice=nice,
                threads=threads,
                cpu_pct=round(cpu_pct, 1),
                mem_bytes=mem_bytes,
                mem_pct=round(mem_bytes * 100.0 / max(1, self.total_mem), 1),
                elapsed_sec=round(elapsed_sec, 1),
            )
            if gp is not None:
                pinfo.gpu_index = gp.gpu_index
                pinfo.gpu_kind = gp.kind
                pinfo.gpu_pct = gp.gpu_util_pct
                pinfo.vram_bytes = gp.vram_bytes
                pinfo.vram_pct = gp.vram_pct
                pinfo.gpu_api = gp.api
            procs.append(pinfo)

        self.prev_proc_cpu = new_proc_cpu
        return procs, total_threads

    def inspect_process(self, pid: int) -> Tuple[str, str]:
        """On-demand inspection of process CWD and GPU/AI environment variables on Linux."""
        cwd = ""
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except Exception:
            pass
        env_matches: List[str] = []
        try:
            with open(f"/proc/{pid}/environ", "rb") as ef:
                raw_env = ef.read(16384)
            target_exact = {
                "CUDA_VISIBLE_DEVICES",
                "NVIDIA_VISIBLE_DEVICES",
                "CONDA_DEFAULT_ENV",
                "VIRTUAL_ENV",
                "METAL_DEVICE_WRAPPER_TYPE",
                "PYTORCH_ENABLE_MPS_FALLBACK",
                "OMP_NUM_THREADS",
                "MASTER_PORT",
                "LOCAL_RANK",
                "RANK",
                "WORLD_SIZE",
                "HF_HOME",
                "OLLAMA_MODELS",
            }
            for item in raw_env.split(b"\x00"):
                if not item or b"=" not in item:
                    continue
                s = item.decode("utf-8", "ignore")
                k, v = s.split("=", 1)
                if k in target_exact or k.startswith(("PYTORCH_", "MLX_")):
                    if k == "VIRTUAL_ENV":
                        v_clean = v.rstrip("/")
                        base_v = os.path.basename(v_clean)
                        parent_v = os.path.basename(os.path.dirname(v_clean))
                        if base_v in (".venv", "venv", "env") and parent_v:
                            s = f"{k}={parent_v}/{base_v}"
                        elif base_v:
                            s = f"{k}={base_v}"
                    env_matches.append(s)
                    if len(env_matches) >= 6:
                        break
        except Exception:
            pass
        return cwd, " │ ".join(env_matches[:6])
