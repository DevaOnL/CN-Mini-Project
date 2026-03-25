"""Session reconnect and GAME_START tests."""

import struct
import time

from client.client import (
    CONNECT_ACK_FORMAT,
    CONNECT_REQ_FORMAT,
    GameClient,
    RELIABLE_EVENT_JOIN,
    RELIABLE_EVENT_FORMAT,
    RELIABLE_EVENT_GAME_START,
    RELIABLE_EVENT_SCORE_SYNC,
)
from common.packet import (
    HANDSHAKE_DISCONNECT_FORMAT,
    Packet,
    PacketType,
    pack_connection_epoch,
    unpack_connection_epoch,
)
from server.client_manager import ClientManager
from server.server import GameServer
from server.session_manager import Session, SessionManager


def test_reconnect_by_token():
    old_addr = ("127.0.0.1", 9000)
    new_addr = ("127.0.0.1", 9100)

    client_mgr = ClientManager()
    session_mgr = SessionManager()

    client = client_mgr.add_client(old_addr)
    session = session_mgr.create(old_addr, client.client_id, token=client.session_token)

    reconnected = session_mgr.reconnect(client.session_token, new_addr)
    client_mgr.bind_address(client, new_addr)

    assert reconnected is session
    assert session.address == new_addr
    assert client_mgr.get_by_address(new_addr) is client
    assert client_mgr.get_by_address(old_addr) is None


def test_session_expiry(monkeypatch):
    session_mgr = SessionManager()
    session = session_mgr.create(("127.0.0.1", 9000), 7)

    monkeypatch.setattr(Session, "IDLE_AFTER", 0.0)
    monkeypatch.setattr(Session, "EXPIRE_AFTER", 0.0)

    expired_ids = session_mgr.expire_sessions()

    assert session.client_id in expired_ids


def test_reusing_token_clears_stale_old_address_mapping():
    session_mgr = SessionManager()
    token = "session-token-001"
    old_addr = ("127.0.0.1", 9000)
    new_addr = ("127.0.0.1", 9100)

    session_mgr.create(old_addr, 1, token=token)
    new_session = session_mgr.create(new_addr, 2, token=token)

    assert session_mgr.get_by_addr(old_addr) is None
    assert session_mgr.get_by_addr(new_addr) is new_session
    assert session_mgr.get_by_token(token) is new_session


def test_expiring_idle_session_does_not_drop_reused_address_mapping():
    session_mgr = SessionManager()
    addr = ("127.0.0.1", 9000)

    old_session = session_mgr.create(addr, 1, token="session-token-old")
    session_mgr.mark_idle(old_session.token)
    old_session.last_heard -= Session.EXPIRE_AFTER + 1.0

    new_session = session_mgr.create(addr, 2, token="session-token-new")
    expired_ids = session_mgr.expire_sessions()

    assert old_session.client_id in expired_ids
    assert session_mgr.get_by_addr(addr) is new_session
    assert session_mgr.get_by_token(new_session.token) is new_session


def test_game_start_flag():
    client = GameClient(headless=True)
    try:
        payload = struct.pack(RELIABLE_EVENT_FORMAT, RELIABLE_EVENT_GAME_START, 0)
        packet = Packet(PacketType.RELIABLE_EVENT, payload=payload)
        client._handle_reliable_event(packet)
        assert client.game_started_by_server is True
    finally:
        client.disconnect(close_socket=True)


def test_score_sync_replaces_local_scores():
    client = GameClient(headless=True)
    try:
        client.scores = {99: 12}
        payload = struct.pack("!BHHHH", RELIABLE_EVENT_SCORE_SYNC, 1, 3, 2, 1)
        packet = Packet(PacketType.RELIABLE_EVENT, payload=payload)
        client._handle_reliable_event(packet)
        assert client.scores == {1: 3, 2: 1}
    finally:
        client.disconnect(close_socket=True)


def test_game_started_resets_when_server_empties():
    server = GameServer(port=0, verbose=False)
    try:
        addr = ("127.0.0.1", 9000)
        client = server.client_mgr.add_client(addr)
        server.session_mgr.create(addr, client.client_id, token=client.session_token)
        server.game_state.add_entity(client.client_id)
        server.game_state.game_started = True
        server.host_client_id = client.client_id
        server.match_elapsed = 10.0
        server.match_over_timer = 1.0

        server._remove_client(client, "test cleanup")

        assert server.game_state.game_started is False
        assert server.host_client_id is None
        assert server.client_mgr.count == 0
        assert server.game_state.tick == 0
        assert server.match_elapsed == 0.0
        assert server.match_over_timer == 0.0
    finally:
        server.sock.close()


