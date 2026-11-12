"""Rich ANSI terminal snapshot renderer (--once, --compact, --stream, --replay, --fleet) and JSON formatter."""

from __future__ import annotations
import curses
import datetime
import json
import shutil
import time
from typing import Any, Dict, List

from ..collectors.base import UnifiedCollector
from ..collectors.fleet import FleetCollector, FleetNodeSnapshot
from ..models import SystemSnapshot
from .braille import (
    fmt_bytes,
    fmt_duration,
    fmt_freq,
    fmt_speed,
    render_bar,
    render_bar_parts,
    render_braille_sparkline,
)
from .themes import ThemeManager
from .tui import fmt_time_plus


# ANSI color helpers
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_CYAN = "\033[38;5;81m"
C_GREEN = "\033[38;5;48m"
C_YELLOW = "\033[38;5;220m"
C_RED = "\033[38;5;196m"
C_MAGENTA = "\033[38;5;171m"
C_ORANGE = "\033[38;5;214m"
C_BLUE = "\033[38;5;39m"
C_BORDER = "\033[38;5;240m"
C_BADGE_CYAN = "\033[1;38;5;16;48;5;81m"
C_BADGE_GREEN = "\033[1;38;5;16;48;5;48m"


def _pct_color(pct: float) -> str:
    if pct >= 80.0:
        return C_RED
    if pct >= 45.0:
        return C_YELLOW
    return C_GREEN


def _ansi_bar(pct: float, width: int, color: str = "", style: str = "block", gradient: bool = False) -> str:
    """Renders an ANSI progress bar color-coded uniformly across the entire bar with a dim empty track."""
    filled_s, empty_s = render_bar_parts(pct, width, style=style)
    out = ""
    if filled_s:
        col = _pct_color(pct) if (gradient or not color) else color
        out += f"{C_BOLD}{col}{filled_s}{C_RESET}"
    if empty_s:
        out += f"{C_BORDER}{empty_s}{C_RESET}"
    return out


