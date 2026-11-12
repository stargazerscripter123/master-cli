"""Golden tests over real captures. No network required.

    python3 -m unittest discover tests

Every assertion here corresponds to something that was measured on the real
fleet and that a future driver, kernel or refactor could silently break.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
ROOT = REPO / "src" / "gpumaster"
sys.path.insert(0, str(REPO / "src"))

from gpumaster.model import (  # noqa: E402
    Fleet,
    Host,
    HostState,
    Level,
    decode_clock_events,
    gpu_alerts,
    host_alerts,
    idle_gpus,
)
from gpumaster.parse import parse_payload, ssh_error_kind  # noqa: E402

FX = pathlib.Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> Host:
    out = (FX / f"{name}.txt").read_text()
    err_path = FX / f"{name}.stderr"
    err = err_path.read_text() if err_path.exists() else ""
    rc = 0 if out.strip() else 255
    return parse_payload(Host(name=name, target=name), out, err, rc, 100.0)


class TestHostStates(unittest.TestCase):
    def test_healthy_host_streams(self):
        h = load("workstation-a")
        self.assertIs(h.state, HostState.STREAMING)
        self.assertEqual(len(h.gpus), 4)

    def test_dead_driver_keeps_host_alive(self):
        """spark-1: nvidia-smi rc=9 but /proc is perfectly healthy.

        The whole machine must not be discarded -- this is the defect that
        made cluster-nvtop report the host as offline.
        """
        h = load("spark-1")
        self.assertIs(h.state, HostState.GPU_DEAD)
        self.assertTrue(h.online, "a dead driver must not mark the host offline")
        self.assertEqual(h.error_code, "nvidia-smi-rc-9")
        self.assertIsNotNone(h.mem.total_kib)
        self.assertIsNotNone(h.mem.available_kib)
        self.assertIsNotNone(h.psi_mem.some10)
        self.assertIsNotNone(h.load1)
        self.assertEqual(h.arch, "aarch64")

    def test_refused_host_is_down(self):
        h = load("workstation-e")
        self.assertIs(h.state, HostState.DOWN)
        self.assertFalse(h.online)
        self.assertIn("refused", h.error_msg.lower())
        self.assertEqual(h.gpus, [])

    def test_ssh_error_classification(self):
        self.assertEqual(ssh_error_kind("ssh: connect ... Connection refused"), "REFUSED")
        self.assertEqual(ssh_error_kind("Permission denied (publickey)."), "AUTH_FAIL")
        self.assertEqual(
            ssh_error_kind("@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @"),
            "HOSTKEY_CHANGED")
        self.assertEqual(
            ssh_error_kind("ControlPath too long ('/var/...' >= 104 bytes)"),
            "CONTROLPATH_TOO_LONG")


class TestNullHandling(unittest.TestCase):
    def test_na_never_becomes_zero(self):
        """Both `N/A` and `[N/A]` must parse to None, never 0."""
        gpu = load("spark-2").gpus[0]
        for attr in ("mem_total_mib", "mem_used_mib", "mem_free_mib",
                     "mem_reserved_mib", "power_limit_w", "fan_percent",
                     "clock_mem", "clock_mem_max"):
            self.assertIsNone(getattr(gpu, attr), f"{attr} should be None on GB10")

    def test_real_values_survive(self):
        gpu = load("spark-2").gpus[0]
        self.assertEqual(gpu.name, "NVIDIA GB10")
        self.assertIsNotNone(gpu.temp_c)
        self.assertIsNotNone(gpu.power_w)
        self.assertEqual(gpu.clock_sm_max, 3003)
        self.assertEqual(gpu.pstate, "P0")


class TestUnifiedMemory(unittest.TestCase):
    def test_gb10_is_unified(self):
        gpu = load("spark-2").gpus[0]
        self.assertTrue(gpu.is_unified)
        self.assertTrue(gpu.mem_derived)

    def test_gpu_resident_is_sum_of_processes(self):
        """See also test_pool_numerator_is_the_process_sum_not_the_host_pair,
        which pins the same sum against the host pair it must not become.
        """
        gpu = load("spark-2").gpus[0]
        self.assertGreater(gpu.gpu_resident_mib, 30000)

    def test_no_memory_percentage_on_unified(self):
        """`mem_percent` means framebuffer occupancy, and there is no
        framebuffer here. The pool ratio lives in `frac_of_pool` instead --
        a different denominator deserves a different name, and this one feeds
        the VRAM alert.
        """
        self.assertIsNone(load("spark-2").gpus[0].mem_percent)

    def test_unified_never_raises_a_vram_alert(self):
        """A full CPU side must never be reported as 'VRAM 98% full'.

        This is why `mem_percent` stays None rather than being repointed at
        the pool: gpu_alerts() turns it into a VRAM alert, and pool pressure
        is host_alerts()' job (RAM / SWAP / PSI), reported once.
        """
        h = load("spark-2")
        h.mem.available_kib = h.mem.total_kib // 50      # pool 98% consumed
        h.gpus[0].uma_available_mib = h.mem.available_kib / 1024
        self.assertNotIn("VRAM", {a.tag for a in gpu_alerts(h.gpus[0])})
        self.assertIn("RAM", {a.tag for a in host_alerts(h)})

    def test_pool_denominator_is_memtotal_not_invented(self):
        """The denominator must be pointable-at, not guessed.

        nvidia-smi reports [N/A] for memory.total on GB10 and NVML returns
        NOT_SUPPORTED, so the only honest total is the pool the GPU allocates
        from. If it ever stops equalling MemTotal exactly, something is
        fabricating it.
        """
        h = load("spark-2")
        gpu = h.gpus[0]
        self.assertIsNone(gpu.mem_total_mib)             # NVML gave nothing
        self.assertIsNone(gpu.mem_used_mib)
        self.assertAlmostEqual(gpu.uma_total_mib, h.mem.total_kib / 1024, places=3)
        self.assertAlmostEqual(gpu.uma_available_mib,
                               h.mem.available_kib / 1024, places=3)
        self.assertAlmostEqual(gpu.pool_used_mib, h.mem.used_kib / 1024, places=3)

    def test_pool_numerator_is_the_process_sum_not_the_host_pair(self):
        """The row's numerator is what the GPU holds, not what the host holds.

        nvtop 3.3.2 draws MemTotal-MemAvailable as its MEM bar on this part;
        putting that in a GPU row would read as '75 GiB of GPU memory' beside
        the RTX rows. The two differ by ~42 GiB here, so this is a sharp test.
        """
        gpu = load("spark-2").gpus[0]
        proc_sum = sum(p.gpu_mem_mib for p in gpu.procs if p.gpu_mem_mib)
        self.assertAlmostEqual(gpu.gpu_resident_mib, proc_sum, places=3)
        self.assertGreater(gpu.pool_used_mib - gpu.gpu_resident_mib, 40000)

    def test_pool_slices_partition_the_pool_and_never_go_negative(self):
        """Invariant #1 in arithmetic form.

        'Other used' is pool-used MINUS gpu-resident, a subtraction inside one
        pool, which makes the UMA double-count structurally impossible to
        draw. The clamp covers the case the kernel does not guarantee: NVML
        per-process accounting and MemAvailable are different subsystems
        sampled at different instants.
        """
        gpu = load("spark-2").gpus[0]
        self.assertAlmostEqual(gpu.gpu_resident_mib + gpu.pool_other_mib,
                               gpu.pool_used_mib, places=3)
        self.assertLess(gpu.gpu_resident_mib, gpu.pool_used_mib)
        self.assertLess(gpu.pool_used_mib, gpu.uma_total_mib)

        gpu.uma_available_mib = gpu.uma_total_mib      # pool reads as empty
        self.assertEqual(gpu.pool_used_mib, 0.0)
        self.assertEqual(gpu.pool_other_mib, 0.0)      # clamped, not negative

    def test_pool_is_attached_only_to_unified_gpus(self):
        """A discrete card does not allocate from the host pool.

        Copying the numbers onto one would leave a loaded gun for a future
        caller to render as a meaningless pair.
        """
        for gpu in load("workstation-a").gpus:
            self.assertIsNone(gpu.uma_total_mib)
            self.assertIsNone(gpu.uma_available_mib)
            self.assertIsNone(gpu.frac_of_pool)

    def test_no_denominator_without_a_source(self):
        """No /proc/meminfo, no pool total -- never a fallback guess."""
        text = (FX / "spark-2.txt").read_text().replace(
            "mem.MemTotal\t130023424", "mem.MemTotalX\t130023424")
        h = parse_payload(Host(name="x", target="x"), text, "", 0, 1.0)
        self.assertIsNone(h.gpus[0].uma_total_mib)
        self.assertIsNone(h.gpus[0].frac_of_pool)
        self.assertGreater(h.gpus[0].gpu_resident_mib, 30000)   # still measured

    def test_utilization_not_trusted_on_gb10(self):
        gpu = load("spark-2").gpus[0]
        self.assertFalse(gpu.util_trustworthy)

    def test_clock_ratio_is_never_used_as_utilization(self):
        """The trap: SM clock sits near 80% of max on a completely idle Spark.

        Substituting that ratio for utilization would show an idle machine as
        permanently busy, so `clock_percent` must stay separate from `util_gpu`.
        """
        gpu = load("spark-2").gpus[0]
        self.assertGreater(gpu.clock_percent, 70)
        self.assertEqual(gpu.util_gpu, 0)
        self.assertFalse(gpu.util_trustworthy)

    def test_gpu_memory_is_subset_of_rss_not_addition(self):
        """On coherent UMA the GPU allocation IS inside the process RSS.

        Measured: 33569 MiB of GPU memory against 37452 MiB of RSS for the same
        PID. Adding them would double count roughly 33 GiB.
        """
        gpu = load("spark-2").gpus[0]
        big = max(gpu.procs, key=lambda p: p.gpu_mem_mib or 0)
        self.assertIsNotNone(big.rss_kib)
        rss_mib = big.rss_kib / 1024
        self.assertLess(abs(rss_mib - big.gpu_mem_mib) / big.gpu_mem_mib, 0.30)

    def test_discrete_card_keeps_real_totals(self):
        gpu = load("workstation-a").gpus[0]
        self.assertFalse(gpu.is_unified)
        self.assertEqual(gpu.mem_total_mib, 49140)
        self.assertIsNotNone(gpu.mem_percent)
        self.assertTrue(gpu.util_trustworthy)


class TestProcesses(unittest.TestCase):
    def test_graphics_processes_are_included(self):
        """-q -d PIDS reports C and G; --query-compute-apps omits every G.

        Measured on this host: 9 processes versus 3.
        """
        h = load("workstation-a")
        types = {p.ptype for p in h.procs}
        self.assertIn("G", types)
        self.assertIn("C", types)
        self.assertGreaterEqual(len(h.procs), 9)

    def test_graphics_processes_are_enriched(self):
        """Every process, not just compute ones, gets user/cpu/rss/cmdline.

        cluster-nvtop took its ps PID list from --query-compute-apps, so every
        G row had an empty user and a 15-character truncated command.
        """
        h = load("workstation-a")
        gfx = [p for p in h.procs if p.ptype == "G"]
        self.assertTrue(gfx)
        for p in gfx:
            self.assertTrue(p.resolved, f"pid {p.pid} not resolved by ps")
            self.assertTrue(p.user)
            self.assertIsNotNone(p.cpu_percent)
            self.assertIsNotNone(p.rss_kib)
            self.assertTrue(p.cmdline)

    def test_every_pid_resolves(self):
        for name in ("workstation-a", "workstation-b", "workstation-c",
                     "workstation-d", "spark-2"):
            h = load(name)
            self.assertEqual(h.ps_requested, h.ps_resolved, name)
            self.assertFalse(h.pid_ns_mismatch, name)

    def test_full_binary_path_is_kept(self):
        h = load("workstation-a")
        pythons = [p for p in h.procs if "python" in p.name]
        self.assertTrue(pythons)
        self.assertIn("miniconda3/envs", pythons[0].name)

    def test_bus_id_join_is_exact(self):
        """GB10's bus id is 0000000F:01:00.0 -- uppercase hex domain.

        Case folding or zero stripping would break the process join.
        """
        h = load("spark-2")
        gpu = h.gpus[0]
        self.assertEqual(gpu.bus_id, "0000000F:01:00.0")
        self.assertTrue(gpu.procs)
        for p in gpu.procs:
            self.assertEqual(p.bus_id, gpu.bus_id)

    def test_processes_land_on_the_right_gpu(self):
        h = load("workstation-a")
        for gpu in h.gpus:
            for p in gpu.procs:
                self.assertEqual(p.bus_id, gpu.bus_id)


class TestClockEvents(unittest.TestCase):
    def test_bit_decoding(self):
        self.assertEqual([b[0] for b in decode_clock_events(0x1)], ["IDLE"])
        self.assertEqual([b[0] for b in decode_clock_events(0x4)], ["PWR"])
        self.assertEqual([b[0] for b in decode_clock_events(0xC0)], ["THRM!", "BRAKE!"])
        self.assertEqual(decode_clock_events(0x0), [])
        self.assertEqual(decode_clock_events(None), [])

    def test_idle_bit_is_not_an_alarm(self):
        self.assertFalse(any(a for _, _, a in decode_clock_events(0x1)))
        self.assertTrue(all(a for _, _, a in decode_clock_events(0xC0)))

    def test_idle_badge_hidden_from_compact_row(self):
        gpu = load("workstation-a").gpus[0]
        gpu.clock_events = 0x1
        self.assertEqual([b[0] for b in gpu.throttle_badges], ["IDLE"])
        self.assertEqual(gpu.display_badges, [])


class TestPressureAlerts(unittest.TestCase):
    def test_swap_pressure_detected_without_psi(self):
        """workstation-b: 8 GiB swapped out while PSI still reads 0.00.

        PSI alone would say everything is fine, so swap must be its own signal.
        """
        h = load("workstation-b")
        self.assertEqual(h.psi_mem.some10, 0.0)
        tags = {a.tag for a in host_alerts(h)}
        self.assertIn("SWAP", tags)
        self.assertIn("RAM", tags)
        crit = [a for a in host_alerts(h) if a.level is Level.CRIT]
        self.assertTrue(crit)

    def test_healthy_host_is_quiet(self):
        self.assertEqual(host_alerts(load("workstation-d")), [])

    def test_memavailable_not_memfree(self):
        """MemFree on the Spark is 3 GiB while MemAvailable is 50 GiB."""
        h = load("spark-2")
        self.assertLess(h.mem.free_kib, 5_000_000)
        self.assertGreater(h.mem.available_kib, 40_000_000)
        self.assertEqual(h.mem.used_kib, h.mem.total_kib - h.mem.available_kib)

    def test_dead_driver_is_not_reported_as_memory_pressure(self):
        h = load("spark-1")
        mem_tags = {"SWAP", "RAM", "PSI"}
        crit_mem = [a for a in host_alerts(h)
                    if a.level is Level.CRIT and a.tag in mem_tags]
        self.assertEqual(crit_mem, [])


class TestLinkAndFan(unittest.TestCase):
    def test_idle_link_width_is_not_flagged(self):
        """Every card downtrains at idle: gen 1 against a max of 4 or 5, and
        widths of x16, x8 and even x1. Flagging that fired on 3 of 6 healthy
        GeForce cards, so the check is gated on the GPU actually being busy."""
        for name in ("workstation-c", "workstation-d"):
            for gpu in load(name).gpus:
                if not gpu.busy:
                    self.assertFalse(gpu.link_degraded,
                                     f"{name}:{gpu.index} flagged while idle")

    def test_fan_zero_is_quiet_when_cool(self):
        """The RTX 5090s report fan=0 at ~50 C with nothing wrong."""
        for gpu in load("workstation-c").gpus:
            self.assertEqual(gpu.fan_percent, 0)
            self.assertLess(gpu.temp_c, 70)
            self.assertFalse(gpu.fan_suspect)

    def test_fan_zero_alarms_when_hot(self):
        gpu = load("workstation-c").gpus[0]
        gpu.temp_c = 78
        self.assertTrue(gpu.fan_suspect)
        self.assertIn("FAN", {a.tag for a in gpu_alerts(gpu)})

    def test_unified_part_never_flags_its_link(self):
        gpu = load("spark-2").gpus[0]
        self.assertEqual(gpu.pcie_width, 1)  # meaningless C2C reading
        self.assertFalse(gpu.link_degraded)


class TestFleetRollup(unittest.TestCase):
    def fleet(self) -> Fleet:
        names = ["workstation-a", "workstation-b", "workstation-c", "workstation-d",
                 "workstation-e", "spark-1", "spark-2"]
        return Fleet(hosts=[load(n) for n in names])

    def test_counts(self):
        c = self.fleet().counts()
        self.assertEqual(c["up"], 5)
        self.assertEqual(c["gpu_dead"], 1)
        self.assertEqual(c["down"], 1)

    def test_vram_total_excludes_unified(self):
        """A Spark's share of system RAM is not fleet VRAM."""
        f = self.fleet()
        used, total = f.vram_totals()
        discrete = sum(g.mem_total_mib for g in f.gpus if not g.is_unified)
        self.assertEqual(total, discrete)

    def test_uma_total_is_reported_separately_from_vram(self):
        """The two are a partition, never a sum.

        vram_totals() answers "how much real VRAM is left"; adding 122 GiB of
        system RAM into it would corrupt that number rather than enrich it.
        """
        f = self.fleet()
        res, total, hosts = f.uma_totals()
        self.assertEqual(hosts, 1)
        gb10b = next(h for h in f.hosts if h.name == "spark-2")
        self.assertAlmostEqual(total, gb10b.mem.total_kib / 1024, places=3)
        self.assertAlmostEqual(res, gb10b.gpus[0].gpu_resident_mib, places=3)
        self.assertEqual(f.vram_totals()[1],
                         sum(g.mem_total_mib for g in f.gpus if not g.is_unified))

    def test_uma_total_counts_each_host_pool_once(self):
        """A two-GPU unified box has ONE 128 GiB pool, not two.

        The fixture fleet has a single-GPU Spark, so this bug is invisible
        without synthesising the second GPU.
        """
        f = self.fleet()
        gb10b = next(h for h in f.hosts if h.name == "spark-2")
        before_res, before_total, before_hosts = f.uma_totals()
        import copy
        twin = copy.deepcopy(gb10b.gpus[0])
        twin.index = 1
        gb10b.gpus.append(twin)
        after_res, after_total, after_hosts = f.uma_totals()
        self.assertEqual(after_total, before_total)          # pool counted once
        self.assertEqual(after_hosts, before_hosts)
        self.assertAlmostEqual(after_res, before_res * 2, places=3)  # resident is per GPU
        # ...and the host line must agree with the fleet line, not re-derive it
        self.assertAlmostEqual(gb10b.uma_pool()[0], after_res, places=3)

    def test_uma_total_excludes_a_host_whose_driver_is_dead(self):
        """spark-1 is up with a healthy /proc but no readable GPU.

        Its pool must not be attributed to a GPU we cannot currently see, even
        though cached_gpu_label still says '1x GB10'.
        """
        f = self.fleet()
        gb10a = next(h for h in f.hosts if h.name == "spark-1")
        self.assertIs(gb10a.state, HostState.GPU_DEAD)
        self.assertGreater(gb10a.mem.total_kib, 0)
        gb10b = next(h for h in f.hosts if h.name == "spark-2")
        self.assertAlmostEqual(f.uma_totals()[1],
                               gb10b.mem.total_kib / 1024, places=3)

    def test_idle_excludes_memory_starved_host(self):
        """workstation-b has idle GPUs but only 7% of its RAM available."""
        f = self.fleet()
        free = idle_gpus(f)
        self.assertNotIn("workstation-b", {g.host for g in free})
        self.assertTrue(free)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestReplayRobustness(unittest.TestCase):
    """A junk file in the capture directory must not crash the tool.

    macOS tar emits AppleDouble `._name` companions; they are binary, they match
    `*.txt`, and reading one raised UnicodeDecodeError after a tarball round
    trip to Ubuntu.
    """

    def test_binary_junk_is_ignored(self):
        import shutil
        import tempfile
        sys.path.insert(0, str(ROOT))
        from gpumaster.cli import load_replay

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            for stem in ("workstation-a", "spark-2"):
                shutil.copy(FX / f"{stem}.txt", tmp_path / f"{stem}.txt")
                shutil.copy(FX / f"{stem}.stderr", tmp_path / f"{stem}.stderr")
            # AppleDouble companions, exactly as macOS tar creates them.
            (tmp_path / "._workstation-a.txt").write_bytes(
                b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        \x00\x02\x00\x00"
                b"\xcb\xcb\xcb\xcb\xff\xfe\xfd")
            (tmp_path / "._workstation-a.stderr").write_bytes(b"\xcb\xcb\x00\xff")

            fleet = load_replay(tmp_path)
            names = {h.name for h in fleet.hosts}
            self.assertEqual(names, {"workstation-a", "spark-2"})
            self.assertTrue(any(h.gpus for h in fleet.hosts))


