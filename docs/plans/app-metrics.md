# App Metrics Instrumentation Plan

Instrument all four Go services to expose Prometheus metrics on `:9000/metrics`.
The `prometheus.yml` scrape config already targets these ports — no changes needed there once the servers are listening.

## Step 1 — Shared metrics package

Create `internal/metrics/metrics.go`:
- Starts a `/metrics` HTTP server on `:9000` (called from each service's `main`)
- Each service defines its own metric vars locally — do NOT define all metrics here, as that would pull irrelevant metrics into every binary
- Dependency to add: `github.com/prometheus/client_golang`

## Step 2 — api service

File: `cmd/api/main.go`

- Middleware wrapping every route: records `http_requests_total{route, method, status}` counter and `http_request_duration_seconds{route}` histogram
- Routes to instrument: `/signup`, `/login`, `/me/follows` (GET/POST/DELETE), `/players/search`
- Business gauges queried from DB on a 60s ticker: `users_total`, `follows_total`

## Step 3 — poller service

File: `cmd/poller/main.go`

- `mlb_api_requests_total{endpoint, status}` — increment on every MLB HTTP call
- `mlb_api_duration_seconds{endpoint}` — histogram, time each MLB HTTP call
- `transitions_emitted_total` — increment when an event is written to `events:transitions`
- `poll_errors_total{type}` — MLB errors, Redis errors

## Step 4 — fanout service

File: `cmd/fanout/main.go`

- `fanout_jobs_written_total` — per delivery job written to `events:deliveries`
- `fanout_errors_total{type}` — DB errors, Redis errors
- `fanout_processing_duration_seconds` — histogram, time per transition event processed

## Step 5 — delivery service

File: `cmd/delivery/main.go`

- `notifications_delivered_total` — successful Discord webhook posts
- `discord_webhook_requests_total{status}` — all attempts including failures
- `discord_webhook_duration_seconds` — histogram, time per webhook call
- `delivery_errors_total{type}` — webhook errors, DB errors

## Step 6 — verify scraping

Restart services and confirm all four `atbatwatch` targets show **UP** in Prometheus at `https://grafana.atbat.prerak.net` → Connections → Prometheus → Explore, or via `http://localhost:9090/targets` locally.

## Step 7 — custom Grafana dashboard

Add `grafana/dashboards/atbatwatch.json` with panels for all metrics above. Provisioning picks it up automatically on container restart.
