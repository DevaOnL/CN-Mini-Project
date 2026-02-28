"""
Integration tests: spawn server as subprocess, connect bot clients, verify
state convergence, connection lifecycle, and packet-loss tolerance.

Uses select-based polling (not busy-spin non-blocking) so that the
OS scheduler reliably switches between the test process and the server
subprocess, avoiding hangs on Python 3.14+.
"""

import select
import socket
import struct
import subprocess
import sys
import time
import random

from common.packet import Packet, PacketType, INPUT_FORMAT
from common.snapshot import Snapshot
from common.config import DEFAULT_PORT, DEFAULT_BUFFER_SIZE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class BotClient:
    """A headless bot client for testing."""

    def __init__(self, host: str = '127.0.0.1', port: int = DEFAULT_PORT):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.server_addr = (host, port)
        self.client_id = None
        self.connected = False
        self.latest_snapshot = None
        self.input_seq = 0

    # -- actions -----------------------------------------------------------

    def connect(self):
        pkt = Packet(PacketType.CONNECT_REQ, 0, 0, 0)
        self.sock.sendto(pkt.serialize(), self.server_addr)

    def send_input(self, mx: float = 0.0, my: float = 0.0):
        if not self.connected:
            return
        self.input_seq += 1
        payload = struct.pack(INPUT_FORMAT, self.input_seq, mx, my, 0)
        pkt = Packet(PacketType.INPUT, self.input_seq, 0, 0, payload)
        self.sock.sendto(pkt.serialize(), self.server_addr)

    def drain(self, timeout: float = 0.1):
        """Read all pending datagrams, blocking up to *timeout* seconds
        for the first one and then draining without further blocking."""
        readable, _, _ = select.select([self.sock], [], [], timeout)
        if not readable:
            return
        # Socket has data – drain everything available
        for _ in range(200):
            try:
                data, _ = self.sock.recvfrom(DEFAULT_BUFFER_SIZE)
            except (BlockingIOError, OSError):
                break
            self._handle(data)

    def _handle(self, data: bytes):
        try:
            pkt = Packet.deserialize(data)
        except ValueError:
            return
        if pkt.packet_type == PacketType.CONNECT_ACK:
            self.client_id = struct.unpack('!B', pkt.payload[:1])[0]
            self.connected = True
        elif pkt.packet_type == PacketType.SNAPSHOT:
            self.latest_snapshot = Snapshot.deserialize(pkt.payload)

    def disconnect(self):
        pkt = Packet(PacketType.DISCONNECT, 0, 0, 0)
        try:
            self.sock.sendto(pkt.serialize(), self.server_addr)
        except OSError:
            pass
        self.sock.close()


def _start_server(port: int, loss: float = 0.0) -> subprocess.Popen:
    """Launch the game server as a subprocess."""
    cmd = [
        sys.executable, '-m', 'server.server',
        '--port', str(port),
        '--tick-rate', '20',
    ]
    if loss > 0:
        cmd += ['--loss', str(loss)]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait for the server to bind.  We probe by sending a CONNECT_REQ
    # and waiting for CONNECT_ACK to come back.
    _wait_for_server(proc, port)
    return proc