def test_reconnecting_client_accepts_fresh_low_sequence_packets():
    client = GameClient(headless=True)
    try:
        client.ack_tracker.on_packet_received(400)
        client.conn_state = type(client.conn_state).RECONNECTING
        client.connect()

        payload = struct.pack(
            CONNECT_ACK_FORMAT,
            3,
            77,
            b"new-session-token".ljust(16, b"\x00"),
            client.connect_nonce,
        )
        packet = Packet(PacketType.CONNECT_ACK, sequence=1, payload=payload)
        client._handle_packet(packet.serialize(), client.server_addr)

        assert client.connected is True
        assert client.client_id == 3
    finally:
        client.disconnect(close_socket=True)


def test_server_removes_timed_out_clients_on_tick():
    server = GameServer(port=0, verbose=False)
    try:
        addr = ("127.0.0.1", 9000)
        client = server.client_mgr.add_client(addr)
        server.session_mgr.create(addr, client.client_id, token=client.session_token)
        server.game_state.add_entity(client.client_id)
        client.last_heard -= 20.0

        server.simulate_tick()

        assert client.client_id not in server.client_mgr.clients
        assert client.client_id not in server.game_state.entities
        session = server.session_mgr.get_by_token(client.session_token)
        assert session is not None
        assert session.state.name == "IDLE"
    finally:
        server.sock.close()


def test_timed_out_session_can_reconnect_with_saved_token():
    server = GameServer(port=0, verbose=False)
    try:
        old_addr = ("127.0.0.1", 9000)
        new_addr = ("127.0.0.1", 9010)
        client = server.client_mgr.add_client(old_addr)
        client.connection_epoch = server._allocate_connection_epoch()
        client.last_connect_nonce = 2
        server.session_mgr.create(
            old_addr, client.client_id, token=client.session_token
        )
        server.game_state.add_entity(client.client_id)
        client.last_heard -= 20.0

        server.simulate_tick()

        reconnect_packet = Packet(
            PacketType.CONNECT_REQ,
            payload=struct.pack(
                CONNECT_REQ_FORMAT,
                client.session_token.encode("ascii").ljust(16, b"\x00"),
                3,
            ),
        )
        server._handle_packet(reconnect_packet.serialize(), new_addr)

        restored = server.client_mgr.get_by_token(client.session_token)
        assert restored is not None
        assert restored.client_id == client.client_id
        assert restored.address == new_addr
        assert server.session_mgr.get_by_token(client.session_token) is not None
    finally:
        server.sock.close()


def test_timed_out_session_reconnect_restores_respawn_state():
    server = GameServer(port=0, verbose=False)
    try:
        peer_addr = ("127.0.0.1", 9050)
        old_addr = ("127.0.0.1", 9060)
        new_addr = ("127.0.0.1", 9070)
        peer = server.client_mgr.add_client(peer_addr)
        server.session_mgr.create(peer_addr, peer.client_id, token=peer.session_token)
        server.game_state.add_entity(peer.client_id)
        client = server.client_mgr.add_client(old_addr)
        client.connection_epoch = server._allocate_connection_epoch()
        client.last_connect_nonce = 1
        server.session_mgr.create(
            old_addr, client.client_id, token=client.session_token
        )
        server.game_state.add_entity(client.client_id)
        server.game_state.entities[client.client_id].health = 0.0
        server.game_state.respawn_timers[client.client_id] = 14
        client.last_heard -= 20.0

        server.simulate_tick()
        server.current_tick = 5
        server.game_state.tick = 5
        reconnect_packet = Packet(
            PacketType.CONNECT_REQ,
            payload=struct.pack(
                CONNECT_REQ_FORMAT,
                client.session_token.encode("ascii").ljust(16, b"\x00"),
                2,
            ),
        )
        server._handle_packet(reconnect_packet.serialize(), new_addr)

        restored = server.client_mgr.get_by_token(client.session_token)
        assert restored is not None
        assert server.game_state.entities[restored.client_id].health == 0.0
        assert server.game_state.respawn_timers[restored.client_id] == 9
    finally:
        server.sock.close()


