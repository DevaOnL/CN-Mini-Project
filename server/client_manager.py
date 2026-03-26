"""
Client connection management on the server side.
Tracks connected clients, their addresses, and reconnect tokens.
"""

import time
import uuid

from common.config import CLIENT_TIMEOUT
from common.net import AckTracker


class ConnectedClient:
    """Represents a single connected client on the server."""

    def __init__(
        self,
        client_id: int,
        address: tuple,
        session_token: str | None = None,
    ):
        self.client_id = client_id
        self.address = address
        self.session_token = session_token or uuid.uuid4().hex[:16]
        self.connection_epoch = 0
        self.last_connect_nonce = 0
        self.last_heard = time.monotonic()
        self.accept_packets_after = self.last_heard
        self.last_processed_input_seq = 0
        self.pending_inputs: list[tuple[int, float, float, int]] = []
        self.ack_tracker = AckTracker()
        self.smoothed_rtt_ms = 0.0

        # Bandwidth tracking
        self.bytes_sent = 0
        self.bytes_received = 0
        self.bytes_sent_last = 0
        self.bytes_received_last = 0
        self.last_bandwidth_check = time.monotonic()

    def touch(self):
        """Update the last-heard timestamp."""
        self.last_heard = time.monotonic()

    def delay_nonconnect_packets(self, seconds: float):
        self.accept_packets_after = time.monotonic() + max(0.0, seconds)

    def is_timed_out(self, timeout: float = CLIENT_TIMEOUT) -> bool:
        return time.monotonic() - self.last_heard > timeout

    def get_bandwidth_KBps(self) -> tuple[float, float]:
        """Get (sent_KB/s, recv_KB/s) since last check."""
        now = time.monotonic()
        elapsed = now - self.last_bandwidth_check
        if elapsed <= 0:
            return (0.0, 0.0)

        sent_kbps = (self.bytes_sent - self.bytes_sent_last) / elapsed / 1024.0
        recv_kbps = (self.bytes_received - self.bytes_received_last) / elapsed / 1024.0

        self.bytes_sent_last = self.bytes_sent
        self.bytes_received_last = self.bytes_received
        self.last_bandwidth_check = now

        return (round(sent_kbps, 2), round(recv_kbps, 2))


class ClientManager:
    """Manages all connected clients."""

    def __init__(self):
        self.clients: dict[int, ConnectedClient] = {}
        self.addr_to_id: dict[tuple, int] = {}
        self.token_to_id: dict[str, int] = {}
        self.next_client_id = 1  # Counter for O(1) ID allocation
        self.freed_ids: list[int] = []  # Stack of freed IDs to reuse

    def _next_free_id(self) -> int:
        # Reuse freed IDs first (O(1))
        if self.freed_ids:
            return self.freed_ids.pop()
        # Allocate new ID (O(1))
        if self.next_client_id >= 65536:
            raise RuntimeError("No free client IDs")
        client_id = self.next_client_id
        self.next_client_id += 1
        return client_id

    def add_client(
        self,
        address: tuple,
        session_token: str | None = None,
    ) -> ConnectedClient:
        """Register a new client connection and recycle freed IDs safely."""
        client_id = self._next_free_id()
        client = ConnectedClient(
            client_id,
            address,
            session_token=session_token,
        )
        self.clients[client_id] = client
        self.addr_to_id[address] = client_id
        self.token_to_id[client.session_token] = client_id
        return client

    def restore_client(
        self,
        client_id: int,
        address: tuple,
        session_token: str,
    ) -> ConnectedClient:
        if client_id in self.clients:
            raise RuntimeError(f"Client ID {client_id} already active")

        client = ConnectedClient(
            client_id,
            address,
            session_token=session_token,
        )
        self.clients[client_id] = client
        self.addr_to_id[address] = client_id
        self.token_to_id[client.session_token] = client_id
        return client

    def remove_client(self, client_id: int):
        """Remove a client and all reverse lookups."""
        client = self.clients.pop(client_id, None)
        if client:
            self.addr_to_id.pop(client.address, None)
            self.token_to_id.pop(client.session_token, None)
            # Add freed ID back to the pool for reuse
            self.freed_ids.append(client_id)

    def bind_address(self, client: ConnectedClient, address: tuple):
        """Move a client to a new socket address during reconnect."""
        self.addr_to_id.pop(client.address, None)
        client.address = address
        self.addr_to_id[address] = client.client_id

    def get_by_address(self, addr: tuple) -> ConnectedClient | None:
        """Look up a client by their address."""
        client_id = self.addr_to_id.get(addr)
        if client_id is not None:
            return self.clients.get(client_id)
        return None

    def get_by_token(self, token: str) -> ConnectedClient | None:
        """Look up a client by their reconnect token."""
        client_id = self.token_to_id.get(token)
        if client_id is not None:
            return self.clients.get(client_id)
        return None

    def has_address(self, addr: tuple) -> bool:
        return addr in self.addr_to_id

    def has_token(self, token: str) -> bool:
        return token in self.token_to_id

    def check_timeouts(self) -> list[int]:
        """Legacy timeout helper retained for compatibility with tests/tools."""
        timed_out = [
            client_id
            for client_id, client in self.clients.items()
            if client.is_timed_out()
        ]
        for client_id in timed_out:
            self.remove_client(client_id)
        return timed_out

    def all_clients(self) -> list[ConnectedClient]:
        """Iterate over all connected clients."""
        return list(self.clients.values())

    @property
    def count(self) -> int:
        return len(self.clients)
