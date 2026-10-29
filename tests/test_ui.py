import curses
import unittest
from unittest.mock import patch

from docker_dash.backend import Backend, Snapshot, demo_containers
from docker_dash.ui import Dashboard


class Screen:
    def __init__(self, height=54, width=200):
        self.height, self.width = height, width
        self.lines = {}

    def getmaxyx(self):
        return self.height, self.width

    def addstr(self, y, x, text, attrs):
        if not (0 <= y < self.height and 0 <= x < self.width):
            raise AssertionError("drawing outside screen")
        line = self.lines.setdefault(y, [" "] * self.width)
        line[x:x + len(text)] = text

    def erase(self):
        self.lines.clear()

    def refresh(self): pass
    def keypad(self, _): pass
    def timeout(self, _): pass
    def bkgd(self, *_): pass

    def text(self):
        return "\n".join("".join(self.lines.get(y, [])) for y in range(self.height))


class UiTests(unittest.TestCase):
    def setUp(self):
        for name in ("start_color", "init_pair", "curs_set"):
            patcher = patch(f"curses.{name}")
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("curses.color_pair", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("curses.COLORS", 256, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend = Backend(demo=True)
        self.snap = Snapshot(containers=demo_containers(0), demo=True, updated=1,
                             host={"available": True, "cpu": 12, "total": 15.5e9, "memory": 4.2e9})

    def test_sizes_details_and_logs_render(self):
        for h, w in [(54, 200), (40, 160), (32, 132), (24, 80), (18, 60)]:
            with self.subTest(size=(h, w)):
                ui = Dashboard(Screen(h, w), self.backend)
                ui.render(self.snap)
                self.assertIn("Docker Dashboard", ui.screen.text())
                if h >= 24 and w >= 80:
                    self.assertIn("nginx-proxy", ui.screen.text())
                    ui.detail_only = True
                    ui.render(self.snap)
                    self.assertIn("LOGS", ui.screen.text())
                    ui.logs_only = True
                    ui.render(self.snap)
                    self.assertIn("LOGS", ui.screen.text())

    def test_filter_navigation_and_selection_survive_reordering(self):
        ui = Dashboard(Screen(), self.backend)
        ui.render(self.snap)
        ui.handle(curses.KEY_DOWN, self.snap)
        self.assertEqual(ui.selected_id, self.snap.containers[1].id)
        self.snap.containers.reverse()
        ui.render(self.snap)
        self.assertEqual(ui.selected_id, demo_containers(0)[1].id)
        ui.handle("f", self.snap)
        for key in "redis\n":
            ui.handle(key, self.snap)
        ui.render(self.snap)
        self.assertEqual(ui.selected_id, demo_containers(0)[4].id)
        self.assertEqual(len(ui.filtered(self.snap)), 1)

    def test_actions_require_confirmation_and_pin_container_id(self):
        ui = Dashboard(Screen(), self.backend)
        ui.render(self.snap)
        ui.handle("s", self.snap)
        self.assertEqual(ui.pending[0], "stop")
        self.assertTrue(self.backend.actions.empty())
        ui.handle("n", self.snap)
        self.assertTrue(self.backend.actions.empty())
        ui.handle("d", self.snap)
        original = ui.pending[1]
        self.snap.containers.reverse()
        ui.handle("y", self.snap)
        self.assertEqual(self.backend.actions.get_nowait(), ("rm", original))

    def test_connection_error_is_visible(self):
        ui = Dashboard(Screen(), self.backend)
        self.snap.error = "permission denied"
        self.snap.containers = []
        ui.render(self.snap)
        self.assertIn("permission denied", ui.screen.text())


if __name__ == "__main__":
    unittest.main()