def test_timed_out_session_reconnect_restores_active_modifier_effects():
    server = GameServer(port=0, verbose=False)
    try:
        peer_addr = ("127.0.0.1", 9075)
        old_addr = ("127.0.0.1", 9080)
        new_addr = ("127.0.0.1", 9090)
        peer = server.client_mgr.add_client(peer_addr)
        server.session_mgr.create(peer_addr, peer.client_id, token=peer.session_token)
        server.game_state.add_entity(peer.client_id)
        client = server.client_mgr.add_client(old_addr)
        client.connection_epoch = server._allocate_connection_epoch()
        client.last_connect_nonce = 4
        server.session_mgr.create(
            old_addr, client.client_id, token=client.session_token
        )
        server.game_state.add_entity(client.client_id)
        server.game_state.damage_boost_until[client.client_id] = (
            server.game_state.tick + 9
        )
        server.game_state.dash_cooldown_until[client.client_id] = (
            server.game_state.tick + 7
        )
        client.last_heard -= 20.0

        server.simulate_tick()
        server.current_tick = 4
        server.game_state.tick = 4
        reconnect_packet = Packet(
            PacketType.CONNECT_REQ,
            payload=struct.pack(
                CONNECT_REQ_FORMAT,
                client.session_token.encode("ascii").ljust(16, b"\x00"),
                5,
            ),
        )
        server._handle_packet(reconnect_packet.serialize(), new_addr)

        restored = server.client_mgr.get_by_token(client.session_token)
        assert restored is not None
        assert server.game_state.damage_boost_until[restored.client_id] == 9
        assert server.game_state.dash_cooldown_until[restored.client_id] == 7
    finally:
        server.sock.close()


def test_full_reset_clears_stale_idle_reconnect_state():
    server = GameServer(port=0, verbose=False)
    try:
        old_addr = ("127.0.0.1", 9100)
        new_addr = ("127.0.0.1", 9110)
        client = server.client_mgr.add_client(old_addr)
        client.connection_epoch = server._allocate_connection_epoch()
        client.last_connect_nonce = 1
        server.session_mgr.create(
            old_addr, client.client_id, token=client.session_token
        )
        server.game_state.add_entity(client.client_id)
        server.game_state.entities[client.client_id].health = 0.0
        server.game_state.scores[client.client_id] = 4
        server.game_state.respawn_timers[client.client_id] = 20
        client.last_heard -= 20.0

        server.simulate_tick()
        assert server.idle_client_state == {}

        reconnect_packet = Packet(
            PacketType.CONNECT_REQ,
            payload=struct.pack(
                CONNECT_REQ_FORMAT,
                client.session_token.encode("ascii").ljust(16, b"\x00"),
                2,
            ),
        )
        server._handle_packet(reconnect_packet.serialize(), new_addr)

        restored = server.client_mgr.get_by_token(client.session_token)
        assert restored is not None
        assert server.game_state.entities[restored.client_id].health == 100.0
        assert server.game_state.scores[restored.client_id] == 0
        assert restored.client_id not in server.game_state.respawn_timers
    finally:
        server.sock.close()


def test_expired_idle_session_clears_stale_score_and_restore_blob():
    server = GameServer(port=0, verbose=False)
    try:
        peer_addr = ("127.0.0.1", 9120)
        timed_out_addr = ("127.0.0.1", 9130)
        peer = server.client_mgr.add_client(peer_addr)
        server.session_mgr.create(peer_addr, peer.client_id, token=peer.session_token)
        server.game_state.add_entity(peer.client_id)

        client = server.client_mgr.add_client(timed_out_addr)
        server.session_mgr.create(
            timed_out_addr, client.client_id, token=client.session_token
        )
        server.game_state.add_entity(client.client_id)
        server.game_state.scores[client.client_id] = 4
        client.last_heard -= 20.0

        server.simulate_tick()
        session = server.session_mgr.get_by_token(client.session_token)
        assert session is not None
        session.last_heard -= 100.0

        server.simulate_tick()

        assert client.client_id not in server.game_state.scores
        assert client.session_token not in server.idle_client_state
    finally:
        server.sock.close()


