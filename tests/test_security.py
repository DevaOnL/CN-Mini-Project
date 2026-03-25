"""Secure UDP handshake and payload protection tests."""

import struct
import time

from common.packet import (
    Packet,
    PacketType,
    DISCONNECT_REASON_AUTH_FAILED,
    DISCONNECT_REASON_SECURE_REQUIRED,
    HANDSHAKE_DISCONNECT_FORMAT,
)
from common.security import (
    PendingSecureHandshake,
    SECURE_HELLO_FORMAT,
    SECURE_PROTOCOL_VERSION,
    build_client_proof,
    build_server_proof,
    decrypt_payload,
    derive_room_psk,
    derive_session_key,
    encrypt_payload,
)
from server.server import CONNECT_REQ_FORMAT, GameServer


def test_room_psk_derivation_is_deterministic():
    assert derive_room_psk("shared secret") == derive_room_psk("shared secret")
    assert derive_room_psk("shared secret") != derive_room_psk("other secret")


def test_server_proof_validation_depends_on_room_key():
    correct_psk = derive_room_psk("alpha")
    wrong_psk = derive_room_psk("beta")
    client_nonce = b"c" * 16
    server_nonce = b"s" * 16

    proof = build_server_proof(correct_psk, client_nonce, server_nonce)

    assert proof == build_server_proof(correct_psk, client_nonce, server_nonce)
    assert proof != build_server_proof(wrong_psk, client_nonce, server_nonce)


def test_secure_payload_round_trip_rejects_tampering():
    psk = derive_room_psk("round-trip")
    session_key = derive_session_key(psk, b"a" * 16, b"b" * 16)
    header = Packet.pack_header(7, 0, 0, PacketType.SNAPSHOT, len(b"payload") + 28)
    encrypted = encrypt_payload(session_key, header, b"payload", nonce=b"0" * 12)

    assert decrypt_payload(session_key, header, encrypted) == b"payload"

    tampered_header = bytearray(header)
    tampered_header[-1] ^= 0x01
    try:
        decrypt_payload(session_key, bytes(tampered_header), encrypted)
    except ValueError:
        pass
    else:
        raise AssertionError("Header tampering should fail authentication")

    tampered_payload = bytearray(encrypted)
    tampered_payload[-1] ^= 0x01
    try:
        decrypt_payload(session_key, header, bytes(tampered_payload))
    except ValueError:
        pass
    else:
        raise AssertionError("Payload tampering should fail authentication")


def test_expired_pending_secure_handshake_is_pruned():
    server = GameServer(port=0, verbose=False, room_key="expiry-check")
    try:
        addr = ("127.0.0.1", 9999)
        server.pending_secure_handshakes[addr] = PendingSecureHandshake(
            client_nonce=b"c" * 16,
            server_nonce=b"s" * 16,
            session_key=b"k" * 32,
            expires_at=time.monotonic() - 0.01,
        )

        assert server._get_pending_secure_handshake(addr) is None
        assert addr not in server.pending_secure_handshakes
    finally:
        server.sock.close()


def test_secure_server_rejects_legacy_cleartext_connect_req():
    server = GameServer(port=0, verbose=False, room_key="secure-only")
    sent_packets = []

    def capture(data, addr):
        sent_packets.append((data, addr))

    server._sendto_immediate = capture
    try:
        payload = struct.pack(CONNECT_REQ_FORMAT, b"\x00" * 16, 55)
        pkt = Packet(PacketType.CONNECT_REQ, payload=payload)

        server._handle_packet(pkt.serialize(), ("127.0.0.1", 9001))

        response, _addr = sent_packets[0]
        disconnect = Packet.deserialize(response)
        assert disconnect.packet_type == PacketType.DISCONNECT
        _token, connect_nonce, reason_code = struct.unpack(
            HANDSHAKE_DISCONNECT_FORMAT,
            disconnect.payload[: struct.calcsize(HANDSHAKE_DISCONNECT_FORMAT)],
        )
        assert connect_nonce == 55
        assert reason_code == DISCONNECT_REASON_SECURE_REQUIRED
    finally:
        server.sock.close()


def test_secure_hello_with_wrong_key_returns_auth_failed():
    server = GameServer(port=0, verbose=False, room_key="right-key")
    sent_packets = []

    def capture(data, addr):
        sent_packets.append((data, addr))

    server._sendto_immediate = capture
    try:
        client_nonce = b"n" * 16
        wrong_psk = derive_room_psk("wrong-key")
        payload = struct.pack(
            SECURE_HELLO_FORMAT,
            SECURE_PROTOCOL_VERSION,
            client_nonce,
            build_client_proof(wrong_psk, client_nonce),
        )
        pkt = Packet(PacketType.SECURE_HELLO, payload=payload)

        server._handle_packet(pkt.serialize(), ("127.0.0.1", 9002))

        response, _addr = sent_packets[0]
        disconnect = Packet.deserialize(response)
        assert disconnect.packet_type == PacketType.DISCONNECT
        assert disconnect.payload[-1] == DISCONNECT_REASON_AUTH_FAILED
    finally:
        server.sock.close()
