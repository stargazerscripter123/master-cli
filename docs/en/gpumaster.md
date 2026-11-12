# gpumaster

One screen for every GPU, over plain SSH. Nothing is installed on the GPU nodes. `remote.sh` is piped to `bash -s` on a multiplexed OpenSSH connection.

A node needs `ssh`, `bash`, and either `nvidia-smi` plus Linux `/proc`, or — on an Apple silicon Mac — the stock `ioreg`, `sysctl` and `vm_stat`.

```bash
gpumaster                 # live TUI
gpumaster --once          # one screen, then exit
gpumaster --json          # one snapshot as JSON
gpumaster free [host]     # GPUs worth taking (--min-mem GiB, -n COUNT, --format table|env|ssh|json)
gpumaster serve           # HTTP, curl ANSI, JSON and Prometheus /metrics (--bind, --port)
gpumaster --mode NAME     # one host set from hosts.json
gpumaster --mobile        # same as --mode mobile; also --desktop, --laptop, --all
gpumaster check HOST      # capability preflight for one machine
gpumaster --probe HOST    # raw remote payload, suitable as a test fixture
gpumaster --replay DIR    # render captured fixtures, no network
gpumaster init            # write ~/.config/gpumaster/hosts.json
```

`master gpu` is the same command.

## Configuration

`gpumaster init` writes `~/.config/gpumaster/hosts.json`. You can also pass `-c FILE`. Put `User`, `Port`, `IdentityFile` and `ProxyJump` in `~/.ssh/config`, and refer to the SSH alias here:

```json
{
  "refresh_seconds": 2.0,
  "hosts": [
    {"name": "train-a", "target": "gpu-a", "tags": ["training"]},
    {"name": "spark", "target": "dgx-spark", "tags": ["gb10"]},
    {"name": "laptop", "target": "mac-laptop", "tags": ["laptop"], "hidden": true}
  ],
  "modes": {
    "desktop": ["train-a", "spark"],
    "laptop": ["laptop"]
  }
}
```

A full sample is [`examples/gpumaster.hosts.json`](../../examples/gpumaster.hosts.json). A host with `"hidden": true` stays off the default screen, because a laptop that is asleep most of the time would otherwise sit there as a red row. `--all` includes hidden hosts. Any other mode lists hosts by `name` or `target`.

On Apple silicon, a host alias that matches this machine's chip is treated as local and is not opened over SSH. An M2 Max matches `m2x` and `mac-m2x`; Ultra and Pro follow the same pattern (`u`, `p`). A target written as `user@host`, or one with its own `ssh_args`, always uses SSH.

Runtime sockets live in `/tmp/gm.$UID/`. That directory is kept short on purpose: macOS rejects an SSH control socket whose path is 104 bytes or longer.

## Keys

| Key | Action |
|---|---|
| `↑` `↓` `j` `k` | Move the GPU selection |
| `Enter` | Expand the selected GPU |
| `Esc` `q` | Back, clear `/` filter, quit |
| `/` | Filter by host, GPU, user, container, PID or command |
| `p` | Show or hide the process table |
| `s` | Cycle process sort |
| `S` | Cycle host sort (`config`, `util`, `free-mem`, `temp`) |
| `Tab` `n` `N` | Select a process in the detail view |
| `K` | Confirmed remote kill (`SIGTERM`, then `SIGKILL`) |
| `h` | Hide or show idle GPUs |
| `m` | Cycle modes |
| `g` | Block or ASCII glyphs |
| `d` | Transport diagnostics |
| `f` | Freeze the display; collection continues |
| `r` `R` | Refresh now, or clear backoff |
| `+` `-` | Faster or slower refresh |

## How to read a row

| Marker | Meaning |
|---|---|
| `[+]` | Streaming |
| `[!]` | The host is up, but its GPUs are unreadable. CPU, RAM and PSI stay live |
| `[x]` | SSH failed. One line, and no invented GPU row |
| `[.]` | Disabled in `hosts.json` |
| `*` | Derived value |
| `?` | Not trustworthy on this part |
| `!` | Over a threshold |
| `uma` | Unified memory. `used/total` is the shared pool, not a framebuffer |
| `+` | A PID that `ps` could not resolve |

The host header includes RAM, swap and PSI. A machine can have idle GPUs and still be unusable because the host is out of memory.

## Unified memory

On Apple silicon, basic GPU utilization and memory come from the stock `ioreg`, `sysctl` and `vm_stat` commands. Temperature, power, GPU clocks and ANE power are optionally enriched by `macmon` when installed on the sampled Mac and available to the remote sampler. Without it, those extra fields remain unavailable. Basic sampling does not provide per-process Apple GPU attribution. `macmaster` uses its own native collectors; it does not require `macmon`.

DGX Spark / GB10 and Apple silicon use a shared memory pool rather than a separate framebuffer. When GB10 framebuffer usage is unsupported, GPU-resident memory is derived from per-process NVIDIA allocations and marked `*`. Apple GPU resident memory comes from IOAccelerator counters. The shared pool comes from Linux `MemTotal` / `MemAvailable`, or macOS `sysctl` / `vm_stat`.

The host line separates GPU-held memory, other use and available memory. GPU-resident memory is a subset of host use, rather than extra physical capacity. Fleet VRAM totals count discrete GPUs only; unified hosts get a separate UMA line to avoid double counting.

## `free` and `serve`

```bash
gpumaster free --min-mem 20 -n 2 --format ssh
gpumaster serve --bind 127.0.0.1 --port 8765
curl -s localhost:8765/metrics
```

`free` skips a GPU on a host that is itself short of RAM. `serve` is a small HTTP server in the standard library: an ANSI screen, JSON, and Prometheus text. Metrics are named `gpumaster_*`.

Selection is based on the current snapshot and does not reserve GPUs. `env` and `ssh` output formats select CUDA hosts; they exclude Apple GPUs. The server exposes `/`, `/fleet.txt`, `/fleet.ansi`, `/api/fleet.json` and `/metrics`. It has no built-in authentication or TLS and includes host and process information. Keep the default loopback binding and use an SSH tunnel for remote access.

## Replay

```bash
mkdir -p captures
gpumaster --probe gpu-a > captures/gpu-a.txt
gpumaster --replay captures --once
```

`--replay` reads `*.txt` fixtures and optional `*.stderr` companions. It ignores AppleDouble `._*` files.
