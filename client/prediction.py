"""
Client-side prediction: apply local inputs immediately for responsive gameplay.
Uses the SAME physics as the server to minimize mispredictions.
"""

from common.config import PLAYER_SPEED, WORLD_WIDTH, WORLD_HEIGHT


class Predictor:
    """
    Applies player inputs to local state instantly.
    Must match server physics exactly.
    """

    def __init__(self, dt: float):
        self.dt = dt

    def predict(self, state: dict, inp: dict) -> dict:
        """
        Apply input to local state immediately.

        Args:
            state: dict with keys x, y, vx, vy, health
            inp: dict with keys move_x, move_y, actions

        Returns:
            New predicted state dict.
        """
        new_state = state.copy()

        move_x = inp.get('move_x', 0.0)
        move_y = inp.get('move_y', 0.0)

        # Normalize diagonal movement (same as server)
        mag = (move_x ** 2 + move_y ** 2) ** 0.5
        if mag > 1.0:
            move_x /= mag
            move_y /= mag

        new_state['vx'] = move_x * PLAYER_SPEED
        new_state['vy'] = move_y * PLAYER_SPEED
        new_state['x'] = new_state['x'] + new_state['vx'] * self.dt
        new_state['y'] = new_state['y'] + new_state['vy'] * self.dt

        # Clamp to world bounds (same as server!)
        new_state['x'] = max(0.0, min(float(WORLD_WIDTH), new_state['x']))
        new_state['y'] = max(0.0, min(float(WORLD_HEIGHT), new_state['y']))

        return new_state
