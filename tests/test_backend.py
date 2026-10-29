import json
import subprocess
import unittest
from unittest.mock import patch

from docker_dash.backend import (Backend, DockerClient, DockerError, HostSampler,
                                 clean, demo_containers, size_bytes)


class DockerTests(unittest.TestCase):
    def test_stats_and_inspect_merge_including_stopped_container(self):
        running, stopped = "a" * 64, "b" * 64
        listing = "\n".join(json.dumps(row) for row in [
            {"ID": running, "Names": "web", "Image": "nginx:alpine", "State": "running", "Status": "Up 3 hours", "Ports": "8080->80/tcp"},
            {"ID": stopped, "Names": "db", "Image": "postgres:16", "State": "exited", "Status": "Exited (0)"}])
        detail = json.dumps([{"Id": running, "HostConfig": {"Memory": 0}},
                             {"Id": stopped, "HostConfig": {"Memory": 512 * 1024**2}}])
        stats = json.dumps({"ID": running, "CPUPerc": "1.25%", "MemPerc": "25%",
                            "MemUsage": "256MiB / 1GiB", "NetIO": "1.5MB / 900kB"})
        client = DockerClient()
        with patch.object(client, "run", side_effect=[listing, detail, stats]) as run:
            containers = client.containers()
        self.assertEqual([c.name for c in containers], ["web", "db"])
        self.assertEqual(containers[0].cpu, 1.25)
        self.assertEqual(containers[0].memory, 256 * 1024**2)
        self.assertEqual(containers[0].net_rx, 1_500_000)
        self.assertEqual(containers[1].cpu, 0)
        self.assertEqual(containers[1].limit, 512 * 1024**2)
        stats_args = run.call_args_list[2].args
        self.assertIn(running, stats_args)
        self.assertNotIn(stopped, stats_args)

    def test_empty_daemon_does_not_inspect_or_collect_stats(self):
        client = DockerClient()
        with patch.object(client, "run", return_value="") as run:
            self.assertEqual(client.containers(), [])
        self.assertEqual(run.call_count, 1)

    def test_socket_error_and_timeout_are_readable(self):
        client = DockerClient()
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "permission denied")):
            with self.assertRaisesRegex(DockerError, "permission denied"):
                client.containers()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 12)):
            with self.assertRaisesRegex(DockerError, "Zeitlimit"):
                client.containers()

    def test_logs_include_stderr_and_strip_controls(self):
        result = subprocess.CompletedProcess([], 0, "normal\n", "\x1b[31merror\x07\n")
        with patch("subprocess.run", return_value=result):
            lines = DockerClient().logs("a" * 64)
        self.assertEqual(lines[0], "normal")
        self.assertNotIn("\x1b", lines[1])
        self.assertIn("error", lines[1])

    def test_deletion_never_forces_or_removes_volumes(self):
        client = DockerClient()
        with patch.object(client, "run") as run:
            client.action("rm", "a" * 64)
        run.assert_called_once_with("rm", "a" * 64, timeout=40)
        with self.assertRaises(ValueError):
            client.action("system", "anything")

    def test_units_and_sanitizing(self):
        self.assertEqual(size_bytes("1.5 GiB"), 1.5 * 1024**3)
        self.assertEqual(size_bytes("12.3kB"), 12300)
        self.assertEqual(size_bytes("--"), 0)
        self.assertEqual(clean("a\n\x00b\x1b"), "ab")

    def test_demo_actions_do_not_touch_docker(self):
        backend = Backend(demo=True)
        backend.action("rm", demo_containers(0)[0].id)
        # Run exactly one worker iteration without timing-dependent sleeps.
        def stop_after_iteration(_):
            backend.closed.set()
        with patch.object(backend.client, "action") as action, patch.object(backend.wake, "wait", side_effect=stop_after_iteration):
            backend._loop()
        action.assert_not_called()
        self.assertEqual(len(backend.read().containers), 7)
        self.assertIn("deaktiviert", backend.read().notice)

    def test_daemon_failure_preserves_last_data(self):
        backend = Backend()
        backend.snapshot.containers = demo_containers(0)
        backend.snapshot.updated = 123.
        def stop_after_iteration(_):
            backend.closed.set()
        with patch.object(backend.client, "containers", side_effect=DockerError("offline")), patch.object(backend.wake, "wait", side_effect=stop_after_iteration):
            backend._loop()
        self.assertEqual(backend.read().updated, 123.)
        self.assertEqual(len(backend.read().containers), 7)
        self.assertEqual(backend.read().error, "offline")

    def test_host_deltas_and_physical_disk_filter(self):
        sampler = HostSampler()
        proc = {"stat": "cpu 100 0 0 100 0 0 0 0\n",
                "meminfo": "MemTotal: 1000 kB\nMemAvailable: 700 kB\nMemFree: 100 kB\n",
                "dev": "header\nheader\n docker0: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0\n veth0: 9000 0 0 0 0 0 0 0 9000\n",
                "diskstats": "8 0 sda 1 0 10 0 1 0 20 0 0 0 0\n8 1 sda1 1 0 10 0 1 0 20 0 0 0 0\n"}
        def read(path):
            return proc[path.name]
        with patch("pathlib.Path.read_text", read), patch("time.monotonic", side_effect=[10., 12.]):
            first = sampler.sample()
            proc["stat"] = "cpu 150 0 0 150 0 0 0 0\n"
            proc["dev"] = proc["dev"].replace("1000", "3000").replace("2000", "4000")
            proc["diskstats"] = proc["diskstats"].replace("sda 1 0 10", "sda 1 0 14")
            second = sampler.sample()
        self.assertTrue(first["available"])
        self.assertEqual(second["cpu"], 50.)
        self.assertEqual(second["rx"], 1000.)
        self.assertEqual(second["read"], 1024.)
        self.assertEqual(second["memory"], 300 * 1024)


if __name__ == "__main__":
    unittest.main()
