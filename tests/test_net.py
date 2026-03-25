"""Networking helper tests."""

import socket
import struct
from typing import Any, cast

import pytest

from client.client import (
    CONNECT_ACK_FORMAT,
    ConnState,
    GameClient,
    ReliableChannel as ClientReliableChannel,
    RELIABLE_EVENT_FORMAT,
    RELIABLE_EVENT_KICKED,
    RELIABLE_EVENT_MATCH_RESET,
    RELIABLE_EVENT_GAME_START,
    RELIABLE_EVENT_SCORE_UPDATE,
    RELIABLE_SCORE_EVENT_FORMAT,
    SNAPSHOT_TRAILER_FORMAT,
)
from common.net import AckTracker, create_server_socket
from common.packet import (
    Packet,
    PacketType,
    DISCONNECT_REASON_FORMAT,
    DISCONNECT_REASON_NONE,
    DISCONNECT_REASON_KICKED,
    HANDSHAKE_DISCONNECT_FORMAT,
    pack_connection_epoch,
)
from common.snapshot import EntityState, Snapshot
from server.server import ReliableChannel as ServerReliableChannel


def test_ack_tracker_detects_duplicate_packet():
    tracker = AckTracker()
    assert tracker.is_duplicate(5) is False
    tracker.on_packet_received(5)
    assert tracker.is_duplicate(5) is True
    tracker.on_packet_received(4)
    assert tracker.is_duplicate(4) is True


def test_ack_tracker_accepts_newer_packet_after_sequence_zero():
    tracker = AckTracker()
    tracker.on_packet_received(0)

    assert tracker.is_duplicate(1) is False


def test_send_input_supports_deque_history_without_slice_errors():
    client = GameClient(headless=True)
    sent_packets = []

    try:
        client.conn_state = ConnState.CONNECTED
        client.game_started_by_server = True
        client.local_state = {
            "x": 100.0,
            "y": 100.0,
            "vx": 0.0,
            "vy": 0.0,
            "health": 100.0,
        }
        client._send_packet = lambda packet_type, payload=b"": sent_packets.append(
            (packet_type, payload)
        )

        client.send_input({"move_x": 1.0, "move_y": 0.5, "actions": 0})

        assert len(sent_packets) == 1
        packet_type, payload = sent_packets[0]
        assert packet_type == PacketType.INPUT
        assert payload[0] == 1
        assert client.pending_inputs[-1]["sequence"] == 1
    finally:
        client.disconnect(close_socket=True)


def test_duplicate_score_update_packet_is_ignored():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.CONNECTED
        client.connection_epoch = 1
        payload = pack_connection_epoch(
            client.connection_epoch,
            struct.pack(RELIABLE_SCORE_EVENT_FORMAT, RELIABLE_EVENT_SCORE_UPDATE, 2, 1),
        )
        packet = Packet(PacketType.RELIABLE_EVENT, sequence=7, payload=payload)
        data = packet.serialize()

        client._handle_packet(data)
        client._handle_packet(data)

        assert client.scores.get(2) == 1
    finally:
        client.disconnect(close_socket=True)


def test_score_update_before_match_reset_sequence_is_ignored():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.CONNECTED
        client.connection_epoch = 1
        reset_packet = Packet(
            PacketType.RELIABLE_EVENT,
            sequence=20,
            payload=pack_connection_epoch(
                client.connection_epoch,
                struct.pack("!B", RELIABLE_EVENT_MATCH_RESET),
            ),
        )
        old_score_packet = Packet(
            PacketType.RELIABLE_EVENT,
            sequence=10,
            payload=pack_connection_epoch(
                client.connection_epoch,
                struct.pack(
                    RELIABLE_SCORE_EVENT_FORMAT,
                    RELIABLE_EVENT_SCORE_UPDATE,
                    2,
                    1,
                ),
            ),
        )

        client._handle_packet(reset_packet.serialize())
        client._handle_packet(old_score_packet.serialize())

        assert client.scores == {}
    finally:
        client.disconnect(close_socket=True)