class TestBackoffReporting(unittest.TestCase):
    def test_backoff_reports_why_not_just_when(self):
        """A backed-off host must still say what went wrong.

        The reason is produced once, on the failing attempt; every frame after
        that is served from the backoff branch. Replacing the message with a
        bare "retry in 2s" left a whole fleet of down hosts on screen with
        nothing to act on -- and with 7 hosts failing at once, the one frame
        carrying the reason is gone before it can be read.
        """
        import asyncio
        from gpumaster.transport import HostSpec, Poller, SshOptions

        poller = Poller(SshOptions(connect_timeout=2, command_timeout=4),
                        ROOT / "remote.sh")
        spec = HostSpec(name="ghost", target="no-such-host-xyz.invalid")

        first = asyncio.run(poller.poll_one(spec))
        self.assertIs(first.state, HostState.DOWN)
        self.assertTrue(first.error_msg)

        backed_off = asyncio.run(poller.poll_one(spec))
        self.assertIs(backed_off.state, HostState.DOWN)
        self.assertIn("retry in", backed_off.error_msg)
        self.assertIn(first.error_msg, backed_off.error_msg,
                      "the reason was dropped in favour of the countdown")
        self.assertEqual(backed_off.error_code, first.error_code)


class TestAppleSilicon(unittest.TestCase):
    """mac-studio: a Mac Studio (M2 Ultra, 192 GiB) sampled by remote.sh's Darwin
    branch through ioreg / sysctl / vm_stat, with no root.

    The fixture is a live capture; volatile figures (utilization, resident
    memory, load, uptime) are asserted by relationship, not by value.
    """

    def test_mac_streams_as_a_unified_part(self):
        h = load("mac-studio")
        self.assertIs(h.state, HostState.STREAMING)
        self.assertEqual(h.fieldset, "apple")
        self.assertEqual(h.hostname, "mac-studio")
        self.assertEqual(h.arch, "arm64")
        self.assertEqual(len(h.gpus), 1)
        gpu = h.gpus[0]
        self.assertTrue(gpu.is_apple)
        self.assertTrue(gpu.is_unified, "memory.total is [N/A] on Apple silicon")
        self.assertEqual(gpu.name, "Apple M2 Ultra")
        self.assertEqual(gpu.cores, 76)
        self.assertEqual(h.gpu_label, "1x M2 Ultra")

    def test_apple_utilization_is_trusted(self):
        """Unlike GB10, Device Utilization % moves with load and is shown."""
        gpu = load("mac-studio").gpus[0]
        self.assertTrue(gpu.util_trustworthy)
        self.assertIsNotNone(gpu.util_gpu)
        self.assertGreaterEqual(gpu.util_gpu, 0)
        self.assertLessEqual(gpu.util_gpu, 100)
        self.assertIs(gpu.history_percent, gpu.util_gpu)

    def test_apple_resident_is_read_not_derived(self):
        """'In use system memory' arrives as memory.used and is used as-is;
        there is no per-process list to sum, and the value must not be 0."""
        gpu = load("mac-studio").gpus[0]
        self.assertFalse(gpu.procs)
        self.assertIsNotNone(gpu.mem_used_mib)
        self.assertGreater(gpu.mem_used_mib, 0)
        self.assertEqual(gpu.gpu_resident_mib, gpu.mem_used_mib)
        self.assertFalse(gpu.mem_derived, "a directly read value must not carry '*'")
        # The driver reservation is a superset of what is in use.
        self.assertIsNotNone(gpu.mem_reserved_mib)
        self.assertGreaterEqual(gpu.mem_reserved_mib, gpu.mem_used_mib)

    def test_gb10_semantics_are_untouched(self):
        """The Apple path must not change how a Spark is read."""
        gpu = load("spark-2").gpus[0]
        self.assertFalse(gpu.is_apple)
        self.assertFalse(gpu.util_trustworthy)
        self.assertTrue(gpu.mem_derived)
        self.assertIsNone(gpu.mem_used_mib)
        self.assertEqual(gpu.gpu_resident_mib,
                         sum(p.gpu_mem_mib for p in gpu.procs if p.gpu_mem_mib))

    def test_gb10_ignores_a_numeric_memory_used(self):
        """A future Spark driver that reports memory.used=0 instead of [N/A]
        must not replace the per-process sum with a zero. The direct read is
        keyed on vendor=apple, never on the value merely being present."""
        text = (FX / "spark-2.txt").read_text()
        row = next(l for l in text.splitlines() if l.startswith("row\t"))
        fields = next(l for l in text.splitlines() if l.startswith("fields\t"))
        names = fields.split("\t", 1)[1].split(",")
        cells = row[4:].split(", ")
        cells[names.index("memory.used")] = "0"
        patched = text.replace(row, "row\t" + ", ".join(cells))
        h = parse_payload(Host(name="gb", target="gb"), patched, "", 0, 1.0)
        gpu = h.gpus[0]
        self.assertEqual(gpu.mem_used_mib, 0.0)
        self.assertTrue(gpu.is_unified)
        self.assertTrue(gpu.mem_derived)
        self.assertGreater(gpu.gpu_resident_mib, 30000, "the process sum must still win")
        self.assertTrue(gpu.busy)

    def test_idle_mac_is_offered_by_gpumaster_free(self):
        """An idle Mac Studio still has 2-3 GiB resident for WindowServer, so
        the GB10 rule 'resident > 1 GiB means busy' would hide every Mac from
        `gpumaster free` forever. Apple utilization is trusted, so it decides."""
        mac = load("mac-studio")
        gb = load("spark-2")
        fleet = Fleet(hosts=[mac, gb])
        mac_gpu = mac.gpus[0]
        self.assertGreater(mac_gpu.gpu_resident_mib, 1024, "fixture no longer exercises the case")
        self.assertLessEqual(mac_gpu.util_gpu, 5.0, "fixture was captured under GPU load")
        free = idle_gpus(fleet)
        self.assertIn(mac_gpu, free)
        self.assertNotIn(gb.gpus[0], free, "a Spark holding 33 GiB is not idle")
        self.assertFalse(mac_gpu.busy, "busy and idle_gpus must agree about the same row")

    def test_pool_is_hw_memsize_and_partitions(self):
        h = load("mac-studio")
        gpu = h.gpus[0]
        self.assertEqual(h.mem.total_kib, 201326592)          # 192 GiB, hw.memsize
        self.assertEqual(gpu.uma_total_mib, 196608.0)
        self.assertIsNotNone(h.mem.available_kib)
        self.assertLess(h.mem.available_kib, h.mem.total_kib)
        self.assertLessEqual(h.mem.free_kib, h.mem.available_kib,
                             "MemAvailable must include more than free pages")
        res, total, avail, other = h.uma_pool()
        self.assertEqual(total, 196608.0)
        self.assertGreaterEqual(other, 0)
        self.assertAlmostEqual(res + other + avail, total, delta=1)
        self.assertIsNone(gpu.mem_percent, "no VRAM percentage on a unified part")

    def test_host_block_is_sane_without_proc(self):
        h = load("mac-studio")
        self.assertEqual(h.ncpu, 24)
        self.assertIsNotNone(h.load1)
        self.assertIsNotNone(h.uptime_s)
        self.assertGreater(h.uptime_s, 0)
        self.assertLess(h.uptime_s, 10 * 365 * 86400, "kern.boottime mis-parsed")
        # macOS swap is dynamic (a swapfile appears under pressure and stays
        # until reboot), so pin the parse, not the value.
        self.assertIsNotNone(h.mem.swap_total_kib)
        self.assertIsNotNone(h.mem.swap_free_kib)
        self.assertLessEqual(h.mem.swap_free_kib, h.mem.swap_total_kib)
        self.assertIsNone(h.psi_mem.some10, "Darwin has no PSI; never invent one")
        self.assertEqual(h.ps_requested, 0)
        self.assertEqual(h.ps_resolved, 0)
        self.assertFalse(h.pid_ns_mismatch)
        self.assertIsNotNone(h.remote_ms)
        self.assertLess(h.remote_ms, 5000)

    def test_apple_raises_no_gpu_alert_and_only_real_host_alerts(self):
        h = load("mac-studio")
        self.assertEqual(gpu_alerts(h.gpus[0]), [])
        tags = {a.tag for a in host_alerts(h)}
        self.assertNotIn("GPU-DEAD", tags)
        self.assertNotIn("SWAP", tags)
        self.assertNotIn("PSI", tags)

    def test_unknown_fields_are_ignored_by_older_clients(self):
        """vendor / gpu.cores are extra columns; a parser that does not know
        them must still index every other field by name."""
        text = (FX / "mac-studio.txt").read_text()
        text = text.replace("vendor,", "future.field,").replace(", apple,", ", whatever,")
        h = parse_payload(Host(name="m", target="m"), text, "", 0, 1.0)
        self.assertIs(h.state, HostState.STREAMING)
        gpu = h.gpus[0]
        self.assertEqual(gpu.name, "Apple M2 Ultra")
        self.assertEqual(gpu.cores, 76)
        self.assertFalse(gpu.is_apple)
        # Without the vendor hint it degrades to GB10 rules: shown as '?'.
        self.assertFalse(gpu.util_trustworthy)

    def test_macmon_enrichment_parses_temp_power_clocks_and_ane(self):
        """When macmon is installed on Darwin, remote.sh appends
        temperature.gpu,power.draw,clocks.sm,ane.power; when absent, [N/A] -> None."""
        text = (FX / "mac-studio.txt").read_text()
        orig_row = "row\t0, APPLE-mac-studio-0, Apple M2 Ultra, apple:0, 15.5.0, apple, 4, [N/A], 2769, 6889, 76\n"
        enriched = text.replace(
            "fields\tindex,uuid,name,pci.bus_id,driver_version,vendor,utilization.gpu,memory.total,memory.used,memory.reserved,gpu.cores\n",
            "fields\tindex,uuid,name,pci.bus_id,driver_version,vendor,utilization.gpu,memory.total,memory.used,memory.reserved,gpu.cores,temperature.gpu,power.draw,clocks.sm,ane.power\n",
        ).replace(
            orig_row,
            "row\t0, APPLE-mac-studio-0, Apple M2 Ultra, apple:0, 15.5.0, apple, 24, [N/A], 2769, 6889, 76, 48.5, 19.2, 1398, 2.4\n",
        )
        h = parse_payload(Host(name="m2u", target="m2u"), enriched, "", 0, 1.0)
        gpu = h.gpus[0]
        self.assertTrue(gpu.is_apple)
        self.assertAlmostEqual(gpu.temp_c, 48.5)
        self.assertAlmostEqual(gpu.power_w, 19.2)
        self.assertAlmostEqual(gpu.clock_sm, 1398.0)
        self.assertAlmostEqual(gpu.ane_power_w, 2.4)

        # Fallback when macmon is not installed: [N/A] cells become None
        fallback = text.replace(
            "fields\tindex,uuid,name,pci.bus_id,driver_version,vendor,utilization.gpu,memory.total,memory.used,memory.reserved,gpu.cores\n",
            "fields\tindex,uuid,name,pci.bus_id,driver_version,vendor,utilization.gpu,memory.total,memory.used,memory.reserved,gpu.cores,temperature.gpu,power.draw,clocks.sm,ane.power\n",
        ).replace(
            orig_row,
            "row\t0, APPLE-mac-studio-0, Apple M2 Ultra, apple:0, 15.5.0, apple, 4, [N/A], 2769, 6889, 76, [N/A], [N/A], [N/A], [N/A]\n",
        )
        h2 = parse_payload(Host(name="m2u", target="m2u"), fallback, "", 0, 1.0)
        gpu2 = h2.gpus[0]
        self.assertIsNone(gpu2.temp_c)
        self.assertIsNone(gpu2.power_w)
        self.assertIsNone(gpu2.clock_sm)
        self.assertIsNone(gpu2.ane_power_w)

    def test_macmon_scientific_notation_and_darwin_path_guard(self):
        """Regression test for FLT-006 adversarial audit:
        1. Darwin remote.sh must add /opt/homebrew/bin to PATH for non-login SSH.
        2. serde_json scientific notation (e.g. 1.5258789e-5) must NOT be
           truncated before 'e-5' into 1.5258789 W, and 0 temp/clock -> [N/A].
        """
        import subprocess
        from gpumaster.parse import num

        script_text = (ROOT / "remote.sh").read_text()
        self.assertIn('/opt/homebrew/bin:/usr/local/bin', script_text)
        self.assertAlmostEqual(num("1.5258789e-5"), 1.5258789e-5, places=9)

        # Extract the awk block from remote.sh and feed it synthetic ioreg + macmon JSON
        awk_start = script_text.index("function grab(key")
        awk_end = script_text.index("END { flush() }'") + len("END { flush() }")
        awk_prog = script_text[awk_start:awk_end]
        fake_ioreg = (
            '+-o AGXAcceleratorG14X  <class AGXAcceleratorG14X>\n'
            '  "model" = "Apple M2 Ultra"\n'
            '  "gpu-core-count" = 76\n'
            '  "PerformanceStatistics" = {"Device Utilization %"=12,"In use system memory"=209715200,"Alloc system memory"=419430400}\n'
        )
        fake_mm = '{"temp":{"cpu_temp_avg":42.1,"gpu_temp_avg":0.0},"gpu_power":0.425,"ane_power":1.5258789e-5,"gpu_usage":[0,0.0]}'
        cp = subprocess.run(
            ["awk", "-v", "hn=m2u", "-v", "osver=15.5", "-v", "chip=Apple M2 Ultra", awk_prog],
            input=fake_ioreg,
            text=True,
            capture_output=True,
            env={"PATH": "/usr/bin:/bin", "MM": fake_mm},
            check=True,
        )
        row = cp.stdout.strip()
        # Fields: idx, uuid, name, bus, drv, vendor, util, mem.tot, mem.used, mem.res, cores, temp, pwr, clk, ane
        cells = row.split("\t", 1)[1].split(", ")
        self.assertEqual(cells[11], "[N/A]", "0.0 C temp must become [N/A]")
        self.assertEqual(cells[12], "0.42", "gpu_power 0.425 rounds cleanly")
        self.assertEqual(cells[13], "[N/A]", "0 MHz clock must become [N/A]")
        self.assertEqual(cells[14], "0.00", "1.5258789e-5 W must become 0.00, never 1.5258789")


