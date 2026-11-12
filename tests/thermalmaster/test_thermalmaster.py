"""Offline tests: every machine in tests/fixtures/ was captured with `thermalmaster --probe`.

    python3 -m unittest discover tests
"""

import contextlib
import csv
import io
import json
import math
import os
import struct
import sys
import tempfile
import unicodedata
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "src"))

import thermalmaster as tm  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
HOSTS = sorted(f[:-5] for f in os.listdir(FIXTURES) if f.endswith(".json") and not f.startswith("._"))
LINUX = [h for h in HOSTS if not h.startswith("mac-")]
MACS = [h for h in HOSTS if h.startswith("mac-")]


def load(host):
    with open(os.path.join(FIXTURES, host + ".json")) as f:
        return json.load(f)


def build(host):
    return tm.sources_from_probe(load(host))


def sensors_of(sources):
    return [s for src in sources for s in src.sensors]


def by_label(sources):
    return {s.label: s for s in sensors_of(sources)}


def app_for(host, samples=40, interval=1.0, glyphs="unicode"):
    """A full App fed with `samples` synthetic readings around the recorded values."""
    sources, notes, info = build(host)
    sensors = sensors_of(sources)
    store = tm.Store(sensors, 600)
    base = {}
    for src in sources:
        base.update(src.poll(0.0, interval, force=True))
    for t in range(samples):
        vals = {}
        for i, s in enumerate(sensors):
            v = base.get(s.sid)
            if v is not None:
                vals[s.sid] = v + (2.0 * math.sin(t / 5.0 + i) if s.kind == "temp" else 0.0)
        store.append(1000.0 + t, vals)
    view = tm.View(tm.Glyphs(glyphs), interval, 300.0, 3600.0)
    collector = tm.Collector(sources, store, interval)
    return tm.App(sources, store, collector, info, notes, view)