def test_older_reliable_event_after_newer_one_is_ignored():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.CONNECTED
        client.connection_epoch = 1

        newer_packet = Packet(
            PacketType.RELIABLE_EVENT,
            sequence=20,
            payload=pack_connection_epoch(
                client.connection_epoch,
                struct.pack(
                    RELIABLE_SCORE_EVENT_FORMAT,
                    RELIABLE_EVENT_SCORE_UPDATE,
                    2,
                    1,
                ),
            ),
        )
        older_packet = Packet(
            PacketType.RELIABLE_EVENT,
            sequence=10,
            payload=pack_connection_epoch(
                client.connection_epoch,
                struct.pack(
                    RELIABLE_SCORE_EVENT_FORMAT,
                    RELIABLE_EVENT_SCORE_UPDATE,
                    3,
                    1,
                ),
            ),
        )

        client._handle_packet(newer_packet.serialize())
        client._handle_packet(older_packet.serialize())

        assert client.scores == {2: 1, 1: 0}
    finally:
        client.disconnect(close_socket=True)


def test_older_match_reset_after_newer_event_is_ignored():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.CONNECTED
        client.connection_epoch = 1

        newer_packet = Packet(
            PacketType.RELIABLE_EVENT,
            sequence=20,
            payload=pack_connection_epoch(
                client.connection_epoch,
                struct.pack(RELIABLE_EVENT_FORMAT, RELIABLE_EVENT_GAME_START, 0),
            ),
        )
        older_reset = Packet(
            PacketType.RELIABLE_EVENT,
            sequence=10,
            payload=pack_connection_epoch(
                client.connection_epoch,
                struct.pack("!B", RELIABLE_EVENT_MATCH_RESET),
            ),
        )

        client._handle_packet(newer_packet.serialize())
        client._handle_packet(older_reset.serialize())

        assert client.game_started_by_server is True
    finally:
        client.disconnect(close_socket=True)


def test_client_ignores_packets_from_unexpected_sender():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.CONNECTING
        client.connect_nonce = 101
        payload = struct.pack(
            CONNECT_ACK_FORMAT,
            7,
            55,
            b"spoofed-token".ljust(16, b"\x00"),
            client.connect_nonce,
        )
        packet = Packet(PacketType.CONNECT_ACK, sequence=1, payload=payload)

        client._handle_packet(packet.serialize(), ("127.0.0.2", client.server_port))

        assert client.conn_state == ConnState.CONNECTING
        assert client.client_id is None

        client._handle_packet(packet.serialize(), client.server_addr)

        assert client.conn_state == ConnState.CONNECTED
        assert client.client_id == 7
        assert client.connection_epoch == 55
    finally:
        client.disconnect(close_socket=True)


def test_connect_does_not_replace_pending_secure_nonce_within_retry_window():
    client = GameClient(headless=True, room_key="shared-key")
    try:
        assert client.connect() is True
        first_nonce = client._pending_secure_client_nonce

        assert first_nonce is not None
        assert client.connect() is True
        assert client._pending_secure_client_nonce == first_nonce
    finally:
        client.disconnect(close_socket=True)


