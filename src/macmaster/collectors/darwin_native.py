"""Native macOS (Apple Silicon & Intel) collector using ctypes (Mach, IOKit, IOReport, AppleSMC, sysctl)."""

from __future__ import annotations
import ctypes
from ctypes import (
    c_bool,
    c_char,
    c_char_p,
    c_double,
    c_int,
    c_int32,
    c_int64,
    c_long,
    c_size_t,
    c_uint,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
    POINTER,
    Structure,
    byref,
    sizeof,
)
import os
import platform
import plistlib
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
    GpuDeviceStats,
    GpuProcessStat,
    MemStats,
    NetInterfaceStat,
    ProcessInfo,
    SensorReading,
    format_smart_cmdline,
)


# --- Mach & BSD Structures ---

class _MachTimebaseInfo(Structure):
    _fields_ = [("numer", c_uint32), ("denom", c_uint32)]


class _VmStatistics64(Structure):
    _fields_ = [
        ("free_count", c_uint),
        ("active_count", c_uint),
        ("inactive_count", c_uint),
        ("wire_count", c_uint),
        ("zero_fill_count", c_uint64),
        ("reactivations", c_uint64),
        ("pageins", c_uint64),
        ("pageouts", c_uint64),
        ("faults", c_uint64),
        ("cow_faults", c_uint64),
        ("lookups", c_uint64),
        ("hits", c_uint64),
        ("purges", c_uint64),
        ("purgeable_count", c_uint),
        ("speculative_count", c_uint),
        ("decompressions", c_uint64),
        ("compressions", c_uint64),
        ("swapins", c_uint64),
        ("swapouts", c_uint64),
        ("compressor_page_count", c_uint),
        ("throttled_count", c_uint),
        ("external_page_count", c_uint),
        ("internal_page_count", c_uint),
        ("total_uncompressed_pages_in_compressor", c_uint64),
    ]


class _XswUsage(Structure):
    _fields_ = [
        ("xsu_total", c_uint64),
        ("xsu_avail", c_uint64),
        ("xsu_used", c_uint64),
        ("xsu_pagesize", c_uint32),
        ("xsu_encrypted", c_bool),
    ]


class _Timeval64(Structure):
    _fields_ = [("tv_sec", c_int64), ("tv_usec", c_int64)]


class _Fsid(Structure):
    _fields_ = [("val", c_int32 * 2)]


class _Statfs64(Structure):
    _fields_ = [
        ("f_bsize", c_uint32),
        ("f_iosize", c_int32),
        ("f_blocks", c_uint64),
        ("f_bfree", c_uint64),
        ("f_bavail", c_uint64),
        ("f_files", c_uint64),
        ("f_ffree", c_uint64),
        ("f_fsid", _Fsid),
        ("f_owner", c_uint32),
        ("f_type", c_uint32),
        ("f_flags", c_uint32),
        ("f_fssubtype", c_uint32),
        ("f_fstypename", c_char * 16),
        ("f_mntonname", c_char * 1024),
        ("f_mntfromname", c_char * 1024),
        ("f_flags_ext", c_uint32),
        ("f_reserved", c_uint32 * 7),
    ]


class _Sockaddr(Structure):
    _fields_ = [("sa_len", c_uint8), ("sa_family", c_uint8), ("sa_data", c_uint8 * 26)]


class _Ifaddrs(Structure):
    pass


_Ifaddrs._fields_ = [
    ("ifa_next", POINTER(_Ifaddrs)),
    ("ifa_name", c_char_p),
    ("ifa_flags", c_uint),
    ("ifa_addr", POINTER(_Sockaddr)),
    ("ifa_netmask", POINTER(_Sockaddr)),
    ("ifa_dstaddr", POINTER(_Sockaddr)),
    ("ifa_data", c_void_p),
]


class _ProcTaskInfo(Structure):
    _fields_ = [
        ("pti_virtual_size", c_uint64),
        ("pti_resident_size", c_uint64),
        ("pti_total_user", c_uint64),
        ("pti_total_system", c_uint64),
        ("pti_threads_user", c_uint64),
        ("pti_threads_system", c_uint64),
        ("pti_policy", c_int32),
        ("pti_faults", c_int32),
        ("pti_pageins", c_int32),
        ("pti_cow_faults", c_int32),
        ("pti_messages_sent", c_int32),
        ("pti_messages_received", c_int32),
        ("pti_syscalls_mach", c_int32),
        ("pti_syscalls_unix", c_int32),
        ("pti_csw", c_int32),
        ("pti_threadnum", c_int32),
        ("pti_numrunning", c_int32),
        ("pti_priority", c_int32),
    ]


class _RusageInfoV4(Structure):
    _fields_ = [
        ("ri_uuid", c_uint8 * 16),
        ("ri_user_time", c_uint64),
        ("ri_system_time", c_uint64),
        ("ri_pkg_idle_wkups", c_uint64),
        ("ri_interrupt_wkups", c_uint64),
        ("ri_pageins", c_uint64),
        ("ri_wired_size", c_uint64),
        ("ri_resident_size", c_uint64),
        ("ri_phys_footprint", c_uint64),
        ("ri_proc_start_abstime", c_uint64),
        ("ri_proc_exit_abstime", c_uint64),
        ("ri_child_user_time", c_uint64),
        ("ri_child_system_time", c_uint64),
        ("ri_child_pkg_idle_wkups", c_uint64),
        ("ri_child_interrupt_wkups", c_uint64),
        ("ri_child_pageins", c_uint64),
        ("ri_child_elapsed_abstime", c_uint64),
        ("ri_diskio_bytesread", c_uint64),
        ("ri_diskio_byteswritten", c_uint64),
        ("ri_cpu_time_qos_default", c_uint64),
        ("ri_cpu_time_qos_maintenance", c_uint64),
        ("ri_cpu_time_qos_background", c_uint64),
        ("ri_cpu_time_qos_utility", c_uint64),
        ("ri_cpu_time_qos_legacy", c_uint64),
        ("ri_cpu_time_qos_user_initiated", c_uint64),
        ("ri_cpu_time_qos_user_interactive", c_uint64),
        ("ri_billed_system_time", c_uint64),
        ("ri_serviced_system_time", c_uint64),
        ("ri_logical_writes", c_uint64),
        ("ri_lifetime_max_phys_footprint", c_uint64),
        ("ri_instructions", c_uint64),
        ("ri_cycles", c_uint64),
        ("ri_billed_energy", c_uint64),
        ("ri_serviced_energy", c_uint64),
        ("ri_interval_max_phys_footprint", c_uint64),
        ("ri_runnable_time", c_uint64),
    ]


# --- AppleSMC Structures ---

class _SMCKeyDataVers(Structure):
    _fields_ = [
        ("major", c_char),
        ("minor", c_char),
        ("build", c_char),
        ("reserved", c_char),
        ("release", c_uint16),
    ]


class _SMCKeyDataPLimit(Structure):
    _fields_ = [
        ("version", c_uint16),
        ("length", c_uint16),
        ("cpuPLimit", c_uint32),
        ("gpuPLimit", c_uint32),
        ("memPLimit", c_uint32),
    ]


class _SMCKeyDataKeyInfo(Structure):
    _fields_ = [
        ("dataSize", c_uint32),
        ("dataType", c_uint32),
        ("dataAttributes", c_uint8),
    ]


class _SMCKeyData(Structure):
    _fields_ = [
        ("key", c_uint32),
        ("vers", _SMCKeyDataVers),
        ("pLimitData", _SMCKeyDataPLimit),
        ("keyInfo", _SMCKeyDataKeyInfo),
        ("result", c_uint8),
        ("status", c_uint8),
        ("data8", c_uint8),
        ("data32", c_uint32),
        ("bytes", c_uint8 * 32),
    ]