def test_reconnect_restore_broadcasts_join_and_score_sync_to_peers():
    server = GameServer(port=0, verbose=False)
    sent = []
    server._sendto = lambda data, addr: sent.append((data, addr))
    try:
        peer_addr = ("127.0.0.1", 9000)
        stale_addr = ("127.0.0.1", 9010)
        new_addr = ("127.0.0.1", 9020)

        peer = server.client_mgr.add_client(peer_addr)
        peer.connection_epoch = server._allocate_connection_epoch()
        server.session_mgr.create(peer_addr, peer.client_id, token=peer.session_token)
        server.game_state.add_entity(peer.client_id)

        reconnecting = server.client_mgr.add_client(stale_addr)
        reconnecting.connection_epoch = server._allocate_connection_epoch()
        reconnecting.last_connect_nonce = 2
        server.session_mgr.create(
            stale_addr,
            reconnecting.client_id,
            token=reconnecting.session_token,
        )
        server.game_state.add_entity(reconnecting.client_id)
        server.game_state.scores[reconnecting.client_id] = 4
        reconnecting.last_heard -= 20.0

        server.simulate_tick()
        sent.clear()

        reconnect_packet = Packet(
            PacketType.CONNECT_REQ,
            payload=struct.pack(
                CONNECT_REQ_FORMAT,
                reconnecting.session_token.encode("ascii").ljust(16, b"\x00"),
                3,
            ),
        )
        server._handle_packet(reconnect_packet.serialize(), new_addr)

        peer_packets = [
            Packet.deserialize(data) for data, addr in sent if addr == peer_addr
        ]
        reliable_payloads = []
        for packet in peer_packets:
            if packet.packet_type == PacketType.RELIABLE_EVENT:
                unpacked = unpack_connection_epoch(packet.payload)
                assert unpacked is not None
                reliable_payloads.append(unpacked[1])

        assert any(
            payload[: struct.calcsize(RELIABLE_EVENT_FORMAT)]
            == struct.pack(
                RELIABLE_EVENT_FORMAT, RELIABLE_EVENT_JOIN, reconnecting.client_id
            )
            for payload in reliable_payloads
        )
        assert any(
            payload[0] == RELIABLE_EVENT_SCORE_SYNC for payload in reliable_payloads
        )
    finally:
        server.sock.close()


def test_server_ignores_early_nonconnect_packets_after_connect():
    server = GameServer(port=0, verbose=False)
    try:
        addr = ("127.0.0.1", 9000)
        client = server.client_mgr.add_client(addr)
        client.connection_epoch = 11
        client.delay_nonconnect_packets(1.0)
        server.host_client_id = client.client_id

        input_packet = Packet(
            PacketType.INPUT,
            payload=pack_connection_epoch(
                client.connection_epoch,
                struct.pack("!IffB", 1, 1.0, 0.0, 0),
            ),
        )
        start_packet = Packet(
            PacketType.RELIABLE_EVENT,
            payload=pack_connection_epoch(
                client.connection_epoch,
                struct.pack(RELIABLE_EVENT_FORMAT, RELIABLE_EVENT_GAME_START, 0),
            ),
        )

        server._handle_packet(input_packet.serialize(), addr)
        server._handle_packet(start_packet.serialize(), addr)

        assert client.pending_inputs == []
        assert server.game_state.game_started is False

        client.accept_packets_after = time.monotonic() - 1.0
        server._handle_packet(input_packet.serialize(), addr)

        assert client.pending_inputs != []
    finally:
        server.sock.close()


def test_recent_disconnect_addr_is_temporarily_ignored_then_allows_new_connect():
    server = GameServer(port=0, verbose=False)
    try:
        addr = ("127.0.0.1", 9000)
        old_client = server.client_mgr.add_client(addr)
        old_client.ack_tracker.local_sequence = 25

        server._remove_client(old_client, "test cleanup", broadcast_leave=False)
        server._handle_packet(Packet(PacketType.CONNECT_REQ).serialize(), addr)

        assert server.client_mgr.get_by_address(addr) is None

        server.recent_disconnect_addrs[addr] = (time.monotonic() - 1.0, 25)
        server._handle_packet(Packet(PacketType.CONNECT_REQ).serialize(), addr)

        new_client = server.client_mgr.get_by_address(addr)
        assert new_client is not None
    finally:
        server.sock.close()


def test_stale_reconnect_request_does_not_rebind_active_session():
    server = GameServer(port=0, verbose=False)
    try:
        old_addr = ("127.0.0.1", 9000)
        new_addr = ("127.0.0.1", 9100)
        client = server.client_mgr.add_client(old_addr)
        client.connection_epoch = server._allocate_connection_epoch()
        client.last_connect_nonce = 5
        session = server.session_mgr.create(
            old_addr, client.client_id, token=client.session_token
        )

        fresh_reconnect = Packet(
            PacketType.CONNECT_REQ,
            payload=struct.pack(
                CONNECT_REQ_FORMAT,
                client.session_token.encode("ascii").ljust(16, b"\x00"),
                6,
            ),
        )
        stale_reconnect = Packet(
            PacketType.CONNECT_REQ,
            payload=struct.pack(
                CONNECT_REQ_FORMAT,
                client.session_token.encode("ascii").ljust(16, b"\x00"),
                5,
            ),
        )

        server._handle_packet(fresh_reconnect.serialize(), new_addr)

        assert client.address == new_addr
        assert session.address == new_addr
        assert client.last_connect_nonce == 6

        server._handle_packet(stale_reconnect.serialize(), old_addr)

        assert client.address == new_addr
        assert session.address == new_addr
        assert server.client_mgr.get_by_address(old_addr) is None
    finally:
        server.sock.close()


