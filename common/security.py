"""Secure UDP helpers for authenticated room-key handshakes and payload AEAD."""

from __future__ import annotations

import hmac
import secrets
import struct
from dataclasses import dataclass
from hashlib import sha256

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


SECURE_PROTOCOL_VERSION = 1
SECURE_HANDSHAKE_NONCE_SIZE = 16
SECURE_HANDSHAKE_PROOF_SIZE = 32
SECURE_PACKET_NONCE_SIZE = 12
SECURE_HANDSHAKE_TIMEOUT_SECS = 5.0
SECURE_PSK_SIZE = 32
SECURE_SESSION_KEY_SIZE = 32
SECURE_PSK_SALT = b"multiplayer-engine-room-key-v1"
SECURE_SESSION_INFO = b"multiplayer-engine-secure-v1"
SECURE_HELLO_FORMAT = f"!B{SECURE_HANDSHAKE_NONCE_SIZE}s{SECURE_HANDSHAKE_PROOF_SIZE}s"
SECURE_HELLO_ACK_FORMAT = (
    f"!B{SECURE_HANDSHAKE_NONCE_SIZE}s{SECURE_HANDSHAKE_PROOF_SIZE}s"
)
SECURE_HELLO_SIZE = struct.calcsize(SECURE_HELLO_FORMAT)
SECURE_HELLO_ACK_SIZE = struct.calcsize(SECURE_HELLO_ACK_FORMAT)


@dataclass(slots=True)
class PendingSecureHandshake:
    client_nonce: bytes
    server_nonce: bytes
    session_key: bytes
    expires_at: float


def derive_room_psk(room_key: str) -> bytes:
    normalized = room_key.strip()
    if not normalized:
        raise ValueError("Room key cannot be empty.")
    # Use stronger KDF parameters for better security
    # n should be between 2^14 and 2^20 per recommendation; using 2^18 for good security/performance balance
    kdf = Scrypt(salt=SECURE_PSK_SALT, length=SECURE_PSK_SIZE, n=2**18, r=8, p=1)
    return kdf.derive(normalized.encode("utf-8"))


def generate_handshake_nonce() -> bytes:
    return secrets.token_bytes(SECURE_HANDSHAKE_NONCE_SIZE)


def generate_packet_nonce() -> bytes:
    return secrets.token_bytes(SECURE_PACKET_NONCE_SIZE)


def build_client_proof(psk: bytes, client_nonce: bytes) -> bytes:
    return hmac.digest(psk, b"client" + client_nonce, sha256)


def build_server_proof(psk: bytes, client_nonce: bytes, server_nonce: bytes) -> bytes:
    return hmac.digest(psk, b"server" + client_nonce + server_nonce, sha256)


def verify_client_proof(psk: bytes, client_nonce: bytes, client_proof: bytes) -> bool:
    expected = build_client_proof(psk, client_nonce)
    return hmac.compare_digest(expected, client_proof)


def verify_server_proof(
    psk: bytes,
    client_nonce: bytes,
    server_nonce: bytes,
    server_proof: bytes,
) -> bool:
    expected = build_server_proof(psk, client_nonce, server_nonce)
    return hmac.compare_digest(expected, server_proof)


def derive_session_key(psk: bytes, client_nonce: bytes, server_nonce: bytes) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=SECURE_SESSION_KEY_SIZE,
        salt=client_nonce + server_nonce,
        info=SECURE_SESSION_INFO,
    )
    return hkdf.derive(psk)


def encrypt_payload(
    session_key: bytes,
    header_bytes: bytes,
    plaintext_payload: bytes,
    *,
    nonce: bytes | None = None,
) -> bytes:
    if nonce is None:
        nonce = generate_packet_nonce()
    cipher = ChaCha20Poly1305(session_key)
    ciphertext = cipher.encrypt(nonce, plaintext_payload, header_bytes)
    return nonce + ciphertext


def decrypt_payload(
    session_key: bytes,
    header_bytes: bytes,
    encrypted_payload: bytes,
) -> bytes:
    if len(encrypted_payload) < SECURE_PACKET_NONCE_SIZE:
        raise ValueError("Encrypted payload is truncated.")
    nonce = encrypted_payload[:SECURE_PACKET_NONCE_SIZE]
    ciphertext = encrypted_payload[SECURE_PACKET_NONCE_SIZE:]
    cipher = ChaCha20Poly1305(session_key)
    try:
        return cipher.decrypt(nonce, ciphertext, header_bytes)
    except InvalidTag as exc:
        raise ValueError("Encrypted payload failed authentication.") from exc