class FixtureDiscovery(unittest.TestCase):
    def test_fixtures_present(self):
        self.assertGreaterEqual(len(LINUX), 1)
        self.assertGreaterEqual(len(MACS), 1)

    def test_every_fixture_builds_with_unique_ids_and_labels(self):
        for host in HOSTS:
            with self.subTest(host=host):
                sources, notes, info = build(host)
                sensors = sensors_of(sources)
                self.assertTrue(sensors)
                self.assertEqual(len({s.sid for s in sensors}), len(sensors))
                self.assertEqual(len({s.label for s in sensors}), len(sensors))
                for s in sensors:
                    self.assertIn(s.group, tm.GROUPS)
                    self.assertIn(s.kind, ("temp", "watt", "rpm", "pct"))
                values = {}
                for src in sources:
                    values.update(src.poll(0.0, 1.0, force=True))
                valid = [s for s in sensors if s.valid(values.get(s.sid))]
                self.assertGreater(len(valid), len(sensors) // 2)
                self.assertTrue(info.get("host"))

    def test_zone_mirrors_are_not_read_twice(self):
        for host in LINUX:
            with self.subTest(host=host):
                sources, _, _ = build(host)
                names = [src.name for src in sources]
                self.assertNotIn("hwmon acpitz", names)
                self.assertFalse(any(n.startswith("hwmon iwlwifi") for n in names))

    def test_hardware_limits_are_sane(self):
        for host in HOSTS:
            sources, _, _ = build(host)
            for s in sensors_of(sources):
                for lim in (s.warn, s.crit):
                    self.assertTrue(lim is None or 0 < lim < 200, (host, s, lim))

    def test_dgx_spark(self):
        sources, _, info = build("spark-1")
        labels = by_label(sources)
        self.assertEqual(info["model"], "NVIDIA DGX Spark")
        gpu = labels["GPU0 GB10"]
        self.assertEqual((gpu.group, gpu.crit, gpu.warn), ("gpu", 86.0, 76.0))
        self.assertEqual(len([s for s in sensors_of(sources) if s.group == "acpi"]), 7)
        # four PCI functions of one ConnectX die collapse to function 0 of each domain
        mlx = [s for s in sensors_of(sources) if "mlx5@" in s.sid]
        self.assertEqual(len(mlx), 2)
        self.assertTrue(all(".0/temp" in s.sid for s in mlx))
        self.assertEqual(labels["wifi phy0"].group, "net")

    def test_popos_3090(self):
        sources, _, _ = build("workstation-e")
        sensors = sensors_of(sources)
        nvme_sensor = [s for s in sensors if s.source == "hwmon nvme" and "Sensor" in s.label]
        self.assertTrue(nvme_sensor)
        self.assertTrue(all(s.warn is None for s in nvme_sensor))    # max=65261850 is junk
        composite = [s for s in sensors if s.label.endswith("Composite")]
        self.assertEqual(len(composite), 5)
        self.assertTrue(all(s.crit and 80 < s.crit < 100 for s in composite))
        labels = by_label(sources)
        self.assertEqual(labels["DIMM 1"].group, "mem")
        self.assertEqual(labels["DIMM 2"].group, "mem")
        self.assertEqual(labels["Package id 0"].group, "cpu")
        self.assertEqual(labels["x86_pkg_temp"].group, "cpu")
        self.assertEqual(labels["iwlwifi_1"].group, "net")
        self.assertEqual(labels["acpitz"].group, "acpi")
        self.assertEqual(labels["r8169 phy"].group, "net")

    def test_popos_5090_placeholders_hidden(self):
        app = app_for("workstation-c", samples=3)
        labels = by_label(app.sources)
        self.assertNotIn(labels["asusec T_Sensor"].sid, app.store.ever)     # reads -40
        self.assertEqual(labels["asusec VRM_E"].group, "vrm")
        self.assertEqual(labels["Tctl"].group, "cpu")
        fans = [s for s in app.store.sensors if s.group == "gpufan"]
        self.assertEqual(len(fans), 6)
        self.assertTrue(all(s.kind == "pct" for s in fans))

    def test_laptop(self):
        sources, _, _ = build("linux-laptop")
        labels = by_label(sources)
        self.assertIn("BAT0 (zone)", labels)
        self.assertIn("BAT0 (hwmon)", labels)
        for name, group in (("TGVR", "vrm"), ("TCVR", "vrm"), ("CHAG", "vrm"), ("TSSD", "storage"),
                            ("VRAM", "gpu"), ("TVGA", "gpu"), ("TCPU", "cpu"), ("TCPU_PCI", "cpu")):
            self.assertEqual(labels[name].group, group, name)
        smm = [src for src in sources if src.name == "hwmon dell_smm"][0]
        self.assertGreaterEqual(smm.interval(1.0), 10.0)
        self.assertIn("alienware_wmi GPU Fan #2", labels)

    def test_dell_workstation(self):
        app = app_for("workstation-a", samples=2)
        labels = by_label(app.sources)
        self.assertNotIn(labels["wifi phy0"].sid, app.store.ever)          # ENETDOWN on read
        zero = [s for s in app.store.sensors if s.source == "hwmon dell_ddv"
                and s.sid not in app.store.ever]
        self.assertTrue(zero)                                                # 0 C slots hidden
        gpus = [s for s in app.store.sensors if s.source == "nvml" and s.kind == "temp"]
        self.assertEqual(len(gpus), 4)
        self.assertTrue(all(s.crit == 100.0 for s in gpus))

    def test_macs(self):
        for host in MACS:
            with self.subTest(host=host):
                sources, _, info = build(host)
                sensors = sensors_of(sources)
                self.assertFalse([s for s in sensors if tm.SMC_ARM_CONSTANT.match(s.label)])
                self.assertFalse([s for s in sensors if s.label.startswith("PMU tcal")])
                self.assertTrue(all(s.lo == tm.MAC_FLOOR for s in sensors if s.kind == "temp"))
                fans = [s for s in sensors if s.group == "fan"]
                self.assertEqual(len(fans), 2)
                self.assertTrue(all(s.rmin is not None and s.rmax is not None and s.rmin < s.rmax for s in fans))
                self.assertTrue(all(".." in s.detail and "rpm" in s.detail for s in fans))
                self.assertTrue(info["cpu"].startswith("Apple M"))
                groups = {s.group for s in sensors}
                self.assertTrue({"cpu", "gpu", "soc", "storage"} <= groups)

    def test_macbook_battery(self):
        sources, _, _ = build("mac-laptop")
        batt = [s.label for s in sensors_of(sources) if s.group == "battery"]
        self.assertIn("TB0T", batt)
        self.assertTrue(any(b.startswith("gas gauge battery") for b in batt))

    def test_filters(self):
        sources, _, _ = build("linux-laptop")
        kept = tm.apply_filters(sources, [], ["dell_smm"])
        self.assertNotIn("hwmon dell_smm", [src.name for src in kept])
        sources, _, _ = build("linux-laptop")
        kept = tm.apply_filters(sources, ["storage"], [])
        self.assertTrue(all(s.group == "storage" for s in sensors_of(kept)))
        sources, _, _ = build("mac-studio")
        kept = tm.apply_filters(sources, ["smc/Tp*"], [])
        self.assertTrue(sensors_of(kept))
        self.assertTrue(all(s.sid.startswith("smc/Tp") for s in sensors_of(kept)))


class Decoding(unittest.TestCase):
    def test_smc_types(self):
        self.assertAlmostEqual(tm.smc_decode("flt ", struct.pack("<f", 45.25)), 45.25)
        self.assertEqual(tm.smc_decode("sp78", bytes([0x2D, 0x80])), 45.5)
        self.assertEqual(tm.smc_decode("sp78", bytes([0xFF, 0x00])), -1.0)
        self.assertEqual(tm.smc_decode("fpe2", bytes([0x0F, 0xA0])), 1000.0)
        self.assertEqual(tm.smc_decode("ui8 ", bytes([2])), 2)
        self.assertIsNone(tm.smc_decode("ioft", bytes(8)))
        self.assertTrue(tm.smc_is_temp("flt ", 4))
        self.assertTrue(tm.smc_is_temp("sp78", 2))
        self.assertFalse(tm.smc_is_temp("ui32", 4))

    def test_smc_constants(self):
        for key in ("Tf06", "Tf96", "Tf45", "Tf01", "Tf32"):
            self.assertTrue(tm.SMC_ARM_CONSTANT.match(key), key)
        for key in ("Tf04", "Tf0E", "Tf9F", "TfA0", "Tp06", "Tg06"):
            self.assertFalse(tm.SMC_ARM_CONSTANT.match(key), key)

    def test_classification(self):
        self.assertEqual(tm.classify_label("CPU Package"), "cpu")
        self.assertEqual(tm.classify_label("VRM_E"), "vrm")
        self.assertEqual(tm.classify_label("SODIMM 2"), "mem")
        self.assertEqual(tm.classify_label("Video"), "gpu")
        self.assertEqual(tm.classify_label("HDD"), "storage")
        self.assertEqual(tm.classify_label("Ambient"), "ambient")
        self.assertEqual(tm.classify_label("temp3"), "board")
        self.assertEqual(tm.classify_zone("x86_pkg_temp"), "cpu")
        self.assertEqual(tm.classify_zone("acpitz"), "acpi")
        self.assertEqual(tm.classify_zone("INT3400 Thermal"), "board")
        self.assertEqual(tm.smc_classify("TB0T", tm.SMC_ARM), ("battery", "battery"))
        self.assertEqual(tm.smc_classify("TB1T", tm.SMC_ARM), ("battery", "battery sensor 1"))
        self.assertEqual(tm.smc_classify("TB0p", tm.SMC_ARM)[0], "board")
        self.assertEqual(tm.smc_classify("TW0p", tm.SMC_ARM), ("net", "Wi-Fi proximity"))
        self.assertEqual(tm.smc_classify("TH0a", tm.SMC_ARM), ("storage", "NAND SSD a"))
        self.assertEqual(tm.smc_classify("TC0P", tm.SMC_INTEL), ("cpu", "CPU proximity"))
        self.assertEqual(tm.hid_classify("gas gauge battery")[0], "battery")

    def test_small_helpers(self):
        self.assertEqual(tm.short_pci("00000000:01:00.0"), "01:00.0")
        self.assertEqual(tm.short_pci("0000000F:01:00.0"), "000f:01:00.0")
        self.assertEqual(tm.short_gpu_name("NVIDIA GeForce RTX 5090 Laptop GPU"), "RTX 5090 Laptop")
        self.assertEqual(tm.short_gpu_name("NVIDIA RTX 6000 Ada Generation"), "RTX 6000 Ada")
        self.assertEqual(tm.compose_label("nvme0", "Composite", "temp1", False), "nvme0 Composite")
        self.assertEqual(tm.compose_label("DIMM 1", "", "temp1", True), "DIMM 1")
        self.assertEqual(tm.compose_label("dell_smm", "", "temp3", False), "dell_smm temp3")
        self.assertIsNone(tm.sane_limit(65261.85))
        self.assertIsNone(tm.sane_limit(-274.0))
        self.assertEqual(tm.parse_duration("5m"), 300.0)
        self.assertEqual(tm.parse_duration("90"), 90.0)
        self.assertEqual(tm.fmt_duration(300), "5m")
        self.assertEqual(tm.fmt_duration(150), "2.5m")


class StoreTests(unittest.TestCase):
    def make(self, cap=5):
        a = tm.Sensor("a", "a", "cpu", "t")
        b = tm.Sensor("b", "b", "cpu", "t")
        f = tm.Sensor("f", "f", "fan", "t", kind="rpm")
        return tm.Store([a, b, f], cap)

    def test_ring_and_window(self):
        st = self.make(5)
        for i in range(8):
            st.append(i, {"a": 40.0 + i, "b": 30.0, "f": 1000.0})
        self.assertEqual(st.window("a", 5), [43.0, 44.0, 45.0, 46.0, 47.0])
        w = st.window("a", 7)
        self.assertTrue(all(v != v for v in w[:2]))
        self.assertEqual(w[2:], [43.0, 44.0, 45.0, 46.0, 47.0])
        self.assertEqual(st.window("a", 3, end=6), [43.0, 44.0, 45.0])
        self.assertEqual(st.at("a", 7), 47.0)
        self.assertTrue(math.isnan(st.at("a", 1)))        # overwritten

    def test_aggregates_and_validity(self):
        st = self.make(10)
        st.append(0, {"a": 50.0, "b": 0.0, "f": 0.0})     # b=0 C is "absent"; a stopped fan is 0 rpm
        self.assertEqual(st.at("@cpu:max", 0), 50.0)
        self.assertEqual(st.at("@cpu:avg", 0), 50.0)
        self.assertEqual(st.at("f", 0), 0.0)
        self.assertNotIn("b", st.ever)
        self.assertEqual(st.hottest(0), "a")
        st.append(1, {"a": 50.0, "b": 60.0})
        self.assertEqual(st.at("@cpu:avg", 1), 55.0)
        self.assertEqual(st.hottest(1), "b")
        self.assertEqual((st.lo["a"], st.hi["b"]), (50.0, 60.0))


class RenderTests(unittest.TestCase):
    SIZES = ((30, 8), (40, 12), (80, 24), (120, 40), (160, 48), (220, 60))

    def check(self, lines, width, height):
        self.assertLessEqual(len(lines), height)
        for line in lines:
            self.assertLessEqual(line.width, width, line.text())
            self.assertEqual(line.width, tm.dwidth(line.text()))

    def test_every_view_fits_every_size(self):
        for host in HOSTS:
            app = app_for(host)
            for width, height in self.SIZES:
                for mode in ("overview", "group", "all", "help"):
                    with self.subTest(host=host, size=(width, height), mode=mode):
                        app.view.help = mode == "help"
                        app.view.mode = mode if mode in ("group", "all") else "overview"
                        app.view.group = app.store.group_keys[0]
                        if mode == "all":
                            for sm in tm.SORT_MODES:
                                app.view.sort = sm
                                self.check(tm.render_screen(app, width, height), width, height)
                            app.view.sort = "value"
                        else:
                            self.check(tm.render_screen(app, width, height), width, height)
                        if mode == "group" and "fan" in app.store.group_keys:
                            app.view.group = "fan"
                            self.check(tm.render_screen(app, width, height), width, height)
            app.view.help = False

    def test_overview_shows_groups_and_host(self):
        app = app_for("workstation-e")
        lines = tm.render_screen(app, 160, 48)
        text = "\n".join(l.text() for l in lines)
        self.assertIn("workstation-e", lines[0].text())
        for title in ("CPU", "GPU", "Storage", "Memory", "Network", "GPU fans"):
            self.assertIn(" %s " % title, text)
        self.assertTrue(any(0x2800 < ord(c) <= 0x28FF for c in text))     # braille curves

    def test_compact_overview_when_small(self):
        app = app_for("mac-studio")
        lines = tm.render_screen(app, 70, 14)
        self.assertEqual(app.view.grid_cols, 1)
        self.assertTrue(any("CPU" in l.text() for l in lines))

    def test_ascii_glyphs_are_cjk_safe(self):
        tm.set_ambiguous_wide(True)
        try:
            app = app_for("linux-laptop", glyphs="ascii")
            for mode in ("overview", "group", "all"):
                app.view.mode = mode
                app.view.group = "cpu"
                sorts = tm.SORT_MODES if mode == "all" else ("value",)
                for sm in sorts:
                    app.view.sort = sm
                    lines = tm.render_screen(app, 150, 45)
                    self.check(lines, 150, 45)
                    for line in lines:
                        for ch in line.text():
                            self.assertNotIn(unicodedata.east_asian_width(ch), ("A", "W", "F"), repr(ch))
        finally:
            tm.set_ambiguous_wide(False)

    def test_braille_line(self):
        rows = tm.braille_chart([50.0] * 8, 4, 2, 48.0, 52.0, lambda v: "ok")
        self.assertEqual(len(rows), 2)
        drawn = "".join(r.text() for r in rows).replace(" ", "")
        self.assertEqual(len(drawn), 4)                     # one glyph per column, a flat line
        rising = tm.braille_chart([48.0, 52.0], 1, 1, 48.0, 52.0, lambda v: "ok")[0].text()
        self.assertTrue(ord(rising) & 0x40 and ord(rising) & 0x08)   # bottom-left, top-right

    def test_resample_keeps_newest_on_the_right(self):
        nan = float("nan")
        px = tm.resample([nan] * 10 + [1.0, 3.0], 6, "max")
        self.assertTrue(all(v != v for v in px[:4]))
        self.assertEqual(px[-1], 3.0)
        self.assertEqual(tm.resample([1.0, 2.0, 3.0, 4.0], 2, "avg"), [1.5, 3.5])

    def test_fan_axis_never_negative(self):
        lo, hi = tm.y_range([0.0, 0.0], 10.0, 0.0)
        self.assertEqual(lo, 0.0)
        self.assertGreaterEqual(hi, 10.0)
        lo, hi = tm.y_range([40.0, 41.0], 4.0)
        self.assertLess(lo, 40.0)
        self.assertGreater(hi, 41.0)

    def test_fahrenheit(self):
        view = tm.View(tm.Glyphs("unicode"), 1.0, 300.0, 3600.0, fahrenheit=True)
        self.assertEqual(view.fmt(100.0), "212.0°F")
        self.assertEqual(view.fmt(1000.0, "rpm"), "1000 rpm")
        self.assertEqual(view.fmt_delta(20.0, "temp"), "36.0°F")
        self.assertEqual(view.fmt_delta(2.5, "temp", signed=True), "+4.5°F")
        self.assertEqual(view.fmt_delta(-0.01, "temp", signed=True), "+0.0°F")
        self.assertEqual(tm.View(tm.Glyphs("ascii"), 1.0, 300.0, 3600.0).fmt(45.0), "45.0C")

    def test_margin_delta_and_fan_range(self):
        ssd = tm.Sensor("s1", "nvme0", "storage", "nvme", warn=65.0, crit=75.0)
        vrm = tm.Sensor("s2", "VRM", "vrm", "board", warn=90.0, crit=105.0)
        fan = tm.Sensor("f1", "Fan 0", "fan", "smc", kind="rpm", rmin=1000.0, rmax=4000.0)
        st = tm.Store([ssd, vrm, fan], 10)
        st.append(0.0, {"s1": 50.0, "s2": 79.0, "f1": 0.0})
        st.append(1.0, {"s1": 73.0, "s2": 80.0, "f1": 2000.0})
        self.assertAlmostEqual(ssd.margin(73.0), 2.0)
        self.assertAlmostEqual(vrm.margin(80.0), 25.0)
        self.assertTrue(math.isnan(fan.margin(2000.0)))
        self.assertAlmostEqual(fan.fan_pct(0.0), 0.0)
        self.assertAlmostEqual(fan.fan_pct(2000.0), 50.0)
        view = tm.View(tm.Glyphs("unicode"), 1.0, 10.0, 60.0)
        view.sort = "value"
        self.assertEqual([s.sid for s in tm.sorted_sensors(st, view, [ssd, vrm, fan], st.n)], ["s2", "s1", "f1"])
        view.sort = "margin"
        self.assertEqual([s.sid for s in tm.sorted_sensors(st, view, [ssd, vrm, fan], st.n)], ["s1", "s2", "f1"])
        view.sort = "delta"
        self.assertEqual([s.sid for s in tm.sorted_sensors(st, view, [ssd, vrm, fan], st.n)], ["s1", "s2", "f1"])
        view.sort = "name"
        self.assertEqual([s.sid for s in tm.sorted_sensors(st, view, [ssd, vrm, fan], st.n)], ["s1", "s2", "f1"])
        # backward-compatible 12-item and new 14-item agent sensor rows
        rebuilt = tm.sensor_from_row(tm.sensor_row(fan))
        self.assertEqual((rebuilt.rmin, rebuilt.rmax), (1000.0, 4000.0))
        legacy = tm.sensor_from_row(tm.sensor_row(fan)[:12])
        self.assertEqual((legacy.rmin, legacy.rmax), (None, None))


class KeyTests(unittest.TestCase):
    def test_navigation(self):
        import curses
        app = app_for("workstation-e")
        tm.render_screen(app, 160, 48)
        v = app.view
        self.assertTrue(tm.handle_key(app, curses.KEY_RIGHT))
        self.assertEqual(v.panel, 1)
        tm.handle_key(app, curses.KEY_LEFT)
        self.assertTrue(tm.handle_key(app, 10))
        self.assertEqual((v.mode, v.group), ("group", "cpu"))
        tm.render_screen(app, 160, 48)
        first = v.sel
        tm.handle_key(app, curses.KEY_DOWN)
        self.assertNotEqual(v.sel, first)
        tm.handle_key(app, 27)
        self.assertEqual(v.mode, "overview")
        tm.handle_key(app, ord("a"))
        self.assertEqual(v.mode, "all")
        for expected_sort in ("margin", "delta", "name", "value"):
            tm.handle_key(app, ord("s"))
            self.assertEqual(v.sort, expected_sort)
            RenderTests.check(self, tm.render_screen(app, 160, 48), 160, 48)
        w = v.window
        tm.handle_key(app, ord("+"))
        self.assertLess(v.window, w)
        for _ in range(10):
            tm.handle_key(app, ord("-"))
        self.assertLessEqual(v.window, v.history)
        tm.handle_key(app, ord("u"))
        self.assertTrue(v.fahrenheit)
        tm.handle_key(app, ord("p"))
        self.assertEqual(v.frozen, {id(app.store): app.store.n})
        self.assertFalse(tm.handle_key(app, ord("q")))

    def test_filter_hot_and_pin(self):
        import curses
        app = app_for("workstation-a")
        v = app.view
        tm.handle_key(app, ord("a"))
        self.assertEqual(v.mode, "all")
        total = len(app.store.visible())
        # start live search with '/'; typing 'q' inside search must not quit
        tm.handle_key(app, ord("/"))
        self.assertTrue(v.filter_editing)
        for ch in "nvmeq":
            self.assertTrue(tm.handle_key(app, ord(ch)))
        self.assertEqual(v.filter_text, "nvmeq")
        # unmatched filter in 'all' mode stays in 'all' mode and shows warning
        lines = tm.render_screen(app, 160, 48)
        RenderTests.check(self, lines, 160, 48)
        self.assertEqual(v.mode, "all")
        self.assertIn("no sensors match active filter", "\n".join(l.text() for l in lines))
        # backspace 'q' -> "nvme", then Enter to keep filter
        tm.handle_key(app, curses.KEY_BACKSPACE)
        self.assertEqual(v.filter_text, "nvme")
        tm.handle_key(app, 10)
        self.assertFalse(v.filter_editing)
        vis = tm.view_visible(app.store, v, app.store.n)
        self.assertGreater(len(vis), 0)
        self.assertLess(len(vis), total)
        lines = tm.render_screen(app, 160, 48)
        RenderTests.check(self, lines, 160, 48)
        self.assertIn("/nvme", lines[0].text())
        # pin the second sensor in the filtered list and verify it moves to the top
        ordered = tm.sorted_sensors(app.store, v, tm.list_sensors(app.store, v, app.store.n), app.store.n)
        target = ordered[-1].sid
        v.sel = target
        tm.handle_key(app, ord("f"))
        self.assertIn(target, v.pinned)
        reordered = tm.sorted_sensors(app.store, v, tm.list_sensors(app.store, v, app.store.n), app.store.n)
        self.assertEqual(reordered[0].sid, target)
        # return to overview, clear filter with Esc, and check Pinned panel exists
        tm.handle_key(app, 27)
        self.assertEqual(v.mode, "overview")
        tm.handle_key(app, 27)
        self.assertEqual(v.filter_text, "")
        lines = tm.render_screen(app, 160, 48)
        RenderTests.check(self, lines, 160, 48)
        self.assertEqual(v.panels[0], "pinned")
        self.assertIn("Pinned", "\n".join(l.text() for l in lines))
        # unpin from 'Pinned' group list while a filter is active -> returns to overview when Pinned becomes empty
        v.filter_text = "nvme"
        v.panel = 0
        tm.handle_key(app, 10)
        self.assertEqual((v.mode, v.group), ("group", "pinned"))
        lines = tm.render_screen(app, 160, 48)
        RenderTests.check(self, lines, 160, 48)
        tm.handle_key(app, ord("f"))
        self.assertNotIn(target, v.pinned)
        lines = tm.render_screen(app, 160, 48)
        self.assertEqual(v.mode, "overview")
        v.filter_text = ""
        # empty filter fits all terminal sizes (including 30x8 and 40x12)
        v.filter_text = "nonexistent_sensor_xyz"
        for width, height in RenderTests.SIZES:
            v.mode = "overview"
            RenderTests.check(self, tm.render_screen(app, width, height), width, height)
            v.mode = "all"
            RenderTests.check(self, tm.render_screen(app, width, height), width, height)
        v.filter_text, v.mode = "", "overview"
        # mixed fan kinds (pct + rpm) in Pinned panel stay homogeneous in chart_members
        f_pct = next(s for s in app.store.visible() if s.kind == "pct")
        f_rpm = next(s for s in app.store.visible() if s.kind == "rpm")
        v.pinned = {f_pct.sid, f_rpm.sid}
        specs = dict(tm.overview_specs(app.store, v, app.store.n))
        self.assertIn("pinned", specs)
        if specs["pinned"].kind == "pct":
            self.assertLessEqual(specs["pinned"].value, 100.0)
        v.pinned.clear()
        # hot_only filter ('w')
        tm.handle_key(app, ord("w"))
        self.assertTrue(v.hot_only)
        RenderTests.check(self, tm.render_screen(app, 160, 48), 160, 48)
        tm.handle_key(app, ord("w"))
        self.assertFalse(v.hot_only)

    def test_time_scrubber_and_mouse(self):
        import curses
        s1 = tm.Sensor("t1", "CPU", "cpu", "hwmon", warn=80.0, crit=95.0)
        s2 = tm.Sensor("t2", "GPU", "gpu", "nvml", warn=80.0, crit=90.0)
        # ring buffer wrap-around with historical end < store.n
        wrap_st = tm.Store([s1], 5)
        for i in range(8):
            wrap_st.append(float(i), {"t1": 10.0 * (i + 1)})
        win = wrap_st.window("t1", 5, end=4)
        self.assertTrue(all(x != x for x in win[:4]))
        self.assertEqual(win[4], 40.0)
        wrap_view = tm.View(tm.Glyphs("unicode"), 1.0, 5.0, 5.0)
        wrap_view.frozen = {id(wrap_st): 1}
        self.assertEqual(tm.view_end(wrap_view, wrap_st), 4)

        st = tm.Store([s1, s2], 50)
        t0 = 1700000000.0
        for i in range(20):
            st.append(t0 + i, {"t1": 40.0 + i, "t2": 50.0 + i})
        view = tm.View(tm.Glyphs("unicode"), 1.0, 16.0, 60.0)
        app = tm.App([], st, None, {"host": "box"}, [], view)
        # '[' automatically freezes and steps back by samples() // 4 == 4
        tm.handle_key(app, ord("["))
        self.assertIsNotNone(view.frozen)
        self.assertEqual(tm.view_end(view, st), 16)
        lines = tm.render_screen(app, 120, 36)
        RenderTests.check(self, lines, 120, 36)
        self.assertIn("PAUSED -4s", lines[0].text())
        # '{' and '}' step by 1 sample
        tm.handle_key(app, ord("{"))
        self.assertEqual(tm.view_end(view, st), 15)
        tm.handle_key(app, ord("}"))
        self.assertEqual(tm.view_end(view, st), 16)
        tm.handle_key(app, ord("]"))
        self.assertEqual(tm.view_end(view, st), 20)
        # 'p' unfreezes
        tm.handle_key(app, ord("p"))
        self.assertIsNone(view.frozen)
        # mouse click on second panel in overview, then double-click to enter group
        tm.render_screen(app, 120, 36)
        _, y0, cols, rows, pw, ph, gap, _ = view.grid_geom
        r1, c1 = 1 // cols, 1 % cols
        mx1, my1 = c1 * (pw + gap) + 2, y0 + r1 * ph + 2
        b1_click = getattr(curses, "BUTTON1_CLICKED", 4)
        b1_dbl = getattr(curses, "BUTTON1_DOUBLE_CLICKED", 8)
        tm.handle_key(app, curses.KEY_MOUSE, mouse=(0, mx1, my1, 0, b1_click))
        self.assertEqual(view.panel, 1)
        tm.handle_key(app, curses.KEY_MOUSE, mouse=(0, mx1, my1, 0, b1_dbl))
        self.assertEqual((view.mode, view.group), ("group", "gpu"))
        # mouse click and wheel in list mode (and footer click is ignored)
        tm.handle_key(app, ord("a"))
        tm.render_screen(app, 120, 36)
        self.assertIsNotNone(view.list_y0)
        tm.handle_key(app, curses.KEY_MOUSE, mouse=(0, 5, view.list_y0 + 1, 0, b1_click))
        ordered = tm.sorted_sensors(st, view, tm.list_sensors(st, view, st.n), st.n)
        self.assertEqual(view.sel, ordered[1].sid)
        tm.handle_key(app, curses.KEY_MOUSE, mouse=(0, 5, 35, 0, b1_click))
        self.assertEqual(view.sel, ordered[1].sid)
        b4 = getattr(curses, "BUTTON4_PRESSED", 0x80000)
        tm.handle_key(app, curses.KEY_MOUSE, mouse=(0, 5, view.list_y0 + 1, 0, b4))
        self.assertEqual(view.sel, ordered[0].sid)

    def test_power_and_throttling_channels(self):
        # 1. NVML throttle bitmask decoding and power channels
        self.assertEqual(tm.decode_nvml_throttle(0x40, 2), (["HW_THERM!", "P2"], "crit"))
        self.assertEqual(tm.decode_nvml_throttle(0x84, 0), (["PWR_BRAKE!", "PWR_CAP", "P0"], "crit"))
        self.assertEqual(tm.decode_nvml_throttle(0x04, 0), (["PWR_CAP", "P0"], "dim"))
        self.assertEqual(tm.decode_nvml_throttle(0x01, 8), ([], None))
        snap = {"driver": "570.00", "gpus": [
            {
                "index": 0, "name": "NVIDIA GeForce RTX 4090", "pci": "01:00.0",
                "thresholds": {"slowdown": 88, "shutdown": 93, "gpu_max": 84},
                "memtemp": False, "fans": 1, "power": True, "power_limit": 450.0,
                "temp_value": 85.0, "fan_values": [78], "power_value": 360.0,
                "pstate": 0, "throttle_reasons": 0x24,
            },
            {
                "index": 1, "name": "NVIDIA GeForce RTX 4090", "pci": "02:00.0",
                "thresholds": {"slowdown": 88, "shutdown": 93, "gpu_max": 84},
                "memtemp": False, "fans": 1, "power": True, "power_limit": 450.0,
                "temp_value": 52.0, "fan_values": [30], "power_value": 90.0,
                "pstate": 8, "throttle_reasons": 0x01,
            },
        ]}
        nv_src = tm.NvmlSource(tm.FakeNvmlApi(snap))
        vals = nv_src.poll(0.0, 1.0, force=True)
        pwr = next(s for s in nv_src.sensors if s.kind == "watt" and s.label.startswith("GPU0"))
        self.assertEqual((pwr.group, pwr.rmax), ("power", 450.0))
        self.assertAlmostEqual(pwr.fan_pct(vals[pwr.sid]), 80.0)
        self.assertEqual(nv_src.throttles, [("GPU0 SW_THERM/PWR_CAP/P0", "warn")])

        # 2. Linux hwmon power*_input / power*_cap and CPU thermal_throttle delta + slow source priority
        fs_data = {
            "glob": {
                "/sys/class/hwmon/hwmon*": ["/sys/class/hwmon/hwmon0", "/sys/class/hwmon/hwmon1"],
                "/sys/class/hwmon/hwmon0/temp*_input": ["/sys/class/hwmon/hwmon0/temp1_input"],
                "/sys/class/hwmon/hwmon0/fan*_input": [],
                "/sys/class/hwmon/hwmon0/power*_input": [],
                "/sys/class/hwmon/hwmon0/power*_average": [],
                "/sys/class/hwmon/hwmon1/temp*_input": ["/sys/class/hwmon/hwmon1/temp1_input",
                                                        "/sys/class/hwmon/hwmon1/temp2_input"],
                "/sys/class/hwmon/hwmon1/fan*_input": [],
                "/sys/class/hwmon/hwmon1/power*_input": ["/sys/class/hwmon/hwmon1/power1_input"],
                "/sys/class/hwmon/hwmon1/power*_average": [],
                "/sys/class/thermal/thermal_zone*": [],
                "/sys/class/power_supply/*": [],
                "/sys/devices/system/cpu/cpu*/thermal_throttle/package_throttle_count": [
                    "/sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count",
                    "/sys/devices/system/cpu/cpu1/thermal_throttle/package_throttle_count",
                ],
                "/sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count": [],
            },
            "read": {
                "/sys/class/hwmon/hwmon0/name": "dell_ddv",
                "/sys/class/hwmon/hwmon0/temp1_label": "CPU",
                "/sys/class/hwmon/hwmon0/temp1_input": "60000",
                "/sys/class/hwmon/hwmon1/name": "zenpower",
                "/sys/class/hwmon/hwmon1/temp1_label": "Tdie",
                "/sys/class/hwmon/hwmon1/temp1_input": "72500",
                "/sys/class/hwmon/hwmon1/temp1_max": "90000",
                "/sys/class/hwmon/hwmon1/temp1_crit": "95000",
                "/sys/class/hwmon/hwmon1/temp2_label": "NVMe SSD",
                "/sys/class/hwmon/hwmon1/temp2_input": "48000",
                "/sys/class/hwmon/hwmon1/power1_label": "SVI2_P_Core",
                "/sys/class/hwmon/hwmon1/power1_input": "125500000",
                "/sys/class/hwmon/hwmon1/power1_cap": "251000000",
                "/sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count": "10",
                "/sys/devices/system/cpu/cpu1/thermal_throttle/package_throttle_count": "10",
            },
            "real": {"/sys/class/hwmon/hwmon0": "/sys/devices/platform/dell_ddv/hwmon/hwmon0",
                     "/sys/class/hwmon/hwmon0/device": "/sys/devices/platform/dell_ddv",
                     "/sys/class/hwmon/hwmon1": "/sys/devices/pci0000:00/0000:00:18.3/hwmon/hwmon1",
                     "/sys/class/hwmon/hwmon1/device": "/sys/devices/pci0000:00/0000:00:18.3"},
            "exists": {"/sys/class/hwmon/hwmon0/device": True, "/sys/class/hwmon/hwmon1/device": True},
            "list": {"/sys/class/hwmon/hwmon0/device/net": [], "/sys/class/hwmon/hwmon1/device/net": []},
        }
        rfs = tm.ReplayFS(fs_data)
        l_sources, _ = tm.linux_sources(rfs, nvml=tm.FakeNvmlApi(snap))
        slow_src = next(src for src in l_sources if src.name == "hwmon dell_ddv")
        cpu_src = next(src for src in l_sources if src.name == "hwmon zenpower")
        self.assertEqual(slow_src.throttle_paths, [])
        self.assertTrue(cpu_src.throttle_paths)
        # apply_filters preserves throttle_paths on CPU source when dell_ddv is excluded,
        # and drops throttle_paths when only storage is included
        kept_no_ddv = tm.apply_filters([tm.FileSource(s.name, rfs, list(s.entries), s.every, throttle_paths=s.throttle_paths)
                                        for s in (slow_src, cpu_src)], [], ["dell_ddv"])
        self.assertEqual(len(kept_no_ddv), 1)
        self.assertTrue(kept_no_ddv[0].throttle_paths)
        kept_power = tm.apply_filters([tm.FileSource(s.name, rfs, list(s.entries), s.every, throttle_paths=s.throttle_paths)
                                       for s in (slow_src, cpu_src)], ["*/power*"], [])
        self.assertEqual(len(kept_power), 1)
        self.assertEqual(kept_power[0].throttle_paths, [])

        cpu_vals = cpu_src.poll(0.0, 1.0, force=True)
        cpu_pwr = next(s for s in cpu_src.sensors if s.kind == "watt")
        self.assertEqual((cpu_pwr.group, cpu_pwr.rmax), ("power", 251.0))
        self.assertAlmostEqual(cpu_vals[cpu_pwr.sid], 125.5)
        self.assertAlmostEqual(cpu_pwr.fan_pct(cpu_vals[cpu_pwr.sid]), 50.0)
        self.assertEqual(cpu_src.throttles, [])
        # Increment package throttle counter on both cores from 10 -> 13 (+3, not +6)
        fs_data["read"]["/sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count"] = "13"
        fs_data["read"]["/sys/devices/system/cpu/cpu1/thermal_throttle/package_throttle_count"] = "13"
        cpu_src.poll(1.0, 1.0, force=True)
        self.assertEqual(cpu_src.throttles, [("CPU THROTTLED (+3)", "crit")])

        # 3. macOS SMC power keys and thermal_state (including live mac_thermal_state on darwin)
        smc_rows = [
            ["Tp01", "flt ", struct.pack("<f", 55.25).hex()],
            ["PSTR", "flt ", struct.pack("<f", 42.5).hex()],
            ["PCPC", "flt ", struct.pack("<f", 18.25).hex()],
        ]
        m_sources, _ = tm.mac_sources(smc=tm.FakeSMC(smc_rows, thermal_state=2), hid=None, arch="arm64")
        m_src = m_sources[0]
        m_vals = m_src.poll(0.0, 1.0, force=True)
        self.assertIn("smc/PSTR", m_vals)
        self.assertAlmostEqual(m_vals["smc/PSTR"], 42.5)
        self.assertEqual(m_src.throttles, [("THERMAL: SERIOUS", "crit")])
        if sys.platform == "darwin":
            self.assertIn(tm.mac_thermal_state(), (0, 1, 2, 3))

        # 4. End-to-end rendering, per-sensor throttle attribution, Recorder CSV, CJK safety, and Fleet
        sensors = sensors_of(l_sources)
        st = tm.Store(sensors, 30)
        view = tm.View(tm.Glyphs("unicode"), 1.0, 60.0, 300.0)
        col = tm.Collector(l_sources, st, 1.0)
        app = tm.App(l_sources, st, col, {"host": "workstation"}, [], view)
        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "power.csv")
            rec = tm.Recorder(csv_path, sensors)
            col.listeners.append(rec)
            col.sample(force=True)
            rec.close()
            with open(csv_path, newline="") as f:
                crows = list(csv.reader(f))
            pwr_idx = next(i for i, h in enumerate(crows[0]) if pwr.sid in h)
            self.assertEqual(crows[1][pwr_idx], "360.00")
        for width, height in RenderTests.SIZES:
            for mode in ("overview", "group", "all"):
                view.mode = mode
                view.group = "power"
                RenderTests.check(self, tm.render_screen(app, width, height), width, height)
        # Per-GPU throttle attribution in list view: GPU0 shows throttle, GPU1 does not, GPU0 power shows GPU0 throttle
        view.mode = "group"
        view.group = "gpu"
        g0 = next(s for s in sensors if s.group == "gpu" and s.label.startswith("GPU0"))
        g1 = next(s for s in sensors if s.group == "gpu" and s.label.startswith("GPU1"))
        view.sel = g0.sid
        g0_text = "\n".join(l.text() for l in tm.render_screen(app, 160, 48)[1:18])
        self.assertIn("GPU0 SW_THERM/PWR_CAP/P0", g0_text)
        view.sel = g1.sid
        g1_text = "\n".join(l.text() for l in tm.render_screen(app, 160, 48)[1:18])
        self.assertNotIn("GPU0 SW_THERM/PWR_CAP/P0", g1_text)
        view.group = "power"
        view.sel = pwr.sid
        p0_text = "\n".join(l.text() for l in tm.render_screen(app, 160, 48)[1:18])
        self.assertIn("GPU0 SW_THERM/PWR_CAP/P0", p0_text)
        # CJK ASCII width safety with active power & throttles
        tm.set_ambiguous_wide(True)
        try:
            view.glyphs = tm.Glyphs("ascii")
            for mode in ("overview", "group", "all"):
                view.mode = mode
                view.group = "power"
                lines = tm.render_screen(app, 150, 45)
                RenderTests.check(self, lines, 150, 45)
                for line in lines:
                    for ch in line.text():
                        self.assertNotIn(unicodedata.east_asian_width(ch), ("A", "W", "F"), repr(ch))
        finally:
            tm.set_ambiguous_wide(False)
            view.glyphs = tm.Glyphs("unicode")
        view.mode = "overview"
        ov_text = "\n".join(l.text() for l in tm.render_screen(app, 160, 48))
        self.assertIn("Power", ov_text)
        self.assertIn("CPU THROTTLED (+3)", ov_text)
        self.assertIn("GPU0 SW_THERM/PWR_CAP/P0", ov_text)
        once_text = "\n".join(l.text() for l in tm.render_once(app))
        self.assertIn("CPU THROTTLED (+3)", once_text)
        self.assertIn("360.0 W", once_text)
        snap_j = tm.snapshot_json(app)
        self.assertIn("CPU THROTTLED (+3)", snap_j["throttles"])
        self.assertTrue(any(s["kind"] == "watt" and s.get("power_pct") == 80.0 for s in snap_j["sensors"]))
        out = io.StringIO()
        tm.run_agent(app, out=out, count=2)
        link = tm.HostLink("workstation", "workstation", 1.0, 30, b"")
        for line in out.getvalue().splitlines():
            link.handle(json.loads(line))
        self.assertTrue(any(s.kind == "watt" for s in link.store.sensors))
        self.assertIn(("CPU THROTTLED (+3)", "crit"), link.throttles)
        fview = tm.View(tm.Glyphs("unicode"), 1.0, 60.0, 300.0)
        fview.mode = "fleet"
        fapp = tm.App([], None, None, {"host": "fleet"}, [], fview, hosts=[link])
        for width, height in RenderTests.SIZES:
            RenderTests.check(self, tm.render_screen(fapp, width, height), width, height)
        fonce_text = "\n".join(l.text() for l in tm.render_fleet_once(fapp))
        self.assertIn("CPU THROTTLED (+3)", fonce_text)
        fj = tm.fleet_json(fapp)
        self.assertIn("CPU THROTTLED (+3)", fj["hosts"][0]["throttles"])
        # Reconnect meta clears stale throttles until next sample
        link.handle({"type": "meta", "version": tm.VERSION, "interval": 1.0, "info": {"host": "workstation"},
                     "notes": [], "sensors": [tm.sensor_row(s) for s in sensors]})
        self.assertEqual(link.throttles, [])

    def test_smc_descriptions_and_hidden_reasons(self):
        # 1. mac-laptop: parked keys (`Tp00` @ 40 C) and below-floor keys (`Ta01` @ 8.6 C), plus exact SMC detail
        sources, notes, info = build("mac-laptop")
        sensors = sensors_of(sources)
        store = tm.Store(sensors, 60, show_all=True)
        collector = tm.Collector(sources, store, 1.0)
        collector.sample(force=True)
        by_label = {s.label: s for s in sensors}
        self.assertEqual(by_label["TW0P"].detail, "SMC TW0P Wi-Fi proximity")
        self.assertEqual(by_label["TH0a"].detail, "SMC TH0a NAND SSD a")
        self.assertEqual(tm.hidden_reason(store, by_label["Tp00"]), "parked (40 C)")
        self.assertEqual(tm.hidden_reason(store, by_label["Ta01"]), "below 15 C floor (8.6 C)")
        self.assertEqual(tm.hidden_reason(store, by_label["Ta00"]), "unconnected (0 C)")
        self.assertEqual(tm.hidden_reason(store, by_label["Tp1i"]), "placeholder (-4 C)")
        self.assertEqual(tm.hidden_reason(store, by_label["Te04"]), "")

        # 2. workstation-c: asusec T_Sensor (-40 C placeholder)
        s5090, _, _ = build("workstation-c")
        st5090 = tm.Store(sensors_of(s5090), 60, show_all=True)
        tm.Collector(s5090, st5090, 1.0).sample(force=True)
        tsens = {s.label: s for s in st5090.sensors}["asusec T_Sensor"]
        self.assertEqual(tm.hidden_reason(st5090, tsens), "placeholder (-40 C)")

        # 3. workstation-a: dell_ddv 0 C unconnected and ENETDOWN no reading
        s6000, _, _ = build("workstation-a")
        st6000 = tm.Store(sensors_of(s6000), 60, show_all=True)
        tm.Collector(s6000, st6000, 1.0).sample(force=True)
        l6000 = {s.label: s for s in st6000.sensors}
        zero_ddv = [s for s in st6000.sensors if s.source == "hwmon dell_ddv" and s.sid not in st6000.ever][0]
        self.assertEqual(tm.hidden_reason(st6000, zero_ddv), "unconnected (0 C)")
        self.assertEqual(tm.hidden_reason(st6000, l6000["wifi phy0"]), "no reading")

        # 4. Watt / custom out-of-range / near-zero ADC noise (`mac-studio`) / iohid floor hidden reasons
        s_studio, _, _ = build("mac-studio")
        st_studio = tm.Store(sensors_of(s_studio), 60, show_all=True)
        tm.Collector(s_studio, st_studio, 1.0).sample(force=True)
        l_studio = {s.label: s for s in st_studio.sensors}
        self.assertEqual(tm.hidden_reason(st_studio, l_studio["Tf20"]), "unconnected (0 C)")
        self.assertEqual(tm.hidden_reason(st_studio, l_studio["Tf00"]), "unconnected (0 C)")
        pw0 = tm.Sensor("p0", "Idle Pwr", "power", "hwmon", kind="watt", lo=0.0, hi=500.0)
        pw_hi = tm.Sensor("phi", "Spike Pwr", "power", "hwmon", kind="watt", lo=0.0, hi=500.0)
        t_hi = tm.Sensor("thi", "Bogus Temp", "cpu", "hwmon", kind="temp", lo=0.0, hi=150.0)
        t_hid = tm.Sensor("thid", "HID Temp", "soc", "iohid", kind="temp", lo=tm.MAC_FLOOR, hi=150.0)
        t_pk_hi = tm.Sensor("tpk", "Parked Hi", "cpu", "smc", kind="temp", lo=tm.MAC_FLOOR, hi=150.0)
        t_pk_hi.parked_at = 200.0
        st_pw = tm.Store([pw0, pw_hi, t_hi, t_hid, t_pk_hi], 10, show_all=True)
        st_pw.append(1.0, {"p0": -0.001, "phi": 900.0, "thi": 250.0, "thid": 10.0})
        self.assertEqual(tm.hidden_reason(st_pw, pw0), "inactive (0 W)")
        self.assertEqual(tm.hidden_reason(st_pw, pw_hi), "out of range (900 W)")
        self.assertEqual(tm.hidden_reason(st_pw, t_hi), "out of range (250 C)")
        self.assertEqual(tm.hidden_reason(st_pw, t_hid), "below 15 C floor (10 C)")
        self.assertEqual(tm.hidden_reason(st_pw, t_pk_hi), "out of range (200 C)")

        # 5. Live search `/parked` matches hidden_reason in --all mode
        view = tm.View(tm.Glyphs("unicode"), 1.0, 120.0, 3600.0)
        app = tm.App(sources, store, collector, info, notes, view)
        view.mode = "all"
        view.filter_text = "parked"
        vis_Parked = tm.view_visible(store, view)
        self.assertTrue(vis_Parked)
        self.assertTrue(all(tm.hidden_reason(store, s).startswith("parked") for s in vis_Parked))
        list_text = "\n".join(l.text() for l in tm.render_screen(app, 140, 40))
        self.assertIn("[parked (40 C)]", list_text)
        view.filter_text = ""

        # 6. render_once and snapshot_json include hidden_reason in --all mode
        once_text = "\n".join(l.text() for l in tm.render_once(app))
        self.assertIn("[parked (40 C)]", once_text)
        self.assertIn("[below 15 C floor (8.6 C)]", once_text)
        snap = tm.snapshot_json(app)
        by_id_json = {r["id"]: r for r in snap["sensors"]}
        self.assertEqual(by_id_json["smc/Tp00"].get("hidden_reason"), "parked (40 C)")
        self.assertEqual(by_id_json["smc/Ta01"].get("hidden_reason"), "below 15 C floor (8.6 C)")
        self.assertNotIn("hidden_reason", by_id_json["smc/Te04"])

        # 7. CJK ASCII width safety with show_all=True
        tm.set_ambiguous_wide(True)
        try:
            app_ascii = tm.App(sources, store, collector, info, notes,
                               tm.View(tm.Glyphs("ascii"), 1.0, 120.0, 3600.0))
            app_ascii.view.mode = "all"
            lines = tm.render_screen(app_ascii, 120, 40)
            RenderTests.check(self, lines, 120, 40)
            for line in lines:
                for ch in line.text():
                    self.assertNotIn(unicodedata.east_asian_width(ch), ("A", "W", "F"), repr(ch))
        finally:
            tm.set_ambiguous_wide(False)

        # 8. Fleet --agent round-trip preserves parked_at (`pk`) and raw_last hidden reasons
        out = io.StringIO()
        tm.run_agent(app, out=out, count=1)
        link = tm.HostLink("m2x", "m2x", 1.0, 60, b"", show_all=True)
        for line in out.getvalue().splitlines():
            link.handle(json.loads(line))
        l_by_label = {s.label: s for s in link.store.sensors}
        self.assertEqual(tm.hidden_reason(link.store, l_by_label["Tp00"]), "parked (40 C)")
        self.assertEqual(tm.hidden_reason(link.store, l_by_label["Ta01"]), "below 15 C floor (8.6 C)")


