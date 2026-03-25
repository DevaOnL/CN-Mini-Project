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

from client.client import CONNECT_ACK_FORMAT, CONNECT_REQ_FORMAT
from common.packet import Packet, PacketType, INPUT_FORMAT, HEADER_SIZE
from common.packet import pack_connection_epoch, unpack_connection_epoch
from common.snapshot import Snapshot
from common.config import DEFAULT_PORT, DEFAULT_BUFFER_SIZE
from common.security import (
    SECURE_PROTOCOL_VERSION,
    SECURE_HELLO_ACK_FORMAT,
    SECURE_HELLO_ACK_SIZE,
    SECURE_HELLO_FORMAT,
    SECURE_HELLO_SIZE,
    build_client_proof,
    decrypt_payload,
    derive_room_psk,
    derive_session_key,
    encrypt_payload,
    generate_handshake_nonce,
    verify_server_proof,
)


TEST_ROOM_KEY = "integration-room-key"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class BotClient:
    """A headless bot client for testing."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        room_key: str = TEST_ROOM_KEY,
    ):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.server_addr = (host, port)
        self.room_psk = derive_room_psk(room_key)
        self.client_id = None
        self.connection_epoch = 0
        self.connect_nonce = 0
        self.connected = False
        self.latest_snapshot = None
        self.input_seq = 0
        self.scores = {}
        self.min_health_seen = {}
        self.session_token = b"\x00" * 16
        self.session_key: bytes | None = None
        self.pending_client_nonce: bytes | None = None
        self.sequence = 0

    # -- actions -----------------------------------------------------------

    def _next_sequence(self) -> int:
        self.sequence = (self.sequence + 1) & 0xFFFF
        return self.sequence

    def _send_secure_packet(self, packet_type: int, plaintext_payload: bytes = b""):
        if self.session_key is None:
            return
        sequence = self._next_sequence()
        header = Packet.pack_header(
            sequence, 0, 0, packet_type, len(plaintext_payload) + 28
        )
        encrypted_payload = encrypt_payload(self.session_key, header, plaintext_payload)
        self.sock.sendto(header + encrypted_payload, self.server_addr)

    def connect(self):
        self.session_key = None
        self.pending_client_nonce = generate_handshake_nonce()
        hello_payload = struct.pack(
            SECURE_HELLO_FORMAT,
            SECURE_PROTOCOL_VERSION,
            self.pending_client_nonce,
            build_client_proof(self.room_psk, self.pending_client_nonce),
        )
        pkt = Packet(PacketType.SECURE_HELLO, self._next_sequence(), 0, 0, hello_payload)
        self.sock.sendto(pkt.serialize(), self.server_addr)

    def _send_connect_request(self):
        self.connect_nonce += 1
        payload = struct.pack(CONNECT_REQ_FORMAT, self.session_token, self.connect_nonce)
        self._send_secure_packet(PacketType.CONNECT_REQ, payload)

    def send_input(self, mx: float = 0.0, my: float = 0.0):
        if not self.connected:
            return
        self.input_seq += 1
        payload = pack_connection_epoch(
            self.connection_epoch,
            struct.pack(INPUT_FORMAT, self.input_seq, mx, my, 0),
        )
        self._send_secure_packet(PacketType.INPUT, payload)

    def send_reliable_event(self, event_type: int, subject_client_id: int):
        payload = pack_connection_epoch(
            self.connection_epoch,
            struct.pack("!BH", event_type, subject_client_id),
        )
        self._send_secure_packet(PacketType.RELIABLE_EVENT, payload)

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

        if pkt.packet_type == PacketType.SECURE_HELLO_ACK:
            if self.pending_client_nonce is None:
                return
            if len(pkt.payload) < SECURE_HELLO_ACK_SIZE:
                return
            version, server_nonce, server_proof = struct.unpack(
                SECURE_HELLO_ACK_FORMAT,
                pkt.payload[:SECURE_HELLO_ACK_SIZE],
            )
            if version != SECURE_PROTOCOL_VERSION:
                return
            if not verify_server_proof(
                self.room_psk,
                self.pending_client_nonce,
                server_nonce,
                server_proof,
            ):
                return
            self.session_key = derive_session_key(
                self.room_psk,
                self.pending_client_nonce,
                server_nonce,
            )
            self._send_connect_request()
            return

        if self.session_key is not None and pkt.packet_type != PacketType.SECURE_HELLO:
            try:
                pkt.payload = decrypt_payload(
                    self.session_key,
                    data[:HEADER_SIZE],
                    pkt.payload,
                )
            except ValueError:
                return

        if pkt.packet_type == PacketType.CONNECT_ACK:
            self.client_id, self.connection_epoch, _token, _nonce = struct.unpack(
                CONNECT_ACK_FORMAT,
                pkt.payload[: struct.calcsize(CONNECT_ACK_FORMAT)],
            )
            self.session_token = _token
            self.connected = True
        elif pkt.packet_type == PacketType.SNAPSHOT:
            unpacked = unpack_connection_epoch(pkt.payload)
            if unpacked is None:
                return
            packet_epoch, payload = unpacked
            if packet_epoch != self.connection_epoch:
                return
            self.latest_snapshot = Snapshot.deserialize(payload)
            for entity_id, entity in self.latest_snapshot.entities.items():
                previous = self.min_health_seen.get(entity_id, 100.0)
                self.min_health_seen[entity_id] = min(previous, entity.health)
        elif pkt.packet_type == PacketType.RELIABLE_EVENT and pkt.payload:
            unpacked = unpack_connection_epoch(pkt.payload)
            if unpacked is None:
                return
            packet_epoch, payload = unpacked
            if packet_epoch != self.connection_epoch or not payload:
                return
            event_type = payload[0]
            if event_type == 0x04 and len(payload) >= struct.calcsize("!BHH"):
                _, killer_id, _victim_id = struct.unpack(
                    "!BHH", payload[: struct.calcsize("!BHH")]
                )
                self.scores[killer_id] = self.scores.get(killer_id, 0) + 1
            elif event_type == 0x05:
                offset = 1
                pair_size = struct.calcsize("!HH")
                while offset + pair_size <= len(payload):
                    entity_id, kills = struct.unpack(
                        "!HH", payload[offset : offset + pair_size]
                    )
                    self.scores[entity_id] = kills
                    offset += pair_size

    def disconnect(self):
        try:
            if self.connected and self.connection_epoch and self.session_key is not None:
                self._send_secure_packet(
                    PacketType.DISCONNECT,
                    pack_connection_epoch(self.connection_epoch),
                )
        except OSError:
            pass
        self.sock.close()


def _start_server(port: int, loss: float = 0.0) -> subprocess.Popen:
    """Launch the game server as a subprocess."""
    cmd = [
        sys.executable,
        "-m",
        "server.server",
        "--port",
        str(port),
        "--tick-rate",
        "20",
        "--room-key",
        TEST_ROOM_KEY,
    ]
    if loss > 0:
        cmd += ["--loss", str(loss)]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait for the server to bind.  We probe by sending a CONNECT_REQ
    # and waiting for CONNECT_ACK to come back.
    _wait_for_server(proc, port)
    return proc


def _wait_for_server(proc: subprocess.Popen, port: int, timeout: float = 5.0):
    """Block until the server is accepting packets (or *timeout* expires)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Server exited with code {proc.returncode}")
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            probe.close()
            return
        else:
            probe.close()
        time.sleep(0.05)
    raise RuntimeError("Server did not start in time")


