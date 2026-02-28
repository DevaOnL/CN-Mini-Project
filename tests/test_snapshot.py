"""
Unit tests for snapshot serialization and game state.
"""

import unittest

from common.snapshot import Snapshot, EntityState
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
        entities = {
            1: EntityState(1, 100.0, 200.0, 10.0, -5.0, 75.0)
        }
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

    def test_roundtrip_multiple_entities(self):
        entities = {}
        for i in range(8):
            entities[i + 1] = EntityState(
                i + 1, float(i * 100), float(i * 50),
                float(i), float(-i), 100.0 - i * 10
            )
        snap = Snapshot(tick=500, entities=entities)
        data = snap.serialize()
        restored = Snapshot.deserialize(data)

        self.assertEqual(restored.tick, 500)
        self.assertEqual(len(restored.entities), 8)

    def test_to_dict(self):
        entities = {1: EntityState(1, 10.0, 20.0, 1.0, 2.0, 100.0)}
        snap = Snapshot(tick=1, entities=entities)
        d = snap.to_dict()
        self.assertEqual(d['tick'], 1)
        self.assertIn(1, d['entities'])
        self.assertAlmostEqual(d['entities'][1]['x'], 10.0)


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


if __name__ == '__main__':
    unittest.main()
