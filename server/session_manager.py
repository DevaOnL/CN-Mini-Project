"""
Session lifecycle: CONNECTING -> ACTIVE -> IDLE -> EXPIRED.
Sessions are keyed by reconnect token instead of socket address.
"""

import time
import uuid
from enum import Enum, auto


class SessionState(Enum):
    CONNECTING = auto()
    ACTIVE = auto()
    IDLE = auto()
    EXPIRED = auto()


class Session:
    IDLE_AFTER = 10.0
    EXPIRE_AFTER = 30.0

    def __init__(self, token: str, client_id: int, address: tuple):
        now = time.monotonic()
        self.token = token
        self.client_id = client_id
        self.address = address
        self.state = SessionState.CONNECTING
        self.last_heard = now
        self.created_at = now

    def touch(self):
        self.last_heard = time.monotonic()
        self.state = SessionState.ACTIVE

    def mark_idle(self):
        self.state = SessionState.IDLE

    def tick(self) -> SessionState:
        idle_for = time.monotonic() - self.last_heard
        if (
            self.state in (SessionState.CONNECTING, SessionState.ACTIVE)
            and idle_for > self.IDLE_AFTER
        ):
            self.state = SessionState.IDLE
        if self.state == SessionState.IDLE and idle_for > self.EXPIRE_AFTER:
            self.state = SessionState.EXPIRED
        return self.state


class SessionManager:
    def __init__(self):
        self.by_token: dict[str, Session] = {}
        self.by_address: dict[tuple, str] = {}
        self.last_expired_tokens: list[str] = []

    def _unbind_address(self, address: tuple, *, token: str | None = None):
        bound_token = self.by_address.get(address)
        if bound_token is None:
            return
        if token is None or bound_token == token:
            self.by_address.pop(address, None)

    def create(
        self, address: tuple, client_id: int, token: str | None = None
    ) -> Session:
        token = token or uuid.uuid4().hex[:16]
        existing = self.by_token.get(token)
        if existing is not None:
            self._unbind_address(existing.address, token=token)
        session = Session(token, client_id, address)
        self.by_token[token] = session
        self.by_address[address] = token
        return session

    def get_by_addr(self, addr: tuple) -> Session | None:
        token = self.by_address.get(addr)
        return self.by_token.get(token) if token else None

    def get_by_token(self, token: str) -> Session | None:
        return self.by_token.get(token)

    def reconnect(self, token: str, new_addr: tuple) -> Session | None:
        session = self.by_token.get(token)
        if session is None or session.state == SessionState.EXPIRED:
            return None

        self._unbind_address(session.address, token=token)
        session.address = new_addr
        self.by_address[new_addr] = token
        session.touch()
        return session

    def remove(self, token: str):
        session = self.by_token.pop(token, None)
        if session is not None:
            self._unbind_address(session.address, token=token)

    def mark_idle(self, token: str):
        session = self.by_token.get(token)
        if session is not None:
            self._unbind_address(session.address, token=token)
            session.mark_idle()

    def expire_sessions(self) -> list[int]:
        self.last_expired_tokens = []
        expired_ids = []
        for token, session in list(self.by_token.items()):
            if session.tick() == SessionState.EXPIRED:
                self._unbind_address(session.address, token=token)
                del self.by_token[token]
                self.last_expired_tokens.append(token)
                expired_ids.append(session.client_id)
        return expired_ids
