#!/usr/bin/env python3
"""thermalmaster -- every temperature sensor on this machine, as live curves in the terminal.

Reads the operating system directly: no helper programs, no root, no packages.

  macOS  (Apple silicon, Intel)     AppleSMC keys and IOHID temperature services, via IOKit
  Linux  (Ubuntu, Pop!_OS, DGX OS)  /sys/class/hwmon, /sys/class/thermal, power_supply, NVML

`thermalmaster --fleet` shows every machine in fleet.json on one screen: it streams this file
into `python3` on each of them over ssh, so nothing is installed there.

Python 3.8+ standard library only. `thermalmaster --help` lists the options, `?` in the TUI
lists the keys.
"""

from __future__ import annotations

import argparse
import array
import collections
import csv
import ctypes
import fnmatch
import glob
import json
import locale
import math
import os
import platform
import re
import shlex
import socket
import struct
import subprocess
import sys
import threading
import time
import unicodedata

VERSION = "1.0.0"
PROBE_FORMAT = "thermalmaster-probe/1"
NAN = float("nan")

# Display order, title, and the warn/crit (Celsius) used when the hardware reports none.
GROUPS = collections.OrderedDict([
    ("cpu", ("CPU", 85.0, 95.0)),
    ("gpu", ("GPU", 83.0, 90.0)),
    ("soc", ("SoC", 85.0, 95.0)),
    ("mem", ("Memory", 70.0, 85.0)),
    ("storage", ("Storage", 65.0, 75.0)),
    ("vrm", ("Power/VRM", 90.0, 105.0)),
    ("net", ("Network", 85.0, 100.0)),
    ("acpi", ("ACPI zones", 85.0, 100.0)),
    ("board", ("Board", 65.0, 80.0)),
    ("ambient", ("Ambient", 45.0, 60.0)),
    ("battery", ("Battery", 45.0, 55.0)),
    ("other", ("Other", 80.0, 95.0)),
    ("fan", ("Fans", None, None)),
    ("gpufan", ("GPU fans", None, None)),
    ("power", ("Power", None, None)),
])

WINDOWS = (60.0, 120.0, 300.0, 900.0, 1800.0, 3600.0, 3 * 3600.0, 6 * 3600.0)


# ----------------------------------------------------------------------------- helpers ---

def natural_key(text):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text)]


def to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def milli(text):
    v = to_float(text)
    return None if v is None else v / 1000.0


def sane_limit(v):
    """Hardware thresholds are often placeholders (0, 65261.85, -274): keep plausible ones."""
    return v if v is not None and 0.0 < v < 200.0 else None


def sane_fan_limit(v):
    """Fan min/max limits are sometimes 0 or 65535 placeholders: keep plausible RPMs."""
    return float(v) if v is not None and 0.0 < v <= 30000.0 else None


def sane_watt_limit(v):
    """Power cap limits in Watts: filter 0 or bogus firmware placeholders."""
    return float(v) if v is not None and 0.0 < v <= 10000.0 else None


def parse_duration(text):
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*$", str(text).lower())
    if not m:
        raise argparse.ArgumentTypeError("expected a duration such as 90s, 5m or 1h")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def fmt_duration(sec):
    if sec >= 3600:
        return "%gh" % round(sec / 3600.0, 2)
    if sec >= 60:
        return "%gm" % round(sec / 60.0, 2)
    return "%ds" % round(sec) if sec >= 1 else "%gs" % round(sec, 2)


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def isnan(v):
    return v is None or v != v


# Display widths. Box-drawing glyphs, the degree sign and the middle dot are East-Asian-Width
# "Ambiguous": one column in a Western terminal, two under a CJK font. The ASCII glyph set
# avoids them; braille (the charts) is "Neutral" and safe everywhere.
_AMBIGUOUS_WIDE = [False]
_CW = {}


def set_ambiguous_wide(flag):
    _AMBIGUOUS_WIDE[0] = bool(flag)
    _CW.clear()


def cwidth(ch):
    w = _CW.get(ch)
    if w is None:
        o = ord(ch)
        if 0x20 <= o < 0x7F:
            w = 1
        elif o < 0x20 or 0x7F <= o < 0xA0 or unicodedata.combining(ch):
            w = 0
        else:
            eaw = unicodedata.east_asian_width(ch)
            w = 2 if eaw in ("W", "F") or (eaw == "A" and _AMBIGUOUS_WIDE[0]) else 1
        _CW[ch] = w
    return w


def dwidth(text):
    return len(text) if text.isascii() else sum(cwidth(c) for c in text)


def clip(text, width):
    if width <= 0:
        return ""
    if dwidth(text) <= width:
        return text
    out, w = [], 0
    for c in text:
        cw = cwidth(c)
        if w + cw > width:
            break
        out.append(c)
        w += cw
    return "".join(out)


def ellipsize(text, width, ell="~"):
    if dwidth(text) <= width:
        return text
    if width <= dwidth(ell):
        return clip(text, width)
    return clip(text, width - dwidth(ell)) + ell


def ljust(text, width):
    return text + " " * max(0, width - dwidth(text))


def rjust(text, width):
    return " " * max(0, width - dwidth(text)) + text


def cjk_locale():
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        val = os.environ.get(var)
        if val:
            return any(tag in val.lower() for tag in ("zh", "ja", "ko", "cjk"))
    return False


class Glyphs:
    def __init__(self, name):
        self.name = name
        if name == "unicode":
            self.h, self.v, self.tl, self.tr, self.bl, self.br = "─", "│", "╭", "╮", "╰", "╯"
            self.tick, self.deg, self.dot, self.ell, self.mark = "┤", "°", "·", "…", ">"
        else:
            self.h, self.v, self.tl, self.tr, self.bl, self.br = "-", "|", "+", "+", "+", "+"
            self.tick, self.deg, self.dot, self.ell, self.mark = "+", "", "|", "~", ">"


# ----------------------------------------------------------------------------- sensors ---

class Sensor:
    """One reading channel. `kind` is "temp" (Celsius), "watt" (Watts), "rpm" or "pct" (fans).

    A value counts only when lo < value < hi; anything else is a placeholder the firmware
    uses for "absent" (0, -40, 10.0 on some Apple keys, 65261.85 ...). `warn`/`crit` are
    limits the hardware itself reports; `defaults` are this tool's fallback for the platform."""

    __slots__ = ("sid", "label", "group", "source", "detail", "kind", "warn", "crit", "defaults",
                 "lo", "hi", "rmin", "rmax", "parked_at")

    def __init__(self, sid, label, group, source, detail="", kind="temp", warn=None, crit=None,
                 lo=None, hi=None, defaults=None, rmin=None, rmax=None):
        self.sid, self.label, self.group, self.source = sid, label, group, source
        self.detail, self.kind, self.warn, self.crit = detail, kind, warn, crit
        self.defaults = defaults or (None, None)
        self.lo = lo if lo is not None else (0.0 if kind in ("temp", "watt") else -1.0)
        self.hi = hi if hi is not None else {"temp": 150.0, "watt": 5000.0,
                                             "rpm": 30000.0, "pct": 100.5}[kind]
        lim_fn = sane_watt_limit if kind == "watt" else sane_fan_limit
        self.rmin, self.rmax = lim_fn(rmin), lim_fn(rmax)
        if self.rmin is not None and self.rmax is not None and self.rmin >= self.rmax:
            self.rmin = None
        self.parked_at = None

    def valid(self, v):
        return v is not None and self.lo < v < self.hi

    def hw_limits(self):
        return self.warn is not None or self.crit is not None

    def limits(self):
        warn = self.warn if self.warn is not None else self.defaults[0]
        crit = self.crit if self.crit is not None else self.defaults[1]
        if warn is None:
            warn = GROUPS[self.group][1]
        if crit is None:
            crit = GROUPS[self.group][2]
        if warn is not None and crit is not None and warn > crit:
            warn = crit
        return warn, crit

    def margin(self, v):
        """Distance to critical limit (crit - v) in Celsius; smaller means closer to critical."""
        if self.kind != "temp" or isnan(v) or not self.valid(v):
            return NAN
        _, crit = self.limits()
        return (crit - float(v)) if crit is not None else NAN

    def fan_pct(self, v):
        """Speed/power as 0..100% of rmax (or direct % for pct fans), or NAN if unknown."""
        if isnan(v) or not self.valid(v):
            return NAN
        if self.kind == "pct":
            return max(0.0, min(100.0, float(v)))
        if self.kind in ("rpm", "watt") and self.rmax is not None and self.rmax > 0.0:
            if v <= 0.0:
                return 0.0
            return max(0.0, min(100.0, float(v) * 100.0 / self.rmax))
        return NAN

    def __repr__(self):
        return "Sensor(%s, %r, %s)" % (self.sid, self.label, self.group)


class Source:
    """Sensors that are read together. Between reads the last values are held, so a slow
    chip (SMM, WMI, SMART) is polled less often without stalling the fast ones."""

    def __init__(self, name, sensors, every=0.0, adaptive=False):
        self.name = name
        self.sensors = list(sensors)
        self.every = every
        self.adaptive = adaptive
        self.cost = None          # seconds per read, smoothed
        self.last = -1e18
        self.values = {}
        self.throttles = []       # [(badge_text, style)] active hardware throttling flags
        self.errors = 0

    def read(self):
        return {}

    def interval(self, tick):
        iv = max(tick, self.every)
        if self.adaptive and self.cost is not None and self.cost > 0.02:
            iv = max(iv, self.cost * 50.0)   # a slow firmware call stays under ~2% of wall time
        return iv

    def poll(self, now, tick, force=False):
        if not force and now - self.last < self.interval(tick) - 1e-3:
            return self.values
        t0 = time.perf_counter()
        try:
            vals = self.read()
        except Exception:  # noqa: BLE001 - one bad chip must not kill the sampler
            vals = {}
            self.errors += 1
        dt = time.perf_counter() - t0
        self.cost = dt if self.cost is None else 0.7 * self.cost + 0.3 * dt
        self.last = now
        self.values = vals
        return vals

    def keep(self, sids):
        self.sensors = [s for s in self.sensors if s.sid in sids]


# ------------------------------------------------------------------------------- Linux ---

class SysFS:
    """Filesystem access for discovery and sampling. With record=True every answer is kept,
    so `--probe` can save a machine's sensor layout as a test fixture."""

    def __init__(self, record=False):
        self.rec = {"glob": {}, "read": {}, "real": {}, "exists": {}, "list": {}} if record else None

    def _keep(self, kind, key, value):
        if self.rec is not None:
            self.rec[kind][key] = value
        return value

    def glob(self, pattern):
        return self._keep("glob", pattern, sorted(glob.glob(pattern), key=natural_key))

    def read(self, path):
        try:
            with open(path, "rb") as f:
                value = f.read(4096).decode("utf-8", "replace").strip()
        except OSError:
            value = None
        return self._keep("read", path, value)

    def realpath(self, path):
        return self._keep("real", path, os.path.realpath(path))

    def exists(self, path):
        return self._keep("exists", path, os.path.exists(path))

    def listdir(self, path):
        try:
            names = sorted(os.listdir(path), key=natural_key)
        except OSError:
            names = []
        return self._keep("list", path, names)


class ReplayFS(SysFS):
    """A recorded /sys (see SysFS). Values never change; tests edit `data["read"]`."""

    def __init__(self, data):
        super().__init__(record=False)
        self.data = data

    def glob(self, pattern):
        return list(self.data["glob"].get(pattern, []))

    def read(self, path):
        return self.data["read"].get(path)

    def realpath(self, path):
        return self.data["real"].get(path, path)

    def exists(self, path):
        return bool(self.data["exists"].get(path, False))

    def listdir(self, path):
        return list(self.data["list"].get(path, []))


class FileSource(Source):
    """One sysfs file per sensor; `scale` turns the raw integer into Celsius, Watts or rpm."""

    def __init__(self, name, fs, entries, every=0.0, adaptive=True, throttle_paths=None):
        super().__init__(name, [e[0] for e in entries], every, adaptive)
        self.fs = fs
        self.entries = entries
        self.throttle_paths = list(throttle_paths or [])
        self.throttle_base = None

    def read(self):
        out = {}
        for sensor, path, scale in self.entries:
            v = to_float(self.fs.read(path))
            if v is not None:
                out[sensor.sid] = v * scale
        if self.throttle_paths:
            if self.throttle_base is None:
                self.throttle_base = {}
            delta = 0
            for tp in self.throttle_paths:
                tv = to_float(self.fs.read(tp))
                if tv is not None and tv >= 0:
                    iv = int(tv)
                    base = self.throttle_base.setdefault(tp, iv)
                    if iv - base > delta:
                        delta = iv - base
            self.throttles = [("CPU THROTTLED (+%d)" % delta, "crit")] if delta > 0 else []
        return out

    def keep(self, sids):
        super().keep(sids)
        self.entries = [e for e in self.entries if e[0].sid in sids]


PCI_FUNCTION = re.compile(r"^([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2})\.([0-7])$")
CPU_CHIPS = ("coretemp", "k10temp", "k8temp", "zenpower", "cpu_thermal", "via_cputemp")
GPU_CHIPS = ("amdgpu", "radeon", "nouveau", "i915", "xe")
STORAGE_CHIPS = ("nvme", "drivetemp")
MEM_CHIPS = ("spd5118", "jc42", "ee1004")
NET_CHIPS = ("iwlwifi", "mt76", "mt79", "ath", "rtw", "brcm", "r8169", "r8125", "r8127", "mlx",
             "bnxt", "igc", "igb", "ixgbe", "i40e", "ice", "tg3", "atlantic", "e1000", "aqc")
# Seconds between reads for chips whose reads are expensive. dell_smm is the one that
# matters: every read is an SMM call (~2 ms with all cores stalled; measured 31 ms for 14
# files on a Precision workstation). WMI and ACPI methods are slow, NVMe reads fetch the
# SMART log, drivetemp issues ATA commands.
SLOW_CHIPS = (
    ("dell_smm", 10.0), ("dell_ddv", 5.0), ("alienware_wmi", 5.0), ("hp_wmi_sensors", 5.0),
    ("asus_wmi_sensors", 5.0), ("asus_ec", 2.0), ("asusec", 2.0), ("thinkpad", 2.0),
    ("drivetemp", 10.0), ("nvme", 2.0), ("spd5118", 2.0), ("jc42", 2.0),
    ("mt7", 2.0), ("ath", 2.0), ("iwlwifi", 2.0),
)

LABEL_GROUPS = (
    (re.compile(r"gpu|video|vga|graphic|vram", re.I), "gpu"),
    (re.compile(r"cpu|core|package|tctl|tccd|tdie|proc|peci", re.I), "cpu"),
    (re.compile(r"vrm|\bvr|vcore|mos|charger|psu|power|pwr", re.I), "vrm"),
    (re.compile(r"hdd|ssd|nvme|disk|drive|m\.2", re.I), "storage"),
    (re.compile(r"dimm|memory|\bram\b|\bmem\b", re.I), "mem"),
    (re.compile(r"ambient|inlet|intake|exhaust|outlet|skin|palm|air", re.I), "ambient"),
    (re.compile(r"batt|\bbat", re.I), "battery"),
    (re.compile(r"wifi|wlan|wireless|\bnic\b", re.I), "net"),
)


def classify_label(label):
    for rx, group in LABEL_GROUPS:
        if rx.search(label or ""):
            return group
    return "board"


def classify_chip(lname, real, dev, is_net):
    """Group for a whole hwmon chip, or None when each sensor must be judged by its label."""
    if lname.startswith(CPU_CHIPS):
        return "cpu"
    if lname.startswith(GPU_CHIPS):
        return "gpu"
    if lname.startswith(STORAGE_CHIPS):
        return "storage"
    if lname.startswith(MEM_CHIPS):
        return "mem"
    if "/power_supply/" in real:
        return "battery" if lname.startswith("bat") else "vrm"
    if (is_net or lname.startswith(NET_CHIPS) or "/ieee80211/" in dev or "/mdio_bus/" in dev
            or "/net/" in dev):
        return "net"
    if lname.startswith("acpitz"):
        return "acpi"
    return None


def classify_zone(typ):
    t = typ.lower()
    if t == "x86_pkg_temp" or t.startswith("tcpu") or "cpu" in t:
        return "cpu"
    if t == "vram" or t.startswith("tvga") or "gpu" in t:
        return "gpu"
    if t.startswith("acpitz"):
        return "acpi"
    if t.startswith(("iwlwifi", "wifi", "wlan", "ath", "mt7")):
        return "net"
    if t.startswith("bat"):
        return "battery"
    if "ssd" in t or "nvme" in t or "disk" in t:
        return "storage"
    if t.endswith("vr") or "vrm" in t or t.startswith(("chag", "charger")):
        return "vrm"
    if "ddr" in t or "dimm" in t or "mem" in t:
        return "mem"
    if "soc" in t or t.startswith("tsens"):
        return "soc"
    if "skin" in t or "amb" in t:
        return "ambient"
    if t.startswith(("pch", "int3400", "tmcu", "sen")):
        return "board"
    return "other"


def slow_interval(lname):
    for prefix, every in SLOW_CHIPS:
        if lname.startswith(prefix):
            return every
    return 0.0


def compose_label(title, label, fallback, single):
    """"nvme0" + "Composite" -> "nvme0 Composite"; a lone unlabeled input keeps the title."""
    if not label and single and title:
        return title
    label = label or fallback
    if not title:
        return label
    if label.lower() == "temp" and single:
        return title
    return "%s %s" % (title, label)


def hwmon_sources(fs, root="/sys"):
    chips = []
    for hw in fs.glob(root + "/class/hwmon/hwmon*"):
        real = fs.realpath(hw)
        if "/thermal_zone" in real:
            continue    # the thermal core's mirror of a zone (acpitz, iwlwifi): read the zone
        name = fs.read(hw + "/name") or os.path.basename(hw)
        has_dev = fs.exists(hw + "/device")
        dev = fs.realpath(hw + "/device") if has_dev else real
        base = hw
        temps = fs.glob(hw + "/temp*_input")
        fans = fs.glob(hw + "/fan*_input")
        powers = sorted(fs.glob(hw + "/power*_input")
                        + [p for p in fs.glob(hw + "/power*_average")
                           if not fs.exists(p[:-len("_average")] + "_input")], key=natural_key)
        if not temps and not fans and not powers and has_dev:     # pre-2014 drivers keep files on the device
            base = hw + "/device"
            temps = fs.glob(base + "/temp*_input")
            fans = fs.glob(base + "/fan*_input")
            powers = sorted(fs.glob(base + "/power*_input")
                            + [p for p in fs.glob(base + "/power*_average")
                               if not fs.exists(p[:-len("_average")] + "_input")], key=natural_key)
        if not temps and not fans and not powers:
            continue
        chips.append({"hw": hw, "real": real, "name": name, "dev": dev, "has_dev": has_dev,
                      "base": base, "temps": temps, "fans": fans, "powers": powers,
                      "devid": os.path.basename(dev)})

    # Every function of a multi-function PCI device reports the same die (all ports of a
    # ConnectX NIC read one ASIC): keep the lowest function only.
    kept, seen = [], set()
    for c in chips:
        m = PCI_FUNCTION.match(c["devid"])
        if m:
            slot = (c["name"], m.group(1))
            if slot in seen:
                continue
            seen.add(slot)
        kept.append(c)

    per_name = collections.Counter(c["name"] for c in kept)
    ordinal = collections.Counter()
    mem_ordinal = [0]
    sources = []
    for c in kept:
        name, lname = c["name"], c["name"].lower()
        ordinal[name] += 1
        k = ordinal[name]
        ifaces = fs.listdir(c["hw"] + "/device/net") if c["has_dev"] else []
        group = classify_chip(lname, c["real"], c["dev"], bool(ifaces))
        model = ""
        if lname == "nvme":
            title = c["devid"]                              # nvme0
            model = fs.read(c["hw"] + "/device/model") or ""
        elif ifaces:
            title = ifaces[0]                               # enp6s0, enP2p1s0f0np0
        elif "/ieee80211/" in c["dev"]:
            title = "wifi " + c["devid"]                    # wifi phy0
        elif "/mdio_bus/" in c["dev"]:
            title = name.split("_")[0] + " phy"             # r8169 phy
        elif group == "mem":
            mem_ordinal[0] += 1
            title = "DIMM %d" % mem_ordinal[0]
        elif group == "cpu":
            title = ("CPU%d" % (k - 1)) if per_name[name] > 1 else ""
        elif group == "battery":
            title = name
        else:
            title = name if per_name[name] == 1 else "%s%d" % (name, k)
        key = "%s@%s" % (name, c["devid"])
        detail = "hwmon %s%s %s %s" % (name, (" " + model) if model else "", "@", c["devid"])
        single = len(c["temps"]) == 1
        entries = []
        for path in c["temps"]:
            m = re.search(r"temp(\d+)_input$", path)
            if not m:
                continue
            n, stem = m.group(1), path[: -len("_input")]
            label = (fs.read(stem + "_label") or "").strip()
            sgroup = group or classify_label(label)
            warn = sane_limit(milli(fs.read(stem + "_max")))
            crit = sane_limit(milli(fs.read(stem + "_crit")))
            sensor = Sensor("hwmon/%s/temp%s" % (key, n),
                            compose_label(title, label, "temp" + n, single),
                            sgroup, "hwmon " + name, detail, "temp", warn, crit)
            entries.append((sensor, path, 0.001))
        for path in c["fans"]:
            m = re.search(r"fan(\d+)_input$", path)
            if not m:
                continue
            n, stem = m.group(1), path[: -len("_input")]
            label = (fs.read(stem + "_label") or "").strip()
            rmin = sane_fan_limit(to_float(fs.read(stem + "_min")))
            rmax = sane_fan_limit(to_float(fs.read(stem + "_max")))
            if rmin is not None and rmax is not None and rmin >= rmax:
                rmin = None
            fdetail = detail
            if rmin is not None and rmax is not None:
                fdetail += " %d..%d rpm" % (rmin, rmax)
            elif rmax is not None:
                fdetail += " max %d rpm" % rmax
            sensor = Sensor("hwmon/%s/fan%s" % (key, n),
                            compose_label(title or name, label, "fan" + n, False),
                            "fan", "hwmon " + name, fdetail, "rpm", rmin=rmin, rmax=rmax)
            entries.append((sensor, path, 1.0))
        for path in c["powers"]:
            m = re.search(r"power(\d+)_(input|average)$", path)
            if not m:
                continue
            n, suffix = m.group(1), m.group(2)
            stem = path[: -len("_" + suffix)]
            label = (fs.read(stem + "_label") or "").strip()
            raw_cap = to_float(fs.read(stem + "_cap"))
            if raw_cap is None:
                raw_cap = to_float(fs.read(stem + "_max"))
            cap = sane_watt_limit(raw_cap * 1e-6 if raw_cap is not None else None)
            pdetail = detail + (" cap %dW" % round(cap) if cap else "")
            plabel = compose_label(title or name, label, "power" + n, False)
            if not any(w in plabel.lower() for w in ("power", "ppt", "watt")):
                plabel += " power"
            sensor = Sensor("hwmon/%s/power%s" % (key, n), plabel,
                            "power", "hwmon " + name, pdetail, "watt", rmax=cap)
            entries.append((sensor, path, 1e-6))
        if entries:
            label = "hwmon %s" % name + ("" if per_name[name] == 1 else " " + c["devid"])
            sources.append(FileSource(label, fs, entries, slow_interval(lname), adaptive=True))
    return sources


