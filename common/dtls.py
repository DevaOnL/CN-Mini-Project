"""DTLS transport, certificate, and trusted-host helpers."""

from __future__ import annotations

import json
import pathlib
import socket
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from OpenSSL import SSL, crypto
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


DEFAULT_CERT_DIR = pathlib.Path.home() / ".multiplayer_engine" / "certs"
DEFAULT_SERVER_CERT_PATH = DEFAULT_CERT_DIR / "server_cert.pem"
DEFAULT_SERVER_KEY_PATH = DEFAULT_CERT_DIR / "server_key.pem"
DEFAULT_KNOWN_HOSTS_PATH = pathlib.Path.home() / ".multiplayer_engine_known_hosts.json"
DTLS_HANDSHAKE_TIMEOUT_SECS = 5.0
DTLS_IDLE_TIMEOUT_SECS = 30.0
DTLS_BIO_BUFFER_SIZE = 65536
DTLS_APPDATA_BUFFER_SIZE = 65536
DTLS_DEFAULT_CIPHERTEXT_MTU = 1200


@dataclass(slots=True)
class ServerCertificateInfo:
    cert_file: pathlib.Path
    key_file: pathlib.Path
    fingerprint: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_path(path: str | pathlib.Path | None, default: pathlib.Path) -> pathlib.Path:
    if path is None:
        return default
    return pathlib.Path(path).expanduser().resolve()


def format_fingerprint(hex_digest: str) -> str:
    normalized = hex_digest.replace(":", "").upper()
    return ":".join(
        normalized[index : index + 2] for index in range(0, len(normalized), 2)
    )


def fingerprint_for_certificate(cert: crypto.X509) -> str:
    der_bytes = crypto.dump_certificate(crypto.FILETYPE_ASN1, cert)
    return format_fingerprint(sha256(der_bytes).hexdigest())


def load_certificate_fingerprint(cert_file: str | pathlib.Path) -> str:
    cert_path = pathlib.Path(cert_file).expanduser().resolve()
    certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return format_fingerprint(certificate.fingerprint(hashes.SHA256()).hex())


def ensure_server_certificate(
    cert_file: str | pathlib.Path | None = None,
    key_file: str | pathlib.Path | None = None,
    *,
    common_name: str | None = None,
) -> ServerCertificateInfo:
    if (cert_file is None) != (key_file is None):
        raise ValueError("Certificate and key paths must be provided together.")

    cert_path = _normalize_path(cert_file, DEFAULT_SERVER_CERT_PATH)
    key_path = _normalize_path(key_file, DEFAULT_SERVER_KEY_PATH)

    if cert_file is not None and (cert_path.exists() ^ key_path.exists()):
        raise ValueError(
            "Explicit DTLS certificate and key files must both exist or both be absent."
        )

    if cert_path.exists() and key_path.exists():
        return ServerCertificateInfo(
            cert_file=cert_path,
            key_file=key_path,
            fingerprint=load_certificate_fingerprint(cert_path),
        )

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    key = ec.generate_private_key(ec.SECP256R1())
    host_name = (common_name or socket.gethostname().strip() or "multiplayer-engine").strip()

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, host_name[:64]),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "multiplayer-engine"),
        ]
    )
    now = _utc_now()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(host_name[:64])]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        key_path.chmod(0o600)
    except OSError:
        pass

    return ServerCertificateInfo(
        cert_file=cert_path,
        key_file=key_path,
        fingerprint=format_fingerprint(certificate.fingerprint(hashes.SHA256()).hex()),
    )


def known_host_key(host: str, port: int) -> str:
    return f"{host.strip().lower()}:{int(port)}"


def load_known_hosts(
    path: str | pathlib.Path = DEFAULT_KNOWN_HOSTS_PATH,
) -> dict[str, str]:
    known_hosts_path = pathlib.Path(path).expanduser().resolve()
    if not known_hosts_path.exists():
        return {}
    try:
        raw = json.loads(known_hosts_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, str] = {}
    for host_key, fingerprint in raw.items():
        if not isinstance(host_key, str) or not isinstance(fingerprint, str):
            continue
        normalized[host_key.strip().lower()] = format_fingerprint(fingerprint)
    return normalized


def save_known_hosts(
    known_hosts: dict[str, str],
    path: str | pathlib.Path = DEFAULT_KNOWN_HOSTS_PATH,
):
    known_hosts_path = pathlib.Path(path).expanduser().resolve()
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = known_hosts_path.with_suffix(f"{known_hosts_path.suffix}.tmp")
    temp_path.write_text(json.dumps(dict(sorted(known_hosts.items())), indent=2))
    temp_path.replace(known_hosts_path)