def fleet_app(hosts=("workstation-a", "spark-1", "mac-studio"), samples=3, down=("workstation-e",)):
    """A --fleet App whose links were fed by real agent output (replayed fixtures)."""
    links = []
    for host in hosts:
        agent = app_for(host, samples=1, interval=0.01)
        out = io.StringIO()
        tm.run_agent(agent, out=out, count=samples)
        link = tm.HostLink(host, host, 1.0, 600, b"")
        for line in out.getvalue().splitlines():
            link.handle(json.loads(line))
        links.append(link)
    for host in down:
        link = tm.HostLink(host, host, 1.0, 600, b"")
        link.state, link.error = "down", "ssh: connect to host %s port 22: Operation timed out" % host
        links.append(link)
    view = tm.View(tm.Glyphs("unicode"), 1.0, 120.0, 3600.0)
    view.mode = "fleet"
    return tm.App([], None, None, {"host": "fleet"}, [], view, hosts=links)


class FleetTests(unittest.TestCase):
    def test_agent_round_trip(self):
        agent = app_for("workstation-c", samples=1, interval=0.01)
        out = io.StringIO()
        tm.run_agent(agent, out=out, count=3)
        msgs = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual(msgs[0]["type"], "meta")
        self.assertEqual([m["type"] for m in msgs].count("s"), 3)
        link = tm.HostLink("box", "box", 1.0, 100, b"")
        for m in msgs:
            link.handle(m)
        self.assertEqual(link.state, "up")
        self.assertEqual(link.store.n, 3)
        self.assertEqual(link.info["host"], "box")
        self.assertEqual(link.info["hostname"], "workstation-c")
        self.assertEqual([s.label for s in link.store.sensors], [s.label for s in agent.store.sensors])
        self.assertEqual(set(link.store.ever), set(agent.store.ever))      # same validity rules
        self.assertTrue(link.sources)
        # a reconnect with the same sensors keeps the history
        link.handle(msgs[0])
        link.handle(msgs[1])
        self.assertEqual(link.store.n, 4)

    def test_fleet_views_fit(self):
        app = fleet_app()
        for width, height in RenderTests.SIZES:
            with self.subTest(size=(width, height)):
                app.host, app.view.mode, app.view.help = None, "fleet", False
                RenderTests.check(self, tm.render_screen(app, width, height), width, height)
                app.view.help = True
                RenderTests.check(self, tm.render_screen(app, width, height), width, height)

    def test_fleet_navigation(self):
        import curses
        app = fleet_app()
        text = "\n".join(l.text() for l in tm.render_screen(app, 160, 48))
        for host in ("workstation-a", "spark-1", "mac-studio", "workstation-e"):
            self.assertIn(host, text)
        self.assertIn("down", text)
        self.assertIn("Operation timed out", text)
        v = app.view
        tm.handle_key(app, curses.KEY_RIGHT)
        tm.handle_key(app, 10)                           # open spark-1
        self.assertEqual((app.host.name, v.mode), ("spark-1", "overview"))
        lines = tm.render_screen(app, 160, 48)
        self.assertIn("spark-1", lines[0].text())
        self.assertIn("ACPI zones", "\n".join(l.text() for l in lines))
        tm.handle_key(app, 10)                           # its first group
        self.assertEqual(v.mode, "group")
        tm.handle_key(app, 27)
        self.assertEqual(v.mode, "overview")
        tm.handle_key(app, 27)                           # back to the fleet
        self.assertEqual((app.host, v.mode), (None, "fleet"))
        for _ in range(3):
            tm.handle_key(app, curses.KEY_RIGHT)
        tm.handle_key(app, 10)                           # a machine that never answered
        self.assertIsNone(app.host)
        self.assertIn("no data", v.message)
        tm.handle_key(app, ord("p"))
        self.assertEqual(len(v.frozen), 3)
        tm.handle_key(app, ord("r"))
        self.assertIn("reset", v.message)

    def test_fleet_once_table(self):
        app = fleet_app()
        text = "\n".join(l.text() for l in tm.render_fleet_once(app))
        self.assertIn("workstation-a", text)
        self.assertIn("Operation timed out", text)
        self.assertIn("CPU", text.splitlines()[2])
        data = tm.fleet_json(app)
        self.assertEqual([h["state"] for h in data["hosts"]], ["up", "up", "up", "down"])
        self.assertTrue(data["hosts"][0]["sensors"])
        self.assertIn("timed out", data["hosts"][3]["error"])

    def test_load_fleet(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "fleet.json")
            with open(path, "w") as f:
                json.dump({"hosts": ["a", {"name": "b", "target": "user@b"},
                                     {"name": "c", "disabled": True}, {"target": "local"}]}, f)
            self.assertEqual(tm.load_fleet("", d), [("a", "a"), ("b", "user@b"), ("local", "local")])
            self.assertEqual(tm.load_fleet(path, "/nowhere"), tm.load_fleet("", d))
        self.assertEqual(tm.load_fleet("x, y", "/nowhere"), [("x", "x"), ("y", "y")])
        example = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                               "examples", "thermalmaster.fleet.json")
        shipped = tm.load_fleet(example, "/nowhere")
        self.assertIn(("gpu-a", "gpu-a"), shipped)
        for laptop in ("mac-laptop", "linux-laptop"):
            self.assertNotIn(laptop, [h for h, _ in shipped])
        with_laptops = tm.load_fleet(example, "/nowhere", mobile=True)
        self.assertEqual(len(with_laptops), len(shipped) + 2)
        for laptop in ("mac-laptop", "linux-laptop"):
            self.assertIn((laptop, laptop), with_laptops)
        # an explicit list is taken as given, mobile or not
        self.assertEqual(tm.load_fleet("mac-laptop", "/nowhere"), [("mac-laptop", "mac-laptop")])

    def test_mobile_flag_only_affects_the_fleet_list(self):
        # a laptop still reports every sensor when thermalmaster runs on it (local mode)
        sources, _, info = build("linux-laptop")
        self.assertGreater(len(sensors_of(sources)), 50)
        self.assertTrue(info.get("model"))

    def test_ssh_command(self):
        link = tm.HostLink("g", "spark-1", 2.0, 10, b"", ["--exclude", "dell smm"])
        cmd = link.command()
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("BatchMode=yes", cmd)
        self.assertEqual(cmd[-2], "spark-1")
        self.assertEqual(cmd[-1], "exec python3 - --agent --interval 2 --exclude 'dell smm'")
        self.assertEqual(tm.HostLink("l", "local", 1.0, 10, b"").command()[0], sys.executable)

    def test_fleet_sort_and_peak_alerts(self):
        # 1. Single-host over-temp peak memory & bell alerts
        s_cpu = tm.Sensor("c0", "CPU0", "cpu", "hwmon", warn=80.0, crit=90.0)
        s_gpu = tm.Sensor("g0", "GPU0", "gpu", "nvml", warn=75.0, crit=85.0)
        st = tm.Store([s_cpu, s_gpu], 60)
        view = tm.View(tm.Glyphs("unicode"), 1.0, 120.0, 3600.0)
        app = tm.App([], st, None, {"host": "rig"}, [], view)
        st.append(100.0, {"c0": 82.0, "g0": 70.0})
        self.assertEqual(st.warn_hits.get("c0"), 1)
        self.assertEqual(st.crit_hits, {})
        self.assertEqual(tm.store_peak_alert(st, view), "")

        # Enable bell with `b` key
        tm.handle_key(app, ord("b"))
        self.assertTrue(view.bell)
        beeps = []
        self.assertFalse(tm.check_bell(app, lambda: beeps.append(1)))

        # Spike above critical limit (95.0 >= 90.0), then cool down (55.0)
        st.append(101.0, {"c0": 95.0, "g0": 70.0})
        self.assertEqual(st.crit_hits.get("c0"), 1)
        self.assertEqual(st.warn_hits.get("c0"), 2)
        self.assertTrue(tm.check_bell(app, lambda: beeps.append(1)))
        self.assertEqual(len(beeps), 1)
        # Subsequent redraw without new sensor breach does not beep again
        self.assertFalse(tm.check_bell(app, lambda: beeps.append(1)))
        self.assertEqual(len(beeps), 1)

        st.append(102.0, {"c0": 55.0, "g0": 50.0})
        self.assertEqual(tm.store_peak_alert(st, view), "PEAK! CPU0 95.0°C")
        hdr_text = tm.render_header(app, 180, st.n).text()
        self.assertIn("PEAK! CPU0 95.0°C", hdr_text)
        self.assertIn("BELL", hdr_text)
        view.mode = "all"
        list_text = "\n".join(l.text() for l in tm.render_screen(app, 160, 40))
        self.assertIn("[crit x1]", list_text)
        snap = tm.snapshot_json(app)
        c0_json = {r["id"]: r for r in snap["sensors"]}["c0"]
        self.assertEqual(c0_json.get("crit_hits"), 1)
        self.assertEqual(c0_json.get("warn_hits"), 2)

        # Pressing `r` resets min/max, peak breach counters, and bell_seen
        tm.handle_key(app, ord("r"))
        self.assertEqual(st.crit_hits, {})
        self.assertEqual(st.warn_hits, {})
        self.assertEqual(tm.store_peak_alert(st, view), "")
        st.append(103.0, {"c0": 92.0, "g0": 50.0})
        self.assertTrue(tm.check_bell(app, lambda: beeps.append(1)))
        self.assertEqual(len(beeps), 2)

        # 2. Fleet `s` key sorting (`config` -> `hottest` -> `margin` -> `config`)
        fapp = fleet_app()
        fv = fapp.view
        self.assertEqual(fv.fleet_sort, "config")
        self.assertEqual([h.name for h in tm.sorted_hosts(fapp)],
                         ["workstation-a", "spark-1", "mac-studio", "workstation-e"])
        # Cycle to `hottest`
        tm.handle_key(fapp, ord("s"))
        self.assertEqual(fv.fleet_sort, "hottest")
        hot_order = tm.sorted_hosts(fapp)
        live_temps = [tm.host_hottest(h, fv)[0] for h in hot_order[:3]]
        self.assertEqual(live_temps, sorted(live_temps, reverse=True))
        self.assertEqual(hot_order[-1].name, "workstation-e")
        # Selection followed `workstation-a`
        self.assertEqual(hot_order[fv.host_sel].name, "workstation-a")
        tm.handle_key(fapp, 10)
        self.assertEqual(fapp.host.name, "workstation-a")
        tm.handle_key(fapp, 27)
        self.assertIsNone(fapp.host)
        self.assertEqual(hot_order[fv.host_sel].name, "workstation-a")

        # Cycle to `margin`
        tm.handle_key(fapp, ord("s"))
        self.assertEqual(fv.fleet_sort, "margin")
        margin_order = tm.sorted_hosts(fapp)
        live_margins = [tm.host_min_margin(h, fv)[0] for h in margin_order[:3]]
        self.assertEqual(live_margins, sorted(live_margins))
        self.assertEqual(margin_order[-1].name, "workstation-e")
        fhdr = tm.render_fleet_header(fapp, 180).text()
        self.assertIn("sort:margin", fhdr)

        # Inject a critical breach on mac-studio and check fleet peak badges + CJK safety
        studio_link = [h for h in fapp.hosts if h.name == "mac-studio"][0]
        studio_temp_s = [s for s in studio_link.store.visible() if s.kind == "temp"][0]
        studio_link.store.append(2000.0, {studio_temp_s.sid: 105.0})
        fhdr2 = tm.render_fleet_header(fapp, 180).text()
        self.assertIn("PEAK! mac-studio", fhdr2)
        self.assertTrue(tm.fleet_rows(fapp)[0].strip().startswith("mac-studio"))
        # If mac-studio goes down, it keeps its PEAK! badge in the header but sorts after live "up" hosts
        studio_link.state = "down"
        self.assertIsNone(tm.host_hottest(studio_link, fv))
        self.assertIsNone(tm.host_min_margin(studio_link, fv))
        self.assertEqual([h.name for h in tm.sorted_hosts(fapp)][:2], ["workstation-a", "spark-1"])
        fhdr_down = tm.render_fleet_header(fapp, 180).text()
        self.assertIn("PEAK! mac-studio", fhdr_down)
        self.assertNotIn("hottest mac-studio", fhdr_down)
        studio_link.state = "up"
        for width, height in RenderTests.SIZES:
            RenderTests.check(self, tm.render_screen(fapp, width, height), width, height)
        tm.set_ambiguous_wide(True)
        try:
            fv.glyphs = tm.Glyphs("ascii")
            for width, height in ((80, 24), (160, 48)):
                lines = tm.render_screen(fapp, width, height)
                RenderTests.check(self, lines, width, height)
                for line in lines:
                    for ch in line.text():
                        self.assertNotIn(unicodedata.east_asian_width(ch), ("A", "W", "F"), repr(ch))
        finally:
            fv.glyphs = tm.Glyphs("unicode")
            tm.set_ambiguous_wide(False)

        # Cycle back to `config`
        tm.handle_key(fapp, ord("s"))
        self.assertEqual(fv.fleet_sort, "config")

    def test_fleet_recorder(self):
        fapp = fleet_app()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "fleet.csv")
            rec = tm.FleetRecorder(path, fapp.hosts, interval=0.5)
            try:
                rec.sample(1700000000.0)
                # Mark spark-1 as stale: its numeric cells must be blank, not stale repeats
                fapp.hosts[1].last_rx = tm.time.monotonic() - 60.0
                rec.sample(1700000001.0)
            finally:
                rec.close()
            with open(path, newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 3)
            header, r1, r2 = rows
            self.assertEqual(header[:2], ["time", "epoch"])
            self.assertEqual(len(r1), len(header))
            self.assertEqual(len(r2), len(header))
            idx_6000_state = header.index("workstation-a state")
            idx_6000_hot = header.index("workstation-a hottest")
            idx_3090_state = header.index("workstation-e state")
            idx_3090_hot = header.index("workstation-e hottest")
            idx_gb10_state = header.index("spark-1 state")
            idx_gb10_hot = header.index("spark-1 hottest")
            self.assertEqual(r1[idx_6000_state], "up")
            self.assertTrue(float(r1[idx_6000_hot]) > 20.0)
            self.assertEqual(r1[idx_3090_state], "down")
            self.assertEqual(r1[idx_3090_hot], "")
            self.assertEqual(r1[idx_gb10_state], "up")
            self.assertTrue(float(r1[idx_gb10_hot]) > 20.0)
            # Row 2: spark-1 is stale, so state starts with "stale" and hottest is blank
            self.assertTrue(r2[idx_gb10_state].startswith("stale"))
            self.assertEqual(r2[idx_gb10_hot], "")

            # Appending with matching host list succeeds
            rec2 = tm.FleetRecorder(path, fapp.hosts, interval=0.5)
            rec2.sample(1700000002.0)
            rec2.close()
            with open(path, newline="") as f:
                self.assertEqual(len(list(csv.reader(f))), 4)

            # Appending with mismatched host list is rejected to protect CSV integrity
            with self.assertRaises(SystemExit):
                tm.FleetRecorder(path, fapp.hosts[:2], interval=0.5)


