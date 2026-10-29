from __future__ import annotations

import copy
import json
import math
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


def clean(value: object) -> str:
    """Keep untrusted names and logs from injecting terminal control sequences."""
    value = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))", "", str(value))
    return "".join(c for c in value if c.isprintable() and c != "\x1b")


def size_bytes(value: str) -> float:
    match = re.fullmatch(r"\s*([\d.]+)\s*([a-zA-Z]*)\s*", value)
    if not match:
        return 0.0
    units = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3,
             "tb": 1000**4, "kib": 1024, "mib": 1024**2,
             "gib": 1024**3, "tib": 1024**4}
    return float(match[1]) * units.get(match[2].lower(), 1)


def human_size(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1000 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1000
    return "0 B"


def percent(value: str) -> float:
    try:
        return float(value.rstrip("%"))
    except (ValueError, AttributeError):
        return 0.0


@dataclass
class Container:
    id: str
    name: str
    image: str
    state: str
    status: str
    ports: str = "-"
    cpu: float = 0.0
    memory: float = 0.0
    limit: float = 0.0
    memory_percent: float = 0.0
    net_rx: float = 0.0
    net_tx: float = 0.0
    detail: dict = field(default_factory=dict)


@dataclass
class Snapshot:
    containers: list[Container] = field(default_factory=list)
    host: dict = field(default_factory=dict)
    logs: dict[str, list[str]] = field(default_factory=dict)
    error: str = ""
    notice: str = ""
    updated: float = 0.0
    demo: bool = False
    busy: bool = False


class DockerError(RuntimeError):
    pass


class DockerClient:
    def run(self, *args: str, timeout: float = 12, logs: bool = False) -> str:
        try:
            result = subprocess.run(["docker", *args], capture_output=True,
                                    text=True, errors="replace", timeout=timeout)
        except FileNotFoundError as exc:
            raise DockerError("Docker CLI fehlt. Docker installieren oder --demo verwenden.") from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerError("Docker antwortet nicht (Zeitlimit). Kontext/Daemon pruefen.") from exc
        if result.returncode:
            raise DockerError(clean(result.stderr.strip() or result.stdout.strip())[:500])
        return result.stdout + (result.stderr if logs else "")

    def containers(self) -> list[Container]:
        rows = self.run("ps", "-a", "--no-trunc", "--format", "{{json .}}")
        containers = []
        for line in rows.splitlines():
            row = json.loads(line)
            containers.append(Container(
                id=row["ID"], name=clean(row["Names"]), image=clean(row["Image"]),
                state=clean(row["State"]), status=clean(row["Status"]),
                ports=clean(row.get("Ports", "")) or "-"))
        if not containers:
            return []
        details = json.loads(self.run("inspect", *(c.id for c in containers)))
        by_id = {d["Id"]: d for d in details}
        for container in containers:
            container.detail = by_id.get(container.id, {})
        running = [c.id for c in containers if c.state == "running"]
        if running:
            stats = self.run("stats", "--no-stream", "--no-trunc", "--format",
                             "{{json .}}", *running)
            by_id = {c.id: c for c in containers}
            for line in stats.splitlines():
                row = json.loads(line)
                container = by_id.get(row.get("ID"))
                if container is None:
                    continue
                memory = row.get("MemUsage", "0 / 0").split(" / ")
                network = row.get("NetIO", "0 / 0").split(" / ")
                container.cpu = percent(row.get("CPUPerc", "0"))
                container.memory_percent = percent(row.get("MemPerc", "0"))
                if len(memory) == 2:
                    container.memory, container.limit = map(size_bytes, memory)
                if len(network) == 2:
                    container.net_rx, container.net_tx = map(size_bytes, network)
        for container in containers:
            if not container.limit:
                container.limit = container.detail.get("HostConfig", {}).get("Memory", 0)
        return sorted(containers, key=lambda c: (c.state != "running", c.name.casefold()))

    def logs(self, container_id: str) -> list[str]:
        return [clean(line) for line in self.run(
            "logs", "--tail", "60", "--timestamps", container_id,
            timeout=5, logs=True).splitlines()][-60:]

    def action(self, action: str, container_id: str) -> None:
        if action not in {"start", "stop", "restart", "rm"}:
            raise ValueError("Unsupported Docker action")
        # Never force deletion or remove volumes.
        self.run(action, container_id, timeout=40)


class HostSampler:
    """Linux /proc counters. Host metrics remain distinct from container stats."""

    def __init__(self):
        self.previous: dict = {}

    def sample(self) -> dict:
        now = time.monotonic()
        result = {"available": False, "cpu": 0., "memory": 0., "total": 0.,
                  "rx": 0., "tx": 0., "read": 0., "write": 0., "interface": "host"}
        try:
            ticks = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:9]))
            cpu_total, idle = sum(ticks), ticks[3] + ticks[4]
            mem = {line.split(":")[0]: int(line.split()[1]) * 1024
                   for line in Path("/proc/meminfo").read_text().splitlines()}
            result.update(available=True, total=mem["MemTotal"],
                          memory=mem["MemTotal"] - mem.get("MemAvailable", mem["MemFree"]))
            interfaces = {}
            for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
                name, values = line.split(":", 1)
                fields = values.split()
                interfaces[name.strip()] = (int(fields[0]), int(fields[8]))
            # Prefer docker0; otherwise count physical uplinks, avoiding veth/bridge duplicates.
            names = ["docker0"] if "docker0" in interfaces else [
                name for name in interfaces if not name.startswith(("lo", "veth", "br-", "docker"))]
            rx = sum(interfaces[name][0] for name in names)
            tx = sum(interfaces[name][1] for name in names)
            result["interface"] = "docker0" if names == ["docker0"] else "host"
            read = write = 0
            for line in Path("/proc/diskstats").read_text().splitlines():
                fields = line.split()
                # Only whole physical devices; omit partitions and stacked virtual disks.
                if re.fullmatch(r"(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+)", fields[2]):
                    read += int(fields[5]) * 512
                    write += int(fields[9]) * 512
            current = dict(time=now, cpu_total=cpu_total, idle=idle,
                           rx=rx, tx=tx, read=read, write=write)
            if self.previous:
                dt = max(.001, now - self.previous["time"])
                delta = cpu_total - self.previous["cpu_total"]
                result["cpu"] = max(0, min(100, 100 * (1 - (idle - self.previous["idle"]) / delta))) if delta > 0 else 0
                for key in ("rx", "tx", "read", "write"):
                    result[key] = max(0, (current[key] - self.previous[key]) / dt)
            self.previous = current
        except (OSError, ValueError, KeyError, IndexError):
            result["available"] = False
        return result


