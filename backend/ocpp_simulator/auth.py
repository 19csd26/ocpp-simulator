"""Optional shared-token protection for the API.

Set SIM_TOKEN to turn it on. The UI exchanges the token for an HttpOnly cookie at POST /api/login; scripts can send
`Authorization: Bearer <token>` instead. Without SIM_TOKEN the API is open, which is only safe on localhost.
"""
import hashlib
import hmac
import os
import time
from collections import deque

COOKIE = "sim_session"
PUBLIC = {"/api/auth", "/api/login", "/api/logout"}  # reachable without a session
MAX_FAILURES, WINDOW_S = 10, 300


class Auth:
    def __init__(self, token: str | None = None) -> None:
        self.token = token if token is not None else os.environ.get("SIM_TOKEN") or None
        self._failures: deque[float] = deque()

    @property
    def required(self) -> bool:
        return self.token is not None

    def _digest(self) -> str:
        # The cookie holds a digest, never the token itself.
        return hashlib.sha256(b"ocpp-sim-session:" + (self.token or "").encode()).hexdigest()

    def session_value(self) -> str:
        return self._digest()

    def check_token(self, candidate: str) -> bool:
        return self.token is not None and hmac.compare_digest(candidate.encode(), self.token.encode())

    def allows(self, cookies: dict[str, str], headers) -> bool:
        if not self.required:
            return True
        cookie = cookies.get(COOKIE)
        if cookie and hmac.compare_digest(cookie, self._digest()):
            return True
        bearer = headers.get("authorization", "")
        return bearer.lower().startswith("bearer ") and self.check_token(bearer[7:].strip())

    # ---- brute-force limit: after MAX_FAILURES wrong tokens, logins are refused for a while ----------
    def locked(self) -> bool:
        now = time.monotonic()
        while self._failures and now - self._failures[0] > WINDOW_S:
            self._failures.popleft()
        return len(self._failures) >= MAX_FAILURES

    def failed(self) -> None:
        self._failures.append(time.monotonic())