def zone_source(fs, root="/sys"):
    zones = fs.glob(root + "/class/thermal/thermal_zone*")
    rows = []
    for z in zones:
        m = re.search(r"(\d+)$", z)
        n = int(m.group(1)) if m else 0
        typ = fs.read(z + "/type") or "zone%d" % n
        trips = collections.defaultdict(list)
        for tp in fs.glob(z + "/trip_point_*_type"):
            mt = re.search(r"trip_point_(\d+)_type$", tp)
            if not mt:
                continue
            t = sane_limit(milli(fs.read(z + "/trip_point_%s_temp" % mt.group(1))))
            if t is not None:
                trips[(fs.read(tp) or "").lower()].append(t)
        crits = trips["critical"] + trips["hot"]
        rows.append((n, typ, z, min(trips["passive"]) if trips["passive"] else None,
                     min(crits) if crits else None))
    counts = collections.Counter(r[1] for r in rows)
    entries = []
    for n, typ, z, warn, crit in rows:
        label = typ if counts[typ] == 1 else "%s #%d" % (typ, n)
        sensor = Sensor("zone/%d/%s" % (n, typ), label, classify_zone(typ), "thermal zone",
                        "thermal_zone%d %s" % (n, typ), "temp", warn, crit)
        entries.append((sensor, z + "/temp", 0.001))
    return FileSource("thermal zones", fs, entries, 0.0, adaptive=True) if entries else None


def power_supply_source(fs, root="/sys"):
    entries = []
    for ps in fs.glob(root + "/class/power_supply/*"):
        if fs.glob(ps + "/hwmon*"):
            continue    # already exposed through hwmon
        if not fs.exists(ps + "/temp"):
            continue
        name = os.path.basename(ps)
        typ = (fs.read(ps + "/type") or "").lower()
        group = "battery" if typ == "battery" or name.lower().startswith("bat") else "vrm"
        sensor = Sensor("psu/%s/temp" % name, name, group, "power_supply", "power_supply " + name)
        entries.append((sensor, ps + "/temp", 0.1))
    return FileSource("power supply", fs, entries, 5.0) if entries else None


class _PciInfo(ctypes.Structure):
    _fields_ = [("busIdLegacy", ctypes.c_char * 16), ("domain", ctypes.c_uint),
                ("bus", ctypes.c_uint), ("device", ctypes.c_uint),
                ("pciDeviceId", ctypes.c_uint), ("pciSubSystemId", ctypes.c_uint),
                ("busId", ctypes.c_char * 32)]


class _FieldValue(ctypes.Structure):
    _fields_ = [("fieldId", ctypes.c_uint), ("scopeId", ctypes.c_uint),
                ("timestamp", ctypes.c_longlong), ("latencyUsec", ctypes.c_longlong),
                ("valueType", ctypes.c_uint), ("nvmlReturn", ctypes.c_uint),
                ("value", ctypes.c_ulonglong)]


class NvmlApi:
    """The few NVML calls needed for temperatures and fans. None of them need root."""

    FI_DEV_MEMORY_TEMP = 82

    def __init__(self):
        lib = None
        for soname in ("libnvidia-ml.so.1", "libnvidia-ml.so"):
            try:
                lib = ctypes.CDLL(soname)
                break
            except OSError:
                continue
        if lib is None:
            raise OSError("libnvidia-ml.so.1 not found (no NVIDIA driver)")
        self.lib = lib
        c, u = ctypes, ctypes.c_uint

        def fn(name, *args):
            f = getattr(lib, name, None)
            if f is not None:
                f.restype = c.c_int
                f.argtypes = list(args)
            return f

        self._init = fn("nvmlInit_v2")
        self._count = fn("nvmlDeviceGetCount_v2", c.POINTER(u))
        self._handle = fn("nvmlDeviceGetHandleByIndex_v2", u, c.POINTER(c.c_void_p))
        self._name = fn("nvmlDeviceGetName", c.c_void_p, c.c_char_p, u)
        self._pci = fn("nvmlDeviceGetPciInfo_v3", c.c_void_p, c.POINTER(_PciInfo))
        self._temp = fn("nvmlDeviceGetTemperature", c.c_void_p, c.c_int, c.POINTER(u))
        self._thr = fn("nvmlDeviceGetTemperatureThreshold", c.c_void_p, c.c_int, c.POINTER(u))
        self._fields = fn("nvmlDeviceGetFieldValues", c.c_void_p, c.c_int, c.POINTER(_FieldValue))
        self._nfans = fn("nvmlDeviceGetNumFans", c.c_void_p, c.POINTER(u))
        self._fan2 = fn("nvmlDeviceGetFanSpeed_v2", c.c_void_p, u, c.POINTER(u))
        self._fan = fn("nvmlDeviceGetFanSpeed", c.c_void_p, c.POINTER(u))
        self._power = fn("nvmlDeviceGetPowerUsage", c.c_void_p, c.POINTER(u))
        self._plimit = fn("nvmlDeviceGetPowerManagementLimit", c.c_void_p, c.POINTER(u))
        self._pstate = fn("nvmlDeviceGetPerformanceState", c.c_void_p, c.POINTER(u))
        self._throttle = fn("nvmlDeviceGetCurrentClocksThrottleReasons", c.c_void_p,
                            c.POINTER(c.c_ulonglong))
        self._driver = fn("nvmlSystemGetDriverVersion", c.c_char_p, u)
        self._shutdown = fn("nvmlShutdown")
        if self._init is None or self._temp is None:
            raise OSError("libnvidia-ml lacks nvmlInit_v2/nvmlDeviceGetTemperature")
        rc = self._init()
        if rc != 0:
            raise OSError("nvmlInit_v2 failed (rc=%d)" % rc)
        self.handles = {}
        cnt = u(0)
        if self._count(c.byref(cnt)) == 0:
            for i in range(cnt.value):
                h = c.c_void_p()
                if self._handle(i, c.byref(h)) == 0:
                    self.handles[i] = h
        self._gpus = None

    def _uint(self, f, *args):
        if f is None:
            return None
        v = ctypes.c_uint(0)
        return v.value if f(*(args + (ctypes.byref(v),))) == 0 else None

    def _ull(self, f, *args):
        if f is None:
            return None
        v = ctypes.c_ulonglong(0)
        return v.value if f(*(args + (ctypes.byref(v),))) == 0 else None

    def driver(self):
        buf = ctypes.create_string_buffer(96)
        if self._driver is not None and self._driver(buf, 96) == 0:
            return buf.value.decode("ascii", "replace")
        return ""

    def gpus(self):
        if self._gpus is None:
            out = []
            for i, h in sorted(self.handles.items()):
                buf = ctypes.create_string_buffer(96)
                name = buf.value.decode("utf-8", "replace") if self._name(h, buf, 96) == 0 else ""
                name = name or "GPU %d" % i
                pci = ""
                info = _PciInfo()
                if self._pci is not None and self._pci(h, ctypes.byref(info)) == 0:
                    pci = short_pci(info.busId.decode("ascii", "replace"))
                thr = {k: self._uint(self._thr, h, code)
                       for k, code in (("shutdown", 0), ("slowdown", 1), ("gpu_max", 3))}
                nfans = self._uint(self._nfans, h) or 0
                if not nfans and self._uint(self._fan, h) is not None:
                    nfans = 1
                out.append({"index": i, "name": name, "pci": pci, "thresholds": thr,
                            "memtemp": self.memtemp(i) is not None, "fans": nfans,
                            "power": self.power(i) is not None, "power_limit": self.power_limit(i)})
            self._gpus = out
        return self._gpus

    def temp(self, i):
        return self._uint(self._temp, self.handles[i], 0)

    def memtemp(self, i):
        if self._fields is None:
            return None
        fv = _FieldValue()
        fv.fieldId = self.FI_DEV_MEMORY_TEMP
        if self._fields(self.handles[i], 1, ctypes.byref(fv)) != 0 or fv.nvmlReturn != 0:
            return None
        return float(fv.value & 0xFFFFFFFF) if fv.valueType in (1, 2) else float(fv.value)

    def fan(self, i, k):
        if self._fan2 is not None:
            v = self._uint(self._fan2, self.handles[i], k)
            if v is not None:
                return v
        return self._uint(self._fan, self.handles[i]) if k == 0 else None

    def power(self, i):
        v = self._uint(self._power, self.handles[i])
        return (v / 1000.0) if v is not None else None

    def power_limit(self, i):
        v = self._uint(self._plimit, self.handles[i])
        return sane_watt_limit(v / 1000.0) if v is not None else None

    def pstate(self, i):
        return self._uint(self._pstate, self.handles[i])

    def throttle_reasons(self, i):
        return self._ull(self._throttle, self.handles[i])

    def snapshot(self):
        gpus = []
        for g in self.gpus():
            i = g["index"]
            gpus.append(dict(g, temp_value=self.temp(i), memtemp_value=self.memtemp(i),
                             fan_values=[self.fan(i, k) for k in range(g["fans"])],
                             power_value=self.power(i), pstate=self.pstate(i),
                             throttle_reasons=self.throttle_reasons(i)))
        return {"driver": self.driver(), "gpus": gpus}

    def close(self):
        if self._shutdown is not None:
            self._shutdown()


class FakeNvmlApi:
    """NVML answers recorded by `--probe` (see NvmlApi.snapshot)."""

    def __init__(self, snap):
        self.snap = snap
        self.by_index = {g["index"]: g for g in snap.get("gpus", [])}

    def driver(self):
        return self.snap.get("driver", "")

    def gpus(self):
        return [{"index": g["index"], "name": g["name"], "pci": g["pci"],
                 "thresholds": g["thresholds"], "memtemp": g["memtemp"], "fans": g["fans"],
                 "power": bool(g.get("power") or g.get("power_value") is not None),
                 "power_limit": sane_watt_limit(g.get("power_limit"))}
                for g in self.snap.get("gpus", [])]

    def temp(self, i):
        return self.by_index[i].get("temp_value")

    def memtemp(self, i):
        return self.by_index[i].get("memtemp_value")

    def fan(self, i, k):
        vals = self.by_index[i].get("fan_values") or []
        return vals[k] if k < len(vals) else None

    def power(self, i):
        return self.by_index[i].get("power_value")

    def pstate(self, i):
        return self.by_index[i].get("pstate")

    def throttle_reasons(self, i):
        return self.by_index[i].get("throttle_reasons")

    def close(self):
        pass


def short_pci(bus_id):
    m = re.match(r"^([0-9a-fA-F]+):([0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])$", bus_id.strip())
    if not m:
        return bus_id.strip().lower()
    domain = int(m.group(1), 16)
    return m.group(2).lower() if domain == 0 else "%04x:%s" % (domain, m.group(2).lower())


def short_gpu_name(name):
    n = re.sub(r"^NVIDIA\s+", "", name)
    n = re.sub(r"^(GeForce|Quadro|Tesla)\s+", "", n)
    n = n.replace(" Generation", "")
    return re.sub(r"\s+GPU$", "", n)


NVML_THROTTLE_BITS = (
    (0x0000000000000040, "HW_THERM!", "crit"),
    (0x0000000000000080, "PWR_BRAKE!", "crit"),
    (0x0000000000000020, "SW_THERM", "warn"),
    (0x0000000000000008, "HW_SLOW", "warn"),
    (0x0000000000000004, "PWR_CAP", "dim"),
)


def decode_nvml_throttle(mask, pstate=None):
    """Return (badge_tokens, worst_style) for NVML clocks throttle bitmask and P-state."""
    if mask is None:
        return [], None
    tokens, style = [], None
    rank = {"crit": 3, "warn": 2, "dim": 1, None: 0}
    for bit, tag, st in NVML_THROTTLE_BITS:
        if mask & bit:
            tokens.append(tag)
            if rank[st] > rank[style]:
                style = st
    if tokens and pstate is not None and 0 <= int(pstate) <= 15:
        tokens.append("P%d" % int(pstate))
    return tokens, style


class NvmlSource(Source):
    def __init__(self, api):
        self.api = api
        self.map = []
        self.gpu_indices = []
        sensors = []
        for g in api.gpus():
            i = g["index"]
            self.gpu_indices.append(i)
            thr = g.get("thresholds") or {}
            slow = sane_limit(thr.get("slowdown"))
            crit = slow or sane_limit(thr.get("shutdown"))
            warn = crit - 10 if crit is not None else None
            gmax = sane_limit(thr.get("gpu_max"))
            if gmax is not None and (warn is None or gmax < warn):
                warn = gmax
            base = "nvml/%s" % (g.get("pci") or i)
            detail = "NVML %s%s%s" % (g["name"], " @ " + g["pci"] if g.get("pci") else "",
                                      " slowdown %d C" % slow if slow else "")
            label = "GPU%d %s" % (i, short_gpu_name(g["name"]))
            sensors.append(Sensor(base + "/gpu", label, "gpu", "nvml", detail, "temp", warn, crit))
            self.map.append((base + "/gpu", i, "temp", 0))
            if g.get("memtemp"):
                sensors.append(Sensor(base + "/mem", label + " memory", "gpu", "nvml", detail))
                self.map.append((base + "/mem", i, "mem", 0))
            nf = int(g.get("fans") or 0)
            for k in range(nf):
                sid = "%s/fan%d" % (base, k)
                sensors.append(Sensor(sid, "GPU%d fan%s" % (i, k if nf > 1 else ""), "gpufan",
                                      "nvml", detail, "pct"))
                self.map.append((sid, i, "fan", k))
            if g.get("power"):
                plim = sane_watt_limit(g.get("power_limit"))
                pdetail = detail + (" cap %dW" % round(plim) if plim else "")
                sensors.append(Sensor(base + "/power", label + " power", "power", "nvml",
                                      pdetail, "watt", rmax=plim))
                self.map.append((base + "/power", i, "power", 0))
        super().__init__("nvml", sensors)

    def read(self):
        out = {}
        for sid, i, what, k in self.map:
            if what == "temp":
                v = self.api.temp(i)
            elif what == "mem":
                v = self.api.memtemp(i)
            elif what == "power":
                v = self.api.power(i)
            else:
                v = self.api.fan(i, k)
            if v is not None:
                out[sid] = float(v)
        throttles = []
        for i in self.gpu_indices:
            tokens, style = decode_nvml_throttle(self.api.throttle_reasons(i), self.api.pstate(i))
            if tokens:
                throttles.append(("GPU%d %s" % (i, "/".join(tokens)), style))
        self.throttles = throttles
        return out

    def keep(self, sids):
        super().keep(sids)
        self.map = [m for m in self.map if m[0] in sids]
        kept_gpus = {m[1] for m in self.map}
        self.gpu_indices = [i for i in self.gpu_indices if i in kept_gpus]


SOURCE_TAG = {"thermal zone": "zone", "power_supply": "psu", "nvml": "nvml", "smc": "smc",
              "iohid": "hid"}


def dedupe_labels(sources):
    """Make every label unique: "BAT0" from a zone and from hwmon become "BAT0 (zone)" and
    "BAT0 (hwmon)"; repeats from one chip ("dell_ddv Video" x4) are numbered."""
    by_label = collections.OrderedDict()
    for src in sources:
        for s in src.sensors:
            by_label.setdefault(s.label, []).append(s)
    for label, same in by_label.items():
        if len(same) < 2:
            continue
        tags = [SOURCE_TAG.get(s.source, s.source.split()[0]) for s in same]
        if len(set(tags)) == len(same):
            for s, tag in zip(same, tags):
                s.label = "%s (%s)" % (label, tag)
        else:
            for k, s in enumerate(same, 1):
                s.label = "%s #%d" % (label, k)
    return sources


def linux_sources(fs, root="/sys", nvml="auto"):
    """All Linux sources. `nvml` is "auto" (load the driver library), an API object, or None."""
    sources, notes = [], []
    sources.extend(hwmon_sources(fs, root))
    for make in (zone_source, power_supply_source):
        src = make(fs, root)
        if src is not None:
            sources.append(src)
    tpaths = (fs.glob(root + "/devices/system/cpu/cpu*/thermal_throttle/package_throttle_count")
              or fs.glob(root + "/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count"))[:16]
    if tpaths:
        fsrcs = [src for src in sources if isinstance(src, FileSource)]
        if fsrcs:
            cpu_src = min(fsrcs, key=lambda src: (not any(s.group == "cpu" for s in src.sensors), src.every))
            cpu_src.throttle_paths = tpaths
    if nvml == "auto":
        try:
            nvml = NvmlApi()
        except OSError as e:
            nvml = None
            if os.path.exists("/proc/driver/nvidia"):
                notes.append("NVML: %s" % e)
    if nvml is not None:
        src = NvmlSource(nvml)
        if src.sensors:
            sources.append(src)
    return dedupe_labels(sources), notes


# ------------------------------------------------------------------------------- macOS ---

class _SMCVers(ctypes.Structure):
    _fields_ = [("major", ctypes.c_ubyte), ("minor", ctypes.c_ubyte), ("build", ctypes.c_ubyte),
                ("reserved", ctypes.c_ubyte), ("release", ctypes.c_uint16)]


class _SMCPLimit(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint16), ("length", ctypes.c_uint16),
                ("cpuPLimit", ctypes.c_uint32), ("gpuPLimit", ctypes.c_uint32),
                ("memPLimit", ctypes.c_uint32)]


class _SMCKeyInfo(ctypes.Structure):
    _fields_ = [("dataSize", ctypes.c_uint32), ("dataType", ctypes.c_uint32),
                ("dataAttributes", ctypes.c_ubyte)]


class _SMCKeyData(ctypes.Structure):
    """SMCKeyData_t from the AppleSMC user client; 80 bytes on every Mac."""
    _fields_ = [("key", ctypes.c_uint32), ("vers", _SMCVers), ("pLimitData", _SMCPLimit),
                ("keyInfo", _SMCKeyInfo), ("result", ctypes.c_ubyte), ("status", ctypes.c_ubyte),
                ("data8", ctypes.c_ubyte), ("data32", ctypes.c_uint32),
                ("bytes", ctypes.c_ubyte * 32)]