class DarwinNativeCollector:
    """High-speed native macOS collector for CPU, Apple Silicon GPU, Memory, Disk, Net, and Processes."""

    def __init__(self) -> None:
        self.libc = ctypes.cdll.LoadLibrary("/usr/lib/libSystem.B.dylib")
        self.cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.iokit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
        try:
            self.ior = ctypes.cdll.LoadLibrary("/usr/lib/libIOReport.dylib")
        except Exception:
            self.ior = None

        self._setup_ctypes()
        self.page_size = os.sysconf("SC_PAGE_SIZE")
        self.total_mem = os.sysconf("SC_PHYS_PAGES") * self.page_size

        tb = _MachTimebaseInfo()
        self.libc.mach_timebase_info(byref(tb))
        self.tb_numer = tb.numer or 1
        self.tb_denom = tb.denom or 1

        # Static CPU metadata
        self.cpu_model = self._sysctl_str("machdep.cpu.brand_string") or platform.processor() or "Apple Silicon"
        self.arch = platform.machine()
        self.phys_cores = self._sysctl_int("hw.physicalcpu") or os.cpu_count() or 1
        self.log_cores = self._sysctl_int("hw.logicalcpu") or os.cpu_count() or 1
        self.p_cores = self._sysctl_int("hw.perflevel0.logicalcpu") or 0
        self.e_cores = self._sysctl_int("hw.perflevel1.logicalcpu") or 0

        # Frequency tables from pmgr (E-cores, P-cores, GPU)
        self.e_freqs_mhz: List[int] = []
        self.p_freqs_mhz: List[int] = []
        self.gpu_freqs_mhz: List[int] = []
        self._load_pmgr_freqs()

        # AppleSMC connection
        self.smc_conn = c_uint32(0)
        self._smc_key_info_cache: Dict[str, Tuple[int, str]] = {}
        self._init_smc()

        # IOReport subscription for GPU/CPU clocks and GPU/ANE Energy
        self.ior_sub = c_void_p()
        self.ior_sub_ch = c_void_p()
        self.prev_ior_sample = c_void_p()
        self.prev_ior_time = 0.0
        self._init_ioreport()

        # Persistent HID client & thermal cache
        self.hid_client = self.iokit.IOHIDEventSystemClientCreate(None)
        self._cached_hid_temps: Tuple[Optional[float], Optional[float], float] = (None, None, 0.0)
        self._cached_hid_sensors: List[SensorReading] = []

        # Peak power trackers & VM page counters
        self.peak_sys_power_w: float = 0.0
        self.peak_cpu_power_w: float = 0.0
        self.peak_gpu_power_w: float = 0.0
        self.prev_vm_pages: Tuple[int, int, float] = (0, 0, 0.0)

        # Previous state for deltas
        self.prev_core_ticks: List[Tuple[int, int, int, int]] = []
        self.prev_disk_stats: Tuple[int, int, int, int, float] = (0, 0, 0, 0, 0.0)
        self.prev_net_stats: Dict[str, Tuple[int, int, int, int, float]] = {}
        self.net_peaks: Dict[str, Tuple[float, float]] = {}
        self.prev_proc_cpu: Dict[int, Tuple[int, float]] = {}
        self.prev_gpu_proc_ns: Dict[int, Tuple[int, float]] = {}
        self.uid_cache: Dict[int, str] = {}
        self.cmdline_cache: Dict[int, Tuple[str, str, float, str]] = {}

        # Prime initial delta states
        self._prime_deltas()

    def _setup_ctypes(self) -> None:
        self.libc.mach_host_self.restype = c_uint
        self.libc.mach_task_self.restype = c_uint
        self.libc.proc_pidpath.restype = c_int
        self.libc.proc_pidpath.argtypes = [c_int, c_void_p, c_uint32]

        self.cf.CFStringCreateWithCString.restype = c_void_p
        self.cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
        self.cf.CFStringGetCString.restype = c_bool
        self.cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
        self.cf.CFDictionaryGetValue.restype = c_void_p
        self.cf.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]
        self.cf.CFArrayGetCount.restype = c_long
        self.cf.CFArrayGetCount.argtypes = [c_void_p]
        self.cf.CFArrayGetValueAtIndex.restype = c_void_p
        self.cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_long]
        self.cf.CFPropertyListCreateData.restype = c_void_p
        self.cf.CFPropertyListCreateData.argtypes = [c_void_p, c_void_p, c_long, c_long, POINTER(c_void_p)]
        self.cf.CFDataGetBytePtr.restype = c_void_p
        self.cf.CFDataGetBytePtr.argtypes = [c_void_p]
        self.cf.CFDataGetLength.restype = c_long
        self.cf.CFDataGetLength.argtypes = [c_void_p]
        self.cf.CFRelease.argtypes = [c_void_p]

        self.iokit.IOServiceMatching.restype = c_void_p
        self.iokit.IOServiceMatching.argtypes = [c_char_p]
        self.iokit.IOServiceNameMatching.restype = c_void_p
        self.iokit.IOServiceNameMatching.argtypes = [c_char_p]
        self.iokit.IOServiceGetMatchingService.restype = c_uint32
        self.iokit.IOServiceGetMatchingService.argtypes = [c_uint32, c_void_p]
        self.iokit.IOServiceGetMatchingServices.restype = c_int
        self.iokit.IOServiceGetMatchingServices.argtypes = [c_uint32, c_void_p, POINTER(c_uint32)]
        self.iokit.IOIteratorNext.restype = c_uint32
        self.iokit.IOIteratorNext.argtypes = [c_uint32]
        self.iokit.IOObjectRelease.argtypes = [c_uint32]
        self.iokit.IORegistryEntryCreateCFProperties.restype = c_int
        self.iokit.IORegistryEntryCreateCFProperties.argtypes = [c_uint32, POINTER(c_void_p), c_void_p, c_uint32]
        self.iokit.IORegistryEntryGetChildIterator.restype = c_int
        self.iokit.IORegistryEntryGetChildIterator.argtypes = [c_uint32, c_char_p, POINTER(c_uint32)]
        self.iokit.IOServiceOpen.restype = c_int
        self.iokit.IOServiceOpen.argtypes = [c_uint32, c_uint32, c_uint32, POINTER(c_uint32)]
        self.iokit.IOConnectCallStructMethod.restype = c_int
        self.iokit.IOConnectCallStructMethod.argtypes = [
            c_uint32, c_uint32, c_void_p, c_size_t, c_void_p, POINTER(c_size_t)
        ]

        # IOHIDEventSystemClient for thermal sensors
        self.iokit.IOHIDEventSystemClientCreate.restype = c_void_p
        self.iokit.IOHIDEventSystemClientCreate.argtypes = [c_void_p]
        self.iokit.IOHIDEventSystemClientCopyServices.restype = c_void_p
        self.iokit.IOHIDEventSystemClientCopyServices.argtypes = [c_void_p]
        self.iokit.IOHIDServiceClientCopyProperty.restype = c_void_p
        self.iokit.IOHIDServiceClientCopyProperty.argtypes = [c_void_p, c_void_p]
        self.iokit.IOHIDServiceClientCopyEvent.restype = c_void_p
        self.iokit.IOHIDServiceClientCopyEvent.argtypes = [c_void_p, c_int64, c_int32, c_int64]
        self.iokit.IOHIDEventGetFloatValue.restype = c_double
        self.iokit.IOHIDEventGetFloatValue.argtypes = [c_void_p, c_uint32]

        if self.ior:
            self.ior.IOReportCopyChannelsInGroup.restype = c_void_p
            self.ior.IOReportCopyChannelsInGroup.argtypes = [c_void_p, c_void_p, c_int64, c_int64, c_int64]
            self.ior.IOReportMergeChannels.argtypes = [c_void_p, c_void_p, c_void_p]
            self.ior.IOReportCreateSubscription.restype = c_void_p
            self.ior.IOReportCreateSubscription.argtypes = [c_void_p, c_void_p, POINTER(c_void_p), c_int64, c_void_p]
            self.ior.IOReportCreateSamples.restype = c_void_p
            self.ior.IOReportCreateSamples.argtypes = [c_void_p, c_void_p, c_void_p]
            self.ior.IOReportCreateSamplesDelta.restype = c_void_p
            self.ior.IOReportCreateSamplesDelta.argtypes = [c_void_p, c_void_p, c_void_p]
            self.ior.IOReportChannelGetGroup.restype = c_void_p
            self.ior.IOReportChannelGetGroup.argtypes = [c_void_p]
            self.ior.IOReportChannelGetSubGroup.restype = c_void_p
            self.ior.IOReportChannelGetSubGroup.argtypes = [c_void_p]
            self.ior.IOReportChannelGetChannelName.restype = c_void_p
            self.ior.IOReportChannelGetChannelName.argtypes = [c_void_p]
            self.ior.IOReportChannelGetUnitLabel.restype = c_void_p
            self.ior.IOReportChannelGetUnitLabel.argtypes = [c_void_p]
            self.ior.IOReportSimpleGetIntegerValue.restype = c_int64
            self.ior.IOReportSimpleGetIntegerValue.argtypes = [c_void_p, c_int32]
            self.ior.IOReportStateGetCount.restype = c_int32
            self.ior.IOReportStateGetCount.argtypes = [c_void_p]
            self.ior.IOReportStateGetNameForIndex.restype = c_void_p
            self.ior.IOReportStateGetNameForIndex.argtypes = [c_void_p, c_int32]
            self.ior.IOReportStateGetResidency.restype = c_int64
            self.ior.IOReportStateGetResidency.argtypes = [c_void_p, c_int32]

    def _cfstr(self, s: str) -> c_void_p:
        return self.cf.CFStringCreateWithCString(None, s.encode("utf-8"), 0x08000100)

    def _from_cfstr(self, ref: c_void_p) -> str:
        if not ref:
            return ""
        buf = ctypes.create_string_buffer(256)
        if self.cf.CFStringGetCString(ref, buf, 256, 0x08000100):
            return buf.value.decode("utf-8", "ignore")
        return ""

    def _entry_to_dict(self, entry: int) -> dict:
        if not entry:
            return {}
        props = c_void_p()
        if self.iokit.IORegistryEntryCreateCFProperties(entry, byref(props), None, 0) != 0 or not props:
            return {}
        data = self.cf.CFPropertyListCreateData(None, props, 200, 0, None)  # binary plist v1_0
        self.cf.CFRelease(props)
        if not data:
            return {}
        ptr = self.cf.CFDataGetBytePtr(data)
        ln = self.cf.CFDataGetLength(data)
        raw = ctypes.string_at(ptr, ln)
        self.cf.CFRelease(data)
        try:
            return plistlib.loads(raw)
        except Exception:
            return {}

    def _sysctl_str(self, name: str) -> str:
        sz = c_size_t(0)
        bname = name.encode("utf-8")
        if self.libc.sysctlbyname(bname, None, byref(sz), None, 0) != 0 or sz.value == 0:
            return ""
        buf = ctypes.create_string_buffer(sz.value)
        if self.libc.sysctlbyname(bname, buf, byref(sz), None, 0) == 0:
            return buf.value.decode("utf-8", "ignore").strip()
        return ""

    def _sysctl_int(self, name: str) -> int:
        val = c_int64(0)
        sz = c_size_t(sizeof(val))
        if self.libc.sysctlbyname(name.encode("utf-8"), byref(val), byref(sz), None, 0) == 0:
            return int(val.value)
        return 0

    def _load_pmgr_freqs(self) -> None:
        srv = self.iokit.IOServiceGetMatchingService(0, self.iokit.IOServiceNameMatching(b"pmgr"))
        if not srv:
            return
        d = self._entry_to_dict(srv)
        self.iokit.IOObjectRelease(srv)

        def _parse_vstates(raw: bytes) -> List[int]:
            out: List[int] = []
            for i in range(0, len(raw), 8):
                if i + 8 > len(raw):
                    break
                v, _ = struct.unpack_from("<II", raw, i)
                if v == 0:
                    continue
                if v > 1_000_000:
                    out.append(v // 1_000_000)
                else:
                    # Apple M4 Q16 period format: 65536000 / period
                    out.append(int(round(65536000.0 / v)))
            return out

        if isinstance(d.get("voltage-states1"), bytes):
            self.e_freqs_mhz = _parse_vstates(d["voltage-states1"])
        if isinstance(d.get("voltage-states5"), bytes):
            self.p_freqs_mhz = _parse_vstates(d["voltage-states5"])
        if isinstance(d.get("voltage-states9"), bytes):
            self.gpu_freqs_mhz = _parse_vstates(d["voltage-states9"])

    def _init_smc(self) -> None:
        srv = self.iokit.IOServiceGetMatchingService(0, self.iokit.IOServiceMatching(b"AppleSMC"))
        if srv:
            self.iokit.IOServiceOpen(srv, self.libc.mach_task_self(), 0, byref(self.smc_conn))
            self.iokit.IOObjectRelease(srv)

    def _read_smc_float(self, key_str: str) -> Optional[float]:
        if not self.smc_conn.value or len(key_str) != 4:
            return None
        key_int = struct.unpack(">I", key_str.encode("ascii"))[0]
        if key_str not in self._smc_key_info_cache:
            inp = _SMCKeyData()
            out = _SMCKeyData()
            inp.key = key_int
            inp.data8 = 9  # kSMCGetKeyInfo
            out_sz = c_size_t(sizeof(_SMCKeyData))
            if (
                self.iokit.IOConnectCallStructMethod(
                    self.smc_conn, 2, byref(inp), sizeof(inp), byref(out), byref(out_sz)
                )
                != 0
                or out.keyInfo.dataSize == 0
            ):
                self._smc_key_info_cache[key_str] = (0, "")
                return None
            dtype = struct.pack(">I", out.keyInfo.dataType).decode("ascii", "ignore")
            self._smc_key_info_cache[key_str] = (int(out.keyInfo.dataSize), dtype)

        dsize, dtype = self._smc_key_info_cache[key_str]
        if dsize == 0:
            return None
        inp = _SMCKeyData()
        out = _SMCKeyData()
        inp.key = key_int
        inp.keyInfo.dataSize = dsize
        inp.data8 = 5  # kSMCReadKey
        out_sz = c_size_t(sizeof(_SMCKeyData))
        if (
            self.iokit.IOConnectCallStructMethod(
                self.smc_conn, 2, byref(inp), sizeof(inp), byref(out), byref(out_sz)
            )
            != 0
        ):
            return None
        raw = bytes(out.bytes[:dsize])
        if dtype == "flt " and dsize == 4:
            val = struct.unpack("<f", raw)[0]
            return None if (val != val) else float(val)
        if dtype == "ui8 " and dsize == 1:
            return float(raw[0])
        if dtype == "ui16" and dsize == 2:
            return float(struct.unpack(">H", raw)[0])
        if dtype == "sp78" and dsize == 2:
            return float(struct.unpack(">h", raw)[0]) / 256.0
        return None

    def _init_ioreport(self) -> None:
        if not self.ior:
            return
        try:
            g_energy = self._cfstr("Energy Model")
            g_gpu = self._cfstr("GPU Stats")
            sg_gpu = self._cfstr("GPU Performance States")
            g_cpu = self._cfstr("CPU Stats")
            sg_cpu = self._cfstr("CPU Complex Performance States")
            ch_e = self.ior.IOReportCopyChannelsInGroup(g_energy, None, 0, 0, 0)
            ch_g = self.ior.IOReportCopyChannelsInGroup(g_gpu, sg_gpu, 0, 0, 0)
            ch_c = self.ior.IOReportCopyChannelsInGroup(g_cpu, sg_cpu, 0, 0, 0)
            self.cf.CFRelease(g_energy)
            self.cf.CFRelease(g_gpu)
            self.cf.CFRelease(sg_gpu)
            self.cf.CFRelease(g_cpu)
            self.cf.CFRelease(sg_cpu)
            if ch_e and ch_g:
                self.ior.IOReportMergeChannels(ch_e, ch_g, None)
                self.cf.CFRelease(ch_g)
            if ch_e and ch_c:
                self.ior.IOReportMergeChannels(ch_e, ch_c, None)
                self.cf.CFRelease(ch_c)
            if ch_e:
                self.ior_sub = self.ior.IOReportCreateSubscription(None, ch_e, byref(self.ior_sub_ch), 0, None)
                self.cf.CFRelease(ch_e)
                if self.ior_sub:
                    self.prev_ior_sample = self.ior.IOReportCreateSamples(self.ior_sub, self.ior_sub_ch, None)
                    self.prev_ior_time = time.perf_counter()
        except Exception:
            pass

    def _sample_ioreport(self) -> Dict[str, object]:
        result: Dict[str, object] = {
            "gpu_power_w": None,
            "cpu_power_w": None,
            "ane_power_w": None,
            "dram_power_w": None,
            "gpu_freq_mhz": 0,
            "gpu_pstate": "OFF",
            "gpu_pstate_dist": {},
            "e_freq_mhz": 0,
            "p_freq_mhz": 0,
        }
        if not self.ior or not self.ior_sub or not self.prev_ior_sample:
            return result
        now = time.perf_counter()
        dt = max(now - self.prev_ior_time, 1e-3)
        cur_sample = self.ior.IOReportCreateSamples(self.ior_sub, self.ior_sub_ch, None)
        if not cur_sample:
            return result
        delta = self.ior.IOReportCreateSamplesDelta(self.prev_ior_sample, cur_sample, None)
        self.cf.CFRelease(self.prev_ior_sample)
        self.prev_ior_sample = cur_sample
        self.prev_ior_time = now
        if not delta:
            return result

        k_ch = self._cfstr("IOReportChannels")
        arr = self.cf.CFDictionaryGetValue(delta, k_ch)
        self.cf.CFRelease(k_ch)
        cnt = self.cf.CFArrayGetCount(arr) if arr else 0

        e_mhz_samples: List[float] = []
        p_mhz_samples: List[float] = []
        dram_watts_accum: float = 0.0
        has_dram: bool = False
        ane_watts_accum: float = 0.0
        has_ane: bool = False

        for i in range(cnt):
            item = self.cf.CFArrayGetValueAtIndex(arr, i)
            grp = self._from_cfstr(self.ior.IOReportChannelGetGroup(item))
            name = self._from_cfstr(self.ior.IOReportChannelGetChannelName(item))
            if grp == "Energy Model":
                unit = self._from_cfstr(self.ior.IOReportChannelGetUnitLabel(item)).strip()
                val = self.ior.IOReportSimpleGetIntegerValue(item, 0)
                if val > 0:
                    scale = 1e-9 if unit == "nJ" else (1e-6 if unit == "uJ" else 1e-3)
                    watts = (val * scale) / dt
                    if name == "GPU Energy" or (name == "GPU" and result["gpu_power_w"] is None):
                        result["gpu_power_w"] = round(watts, 2)
                    elif name == "CPU Energy":
                        result["cpu_power_w"] = round(watts, 2)
                    elif name.startswith("ANE") or "ANE" in name:
                        ane_watts_accum += watts
                        has_ane = True
                    elif name.startswith(("DRAM", "DCS")):
                        dram_watts_accum += watts
                        has_dram = True
            elif grp == "GPU Stats" and name == "GPUPH":
                sc = self.ior.IOReportStateGetCount(item)
                tot_res = 0
                active_res = 0
                weighted_freq = 0.0
                top_state = "OFF"
                top_state_res = -1
                dist: Dict[str, int] = {}
                for idx in range(sc):
                    sname = self._from_cfstr(self.ior.IOReportStateGetNameForIndex(item, idx))
                    res = int(self.ior.IOReportStateGetResidency(item, idx))
                    if res <= 0:
                        continue
                    tot_res += res
                    dist[sname] = res
                    if sname != "OFF" and sname.startswith("P"):
                        try:
                            p_idx = int(sname[1:]) - 1
                            if 0 <= p_idx < len(self.gpu_freqs_mhz):
                                weighted_freq += res * self.gpu_freqs_mhz[p_idx]
                                active_res += res
                        except ValueError:
                            pass
                        if res > top_state_res:
                            top_state_res = res
                            top_state = sname
                if active_res > 0:
                    result["gpu_freq_mhz"] = int(round(weighted_freq / active_res))
                    result["gpu_pstate"] = top_state
                elif self.gpu_freqs_mhz:
                    result["gpu_freq_mhz"] = self.gpu_freqs_mhz[0]
                    result["gpu_pstate"] = "P1"
                if tot_res > 0:
                    result["gpu_pstate_dist"] = {k: round(v * 100.0 / tot_res, 1) for k, v in dist.items()}
            elif grp == "CPU Stats":
                subgrp = self._from_cfstr(self.ior.IOReportChannelGetSubGroup(item))
                if subgrp == "CPU Complex Performance States" and name in ("ECPU", "PCPU", "PCPU1"):
                    freqs = self.e_freqs_mhz if name == "ECPU" else self.p_freqs_mhz
                    if freqs:
                        sc = self.ior.IOReportStateGetCount(item)
                        act_res = 0
                        w_freq = 0.0
                        p_offset = 0
                        for idx in range(sc):
                            sname = self._from_cfstr(self.ior.IOReportStateGetNameForIndex(item, idx))
                            res = int(self.ior.IOReportStateGetResidency(item, idx))
                            if sname in ("IDLE", "DOWN", "OFF"):
                                continue
                            if p_offset < len(freqs) and res > 0:
                                w_freq += res * freqs[p_offset]
                                act_res += res
                            p_offset += 1
                        if act_res > 0:
                            if name == "ECPU":
                                e_mhz_samples.append(w_freq / act_res)
                            else:
                                p_mhz_samples.append(w_freq / act_res)

        self.cf.CFRelease(delta)
        if has_ane:
            result["ane_power_w"] = round(ane_watts_accum, 2)
        if has_dram:
            result["dram_power_w"] = round(dram_watts_accum, 2)
        if e_mhz_samples:
            result["e_freq_mhz"] = int(round(sum(e_mhz_samples) / len(e_mhz_samples)))
        elif self.e_freqs_mhz:
            result["e_freq_mhz"] = self.e_freqs_mhz[0]
        if p_mhz_samples:
            result["p_freq_mhz"] = int(round(sum(p_mhz_samples) / len(p_mhz_samples)))
        elif self.p_freqs_mhz:
            result["p_freq_mhz"] = self.p_freqs_mhz[0]
        return result

    def _read_hid_temperatures(self) -> Tuple[Optional[float], Optional[float]]:
        """Reads Apple Silicon PMU tdie / tdev / NAND thermal sensors via IOHIDEventSystemClient."""
        now = time.perf_counter()
        if now - self._cached_hid_temps[2] < 2.0 and self._cached_hid_temps[0] is not None:
            return self._cached_hid_temps[0], self._cached_hid_temps[1]
        client = self.hid_client
        if not client:
            return None, None
        services = self.iokit.IOHIDEventSystemClientCopyServices(client)
        if not services:
            return None, None
        k_prod = self._cfstr("Product")
        count = self.cf.CFArrayGetCount(services)
        die_temps: List[float] = []
        dev_temps: List[float] = []
        sensor_map: Dict[str, float] = {}
        for i in range(count):
            srv = self.cf.CFArrayGetValueAtIndex(services, i)
            prod_ref = self.iokit.IOHIDServiceClientCopyProperty(srv, k_prod)
            if not prod_ref:
                continue
            name = self._from_cfstr(prod_ref)
            self.cf.CFRelease(prod_ref)
            if not name:
                continue
            ev = self.iokit.IOHIDServiceClientCopyEvent(srv, 15, 0, 0)
            if ev:
                val = float(self.iokit.IOHIDEventGetFloatValue(ev, 15 << 16))
                self.cf.CFRelease(ev)
                if 15.0 < val < 125.0:
                    sensor_map[name] = max(sensor_map.get(name, 0.0), round(val, 1))
                    if "tdie" in name:
                        die_temps.append(val)
                    elif "tdev" in name or "PMU" in name:
                        dev_temps.append(val)
        self.cf.CFRelease(k_prod)
        self.cf.CFRelease(services)
        cpu_t = round(sum(die_temps) / len(die_temps), 1) if die_temps else (round(max(dev_temps), 1) if dev_temps else None)
        gpu_t = round(max(die_temps), 1) if die_temps else cpu_t
        self._cached_hid_temps = (cpu_t, gpu_t, now)
        self._cached_hid_sensors = [
            SensorReading(name=k, category="temp", value=v, unit="°C")
            for k, v in sorted(sensor_map.items())
        ]
        return cpu_t, gpu_t

    def _read_core_ticks(self) -> List[Tuple[int, int, int, int]]:
        num_cpus = c_uint(0)
        info_ptr = POINTER(c_int)()
        info_cnt = c_uint(0)
        host = self.libc.mach_host_self()
        if self.libc.host_processor_info(host, 2, byref(num_cpus), byref(info_ptr), byref(info_cnt)) != 0:
            return []
        cores: List[Tuple[int, int, int, int]] = []
        for i in range(num_cpus.value):
            base = i * 4
            cores.append((int(info_ptr[base]), int(info_ptr[base + 1]), int(info_ptr[base + 2]), int(info_ptr[base + 3])))
        self.libc.vm_deallocate(
            self.libc.mach_task_self(),
            ctypes.cast(info_ptr, c_void_p).value,
            info_cnt.value * sizeof(c_int),
        )
        return cores

    def _prime_deltas(self) -> None:
        self.prev_core_ticks = self._read_core_ticks()
        r_b, w_b, r_op, w_op = self._read_raw_disk_io()
        self.prev_disk_stats = (r_b, w_b, r_op, w_op, time.perf_counter())
        self.prev_net_stats = self._read_raw_net()
        # Prime memory, GPU & process deltas briefly
        self.collect_memory()
        self.collect_gpus({})
        self.collect_processes({})

    def collect_cpu_and_power(self) -> Tuple[CpuStats, Dict[str, object]]:
        ior_data = self._sample_ioreport()
        cur_ticks = self._read_core_ticks()
        prev_ticks = self.prev_core_ticks
        self.prev_core_ticks = cur_ticks

        e_mhz = int(ior_data.get("e_freq_mhz") or (self.e_freqs_mhz[-1] if self.e_freqs_mhz else 2400))
        p_mhz = int(ior_data.get("p_freq_mhz") or (self.p_freqs_mhz[-1] if self.p_freqs_mhz else 3500))
        max_mhz = max(self.p_freqs_mhz or self.e_freqs_mhz or [p_mhz, 4000])

        # Temperatures & Fans from SMC + HID
        hid_cpu_t, hid_gpu_t = self._read_hid_temperatures()
        smc_cpu_t = None
        for k in ("TPD0", "TPD1", "Tp09", "Tp01", "Tc0a", "TC0P"):
            v = self._read_smc_float(k)
            if v and 20.0 < v < 120.0:
                smc_cpu_t = round(v, 1)
                break
        cpu_temp = hid_cpu_t or smc_cpu_t
        ior_data["gpu_temp_c"] = None
        for k in ("Tg05", "Tg0j", "Tg0D", "Tg0P", "Tg0f"):
            v = self._read_smc_float(k)
            if v and 20.0 < v < 120.0:
                ior_data["gpu_temp_c"] = round(v, 1)
                break
        if ior_data["gpu_temp_c"] is None:
            ior_data["gpu_temp_c"] = hid_gpu_t

        # Fans
        fans: List[Tuple[str, int, int, float]] = []
        fnum = int(self._read_smc_float("FNum") or 0)
        for fi in range(min(fnum, 4)):
            rpm = int(self._read_smc_float(f"F{fi}Ac") or 0)
            mx = int(self._read_smc_float(f"F{fi}Mx") or 5800)
            pct = round(rpm * 100.0 / mx, 1) if mx > 0 else 0.0
            fans.append((f"Fan {fi}", rpm, mx, pct))
        ior_data["fans"] = fans

        # Power from SMC + IOReport
        sys_power = self._read_smc_float("PSTR") or self._read_smc_float("PDTR")
        cpu_power = (
            ior_data.get("cpu_power_w")
            or self._read_smc_float("PHPC")
            or self._read_smc_float("PCPC")
            or self._read_smc_float("PC0C")
        )
        dram_power = ior_data.get("dram_power_w") or self._read_smc_float("PMVR")
        if sys_power is not None:
            self.peak_sys_power_w = max(self.peak_sys_power_w, float(sys_power))
        if cpu_power is not None:
            self.peak_cpu_power_w = max(self.peak_cpu_power_w, float(cpu_power))

        # Assemble hardware sensor list (Power rails + Fans + HID/SMC temperatures)
        sensors: List[SensorReading] = []
        if sys_power is not None:
            sensors.append(SensorReading("System Total Power", "power", round(float(sys_power), 2), "W"))
        if cpu_power is not None:
            sensors.append(SensorReading("CPU Package Power", "power", round(float(cpu_power), 2), "W"))
        if ior_data.get("gpu_power_w") is not None:
            sensors.append(SensorReading("GPU Power", "power", round(float(ior_data["gpu_power_w"]), 2), "W"))
        if dram_power is not None:
            sensors.append(SensorReading("DRAM Memory Power", "power", round(float(dram_power), 2), "W"))
        if ior_data.get("ane_power_w") is not None:
            sensors.append(SensorReading("Apple Neural Engine", "power", round(float(ior_data["ane_power_w"]), 2), "W"))
        for fname, rpm, _, _ in fans:
            sensors.append(SensorReading(fname, "fan", float(rpm), "RPM"))
        if self._cached_hid_sensors:
            sensors.extend(self._cached_hid_sensors)
        else:
            if cpu_temp is not None:
                sensors.append(SensorReading("CPU Die (SMC)", "temp", float(cpu_temp), "°C"))
            if ior_data.get("gpu_temp_c") is not None:
                sensors.append(SensorReading("GPU Die (SMC)", "temp", float(ior_data["gpu_temp_c"]), "°C"))
        ior_data["sensors"] = sensors

        therm_state = "Nominal"
        if cpu_temp is not None:
            if cpu_temp >= 98.0:
                therm_state = "Critical"
            elif cpu_temp >= 90.0:
                therm_state = "Serious"
            elif cpu_temp >= 80.0:
                therm_state = "Fair"

        cores: List[CoreStat] = []
        tot_user = tot_sys = tot_idle = tot_nice = 0
        for idx, (u, s, idl, n) in enumerate(cur_ticks):
            pu, ps, pidl, pn = prev_ticks[idx] if idx < len(prev_ticks) else (u, s, idl, n)
            du = max(0, u - pu)
            ds = max(0, s - ps)
            didl = max(0, idl - pidl)
            dn = max(0, n - pn)
            dtot = du + ds + didl + dn
            tot_user += du
            tot_sys += ds
            tot_idle += didl
            tot_nice += dn
            if dtot > 0:
                u_pct = du * 100.0 / dtot
                s_pct = ds * 100.0 / dtot
                n_pct = dn * 100.0 / dtot
                use_pct = min(100.0, u_pct + s_pct + n_pct)
            else:
                u_pct = s_pct = use_pct = 0.0

            is_ecore = self.e_cores > 0 and idx < self.e_cores
            label = f"E{idx}" if is_ecore else (f"P{idx - self.e_cores}" if self.e_cores > 0 else f"C{idx}")
            c_freq = e_mhz if is_ecore else p_mhz
            cores.append(
                CoreStat(
                    core_id=idx,
                    label=label,
                    usage_pct=round(use_pct, 1),
                    user_pct=round(u_pct, 1),
                    sys_pct=round(s_pct, 1),
                    freq_mhz=c_freq,
                    temp_c=cpu_temp,
                )
            )

        all_tot = tot_user + tot_sys + tot_idle + tot_nice
        if all_tot > 0:
            user_pct = round(tot_user * 100.0 / all_tot, 1)
            sys_pct = round(tot_sys * 100.0 / all_tot, 1)
            nice_pct = round(tot_nice * 100.0 / all_tot, 1)
            idle_pct = round(tot_idle * 100.0 / all_tot, 1)
            total_pct = round(min(100.0, 100.0 - idle_pct), 1)
        else:
            user_pct = sys_pct = nice_pct = total_pct = 0.0
            idle_pct = 100.0

        # Uptime
        bt = _Timeval64()
        sz = c_size_t(sizeof(bt))
        uptime = 0.0
        if self.libc.sysctlbyname(b"kern.boottime", byref(bt), byref(sz), None, 0) == 0 and bt.tv_sec > 0:
            uptime = max(0.0, time.time() - bt.tv_sec)

        try:
            l1, l5, l15 = os.getloadavg()
        except Exception:
            l1 = l5 = l15 = 0.0

        avg_freq = int(round((e_mhz * self.e_cores + p_mhz * max(1, self.p_cores)) / max(1, self.log_cores))) if self.e_cores else p_mhz

        cpu_stats = CpuStats(
            model=self.cpu_model,
            arch=self.arch,
            physical_cores=self.phys_cores,
            logical_cores=self.log_cores,
            e_cores=self.e_cores,
            p_cores=self.p_cores,
            total_pct=total_pct,
            user_pct=user_pct,
            sys_pct=sys_pct,
            idle_pct=idle_pct,
            nice_pct=nice_pct,
            freq_mhz=avg_freq,
            freq_max_mhz=max_mhz,
            e_freq_mhz=e_mhz,
            p_freq_mhz=p_mhz,
            temp_c=cpu_temp,
            power_w=round(float(cpu_power), 1) if cpu_power is not None else None,
            pkg_power_w=round(float(cpu_power) + float(ior_data.get("gpu_power_w") or 0.0) + float(dram_power or 0.0), 1) if cpu_power is not None else None,
            sys_power_w=round(float(sys_power), 1) if sys_power is not None else None,
            dram_power_w=round(float(dram_power), 2) if dram_power is not None else None,
            peak_sys_power_w=round(self.peak_sys_power_w, 1) if self.peak_sys_power_w > 0 else None,
            peak_cpu_power_w=round(self.peak_cpu_power_w, 1) if self.peak_cpu_power_w > 0 else None,
            thermal_state=therm_state,
            load_avg=(round(l1, 2), round(l5, 2), round(l15, 2)),
            uptime_sec=uptime,
            fans_rpm=fans,
            cores=cores,
        )
        return cpu_stats, ior_data

    def collect_battery(self) -> BatteryStats:
        srv = self.iokit.IOServiceGetMatchingService(0, self.iokit.IOServiceMatching(b"AppleSmartBattery"))
        if not srv:
            return BatteryStats(present=False)
        d = self._entry_to_dict(srv)
        self.iokit.IOObjectRelease(srv)
        if not d or not d.get("BatteryInstalled", True):
            return BatteryStats(present=False)

        cur_cap = d.get("CurrentCapacity", 0)
        max_cap = d.get("MaxCapacity", 100)
        pct = float(cur_cap) if max_cap == 100 else (cur_cap * 100.0 / max(1, max_cap))
        is_charging = bool(d.get("IsCharging", False))
        ext_conn = bool(d.get("ExternalConnected", False))
        fully_charged = bool(d.get("FullyCharged", False))
        status = "Charging" if is_charging else ("Full (AC)" if (ext_conn and fully_charged) else ("AC" if ext_conn else "Discharging"))
        time_rem = d.get("TimeRemaining", None)
        if isinstance(time_rem, int) and (time_rem <= 0 or time_rem >= 65535):
            time_rem = None
        temp_raw = d.get("Temperature", None)
        temp_c = round(temp_raw / 100.0, 1) if isinstance(temp_raw, int) and 1000 < temp_raw < 8000 else None
        cycles = d.get("CycleCount", None)
        return BatteryStats(
            present=True,
            percent=round(min(100.0, max(0.0, pct)), 1),
            status=status,
            time_remaining_min=time_rem,
            temp_c=temp_c,
            cycle_count=cycles,
        )

    def collect_gpus(self, ior_data: Dict[str, object]) -> List[GpuDeviceStats]:
        it = c_uint32(0)
        if self.iokit.IOServiceGetMatchingServices(0, self.iokit.IOServiceMatching(b"IOAccelerator"), byref(it)) != 0:
            return []
        devices: List[GpuDeviceStats] = []
        now = time.perf_counter()
        idx = 0
        new_gpu_proc_ns: Dict[int, Tuple[int, float]] = {}

        while True:
            acc = self.iokit.IOIteratorNext(it)
            if not acc:
                break
            d = self._entry_to_dict(acc)
            perf = d.get("PerformanceStatistics", {}) or {}
            model = d.get("model") or self.cpu_model
            cores = int(d.get("gpu-core-count") or 0)
            dev_util = float(perf.get("Device Utilization %", 0))
            ren_util = perf.get("Renderer Utilization %", None)
            til_util = perf.get("Tiler Utilization %", None)
            in_use_mem = int(perf.get("In use system memory", 0))
            alloc_mem = int(perf.get("Alloc system memory", 0))
            pb_size = int(perf.get("Allocated PB Size", 0))
            tiled_bytes = int(perf.get("TiledSceneBytes", 0))

            # Collect per-process AGXDeviceUserClient children
            cit = c_uint32(0)
            proc_ns_accum: Dict[int, Tuple[str, int]] = {}
            if self.iokit.IORegistryEntryGetChildIterator(acc, b"IOService", byref(cit)) == 0:
                while True:
                    ch = self.iokit.IOIteratorNext(cit)
                    if not ch:
                        break
                    cd = self._entry_to_dict(ch)
                    self.iokit.IOObjectRelease(ch)
                    creator = cd.get("IOUserClientCreator", "")
                    if not creator or not creator.startswith("pid "):
                        continue
                    try:
                        p_part, name_part = creator[4:].split(",", 1)
                        pid = int(p_part.strip())
                        pname = name_part.strip()
                    except Exception:
                        continue
                    app_usage = cd.get("AppUsage", []) or []
                    tot_ns = 0
                    for entry in app_usage:
                        if isinstance(entry, dict):
                            tot_ns += int(entry.get("accumulatedGPUTime", 0))
                    prev_name, prev_ns = proc_ns_accum.get(pid, (pname, 0))
                    proc_ns_accum[pid] = (prev_name or pname, prev_ns + tot_ns)
                self.iokit.IOObjectRelease(cit)

            self.iokit.IOObjectRelease(acc)

            # Calculate per-process GPU % from accumulatedGPUTime deltas
            gpu_procs: List[GpuProcessStat] = []
            tot_delta_ns = 0
            proc_deltas: List[Tuple[int, str, int]] = []
            for pid, (pname, cur_ns) in proc_ns_accum.items():
                new_gpu_proc_ns[pid] = (cur_ns, now)
                if pid in self.prev_gpu_proc_ns:
                    prev_ns, prev_t = self.prev_gpu_proc_ns[pid]
                    dt_ns = max(1.0, (now - prev_t) * 1e9)
                    dns = max(0, cur_ns - prev_ns)
                    pct = min(100.0, dns * 100.0 / dt_ns)
                else:
                    dns = 0
                    pct = 0.0
                tot_delta_ns += dns
                if pct > 0.05 or cur_ns > 0:
                    proc_deltas.append((pid, pname, dns))
                    gpu_procs.append(
                        GpuProcessStat(
                            pid=pid,
                            process_name=pname,
                            gpu_index=idx,
                            kind="M",
                            gpu_util_pct=round(pct, 1),
                            api="Metal",
                        )
                    )

            # Attribute active GPU in-use memory across active Metal GPU clients
            active_clients = len(gpu_procs)
            for gp in gpu_procs:
                if gp.gpu_util_pct > 0 and dev_util > 0:
                    share = min(1.0, gp.gpu_util_pct / max(dev_util, gp.gpu_util_pct))
                    gp.vram_bytes = int(in_use_mem * share * 0.7 + (in_use_mem * 0.3 / max(1, active_clients)))
                elif active_clients > 0:
                    gp.vram_bytes = int(in_use_mem * 0.25 / active_clients)
                gp.vram_pct = round(gp.vram_bytes * 100.0 / max(1, self.total_mem), 1)
                gp.mem_util_pct = gp.vram_pct

            gpu_procs.sort(key=lambda x: (x.gpu_util_pct, x.vram_bytes), reverse=True)

            fans = ior_data.get("fans") or []
            fan_rpm = fans[0][1] if fans else None
            fan_pct = fans[0][3] if fans else None
            gpu_max_mhz = max(self.gpu_freqs_mhz) if self.gpu_freqs_mhz else 1578
            gpu_cur_mhz = int(ior_data.get("gpu_freq_mhz") or (self.gpu_freqs_mhz[0] if self.gpu_freqs_mhz else 338))
            gpu_pwr = ior_data.get("gpu_power_w")
            ane_pwr = ior_data.get("ane_power_w")
            if gpu_pwr is not None:
                self.peak_gpu_power_w = max(self.peak_gpu_power_w, float(gpu_pwr))
            pstate_val = str(ior_data.get("gpu_pstate") or "P1")
            gpu_temp_val = ior_data.get("gpu_temp_c")
            plim = 60.0 if "Max" in model else (120.0 if "Ultra" in model else 35.0)
            throttle_str = "None"
            if isinstance(gpu_temp_val, (int, float)) and gpu_temp_val >= 95.0:
                throttle_str = "ThermSlow"
            elif gpu_pwr is not None and float(gpu_pwr) >= plim * 0.92:
                throttle_str = "PwrCap"
            elif dev_util < 5.0 and pstate_val in ("OFF", "P1", "P2", "P3"):
                throttle_str = "Idle"

            devices.append(
                GpuDeviceStats(
                    index=idx,
                    vendor="Apple",
                    name=f"{model} ({cores}-Core GPU)" if cores else model,
                    uuid=f"AppleSilicon-GPU-{idx}",
                    bus_id="SoC Unified Fabric",
                    driver_version=f"macOS {platform.mac_ver()[0]}",
                    api_version=f"Metal 3 ({cores}c)" if cores else "Metal 3",
                    core_count=cores,
                    gpu_util_pct=round(dev_util, 1),
                    mem_ctrl_util_pct=round(in_use_mem * 100.0 / max(1, alloc_mem), 1) if alloc_mem > 0 else 0.0,
                    renderer_util_pct=float(ren_util) if ren_util is not None else None,
                    tiler_util_pct=float(til_util) if til_util is not None else None,
                    ane_util_pct=round(min(100.0, float(ane_pwr) * 100.0 / 16.0), 1) if ane_pwr else 0.0,
                    vram_used_bytes=in_use_mem,
                    vram_alloc_bytes=alloc_mem,
                    vram_free_bytes=max(0, self.total_mem - in_use_mem),
                    vram_total_bytes=self.total_mem,
                    vram_pct=round(in_use_mem * 100.0 / max(1, self.total_mem), 1),
                    unified_memory=True,
                    pb_alloc_bytes=pb_size,
                    tiled_scene_bytes=tiled_bytes,
                    gpu_clock_mhz=gpu_cur_mhz,
                    gpu_clock_max_mhz=gpu_max_mhz,
                    pstate=pstate_val,
                    throttle_reasons=throttle_str,
                    pstate_dist=dict(ior_data.get("gpu_pstate_dist") or {}),
                    compute_mode="Unified Metal/Compute",
                    power_draw_w=float(gpu_pwr) if gpu_pwr is not None else None,
                    power_limit_w=plim,
                    power_pct=round(float(gpu_pwr) * 100.0 / plim, 1) if gpu_pwr is not None else None,
                    peak_power_w=round(self.peak_gpu_power_w, 2) if self.peak_gpu_power_w > 0 else None,
                    ane_power_w=float(ane_pwr) if ane_pwr is not None else 0.0,
                    temp_c=gpu_temp_val,
                    fan_speed_pct=fan_pct,
                    fan_rpm=fan_rpm,
                    processes=gpu_procs,
                )
            )
            idx += 1

        self.iokit.IOObjectRelease(it)
        self.prev_gpu_proc_ns = new_gpu_proc_ns
        return devices

    def collect_memory(self) -> MemStats:
        vm = _VmStatistics64()
        cnt = c_uint(sizeof(_VmStatistics64) // sizeof(c_int))
        self.libc.host_statistics64(self.libc.mach_host_self(), 4, byref(vm), byref(cnt))
        ps = self.page_size
        tot = self.total_mem
        app_b = max(0, (vm.internal_page_count - vm.purgeable_count) * ps)
        wired_b = vm.wire_count * ps
        comp_b = vm.compressor_page_count * ps
        used_b = min(tot, app_b + wired_b + comp_b)
        cached_b = (vm.external_page_count + vm.purgeable_count) * ps
        avail_b = max(0, tot - used_b)
        free_b = max(0, tot - used_b - cached_b)

        sw = _XswUsage()
        sz = c_size_t(sizeof(_XswUsage))
        self.libc.sysctlbyname(b"vm.swapusage", byref(sw), byref(sz), None, 0)
        sw_tot = int(sw.xsu_total)
        sw_used = int(sw.xsu_used)
        sw_free = int(sw.xsu_avail)

        # Memory pressure level (macmon parity: 1=Normal, 2=Warn, 4=Critical)
        p_lvl = self._sysctl_int("kern.memorystatus_vm_pressure_level")
        pressure_str = "Critical" if p_lvl >= 4 else ("Warn" if p_lvl == 2 else "Normal")

        now = time.perf_counter()
        cur_pi = int(vm.pageins)
        cur_po = int(vm.pageouts)
        prev_pi, prev_po, prev_t = self.prev_vm_pages
        self.prev_vm_pages = (cur_pi, cur_po, now)
        if prev_t > 0:
            dt = max(1e-3, now - prev_t)
            sin_bps = max(0.0, (cur_pi - prev_pi) * ps / dt)
            sout_bps = max(0.0, (cur_po - prev_po) * ps / dt)
        else:
            sin_bps = sout_bps = 0.0

        return MemStats(
            total_bytes=tot,
            used_bytes=used_b,
            free_bytes=free_b,
            available_bytes=avail_b,
            cached_bytes=cached_b,
            wired_bytes=wired_b,
            compressed_bytes=comp_b,
            app_bytes=app_b,
            used_pct=round(used_b * 100.0 / max(1, tot), 1),
            available_pct=round(avail_b * 100.0 / max(1, tot), 1),
            cached_pct=round(cached_b * 100.0 / max(1, tot), 1),
            free_pct=round(free_b * 100.0 / max(1, tot), 1),
            swap_total_bytes=sw_tot,
            swap_used_bytes=sw_used,
            swap_free_bytes=sw_free,
            swap_pct=round(sw_used * 100.0 / sw_tot, 1) if sw_tot > 0 else 0.0,
            pageins=cur_pi,
            pageouts=cur_po,
            swap_in_bps=sin_bps,
            swap_out_bps=sout_bps,
            pressure_level=pressure_str,
        )

    def _read_raw_disk_io(self) -> Tuple[int, int, int, int]:
        it = c_uint32(0)
        if self.iokit.IOServiceGetMatchingServices(0, self.iokit.IOServiceMatching(b"IOBlockStorageDriver"), byref(it)) != 0:
            return 0, 0, 0, 0
        rb = wb = rop = wop = 0
        while True:
            drv = self.iokit.IOIteratorNext(it)
            if not drv:
                break
            d = self._entry_to_dict(drv)
            self.iokit.IOObjectRelease(drv)
            st = d.get("Statistics", {})
            if isinstance(st, dict):
                rb += int(st.get("Bytes (Read)", 0))
                wb += int(st.get("Bytes (Write)", 0))
                rop += int(st.get("Operations (Read)", 0))
                wop += int(st.get("Operations (Write)", 0))
        self.iokit.IOObjectRelease(it)
        return rb, wb, rop, wop

    def collect_disks(self) -> DiskIOStats:
        now = time.perf_counter()
        rb, wb, rop, wop = self._read_raw_disk_io()
        prb, pwb, prop, pwop, pt = self.prev_disk_stats
        dt = max(1e-3, now - pt)
        self.prev_disk_stats = (rb, wb, rop, wop, now)

        r_bps = max(0.0, (rb - prb) / dt)
        w_bps = max(0.0, (wb - pwb) / dt)
        r_iops = max(0.0, (rop - prop) / dt)
        w_iops = max(0.0, (wop - pwop) / dt)

        cnt = self.libc.getfsstat(None, 0, 2)  # MNT_NOWAIT
        mounts: List[DiskMountStat] = []
        if cnt > 0:
            buf = (_Statfs64 * cnt)()
            cnt = self.libc.getfsstat(byref(buf), sizeof(buf), 2)
            seen_devs = set()
            for i in range(cnt):
                s = buf[i]
                fstype = s.f_fstypename.decode("utf-8", "ignore")
                mnt = s.f_mntonname.decode("utf-8", "ignore")
                dev = s.f_mntfromname.decode("utf-8", "ignore")
                if fstype in ("devfs", "autofs", "nullfs") or s.f_blocks == 0:
                    continue
                if mnt.startswith("/System/Volumes/") and mnt != "/System/Volumes/Data":
                    continue
                if mnt == "/Volumes/Recovery":
                    continue
                # Deduplicate APFS container root & Data volume even when f_bavail shifts during active writes
                container_key = ("apfs_root", int(s.f_blocks)) if mnt in ("/", "/System/Volumes/Data") else (mnt, dev)
                if container_key in seen_devs:
                    continue
                seen_devs.add(container_key)
                tot = int(s.f_blocks * s.f_bsize)
                free = int(s.f_bavail * s.f_bsize)
                used = max(0, tot - free)
                mounts.append(
                    DiskMountStat(
                        mount_point="/" if mnt == "/System/Volumes/Data" else mnt,
                        device=dev,
                        fstype=fstype,
                        total_bytes=tot,
                        used_bytes=used,
                        free_bytes=free,
                        used_pct=round(used * 100.0 / max(1, tot), 1),
                        read_bps=r_bps if mnt in ("/", "/System/Volumes/Data") else 0.0,
                        write_bps=w_bps if mnt in ("/", "/System/Volumes/Data") else 0.0,
                    )
                )

        return DiskIOStats(
            read_bps=r_bps,
            write_bps=w_bps,
            read_total_bytes=rb,
            write_total_bytes=wb,
            read_iops=round(r_iops, 1),
            write_iops=round(w_iops, 1),
            mounts=mounts,
        )

    def _read_raw_net(self) -> Dict[str, Tuple[int, int, int, int, float]]:
        now = time.perf_counter()
        mib = (c_int * 6)(4, 17, 0, 0, 6, 0)  # CTL_NET, PF_ROUTE, 0, 0, NET_RT_IFLIST2, 0
        sz = c_size_t(0)
        out: Dict[str, Tuple[int, int, int, int, float]] = {}
        if self.libc.sysctl(mib, 6, None, byref(sz), None, 0) != 0 or sz.value == 0:
            return out
        buf = ctypes.create_string_buffer(sz.value)
        if self.libc.sysctl(mib, 6, buf, byref(sz), None, 0) != 0:
            return out
        raw = buf.raw[: sz.value]
        off = 0
        while off + 168 <= len(raw):
            msglen, _, mtype = struct.unpack_from("<HBB", raw, off)
            if msglen == 0:
                break
            if mtype == 0x12 and off + 168 <= len(raw):  # RTM_IFINFO2
                ipkts, _, opkts, _, _, ibytes, obytes = struct.unpack_from("<QQQQQQQ", raw, off + 56)
                _, _, sdl_nlen = struct.unpack_from("<2xHBB", raw, off + 160)
                ifname = raw[off + 168 : off + 168 + sdl_nlen].decode("utf-8", "ignore")
                if ifname:
                    out[ifname] = (int(ibytes), int(obytes), int(ipkts), int(opkts), now)
            off += msglen
        return out

    def collect_network(self) -> Tuple[Dict[str, NetInterfaceStat], str]:
        cur_net = self._read_raw_net()
        prev_net = self.prev_net_stats
        self.prev_net_stats = cur_net

        # IPv4 / IPv6 addresses via getifaddrs
        ip4_map: Dict[str, str] = {}
        ip6_map: Dict[str, str] = {}
        up_map: Dict[str, bool] = {}
        ptr = POINTER(_Ifaddrs)()
        if self.libc.getifaddrs(byref(ptr)) == 0:
            cur = ptr
            while cur:
                ifa = cur.contents
                name = ifa.ifa_name.decode("utf-8", "ignore") if ifa.ifa_name else ""
                up_map[name] = bool(ifa.ifa_flags & 0x1)  # IFF_UP
                if ifa.ifa_addr:
                    fam = ifa.ifa_addr.contents.sa_family
                    if fam == 2:  # AF_INET
                        raw_ip = bytes(ifa.ifa_addr.contents.sa_data[2:6])
                        ip4_map[name] = socket.inet_ntoa(raw_ip)
                    elif fam == 30:  # AF_INET6
                        raw_ip6 = bytes(ifa.ifa_addr.contents.sa_data[6:22])
                        try:
                            s6 = socket.inet_ntop(socket.AF_INET6, raw_ip6)
                            if not s6.startswith("fe80"):
                                ip6_map[name] = s6
                            elif name not in ip6_map:
                                ip6_map[name] = s6.split("%")[0]
                        except Exception:
                            pass
                cur = ifa.ifa_next
            self.libc.freeifaddrs(ptr)

        stats: Dict[str, NetInterfaceStat] = {}
        for name, (ib, ob, ipk, opk, now) in cur_net.items():
            if ib == 0 and ob == 0 and name not in ip4_map:
                continue
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
            stats[name] = NetInterfaceStat(
                name=name,
                ipv4=ip4_map.get(name, ""),
                ipv6=ip6_map.get(name, ""),
                is_up=up_map.get(name, True),
                rx_bps=rx_bps,
                tx_bps=tx_bps,
                rx_peak_bps=pk_rx,
                tx_peak_bps=pk_tx,
                rx_total_bytes=ib,
                tx_total_bytes=ob,
                rx_pps=round(rx_pps, 1),
                tx_pps=round(tx_pps, 1),
            )

        # Pick primary active interface (prefer en0 or interface with IPv4 and highest traffic)
        primary = "en0"
        if primary not in stats or (not stats[primary].ipv4 and len(stats) > 1):
            candidates = sorted(
                stats.values(),
                key=lambda s: (bool(s.ipv4 and s.name != "lo0"), s.rx_total_bytes + s.tx_total_bytes),
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

    def _get_proc_cmd(self, pid: int, fallback_comm: str, now: float) -> Tuple[str, str]:
        cached = self.cmdline_cache.get(pid)
        if cached and (now - cached[2]) < 30.0 and cached[3] == fallback_comm:
            return cached[0], cached[1]

        path_buf = ctypes.create_string_buffer(2048)
        r = self.libc.proc_pidpath(pid, path_buf, 2048)
        exe_path = path_buf.value.decode("utf-8", "ignore") if r > 0 else ""
        prog_name = os.path.basename(exe_path) if exe_path else fallback_comm

        cmd_str = exe_path or fallback_comm
        mib = (c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
        sz = c_size_t(0)
        if self.libc.sysctl(mib, 3, None, byref(sz), None, 0) == 0 and 4 < sz.value <= 262144:
            abuf = ctypes.create_string_buffer(sz.value)
            if self.libc.sysctl(mib, 3, abuf, byref(sz), None, 0) == 0:
                raw = abuf.raw[: sz.value]
                argc = struct.unpack_from("<i", raw, 0)[0]
                pos = 4
                while pos < len(raw) and raw[pos] != 0:
                    pos += 1
                while pos < len(raw) and raw[pos] == 0:
                    pos += 1
                args = raw[pos:].split(b"\x00")[: max(1, min(argc, 32))]
                parsed = " ".join(a.decode("utf-8", "ignore") for a in args if a)
                if parsed:
                    cmd_str = parsed

        self.cmdline_cache[pid] = (prog_name or fallback_comm, cmd_str, now, fallback_comm)
        return prog_name or fallback_comm, cmd_str

    def inspect_process(self, pid: int) -> Tuple[str, str]:
        """On-demand deep process inspection: returns (cwd, gpu_env_summary)."""
        cwd = ""
        vbuf = ctypes.create_string_buffer(2352)
        if self.libc.proc_pidinfo(pid, 9, 0, vbuf, 2352) == 2352:
            cwd = vbuf.raw[152 : 152 + 1024].split(b"\x00", 1)[0].decode("utf-8", "ignore")

        env_items: List[str] = []
        mib = (c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
        sz = c_size_t(0)
        if self.libc.sysctl(mib, 3, None, byref(sz), None, 0) == 0 and 4 < sz.value <= 262144:
            abuf = ctypes.create_string_buffer(sz.value)
            if self.libc.sysctl(mib, 3, abuf, byref(sz), None, 0) == 0:
                raw = abuf.raw[: sz.value]
                argc = struct.unpack_from("<i", raw, 0)[0]
                pos = 4
                while pos < len(raw) and raw[pos] != 0:
                    pos += 1
                while pos < len(raw) and raw[pos] == 0:
                    pos += 1
                tokens = raw[pos:].split(b"\x00")
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
                for b_tok in tokens[max(1, argc) :]:
                    if not b_tok or b"=" not in b_tok:
                        continue
                    s_tok = b_tok.decode("utf-8", "ignore")
                    k, v = s_tok.split("=", 1)
                    if k in target_exact or k.startswith(("PYTORCH_", "MLX_")):
                        if k == "VIRTUAL_ENV":
                            v_clean = v.rstrip("/")
                            base_v = os.path.basename(v_clean)
                            parent_v = os.path.basename(os.path.dirname(v_clean))
                            if base_v in (".venv", "venv", "env") and parent_v:
                                s_tok = f"{k}={parent_v}/{base_v}"
                            elif base_v:
                                s_tok = f"{k}={base_v}"
                        env_items.append(s_tok)
                        if len(env_items) >= 6:
                            break
        return cwd, " │ ".join(env_items[:6])

    def collect_processes(self, gpu_proc_map: Dict[int, GpuProcessStat]) -> Tuple[List[ProcessInfo], int]:
        now = time.perf_counter()
        wall_now = time.time()
        mib = (c_int * 4)(1, 14, 0, 0)  # CTL_KERN, KERN_PROC, KERN_PROC_ALL, 0
        sz = c_size_t(0)
        if self.libc.sysctl(mib, 4, None, byref(sz), None, 0) != 0 or sz.value == 0:
            return [], 0
        buf = ctypes.create_string_buffer(sz.value)
        if self.libc.sysctl(mib, 4, buf, byref(sz), None, 0) != 0:
            return [], 0

        raw = buf.raw[: sz.value]
        KP_SIZE = 648
        n = len(raw) // KP_SIZE
        ti = _ProcTaskInfo()
        ti_sz = sizeof(_ProcTaskInfo)
        ru = _RusageInfoV4()

        procs: List[ProcessInfo] = []
        new_proc_cpu: Dict[int, Tuple[int, float]] = {}
        total_threads = 0
        stat_map = {1: "I", 2: "R", 3: "S", 4: "T", 5: "Z"}

        for i in range(n):
            chunk = raw[i * KP_SIZE : (i + 1) * KP_SIZE]
            pid = struct.unpack_from("<i", chunk, 40)[0]
            if pid <= 0:
                continue
            start_sec = struct.unpack_from("<q", chunk, 0)[0]
            elapsed_sec = max(0.0, wall_now - start_sec) if 1_000_000_000 < start_sec <= wall_now + 10 else 0.0
            stat_code = chunk[36]
            nice = struct.unpack_from("<b", chunk, 241)[0]
            comm = chunk[243:260].split(b"\x00", 1)[0].decode("utf-8", "ignore")
            ppid = struct.unpack_from("<i", chunk, 560)[0]
            uid = struct.unpack_from("<I", chunk, 420)[0]
            user = self._get_username(uid)

            threads = 1
            mem_bytes = 0
            cpu_pct = 0.0
            io_r = 0
            io_w = 0

            if self.libc.proc_pidinfo(pid, 4, 0, byref(ti), ti_sz) == ti_sz:
                threads = max(1, int(ti.pti_threadnum))
                mem_bytes = int(ti.pti_resident_size)
                tot_ticks = int(ti.pti_total_user + ti.pti_total_system)
                tot_ns = (tot_ticks * self.tb_numer) // self.tb_denom
                new_proc_cpu[pid] = (tot_ns, now)
                if pid in self.prev_proc_cpu:
                    prev_ns, prev_t = self.prev_proc_cpu[pid]
                    dt_ns = max(1.0, (now - prev_t) * 1e9)
                    cpu_pct = max(0.0, (tot_ns - prev_ns) * 100.0 / dt_ns)

            if self.libc.proc_pid_rusage(pid, 4, byref(ru)) == 0:
                if ru.ri_phys_footprint > 0:
                    mem_bytes = int(ru.ri_phys_footprint)
                io_r = int(ru.ri_diskio_bytesread)
                io_w = int(ru.ri_diskio_byteswritten)

            total_threads += threads
            gp = gpu_proc_map.get(pid)
            if gp and gp.process_name and (not comm or len(gp.process_name) > len(comm)):
                comm = gp.process_name

            # Fetch full cmdline for active/GPU processes or fresh cache hits
            cached = self.cmdline_cache.get(pid)
            if cpu_pct > 0.1 or mem_bytes > 20 * 1024 * 1024 or gp is not None:
                name, cmdline = self._get_proc_cmd(pid, comm, now)
            elif cached and (now - cached[2]) < 30.0 and cached[3] == comm:
                name, cmdline = cached[0], cached[1]
            else:
                name = comm
                cmdline = comm

            final_name = name or comm or f"pid-{pid}"
            final_cmd = cmdline or name or comm
            pinfo = ProcessInfo(
                pid=pid,
                ppid=ppid,
                name=final_name,
                cmdline=final_cmd,
                smart_cmd=format_smart_cmdline(final_name, final_cmd),
                user=user,
                state=stat_map.get(stat_code, "S"),
                nice=nice,
                threads=threads,
                cpu_pct=round(cpu_pct, 1),
                mem_bytes=mem_bytes,
                mem_pct=round(mem_bytes * 100.0 / max(1, self.total_mem), 1),
                io_read_bytes=io_r,
                io_write_bytes=io_w,
                elapsed_sec=round(elapsed_sec, 1),
            )
            if gp is not None:
                pinfo.gpu_index = gp.gpu_index
                pinfo.gpu_kind = gp.kind
                pinfo.gpu_pct = gp.gpu_util_pct
                pinfo.vram_bytes = gp.vram_bytes
                pinfo.vram_pct = gp.vram_pct
                pinfo.gpu_api = gp.api
                if pinfo.mem_bytes == 0 and gp.vram_bytes > 0:
                    pinfo.mem_bytes = gp.vram_bytes
                    pinfo.mem_pct = gp.vram_pct

            procs.append(pinfo)

        if len(self.cmdline_cache) > 1024:
            dead_pids = [k for k in self.cmdline_cache if k not in new_proc_cpu]
            for k in dead_pids:
                self.cmdline_cache.pop(k, None)

        self.prev_proc_cpu = new_proc_cpu
        return procs, total_threads
