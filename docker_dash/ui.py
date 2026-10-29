from __future__ import annotations

import curses
import math
import textwrap
import unicodedata
from collections import deque
from datetime import datetime

from .backend import Backend, Container, Snapshot, clean, human_size


BLUE, CYAN, GREEN, PURPLE, YELLOW, RED, MUTED, SELECTED = range(1, 9)


class Dashboard:
    def __init__(self, screen, backend: Backend):
        self.screen, self.backend = screen, backend
        self.filter = ""
        self.edit_filter = False
        self.selected_id = ""
        self.index = 0
        self.offset = 0
        self.detail_only = False
        self.logs_only = False
        self.log_offset = 0
        self.pending: tuple[str, str, str] | None = None
        self.last_update = 0.
        self.history = {key: deque(maxlen=240) for key in ("cpu", "memory", "network", "disk")}
        if backend.demo:
            for i in range(90):
                self.history["cpu"].append(10 + 12 * abs(math.sin(i * 1.7)) + (i % 11 == 0) * 20)
                self.history["memory"].append(27 + 6 * math.sin(i / 28))
                self.history["network"].append((2.5 + 2 * abs(math.sin(i * 2))) * 1e6)
                self.history["disk"].append(4096)
        self.colors()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        screen.timeout(100)

    def colors(self):
        curses.start_color()
        palette = [curses.COLOR_BLUE, curses.COLOR_CYAN, curses.COLOR_GREEN,
                   curses.COLOR_MAGENTA, curses.COLOR_YELLOW, curses.COLOR_RED,
                   curses.COLOR_WHITE, curses.COLOR_CYAN]
        if curses.COLORS >= 256:
            palette = [75, 45, 84, 141, 221, 203, 103, 117]
        for number, foreground in enumerate(palette, 1):
            curses.init_pair(number, foreground, 17 if number == SELECTED and curses.COLORS >= 256 else curses.COLOR_BLACK)
        self.screen.bkgd(" ", curses.color_pair(BLUE))

    def put(self, y: int, x: int, value: object, color: int = BLUE, width: int | None = None, bold: bool = False):
        h, w = self.screen.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        text = clean(value)
        limit = max(0, min(w - x, width if width is not None else w - x))
        # CJK and other wide characters must not spill into adjacent columns.
        result, used = [], 0
        for char in text:
            cells = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
            if used + cells > limit:
                break
            result.append(char)
            used += cells
        try:
            self.screen.addstr(y, x, "".join(result), curses.color_pair(color) | (curses.A_BOLD if bold else 0))
        except curses.error:
            pass

    def box(self, y: int, x: int, h: int, w: int, color: int = CYAN, title: str = ""):
        if h < 2 or w < 2:
            return
        self.put(y, x, "╭" + "─" * (w - 2) + "╮", color)
        self.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", color)
        for row in range(y + 1, y + h - 1):
            self.put(row, x, "│", color)
            self.put(row, x + w - 1, "│", color)
        if title:
            self.put(y, x + 2, f" {title} ", color, w - 4)

    def rule(self, y: int, x: int, w: int):
        self.put(y, x, "─" * max(0, w), MUTED)

    def bar(self, y: int, x: int, width: int, value: float, color: int):
        width = max(0, width)
        filled = max(0, min(width, round(width * value / 100)))
        if value > 0 and width:
            filled = max(1, filled)
        self.put(y, x, "━" * width, MUTED)
        self.put(y, x, "━" * filled, color)

    def graph(self, y: int, x: int, h: int, w: int, history, maximum: float, color: int, percentage: bool):
        chart_x, chart_w = x + 5, w - 5
        if h < 2 or chart_w < 2:
            return
        for row in range(h):
            self.put(y + row, chart_x, "┊" + " " * (chart_w - 1), MUTED)
            for col in range(0, chart_w, max(1, chart_w // 5)):
                self.put(y + row, chart_x + col, "┊", MUTED)
        self.put(y + h - 1, chart_x, "─" * chart_w, MUTED)
        self.put(y, x, "100%" if percentage else human_size(maximum).replace(" ", ""), MUTED, 4)
        self.put(y + h - 1, x, "0", MUTED, 4)
        values = list(history)[-chart_w:]
        chars = " ▁▂▃▄▅▆▇█"
        for col, value in enumerate(values):
            level = max(0, min(h * 8, round(value / max(1, maximum) * h * 8)))
            if level == 0:
                self.put(y + h - 1, chart_x + chart_w - len(values) + col, "─", color)
            for row in range(h):
                amount = max(0, min(8, level - (h - 1 - row) * 8))
                if amount:
                    self.put(y + row, chart_x + chart_w - len(values) + col, chars[amount], color)

    def metric(self, y: int, x: int, h: int, w: int, title: str, lines: list[str], key: str,
               color: int, maximum: float, percentage: bool = False):
        self.box(y, x, h, w, color)
        self.put(y + 1, x + 2, title, BLUE, max(0, w - 4), True)
        for i, line in enumerate(lines):
            self.put(y + 2 + i, x + 2, line, color, w - 4)
        self.graph(y + 4, x + 2, h - 5, w - 4, self.history[key], maximum, color, percentage)

    def metrics(self, snap: Snapshot, width: int, height: int):
        host = snap.host
        available = host.get("available", False)
        total, mem = host.get("total", 0), host.get("memory", 0)
        specs = [
            ("CPU · HOST", [f"{host.get('cpu', 0):.1f}%"], "cpu", CYAN, 100, True),
            ("MEMORY · HOST", [f"{human_size(mem)} / {human_size(total)}"], "memory", PURPLE, 100, True),
            (f"NETWORK · {host.get('interface', 'host')}",
             [f"↑ {human_size(host.get('tx', 0))}/s  ↓ {human_size(host.get('rx', 0))}/s"],
             "network", GREEN, max(1e6, max(self.history["network"], default=0) * 1.15), False),
            ("DISK · HOST", [f"R {human_size(host.get('read', 0))}/s  W {human_size(host.get('write', 0))}/s"],
             "disk", YELLOW, max(10000, max(self.history["disk"], default=0) * 1.15), False)]
        for i, spec in enumerate(specs):
            left = 1 + i * (width - 2) // 4
            right = 1 + (i + 1) * (width - 2) // 4
            title, lines, key, color, maximum, percentage = spec
            self.metric(3, left, height, right - left - (i < 3), title,
                        lines if available else ["/proc nicht verfuegbar"], key, color, maximum, percentage)

    def filtered(self, snap: Snapshot) -> list[Container]:
        query = self.filter.casefold()
        return [c for c in snap.containers if query in f"{c.name} {c.image} {c.state} {c.id}".casefold()]

    def selection(self, containers: list[Container]) -> Container | None:
        if not containers:
            self.index = 0
            self.selected_id = ""
            self.backend.select("")
            return None
        self.index = next((i for i, c in enumerate(containers) if c.id == self.selected_id),
                          min(self.index, len(containers) - 1))
        selected = containers[self.index]
        self.selected_id = selected.id
        self.backend.select(selected.id)
        return selected

    def table(self, y: int, x: int, h: int, w: int, containers: list[Container], snap: Snapshot):
        self.box(y, x, h, w, title=f"CONTAINERS · {len(containers)}")
        inner = w - 4
        # Allocate useful columns at each terminal width.
        if inner >= 118:
            columns = [("CONTAINER", 20), ("STATUS", 13), ("IMAGE", 22), ("CPU", 8),
                       ("MEM", 19), ("NET I/O", 20), ("PORTS", max(12, inner - 102))]
        elif inner >= 73:
            columns = [("CONTAINER", 21), ("STATUS", 12), ("CPU", 8), ("MEM", 20), ("PORTS", inner - 61)]
        else:
            columns = [("CONTAINER", max(20, inner - 31)), ("STATUS", 11), ("CPU", 8), ("MEM", 12)]
        xx = x + 2
        for title, column_width in columns:
            self.put(y + 2, xx, title, BLUE, column_width - 1)
            xx += column_width
        self.rule(y + 3, x + 1, w - 2)
        row_height = 4 if h >= 24 else 3
        capacity = max(1, (h - 6) // row_height)
        self.offset = max(0, min(self.offset, max(0, len(containers) - capacity)))
        if self.index < self.offset:
            self.offset = self.index
        if self.index >= self.offset + capacity:
            self.offset = self.index - capacity + 1
        for pos, container in enumerate(containers[self.offset:self.offset + capacity]):
            row = y + 4 + pos * row_height
            active = container.id == self.selected_id
            if active:
                for rr in range(row, row + row_height - 1):
                    self.put(rr, x + 1, " " * (w - 2), SELECTED)
            state_color = GREEN if container.state == "running" else RED if container.state in ("exited", "dead") else YELLOW
            memory = f"{human_size(container.memory)} / {human_size(container.limit)}" if container.limit else human_size(container.memory)
            values = {"CONTAINER": ("▸ " if active else "  ") + container.name,
                      "STATUS": "● " + ("stopped" if container.state == "exited" else container.state),
                      "IMAGE": container.image, "CPU": f"{container.cpu:.1f}%", "MEM": memory,
                      "NET I/O": f"{human_size(container.net_rx)} / {human_size(container.net_tx)}",
                      "PORTS": container.ports}
            xx = x + 2
            for title, column_width in columns:
                self.put(row, xx, values[title], state_color if title == "STATUS" else SELECTED if active else BLUE,
                         column_width - 1, title == "CONTAINER")
                if title == "CONTAINER":
                    self.put(row + 1, xx + 2, container.id[:12], MUTED, column_width - 3)
                elif title == "STATUS":
                    self.put(row + 1, xx + 2, container.status, MUTED, column_width - 3)
                elif title == "CPU":
                    self.bar(row + 1, xx, column_width - 2, container.cpu, BLUE)
                elif title == "MEM":
                    self.bar(row + 1, xx, column_width - 2, container.memory_percent, PURPLE)
                elif title == "NET I/O":
                    self.bar(row + 1, xx, column_width - 2, min(100, container.net_rx / 1000), GREEN)
                xx += column_width
            self.rule(row + row_height - 1, x + 1, w - 2)
        if not containers:
            messages = ["Keine passenden Container.", "[f] Filter bearbeiten"] if self.filter else [
                "Verbindung wird hergestellt …" if not snap.updated and not snap.error else "Keine Container vorhanden.",
                "Docker-Verbindung pruefen oder ./docker-dash --demo starten."]
            if snap.error:
                messages = ["Docker-Verbindung fehlgeschlagen", *textwrap.wrap(snap.error, max(10, inner)),
                            "Socket-Rechte / Docker-Kontext pruefen.", "Demo: ./docker-dash --demo"]
            for i, line in enumerate(messages[:h - 7]):
                self.put(y + 6 + i, x + 3, line, RED if snap.error else MUTED, w - 6)
        self.put(y + h - 2, x + 2,
                 f"{self.index + 1 if containers else 0}/{len(containers)}  ·  Filter: {self.filter or 'alle'}", MUTED, w - 4)

    def logs(self, y: int, x: int, h: int, w: int, container: Container, snap: Snapshot):
        self.box(y, x, h, w, title="LOGS · tail 60", color=CYAN)
        lines = snap.logs.get(container.id, ["Logs werden geladen …"])
        if not lines:
            lines = ["Keine Log-Ausgabe."]
        capacity = max(1, h - 3)
        self.log_offset = min(self.log_offset, max(0, len(lines) - capacity))
        end = len(lines) - self.log_offset
        for i, line in enumerate(lines[max(0, end - capacity):end]):
            self.put(y + 1 + i, x + 2, line, BLUE, w - 4)
        if self.log_offset:
            self.put(y + h - 1, x + 2, f" {self.log_offset} Zeilen ueber Ende · PgUp/PgDn ", MUTED, w - 4)

    def detail(self, y: int, x: int, h: int, w: int, container: Container | None, snap: Snapshot):
        self.box(y, x, h, w, title="DETAILS")
        if not container:
            self.put(y + 2, x + 2, "Container auswaehlen", MUTED, w - 4)
            return
        self.put(y + 1, x + 2, container.name, CYAN, w - 18, True)
        self.put(y + 1, x + w - 14, "● " + container.state, GREEN if container.state == "running" else RED, 12)
        self.put(y + 2, x + 2, f"{container.id[:12]}  │  {container.image}", MUTED, w - 4)
        self.rule(y + 3, x + 2, w - 4)
        if self.logs_only:
            self.logs(y + 5, x + 2, h - 7, w - 4, container, snap)
            return
        if h < 24:
            self.put(y + 4, x + 2, f"{container.status} · CPU {container.cpu:.1f}% · MEM {human_size(container.memory)}", BLUE, w - 4)
            self.put(y + 5, x + 2, container.ports, MUTED, w - 4)
            self.logs(y + 7, x + 2, h - 8, w - 4, container, snap)
            return
        detail = container.detail
        label_width = 14 if w > 40 else 11
        value_x = x + 2 + label_width
        value_w = w - label_width - 4

        def field(row, label, value, color=BLUE):
            self.put(y + row, x + 2, label, MUTED, label_width - 1)
            self.put(y + row, value_x, value, color, value_w)

        field(4, "Status", container.status)
        created = detail.get("Created", "-")
        try:
            created = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            created = "-"
        field(5, "Created", created)
        field(6, "Restarts", detail.get("RestartCount", 0))
        field(7, "CPU", f"{container.cpu:.1f}%", CYAN)
        self.bar(y + 8, value_x, value_w, container.cpu, CYAN)
        field(9, "Memory", f"{human_size(container.memory)} / {human_size(container.limit)}", PURPLE)
        self.bar(y + 10, value_x, value_w, container.memory_percent, PURPLE)
        network = detail.get("NetworkSettings", {})
        networks = network.get("Networks") or {}
        field(12, "Networks", ", ".join(networks) or "-")
        field(13, "IP address", ", ".join(n.get("IPAddress", "") for n in networks.values()) or "-")
        sections = [("Ports", [f"{binding.get('HostIp', '')}:{binding.get('HostPort', '')} → {port}"
                               for port, bindings in (network.get("Ports") or {}).items() for binding in bindings or []]),
                    ("Command", [" ".join([detail.get("Path", ""), *(detail.get("Args") or [])])]),
                    ("Mounts", [f"{m.get('Source', '')} → {m.get('Destination', '')}" for m in detail.get("Mounts", [])])]
        row = 15
        log_height = min(9, max(5, h // 4))
        log_y = y + h - log_height - 1
        for title, lines in sections:
            if y + row + 2 >= log_y:
                break
            self.put(y + row, x + 2, title, CYAN, w - 4, True)
            self.rule(y + row, x + 12, w - 14)
            row += 1
            for line in (lines or ["-"])[:3]:
                if y + row >= log_y - 1:
                    break
                self.put(y + row, x + 2, line, BLUE, w - 4)
                row += 1
            row += 1
        self.logs(log_y, x + 2, log_height, w - 4, container, snap)

    def render(self, snap: Snapshot):
        self.screen.erase()
        h, w = self.screen.getmaxyx()
        if h < 24 or w < 80:
            self.put(1, 2, "Docker Dashboard", CYAN, bold=True)
            self.put(3, 2, f"Terminal vergroessern: mindestens 80 × 24 (aktuell {w} × {h})", YELLOW)
            self.put(5, 2, "[q] beenden")
            self.screen.refresh()
            return
        containers = self.filtered(snap)
        selected = self.selection(containers)
        running = sum(c.state == "running" for c in snap.containers)
        title = f"▟▙  Docker Dashboard  │  {len(snap.containers)} containers  │  {running} running"
        self.put(1, 2, title, CYAN, w - 25, True)
        self.put(1, w - 28, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), MUTED)
        self.rule(2, 1, w - 2)
        if h >= 32:
            metric_h = 9 if h >= 40 else 8
            self.metrics(snap, w, metric_h)
            body_y = 3 + metric_h + 1
        else:
            self.put(3, 2, f"HOST  CPU {snap.host.get('cpu', 0):.1f}%  │  MEM {human_size(snap.host.get('memory', 0))}", PURPLE)
            body_y = 5
        body_h = h - body_y - 5
        if self.detail_only:
            self.detail(body_y, 1, body_h, w - 2, selected, snap)
        elif w >= 132:
            left_w = int((w - 3) * .67)
            self.table(body_y, 1, body_h, left_w, containers, snap)
            self.detail(body_y, left_w + 2, body_h, w - left_w - 3, selected, snap)
        else:
            self.table(body_y, 1, body_h, w - 2, containers, snap)
        status = ("DEMO · simulierte Daten" if snap.demo else f"LIVE · refresh {self.backend.interval:g}s")
        color = YELLOW if snap.demo else GREEN
        if snap.error:
            status = "VERBINDUNGSFEHLER · Daten veraltet · " + snap.error
            color = RED
        elif snap.busy:
            status += " · Aktion laeuft …"
        elif snap.notice:
            status += " · " + snap.notice
        elif snap.updated:
            status += " · " + datetime.fromtimestamp(snap.updated).strftime("%H:%M:%S")
        self.put(h - 5, 2, status, color, w - 4)
        self.box(h - 4, 1, 3, w - 2)
        footer = "[q] quit  [f] filter  [↑↓/jk] navigate  [enter] details  [l] logs  [s] start/stop  [r] restart  [d] delete"
        if w < 120:
            footer = "q quit · f filter · ↑↓ select · ↵ details · l logs · s start/stop · r · d"
        if self.edit_filter:
            footer = f"Filter: {self.filter}▏  [enter] uebernehmen  [esc] loeschen"
        self.put(h - 3, 3, footer, BLUE, w - 6)
        if self.pending:
            action, _, name = self.pending
            modal_w = min(w - 8, 68)
            modal_x, modal_y = (w - modal_w) // 2, h // 2 - 3
            for row in range(modal_y, modal_y + 7):
                self.put(row, modal_x, " " * modal_w, SELECTED)
            self.box(modal_y, modal_x, 7, modal_w, YELLOW, "AKTION BESTAETIGEN")
            self.put(modal_y + 2, modal_x + 3, f"Docker {action}: {name}", BLUE, modal_w - 6, True)
            self.put(modal_y + 4, modal_x + 3, "[y] ausfuehren   [n / esc] abbrechen", YELLOW, modal_w - 6)
        self.screen.refresh()

    def handle(self, key, snap: Snapshot) -> bool:
        if self.pending:
            if key in ("y", "Y"):
                action, ident, _ = self.pending
                self.backend.action(action, ident)
                self.pending = None
            elif key in ("n", "N", "\x1b", "q"):
                self.pending = None
            return True
        if self.edit_filter:
            if key in ("\n", "\r", curses.KEY_ENTER):
                self.edit_filter = False
            elif key == "\x1b":
                self.filter = ""
                self.edit_filter = False
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                self.filter = self.filter[:-1]
            elif isinstance(key, str) and key.isprintable():
                self.filter += key
            self.offset = 0
            return True
        if key in ("q", "Q", "\x03"):
            return False
        containers = self.filtered(snap)
        selected = self.selection(containers)
        if key in (curses.KEY_DOWN, "j", curses.KEY_UP, "k", curses.KEY_HOME, curses.KEY_END):
            if containers:
                if key == curses.KEY_HOME:
                    self.index = 0
                elif key == curses.KEY_END:
                    self.index = len(containers) - 1
                else:
                    self.index = max(0, min(len(containers) - 1,
                                           self.index + (1 if key in (curses.KEY_DOWN, "j") else -1)))
                self.selected_id = containers[self.index].id
                self.log_offset = 0
        elif key in ("f", "/"):
            self.edit_filter = True
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self.detail_only = not self.detail_only
            self.logs_only = False
        elif key == "l":
            self.logs_only = not self.logs_only
            self.detail_only = self.logs_only
            self.log_offset = 0
        elif key == "\x1b":
            self.detail_only = self.logs_only = False
        elif key == curses.KEY_PPAGE:
            self.log_offset += 5
        elif key == curses.KEY_NPAGE:
            self.log_offset = max(0, self.log_offset - 5)
        elif key == " ":
            self.backend.wake.set()
        elif key in ("s", "r", "d") and selected and not snap.busy:
            action = {"s": "stop" if selected.state == "running" else "start", "r": "restart", "d": "rm"}[key]
            self.pending = (action, selected.id, selected.name)
        return True

    def run(self):
        self.backend.start()
        try:
            while True:
                snap = self.backend.read()
                if snap.updated and snap.updated != self.last_update and not snap.error:
                    self.last_update = snap.updated
                    host = snap.host
                    if host.get("available"):
                        self.history["cpu"].append(host.get("cpu", 0))
                        self.history["memory"].append(100 * host.get("memory", 0) / max(1, host.get("total", 1)))
                        self.history["network"].append(host.get("rx", 0) + host.get("tx", 0))
                        self.history["disk"].append(host.get("read", 0) + host.get("write", 0))
                self.render(snap)
                try:
                    key = self.screen.get_wch()
                except curses.error:
                    continue
                if not self.handle(key, snap):
                    break
        finally:
            self.backend.stop()


def run(backend: Backend):
    curses.wrapper(lambda screen: Dashboard(screen, backend).run())
