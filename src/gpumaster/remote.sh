#!/usr/bin/env bash
# gpumaster remote sampler.
#
# Piped to each host over `ssh -T <host> 'bash -s'`. Nothing is installed
# remotely; requirements are bash, nvidia-smi and Linux /proc -- or, on a Mac,
# bash 3.2 plus the stock ioreg / sysctl / vm_stat (see the Darwin branch).
#
# Design rules (each backed by a measurement recorded in the plan):
#
#  * ALWAYS exit 0 when the host itself is reachable, even if nvidia-smi fails.
#    A DGX Spark can return rc=9 from nvidia-smi while /proc is perfectly healthy;
#    exiting non-zero there made the client discard the whole machine.
#
#  * Process source is `nvidia-smi -q -d PIDS`, never --query-compute-apps.
#    The latter omits every graphics (G) process: measured 2-3 rows vs 6-9.
#
#  * No `pmon`. Its per-process sm/mem columns are '-' for every real compute
#    job on GeForce and RTX 6000 Ada, and accounting.mode is Disabled fleet
#    wide. Dropping it took the sample from 391-646 ms to 73-239 ms per host.
#
#  * `ps -ww` is mandatory; without it args= is truncated to terminal width and
#    the two ComfyUI jobs (differing only in trailing --port) become identical.
#
# Output is section-delimited plain text. Every section marker is on its own
# line so the client can split without a real parser.

set -u
export LC_ALL=C

# BSD date (macOS before 26) has no %N and prints the letter literally, which
# would make the __END__ arithmetic a bash error. Fall through to perl, then
# to whole seconds, rather than lose the marker.
_now_ms() {
  _t="$(date +%s%N 2>/dev/null)"
  case "$_t" in
    ''|*[!0-9]*)
      perl -MTime::HiRes=time -e 'printf "%d", time*1000' 2>/dev/null \
        || printf '%s000' "$(date +%s)" ;;
    *) printf '%s' "$_t" | cut -c1-13 ;;
  esac
}
T0="$(_now_ms)"

printf '__META__\n'
printf 'proto\t2\n'
printf 'ts\t%s\n' "$(date +%s)"

