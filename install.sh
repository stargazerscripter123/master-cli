#!/usr/bin/env bash
# Install master-cli launchers that run this checkout.
# Some file-sync clients strip the executable bit: use `bash install.sh`.

set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PREFIX="${HOME}/.local"
ACTION="install"

usage() {
  cat <<EOF
Usage: bash install.sh [--prefix DIR] [--check] [--uninstall]

Writes launchers into DIR/bin (default: ~/.local/bin) for:

  master  gpumaster  macmaster  thermalmaster  tokenmaster  btop-nvtop

Each launcher runs the code in this directory, so later edits apply immediately.
Python 3.10 or newer is required. Nothing is installed on remote machines.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)
      PREFIX="${2:?--prefix needs a directory}"
      shift 2
      ;;
    --check)
      ACTION="check"
      shift
      ;;
    --uninstall)
      ACTION="uninstall"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "install.sh: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

BIN="${PREFIX}/bin"
# name:python-module
COMMANDS=(
  master:mastercli
  gpumaster:gpumaster
  macmaster:macmaster
  thermalmaster:thermalmaster
  tokenmaster:tokenmaster
  btop-nvtop:macmaster
)

if ! python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
then
  echo "install.sh: Python 3.10 or newer is required (found $(python3 -c 'import sys; print(sys.version.split()[0])'))" >&2
  exit 1
fi

if [ "$ACTION" = "check" ]; then
  echo "source:  $ROOT"
  echo "bin:     $BIN"
  python3 -c 'import sys; print("python: ", sys.executable, sys.version.split()[0])'
  exit 0
fi

if [ "$ACTION" = "uninstall" ]; then
  for item in "${COMMANDS[@]}"; do
    name="${item%%:*}"
    if [ -f "$BIN/$name" ]; then
      rm -f "$BIN/$name"
      echo "removed $BIN/$name"
    fi
  done
  exit 0
fi

mkdir -p "$BIN"
for item in "${COMMANDS[@]}"; do
  name="${item%%:*}"
  module="${item#*:}"
  cat > "$BIN/$name" <<EOF
#!/bin/sh
export PYTHONPATH="$ROOT/src\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m $module "\$@"
EOF
  chmod +x "$BIN/$name"
  echo "installed $BIN/$name"
done

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "Add $BIN to PATH to run the commands from any directory." ;;
esac