class _IOKit:
    """ctypes bindings for the IOKit / CoreFoundation calls used here (loaded once)."""

    _instance = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        c, vp, u32 = ctypes, ctypes.c_void_p, ctypes.c_uint32
        try:
            io = c.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
            cf = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            libc = c.CDLL("/usr/lib/libSystem.B.dylib")
        except OSError as e:
            raise OSError("IOKit is not available: %s" % e)
        self.io, self.cf, self.libc = io, cf, libc

        def sig(f, res, *args):
            f.restype = res
            f.argtypes = list(args)
            return f

        self.IOServiceMatching = sig(io.IOServiceMatching, vp, c.c_char_p)
        self.IOServiceGetMatchingService = sig(io.IOServiceGetMatchingService, u32, u32, vp)
        self.IOServiceOpen = sig(io.IOServiceOpen, c.c_int, u32, u32, u32, c.POINTER(u32))
        self.IOServiceClose = sig(io.IOServiceClose, c.c_int, u32)
        self.IOObjectRelease = sig(io.IOObjectRelease, c.c_int, u32)
        self.IOConnectCallStructMethod = sig(io.IOConnectCallStructMethod, c.c_int, u32, u32, vp,
                                             c.c_size_t, vp, c.POINTER(c.c_size_t))
        self.CFStringCreateWithCString = sig(cf.CFStringCreateWithCString, vp, vp, c.c_char_p, u32)
        self.CFNumberCreate = sig(cf.CFNumberCreate, vp, vp, c.c_long, vp)
        self.CFDictionaryCreate = sig(cf.CFDictionaryCreate, vp, vp, c.POINTER(vp),
                                      c.POINTER(vp), c.c_long, vp, vp)
        self.CFArrayGetCount = sig(cf.CFArrayGetCount, c.c_long, vp)
        self.CFArrayGetValueAtIndex = sig(cf.CFArrayGetValueAtIndex, vp, vp, c.c_long)
        self.CFStringGetCString = sig(cf.CFStringGetCString, c.c_bool, vp, c.c_char_p, c.c_long, u32)
        self.CFGetTypeID = sig(cf.CFGetTypeID, c.c_ulong, vp)
        self.CFStringGetTypeID = sig(cf.CFStringGetTypeID, c.c_ulong)
        self.CFRelease = sig(cf.CFRelease, None, vp)
        self.keyCallBacks = c.addressof(c.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
        self.valueCallBacks = c.addressof(c.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
        self.sysctlbyname = sig(libc.sysctlbyname, c.c_int, c.c_char_p, vp, c.POINTER(c.c_size_t),
                                vp, c.c_size_t)
        self.notify_register_check = sig(libc.notify_register_check, u32, c.c_char_p, c.POINTER(c.c_int))
        self.notify_get_state = sig(libc.notify_get_state, u32, c.c_int, c.POINTER(c.c_uint64))
        self.notify_cancel = sig(libc.notify_cancel, u32, c.c_int)
        self.task = u32.in_dll(libc, "mach_task_self_").value

    def cfstr(self, text):
        return self.CFStringCreateWithCString(None, text.encode("utf-8"), 0x08000100)

    def pystr(self, ref):
        if not ref or self.CFGetTypeID(ref) != self.CFStringGetTypeID():
            return None
        buf = ctypes.create_string_buffer(512)
        if self.CFStringGetCString(ref, buf, 512, 0x08000100):
            return buf.value.decode("utf-8", "replace")
        return None


def sysctl_string(name):
    try:
        k = _IOKit.get()
        size = ctypes.c_size_t(0)
        if k.sysctlbyname(name.encode(), None, ctypes.byref(size), None, 0) != 0 or not size.value:
            return ""
        buf = ctypes.create_string_buffer(size.value)
        if k.sysctlbyname(name.encode(), buf, ctypes.byref(size), None, 0) != 0:
            return ""
        return buf.value.decode("utf-8", "replace").strip()
    except (OSError, AttributeError):
        return ""


def sysctl_int(name):
    try:
        k = _IOKit.get()
        val = ctypes.c_int32(0)
        size = ctypes.c_size_t(ctypes.sizeof(val))
        if k.sysctlbyname(name.encode(), ctypes.byref(val), ctypes.byref(size), None, 0) != 0:
            return None
        return int(val.value)
    except (OSError, AttributeError):
        return None


def mac_thermal_state():
    v = sysctl_int("kern.thermalstate")
    if v is not None:
        return min(3, max(0, v))
    try:
        k = _IOKit.get()
        tok = ctypes.c_int(0)
        if k.notify_register_check(b"com.apple.system.thermalpressurelevel", ctypes.byref(tok)) != 0:
            return None
        try:
            state = ctypes.c_uint64(0)
            if k.notify_get_state(tok.value, ctypes.byref(state)) != 0:
                return None
            return min(3, int(state.value))
        finally:
            k.notify_cancel(tok.value)
    except (OSError, AttributeError):
        return None


MAC_THERMAL_STATES = {
    1: ("THERMAL: FAIR", "warn"),
    2: ("THERMAL: SERIOUS", "crit"),
    3: ("THERMAL: CRITICAL!", "crit"),
}


def fourcc(key):
    return int.from_bytes(key.encode("latin-1"), "big")


def fourcc_str(value):
    return value.to_bytes(4, "big").decode("latin-1")


class SMC:
    """The AppleSMC user client every fan/temperature utility talks to. No root needed."""

    def __init__(self):
        k = _IOKit.get()
        svc = k.IOServiceGetMatchingService(0, k.IOServiceMatching(b"AppleSMC"))
        if not svc:
            raise OSError("AppleSMC service not found")
        self.conn = ctypes.c_uint32(0)
        kr = k.IOServiceOpen(svc, k.task, 0, ctypes.byref(self.conn))
        k.IOObjectRelease(svc)
        if kr != 0:
            raise OSError("IOServiceOpen(AppleSMC) failed: 0x%x" % (kr & 0xFFFFFFFF))
        self._call = k.IOConnectCallStructMethod
        self._codes = {}

    def _do(self, key, cmd, data32=0, size=0):
        inp, out = _SMCKeyData(), _SMCKeyData()
        inp.key, inp.data8, inp.data32 = key, cmd, data32
        inp.keyInfo.dataSize = size
        n = ctypes.c_size_t(ctypes.sizeof(out))
        kr = self._call(self.conn.value, 2, ctypes.byref(inp), ctypes.sizeof(inp),
                        ctypes.byref(out), ctypes.byref(n))
        return out if kr == 0 and out.result == 0 else None

    def _code(self, key):
        code = self._codes.get(key)
        if code is None:
            code = self._codes[key] = fourcc(key)
        return code

    def read(self, key, size):
        out = self._do(self._code(key), 5, size=size)
        return bytes(out.bytes[:size]) if out is not None else None

    def key_info(self, key):
        out = self._do(self._code(key), 9)
        if out is None:
            return None
        return out.keyInfo.dataSize, fourcc_str(out.keyInfo.dataType)

    def all_keys(self):
        raw = self.read("#KEY", 4)
        count = int.from_bytes(raw, "big") if raw else 0
        keys = []
        for i in range(count):
            out = self._do(0, 8, data32=i)
            if out is not None:
                keys.append(fourcc_str(out.key))
        return keys


class FakeSMC:
    """SMC keys recorded by `--probe`: [key, type, hex bytes]."""

    def __init__(self, rows, thermal_state=None):
        self.rows = {k: (typ, bytes.fromhex(hx)) for k, typ, hx in rows}
        self.order = [r[0] for r in rows]
        self.thermal_state = thermal_state

    def all_keys(self):
        return list(self.order)

    def key_info(self, key):
        row = self.rows.get(key)
        return (len(row[1]), row[0]) if row else None

    def read(self, key, size):
        row = self.rows.get(key)
        return row[1][:size] if row else None


def smc_decode(typ, raw):
    if raw is None:
        return None
    if typ == "flt " and len(raw) == 4:
        return struct.unpack("<f", raw)[0]
    if len(typ) == 4 and typ[:2] in ("sp", "fp") and len(raw) == 2:
        try:
            frac = int(typ[3], 16)
        except ValueError:
            return None
        return struct.unpack(">h" if typ[:2] == "sp" else ">H", raw)[0] / float(1 << frac)
    if typ in ("ui8 ", "ui16", "ui32"):
        return int.from_bytes(raw, "big")
    if typ in ("si8 ", "si16", "si32"):
        return int.from_bytes(raw, "big", signed=True)
    return None


def smc_is_temp(typ, size):
    return (typ == "flt " and size == 4) or (typ[:2] == "sp" and size == 2)


def smc_is_power(typ, size):
    return (typ == "flt " and size == 4) or (len(typ) == 4 and typ[:2] in ("sp", "fp") and size == 2)


SMC_POWER = {
    "PSTR": "system total power", "PDTR": "DC-in total power", "PDBR": "delivery brick power",
    "PCPC": "CPU package power", "PCPG": "GPU power", "PCPT": "package total power",
    "PC0C": "CPU core power", "PCAM": "ANE power", "PMVR": "memory power", "PPBR": "battery power",
}
SMC_POWER_PREFIX = {
    "PC": "CPU power", "PG": "GPU power", "PM": "memory power", "PP": "power rail",
    "PD": "DC-in power", "PS": "system power", "PH": "storage power", "PB": "battery power",
}


def smc_is_power_key(key):
    return len(key) == 4 and (key in SMC_POWER or key[:2] in SMC_POWER_PREFIX)


# Apple does not document SMC keys. The second character names the subsystem; these maps
# follow the community's reverse engineering and err toward "SoC"/"Other" when unsure.
SMC_ARM = {
    "Tp": ("cpu", "CPU performance cluster"), "Te": ("cpu", "CPU efficiency cluster"),
    "Tc": ("cpu", "CPU core"), "TC": ("cpu", "CPU die"),
    "Tg": ("gpu", "GPU"), "TG": ("gpu", "GPU"), "TR": ("gpu", "GPU"),
    "Tf": ("soc", "SoC die"), "TP": ("soc", "SoC package"), "Tr": ("soc", "SoC"),
    "Th": ("soc", "SoC"),
    "TH": ("storage", "NAND"), "Ts": ("storage", "SSD"), "TS": ("storage", "SSD"),
    "TM": ("mem", "memory"), "Tm": ("mem", "memory"),
    "TV": ("vrm", "voltage regulator"), "Tv": ("vrm", "voltage regulator"),
    "TW": ("net", "Wi-Fi"),
    "Ta": ("ambient", "airflow"), "TA": ("ambient", "ambient"), "TF": ("ambient", "airflow"),
    "TI": ("board", "Thunderbolt / I/O"), "TB": ("board", "board"), "TL": ("board", "display"),
    "TD": ("board", "display"), "TZ": ("other", ""), "Tz": ("other", ""),
}
SMC_INTEL = {
    "TC": ("cpu", "CPU"), "Tc": ("cpu", "CPU core"), "TG": ("gpu", "GPU"), "Tg": ("gpu", "GPU"),
    "TM": ("mem", "memory"), "Tm": ("mem", "memory"), "TH": ("storage", "drive"),
    "TA": ("ambient", "ambient"), "Ta": ("ambient", "airflow"), "Ts": ("ambient", "palm rest"),
    "TN": ("board", "PCH"), "TP": ("board", "PCH / platform"), "TI": ("board", "Thunderbolt"),
    "TL": ("board", "display"), "TO": ("board", "optical drive"), "TS": ("board", "expansion"),
    "Tp": ("vrm", "power supply"), "TV": ("vrm", "voltage regulator"), "TW": ("net", "Wi-Fi"),
    "TB": ("board", "board"),
}
MAC_LIMITS = {"cpu": (90.0, 100.0), "gpu": (90.0, 100.0), "soc": (90.0, 100.0)}
# "Tf" + cluster + field on Apple silicon: fields 1, 2, 5 and 6 are constants, not readings.
# On an idle machine they stay bit-identical for hours (Tf?6 around 60-87 C, Tf?5 around 29-31,
# Tf?1 around 10, Tf?2 around -10) while the live fields 0/4/8/9/A/B/E/F jitter.
SMC_ARM_CONSTANT = re.compile(r"^Tf[0-9A-Za-z][1256]$")
# Die and regulator sensors of a power-gated block are parked at a round value: every P-core
# key of an idle Apple silicon chip reads exactly 40.0 until the cluster wakes. A live reading jitters, so
# "bit-identical to the previous read AND an exact integer" marks a parked (or constant) key.
SMC_PARKABLE = ("Tp", "Te", "Tc", "TC", "Tg", "TG", "TR", "Tf", "TP", "Tr", "Th", "TV")
# Apple silicon parks unused keys at 0 or at a round 10.0; nothing inside a running Mac sits
# below 15 C, so readings under that are treated as absent.
MAC_FLOOR = 15.0


SMC_EXACT = {
    "TC0P": "CPU proximity", "TC0D": "CPU die", "TC0E": "CPU die virtual",
    "TC0F": "CPU die filtered", "TC0H": "CPU heatsink", "TCXC": "PECI CPU",
    "TG0P": "GPU proximity", "TG0D": "GPU die", "TG0H": "GPU heatsink",
    "TM0P": "memory proximity", "TM0S": "memory slot",
    "TW0P": "Wi-Fi proximity", "TW0p": "Wi-Fi proximity",
    "TH0a": "NAND SSD a", "TH0b": "NAND SSD b", "TH0c": "NAND SSD c",
    "TH0x": "NAND controller", "TH0A": "NAND SSD A", "TH0B": "NAND SSD B",
    "TA0P": "ambient airflow", "TaLP": "airflow left", "TaRP": "airflow right",
    "TaSP": "airflow rear", "TPCD": "PCH die", "TN0P": "PCH proximity",
    "TB1T": "battery sensor 1", "TB2T": "battery sensor 2",
    "TI0P": "Thunderbolt proximity", "TI1P": "Thunderbolt proximity 2",
    "TL0P": "LCD proximity",
}


def smc_classify(key, table):
    if key.startswith("TB") and key.endswith("T"):
        return "battery", SMC_EXACT.get(key, "battery")
    group, desc = table.get(key[:2], ("other", "unmapped key"))
    if key in SMC_EXACT:
        desc = SMC_EXACT[key]
    return group, desc


class SMCSource(Source):
    def __init__(self, smc, entries, park=False, thermal_fn=None):
        super().__init__("smc", [e[0] for e in entries])
        self.smc = smc
        self.entries = entries      # (sensor, key, size, type)
        self.park = park
        self.thermal_fn = thermal_fn
        self.prev = {}
        if park:
            self.read()             # seed, so even the first sample can spot a parked key

    def read(self):
        out = {}
        for sensor, key, size, typ in self.entries:
            raw = self.smc.read(key, size)
            v = smc_decode(typ, raw)
            if v is None or not math.isfinite(v):
                sensor.parked_at = None
                continue
            if self.park and key[:2] in SMC_PARKABLE:
                last = self.prev.get(key)
                self.prev[key] = raw
                if raw == last and v == int(v):
                    sensor.parked_at = float(v)
                    continue
            sensor.parked_at = None
            out[sensor.sid] = float(v)
        tstate = self.thermal_fn() if self.thermal_fn is not None else getattr(self.smc, "thermal_state", None)
        badge = MAC_THERMAL_STATES.get(tstate)
        self.throttles = [badge] if badge else []
        return out

    def keep(self, sids):
        super().keep(sids)
        self.entries = [e for e in self.entries if e[0].sid in sids]


def smc_source(smc, arch):
    table = SMC_ARM if arch == "arm64" else SMC_INTEL
    entries = []
    for key in smc.all_keys():
        if len(key) != 4 or key[0] != "T":
            continue
        if arch == "arm64" and SMC_ARM_CONSTANT.match(key):
            continue
        info = smc.key_info(key)
        if not info or not smc_is_temp(info[1], info[0]):
            continue
        group, desc = smc_classify(key, table)
        sensor = Sensor("smc/" + key, key, group, "smc", ("SMC %s %s" % (key, desc)).rstrip(),
                        "temp", lo=MAC_FLOOR, defaults=MAC_LIMITS.get(group))
        entries.append((sensor, key, info[0], info[1]))
    raw = smc.read("FNum", 1)
    for i in range(raw[0] if raw else 0):
        key = "F%dAc" % i
        info = smc.key_info(key)
        if not info:
            continue
        mn_info = smc.key_info("F%dMn" % i)
        mx_info = smc.key_info("F%dMx" % i)
        mn = sane_fan_limit(smc_decode(mn_info[1], smc.read("F%dMn" % i, mn_info[0]))) if mn_info else None
        mx = sane_fan_limit(smc_decode(mx_info[1], smc.read("F%dMx" % i, mx_info[0]))) if mx_info else None
        if mn is not None and mx is not None and mn >= mx:
            mn = None
        if mn is not None and mx is not None:
            detail = "SMC %s %d..%d rpm" % (key, mn, mx)
        elif mx is not None:
            detail = "SMC %s max %d rpm" % (key, mx)
        else:
            detail = "SMC %s" % key
        entries.append((Sensor("smc/" + key, "Fan %d" % i, "fan", "smc", detail, "rpm",
                               rmin=mn, rmax=mx),
                        key, info[0], info[1]))
    for key in smc.all_keys():
        if not smc_is_power_key(key):
            continue
        info = smc.key_info(key)
        if not info or not smc_is_power(info[1], info[0]):
            continue
        desc = SMC_POWER.get(key) or SMC_POWER_PREFIX.get(key[:2], "power")
        sensor = Sensor("smc/" + key, key, "power", "smc", ("SMC %s %s" % (key, desc)).rstrip(),
                        "watt", lo=0.05, hi=2000.0)
        entries.append((sensor, key, info[0], info[1]))
    tfn = mac_thermal_state if isinstance(smc, SMC) else None
    return SMCSource(smc, entries, park=arch == "arm64", thermal_fn=tfn) if entries else None


class HIDTemps:
    """IOHID temperature services (Apple silicon): the PMU die sensors, NAND, battery gauge.
    IOHIDEventSystemClient is private API, but stable and used by every monitor on AS."""

    def __init__(self):
        k = self.k = _IOKit.get()
        c, vp = ctypes, ctypes.c_void_p
        io = k.io
        try:
            create = io.IOHIDEventSystemClientCreate
            set_matching = io.IOHIDEventSystemClientSetMatching
            copy_services = io.IOHIDEventSystemClientCopyServices
            self._prop = io.IOHIDServiceClientCopyProperty
            self._event = io.IOHIDServiceClientCopyEvent
            self._float = io.IOHIDEventGetFloatValue
        except AttributeError as e:
            raise OSError("IOHID event system not available: %s" % e)
        create.restype, create.argtypes = vp, [vp]
        set_matching.restype, set_matching.argtypes = c.c_int, [vp, vp]
        copy_services.restype, copy_services.argtypes = vp, [vp]
        self._prop.restype, self._prop.argtypes = vp, [vp, vp]
        self._event.restype, self._event.argtypes = vp, [vp, c.c_int64, c.c_int32, c.c_int64]
        self._float.restype, self._float.argtypes = c.c_double, [vp, c.c_int32]
        page, usage = c.c_int32(0xFF00), c.c_int32(5)     # vendor page, temperature sensor
        keys = (vp * 2)(k.cfstr("PrimaryUsagePage"), k.cfstr("PrimaryUsage"))
        vals = (vp * 2)(k.CFNumberCreate(None, 3, c.byref(page)), k.CFNumberCreate(None, 3, c.byref(usage)))
        matching = k.CFDictionaryCreate(None, keys, vals, 2, k.keyCallBacks, k.valueCallBacks)
        self.client = create(None)
        if not self.client:
            raise OSError("IOHIDEventSystemClientCreate failed")
        set_matching(self.client, matching)
        self.services = copy_services(self.client)     # kept alive: holds the service refs
        count = k.CFArrayGetCount(self.services) if self.services else 0
        if not count:
            raise OSError("no IOHID temperature services")
        product = k.cfstr("Product")
        self.refs, self._names = [], []
        for i in range(count):
            ref = k.CFArrayGetValueAtIndex(self.services, i)
            p = self._prop(ref, product)
            self._names.append(k.pystr(p) or "sensor %d" % i)
            if p:
                k.CFRelease(p)
            self.refs.append(ref)

    def names(self):
        return list(self._names)

    def read_all(self, indices=None):
        out = [None] * len(self.refs)
        for i in (range(len(self.refs)) if indices is None else indices):
            ev = self._event(self.refs[i], 15, 0, 0)          # kIOHIDEventTypeTemperature
            if ev:
                out[i] = self._float(ev, 15 << 16)
                self.k.CFRelease(ev)
        return out


class FakeHID:
    def __init__(self, rows):
        self.rows = rows

    def names(self):
        return [r[0] for r in self.rows]

    def read_all(self, indices=None):
        return [r[1] for r in self.rows]


HID_SKIP = {"PMU tcal"}     # a calibration constant (51.85 on every Mac), not a temperature


def hid_classify(name):
    n = name.lower()
    if "battery" in n or "gas gauge" in n:
        return "battery", "battery gas gauge"
    if "nand" in n or "ssd" in n:
        return "storage", "NAND"
    if "tdie" in n:
        return "soc", "PMU die sensor"
    if "tdev" in n:
        return "soc", "PMU device sensor"
    if "gpu" in n:
        return "gpu", "GPU"
    if "pacc" in n or "eacc" in n or "cpu" in n:
        return "cpu", "CPU cluster"
    if "soc" in n or "pmgr" in n or "ane" in n or "isp" in n:
        return "soc", "SoC"
    return "other", "HID sensor"


class HIDSource(Source):
    def __init__(self, hid, entries):
        super().__init__("iohid", [e[0] for e in entries], every=2.0)
        self.hid = hid
        self.entries = entries      # (sensor, index)

    def read(self):
        vals = self.hid.read_all([i for _, i in self.entries])
        return {s.sid: float(vals[i]) for s, i in self.entries if vals[i] is not None}

    def keep(self, sids):
        super().keep(sids)
        self.entries = [e for e in self.entries if e[0].sid in sids]


def hid_source(hid):
    names = hid.names()
    counts = collections.Counter(names)
    seen = collections.Counter()
    entries = []
    for i, name in enumerate(names):
        if name in HID_SKIP:
            continue
        seen[name] += 1
        label = name if counts[name] == 1 else "%s #%d" % (name, seen[name])
        group, desc = hid_classify(name)
        entries.append((Sensor("hid/" + label, label, group, "iohid", "IOHID " + desc, "temp",
                               lo=MAC_FLOOR, defaults=MAC_LIMITS.get(group)), i))
    return HIDSource(hid, entries) if entries else None


def mac_sources(smc="auto", hid="auto", arch=None):
    arch = arch or platform.machine()
    sources, notes = [], []
    if smc == "auto":
        try:
            smc = SMC()
        except OSError as e:
            smc, _ = None, notes.append("SMC: %s" % e)
    if smc is not None:
        src = smc_source(smc, arch)
        if src is not None:
            sources.append(src)
    if hid == "auto":
        hid = None
        if arch == "arm64":
            try:
                hid = HIDTemps()
            except OSError as e:
                notes.append("IOHID: %s" % e)
    if hid is not None:
        src = hid_source(hid)
        if src is not None:
            sources.append(src)
    return dedupe_labels(sources), notes


# -------------------------------------------------------------------------- machine info ---

def dmi_model(fs):
    """"LENOVO ThinkStation PX", "NVIDIA DGX Spark", "Gigabyte TRX50 AERO D"."""
    junk = ("to be filled", "system product name", "default string", "o.e.m", "not specified",
            "not applicable", "type1productconfigid", "none")

    def dmi(name):
        v = re.sub(r"\s+", " ", (fs.read("/sys/class/dmi/id/" + name) or "").replace("_", " ")).strip()
        return "" if not v or any(j in v.lower() for j in junk) else v

    vendor, product = dmi("sys_vendor"), dmi("product_name")
    if not product:
        vendor, product = dmi("board_vendor") or vendor, dmi("board_name")
    if vendor and product.lower().startswith(vendor.split()[0].lower()):
        vendor = ""
    return " ".join(p for p in (vendor, product) if p)


def machine_info(fs=None):
    info = {"host": socket.gethostname().split(".")[0], "arch": platform.machine(),
            "kernel": platform.release(), "os": "", "model": "", "cpu": ""}
    if sys.platform == "darwin":
        info["os"] = "macOS " + (platform.mac_ver()[0] or "?")
        info["model"] = sysctl_string("hw.model")
        info["cpu"] = sysctl_string("machdep.cpu.brand_string")
        return info
    fs = fs or SysFS()
    for line in (fs.read("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            info["os"] = line.split("=", 1)[1].strip().strip('"')
    info["model"] = dmi_model(fs)
    for line in (fs.read("/proc/cpuinfo") or "").splitlines():
        if line.lower().startswith("model name"):
            cpu = line.split(":", 1)[1]
            cpu = re.sub(r"\((R|TM|tm|r)\)", "", cpu)
            info["cpu"] = re.sub(r"\s+", " ", re.sub(r"\s*CPU\s*@.*$", "", cpu)).strip()
            break
    return info


# ----------------------------------------------------------------------- store & sampler ---

class Store:
    """Ring buffer of samples: one float32 column per sensor, plus max/avg per group."""

    def __init__(self, sensors, capacity, show_all=False):
        self.lock = threading.Lock()
        self.cap = max(2, int(capacity))
        self.sensors = list(sensors)
        self.by_id = {s.sid: s for s in self.sensors}
        self.show_all = show_all
        blank = array.array("f", [NAN]) * self.cap
        self.cols = {s.sid: array.array("f", blank) for s in self.sensors}
        self.group_keys = [g for g in GROUPS if any(s.group == g for s in self.sensors)]
        for g in self.group_keys:
            for m in ("max", "avg"):
                self.cols["@%s:%s" % (g, m)] = array.array("f", blank)
        self.times = array.array("d", [NAN]) * self.cap
        self.hot = [None] * self.cap
        self.n = 0
        self.ever = set()
        self.raw_last = {}
        self.lo, self.hi = {}, {}
        self.warn_hits, self.crit_hits, self.crit_at = {}, {}, {}

    def append(self, ts, values):
        with self.lock:
            i = self.n % self.cap
            self.times[i] = ts
            agg = {}
            hot_v, hot_sid = -1e9, None
            for s in self.sensors:
                v = values.get(s.sid)
                col = self.cols[s.sid]
                if v is None or not (s.lo < v < s.hi):
                    if v is not None and math.isfinite(v):
                        self.raw_last[s.sid] = float(v)
                    elif getattr(s, "parked_at", None) is not None:
                        self.raw_last.pop(s.sid, None)
                    col[i] = NAN
                    continue
                self.raw_last.pop(s.sid, None)
                col[i] = v
                self.ever.add(s.sid)
                if v < self.lo.get(s.sid, 1e18):
                    self.lo[s.sid] = v
                if v > self.hi.get(s.sid, -1e18):
                    self.hi[s.sid] = v
                a = agg.get(s.group)
                if a is None:
                    agg[s.group] = [v, v, 1]
                else:
                    if v > a[0]:
                        a[0] = v
                    a[1] += v
                    a[2] += 1
                if s.kind == "temp":
                    warn, crit = s.limits()
                    if warn is not None and v >= warn:
                        self.warn_hits[s.sid] = self.warn_hits.get(s.sid, 0) + 1
                    if crit is not None and v >= crit:
                        self.crit_hits[s.sid] = self.crit_hits.get(s.sid, 0) + 1
                        self.crit_at[s.sid] = ts
                    if v > hot_v:
                        hot_v, hot_sid = v, s.sid
            for g in self.group_keys:
                a = agg.get(g)
                self.cols["@%s:max" % g][i] = a[0] if a else NAN
                self.cols["@%s:avg" % g][i] = a[1] / a[2] if a else NAN
            self.hot[i] = hot_sid
            self.n += 1

    def window(self, key, count, end=None):
        """The `count` samples before sample number `end`, oldest first, NaN-padded."""
        count = max(0, int(count))
        col = self.cols.get(key)
        if col is None or count == 0:
            return [NAN] * count
        with self.lock:
            end = self.n if end is None else min(end, self.n)
            first = max(end - count, self.n - self.cap, 0)
            if end <= first:
                return [NAN] * count
            a, length = first % self.cap, end - first
            if a + length <= self.cap:
                seg = col[a:a + length]
            else:
                seg = col[a:] + col[:a + length - self.cap]
        return [NAN] * (count - length) + seg.tolist()

    def at(self, key, idx):
        col = self.cols.get(key)
        if col is None or idx < 0 or idx >= self.n or idx < self.n - self.cap:
            return NAN
        return col[idx % self.cap]

    def time_at(self, idx):
        if idx < 0 or idx >= self.n or idx < self.n - self.cap:
            return NAN
        return self.times[idx % self.cap]

    def hottest(self, idx):
        if idx < 0 or idx >= self.n or idx < self.n - self.cap:
            return None
        return self.hot[idx % self.cap]

    def visible(self):
        return [s for s in self.sensors if self.show_all or s.sid in self.ever]

    def reset_stats(self):
        with self.lock:
            self.lo.clear()
            self.hi.clear()
            self.warn_hits.clear()
            self.crit_hits.clear()
            self.crit_at.clear()


class Collector:
    def __init__(self, sources, store, interval, listeners=None):
        self.sources, self.store, self.interval = sources, store, interval
        self.listeners = list(listeners or [])
        self._stop = threading.Event()
        self.thread = None

    def sample(self, force=False):
        now = time.monotonic()
        values = {}
        for src in self.sources:
            values.update(src.poll(now, self.interval, force))
        ts = time.time()
        self.store.append(ts, values)
        for cb in self.listeners:
            try:
                cb(ts, values)
            except Exception:  # noqa: BLE001 - a full disk must not stop the display
                pass
        return values

    def start(self):
        self.thread = threading.Thread(target=self._run, name="sampler", daemon=True)
        self.thread.start()

    def _run(self):
        nxt = time.monotonic()
        while not self._stop.is_set():
            self.sample()
            nxt += self.interval
            delay = nxt - time.monotonic()
            if delay < 0:
                nxt, delay = time.monotonic(), 0.0
            self._stop.wait(delay)

    def stop(self):
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=3)


class Recorder:
    """Appends one CSV row per sample: time, epoch, then one column per sensor."""

    def __init__(self, path, sensors):
        self.sensors = list(sensors)
        header = ["time", "epoch"] + ["%s [%s]" % (s.label, s.sid) for s in self.sensors]
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        if exists:
            with open(path, newline="") as f:
                if next(csv.reader(f), None) != header:
                    raise SystemExit("thermalmaster: %s already holds different columns; "
                                     "record to a new file" % path)
        self.f = open(path, "a", newline="")
        self.w = csv.writer(self.f)
        if not exists:
            self.w.writerow(header)

    def __call__(self, ts, values):
        row = [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)), "%.3f" % ts]
        for s in self.sensors:
            v = values.get(s.sid)
            row.append("" if not s.valid(v) else ("%.2f" % v if s.kind in ("temp", "watt") else "%d" % round(v)))
        self.w.writerow(row)
        self.f.flush()

    def close(self):
        self.f.close()


# ------------------------------------------------------------------------------- render ---

class Line:
    """A row of (text, style) segments with a tracked display width."""

    __slots__ = ("segs", "width")

    def __init__(self, text="", style="n"):
        self.segs, self.width = [], 0
        if text:
            self.add(text, style)

    def add(self, text, style="n"):
        if text:
            if self.segs and self.segs[-1][1] == style:
                self.segs[-1] = (self.segs[-1][0] + text, style)
            else:
                self.segs.append((text, style))
            self.width += dwidth(text)
        return self

    def extend(self, other):
        for text, style in other.segs:
            self.add(text, style)
        return self

    def pad(self, width, style="n"):
        if self.width < width:
            self.add(" " * (width - self.width), style)
        return self

    def fit(self, width):
        if self.width > width:
            segs, self.segs, self.width = self.segs, [], 0
            for text, style in segs:
                room = width - self.width
                if room <= 0:
                    break
                self.add(clip(text, room), style)
        return self

    def text(self):
        return "".join(t for t, _ in self.segs)


def level(v, warn, crit):
    if isnan(v):
        return "dim"
    if crit is not None and v >= crit:
        return "crit"
    if warn is not None and v >= warn:
        return "warn"
    if warn is not None and v < warn - 25:
        return "cool"
    return "ok"


_DOT = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))    # [row][column]
_BAR = ((0x40, 0x80), (0x04, 0x20), (0x02, 0x10), (0x01, 0x08))    # bottom-up


