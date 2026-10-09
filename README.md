# Tinybox

A tiny, self-hosted server dashboard designed for small Linux machines. No Node.js, npm, frontend framework, external fonts, analytics, or third-party runtime dependencies.

## What it does

- Serves a responsive, dark dashboard from a single Go HTTP server.
- Reports Linux memory usage, load averages, uptime, hostname, and Go process memory.
- Exposes `/api/health` for health checks and `/api/metrics` for dashboard data.
- Uses embedded static assets, so deployment is one small binary.
- Includes a multi-stage Docker build and conservative container resource limits.

> System metrics are read from Linux `/proc`. On non-Linux hosts, unsupported metrics may be omitted.

## Run locally

Requires Go 1.24+.

```sh
go run .
```

Open http://localhost:8080.

## Build and run

```sh
go build -trimpath -ldflags="-s -w" -o tinybox .
./tinybox
```

Set `PORT` to change the listen port. The default is `8080`.

## Docker

```sh
docker build -t tinybox .
docker run --rm -p 8080:8080 --memory=64m --cpus=0.3 tinybox
```

Or use the included Compose file:

```sh
docker compose up --build -d
```

The Compose configuration caps the container at 64 MiB RAM and 0.3 CPU. These are example limits; adjust them to match your host and workload.

## Endpoints

- `GET /` — dashboard
- `GET /api/health` — small JSON health response
- `GET /api/metrics` — system and process metrics

## Resource-testing notes

Tinybox is intentionally dependency-free, but actual resource usage depends on the OS, architecture, Go version, and container runtime. Measure on the target server rather than assuming a fixed footprint:

```sh
docker stats tinybox
/usr/bin/time -v ./tinybox
```

The dashboard refreshes metrics every 3 seconds and avoids external requests.
