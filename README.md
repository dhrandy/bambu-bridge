# bambu-bridge

Tiny service that watches your Bambu Lab printer through Bambu Cloud and
answers one question over HTTP: **how's my print?**

Your Bambu email/password live only in Dockhand's Environment tab for this
stack. Nothing with your credentials ever leaves the NAS. The outside world
only sees a key-gated `GET /status` behind your Cloudflare Tunnel.

## What it reports

`GET /status` (header `X-Api-Key: <your key>`):

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
`FAILED`, etc. `GET /health` is unauthenticated for tunnel checks.

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
4. Deploy. Check the logs for `MQTT connected, serving status`.
5. Add a Cloudflare Tunnel ingress, e.g.
   `bambu-bridge.<your-domain>` -> `http://localhost:8080`
   (use `http://bambu-bridge:8080` if cloudflared runs in Docker on the
   same network).
6. Test: `curl -H "X-Api-Key: <key>" https://bambu-bridge.<your-domain>/status`
7. Tell your assistant the public URL. It will store the API key in its
   vault and wire up `bambu status` from there.

## Notes

- The bridge re-logs-in and rebuilds its MQTT connection daily, so the
  Bambu token (expires after ~3 months) never goes stale silently.
- Read-only by design: it never sends commands to the printer.
- Uses `pybambu` vendored from
  [greghesp/ha-bambulab](https://github.com/greghesp/ha-bambulab)
  (the standalone PyPI `pybambu` package is stale).
