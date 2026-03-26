"""Integration tests for DTLS-secured multiplayer flows."""

from __future__ import annotations

import random
import time

import pytest

from client.client import ConnState, GameClient
from server.server import GameServer


TEST_ROOM_KEY = "integration-room-key"


def _make_server(*, room_key: str = TEST_ROOM_KEY, cert_file=None, key_file=None) -> GameServer:
    port = random.randint(10000, 60000)
    return GameServer(
        host="127.0.0.1",
        port=port,
        tick_rate=20,
        verbose=False,
        room_key=room_key,
        cert_file=cert_file,
        key_file=key_file,
    )


def _pump(server: GameServer, *clients: GameClient, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for client in clients:
            client.receive_packets()
        server.receive_all_packets()
        server.simulate_tick()
        server.send_snapshots()
        server.current_tick += 1
        time.sleep(0.02)


def _connect_client(client: GameClient, server: GameServer, timeout: float = 3.0):
    assert client.connect() is True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _pump(server, client, timeout=0.05)
        if client.connected:
            return
    raise AssertionError("Client did not connect in time.")


def test_single_client_connects_over_dtls_and_receives_snapshots():
    server = _make_server()
    client = GameClient(
        server_host="127.0.0.1",
        server_port=server.port,
        headless=True,
        room_key=TEST_ROOM_KEY,
    )
    try:
        _connect_client(client, server)

        _pump(server, client, timeout=0.3)

        assert client.client_id == 1
        assert client.server_certificate_fingerprint == server.certificate_fingerprint
        assert client.server_snapshots
        assert 1 in client.server_snapshots[-1].entities
    finally:
        client.disconnect(close_socket=True)
        server.dtls_transport.close()
        server.sock.close()


def test_two_clients_connect_and_share_lobby_and_game_start():
    server = _make_server()
    host = GameClient(
        server_host="127.0.0.1",
        server_port=server.port,
        headless=True,
        room_key=TEST_ROOM_KEY,
    )
    guest = GameClient(
        server_host="127.0.0.1",
        server_port=server.port,
        headless=True,
        room_key=TEST_ROOM_KEY,
    )
    try:
        _connect_client(host, server)
        _connect_client(guest, server)
        _pump(server, host, guest, timeout=0.5)

        assert host.client_id == 1
        assert guest.client_id == 2
        assert host.server_snapshots
        assert guest.server_snapshots
        assert sorted(host.server_snapshots[-1].entities) == [1, 2]
        assert sorted(guest.server_snapshots[-1].entities) == [1, 2]

        assert host.request_game_start() is True
        _pump(server, host, guest, timeout=0.5)

        assert host.game_started_by_server is True
        assert guest.game_started_by_server is True
    finally:
        host.disconnect(close_socket=True)
        guest.disconnect(close_socket=True)
        server.dtls_transport.close()
        server.sock.close()


def test_connected_clients_receive_each_others_aliases():
    server = _make_server()
    host = GameClient(
        server_host="127.0.0.1",
        server_port=server.port,
        headless=True,
        room_key=TEST_ROOM_KEY,
    )
    guest = GameClient(
        server_host="127.0.0.1",
        server_port=server.port,
        headless=True,
        room_key=TEST_ROOM_KEY,
    )
    host.player_name = "Alpha"
    guest.player_name = "Bravo"
    try:
        _connect_client(host, server)
        _connect_client(guest, server)
        _pump(server, host, guest, timeout=0.5)

        assert host.display_name_for(1) == "Alpha"
        assert host.display_name_for(2) == "Bravo"
        assert guest.display_name_for(1) == "Alpha"
        assert guest.display_name_for(2) == "Bravo"
    finally:
        host.disconnect(close_socket=True)
        guest.disconnect(close_socket=True)
        server.dtls_transport.close()
        server.sock.close()


def test_wrong_room_key_returns_auth_failed():
    server = _make_server()
    client = GameClient(
        server_host="127.0.0.1",
        server_port=server.port,
        headless=True,
        room_key="wrong-room-key",
    )
    try:
        assert client.connect() is True
        _pump(server, client, timeout=0.6)

        assert client.connected is False
        assert client.conn_state == ConnState.DISCONNECTED
        assert client.last_connection_error == "Room key rejected by server."
    finally:
        client.disconnect(close_socket=True)
        server.dtls_transport.close()
        server.sock.close()


def test_reconnect_reuses_session_token_after_fresh_dtls_handshake():
    server = _make_server()
    client = GameClient(
        server_host="127.0.0.1",
        server_port=server.port,
        headless=True,
        room_key=TEST_ROOM_KEY,
    )
    try:
        _connect_client(client, server)
        original_id = client.client_id
        original_token = client.session_token
        original_socket = client.sock

        client._replace_socket()
        assert client.sock is not original_socket
        client.conn_state = ConnState.RECONNECTING
        client._last_connect_attempt_time = 0.0
        assert client.connect() is True
        _pump(server, client, timeout=1.0)

        assert client.connected is True
        assert client.client_id == original_id
        assert client.session_token == original_token
    finally:
        client.disconnect(close_socket=True)
        server.dtls_transport.close()
        server.sock.close()


def test_trusted_host_pin_blocks_certificate_rotation(tmp_path):
    port = random.randint(10000, 60000)
    known_hosts_path = tmp_path / "known_hosts.json"
    cert_a = tmp_path / "server_a.pem"
    key_a = tmp_path / "server_a.key"
    cert_b = tmp_path / "server_b.pem"
    key_b = tmp_path / "server_b.key"

    first_server = GameServer(
        host="127.0.0.1",
        port=port,
        tick_rate=20,
        verbose=False,
        room_key=TEST_ROOM_KEY,
        cert_file=cert_a,
        key_file=key_a,
    )
    first_client = GameClient(
        server_host="127.0.0.1",
        server_port=port,
        headless=True,
        room_key=TEST_ROOM_KEY,
    )
    first_client.set_known_hosts_path(str(known_hosts_path))

    try:
        _connect_client(first_client, first_server)
    finally:
        first_client.disconnect(close_socket=True)
        first_server.dtls_transport.close()
        first_server.sock.close()

    second_server = GameServer(
        host="127.0.0.1",
        port=port,
        tick_rate=20,
        verbose=False,
        room_key=TEST_ROOM_KEY,
        cert_file=cert_b,
        key_file=key_b,
    )
    second_client = GameClient(
        server_host="127.0.0.1",
        server_port=port,
        headless=True,
        room_key=TEST_ROOM_KEY,
    )
    second_client.set_known_hosts_path(str(known_hosts_path))

    try:
        assert second_client.connect() is True
        _pump(second_server, second_client, timeout=0.6)

        assert second_client.connected is False
        assert second_client.conn_state == ConnState.DISCONNECTED
        assert second_client.last_connection_error is not None
        assert "Trusted host changed" in second_client.last_connection_error
    finally:
        second_client.disconnect(close_socket=True)
        second_server.dtls_transport.close()
        second_server.sock.close()
