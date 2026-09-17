# bambu-bridge

Tiny service that watches your Bambu Lab printer through Bambu Cloud and
answers one question over HTTP: **how's my print?**

Your Bambu email/password live only in Dockhand's Environment tab for this
stack. Nothing with your credentials ever leaves the NAS. The outside world
only sees a key-gated API behind your Cloudflare Tunnel.

## Endpoints

All endpoints are key-gated via the header `X-Api-Key: <your-key>`,
except `/health`.

`GET /health` -> `{"ok": true}` (unauthenticated, for tunnel checks)

`GET /status` -> current printer state:

```json
{
  "state": "RUNNING",
  "progress_pct": 62,
  "remaining_min": 38,
  "file": "pistol_stand.3mf",
  "nozzle_c": 220.0,
  "bed_c": 65.0,
  "data_age_s": 4.2
}
```

`state` is the printer's gcode state: `RUNNING`, `PAUSE`, `FINISH`, `IDLE`,
`FAILED`, etc.

`data_age_s` is seconds since the last MQTT update arrived from the printer.
The bridge is push-based (no polling): the printer only sends data when
something changes. When the printer is off or unreachable, this number just
grows. Treat anything over ~120s as stale.

Error responses on `/status`:

- `401 {"error": "unauthorized"}`: wrong or missing API key.
- `503 {"error": "printer client not connected"}`: the bridge isn't logged
  into Bambu Cloud. Check the logs; if Bambu is asking for a verification
  code, use `/verify` below.
- `502 {"error": "read failed: ..."}`: connected, but reading the printer
  state failed.

`POST /verify` with JSON body `{"code": "123456"}`: completes the
email-verification login when Bambu gates the account behind a code. Codes
expire fast, so submit the freshest one you have. A successful verify saves
the session to `/data/session.json` and connects immediately.

## Setup

1. Create a **public** repo named `bambu-bridge` on GitHub and upload these
   files (drag and drop on the web UI works). The image itself contains no
   secrets, so public is fine and keeps the NAS pull simple.
2. Pushing to `main` triggers the workflow in `.github/workflows`, which
   builds the image and publishes it to
   `ghcr.io/dhrandy/bambu-bridge:latest`. Wait for the green check under
   the repo's Actions tab.
3. In Dockhand, create a new stack, paste in `docker-compose.yml`, and add
   these in the **Environment** tab:
   - `BAMBU_EMAIL` / `BAMBU_PASSWORD`: your Bambu Lab account. If the
     account has 2FA on, the bridge can't re-login by itself, so prefer an
     account without 2FA or expect a manual nudge every few months.
   - `BAMBU_SERIAL`: leave empty if this account has exactly one printer
     (it auto-detects). Otherwise find it in Bambu Handy under device
     settings.
   - `API_KEY`: invent a long random string. You'll also save this in the
     assistant's secure vault, so keep it handy.
4. Deploy. Check the logs for `bambu-bridge v3 listening on :8080` and
   `MQTT connected, serving status`.
5. Add a Cloudflare Tunnel ingress, e.g.
   `bambu-bridge.<your-domain>` -> `http://localhost:8080`
   (use `http://bambu-bridge:8080` if cloudflared runs in Docker on the
   same network).
6. Test: `curl -H "X-Api-Key: <key>" https://bambu-bridge.<your-domain>/status`
7. Tell your assistant the public URL. It will store the API key in its
   vault and wire up `bambu status` from there.

## Login and verification

Bambu sometimes gates a login behind an email verification code. When that
happens:

1. The bridge logs the demand and backs off to one login retry per hour.
   Don't hammer it; repeated attempts get the address throttled and the
   code emails stop arriving for a while.
2. Grab the freshest code from your email and POST it to `/verify`:
   `curl -H "X-Api-Key: <key>" -H "Content-Type: application/json" -d '{"code":"123456"}' https://bambu-bridge.<your-domain>/verify`
3. A `{"ok": true}` response means the session is saved and the bridge is
   connected. No restart needed.

## Session persistence

The login session lives at `/data/session.json` inside the container, on
the named volume `bambu-bridge-data`. Restarts, image updates, and
redeploys reuse it, so you only need a verification code again if the
session actually expires. If you ever need to force a fresh login, delete
the volume and redeploy.

## Updating the image

Dockhand workflow: stop the container, pull the image, start the container.
No image or container deletion needed. The session volume keeps you logged
in across updates.

## Image tags

Every push to `main` publishes three tags:

- `latest`: the newest build (what Dockhand pulls).
- `build-<run number>`: immutable tag for that exact build, e.g.
  `build-45`. Use it to pin a known-good build.
- `<git sha>`: immutable tag for the exact commit.

The build number is baked in at build time and reported two ways, so you
can always confirm which build is actually running:

- Container logs: `bambu-bridge v3 (build 45) listening on :8080`
- `GET /health` returns `{"ok": true, "build": "45"}`

## Notes

- The bridge re-logs-in and rebuilds its MQTT connection daily, so the
  Bambu token (expires after ~3 months) never goes stale silently.
- Read-only by design: it never sends commands to the printer.
- Ports are published as `28550:8080`. Binding to `127.0.0.1` caused
  reverse-proxy 502 errors in this setup, so the plain mapping is used.
- Uses `pybambu` vendored from
  [greghesp/ha-bambulab](https://github.com/greghesp/ha-bambulab)
  (the standalone PyPI `pybambu` package is stale).

## Agent skill

The `skill/` folder is a drop-in skill for AI agents (SKILL.md format). It
gives an agent a live one-line read on the printer through this bridge:

    export BAMBU_BRIDGE_URL="https://your-bridge-host"
    export BAMBU_BRIDGE_API_KEY="your-key-here"
    ./skill/bambu status

Also usable: `./skill/bambu status --json` for raw output, `./skill/bambu
health` for a health check. The skill is read-only; the bridge cannot pause
or cancel prints.