def test_snapshot_without_matching_input_uses_authoritative_dash_state():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.CONNECTED
        client.client_id = 1
        client.connection_epoch = 1
        client.local_state = {
            "x": 110.0,
            "y": 120.0,
            "vx": 0.0,
            "vy": 0.0,
            "health": 100.0,
            "dash_cooldown": 0.8,
            "dash_timer": 0.1,
        }
        client.visual_state = client.local_state.copy()
        client.last_acked_dash_cooldown = 0.8
        client.last_acked_dash_timer = 0.1

        snapshot = Snapshot(
            tick=5,
            entities={
                1: EntityState(1, 100.0, 100.0, 0.0, 0.0, 100.0, 0, 0, 0, 0.25, 0.05)
            },
        )
        payload = pack_connection_epoch(
            client.connection_epoch,
            snapshot.serialize()
            + struct.pack(SNAPSHOT_TRAILER_FORMAT, 999, 0.0, 0.0, 1),
        )
        packet = Packet(PacketType.SNAPSHOT, sequence=2, payload=payload)

        client._handle_packet(packet.serialize())

        assert client.local_state["dash_cooldown"] == pytest.approx(0.25)
        assert client.local_state["dash_timer"] == pytest.approx(0.05)
        assert client.last_acked_dash_cooldown == pytest.approx(0.25)
        assert client.last_acked_dash_timer == pytest.approx(0.05)
    finally:
        client.disconnect(close_socket=True)


def _make_reliable_packet(sequence: int) -> bytes:
    payload = struct.pack("!BH", RELIABLE_EVENT_GAME_START, 1)
    return Packet(
        PacketType.RELIABLE_EVENT, sequence=sequence, payload=payload
    ).serialize()


def test_reliable_channel_queues_packets_when_window_full():
    for channel_cls in (ClientReliableChannel, ServerReliableChannel):
        sent_sequences = []

        def send_fn(data, _addr):
            sent_sequences.append(Packet.deserialize(data).sequence)

        channel = channel_cls(send_fn)
        addr = ("127.0.0.1", 9000)

        for sequence in range(1, channel.WINDOW + 2):
            assert channel.send(_make_reliable_packet(sequence), addr) == sequence

        assert sent_sequences == [1, 2, 3, 4]

        channel.ack(1)

        assert sent_sequences == [1, 2, 3, 4, 5]


def test_request_game_start_succeeds_when_reliable_window_is_full():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.CONNECTED
        client.client_id = 1
        client.connection_epoch = 1
        payload = struct.pack("!BH", RELIABLE_EVENT_GAME_START, 1)

        for _ in range(client.reliable_channel.WINDOW):
            _, data = client._build_packet(
                PacketType.RELIABLE_EVENT,
                payload,
                track_send=False,
            )
            client.reliable_channel.send(
                data,
                client.server_addr,
            )

        assert client.request_game_start() is True
        assert len(client.reliable_channel._queued) == 1
        queued_seq = client.ack_tracker.local_sequence
        assert queued_seq not in client.ack_tracker.sent_packets

        client.reliable_channel.ack(1)

        assert queued_seq in client.ack_tracker.sent_packets
    finally:
        client.disconnect(close_socket=True)


def test_server_socket_rejects_duplicate_bind():
    first = create_server_socket("127.0.0.1", 0)
    port = first.getsockname()[1]
    try:
        try:
            second = create_server_socket("127.0.0.1", port)
        except OSError:
            second = None
        else:
            second.close()
            raise AssertionError("second UDP server bind unexpectedly succeeded")
    finally:
        first.close()


def test_client_disconnect_packet_sets_kicked_notice():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.CONNECTED
        client.client_id = 2
        client.connection_epoch = 1
        packet = Packet(
            PacketType.DISCONNECT,
            sequence=9,
            payload=pack_connection_epoch(
                client.connection_epoch,
                struct.pack(DISCONNECT_REASON_FORMAT, DISCONNECT_REASON_KICKED),
            ),
        )

        client._handle_packet(packet.serialize(), client.server_addr)

        assert client.conn_state == ConnState.DISCONNECTED
        assert client.client_id is None
        assert client.ui_notice == "You were kicked by the host."
        assert client.last_connection_error == "You were kicked by the host."
    finally:
        client.disconnect(close_socket=True)


def test_disconnected_client_ignores_late_snapshot_packets():
    client = GameClient(headless=True)
    try:
        snapshot = Snapshot(
            tick=5,
            entities={1: EntityState(1, 100.0, 100.0, 0.0, 0.0, 100.0, 0)},
        )
        packet = Packet(PacketType.SNAPSHOT, sequence=3, payload=snapshot.serialize())

        client._handle_packet(packet.serialize(), client.server_addr)

        assert client.server_snapshots == []
        assert client.scores == {}
    finally:
        client.disconnect(close_socket=True)


