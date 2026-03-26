"""
Custom packet protocol for the Real-Time Multiplayer Game Networking Engine.

Packet Header (15 bytes):
    Protocol ID    (4 bytes) - Magic number 0x47414D45 ("GAME")
    Sequence       (2 bytes) - Outgoing sequence number
    Ack            (2 bytes) - Latest received remote sequence
    Ack Bitfield   (4 bytes) - Bitfield acking previous 32 packets
    Packet Type    (1 byte)  - Type identifier
    Payload Length (2 bytes) - Length of payload data
"""

import struct

PROTOCOL_ID = 0x47414D45  # "GAME" in ASCII

# Network byte order: uint32, uint16, uint16, uint32, uint8, uint16
HEADER_FORMAT = "!I H H I B H"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 15 bytes


class PacketType:
    """Packet type identifiers."""

    CONNECT_REQ = 0x01
    CONNECT_ACK = 0x02
    DISCONNECT = 0x03
    INPUT = 0x04
    SNAPSHOT = 0x05
    PING = 0x06
    PONG = 0x07
    RELIABLE_EVENT = 0x08
    HEARTBEAT = 0x09

    _NAMES = {
        0x01: "CONNECT_REQ",
        0x02: "CONNECT_ACK",
        0x03: "DISCONNECT",
        0x04: "INPUT",
        0x05: "SNAPSHOT",
        0x06: "PING",
        0x07: "PONG",
        0x08: "RELIABLE_EVENT",
        0x09: "HEARTBEAT",
    }

    @classmethod
    def name(cls, ptype: int) -> str:
        return cls._NAMES.get(ptype, f"UNKNOWN({ptype:#x})")


CONNECTION_EPOCH_FORMAT = "!I"
CONNECTION_EPOCH_SIZE = struct.calcsize(CONNECTION_EPOCH_FORMAT)
EPOCH_PROTECTED_PACKET_TYPES = frozenset(
    {
        PacketType.DISCONNECT,
        PacketType.INPUT,
        PacketType.SNAPSHOT,
        PacketType.PING,
        PacketType.PONG,
        PacketType.RELIABLE_EVENT,
        PacketType.HEARTBEAT,
    }
)


def packet_uses_connection_epoch(packet_type: int) -> bool:
    return packet_type in EPOCH_PROTECTED_PACKET_TYPES


def pack_connection_epoch(epoch: int, payload: bytes = b"") -> bytes:
    return struct.pack(CONNECTION_EPOCH_FORMAT, epoch & 0xFFFFFFFF) + payload


def unpack_connection_epoch(payload: bytes) -> tuple[int, bytes] | None:
    if len(payload) < CONNECTION_EPOCH_SIZE:
        return None
    epoch = struct.unpack(CONNECTION_EPOCH_FORMAT, payload[:CONNECTION_EPOCH_SIZE])[0]
    return epoch, payload[CONNECTION_EPOCH_SIZE:]


# Input payload: tick_number(u32), move_x(f32), move_y(f32), actions(u8)
INPUT_FORMAT = "!I f f B"
INPUT_SIZE = struct.calcsize(INPUT_FORMAT)

# Entity state: entity_id(u16), x(f32), y(f32), vx(f32), vy(f32), health(f32), ping_ms(u16), respawn_ticks(u16), effect_flags(u8), dash_cooldown(f32), dash_timer(f32)
ENTITY_STATE_FORMAT = "!H f f f f f H H B f f"
ENTITY_STATE_SIZE = struct.calcsize(ENTITY_STATE_FORMAT)

# Snapshot header: tick(u32), entity_count(u16), modifier_count(u16)
SNAPSHOT_HEADER_FORMAT = "!I H H"
SNAPSHOT_HEADER_SIZE = struct.calcsize(SNAPSHOT_HEADER_FORMAT)

# Modifier state: modifier_type(u8), x(f32), y(f32)
MODIFIER_STATE_FORMAT = "!B f f"
MODIFIER_STATE_SIZE = struct.calcsize(MODIFIER_STATE_FORMAT)

# Ping/Pong payload: timestamp(f64)
PING_FORMAT = "!d"
PING_SIZE = struct.calcsize(PING_FORMAT)

# Connect request payload:
#     session_token(16 bytes, ASCII, NUL padded)
#     connect_nonce(u32)
#     room_key_length(u8)
#     player_name_length(u8)
#     room_key_utf8(room_key_length bytes)
#     player_name_utf8(player_name_length bytes)
CONNECT_TOKEN_SIZE = 16
PLAYER_NAME_MAX_BYTES = 64
CONNECT_REQ_PREFIX_FORMAT = f"!{CONNECT_TOKEN_SIZE}sIBB"
CONNECT_REQ_PREFIX_SIZE = struct.calcsize(CONNECT_REQ_PREFIX_FORMAT)

PLAYER_ALIAS_ENTRY_FORMAT = "!HB"
PLAYER_ALIAS_ENTRY_SIZE = struct.calcsize(PLAYER_ALIAS_ENTRY_FORMAT)

# Optional disconnect payload: reason(u8)
DISCONNECT_REASON_FORMAT = "!B"
DISCONNECT_REASON_SIZE = struct.calcsize(DISCONNECT_REASON_FORMAT)
DISCONNECT_REASON_NONE = 0x00
DISCONNECT_REASON_KICKED = 0x01
DISCONNECT_REASON_AUTH_FAILED = 0x03