def clear_known_hosts(path: str | pathlib.Path = DEFAULT_KNOWN_HOSTS_PATH):
    known_hosts_path = pathlib.Path(path).expanduser().resolve()
    if known_hosts_path.exists():
        known_hosts_path.unlink()


def verify_or_trust_host(
    host: str,
    port: int,
    fingerprint: str,
    *,
    path: str | pathlib.Path = DEFAULT_KNOWN_HOSTS_PATH,
) -> str | None:
    endpoint_key = known_host_key(host, port)
    normalized_fingerprint = format_fingerprint(fingerprint)
    known_hosts = load_known_hosts(path)
    existing = known_hosts.get(endpoint_key)
    if existing is None:
        known_hosts[endpoint_key] = normalized_fingerprint
        save_known_hosts(known_hosts, path)
        return None
    if existing == normalized_fingerprint:
        return None
    return (
        f"Trusted host changed for {endpoint_key}. "
        "Clear trusted hosts in Settings if this is expected."
    )


def _configure_context(context: SSL.Context):
    context.set_verify(SSL.VERIFY_NONE, lambda *_args: True)
    context.set_cipher_list(b"DEFAULT")


def _client_context() -> SSL.Context:
    context = SSL.Context(SSL.DTLS_CLIENT_METHOD)
    _configure_context(context)
    return context


def _server_context(cert_file: pathlib.Path, key_file: pathlib.Path) -> SSL.Context:
    context = SSL.Context(SSL.DTLS_SERVER_METHOD)
    _configure_context(context)
    context.use_certificate_file(str(cert_file))
    context.use_privatekey_file(str(key_file))
    context.check_privatekey()
    return context


class _DtlsEndpoint:
    def __init__(self, context: SSL.Context, *, server_side: bool):
        self.connection = SSL.Connection(context, None)
        self.connection.set_ciphertext_mtu(DTLS_DEFAULT_CIPHERTEXT_MTU)
        if server_side:
            self.connection.set_accept_state()
        else:
            self.connection.set_connect_state()
        self.server_side = server_side
        self.handshake_complete = False
        self.closed = False
        self.last_error: str | None = None
        self.created_at = time.monotonic()
        self.last_activity = self.created_at
        self._outbound = deque()
        self._incoming = deque()

    def start(self):
        self._pump()

    def close(self):
        self.closed = True

    def expired(self, *, now: float, handshake_timeout: float, idle_timeout: float) -> bool:
        if self.handshake_complete:
            return now - self.last_activity > idle_timeout
        return now - self.created_at > handshake_timeout

    def feed_datagram(self, data: bytes):
        if self.closed:
            return
        self.last_activity = time.monotonic()
        try:
            self.connection.bio_write(data)
        except SSL.Error as exc:
            self._fail(f"DTLS bio write failed: {exc}")
            return
        self._pump()

    def send_appdata(self, data: bytes):
        if self.closed:
            raise RuntimeError(self.last_error or "DTLS transport is closed.")
        if not self.handshake_complete:
            raise RuntimeError("DTLS handshake is not complete.")
        try:
            self.connection.send(data)
        except (SSL.WantReadError, SSL.WantWriteError):
            pass
        except SSL.Error as exc:
            self._fail(f"DTLS send failed: {exc}")
            raise RuntimeError(self.last_error or "DTLS send failed.") from exc
        self._drain_outbound()

    def poll(self):
        if self.closed:
            return
        try:
            timeout = self.connection.DTLSv1_get_timeout()
        except SSL.Error as exc:
            self._fail(f"DTLS timeout query failed: {exc}")
            return
        if timeout is not None and timeout <= 0:
            try:
                self.connection.DTLSv1_handle_timeout()
            except SSL.Error as exc:
                self._fail(f"DTLS timeout handling failed: {exc}")
                return
            self._pump()
        else:
            self._drain_outbound()

    def pop_outbound(self) -> list[bytes]:
        data = list(self._outbound)
        self._outbound.clear()
        return data

    def pop_incoming(self) -> list[bytes]:
        data = list(self._incoming)
        self._incoming.clear()
        return data

    def peer_fingerprint(self) -> str | None:
        if not self.handshake_complete:
            return None
        cert = self.connection.get_peer_certificate()
        if cert is None:
            return None
        return fingerprint_for_certificate(cert)

    def _pump(self):
        if self.closed:
            return

        if not self.handshake_complete:
            try:
                self.connection.do_handshake()
                self.handshake_complete = True
            except (SSL.WantReadError, SSL.WantWriteError):
                pass
            except SSL.Error as exc:
                self._fail(f"DTLS handshake failed: {exc}")
                return
            self._drain_outbound()

        if self.handshake_complete:
            self._drain_plaintext()

    def _drain_outbound(self):
        while not self.closed:
            try:
                datagram = self.connection.bio_read(DTLS_BIO_BUFFER_SIZE)
            except SSL.WantReadError:
                break
            except SSL.Error as exc:
                self._fail(f"DTLS outbound drain failed: {exc}")
                break
            if not datagram:
                break
            self._outbound.append(datagram)

    def _drain_plaintext(self):
        while not self.closed:
            try:
                data = self.connection.recv(DTLS_APPDATA_BUFFER_SIZE)
            except SSL.WantReadError:
                break
            except SSL.ZeroReturnError:
                self._fail("DTLS peer closed the connection.")
                break
            except SSL.Error as exc:
                self._fail(f"DTLS receive failed: {exc}")
                break
            if not data:
                break
            self.last_activity = time.monotonic()
            self._incoming.append(data)
        self._drain_outbound()

    def _fail(self, message: str):
        self.closed = True
        self.last_error = message


