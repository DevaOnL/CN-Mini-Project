"""
Main game server — authoritative, tick-based, UDP.

Handles:
- Client connections/disconnections
- Session-based reconnects by token
- Input processing and authoritative simulation
- Snapshot broadcasting
- Ping/pong and heartbeat handling
- Reliable join/leave event delivery
"""

import math
import signal
import struct
import threading
import time
from collections import deque

from common.packet import (
    Packet,
    PacketType,
    HEADER_SIZE,
    INPUT_FORMAT,
    INPUT_SIZE,
    PING_FORMAT,
    PING_SIZE,
    packet_requires_encryption,
    packet_uses_connection_epoch,
    pack_connection_epoch,
    unpack_connection_epoch,
    HANDSHAKE_DISCONNECT_FORMAT,
    HANDSHAKE_DISCONNECT_SIZE,
    DISCONNECT_REASON_FORMAT,
    DISCONNECT_REASON_NONE,
    DISCONNECT_REASON_KICKED,
    DISCONNECT_REASON_SECURE_REQUIRED,
    DISCONNECT_REASON_AUTH_FAILED,
)
from common.net import create_server_socket, NetworkSimulator
from common.security import (
    PendingSecureHandshake,
    SECURE_PROTOCOL_VERSION,
    SECURE_HELLO_ACK_FORMAT,
    SECURE_HELLO_FORMAT,
    SECURE_HELLO_SIZE,
    SECURE_HANDSHAKE_TIMEOUT_SECS,
    build_server_proof,
    decrypt_payload,
    derive_room_psk,
    derive_session_key,
    encrypt_payload,
    generate_handshake_nonce,
    verify_client_proof,
)
from common.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TICK_RATE,
    DEFAULT_BUFFER_SIZE,
    MATCH_DURATION_SECS,
    MATCH_OVER_DISPLAY_SECS,
    RELIABLE_MAX_RETRIES,
    RELIABLE_RETRY_INTERVAL,
    RESPAWN_HEALTH,
)
from common.metrics_logger import MetricsLogger
from server.game_state import GameState
from server.client_manager import ClientManager, ConnectedClient
from server.session_manager import SessionManager, SessionState


CONNECT_TOKEN_SIZE = 16
CONNECT_REQ_FORMAT = f"!{CONNECT_TOKEN_SIZE}sI"
CONNECT_ACK_FORMAT = f"!HI{CONNECT_TOKEN_SIZE}sI"
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
POST_CONNECT_GUARD_SECS = 0.2
KICK_GRACE_SECS = 2.0
KICK_TOKEN_BLOCK_SECS = 10.0