# ---------------------------------------------------------------- macOS branch
# Apple silicon has neither nvidia-smi nor /proc. Everything below is readable
# WITHOUT root: ioreg exposes the AGX accelerator's PerformanceStatistics (the
# same counters Activity Monitor's GPU History plots), sysctl and vm_stat give
# memory and load. powermetrics would add temperature, power draw and a
# per-process GPU list, but it needs sudo, so those cells stay '-'.
#
# The payload keeps the Linux shape (same section markers, /proc/meminfo key
# names, `fields` header) so the client parses it with no Darwin special case:
# memory.total is [N/A], which is exactly how a unified-memory part already
# declares itself; `vendor=apple` is the one hint the model layer needs to know
# that utilization.gpu and memory.used ARE believable here, unlike on GB10.
if [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
  _hn="$(hostname -s 2>/dev/null || hostname 2>/dev/null || printf unknown)"
  printf '__HOST__\n'
  printf 'hostname\t%s\n' "$_hn"
  printf 'arch\t%s\n' "$(uname -m 2>/dev/null || printf unknown)"
  printf 'kernel\t%s\n' "$(uname -r 2>/dev/null || printf unknown)"
  printf 'ncpu\t%s\n' "$(sysctl -n hw.ncpu 2>/dev/null || printf 0)"
  # `{ 5.24 5.44 6.63 }` -> the /proc/loadavg shape the client already parses.
  printf 'loadavg\t%s 0/0 0\n' \
    "$(sysctl -n vm.loadavg 2>/dev/null | tr -d '{}' | awk '{ printf "%s %s %s", $1, $2, $3 }')"
  # `{ sec = 1787621074, usec = 608673 } Tue Aug 25 11:24:34 2026` -- anchor on
  # the opening brace: a greedy `.*sec = ` would land on `usec = ` instead.
  _now_s="$(date +%s)"
  _boot="$(sysctl -n kern.boottime 2>/dev/null | sed -n 's/^{ *sec = \([0-9][0-9]*\),.*/\1/p')"
  printf 'uptime\t%s\n' "$(( _now_s - ${_boot:-$_now_s} ))"

  # Memory in kB under the /proc/meminfo names, so HostMem needs no new fields.
  # MemAvailable = free + inactive + speculative: pages the kernel can hand
  # out without swapping or compressing anything. Activity Monitor's "Memory
  # Used" is roughly the complement (wired + active + compressor). The unit of
  # every figure is kB, never pages -- the page size is 16 KiB on Apple
  # silicon and 4 KiB on Intel, and it is read from vm_stat's own header.
  vm_stat 2>/dev/null | awk -v total="$(sysctl -n hw.memsize 2>/dev/null || printf 0)" '
    /page size of/         { ps = $8 + 0 }
    /^Pages free:/         { free = $3 + 0 }
    /^Pages inactive:/     { inact = $3 + 0 }
    /^Pages speculative:/  { spec = $3 + 0 }
    /^File-backed pages:/  { fb = $3 + 0 }
    END {
      if (ps == 0) ps = 16384
      printf "mem.MemTotal\t%d\n", total / 1024
      printf "mem.MemFree\t%d\n", free * ps / 1024
      printf "mem.MemAvailable\t%d\n", (free + inact + spec) * ps / 1024
      printf "mem.Cached\t%d\n", fb * ps / 1024
    }'
  # `total = 2048.00M  used = 512.00M  free = 1536.00M  (encrypted)`
  sysctl -n vm.swapusage 2>/dev/null | awk '
    function kib(v,   n, u) {
      u = substr(v, length(v)); n = substr(v, 1, length(v) - 1) + 0
      if (u == "G") return n * 1048576
      if (u == "M") return n * 1024
      if (u == "K") return n
      return (v + 0) / 1024
    }
    { for (i = 1; i <= NF; i++) { if ($i == "total") t = $(i + 2); if ($i == "free") f = $(i + 2) } }
    END { printf "mem.SwapTotal\t%d\nmem.SwapFree\t%d\n", kib(t), kib(f) }'
  df -Pk / 2>/dev/null | awk 'NR == 2 && NF >= 6 && $(NF-4) ~ /^[0-9]+$/ && $(NF-2) ~ /^[0-9]+$/ {
    printf "disk.total\t%s\ndisk.avail\t%s\n", $(NF-4), $(NF-2) }'

  # No PSI on Darwin. The marker is emitted empty so the payload shape is the
  # same on every platform; the client already treats a missing value as None.
  printf '__PSI__\n'

  # Apple silicon only. An Intel Mac carries Intel/AMD IOAccelerator objects
  # whose counters mean different things (a discrete Radeon has real VRAM), and
  # labelling those `vendor=apple` would render them as unified parts. Keep the
  # host block live and mark the GPU side unsupported, the same way a dead
  # nvidia-smi is handled on Linux.
  if [ "$(uname -m 2>/dev/null)" != "arm64" ]; then
    printf '__ERROR__\n'
    printf 'code\tintel-mac-unsupported\n'
    printf 'msg\tonly Apple silicon GPUs are sampled on macOS\n'
    printf '__END__\t%s\n' "$(( $(_now_ms) - T0 ))"
    exit 0
  fi

  _acc="$(ioreg -r -d 1 -c IOAccelerator 2>/dev/null)"
  if ! printf '%s\n' "$_acc" | grep -q '"PerformanceStatistics"'; then
    printf '__ERROR__\n'
    printf 'code\tno-accelerator\n'
    printf 'msg\tioreg lists no IOAccelerator with PerformanceStatistics\n'
    printf '__END__\t%s\n' "$(( $(_now_ms) - T0 ))"
    exit 0
  fi

  # Optional rootless enrichment via `macmon` (IOReport / SMC): GPU temp (C),
  # GPU power (W), GPU frequency (MHz) and ANE power (W). When macmon is not
  # installed, every extra column stays `[N/A]` and parses to None.
  # macOS sshd sets PATH=/usr/bin:/bin:/usr/sbin:/sbin for non-login shells,
  # so include Homebrew and Cargo bin directories explicitly.
  export PATH="/opt/homebrew/bin:/usr/local/bin:${HOME:-/var/root}/.cargo/bin:$PATH"
  _mm=""
  if command -v macmon >/dev/null 2>&1; then
    _mm="$(macmon pipe -s 1 -i 100 2>/dev/null | head -1)"
  fi

  printf '__GPU__\n'
  printf 'fieldset\tapple\n'
  printf 'fields\tindex,uuid,name,pci.bus_id,driver_version,vendor,utilization.gpu,memory.total,memory.used,memory.reserved,gpu.cores,temperature.gpu,power.draw,clocks.sm,ane.power\n'
  # One row per accelerator object. "In use system memory" is what the GPU is
  # holding right now; "Alloc system memory" is the larger driver reservation
  # and travels as memory.reserved. Both are bytes and become MiB here.
  printf '%s\n' "$_acc" | MM="$_mm" awk \
    -v hn="$_hn" \
    -v osver="$(sw_vers -productVersion 2>/dev/null || printf unknown)" \
    -v chip="$(sysctl -n machdep.cpu.brand_string 2>/dev/null || printf 'Apple GPU')" '
    function grab(key,   m) {
      if (match($0, "\"" key "\"=[0-9]+")) {
        m = substr($0, RSTART, RLENGTH); sub(/.*=/, "", m); return m + 0
      }
      return ""
    }
    function jnum(src, pat, allow_zero,   s, v) {
      if (match(src, pat)) {
        s = substr(src, RSTART, RLENGTH)
        sub(/^.*:[[:space:]]*(\[[[:space:]]*)?/, "", s)
        if (s ~ /^[-+]?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$/) {
          v = s + 0
          if ((allow_zero && v >= 0) || (!allow_zero && v > 0)) {
            return sprintf("%.2f", v)
          }
        }
      }
      return "[N/A]"
    }
    function reset() { agx = 0; seen = 0; name = chip; util = "[N/A]"; used = "[N/A]"; alloc = "[N/A]"; cores = "[N/A]" }
    function flush() {
      if (seen) {
        printf "row\t%d, APPLE-%s-%d, %s, apple:%d, %s, apple, %s, [N/A], %s, %s, %s, %s, %s, %s, %s\n",
               idx, hn, idx, name, idx, osver, util, used, alloc, cores, m_temp, m_pwr, m_clk, m_ane
        idx++
      }
      reset()
    }
    BEGIN {
      idx = 0
      mm = ENVIRON["MM"]
      m_temp = jnum(mm, "\"gpu_temp_avg\"[[:space:]]*:[[:space:]]*[-+0-9.eE]+", 0)
      m_pwr  = jnum(mm, "\"gpu_power\"[[:space:]]*:[[:space:]]*[-+0-9.eE]+", 1)
      m_clk  = jnum(mm, "\"gpu_usage\"[[:space:]]*:[[:space:]]*\\[[[:space:]]*[-+0-9.eE]+", 0)
      m_ane  = jnum(mm, "\"ane_power\"[[:space:]]*:[[:space:]]*[-+0-9.eE]+", 1)
      reset()
    }
    # Each object opens with `+-o AGXAcceleratorG15X  <class ...>`. Only the
    # Apple GPU class gets a row; any other IOAccelerator subclass is skipped.
    /^[ |]*\+-o / { flush(); if ($0 ~ /AGXAccelerator/) agx = 1 }
    /"model" = "/ { m = $0; sub(/.*"model" = "/, "", m); sub(/"[[:space:]]*$/, "", m); gsub(/[,\\]/, " ", m); if (m != "") name = m }
    /"gpu-core-count" = / { c = $0; sub(/.*"gpu-core-count" = /, "", c); cores = c + 0 }
    /"PerformanceStatistics" = / {
      if (!agx) next
      seen = 1
      u = grab("Device Utilization %");  if (u != "") util = u
      b = grab("In use system memory");  if (b != "") used = int(b / 1048576)
      a = grab("Alloc system memory");   if (a != "") alloc = int(a / 1048576)
    }
    END { flush() }'

  # No per-process GPU attribution without root (that is powermetrics' job).
  printf '__PIDS__\n'
  printf '__PS__\n'
  printf 'requested\t0\n'
  printf '__END__\t%s\n' "$(( $(_now_ms) - T0 ))"
  exit 0
fi

# ---------------------------------------------------------------- host block
printf '__HOST__\n'
printf 'hostname\t%s\n' "$(hostname -s 2>/dev/null || hostname 2>/dev/null || printf unknown)"
printf 'arch\t%s\n' "$(uname -m 2>/dev/null || printf unknown)"
printf 'kernel\t%s\n' "$(uname -r 2>/dev/null || printf unknown)"
printf 'ncpu\t%s\n' "$(nproc 2>/dev/null || printf 0)"
printf 'loadavg\t%s\n' "$(cat /proc/loadavg 2>/dev/null || printf '0 0 0 0/0 0')"
printf 'uptime\t%s\n' "$(cut -d. -f1 /proc/uptime 2>/dev/null || printf 0)"

# MemAvailable is the number that matters, not MemFree: on a DGX Spark
# MemFree can be 3 GiB while MemAvailable is 50 GiB, the difference being
# reclaimable page cache. Reporting MemFree would look like imminent OOM.
if [ -r /proc/meminfo ]; then
  awk '/^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SReclaimable|Shmem|Dirty|SwapTotal|SwapFree):/ {
         sub(/:$/, "", $1); printf "mem.%s\t%s\n", $1, $2 }' /proc/meminfo
  df -Pk / 2>/dev/null | awk 'NR == 2 && NF >= 6 && $(NF-4) ~ /^[0-9]+$/ && $(NF-2) ~ /^[0-9]+$/ {
    printf "disk.total\t%s\ndisk.avail\t%s\n", $(NF-4), $(NF-2) }'
