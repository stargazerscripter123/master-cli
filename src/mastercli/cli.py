"""Dispatch to one tool in the master-cli toolkit."""

from __future__ import annotations

import importlib
import sys

TOOLS = (
    ("gpumaster", "GPUs across every machine, over SSH"),
    ("macmaster", "Local system and GPU monitor"),
    ("thermalmaster", "Temperatures, fans and power"),
    ("tokenmaster", "AI subscription quotas and credits"),
)
ALIASES = {
    "gpu": "gpumaster",
    "mac": "macmaster",
    "thermal": "thermalmaster",
    "token": "tokenmaster",
}


def _help() -> str:
    lines = [
        "master-cli 0.1.0",
        "",
        "usage: master <tool> [args]",
        "       <tool> [args]",
        "",
        "tools:",
    ]
    for name, blurb in TOOLS:
        lines.append(f"  {name:<16} {blurb}")
    lines += [
        "",
        "aliases: gpu, mac, thermal, token",
        "docs:    docs/en/  and  docs/zh-Hant/",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(_help())
        return 0
    if args[0] in ("-V", "--version"):
        from mastercli import __version__
        print(f"master-cli {__version__}")
        return 0
    name = ALIASES.get(args[0], args[0])
    known = {tool for tool, _ in TOOLS}
    if name not in known:
        print(_help(), file=sys.stderr)
        print(f"\nmaster: unknown tool {args[0]!r}", file=sys.stderr)
        return 2
    module = importlib.import_module(f"{name}.cli")
    return int(module.main(args[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
