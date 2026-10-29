import argparse
import json
import os
import sys

from .backend import DockerClient, DockerError, HostSampler, demo_containers


def main():
    parser = argparse.ArgumentParser(description="Docker Dashboard — Live-TUI im Stil der Referenz")
    parser.add_argument("--demo", action="store_true", help="Simulierte Container, kein Docker-Zugriff")
    parser.add_argument("--refresh", type=float, default=2., metavar="SEKUNDEN", help="Refresh-Intervall (Standard: 2)")
    parser.add_argument("--check", action="store_true", help="Verbindung pruefen, JSON ausgeben und beenden")
    args = parser.parse_args()
    if not .5 <= args.refresh <= 3600:
        parser.error("--refresh muss zwischen 0.5 und 3600 Sekunden liegen")
    if args.check:
        try:
            containers = demo_containers(0) if args.demo else DockerClient().containers()
        except (DockerError, ValueError, KeyError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, "demo": args.demo, "containers": len(containers),
                          "running": sum(c.state == "running" for c in containers),
                          "host_metrics": HostSampler().sample()["available"]}, ensure_ascii=False))
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("Interaktives Terminal benoetigt. Verwende --check fuer einen Verbindungstest.")
    if os.environ.get("TERM", "dumb") == "dumb":
        parser.error("Terminal unterstuetzt keine TUI. TERM pruefen (z.B. xterm-256color).")
    from .backend import Backend
    from .ui import run
    try:
        run(Backend(args.refresh, args.demo))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