else
  printf '__ERROR__\n'
  printf 'code\tunsupported-os\n'
  printf 'msg\t%s has no /proc/meminfo\n' "$(uname -s 2>/dev/null || printf unknown)"
  printf '__END__\n'
  exit 0
fi

# Linux PSI. Present on current kernels, including aarch64 DGX Sparks.
# Needed because workstation-b sits at 9.3/124.8 GiB available with 8 GiB swapped
# out while PSI still reads 0.00 -- PSI alone is not a sufficient alarm, so the
# client shows swap-used alongside it.
printf '__PSI__\n'
for _res in memory cpu io; do
  if [ -r "/proc/pressure/$_res" ]; then
    awk -v r="$_res" '{ printf "%s.%s\t%s\t%s\t%s\n", r, $1, $2, $3, $4 }' "/proc/pressure/$_res"
  fi
done

# High-speed fabric / RoCEv2 links (ConnectX-7 200GbE / InfiniBand / >=25GbE).
# Emitted before nvidia-smi so a GPU_DEAD Spark still reports its RDMA rails.
printf '__NET__\n'
for _p in /sys/class/net/*; do
  [ -e "$_p" ] || continue
  _if="${_p##*/}"
  case "$_if" in
    lo|docker*|br-*|veth*|virbr*|tailscale*|wg*|tun*|tap*) continue ;;
  esac
  _rdma=""
  for _ib in "$_p"/device/infiniband/*; do
    if [ -e "$_ib" ]; then
      _rdma="${_ib##*/}"
      break
    fi
  done
  if [ -z "$_rdma" ]; then
    for _ib in /sys/class/infiniband/*; do
      [ -e "$_ib" ] || continue
      if [ -e "$_ib/device/net/$_if" ]; then
        _rdma="${_ib##*/}"
        break
      fi
    done
  fi
  _st="$(cat "$_p/operstate" 2>/dev/null || printf unknown)"
  _spd="$(cat "$_p/speed" 2>/dev/null || printf -- -1)"
  case "$_spd" in ''|*[!0-9-]*) _spd=-1 ;; esac
  _hs=0
  if [ -n "$_rdma" ]; then
    _hs=1
  elif [ "$_st" = "up" ] && [ "$_spd" -ge 25000 ] 2>/dev/null && [ "$_spd" -le 1600000 ] 2>/dev/null && [ "$_spd" -ne 65535 ] 2>/dev/null; then
    _hs=1
  fi
  if [ "$_hs" -eq 1 ]; then
    _mtu="$(cat "$_p/mtu" 2>/dev/null || printf 0)"
    _rx="$(cat "$_p/statistics/rx_bytes" 2>/dev/null || printf -- -1)"
    _tx="$(cat "$_p/statistics/tx_bytes" 2>/dev/null || printf -- -1)"
    printf 'if\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$_if" "$_st" "$_spd" "$_mtu" "$_rx" "$_tx" "$_rdma"
  fi
