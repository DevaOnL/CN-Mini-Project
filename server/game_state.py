"""
Authoritative game state owned by the server.
All game logic runs here — clients only send inputs.
"""

from common.config import WORLD_WIDTH, WORLD_HEIGHT, PLAYER_SPEED
from common.snapshot import EntityState, Snapshot


class GameState:
    """
    The single source of truth for the game world.
    Maintains all entity states and applies physics.
    """

    def __init__(self):
        self.entities = {}   # entity_id -> EntityState
        self.tick = 0

    def add_entity(self, entity_id: int, x: float = None, y: float = None) -> EntityState:
        """Add a new entity (player) to the world."""
        if entity_id in self.entities:
            return self.entities[entity_id]  # Already exists
        if x is None:
            # Spread players across the map
            count = len(self.entities)
            x = 100 + (count * 150) % (WORLD_WIDTH - 200)
        if y is None:
            y = WORLD_HEIGHT / 2.0

        e = EntityState(entity_id, x, y, 0.0, 0.0, 100.0)
        self.entities[entity_id] = e
        return e

    def remove_entity(self, entity_id: int):
        """Remove an entity from the world."""
        self.entities.pop(entity_id, None)

    def apply_input(self, entity_id: int, move_x: float, move_y: float,
                    actions: int, dt: float):
        """Apply a player's input to the simulation."""
        e = self.entities.get(entity_id)
        if e is None:
            return

        # Normalize diagonal movement
        mag = (move_x ** 2 + move_y ** 2) ** 0.5
        if mag > 1.0:
            move_x /= mag
            move_y /= mag

        e.vx = move_x * PLAYER_SPEED
        e.vy = move_y * PLAYER_SPEED
        e.x += e.vx * dt
        e.y += e.vy * dt

        # Clamp to world bounds
        e.x = max(0.0, min(float(WORLD_WIDTH), e.x))
        e.y = max(0.0, min(float(WORLD_HEIGHT), e.y))

    def get_snapshot(self) -> Snapshot:
        """Create a snapshot of the current world state."""
        entities_copy = {}
        for eid, e in self.entities.items():
            entities_copy[eid] = e.copy()
        return Snapshot(self.tick, entities_copy)