class TestModes(unittest.TestCase):
    """A mode is a named host set. Two are built in: "default" is every host
    not marked `hidden` -- a laptop that is asleep most of the time would sit
    there as a permanent [x] -- and "all" is every host. The rest come from
    "modes" in hosts.json and list their hosts by name.
    """

    RAW = {
        "hosts": [
            {"name": "rack", "target": "workstation-d", "tags": ["geforce"]},
            {"name": "lap", "target": "linux-laptop"},
            {"name": "mac", "target": "mac-laptop", "hidden": True},
        ],
        # Listed out of config order on purpose, and "rack" by its target.
        "modes": {"laptop": ["mac", "lap"], "racks": ["workstation-d"]},
    }

    def _load(self, raw=None):
        import json
        import tempfile
        from gpumaster.config import load_config

        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "hosts.json"
            p.write_text(json.dumps(raw if raw is not None else self.RAW))
            return load_config(p)

    def _names(self, cfg, mode):
        cfg.mode = mode
        return [h.name for h in cfg.active_hosts()]

    def test_default_mode_leaves_out_hidden_hosts(self):
        from unittest.mock import patch

        cfg = self._load()
        self.assertEqual([h.name for h in cfg.hosts], ["rack", "lap", "mac"],
                         "every entry must stay loaded, filtered only at use")
        self.assertEqual(cfg.mode, "default")
        self.assertEqual(cfg.mode_label, "")
        with patch("gpumaster.config.Poller.is_local_target", return_value=False):
            self.assertEqual([h.name for h in cfg.active_hosts()], ["rack", "lap"])
        # When running on the hidden laptop itself (is_local_target == True),
        # default mode must keep the local machine visible.
        with patch("gpumaster.config.Poller.is_local_target", side_effect=lambda h: h.name == "mac"):
            self.assertEqual([h.name for h in cfg.active_hosts()], ["rack", "lap", "mac"])

    def test_named_mode_shows_its_hosts_in_config_order(self):
        cfg = self._load()
        self.assertEqual(self._names(cfg, "laptop"), ["lap", "mac"],
                         "a hidden host still shows in a mode that lists it")
        self.assertEqual(cfg.mode_label, "laptop")
        self.assertEqual(self._names(cfg, "racks"), ["rack"])

    def test_all_mode_includes_hidden_hosts(self):
        self.assertEqual(self._names(self._load(), "all"), ["rack", "lap", "mac"])

    def test_mode_names_run_default_then_file_order_then_all(self):
        self.assertEqual(self._load().mode_names, ["default", "laptop", "racks", "all"])

    def test_unknown_host_in_a_mode_is_a_warning(self):
        cfg = self._load({"hosts": ["a"], "modes": {"x": ["a", "typo"]}})
        self.assertTrue(any("'typo'" in w for w in cfg.warnings), cfg.warnings)

    def test_absent_keys_change_nothing(self):
        """A hosts.json with neither `hidden` nor `modes` shows every host."""
        cfg = self._load({"hosts": [{"name": "a", "target": "t"}, "bare-string"]})
        self.assertFalse(any(h.hidden for h in cfg.hosts))
        self.assertEqual(len(cfg.active_hosts()), 2)
        self.assertEqual(cfg.mode_names, ["default", "all"])

    def test_check_and_probe_can_still_reach_a_hidden_host(self):
        """The lookup for `gpumaster check <host>` reads cfg.hosts, not the
        filtered list, so a laptop keeps its tags and ssh_args."""
        from gpumaster.cli import _find_spec

        cfg = self._load()
        spec = _find_spec(cfg, "mac-laptop")
        self.assertEqual(spec.name, "mac")
        self.assertTrue(spec.hidden)

    def test_mode_flags(self):
        import contextlib
        import io
        from gpumaster.cli import parse_args

        self.assertIsNone(parse_args([]).mode)
        for name in ("mobile", "desktop", "laptop", "all"):
            self.assertEqual(parse_args([f"--{name}"]).mode, name)
        self.assertEqual(parse_args(["--mode", "racks"]).mode, "racks")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["--mobile", "--laptop"])

    def test_unknown_mode_is_refused_before_any_ssh(self):
        import contextlib
        import io
        import json
        import tempfile
        from gpumaster.cli import main

        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "hosts.json"
            p.write_text(json.dumps(self.RAW))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = main(["--once", "-c", str(p), "--mode", "nope"])
        self.assertEqual(rc, 2)
        self.assertIn("no mode 'nope'", err.getvalue())
        self.assertIn("default, laptop, racks, all", err.getvalue())

    def test_example_config_modes(self):
        """examples/gpumaster.hosts.json is a valid mode map with one hidden laptop."""
        from unittest.mock import patch
        from gpumaster.config import load_config

        cfg = load_config(REPO / "examples" / "gpumaster.hosts.json")
        self.assertEqual(cfg.warnings, [])
        self.assertEqual(cfg.mode_names,
                         ["default", "mobile", "desktop", "laptop", "all"])
        with patch("gpumaster.config.Poller.is_local_target", return_value=False):
            self.assertEqual(self._names(cfg, "default"),
                             ["train-a", "train-b", "spark", "studio"])
        with patch("gpumaster.config.Poller.is_local_target",
                   side_effect=lambda h: h.name == "laptop"):
            self.assertEqual(self._names(cfg, "default"),
                             ["train-a", "train-b", "spark", "studio", "laptop"])
        self.assertEqual(self._names(cfg, "mobile"),
                         ["spark", "studio", "laptop"])
        self.assertEqual(self._names(cfg, "desktop"),
                         ["train-a", "train-b", "spark"])
        self.assertEqual(self._names(cfg, "laptop"), ["laptop"])
        self.assertEqual(self._names(cfg, "all"),
                         ["train-a", "train-b", "spark", "studio", "laptop"])