def resample(samples, npx, how="max"):
    """Spread samples (oldest first, NaN = missing) over npx pixel columns."""
    n = len(samples)
    if npx <= 0:
        return []
    if n == 0:
        return [NAN] * npx
    out = []
    for j in range(npx):
        a = j * n // npx
        b = max(a + 1, (j + 1) * n // npx)
        seg = [v for v in samples[a:b] if v == v]
        if not seg:
            out.append(NAN)
        elif how == "max":
            out.append(max(seg))
        else:
            out.append(sum(seg) / len(seg))
    return out


def y_range(values, min_span, floor=None):
    """Axis limits: the data range padded, at least `min_span` wide, never below `floor`."""
    good = [v for v in values if v == v]
    if not good:
        return 0.0, 1.0
    lo, hi = min(good), max(good)
    if hi - lo < min_span:
        mid = (hi + lo) / 2.0
        lo, hi = mid - min_span / 2.0, mid + min_span / 2.0
    pad = (hi - lo) * 0.06
    lo, hi = float(math.floor(lo - pad)), float(math.ceil(hi + pad))
    if floor is not None and lo < floor:
        lo, hi = floor, max(hi + (floor - lo), floor + min_span)
    return lo, hi


def braille_chart(px, cols, rows, lo, hi, style_of):
    """A connected line through px (2 per cell, NaN = gap); each cell coloured by its peak."""
    height = rows * 4
    span = (hi - lo) or 1.0
    grid = [bytearray(cols) for _ in range(rows)]
    peak = [NAN] * cols
    prev = None
    for x, v in enumerate(px[: cols * 2]):
        if v != v:
            prev = None
            continue
        y = height - 1 - min(height - 1, max(0, int(round((v - lo) / span * (height - 1)))))
        a, b = (y, y) if prev is None else (min(prev, y), max(prev, y))
        cx, sub = x >> 1, x & 1
        for yy in range(a, b + 1):
            grid[yy >> 2][cx] |= _DOT[yy & 3][sub]
        prev = y
        if not peak[cx] >= v:
            peak[cx] = v
    out = []
    for r in range(rows):
        line = Line()
        for c in range(cols):
            bits = grid[r][c]
            if bits:
                line.add(chr(0x2800 + bits), style_of(peak[c]))
            else:
                line.add(" ")
        out.append(line)
    return out


def braille_spark(px, cols, lo, hi, style_of):
    """One-row bar sparkline: 2 samples per cell, 4 levels each."""
    span = (hi - lo) or 1.0
    line = Line()
    for c in range(cols):
        bits, peak = 0, NAN
        for sub in (0, 1):
            i = c * 2 + sub
            v = px[i] if i < len(px) else NAN
            if v != v:
                continue
            h = max(1, min(4, 1 + int(round((v - lo) / span * 3))))
            for k in range(h):
                bits |= _BAR[k][sub]
            if not peak >= v:
                peak = v
        if bits:
            line.add(chr(0x2800 + bits), style_of(peak))
        else:
            line.add(" ")
    return line


class View:
    """Everything the user can change from the keyboard."""

    def __init__(self, glyphs, interval, window, history, fahrenheit=False, bell=False):
        self.glyphs, self.interval, self.history = glyphs, interval, history
        self.window, self.fahrenheit = window, fahrenheit
        self.metric = "max"
        self.mode = "overview"      # overview | group | all
        self.panel = 0
        self.group = None
        self.sel = None             # selected sensor id in list views
        self.top = 0
        self.sort = "value"
        self.fleet_sort = "config"
        self.frozen = None          # {id(store): sample number} while paused
        self.host_sel = 0           # selected machine in the fleet overview
        self.help = False
        self.grid_cols = 1
        self.panels = []
        self.filter_text = ""
        self.filter_editing = False
        self.hot_only = False
        self.pinned = set()
        self.bell = bool(bell)
        self.bell_seen = set()
        self.list_y0 = None
        self.list_rows = 0
        self.grid_geom = None
        self.message = ""
        self.message_t = 0.0

    def say(self, text):
        self.message, self.message_t = text, time.time()

    def samples(self):
        return max(2, int(round(self.window / self.interval)))

    def unit(self, kind):
        if kind == "rpm":
            return " rpm"
        if kind == "pct":
            return "%"
        if kind == "watt":
            return " W"
        return self.glyphs.deg + ("F" if self.fahrenheit else "C")

    def conv(self, v, kind="temp"):
        if kind == "temp" and self.fahrenheit and v == v:
            return v * 9.0 / 5.0 + 32.0
        return v

    def conv_delta(self, dv, kind="temp"):
        if kind == "temp" and self.fahrenheit and dv == dv:
            return dv * 9.0 / 5.0
        return dv

    def fmt(self, v, kind="temp", unit=True, digits=1):
        if isnan(v):
            return "-"
        v = self.conv(v, kind)
        is_flt = kind in ("temp", "watt") and digits > 0
        text = ("%.*f" % (digits, v)) if is_flt else "%d" % round(v)
        return text + (self.unit(kind) if unit else "")

    def fmt_delta(self, dv, kind="temp", unit=True, signed=False, digits=1):
        if isnan(dv):
            return "-"
        dv = self.conv_delta(dv, kind)
        is_flt = kind in ("temp", "watt") and digits > 0
        step = 10.0 ** (-digits) if is_flt else 1.0
        if abs(dv) < 0.5 * step:
            dv = 0.0
        pat = ("%+.*f" if signed else "%.*f") if is_flt else ("%+d" if signed else "%d")
        text = (pat % (digits, dv)) if is_flt else (pat % round(dv))
        return text + (self.unit(kind) if unit else "")


def view_end(view, store):
    """Sample number the display ends at: the newest one, or where `p` froze this store."""
    if store is None or store.n <= 0:
        return 0
    if view.frozen is None:
        return store.n
    end = view.frozen.get(id(store), store.n)
    return max(max(1, store.n - store.cap + 1), min(store.n, end))


def hidden_reason(store, s):
    """Why sensor `s` has no valid reading in `store.ever` (for `--all` diagnostics)."""
    if store is None or s.sid in store.ever:
        return ""
    pv = getattr(s, "parked_at", None)
    if pv is not None:
        r = round(pv, 1)
        if not r:
            return "unconnected (0 C)"
        if 0.0 < r <= s.lo:
            return "below %d C floor (%g C)" % (round(s.lo), r)
        if r < 0.0:
            return "placeholder (%g C)" % r
        if r >= s.hi:
            return "out of range (%g C)" % r
        return "parked (%d C)" % round(pv)
    rv = getattr(store, "raw_last", {}).get(s.sid)
    if rv is not None:
        r = round(rv, 2 if s.kind == "watt" else 1)
        if not r:
            r = 0.0
        if s.kind == "watt":
            return "inactive (%g W)" % r if r <= s.lo else "out of range (%g W)" % r
        if s.kind == "temp":
            if r == 0.0:
                return "unconnected (0 C)"
            if 0.0 < r <= s.lo:
                return "below %d C floor (%g C)" % (round(s.lo), r)
            if r < 0.0:
                return "placeholder (%g C)" % r
            return "out of range (%g C)" % r
        if r == 0.0:
            return "inactive (0)"
        return "out of range (%g)" % r
    return "no reading"


def sensor_matches(sensor, pattern, source_name=None, extra=""):
    """Glob or substring match on id, label, group, source or detail. Smart case: a pattern
    with capitals is case-sensitive, because SMC keys are ("Tp" is a CPU cluster, "TP" not)."""
    if not pattern:
        return True
    fold = pattern == pattern.lower()
    fields = (sensor.sid, sensor.label, sensor.group, GROUPS[sensor.group][0],
              source_name or sensor.source, sensor.detail) + ((extra,) if extra else ())
    for f in fields:
        if fold:
            f = f.lower()
        if fnmatch.fnmatchcase(f, pattern) or (not any(c in pattern for c in "*?[") and pattern in f):
            return True
    return False


def is_hot_sensor(sensor, v):
    """True if `v` is near or above its warning/critical limit (for `w` hot-only filter)."""
    if v != v or not sensor.valid(v):
        return False
    if sensor.kind == "temp":
        warn, crit = sensor.limits()
        if warn is not None and v >= warn - 10.0:
            return True
        if crit is not None and v >= crit - 15.0:
            return True
        return False
    pct = sensor.fan_pct(v)
    return pct == pct and pct >= 75.0


def view_visible(store, view=None, end=None):
    """Sensors visible in `store` after applying `view.filter_text` and `view.hot_only`."""
    vis = store.visible()
    if view is None or (not view.filter_text and not view.hot_only):
        return vis
    idx = (store.n if end is None else end) - 1
    out = []
    for s in vis:
        if view.filter_text and not sensor_matches(s, view.filter_text, extra=hidden_reason(store, s)):
            continue
        if view.hot_only and not is_hot_sensor(s, store.at(s.sid, idx)):
            continue
        out.append(s)
    return out


def group_title(g):
    return "Pinned" if g == "pinned" else GROUPS[g][0]


def window_stats(store, sid, count, end):
    """Return (wmin, wavg, wmax, delta) over the current window [end-count..end)."""
    if store is None or end <= 0:
        return NAN, NAN, NAN, NAN
    good = [u for u in store.window(sid, count, end) if u == u]
    if not good:
        return NAN, NAN, NAN, NAN
    wmin, wmax = min(good), max(good)
    wavg = sum(good) / float(len(good))
    delta = (good[-1] - good[0]) if len(good) >= 2 else 0.0
    return wmin, wavg, wmax, delta


def min_span(kind, view):
    if kind == "rpm":
        return 200.0
    if kind in ("pct", "watt"):
        return 10.0
    return 7.0 if view.fahrenheit else 4.0


def group_members(store, g, view=None, end=None):
    vis = view_visible(store, view, end)
    if g == "pinned":
        return [s for s in vis if view is not None and s.sid in view.pinned]
    return [s for s in vis if s.group == g]


def group_limits(members):
    warns = [s.limits()[0] for s in members if s.limits()[0] is not None]
    crits = [s.limits()[1] for s in members if s.limits()[1] is not None]
    return (min(warns) if warns else None), (min(crits) if crits else None)


def group_kind(members):
    return members[0].kind if members else "temp"


def member_series(store, members, metric, count, end):
    """Compute max/avg series and (max_now, avg_now) directly from a subset of `members`."""
    if not members:
        return [NAN] * count, NAN, NAN
    wins = [store.window(s.sid, count, end) for s in members]
    series = []
    for k in range(count):
        good = [w[k] for w in wins if w[k] == w[k]]
        if not good:
            series.append(NAN)
        elif metric == "max":
            series.append(max(good))
        else:
            series.append(sum(good) / float(len(good)))
    now_good = [v for v in (store.at(s.sid, end - 1) for s in members) if v == v]
    if not now_good:
        return series, NAN, NAN
    return series, max(now_good), sum(now_good) / float(len(now_good))


class PanelSpec:
    def __init__(self, title, subtitle, series, kind, warn, crit, value, info, how="max",
                 xaxis=False, value_style=None, alt=""):
        self.title, self.subtitle, self.series, self.kind = title, subtitle, series, kind
        self.warn, self.crit, self.value, self.info = warn, crit, value, info
        self.how, self.xaxis, self.value_style, self.alt = how, xaxis, value_style, alt


def xaxis_text(cols, window):
    buf = [" "] * cols
    marks = [(0, "-" + fmt_duration(window))]
    if cols >= 40:
        marks.append((cols // 2 - 2, "-" + fmt_duration(window / 2.0)))
    marks.append((cols - 3, "now"))
    for pos, text in marks:
        pos = max(0, min(pos, cols - len(text)))
        buf[pos:pos + len(text)] = list(text)
    return "".join(buf[:cols])


def render_panel(spec, pw, ph, view, selected=False):
    """A bordered chart box exactly pw x ph cells."""
    G = view.glyphs
    border = "bsel" if selected else "border"
    iw = pw - 2
    lines = []
    value = view.fmt(spec.value, spec.kind)
    vstyle = spec.value_style or (level(spec.value, spec.warn, spec.crit)
                                  if spec.kind == "temp" else "fan")
    title = " %s " % spec.title
    sub = ("%s " % spec.subtitle) if spec.subtitle else ""
    vtext = " %s " % value
    room = pw - 4 - dwidth(vtext)
    if dwidth(title) + dwidth(sub) > room:
        sub = ""
    title = ellipsize(title, max(0, room), G.ell)
    top = Line(G.tl + G.h, border).add(title, "bsel" if selected else "title").add(sub, "dim")
    top.add(G.h * max(0, pw - 2 - top.width - dwidth(vtext)), border)
    top.add(vtext, vstyle).add(G.h + G.tr, border)
    lines.append(top.fit(pw).pad(pw))

    has_info = ph >= 6 and spec.info is not None
    has_x = spec.xaxis and ph >= 9
    rows = ph - 2 - (1 if has_info else 0) - (1 if has_x else 0)
    if rows >= 1:
        vals = [view.conv(v, spec.kind) for v in spec.series]
        lo, hi = y_range(vals, min_span(spec.kind, view), 0.0 if spec.kind != "temp" else None)
        labels = {0: "%d" % hi, rows - 1: "%d" % lo}
        if rows >= 5:
            labels[rows // 2] = "%d" % round((lo + hi) / 2.0)
        lab_w = max(dwidth(t) for t in labels.values())
        cols = max(1, iw - lab_w - 1)
        warn, crit = view.conv(spec.warn) if spec.warn is not None else None, \
            view.conv(spec.crit) if spec.crit is not None else None
        if spec.kind == "temp":
            def style_of(v, w=warn, c=crit):
                return level(v, w, c)
        else:
            def style_of(v):
                return "fan"
        chart = braille_chart(resample(vals, cols * 2, spec.how), cols, rows, lo, hi, style_of)
        for r in range(rows):
            line = Line(G.v, border)
            if r in labels:
                line.add(rjust(labels[r], lab_w) + G.tick, "axis")
            else:
                line.add(" " * lab_w + G.v, "axis")
            line.extend(chart[r])
            lines.append(line.fit(pw - 1).pad(pw - 1).add(G.v, border))
        if has_x:
            line = Line(G.v, border).add(" " * (lab_w + 1) + xaxis_text(cols, view.window), "axis")
            lines.append(line.fit(pw - 1).pad(pw - 1).add(G.v, border))
    if has_info:
        info = spec.info if isinstance(spec.info, Line) else Line(spec.info, "dim")
        line = Line(G.v, border).add(" ").extend(info)
        lines.append(line.fit(pw - 1).pad(pw - 1).add(G.v, border))
    lines.append(Line(G.bl + G.h * max(0, pw - 2) + G.br, border).fit(pw))
    return lines[:ph]


def app_throttles(app):
    """Active [(badge_text, style)] hardware throttling flags for the viewed machine."""
    if getattr(app, "host", None) is not None:
        return list(getattr(app.host, "throttles", ()))
    out = []
    for src in getattr(app, "sources", ()) or ():
        for item in getattr(src, "throttles", ()) or ():
            if item not in out:
                out.append(item)
    return out


def group_throttles(g, throttles):
    if g == "gpu":
        return [t for t in throttles if t[0].startswith("GPU")]
    if g in ("cpu", "soc"):
        return [t for t in throttles if t[0].startswith(("CPU", "THERMAL"))]
    return []


def sensor_throttles(s, throttles):
    m = re.match(r"^GPU(\d+)\b", s.label)
    if m:
        return [t for t in throttles if t[0].startswith("GPU%s " % m.group(1))]
    if s.group == "power":
        return [t for t in throttles if t[0].startswith(("CPU", "THERMAL"))]
    return group_throttles(s.group, throttles)


def overview_specs(store, view, end, throttles=()):
    specs = []
    count = view.samples()
    all_vis = store.visible()
    keys = (["pinned"] if view.pinned else []) + list(store.group_keys)
    for g in keys:
        members = group_members(store, g, view, end)
        if not members:
            continue
        if g == "pinned":
            temps = [s for s in members if s.kind == "temp"]
            chart_members = temps if temps else [s for s in members if s.kind == members[0].kind]
        else:
            chart_members = members
        kind = group_kind(chart_members)
        warn, crit = group_limits(chart_members) if kind == "temp" else (None, None)
        unfiltered_n = sum(1 for s in all_vis if s.group == g) if g != "pinned" else -1
        if len(chart_members) == unfiltered_n:
            series = store.window("@%s:%s" % (g, view.metric), count, end)
            value = store.at("@%s:%s" % (g, view.metric), end - 1)
            other = "avg" if view.metric == "max" else "max"
            ov = store.at("@%s:%s" % (g, other), end - 1)
        else:
            series, mx_now, av_now = member_series(store, chart_members, view.metric, count, end)
            value = mx_now if view.metric == "max" else av_now
            other = "avg" if view.metric == "max" else "max"
            ov = av_now if view.metric == "max" else mx_now
        info = Line()
        alt = ""
        if kind == "temp":
            best, best_v = None, -1e9
            for s in chart_members:
                v = store.at(s.sid, end - 1)
                if v == v and v > best_v:
                    best, best_v = s, v
            if best is not None:
                info.add("hottest ", "dim").add(best.label + " ", "n")
                info.add(view.fmt(best_v), level(best_v, *best.limits()))
                info.add("  %s %s" % (other, view.fmt(ov)), "dim")
                alt = "%s %s" % (other, view.fmt(ov))
            for ttext, tstyle in group_throttles(g, throttles):
                info.add("  " + ttext, tstyle)
        else:
            for s in members:
                fv = store.at(s.sid, end - 1)
                ftext = view.fmt(fv, s.kind)
                if s.kind in ("rpm", "watt") and fv == fv and s.valid(fv):
                    if s.kind == "rpm" and fv == 0.0 and s.rmin:
                        ftext += " (idle)"
                    else:
                        pct = s.fan_pct(fv)
                        if pct == pct:
                            ftext += " (%d%%)" % round(pct)
                info.add("%s " % s.label, "dim").add(ftext + "  ", "fan")
        title = group_title(g) + ("" if view.metric == "max" or kind != "temp" else " avg")
        specs.append((g, PanelSpec(title, "%d" % len(members), series, kind, warn, crit, value,
                                   info, "max" if view.metric == "max" else "avg", alt=alt)))
    return specs


def choose_grid(n, width, height, min_w=34, min_h=6):
    best = None
    for cols in range(1, n + 1):
        rows = -(-n // cols)
        pw, ph = width // cols, height // rows
        if pw < min_w or ph < min_h:
            continue
        score = abs(math.log((pw / float(ph)) / 4.0)) + 0.35 * (cols * rows - n)
        if best is None or score < best[0]:
            best = (score, cols, rows)
    return (best[1], best[2]) if best else None


def compose_header(width, left, right):
    """" thermalmaster " + left parts, right parts flush right; drops parts until it fits."""
    line = Line(" thermalmaster ", "hdr")
    left, right = list(left), list(right)
    while True:
        r = Line()
        for i, part in enumerate(right):
            if i:
                r.add("  ")
            r.extend(part)
        if line.width + sum(p.width for p in left) + r.width + 2 <= width:
            break
        if len(left) > 1:
            left.pop()
        elif len(right) > 1:
            right.pop(0)
        else:
            break
    for part in left:
        line.extend(part)
    line.pad(max(line.width, width - r.width - 1))
    return line.extend(r).fit(width)


def store_peak_alert(store, view):
    """Badge text when any sensor in `store` has breached its critical limit since last reset."""
    if store is None or not store.crit_hits:
        return ""
    sids = [sid for sid in store.crit_hits if sid in store.by_id]
    if not sids:
        return ""
    sid = max(sids, key=lambda k: (store.hi.get(k, -1e9), store.crit_hits[k]))
    s = store.by_id[sid]
    hi_v = store.hi.get(sid, NAN)
    more = " (+%d)" % (len(sids) - 1) if len(sids) > 1 else ""
    return "PEAK! %s %s%s" % (s.label, view.fmt(hi_v), more)


def check_bell(app, beep_fn=None):
    """If `view.bell` is enabled and any sensor has newly breached `crit`, trigger `beep_fn` once."""
    view = app.view
    if not view.bell:
        return False
    rang = False
    for st in app.stores():
        if st is None:
            continue
        for sid in list(st.crit_hits):
            key = (id(st), sid)
            if key not in view.bell_seen:
                view.bell_seen.add(key)
                rang = True
    if rang and beep_fn is not None:
        try:
            beep_fn()
        except Exception:  # noqa: BLE001
            pass
    return rang


def header_tail(view, store=None, end=None):
    tail = [Line("every %s  window %s" % (fmt_duration(view.interval), fmt_duration(view.window)), "dim")]
    if view.bell:
        tail.append(Line("BELL", "bsel"))
    clock = time.strftime("%H:%M:%S")
    if view.frozen is not None:
        badge = "PAUSED"
        if store is not None and end is not None and 0 < end <= store.n:
            t_end = store.time_at(end - 1)
            if t_end == t_end and t_end > 946684800.0:
                clock = time.strftime("%H:%M:%S", time.localtime(t_end))
            if end < store.n:
                t_now = store.time_at(store.n - 1)
                if t_end == t_end and t_now == t_now and t_now >= t_end:
                    ago = max(1.0, round(t_now - t_end))
                else:
                    ago = max(1.0, round((store.n - end) * view.interval))
                badge = "PAUSED -%s" % fmt_duration(ago)
        tail.append(Line(badge, "warn"))
    tail.append(Line(clock, "dim"))
    return tail


def render_header(app, width, end):
    view, store = app.view, app.store
    info = app.info
    left = []
    if app.hosts is not None:
        left.append(Line(" fleet >", "dim"))
    left.append(Line(" " + info.get("host", "?"), "b"))
    desc = " ".join(p for p in (info.get("cpu") or info.get("model"), info.get("os")) if p)
    if desc:
        left.append(Line("  " + desc, "dim"))
    right = []
    if app.host is not None:
        word, style = link_status(app.host)
        if word != "up":
            right.append(Line(word, style))
    for ttext, tstyle in app_throttles(app):
        right.append(Line(ttext, tstyle))
    peak = store_peak_alert(store, view)
    if peak:
        right.append(Line(peak, "crit"))
    if view.filter_text:
        right.append(Line("/" + view.filter_text, "bsel"))
    if view.hot_only:
        right.append(Line("HOT", "warn"))
    all_vis = store.visible()
    n_pin = sum(1 for s in all_vis if s.sid in view.pinned)
    if n_pin:
        right.append(Line("pin:%d" % n_pin, "bsel"))
    sid = store.hottest(end - 1)
    if sid:
        s = store.by_id[sid]
        v = store.at(sid, end - 1)
        right.append(Line("hottest ", "dim").add(s.label + " ", "n").add(view.fmt(v), level(v, *s.limits())))
    shown_vis = view_visible(store, view, end)
    if len(shown_vis) != len(all_vis):
        right.append(Line("%d/%s" % (len(shown_vis), plural(len(all_vis), "sensor")), "dim"))
    else:
        right.append(Line(plural(len(all_vis), "sensor"), "dim"))
    return compose_header(width, left, right + header_tail(view, store, end))


FOOTERS = {
    "fleet": ["arrows select", "enter open machine", "s sort", "m max/avg", "+/- window", "[/] scrub", "b bell", "u C/F", "p pause"],
    "overview": ["arrows select", "enter sensors", "/ filter", "w hot", "a all", "m max/avg", "+/- window", "[/] scrub", "b bell", "u C/F", "p pause"],
    "group": ["up/down select", "esc back", "/ filter", "w hot", "f pin", "s sort", "a all", "+/- window", "[/] scrub", "b bell", "u C/F", "p pause"],
    "all": ["up/down select", "esc back", "/ filter", "w hot", "f pin", "s sort", "+/- window", "[/] scrub", "b bell", "u C/F", "p pause"],
}


def render_footer(view, width, fleet=False):
    width -= 1      # curses cannot draw the bottom-right cell
    if view.filter_editing:
        return Line(" /" + view.filter_text + "_", "bsel").add("  (enter keep, esc clear)", "dim").fit(width)
    if view.message:
        return Line(" " + view.message, "warn").fit(width)
    tail = "  ? help  q quit"
    text = ""
    items = FOOTERS.get(view.mode, [])
    if fleet and view.mode == "overview":
        items = ["esc fleet"] + items
    for item in items:
        if dwidth(" " + text + "  " + item + tail) > width:
            break
        text = (text + "  " + item) if text else item
    return Line(" " + text + tail, "dim").fit(width)


def render_overview(app, width, height, end):
    view, store = app.view, app.store
    lines = [render_header(app, width, end)]
    body = height - 2
    specs = overview_specs(store, view, end, app_throttles(app))
    view.panels = [g for g, _ in specs]
    if view.panel >= len(specs):
        view.panel = max(0, len(specs) - 1)
    if not specs:
        view.grid_geom = None
        if (view.filter_text or view.hot_only) and store.visible():
            lines.append(Line(" no sensors match active filter (press / or w or esc to clear)", "warn").fit(width))
        else:
            lines.append(Line(" waiting for the first valid reading ...", "dim").fit(width))
    grid = choose_grid(len(specs), width, body) if specs else None
    if grid:
        cols, rows = grid
        view.grid_cols = cols
        gap = 1 if cols > 1 else 0
        pw, ph = (width - gap * (cols - 1)) // cols, body // rows
        view.grid_geom = ("grid", 1, cols, rows, pw, ph, gap, len(specs))
        for r in range(rows):
            row = specs[r * cols:(r + 1) * cols]
            boxes = [render_panel(spec, pw, ph, view, selected=(r * cols + i == view.panel))
                     for i, (g, spec) in enumerate(row)]
            for y in range(ph):
                line = Line()
                for i, box in enumerate(boxes):
                    if i:
                        line.add(" " * gap)
                    line.extend(box[y])
                lines.append(line)
    elif specs:
        view.grid_cols = 1
        k = max(1, min(4, body // max(1, len(specs))))
        view.grid_geom = ("rows", 1, k, min(len(specs), max(1, body // k)))
        lines.extend(render_group_rows(specs, view, width, body))
    while len(lines) < height - 1:
        lines.append(Line())
    lines = lines[:height - 1]
    lines.append(render_footer(view, width, fleet=app.hosts is not None))
    return lines


def render_group_rows(specs, view, width, height):
    """Overview for terminals too small for chart boxes: each group gets 1-4 rows of chart."""
    n = len(specs)
    k = max(1, min(4, height // max(1, n)))
    name_w = max(dwidth(group_title(g)) for g, _ in specs) + 1
    left_w = 2 + name_w + 11
    chart_w = max(4, width - left_w - 12)
    out = []
    for i, (g, spec) in enumerate(specs[:max(1, height // k)]):
        sel = i == view.panel
        vals = [view.conv(v, spec.kind) for v in spec.series]
        lo, hi = y_range(vals, min_span(spec.kind, view), 0.0 if spec.kind != "temp" else None)
        w_ = view.conv(spec.warn) if spec.warn is not None else None
        c_ = view.conv(spec.crit) if spec.crit is not None else None
        style_of = (lambda v: level(v, w_, c_)) if spec.kind == "temp" else (lambda v: "fan")
        px = resample(vals, chart_w * 2, spec.how)
        rows = [braille_spark(px, chart_w, lo, hi, style_of)] if k == 1 else \
            braille_chart(px, chart_w, k, lo, hi, style_of)
        vstyle = level(spec.value, spec.warn, spec.crit) if spec.kind == "temp" else "fan"
        for r in range(k):
            if r == 0:
                line = Line((view.glyphs.mark + " ") if sel else "  ", "bsel" if sel else "n")
                line.add(ljust(group_title(g), name_w), "bsel" if sel else "title")
                line.add(rjust(view.fmt(spec.value, spec.kind), 10) + " ", vstyle)
            elif r == 1 and spec.alt:
                line = Line("  " + rjust(spec.alt, name_w + 10) + " ", "dim")
            else:
                line = Line()
            line.pad(left_w).extend(rows[r])
            if r == 0:
                line.add("  %s" % spec.subtitle, "dim")
            elif r == k - 1:
                line.add("  %d-%d" % (lo, hi), "axis")
            out.append(line.fit(width))
    return out


SORT_MODES = ("value", "margin", "delta", "name")
KIND_RANK = {"temp": 0, "watt": 1, "pct": 2, "rpm": 3}


def sorted_sensors(store, view, sensors, end):
    mode = view.sort if view.sort in SORT_MODES else "value"
    pinned = view.pinned
    if mode == "name":
        return sorted(sensors, key=lambda s: (
            0 if s.sid in pinned else 1, list(GROUPS).index(s.group), natural_key(s.label)))
    if mode == "margin":
        def mkey(s):
            pin = 0 if s.sid in pinned else 1
            v = store.at(s.sid, end - 1)
            kr = KIND_RANK.get(s.kind, 4)
            if s.kind == "temp":
                m = s.margin(v)
                if m == m:
                    return (pin, 0, kr, m, -v if v == v else 0.0, natural_key(s.label))
            else:
                pct = s.fan_pct(v)
                if pct == pct:
                    return (pin, 0, kr, 100.0 - pct, -v if v == v else 0.0, natural_key(s.label))
            return (pin, 1, kr, 0.0, -v if v == v else 0.0, natural_key(s.label))
        return sorted(sensors, key=mkey)
    if mode == "delta":
        count = view.samples()

        def dkey(s):
            pin = 0 if s.sid in pinned else 1
            v = store.at(s.sid, end - 1)
            kr = KIND_RANK.get(s.kind, 4)
            _, _, _, dv = window_stats(store, s.sid, count, end)
            if dv == dv and v == v:
                return (pin, 0, kr, -dv, -v, natural_key(s.label))
            return (pin, 1, kr, 0.0, -v if v == v else 0.0, natural_key(s.label))
        return sorted(sensors, key=dkey)

    def key(s):
        pin = 0 if s.sid in pinned else 1
        v = store.at(s.sid, end - 1)
        kr = KIND_RANK.get(s.kind, 4)
        return (pin, 1, kr, 0.0, natural_key(s.label)) if v != v else (pin, 0, kr, -v, natural_key(s.label))
    return sorted(sensors, key=key)


def list_sensors(store, view, end=None):
    vis = view_visible(store, view, end)
    if view.mode == "group":
        if view.group == "pinned":
            return [s for s in vis if s.sid in view.pinned]
        return [s for s in vis if s.group == view.group]
    return vis


def render_list(app, width, height, end):
    view, store, G = app.view, app.store, app.view.glyphs
    lines = [render_header(app, width, end)]
    sensors = sorted_sensors(store, view, list_sensors(store, view, end), end)
    if not sensors:
        view.list_y0, view.list_rows = None, 0
        if view.mode == "group" and view.group == "pinned" and not any(s.sid in view.pinned for s in store.visible()):
            view.mode = "overview"
            return render_overview(app, width, height, end)
        if (view.filter_editing or view.filter_text or view.hot_only) and store.visible():
            lines.append(Line(" no sensors match active filter (press / or w or esc to clear)", "warn").fit(width))
            while len(lines) < height - 1:
                lines.append(Line())
            lines = lines[:height - 1]
            lines.append(render_footer(view, width))
            return lines
        view.mode = "overview"
        return render_overview(app, width, height, end)
    ids = [s.sid for s in sensors]
    if view.sel not in ids:
        view.sel = ids[0]
    idx = ids.index(view.sel)
    s = sensors[idx]
    count = view.samples()
    chart_h = max(6, min(18, (height - 4) * 2 // 5)) if height >= 20 else 0
    if chart_h:
        series = store.window(s.sid, count, end)
        wmin, wavg, wmax, wdelta = window_stats(store, s.sid, count, end)
        cur_v = store.at(s.sid, end - 1)
        info = Line()
        if wmin == wmin:
            info.add("window min %s  avg %s  max %s" % (
                view.fmt(wmin, s.kind), view.fmt(wavg, s.kind),
                view.fmt(wmax, s.kind)), "dim")
            info.add("  delta %s" % view.fmt_delta(wdelta, s.kind, signed=True), "dim")
        warn, crit = s.limits() if s.kind == "temp" else (None, None)
        if s.kind == "temp":
            m = s.margin(cur_v)
            if m == m:
                info.add("  headroom %s" % view.fmt_delta(m, "temp"), level(cur_v, warn, crit))
            if s.hw_limits():
                info.add("  limits %s / %s" % (view.fmt(warn), view.fmt(crit)), "dim")
        elif s.kind in ("rpm", "watt") and cur_v == cur_v and s.valid(cur_v):
            if s.kind == "rpm" and cur_v == 0.0 and s.rmin:
                info.add("  idle", "dim")
            else:
                pct = s.fan_pct(cur_v)
                if pct == pct:
                    info.add("  %d%% of max" % round(pct), "fan")
        for ttext, tstyle in sensor_throttles(s, app_throttles(app)):
            info.add("  " + ttext, tstyle)
        if store.crit_hits.get(s.sid):
            info.add("  [crit x%d]" % store.crit_hits[s.sid], "crit")
        elif store.warn_hits.get(s.sid):
            info.add("  [warn x%d]" % store.warn_hits[s.sid], "warn")
        if s.sid not in store.ever:
            reason = hidden_reason(store, s)
            if reason:
                info.add("  [%s]" % reason, "warn")
        info.add("  " + s.detail, "dim")
        where = GROUPS[s.group][0] if (view.mode == "all" or view.group == "pinned") else ""
        spec = PanelSpec(s.label, where, series, s.kind, warn, crit, cur_v, info,
                         "max", xaxis=True)
        lines.extend(render_panel(spec, width, chart_h, view, selected=True))
    title = "All sensors" if view.mode == "all" else group_title(view.group)
    lines.append(Line(" %s  %s  sorted by %s" % (title, plural(len(sensors), "sensor"), view.sort), "title").fit(width))
    label_w = min(30, max(12, max(dwidth(x.label) for x in sensors)))
    show_extra = width >= 110
    extra_col = "delta" if view.sort == "delta" else "headrm"
    head = Line("  " + ljust("sensor", label_w) + rjust("now", 10) + rjust("min", 8)
                + (rjust("avg", 8) if show_extra else "") + rjust("max", 8)
                + (rjust(extra_col, 8) if show_extra else "") + "  ", "dim")
    spark_w = max(8, min(60, (width - head.width) // 2))
    head.add(ljust("last " + fmt_duration(view.window), spark_w) + "  source", "dim")
    lines.append(head.fit(width))
    view.list_y0 = len(lines)
    rows = max(1, height - len(lines) - 1)
    if idx < view.top:
        view.top = idx
    if idx >= view.top + rows:
        view.top = idx - rows + 1
    view.top = max(0, min(view.top, max(0, len(sensors) - rows)))
    view.list_rows = min(rows, max(0, len(sensors) - view.top))
    for x in sensors[view.top:view.top + rows]:
        sel = x.sid == view.sel
        pin = x.sid in view.pinned
        mark = (G.mark + ("*" if pin else " ")) if sel else (" *" if pin else "  ")
        line = Line(mark, "bsel" if (sel or pin) else "n")
        line.add(ljust(ellipsize(x.label, label_w, G.ell), label_w), "bsel" if sel else "n")
        v = store.at(x.sid, end - 1)
        warn, crit = x.limits() if x.kind == "temp" else (None, None)
        _, xavg, _, xdelta = window_stats(store, x.sid, count, end) if show_extra else (NAN, NAN, NAN, NAN)
        line.add(rjust(view.fmt(v, x.kind), 10), level(v, warn, crit) if x.kind == "temp" else "fan")
        line.add(rjust(view.fmt(store.lo.get(x.sid, NAN), x.kind, unit=False), 8), "dim")
        if show_extra:
            line.add(rjust(view.fmt(xavg, x.kind, unit=False), 8), "dim")
        line.add(rjust(view.fmt(store.hi.get(x.sid, NAN), x.kind, unit=False), 8), "dim")
        if show_extra:
            if view.sort == "delta":
                ext_txt = view.fmt_delta(xdelta, x.kind, unit=False, signed=True)
                ext_style = "dim"
            elif x.kind == "temp":
                xm = x.margin(v)
                ext_txt = view.fmt_delta(xm, "temp", unit=False)
                ext_style = level(v, warn, crit) if xm == xm else "dim"
            elif x.kind == "rpm" and v == 0.0 and x.rmin:
                ext_txt, ext_style = "idle", "dim"
            else:
                pct = x.fan_pct(v)
                ext_txt = ("%d%%" % round(pct)) if pct == pct else "-"
                ext_style = "fan" if pct == pct else "dim"
            line.add(rjust(ext_txt, 8), ext_style)
        line.add("  ", "dim")
        vals = [view.conv(u, x.kind) for u in store.window(x.sid, count, end)]
        lo, hi = y_range(vals, min_span(x.kind, view), 0.0 if x.kind != "temp" else None)
        cw, cc = (view.conv(warn) if warn else None), (view.conv(crit) if crit else None)
        style_of = (lambda u, a=cw, b=cc: level(u, a, b)) if x.kind == "temp" else (lambda u: "fan")
        line.extend(braille_spark(resample(vals, spark_w * 2, "max"), spark_w, lo, hi, style_of))
        where = (GROUPS[x.group][0] + " " + G.dot + " ") if (view.mode == "all" or view.group == "pinned") else ""
        hnote = ("[%s] " % hidden_reason(store, x)) if x.sid not in store.ever else ""
        line.add("  " + where + hnote + x.detail, "dim")
        lines.append(line.fit(width))
    while len(lines) < height - 1:
        lines.append(Line())
    lines = lines[:height - 1]
    lines.append(render_footer(view, width))
    return lines


HELP = """\
thermalmaster {version}

overview
  arrows / hjkl     select a group (or click with the mouse)
  enter             list every sensor in the group, with its own chart
  a                 list all sensors
  m                 plot the group maximum or the group average

sensor lists
  up/down  pgup/pgdn  home/end   select a sensor (its curve is drawn on top)
  /                 live search / filter by name, id, group or source
  w                 show only hot / warning sensors
  f                 pin / unpin the selected sensor to the top
  s                 sort by current value, headroom (margin), trend (delta) or name
  esc / backspace   clear filter or back to the overview

fleet (--fleet)
  arrows / hjkl     select a machine
  enter             open it: the same boxes and lists as a local run
  s                 sort machines by config order, hottest or thermal headroom
  esc               back to the fleet from a machine's overview

everywhere
  + / -             shorter / longer time window (1m .. 6h, up to --history)
  [ / ]  {{ / }}      scrub backward / forward through history (pauses display)
  b                 toggle terminal bell alert on critical limit breaches
  u                 Celsius / Fahrenheit
  p / space         pause / resume live display (sampling continues)
  r                 reset the min / max and peak breach counters
  g                 box-drawing or ASCII borders
  ?                 this help        q  quit

colours: cyan cool, green normal, yellow at the warning limit, red at the critical
limit. Limits come from the hardware (hwmon max/crit, NVML slowdown, ACPI trip points)
when it reports them, otherwise from a per-group default.

{where}:
{sources}
"""


def source_rows(sources, view=None):
    rows = []
    for src in sources:
        cost = "%.1f ms" % (src.cost * 1000) if src.cost is not None else "-"
        every = fmt_duration(src.interval(view.interval if view else 1.0))
        rows.append("  %-28s %12s  %9s  every %s" % (src.name, plural(len(src.sensors), "sensor"), cost, every))
    return rows


def render_help(app, width, height):
    if app.hosts is not None and app.host is None:
        where, rows = "machines", fleet_rows(app)
    else:
        where = "sources on %s" % app.info.get("host", "this machine")
        rows = source_rows(app.sources, app.view)
    text = HELP.format(version=VERSION, where=where, sources="\n".join(rows))
    for note in app.notes:
        text += "  note: %s\n" % note
    lines = [Line(" " + t, "n").fit(width) for t in text.splitlines()]
    lines = lines[:height - 1]
    while len(lines) < height - 1:
        lines.append(Line())
    lines.append(Line(" press any key", "dim").fit(width))
    return lines


def render_screen(app, width, height):
    view = app.view
    if width < 30 or height < 8:
        return [Line("terminal too small (%dx%d)" % (width, height), "warn").fit(width)]
    if view.help:
        return render_help(app, width, height)
    if app.hosts is not None and app.host is None:
        return render_fleet(app, width, height)
    end = view_end(view, app.store)
    if app.store.n == 0:
        return [render_header(app, width, end), Line(" sampling ...", "dim").fit(width)]
    if view.mode in ("group", "all"):
        return render_list(app, width, height, end)
    return render_overview(app, width, height, end)


# --------------------------------------------------------------------------------- TUI ---

class App:
    """What the screens draw from. With --fleet, `hosts` holds one HostLink per machine and
    `host` is the machine being looked at (None: the fleet overview)."""

    def __init__(self, sources, store, collector, info, notes, view, hosts=None):
        self._sources, self._store, self.collector = sources, store, collector
        self._info, self._notes, self.view = info, notes, view
        self.hosts = hosts
        self.host = None

    store = property(lambda self: self.host.store if self.host is not None else self._store)
    info = property(lambda self: self.host.info if self.host is not None else self._info)
    notes = property(lambda self: self.host.notes if self.host is not None else self._notes)
    sources = property(lambda self: self.host.sources if self.host is not None else self._sources)

    def stores(self):
        if self.hosts is None:
            return [self._store]
        return [h.store for h in self.hosts if h.store is not None]

    def data_version(self):
        if self.hosts is None:
            return self._store.n
        return tuple((h.state, h.store.n if h.store is not None else -1) for h in self.hosts)

    def start(self):
        if self.hosts is None:
            self.collector.start()
        else:
            for h in self.hosts:
                h.start()

    def stop(self):
        if self.hosts is None:
            self.collector.stop()
        else:
            for h in self.hosts:
                h.stop()


def scrub_time(app, delta):
    """Step the paused sample cursor by `delta` samples (entering pause when stepping back)."""
    view = app.view
    stores = [st for st in app.stores() if st is not None and st.n > 0]
    if not stores:
        return
    if view.frozen is None:
        if delta >= 0:
            view.say("already at live edge")
            return
        view.frozen = {id(st): st.n for st in stores}
    for st in stores:
        cur = view.frozen.get(id(st), st.n)
        min_end = max(1, st.n - st.cap + 1)
        view.frozen[id(st)] = max(min_end, min(st.n, cur + delta))


def handle_mouse(app, mouse, curses):
    """Apply a curses mouse 5-tuple (id, x, y, z, bstate)."""
    if not mouse or len(mouse) < 5:
        return
    _, mx, my, _, bstate = mouse[:5]
    view, store = app.view, app.store
    b4 = getattr(curses, "BUTTON4_PRESSED", 0)
    b5 = getattr(curses, "BUTTON5_PRESSED", 0x200000)
    if bstate & b4:
        handle_key(app, curses.KEY_UP if view.mode in ("group", "all") else curses.KEY_LEFT)
        return
    if bstate & b5:
        handle_key(app, curses.KEY_DOWN if view.mode in ("group", "all") else curses.KEY_RIGHT)
        return
    click = (getattr(curses, "BUTTON1_CLICKED", 0) | getattr(curses, "BUTTON1_PRESSED", 0)
             | getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0))
    dbl = getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
    if not (bstate & click):
        return
    if view.mode in ("group", "all") and store is not None:
        if view.list_y0 is not None and 0 <= my - view.list_y0 < view.list_rows:
            end = view_end(view, store)
            ids = [s.sid for s in sorted_sensors(store, view, list_sensors(store, view, end), end)]
            idx = view.top + (my - view.list_y0)
            if 0 <= idx < len(ids):
                view.sel = ids[idx]
        return
    geom = view.grid_geom
    if not geom:
        return
    idx = -1
    if geom[0] == "grid":
        _, y0, cols, rows, pw, ph, gap, count = geom
        if my >= y0 and pw > 0 and ph > 0:
            r = (my - y0) // ph
            stride = max(1, pw + gap)
            c, rem = mx // stride, mx % stride
            if 0 <= r < rows and 0 <= c < cols and rem < pw:
                idx = r * cols + c
                if idx >= count:
                    idx = -1
    elif geom[0] == "rows":
        _, y0, k, count = geom
        if my >= y0 and k > 0:
            idx = (my - y0) // k
            if idx >= count:
                idx = -1
    if idx < 0:
        return
    if view.mode == "fleet":
        view.host_sel = idx
        if bstate & dbl:
            handle_key(app, 10)
    elif view.mode == "overview":
        view.panel = idx
        if bstate & dbl:
            handle_key(app, 10)


def handle_key(app, ch, mouse=None):
    """Apply one keypress; returns False to quit."""
    import curses
    view, store = app.view, app.store
    view.message = ""
    if view.help:
        view.help = False
        return True
    if ch == getattr(curses, "KEY_MOUSE", -2):
        if mouse is None:
            try:
                mouse = curses.getmouse()
            except curses.error:
                return True
        handle_mouse(app, mouse, curses)
        return True
    if view.filter_editing:
        if ch in (curses.KEY_ENTER, 10, 13):
            view.filter_editing = False
            view.sel, view.top = None, 0
            if view.filter_text and store is not None and not view_visible(store, view, view_end(view, store)):
                view.say("no sensors match /%s" % view.filter_text)
        elif ch == 27:
            view.filter_editing = False
            view.filter_text = ""
            view.sel, view.top = None, 0
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            if view.filter_text:
                view.filter_text = view.filter_text[:-1]
            else:
                view.filter_editing = False
            view.sel, view.top = None, 0
        elif 32 <= ch <= 126 and len(view.filter_text) < 40:
            view.filter_text += chr(ch)
            view.sel, view.top = None, 0
        return True
    if ch in (ord("q"), ord("Q")):
        return False
    if ch == ord("?"):
        view.help = True
    elif ch in (ord("+"), ord("=")):
        shorter = [w for w in WINDOWS if w < view.window]
        view.window = shorter[-1] if shorter else view.window
    elif ch in (ord("-"), ord("_")):
        longer = [w for w in WINDOWS if w > view.window and w <= view.history + 1e-6]
        if longer:
            view.window = longer[0]
        else:
            view.say("window is capped by --history %s" % fmt_duration(view.history))
    elif ch == ord("u"):
        view.fahrenheit = not view.fahrenheit
    elif ch == ord("b"):
        view.bell = not view.bell
        if view.bell:
            view.bell_seen = {(id(st), sid) for st in app.stores() if st is not None for sid in st.crit_hits}
        view.say("bell alert " + ("on" if view.bell else "off"))
    elif ch in (ord("p"), ord(" ")):
        view.frozen = None if view.frozen is not None else {id(st): st.n for st in app.stores()}
    elif ch == ord("["):
        scrub_time(app, -max(1, view.samples() // 4))
    elif ch == ord("]"):
        scrub_time(app, max(1, view.samples() // 4))
    elif ch == ord("{"):
        scrub_time(app, -1)
    elif ch == ord("}"):
        scrub_time(app, 1)
    elif ch == ord("/") and view.mode != "fleet":
        view.filter_editing = True
    elif ch == ord("w") and view.mode != "fleet":
        view.hot_only = not view.hot_only
        view.sel, view.top = None, 0
        if view.hot_only and store is not None and not view_visible(store, view, view_end(view, store)):
            view.say("all sensors normal (no warnings)")
    elif ch == ord("f") and view.mode in ("group", "all") and store is not None:
        end = view_end(view, store)
        ids = [s.sid for s in sorted_sensors(store, view, list_sensors(store, view, end), end)]
        sel = view.sel if view.sel in ids else (ids[0] if ids else None)
        if sel:
            if sel in view.pinned:
                view.pinned.remove(sel)
                view.say("unpinned %s" % store.by_id[sel].label)
            else:
                view.pinned.add(sel)
                view.say("pinned %s" % store.by_id[sel].label)
            view.sel = sel
    elif ch == ord("r"):
        targets = [store] if store is not None else app.stores()
        target_ids = {id(st) for st in targets if st is not None}
        for st in targets:
            if st is not None:
                st.reset_stats()
        view.bell_seen = {p for p in view.bell_seen if p[0] not in target_ids}
        view.say("min / max reset")
    elif ch == ord("g"):
        view.glyphs = Glyphs("ascii" if view.glyphs.name == "unicode" else "unicode")
    elif ch == ord("m") and view.mode in ("overview", "fleet"):
        view.metric = "avg" if view.metric == "max" else "max"
    elif ch == ord("a") and view.mode != "fleet":
        view.mode, view.sel, view.top = ("overview", None, 0) if view.mode == "all" else ("all", None, 0)
    elif ch == ord("s") and view.mode == "fleet":
        links = sorted_hosts(app)
        cur = links[view.host_sel] if 0 <= view.host_sel < len(links) else None
        idx = FLEET_SORT_MODES.index(view.fleet_sort) if view.fleet_sort in FLEET_SORT_MODES else 0
        view.fleet_sort = FLEET_SORT_MODES[(idx + 1) % len(FLEET_SORT_MODES)]
        new_links = sorted_hosts(app)
        view.host_sel = new_links.index(cur) if cur in new_links else 0
        view.say("fleet sorted by %s" % view.fleet_sort)
    elif ch == ord("s") and view.mode not in ("overview", "fleet"):
        idx = SORT_MODES.index(view.sort) if view.sort in SORT_MODES else 0
        view.sort = SORT_MODES[(idx + 1) % len(SORT_MODES)]
        view.sel, view.top = None, 0
    elif view.mode == "fleet":
        links = sorted_hosts(app)
        n = len(links)
        cols = max(1, view.grid_cols)
        if ch in (curses.KEY_RIGHT, ord("l")):
            view.host_sel = min(n - 1, view.host_sel + 1)
        elif ch in (curses.KEY_LEFT, ord("h")):
            view.host_sel = max(0, view.host_sel - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            view.host_sel = min(n - 1, view.host_sel + cols)
        elif ch in (curses.KEY_UP, ord("k")):
            view.host_sel = max(0, view.host_sel - cols)
        elif ch in (curses.KEY_ENTER, 10, 13) and n:
            link = links[view.host_sel]
            if link.store is None or not link.store.n:
                view.say("no data from %s yet" % link.name)
            else:
                app.host = link
                view.mode, view.panel, view.group, view.sel, view.top = "overview", 0, None, None, 0
    elif view.mode == "overview" and (view.filter_text or view.hot_only) and ch in (27, curses.KEY_BACKSPACE, 127, 8):
        view.filter_text, view.hot_only = "", False
        view.sel, view.top = None, 0
        view.say("filters cleared")
    elif view.mode == "overview" and app.hosts is not None and ch in (27, curses.KEY_BACKSPACE, 127, 8):
        prev = app.host
        app.host = None
        view.mode = "fleet"
        links = sorted_hosts(app)
        if prev in links:
            view.host_sel = links.index(prev)
    elif view.mode == "overview":
        n = len(view.panels)
        cols = max(1, view.grid_cols)
        if ch in (curses.KEY_RIGHT, ord("l")):
            view.panel = min(n - 1, view.panel + 1)
        elif ch in (curses.KEY_LEFT, ord("h")):
            view.panel = max(0, view.panel - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            view.panel = min(n - 1, view.panel + cols)
        elif ch in (curses.KEY_UP, ord("k")):
            view.panel = max(0, view.panel - cols)
        elif ch in (curses.KEY_ENTER, 10, 13) and n:
            view.mode, view.group, view.sel, view.top = "group", view.panels[view.panel], None, 0
    else:
        end = view_end(view, store)
        ids = [s.sid for s in sorted_sensors(store, view, list_sensors(store, view, end), end)]
        i = ids.index(view.sel) if view.sel in ids else 0
        step = {curses.KEY_DOWN: 1, ord("j"): 1, curses.KEY_UP: -1, ord("k"): -1,
                curses.KEY_NPAGE: 10, curses.KEY_PPAGE: -10}.get(ch)
        if step and ids:
            view.sel = ids[max(0, min(len(ids) - 1, i + step))]
        elif ch == curses.KEY_HOME and ids:
            view.sel = ids[0]
        elif ch == curses.KEY_END and ids:
            view.sel = ids[-1]
        elif ch in (27, curses.KEY_BACKSPACE, 127, 8, curses.KEY_LEFT, ord("h")):
            if not ids and (view.filter_text or view.hot_only):
                view.filter_text, view.hot_only = "", False
                view.sel, view.top = None, 0
                view.say("filters cleared")
            else:
                view.mode, view.sel, view.top = "overview", None, 0
    return True


def read_escape(win, curses):
    """Decode an escape sequence curses left undecoded. In keypad mode ncurses only knows the
    application-mode arrows (ESC O B); tmux, mosh and some terminals send ESC [ B instead."""
    table = {"[A": curses.KEY_UP, "OA": curses.KEY_UP, "[B": curses.KEY_DOWN, "OB": curses.KEY_DOWN,
             "[C": curses.KEY_RIGHT, "OC": curses.KEY_RIGHT, "[D": curses.KEY_LEFT, "OD": curses.KEY_LEFT,
             "[5~": curses.KEY_PPAGE, "[6~": curses.KEY_NPAGE, "[H": curses.KEY_HOME, "OH": curses.KEY_HOME,
             "[1~": curses.KEY_HOME, "[F": curses.KEY_END, "OF": curses.KEY_END, "[4~": curses.KEY_END}
    seq = ""
    win.timeout(30)
    try:
        while len(seq) < 6:
            c = win.getch()
            if c < 0 or c > 255:
                break
            seq += chr(c)
            if seq[-1].isalpha() or seq[-1] == "~":
                break
    finally:
        win.timeout(200)
    if not seq:
        return 27
    return table.get(seq, -1)


def run_tui(app):
    import curses
    os.environ.setdefault("ESCDELAY", "25")
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass

    def main(stdscr):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except curses.error:
            pass
        stdscr.keypad(True)
        stdscr.timeout(200)
        attrs = init_colors(curses)
        drawn_ui, drawn_data, last = None, None, 0.0
        # Nine machines each report once per interval, out of phase: redraw for new data at
        # most twice a second instead of nine times.
        min_gap = 0.5 if app.hosts is not None else 0.0
        while True:
            height, width = stdscr.getmaxyx()
            if app.view.message and time.time() - app.view.message_t > 3.0:
                app.view.message = ""
            ui = (width, height, app.view.window, app.view.mode, app.view.help, app.view.message,
                  app.host is not None, app.view.filter_editing, app.view.filter_text,
                  app.view.hot_only, app.view.bell, app.view.fleet_sort)
            data = app.data_version()
            check_bell(app, curses.beep)
            if ui != drawn_ui or (data != drawn_data and time.monotonic() - last >= min_gap):
                draw(stdscr, curses, render_screen(app, width, height), attrs)
                drawn_ui, drawn_data, last = ui, data, time.monotonic()
            ch = stdscr.getch()
            if ch == 27:
                ch = read_escape(stdscr, curses)
            if ch == -1:
                continue
            if ch == curses.KEY_RESIZE:
                drawn_ui = None
                continue
            if not handle_key(app, ch):
                return
            drawn_ui = None

    app.start()
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()


def init_colors(curses):
    attrs = {"n": 0, "b": curses.A_BOLD, "title": curses.A_BOLD, "hdr": curses.A_REVERSE | curses.A_BOLD,
             "dim": curses.A_DIM, "axis": curses.A_DIM, "border": curses.A_DIM, "bsel": curses.A_BOLD,
             "cool": 0, "ok": 0, "warn": curses.A_BOLD, "crit": curses.A_BOLD | curses.A_REVERSE,
             "fan": 0}
    if not curses.has_colors():
        return attrs
    try:
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        pairs = [("cool", curses.COLOR_CYAN, 0), ("ok", curses.COLOR_GREEN, 0),
                 ("warn", curses.COLOR_YELLOW, curses.A_BOLD), ("crit", curses.COLOR_RED, curses.A_BOLD),
                 ("fan", curses.COLOR_MAGENTA, 0), ("bsel", curses.COLOR_CYAN, curses.A_BOLD)]
        if curses.COLORS >= 16:
            pairs += [("dim", 8, 0), ("axis", 8, 0), ("border", 8, 0)]
        for i, (name, fg, extra) in enumerate(pairs, start=1):
            curses.init_pair(i, fg, bg)
            attrs[name] = curses.color_pair(i) | extra
    except curses.error:
        pass
    return attrs


def draw(win, curses, lines, attrs):
    win.erase()
    height, width = win.getmaxyx()
    for y, line in enumerate(lines[:height]):
        x = 0
        for text, style in line.segs:
            if x >= width:
                break
            room = width - x - (1 if y == height - 1 else 0)
            text = clip(text, room)
            if not text:
                continue
            try:
                win.addstr(y, x, text, attrs.get(style, 0))
            except curses.error:
                pass
            x += dwidth(text)
    win.noutrefresh()
    curses.doupdate()


# ------------------------------------------------------------------------- text output ---

ANSI = {"b": "1", "title": "1", "hdr": "1;7", "dim": "2", "axis": "2", "border": "2",
        "bsel": "1;36", "cool": "36", "ok": "32", "warn": "1;33", "crit": "1;31", "fan": "35"}


def emit(lines, color, out=None):
    out = out or sys.stdout
    for line in lines:
        parts = []
        for text, style in line.segs:
            code = ANSI.get(style) if color else None
            parts.append("\033[%sm%s\033[0m" % (code, text) if code else text)
        out.write("".join(parts).rstrip() + "\n")


def render_once(app):
    view, store = app.view, app.store
    end = store.n
    info = app.info
    head_line = Line("thermalmaster %s  " % VERSION, "b").add(
        "  ".join(p for p in (info.get("host"), info.get("model"), info.get("cpu"), info.get("os"),
                              "%s %s" % (info.get("kernel", ""), info.get("arch", ""))) if p), "dim")
    for ttext, tstyle in app_throttles(app):
        head_line.add("  " + ttext, tstyle)
    lines = [head_line]
    visible = store.visible()
    label_w = min(34, max([dwidth(s.label) for s in visible] + [8]))
    for g in store.group_keys:
        members = [s for s in visible if s.group == g]
        if not members:
            continue
        kind = group_kind(members)
        lines.append(Line())
        head = Line(GROUPS[g][0], "title").add("  " + plural(len(members), "sensor"), "dim")
        mx, av = store.at("@%s:max" % g, end - 1), store.at("@%s:avg" % g, end - 1)
        head.add("  max %s  avg %s" % (view.fmt(mx, kind), view.fmt(av, kind)), "dim")
        lines.append(head)
        for s in sorted_sensors(store, view, members, end):
            v = store.at(s.sid, end - 1)
            warn, crit = s.limits() if s.kind == "temp" else (None, None)
            line = Line("  " + ljust(s.label, label_w) + " ")
            line.add(rjust(view.fmt(v, s.kind), 10), level(v, warn, crit) if s.kind == "temp" else "fan")
            lim = ""
            if s.sid not in store.ever:
                reason = hidden_reason(store, s)
                if reason:
                    lim = "  [%s]" % reason
            elif s.kind == "temp" and s.hw_limits():
                lim = "  limits %s/%s" % (view.fmt(warn, unit=False, digits=0), view.fmt(crit, unit=False, digits=0))
            elif s.kind in ("rpm", "watt") and v == v and s.valid(v):
                if s.kind == "rpm" and v == 0.0 and s.rmin:
                    lim = "  idle"
                else:
                    pct = s.fan_pct(v)
                    if pct == pct:
                        lim = "  %d%%" % round(pct)
            line.add(lim + "  " + s.detail, "dim")
            lines.append(line)
    hidden = len(store.sensors) - len(visible)
    lines.append(Line())
    lines.append(Line("Sources", "title").add(
        "  %s shown%s" % (plural(len(visible), "sensor"), ", %d without a valid reading hidden (--all shows them)" % hidden if hidden else ""), "dim"))
    for row in source_rows(app.sources, view):
        lines.append(Line(row, "dim"))
    for note in app.notes:
        lines.append(Line("  note: " + note, "warn"))
    return lines


def sensor_json(store):
    end, rows = store.n, []
    for s in store.sensors if store.show_all else store.visible():
        v = store.at(s.sid, end - 1)
        warn, crit = s.limits() if s.kind == "temp" else (None, None)
        m = s.margin(v)
        row = {"id": s.sid, "label": s.label, "group": s.group, "kind": s.kind,
               "value": None if v != v else round(v, 2), "warn": warn, "crit": crit,
               "margin": None if m != m else round(m, 2),
               "source": s.source, "detail": s.detail}
        if s.kind in ("rpm", "pct"):
            pct = s.fan_pct(v)
            row["fan_pct"] = None if pct != pct else round(pct, 1)
            if s.rmin is not None:
                row["rmin"] = s.rmin
            if s.rmax is not None:
                row["rmax"] = s.rmax
        elif s.kind == "watt":
            pct = s.fan_pct(v)
            row["power_pct"] = None if pct != pct else round(pct, 1)
            if s.rmax is not None:
                row["rmax"] = s.rmax
        if s.sid not in store.ever:
            row["hidden_reason"] = hidden_reason(store, s)
        if store.warn_hits.get(s.sid):
            row["warn_hits"] = store.warn_hits[s.sid]
        if store.crit_hits.get(s.sid):
            row["crit_hits"] = store.crit_hits[s.sid]
        rows.append(row)
    return rows


def snapshot_json(app):
    sensors = sensor_json(app.store)
    return {"thermalmaster": VERSION, "time": time.time(), "info": app.info, "notes": app.notes,
            "throttles": [t[0] for t in app_throttles(app)],
            "sources": [{"name": src.name, "sensors": len(src.sensors),
                         "read_ms": None if src.cost is None else round(src.cost * 1000, 2)}
                        for src in app.sources],
            "sensors": sensors}


# -------------------------------------------------------------------------------- fleet ---
#
# `--fleet` runs `thermalmaster --agent` on every machine: this very file is piped into
# `python3 -` over ssh (like gpumaster's remote.sh), so the machines need nothing installed
# and always run the same version as the viewer. An agent prints one JSON line describing
# its sensors, then one line of values per sample.

def sensor_row(s):
    return [s.sid, s.label, s.group, s.source, s.detail, s.kind, s.warn, s.crit,
            s.defaults[0], s.defaults[1], s.lo, s.hi, s.rmin, s.rmax]


def sensor_from_row(row):
    sid, label, group, source, detail, kind, warn, crit, dwarn, dcrit, lo, hi = row[:12]
    rmin = row[12] if len(row) > 12 else None
    rmax = row[13] if len(row) > 13 else None
    return Sensor(sid, label, group if group in GROUPS else "other", source, detail,
                  kind if kind in ("temp", "watt", "rpm", "pct") else "temp", warn, crit, lo, hi,
                  (dwarn, dcrit), rmin=rmin, rmax=rmax)


def run_agent(app, out=None, count=None):
    """`--agent`: stream this machine's sensors as JSON lines until the reader goes away."""
    out = out or sys.stdout
    sensors = app.store.sensors
    tick = app.view.interval

    def send(msg):
        out.write(json.dumps(msg, separators=(",", ":")) + "\n")
        out.flush()

    try:
        send({"type": "meta", "version": VERSION, "interval": tick, "info": app.info,
              "notes": app.notes, "sensors": [sensor_row(s) for s in sensors]})
        n, nxt = 0, time.monotonic()
        while count is None or n < count:
            vals = app.collector.sample()
            row = []
            for s in sensors:
                v = vals.get(s.sid)
                row.append(round(v, 2) if v is not None and math.isfinite(v) else None)
            th = [[t, st] for t, st in app_throttles(app)]
            pk = {s.sid: round(s.parked_at, 1) for s in sensors if s.parked_at is not None}
            msg = {"type": "s", "t": round(time.time(), 3), "v": row}
            if th:
                msg["th"] = th
            if pk:
                msg["pk"] = pk
            send(msg)
            if n % 10 == 0:
                send({"type": "src", "sources": [[src.name, len(src.sensors), src.cost,
                                                  src.interval(tick)] for src in app.sources]})
            n += 1
            nxt += tick
            delay = nxt - time.monotonic()
            if delay < 0:
                nxt, delay = time.monotonic(), 0.0
            if count is None or n < count:
                time.sleep(delay)
    except (BrokenPipeError, KeyboardInterrupt):
        try:        # the viewer is gone; keep the interpreter from complaining at exit
            os.dup2(os.open(os.devnull, os.O_WRONLY), out.fileno())
        except (OSError, AttributeError, ValueError):
            pass
    return 0


class RemoteSource:
    """A fleet member's report about one of its sources (for the help screen)."""

    def __init__(self, name, n, cost, every):
        self.name, self.sensors, self.cost, self.every = name, [None] * int(n or 0), cost, every

    def interval(self, tick):
        return self.every or tick


class HostLink:
    """One --fleet machine: an agent over ssh (target "local" runs it here), feeding its own
    Store. Reconnects with backoff; the history survives a reconnect."""

    def __init__(self, name, target, interval, capacity, payload, agent_args=(), show_all=False):
        self.name, self.target, self.interval = name, target, interval
        self.capacity, self.payload, self.show_all = capacity, payload, show_all
        self.agent_args = list(agent_args)
        self.state = "connecting"       # connecting | up | down
        self.error = ""
        self.retry_at = None
        self.info = {"host": name}
        self.notes, self.sources = [], []
        self.throttles = []
        self.store, self.sids = None, []
        self.last_rx = None
        self.proc = None
        self.thread = None
        self._stop = threading.Event()
        self._err = collections.deque(maxlen=6)

    def command(self):
        agent = ["python3", "-", "--agent", "--interval", "%g" % self.interval] + self.agent_args
        if self.target == "local":
            return [sys.executable] + agent[1:]
        return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
                "-o", "LogLevel=ERROR", self.target,
                "exec " + " ".join(shlex.quote(a) for a in agent)]

    def start(self):
        self.thread = threading.Thread(target=self._run, name="fleet-" + self.name, daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        self.retry_at = None
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _run(self):
        backoff = 2.0
        while not self._stop.is_set():
            if self._session():
                backoff = 2.0
            if self._stop.is_set():
                break
            self.state = "down"
            self.retry_at = time.monotonic() + backoff
            self._stop.wait(backoff)
            backoff = min(30.0, backoff * 2)
            if not self._stop.is_set():
                self.state = "connecting"

    def _session(self):
        """One connection; True if it delivered samples."""
        got = False
        self._err.clear()
        try:
            proc = subprocess.Popen(self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        except OSError as e:
            self.error = str(e)
            return False
        self.proc = proc
        threading.Thread(target=self._drain, args=(proc.stderr,), daemon=True).start()
        try:
            proc.stdin.write(self.payload)
            proc.stdin.close()
        except OSError:
            pass
        try:
            for raw in proc.stdout:
                if self._stop.is_set():
                    break
                try:
                    msg = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue        # a chatty login script, not the agent
                if isinstance(msg, dict) and self.handle(msg) == "s":
                    got = True
        finally:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
            proc.wait()
        err = [line for line in self._err if line]
        self.error = err[-1] if err else "agent exited (rc %s)" % proc.returncode
        return got

    def _drain(self, pipe):
        for raw in pipe:
            self._err.append(raw.decode("utf-8", "replace").strip())

    def handle(self, msg):
        kind = msg.get("type")
        if kind == "meta":
            sensors = [sensor_from_row(r) for r in msg.get("sensors") or []]
            sids = [x.sid for x in sensors]
            if self.store is None or sids != self.sids:
                self.store, self.sids = Store(sensors, self.capacity, self.show_all), sids
            else:
                for s in self.store.sensors:
                    s.parked_at = None
            info = dict(msg.get("info") or {})
            info["hostname"], info["host"] = info.get("host", ""), self.name
            self.info = info
            self.notes = list(msg.get("notes") or [])
            self.throttles = []
            self.state, self.error = "up", ""
        elif kind == "s" and self.store is not None:
            pk = msg.get("pk") if isinstance(msg.get("pk"), dict) else {}
            for s in self.store.sensors:
                pv = pk.get(s.sid)
                s.parked_at = float(pv) if isinstance(pv, (int, float)) else None
            vals = {sid: v for sid, v in zip(self.sids, msg.get("v") or []) if v is not None}
            self.store.append(msg.get("t") or time.time(), vals)
            self.throttles = [(r[0], r[1]) for r in (msg.get("th") or [])
                              if isinstance(r, (list, tuple)) and len(r) >= 2]
            self.last_rx = time.monotonic()
            self.state = "up"
        elif kind == "src":
            self.sources = [RemoteSource(*r[:4]) for r in msg.get("sources") or [] if len(r) >= 4]
        return kind

    def stale(self):
        return self.last_rx is None or time.monotonic() - self.last_rx > max(5.0, 4 * self.interval)


def load_fleet(spec, here, mobile=False):
    """--fleet: "" -> fleet.json next to this file; "a,b" -> those ssh aliases; else a .json.

    Machines marked "mobile" in the file (the laptops, which are usually asleep or away)
    are left out unless --mobile. An explicit comma list is taken as given."""
    if spec and not spec.endswith(".json") and not os.path.isfile(spec):
        return [(h, h) for h in (x.strip() for x in spec.split(",")) if h]
    path = spec or os.path.join(here, "fleet.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(
            "no fleet file at %s. Copy examples/thermalmaster.fleet.json, "
            "edit the ssh aliases, and pass it with --fleet FILE" % path
        )
    with open(path) as f:
        data = json.load(f)
    hosts = []
    for h in data.get("hosts", []) if isinstance(data, dict) else data:
        if isinstance(h, str):
            hosts.append((h, h))
        elif isinstance(h, dict) and not h.get("disabled"):
            if h.get("mobile") and not mobile:
                continue
            name = h.get("name") or h.get("target")
            if name:
                hosts.append((name, h.get("target") or name))
    return hosts


def link_status(link):
    """(word, style) for a fleet machine: up, stale 12s, connecting, reconnecting, down."""
    if link.state == "up":
        if not link.stale():
            return "up", "ok"
        age = time.monotonic() - link.last_rx if link.last_rx else 0.0
        return "stale %ds" % age, "warn"
    if link.state == "down":
        return "down", "crit"
    return ("reconnecting" if link.store is not None else "connecting"), "dim"


def link_error(link):
    if link.state != "down":
        return ""
    wait = ""
    if link.retry_at is not None:
        wait = " (retry in %ds)" % max(0, round(link.retry_at - time.monotonic()))
    return (link.error or "no connection") + wait


GROUP_SHORT = {"cpu": "CPU", "gpu": "GPU", "soc": "SoC", "mem": "Mem", "storage": "Disk",
               "vrm": "VRM", "net": "Net", "acpi": "ACPI", "board": "Board", "ambient": "Amb",
               "battery": "Bat", "other": "Other", "power": "Pwr", "fan": "Fan", "gpufan": "GPUfan"}


def fleet_rows(app):
    rows = []
    for h in sorted_hosts(app):
        word = link_status(h)[0]
        n = len(h.store.visible()) if h.store is not None else 0
        rows.append("  %-16s %-12s %12s  %s" % (h.name, word, plural(n, "sensor"), link_error(h)))
    return rows


FLEET_SORT_MODES = ("config", "hottest", "margin")


def host_hottest(link, view):
    store = link.store
    if store is None or not store.n:
        return None
    if view.frozen is None and link_status(link)[0] != "up":
        return None
    end = view_end(view, store)
    sid = store.hottest(end - 1)
    if not sid:
        return None
    return store.at(sid, end - 1), store.by_id[sid]


def host_min_margin(link, view):
    store = link.store
    if store is None or not store.n:
        return None
    if view.frozen is None and link_status(link)[0] != "up":
        return None
    end = view_end(view, store)
    best = None
    for s in store.visible():
        if s.kind != "temp":
            continue
        v = store.at(s.sid, end - 1)
        m = s.margin(v)
        if m == m and (best is None or (m, -v) < (best[0], -best[1])):
            best = (m, v, s)
    return best


def sorted_hosts(app):
    links = list(app.hosts or [])
    mode = getattr(app.view, "fleet_sort", "config")
    if mode == "hottest":
        def hkey(item):
            idx, h = item
            hot = host_hottest(h, app.view)
            if hot is not None and hot[0] == hot[0]:
                return (0, -hot[0], idx)
            return (1, 0 if h.state == "down" else 1, idx)
        return [h for _, h in sorted(enumerate(links), key=hkey)]
    if mode == "margin":
        def mkey(item):
            idx, h = item
            mm = host_min_margin(h, app.view)
            if mm is not None:
                return (0, mm[0], -mm[1], idx)
            return (1, 0 if h.state == "down" else 1, 0.0, idx)
        return [h for _, h in sorted(enumerate(links), key=mkey)]
    return links


def render_host_box(link, pw, ph, view, selected=False):
    """One machine in the fleet overview: a row per sensor group, value and curve."""
    G = view.glyphs
    word, wstyle = link_status(link)
    border = "bsel" if selected else ("crit" if word == "down" else "border")
    hot = host_hottest(link, view)
    hot_text = " %s " % view.fmt(hot[0]) if hot else ""
    top = Line(G.tl + G.h, border).add(" %s " % link.name, "bsel" if selected else "title")
    if word != "up":
        top.add(word + " ", wstyle)
    elif link.throttles:
        tbadge, tstyle = link.throttles[0]
        if top.width + dwidth(tbadge) + 1 + dwidth(hot_text) <= pw - 4:
            top.add(tbadge + " ", tstyle)
    elif link.store is not None and link.store.crit_hits:
        pbadge = "PEAK!"
        if top.width + dwidth(pbadge) + 1 + dwidth(hot_text) <= pw - 4:
            top.add(pbadge + " ", "crit")
    top.add(G.h * max(0, pw - 2 - top.width - dwidth(hot_text)), border)
    top.add(hot_text, level(hot[0], *hot[1].limits()) if hot else "dim").add(G.h + G.tr, border)
    lines = [top.fit(pw).pad(pw)]
    inner, iw = ph - 2, pw - 2
    body = []
    store = link.store
    if store is None or not store.n:
        body.append(Line(" " + ("down" if word == "down" else "connecting ..."), wstyle))
        err = link_error(link)
        if err:
            body.append(Line(" " + ellipsize(err, iw - 1, G.ell), "dim"))
    else:
        end = view_end(view, store)
        visible = store.visible()
        groups = [g for g in store.group_keys if any(x.group == g for x in visible)]
        room = inner - 1 if inner >= 3 else inner
        shown = groups if len(groups) <= room else groups[:max(0, room - 1)]
        name_w, val_w = 10, 9
        spark_w = max(3, iw - 3 - name_w - val_w)
        count = view.samples()
        how = "max" if view.metric == "max" else "avg"
        for g in shown:
            members = [x for x in visible if x.group == g]
            kind = group_kind(members)
            warn, crit = group_limits(members) if kind == "temp" else (None, None)
            key = "@%s:%s" % (g, view.metric)
            value = store.at(key, end - 1)
            series = [view.conv(v, kind) for v in store.window(key, count, end)]
            lo, hi = y_range(series, min_span(kind, view), 0.0 if kind != "temp" else None)
            if kind == "temp":
                cw = view.conv(warn) if warn is not None else None
                cc = view.conv(crit) if crit is not None else None

                def style_of(v, a=cw, b=cc):
                    return level(v, a, b)
            else:
                def style_of(v):
                    return "fan"
            line = Line(" ").add(ljust(GROUPS[g][0], name_w))
            line.add(rjust(view.fmt(value, kind), val_w) + " ",
                     level(value, warn, crit) if kind == "temp" else "fan")
            line.extend(braille_spark(resample(series, spark_w * 2, how), spark_w, lo, hi, style_of))
            body.append(line)
        if len(shown) < len(groups):
            body.append(Line(" +%d more" % (len(groups) - len(shown)), "dim"))
        if inner >= 3:
            body = body[:inner - 1]
            while len(body) < inner - 1:
                body.append(Line())
            if word == "up":
                desc = link.info.get("cpu") or link.info.get("model") or ""
                foot = Line(" " + (desc + " " + G.dot + " " if desc else "") + plural(len(visible), "sensor"), "dim")
            else:
                foot = Line(" " + word + ((": " + link_error(link)) if word == "down" else ""), wstyle)
            body.append(foot)
    body = body[:inner]
    while len(body) < inner:
        body.append(Line())
    for b in body:
        lines.append(Line(G.v, border).extend(b.fit(iw)).pad(pw - 1).add(G.v, border))
    lines.append(Line(G.bl + G.h * max(0, pw - 2) + G.br, border).fit(pw))
    return lines[:ph]


def render_host_rows(app, width, height):
    """Fleet overview for small terminals: one line per machine, group maxima inline."""
    view, links = app.view, sorted_hosts(app)
    name_w = max(dwidth(h.name) for h in links) + 1
    out = []
    for i, link in enumerate(links[:height]):
        sel = i == view.host_sel
        word, wstyle = link_status(link)
        line = Line((view.glyphs.mark + " ") if sel else "  ", "bsel" if sel else "n")
        line.add(ljust(link.name, name_w), "bsel" if sel else "title")
        store = link.store
        if store is None or not store.n:
            line.add(word + ("  " + link_error(link) if word == "down" else ""), wstyle)
        else:
            if word != "up":
                line.add(word + " ", wstyle)
            elif link.throttles:
                line.add(link.throttles[0][0] + " ", link.throttles[0][1])
            elif store.crit_hits:
                line.add("PEAK! ", "crit")
            end = view_end(view, store)
            visible = store.visible()
            for g in store.group_keys:
                members = [x for x in visible if x.group == g]
                if not members:
                    continue
                kind = group_kind(members)
                v = store.at("@%s:%s" % (g, view.metric), end - 1)
                if v != v:
                    continue
                warn, crit = group_limits(members) if kind == "temp" else (None, None)
                piece = Line(" %s " % GROUP_SHORT[g], "dim").add(
                    view.fmt(v, kind, unit=kind != "temp", digits=0),
                    level(v, warn, crit) if kind == "temp" else "fan")
                if line.width + piece.width > width:
                    break
                line.extend(piece)
        out.append(line.fit(width))
    return out


def render_fleet_header(app, width):
    view, links = app.view, app.hosts
    up = sum(1 for h in links if link_status(h)[0] == "up")
    down = sum(1 for h in links if h.state == "down")
    left = [Line(" fleet", "b").add("  %d machines, %d up%s" % (
        len(links), up, ", %d down" % down if down else ""), "crit" if down else "dim")]
    best = None
    peak_best = None
    peak_hosts = 0
    ref_st = None
    for h in links:
        if ref_st is None and h.store is not None and h.store.n > 0:
            ref_st = h.store
        hot = host_hottest(h, view)
        if hot and (best is None or hot[0] > best[0]):
            best = (hot[0], hot[1], h.name)
        if h.store is not None and h.store.crit_hits:
            sids = [sid for sid in h.store.crit_hits if sid in h.store.by_id]
            if sids:
                peak_hosts += 1
                psid = max(sids, key=lambda k: (h.store.hi.get(k, -1e9), h.store.crit_hits[k]))
                pv = h.store.hi.get(psid, NAN)
                if peak_best is None or pv > peak_best[0]:
                    peak_best = (pv, h.store.by_id[psid], h.name)
    right = []
    if peak_best is not None:
        pv, ps, pname = peak_best
        more = " (+%d)" % (peak_hosts - 1) if peak_hosts > 1 else ""
        right.append(Line("PEAK! %s %s%s" % (pname, view.fmt(pv), more), "crit"))
    if view.fleet_sort != "config":
        right.append(Line("sort:%s" % view.fleet_sort, "bsel"))
    if best:
        v, s, name = best
        right.append(Line("hottest ", "dim").add("%s %s " % (name, s.label), "n")
                     .add(view.fmt(v), level(v, *s.limits())))
    ref_end = view_end(view, ref_st) if ref_st is not None else None
    return compose_header(width, left, right + header_tail(view, ref_st, ref_end))


def render_fleet(app, width, height):
    view, links = app.view, sorted_hosts(app)
    lines = [render_fleet_header(app, width)]
    body = height - 2
    if view.host_sel >= len(links):
        view.host_sel = max(0, len(links) - 1)
    grid = choose_grid(len(links), width, body, min_w=34, min_h=6)
    if grid:
        cols, rows = grid
        view.grid_cols = cols
        gap = 1 if cols > 1 else 0
        pw, ph = (width - gap * (cols - 1)) // cols, body // rows
        view.grid_geom = ("grid", 1, cols, rows, pw, ph, gap, len(links))
        for r in range(rows):
            row = links[r * cols:(r + 1) * cols]
            boxes = [render_host_box(h, pw, ph, view, r * cols + i == view.host_sel)
                     for i, h in enumerate(row)]
            for y in range(ph):
                line = Line()
                for i, box in enumerate(boxes):
                    if i:
                        line.add(" " * gap)
                    line.extend(box[y])
                lines.append(line)
    else:
        view.grid_cols = 1
        view.grid_geom = ("rows", 1, 1, min(len(links), max(0, body)))
        lines.extend(render_host_rows(app, width, body))
    while len(lines) < height - 1:
        lines.append(Line())
    lines = lines[:height - 1]
    lines.append(render_footer(view, width))
    return lines


def render_fleet_once(app):
    """`--fleet --once`: the hottest reading of every sensor group, one row per machine."""
    view, links = app.view, sorted_hosts(app)
    groups = [g for g in GROUPS if g not in ("fan", "gpufan", "power") and any(
        h.store is not None and any(x.group == g for x in h.store.visible()) for h in links)]
    name_w = max([dwidth(h.name) for h in links] + [7]) + 2
    col_w = 7
    lines = [Line("thermalmaster %s  fleet  " % VERSION, "b").add(
        "hottest sensor of each group, %s" % view.unit("temp").strip(), "dim"), Line()]
    head = Line(ljust("machine", name_w) + ljust("state", 7), "dim")
    for g in groups:
        head.add(rjust(GROUP_SHORT[g], col_w), "dim")
    lines.append(head.add("   hottest", "dim"))
    for h in links:
        word, wstyle = link_status(h)
        line = Line(ljust(h.name, name_w), "title").add(ljust(word, 7), wstyle)
        st = h.store
        if st is None or not st.n:
            lines.append(line.add(link_error(h) or "no data", "crit" if h.state == "down" else "dim"))
            continue
        end, visible = st.n, st.visible()
        for g in groups:
            members = [x for x in visible if x.group == g]
            v = st.at("@%s:max" % g, end - 1) if members else NAN
            if v != v:
                line.add(rjust("-", col_w), "dim")
            else:
                line.add(rjust(view.fmt(v, unit=False), col_w), level(v, *group_limits(members)))
        hot = host_hottest(h, view)
        if hot:
            line.add("   %s " % hot[1].label).add(view.fmt(hot[0]), level(hot[0], *hot[1].limits()))
        for tbadge, tstyle in h.throttles:
            line.add("  " + tbadge, tstyle)
        lines.append(line)
    return lines


def fleet_json(app):
    return {"thermalmaster": VERSION, "time": time.time(), "hosts": [
        {"name": h.name, "target": h.target, "state": link_status(h)[0], "error": link_error(h),
         "info": h.info, "notes": h.notes, "throttles": [t[0] for t in h.throttles],
         "sensors": sensor_json(h.store) if h.store else []}
        for h in app.hosts]}


def fleet_collect(links, timeout):
    """Connect to every machine and wait until each has sent a sample or failed."""
    for h in links:
        h.start()
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if all((h.store is not None and h.store.n > 0) or h.state == "down" for h in links):
                break
            time.sleep(0.1)
    finally:
        for h in links:
            h.stop()


class FleetRecorder:
    """Appends one CSV row per tick across all --fleet hosts: state, hottest and group maxima."""

    def __init__(self, path, links, interval=1.0):
        self.links = list(links)
        self.interval = interval
        self.groups = list(GROUPS)
        header = ["time", "epoch"]
        for h in self.links:
            header.extend(["%s state" % h.name, "%s hottest" % h.name,
                           "%s hottest_sensor" % h.name, "%s throttles" % h.name])
            for g in self.groups:
                header.append("%s @%s:max" % (h.name, g))
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        if exists:
            with open(path, newline="") as f:
                if next(csv.reader(f), None) != header:
                    raise SystemExit("thermalmaster: %s already holds different columns; "
                                     "record to a new file" % path)
        self.f = open(path, "a", newline="")
        self.w = csv.writer(self.f)
        if not exists:
            self.w.writerow(header)
        self._stop = threading.Event()
        self.thread = None

    def sample(self, ts=None):
        if ts is None:
            ts = time.time()
        row = [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)), "%.3f" % ts]
        for h in self.links:
            word = link_status(h)[0]
            row.append(word)
            st = h.store
            if word == "up" and st is not None and st.n > 0:
                idx = st.n - 1
                sid = st.hottest(idx)
                hot_v = st.at(sid, idx) if sid else NAN
                hot_s = st.by_id.get(sid) if sid else None
                row.append("%.2f" % hot_v if (hot_v == hot_v and math.isfinite(hot_v)) else "")
                row.append(hot_s.label if hot_s is not None else "")
                row.append(";".join(t[0] for t in h.throttles))
                for g in self.groups:
                    v = st.at("@%s:max" % g, idx)
                    if v != v or not math.isfinite(v):
                        row.append("")
                    elif g in ("fan", "gpufan"):
                        row.append("%d" % round(v))
                    else:
                        row.append("%.2f" % v)
            else:
                row.extend([""] * (3 + len(self.groups)))
        self.w.writerow(row)
        self.f.flush()

    def start(self):
        self.thread = threading.Thread(target=self._run, name="fleet-recorder", daemon=True)
        self.thread.start()

    def _run(self):
        nxt = time.monotonic()
        while not self._stop.is_set():
            nxt += self.interval
            delay = nxt - time.monotonic()
            if delay < 0:
                nxt, delay = time.monotonic(), 0.0
            if self._stop.wait(delay):
                break
            try:
                self.sample()
            except Exception:  # noqa: BLE001
                pass

    def stop(self):
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=3)

    def close(self):
        self.f.close()


def run_fleet(args, view, capacity):
    here = os.path.dirname(os.path.realpath(__file__))
    try:
        hosts = load_fleet(args.fleet, here, args.mobile)
    except (OSError, ValueError) as e:
        raise SystemExit("thermalmaster: --fleet: %s" % e)
    if not hosts:
        raise SystemExit("thermalmaster: --fleet: no machines listed")
    try:
        with open(os.path.realpath(__file__), "rb") as f:
            payload = f.read()
    except OSError as e:
        raise SystemExit("thermalmaster: --fleet needs thermalmaster.py on disk: %s" % e)
    extra = []
    for pat in args.include:
        extra += ["--include", pat]
    for pat in args.exclude:
        extra += ["--exclude", pat]
    links = [HostLink(name, target, args.interval, capacity, payload, extra, args.all)
             for name, target in hosts]
    view.mode = "fleet"
    app = App([], None, None, {"host": "fleet"}, [], view, hosts=links)
    recorder = FleetRecorder(args.record, links, args.interval) if args.record else None
    try:
        if args.once or args.json:
            fleet_collect(links, timeout=30.0)
            if recorder is not None:
                recorder.sample()
            if args.json:
                json.dump(fleet_json(app), sys.stdout, indent=1)
                sys.stdout.write("\n")
            else:
                emit(render_fleet_once(app), color=sys.stdout.isatty() and not args.no_color
                     and not os.environ.get("NO_COLOR"))
            return 0 if any(h.store is not None and h.store.n for h in links) else 1
        if args.headless:
            if recorder is None:
                raise SystemExit("thermalmaster: --headless needs --record FILE")
            sys.stderr.write("recording %d fleet machines every %gs to %s (Ctrl-C stops)\n" % (
                len(links), args.interval, args.record))
            for h in links:
                h.start()
            deadline = time.monotonic() + args.duration if args.duration else None
            wait_until = min(deadline, time.monotonic() + 10.0) if deadline else time.monotonic() + 10.0
            try:
                while time.monotonic() < wait_until:
                    if all((h.store is not None and h.store.n > 0) or h.state == "down" for h in links):
                        break
                    time.sleep(0.05)
                recorder.sample()
                recorder.start()
                while deadline is None or time.monotonic() < deadline:
                    time.sleep(min(0.2, max(0.01, (deadline - time.monotonic()) if deadline else 0.2)))
            except KeyboardInterrupt:
                pass
            finally:
                recorder.stop()
                for h in links:
                    h.stop()
            return 0
        if not sys.stdout.isatty():
            raise SystemExit("thermalmaster: the TUI needs a terminal; try --fleet --once or --json")
        if recorder is not None:
            recorder.start()
        try:
            run_tui(app)
        finally:
            if recorder is not None:
                recorder.stop()
        return 0
    finally:
        if recorder is not None:
            recorder.close()


# ------------------------------------------------------------------------ probe / replay ---

def probe():
    """Raw answers from every source on this machine, for tests/fixtures (`--probe`)."""
    if sys.platform == "darwin":
        rows, hid_rows, notes = [], [], []
        try:
            smc = SMC()
            for key in smc.all_keys():
                if not (key.startswith("T") or smc_is_power_key(key) or key == "FNum"
                        or re.match(r"^F\d(Ac|Mx|Mn)$", key)):
                    continue
                info = smc.key_info(key)
                if not info:
                    continue
                raw = smc.read(key, info[0])
                rows.append([key, info[1], (raw or b"").hex()])
        except OSError as e:
            notes.append("SMC: %s" % e)
        if platform.machine() == "arm64":
            try:
                hid = HIDTemps()
                hid_rows = [[n, v] for n, v in zip(hid.names(), hid.read_all())]
            except OSError as e:
                notes.append("IOHID: %s" % e)
        return {"format": PROBE_FORMAT, "platform": "darwin", "arch": platform.machine(),
                "info": machine_info(), "smc": rows, "hid": hid_rows,
                "thermal_state": mac_thermal_state(), "notes": notes}
    fs = SysFS(record=True)
    info = machine_info(fs)
    try:
        api = NvmlApi()
        nvml = api.snapshot()
    except OSError as e:
        api, nvml = None, {"error": str(e)}
    sources, notes = linux_sources(fs, nvml=api)
    for src in sources:
        src.poll(time.monotonic(), 1.0, force=True)
    return {"format": PROBE_FORMAT, "platform": "linux", "arch": platform.machine(),
            "info": info, "fs": fs.rec, "nvml": nvml, "notes": notes}


def sources_from_probe(data):
    """Rebuild a machine's sources from a `--probe` capture: (sources, notes, info)."""
    if data.get("format") != PROBE_FORMAT:
        raise ValueError("not a %s capture" % PROBE_FORMAT)
    if data["platform"] == "darwin":
        smc = FakeSMC(data.get("smc") or [], data.get("thermal_state"))
        hid = FakeHID(data["hid"]) if data.get("hid") else None
        sources, notes = mac_sources(smc=smc, hid=hid, arch=data.get("arch", "arm64"))
    else:
        nv = data.get("nvml") or {}
        api = FakeNvmlApi(nv) if nv.get("gpus") else None
        sources, notes = linux_sources(ReplayFS(data["fs"]), nvml=api)
    return sources, list(data.get("notes") or []) + notes, dict(data.get("info") or {})


# --------------------------------------------------------------------------------- main ---

def matches(sensor, source, pattern):
    """Glob or substring match on id, label, group, source or detail. Smart case: a pattern
    with capitals is case-sensitive, because SMC keys are ("Tp" is a CPU cluster, "TP" not)."""
    return sensor_matches(sensor, pattern, source.name if source is not None else None)


def apply_filters(sources, include, exclude):
    tpaths = []
    for src in sources:
        if isinstance(src, FileSource) and src.throttle_paths:
            tpaths = list(src.throttle_paths)
            break
    kept = []
    for src in sources:
        sids = set()
        for s in src.sensors:
            if include and not any(matches(s, src, p) for p in include):
                continue
            if any(matches(s, src, p) for p in exclude):
                continue
            sids.add(s.sid)
        if sids:
            src.keep(sids)
            if isinstance(src, FileSource):
                src.throttle_paths = []
            kept.append(src)
    if tpaths and any(s.group in ("cpu", "soc") for src in kept for s in src.sensors):
        fsrcs = [src for src in kept if isinstance(src, FileSource)]
        if fsrcs:
            cpu_src = min(fsrcs, key=lambda src: (not any(s.group == "cpu" for s in src.sensors), src.every))
            cpu_src.throttle_paths = tpaths
    return kept


def discover(args):
    if args.replay:
        with open(args.replay) as f:
            sources, notes, info = sources_from_probe(json.load(f))
    elif sys.platform == "darwin":
        sources, notes = mac_sources()
        info = machine_info()
    elif sys.platform.startswith("linux"):
        fs = SysFS()
        sources, notes = linux_sources(fs)
        info = machine_info(fs)
    else:
        sources, notes, info = [], ["unsupported platform: %s" % sys.platform], machine_info()
    return apply_filters(sources, args.include, args.exclude), notes, info


def build_parser():
    p = argparse.ArgumentParser(
        prog="thermalmaster",
        description="Every temperature sensor on this machine as live curves in the terminal. "
                    "Reads SMC/IOHID on macOS and hwmon/thermal zones/NVML on Linux; no root.",
        epilog="examples:  thermalmaster            live TUI for this machine\n"
               "           thermalmaster --fleet    every machine in fleet.json, over ssh\n"
               "           thermalmaster --fleet --mobile   the laptops as well\n"
               "           thermalmaster --once     print every sensor once\n"
               "           thermalmaster -i 2 -w 15m --exclude dell_smm\n"
               "           thermalmaster --record run.csv --headless --duration 2h",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-i", "--interval", type=float, default=1.0, metavar="SEC",
                   help="seconds between samples (default 1)")
    p.add_argument("-w", "--window", type=parse_duration, default=120.0, metavar="DUR",
                   help="time span of the curves, e.g. 90s, 5m, 1h (default 2m)")
    p.add_argument("--history", type=parse_duration, default=3600.0, metavar="DUR",
                   help="how much history to keep in memory (default 1h)")
    p.add_argument("-F", "--fahrenheit", action="store_true", help="show Fahrenheit")
    p.add_argument("--glyphs", choices=("auto", "unicode", "ascii"), default="auto",
                   help="border glyphs; auto uses ASCII under a CJK locale")
    p.add_argument("--include", action="append", default=[], metavar="PAT",
                   help="only sensors whose id, label, group or source matches (repeatable)")
    p.add_argument("--exclude", action="append", default=[], metavar="PAT",
                   help="drop matching sensors; a fully excluded chip is never read")
    p.add_argument("--all", action="store_true",
                   help="also show sensors that have not produced a plausible reading")
    p.add_argument("--bell", action="store_true",
                   help="ring terminal bell when a sensor breaches its critical limit")
    p.add_argument("--once", action="store_true", help="print every sensor once and exit")
    p.add_argument("--json", action="store_true", help="print one JSON snapshot and exit")
    p.add_argument("--no-color", action="store_true", help="no colours in --once output")
    p.add_argument("--record", metavar="FILE", help="append every sample to a CSV file")
    p.add_argument("--headless", action="store_true", help="no TUI; use with --record")
    p.add_argument("--duration", type=parse_duration, metavar="DUR",
                   help="stop after this long (headless recording)")
    p.add_argument("--fleet", nargs="?", const="", metavar="HOSTS",
                   help="every machine in fleet.json on one screen (or a comma list of ssh "
                        "aliases, or another .json); nothing is installed on them")
    p.add_argument("--mobile", action="store_true",
                   help="with --fleet, also show the laptops (the machines marked \"mobile\" "
                        "in fleet.json); they are left out by default")
    p.add_argument("--probe", action="store_true",
                   help="dump raw sensor data as JSON (test fixtures, bug reports)")
    p.add_argument("--agent", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--replay", metavar="FILE", help=argparse.SUPPRESS)
    p.add_argument("-V", "--version", action="version", version="thermalmaster " + VERSION)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.interval < 0.1:
        raise SystemExit("thermalmaster: --interval must be at least 0.1 s")
    if args.probe:
        json.dump(probe(), sys.stdout, indent=1)
        sys.stdout.write("\n")
        return 0
    wide = cjk_locale()
    set_ambiguous_wide(wide)
    glyph_name = args.glyphs if args.glyphs != "auto" else ("ascii" if wide else "unicode")
    history = max(args.history, args.window)
    capacity = min(200000, int(math.ceil(history / args.interval)) + 1)

    if args.fleet is not None:
        if args.replay or args.agent:
            raise SystemExit("thermalmaster: --fleet cannot be combined with --replay")
        if args.headless and not args.record:
            raise SystemExit("thermalmaster: --headless needs --record FILE")
        view = View(Glyphs(glyph_name), args.interval, args.window, history, args.fahrenheit,
                    bell=args.bell)
        return run_fleet(args, view, capacity)

    sources, notes, info = discover(args)
    if args.agent:
        store = Store([s for src in sources for s in src.sensors], 4)
        view = View(Glyphs(glyph_name), args.interval, args.window, history)
        return run_agent(App(sources, store, Collector(sources, store, args.interval), info,
                             notes, view))
    sensors = [s for src in sources for s in src.sensors]
    if not sensors:
        sys.stderr.write("thermalmaster: no temperature sensors found%s\n" % (
            "".join("\n  " + n for n in notes)))
        return 1
    store = Store(sensors, capacity, show_all=args.all)
    collector = Collector(sources, store, args.interval)
    view = View(Glyphs(glyph_name), args.interval, args.window, history, args.fahrenheit,
                bell=args.bell)
    app = App(sources, store, collector, info, notes, view)

    recorder = Recorder(args.record, sensors) if args.record else None
    if recorder is not None:
        collector.listeners.append(recorder)
    try:
        if args.once or args.json:
            collector.sample(force=True)
            if args.json:
                json.dump(snapshot_json(app), sys.stdout, indent=1)
                sys.stdout.write("\n")
            else:
                emit(render_once(app), color=sys.stdout.isatty() and not args.no_color
                     and not os.environ.get("NO_COLOR"))
            return 0
        if args.headless:
            if recorder is None:
                raise SystemExit("thermalmaster: --headless needs --record FILE")
            sys.stderr.write("recording %d sensors every %gs to %s (Ctrl-C stops)\n" % (
                len(sensors), args.interval, args.record))
            collector.start()
            deadline = time.monotonic() + args.duration if args.duration else None
            try:
                while deadline is None or time.monotonic() < deadline:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
            finally:
                collector.stop()
            return 0
        if not sys.stdout.isatty():
            raise SystemExit("thermalmaster: the TUI needs a terminal; try --once or --json")
        run_tui(app)
    finally:
        if recorder is not None:
            recorder.close()
        for src in sources:
            api = getattr(src, "api", None)
            if api is not None:
                api.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