def render_snapshot_ansi(collector: UnifiedCollector, snap: SystemSnapshot, top_n: int = 12) -> str:
    """Renders a complete btop + nvtop ANSI terminal dashboard snapshot."""
    term_w = min(140, max(88, shutil.get_terminal_size((110, 40)).columns))
    lines: List[str] = []

    def _box_top(title: str, right: str = "") -> str:
        t_vis = f"┤ {title} ├"
        r_vis = f"┤ {right} ├" if right else ""
        dashes = max(2, term_w - 2 - len(t_vis) - len(r_vis))
        if title and title[0] in ("¹", "²", "³", "⁴", "⁵") and " " in title:
            b_part, rest_part = title.split(" ", 1)
            t_styled = f"{C_BORDER}┤{C_RESET} {C_BADGE_CYAN} {b_part} {C_RESET} {C_BOLD}{C_CYAN}{rest_part}{C_RESET} {C_BORDER}├{C_RESET}"
        else:
            t_styled = f"{C_BOLD}{C_CYAN}{t_vis}{C_RESET}"
        return (
            f"{C_BORDER}╭─{C_RESET}{t_styled}"
            f"{C_BORDER}{'─' * (dashes - 1)}{C_RESET}"
            f"{C_BOLD}{r_vis}{C_RESET}{C_BORDER}╮{C_RESET}"
        )

    def _box_bot() -> str:
        return f"{C_BORDER}╰{'─' * (term_w - 2)}╯{C_RESET}"

    now_s = datetime.datetime.fromtimestamp(snap.timestamp).strftime("%Y-%m-%d %H:%M:%S")
    l1, l5, l15 = snap.cpu.load_avg
    up_s = fmt_duration(snap.cpu.uptime_sec)
    pwr_s = f" │ Sys {snap.cpu.sys_power_w:.1f}W" if snap.cpu.sys_power_w else ""
    if snap.cpu.peak_sys_power_w:
        pwr_s += f" (Pk {snap.cpu.peak_sys_power_w:.1f}W)"
    bat_s = f" │ Bat {snap.battery.percent:.0f}% ({snap.battery.status})" if snap.battery.present else ""
    alert_s = f" │ {C_RED}{C_BOLD}⚠ {' / '.join(snap.alerts)}{C_RESET}" if snap.alerts else ""

    lines.append(
        f"{C_BADGE_CYAN} ✦ MACMASTER v2.0 {C_RESET} │ {now_s} │ "
        f"{C_BOLD}{snap.hostname}{C_RESET} ({snap.os_name} {snap.os_release} {snap.arch}) │ "
        f"Up {up_s} │ Load {l1:.2f} {l5:.2f} {l15:.2f}{pwr_s}{bat_s}{alert_s}"
    )

    # 1. CPU Panel
    cpu = snap.cpu
    freq_s = f"E:{fmt_freq(cpu.e_freq_mhz)} P:{fmt_freq(cpu.p_freq_mhz)}" if cpu.e_cores else fmt_freq(cpu.freq_mhz)
    temp_s = f" │ {cpu.temp_c:.1f}°C ({cpu.thermal_state})" if cpu.temp_c is not None else ""
    cpwr_s = f" │ CPU {cpu.power_w:.1f}W" if cpu.power_w is not None else ""
    if cpu.dram_power_w is not None:
        cpwr_s += f" DRAM {cpu.dram_power_w:.2f}W"
    fan_s = f" │ Fan {cpu.fans_rpm[0][1]}RPM ({cpu.fans_rpm[0][3]:.0f}%)" if cpu.fans_rpm else ""
    lines.append(_box_top(f"¹cpu ─ {cpu.model} ({cpu.logical_cores} cores)", f"{freq_s}{temp_s}{cpwr_s}{fan_s}"))

    c_col = _pct_color(cpu.total_pct)
    c_spark = render_braille_sparkline(collector.history.cpu_total, width=16)
    lines.append(
        f"  {C_BOLD}Total CPU:{C_RESET} {_ansi_bar(cpu.total_pct, 28, gradient=True)} {c_col}{C_BOLD}{cpu.total_pct:5.1f}%{C_RESET} "
        f"({C_GREEN}usr {cpu.user_pct:4.1f}%{C_RESET} │ {C_YELLOW}sys {cpu.sys_pct:4.1f}%{C_RESET} │ idle {cpu.idle_pct:4.1f}%) "
        f"Hist: {c_col}[{c_spark}]{C_RESET}"
    )

    core_cells: List[str] = []
    for c in cpu.cores:
        col = _pct_color(c.usage_pct)
        l_col = C_BLUE if c.label.startswith("E") else C_CYAN
        core_cells.append(f"{C_BOLD}{l_col}{c.label:<3}{C_RESET} {_ansi_bar(c.usage_pct, 8, style='block', gradient=True)} {col}{c.usage_pct:5.1f}%{C_RESET}")
    for i in range(0, len(core_cells), 4):
        lines.append("  " + "   ".join(core_cells[i : i + 4]))
    lines.append(_box_bot())

    # 2. GPU Panel (nvtop)
    for g in snap.gpus:
        g_right = f"Clock {g.gpu_clock_mhz}/{g.gpu_clock_max_mhz}MHz [{g.pstate or 'P0'}]"
        if g.throttle_reasons and g.throttle_reasons not in ("None", "Idle"):
            g_right += f" ⚠{g.throttle_reasons}"
        if g.power_draw_w is not None:
            g_right += f" │ {g.power_draw_w:.2f}W" + (f"/{g.power_limit_w:.0f}W" if g.power_limit_w else "")
        if g.temp_c is not None:
            g_right += f" │ {g.temp_c:.1f}°C"
        lines.append(_box_top(f"²gpu [nvtop] ─ [{g.index}] {g.name} ({g.api_version})", g_right))

        u_spark = render_braille_sparkline(collector.history.gpu_util.get(g.index, [g.gpu_util_pct]), width=12)
        m_spark = render_braille_sparkline(collector.history.gpu_mem.get(g.index, [g.vram_pct]), width=12)
        u_col = _pct_color(g.gpu_util_pct)
        v_col = _pct_color(g.vram_pct)
        lines.append(
            f"  {C_BOLD}GPU Util :{C_RESET} {_ansi_bar(g.gpu_util_pct, 24, gradient=True)} {u_col}{C_BOLD}{g.gpu_util_pct:5.1f}%{C_RESET} [{u_col}{u_spark}{C_RESET}]   "
            f"{C_BOLD}{'Uni VRAM' if g.unified_memory else 'GPU VRAM'}:{C_RESET} {_ansi_bar(g.vram_pct, 20, gradient=True)} {v_col}{C_BOLD}{g.vram_pct:4.1f}% "
            f"({fmt_bytes(g.vram_used_bytes)} / {fmt_bytes(g.vram_total_bytes)}){C_RESET} [{v_col}{m_spark}{C_RESET}]"
        )
        if g.vendor == "Apple":
            dist_s = " ".join(f"{k}:{v:.0f}%" for k, v in list(g.pstate_dist.items())[:5])
            lines.append(
                f"  {C_BOLD}Engines  :{C_RESET} Renderer {C_CYAN}{g.renderer_util_pct or 0.0:4.1f}%{C_RESET} │ "
                f"Tiler {C_CYAN}{g.tiler_util_pct or 0.0:4.1f}%{C_RESET} │ "
                f"ANE {C_ORANGE}{g.ane_power_w or 0.0:.2f}W{C_RESET} │ "
                f"Alloc Sys Mem {fmt_bytes(g.vram_alloc_bytes)} │ PB {fmt_bytes(g.pb_alloc_bytes)} │ "
                f"P-States [{dist_s or g.pstate}] │ Active Metal Clients: {len(g.processes)}"
            )
        else:
            pcie_s = f"Gen{g.pcie_gen or '?'}/{g.pcie_gen_max or '?'} x{g.pcie_width or '?'}/{g.pcie_width_max or '?'}"
            thr_s = f" │ Throttle: {C_YELLOW}{g.throttle_reasons}{C_RESET}" if g.throttle_reasons else ""
            lines.append(
                f"  {C_BOLD}NV Engines:{C_RESET} SM {g.gpu_util_pct:4.1f}% │ MemCtrl {g.mem_ctrl_util_pct:4.1f}% │ "
                f"ENC {g.enc_util_pct or 0.0:4.1f}% │ DEC {g.dec_util_pct or 0.0:4.1f}% │ "
                f"PCIe {pcie_s} (RX {fmt_speed((g.pcie_rx_kbps or 0)*1024)} / TX {fmt_speed((g.pcie_tx_kbps or 0)*1024)}) │ "
                f"Fan {g.fan_speed_pct or 0.0:.0f}% │ Mode {g.compute_mode}{thr_s}"
            )
        lines.append(_box_bot())

    # 3. Memory, Disks & Network Panel
    mem = snap.mem
    dsk = snap.disk
    nstat = snap.net.get(snap.primary_net_iface)
    lines.append(
        _box_top(
            f"³mem & disks ─ RAM {fmt_bytes(mem.used_bytes)}/{fmt_bytes(mem.total_bytes)} ({mem.used_pct:.1f}%) [{mem.pressure_level}]",
            f"⁴net [{snap.primary_net_iface}] ▼ {fmt_speed(nstat.rx_bps if nstat else 0)} ▲ {fmt_speed(nstat.tx_bps if nstat else 0)}",
        )
    )
    extra_mem = (
        f"Wired {fmt_bytes(mem.wired_bytes)} │ Compressed {fmt_bytes(mem.compressed_bytes)} │ Pressure: {mem.pressure_level}"
        if mem.wired_bytes > 0
        else f"Buffers {fmt_bytes(mem.buffers_bytes)} │ Shared {fmt_bytes(mem.shared_bytes)} │ Pressure: {mem.pressure_level}"
    )
    lines.append(
        f"  {C_BOLD}Memory   :{C_RESET} {_ansi_bar(mem.used_pct, 22, gradient=True)} {_pct_color(mem.used_pct)}{C_BOLD}{mem.used_pct:4.1f}%{C_RESET} │ "
        f"Avail {fmt_bytes(mem.available_bytes)} ({mem.available_pct:.1f}%) │ Cached {fmt_bytes(mem.cached_bytes)} │ "
        f"Swap {fmt_bytes(mem.swap_used_bytes)}/{fmt_bytes(mem.swap_total_bytes)} │ {extra_mem}"
    )
    mnt_summary = "   ".join(
        f"{C_BOLD}{m.mount_point}{C_RESET} [{m.fstype}] {_pct_color(m.used_pct)}{m.used_pct:.1f}%{C_RESET} ({fmt_bytes(m.used_bytes)}/{fmt_bytes(m.total_bytes)})"
        for m in dsk.mounts[:3]
    )
    lines.append(
        f"  {C_BOLD}Disks I/O:{C_RESET} R {C_GREEN}{fmt_speed(dsk.read_bps)}{C_RESET} ({dsk.read_iops:.0f} IOPS) │ "
        f"W {C_ORANGE}{fmt_speed(dsk.write_bps)}{C_RESET} ({dsk.write_iops:.0f} IOPS) │ {mnt_summary}"
    )
    if nstat:
        lines.append(
            f"  {C_BOLD}Network  :{C_RESET} [{nstat.name} {nstat.ipv4 or nstat.ipv6}] "
            f"▼ Down {C_GREEN}{fmt_speed(nstat.rx_bps)}{C_RESET} (Total {fmt_bytes(nstat.rx_total_bytes)}, {nstat.rx_pps:.0f} pkt/s) │ "
            f"▲ Up {C_ORANGE}{fmt_speed(nstat.tx_bps)}{C_RESET} (Total {fmt_bytes(nstat.tx_total_bytes)}, {nstat.tx_pps:.0f} pkt/s)"
        )
    lines.append(_box_bot())

    # 4. Unified Process + GPU Process Table
    lines.append(
        _box_top(
            f"⁵proc (btop + nvtop Unified) ─ Top {top_n} Processes",
            f"Total: {snap.total_processes} procs, {snap.total_threads} threads, {snap.gpu_process_count} active on GPU",
        )
    )
    lines.append(
        f"  {C_BOLD}{C_CYAN}{'PID':>7}  {'Program':<18} {'GPU':>6} {'TIME+':>7} {'User':<9} {'MemB':>9} {'Mem%':>5} {'VRAM':>9} {'GPU%':>6} {'CPU%':>6}  {'Smart Command'}{C_RESET}"
    )
    sorted_procs = sorted(snap.processes, key=lambda p: (p.cpu_pct + p.gpu_pct * 1.5, p.mem_bytes), reverse=True)[:top_n]
    for p in sorted_procs:
        g_tag = f"{p.gpu_index}:{p.gpu_kind}" if p.gpu_index is not None else "-"
        vram_s = fmt_bytes(p.vram_bytes) if p.vram_bytes > 0 else "-"
        g_col = C_BLUE if p.gpu_pct > 0 else C_DIM
        c_col = _pct_color(min(100.0, p.cpu_pct))
        cmd_s = (p.smart_cmd or p.cmdline)[: max(20, term_w - 97)]
        t_plus = fmt_time_plus(p.elapsed_sec)
        lines.append(
            f"  {p.pid:>7}  {p.name[:18]:<18} {g_col}{g_tag:>6}{C_RESET} {t_plus:>7} {p.user[:9]:<9} "
            f"{fmt_bytes(p.mem_bytes):>9} {p.mem_pct:>4.1f}% {g_col}{vram_s:>9} {p.gpu_pct:>5.1f}%{C_RESET} "
            f"{c_col}{p.cpu_pct:>5.1f}%{C_RESET}  {C_DIM}{cmd_s}{C_RESET}"
        )
    lines.append(_box_bot())
    return "\n".join(lines)


