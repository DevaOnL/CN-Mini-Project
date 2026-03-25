"""
Main game client — connects to the server, sends inputs, receives snapshots,
and drives prediction, reconciliation, interpolation, and the GUI.
"""

import bisect
import ipaddress
import socket as _socket
import struct
import subprocess
import time
from collections import deque
from enum import Enum, auto

from common.packet import (
    Packet,
    PacketType,
    HEADER_SIZE,
    INPUT_FORMAT,
    PING_FORMAT,
    PING_SIZE,
    CONNECTION_EPOCH_SIZE,
    HANDSHAKE_DISCONNECT_FORMAT,
    HANDSHAKE_DISCONNECT_SIZE,
    DISCONNECT_REASON_FORMAT,
    DISCONNECT_REASON_SIZE,
    DISCONNECT_REASON_NONE,
    DISCONNECT_REASON_KICKED,
    DISCONNECT_REASON_SECURE_REQUIRED,
    DISCONNECT_REASON_AUTH_FAILED,
    packet_requires_encryption,
    packet_uses_connection_epoch,
    pack_connection_epoch,
    unpack_connection_epoch,
)
from common.net import create_client_socket, AckTracker, NetworkSimulator
from common.security import (
    SECURE_PROTOCOL_VERSION,
    SECURE_HELLO_ACK_FORMAT,
    SECURE_HELLO_ACK_SIZE,
    SECURE_HELLO_FORMAT,
    build_client_proof,
    decrypt_payload,
    derive_room_psk,
    derive_session_key,
    encrypt_payload,
    generate_handshake_nonce,
    verify_server_proof,
)
from common.snapshot import Snapshot
from common.config import (
    DEFAULT_PORT,
    DEFAULT_TICK_RATE,
    DEFAULT_BUFFER_SIZE,
    PING_INTERVAL,
    INPUT_REDUNDANCY,
    INTERPOLATION_TICKS,
    CONNECT_RETRY_INTERVAL,
    DASH_COOLDOWN_REDUCTION_FACTOR,
    EFFECT_FLAG_DASH_COOLDOWN,
    WORLD_WIDTH,
    WORLD_HEIGHT,
    RELIABLE_MAX_RETRIES,
    RELIABLE_RETRY_INTERVAL,
)
from common.metrics_logger import MetricsLogger
from client.prediction import Predictor
from client.reconciliation import Reconciler, smooth_correction
from client.interpolation import Interpolator
from client.gui.config_store import normalize_config
from client.gui.validation import suggested_ipv4_correction


CONNECT_TOKEN_SIZE = 16
CONNECT_REQ_FORMAT = f"!{CONNECT_TOKEN_SIZE}sI"
CONNECT_ACK_FORMAT = f"!HI{CONNECT_TOKEN_SIZE}sI"
CONNECT_NONCE_SIZE = struct.calcsize("!I")
SNAPSHOT_TRAILER_FORMAT = "!IdfH"
SNAPSHOT_TRAILER_SIZE = struct.calcsize(SNAPSHOT_TRAILER_FORMAT)
RELIABLE_EVENT_FORMAT = "!BH"
RELIABLE_EVENT_JOIN = 0x01
RELIABLE_EVENT_LEAVE = 0x02
RELIABLE_EVENT_GAME_START = 0x03
RELIABLE_EVENT_SCORE_UPDATE = 0x04
RELIABLE_EVENT_SCORE_SYNC = 0x05
RELIABLE_EVENT_MATCH_OVER = 0x06
RELIABLE_EVENT_MATCH_RESET = 0x07
RELIABLE_EVENT_KICK_PLAYER = 0x08
RELIABLE_EVENT_KICKED = 0x09
RELIABLE_SCORE_EVENT_FORMAT = "!BHH"


