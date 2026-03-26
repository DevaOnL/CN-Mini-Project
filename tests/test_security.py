"""DTLS certificate, pinning, and transport tests."""

from __future__ import annotations

from common.dtls import (
    DtlsClientTransport,
    DtlsServerTransport,
    clear_known_hosts,
    ensure_server_certificate,
    load_known_hosts,
    verify_or_trust_host,
)
from common.packet import pack_connect_request, unpack_connect_request


def _pump_dtls_pair(
    client: DtlsClientTransport,
    server: DtlsServerTransport,
    addr: tuple = ("127.0.0.1", 9999),
    *,
    steps: int = 200,
):
    for _ in range(steps):
        client.poll()
        for datagram in client.drain_outbound():
            server.receive_datagram(addr, datagram)

        server.poll()
        for server_addr, datagram in server.drain_outbound():
            assert server_addr == addr
            client.feed_datagram(datagram)

        if client.handshake_complete and server.handshake_complete(addr):
            return

    raise AssertionError("DTLS handshake did not complete in time.")


def test_server_certificate_generation_is_stable(tmp_path):
    cert_file = tmp_path / "server_cert.pem"
    key_file = tmp_path / "server_key.pem"

    first = ensure_server_certificate(cert_file, key_file, common_name="test-host")
    second = ensure_server_certificate(cert_file, key_file, common_name="ignored")

    assert cert_file.exists()
    assert key_file.exists()
    assert first.fingerprint == second.fingerprint


def test_trusted_host_store_records_and_rejects_mismatch(tmp_path):
    known_hosts_path = tmp_path / "known_hosts.json"

    assert verify_or_trust_host(
        "127.0.0.1",
        9000,
        "AA" * 32,
        path=known_hosts_path,
    ) is None
    assert load_known_hosts(known_hosts_path) == {
        "127.0.0.1:9000": ":".join(["AA"] * 32)
    }

    assert verify_or_trust_host(
        "127.0.0.1",
        9000,
        "AA" * 32,
        path=known_hosts_path,
    ) is None

    mismatch = verify_or_trust_host(
        "127.0.0.1",
        9000,
        "BB" * 32,
        path=known_hosts_path,
    )
    assert mismatch is not None
    assert "Trusted host changed" in mismatch

    clear_known_hosts(known_hosts_path)
    assert load_known_hosts(known_hosts_path) == {}


def test_connect_request_round_trip_and_malformed_lengths():
    payload = pack_connect_request("reconnect-token", 17, "shared-room", "Alpha")

    assert unpack_connect_request(payload) == (
        "reconnect-token",
        17,
        "shared-room",
        "Alpha",
    )
    assert unpack_connect_request(payload[:-1]) is None
    assert unpack_connect_request(payload + b"\x00") is None


def test_dtls_transports_complete_handshake_and_exchange_packets(tmp_path):
    cert_info = ensure_server_certificate(
        tmp_path / "server_cert.pem",
        tmp_path / "server_key.pem",
        common_name="dtls-test",
    )
    server = DtlsServerTransport(cert_info.cert_file, cert_info.key_file)
    client = DtlsClientTransport()
    client.start()

    _pump_dtls_pair(client, server)

    assert client.handshake_complete is True
    assert server.handshake_complete(("127.0.0.1", 9999)) is True
    assert client.peer_fingerprint() == cert_info.fingerprint

    client.send_packet(b"hello")
    _pump_dtls_pair(client, server)
    server_packets = server.drain_packets()
    assert server_packets == [(("127.0.0.1", 9999), b"hello")]

    server.send_packet(("127.0.0.1", 9999), b"world")
    _pump_dtls_pair(client, server)
    assert client.drain_packets() == [b"world"]