def render_compact_ansi(snap: SystemSnapshot) -> str:
    """Renders a gpustat-style ultra-compact 2-3 line color summary for SSH login or tmux."""
    now_s = datetime.datetime.fromtimestamp(snap.timestamp).strftime("%H:%M:%S")
    cpu = snap.cpu
    mem = snap.mem
    nstat = snap.net.get(snap.primary_net_iface)
    c_col = _pct_color(cpu.total_pct)
    m_col = _pct_color(mem.used_pct)
    pwr_s = f" ⚡{cpu.sys_power_w or cpu.pkg_power_w or 0.0:.1f}W" if (cpu.sys_power_w or cpu.pkg_power_w) else ""
    temp_s = f" {cpu.temp_c:.0f}°C" if cpu.temp_c is not None else ""

    lines = [
        f"{C_BOLD}{C_CYAN}{snap.hostname}{C_RESET} ({snap.os_name} {snap.arch}) [{now_s}] │ "
        f"{C_BOLD}CPU{C_RESET} {c_col}{cpu.total_pct:4.1f}%{C_RESET} ({cpu.logical_cores}c{temp_s}{pwr_s}) │ "
        f"{C_BOLD}RAM{C_RESET} {m_col}{fmt_bytes(mem.used_bytes)}/{fmt_bytes(mem.total_bytes)} ({mem.used_pct:.0f}%){C_RESET} [{mem.pressure_level}] │ "
        f"{C_BOLD}Disk{C_RESET} R:{fmt_speed(snap.disk.read_bps)} W:{fmt_speed(snap.disk.write_bps)} │ "
        f"{C_BOLD}Net{C_RESET} ▼{fmt_speed(nstat.rx_bps if nstat else 0)} ▲{fmt_speed(nstat.tx_bps if nstat else 0)}"
    ]
    for g in snap.gpus:
        g_col = _pct_color(g.gpu_util_pct)
        v_col = _pct_color(g.vram_pct)
        t_s = f"{g.temp_c:4.0f}°C" if g.temp_c is not None else "  N/A"
        p_s = f"{g.power_draw_w:5.1f}W" if g.power_draw_w is not None else "  N/A"
        if g.power_limit_w:
            p_s += f"/{g.power_limit_w:.0f}W"
        thr_s = f" {C_YELLOW}[{g.throttle_reasons}]{C_RESET}" if g.throttle_reasons and g.throttle_reasons not in ("None", "Idle") else ""
        top_procs = sorted(g.processes, key=lambda p: (p.gpu_util_pct, p.vram_bytes), reverse=True)[:4]
        proc_tags = " ".join(
            f"{C_BOLD}{p.process_name or p.pid}{C_RESET}({C_BLUE}{p.gpu_util_pct:.0f}%{C_RESET}/{C_MAGENTA}{fmt_bytes(p.vram_bytes)}{C_RESET})"
            for p in top_procs
        )
        lines.append(
            f"  [{g.index}] {C_BOLD}{C_GREEN}{g.name}{C_RESET} │ {t_s}, {g_col}{g.gpu_util_pct:4.1f}%{C_RESET} [{g.pstate or 'P0'}]{thr_s} │ "
            f"{C_ORANGE}{p_s}{C_RESET} │ {v_col}{fmt_bytes(g.vram_used_bytes)} / {fmt_bytes(g.vram_total_bytes)} ({g.vram_pct:.1f}%){C_RESET} │ "
            f"{proc_tags or f'{C_DIM}(no active GPU processes){C_RESET}'}"
        )
    return "\n".join(lines)


