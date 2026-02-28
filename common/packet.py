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
HEADER_FORMAT = '!I H H I B H'
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 15 bytes


class PacketType:
    """Packet type identifiers."""
    CONNECT_REQ    = 0x01
    CONNECT_ACK    = 0x02
    DISCONNECT     = 0x03
    INPUT          = 0x04
    SNAPSHOT       = 0x05
    PING           = 0x06
    PONG           = 0x07
    RELIABLE_EVENT = 0x08
    HEARTBEAT      = 0x09

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


# Input payload: tick_number(u32), move_x(f32), move_y(f32), actions(u8)
INPUT_FORMAT = '!I f f B'
INPUT_SIZE = struct.calcsize(INPUT_FORMAT)

# Entity state: entity_id(u8), x(f32), y(f32), vx(f32), vy(f32), health(f32)
ENTITY_STATE_FORMAT = '!B f f f f f'
ENTITY_STATE_SIZE = struct.calcsize(ENTITY_STATE_FORMAT)

# Snapshot header: tick(u32), entity_count(u8)
SNAPSHOT_HEADER_FORMAT = '!I B'
SNAPSHOT_HEADER_SIZE = struct.calcsize(SNAPSHOT_HEADER_FORMAT)

# Ping/Pong payload: timestamp(f64)
PING_FORMAT = '!d'
PING_SIZE = struct.calcsize(PING_FORMAT)


class Packet:
    """
    Represents a single game networking packet.
    Handles serialization/deserialization with the custom binary protocol.
    """

    def __init__(self, packet_type: int, sequence: int = 0, ack: int = 0,
                 ack_bitfield: int = 0, payload: bytes = b''):
        self.protocol_id = PROTOCOL_ID
        self.packet_type = packet_type
        self.sequence = sequence & 0xFFFF        # Wrap at 16-bit
        self.ack = ack & 0xFFFF
        self.ack_bitfield = ack_bitfield & 0xFFFFFFFF
        self.payload = payload

    def serialize(self) -> bytes:
        """Serialize packet to bytes for transmission."""
        header = struct.pack(
            HEADER_FORMAT,
            self.protocol_id,
            self.sequence,
            self.ack,
            self.ack_bitfield,
            self.packet_type,
            len(self.payload)
        )
        return header + self.payload

    @staticmethod
    def deserialize(data: bytes) -> 'Packet':
        """Deserialize bytes to a Packet object."""
        if len(data) < HEADER_SIZE:
            raise ValueError(f"Packet too short: {len(data)} < {HEADER_SIZE}")

        proto_id, seq, ack, ack_bits, ptype, plen = struct.unpack(
            HEADER_FORMAT, data[:HEADER_SIZE]
        )

        if proto_id != PROTOCOL_ID:
            raise ValueError(f"Invalid protocol ID: {proto_id:#x}")

        payload = data[HEADER_SIZE:HEADER_SIZE + plen]
        if len(payload) < plen:
            raise ValueError(f"Payload truncated: got {len(payload)}, expected {plen}")

        return Packet(ptype, seq, ack, ack_bits, payload)

    def __repr__(self):
        return (f"Packet(type={PacketType.name(self.packet_type)}, "
                f"seq={self.sequence}, ack={self.ack}, "
                f"payload_len={len(self.payload)})")
