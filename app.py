#!/usr/bin/env python3
"""bambu-bridge: expose Bambu Lab print status over HTTP, key-gated.

Runs on the NAS next to the printer. Logs into Bambu Cloud with the owner's
account, holds an MQTT subscription to the printer, and serves a tiny HTTP API
so an assistant (or anything else) can ask "how's my print?" without the
Bambu password ever leaving this box.

Endpoints (all key-gated except /health):
  GET /health  -> {"ok": true}
  GET /status  -> {"state","progress_pct","remaining_min","file",
                   "nozzle_c","bed_c","data_age_s"}

Auth: send your API_KEY as the X-Api-Key header.
"""

import asyncio
import hmac
import json
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "ha-bambulab",
        "custom_components",
        "bambu_lab",
    ),
)

from pybambu import BambuClient  # noqa: E402
from pybambu.bambu_cloud import (  # noqa: E402
    BambuCloud,
    CodeRequiredError,
    TfaCodeRequiredError,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("bambu-bridge")

EMAIL = os.environ["BAMBU_EMAIL"]
PASSWORD = os.environ["BAMBU_PASSWORD"]
REGION = os.environ.get("BAMBU_REGION", "us")
SERIAL = os.environ.get("BAMBU_SERIAL", "").strip()
DEVICE_TYPE = os.environ.get("BAMBU_DEVICE_TYPE", "A1")
API_KEY = os.environ["API_KEY"]
PORT = int(os.environ.get("PORT", "8080"))

state = {"client": None, "last_update": 0.0}


def on_event(_event: str):
    state["last_update"] = time.time()


def pick_serial(cloud: BambuCloud) -> str:
    if SERIAL:
        return SERIAL
    devices = cloud.get_device_list() or []
    serials = []
    for d in devices:
        s = d.get("dev_id") or d.get("serial")
        if s:
            serials.append((s, d.get("name", "?")))
    if len(serials) == 1:
        log.info("Auto-detected printer: %s (%s)", serials[0][1], serials[0][0])
        return serials[0][0]
    if not serials:
        raise RuntimeError("No printers found on this Bambu account.")
    names = ", ".join(f"{name} [{s}]" for s, name in serials)
    raise RuntimeError(
        f"Multiple printers found ({names}). Set BAMBU_SERIAL to one of them."
    )


def build_client() -> BambuClient:
    cloud = BambuCloud(region="", email="", username="", auth_token="")
    try:
        cloud.login(REGION, EMAIL, PASSWORD)
    except CodeRequiredError:
        raise RuntimeError(
            "Bambu wants an email verification code. Sign into the Bambu Handy "
            "app once, then restart this container."
        )
    except TfaCodeRequiredError:
        raise RuntimeError(
            "This Bambu account has 2FA enabled, so the bridge can't re-login "
            "on its own. Disable 2FA on the Bambu account or expect to "
            "re-auth by hand every few months."
        )
    serial = pick_serial(cloud)
    config = {
        "host": "",
        "local_mqtt": False,
        "region": REGION,
        "email": EMAIL,
        "username": cloud.username,
        "auth_token": cloud.auth_token,
        "serial": serial,
        "device_type": DEVICE_TYPE,
        "enable_camera": False,
    }
    return BambuClient(config)


async def supervise():
    """Keep one MQTT client alive; rebuild it daily so the token stays fresh."""
    while True:
        client = None
        try:
            client = build_client()
            state["client"] = client
            await client.connect(on_event)
            log.info("MQTT connected, serving status")
            await asyncio.sleep(24 * 3600)
            log.info("Scheduled rebuild for token freshness")
        except Exception as e:  # noqa: BLE001 - must never die
            log.error("client error: %s; retrying in 60s", e)
            await asyncio.sleep(60)
        finally:
            state["client"] = None
            if client is not None:
                try:
                    client.disconnect()
                except Exception:  # noqa: BLE001, S110
                    pass


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return hmac.compare_digest(
            self.headers.get("X-Api-Key", ""), API_KEY
        )

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if self.path != "/status":
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        client = state["client"]
        if client is None:
            return self._send(503, {"error": "printer client not connected"})
        try:
            pj = client._device.print_job
            temp = client._device.temperature
            payload = {
                "state": pj.gcode_state,
                "progress_pct": pj.print_percentage,
                "remaining_min": pj.remaining_time,
                "file": pj.subtask_name,
                "nozzle_c": round(temp.active_nozzle_temperature(), 1),
                "bed_c": round(temp.bed_temp, 1),
                "data_age_s": (
                    round(time.time() - state["last_update"], 1)
                    if state["last_update"]
                    else None
                ),
            }
        except Exception as e:  # noqa: BLE001
            return self._send(502, {"error": f"read failed: {e}"})
        return self._send(200, payload)

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(supervise())
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("bambu-bridge listening on :%d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
