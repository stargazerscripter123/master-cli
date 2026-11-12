"""Cross-host Fleet Collector (macmaster --fleet) for multi-node Apple Silicon & NVIDIA clusters."""

from __future__ import annotations
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
import json
import subprocess
import threading
import time
from typing import Any, Deque, Dict, List, Optional

from .base import UnifiedCollector

DEFAULT_FLEET_HOSTS = ["local"]


@dataclass
class FleetNodeSnapshot:
    host: str
    online: bool = False
    latency_ms: float = 0.0
    error: str = ""
    data: Optional[Dict[str, Any]] = None
    cpu_hist: List[float] = field(default_factory=list)
    gpu_hist: List[float] = field(default_factory=list)


class FleetCollector:
    """Concurrently collects telemetry from local machine and remote SSH hosts."""

    def __init__(self, hosts: Optional[List[str]] = None) -> None:
        raw_hosts = hosts if hosts else list(DEFAULT_FLEET_HOSTS)
        self.hosts: List[str] = list(dict.fromkeys(h.strip() for h in raw_hosts if h and h.strip()))
        if not self.hosts:
            self.hosts = list(DEFAULT_FLEET_HOSTS)
        self.local_collector = UnifiedCollector()
        self._local_lock = threading.Lock()
        self._init_ts = time.perf_counter()
        self._sample_count = 0
        self.cpu_history: Dict[str, Deque[float]] = {h: deque(maxlen=60) for h in self.hosts}
        self.gpu_history: Dict[str, Deque[float]] = {h: deque(maxlen=60) for h in self.hosts}

    def _fetch_one(self, host: str) -> FleetNodeSnapshot:
        t0 = time.perf_counter()
        if not host or host.startswith("-"):
            return FleetNodeSnapshot(host=host, online=False, latency_ms=0.0, error="Invalid hostname")
        if host in ("local", "localhost", "127.0.0.1", self.local_collector.hostname):
            try:
                with self._local_lock:
                    elapsed = time.perf_counter() - self._init_ts
                    if self._sample_count == 0 and elapsed < 0.2:
                        time.sleep(max(0.05, 0.2 - elapsed))
                    snap = self.local_collector.collect()
                    self._sample_count += 1
                dt_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                d = asdict(snap)
                return FleetNodeSnapshot(host=host, online=True, latency_ms=dt_ms, data=d)
            except Exception as e:
                dt_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                return FleetNodeSnapshot(host=host, online=False, latency_ms=dt_ms, error=str(e)[:60])

        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=3",
            "--",
            host,
            'export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"; macmaster --json',
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            dt_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip().splitlines()
                msg = err[-1] if err else f"SSH exit {proc.returncode}"
                return FleetNodeSnapshot(host=host, online=False, latency_ms=dt_ms, error=msg[:60])
            raw = proc.stdout.strip()
            # Find JSON object start in case of login banner
            idx = raw.find("{")
            if idx > 0:
                raw = raw[idx:]
            d = json.loads(raw)
            return FleetNodeSnapshot(host=host, online=True, latency_ms=dt_ms, data=d)
        except subprocess.TimeoutExpired:
            dt_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            return FleetNodeSnapshot(host=host, online=False, latency_ms=dt_ms, error="SSH timeout (>5s)")
        except Exception as e:
            dt_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            return FleetNodeSnapshot(host=host, online=False, latency_ms=dt_ms, error=str(e)[:60])

    def collect(self) -> List[FleetNodeSnapshot]:
        results: Dict[str, FleetNodeSnapshot] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(self.hosts))) as pool:
            fut_map = {pool.submit(self._fetch_one, h): h for h in self.hosts}
            for fut in as_completed(fut_map):
                h = fut_map[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = FleetNodeSnapshot(host=h, online=False, error=str(e)[:60])
                results[h] = res

        ordered: List[FleetNodeSnapshot] = []
        for h in self.hosts:
            node = results[h]
            if h not in self.cpu_history:
                self.cpu_history[h] = deque(maxlen=60)
                self.gpu_history[h] = deque(maxlen=60)
            if node.online and node.data:
                cpu_pct = float(node.data.get("cpu", {}).get("total_pct", 0.0))
                gpus = node.data.get("gpus", [])
                gpu_pct = max((float(g.get("gpu_util_pct", 0.0)) for g in gpus), default=0.0)
                self.cpu_history[h].append(cpu_pct)
                self.gpu_history[h].append(gpu_pct)
            node.cpu_hist = list(self.cpu_history[h])
            node.gpu_hist = list(self.gpu_history[h])
            ordered.append(node)
        return ordered
