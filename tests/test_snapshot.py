"""
Unit tests for snapshot serialization and game state.
"""

import struct
import unittest

from common.config import MODIFIER_DAMAGE_BOOST
from common.snapshot import ModifierState, Snapshot, EntityState
from server.game_state import GameState


class TestSnapshot(unittest.TestCase):
    """Test snapshot serialization/deserialization."""

    def test_roundtrip_empty(self):
        snap = Snapshot(tick=42, entities={})
        data = snap.serialize()
        restored = Snapshot.deserialize(data)
        self.assertEqual(restored.tick, 42)
        self.assertEqual(len(restored.entities), 0)

    def test_roundtrip_single_entity(self):
        entities = {1: EntityState(1, 100.0, 200.0, 10.0, -5.0, 75.0, 42, 9)}
        snap = Snapshot(tick=100, entities=entities)
        data = snap.serialize()
        restored = Snapshot.deserialize(data)

        self.assertEqual(restored.tick, 100)
        self.assertEqual(len(restored.entities), 1)
        e = restored.entities[1]
        self.assertAlmostEqual(e.x, 100.0, places=1)
        self.assertAlmostEqual(e.y, 200.0, places=1)
        self.assertAlmostEqual(e.vx, 10.0, places=1)
        self.assertAlmostEqual(e.vy, -5.0, places=1)
        self.assertAlmostEqual(e.health, 75.0, places=1)
        self.assertEqual(e.ping_ms, 42)
        self.assertEqual(e.respawn_ticks_remaining, 9)

    def test_roundtrip_multiple_entities(self):
        entities = {}
        for i in range(8):
            entities[i + 1] = EntityState(
                i + 1,
                float(i * 100),
                float(i * 50),
                float(i),
                float(-i),
                100.0 - i * 10,
                i * 5,
            )
        snap = Snapshot(tick=500, entities=entities)
        data = snap.serialize()
        restored = Snapshot.deserialize(data)

        self.assertEqual(restored.tick, 500)
        self.assertEqual(len(restored.entities), 8)

    def test_to_dict(self):
        entities = {1: EntityState(1, 10.0, 20.0, 1.0, 2.0, 100.0, 7)}
        snap = Snapshot(tick=1, entities=entities)
        d = snap.to_dict()
        self.assertEqual(d["tick"], 1)
        self.assertIn(1, d["entities"])
        self.assertAlmostEqual(d["entities"][1]["x"], 10.0)
        self.assertEqual(d["entities"][1]["ping_ms"], 7)

    def test_roundtrip_modifiers(self):
        snap = Snapshot(
            tick=4,
            entities={1: EntityState(1)},
            modifiers={1: ModifierState(MODIFIER_DAMAGE_BOOST, 120.0, 220.0)},
        )

        restored = Snapshot.deserialize(snap.serialize())

        self.assertEqual(len(restored.modifiers), 1)
        modifier = restored.modifiers[0]
        self.assertEqual(modifier.modifier_type, MODIFIER_DAMAGE_BOOST)
        self.assertAlmostEqual(modifier.x, 120.0, places=1)
        self.assertAlmostEqual(modifier.y, 220.0, places=1)

    def test_ping_ms_clamped_on_serialize(self):
        entities = {1: EntityState(1, 0.0, 0.0, 0.0, 0.0, 100.0, 999999)}
        snap = Snapshot(tick=5, entities=entities)
        restored = Snapshot.deserialize(snap.serialize())
        self.assertEqual(restored.entities[1].ping_ms, 65535)

    def test_snapshot_trailer_roundtrip(self):
        trailer_format = "!Idf"
        payload = struct.pack(trailer_format, 17, 123.456, 98.5)
        restored = struct.unpack(trailer_format, payload)
        self.assertEqual(restored[0], 17)
        self.assertAlmostEqual(restored[1], 123.456, places=3)
        self.assertAlmostEqual(restored[2], 98.5, places=3)


class TestGameState(unittest.TestCase):
    """Test authoritative game state logic."""

    def setUp(self):
        self.gs = GameState()

    def test_add_remove_entity(self):
        e = self.gs.add_entity(1)
        self.assertIn(1, self.gs.entities)
        self.gs.remove_entity(1)
        self.assertNotIn(1, self.gs.entities)

    def test_apply_input_moves_entity(self):
        self.gs.add_entity(1, x=100.0, y=100.0)
        self.gs.apply_input(1, move_x=1.0, move_y=0.0, actions=0, dt=0.05)
        e = self.gs.entities[1]
        self.assertGreater(e.x, 100.0)
        self.assertEqual(e.y, 100.0)

    def test_clamp_to_bounds(self):
        self.gs.add_entity(1, x=799.0, y=100.0)
        self.gs.apply_input(1, move_x=1.0, move_y=0.0, actions=0, dt=1.0)
        e = self.gs.entities[1]
        self.assertLessEqual(e.x, 800.0)

    def test_snapshot_creation(self):
        self.gs.add_entity(1, x=50.0, y=60.0)
        self.gs.add_entity(2, x=200.0, y=300.0)
        self.gs.tick = 42
        snap = self.gs.get_snapshot()
        self.assertEqual(snap.tick, 42)
        self.assertEqual(len(snap.entities), 2)

    def test_diagonal_normalization(self):
        """Moving diagonally should be normalized to prevent faster diagonal speed."""
        self.gs.add_entity(1, x=400.0, y=300.0)
        self.gs.add_entity(2, x=400.0, y=300.0)

        # Move entity 1 right only
        self.gs.apply_input(1, move_x=1.0, move_y=0.0, actions=0, dt=0.05)
        # Move entity 2 diagonally
        self.gs.apply_input(2, move_x=1.0, move_y=1.0, actions=0, dt=0.05)

        e1 = self.gs.entities[1]
        e2 = self.gs.entities[2]
        # Diagonal X movement should be less than straight X movement
        self.assertLess(e2.x - 400.0, e1.x - 400.0)


if __name__ == "__main__":
    unittest.main()