def render_snapshot_json(snap: SystemSnapshot) -> str:
    """Serializes the full SystemSnapshot into formatted JSON."""
    return json.dumps(snap.to_dict(), indent=2, ensure_ascii=False)


def render_stream_ndjson(snap: SystemSnapshot) -> str:
    """Serializes a compact single-line NDJSON record for --stream / --record."""
    g0 = snap.gpus[0] if snap.gpus else None
    gpu_powers = [g.power_draw_w for g in snap.gpus if g.power_draw_w is not None]
    gpu_temps = [g.temp_c for g in snap.gpus if g.temp_c is not None]
    throttles = [g.throttle_reasons for g in snap.gpus if g.throttle_reasons and g.throttle_reasons not in ("None", "Idle")]
    gpu_throttle = ",".join(dict.fromkeys(throttles)) if throttles else (g0.throttle_reasons if g0 else "")
    payload = {
        "ts": round(snap.timestamp, 3),
        "host": snap.hostname,
        "os": snap.os_name,
        "cpu_pct": snap.cpu.total_pct,
        "cpu_temp_c": snap.cpu.temp_c,
        "cpu_power_w": snap.cpu.power_w,
        "dram_power_w": snap.cpu.dram_power_w,
        "sys_power_w": snap.cpu.sys_power_w,
        "mem_pct": snap.mem.used_pct,
        "mem_used_bytes": snap.mem.used_bytes,
        "mem_pressure": snap.mem.pressure_level,
        "disk_r_bps": round(snap.disk.read_bps, 1),
        "disk_w_bps": round(snap.disk.write_bps, 1),
        "gpu_count": len(snap.gpus),
        "gpu_util_pct": max((g.gpu_util_pct for g in snap.gpus), default=0.0),
        "gpu_vram_pct": max((g.vram_pct for g in snap.gpus), default=0.0),
        "gpu_power_w": round(sum(gpu_powers), 2) if gpu_powers else None,
        "gpu_temp_c": max(gpu_temps) if gpu_temps else None,
        "gpu_throttle": gpu_throttle,
        "gpus": [
            {
                "index": g.index,
                "name": g.name,
                "util_pct": g.gpu_util_pct,
                "vram_pct": g.vram_pct,
                "power_w": g.power_draw_w,
                "temp_c": g.temp_c,
                "throttle": g.throttle_reasons,
            }
            for g in snap.gpus
        ],
        "alerts": snap.alerts,
    }
    return json.dumps(payload, ensure_ascii=False)