class CLITests(unittest.TestCase):
    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = tm.main(list(argv))
        return rc, out.getvalue()

    def test_once(self):
        for host in ("workstation-a", "mac-laptop"):
            rc, out = self.run_main("--replay", os.path.join(FIXTURES, host + ".json"), "--once", "--no-color")
            self.assertEqual(rc, 0)
            self.assertIn("Sources", out)
            self.assertNotIn("\033[", out)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "once.csv")
            rc, _ = self.run_main("--replay", os.path.join(FIXTURES, "workstation-a.json"),
                                  "--once", "--no-color", "--record", path)
            self.assertEqual(rc, 0)
            with open(path, newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][:2], ["time", "epoch"])
            self.assertEqual(len(rows[1]), len(rows[0]))

    def test_json(self):
        rc, out = self.run_main("--replay", os.path.join(FIXTURES, "spark-2.json"), "--json")
        data = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(data["info"]["model"], "NVIDIA DGX Spark")
        self.assertTrue(any(s["group"] == "gpu" and s["value"] and s["margin"] is not None for s in data["sensors"]))
        rc_m, out_m = self.run_main("--replay", os.path.join(FIXTURES, "mac-studio.json"), "--json")
        data_m = json.loads(out_m)
        self.assertEqual(rc_m, 0)
        fans = [s for s in data_m["sensors"] if s["group"] == "fan"]
        self.assertEqual(len(fans), 2)
        self.assertTrue(all(f["rmin"] == 1000.0 and f["rmax"] == 3625.0 and f["fan_pct"] > 0 for f in fans))

    def test_headless_record(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "run.csv")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc, _ = self.run_main("--replay", os.path.join(FIXTURES, "workstation-d.json"),
                                      "--record", path, "--headless", "--duration", "1.2", "-i", "0.3")
            self.assertEqual(rc, 0)
            with open(path, newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0][:2], ["time", "epoch"])
            self.assertGreaterEqual(len(rows), 3)
            self.assertEqual(len(rows[1]), len(rows[0]))


if __name__ == "__main__":
    unittest.main()