class TestHighSpeedFabric(unittest.TestCase):
    """ConnectX-7 200GbE / RoCEv2 high-speed fabric telemetry (FLT-005)."""

    def _inject_net(self, fixture: str, net_lines: list[str]) -> Host:
        raw = (FX / f"{fixture}.txt").read_text()
        block = "__PSI__\n__NET__\n" + "\n".join(net_lines) + "\n"
        patched = raw.replace("__PSI__\n", block, 1)
        return parse_payload(Host(name=fixture, target=fixture), patched, "", 0, 5.0)

    def test_dual_rail_connectx7_healthy(self):
        h = self._inject_net(
            "spark-2",
            [
                "if\tenp1s0f0np0\tup\t100000\t9000\t12500000000\t25000000000\trocep1s0f0",
                "if\tenp1s0f1np1\tup\t100000\t9000\t12500000000\t25000000000\trocep1s0f1",
            ],
        )
        self.assertEqual(len(h.net_links), 2)
        self.assertTrue(all(nl.up for nl in h.net_links))
        self.assertEqual(h.net_links[0].speed_gbps, 100)
        self.assertEqual(h.net_links[0].rdma_dev, "rocep1s0f0")
        self.assertEqual(h.hs_fabric_summary(), "RDMA 2x100G up")
        self.assertEqual(host_alerts(h), [])

    def test_partial_rail_down_and_invalid_sysfs_speeds(self):
        """A downed Linux NIC often reports -1, 65535, or 4294967295 in sysfs
        speed. That must parse to None (never a fake negative or 4294 Gbps),
        and 1/2 rails up must raise a WARN RDMA alert."""
        h = self._inject_net(
            "spark-2",
            [
                "if\tenp1s0f0np0\tup\t100000\t9000\t1000\t2000\trocep1s0f0",
                "if\tenp1s0f1np1\tdown\t-1\t9000\t0\t0\trocep1s0f1",
            ],
        )
        self.assertEqual(len(h.net_links), 2)
        self.assertTrue(h.net_links[0].up)
        self.assertFalse(h.net_links[1].up)
        self.assertIsNone(h.net_links[1].speed_mbps)
        self.assertIsNone(h.net_links[1].speed_gbps)
        self.assertEqual(h.hs_fabric_summary(), "RDMA 1/2 rails!")
        alerts = host_alerts(h)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].tag, "RDMA")
        self.assertEqual(alerts[0].detail, "1/2 rails up")

        # Also check uint16/uint32 -1 sentinels (65535 and 4294967295)
        h2 = self._inject_net(
            "spark-2",
            [
                "if\tenp1s0f0np0\tdown\t4294967295\t9000\t0\t0\trocep1s0f0",
                "if\tenp1s0f1np1\tdown\t65535\t9000\t0\t0\trocep1s0f1",
            ],
        )
        self.assertIsNone(h2.net_links[0].speed_mbps)
        self.assertIsNone(h2.net_links[1].speed_mbps)
        # Both unplugged (0/2 up): no false-positive alert
        self.assertEqual(h2.hs_fabric_summary(), "RDMA 0/2 up")
        self.assertEqual(host_alerts(h2), [])

    def test_roce_mtu_below_9000_triggers_mtu_alert(self):
        h = self._inject_net(
            "spark-2",
            [
                "if\tenp1s0f0np0\tup\t100000\t1500\t1000\t2000\trocep1s0f0",
                "if\tenp1s0f1np1\tup\t100000\t9000\t1000\t2000\trocep1s0f1",
            ],
        )
        alerts = host_alerts(h)
        self.assertEqual([a.tag for a in alerts], ["MTU"])
        self.assertIn("enp1s0f0np0 mtu 1500 < 9000", alerts[0].detail)

    def test_poller_computes_live_gbps_and_ignores_counter_reset(self):
        from gpumaster.transport import Poller, SshOptions

        poller = Poller(SshOptions(multiplex=False), ROOT / "remote.sh")
        h1 = self._inject_net(
            "spark-2",
            [
                "if\tenp1s0f0np0\tup\t100000\t9000\t1000000000\t2000000000\trocep1s0f0",
            ],
        )
        poller._update_net_rates(h1, 100.0)
        self.assertIsNone(h1.net_links[0].rx_gbps, "first sample has no delta")

        # 2 seconds later: +12.5 GB rx (50 Gbps) and +12.5 GB tx (50 Gbps) -> 100.0 Gbps total
        h2 = self._inject_net(
            "spark-2",
            [
                "if\tenp1s0f0np0\tup\t100000\t9000\t13500000000\t14500000000\trocep1s0f0",
            ],
        )
        poller._update_net_rates(h2, 102.0)
        self.assertAlmostEqual(h2.net_links[0].rx_gbps, 50.0, places=2)
        self.assertAlmostEqual(h2.net_links[0].tx_gbps, 50.0, places=2)
        self.assertAlmostEqual(h2.net_links[0].total_gbps, 100.0, places=2)
        self.assertEqual(h2.hs_fabric_summary(), "RDMA 100G 100.0Gbps")

        # Counter reset (reboot): rx_bytes drops below previous value -> None, never negative
        h3 = self._inject_net(
            "spark-2",
            [
                "if\tenp1s0f0np0\tup\t100000\t9000\t500\t500\trocep1s0f0",
            ],
        )
        poller._update_net_rates(h3, 104.0)
        self.assertIsNone(h3.net_links[0].rx_gbps)
        self.assertIsNone(h3.net_links[0].tx_gbps)