def render_replay_summary(path: str) -> str:
    """Replays a recorded .jsonl telemetry file with historical Braille sparklines and peak/avg stats."""
    records: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    except OSError as e:
        return f"Error opening replay file '{path}': {e}"
    if not records:
        return f"No valid telemetry records found in {path}"

    host = records[-1].get("host", "unknown")
    os_name = records[-1].get("os", "")
    t_start = datetime.datetime.fromtimestamp(records[0].get("ts", time.time())).strftime("%H:%M:%S")
    t_end = datetime.datetime.fromtimestamp(records[-1].get("ts", time.time())).strftime("%H:%M:%S")
    dur = max(0.0, float(records[-1].get("ts", 0)) - float(records[0].get("ts", 0)))

    cpu_vals = [float(r.get("cpu_pct", 0.0)) for r in records]
    gpu_vals = [float(r.get("gpu_util_pct", 0.0)) for r in records]
    vram_vals = [float(r.get("gpu_vram_pct", 0.0)) for r in records]
    mem_vals = [float(r.get("mem_pct", 0.0)) for r in records]
    pwr_vals = [float(r.get("sys_power_w") or r.get("gpu_power_w") or r.get("cpu_power_w") or 0.0) for r in records]

    lines = [
        f"{C_BOLD}{C_CYAN}✦ MacMaster Telemetry Replay{C_RESET} │ File: {C_BOLD}{path}{C_RESET} │ Host: {C_BOLD}{host}{C_RESET} ({os_name})",
        f"  Samples: {len(records)} │ Window: {t_start} → {t_end} ({dur:.1f}s)",
        f"  {C_BOLD}CPU Util :{C_RESET} Avg {sum(cpu_vals)/len(cpu_vals):5.1f}% │ Peak {max(cpu_vals):5.1f}% │ [{C_GREEN}{render_braille_sparkline(cpu_vals, 32)}{C_RESET}]",
        f"  {C_BOLD}GPU Util :{C_RESET} Avg {sum(gpu_vals)/len(gpu_vals):5.1f}% │ Peak {max(gpu_vals):5.1f}% │ [{C_BLUE}{render_braille_sparkline(gpu_vals, 32)}{C_RESET}]",
        f"  {C_BOLD}GPU VRAM :{C_RESET} Avg {sum(vram_vals)/len(vram_vals):5.1f}% │ Peak {max(vram_vals):5.1f}% │ [{C_MAGENTA}{render_braille_sparkline(vram_vals, 32)}{C_RESET}]",
        f"  {C_BOLD}Sys RAM  :{C_RESET} Avg {sum(mem_vals)/len(mem_vals):5.1f}% │ Peak {max(mem_vals):5.1f}% │ [{C_YELLOW}{render_braille_sparkline(mem_vals, 32)}{C_RESET}]",
        f"  {C_BOLD}Power (W):{C_RESET} Avg {sum(pwr_vals)/len(pwr_vals):5.1f}W │ Peak {max(pwr_vals):5.1f}W │ [{C_ORANGE}{render_braille_sparkline(pwr_vals, 32, 0.0, max(10.0, max(pwr_vals)))}{C_RESET}]",
    ]
    return "\n".join(lines)


