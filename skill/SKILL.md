---
name: "bambu-bridge-monitor"
description: "Check a Bambu Lab printer's live print status through this repo's bambu-bridge REST API."
---

# Bambu Bridge Monitor

Companion skill for the bambu-bridge in this repo. Gives an AI agent a live
one-line read on the printer: progress %, time left, temps, file name.

## Setup
The bridge must be deployed (see the repo README and `docker-compose.yml`).
Note the API key chosen at setup, then export:

```
export BAMBU_BRIDGE_URL="https://your-bridge-host"
export BAMBU_BRIDGE_API_KEY="your-key-here"
```

## Tooling
CLI at `skill/bambu`:

- `bambu status` — one-line human summary of the print.
- `bambu status --json` — raw bridge JSON.
- `bambu health` — bridge health check (unauthenticated `/health`).

Bridge endpoints used:
- `GET /health` — no auth; returns `{"ok": true}` when the service is up.
- `GET /status` — `X-Api-Key` header; returns `state` (RUNNING/PAUSE/FINISH/
  IDLE/FAILED), `file`, `progress_pct`, `remaining_min`, `nozzle_c`, `bed_c`,
  and `data_age_s` (seconds since the last MQTT update; over ~120s usually
  means the printer is off or offline).

## Auth
The API key is read from the `BAMBU_BRIDGE_API_KEY` environment variable at
runtime and sent as the `X-Api-Key` header to the configured bridge host only.
Nothing here collects, prints, logs, or persists the key.

## Operating Rules
1. Restrict requests to the configured bridge host only.
2. Never print, log, or persist the API key.
3. The bridge is read-only; it cannot pause or cancel prints. Don't imply it can.
4. If the bridge is unreachable, say so plainly instead of guessing printer state.