class ConnState(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    RECONNECTING = auto()


class ReliableChannel:
    """
    Retransmit reliable UDP packets until their packet-header sequence is acked.
    Used for RELIABLE_EVENT packets without changing the existing transport.
    """

    WINDOW = 4
    RETRY_INTERVAL = RELIABLE_RETRY_INTERVAL
    MAX_RETRIES = RELIABLE_MAX_RETRIES

    def __init__(self, send_fn, on_send=None):
        self._send = send_fn
        self._on_send = on_send
        self._pending: dict[int, dict] = {}
        self._queued = deque()

    def send(self, data: bytes, addr: tuple) -> int:
        seq = self._extract_sequence(data)
        if seq in self._pending:
            return seq

        if any(entry["seq"] == seq for entry in self._queued):
            return seq

        entry = {
            "seq": seq,
            "data": data,
            "addr": addr,
            "tries": 0,
            "last_sent": 0.0,
        }

        if len(self._pending) >= self.WINDOW:
            self._queued.append(entry)
            return seq

        self._pending[seq] = entry
        self._flush_one(seq)
        return seq

    def ack(self, seq: int):
        self._pending.pop(seq, None)
        self._queued = deque(entry for entry in self._queued if entry["seq"] != seq)
        self._promote_queued()

    def reset(self):
        self._pending.clear()
        self._queued.clear()

    def tick(self):
        now = time.perf_counter()
        for seq, entry in list(self._pending.items()):
            if now - entry["last_sent"] < self.RETRY_INTERVAL:
                continue
            if entry["tries"] >= self.MAX_RETRIES:
                del self._pending[seq]
                continue
            self._flush_one(seq)
        self._promote_queued()

    def _flush_one(self, seq: int):
        entry = self._pending[seq]
        if self._on_send is not None:
            self._on_send(seq)
        self._send(entry["data"], entry["addr"])
        entry["tries"] += 1
        entry["last_sent"] = time.perf_counter()

    def _promote_queued(self):
        while self._queued and len(self._pending) < self.WINDOW:
            entry = self._queued.popleft()
            seq = entry["seq"]
            if seq in self._pending:
                continue
            self._pending[seq] = entry
            self._flush_one(seq)

    @staticmethod
    def _extract_sequence(data: bytes) -> int:
        try:
            return Packet.deserialize(data).sequence
        except ValueError:
            return 0


class GameClient:
    """Authoritative-networked client with prediction and GUI support."""

    def __init__(
        self,
        server_host: str = "127.0.0.1",
        server_port: int = DEFAULT_PORT,
        tick_rate: int = DEFAULT_TICK_RATE,
        headless: bool = False,
        loss_sim: float = 0.0,
        latency_sim: float = 0.0,
        room_key: str | None = None,
    ):
        self.server_host = server_host
        self.server_port = server_port
        self.server_addr = (server_host, server_port)
        self.tick_rate = tick_rate
        self.dt = 1.0 / tick_rate
        self.running = False
        self.headless = headless

        self.sock = create_client_socket()

        self.net_sim = None
        if loss_sim > 0 or latency_sim > 0:
            self.net_sim = NetworkSimulator(
                self.sock,
                loss_rate=loss_sim,
                min_latency=latency_sim * 0.5,
                max_latency=latency_sim * 1.5,
            )

        self.client_id: int | None = None
        self.session_token: str | None = None
        self.connection_epoch = 0
        self.connect_nonce = 0
        self._next_connect_nonce = 1
        self.conn_state = ConnState.DISCONNECTED
        self.last_connection_error: str | None = None
        self.ui_notice: str | None = None
        self.pending_server_disconnect_notice: str | None = None
        self.last_packet_recv_time = time.perf_counter()
        self._last_sent_time = time.perf_counter()
        self._last_connect_attempt_time = 0.0

        self.ack_tracker = AckTracker()
        self.reliable_channel = ReliableChannel(
            self._sendto, self.ack_tracker.on_packet_sent
        )

        self.local_state: dict = {}
        self.visual_state: dict = {}
        self.server_snapshots: list[Snapshot] = []
        self.snapshot_recv_times: dict[int, float] = {}
        self.pending_inputs: list[dict] = []
        self.input_sequence = 0
        self.input_history: list[dict] = []
        self.recent_events: list[dict] = []
        self.game_started_by_server = False
        self.scores: dict[int, int] = {}
        self.match_winner_id: int | None = None
        self.match_elapsed = 0.0
        self.min_server_packet_seq = 0
        self.min_server_event_seq = 0
        self.previous_connection_epoch: int | None = None
        self.pre_reconnect_server_seq: int | None = None
        self.strict_server_sequence_until = 0.0
        self.awaiting_phase_sync = False

        self.predictor = Predictor(dt=self.dt)
        self.reconciler = Reconciler(self.predictor)
        self.interpolator = Interpolator(
            tick_rate=tick_rate, interp_ticks=INTERPOLATION_TICKS
        )

        self.metrics = MetricsLogger()
        self.rtt_samples: list[float] = []
        self.current_rtt = 0.0
        self.current_jitter = 0.0
        self.last_server_tick = 0
        self.last_server_send_time = 0.0
        self.server_host_client_id: int | None = None

        self.total_bytes_sent = 0
        self.total_bytes_recv = 0

        self.last_acked_dash_cooldown = 0.0
        self.last_acked_dash_timer = 0.0

        self._predict_accumulator = 0.0
        now = time.perf_counter()
        self._next_send_time = now
        self._next_ping_time = now + PING_INTERVAL

        self.target_fps = 60
        self.current_fps = 0.0
        self.player_name = "Player"
        self.show_debug_stats = True
        self.host_server_proc = None
        self.host_mode = False
        self.room_key = ""
        self._room_psk: bytes | None = None
        self._session_key: bytes | None = None
        self._pending_secure_client_nonce: bytes | None = None
        self._pending_secure_session_key: bytes | None = None
        self.set_room_key(room_key)

    @property
    def connected(self) -> bool:
        return self.conn_state == ConnState.CONNECTED

    @property
    def secure_required(self) -> bool:
        return self._room_psk is not None

    def set_room_key(self, room_key: str | None):
        normalized = (room_key or "").strip()
        self.room_key = normalized
        self._room_psk = derive_room_psk(normalized) if normalized else None
        self._reset_secure_state(clear_established=True)

    def _reset_secure_state(self, clear_established: bool = True):
        self._pending_secure_client_nonce = None
        self._pending_secure_session_key = None
        if clear_established:
            self._session_key = None

    def apply_settings(self, settings: dict, update_connection: bool = True):
        settings = normalize_config(settings)
        if update_connection:
            self.server_host = (
                str(settings.get("host", self.server_host)).strip() or self.server_host
            )
            self.server_port = int(settings.get("port", self.server_port))
            self.server_addr = (self.server_host, self.server_port)
        self.player_name = str(settings.get("name", self.player_name))[:16] or "Player"
        self.target_fps = max(10, min(240, int(settings.get("fps", self.target_fps))))
        interp_ms = max(
            0,
            min(
                500,
                int(
                    settings.get("interp_ms", int(self.dt * INTERPOLATION_TICKS * 1000))
                ),
            ),
        )
        interp_ticks = max(0, int(round((interp_ms / 1000.0) / self.dt)))
        self.interpolator.interp_ticks = interp_ticks
        self.show_debug_stats = bool(settings.get("show_debug", self.show_debug_stats))

    def stop_host_server(self, wait_timeout: float = 2.0):
        proc = self.host_server_proc
        self.host_server_proc = None
        if proc is None:
            return
        if proc.poll() is not None:
            return

        try:
            proc.terminate()
        except OSError:
            return
        try:
            proc.wait(timeout=wait_timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                return
            try:
                proc.wait(timeout=wait_timeout)
            except subprocess.TimeoutExpired:
                pass

    def begin_new_session(self):
        self.disconnect(clear_session_token=True)

    def _advance_gameplay_ticks(self, dt: float, inp: dict, allow_gameplay_input: bool):
        if not self.game_started_by_server or not allow_gameplay_input:
            self._predict_accumulator = 0.0
            return

        self._predict_accumulator += dt
        while self._predict_accumulator >= self.dt:
            self.predict_local(inp)
            if self.connected:
                self.send_input(inp)
            self._predict_accumulator -= self.dt

    def _reset_world_state(self):
        self.local_state = {}
        self.visual_state = {}
        self.server_snapshots = []
        self.snapshot_recv_times = {}
        self.pending_inputs = []
        # Use deque with maxlen to automatically prune old inputs and avoid memory bloat
        self.input_history = deque(maxlen=INPUT_REDUNDANCY * 2)
        self.input_sequence = 0
        self.recent_events = []
        self.game_started_by_server = False
        self.scores = {}
        self.match_winner_id = None
        self.match_elapsed = 0.0
        self.last_server_tick = 0
        self.last_server_send_time = 0.0
        self.server_host_client_id = None
        self.last_acked_dash_cooldown = 0.0
        self.last_acked_dash_timer = 0.0
        self._predict_accumulator = 0.0
        self.reliable_channel.reset()

    def _sendto(self, data: bytes, addr: tuple | None = None):
        addr = addr or self.server_addr
        if self.net_sim:
            self.net_sim.sendto(data, addr)
        else:
            try:
                self.sock.sendto(data, addr)
            except (BlockingIOError, OSError):
                pass

        self.total_bytes_sent += len(data)
        self._last_sent_time = time.perf_counter()

    def _replace_socket(self):
        old_sock = self.sock
        self.sock = create_client_socket()
        if self.net_sim is not None:
            self.net_sim = NetworkSimulator(
                self.sock,
                loss_rate=self.net_sim.loss_rate,
                min_latency=self.net_sim.min_latency,
                max_latency=self.net_sim.max_latency,
            )
        try:
            old_sock.close()
        except OSError:
            pass

    def _sendto_immediate(self, data: bytes, addr: tuple | None = None):
        addr = addr or self.server_addr
        try:
            self.sock.sendto(data, addr)
        except (BlockingIOError, OSError):
            pass

        self.total_bytes_sent += len(data)
        self._last_sent_time = time.perf_counter()

    def _build_packet(
        self, packet_type: int, payload: bytes = b"", track_send: bool = True
    ) -> tuple[int, bytes]:
        if packet_uses_connection_epoch(packet_type) and self.connection_epoch:
            payload = pack_connection_epoch(self.connection_epoch, payload)

        seq = self.ack_tracker.next_sequence()
        pkt = Packet(
            packet_type,
            seq,
            self.ack_tracker.remote_sequence,
            self.ack_tracker.ack_bitfield,
            payload,
        )
        if packet_requires_encryption(packet_type) and self._session_key is not None:
            encrypted_length = len(payload) + 28
            header = pkt.serialize_header(encrypted_length)
            pkt.payload = encrypt_payload(self._session_key, header, payload)
            data = header + pkt.payload
        elif packet_requires_encryption(packet_type) and self.secure_required:
            raise RuntimeError(
                f"Secure packet {PacketType.name(packet_type)} has no session key."
            )
        else:
            data = pkt.serialize()
        if track_send:
            self.ack_tracker.on_packet_sent(seq)
        return seq, data

    def _send_packet(self, packet_type: int, payload: bytes = b"") -> int:
        seq, data = self._build_packet(packet_type, payload)
        self._sendto(data)
        return seq

    def _ack_reliable_sequences(self, ack_seq: int, ack_bitfield: int):
        self.reliable_channel.ack(ack_seq)
        for i in range(32):
            if ack_bitfield & (1 << i):
                self.reliable_channel.ack((ack_seq - 1 - i) & 0xFFFF)

    def _local_loopback_host(self, host: str) -> str | None:
        normalized = host.strip().lower()
        if not normalized:
            return None

        local_aliases = {
            "localhost",
            "localhost.localdomain",
            "0.0.0.0",
            "::",
            "[::]",
        }
        try:
            local_aliases.add(_socket.gethostname().strip().lower())
        except OSError:
            pass
        try:
            local_aliases.add(_socket.getfqdn().strip().lower())
        except OSError:
            pass

        if normalized in local_aliases:
            return "127.0.0.1"
        return None

    def _resolve_server_addr(self) -> tuple[tuple | None, str | None]:
        host = self.server_host.strip()
        if not host:
            return None, "Server host cannot be empty."

        loopback_host = self._local_loopback_host(host)
        if loopback_host is not None:
            return (loopback_host, self.server_port), None

        try:
            ipv4 = ipaddress.IPv4Address(host)
        except ipaddress.AddressValueError:
            ipv4 = None
        if ipv4 is not None:
            return (str(ipv4), self.server_port), None

        try:
            resolved = _socket.getaddrinfo(
                host,
                self.server_port,
                _socket.AF_UNSPEC,
                _socket.SOCK_DGRAM,
            )
        except _socket.gaierror as exc:
            suggestion = suggested_ipv4_correction(host)
            if suggestion is not None:
                return (
                    None,
                    f"Invalid IPv4 address '{self.server_host}'. Did you mean '{suggestion}'?",
                )
            return None, f"DNS resolution failed for {self.server_host}: {exc}"

        for family, _socktype, _proto, _canonname, sockaddr in resolved:
            if family == _socket.AF_INET:
                ip_addr, port = sockaddr[:2]
                if ip_addr == "0.0.0.0":
                    ip_addr = "127.0.0.1"
                return (ip_addr, port), None

        if any(family == _socket.AF_INET6 for family, *_rest in resolved):
            return (
                None,
                f"{self.server_host} resolves only to IPv6, but this client currently uses IPv4 UDP sockets.",
            )

        return None, f"Could not resolve an IPv4 address for {self.server_host}."

    def connect(self):
        now = time.perf_counter()
        if (
            self.conn_state in (ConnState.CONNECTING, ConnState.RECONNECTING)
            and now - self._last_connect_attempt_time < CONNECT_RETRY_INTERVAL
        ):
            return True

        resolved_addr, error = self._resolve_server_addr()
        if error is not None or resolved_addr is None:
            self.last_connection_error = error or "Could not resolve server address."
            self.conn_state = ConnState.DISCONNECTED
            print(f"[CLIENT] {self.last_connection_error}")
            return False
        self.server_addr = resolved_addr

        if self.conn_state == ConnState.RECONNECTING:
            self.previous_connection_epoch = self.connection_epoch
            self.pre_reconnect_server_seq = (
                self.ack_tracker.remote_sequence
                if self.ack_tracker.has_remote_sequence
                else None
            )
            self.reliable_channel.reset()
            self.ack_tracker.reset()
            self.connection_epoch = 0
            self.min_server_packet_seq = 0
            self.min_server_event_seq = 0

        self._reset_secure_state(clear_established=True)

        self.last_connection_error = None
        self.ui_notice = None
        self.pending_server_disconnect_notice = None

        if self.conn_state != ConnState.RECONNECTING:
            self.conn_state = ConnState.CONNECTING

        if self.secure_required:
            if self._room_psk is None:
                self.last_connection_error = "Room key is required for secure play."
                self.conn_state = ConnState.DISCONNECTED
                return False
            self._pending_secure_client_nonce = generate_handshake_nonce()
            payload = struct.pack(
                SECURE_HELLO_FORMAT,
                SECURE_PROTOCOL_VERSION,
                self._pending_secure_client_nonce,
                build_client_proof(self._room_psk, self._pending_secure_client_nonce),
            )
            self._send_packet(PacketType.SECURE_HELLO, payload)
        else:
            self._send_connect_request()
        self._last_connect_attempt_time = now
        self._next_send_time = time.perf_counter()
        self._next_ping_time = time.perf_counter() + PING_INTERVAL
        print(f"[CLIENT] Connecting to {self.server_addr}...")
        return True

    def _send_connect_request(self):
        token_bytes = b""
        if self.session_token:
            token_bytes = self.session_token.encode("ascii")
            if len(token_bytes) > CONNECT_TOKEN_SIZE:
                raise ValueError(
                    f"Session token too long: {len(token_bytes)} > {CONNECT_TOKEN_SIZE}"
                )
        token_bytes = token_bytes.ljust(CONNECT_TOKEN_SIZE, b"\x00")
        self.connect_nonce = self._next_connect_nonce
        self._next_connect_nonce = (self._next_connect_nonce + 1) & 0xFFFFFFFF or 1
        payload = struct.pack(CONNECT_REQ_FORMAT, token_bytes, self.connect_nonce)
        self._send_packet(PacketType.CONNECT_REQ, payload)

    def _reset_connection_state(self, clear_notice: bool = True):
        self.conn_state = ConnState.DISCONNECTED
        self.client_id = None
        self.connection_epoch = 0
        self.connect_nonce = 0
        self._last_connect_attempt_time = 0.0
        self._reset_secure_state(clear_established=True)
        if clear_notice:
            self.last_connection_error = None
            self.ui_notice = None
        self.pending_server_disconnect_notice = None
        self.min_server_packet_seq = 0
        self.min_server_event_seq = 0
        self.previous_connection_epoch = None
        self.pre_reconnect_server_seq = None
        self.strict_server_sequence_until = 0.0
        self.awaiting_phase_sync = False
        self.ack_tracker.reset()
        self.rtt_samples = []
        self.current_rtt = 0.0
        self.current_jitter = 0.0
        self.last_packet_recv_time = time.perf_counter()
        self._reset_world_state()

    def disconnect(self, close_socket: bool = False, clear_session_token: bool = True):
        if self.connected:
            _seq, data = self._build_packet(PacketType.DISCONNECT)
            self._sendto_immediate(data)
            print("[CLIENT] Disconnected")
        elif self.conn_state in (ConnState.CONNECTING, ConnState.RECONNECTING):
            if self.connect_nonce:
                token_bytes = b""
                if self.session_token:
                    token_bytes = self.session_token.encode("ascii")[
                        :CONNECT_TOKEN_SIZE
                    ]
                payload = struct.pack(
                    HANDSHAKE_DISCONNECT_FORMAT,
                    token_bytes.ljust(CONNECT_TOKEN_SIZE, b"\x00"),
                    self.connect_nonce,
                    DISCONNECT_REASON_NONE,
                )
            else:
                payload = b""

            if payload:
                self._sendto_immediate(
                    Packet(PacketType.DISCONNECT, payload=payload).serialize()
                )

        if clear_session_token and not close_socket:
            self._replace_socket()

        if clear_session_token:
            self.session_token = None
        self._reset_connection_state(clear_notice=True)

        if close_socket:
            try:
                self.sock.close()
            except OSError:
                pass

    def receive_packets(self):
        # Use time budget instead of fixed count to prevent packet starvation
        deadline = time.perf_counter() + 0.02  # 20ms max per tick
        packets_processed = 0
        while time.perf_counter() < deadline and packets_processed < 10000:
            try:
                data, addr = self.sock.recvfrom(DEFAULT_BUFFER_SIZE)
                self.total_bytes_recv += len(data)
                self._handle_packet(data, addr)
                packets_processed += 1
            except BlockingIOError:
                break
            except OSError:
                break

        self._flush_pending_server_disconnect()

    def _handle_packet(self, data: bytes, addr: tuple | None = None):
        if addr is not None and addr != self.server_addr:
            return

        try:
            pkt = Packet.deserialize(data)
        except ValueError:
            return

        if (
            self.pending_server_disconnect_notice
            and pkt.packet_type != PacketType.DISCONNECT
        ):
            return

        if self.conn_state == ConnState.DISCONNECTED:
            return

        if self.conn_state in (ConnState.CONNECTING, ConnState.RECONNECTING):
            if pkt.packet_type not in (
                PacketType.SECURE_HELLO_ACK,
                PacketType.CONNECT_ACK,
                PacketType.DISCONNECT,
            ):
                return

        allow_cleartext_connect_disconnect = (
            pkt.packet_type == PacketType.DISCONNECT
            and self.conn_state in (ConnState.CONNECTING, ConnState.RECONNECTING)
            and self._session_key is None
        )
        if packet_requires_encryption(pkt.packet_type) and self._session_key is not None:
            try:
                pkt.payload = decrypt_payload(
                    self._session_key,
                    data[:HEADER_SIZE],
                    pkt.payload,
                )
            except ValueError:
                return
        elif (
            packet_requires_encryption(pkt.packet_type)
            and self.secure_required
            and not allow_cleartext_connect_disconnect
        ):
            return

        if self.connected and packet_uses_connection_epoch(pkt.packet_type):
            unpacked = unpack_connection_epoch(pkt.payload)
            if unpacked is None:
                return
            packet_epoch, payload = unpacked
            if packet_epoch != self.connection_epoch:
                return
            pkt.payload = payload

        if (
            self.conn_state in (ConnState.CONNECTING, ConnState.RECONNECTING)
            and pkt.packet_type == PacketType.DISCONNECT
        ):
            if len(pkt.payload) >= HANDSHAKE_DISCONNECT_SIZE:
                _token_bytes, connect_nonce, reason_code = struct.unpack(
                    HANDSHAKE_DISCONNECT_FORMAT,
                    pkt.payload[:HANDSHAKE_DISCONNECT_SIZE],
                )
                if connect_nonce != self.connect_nonce:
                    return
                pkt.payload = struct.pack(DISCONNECT_REASON_FORMAT, reason_code)
            elif len(pkt.payload) >= CONNECTION_EPOCH_SIZE:
                unpacked = unpack_connection_epoch(pkt.payload)
                if unpacked is None:
                    return
                packet_epoch, payload = unpacked
                if (
                    self.previous_connection_epoch is None
                    or packet_epoch != self.previous_connection_epoch
                ):
                    return
                pkt.payload = payload
            elif self.connect_nonce != 0:
                return

        if (
            self.conn_state == ConnState.CONNECTED
            and pkt.packet_type != PacketType.CONNECT_ACK
            and self.min_server_packet_seq
            and (
                pkt.sequence == self.min_server_packet_seq
                or not AckTracker._sequence_greater_than(
                    pkt.sequence, self.min_server_packet_seq
                )
            )
        ):
            return

        if (
            self.conn_state == ConnState.CONNECTED
            and pkt.packet_type != PacketType.CONNECT_ACK
            and time.perf_counter() < self.strict_server_sequence_until
            and self.ack_tracker.has_remote_sequence
            and AckTracker._sequence_greater_than(
                pkt.sequence, self.ack_tracker.remote_sequence
            )
            and ((pkt.sequence - self.ack_tracker.remote_sequence) & 0xFFFF) > 64
        ):
            return

        self.last_packet_recv_time = time.perf_counter()
        is_duplicate = self.ack_tracker.is_duplicate(pkt.sequence)

        if not is_duplicate and pkt.packet_type == PacketType.SECURE_HELLO_ACK:
            self._handle_secure_hello_ack(pkt)
        elif not is_duplicate and pkt.packet_type == PacketType.CONNECT_ACK:
            self._handle_connect_ack(pkt)
        elif not is_duplicate and pkt.packet_type == PacketType.DISCONNECT:
            self._handle_disconnect_packet(pkt)
            return
        elif not is_duplicate and pkt.packet_type == PacketType.SNAPSHOT:
            self._handle_snapshot(pkt)
        elif not is_duplicate and pkt.packet_type == PacketType.PONG:
            self._handle_pong(pkt)
        elif not is_duplicate and pkt.packet_type == PacketType.HEARTBEAT:
            self._handle_heartbeat(pkt)
        elif not is_duplicate and pkt.packet_type == PacketType.RELIABLE_EVENT:
            self._handle_reliable_event(pkt)

        self.ack_tracker.on_packet_received(pkt.sequence)
        if self.ack_tracker.should_process_ack(pkt.ack, pkt.ack_bitfield):
            self.ack_tracker.on_ack_received(pkt.ack, pkt.ack_bitfield)
            self._ack_reliable_sequences(pkt.ack, pkt.ack_bitfield)

    def _handle_secure_hello_ack(self, pkt: Packet):
        if not self.secure_required or self._room_psk is None:
            return
        if self._pending_secure_client_nonce is None:
            return
        if len(pkt.payload) < SECURE_HELLO_ACK_SIZE:
            return

        version, server_nonce, server_proof = struct.unpack(
            SECURE_HELLO_ACK_FORMAT,
            pkt.payload[:SECURE_HELLO_ACK_SIZE],
        )
        if version != SECURE_PROTOCOL_VERSION:
            self.last_connection_error = "Secure server version mismatch."
            self.ui_notice = self.last_connection_error
            self._reset_connection_state(clear_notice=False)
            return
        if not verify_server_proof(
            self._room_psk,
            self._pending_secure_client_nonce,
            server_nonce,
            server_proof,
        ):
            self.last_connection_error = "Room key rejected by server."
            self.ui_notice = self.last_connection_error
            self._reset_connection_state(clear_notice=False)
            return

        self._pending_secure_session_key = derive_session_key(
            self._room_psk,
            self._pending_secure_client_nonce,
            server_nonce,
        )
        self._session_key = self._pending_secure_session_key
        self._send_connect_request()

    def _handle_connect_ack(self, pkt: Packet):
        if len(pkt.payload) >= struct.calcsize(CONNECT_ACK_FORMAT):
            client_id, connection_epoch, token_bytes, connect_nonce = struct.unpack(
                CONNECT_ACK_FORMAT, pkt.payload[: struct.calcsize(CONNECT_ACK_FORMAT)]
            )

            if connect_nonce != self.connect_nonce:
                return

            self.client_id = client_id
            self.connection_epoch = connection_epoch
            self.session_token = (
                token_bytes.decode("ascii", errors="ignore").rstrip("\x00")
                or self.session_token
            )
            self._reset_world_state()
            self.awaiting_phase_sync = True
            self.conn_state = ConnState.CONNECTED
            self.last_connection_error = None
            now = time.perf_counter()
            self._next_send_time = now
            self._next_ping_time = now + PING_INTERVAL
            self.min_server_packet_seq = pkt.sequence
            if (
                self.pre_reconnect_server_seq is not None
                and not AckTracker._sequence_greater_than(
                    pkt.sequence, self.pre_reconnect_server_seq
                )
            ):
                self.strict_server_sequence_until = time.perf_counter() + 1.0
            else:
                self.strict_server_sequence_until = 0.0
            self.previous_connection_epoch = None
            self.pre_reconnect_server_seq = None
            self.connect_nonce = 0
            self._pending_secure_client_nonce = None
            self._pending_secure_session_key = None
            if self.min_server_event_seq == 0:
                self.min_server_event_seq = pkt.sequence
            print(f"[CLIENT] Connected! Assigned ID: {self.client_id}")

    def phase_sync_pending(self) -> bool:
        return self.awaiting_phase_sync

    def _handle_disconnect_packet(self, pkt: Packet):
        reason_code = DISCONNECT_REASON_NONE
        if len(pkt.payload) >= DISCONNECT_REASON_SIZE:
            reason_code = struct.unpack(
                DISCONNECT_REASON_FORMAT, pkt.payload[:DISCONNECT_REASON_SIZE]
            )[0]

        retry_without_token = (
            reason_code == DISCONNECT_REASON_NONE
            and self.conn_state in (ConnState.CONNECTING, ConnState.RECONNECTING)
            and self.session_token is not None
        )

        if reason_code == DISCONNECT_REASON_KICKED:
            notice = "You were kicked by the host."
        elif reason_code == DISCONNECT_REASON_SECURE_REQUIRED:
            notice = "Server requires a room key before joining."
        elif reason_code == DISCONNECT_REASON_AUTH_FAILED:
            notice = "Room key rejected by server."
        else:
            notice = "Disconnected by server."

        if retry_without_token:
            self.session_token = None
            self._reset_connection_state(clear_notice=True)
            self.connect()
            return

        if reason_code == DISCONNECT_REASON_KICKED:
            self.session_token = None

        self.last_connection_error = notice
        self.ui_notice = notice
        self._reset_connection_state(clear_notice=False)

    def _flush_pending_server_disconnect(self):
        if not self.pending_server_disconnect_notice:
            return

        notice = self.pending_server_disconnect_notice
        if self.connected:
            for _ in range(3):
                _seq, data = self._build_packet(PacketType.DISCONNECT)
                self._sendto_immediate(data)

        if notice == "You were kicked by the host.":
            self.session_token = None

        self.last_connection_error = notice
        self.ui_notice = notice
        self._reset_connection_state(clear_notice=False)

    def _insert_snapshot(self, snapshot: Snapshot, recv_time: float) -> bool:
        ticks = [item.tick for item in self.server_snapshots]
        index = bisect.bisect_left(ticks, snapshot.tick)
        if index < len(ticks) and ticks[index] == snapshot.tick:
            return False

        self.server_snapshots.insert(index, snapshot)
        self.snapshot_recv_times[snapshot.tick] = recv_time

        if len(self.server_snapshots) > 32:
            # Keep only the most recent 32 snapshots for interpolation
            self.server_snapshots = self.server_snapshots[-32:]
        return True

    def _handle_snapshot(self, pkt: Packet):
        try:
            snapshot = Snapshot.deserialize(pkt.payload)
        except ValueError:
            return

        last_input_seq = 0
        snapshot_data_len = snapshot.serialized_size()
        if len(pkt.payload) >= snapshot_data_len + SNAPSHOT_TRAILER_SIZE:
            (
                last_input_seq,
                self.last_server_send_time,
                self.match_elapsed,
                self.server_host_client_id,
            ) = struct.unpack(
                SNAPSHOT_TRAILER_FORMAT,
                pkt.payload[
                    snapshot_data_len : snapshot_data_len + SNAPSHOT_TRAILER_SIZE
                ],
            )
            if self.awaiting_phase_sync:
                self.game_started_by_server = self.match_elapsed > 0.0
                self.awaiting_phase_sync = False

        recv_time = time.perf_counter()

        # Fix reordering: keep snapshots sorted by authoritative tick and drop duplicates.
        inserted = self._insert_snapshot(snapshot, recv_time)
        if not inserted:
            return

        self.last_server_tick = max(self.last_server_tick, snapshot.tick)
        for entity_id in snapshot.entities:
            self.scores.setdefault(entity_id, 0)

        if self.client_id and self.client_id in snapshot.entities:
            server_entity = snapshot.entities[self.client_id]
            server_state = server_entity.to_dict()
            server_state["dash_cooldown_factor"] = (
                DASH_COOLDOWN_REDUCTION_FACTOR
                if server_state.get("effect_flags", 0) & EFFECT_FLAG_DASH_COOLDOWN
                else 1.0
            )
            if self.local_state:
                dash_state_found = False
                for p_inp in self.pending_inputs:
                    if p_inp["sequence"] == last_input_seq:
                        self.last_acked_dash_cooldown = p_inp["predicted_state"].get(
                            "dash_cooldown", 0.0
                        )
                        self.last_acked_dash_timer = p_inp["predicted_state"].get(
                            "dash_timer", 0.0
                        )
                        dash_state_found = True
                        break

                if dash_state_found:
                    server_state["dash_cooldown"] = self.last_acked_dash_cooldown
                    server_state["dash_timer"] = self.last_acked_dash_timer
                else:
                    self.last_acked_dash_cooldown = server_state.get(
                        "dash_cooldown", 0.0
                    )
                    self.last_acked_dash_timer = server_state.get("dash_timer", 0.0)

            if not self.local_state:
                self.local_state = server_state.copy()
                self.visual_state = server_state.copy()

            corrected, remaining, error = self.reconciler.reconcile(
                server_state,
                last_input_seq,
                self.pending_inputs,
            )
            self.pending_inputs = remaining
            self.local_state = corrected

            if self.visual_state and "x" in self.visual_state and "y" in self.visual_state:
                self.visual_state = smooth_correction(
                    self.visual_state, self.local_state, smoothing=0.3
                )
            else:
                self.visual_state = self.local_state.copy()

            if error > 0.01:
                self.metrics.log_prediction_error(error)

    def _handle_pong(self, pkt: Packet):
        if len(pkt.payload) >= PING_SIZE:
            sent_time = struct.unpack(PING_FORMAT, pkt.payload[:PING_SIZE])[0]
            rtt_ms = (time.perf_counter() - sent_time) * 1000.0

            self.current_rtt = rtt_ms
            self.rtt_samples.append(rtt_ms)
            if len(self.rtt_samples) > 200:
                self.rtt_samples = self.rtt_samples[-200:]

            self.metrics.log_rtt(rtt_ms)

    def _handle_heartbeat(self, pkt: Packet):
        _ = pkt

    def _handle_reliable_event(self, pkt: Packet):
        if not pkt.payload or len(pkt.payload) < 1:
            return

        event_type = pkt.payload[0]
        if (
            self.conn_state in (ConnState.CONNECTING, ConnState.RECONNECTING)
            and event_type != RELIABLE_EVENT_KICKED
        ):
            return

        if self.min_server_event_seq:
            if (
                pkt.sequence == self.min_server_event_seq
                or not AckTracker._sequence_greater_than(
                    pkt.sequence, self.min_server_event_seq
                )
            ):
                return

        label = "event"
        client_id = None

        if event_type == RELIABLE_EVENT_SCORE_UPDATE:
            if len(pkt.payload) < struct.calcsize(RELIABLE_SCORE_EVENT_FORMAT):
                return
            _, killer_id, victim_id = struct.unpack(
                RELIABLE_SCORE_EVENT_FORMAT,
                pkt.payload[: struct.calcsize(RELIABLE_SCORE_EVENT_FORMAT)],
            )
            self.scores[killer_id] = self.scores.get(killer_id, 0) + 1
            if victim_id:
                self.scores.setdefault(victim_id, 0)
            client_id = killer_id
            label = "score_update"
        elif event_type == RELIABLE_EVENT_SCORE_SYNC:
            offset = struct.calcsize("!B")
            pair_size = struct.calcsize("!HH")
            synced_scores: dict[int, int] = {}
            while offset + pair_size <= len(pkt.payload):
                entity_id, kills = struct.unpack(
                    "!HH", pkt.payload[offset : offset + pair_size]
                )
                synced_scores[entity_id] = kills
                offset += pair_size
            self.scores = synced_scores
            label = "score_sync"
        elif event_type == RELIABLE_EVENT_MATCH_RESET:
            self.match_winner_id = None
            self._reset_world_state()
            self.awaiting_phase_sync = False
            self.min_server_event_seq = pkt.sequence
            label = "match_reset"
        else:
            if len(pkt.payload) < struct.calcsize(RELIABLE_EVENT_FORMAT):
                return

            event_type, client_id = struct.unpack(
                RELIABLE_EVENT_FORMAT,
                pkt.payload[: struct.calcsize(RELIABLE_EVENT_FORMAT)],
            )
            if event_type == RELIABLE_EVENT_GAME_START:
                self.game_started_by_server = True
                self.match_winner_id = None
                self.match_elapsed = 0.0
                self.awaiting_phase_sync = False
                label = "game_start"
            elif event_type == RELIABLE_EVENT_JOIN:
                self.scores.setdefault(client_id, 0)
                label = "join"
            elif event_type == RELIABLE_EVENT_LEAVE:
                self.scores.pop(client_id, None)
                label = "leave"
            elif event_type == RELIABLE_EVENT_MATCH_OVER:
                self.match_winner_id = client_id
                self.awaiting_phase_sync = False
                label = "match_over"
            elif event_type == RELIABLE_EVENT_KICKED:
                self.pending_server_disconnect_notice = "You were kicked by the host."
                label = "kicked"

        self.min_server_event_seq = pkt.sequence
        self.recent_events.append(
            {"type": label, "client_id": client_id, "t": time.perf_counter()}
        )
        if len(self.recent_events) > 20:
            self.recent_events = self.recent_events[-20:]

    def request_game_start(self):
        """Send a reliable start-game signal to the server."""
        if not self.connected:
            return False

        payload = struct.pack(
            RELIABLE_EVENT_FORMAT,
            RELIABLE_EVENT_GAME_START,
            self.client_id or 0,
        )
        seq, data = self._build_packet(
            PacketType.RELIABLE_EVENT,
            payload,
            track_send=False,
        )
        if self.reliable_channel.send(data, self.server_addr) >= 0:
            self.recent_events.append(
                {
                    "type": "game_start_request",
                    "client_id": self.client_id or 0,
                    "t": time.perf_counter(),
                }
            )
            if len(self.recent_events) > 20:
                self.recent_events = self.recent_events[-20:]
            return True
        return False

    def request_kick_player(self, target_client_id: int) -> bool:
        if (
            not self.connected
            or self.client_id is None
            or target_client_id <= 0
            or target_client_id == self.client_id
        ):
            return False

        payload = struct.pack(
            RELIABLE_EVENT_FORMAT,
            RELIABLE_EVENT_KICK_PLAYER,
            target_client_id,
        )
        _, data = self._build_packet(
            PacketType.RELIABLE_EVENT,
            payload,
            track_send=False,
        )
        if self.reliable_channel.send(data, self.server_addr) >= 0:
            self.recent_events.append(
                {
                    "type": "kick_request",
                    "client_id": target_client_id,
                    "t": time.perf_counter(),
                }
            )
            if len(self.recent_events) > 20:
                self.recent_events = self.recent_events[-20:]
            return True
        return False

    def send_input(self, inp: dict):
        if not self.connected or not self.game_started_by_server:
            return

        self.input_sequence += 1
        input_record = {
            "sequence": self.input_sequence,
            "move_x": inp["move_x"],
            "move_y": inp["move_y"],
            "actions": inp["actions"],
        }

        self.input_history.append(input_record)
        # deque with maxlen automatically prunes old entries

        recent_inputs = list(self.input_history)[-INPUT_REDUNDANCY:]
        payload = struct.pack("!B", len(recent_inputs))
        for record in recent_inputs:
            payload += struct.pack(
                INPUT_FORMAT,
                record["sequence"],
                record["move_x"],
                record["move_y"],
                record["actions"],
            )

        self._send_packet(PacketType.INPUT, payload)

        self.pending_inputs.append(
            {
                "sequence": self.input_sequence,
                "input": inp,
                "predicted_state": self.local_state.copy(),
            }
        )
        if len(self.pending_inputs) > 60:
            self.pending_inputs = self.pending_inputs[-60:]

    def send_ping(self):
        timestamp = time.perf_counter()
        payload = struct.pack(PING_FORMAT, timestamp)
        self._send_packet(PacketType.PING, payload)

    def send_heartbeat(self):
        self._send_packet(PacketType.HEARTBEAT)

    def predict_local(self, inp: dict):
        if (
            not self.connected
            or not self.game_started_by_server
            or not self.local_state
        ):
            return

        self.local_state = self.predictor.predict(self.local_state, inp)
        if self.visual_state and "x" in self.visual_state and "y" in self.visual_state:
            self.visual_state = smooth_correction(
                self.visual_state, self.local_state, smoothing=0.5
            )
        else:
            self.visual_state = self.local_state.copy()

    def get_remote_states(self) -> dict:
        if not self.server_snapshots:
            return {}

        return self.interpolator.interpolate(
            self.server_snapshots, self.snapshot_recv_times, self.client_id or -1
        )

    def get_metrics_display(self) -> dict:
        metrics = {
            "RTT": f"{self.current_rtt:.1f} ms",
            "Loss": f"{self.ack_tracker.get_loss_rate() * 100:.1f}%",
            "Tick": str(self.last_server_tick),
            "Pending": str(len(self.pending_inputs)),
            "Players": str(
                len(self.server_snapshots[-1].entities) if (self.server_snapshots and len(self.server_snapshots) > 0) else 0
            ),
        }

        if len(self.rtt_samples) >= 2:
            jitters = [
                abs(self.rtt_samples[i] - self.rtt_samples[i - 1])
                for i in range(1, len(self.rtt_samples))
            ]
            if jitters:  # Guard against empty list
                self.current_jitter = sum(jitters[-10:]) / min(10, len(jitters))
                metrics["Jitter"] = f"{self.current_jitter:.1f} ms"
            else:
                metrics["Jitter"] = "0.0 ms"
        else:
            metrics["Jitter"] = "0.0 ms"

        return metrics

    def _maybe_log_metrics(
        self,
        now: float,
        last_bandwidth_time: float,
        last_bytes_sent: int,
        last_bytes_recv: int,
    ):
        if now - last_bandwidth_time < 1.0:
            return last_bandwidth_time, last_bytes_sent, last_bytes_recv

        self.metrics.log_bandwidth(
            self.total_bytes_sent - last_bytes_sent,
            self.total_bytes_recv - last_bytes_recv,
        )
        self.ack_tracker.detect_lost_packets()
        self.metrics.log_packet_loss(self.ack_tracker.get_loss_rate())
        return now, self.total_bytes_sent, self.total_bytes_recv

    def _run_headless(self):
        self.connect()
        connect_retry_time = time.perf_counter() + CONNECT_RETRY_INTERVAL
        last_time = time.perf_counter()
        last_bandwidth_time = last_time
        last_bytes_sent = 0
        last_bytes_recv = 0

        try:
            while self.running:
                now = time.perf_counter()
                dt = now - last_time
                last_time = now

                if self.net_sim:
                    self.net_sim.flush()
                self.receive_packets()
                self._flush_pending_server_disconnect()

                if self.ui_notice:
                    print(f"[CLIENT] {self.ui_notice}")
                    self.running = False
                    continue

                if self.connected and (now - self.last_packet_recv_time) > 5.0:
                    print("[CLIENT] Server silent - reconnecting...")
                    self.conn_state = ConnState.RECONNECTING
                    self.connect()
                    connect_retry_time = now + CONNECT_RETRY_INTERVAL

                if (
                    self.conn_state in (ConnState.CONNECTING, ConnState.RECONNECTING)
                    and now >= connect_retry_time
                ):
                    self.connect()
                    connect_retry_time = now + CONNECT_RETRY_INTERVAL

                inp = {"move_x": 0.0, "move_y": 0.0, "actions": 0}

                # Fix frame-rate dependent prediction: only accumulate gameplay time in-match.
                self._advance_gameplay_ticks(dt, inp, allow_gameplay_input=True)

                if now >= self._next_ping_time and self.connected:
                    self.send_ping()
                    self._next_ping_time += PING_INTERVAL

                if now - self._last_sent_time >= 1.0 and self.connected:
                    self.send_heartbeat()

                self.reliable_channel.tick()
                last_bandwidth_time, last_bytes_sent, last_bytes_recv = (
                    self._maybe_log_metrics(
                        now,
                        last_bandwidth_time,
                        last_bytes_sent,
                        last_bytes_recv,
                    )
                )

                time.sleep(0.001)
        except KeyboardInterrupt:
            print("\n[CLIENT] Interrupted")

    def _run_gui(self):
        try:
            import pygame
        except ModuleNotFoundError as exc:
            if exc.name != "pygame":
                raise
            raise SystemExit(
                "pygame is not installed for this Python interpreter. "
                "Run `.venv/bin/python -m client.client` or activate the project venv first."
            ) from exc

        from client.gui.scene_manager import SceneManager
        from client.gui.scenes.main_menu import MainMenuScene
        from client.gui.scenes.settings import load_config

        pygame.init()
        screen = pygame.display.set_mode((WORLD_WIDTH, WORLD_HEIGHT))
        pygame.display.set_caption("Multiplayer Engine")
        clock = pygame.time.Clock()

        self.apply_settings(load_config())

        scene_mgr = SceneManager(screen)
        scene_mgr.push(MainMenuScene(scene_mgr, client=self))

        last_time = time.perf_counter()
        connect_retry_time = last_time + CONNECT_RETRY_INTERVAL
        last_bandwidth_time = last_time
        last_bytes_sent = 0
        last_bytes_recv = 0

        try:
            while self.running:
                now = time.perf_counter()
                dt = now - last_time
                last_time = now

                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        self.running = False
                    scene_mgr.handle_event(event)

                if self.net_sim:
                    self.net_sim.flush()
                self.receive_packets()
                self._flush_pending_server_disconnect()

                if (
                    self.ui_notice
                    and scene_mgr.current is not None
                    and not isinstance(scene_mgr.current, MainMenuScene)
                ):
                    scene_mgr.reset(MainMenuScene(scene_mgr, client=self))

                if self.connected and (now - self.last_packet_recv_time) > 5.0:
                    print("[CLIENT] Server silent - reconnecting...")
                    self.conn_state = ConnState.RECONNECTING
                    self.connect()
                    connect_retry_time = now + CONNECT_RETRY_INTERVAL

                if (
                    self.conn_state in (ConnState.CONNECTING, ConnState.RECONNECTING)
                    and now >= connect_retry_time
                ):
                    self.connect()
                    connect_retry_time = now + CONNECT_RETRY_INTERVAL

                current_scene = scene_mgr.current
                get_input = getattr(current_scene, "get_input", None)
                inp = {"move_x": 0.0, "move_y": 0.0, "actions": 0}
                gameplay_input_enabled = callable(get_input)
                if callable(get_input):
                    scene_input = get_input()
                    if isinstance(scene_input, dict):
                        inp = scene_input

                self._advance_gameplay_ticks(dt, inp, gameplay_input_enabled)

                if now >= self._next_ping_time and self.connected:
                    self.send_ping()
                    self._next_ping_time += PING_INTERVAL

                if now - self._last_sent_time >= 1.0 and self.connected:
                    self.send_heartbeat()

                self.reliable_channel.tick()
                last_bandwidth_time, last_bytes_sent, last_bytes_recv = (
                    self._maybe_log_metrics(
                        now,
                        last_bandwidth_time,
                        last_bytes_sent,
                        last_bytes_recv,
                    )
                )

                scene_mgr.update(dt)
                scene_mgr.draw()
                clock.tick(self.target_fps)
                self.current_fps = clock.get_fps()
        finally:
            pygame.quit()

    def run(self):
        self.running = True

        try:
            if self.headless:
                self._run_headless()
            else:
                self._run_gui()
        finally:
            self.running = False
            save_client_id = self.client_id
            self.stop_host_server()
            self.disconnect(close_socket=True)
            self.metrics.save(f"client_{save_client_id or 0}_metrics.json")
            summary = self.metrics.get_summary()
            if summary:
                print(f"[CLIENT] Metrics summary: {summary}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Game Networking Engine Client")
    parser.add_argument("--host", default="127.0.0.1", help="Server address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Server port")
    parser.add_argument(
        "--tick-rate", type=int, default=DEFAULT_TICK_RATE, help="Client tick rate (Hz)"
    )
    parser.add_argument(
        "--headless", action="store_true", help="Run without pygame (for bots/testing)"
    )
    parser.add_argument(
        "--loss", type=float, default=0.0, help="Simulated packet loss rate (0.0-1.0)"
    )
    parser.add_argument(
        "--latency", type=float, default=0.0, help="Simulated base latency (seconds)"
    )
    parser.add_argument(
        "--room-key",
        default=None,
        help="Shared room key for secure UDP sessions",
    )
    args = parser.parse_args()

    if args.headless and not (args.room_key or "").strip():
        parser.error("--room-key is required in headless mode.")

    client = GameClient(
        server_host=args.host,
        server_port=args.port,
        tick_rate=args.tick_rate,
        headless=args.headless,
        loss_sim=args.loss,
        latency_sim=args.latency,
        room_key=args.room_key,
    )
    client.run()


if __name__ == "__main__":
    main()
