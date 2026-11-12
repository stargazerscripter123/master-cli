# thermalmaster

Every temperature, fan and power sensor on the machine, as live curves in the terminal. One Python file, standard library only. It reads the operating system directly and does not need root. The same file runs on macOS and on Linux.

```bash
thermalmaster                         # this machine
thermalmaster --once                  # every sensor once
thermalmaster --json                  # one snapshot as JSON
thermalmaster --all                   # include sensors that are normally hidden
thermalmaster --probe                 # raw dump of every source
thermalmaster --record run.csv        # log samples (--headless, --duration 2h)
thermalmaster --bell                  # terminal bell on a new critical breach
thermalmaster --fleet fleet.json      # every machine in the file, over SSH
thermalmaster --fleet a,b             # an explicit list of SSH aliases
thermalmaster --fleet fleet.json --mobile
```

`master thermal` is the same command.

## A fleet

`fleet.json` lists SSH aliases:

```json
{
  "hosts": [
    "gpu-a",
    "dgx-spark",
    "mac-studio",
    {"name": "mac-laptop", "mobile": true}
  ]
}
```

See [`examples/thermalmaster.fleet.json`](../../examples/thermalmaster.fleet.json).

Plain `--fleet FILE` skips entries marked `"mobile": true`, because a closed laptop is a host that stays down and keeps retrying. `--mobile` includes them. An explicit comma list is used as given. `"disabled": true` drops a host. `"target": "local"` reads the viewer itself without SSH. `"target": "user@host"` is passed to SSH as written.

Nothing is installed on the other machines. The viewer pipes `app.py` into `python3 -` over SSH, so every machine runs the same version you are looking at. A machine only needs SSH key login (`BatchMode`) and Python 3.8 or newer. If `fleet.json` is missing, the error tells you to copy the example.

The fleet screen has one box per machine. `Enter` opens that machine. `Esc` returns to the fleet. A machine that cannot be reached shows `down` with SSH's own reason and is retried with backoff. A reconnect keeps the history. A machine that stops talking without dropping the connection shows `stale`.

```bash
thermalmaster --fleet fleet.json --once
thermalmaster --fleet fleet.json --json
thermalmaster --fleet fleet.json --record fleet.csv
```

`thermalmaster` with no `--fleet` never opens the network.

## Keys

| Key | Action |
|---|---|
| Arrows, `hjkl`, mouse wheel | Select a group, sensor or machine |
| Click | Select. Click again to open it |
| `Enter` | Every sensor in the group, with the selected curve on top |
| `a` | All sensors |
| `m` | Plot the group maximum or the group average |
| `s` | Cycle sort. In the fleet view: `config`, `hottest`, `margin` |
| `/` | Substring filter |
| `w` | Only hot, alerting, throttling or fast-rising sensors |
| `f` | Pin the selected sensor |
| `[` `]` `{` `}` | Step through history by 1 or 10 samples |
| `b` | Bell on a new critical breach |
| `Esc`, Backspace | Back |
| `+` `-` | Shorter or longer time window |
| `u` | Celsius or Fahrenheit |
| `p`, Space | Pause the display. Sampling continues |
| `r` | Reset min, max and peak-breach memory |
| `g` | Box drawing or ASCII |
| `?` | Help, including how expensive each source is to read |
| `q` | Quit |

## Reading the screen

Each group (CPU, GPU, SoC, memory, storage, power, fans, battery, and so on) has a box. The curve is the group maximum over time, and the title is the current maximum. `m` switches the curve to the average. Time runs right to left: now is the right edge.

Colors run from cyan (cool) through green and yellow (warning) to red (critical). Hardware limits are used when the sensor reports them. A warning or critical crossing leaves a peak-breach marker until `r`. Throttle states show up as badges.

`--all` keeps the sensors that are normally hidden and prints each one's `hidden_reason`. Hidden readings include unused Apple silicon keys that sit on a placeholder temperature, disconnected firmware channels, and sensors with no reading.

## Where the numbers come from

**macOS**, through IOKit:

| Source | What |
|---|---|
| AppleSMC | `T???` temperatures, fans, power keys |
| IOHID | PMU die sensors, NAND, battery |
| `kern.thermalstate` | System thermal pressure |

**Linux**:

| Source | What |
|---|---|
| `/sys/class/hwmon` | coretemp, k10temp, NVMe, DIMMs, NICs, GPUs, fans, power, battery |
| `/sys/class/thermal` | Thermal zones, including SoC and laptop ACPI zones |
| `/sys/class/power_supply` | Battery temperature when it has no hwmon device |
| `/sys/class/powercap` | Intel RAPL energy counters, converted to watts, skipped when root-only |
| `/sys/devices/system/cpu` | Package and core throttle counters |
| NVML | GPU and HBM temperature, slowdown limits, fan, power, P-state, throttle reasons |
