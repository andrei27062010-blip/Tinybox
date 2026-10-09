#!/usr/bin/env python3
"""Tinybox: lightweight, dependency-free server dashboard."""
import json
import os
import platform
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
STARTED_AT = time.monotonic()


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def read_int(path):
    value = read_text(path)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def memory_metrics():
    # Prefer cgroup limits/current usage so container metrics describe this container.
    for base in (Path("/sys/fs/cgroup"), Path("/sys/fs/cgroup/memory")):
        current_path = base / ("memory.current" if base.name == "cgroup" else "memory.usage_in_bytes")
        limit_path = base / ("memory.max" if base.name == "cgroup" else "memory.limit_in_bytes")
        current = read_int(current_path)
        limit_text = read_text(limit_path)
        try:
            limit = int(limit_text) if limit_text and limit_text != "max" else None
        except ValueError:
            limit = None
        if current is not None and limit is not None and 0 < limit < (1 << 60):
            return {"memTotalBytes": limit, "memAvailBytes": max(0, limit - current),
                    "memorySource": "container cgroup"}

    values = {}
    info = read_text("/proc/meminfo")
    if info:
        for line in info.splitlines():
            match = re.match(r"^(MemTotal|MemAvailable):\\s+(\\d+)\\s+kB", line)
            if match:
                values[match.group(1)] = int(match.group(2)) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    return {"memTotalBytes": total, "memAvailBytes": available,
            "memorySource": "host /proc/meminfo" if total else "unavailable"}


def collect_metrics():
    uptime = read_text("/proc/uptime")
    load = read_text("/proc/loadavg")
    process_status = read_text("/proc/self/status") or ""
    process_rss = 0
    process_threads = 1
    for line in process_status.splitlines():
        if line.startswith("VmRSS:"):
            try:
                process_rss = int(line.split()[1]) * 1024
            except (ValueError, IndexError):
                pass
        elif line.startswith("Threads:"):
            try:
                process_threads = int(line.split()[1])
            except (ValueError, IndexError):
                pass

    loads = [None, None, None]
    if load:
        try:
            loads = [float(value) for value in load.split()[:3]]
        except ValueError:
            pass

    memory = memory_metrics()
    return {
        "hostname": platform.node() or "unknown",
        "os": platform.system().lower(),
        "arch": platform.machine() or "unknown",
        "cpus": os.cpu_count() or 1,
        "uptimeSeconds": float(uptime.split()[0]) if uptime else 0,
        "load1": loads[0], "load5": loads[1], "load15": loads[2],
        "memTotalBytes": memory["memTotalBytes"],
        "memAvailBytes": memory["memAvailBytes"],
        "memorySource": memory["memorySource"],
        "processRssBytes": process_rss,
        "threads": process_threads,
        "pythonVersion": platform.python_version(),
        "collectedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "processUptimeSeconds": time.monotonic() - STARTED_AT,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "Tinybox/1.0"
    sys_version = ""

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self'; script-src 'self'; "
                         "connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        super().end_headers()

    def send_bytes(self, status, body, content_type, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        self.route()

    def do_HEAD(self):
        self.route()

    def route(self):
        path = urlsplit(self.path).path
        if path == "/api/health":
            self.send_bytes(200, b'{"status":"ok"}\\n', "application/json; charset=utf-8")
            return
        if path == "/api/metrics":
            body = json.dumps(collect_metrics(), separators=(",", ":")).encode("utf-8")
            self.send_bytes(200, body, "application/json; charset=utf-8")
            return
        files = {
            "/": ("index.html", "text/html; charset=utf-8", "no-cache"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8", "public, max-age=300"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8", "public, max-age=300"),
        }
        item = files.get(path)
        if item is None:
            self.send_bytes(404, b"Not found\\n", "text/plain; charset=utf-8")
            return
        filename, content_type, cache = item
        try:
            body = (WEB / filename).read_bytes()
        except OSError:
            self.send_bytes(500, b"Dashboard asset unavailable\\n", "text/plain; charset=utf-8")
            return
        self.send_bytes(200, body, content_type, cache)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.log_date_time_string(), fmt % args), flush=True)


def main():
    raw_port = os.environ.get("PORT", "8080").strip()
    try:
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit("PORT must be an integer between 1 and 65535")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print("Tinybox (Python %s) listening on 0.0.0.0:%s" %
          (platform.python_version(), port), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