class DtlsClientTransport:
    def __init__(self):
        self._endpoint = _DtlsEndpoint(_client_context(), server_side=False)

    @property
    def handshake_complete(self) -> bool:
        return self._endpoint.handshake_complete

    @property
    def closed(self) -> bool:
        return self._endpoint.closed

    @property
    def last_error(self) -> str | None:
        return self._endpoint.last_error

    def start(self):
        self._endpoint.start()

    def close(self):
        self._endpoint.close()

    def feed_datagram(self, data: bytes):
        self._endpoint.feed_datagram(data)

    def send_packet(self, data: bytes):
        self._endpoint.send_appdata(data)

    def poll(self):
        self._endpoint.poll()

    def drain_outbound(self) -> list[bytes]:
        return self._endpoint.pop_outbound()

    def drain_packets(self) -> list[bytes]:
        return self._endpoint.pop_incoming()

    def peer_fingerprint(self) -> str | None:
        return self._endpoint.peer_fingerprint()


class DtlsServerTransport:
    def __init__(
        self,
        cert_file: str | pathlib.Path,
        key_file: str | pathlib.Path,
        *,
        handshake_timeout: float = DTLS_HANDSHAKE_TIMEOUT_SECS,
        idle_timeout: float = DTLS_IDLE_TIMEOUT_SECS,
    ):
        self.cert_file = pathlib.Path(cert_file).expanduser().resolve()
        self.key_file = pathlib.Path(key_file).expanduser().resolve()
        self.handshake_timeout = handshake_timeout
        self.idle_timeout = idle_timeout
        self._context = _server_context(self.cert_file, self.key_file)
        self._peers: dict[tuple, _DtlsEndpoint] = {}

    def close(self):
        self._peers.clear()

    def remove_peer(self, addr: tuple):
        self._peers.pop(addr, None)

    def has_peer(self, addr: tuple) -> bool:
        return addr in self._peers

    def handshake_complete(self, addr: tuple) -> bool:
        peer = self._peers.get(addr)
        return peer.handshake_complete if peer is not None else False

    def receive_datagram(self, addr: tuple, data: bytes):
        peer = self._peers.get(addr)
        if peer is None:
            peer = _DtlsEndpoint(self._context, server_side=True)
            self._peers[addr] = peer
        peer.feed_datagram(data)

    def send_packet(self, addr: tuple, data: bytes):
        peer = self._peers.get(addr)
        if peer is None:
            raise RuntimeError(f"No DTLS peer registered for {addr}.")
        peer.send_appdata(data)

    def poll(self):
        now = time.monotonic()
        expired = []
        for addr, peer in self._peers.items():
            peer.poll()
            if peer.closed or peer.expired(
                now=now,
                handshake_timeout=self.handshake_timeout,
                idle_timeout=self.idle_timeout,
            ):
                expired.append(addr)
        for addr in expired:
            self._peers.pop(addr, None)

    def drain_outbound(self) -> list[tuple[tuple, bytes]]:
        outbound: list[tuple[tuple, bytes]] = []
        for addr, peer in self._peers.items():
            for datagram in peer.pop_outbound():
                outbound.append((addr, datagram))
        return outbound

    def drain_packets(self) -> list[tuple[tuple, bytes]]:
        packets: list[tuple[tuple, bytes]] = []
        for addr, peer in self._peers.items():
            for packet in peer.pop_incoming():
                packets.append((addr, packet))
        return packets
