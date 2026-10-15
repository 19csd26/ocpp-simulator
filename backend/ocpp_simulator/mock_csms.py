"""A small, permissive OCPP 2.0.1 CSMS for testing the simulator without a real backend.

It accepts every message a charging station can send, records them, and can send any command
to a connected station:

    csms = MockCsms(port=9000)
    await csms.start()
    result = await csms.command("SIM001", "Reset", {"type": "Immediate"})
"""
import asyncio
import base64
import logging
import re
from datetime import datetime, timezone
from http import HTTPStatus

from ocpp.charge_point import camel_to_snake_case, remove_nones, serialize_as_dict, snake_to_camel_case
from ocpp.routing import on
from ocpp.v201 import ChargePoint as BaseChargePoint
from ocpp.v201 import call, call_result
from ocpp.v201.enums import Action
from websockets.asyncio.server import serve

log = logging.getLogger("mock_csms")

CP_INITIATED = [
    "Authorize", "BootNotification", "ClearedChargingLimit", "DataTransfer", "FirmwareStatusNotification", "Get15118EVCertificate",
    "GetCertificateStatus", "Heartbeat", "LogStatusNotification", "MeterValues", "NotifyChargingLimit", "NotifyCustomerInformation",
    "NotifyDisplayMessages", "NotifyEVChargingNeeds", "NotifyEVChargingSchedule", "NotifyEvent", "NotifyMonitoringReport", "NotifyReport",
    "PublishFirmwareStatusNotification", "ReportChargingProfiles", "ReservationStatusUpdate", "SecurityEventNotification", "SignCertificate",
    "StatusNotification", "TransactionEvent",
]


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower().replace("e_v_", "ev_")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MockChargePointHandler(BaseChargePoint):
    csms: "MockCsms"


def _make_handler(name: str):
    async def handler(self, **kwargs):
        csms: MockCsms = self.csms
        csms.received.append({"cp": self.id, "action": name, "payload": kwargs, "at": _now()})
        csms.on_message(self.id, name, kwargs)
        if csms.delay:
            await asyncio.sleep(csms.delay)
        reply = csms.reply(self.id, name, kwargs)
        return getattr(call_result, name)(**reply)

    handler.__name__ = f"on_{_snake(name)}"  # the ocpp library routes by function name
    return on(Action(name))(handler)


for _name in CP_INITIATED:
    setattr(MockChargePointHandler, f"on_{_snake(_name)}", _make_handler(_name))


class _Adapter:
    def __init__(self, ws):
        self.ws = ws

    async def recv(self) -> str:
        return await self.ws.recv()

    async def send(self, msg: str) -> None:
        await self.ws.send(msg)