def demo_containers(tick: int) -> list[Container]:
    specs = [
        ("nginx-proxy", "nginx:alpine", .2, 45.2, 1000, "8080", "80"),
        ("app-frontend", "node:20-alpine", 1.3, 218.7, 1000, "3000", "3000"),
        ("app-backend", "python:3.11-slim", .8, 156.4, 1000, "8000", "8000"),
        ("postgres", "postgres:16", .4, 312.8, 2000, "5432", "5432"),
        ("redis", "redis:7-alpine", .1, 24.6, 512, "6379", "6379"),
        ("grafana", "grafana/grafana:latest", .6, 248.2, 1000, "3001", "3000"),
        ("influxdb", "influxdb:2.7", 0., 0., 1000, "", "")]
    now = datetime.now(timezone.utc)
    result = []
    for i, (name, image, cpu, mem, limit, hostport, port) in enumerate(specs):
        running = i != 6
        ident = f"{i + 1:012x}" + "a" * 52
        detail = {"Created": (now - timedelta(hours=3 + i)).isoformat(), "RestartCount": 0,
                  "Path": "/docker-entrypoint.sh", "Args": ["nginx", "-g", "daemon off;"] if i == 0 else [name],
                  "NetworkSettings": {"Networks": {"docker_default": {"IPAddress": f"172.18.0.{i + 3}"}},
                                      "Ports": {f"{port}/tcp": [{"HostIp": "0.0.0.0", "HostPort": hostport}]} if port else {}},
                  "Mounts": [{"Source": "/etc/nginx/nginx.conf", "Destination": "/etc/nginx/nginx.conf"}] if i == 0 else [],
                  "State": {"Status": "running" if running else "exited"}, "HostConfig": {"Memory": limit * 1e6}}
        result.append(Container(ident, name, image, "running" if running else "exited",
                                f"Up {3 + i} hours" if running else "Exited (0) 3 days ago",
                                f"0.0.0.0:{hostport} → {port}/tcp" if port else "-",
                                cpu * (1 + .25 * math.sin(tick + i)), mem * 1e6, limit * 1e6,
                                mem / limit * 100, (12.3 + i * 5) * 1000 if running else 0,
                                (8.7 + i * 2) * 1000 if running else 0, detail))
    return result


