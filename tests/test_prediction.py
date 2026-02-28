"""
Unit tests for client-side prediction and reconciliation.
"""

import unittest

from client.prediction import Predictor
from client.reconciliation import Reconciler


class TestPredictor(unittest.TestCase):
    """Test client-side prediction logic."""

    def setUp(self):
        self.predictor = Predictor(dt=0.05)  # 20 Hz
        self.base_state = {
            'x': 100.0, 'y': 100.0,
            'vx': 0.0, 'vy': 0.0,
            'health': 100.0
        }

    def test_move_right(self):
        inp = {'move_x': 1.0, 'move_y': 0.0, 'actions': 0}
        new_state = self.predictor.predict(self.base_state, inp)
        self.assertGreater(new_state['x'], 100.0)
        self.assertEqual(new_state['y'], 100.0)

    def test_move_left(self):
        inp = {'move_x': -1.0, 'move_y': 0.0, 'actions': 0}
        new_state = self.predictor.predict(self.base_state, inp)
        self.assertLess(new_state['x'], 100.0)

    def test_move_up(self):
        inp = {'move_x': 0.0, 'move_y': -1.0, 'actions': 0}
        new_state = self.predictor.predict(self.base_state, inp)
        self.assertLess(new_state['y'], 100.0)

    def test_move_diagonal(self):
        inp = {'move_x': 1.0, 'move_y': 1.0, 'actions': 0}
        new_state = self.predictor.predict(self.base_state, inp)
        self.assertGreater(new_state['x'], 100.0)
        self.assertGreater(new_state['y'], 100.0)
        # Diagonal should be normalized, so movement per axis is less than straight
        inp_straight = {'move_x': 1.0, 'move_y': 0.0, 'actions': 0}
        straight = self.predictor.predict(self.base_state, inp_straight)
        self.assertLess(new_state['x'] - 100.0, straight['x'] - 100.0)

    def test_no_movement(self):
        inp = {'move_x': 0.0, 'move_y': 0.0, 'actions': 0}
        new_state = self.predictor.predict(self.base_state, inp)
        self.assertEqual(new_state['x'], 100.0)
        self.assertEqual(new_state['y'], 100.0)

    def test_clamp_right_bound(self):
        state = {'x': 799.0, 'y': 100.0, 'vx': 0.0, 'vy': 0.0, 'health': 100.0}
        inp = {'move_x': 1.0, 'move_y': 0.0, 'actions': 0}
        new_state = self.predictor.predict(state, inp)
        self.assertLessEqual(new_state['x'], 800.0)

    def test_clamp_left_bound(self):
        state = {'x': 1.0, 'y': 100.0, 'vx': 0.0, 'vy': 0.0, 'health': 100.0}
        inp = {'move_x': -1.0, 'move_y': 0.0, 'actions': 0}
        new_state = self.predictor.predict(state, inp)
        self.assertGreaterEqual(new_state['x'], 0.0)

    def test_clamp_bottom_bound(self):
        state = {'x': 100.0, 'y': 599.0, 'vx': 0.0, 'vy': 0.0, 'health': 100.0}
        inp = {'move_x': 0.0, 'move_y': 1.0, 'actions': 0}
        new_state = self.predictor.predict(state, inp)
        self.assertLessEqual(new_state['y'], 600.0)

    def test_health_preserved(self):
        inp = {'move_x': 1.0, 'move_y': 0.0, 'actions': 0}
        new_state = self.predictor.predict(self.base_state, inp)
        self.assertEqual(new_state['health'], 100.0)

    def test_state_immutability(self):
        """Original state should not be modified."""
        inp = {'move_x': 1.0, 'move_y': 1.0, 'actions': 0}
        _ = self.predictor.predict(self.base_state, inp)
        self.assertEqual(self.base_state['x'], 100.0)
        self.assertEqual(self.base_state['y'], 100.0)


class TestReconciler(unittest.TestCase):
    """Test server reconciliation logic."""

    def setUp(self):
        self.predictor = Predictor(dt=0.05)
        self.reconciler = Reconciler(self.predictor)

    def test_no_pending_inputs(self):
        server_state = {'x': 150.0, 'y': 200.0, 'vx': 0.0, 'vy': 0.0,
                        'health': 100.0}
        state, remaining, error = self.reconciler.reconcile(
            server_state, last_acked_input_seq=10, pending_inputs=[]
        )
        self.assertEqual(state['x'], 150.0)
        self.assertEqual(state['y'], 200.0)
        self.assertEqual(len(remaining), 0)

    def test_discard_processed_inputs(self):
        pending = [
            {'sequence': 5, 'input': {'move_x': 1.0, 'move_y': 0.0, 'actions': 0},
             'predicted_state': {'x': 110.0, 'y': 100.0}},
            {'sequence': 6, 'input': {'move_x': 1.0, 'move_y': 0.0, 'actions': 0},
             'predicted_state': {'x': 120.0, 'y': 100.0}},
            {'sequence': 7, 'input': {'move_x': 1.0, 'move_y': 0.0, 'actions': 0},
             'predicted_state': {'x': 130.0, 'y': 100.0}},
        ]
        server_state = {'x': 120.0, 'y': 100.0, 'vx': 200.0, 'vy': 0.0,
                        'health': 100.0}

        state, remaining, error = self.reconciler.reconcile(
            server_state, last_acked_input_seq=6, pending_inputs=pending
        )
        # Only input 7 should remain
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]['sequence'], 7)
        # State should be server state + replayed input 7
        self.assertGreater(state['x'], 120.0)

    def test_all_inputs_processed(self):
        pending = [
            {'sequence': 3, 'input': {'move_x': 1.0, 'move_y': 0.0, 'actions': 0},
             'predicted_state': {'x': 110.0, 'y': 100.0}},
        ]
        server_state = {'x': 115.0, 'y': 100.0, 'vx': 0.0, 'vy': 0.0,
                        'health': 100.0}

        state, remaining, error = self.reconciler.reconcile(
            server_state, last_acked_input_seq=5, pending_inputs=pending
        )
        # All inputs processed, state = server state
        self.assertEqual(len(remaining), 0)
        self.assertEqual(state['x'], 115.0)


if __name__ == '__main__':
    unittest.main()
