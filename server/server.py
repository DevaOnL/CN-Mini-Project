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
    CONNECTION_EPOCH_SIZE,
    INPUT_FORMAT,
    INPUT_SIZE,
    PING_FORMAT,
    PING_SIZE,
    CONNECT_TOKEN_SIZE,
    unpack_connect_request,
    packet_uses_connection_epoch,
    pack_connection_epoch,
    unpack_connection_epoch,
    DISCONNECT_REASON_FORMAT,
    DISCONNECT_REASON_NONE,
    DISCONNECT_REASON_KICKED,
    DISCONNECT_REASON_AUTH_FAILED,
)
from common.dtls import DtlsServerTransport, ensure_server_certificate
from common.net import create_server_socket, NetworkSimulator
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
        cert_file: str | None = None,
        key_file: str | None = None,
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
        normalized = (room_key or "").strip()
        if not normalized:
            raise ValueError("Room key is required for DTLS sessions.")
        self.room_key = normalized

        cert_info = ensure_server_certificate(
            cert_file,
            key_file,
            common_name=host if host not in {"0.0.0.0", "::"} else None,
        )
        self.cert_file = cert_info.cert_file
        self.key_file = cert_info.key_file
        self.certificate_fingerprint = cert_info.fingerprint
        self.dtls_transport = DtlsServerTransport(self.cert_file, self.key_file)

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

    def _send_transport_bytes(self, addr: tuple, data: bytes) -> bool:
        try:
            self.dtls_transport.send_packet(addr, data)
        except RuntimeError:
            return False
        self._drain_dtls_transport()
        return True

    def _send_client_packet(
        self,
        client: ConnectedClient,
        packet_type: int,
        payload: bytes = b"",
        *,
        track_send: bool = True,
    ) -> bool:
        data, _ = self._make_client_packet(
            client,
            packet_type,
            payload,
            track_send=track_send,
        )
        sent = self._send_transport_bytes(client.address, data)
        if sent:
            client.bytes_sent += len(data)
        return sent

    def _send_reliable_datagram(self, data: bytes, addr: tuple):
        sent = self._send_transport_bytes(addr, data)
        if not sent:
            return
        client = self.client_mgr.get_by_address(addr)
        if client is not None:
            client.bytes_sent += len(data)

    def _get_reliable_channel(self, client: ConnectedClient) -> ReliableChannel:
        channel = self.reliable_channels.get(client.client_id)
        if channel is None:
            channel = ReliableChannel(
                self._send_reliable_datagram,
                client.ack_tracker.on_packet_sent,
            )
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

    def _drain_dtls_transport(self):
        self.dtls_transport.poll()
        while True:
            progressed = False
            for addr, datagram in self.dtls_transport.drain_outbound():
                self._sendto(datagram, addr)
                progressed = True
            for addr, packet_bytes in self.dtls_transport.drain_packets():
                self._handle_packet(packet_bytes, addr)
                progressed = True
            if not progressed:
                break

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
    ) -> bytes:
        pkt = Packet(packet_type, sequence, ack, ack_bitfield, payload)
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
        )
        if track_send:
            client.ack_tracker.on_packet_sent(seq)
        return data, seq

    def _parse_connect_request(self, payload: bytes) -> tuple[str | None, int, str] | None:
        return unpack_connect_request(payload)

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

    def _send_packet_to_addr(
        self,
        addr: tuple,
        packet_type: int,
        payload: bytes = b"",
    ) -> bool:
        packet_bytes = Packet(packet_type, payload=payload).serialize()
        try:
            self.dtls_transport.send_packet(addr, packet_bytes)
        except RuntimeError:
            return False
        self._drain_dtls_transport()
        return True

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
        self.dtls_transport.remove_peer(client.address)
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
        return channel.send(data, client.address)

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
    ):
        payload = struct.pack(DISCONNECT_REASON_FORMAT, reason_code)
        if client is not None:
            data, _ = self._make_client_packet(
                client,
                PacketType.DISCONNECT,
                payload,
            )
            try:
                self.dtls_transport.send_packet(addr, data)
            except RuntimeError:
                return
            self._drain_dtls_transport()
        else:
            self._send_packet_to_addr(addr, PacketType.DISCONNECT, payload)

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
        # Use time budget instead of fixed count to prevent packet starvation
        deadline = time.perf_counter() + 0.01  # 10ms max per tick
        packets_processed = 0
        while time.perf_counter() < deadline and packets_processed < 10000:
            try:
                data, addr = self.sock.recvfrom(DEFAULT_BUFFER_SIZE)
                self.total_bytes_recv += len(data)
                client = self.client_mgr.get_by_address(addr)
                if client is not None:
                    client.bytes_received += len(data)
                self.dtls_transport.receive_datagram(addr, data)
                self._drain_dtls_transport()
                packets_processed += 1
            except BlockingIOError:
                break
            except OSError:
                break
        self._drain_dtls_transport()

    def _handle_packet(self, data: bytes, addr: tuple):
        try:
            pkt = Packet.deserialize(data)
        except ValueError:
            return

        client = self.client_mgr.get_by_address(addr)

        if pkt.packet_type != PacketType.CONNECT_REQ:
            if pkt.packet_type == PacketType.DISCONNECT:
                if client is not None:
                    if len(pkt.payload) >= CONNECTION_EPOCH_SIZE:
                        unpacked = unpack_connection_epoch(pkt.payload)
                        if unpacked is None:
                            return
                        packet_epoch, payload = unpacked
                        if packet_epoch != client.connection_epoch:
                            return
                        pkt.payload = payload
                elif len(pkt.payload) < struct.calcsize(DISCONNECT_REASON_FORMAT):
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
            client.ack_tracker.on_packet_received(pkt.sequence)

            # Fix server-side loss metrics: consume the client's ack fields too.
            if client.ack_tracker.should_process_ack(pkt.ack, pkt.ack_bitfield):
                client.ack_tracker.on_ack_received(pkt.ack, pkt.ack_bitfield)
                self._ack_reliable_sequences(client, pkt.ack, pkt.ack_bitfield)

        session = self.session_mgr.get_by_addr(addr)
        if session and (client is None or client.client_id not in self.pending_kicks):
            session.touch()

    def _send_connect_ack(self, client: ConnectedClient, connect_nonce: int) -> bool:
        return self._send_client_packet(
            client,
            PacketType.CONNECT_ACK,
            self._build_connect_ack_payload(client, connect_nonce),
        )

    def _handle_connect(self, pkt: Packet, addr: tuple):
        self._prune_kicked_tokens()
        self._prune_recent_disconnect_addrs()
        parsed = self._parse_connect_request(pkt.payload)
        if parsed is None:
            return
        reconnect_token, connect_nonce, room_key = parsed

        if room_key != self.room_key:
            self._send_disconnect_notice(addr, DISCONNECT_REASON_AUTH_FAILED)
            return

        if not reconnect_token and addr in self.recent_disconnect_addrs:
            return

        if connect_nonce == 0:
            return

        if reconnect_token and reconnect_token in self.kicked_tokens:
            self._send_disconnect_notice(addr, DISCONNECT_REASON_KICKED)
            return

        client = self.client_mgr.get_by_address(addr)
        if client is not None:
            if client.client_id in self.pending_kicks:
                self._send_reliable_event_to_client(
                    client,
                    RELIABLE_EVENT_KICKED,
                    self.pending_kicks[client.client_id]["host_id"],
                )
                self._send_disconnect_notice(addr, DISCONNECT_REASON_KICKED)
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
            client.delay_nonconnect_packets(POST_CONNECT_GUARD_SECS)
            self._send_connect_ack(client, connect_nonce)
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

                    self._send_connect_ack(client, connect_nonce)
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
                        addr, DISCONNECT_REASON_KICKED
                    )
                    return
                if connect_nonce and connect_nonce <= client.last_connect_nonce:
                    return
                old_addr = client.address
                if self.session_mgr.reconnect(reconnect_token, addr) is None:
                    return
                if old_addr != addr:
                    self.dtls_transport.remove_peer(old_addr)
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
                client.delay_nonconnect_packets(POST_CONNECT_GUARD_SECS)
                self._send_connect_ack(client, connect_nonce)
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

            self._send_disconnect_notice(addr, DISCONNECT_REASON_NONE)
            return

        client = self.client_mgr.add_client(addr)
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

        self._send_connect_ack(client, connect_nonce)
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

        self._send_client_packet(client, PacketType.PONG, pkt.payload[:PING_SIZE])

    def _handle_heartbeat(self, addr: tuple):
        client = self.client_mgr.get_by_address(addr)
        if client is None or client.client_id in self.pending_kicks:
            return
        if time.monotonic() < client.accept_packets_after:
            return

        # Heartbeats are now real keep-alives instead of a no-op.
        self._send_client_packet(client, PacketType.HEARTBEAT)

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

        parsed = self._parse_connect_request(pkt.payload)
        if parsed is not None:
            reconnect_token, connect_nonce, _room_key = parsed
            client = self.client_mgr.get_by_token(reconnect_token)
            if client is not None and connect_nonce == client.last_connect_nonce:
                self._remove_client(
                    client,
                    "disconnected",
                    broadcast_leave=client.client_id not in self.pending_kicks,
                )
                return

        self.dtls_transport.remove_peer(addr)

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
            self._send_client_packet(client, PacketType.SNAPSHOT, payload)

    def run(self):
        self.running = True
        self._log(
            f"[SERVER] Started on {self.host}:{self.port} @ {self.tick_rate} Hz "
            f"(dt={self.dt:.4f}s) | DTLS fingerprint: {self.certificate_fingerprint}"
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
            self.dtls_transport.close()
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
        help="Shared room key required after DTLS handshake",
    )
    parser.add_argument(
        "--cert-file",
        default=None,
        help="PEM certificate file for DTLS hosting",
    )
    parser.add_argument(
        "--key-file",
        default=None,
        help="PEM private key file for DTLS hosting",
    )
    args = parser.parse_args()

    if not (args.room_key or "").strip():
        parser.error("--room-key is required.")

    server = GameServer(
        host=args.host,
        port=args.port,
        tick_rate=args.tick_rate,
        loss_sim=args.loss,
        latency_sim=args.latency,
        room_key=args.room_key,
        cert_file=args.cert_file,
        key_file=args.key_file,
    )
    server.run()


if __name__ == "__main__":
    main()