class Backend:
    def __init__(self, interval: float = 2., demo: bool = False):
        self.interval, self.demo = interval, demo
        self.client = DockerClient()
        self.host = HostSampler()
        self.lock = threading.Lock()
        self.snapshot = Snapshot(demo=demo)
        self.selected = ""
        self.actions: queue.Queue = queue.Queue()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.tick = 0

    def start(self):
        self.thread.start()

    def stop(self):
        self.closed.set()
        self.wake.set()
        self.thread.join(timeout=.2)

    def read(self) -> Snapshot:
        with self.lock:
            return copy.deepcopy(self.snapshot)

    def select(self, container_id: str):
        with self.lock:
            changed = self.selected != container_id
            self.selected = container_id
        if changed:
            self.wake.set()

    def action(self, action: str, container_id: str):
        self.actions.put((action, container_id))
        self.wake.set()

    def _loop(self):
        while not self.closed.is_set():
            self.wake.clear()
            snapshot = self.read()
            try:
                while not self.actions.empty():
                    action, ident = self.actions.get_nowait()
                    with self.lock:
                        self.snapshot.busy = True
                    if self.demo:
                        snapshot.notice = "DEMO: Aktionen sind deaktiviert."
                    else:
                        self.client.action(action, ident)
                        snapshot.notice = f"Docker {action}: erfolgreich."
                snapshot.host = self.host.sample()
                if self.demo:
                    snapshot.containers = demo_containers(self.tick)
                    snapshot.host = dict(available=True, cpu=12.4 + 8 * math.sin(self.tick * .8),
                                         memory=4.2e9, total=15.5e9, rx=(3.1 + math.sin(self.tick)) * 1e6,
                                         tx=(2.4 + .8 * math.cos(self.tick)) * 1e6,
                                         read=0., write=0., interface="docker0")
                else:
                    snapshot.containers = self.client.containers()
                with self.lock:
                    ident = self.selected
                if not ident and snapshot.containers:
                    ident = snapshot.containers[0].id
                if ident and any(c.id == ident for c in snapshot.containers):
                    if self.demo:
                        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        snapshot.logs = {ident: [f"{stamp}  [notice] demo log output",
                                                f"{stamp}  [notice] service ready",
                                                f"{stamp}  GET /health 200 OK",
                                                f"{stamp}  [info] request completed ({self.tick + 1})"]}
                    else:
                        try:
                            snapshot.logs = {ident: self.client.logs(ident)}
                        except DockerError as exc:
                            snapshot.logs = {ident: [f"Logs: {exc}"]}
                else:
                    snapshot.logs = {}
                snapshot.error = ""
                self.tick += 1
            except (DockerError, ValueError, KeyError, TypeError) as exc:
                snapshot.error = clean(exc)
                # Keep the previous snapshot visible and explicitly mark it as stale.
            snapshot.busy = False
            if not snapshot.error:
                snapshot.updated = time.time()
            with self.lock:
                self.snapshot = snapshot
            self.wake.wait(self.interval)