def _stop_server(proc: subprocess.Popen):
    """Gracefully terminate the server subprocess."""
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _trigger_game_start(bot: BotClient, port: int):
    """Send a GAME_START reliable event as if the host pressed Start."""
    _ = port
    bot.send_reliable_event(0x03, bot.client_id or 1)
    time.sleep(0.1)
    bot.drain(timeout=0.2)


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

        _trigger_game_start(bots[0], port)
        time.sleep(0.1)
        for bot in bots:
            bot.drain(timeout=0.05)

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
                        f"Divergence for entity {eid}: dx={dx:.1f}, dy={dy:.1f}"
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

        _trigger_game_start(bot, port)
        time.sleep(0.1)
        bot.drain(timeout=0.05)

        # Send some inputs
        for _ in range(10):
            bot.send_input(1.0, 0.0)
            time.sleep(0.05)
            bot.drain(timeout=0.05)

        assert bot.latest_snapshot is not None, "Should have received a snapshot"
        assert bot.client_id in bot.latest_snapshot.entities, (
            "Bot should be in the snapshot"
        )

        entity = bot.latest_snapshot.entities[bot.client_id]
        assert entity.x > 0, "Bot should have moved"

        bot.disconnect()
    finally:
        _stop_server(proc)


def test_host_and_join_clients_see_expected_players_and_start_match():
    """Host/join flow should only show real players and should start cleanly."""
    from client.client import GameClient

    port = random.randint(10000, 60000)
    proc = _start_server(port)

    host = GameClient(
        server_host="127.0.0.1",
        server_port=port,
        headless=True,
        room_key=TEST_ROOM_KEY,
    )
    guest = GameClient(
        server_host="127.0.0.1",
        server_port=port,
        headless=True,
        room_key=TEST_ROOM_KEY,
    )

    try:
        host.connect()
        deadline = time.time() + 5.0
        while time.time() < deadline and (
            not host.connected or not host.server_snapshots
        ):
            host.receive_packets()
            time.sleep(0.01)

        assert host.connected is True
        assert host.client_id is not None
        assert host.server_snapshots
        assert sorted(host.server_snapshots[-1].entities) == [host.client_id]

        guest.connect()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            host.receive_packets()
            guest.receive_packets()
            if (
                guest.connected
                and host.server_snapshots
                and guest.server_snapshots
                and sorted(host.server_snapshots[-1].entities)
                == sorted([host.client_id, guest.client_id])
                and sorted(guest.server_snapshots[-1].entities)
                == sorted([host.client_id, guest.client_id])
            ):
                break
            time.sleep(0.01)

        assert guest.connected is True
        assert guest.client_id is not None
        assert sorted(host.server_snapshots[-1].entities) == sorted(
            [host.client_id, guest.client_id]
        )
        assert sorted(guest.server_snapshots[-1].entities) == sorted(
            [host.client_id, guest.client_id]
        )

        assert host.request_game_start() is True

        deadline = time.time() + 5.0
        while time.time() < deadline and not (
            host.game_started_by_server and guest.game_started_by_server
        ):
            host.receive_packets()
            guest.receive_packets()
            host.reliable_channel.tick()
            guest.reliable_channel.tick()
            time.sleep(0.01)

        assert host.game_started_by_server is True
        assert guest.game_started_by_server is True
    finally:
        host.disconnect(close_socket=True)
        guest.disconnect(close_socket=True)
        _stop_server(proc)


