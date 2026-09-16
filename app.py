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
  POST /verify -> {"code": "123456"} completes an email-verification login

Auth: send your API_KEY as the X-Api-Key header.
"""

import asyncio
import hmac
import json
import logging
import threading
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

state = {"client": None, "last_update": 0.0, "generation": 0}
_event_loop = None  # set in main(); do_POST uses it to run verify on the loop

DATA_DIR = os.environ.get("DATA_DIR", "/data")
SESSION_FILE = os.path.join(DATA_DIR, "session.json")


class CodeRequired(RuntimeError):
    """Bambu gated this login behind an email verification code."""


def save_session(cloud: BambuCloud) -> None:
    """Persist the auth token so restarts/redeploys skip re-login."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = SESSION_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(
                {
                    "username": cloud.username,
                    "auth_token": cloud.auth_token,
                },
                f,
            )
        os.replace(tmp, SESSION_FILE)
        log.info("Saved Bambu session to %s", SESSION_FILE)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not save session: %s", e)


def load_session_cloud() -> "BambuCloud | None":
    try:
        with open(SESSION_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("Could not read session file: %s", e)
        return None
    token = data.get("auth_token", "")
    if not token:
        return None
    return BambuCloud(
        region=REGION,
        email=EMAIL,
        username=data.get("username", ""),
        auth_token=token,
    )


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


def build_client_from_cloud(cloud: BambuCloud) -> BambuClient:
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


def build_client() -> BambuClient:
    cloud = BambuCloud(region="", email="", username="", auth_token="")
    try:
        cloud.login(REGION, EMAIL, PASSWORD)
    except CodeRequiredError:
        raise CodeRequired(
            "Bambu wants an email verification code. POST it to /verify "
            'as {"code": "123456"}.'
        )
    except TfaCodeRequiredError:
        raise RuntimeError(
            "This Bambu account has 2FA enabled, so the bridge can't re-login "
            "on its own. Disable 2FA on the Bambu account or expect to "
            "re-auth by hand every few months."
        )
    save_session(cloud)
    return build_client_from_cloud(cloud)


def build_client_from_session() -> "BambuClient | None":
    """Reuse the saved session token when it's still valid."""
    cloud = load_session_cloud()
    if cloud is None:
        return None
    try:
        ok = cloud.test_authentication(
            REGION, EMAIL, cloud.username, cloud.auth_token
        )
    except Exception as e:  # noqa: BLE001
        log.info("Saved session check failed (%s); will re-login", e)
        return None
    if not ok:
        log.info("Saved session expired; will re-login")
        return None
    log.info("Reusing saved Bambu session")
    return build_client_from_cloud(cloud)


def build_client_with_session() -> BambuClient:
    """Prefer the saved session; fall back to a password login."""
    client = build_client_from_session()
    if client is not None:
        return client
    return build_client()


def do_code_login(code: str) -> BambuCloud:
    """Complete the email-verification login Bambu demanded (blocking)."""
    cloud = BambuCloud(
        region=REGION, email=EMAIL, username="", auth_token=""
    )
    cloud.login_with_verification_code(code)
    return cloud


async def verify_and_connect(code: str) -> None:
    """Log in with a user-supplied verification code and go live."""
    cloud = await asyncio.to_thread(do_code_login, code)
    save_session(cloud)
    client = await asyncio.to_thread(build_client_from_cloud, cloud)
    await asyncio.wait_for(client.connect(on_event), timeout=120)
    old = state["client"]
    state["client"] = client
    state["generation"] += 1
    state["last_update"] = time.time()
    if old is not None:
        try:
            old.disconnect()
        except Exception:  # noqa: BLE001, S110
            pass
    log.info("Verified via code; MQTT connected, serving status")


async def supervise():
    """Keep one MQTT client alive; rebuild it daily so the token stays fresh."""
    while True:
        client = None
        gen = state["generation"]
        try:
            log.info("supervisor: logging into Bambu Cloud...")
            client = await asyncio.wait_for(
                asyncio.to_thread(build_client_with_session), timeout=120
            )
            state["client"] = client
            await asyncio.wait_for(client.connect(on_event), timeout=120)
            log.info("MQTT connected, serving status")
            await asyncio.sleep(24 * 3600)
            log.info("Scheduled rebuild for token freshness")
        except CodeRequired as e:
            # Bambu gated the login behind an email code. Retrying fast just
            # hammers their login endpoint and extends any throttling, so
            # back off hard. POSTing a code to /verify still works anytime.
            log.error("client error: %s; retrying in 60 min", e)
            await asyncio.sleep(3600)
        except Exception as e:  # noqa: BLE001 - must never die
            if state["client"] is not None:
                # A /verify login has us covered; retry quietly once an hour
                # in case password login starts working again.
                log.info("login still needs a code; keeping verified client")
                await asyncio.sleep(3600)
            else:
                log.error("client error: %s; retrying in 60s", e)
                await asyncio.sleep(60)
        finally:
            # Don't clobber a client installed by /verify after we sampled gen.
            if state["generation"] == gen:
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
                "nozzle_c": round(temp.active_nozzle_temperature, 1),
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

    def do_POST(self):  # noqa: N802
        if self.path != "/verify":
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            return self._send(400, {"error": "bad json"})
        code = str(body.get("code", "")).strip()
        if not code:
            return self._send(400, {"error": "missing code"})
        try:
            fut = asyncio.run_coroutine_threadsafe(
                verify_and_connect(code), _event_loop
            )
            fut.result(timeout=150)
        except Exception as e:  # noqa: BLE001
            return self._send(502, {"error": str(e)[:200]})
        return self._send(200, {"ok": True})

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)


def main():
    global _event_loop
    loop = asyncio.new_event_loop()
    _event_loop = loop

    def _run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    threading.Thread(target=_run_loop, daemon=True, name="bambu-loop").start()
    asyncio.run_coroutine_threadsafe(supervise(), loop)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("bambu-bridge v3 listening on :%d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