def test_connecting_client_ignores_reliable_events_until_connect_ack():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.CONNECTING

        reliable_packet = Packet(
            PacketType.RELIABLE_EVENT,
            sequence=4,
            payload=struct.pack(RELIABLE_EVENT_FORMAT, RELIABLE_EVENT_KICKED, 1),
        )

        client._handle_packet(reliable_packet.serialize(), client.server_addr)

        assert client.pending_server_disconnect_notice is None
    finally:
        client.disconnect(close_socket=True)


def test_reconnecting_client_ignores_epoch_wrapped_stale_disconnects():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.RECONNECTING
        client.connection_epoch = 77
        packet = Packet(
            PacketType.DISCONNECT,
            sequence=9,
            payload=pack_connection_epoch(
                77,
                struct.pack(DISCONNECT_REASON_FORMAT, DISCONNECT_REASON_KICKED),
            ),
        )

        client._handle_packet(packet.serialize(), client.server_addr)

        assert client.conn_state == ConnState.RECONNECTING
        assert client.ui_notice is None
    finally:
        client.disconnect(close_socket=True)


def test_kicked_client_ignores_following_stale_packets():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.CONNECTED
        client.client_id = 2
        client.connection_epoch = 1

        kicked_packet = Packet(
            PacketType.RELIABLE_EVENT,
            sequence=4,
            payload=pack_connection_epoch(
                client.connection_epoch,
                struct.pack(RELIABLE_EVENT_FORMAT, RELIABLE_EVENT_KICKED, 1),
            ),
        )
        snapshot = Snapshot(
            tick=9,
            entities={2: EntityState(2, 100.0, 100.0, 0.0, 0.0, 100.0, 0)},
        )
        snapshot_packet = Packet(
            PacketType.SNAPSHOT,
            sequence=5,
            payload=pack_connection_epoch(
                client.connection_epoch, snapshot.serialize()
            ),
        )

        client._handle_packet(kicked_packet.serialize(), client.server_addr)
        client._handle_packet(snapshot_packet.serialize(), client.server_addr)

        assert client.pending_server_disconnect_notice == "You were kicked by the host."
        assert client.session_token is None
        assert client.server_snapshots == []
    finally:
        client.disconnect(close_socket=True)


def test_kicked_disconnect_clears_session_token():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.CONNECTED
        client.client_id = 2
        client.connection_epoch = 1
        client.session_token = "blocked-token"
        packet = Packet(
            PacketType.DISCONNECT,
            sequence=9,
            payload=pack_connection_epoch(
                client.connection_epoch,
                struct.pack(DISCONNECT_REASON_FORMAT, DISCONNECT_REASON_KICKED),
            ),
        )

        client._handle_packet(packet.serialize(), client.server_addr)

        assert client.session_token is None
    finally:
        client.disconnect(close_socket=True)


def test_connected_client_ignores_old_server_sequences_after_connect_ack():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.CONNECTING
        client.connect_nonce = 303
        ack_packet = Packet(
            PacketType.CONNECT_ACK,
            sequence=10,
            payload=struct.pack(
                CONNECT_ACK_FORMAT,
                3,
                77,
                b"token".ljust(16, b"\x00"),
                client.connect_nonce,
            ),
        )
        stale_snapshot = Packet(
            PacketType.SNAPSHOT,
            sequence=5,
            payload=pack_connection_epoch(
                77,
                Snapshot(
                    tick=1,
                    entities={3: EntityState(3, 100.0, 100.0, 0.0, 0.0, 100.0, 0)},
                ).serialize(),
            ),
        )
        fresh_snapshot = Packet(
            PacketType.SNAPSHOT,
            sequence=11,
            payload=pack_connection_epoch(
                77,
                Snapshot(
                    tick=2,
                    entities={3: EntityState(3, 120.0, 100.0, 0.0, 0.0, 100.0, 0)},
                ).serialize(),
            ),
        )

        client._handle_packet(ack_packet.serialize(), client.server_addr)
        client._handle_packet(stale_snapshot.serialize(), client.server_addr)
        client._handle_packet(fresh_snapshot.serialize(), client.server_addr)

        assert client.connected is True
        assert len(client.server_snapshots) == 1
        assert client.server_snapshots[0].tick == 2
    finally:
        client.disconnect(close_socket=True)