class TestContainerAndStaleProcs(unittest.TestCase):
    """Container attribution (`cg\\t<pid>\\t<name>`) and stale allocation (`IDLE-HOLD`) detection (FLT-007)."""

    def test_parse_etime_seconds(self):
        from gpumaster.model import parse_etime_seconds

        self.assertEqual(parse_etime_seconds("04:58"), 298)
        self.assertEqual(parse_etime_seconds("02:15:30"), 8130)
        self.assertEqual(parse_etime_seconds("1-14:05:27"), 137127)
        self.assertIsNone(parse_etime_seconds("-"))
        self.assertIsNone(parse_etime_seconds(""))
        self.assertIsNone(parse_etime_seconds("bad-time"))

    def test_cgroup_container_attribution_and_stale_detection(self):
        from gpumaster.model import is_stale_proc

        raw = (FX / "workstation-a.txt").read_text()
        # Inject container attribution for PID 2296157 in __PS__ and set its CPU% to 0.0
        patched = raw.replace(
            "row\t2296157 user    14.0 ",
            "row\t2296157 user     0.0 ",
        ).replace(
            "__END__\t",
            "cg\t2296157\tvllm-prod\n__END__\t",
        )
        h = parse_payload(Host(name="workstation-a", target="workstation-a"), patched, "", 0, 10.0)
        p_map = {p.pid: p for p in h.procs}
        self.assertIn(2296157, p_map)
        self.assertEqual(p_map[2296157].container, "vllm-prod")

        # GPU 0 on workstation-a has util_gpu=0 and PID 2296157 holding 33.8 GiB
        # for 05:49:06 with 0.0% CPU -> stale allocation
        gpu0 = h.gpus[0]
        self.assertEqual(len(gpu0.stale_procs), 1)
        self.assertEqual(gpu0.stale_procs[0].pid, 2296157)
        self.assertTrue(is_stale_proc(gpu0, gpu0.stale_procs[0]))

    def test_gb10_never_accused_of_stale_alloc(self):
        """On DGX Spark (GB10), utilization.gpu is 0 even during active 33 GiB jobs.
        Since util_trustworthy is False, is_stale_proc must NEVER return True."""
        from gpumaster.model import is_stale_proc

        h = load("spark-2")
        gpu = h.gpus[0]
        self.assertFalse(gpu.util_trustworthy)
        for p in gpu.procs:
            p.cpu_percent = 0.0
            p.etime = "2-00:00:00"
            self.assertFalse(is_stale_proc(gpu, p))
        self.assertEqual(gpu.stale_procs, [])

    def test_remote_sh_cgroup_awk_portable_without_interval_quantifiers(self):
        """Regression test for FLT-007 adversarial review: mawk on Ubuntu/Pop!_OS
        does not support `{64}` regex interval quantifiers without POSIX flags.
        Verify the exact awk snippets in remote.sh extract 64-hex container IDs
        and map them via DMAP or 12-char fallback."""
        import subprocess

        script_text = (ROOT / "remote.sh").read_text()
        self.assertNotIn("[0-9a-f]{64}", script_text)

        cg_awk = (
            'match($0, "(docker[-/]|(cri-)?containerd[-/]|libpod-(conmon-)?)[0-9a-f]+") {\n'
            '  s = substr($0, RSTART, RLENGTH)\n'
            '  sub("^.*[-/]", "", s)\n'
            '  if (length(s) == 64) { print s; exit }\n'
            '}'
        )
        cid64 = "a1b2c3d4e5f60123456789abcdef0123456789abcdef0123456789abcdef0123"
        for line in (
            f"0::/system.slice/docker-{cid64}.scope\n",
            f"0::/docker/{cid64}\n",
            f"0::/kubepods.slice/cri-containerd-{cid64}.scope\n",
            f"0::/machine.slice/libpod-{cid64}.scope\n",
        ):
            cp = subprocess.run(["awk", cg_awk], input=line, text=True, capture_output=True, check=True)
            self.assertEqual(cp.stdout.strip(), cid64)

        # Short or non-container cgroup lines must produce nothing
        for non_cg in (
            "0::/user.slice/user-1000.slice/session-2.scope\n",
            "0::/system.slice/docker-1234567890abcdef.scope\n",
        ):
            cp = subprocess.run(["awk", cg_awk], input=non_cg, text=True, capture_output=True, check=True)
            self.assertEqual(cp.stdout.strip(), "")