done

# ------------------------------------------------------------------ gpu block
if ! command -v nvidia-smi >/dev/null 2>&1; then
  printf '__ERROR__\n'
  printf 'code\tno-nvidia-smi\n'
  printf 'msg\tnvidia-smi not found on remote host\n'
  printf '__END__\t%s\n' "$(( $(_now_ms) - T0 ))"
  exit 0
fi

# Field-set negotiation. One renamed field on a future driver must degrade a
# single cell, not blind the whole machine, so we fall back to a smaller set
# and tell the client which one was used. The client indexes BY NAME.
GF_FULL='index,uuid,name,pci.bus_id,driver_version,vbios_version,pstate,temperature.gpu,temperature.gpu.tlimit,fan.speed,utilization.gpu,utilization.memory,utilization.encoder,utilization.decoder,memory.total,memory.used,memory.free,memory.reserved,power.draw.instant,power.limit,enforced.power.limit,power.max_limit,clocks.sm,clocks.max.sm,clocks.mem,clocks.max.mem,clocks.video,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max,clocks_event_reasons.active,compute_mode,persistence_mode,mig.mode.current'
GF_SAFE='index,uuid,name,pci.bus_id,driver_version,pstate,temperature.gpu,fan.speed,utilization.gpu,utilization.memory,memory.total,memory.used,power.draw,power.limit,clocks.sm,clocks.max.sm'