def pack_connect_request(
    session_token: str | None,
    connect_nonce: int,
    room_key: str,
    player_name: str = "Player",
) -> bytes:
    token_bytes = (session_token or "").encode("ascii", errors="ignore")[
        :CONNECT_TOKEN_SIZE
    ]
    token_bytes = token_bytes.ljust(CONNECT_TOKEN_SIZE, b"\x00")
    room_key_bytes = room_key.encode("utf-8")
    player_name_bytes = _trim_utf8(player_name or "Player", PLAYER_NAME_MAX_BYTES)
    if len(room_key_bytes) > 0xFF:
        raise ValueError("Room key must be at most 255 UTF-8 bytes.")
    if not player_name_bytes:
        player_name_bytes = b"Player"
    return struct.pack(
        CONNECT_REQ_PREFIX_FORMAT,
        token_bytes,
        connect_nonce & 0xFFFFFFFF,
        len(room_key_bytes),
        len(player_name_bytes),
    ) + room_key_bytes + player_name_bytes


def unpack_connect_request(payload: bytes) -> tuple[str | None, int, str, str] | None:
    if len(payload) < CONNECT_REQ_PREFIX_SIZE:
        return None
    token_bytes, connect_nonce, room_key_length, player_name_length = struct.unpack(
        CONNECT_REQ_PREFIX_FORMAT,
        payload[:CONNECT_REQ_PREFIX_SIZE],
    )
    variable_bytes = payload[CONNECT_REQ_PREFIX_SIZE:]
    expected_length = room_key_length + player_name_length
    if len(variable_bytes) != expected_length:
        return None
    room_key_bytes = variable_bytes[:room_key_length]
    player_name_bytes = variable_bytes[room_key_length:]
    try:
        room_key = room_key_bytes.decode("utf-8")
        player_name = player_name_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    token = token_bytes.decode("ascii", errors="ignore").rstrip("\x00")
    return token or None, connect_nonce, room_key, player_name or "Player"


def pack_player_aliases(player_aliases: dict[int, str]) -> bytes:
    payload = bytearray()
    for client_id, name in sorted(player_aliases.items()):
        name_bytes = _trim_utf8(name or "Player", PLAYER_NAME_MAX_BYTES)
        if not name_bytes:
            name_bytes = b"Player"
        payload.extend(
            struct.pack(
                PLAYER_ALIAS_ENTRY_FORMAT,
                client_id & 0xFFFF,
                len(name_bytes),
            )
        )
        payload.extend(name_bytes)
    return bytes(payload)


def unpack_player_aliases(payload: bytes) -> dict[int, str] | None:
    aliases: dict[int, str] = {}
    offset = 0
    while offset < len(payload):
        if offset + PLAYER_ALIAS_ENTRY_SIZE > len(payload):
            return None
        client_id, name_length = struct.unpack(
            PLAYER_ALIAS_ENTRY_FORMAT,
            payload[offset : offset + PLAYER_ALIAS_ENTRY_SIZE],
        )
        offset += PLAYER_ALIAS_ENTRY_SIZE
        if offset + name_length > len(payload):
            return None
        name_bytes = payload[offset : offset + name_length]
        offset += name_length
        try:
            aliases[client_id] = name_bytes.decode("utf-8") or "Player"
        except UnicodeDecodeError:
            return None
    return aliases


def _trim_utf8(text: str, max_bytes: int) -> bytes:
    encoded = (text or "").strip().encode("utf-8")
    if len(encoded) <= max_bytes:
        return encoded
    trimmed = encoded[:max_bytes]
    while trimmed:
        try:
            trimmed.decode("utf-8")
            return trimmed
        except UnicodeDecodeError:
            trimmed = trimmed[:-1]
    return b""


class Packet:
    """
    Represents a single game networking packet.
    Handles serialization/deserialization with the custom binary protocol.
    """

    def __init__(
        self,
        packet_type: int,
        sequence: int = 0,
        ack: int = 0,
        ack_bitfield: int = 0,
        payload: bytes = b"",
    ):
        self.protocol_id = PROTOCOL_ID
        self.packet_type = packet_type
        self.sequence = sequence & 0xFFFF  # Wrap at 16-bit
        self.ack = ack & 0xFFFF
        self.ack_bitfield = ack_bitfield & 0xFFFFFFFF
        self.payload = payload

    def serialize(self) -> bytes:
        """Serialize packet to bytes for transmission."""
        header = self.serialize_header()
        return header + self.payload

    def serialize_header(self, payload_length: int | None = None) -> bytes:
        if payload_length is None:
            payload_length = len(self.payload)
        return Packet.pack_header(
            self.sequence,
            self.ack,
            self.ack_bitfield,
            self.packet_type,
            payload_length,
        )

    @staticmethod
    def pack_header(
        sequence: int,
        ack: int,
        ack_bitfield: int,
        packet_type: int,
        payload_length: int,
        protocol_id: int = PROTOCOL_ID,
    ) -> bytes:
        return struct.pack(
            HEADER_FORMAT,
            protocol_id,
            sequence,
            ack,
            ack_bitfield,
            packet_type,
            payload_length,
        )

    @staticmethod
    def deserialize(data: bytes) -> "Packet":
        """Deserialize bytes to a Packet object."""
        if len(data) < HEADER_SIZE:
            raise ValueError(f"Packet too short: {len(data)} < {HEADER_SIZE}")

        proto_id, seq, ack, ack_bits, ptype, plen = struct.unpack(
            HEADER_FORMAT, data[:HEADER_SIZE]
        )

        if proto_id != PROTOCOL_ID:
            raise ValueError(f"Invalid protocol ID: {proto_id:#x}")

        payload = data[HEADER_SIZE : HEADER_SIZE + plen]
        if len(payload) < plen:
            raise ValueError(f"Payload truncated: got {len(payload)}, expected {plen}")

        return Packet(ptype, seq, ack, ack_bits, payload)

    def __repr__(self):
        return (
            f"Packet(type={PacketType.name(self.packet_type)}, "
            f"seq={self.sequence}, ack={self.ack}, "
            f"payload_len={len(self.payload)})"
        )