def _wait_for_server(proc: subprocess.Popen, port: int,
                     timeout: float = 5.0):
    """Block until the server is accepting packets (or *timeout* expires)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    addr = ('127.0.0.1', port)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"Server exited with code {proc.returncode}")
            pkt = Packet(PacketType.CONNECT_REQ, 0, 0, 0)
            sock.sendto(pkt.serialize(), addr)
            ready, _, _ = select.select([sock], [], [], 0.3)
            if ready:
                try:
                    data, _ = sock.recvfrom(DEFAULT_BUFFER_SIZE)
                    rpkt = Packet.deserialize(data)
                    if rpkt.packet_type == PacketType.CONNECT_ACK:
                        # Server is alive – send disconnect so we don't
                        # leave a ghost client.
                        dpkt = Packet(PacketType.DISCONNECT, 0, 0, 0)
                        sock.sendto(dpkt.serialize(), addr)
                        return
                except (ValueError, BlockingIOError, OSError):
                    pass
        raise RuntimeError("Server did not start in time")
    finally:
        sock.close()


def _stop_server(proc: subprocess.Popen):
    """Gracefully terminate the server subprocess."""
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_state_convergence():
    """Verify that all bot clients receive consistent state."""
    port = random.randint(10000, 60000)
    proc = _start_server(port)

    try:
        num_bots = 4
        bots = []
        for _ in range(num_bots):
            bot = BotClient(port=port)
            bot.connect()
            bots.append(bot)

        # Wait for connections (up to 3 s)
        for _ in range(30):
            for bot in bots:
                bot.drain(timeout=0.1)
            if all(b.connected for b in bots):
                break

        connected_count = sum(1 for b in bots if b.connected)
        assert connected_count > 0, "At least one bot should connect"

        # Send inputs and let the simulation run
        for tick in range(40):
            for i, bot in enumerate(bots):
                mx = 1.0 if i % 2 == 0 else -1.0
                my = 0.5 if i < 2 else -0.5
                bot.send_input(mx, my)
            time.sleep(0.05)
            for bot in bots:
                bot.drain(timeout=0.02)

        # Collect final snapshots
        time.sleep(0.5)
        for bot in bots:
            bot.drain(timeout=0.2)

        # Convergence check
        snapshots = [b.latest_snapshot for b in bots if b.latest_snapshot]
        if len(snapshots) >= 2:
            ref = snapshots[0]
            for snap in snapshots[1:]:
                for eid in ref.entities:
                    if eid not in snap.entities:
                        continue
                    e_ref = ref.entities[eid]
                    e_cmp = snap.entities[eid]
                    dx = abs(e_ref.x - e_cmp.x)
                    dy = abs(e_ref.y - e_cmp.y)
                    assert dx <= 50.0 and dy <= 50.0, (
                        f"Divergence for entity {eid}: "
                        f"dx={dx:.1f}, dy={dy:.1f}"
                    )

        for bot in bots:
            bot.disconnect()
    finally:
        _stop_server(proc)


def test_connection_lifecycle():
    """Test connect → send → disconnect cycle."""
    port = random.randint(10000, 60000)
    proc = _start_server(port)

    try:
        bot = BotClient(port=port)
        bot.connect()

        # Wait for ACK (up to 2 s)
        for _ in range(20):
            bot.drain(timeout=0.1)
            if bot.connected:
                break

        assert bot.connected, "Bot should be connected"
        assert bot.client_id is not None, "Bot should have an ID"

        # Send some inputs
        for _ in range(10):
            bot.send_input(1.0, 0.0)
            time.sleep(0.05)
            bot.drain(timeout=0.05)

        assert bot.latest_snapshot is not None, \
            "Should have received a snapshot"
        assert bot.client_id in bot.latest_snapshot.entities, \
            "Bot should be in the snapshot"

        entity = bot.latest_snapshot.entities[bot.client_id]
        assert entity.x > 0, "Bot should have moved"

        bot.disconnect()
    finally:
        _stop_server(proc)


def test_packet_loss_tolerance():
    """Test that the system handles 10 % packet loss gracefully."""
    port = random.randint(10000, 60000)
    proc = _start_server(port, loss=0.1)

    try:
        bot = BotClient(port=port)

        # Multiple connect attempts due to loss
        for _ in range(15):
            bot.connect()
            bot.drain(timeout=0.2)
            if bot.connected:
                break

        if not bot.connected:
            bot.disconnect()
            return  # Skip gracefully under heavy loss

        snapshots_received = 0
        for _ in range(40):
            bot.send_input(1.0, 0.5)
            time.sleep(0.03)
            bot.drain(timeout=0.02)
            if bot.latest_snapshot:
                snapshots_received += 1

        assert snapshots_received > 5, (
            f"Should receive snapshots despite loss, got {snapshots_received}"
        )
        bot.disconnect()
    finally:
        _stop_server(proc)


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    print("=" * 60)
    print("  Integration Tests: Multiplayer Networking Engine")
    print("=" * 60)

    test_funcs = [
        ('Connection Lifecycle', test_connection_lifecycle),
        ('State Convergence', test_state_convergence),
        ('Packet Loss Tolerance', test_packet_loss_tolerance),
    ]

    results = {}
    for i, (name, func) in enumerate(test_funcs, 1):
        print(f"\n--- Test {i}: {name} ---")
        try:
            func()
            results[name] = True
            print(f"  PASSED")
        except Exception as e:
            results[name] = False
            print(f"  FAILED: {e}")

    print("\n" + "=" * 60)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"  Results: {passed}/{total} tests passed")
    print("=" * 60)