gpu_out="$(nvidia-smi --query-gpu="$GF_FULL" --format=csv,noheader,nounits 2>&1)"
gpu_rc=$?
gpu_fields="$GF_FULL"
gpu_set=full

if [ "$gpu_rc" -ne 0 ]; then
  gpu_out2="$(nvidia-smi --query-gpu="$GF_SAFE" --format=csv,noheader,nounits 2>&1)"
  gpu_rc2=$?
  if [ "$gpu_rc2" -eq 0 ]; then
    gpu_out="$gpu_out2"; gpu_rc=0; gpu_fields="$GF_SAFE"; gpu_set=safe
  fi
fi

if [ "$gpu_rc" -ne 0 ]; then
  # Host is fine, the GPU stack is not. Emit the error and still exit 0 so the
  # client keeps rendering live CPU/RAM/PSI for this machine. This is exactly
  # a Spark with a dead driver (rc=9, host otherwise healthy).
  printf '__ERROR__\n'
  printf 'code\tnvidia-smi-rc-%s\n' "$gpu_rc"
  printf 'msg\t%s\n' "$(printf '%s' "$gpu_out" | head -1 | tr -d '\t')"
  printf '__END__\t%s\n' "$(( $(_now_ms) - T0 ))"
  exit 0
fi

printf '__GPU__\n'
printf 'fieldset\t%s\n' "$gpu_set"
printf 'fields\t%s\n' "$gpu_fields"
printf '%s\n' "$gpu_out" | sed 's/^/row\t/'

