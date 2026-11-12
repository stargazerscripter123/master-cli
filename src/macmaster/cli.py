"""CLI entry point for MacMaster v2.0 (btop + nvtop unified monitor for macOS & Ubuntu)."""

from __future__ import annotations
import argparse
import sys
import time

from . import __version__
from .collectors.base import UnifiedCollector
from .collectors.fleet import DEFAULT_FLEET_HOSTS, FleetCollector
from .ui.snapshot_view import (
    render_compact_ansi,
    render_fleet_ansi,
    render_replay_summary,
    render_snapshot_ansi,
    render_snapshot_json,
    render_stream_ndjson,
    run_fleet_live,
)
from .ui.themes import THEMES
from .ui.tui import MacMasterTUI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="macmaster",
        description="MacMaster v2.0: Unified btop + nvtop System, GPU & Fleet Monitor for macOS (Apple Silicon) and Ubuntu (NVIDIA).",
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        default=1.0,
        help="Refresh interval in seconds (0.1 to 10.0, default: 1.0)",
    )
    parser.add_argument(
        "-1",
        "--once",
        "--snapshot",
        dest="once",
        action="store_true",
        help="Render a single btop + nvtop dashboard snapshot to stdout and exit",
    )
    parser.add_argument(
        "-c",
        "--compact",
        action="store_true",
        help="Render a gpustat-style ultra-compact 2-3 line color summary and exit",
    )
    parser.add_argument(
        "-j",
        "--json",
        action="store_true",
        help="Output a single complete telemetry snapshot as JSON and exit",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Continuously stream single-line NDJSON telemetry records to stdout",
    )
    parser.add_argument(
        "--record",
        metavar="FILE",
        help="Record NDJSON telemetry stream to FILE (use with --samples N or Ctrl+C)",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=0,
        help="Number of samples to collect in --stream or --record mode (0 = unlimited)",
    )
    parser.add_argument(
        "--replay",
        metavar="FILE",
        help="Replay a recorded .jsonl telemetry file with Braille sparklines and Peak/Avg summary",
    )
    parser.add_argument(
        "--fleet",
        nargs="*",
        metavar="HOST",
        help=f"Monitor multi-node cluster via SSH (default hosts: {' '.join(DEFAULT_FLEET_HOSTS)})",
    )
    parser.add_argument(
        "-g",
        "--gpu-only",
        action="store_true",
        help="Start in GPU-focused nvtop mode (GPU panel + GPU processes)",
    )
    parser.add_argument(
        "-t",
        "--theme",
        choices=[t.name for t in THEMES],
        default="default",
        help=f"Color theme ({', '.join(t.name for t in THEMES)})",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="Number of processes to display in --once snapshot mode (default: 15)",
    )
    parser.add_argument(
        "--simulate-nvidia",
        action="store_true",
        help="Include simulated NVIDIA H100 GPUs for testing multi-GPU nvtop views",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"macmaster {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 1. Replay mode
    if args.replay:
        out = render_replay_summary(args.replay)
        if out.startswith("Error ") or out.startswith("No valid "):
            print(out, file=sys.stderr)
            return 1
        print(out)
        return 0

    # 2. Fleet cluster mode (--fleet [HOSTS...])
    if args.fleet is not None:
        hosts = args.fleet if len(args.fleet) > 0 else list(DEFAULT_FLEET_HOSTS)
        fleet = FleetCollector(hosts=hosts)
        if args.once or args.json or not sys.stdout.isatty():
            nodes = fleet.collect()
            if args.json:
                import json
                from dataclasses import asdict
                print(json.dumps([asdict(n) for n in nodes], indent=2, ensure_ascii=False))
            else:
                print(render_fleet_ansi(nodes))
            return 0
        try:
            run_fleet_live(fleet, interval_sec=max(1.0, args.interval), theme_name=args.theme)
        except KeyboardInterrupt:
            pass
        return 0

    collector = UnifiedCollector(simulate_nvidia=args.simulate_nvidia)

    # 3. Continuous NDJSON streaming or recording (--stream / --record FILE)
    if args.stream or args.record:
        out_f = None
        if args.record:
            try:
                out_f = open(args.record, "a", encoding="utf-8")
            except OSError as e:
                print(f"Error opening record file '{args.record}': {e}", file=sys.stderr)
                return 1
        target_samples = args.samples if args.samples > 0 else (1 if args.once else 0)
        count = 0
        try:
            while True:
                time.sleep(max(0.1, args.interval if count > 0 else 0.2))
                snap = collector.collect()
                line = render_stream_ndjson(snap)
                if out_f is not None:
                    out_f.write(line + "\n")
                    out_f.flush()
                if args.stream or out_f is None:
                    print(line, flush=True)
                count += 1
                if target_samples > 0 and count >= target_samples:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            if out_f is not None:
                out_f.close()
        return 0

    # 4. Compact gpustat-style mode (-c / --compact)
    if args.compact:
        time.sleep(0.22)
        snap = collector.collect()
        print(render_compact_ansi(snap))
        return 0

    # 5. Single snapshot or JSON mode (-1 / --once / -j / --json / non-TTY)
    if args.once or args.json or not sys.stdout.isatty():
        time.sleep(0.25)
        snap = collector.collect()
        if args.json:
            print(render_snapshot_json(snap))
        else:
            print(render_snapshot_ansi(collector, snap, top_n=args.top))
        return 0

    # 6. Full-screen interactive TUI
    tui = MacMasterTUI(
        collector=collector,
        interval_sec=args.interval,
        gpu_only_mode=args.gpu_only,
        theme_name=args.theme,
    )
    try:
        tui.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