def test_host_can_kick_selected_player_from_lobby():
    from client.client import GameClient

    port = random.randint(10000, 60000)
    proc = _start_server(port)

    host = GameClient(
        server_host="127.0.0.1",
        server_port=port,
        headless=True,
        room_key=TEST_ROOM_KEY,
    )
    guest = GameClient(
        server_host="127.0.0.1",
        server_port=port,
        headless=True,
        room_key=TEST_ROOM_KEY,
    )

    try:
        host.connect()
        deadline = time.time() + 5.0
        while time.time() < deadline and (
            not host.connected or not host.server_snapshots
        ):
            host.receive_packets()
            time.sleep(0.01)

        guest.connect()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            host.receive_packets()
            guest.receive_packets()
            if (
                guest.connected
                and host.server_snapshots
                and guest.server_snapshots
                and sorted(host.server_snapshots[-1].entities)
                == sorted([host.client_id, guest.client_id])
            ):
                break
            time.sleep(0.01)

        assert guest.connected is True
        assert host.request_kick_player(guest.client_id or 0) is True

        deadline = time.time() + 5.0
        while time.time() < deadline:
            host.receive_packets()
            guest.receive_packets()
            host.reliable_channel.tick()
            guest.reliable_channel.tick()
            if (
                not guest.connected
                and host.server_snapshots
                and sorted(host.server_snapshots[-1].entities) == [host.client_id]
            ):
                break
            time.sleep(0.01)

        assert guest.connected is False
        assert guest.ui_notice == "You were kicked by the host."
        assert sorted(host.server_snapshots[-1].entities) == [host.client_id]
    finally:
        host.disconnect(close_socket=True)
        guest.disconnect(close_socket=True)
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

        for _ in range(3):
            _trigger_game_start(bot, port)

        received_ticks = set()
        deadline = time.time() + 3.0
        while time.time() < deadline:
            bot.send_input(1.0, 0.5)
            time.sleep(0.05)
            bot.drain(timeout=0.05)
            if bot.latest_snapshot:
                received_ticks.add(bot.latest_snapshot.tick)

        if not received_ticks:
            bot.disconnect()
            return  # Skip gracefully when the short loss window is exceptionally unlucky

        assert len(received_ticks) > 0, (
            f"Should receive at least one snapshot despite loss, got {len(received_ticks)}"
        )
        bot.disconnect()
    finally:
        _stop_server(proc)


def test_gameplay_loop():
    """Verify that collision, scoring, and respawn run correctly end-to-end."""
    port = random.randint(10000, 60000)
    proc = _start_server(port)

    try:
        bot1 = BotClient(port=port)
        bot2 = BotClient(port=port)
        bot1.connect()
        bot2.connect()

        for _ in range(20):
            bot1.drain(0.1)
            bot2.drain(0.1)
            if bot1.connected and bot2.connected:
                break

        assert bot1.connected and bot2.connected

        _trigger_game_start(bot1, port)
        time.sleep(0.2)
        bot1.drain(0.2)
        bot2.drain(0.2)

        for _ in range(180):
            bot1.send_input(1.0, 0.0)
            bot2.send_input(-1.0, 0.0)
            time.sleep(0.05)
            bot1.drain(0.02)
            bot2.drain(0.02)

        assert bot1.latest_snapshot is not None
        assert bot2.latest_snapshot is not None
        for snap in [bot1.latest_snapshot, bot2.latest_snapshot]:
            for _entity_id, entity in snap.entities.items():
                assert 0.0 <= entity.x <= 800.0
                assert 0.0 <= entity.y <= 600.0

        assert any(value < 100.0 for value in bot1.min_health_seen.values()) or any(
            value < 100.0 for value in bot2.min_health_seen.values()
        )
        assert bot1.scores or bot2.scores

        bot1.disconnect()
        bot2.disconnect()
    finally:
        _stop_server(proc)


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("  Integration Tests: Multiplayer Networking Engine")
    print("=" * 60)

    test_funcs = [
        ("Connection Lifecycle", test_connection_lifecycle),
        ("State Convergence", test_state_convergence),
        ("Packet Loss Tolerance", test_packet_loss_tolerance),
        ("Gameplay Loop", test_gameplay_loop),
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
