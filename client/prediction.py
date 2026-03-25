"""
Client-side prediction: apply local inputs immediately for responsive gameplay.
Uses the SAME physics as the server to minimize mispredictions.
"""

from common.config import (
    DASH_COOLDOWN,
    DASH_DURATION,
    DASH_SPEED_MULTIPLIER,
    PLAYER_SPEED,
    WORLD_HEIGHT,
    WORLD_WIDTH,
)


class Predictor:
    """
    Applies player inputs to local state instantly.
    Must match server physics exactly.
    """

    def __init__(self, dt: float):
        self.dt = dt

    def predict(self, state: dict, inp: dict) -> dict:
        """Apply input to local state immediately."""
        new_state = state.copy()

        new_state["dash_cooldown"] = max(
            0.0, new_state.get("dash_cooldown", 0.0) - self.dt
        )
        new_state["dash_timer"] = max(0.0, new_state.get("dash_timer", 0.0) - self.dt)

        if new_state.get("health", 100.0) <= 0.0:
            new_state["vx"] = 0.0
            new_state["vy"] = 0.0
            return new_state

        move_x = inp.get("move_x", 0.0)
        move_y = inp.get("move_y", 0.0)
        actions = inp.get("actions", 0)

        mag = (move_x**2 + move_y**2) ** 0.5
        if mag > 1.0:
            move_x /= mag
            move_y /= mag

        if (actions & 0x01) and new_state["dash_cooldown"] <= 0.0 and mag > 0.01:
            new_state["dash_cooldown"] = DASH_COOLDOWN * new_state.get(
                "dash_cooldown_factor", 1.0
            )
            new_state["dash_timer"] = DASH_DURATION

        speed_multiplier = (
            DASH_SPEED_MULTIPLIER
            if new_state.get("dash_timer", 0.0) > 0.0 and mag > 0.01
            else 1.0
        )

        new_state["vx"] = move_x * PLAYER_SPEED * speed_multiplier
        new_state["vy"] = move_y * PLAYER_SPEED * speed_multiplier
        new_state["x"] = new_state["x"] + new_state["vx"] * self.dt
        new_state["y"] = new_state["y"] + new_state["vy"] * self.dt

        new_state["x"] = max(0.0, min(float(WORLD_WIDTH), new_state["x"]))
        new_state["y"] = max(0.0, min(float(WORLD_HEIGHT), new_state["y"]))

        return new_state