def test_connect_ack_resets_stale_gameplay_state():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.RECONNECTING
        client.connect_nonce = 404
        client.game_started_by_server = True
        client.match_winner_id = 2
        client.scores = {2: 3}
        client.server_snapshots = [
            Snapshot(
                tick=7,
                entities={2: EntityState(2, 100.0, 100.0, 0.0, 0.0, 100.0, 0)},
            )
        ]

        ack_packet = Packet(
            PacketType.CONNECT_ACK,
            sequence=12,
            payload=struct.pack(
                CONNECT_ACK_FORMAT,
                2,
                88,
                b"reconnect-token".ljust(16, b"\x00"),
                client.connect_nonce,
            ),
        )

        client._handle_packet(ack_packet.serialize(), client.server_addr)

        assert client.connected is True
        assert client.game_started_by_server is False
        assert client.match_winner_id is None
        assert client.scores == {}
        assert client.server_snapshots == []
    finally:
        client.disconnect(close_socket=True)


def test_disconnect_bypasses_network_simulator_queue():
    class DummySocket:
        def __init__(self):
            self.sent = []
            self.closed = False

        def sendto(self, data, addr):
            self.sent.append((data, addr))

        def close(self):
            self.closed = True

    class DummyNetSim:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr):
            self.sent.append((data, addr))

    client = GameClient(headless=True)
    try:
        dummy_sock = DummySocket()
        dummy_net_sim = DummyNetSim()
        client.sock = cast(Any, dummy_sock)
        client.net_sim = cast(Any, dummy_net_sim)
        client.conn_state = ConnState.CONNECTED
        client.client_id = 4
        client.connection_epoch = 99

        client.disconnect(close_socket=True)

        assert dummy_sock.sent
        assert dummy_net_sim.sent == []
        assert dummy_sock.closed is True
    finally:
        try:
            client.disconnect(close_socket=True)
        except Exception:
            pass


def test_handshake_disconnect_with_token_retries_as_fresh_join():
    client = GameClient(headless=True)
    try:
        retry_calls = []

        def fake_connect():
            retry_calls.append(True)
            client.conn_state = ConnState.CONNECTING
            return True

        client.connect = fake_connect  # type: ignore[method-assign]
        client.conn_state = ConnState.CONNECTING
        client.session_token = "stale-token"
        client.connect_nonce = 17

        packet = Packet(
            PacketType.DISCONNECT,
            payload=struct.pack(
                HANDSHAKE_DISCONNECT_FORMAT,
                b"\x00" * 16,
                client.connect_nonce,
                DISCONNECT_REASON_NONE,
            ),
        )

        client._handle_packet(packet.serialize(), client.server_addr)

        assert client.session_token is None
        assert retry_calls == [True]
    finally:
        client.disconnect(close_socket=True)


def test_manual_disconnect_clears_session_token():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.CONNECTED
        client.client_id = 5
        client.connection_epoch = 12
        client.session_token = "persist-me"
        old_sock = client.sock

        client.disconnect(close_socket=False)

        assert client.session_token is None
        assert client.sock is not old_sock
    finally:
        try:
            client.disconnect(close_socket=True)
        except Exception:
            pass