def render_fleet_ansi(nodes: List[FleetNodeSnapshot]) -> str:
    """Renders a unified multi-host cluster dashboard (the local machine and any SSH hosts you name)."""
    term_w = min(140, max(90, shutil.get_terminal_size((115, 40)).columns))
    now_s = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    online_cnt = sum(1 for n in nodes if n.online)

    lines = [
        f"{C_BADGE_CYAN} ✦ MACMASTER FLEET {C_RESET} {C_BOLD}{C_CYAN}Apple Silicon + NVIDIA Cluster Monitor{C_RESET} │ {now_s} │ "
        f"Nodes Online: {C_BADGE_GREEN} {online_cnt}/{len(nodes)} {C_RESET}"
    ]

    for node in nodes:
        if not node.online or not node.data:
            lines.append(f"{C_BORDER}╭─{C_RESET}{C_BOLD}{C_RED}┤ ✖ {node.host} (OFFLINE — {node.error}) ├{C_RESET}{C_BORDER}{'─' * max(2, term_w - 30 - len(node.host) - len(node.error))}╮{C_RESET}")
            lines.append(f"  {C_DIM}Unreachable via SSH ({node.latency_ms:.0f}ms). Check connectivity or run `bash install.sh --push {node.host}`.{C_RESET}")
            lines.append(f"{C_BORDER}╰{'─' * (term_w - 2)}╯{C_RESET}")
            continue

        d = node.data
        cpu = d.get("cpu", {})
        mem = d.get("mem", {})
        dsk = d.get("disk", {})
        gpus = d.get("gpus", [])
        procs = d.get("processes", [])
        hostname = d.get("hostname", node.host)
        os_s = f"{d.get('os_name', '')} {d.get('os_release', '')} {d.get('arch', '')}".strip()

        cpu_pct = float(cpu.get("total_pct", 0.0))
        mem_pct = float(mem.get("used_pct", 0.0))
        mem_used = int(mem.get("used_bytes", 0))
        mem_tot = int(mem.get("total_bytes", 1))
        sys_pwr = cpu.get("sys_power_w") or cpu.get("pkg_power_w")
        pwr_s = f" │ ⚡{float(sys_pwr):.1f}W" if sys_pwr is not None else ""
        temp_s = f" │ 🌡{float(cpu['temp_c']):.1f}°C" if cpu.get("temp_c") is not None else ""

        hdr_left = f"● {node.host} ({hostname} — {os_s})"
        hdr_right = f"SSH {node.latency_ms:.0f}ms{temp_s}{pwr_s}"
        dashes = max(2, term_w - 10 - len(hdr_left) - len(hdr_right))
        lines.append(
            f"{C_BORDER}╭─┤{C_RESET} {C_BADGE_GREEN} ● {node.host} {C_RESET} {C_BOLD}{C_CYAN}({hostname} — {os_s}){C_RESET} {C_BORDER}├"
            f"{'─' * max(2, dashes - 2)}{C_RESET}"
            f"{C_BOLD}┤ {hdr_right} ├{C_RESET}{C_BORDER}╮{C_RESET}"
        )

        c_spark = render_braille_sparkline(node.cpu_hist or [cpu_pct], width=10)
        lines.append(
            f"  {C_BOLD}CPU:{C_RESET} {_ansi_bar(cpu_pct, 18, gradient=True)} {_pct_color(cpu_pct)}{C_BOLD}{cpu_pct:5.1f}%{C_RESET} [{C_CYAN}{c_spark}{C_RESET}] ({cpu.get('model', '')} {cpu.get('logical_cores', 0)}c) │ "
            f"{C_BOLD}RAM:{C_RESET} {_ansi_bar(mem_pct, 14, gradient=True)} {_pct_color(mem_pct)}{C_BOLD}{mem_pct:4.1f}%{C_RESET} ({fmt_bytes(mem_used)}/{fmt_bytes(mem_tot)}) [{mem.get('pressure_level', 'Normal')}]"
        )

        for g in gpus:
            gu = float(g.get("gpu_util_pct", 0.0))
            gv = float(g.get("vram_pct", 0.0))
            vu = int(g.get("vram_used_bytes", 0))
            vt = int(g.get("vram_total_bytes", 1))
            gpwr = g.get("power_draw_w")
            glim = g.get("power_limit_w")
            gtmp = g.get("temp_c")
            g_spark = render_braille_sparkline(node.gpu_hist or [gu], width=10)
            p_str = f"{float(gpwr):.1f}W" + (f"/{float(glim):.0f}W" if glim else "") if gpwr is not None else "N/A"
            t_str = f"{float(gtmp):.1f}°C" if gtmp is not None else "N/A"
            thr = g.get("throttle_reasons", "")
            thr_s = f" ⚠{thr}" if thr and thr not in ("None", "Idle") else ""
            lines.append(
                f"  {C_BOLD}GPU:{C_RESET} {_ansi_bar(gu, 18, color=C_BLUE, gradient=True)} {C_BLUE}{C_BOLD}{gu:5.1f}%{C_RESET} [{C_BLUE}{g_spark}{C_RESET}] {C_BOLD}{g.get('name', 'GPU')}{C_RESET} "
                f"[{g.get('pstate', 'P0')} {g.get('gpu_clock_mhz', 0)}MHz]{thr_s} │ "
                f"{C_MAGENTA}VRAM {fmt_bytes(vu)}/{fmt_bytes(vt)} ({gv:.1f}%){C_RESET} │ ⚡{p_str} │ 🌡{t_str}"
            )

        top_p = sorted(
            procs,
            key=lambda p: (float(p.get("gpu_pct", 0.0)) * 1.5 + float(p.get("cpu_pct", 0.0)), int(p.get("mem_bytes", 0))),
            reverse=True,
        )[:3]
        p_summary = " │ ".join(
            f"{C_BOLD}{p.get('name', '')[:16]}{C_RESET}(pid:{p.get('pid')} cpu:{float(p.get('cpu_pct', 0)):.1f}% gpu:{float(p.get('gpu_pct', 0)):.1f}% mem:{fmt_bytes(int(p.get('mem_bytes', 0)))})"
            for p in top_p
        )
        lines.append(f"  {C_BOLD}Top:{C_RESET} {p_summary} │ {C_DIM}Disk R:{fmt_speed(float(dsk.get('read_bps', 0)))} W:{fmt_speed(float(dsk.get('write_bps', 0)))}{C_RESET}")
        lines.append(f"{C_BORDER}╰{'─' * (term_w - 2)}╯{C_RESET}")

    return "\n".join(lines)


