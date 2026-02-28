"""
Client connection management on the server side.
Tracks connected clients, their addresses, and connection health.
"""

import time
from common.config import CLIENT_TIMEOUT
from common.net import AckTracker


class ConnectedClient:
    """Represents a single connected client on the server."""

    def __init__(self, client_id: int, address: tuple):
        self.client_id = client_id
        self.address = address              # (ip, port)
        self.last_heard = time.time()
        self.last_ack_snapshot = 0           # Last snapshot tick client ack'd
        self.last_processed_input_seq = 0    # Last input seq processed by server
        self.pending_inputs = []            # Inputs queued for next tick
        self.ack_tracker = AckTracker()

        # Bandwidth tracking
        self.bytes_sent = 0
        self.bytes_received = 0
        self.bytes_sent_last = 0
        self.bytes_received_last = 0
        self.last_bandwidth_check = time.time()

    def touch(self):
        """Update the last-heard timestamp."""
        self.last_heard = time.time()

    def is_timed_out(self, timeout: float = CLIENT_TIMEOUT) -> bool:
        return time.time() - self.last_heard > timeout

    def get_bandwidth_kbps(self) -> tuple:
        """Get (sent_KB/s, recv_KB/s) since last check."""
        now = time.time()
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
        self.clients = {}         # client_id -> ConnectedClient
        self.addr_to_id = {}      # (ip, port) -> client_id
        self.next_id = 1

    def add_client(self, address: tuple) -> ConnectedClient:
        """Register a new client connection."""
        cid = self.next_id
        self.next_id += 1
        client = ConnectedClient(cid, address)
        self.clients[cid] = client
        self.addr_to_id[address] = cid
        return client

    def remove_client(self, client_id: int):
        """Remove a client."""
        client = self.clients.pop(client_id, None)
        if client:
            self.addr_to_id.pop(client.address, None)

    def get_by_address(self, addr: tuple) -> ConnectedClient:
        """Look up a client by their address."""
        cid = self.addr_to_id.get(addr)
        if cid is not None:
            return self.clients.get(cid)
        return None

    def has_address(self, addr: tuple) -> bool:
        return addr in self.addr_to_id

    def check_timeouts(self) -> list:
        """Find and remove timed-out clients. Returns list of removed IDs."""
        timed_out = [
            cid for cid, c in self.clients.items()
            if c.is_timed_out()
        ]
        for cid in timed_out:
            self.remove_client(cid)
        return timed_out

    def all_clients(self):
        """Iterate over all connected clients."""
        return list(self.clients.values())

    @property
    def count(self) -> int:
        return len(self.clients)