def test_fixed_tick_advance_sends_input_for_each_elapsed_tick():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.CONNECTED
        client.game_started_by_server = True
        predict_calls = []
        send_calls = []
        client.predict_local = lambda inp: predict_calls.append(inp.copy())  # type: ignore[method-assign]
        client.send_input = lambda inp: send_calls.append(inp.copy())  # type: ignore[method-assign]

        client._advance_gameplay_ticks(client.dt * 3.4, {"move_x": 1.0}, True)

        assert len(predict_calls) == 3
        assert len(send_calls) == 3
        assert 0.0 <= client._predict_accumulator < client.dt
    finally:
        client.disconnect(close_socket=True)


def test_fixed_tick_advance_pauses_when_gameplay_input_disabled():
    client = GameClient(headless=True)
    try:
        client.conn_state = ConnState.CONNECTED
        client.game_started_by_server = True
        client._predict_accumulator = client.dt * 0.5
        predict_calls = []
        client.predict_local = lambda inp: predict_calls.append(inp.copy())  # type: ignore[method-assign]

        client._advance_gameplay_ticks(client.dt * 2.0, {"move_x": 1.0}, False)

        assert predict_calls == []
        assert client._predict_accumulator == 0.0
    finally:
        client.disconnect(close_socket=True)


def test_connect_treats_wildcard_host_as_local_loopback():
    client = GameClient(server_host="0.0.0.0", headless=True)
    try:
        assert client.connect() is True
        assert client.server_addr == ("127.0.0.1", client.server_port)
        assert client.conn_state == ConnState.CONNECTING
    finally:
        client.disconnect(close_socket=True)


def test_connect_uses_loopback_for_local_hostname_without_dns(monkeypatch):
    hostname = "my-workstation"
    client = GameClient(server_host=hostname, headless=True)
    try:
        monkeypatch.setattr("client.client._socket.gethostname", lambda: hostname)
        monkeypatch.setattr("client.client._socket.getfqdn", lambda: hostname)

        def fail_getaddrinfo(*_args, **_kwargs):
            raise AssertionError("local hostname fallback should not hit DNS")

        monkeypatch.setattr("client.client._socket.getaddrinfo", fail_getaddrinfo)

        assert client.connect() is True
        assert client.server_addr == ("127.0.0.1", client.server_port)
        assert client.last_connection_error is None
    finally:
        client.disconnect(close_socket=True)


def test_connect_reports_ipv6_only_resolution_clearly(monkeypatch):
    client = GameClient(server_host="ipv6-only-host", headless=True)
    try:
        monkeypatch.setattr("client.client._socket.gethostname", lambda: "other-host")
        monkeypatch.setattr(
            "client.client._socket.getfqdn", lambda: "other-host.localdomain"
        )
        monkeypatch.setattr(
            "client.client._socket.getaddrinfo",
            lambda *_args, **_kwargs: [
                (
                    socket.AF_INET6,
                    socket.SOCK_DGRAM,
                    0,
                    "",
                    ("::1", client.server_port, 0, 0),
                )
            ],
        )

        assert client.connect() is False
        assert client.conn_state == ConnState.DISCONNECTED
        assert client.last_connection_error is not None
        assert "IPv6" in client.last_connection_error
    finally:
        client.disconnect(close_socket=True)


def test_connect_reports_mistyped_ipv4_clearly(monkeypatch):
    client = GameClient(server_host="127.0.0.1s", headless=True)
    try:
        monkeypatch.setattr("client.client._socket.gethostname", lambda: "other-host")
        monkeypatch.setattr(
            "client.client._socket.getfqdn", lambda: "other-host.localdomain"
        )

        def raise_gaierror(*_args, **_kwargs):
            raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr("client.client._socket.getaddrinfo", raise_gaierror)

        assert client.connect() is False
        assert client.last_connection_error == (
            "Invalid IPv4 address '127.0.0.1s'. Did you mean '127.0.0.1'?"
        )
    finally:
        client.disconnect(close_socket=True)
