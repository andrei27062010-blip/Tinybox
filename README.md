# Tinybox

A tiny, self-hosted server dashboard designed for small Linux machines. Runs as a plain Python script with **no third-party dependencies**.

## What it does

- Serves a responsive dark dashboard.
- Reports uptime, load averages, memory, hostname, platform, Python version, and process RSS/thread count.
- Exposes `/api/health` and `/api/metrics`.
- Prefers Linux cgroup memory counters when available, so RAM usage can reflect the container limit; otherwise it falls back to host `/proc/meminfo`.
- Uses only the Python standard library. No Docker image or package installation is required for a normal run.

## Run

Requires Python 3.8+.

```sh
python main.py
```

The server listens on `0.0.0.0:8080` by default. Set `PORT` if your host provides a different port, for example:

```sh
PORT=5000 python main.py
```

Choose the hosting panel's **Script** launch mode and set the entry point to `main.py`.

## Endpoints

- `GET /` — dashboard
- `GET /api/health` — health response, `{"status":"ok"}`
- `GET /api/metrics` — system and process metrics

The dashboard refreshes metrics every 3 seconds. On Linux containers, metrics depend on which `/proc` and cgroup files the host exposes. If cgroup counters are unavailable, memory falls back to host-level information.

## Resource testing

The application intentionally uses only the standard library. Measure actual usage on the target host; the process RSS is included in `/api/metrics`.
