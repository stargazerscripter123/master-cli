# macmaster

A local terminal monitor that puts CPU, GPU, memory, disks, network and processes on one screen. It runs on macOS and Linux. There are no required third-party Python packages. Collectors use `ctypes` against Mach, IOKit, `libIOReport`, AppleSMC and `sysctl` on macOS, and `/proc`, `/sys` and `libnvidia-ml.so.1` on Linux.

```bash
macmaster                      # full-screen TUI
macmaster --once               # one color snapshot, then exit
macmaster --gpu-only           # GPU panel and GPU processes
macmaster --json               # one snapshot as JSON
macmaster --compact            # dense one-screen summary
macmaster --stream             # newline-delimited JSON
macmaster --record run.jsonl   # append samples to a file
macmaster --replay run.jsonl   # play a recording back
macmaster --simulate-nvidia    # preview an NVIDIA layout without a local GPU
macmaster --fleet host-a host-b
```

`master mac` and `btop-nvtop` are the same program.

## What is on screen

- **CPU.** Total and per-core utilization, Braille history, E-core and P-core clocks on Apple silicon, load, uptime, power and battery.
- **GPU.** Apple GPU utilization (device, renderer, tiler), unified memory, clocks, P-state, GPU and ANE power, die temperature, and per-process Metal time. On NVIDIA: SM and memory-controller utilization, VRAM, BAR1, clocks, P-state, encoder and decoder, power, temperature, fan, PCIe generation, width and throughput, and per-process CUDA or graphics use.
- **Memory and disks.** RAM, swap, and live read/write throughput per mount.
- **Network.** Per-interface addresses, throughput and totals. `b` and `n` cycle interfaces.
- **Processes.** PID, program, command, GPU, threads, user, memory, VRAM, GPU% and CPU% in one table.

No sudo is required for the Apple silicon or NVML paths.

## Keys

| Key | Action |
|---|---|
| `1`–`5` | Show or hide CPU, GPU, memory and disk, network, processes |
| `z` `m` | Zoom the active panel |
| `g` | GPU processes only |
| `t` | Process tree |
| `/` `f` | Filter processes. `Esc` clears it |
| `<` `>` `s` | Cycle the sort column |
| `r` | Reverse sort order |
| `↑` `↓` `j` `k` | Move in the process list |
| `Enter` `Space` | Process detail |
| `K` `x` | Signal the selected process (`SIGTERM` / `SIGKILL`) |
| `d` | Next GPU |
| `b` `n` | Next network interface |
| `+` `-` | Refresh interval, from 100 ms to 10 s |
| `c` | Color theme |
| `?` `h` | Help |
| `q` `Esc` | Quit |

Themes: `default`, `nvidia`, `apple`, `dracula`, `nord`, `monokai`, `tokyo-night`, `catppuccin`.

## A small fleet

```bash
macmaster --fleet
macmaster --fleet gpu-a mac-studio
```

With no host names, `--fleet` watches this machine only. Each extra argument is an SSH alias. The remote machine must already have `macmaster` on its `PATH`; the collector runs `macmaster --json` there. Names that start with `-` are rejected.

This is separate from `gpumaster`. Use `gpumaster` when the nodes should not have this toolkit installed. Use `macmaster --fleet` when every machine already has `macmaster` and you want the local dashboard repeated for each of them.