def test_same_address_reconnect_rotates_connection_epoch():
    server = GameServer(port=0, verbose=False)
    try:
        addr = ("127.0.0.1", 9050)
        client = server.client_mgr.add_client(addr)
        client.connection_epoch = server._allocate_connection_epoch()
        client.last_connect_nonce = 3
        server.session_mgr.create(addr, client.client_id, token=client.session_token)
        original_epoch = client.connection_epoch

        reconnect_packet = Packet(
            PacketType.CONNECT_REQ,
            payload=struct.pack(
                CONNECT_REQ_FORMAT,
                client.session_token.encode("ascii").ljust(16, b"\x00"),
                4,
            ),
        )

        server._handle_packet(reconnect_packet.serialize(), addr)

        assert client.connection_epoch != original_epoch
        assert client.last_connect_nonce == 4
        assert client.pending_inputs == []
    finally:
        server.sock.close()


def test_token_only_reconnect_request_is_ignored():
    server = GameServer(port=0, verbose=False)
    try:
        old_addr = ("127.0.0.1", 9000)
        new_addr = ("127.0.0.1", 9100)
        client = server.client_mgr.add_client(old_addr)
        client.connection_epoch = server._allocate_connection_epoch()
        client.last_connect_nonce = 7
        session = server.session_mgr.create(
            old_addr, client.client_id, token=client.session_token
        )

        legacy_reconnect = Packet(
            PacketType.CONNECT_REQ,
            payload=client.session_token.encode("ascii").ljust(16, b"\x00"),
        )

        server._handle_packet(legacy_reconnect.serialize(), new_addr)

        assert client.address == old_addr
        assert session.address == old_addr
        assert server.client_mgr.get_by_address(new_addr) is None
    finally:
        server.sock.close()


def test_unknown_reconnect_token_is_rejected_instead_of_creating_new_client():
    server = GameServer(port=0, verbose=False)
    try:
        addr = ("127.0.0.1", 9200)
        packet = Packet(
            PacketType.CONNECT_REQ,
            payload=struct.pack(
                CONNECT_REQ_FORMAT,
                b"missing-token".ljust(16, b"\x00"),
                9,
            ),
        )

        server._handle_packet(packet.serialize(), addr)

        assert server.client_mgr.get_by_address(addr) is None
        assert server.client_mgr.count == 0
    finally:
        server.sock.close()


def test_pending_connect_disconnect_by_nonce_removes_client():
    server = GameServer(port=0, verbose=False)
    try:
        addr = ("127.0.0.1", 9300)
        client = server.client_mgr.add_client(addr)
        client.connection_epoch = server._allocate_connection_epoch()
        client.last_connect_nonce = 11

        packet = Packet(
            PacketType.DISCONNECT,
            payload=struct.pack(
                HANDSHAKE_DISCONNECT_FORMAT,
                b"\x00" * 16,
                client.last_connect_nonce,
                0,
            ),
        )

        server._handle_packet(packet.serialize(), addr)

        assert server.client_mgr.get_by_address(addr) is None
    finally:
        server.sock.close()


def test_reconnecting_disconnect_by_token_removes_client():
    server = GameServer(port=0, verbose=False)
    try:
        old_addr = ("127.0.0.1", 9000)
        new_addr = ("127.0.0.1", 9400)
        client = server.client_mgr.add_client(old_addr)
        client.connection_epoch = server._allocate_connection_epoch()
        client.last_connect_nonce = 12
        server.session_mgr.create(
            old_addr, client.client_id, token=client.session_token
        )

        packet = Packet(
            PacketType.DISCONNECT,
            payload=struct.pack(
                CONNECT_REQ_FORMAT,
                client.session_token.encode("ascii").ljust(16, b"\x00"),
                client.last_connect_nonce,
            ),
        )

        server._handle_packet(packet.serialize(), new_addr)

        assert server.client_mgr.get_by_token(client.session_token) is None
    finally:
        server.sock.close()


def test_stale_epoch_disconnect_does_not_match_handshake_cancel():
    server = GameServer(port=0, verbose=False)
    try:
        addr = ("127.0.0.1", 9500)
        client = server.client_mgr.add_client(addr)
        client.connection_epoch = 99
        client.last_connect_nonce = 7

        stale_disconnect = Packet(
            PacketType.DISCONNECT,
            payload=pack_connection_epoch(7, b"\x00"),
        )

        server._handle_packet(stale_disconnect.serialize(), addr)

        assert server.client_mgr.get_by_address(addr) is client
    finally:
        server.sock.close()