def run_fleet_live(fleet: FleetCollector, interval_sec: float = 2.0, theme_name: str = "default") -> None:
    """Interactive live-updating Fleet TUI using curses across all cluster nodes."""

    def _safe_add(win, y: int, x: int, text: str, attr: int = 0, max_x: Optional[int] = None) -> None:
        h, w = win.getmaxyx()
        limit_w = min(w, max_x) if max_x is not None else w
        if y < 0 or y >= h or x < 0 or x >= limit_w or not text:
            return
        max_len = limit_w - x
        if y == h - 1 and limit_w == w:
            max_len -= 1
        if max_len <= 0:
            return
        try:
            win.addstr(y, x, text[:max_len], attr)
        except curses.error:
            pass

    def _loop(stdscr) -> None:
        try:
            curses.curs_set(0)
        except Exception:
            pass
        stdscr.nodelay(True)
        stdscr.keypad(True)
        theme = ThemeManager(theme_name)
        theme.apply()
        nodes = fleet.collect()
        last_ts = time.perf_counter()
        frozen = False

        while True:
            now = time.perf_counter()
            if not frozen and now - last_ts >= max(0.5, interval_sec):
                nodes = fleet.collect()
                last_ts = time.perf_counter()

            stdscr.erase()
            h, w = stdscr.getmaxyx()
            if h < 10 or w < 45:
                _safe_add(stdscr, 0, 0, f"Terminal too small ({w}x{h}). Resize to >= 80x20.", theme.attr(ThemeManager.PAIR_HIGH, bold=True))
            else:
                now_s = datetime.datetime.now().strftime("%H:%M:%S")
                online_cnt = sum(1 for n in nodes if n.online)
                frz_tag = " [FROZEN] │" if frozen else ""
                hdr = f" ✦ MacMaster Fleet Cluster ({online_cnt}/{len(nodes)} Online) │ {now_s} │{frz_tag} Interval {interval_sec:.1f}s │ [f]Freeze [c]Theme [q]Quit "
                _safe_add(stdscr, 0, 0, hdr.ljust(w), theme.attr(ThemeManager.PAIR_HEADER, bold=True))

                cur_y = 1
                node_h = max(6, (h - 1) // max(1, len(nodes)))
                for node in nodes:
                    if cur_y + 4 > h:
                        break
                    bh = min(node_h, h - cur_y)
                    box_w = max(4, w - 1)
                    top_bar = "╭" + ("─" * (box_w - 2)) + "╮"
                    bot_bar = "╰" + ("─" * (box_w - 2)) + "╯"
                    _safe_add(stdscr, cur_y, 0, top_bar, theme.attr(ThemeManager.PAIR_BORDER))
                    for r in range(cur_y + 1, cur_y + bh - 1):
                        _safe_add(stdscr, r, 0, "│", theme.attr(ThemeManager.PAIR_BORDER))
                        _safe_add(stdscr, r, box_w - 1, "│", theme.attr(ThemeManager.PAIR_BORDER))
                    _safe_add(stdscr, cur_y + bh - 1, 0, bot_bar, theme.attr(ThemeManager.PAIR_BORDER))

                    inner_max_x = box_w - 1
                    if not node.online or not node.data:
                        _safe_add(stdscr, cur_y, 3, f" ✖ {node.host} [OFFLINE: {node.error}] ", theme.attr(ThemeManager.PAIR_HIGH, bold=True), max_x=inner_max_x)
                        _safe_add(
                            stdscr,
                            cur_y + 1,
                            2,
                            f"Unreachable ({node.latency_ms:.0f}ms). Check SSH or run `bash install.sh --push {node.host}`.",
                            theme.attr(ThemeManager.PAIR_DIM),
                            max_x=inner_max_x,
                        )
                        cur_y += bh
                        continue

                    d = node.data
                    cpu = d.get("cpu", {})
                    mem = d.get("mem", {})
                    gpus = d.get("gpus", [])
                    procs = d.get("processes", [])
                    cpu_pct = float(cpu.get("total_pct", 0.0))
                    mem_pct = float(mem.get("used_pct", 0.0))
                    sys_pwr = cpu.get("sys_power_w") or cpu.get("pkg_power_w") or 0.0
                    title = f" ● {node.host} ({d.get('hostname', '')} — {d.get('os_name', '')} {d.get('arch', '')}) │ SSH {node.latency_ms:.0f}ms │ ⚡{float(sys_pwr):.1f}W "
                    _safe_add(stdscr, cur_y, 3, title, theme.attr(ThemeManager.PAIR_LOW, bold=True), max_x=inner_max_x)
                    c_spark = render_braille_sparkline(node.cpu_hist or [cpu_pct], width=12)
                    line1 = (
                        f"CPU [{c_spark}] {render_bar(cpu_pct, 16)} {cpu_pct:5.1f}% ({cpu.get('model', '')} {cpu.get('logical_cores', 0)}c)   "
                        f"RAM {render_bar(mem_pct, 14)} {mem_pct:4.1f}% ({fmt_bytes(int(mem.get('used_bytes', 0)))}/{fmt_bytes(int(mem.get('total_bytes', 1)))})"
                    )
                    _safe_add(stdscr, cur_y + 1, 2, line1, theme.attr(ThemeManager.PAIR_TEXT, bold=True), max_x=inner_max_x)
                    ry = cur_y + 2
                    for g in gpus:
                        if ry >= cur_y + bh - 1:
                            break
                        gu = float(g.get("gpu_util_pct", 0.0))
                        gv = float(g.get("vram_pct", 0.0))
                        g_spark = render_braille_sparkline(node.gpu_hist or [gu], width=12)
                        gline = (
                            f"GPU [{g_spark}] {render_bar(gu, 16)} {gu:5.1f}% {g.get('name', '')[:24]} [{g.get('pstate', 'P0')}]   "
                            f"VRAM {render_bar(gv, 12)} {gv:4.1f}% ({fmt_bytes(int(g.get('vram_used_bytes', 0)))}/{fmt_bytes(int(g.get('vram_total_bytes', 1)))}) "
                            f"⚡{float(g.get('power_draw_w') or 0.0):.1f}W 🌡{float(g.get('temp_c') or 0.0):.1f}°C"
                        )
                        _safe_add(stdscr, ry, 2, gline, theme.attr(ThemeManager.PAIR_GPU_MAIN, bold=True), max_x=inner_max_x)
                        ry += 1
                    if ry < cur_y + bh - 1 and procs:
                        top_p = sorted(procs, key=lambda p: float(p.get("gpu_pct", 0)) * 1.5 + float(p.get("cpu_pct", 0)), reverse=True)[:4]
                        pline = "Top Procs: " + "  │  ".join(
                            f"{p.get('name', '')[:16]} (CPU {float(p.get('cpu_pct', 0)):.1f}% GPU {float(p.get('gpu_pct', 0)):.1f}%)"
                            for p in top_p
                        )
                        _safe_add(stdscr, ry, 2, pline, theme.attr(ThemeManager.PAIR_DIM), max_x=inner_max_x)
                    cur_y += bh

            stdscr.refresh()
            curses.napms(50)
            quit_flag = False
            while True:
                ch = stdscr.getch()
                if ch == -1:
                    break
                if ch in (ord("q"), ord("Q"), 27):
                    quit_flag = True
                    break
                if ch in (ord("f"), ord("F")):
                    frozen = not frozen
                elif ch in (ord("c"), ord("C")):
                    theme.cycle()
            if quit_flag:
                break

    curses.wrapper(_loop)
