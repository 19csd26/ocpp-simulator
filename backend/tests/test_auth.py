"""Shared-token protection: locked by default when SIM_TOKEN is set, open when it is not."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ocpp_simulator.main import create_app

TOKEN = "correct-horse-battery-staple"


@pytest.fixture
def locked():
    with TestClient(create_app(token=TOKEN)) as client:
        yield client


def test_api_is_locked_without_a_session(locked):
    assert locked.get("/api/stations").status_code == 401
    assert locked.get("/api/meta").status_code == 401
    assert locked.post("/api/stations", json={"ocpp_id": "X1", "csms_url": "ws://x"}).status_code == 401
    assert locked.get("/api/auth").json() == {"required": True, "authenticated": False}


def test_the_ui_files_stay_reachable_so_the_login_page_can_load(locked):
    # no dist in the test checkout is fine: only the API must be locked, never a 401 on a static path
    assert locked.get("/index.html").status_code != 401


def test_wrong_token_is_refused_and_right_token_logs_in(locked):
    assert locked.post("/api/login", json={"token": "nope"}).status_code == 401
    ok = locked.post("/api/login", json={"token": TOKEN})
    assert ok.status_code == 200 and ok.json()["authenticated"] is True
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and TOKEN not in ok.headers["set-cookie"]
    assert locked.get("/api/stations").status_code == 200  # the cookie is now sent automatically
    assert locked.get("/api/auth").json()["authenticated"] is True
    locked.post("/api/logout")
    assert locked.get("/api/stations").status_code == 401


def test_cookie_is_secure_behind_a_tls_tunnel(locked):
    r = locked.post("/api/login", json={"token": TOKEN}, headers={"x-forwarded-proto": "https"})
    assert "secure" in r.headers["set-cookie"].lower()


def test_bearer_token_works_for_scripts_and_wrong_bearer_does_not(locked):
    assert locked.get("/api/stations", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert locked.get("/api/stations", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert locked.get("/api/stations", headers={"Authorization": TOKEN}).status_code == 401


def test_websocket_needs_the_session(locked):
    with pytest.raises(WebSocketDisconnect) as exc:
        with locked.websocket_connect("/ws"):
            pass
    assert exc.value.code == 4401
    locked.post("/api/login", json={"token": TOKEN})
    with locked.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "init"


def test_repeated_wrong_tokens_lock_login_out(locked):
    for _ in range(10):
        assert locked.post("/api/login", json={"token": "bad"}).status_code == 401
    assert locked.post("/api/login", json={"token": TOKEN}).status_code == 429  # even the right token waits


def test_open_when_no_token_is_configured(monkeypatch):
    monkeypatch.delenv("SIM_TOKEN", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/api/stations").status_code == 200
        assert client.get("/api/auth").json() == {"required": False, "authenticated": True}


def test_refuses_to_listen_on_the_network_without_a_token(monkeypatch):
    import sys
    from ocpp_simulator import __main__ as entry
    monkeypatch.delenv("SIM_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["ocpp_simulator", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit):
        entry.main()
