"""
Networking utilities: socket creation, ack tracking, network simulation.
"""

import socket
import time
import random

from common.config import DEFAULT_HOST, DEFAULT_PORT


def create_server_socket(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> socket.socket:
    """Create and bind a non-blocking UDP socket for the server."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Increase OS send/receive buffers
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
    sock.bind((host, port))
    sock.setblocking(False)
    return sock


def create_client_socket() -> socket.socket:
    """Create a non-blocking UDP socket for the client."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    return sock


class AckTracker:
    """
    Tracks sent/received packet sequence numbers and acknowledgements.
    Uses a 32-bit bitfield for piggybacked acks.
    """

    def __init__(self):
        self.local_sequence = 0
        self.remote_sequence = 0
        self.ack_bitfield = 0

        self.sent_packets = {}        # seq -> send_time
        self.acked_packets = set()
        self.lost_packets = set()

        # Statistics
        self.total_sent = 0
        self.total_received = 0
        self.total_acked = 0
        self.total_lost = 0

    def next_sequence(self) -> int:
        """Get next outgoing sequence number."""
        self.local_sequence = (self.local_sequence + 1) & 0xFFFF
        self.total_sent += 1
        return self.local_sequence

    def on_packet_sent(self, seq: int):
        """Record that we sent a packet with this sequence."""
        self.sent_packets[seq] = time.perf_counter()

    def on_packet_received(self, remote_seq: int):
        """Update ack state when we receive a packet from the remote."""
        self.total_received += 1

        if remote_seq == 0:
            return

        if self._sequence_greater_than(remote_seq, self.remote_sequence):
            diff = (remote_seq - self.remote_sequence) & 0xFFFF
            if diff <= 32:
                self.ack_bitfield = (self.ack_bitfield << diff) | 1
            else:
                self.ack_bitfield = 1
            self.remote_sequence = remote_seq
        else:
            diff = (self.remote_sequence - remote_seq) & 0xFFFF
            if 0 < diff <= 32:
                self.ack_bitfield |= (1 << diff)

    def on_ack_received(self, ack_seq: int, ack_bitfield: int):
        """Process acks from the remote telling us which of our packets they received."""
        if ack_seq > 0:
            self._mark_acked(ack_seq)
        for i in range(32):
            if ack_bitfield & (1 << i):
                past_seq = (ack_seq - 1 - i) & 0xFFFF
                if past_seq > 0:
                    self._mark_acked(past_seq)

    def _mark_acked(self, seq: int):
        if seq not in self.acked_packets:
            self.acked_packets.add(seq)
            self.total_acked += 1
            self.sent_packets.pop(seq, None)

    def detect_lost_packets(self, max_age: float = 1.0) -> list:
        """Detect packets that were sent but never acked within max_age seconds."""
        now = time.perf_counter()
        lost = []
        for seq, send_time in list(self.sent_packets.items()):
            if seq in self.acked_packets:
                del self.sent_packets[seq]
                continue
            if now - send_time > max_age:
                lost.append(seq)
                self.lost_packets.add(seq)
                self.total_lost += 1
                del self.sent_packets[seq]
        return lost

    def get_loss_rate(self) -> float:
        """Get overall packet loss rate."""
        total = self.total_acked + self.total_lost
        if total == 0:
            return 0.0
        return self.total_lost / total

    @staticmethod
    def _sequence_greater_than(s1: int, s2: int) -> bool:
        """Compare sequence numbers with wrap-around handling."""
        return ((s1 > s2) and (s1 - s2 <= 32768)) or \
               ((s1 < s2) and (s2 - s1 > 32768))


class NetworkSimulator:
    """
    Wraps a UDP socket with simulated network conditions:
    latency, jitter, packet loss, duplication.
    """

    def __init__(self, sock: socket.socket, loss_rate: float = 0.0,
                 min_latency: float = 0.0, max_latency: float = 0.0):
        self.sock = sock
        self.loss_rate = loss_rate
        self.min_latency = min_latency
        self.max_latency = max_latency
        self.delayed_packets = []

    def sendto(self, data: bytes, addr: tuple):
        """Send with simulated conditions."""
        if random.random() < self.loss_rate:
            return  # Dropped

        if self.min_latency > 0 or self.max_latency > 0:
            delay = random.uniform(self.min_latency, self.max_latency)
            deliver_time = time.perf_counter() + delay
            self.delayed_packets.append((deliver_time, data, addr))
        else:
            self.sock.sendto(data, addr)

    def flush(self):
        """Send any delayed packets whose time has elapsed."""
        now = time.perf_counter()
        still_waiting = []
        for deliver_time, data, addr in self.delayed_packets:
            if now >= deliver_time:
                try:
                    self.sock.sendto(data, addr)
                except (BlockingIOError, OSError):
                    pass
            else:
                still_waiting.append((deliver_time, data, addr))
        self.delayed_packets = still_waiting
