"""
Unit tests for packet serialization/deserialization.
"""

import unittest

from common.packet import Packet, PacketType, HEADER_SIZE, PROTOCOL_ID


class TestPacketSerialization(unittest.TestCase):
    """Test the custom binary packet protocol."""

    def test_roundtrip_basic(self):
        """Packet should survive serialize → deserialize round trip."""
        original = Packet(PacketType.INPUT, sequence=42, ack=41,
                          ack_bitfield=0xFFFFFFFF, payload=b'\x01\x02\x03')
        data = original.serialize()
        restored = Packet.deserialize(data)

        self.assertEqual(restored.packet_type, PacketType.INPUT)
        self.assertEqual(restored.sequence, 42)
        self.assertEqual(restored.ack, 41)
        self.assertEqual(restored.ack_bitfield, 0xFFFFFFFF)
        self.assertEqual(restored.payload, b'\x01\x02\x03')

    def test_roundtrip_empty_payload(self):
        """Packets with no payload should work."""
        original = Packet(PacketType.CONNECT_REQ, sequence=1, ack=0,
                          ack_bitfield=0)
        data = original.serialize()
        restored = Packet.deserialize(data)

        self.assertEqual(restored.packet_type, PacketType.CONNECT_REQ)
        self.assertEqual(restored.payload, b'')

    def test_all_packet_types(self):
        """Every packet type should serialize/deserialize correctly."""
        for ptype in [PacketType.CONNECT_REQ, PacketType.CONNECT_ACK,
                      PacketType.DISCONNECT, PacketType.INPUT,
                      PacketType.SNAPSHOT, PacketType.PING,
                      PacketType.PONG, PacketType.RELIABLE_EVENT,
                      PacketType.HEARTBEAT]:
            pkt = Packet(ptype, sequence=100, ack=99, ack_bitfield=0xABCD)
            data = pkt.serialize()
            restored = Packet.deserialize(data)
            self.assertEqual(restored.packet_type, ptype,
                             f"Failed for type {PacketType.name(ptype)}")

    def test_invalid_protocol_id(self):
        """Invalid protocol ID should raise ValueError."""
        data = b'\x00\x00\x00\x00' + b'\x00' * 20
        with self.assertRaises(ValueError):
            Packet.deserialize(data)

    def test_too_short(self):
        """Truncated data should raise ValueError."""
        with self.assertRaises(ValueError):
            Packet.deserialize(b'\x01\x02')

    def test_sequence_wrapping(self):
        """Sequence numbers should wrap at 16 bits."""
        pkt = Packet(PacketType.HEARTBEAT, sequence=0xFFFF)
        data = pkt.serialize()
        restored = Packet.deserialize(data)
        self.assertEqual(restored.sequence, 0xFFFF)

        pkt2 = Packet(PacketType.HEARTBEAT, sequence=0x10000)  # Should wrap
        data2 = pkt2.serialize()
        restored2 = Packet.deserialize(data2)
        self.assertEqual(restored2.sequence, 0)

    def test_large_payload(self):
        """Large payloads should work correctly."""
        payload = b'\xAA' * 1000
        pkt = Packet(PacketType.SNAPSHOT, sequence=1, ack=0,
                     ack_bitfield=0, payload=payload)
        data = pkt.serialize()
        restored = Packet.deserialize(data)
        self.assertEqual(len(restored.payload), 1000)
        self.assertEqual(restored.payload, payload)

    def test_header_size(self):
        """Header should be exactly 15 bytes."""
        self.assertEqual(HEADER_SIZE, 15)

    def test_protocol_id_value(self):
        """Protocol ID should be 'GAME' in ASCII."""
        self.assertEqual(PROTOCOL_ID, 0x47414D45)


if __name__ == '__main__':
    unittest.main()