# --------------------------------------------------------------- process block
# -q -d PIDS groups processes under `GPU <pci.bus_id>` headers and reports both
# compute and graphics processes with their full binary path. It is the only
# correct source; the bus_id key also sidesteps any index-ordering question.
printf '__PIDS__\n'
pids_out="$(nvidia-smi -q -d PIDS 2>/dev/null)"
printf '%s\n' "$pids_out" | sed 's/^/|/'

printf '__PS__\n'
pid_list="$(printf '%s\n' "$pids_out" \
  | awk '/^ *Process ID *:/ { print $NF }' \
  | awk '/^[0-9]+$/' | sort -un | paste -sd, -)"
if [ -n "$pid_list" ]; then
  printf 'requested\t%s\n' "$(printf '%s' "$pid_list" | tr ',' '\n' | grep -c .)"
  # ps silently drops unresolvable PIDs with rc=0, so the client compares the
  # requested count against the number of rows it actually got back.
  ps -ww -o pid=,user=,pcpu=,rss=,etime=,args= -p "$pid_list" 2>/dev/null \
    | sed 's/^/row\t/'

  # Rootless container attribution: inspect /proc/<pid>/cgroup for Docker,
  # containerd or Podman 64-hex container IDs. Only query `docker ps` when at
  # least one GPU PID is actually inside a container, and bound it to 1s so a
  # wedged container daemon can never stall sampling.
  _cg_raw=""
  for _pid in $(printf '%s' "$pid_list" | tr ',' ' '); do
    if [ -r "/proc/$_pid/cgroup" ]; then
      _cid="$(awk '
        match($0, "(docker[-/]|(cri-)?containerd[-/]|libpod-(conmon-)?)[0-9a-f]+") {
          s = substr($0, RSTART, RLENGTH)
          sub("^.*[-/]", "", s)
          if (length(s) == 64) {
            print s
            exit
          }
        }' "/proc/$_pid/cgroup" 2>/dev/null)"
      if [ -n "$_cid" ]; then
        _cg_raw="${_cg_raw}${_pid}	${_cid}
"
      fi
    fi
  done
  if [ -n "$_cg_raw" ]; then
    _dmap=""
    if command -v docker >/dev/null 2>&1; then
      if command -v timeout >/dev/null 2>&1; then
        _dmap="$(timeout 1 docker ps --no-trunc --format '{{.ID}}\t{{.Names}}' 2>/dev/null)"
      else
        _dmap="$(docker ps --no-trunc --format '{{.ID}}\t{{.Names}}' 2>/dev/null)"
      fi
    fi
    printf '%s' "$_cg_raw" | DMAP="$_dmap" awk -F '\t' '
      BEGIN {
        n = split(ENVIRON["DMAP"], lines, "\n")
        for (i = 1; i <= n; i++) {
          if (split(lines[i], kv, /[\t ]+/) >= 2 && kv[1] != "" && kv[2] != "") {
            names[kv[1]] = kv[2]
          }
        }
      }
      NF >= 2 && $1 != "" && $2 != "" {
        cid = $2
        cname = (cid in names) ? names[cid] : substr(cid, 1, 12)
        printf "cg\t%s\t%s\n", $1, cname
      }'
  fi
else
  printf 'requested\t0\n'
fi

printf '__END__\t%s\n' "$(( $(_now_ms) - T0 ))"
exit 0