class TestFreeGpuSelector(unittest.TestCase):
    """`gpumaster free` device selector: --min-mem, --count, --format table|env|ssh|json (FLT-008)."""

    def _fleet(self) -> Fleet:
        names = ["workstation-a", "workstation-b", "workstation-c", "workstation-d",
                 "workstation-e", "spark-1", "spark-2", "mac-studio"]
        return Fleet(hosts=[load(n) for n in names])

    def test_select_free_gpus_min_mem_and_count(self):
        from gpumaster.model import Gpu, select_free_gpus

        f = self._fleet()
        # Default: workstation-a has 2 idle 48G cards (#2, #3), workstation-c has 1 idle 32G card (#1),
        # workstation-d has 1 idle 24G card (#0), mac-studio has 1 idle 512G UMA GPU (#0).
        all_free = select_free_gpus(f)
        self.assertIn("workstation-a", all_free)
        self.assertEqual([g.index for g in all_free["workstation-a"]], [2, 3])
        self.assertIn("mac-studio", all_free)

        # Require >= 2 GPUs on a single host with >= 40 GiB free each -> only workstation-a qualifies
        two_40g = select_free_gpus(f, min_mem_gib=40.0, count=2)
        self.assertEqual(list(two_40g.keys()), ["workstation-a"])
        self.assertEqual([g.index for g in two_40g["workstation-a"]], [2, 3])

        # cuda_only=True excludes Apple silicon so CUDA_VISIBLE_DEVICES never targets macOS
        cuda_free = select_free_gpus(f, min_mem_gib=20.0, count=1, cuda_only=True)
        self.assertNotIn("mac-studio", cuda_free)
        self.assertEqual(next(iter(cuda_free.keys())), "workstation-a")

        # Unified host pool is not double-counted when count=2
        u_host = Host(name="dual-uma", target="dual-uma", state=HostState.STREAMING)
        u_host.mem.total_kib = 128 * 1048576
        u_host.mem.available_kib = 80 * 1048576
        g0 = Gpu(host="dual-uma", index=0, name="NVIDIA GB10", uma_total_mib=131072.0, uma_available_mib=81920.0)
        g1 = Gpu(host="dual-uma", index=1, name="NVIDIA GB10", uma_total_mib=131072.0, uma_available_mib=81920.0)
        u_host.gpus = [g0, g1]
        # 2x 35 GiB (70 GiB total) fits in 80 GiB pool:
        self.assertIn("dual-uma", select_free_gpus(Fleet(hosts=[u_host]), min_mem_gib=35.0, count=2))
        # 2x 50 GiB (100 GiB total) does NOT fit in 80 GiB shared pool:
        self.assertEqual(select_free_gpus(Fleet(hosts=[u_host]), min_mem_gib=50.0, count=2), {})

    def test_cmd_free_formats_and_exit_codes(self):
        import contextlib
        import io
        import json
        from gpumaster.cli import cmd_free

        f = self._fleet()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cmd_free(f, 120, min_mem_gib=40.0, count=2, fmt="env")
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "CUDA_VISIBLE_DEVICES=2,3")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cmd_free(f, 120, min_mem_gib=40.0, count=2, fmt="ssh")
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "ssh workstation-a CUDA_VISIBLE_DEVICES=2,3")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cmd_free(f, 120, min_mem_gib=40.0, count=2, fmt="json")
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["host"], "workstation-a")
        self.assertEqual(data[0]["cuda_visible_devices"], "2,3")

        # Unsatisfiable constraint returns exit code 1 and empty stdout for env
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cmd_free(f, 120, min_mem_gib=80.0, count=4, fmt="env")
        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("no CUDA host", err.getvalue())

        # Host filter targets a specific host; Apple silicon sets cuda_visible_devices to None in json
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cmd_free(f, 120, fmt="json", host_filter="mac-studio")
        self.assertEqual(rc, 0)
        mac_data = json.loads(out.getvalue())
        self.assertEqual(len(mac_data), 1)
        self.assertEqual(mac_data[0]["host"], "mac-studio")
        self.assertIsNone(mac_data[0]["cuda_visible_devices"])

    def test_render_free_matches_gpu_free_mib_and_main_host_arg(self):
        """Regression test for FLT-008 adversarial audit:
        1. `render_free()` must use `gpu_free_mib()` (`memory.free`) so its GiB figure
           matches `--min-mem` (e.g. 46.6 GiB on workstation-a, not 47.3 GiB).
        2. `main()` must pass `args.arg` (`host_filter`) in both `--replay` and live
           modes, and reach `hidden: true` hosts when explicitly requested.
        """
        import contextlib
        import io
        from unittest.mock import patch
        from gpumaster.config import Config, HostSpec
        from gpumaster.cli import main
        from gpumaster.render import lines_to_text, render_free

        f = self._fleet()
        txt = "\n".join(lines_to_text(render_free(f, 120)))
        self.assertIn("46.6 GiB free each", txt)
        self.assertNotIn("47.3 GiB free each", txt)

        # CLI --replay with positional host filter
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(["--replay", str(FX), "free", "workstation-c", "--format", "env"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "CUDA_VISIBLE_DEVICES=1")

        # Live path reaches hidden host in cfg.hosts and forwards args.arg to cmd_free
        cfg = Config(hosts=[
            HostSpec(name="workstation-a", target="workstation-a"),
            HostSpec(name="workstation-c", target="workstation-c", hidden=True),
        ])
        polled_specs = []

        def fake_collect_once(_cfg, _poller, specs=None):
            polled_specs.extend(specs or [])
            return Fleet(hosts=[load("workstation-c")])

        out = io.StringIO()
        with patch("gpumaster.cli.load_config", return_value=cfg), \
             patch("gpumaster.cli.collect_once", side_effect=fake_collect_once), \
             contextlib.redirect_stdout(out):
            rc = main(["free", "workstation-c", "--format", "env"])
        self.assertEqual(rc, 0)
        self.assertEqual([s.name for s in polled_specs], ["workstation-c"])
        self.assertEqual(out.getvalue().strip(), "CUDA_VISIBLE_DEVICES=1")


class TestLocalFastPathAndDiskAlert(unittest.TestCase):
    """Localhost SSH-bypass fast-path and root disk space `DISK` alert (FLT-010)."""

    def test_is_local_target_and_exec_argv(self):
        from pathlib import Path
        from gpumaster.transport import HostSpec, Poller, SshOptions

        poller = Poller(
            SshOptions(multiplex=False),
            ROOT / "remote.sh",
        )
        for tgt in ("localhost", "127.0.0.1", "::1"):
            spec = HostSpec(name="local", target=tgt)
            self.assertTrue(Poller.is_local_target(spec))
            self.assertEqual(poller.exec_argv(spec), ["bash", "-s"])

        # Tagged "local" without ssh_args or user@ prefix also uses local fast-path
        tagged = HostSpec(name="studio", target="mac-studio", tags=["local"])
        self.assertTrue(Poller.is_local_target(tagged))
        self.assertEqual(poller.exec_argv(tagged), ["bash", "-s"])

        # Port-forwarded SSH tunnel or explicit user@ must NOT bypass SSH
        tunneled = HostSpec(name="tun", target="localhost", ssh_args=["-p", "2222"])
        self.assertFalse(Poller.is_local_target(tunneled))
        self.assertEqual(poller.exec_argv(tunneled)[0], "ssh")

        user_at = HostSpec(name="root-loc", target="root@localhost")
        self.assertFalse(Poller.is_local_target(user_at))
        self.assertEqual(poller.exec_argv(user_at)[0], "ssh")

        # Auto-detection by local hostname, IP, or Darwin Apple Silicon chip alias
        from unittest.mock import patch
        with patch("gpumaster.transport._local_identities", return_value={"localhost", "mbp.local", "10.0.0.8", "m2x", "mac-laptop"}), \
             patch("gpumaster.transport._ssh_config_hostnames", return_value={"my-alias": "10.0.0.8"}):
            self.assertTrue(Poller.is_local_target(HostSpec(name="m2x", target="mac-laptop")))
            self.assertTrue(Poller.is_local_target(HostSpec(name="box", target=" my-alias ")))
            self.assertFalse(Poller.is_local_target(HostSpec(name="m1u", target="mac-studio")))

    def test_disk_parsing_and_alerts(self):
        from gpumaster.cli import fleet_to_json
        from gpumaster.render import GLYPHS_BLOCK, lines_to_text, render_gpu_detail

        raw = (FX / "workstation-a.txt").read_text().replace(
            "mem.SwapFree\t19745764\n",
            "mem.SwapFree\t19745764\ndisk.total\t104857600\ndisk.avail\t3145728\n",
        )
        h = parse_payload(Host(name="workstation-a", target="workstation-a"), raw, "", 0, 10.0)
        self.assertEqual(h.mem.disk_total_kib, 104857600)  # 100 GiB
        self.assertEqual(h.mem.disk_avail_kib, 3145728)    # 3.0 GiB (3%)
        self.assertAlmostEqual(h.mem.disk_avail_gib, 3.0, places=2)
        alerts = {a.tag: a for a in host_alerts(h)}
        self.assertIn("DISK", alerts)
        self.assertEqual(alerts["DISK"].level, Level.WARN)

        # Critical when < 2 GiB free
        h.mem.disk_avail_kib = int(1.5 * 1048576)
        alerts_crit = {a.tag: a for a in host_alerts(h)}
        self.assertEqual(alerts_crit["DISK"].level, Level.CRIT)

        # Huge 4 TiB disk with 100 GiB (2.4%) free must NOT raise a false DISK alert
        h.mem.disk_total_kib = 4 * 1024 * 1048576
        h.mem.disk_avail_kib = 100 * 1048576
        self.assertNotIn("DISK", {a.tag for a in host_alerts(h)})

        # Detail panel and JSON include root disk telemetry
        dtxt = "\n".join(lines_to_text(render_gpu_detail(h, h.gpus[0], 120, GLYPHS_BLOCK)))
        self.assertIn("root disk (/)", dtxt)
        self.assertIn("100G free of 4096G (2% avail)", dtxt)
        j = fleet_to_json(Fleet(hosts=[h]))
        self.assertEqual(j[0]["disk_root_kib"]["available"], 100 * 1048576)