class ReliableChannel:
    """
    Retransmit reliable UDP packets until their packet-header sequence is acked.
    Used for player join/leave events without changing the transport stack.
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


class GameServer:
    """
    Authoritative game server.
    Runs a fixed-timestep simulation loop, processes client inputs,
    and broadcasts state snapshots over UDP.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        tick_rate: int = DEFAULT_TICK_RATE,
        loss_sim: float = 0.0,
        latency_sim: float = 0.0,
        verbose: bool = True,
        room_key: str | None = None,
    ):
        self.host = host
        self.port = port
        self.tick_rate = tick_rate
        self.dt = 1.0 / tick_rate
        self.running = False
        self.verbose = verbose

        self.sock = create_server_socket(host, port)

        self.net_sim = None
        if loss_sim > 0 or latency_sim > 0:
            self.net_sim = NetworkSimulator(
                self.sock,
                loss_rate=loss_sim,
                min_latency=latency_sim * 0.5,
                max_latency=latency_sim * 1.5,
            )

        self.game_state = GameState()
        self.client_mgr = ClientManager()
        self.session_mgr = SessionManager()
        self.metrics = MetricsLogger()
        self.reliable_channels: dict[int, ReliableChannel] = {}
        self.pending_kicks: dict[int, dict] = {}
        self.kicked_tokens: dict[str, float] = {}
        self.idle_client_state: dict[str, dict] = {}
        self.recent_disconnect_addrs: dict[tuple, tuple[float, int]] = {}
        self._next_connection_epoch = 1
        self.host_client_id: int | None = None
        self.match_over_timer = 0.0
        self.match_elapsed = 0.0
        self.match_winner_id: int | None = None
        self.lobby_snapshot_stride = max(1, self.tick_rate // 5)

        self.current_tick = 0
        self.total_bytes_sent = 0
        self.total_bytes_recv = 0
        self.room_key = ""
        self._room_psk: bytes | None = None
        self.pending_secure_handshakes: dict[tuple, PendingSecureHandshake] = {}
        self.set_room_key(room_key)

    @property
    def secure_required(self) -> bool:
        return self._room_psk is not None

    def set_room_key(self, room_key: str | None):
        normalized = (room_key or "").strip()
        self.room_key = normalized
        self._room_psk = derive_room_psk(normalized) if normalized else None
        self.pending_secure_handshakes.clear()

    def _log(self, msg: str):
        if self.verbose:
            print(msg, flush=True)

    def _sendto(self, data: bytes, addr: tuple):
        if self.net_sim:
            self.net_sim.sendto(data, addr)
        else:
            try:
                self.sock.sendto(data, addr)
            except (BlockingIOError, OSError):
                pass
        self.total_bytes_sent += len(data)

    def _sendto_immediate(self, data: bytes, addr: tuple):
        try:
            self.sock.sendto(data, addr)
        except (BlockingIOError, OSError):
            pass
        self.total_bytes_sent += len(data)

    def _get_reliable_channel(self, client: ConnectedClient) -> ReliableChannel:
        channel = self.reliable_channels.get(client.client_id)
        if channel is None:
            channel = ReliableChannel(self._sendto, client.ack_tracker.on_packet_sent)
            self.reliable_channels[client.client_id] = channel
        return channel

    def _active_client_ids(self) -> list[int]:
        return sorted(
            client_id
            for client_id in self.client_mgr.clients
            if client_id not in self.pending_kicks
        )

    def _prune_kicked_tokens(self):
        now = time.monotonic()
        expired = [
            token for token, deadline in self.kicked_tokens.items() if deadline <= now
        ]
        for token in expired:
            del self.kicked_tokens[token]

    def _prune_recent_disconnect_addrs(self):
        now = time.monotonic()
        expired = [
            addr
            for addr, (deadline, _last_seq) in self.recent_disconnect_addrs.items()
            if deadline <= now
        ]
        for addr in expired:
            del self.recent_disconnect_addrs[addr]

    def _prune_pending_secure_handshakes(self):
        now = time.monotonic()
        expired = [
            addr
            for addr, handshake in self.pending_secure_handshakes.items()
            if handshake.expires_at <= now
        ]
        for addr in expired:
            del self.pending_secure_handshakes[addr]

    def _get_pending_secure_handshake(
        self, addr: tuple
    ) -> PendingSecureHandshake | None:
        handshake = self.pending_secure_handshakes.get(addr)
        if handshake is None:
            return None
        if handshake.expires_at <= time.monotonic():
            del self.pending_secure_handshakes[addr]
            return None
        return handshake

    def _allocate_connection_epoch(self) -> int:
        epoch = self._next_connection_epoch & 0xFFFFFFFF
        self._next_connection_epoch = (epoch + 1) & 0xFFFFFFFF
        if epoch == 0:
            epoch = 1
            self._next_connection_epoch = 2
        return epoch

    def _ack_reliable_sequences(
        self, client: ConnectedClient, ack_seq: int, ack_bitfield: int
    ):
        channel = self._get_reliable_channel(client)
        channel.ack(ack_seq)
        for i in range(32):
            if ack_bitfield & (1 << i):
                channel.ack((ack_seq - 1 - i) & 0xFFFF)

    def _serialize_packet(
        self,
        packet_type: int,
        payload: bytes,
        *,
        sequence: int = 0,
        ack: int = 0,
        ack_bitfield: int = 0,
        session_key: bytes | None = None,
    ) -> bytes:
        pkt = Packet(packet_type, sequence, ack, ack_bitfield, payload)
        if packet_requires_encryption(packet_type) and session_key is not None:
            encrypted_length = len(payload) + 28
            header = pkt.serialize_header(encrypted_length)
            encrypted_payload = encrypt_payload(session_key, header, payload)
            return header + encrypted_payload
        return pkt.serialize()

    def _make_client_packet(
        self,
        client: ConnectedClient | None,
        packet_type: int,
        payload: bytes = b"",
        track_send: bool = True,
    ):
        if client is None:
            return self._serialize_packet(packet_type, payload), None

        if packet_uses_connection_epoch(packet_type) and client.connection_epoch:
            payload = pack_connection_epoch(client.connection_epoch, payload)

        seq = client.ack_tracker.next_sequence()
        data = self._serialize_packet(
            packet_type,
            payload,
            sequence=seq,
            ack=client.ack_tracker.remote_sequence,
            ack_bitfield=client.ack_tracker.ack_bitfield,
            session_key=client.session_key,
        )
        if track_send:
            client.ack_tracker.on_packet_sent(seq)
        return data, seq

    def _parse_connect_request(self, payload: bytes) -> tuple[str | None, int]:
        if len(payload) >= struct.calcsize(CONNECT_REQ_FORMAT):
            raw, nonce = struct.unpack(
                CONNECT_REQ_FORMAT,
                payload[: struct.calcsize(CONNECT_REQ_FORMAT)],
            )
            token = raw.decode("ascii", errors="ignore").rstrip("\x00")
            return token or None, nonce

        if len(payload) >= CONNECT_TOKEN_SIZE:
            raw = payload[:CONNECT_TOKEN_SIZE]
            token = raw.decode("ascii", errors="ignore").rstrip("\x00")
            return token or None, 0

        return None, 0

    def _build_connect_ack_payload(
        self, client: ConnectedClient, connect_nonce: int
    ) -> bytes:
        token_bytes = client.session_token.encode("ascii")[:CONNECT_TOKEN_SIZE]
        token_bytes = token_bytes.ljust(CONNECT_TOKEN_SIZE, b"\x00")
        return struct.pack(
            CONNECT_ACK_FORMAT,
            client.client_id,
            client.connection_epoch,
            token_bytes,
            connect_nonce,
        )

    def _pending_disconnect_matches_client(
        self, payload: bytes, client: ConnectedClient
    ) -> bool:
        if len(payload) >= HANDSHAKE_DISCONNECT_SIZE:
            token_bytes, connect_nonce, _reason_code = struct.unpack(
                HANDSHAKE_DISCONNECT_FORMAT,
                payload[:HANDSHAKE_DISCONNECT_SIZE],
            )
            token = token_bytes.decode("ascii", errors="ignore").rstrip("\x00")
            return connect_nonce == client.last_connect_nonce and (
                not token or token == client.session_token
            )

        if len(payload) >= struct.calcsize(CONNECT_REQ_FORMAT):
            reconnect_token, connect_nonce = self._parse_connect_request(payload)
            return (
                reconnect_token == client.session_token
                and connect_nonce == client.last_connect_nonce
            )

        return False

    def _remove_client(
        self,
        client: ConnectedClient,
        reason: str,
        broadcast_leave: bool = True,
        remove_session: bool = True,
    ):
        preserved_score = self.game_state.scores.get(client.client_id)
        if not remove_session:
            entity = self.game_state.entities.get(client.client_id)
            respawn_ticks_remaining = self.game_state.respawn_timers.get(
                client.client_id, 0
            )
            self.idle_client_state[client.session_token] = {
                "entity": entity.copy() if entity is not None else None,
                "score": preserved_score or 0,
                "respawn_resume_tick": self.game_state.tick + respawn_ticks_remaining
                if respawn_ticks_remaining > 0
                else 0,
                "dash_cooldown_resume_tick": self.game_state.tick
                + math.ceil(
                    (entity.dash_cooldown if entity is not None else 0.0) / self.dt
                )
                if entity is not None and entity.dash_cooldown > 0.0
                else 0,
                "dash_timer_resume_tick": self.game_state.tick
                + math.ceil(
                    (entity.dash_timer if entity is not None else 0.0) / self.dt
                )
                if entity is not None and entity.dash_timer > 0.0
                else 0,
                "invincibility_until_tick": self.game_state.invincibility_until.get(
                    client.client_id, 0
                ),
                "damage_boost_until_tick": self.game_state.damage_boost_until.get(
                    client.client_id, 0
                ),
                "dash_cooldown_until_tick": self.game_state.dash_cooldown_until.get(
                    client.client_id, 0
                ),
            }

        if broadcast_leave:
            self._broadcast_presence_event(RELIABLE_EVENT_LEAVE, client.client_id)
        self.recent_disconnect_addrs[client.address] = (
            time.monotonic() + POST_CONNECT_GUARD_SECS,
            client.ack_tracker.local_sequence,
        )
        self.game_state.remove_entity(client.client_id)
        if not remove_session and preserved_score is not None:
            self.game_state.scores[client.client_id] = preserved_score
        self.pending_secure_handshakes.pop(client.address, None)
        self.client_mgr.remove_client(client.client_id)
        active_client_ids = self._active_client_ids()
        if not active_client_ids:
            self.game_state.reset()
            self.current_tick = 0
            self.host_client_id = None
            self.match_over_timer = 0.0
            self.match_elapsed = 0.0
            self.match_winner_id = None
            self.idle_client_state.clear()
            self._log("[SERVER] All clients gone - full reset.")
        if remove_session:
            self.session_mgr.remove(client.session_token)
            self.idle_client_state.pop(client.session_token, None)
        else:
            self.session_mgr.mark_idle(client.session_token)
        self.reliable_channels.pop(client.client_id, None)
        self.pending_kicks.pop(client.client_id, None)
        if active_client_ids and self.host_client_id == client.client_id:
            self.host_client_id = active_client_ids[0]
        for active_client in self.client_mgr.all_clients():
            self._send_score_sync(active_client)
        self._log(f"[SERVER] Client {client.client_id} {reason}")

    def _broadcast_presence_event(
        self,
        event_type: int,
        subject_client_id: int,
        exclude_client_id: int | None = None,
    ):
        for client in self.client_mgr.all_clients():
            if exclude_client_id is not None and client.client_id == exclude_client_id:
                continue
            if client.client_id in self.pending_kicks:
                continue
            self._send_reliable_event_to_client(client, event_type, subject_client_id)

    def _send_reliable_payload(self, client: ConnectedClient, payload: bytes) -> int:
        data, _ = self._make_client_packet(
            client,
            PacketType.RELIABLE_EVENT,
            payload,
            track_send=False,
        )
        channel = self._get_reliable_channel(client)
        seq = channel.send(data, client.address)
        if seq >= 0:
            client.bytes_sent += len(data)
        return seq

    def _send_reliable_event_to_client(
        self, client: ConnectedClient, event_type: int, subject_client_id: int
    ) -> int:
        payload = struct.pack(RELIABLE_EVENT_FORMAT, event_type, subject_client_id)
        return self._send_reliable_payload(client, payload)

    def _send_disconnect_notice(
        self,
        addr: tuple,
        reason_code: int,
        client: ConnectedClient | None = None,
        connect_nonce: int = 0,
        session_key: bytes | None = None,
    ):
        if client is not None:
            data, _ = self._make_client_packet(
                client,
                PacketType.DISCONNECT,
                struct.pack(DISCONNECT_REASON_FORMAT, reason_code),
            )
        else:
            payload = struct.pack(DISCONNECT_REASON_FORMAT, reason_code)
            if connect_nonce and session_key is None:
                payload = struct.pack(
                    HANDSHAKE_DISCONNECT_FORMAT,
                    b"\x00" * CONNECT_TOKEN_SIZE,
                    connect_nonce,
                    reason_code,
                )
            data = self._serialize_packet(
                PacketType.DISCONNECT,
                payload,
                session_key=session_key,
            )
        self._sendto_immediate(data, addr)
        if client is not None:
            client.bytes_sent += len(data)

    def _broadcast_game_start(self):
        for client in self.client_mgr.all_clients():
            if client.client_id in self.pending_kicks:
                continue
            self._send_reliable_event_to_client(client, RELIABLE_EVENT_GAME_START, 0)

    def _send_score_sync(self, client: ConnectedClient):
        if not self.game_state.scores or client.client_id in self.pending_kicks:
            return

        payload = struct.pack("!B", RELIABLE_EVENT_SCORE_SYNC)
        for entity_id, kills in sorted(self.game_state.scores.items()):
            payload += struct.pack("!HH", entity_id, kills)
        self._send_reliable_payload(client, payload)

    def _broadcast_score_update(self, killer_id: int, victim_id: int):
        payload = struct.pack(
            RELIABLE_SCORE_EVENT_FORMAT,
            RELIABLE_EVENT_SCORE_UPDATE,
            killer_id,
            victim_id,
        )
        for client in self.client_mgr.all_clients():
            if client.client_id in self.pending_kicks:
                continue
            self._send_reliable_payload(client, payload)

    def _broadcast_match_over(self, winner_id: int):
        self.match_winner_id = winner_id
        payload = struct.pack(
            RELIABLE_EVENT_FORMAT, RELIABLE_EVENT_MATCH_OVER, winner_id
        )
        for client in self.client_mgr.all_clients():
            if client.client_id in self.pending_kicks:
                continue
            self._send_reliable_payload(client, payload)

    def _broadcast_match_reset(self):
        payload = struct.pack("!B", RELIABLE_EVENT_MATCH_RESET)
        for client in self.client_mgr.all_clients():
            if client.client_id in self.pending_kicks:
                continue
            self._send_reliable_payload(client, payload)

    def _reset_match(self):
        self.game_state.reset()
        self.idle_client_state.clear()
        self.current_tick = 0
        for client in self.client_mgr.all_clients():
            if client.client_id in self.pending_kicks:
                client.pending_inputs.clear()
                self._get_reliable_channel(client).reset()
                continue
            self.game_state.add_entity(client.client_id)
            client.last_processed_input_seq = 0
            client.pending_inputs.clear()
            self._get_reliable_channel(client).reset()
        self.match_elapsed = 0.0
        self.match_over_timer = 0.0
        self.match_winner_id = None
        active_client_ids = self._active_client_ids()
        self.host_client_id = active_client_ids[0] if active_client_ids else None
        self._broadcast_match_reset()
        self._log("[SERVER] Match reset - returning to lobby.")

    def receive_all_packets(self):
        self._prune_pending_secure_handshakes()
        # Use time budget instead of fixed count to prevent packet starvation
        deadline = time.perf_counter() + 0.01  # 10ms max per tick
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

    def _handle_packet(self, data: bytes, addr: tuple):
        try:
            pkt = Packet.deserialize(data)
        except ValueError:
            return

        if pkt.packet_type == PacketType.SECURE_HELLO:
            self._handle_secure_hello(pkt, addr)
            return

        client = self.client_mgr.get_by_address(addr)
        pending_secure = self._get_pending_secure_handshake(addr)

        if self.secure_required:
            if pkt.packet_type == PacketType.CONNECT_REQ and pending_secure is None:
                if len(pkt.payload) == struct.calcsize(CONNECT_REQ_FORMAT):
                    _reconnect_token, connect_nonce = self._parse_connect_request(
                        pkt.payload
                    )
                    self._send_disconnect_notice(
                        addr,
                        DISCONNECT_REASON_SECURE_REQUIRED,
                        connect_nonce=connect_nonce,
                    )
                return
            if packet_requires_encryption(pkt.packet_type):
                allow_cleartext_connect_disconnect = (
                    pkt.packet_type == PacketType.DISCONNECT
                    and len(pkt.payload) >= HANDSHAKE_DISCONNECT_SIZE
                )
                session_key = None
                if pkt.packet_type == PacketType.CONNECT_REQ and pending_secure is not None:
                    session_key = pending_secure.session_key
                elif client is not None and client.session_key is not None:
                    session_key = client.session_key
                elif pending_secure is not None:
                    session_key = pending_secure.session_key

                if session_key is None:
                    if allow_cleartext_connect_disconnect:
                        pass
                    else:
                        return
                else:
                    try:
                        pkt.payload = decrypt_payload(
                            session_key,
                            data[:HEADER_SIZE],
                            pkt.payload,
                        )
                    except ValueError:
                        if (
                            pkt.packet_type == PacketType.DISCONNECT
                            and (
                                allow_cleartext_connect_disconnect
                                or (
                                    client is not None
                                    and self._pending_disconnect_matches_client(
                                        pkt.payload, client
                                    )
                                )
                            )
                        ):
                            pass
                        else:
                            return

        if pkt.packet_type != PacketType.CONNECT_REQ:
            if pkt.packet_type == PacketType.DISCONNECT:
                if client is not None:
                    if self._pending_disconnect_matches_client(pkt.payload, client):
                        pass
                    else:
                        unpacked = unpack_connection_epoch(pkt.payload)
                        if unpacked is None:
                            return
                        packet_epoch, payload = unpacked
                        if packet_epoch != client.connection_epoch:
                            return
                        pkt.payload = payload
                elif len(pkt.payload) < struct.calcsize(CONNECT_REQ_FORMAT):
                    return
            elif packet_uses_connection_epoch(pkt.packet_type):
                if client is None:
                    return
                unpacked = unpack_connection_epoch(pkt.payload)
                if unpacked is None:
                    return
                packet_epoch, payload = unpacked
                if packet_epoch != client.connection_epoch:
                    return
                pkt.payload = payload

        if pkt.packet_type == PacketType.CONNECT_REQ:
            self._handle_connect(pkt, addr)
        elif pkt.packet_type == PacketType.INPUT:
            self._handle_input(pkt, addr)
        elif pkt.packet_type == PacketType.DISCONNECT:
            self._handle_disconnect(pkt, addr)
        elif pkt.packet_type == PacketType.PING:
            self._handle_ping(pkt, addr)
        elif pkt.packet_type == PacketType.HEARTBEAT:
            self._handle_heartbeat(addr)
        elif pkt.packet_type == PacketType.RELIABLE_EVENT:
            self._handle_reliable_event(pkt, addr)

        client = self.client_mgr.get_by_address(addr)
        if client:
            if client.client_id not in self.pending_kicks:
                client.touch()
            client.bytes_received += len(data)
            client.ack_tracker.on_packet_received(pkt.sequence)

            # Fix server-side loss metrics: consume the client's ack fields too.
            if client.ack_tracker.should_process_ack(pkt.ack, pkt.ack_bitfield):
                client.ack_tracker.on_ack_received(pkt.ack, pkt.ack_bitfield)
                self._ack_reliable_sequences(client, pkt.ack, pkt.ack_bitfield)

        session = self.session_mgr.get_by_addr(addr)
        if session and (client is None or client.client_id not in self.pending_kicks):
            session.touch()

    def _handle_secure_hello(self, pkt: Packet, addr: tuple):
        if not self.secure_required or self._room_psk is None:
            return
        if len(pkt.payload) < SECURE_HELLO_SIZE:
            return

        version, client_nonce, client_proof = struct.unpack(
            SECURE_HELLO_FORMAT,
            pkt.payload[:SECURE_HELLO_SIZE],
        )
        if version != SECURE_PROTOCOL_VERSION:
            self._send_disconnect_notice(addr, DISCONNECT_REASON_AUTH_FAILED)
            return
        if not verify_client_proof(self._room_psk, client_nonce, client_proof):
            self._send_disconnect_notice(addr, DISCONNECT_REASON_AUTH_FAILED)
            return

        server_nonce = generate_handshake_nonce()
        session_key = derive_session_key(self._room_psk, client_nonce, server_nonce)
        self.pending_secure_handshakes[addr] = PendingSecureHandshake(
            client_nonce=client_nonce,
            server_nonce=server_nonce,
            session_key=session_key,
            expires_at=time.monotonic() + SECURE_HANDSHAKE_TIMEOUT_SECS,
        )
        payload = struct.pack(
            SECURE_HELLO_ACK_FORMAT,
            SECURE_PROTOCOL_VERSION,
            server_nonce,
            build_server_proof(self._room_psk, client_nonce, server_nonce),
        )
        data = self._serialize_packet(PacketType.SECURE_HELLO_ACK, payload)
        self._sendto_immediate(data, addr)

    def _handle_connect(self, pkt: Packet, addr: tuple):
        self._prune_kicked_tokens()
        self._prune_recent_disconnect_addrs()
        pending_secure = self._get_pending_secure_handshake(addr)
        pending_session_key = pending_secure.session_key if pending_secure else None
        reconnect_token, connect_nonce = self._parse_connect_request(pkt.payload)

        if not reconnect_token and addr in self.recent_disconnect_addrs:
            return

        if reconnect_token and reconnect_token in self.kicked_tokens:
            self._send_disconnect_notice(
                addr,
                DISCONNECT_REASON_KICKED,
                connect_nonce=connect_nonce,
                session_key=pending_session_key,
            )
            self.pending_secure_handshakes.pop(addr, None)
            return

        if reconnect_token and connect_nonce == 0:
            return

        client = self.client_mgr.get_by_address(addr)
        if client is not None:
            if client.client_id in self.pending_kicks:
                self._send_reliable_event_to_client(
                    client,
                    RELIABLE_EVENT_KICKED,
                    self.pending_kicks[client.client_id]["host_id"],
                )
                self._send_disconnect_notice(
                    addr,
                    DISCONNECT_REASON_KICKED,
                    connect_nonce=connect_nonce,
                    session_key=pending_session_key,
                )
                self.pending_secure_handshakes.pop(addr, None)
                return
            if connect_nonce and connect_nonce <= client.last_connect_nonce:
                return
            refresh_existing_connection = (
                reconnect_token == client.session_token
                and connect_nonce > client.last_connect_nonce
            )
            if refresh_existing_connection:
                client.ack_tracker.reset()
                self._get_reliable_channel(client).reset()
                client.pending_inputs.clear()
                client.connection_epoch = self._allocate_connection_epoch()
            if connect_nonce:
                client.last_connect_nonce = connect_nonce
            if client.connection_epoch == 0:
                client.connection_epoch = self._allocate_connection_epoch()
            if pending_session_key is not None:
                client.session_key = pending_session_key
            client.delay_nonconnect_packets(POST_CONNECT_GUARD_SECS)
            data, _ = self._make_client_packet(
                client,
                PacketType.CONNECT_ACK,
                self._build_connect_ack_payload(client, connect_nonce),
            )
            self._sendto(data, addr)
            client.bytes_sent += len(data)
            self.pending_secure_handshakes.pop(addr, None)
            if self.game_state.game_started:
                self._send_reliable_event_to_client(
                    client, RELIABLE_EVENT_GAME_START, 0
                )
                self._send_score_sync(client)
                if self.match_over_timer > 0.0 and self.match_winner_id is not None:
                    self._send_reliable_event_to_client(
                        client, RELIABLE_EVENT_MATCH_OVER, self.match_winner_id
                    )
            return

        if reconnect_token:
            session = self.session_mgr.get_by_token(reconnect_token)
            client = self.client_mgr.get_by_token(reconnect_token)
            if session is not None and session.state != SessionState.EXPIRED:
                if client is None:
                    client = self.client_mgr.restore_client(
                        session.client_id,
                        addr,
                        reconnect_token,
                        session_key=pending_session_key,
                    )
                    client.connection_epoch = self._allocate_connection_epoch()
                    client.last_connect_nonce = connect_nonce
                    client.delay_nonconnect_packets(POST_CONNECT_GUARD_SECS)
                    self.session_mgr.reconnect(reconnect_token, addr)
                    restored_state = self.idle_client_state.pop(reconnect_token, None)
                    if restored_state and restored_state.get("entity") is not None:
                        stored_entity = restored_state["entity"]
                        # Validate entity has required attributes
                        if not (hasattr(stored_entity, 'x') and hasattr(stored_entity, 'y') and
                                hasattr(stored_entity, 'vx') and hasattr(stored_entity, 'vy')):
                            # Invalid restored entity, create new one instead
                            entity = self.game_state.add_entity(client.client_id)
                        else:
                            entity = self.game_state.add_entity(
                                client.client_id,
                                x=stored_entity.x,
                                y=stored_entity.y,
                            )
                            entity.vx = stored_entity.vx
                            entity.vy = stored_entity.vy
                        self.game_state.scores[client.client_id] = restored_state.get(
                            "score", 0
                        )
                        respawn_ticks_remaining = max(
                            0,
                            restored_state.get("respawn_resume_tick", 0)
                            - self.game_state.tick,
                        )
                        if respawn_ticks_remaining > 0:
                            entity.health = 0.0
                            self.game_state.respawn_timers[client.client_id] = (
                                respawn_ticks_remaining
                            )
                        else:
                            entity.health = (
                                stored_entity.health
                                if stored_entity.health > 0.0
                                else RESPAWN_HEALTH
                            )
                            if stored_entity.health <= 0.0:
                                entity.x, entity.y = self.game_state._spawn_position(
                                    client.client_id
                                )

                        entity.dash_cooldown = max(
                            0.0,
                            (
                                restored_state.get("dash_cooldown_resume_tick", 0)
                                - self.game_state.tick
                            )
                            * self.dt,
                        )
                        entity.dash_timer = max(
                            0.0,
                            (
                                restored_state.get("dash_timer_resume_tick", 0)
                                - self.game_state.tick
                            )
                            * self.dt,
                        )

                        if (
                            restored_state.get("invincibility_until_tick", 0)
                            > self.game_state.tick
                        ):
                            self.game_state.invincibility_until[client.client_id] = (
                                restored_state["invincibility_until_tick"]
                            )
                        if (
                            restored_state.get("damage_boost_until_tick", 0)
                            > self.game_state.tick
                        ):
                            self.game_state.damage_boost_until[client.client_id] = (
                                restored_state["damage_boost_until_tick"]
                            )
                        if (
                            restored_state.get("dash_cooldown_until_tick", 0)
                            > self.game_state.tick
                        ):
                            self.game_state.dash_cooldown_until[client.client_id] = (
                                restored_state["dash_cooldown_until_tick"]
                            )
                    else:
                        self.game_state.add_entity(client.client_id)
                    if self.host_client_id is None:
                        self.host_client_id = client.client_id

                    data, _ = self._make_client_packet(
                        client,
                        PacketType.CONNECT_ACK,
                        self._build_connect_ack_payload(client, connect_nonce),
                    )
                    self._sendto(data, addr)
                    client.bytes_sent += len(data)
                    self.pending_secure_handshakes.pop(addr, None)
                    self._broadcast_presence_event(
                        RELIABLE_EVENT_JOIN, client.client_id
                    )
                    for active_client in self.client_mgr.all_clients():
                        self._send_score_sync(active_client)
                    if self.game_state.game_started:
                        self._send_reliable_event_to_client(
                            client, RELIABLE_EVENT_GAME_START, 0
                        )
                        if (
                            self.match_over_timer > 0.0
                            and self.match_winner_id is not None
                        ):
                            self._send_reliable_event_to_client(
                                client,
                                RELIABLE_EVENT_MATCH_OVER,
                                self.match_winner_id,
                            )
                    self._log(
                        f"[SERVER] Client {client.client_id} resumed idle session from {addr}"
                    )
                    return

                if client.client_id in self.pending_kicks:
                    self._send_reliable_event_to_client(
                        client,
                        RELIABLE_EVENT_KICKED,
                        self.pending_kicks[client.client_id]["host_id"],
                    )
                    self._send_disconnect_notice(
                        addr,
                        DISCONNECT_REASON_KICKED,
                        connect_nonce=connect_nonce,
                        session_key=pending_session_key,
                    )
                    return
                if connect_nonce and connect_nonce <= client.last_connect_nonce:
                    return
                old_addr = client.address
                if self.session_mgr.reconnect(reconnect_token, addr) is None:
                    return
                self.client_mgr.bind_address(client, addr)
                self.recent_disconnect_addrs[old_addr] = (
                    time.monotonic() + POST_CONNECT_GUARD_SECS,
                    client.ack_tracker.local_sequence,
                )
                client.ack_tracker.reset()
                self._get_reliable_channel(client).reset()
                client.pending_inputs.clear()
                client.touch()
                client.connection_epoch = self._allocate_connection_epoch()
                client.last_connect_nonce = connect_nonce
                if pending_session_key is not None:
                    client.session_key = pending_session_key
                client.delay_nonconnect_packets(POST_CONNECT_GUARD_SECS)
                data, _ = self._make_client_packet(
                    client,
                    PacketType.CONNECT_ACK,
                    self._build_connect_ack_payload(client, connect_nonce),
                )
                self._sendto(data, addr)
                client.bytes_sent += len(data)
                self.pending_secure_handshakes.pop(addr, None)
                if self.game_state.game_started:
                    self._send_reliable_event_to_client(
                        client, RELIABLE_EVENT_GAME_START, 0
                    )
                    self._send_score_sync(client)
                    if self.match_over_timer > 0.0 and self.match_winner_id is not None:
                        self._send_reliable_event_to_client(
                            client, RELIABLE_EVENT_MATCH_OVER, self.match_winner_id
                        )
                self._log(f"[SERVER] Client {client.client_id} reconnected from {addr}")
                return

            self._send_disconnect_notice(
                addr,
                DISCONNECT_REASON_NONE,
                connect_nonce=connect_nonce,
                session_key=pending_session_key,
            )
            self.pending_secure_handshakes.pop(addr, None)
            return

        client = self.client_mgr.add_client(addr, session_key=pending_session_key)
        client.connection_epoch = self._allocate_connection_epoch()
        client.last_connect_nonce = connect_nonce
        if addr in self.recent_disconnect_addrs:
            _deadline, last_sequence = self.recent_disconnect_addrs[addr]
            client.delay_nonconnect_packets(POST_CONNECT_GUARD_SECS)
            client.ack_tracker.local_sequence = last_sequence
        self.session_mgr.create(addr, client.client_id, token=client.session_token)
        self.game_state.add_entity(client.client_id)
        if self.host_client_id is None:
            self.host_client_id = client.client_id

        data, _ = self._make_client_packet(
            client,
            PacketType.CONNECT_ACK,
            self._build_connect_ack_payload(client, connect_nonce),
        )
        self._sendto(data, addr)
        client.bytes_sent += len(data)
        self.pending_secure_handshakes.pop(addr, None)
        self._broadcast_presence_event(RELIABLE_EVENT_JOIN, client.client_id)
        if self.game_state.game_started:
            self._send_reliable_event_to_client(client, RELIABLE_EVENT_GAME_START, 0)
            self._send_score_sync(client)
            if self.match_over_timer > 0.0 and self.match_winner_id is not None:
                self._send_reliable_event_to_client(
                    client, RELIABLE_EVENT_MATCH_OVER, self.match_winner_id
                )
        self._log(f"[SERVER] Client {client.client_id} connected from {addr}")

    def _handle_input(self, pkt: Packet, addr: tuple):
        client = self.client_mgr.get_by_address(addr)
        if client is None or client.client_id in self.pending_kicks:
            return
        if time.monotonic() < client.accept_packets_after:
            return

        payload = pkt.payload
        pending = client.pending_inputs

        def _queue_input(input_tick: int, move_x: float, move_y: float, actions: int):
            # Fix stale redundant bundles: drop anything already processed before enqueueing.
            if input_tick <= client.last_processed_input_seq:
                return
            if any(existing[0] == input_tick for existing in pending):
                return
            pending.append((input_tick, move_x, move_y, actions))

        if len(payload) > 0 and len(payload) != INPUT_SIZE:
            count = payload[0]
            # Validate count doesn't exceed available payload
            max_inputs = (len(payload) - 1) // INPUT_SIZE
            count = min(count, max_inputs)
            offset = 1
            for _ in range(count):
                if offset + INPUT_SIZE > len(payload):
                    break
                input_tick, move_x, move_y, actions = struct.unpack(
                    INPUT_FORMAT, payload[offset : offset + INPUT_SIZE]
                )
                _queue_input(input_tick, move_x, move_y, actions)
                offset += INPUT_SIZE
        elif len(payload) >= INPUT_SIZE:
            input_tick, move_x, move_y, actions = struct.unpack(
                INPUT_FORMAT, payload[:INPUT_SIZE]
            )
            _queue_input(input_tick, move_x, move_y, actions)

        pending.sort(key=lambda entry: entry[0])

    def _handle_ping(self, pkt: Packet, addr: tuple):
        client = self.client_mgr.get_by_address(addr)
        if (
            client is None
            or client.client_id in self.pending_kicks
            or len(pkt.payload) < PING_SIZE
        ):
            return
        if time.monotonic() < client.accept_packets_after:
            return

        sent_time = struct.unpack(PING_FORMAT, pkt.payload[:PING_SIZE])[0]
        rtt_ms = (time.perf_counter() - sent_time) * 1000.0
        client.smoothed_rtt_ms += (rtt_ms - client.smoothed_rtt_ms) * 0.125

        data, _ = self._make_client_packet(
            client, PacketType.PONG, pkt.payload[:PING_SIZE]
        )
        self._sendto(data, addr)
        client.bytes_sent += len(data)

    def _handle_heartbeat(self, addr: tuple):
        client = self.client_mgr.get_by_address(addr)
        if client is None or client.client_id in self.pending_kicks:
            return
        if time.monotonic() < client.accept_packets_after:
            return

        # Heartbeats are now real keep-alives instead of a no-op.
        data, _ = self._make_client_packet(client, PacketType.HEARTBEAT)
        self._sendto(data, addr)
        client.bytes_sent += len(data)

    def _handle_reliable_event(self, pkt: Packet, addr: tuple):
        client = self.client_mgr.get_by_address(addr)
        if client is None or not pkt.payload:
            return
        if time.monotonic() < client.accept_packets_after:
            return

        event_type = pkt.payload[0]

        if len(pkt.payload) < struct.calcsize(RELIABLE_EVENT_FORMAT):
            return

        event_type, _subject_client_id = struct.unpack(
            RELIABLE_EVENT_FORMAT, pkt.payload[: struct.calcsize(RELIABLE_EVENT_FORMAT)]
        )
        if (
            event_type == RELIABLE_EVENT_GAME_START
            and client.client_id == self.host_client_id
            and not self.game_state.game_started
            and self.match_over_timer <= 0.0
        ):
            self.game_state.game_started = True
            self.match_elapsed = 0.0
            self._broadcast_game_start()
        elif (
            event_type == RELIABLE_EVENT_KICK_PLAYER
            and client.client_id == self.host_client_id
            and _subject_client_id != client.client_id
        ):
            target_client = self.client_mgr.clients.get(_subject_client_id)
            if (
                target_client is not None
                and target_client.client_id not in self.pending_kicks
                and target_client.session_token  # Validate token exists
            ):
                kick_deadline = time.monotonic() + KICK_GRACE_SECS
                self.pending_kicks[target_client.client_id] = {
                    "host_id": client.client_id,
                    "deadline": kick_deadline,
                }
                self.kicked_tokens[target_client.session_token] = (
                    time.monotonic() + KICK_TOKEN_BLOCK_SECS
                )
                target_client.pending_inputs.clear()
                self.game_state.remove_entity(target_client.client_id)
                self._send_reliable_event_to_client(
                    target_client,
                    RELIABLE_EVENT_KICKED,
                    client.client_id,
                )
                self._send_disconnect_notice(
                    target_client.address,
                    DISCONNECT_REASON_KICKED,
                    client=target_client,
                )
                self._broadcast_presence_event(
                    RELIABLE_EVENT_LEAVE,
                    target_client.client_id,
                    exclude_client_id=target_client.client_id,
                )
                self._log(
                    f"[SERVER] Client {target_client.client_id} marked for kick by host {client.client_id}"
                )

    def _handle_disconnect(self, pkt: Packet, addr: tuple):
        client = self.client_mgr.get_by_address(addr)
        if client:
            self._remove_client(
                client,
                "disconnected",
                broadcast_leave=client.client_id not in self.pending_kicks,
            )
            return

        reconnect_token, connect_nonce = self._parse_connect_request(pkt.payload)
        if reconnect_token:
            client = self.client_mgr.get_by_token(reconnect_token)
            if client is not None and connect_nonce == client.last_connect_nonce:
                self._remove_client(
                    client,
                    "disconnected",
                    broadcast_leave=client.client_id not in self.pending_kicks,
                )
                return

        self.pending_secure_handshakes.pop(addr, None)

    def simulate_tick(self):
        tick_start = time.perf_counter()
        self.game_state.tick = self.current_tick

        for client in self.client_mgr.all_clients():
            if client.client_id in self.pending_kicks:
                client.pending_inputs.clear()
                continue
            applied_input = False

            if not self.game_state.game_started or self.match_over_timer > 0.0:
                client.pending_inputs.clear()
                self.game_state.apply_input(client.client_id, 0.0, 0.0, 0, self.dt)
                continue

            while (
                client.pending_inputs
                and client.pending_inputs[0][0] <= client.last_processed_input_seq
            ):
                client.pending_inputs.pop(0)

            if client.pending_inputs:
                input_tick, move_x, move_y, actions = client.pending_inputs.pop(0)

                # Fix burst movement: consume at most one fresh input per client per tick.
                if input_tick > client.last_processed_input_seq:
                    self.game_state.apply_input(
                        client.client_id, move_x, move_y, actions, self.dt
                    )
                    client.last_processed_input_seq = input_tick
                    applied_input = True

            if not applied_input:
                self.game_state.apply_input(client.client_id, 0.0, 0.0, 0, self.dt)

        self.game_state.spawn_modifiers()
        self.game_state.collect_modifiers()

        if self.game_state.game_started and self.match_over_timer <= 0.0:
            score_events = self.game_state.resolve_collisions(self.dt)
            self.game_state.queue_respawns()
            self.game_state.tick_respawns()
            for killer_id, victim_id in score_events:
                self._broadcast_score_update(killer_id, victim_id)

            self.match_elapsed += self.dt

            if self.match_over_timer <= 0.0:
                winner_id = self.game_state.check_win_condition()

                if winner_id is None and MATCH_DURATION_SECS > 0:
                    if self.match_elapsed >= MATCH_DURATION_SECS:
                        winner_id = max(
                            self.game_state.scores,
                            key=lambda entity_id: (
                                self.game_state.scores[entity_id],
                                -entity_id,
                            ),
                            default=None,
                        )

                if winner_id is not None:
                    self._broadcast_match_over(winner_id)
                    self.match_over_timer = MATCH_OVER_DISPLAY_SECS

        was_over = self.match_over_timer > 0.0
        if was_over:
            self.match_over_timer = max(0.0, self.match_over_timer - self.dt)
            if self.match_over_timer <= 0.0:
                self._reset_match()

        timed_out_clients = [
            client for client in self.client_mgr.all_clients() if client.is_timed_out()
        ]
        for client in timed_out_clients:
            if client.client_id in self.client_mgr.clients:
                self._remove_client(
                    client,
                    "timed out",
                    broadcast_leave=client.client_id not in self.pending_kicks,
                    remove_session=False,
                )

        now = time.monotonic()
        for client_id, info in list(self.pending_kicks.items()):
            if now < info["deadline"]:
                continue
            client = self.client_mgr.clients.get(client_id)
            if client is not None:
                self._remove_client(
                    client, "kick grace period elapsed", broadcast_leave=False
                )

        expired_ids = self.session_mgr.expire_sessions()
        expired_tokens = list(self.session_mgr.last_expired_tokens)
        for index, client_id in enumerate(expired_ids):
            client = self.client_mgr.clients.get(client_id)
            if client is not None:
                self._remove_client(
                    client,
                    "expired after session timeout",
                    broadcast_leave=client.client_id not in self.pending_kicks,
                )
            else:
                if index < len(expired_tokens):
                    self.idle_client_state.pop(expired_tokens[index], None)
                self.game_state.scores.pop(client_id, None)

        if expired_ids:
            for active_client in self.client_mgr.all_clients():
                self._send_score_sync(active_client)

        tick_duration = (time.perf_counter() - tick_start) * 1000.0
        self.metrics.log_tick_time(self.current_tick, tick_duration)

    def send_snapshots(self):
        if not self._active_client_ids():
            return
        if (
            not self.game_state.game_started
            and self.current_tick % self.lobby_snapshot_stride != 0
        ):
            return

        snapshot = self.game_state.get_snapshot()
        for entity_id, entity in snapshot.entities.items():
            client = self.client_mgr.clients.get(entity_id)
            entity.ping_ms = (
                int(min(65535, round(client.smoothed_rtt_ms))) if client else 0
            )
        base_payload = snapshot.serialize()
        server_send_time = time.perf_counter()

        for client in self.client_mgr.all_clients():
            if client.client_id in self.pending_kicks:
                continue
            # The trailer carries reconciliation state, a send timestamp, and match time.
            payload = base_payload + struct.pack(
                SNAPSHOT_TRAILER_FORMAT,
                client.last_processed_input_seq,
                server_send_time,
                self.match_elapsed,
                self.host_client_id or 0,
            )
            data, _ = self._make_client_packet(client, PacketType.SNAPSHOT, payload)
            self._sendto(data, client.address)
            client.bytes_sent += len(data)

    def run(self):
        self.running = True
        self._log(
            f"[SERVER] Started on {self.host}:{self.port} @ {self.tick_rate} Hz "
            f"(dt={self.dt:.4f}s)"
        )

        previous_sigterm = None
        previous_sigint = None
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            previous_sigint = signal.getsignal(signal.SIGINT)

            def _request_shutdown(_signum, _frame):
                self.running = False

            signal.signal(signal.SIGTERM, _request_shutdown)
            signal.signal(signal.SIGINT, _request_shutdown)

        next_tick_time = time.perf_counter()
        last_stats_time = time.perf_counter()
        last_metrics_time = time.perf_counter()
        last_metrics_bytes_sent = 0
        last_metrics_bytes_recv = 0
        stats_interval = 5.0
        metrics_interval = 1.0

        try:
            while self.running:
                now = time.perf_counter()

                self.receive_all_packets()

                if self.net_sim:
                    self.net_sim.flush()

                for channel in self.reliable_channels.values():
                    channel.tick()

                while now >= next_tick_time:
                    self.simulate_tick()
                    self.send_snapshots()
                    self.current_tick += 1
                    next_tick_time += self.dt

                if now - last_metrics_time >= metrics_interval:
                    self.metrics.log_bandwidth(
                        self.total_bytes_sent - last_metrics_bytes_sent,
                        self.total_bytes_recv - last_metrics_bytes_recv,
                    )
                    last_metrics_bytes_sent = self.total_bytes_sent
                    last_metrics_bytes_recv = self.total_bytes_recv

                    clients_list = list(self.client_mgr.all_clients())
                    if clients_list:
                        # Sample loss after detecting new losses so the metric is current.
                        for client in clients_list:
                            client.ack_tracker.detect_lost_packets()

                        total_acked = sum(
                            client.ack_tracker.total_acked for client in clients_list
                        )
                        total_lost = sum(
                            client.ack_tracker.total_lost for client in clients_list
                        )
                        total = total_acked + total_lost
                        loss = total_lost / total if total > 0 else 0.0
                        self.metrics.log_packet_loss(loss)

                    last_metrics_time = now

                if now - last_stats_time >= stats_interval:
                    self._log(
                        f"[SERVER] Tick {self.current_tick} | Clients: {self.client_mgr.count} | "
                        f"Sent: {self.total_bytes_sent / 1024:.1f} KB | "
                        f"Recv: {self.total_bytes_recv / 1024:.1f} KB"
                    )
                    last_stats_time = now

                sleep_time = next_tick_time - time.perf_counter()
                if sleep_time > 0.001:
                    time.sleep(sleep_time * 0.8)
                elif sleep_time > 0:
                    time.sleep(0.0001)

        except KeyboardInterrupt:
            self._log("\n[SERVER] Shutting down...")
        finally:
            self.running = False
            if threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGTERM, previous_sigterm)
                signal.signal(signal.SIGINT, previous_sigint)

            for client in self.client_mgr.all_clients():
                self._send_disconnect_notice(
                    client.address,
                    DISCONNECT_REASON_NONE,
                )
                self._send_disconnect_notice(
                    client.address,
                    DISCONNECT_REASON_NONE,
                    client=client,
                )
            self.sock.close()
            self.metrics.save("server_metrics.json")
            summary = self.metrics.get_summary()
            if summary:
                self._log(f"[SERVER] Metrics summary: {summary}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Game Networking Engine Server")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port")
    parser.add_argument(
        "--tick-rate", type=int, default=DEFAULT_TICK_RATE, help="Server tick rate (Hz)"
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

    # Room key is optional - server can run secure or unsecured
    server = GameServer(
        host=args.host,
        port=args.port,
        tick_rate=args.tick_rate,
        loss_sim=args.loss,
        latency_sim=args.latency,
        room_key=args.room_key,
    )
    server.run()


if __name__ == "__main__":
    main()
