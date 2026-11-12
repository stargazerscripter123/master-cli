"""Layout tests. No network required.

The central invariant: no rendered line may ever exceed the requested display
width. A single overflowing line makes the terminal wrap, which shifts every
row below it and desynchronises the whole grid.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
ROOT = REPO / "src" / "gpumaster"
sys.path.insert(0, str(REPO / "src"))

from gpumaster.model import Fleet, Host, short_gpu_name  # noqa: E402
from gpumaster.parse import parse_payload  # noqa: E402
from gpumaster.render import (  # noqa: E402
    GLYPHS_ASCII,
    GLYPHS_BLOCK,
    dwidth,
    ellipsize,
    fit_command,
    lines_to_text,
    render_fleet,
    render_free,
    render_gpu_detail,
    set_ambiguous_wide,
    shorten_command,
)

FX = pathlib.Path(__file__).resolve().parent / "fixtures"
NAMES = ["workstation-a", "workstation-b", "workstation-c", "workstation-d",
         "workstation-e", "spark-1", "spark-2", "mac-studio"]


def fleet() -> Fleet:
    hosts = []
    for name in NAMES:
        out = (FX / f"{name}.txt").read_text()
        errp = FX / f"{name}.stderr"
        err = errp.read_text() if errp.exists() else ""
        rc = 0 if out.strip() else 255
        hosts.append(parse_payload(Host(name=name, target=name), out, err, rc, 120.0))
    return Fleet(hosts=hosts)


class TestWidths(unittest.TestCase):
    def test_fleet_never_overflows(self):
        f = fleet()
        for width in (240, 200, 180, 160, 150, 140, 130, 120, 110, 100, 90, 80, 70, 60):
            for glyphs in (GLYPHS_BLOCK, GLYPHS_ASCII):
                for hide_idle in (False, True):
                    for procs in (False, True):
                        lines = render_fleet(f, width, 50, glyphs,
                                             hide_idle=hide_idle, show_procs=procs)
                        for i, text in enumerate(lines_to_text(lines)):
                            self.assertLessEqual(
                                dwidth(text), width,
                                f"line {i} at width={width} glyphs={glyphs.name} "
                                f"hide_idle={hide_idle} procs={procs}: {text!r}")

    def test_detail_never_overflows(self):
        f = fleet()
        host = next(h for h in f.hosts if h.gpus)
        for width in (200, 150, 120, 100, 80):
            lines = render_gpu_detail(host, host.gpus[0], width, GLYPHS_BLOCK)
            for text in lines_to_text(lines):
                self.assertLessEqual(dwidth(text), width)

    def test_free_never_overflows(self):
        for width in (150, 110, 90, 70):
            for text in lines_to_text(render_free(fleet(), width)):
                self.assertLessEqual(dwidth(text), width)

    def test_ascii_glyphs_are_all_narrow(self):
        """The ASCII fallback must be immune to East-Asian-Width ambiguity."""
        import unicodedata
        pool = (GLYPHS_ASCII.bar_full + GLYPHS_ASCII.bar_parts
                + GLYPHS_ASCII.bar_mid + GLYPHS_ASCII.bar_track
                + GLYPHS_ASCII.spark)
        for ch in pool:
            self.assertIn(unicodedata.east_asian_width(ch), ("Na", "N"),
                          f"{ch!r} is not unambiguously narrow")

    def test_shade_track_and_braille_are_unambiguous(self):
        """U+2591 and the braille block are EAW Neutral, so always one column.

        The block bars themselves ARE Ambiguous, which is exactly why the
        renderer has to know whether the terminal doubles them.
        """
        import unicodedata
        from gpumaster.render import BRAILLE_SPARK
        for ch in GLYPHS_BLOCK.bar_track + BRAILLE_SPARK.strip():
            self.assertIn(unicodedata.east_asian_width(ch), ("Na", "N", "H"), repr(ch))
        self.assertEqual(unicodedata.east_asian_width(GLYPHS_BLOCK.bar_full), "A")
        # The stacked bar's middle segment is Ambiguous too, which is why
        # stacked_bar() sizes its segments by dwidth rather than by count.
        self.assertEqual(unicodedata.east_asian_width(GLYPHS_BLOCK.bar_mid), "A")
        # The track must stay Neutral: it is what closes a stacked bar to
        # exactly `width` when a two-column segment glyph cannot.
        self.assertEqual(unicodedata.east_asian_width(GLYPHS_BLOCK.bar_track), "N")

    def test_widths_hold_when_ambiguous_is_wide(self):
        """A CJK terminal doubles Ambiguous glyphs; nothing may overflow then."""
        from gpumaster.render import set_ambiguous_wide
        f = fleet()
        try:
            set_ambiguous_wide(True)
            for width in (200, 150, 120, 100, 80):
                for glyphs in (GLYPHS_BLOCK, GLYPHS_ASCII):
                    lines = render_fleet(f, width, 50, glyphs)
                    for text in lines_to_text(lines):
                        self.assertLessEqual(dwidth(text), width,
                                             f"overflow at {width} with {glyphs.name}")
        finally:
            set_ambiguous_wide(False)


class TestCommandFitting(unittest.TestCase):
    """The four ComfyUI jobs differ only in a trailing --port value."""

    CMDS = [
        "/home/user/miniconda3/envs/train_py312/bin/python main.py "
        f"--listen 127.0.0.1 --port {port} --disable-auto-launch"
        for port in (8188, 8189, 8190, 8191)
    ]

    def test_shorten_collapses_conda_path(self):
        s = shorten_command(self.CMDS[0])
        self.assertIn("py@train_py312", s)
        self.assertNotIn("miniconda3", s)

    def test_ports_survive_narrow_columns(self):
        for width in (30, 34, 40, 48, 60):
            rendered = [fit_command(shorten_command(c), width) for c in self.CMDS]
            self.assertEqual(len(set(rendered)), 4,
                             f"commands became indistinguishable at width={width}: "
                             f"{rendered}")
            for text, port in zip(rendered, ("8188", "8189", "8190", "8191")):
                self.assertIn(port, text, f"port lost at width={width}: {text!r}")

    def test_fit_respects_width(self):
        for width in range(8, 80):
            for c in self.CMDS:
                self.assertLessEqual(dwidth(fit_command(shorten_command(c), width)), width)

    def test_ellipsize_keeps_the_tail(self):
        self.assertTrue(ellipsize("abcdefghijklmnop", 10).endswith("nop"))

    def test_inline_column_keeps_ports(self):
        """End to end: the inline top-process column must stay unambiguous."""
        f = fleet()
        text = "\n".join(lines_to_text(render_fleet(f, 150, 60, GLYPHS_BLOCK,
                                                    show_procs=False)))
        for port in ("8188", "8189", "8190", "8191"):
            self.assertIn(f"--port {port}", text,
                          f"port {port} missing from the compact rows")


class TestContent(unittest.TestCase):
    def test_unified_row_shows_marker_and_question(self):
        f = fleet()
        text = "\n".join(lines_to_text(render_fleet(f, 150, 60, GLYPHS_BLOCK,
                                                    show_procs=False)))
        gb = [l for l in text.splitlines() if "GB10" in l and l.startswith("  0")]
        self.assertTrue(gb)
        row = gb[0]
        self.assertIn("*", row, "derived-memory marker missing")
        self.assertIn("?", row, "unknown-utilization marker missing")
        self.assertIn("uma", row)

    def test_unified_row_shows_the_pool_pair(self):
        """A pair sourced from the pool -- and specifically NOT nvtop's pair.

        nvtop 3.3.2 draws host used/total as its MEM bar on this
        part. Putting that in a GPU row would read as "this GPU is holding
        75 GiB" beside the RTX rows' 33.1G/48.0G, so the numerator must be the
        GPU-resident sum and the denominator MemTotal.
        """
        f = fleet()
        text = "\n".join(lines_to_text(render_fleet(f, 150, 60, GLYPHS_BLOCK,
                                                    show_procs=False)))
        row = next(l for l in text.splitlines() if "GB10" in l and l.startswith("  0"))
        # after the util cell's '?' comes one space, then MEMORY (15 wide)
        mem_field = row.split("?")[1][:16].strip()
        self.assertTrue(mem_field.startswith("33.4G/"), mem_field)   # GPU-resident
        self.assertIn("124.0G", mem_field)                           # MemTotal
        self.assertTrue(mem_field.endswith("*"), mem_field)          # still derived
        self.assertNotIn("75", mem_field, "row shows nvtop's host pair")

    def test_unified_row_falls_back_when_the_pool_is_unknown(self):
        """No /proc/meminfo, no denominator -- never an invented one."""
        f = fleet()
        for h in f.hosts:
            for gpu in h.gpus:
                gpu.uma_total_mib = None
        text = "\n".join(lines_to_text(render_fleet(f, 150, 60, GLYPHS_BLOCK,
                                                    show_procs=False)))
        row = next(l for l in text.splitlines() if "GB10" in l and l.startswith("  0"))
        # after the util cell's '?' comes one space, then MEMORY (15 wide)
        mem_field = row.split("?")[1][:16].strip()
        self.assertEqual(mem_field, "33.4G*")

    def test_unified_memory_cell_fits_the_column(self):
        """`pad(..., 'right')` trims from the right, which would eat the `*`.

        The marker is the one character stopping the pair being read as a
        framebuffer, so the cell has to degrade before it overflows.
        """
        from gpumaster.model import Proc
        from gpumaster.render import compute_layout, uma_cell

        width = compute_layout(150).mem
        f = fleet()
        gpu = next(h for h in f.hosts if h.name == "spark-2").gpus[0]
        seen = set()
        # The numerator has to be set too: with an empty process list every
        # pool size renders "0M/..." and the degradation branch never runs.
        for pool_gib, res_gib in ((128, 33), (1024, 900), (8192, 7000)):
            gpu.uma_total_mib = pool_gib * 1024.0
            gpu.uma_available_mib = gpu.uma_total_mib / 2
            gpu.procs[:] = [Proc(bus_id=gpu.bus_id, pid=1,
                                 gpu_mem_mib=res_gib * 1024.0)]
            for glyphs in (GLYPHS_BLOCK, GLYPHS_ASCII):
                cell = uma_cell(gpu, glyphs, width)
                seen.add(cell)
                self.assertLessEqual(dwidth(cell), width, cell)
                self.assertTrue(cell.endswith(glyphs.marker_derived), cell)
        # The degradation must actually have fired, or this test is only
        # re-checking the 13-wide string the other tests already cover.
        # 900.0G/1024.0G* is exactly 15 and still fits; 8 TiB is what overflows.
        self.assertIn("900.0G/1024.0G*", seen)
        self.assertIn("7000/8192G*", seen)

    def test_stacked_bar_slices_never_exceed_the_track(self):
        """Slices of ONE pool, so a double-count shows as a full bar.

        It must never overflow the track or produce a negative segment --
        that is what makes the UMA double-count impossible to draw. Measured
        in DISPLAY columns, including the CJK mode where `█` and `▒` are two
        columns each: counting characters there drew a 22-wide bar 38 wide.
        """
        from gpumaster.render import stacked_bar

        try:
            for ambiguous in (False, True):
                set_ambiguous_wide(ambiguous)
                for glyphs in (GLYPHS_BLOCK, GLYPHS_ASCII):
                    for w in (10, 22, 23, 1):
                        for a, b in ((0.3, 0.4), (0.9, 0.9), (1.0, 1.0),
                                     (-1.0, 2.0), (None, None), (0.0, 0.0),
                                     (0.05, 0.0)):
                            spans = stacked_bar(a, b, w, glyphs)
                            self.assertEqual(
                                dwidth("".join(t for t, _ in spans)), w,
                                f"{a},{b} w={w} glyphs={glyphs.name} "
                                f"ambiguous={ambiguous}")
        finally:
            set_ambiguous_wide(False)

    def test_host_uma_line_splits_the_same_pool(self):
        f = fleet()
        text = "\n".join(lines_to_text(render_fleet(f, 150, 60, GLYPHS_BLOCK,
                                                    show_procs=False)))
        line = next(l for l in text.splitlines() if l.strip().startswith("UMA ██"))
        self.assertIn("33.4G GPU", line)
        self.assertIn("40.6G system", line)
        self.assertIn("124.0G", line)
        self.assertIn("50.0G available", line)

    def test_fleet_summary_reports_uma_separately_from_vram(self):
        """Two lines, not one blended figure, and VRAM comes first."""
        f = fleet()
        lines = lines_to_text(render_fleet(f, 150, 60, GLYPHS_BLOCK,
                                           show_procs=False))
        vram = next(i for i, l in enumerate(lines) if "FLEET" in l)
        uma = next(i for i, l in enumerate(lines) if l.startswith("      UMA  "))
        self.assertLess(vram, uma)
        self.assertIn("227/496G", lines[vram], "FLEET VRAM must stay discrete-only")
        # GB10's 33.4G/124.0G pool plus the Mac's 2.7G/192.0G: the Mac rolls
        # into the UMA line, never into VRAM.
        self.assertIn("36/316G", lines[uma])
        self.assertIn("(2 hosts)", lines[uma])
        self.assertNotIn("UMA", lines[vram], "the two must not be blended")

    def test_header_names_a_non_default_mode(self):
        """A partial fleet must not read as the whole one; the default screen
        carries no label."""
        f = fleet()
        plain = lines_to_text(render_fleet(f, 150, 60, GLYPHS_BLOCK, show_procs=False))
        self.assertNotIn("[", plain[0])
        for width in (60, 150):
            head = lines_to_text(render_fleet(f, width, 60, GLYPHS_BLOCK,
                                              show_procs=False, mode="desktop"))[0]
            self.assertIn("[desktop]", head)
            self.assertLessEqual(dwidth(head), width)

    def test_all_unified_fleet_has_no_orphan_label(self):
        """A fleet of nothing but Sparks reports no framebuffer and no power
        limit, so the FLEET line would otherwise be a bare label with an
        indented continuation dangling under it.
        """
        f = Fleet(hosts=[h for h in fleet().hosts if h.name == "spark-2"])
        lines = lines_to_text(render_fleet(f, 150, 30, GLYPHS_BLOCK,
                                           show_procs=False))
        line = next(l for l in lines if l.startswith("FLEET"))
        self.assertNotEqual(line.rstrip(), "FLEET")
        self.assertIn("UMA", line)
        self.assertIn("33/124G", line)

    def test_dead_driver_host_shows_live_memory(self):
        f = fleet()
        text = "\n".join(lines_to_text(render_fleet(f, 150, 60, GLYPHS_BLOCK,
                                                    show_procs=False)))
        line = next(l for l in text.splitlines() if "spark-1" in l)
        self.assertIn("[!]", line)
        self.assertIn("RAM", line, "CPU/RAM must stay visible when the driver is dead")
        self.assertNotIn("no GPU", line)

    def test_down_host_fabricates_nothing(self):
        f = fleet()
        lines = lines_to_text(render_fleet(f, 150, 60, GLYPHS_BLOCK, show_procs=False))
        idx = next(i for i, l in enumerate(lines) if "workstation-e" in l)
        self.assertIn("[x]", lines[idx])
        self.assertIn("HOST DOWN", lines[idx])
        # The next line must be another host or a rule, never a GPU row.
        nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
        self.assertFalse(nxt.startswith("  0"), f"down host produced a GPU row: {nxt!r}")

    def test_pressure_surfaces_in_summary(self):
        f = fleet()
        head = lines_to_text(render_fleet(f, 180, 60, GLYPHS_BLOCK,
                                          show_procs=False))[0]
        self.assertIn("PRESSURE", head)
        self.assertIn("workstation-b", head)
        self.assertNotIn("GB10a", head, "a dead driver is not memory pressure")


class TestAppleRows(unittest.TestCase):
    """The Mac renders as a unified part whose numbers are believed."""

    def _mac_row(self, width: int = 150) -> str:
        f = fleet()
        text = "\n".join(lines_to_text(render_fleet(f, width, 60, GLYPHS_BLOCK,
                                                    show_procs=False)))
        return next(l for l in text.splitlines()
                    if "M2 Ultra" in l and l.startswith("  0"))

    def test_apple_row_shows_a_real_utilization_and_no_derived_marker(self):
        import re
        row = self._mac_row()
        self.assertIn("uma", row)
        cells = row.split("uma")[0]
        self.assertNotIn("?", cells, "Apple utilization is trusted, never '?'")
        self.assertNotIn("*", cells, "resident is read directly, so no derived marker")
        # <util number>  <resident>/<hw.memsize as the denominator>
        self.assertRegex(cells, r"\s\d{1,3}\s+\d+(\.\d+)?[MG]/192\.0G\s")
        self.assertNotIn("Apple ", row, "short name drops the vendor prefix")

    def test_apple_host_header_uses_the_uma_tag(self):
        f = fleet()
        lines = lines_to_text(render_fleet(f, 150, 60, GLYPHS_BLOCK, show_procs=False))
        head = next(l for l in lines if "mac-studio" in l)
        self.assertIn("[+]", head)
        self.assertIn("UMA", head)
        self.assertIn("1x M2 Ultra", head)
        self.assertIn("24c", head)

    def test_apple_detail_never_overflows_and_names_its_sources(self):
        f = fleet()
        host = next(h for h in f.hosts if h.name == "mac-studio")
        for width in (200, 150, 120, 100, 80):
            lines = render_gpu_detail(host, host.gpus[0], width, GLYPHS_BLOCK)
            for text in lines_to_text(lines):
                self.assertLessEqual(dwidth(text), width)
        text = "\n".join(lines_to_text(render_gpu_detail(host, host.gpus[0], 200,
                                                         GLYPHS_BLOCK)))
        self.assertIn("In use system memory", text)
        self.assertIn("GPU cores", text)
        self.assertNotIn("NVML", text.split("processes")[0],
                         "Apple drill-down must not explain itself in NVML terms")
        self.assertNotIn("pinned P0", text)

    def test_apple_macmon_enriched_detail_and_json(self):
        from gpumaster.cli import fleet_to_json

        f = fleet()
        host = next(h for h in f.hosts if h.name == "mac-studio")
        gpu = host.gpus[0]
        gpu.temp_c = 52.0
        gpu.power_w = 24.5
        gpu.clock_sm = 1398.0
        gpu.ane_power_w = 3.2
        detail_lines = render_gpu_detail(host, gpu, 120, GLYPHS_BLOCK)
        for text in lines_to_text(detail_lines):
            self.assertLessEqual(dwidth(text), 120)
        detail = "\n".join(lines_to_text(detail_lines))
        self.assertIn("ANE power", detail)
        self.assertIn("3.2W", detail)
        self.assertIn("temp/power via macmon", detail)
        j = fleet_to_json(Fleet(hosts=[host]))
        self.assertAlmostEqual(j[0]["gpus"][0]["ane_power_w"], 3.2)


class TestGpuNameShortening(unittest.TestCase):
    """A model name must never fill its cell exactly.

    'RTX 5090 Laptop GPU' is 19 columns. Trimmed to the 14-column model cell it
    became 'RTX 5090 Lapto', and as '1x RTX 5090 Laptop GPU' it trimmed to
    exactly the 18 columns of the host label -- so in both places the name sat
    flush against the next column and the separating space vanished
    ('1x RTX 5090 Laptop24c'). Trimming cannot fix this; the name has to be
    short enough to leave a gap.
    """

    CASES = [
        ("NVIDIA GeForce RTX 5090 Laptop GPU", "RTX 5090m"),
        ("NVIDIA GeForce RTX 5090", "RTX 5090"),
        ("NVIDIA RTX 6000 Ada Generation", "RTX 6000 Ada"),
        ("NVIDIA GB10", "GB10"),
        ("Apple M2 Ultra", "M2 Ultra"),
    ]

    def test_shortening(self):
        for raw, want in self.CASES:
            self.assertEqual(short_gpu_name(raw), want)

    def test_laptop_stays_distinct_from_desktop(self):
        # The fleet runs both, with different framebuffers (24 vs 32 GiB).
        self.assertNotEqual(short_gpu_name("NVIDIA GeForce RTX 5090 Laptop GPU"),
                            short_gpu_name("NVIDIA GeForce RTX 5090"))

    def test_every_name_leaves_a_column_separator(self):
        for raw, _ in self.CASES:
            short = short_gpu_name(raw)
            self.assertLess(dwidth(short), 14,
                            f"{short!r} fills the 14-column model cell")
            self.assertLess(dwidth(f"4x {short}"), 18,
                            f"{short!r} fills the 18-column host label")

    def test_net_breakdown_and_detail_fabric_render_within_width(self):
        from gpumaster.model import NetLink
        from gpumaster.cli import fleet_to_json

        h = parse_payload(
            Host(name="spark-2", target="spark-2"),
            (FX / "spark-2.txt").read_text(),
            "",
            0,
            120.0,
        )
        h.net_links = [
            NetLink(
                name="enp1s0f0np0",
                operstate="up",
                speed_mbps=100000,
                mtu=9000,
                rx_bytes=1000,
                tx_bytes=2000,
                rdma_dev="rocep1s0f0",
                rx_gbps=91.2,
                tx_gbps=92.0,
            ),
            NetLink(
                name="enp1s0f1np1",
                operstate="down",
                speed_mbps=None,
                mtu=9000,
                rx_bytes=0,
                tx_bytes=0,
                rdma_dev="rocep1s0f1",
            ),
        ]
        f = Fleet(hosts=[h])
        for w in (60, 80, 100, 120, 140):
            lines = render_fleet(f, width=w, height=40, g=GLYPHS_BLOCK)
            for text in lines_to_text(lines):
                self.assertLessEqual(dwidth(text), w)
        txt = "\n".join(lines_to_text(render_fleet(f, width=120, height=40)))
        self.assertIn("RDMA 1/2 rails!", txt)
        self.assertIn("enp1s0f0np0/rocep1s0f0:100G", txt)

        detail_txt = "\n".join(
            lines_to_text(render_gpu_detail(h, h.gpus[0], width=110, g=GLYPHS_BLOCK))
        )
        self.assertIn("cluster fabric", detail_txt)
        self.assertIn("RDMA 1/2 rails!", detail_txt)

        j = fleet_to_json(f)
        self.assertEqual(len(j[0]["net_links"]), 2)
        self.assertEqual(j[0]["net_links"][0]["rdma_dev"], "rocep1s0f0")
        self.assertAlmostEqual(j[0]["net_links"][0]["total_gbps"], 183.2, places=1)

    def test_container_and_stale_badges_render_within_width(self):
        from gpumaster.cli import fleet_to_json

        f = fleet()
        h = next(x for x in f.hosts if x.name == "workstation-a")
        h.gpus[0].procs[0].container = "comfy-docker"
        h.gpus[0].procs[0].cpu_percent = 0.0
        for w in (60, 80, 100, 120, 150):
            for line in lines_to_text(render_fleet(f, w, 60, GLYPHS_BLOCK, show_procs=True)):
                self.assertLessEqual(dwidth(line), w)
            for line in lines_to_text(render_gpu_detail(h, h.gpus[0], w, GLYPHS_BLOCK)):
                self.assertLessEqual(dwidth(line), w)
            for line in lines_to_text(render_free(f, w)):
                self.assertLessEqual(dwidth(line), w)

        fleet_txt = "\n".join(lines_to_text(render_fleet(f, 150, 70, GLYPHS_BLOCK, show_procs=True)))
        self.assertIn("[comfy-docker]", fleet_txt)
        self.assertIn("[IDLE-HOLD]", fleet_txt)

        detail_txt = "\n".join(lines_to_text(render_gpu_detail(h, h.gpus[0], 120, GLYPHS_BLOCK)))
        self.assertIn("stale idle allocation(s) [IDLE-HOLD]", detail_txt)

        free_txt = "\n".join(lines_to_text(render_free(f, 120)))
        self.assertIn("IDLE-HOLD by PID", free_txt)

        j = fleet_to_json(Fleet(hosts=[h]))
        p0 = j[0]["gpus"][0]["processes"][0]
        self.assertEqual(p0["container"], "comfy-docker")
        self.assertTrue(p0["stale"])
        self.assertGreater(p0["elapsed_seconds"], 1800)


class TestInteractiveFilterSortAndKill(unittest.TestCase):
    """Interactive TUI `/` search filter, `S` host sort, and `K` remote kill (FLT-009)."""

    def test_host_sort_and_visible_fleet_gpus(self):
        from gpumaster.render import HOST_SORTS, sort_hosts, visible_fleet_gpus

        f = fleet()
        for key in HOST_SORTS:
            ordered = sort_hosts(f.hosts, key)
            self.assertEqual(len(ordered), len(f.hosts))
            # Down host (workstation-e) and driver-dead host (spark-1) sort after live GPU hosts
            if key != "config":
                live_names = [h.name for h in ordered if h.online and h.gpus]
                self.assertEqual(
                    [h.name for h in ordered[: len(live_names)]],
                    live_names,
                )

        # free-mem puts mac-studio (470+ GiB UMA headroom) first
        by_free = sort_hosts(f.hosts, "free-mem")
        self.assertEqual(by_free[0].name, "mac-studio")

        # visible_fleet_gpus matches filter_query and hide_idle
        vis = visible_fleet_gpus(f, hide_idle=False, filter_query="8188", host_sort="config")
        self.assertEqual(
            [(h.name, g.index) for h, g in vis],
            [("workstation-a", 0), ("workstation-b", 0), ("workstation-c", 0), ("spark-2", 0)],
        )
        vis_pid = visible_fleet_gpus(f, hide_idle=False, filter_query="2296157", host_sort="config")
        self.assertEqual([(h.name, g.index) for h, g in vis_pid], [("workstation-a", 0)])

    def test_render_fleet_filter_and_sort_and_detail_selection(self):
        from gpumaster.render import S_SEL, set_ambiguous_wide

        f = fleet()
        for amb in (False, True):
            set_ambiguous_wide(amb)
            try:
                for w in (80, 100, 140):
                    lines = render_fleet(
                        f, w, 50, GLYPHS_BLOCK,
                        filter_query="8188", host_sort="temp",
                    )
                    for text in lines_to_text(lines):
                        self.assertLessEqual(dwidth(text), w)
            finally:
                set_ambiguous_wide(False)

        txt = "\n".join(
            lines_to_text(
                render_fleet(f, 150, 50, GLYPHS_BLOCK, filter_query="8188", host_sort="temp")
            )
        )
        self.assertIn("[sort:temp]", txt)
        self.assertIn("[/8188]", txt)
        self.assertIn("workstation-a", txt)
        self.assertNotIn("mac-studio", txt)

        # Unmatched filter shows helpful empty notice
        empty_txt = "\n".join(
            lines_to_text(
                render_fleet(f, 120, 40, GLYPHS_BLOCK, filter_query="nonexistent-xyz")
            )
        )
        self.assertIn("no hosts or GPUs match /nonexistent-xyz", empty_txt)

        # GPU detail highlights selected process and shows [Tab/n] / [K] hint
        host_a = next(h for h in f.hosts if h.name == "workstation-a")
        dlines = render_gpu_detail(host_a, host_a.gpus[0], 130, GLYPHS_BLOCK, selected_proc=0)
        dtxt = "\n".join(lines_to_text(dlines))
        self.assertIn("[Tab/n] select  [K] kill", dtxt)
        sel_spans = [t for line in dlines for t, st in line if st == S_SEL]
        self.assertEqual(len(sel_spans), 1)
        self.assertIn("2296157", sel_spans[0])

    def test_edit_filter_key_and_kill_remote_proc(self):
        from pathlib import Path
        from unittest.mock import MagicMock, patch
        from gpumaster.transport import HostSpec, Poller, SshOptions
        from gpumaster.tui import edit_filter_key

        q, editing = edit_filter_key(ord("c"), "")
        self.assertEqual((q, editing), ("c", True))
        q, editing = edit_filter_key(ord("o"), q)
        self.assertEqual((q, editing), ("co", True))
        q, editing = edit_filter_key(127, q)  # Backspace
        self.assertEqual((q, editing), ("c", True))
        q, editing = edit_filter_key(10, q)   # Enter
        self.assertEqual((q, editing), ("c", False))
        q, editing = edit_filter_key(27, "abc")  # Esc clears
        self.assertEqual((q, editing), ("", False))

        poller = Poller(SshOptions(multiplex=False), ROOT / "remote.sh")
        spec = HostSpec(name="workstation-a", target="workstation-a")
        self.assertFalse(poller.kill_remote_proc(spec, 0, "TERM")[0])
        self.assertFalse(poller.kill_remote_proc(spec, 1, "TERM")[0])
        self.assertFalse(poller.kill_remote_proc(spec, True, "TERM")[0])
        self.assertFalse(poller.kill_remote_proc(spec, 12345, "SEGV")[0])

        fake_cp = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake_cp) as m_run:
            ok, msg = poller.kill_remote_proc(spec, 2296157, "TERM")
            self.assertTrue(ok)
            self.assertIn("sent SIGTERM to workstation-a PID 2296157", msg)
            self.assertEqual(m_run.call_args.kwargs["input"], "kill -s TERM 2296157\n")

    def test_modal_status_line_fits_80_columns_and_preserves_prompts(self):
        """Regression test for FLT-009 adversarial audit:
        Modal `/` filter and `K` kill confirmation prompts must replace the 96-column
        key legend and fit within 80 columns without clipping action instructions.
        """
        from gpumaster.config import Config
        from gpumaster.render import S_ACCENT, S_CRIT
        from gpumaster.tui import Collector, status_line

        cfg = Config()
        coll = Collector(cfg, None)

        filt_line = status_line(
            cfg,
            coll,
            "filter: /8188_  [enter]apply [esc]clear",
            S_ACCENT,
            modal=True,
            width=80,
        )
        filt_txt = lines_to_text([filt_line])[0]
        self.assertLessEqual(dwidth(filt_txt), 80)
        self.assertIn("filter: /8188_", filt_txt)
        self.assertIn("[enter]apply [esc]clear", filt_txt)

        kill_line = status_line(
            cfg,
            coll,
            "KILL workstation-a PID 2296157 (user: /home/user/miniconda3/envs/train_py312/bin/python)? "
            "[y/t]=SIGTERM [k]=SIGKILL [any]=cancel",
            S_CRIT,
            modal=True,
            width=80,
        )
        kill_txt = lines_to_text([kill_line])[0]
        self.assertLessEqual(dwidth(kill_txt), 80)
        self.assertIn("KILL workstation-a PID 2296157", kill_txt)
        self.assertIn("[y/t]=SIGTERM [k]=SIGKILL", kill_txt)

    def test_serve_ansi_prometheus_and_http_responses(self):
        import json as json_mod
        from types import SimpleNamespace
        from gpumaster.cli import build_serve_response, main
        from gpumaster.model import NetLink
        from gpumaster.render import S_CRIT, S_NORMAL, S_OK, fleet_to_prometheus, lines_to_ansi

        f = fleet()
        # Attach a ConnectX-7 net link, disk telemetry, and macmon CPU telemetry
        spark = next(h for h in f.hosts if h.name == "spark-2")
        spark.net_links = [
            NetLink(
                name="enp1s0f0np0",
                operstate="up",
                speed_mbps=200000,
                mtu=9000,
                rx_gbps=18.25,
                tx_gbps=42.5,
                rdma_dev="roceP2p1s0f0",
            )
        ]
        spark.mem.disk_total_kib = 100 * 1024 * 1024
        spark.mem.disk_avail_kib = 80 * 1024 * 1024
        mac = next(h for h in f.hosts if h.name == "mac-studio")
        mac.gpus[0].ane_power_w = 3.25

        # 1. lines_to_ansi wraps styled spans in ANSI SGR codes and resets
        ansi_lines = lines_to_ansi([[("OK", S_OK), (" plain ", S_NORMAL), ("ERR", S_CRIT)]])
        self.assertEqual(len(ansi_lines), 1)
        self.assertIn("\x1b[32mOK\x1b[0m", ansi_lines[0])
        self.assertIn(" plain ", ansi_lines[0])
        self.assertIn("\x1b[1;31mERR\x1b[0m", ansi_lines[0])

        # 2. fleet_to_prometheus groups metric families (single # HELP / # TYPE per family)
        #    and respects GB10 / UMA / discrete rules
        prom = fleet_to_prometheus(f)
        self.assertEqual(prom.count("# HELP gpumaster_host_online "), 1)
        self.assertEqual(prom.count("# TYPE gpumaster_host_online gauge"), 1)
        self.assertIn('gpumaster_host_online{host="spark-2",state="streaming"} 1', prom)
        self.assertIn('gpumaster_host_online{host="workstation-e",state="down"} 0', prom)
        # GB10 utilization is untrusted ('?') and must NOT be emitted as a number
        self.assertNotIn('gpumaster_gpu_utilization_percent{host="spark-2"', prom)
        # Discrete and Apple utilization ARE trustworthy and present
        self.assertIn('gpumaster_gpu_utilization_percent{host="workstation-a"', prom)
        self.assertIn('gpumaster_gpu_utilization_percent{host="mac-studio"', prom)
        # Unified GPUs must NOT emit discrete framebuffer memory metrics
        self.assertNotIn('gpumaster_gpu_memory_used_mib{host="spark-2"', prom)
        self.assertNotIn('gpumaster_gpu_memory_used_mib{host="mac-studio"', prom)
        self.assertIn('gpumaster_gpu_memory_used_mib{host="workstation-a"', prom)
        # Unified hosts emit UMA pool metrics and resident memory with memory_kind
        self.assertIn('gpumaster_host_uma_pool_total_mib{host="spark-2"}', prom)
        self.assertIn('gpumaster_host_uma_pool_available_mib{host="mac-studio"}', prom)
        self.assertIn('memory_kind="unified"', prom)
        # ConnectX-7 net link, disk, PSI, and macmon metrics are present
        self.assertIn('gpumaster_net_link_speed_gbps{host="spark-2",interface="enp1s0f0np0",rdma_dev="roceP2p1s0f0"} 200', prom)
        self.assertIn('gpumaster_net_link_tx_gbps{host="spark-2",interface="enp1s0f0np0",rdma_dev="roceP2p1s0f0"} 42.5000', prom)
        self.assertIn('gpumaster_host_disk_available_kib{host="spark-2"}', prom)
        self.assertIn('gpumaster_host_psi_memory_some10{host="spark-2"}', prom)
        self.assertIn('gpumaster_gpu_ane_power_watts{host="mac-studio"', prom)

        # 3. build_serve_response handles /, /fleet.txt, /fleet.ansi, /api/fleet.json, /metrics, 404
        dummy_args = SimpleNamespace(hide_idle=False, no_procs=False)
        st, ct, body = build_serve_response("/", f, 120, 40, GLYPHS_BLOCK, dummy_args, refresh_seconds=2.0)
        self.assertEqual(st, 200)
        self.assertIn("text/html", ct)
        self.assertIn(b'http-equiv="refresh" content="2"', body)
        self.assertIn(b"/metrics", body)

        st, ct, body = build_serve_response("/fleet.txt", f, 120, 40, GLYPHS_BLOCK, dummy_args)
        self.assertEqual(st, 200)
        self.assertIn("text/plain", ct)
        self.assertIn(b"gpumaster", body)

        st, ct, body = build_serve_response("/fleet.ansi", f, 120, 40, GLYPHS_BLOCK, dummy_args)
        self.assertEqual(st, 200)
        self.assertIn(b"\x1b[", body)

        st, ct, body = build_serve_response("/api/fleet.json", f, 120, 40, GLYPHS_BLOCK, dummy_args)
        self.assertEqual(st, 200)
        self.assertIn("application/json", ct)
        parsed = json_mod.loads(body.decode("utf-8"))
        self.assertEqual(len(parsed), len(f.hosts))

        st, ct, body = build_serve_response("/metrics?scrape=1", f, 120, 40, GLYPHS_BLOCK, dummy_args)
        self.assertEqual(st, 200)
        self.assertIn("version=0.0.4", ct)
        self.assertIn(b"gpumaster_host_online", body)

        st, ct, body = build_serve_response("/unknown", f, 120, 40, GLYPHS_BLOCK, dummy_args)
        self.assertEqual(st, 404)

        # Invalid --port returns exit code 2
        self.assertEqual(main(["serve", "--port", "0"]), 2)
        self.assertEqual(main(["serve", "--port", "70000"]), 2)

        # 4. Regression test for FLT-011 adversarial audit:
        #    cmd_serve() in live mode (static_fleet=None) must use cfg.ssh.command_timeout,
        #    enable httpd.daemon_threads, and shut down both httpd and collector cleanly.
        from unittest.mock import MagicMock, patch
        from gpumaster.config import Config
        from gpumaster.cli import cmd_serve

        cfg = Config()
        fake_collector = MagicMock()
        fake_collector.snapshot.return_value = (f, {}, 1, 12.0)
        fake_httpd = MagicMock()
        fake_httpd.serve_forever.side_effect = KeyboardInterrupt()
        serve_args = SimpleNamespace(
            bind="127.0.0.1",
            port=8765,
            hide_idle=False,
            no_procs=False,
        )
        with patch("gpumaster.tui.Collector", return_value=fake_collector) as m_coll, patch(
            "http.server.ThreadingHTTPServer", return_value=fake_httpd
        ) as m_srv:
            rc = cmd_serve(cfg, None, GLYPHS_BLOCK, 120, 40, serve_args, static_fleet=None)
            self.assertEqual(rc, 0)
            m_coll.assert_called_once_with(cfg, None)
            fake_collector.start.assert_called_once()
            fake_collector.shutdown.assert_called_once()
            m_srv.assert_called_once()
            self.assertTrue(fake_httpd.daemon_threads)
            fake_httpd.serve_forever.assert_called_once()
            fake_httpd.server_close.assert_called_once()

    def test_dgx_spark_spare_port_filtering_and_tui_visual_polish(self):
        from gpumaster.model import NetLink, host_alerts
        from gpumaster.render import S_HEAD, gpu_row, compute_layout, rule, set_ambiguous_wide

        # 1. DGX Spark ConnectX-7 Socket-Direct: 2 active MTU 9000 rails on port p0 +
        #    2 unplugged MTU 1500 spare netdevs on port p1 must not trigger '2/4 rails!'
        f = fleet()
        spark = next(h for h in f.hosts if h.name == "spark-2")
        spark.net_links = [
            NetLink(name="enP2p1s0f0np0", operstate="up", speed_mbps=200000, mtu=9000, rdma_dev="roceP2p1s0f0", rx_gbps=0.0, tx_gbps=0.0),
            NetLink(name="enP2p1s0f1np1", operstate="down", speed_mbps=None, mtu=1500, rdma_dev="roceP2p1s0f1"),
            NetLink(name="enp1s0f0np0", operstate="up", speed_mbps=200000, mtu=9000, rdma_dev="rocep1s0f0", rx_gbps=0.0, tx_gbps=0.0),
            NetLink(name="enp1s0f1np1", operstate="down", speed_mbps=None, mtu=1500, rdma_dev="rocep1s0f1"),
        ]
        self.assertEqual(len(spark.active_net_links), 2)
        self.assertEqual(spark.hs_fabric_summary(), "RDMA 2x200G 0.0Gbps")
        self.assertFalse(any(a.tag == "RDMA" for a in host_alerts(spark)))

        # 2. Box-drawing rule uses '─' in block mode and '-' in CJK/ASCII mode
        self.assertEqual(rule(10, g=GLYPHS_BLOCK)[0][0], "─" * 10)
        self.assertEqual(rule(10, g=GLYPHS_ASCII)[0][0], "-" * 10)
        try:
            set_ambiguous_wide(True)
            self.assertEqual(rule(10, g=GLYPHS_BLOCK)[0][0], "-" * 10)
        finally:
            set_ambiguous_wide(False)

        # 3. Selected GPU row uses '▸' and styles index + model name with S_HEAD
        lay = compute_layout(120)
        sel_row = gpu_row(spark.gpus[0], lay, GLYPHS_BLOCK, selected=True)
        self.assertTrue(sel_row[0][0].startswith("▸"))
        self.assertEqual(sel_row[0][1], S_HEAD)
        self.assertEqual(sel_row[1][1], S_HEAD)


if __name__ == "__main__":
    unittest.main(verbosity=2)