class MockCsms:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, *, passwords: dict[str, str] | None = None,
                 allowed_tokens: set[str] | None = None, boot_status: str = "Accepted", interval: int = 30) -> None:
        self.host, self.port = host, port
        self.passwords = passwords or {}  # station id -> Basic-auth password; empty = no authentication
        self.allowed_tokens = allowed_tokens  # None = every token is accepted
        self.boot_status = boot_status
        self.interval = interval
        self.delay = 0.0
        self.refuse: set[str] = set()  # actions to answer with a CALLERROR (via reply())
        self.connections: dict[str, MockChargePointHandler] = {}
        self.received: list[dict] = []
        self.connect_log: list[tuple[str, str]] = []
        self._server = None
        self._tasks: set[asyncio.Task] = set()

    # ---- behaviour you can override or tweak in tests ------------------------------------------
    def on_message(self, cp_id: str, action: str, payload: dict) -> None:
        pass

    def reply(self, cp_id: str, action: str, payload: dict) -> dict:
        now = _now()
        if action == "BootNotification":
            return {"current_time": now, "interval": self.interval, "status": self.boot_status}
        if action == "Heartbeat":
            return {"current_time": now}
        if action == "Authorize":
            token = payload["id_token"]["id_token"]
            ok = self.allowed_tokens is None or token in self.allowed_tokens
            return {"id_token_info": {"status": "Accepted" if ok else "Invalid"}}
        if action == "TransactionEvent" and payload.get("id_token"):
            return {"id_token_info": {"status": "Accepted"}}
        if action == "NotifyEVChargingNeeds":
            return {"status": "Accepted"}
        if action == "NotifyEVChargingSchedule":
            return {"status": "Accepted"}
        if action == "SignCertificate":
            return {"status": "Accepted"}
        if action == "Get15118EVCertificate":
            return {"status": "Accepted", "exi_response": "ZXhp"}
        if action == "GetCertificateStatus":
            return {"status": "Accepted", "ocsp_result": "b2NzcA=="}
        if action == "DataTransfer":
            return {"status": "Accepted", "data": payload.get("data")}
        return {}

    # ---- lifecycle ------------------------------------------------------------------------------
    async def start(self) -> None:
        self._server = await serve(self._handle, self.host, self.port, subprotocols=["ocpp2.0.1"], process_request=self._auth, ping_interval=None)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for cp in list(self.connections.values()):
            await asyncio.sleep(0)
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for t in self._tasks:
            t.cancel()

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}/ocpp"

    def _auth(self, connection, request):
        cp_id = request.path.rstrip("/").rsplit("/", 1)[-1]
        expected = self.passwords.get(cp_id)
        if not expected:
            return None
        header = request.headers.get("Authorization", "")
        try:
            user, _, password = base64.b64decode(header.removeprefix("Basic ")).decode().partition(":")
        except Exception:
            user, password = "", ""
        if user != cp_id or password != expected:
            return connection.respond(HTTPStatus.UNAUTHORIZED, "invalid credentials\n")
        return None

    async def _handle(self, ws) -> None:
        cp_id = ws.request.path.rstrip("/").rsplit("/", 1)[-1]
        if ws.subprotocol != "ocpp2.0.1":
            await ws.close(1002)
            return
        cp = MockChargePointHandler(cp_id, _Adapter(ws))
        cp.csms = self
        previous = self.connections.get(cp_id)
        self.connections[cp_id] = cp
        self.connect_log.append((cp_id, "connected"))
        try:
            await cp.start()
        except Exception:  # connection closed
            pass
        finally:
            if self.connections.get(cp_id) is cp:
                del self.connections[cp_id]
            self.connect_log.append((cp_id, "disconnected"))

    # ---- helpers for tests ----------------------------------------------------------------------
    def actions(self, cp_id: str | None = None) -> list[str]:
        return [r["action"] for r in self.received if cp_id is None or r["cp"] == cp_id]

    def messages(self, action: str, cp_id: str | None = None) -> list[dict]:
        return [r["payload"] for r in self.received if r["action"] == action and (cp_id is None or r["cp"] == cp_id)]

    async def wait_for(self, action: str, *, cp_id: str | None = None, count: int = 1, timeout: float = 10.0, predicate=None) -> list[dict]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            found = [m for m in self.messages(action, cp_id) if predicate is None or predicate(m)]
            if len(found) >= count:
                return found
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"Timed out waiting for {count} x {action} (got {len(found)}); saw {self.actions(cp_id)[-12:]}")
            await asyncio.sleep(0.05)

    async def command(self, cp_id: str, action: str, payload: dict | None = None, *, timeout: float = 15.0) -> dict:
        """Send a CSMS -> station command (payload in the spec's camelCase) and return the response as camelCase."""
        cp = self.connections.get(cp_id)
        if cp is None:
            raise ConnectionError(f"{cp_id} is not connected")
        message = getattr(call, action)(**camel_to_snake_case(payload or {}))
        response = await asyncio.wait_for(cp.call(message, suppress=False), timeout)
        return remove_nones(snake_to_camel_case(serialize_as_dict(response)))


async def _main() -> None:  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Permissive OCPP 2.0.1 CSMS for testing the simulator")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    csms = MockCsms(args.host, args.port)
    await csms.start()
    print(f"Mock CSMS listening on {csms.url}/<station id>")
    await asyncio.Event().wait()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_main())
