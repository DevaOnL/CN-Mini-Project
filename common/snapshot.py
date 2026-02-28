"""
Snapshot serialization/deserialization for game state.
"""

import struct
from common.packet import (
    ENTITY_STATE_FORMAT, ENTITY_STATE_SIZE,
    SNAPSHOT_HEADER_FORMAT, SNAPSHOT_HEADER_SIZE
)


class EntityState:
    """Represents one entity's state in a snapshot."""

    __slots__ = ('entity_id', 'x', 'y', 'vx', 'vy', 'health')

    def __init__(self, entity_id: int = 0, x: float = 0.0, y: float = 0.0,
                 vx: float = 0.0, vy: float = 0.0, health: float = 100.0):
        self.entity_id = entity_id
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.health = health

    def to_dict(self) -> dict:
        return {
            'entity_id': self.entity_id,
            'x': self.x, 'y': self.y,
            'vx': self.vx, 'vy': self.vy,
            'health': self.health
        }

    def copy(self) -> 'EntityState':
        return EntityState(self.entity_id, self.x, self.y,
                           self.vx, self.vy, self.health)


class Snapshot:
    """A full game state snapshot at a specific tick."""

    def __init__(self, tick: int = 0, entities: dict = None):
        self.tick = tick
        self.entities = entities or {}  # entity_id -> EntityState

    def serialize(self) -> bytes:
        """Serialize snapshot to binary payload."""
        entity_list = list(self.entities.values())
        buf = struct.pack(SNAPSHOT_HEADER_FORMAT, self.tick, len(entity_list))
        for e in entity_list:
            buf += struct.pack(ENTITY_STATE_FORMAT,
                               e.entity_id, e.x, e.y, e.vx, e.vy, e.health)
        return buf

    def serialized_size(self) -> int:
        """Return the byte-size of the serialized snapshot (without trailer)."""
        return SNAPSHOT_HEADER_SIZE + len(self.entities) * ENTITY_STATE_SIZE

    @staticmethod
    def deserialize(data: bytes) -> 'Snapshot':
        """Deserialize binary payload to Snapshot."""
        if len(data) < SNAPSHOT_HEADER_SIZE:
            raise ValueError("Snapshot data too short")

        tick, count = struct.unpack(SNAPSHOT_HEADER_FORMAT,
                                    data[:SNAPSHOT_HEADER_SIZE])
        offset = SNAPSHOT_HEADER_SIZE
        entities = {}

        for _ in range(count):
            if offset + ENTITY_STATE_SIZE > len(data):
                raise ValueError(
                    f"Snapshot truncated: expected {count} entities, "
                    f"got {len(entities)}"
                )
            eid, x, y, vx, vy, health = struct.unpack(
                ENTITY_STATE_FORMAT, data[offset:offset + ENTITY_STATE_SIZE]
            )
            entities[eid] = EntityState(eid, x, y, vx, vy, health)
            offset += ENTITY_STATE_SIZE

        return Snapshot(tick, entities)

    def to_dict(self) -> dict:
        return {
            'tick': self.tick,
            'entities': {eid: e.to_dict() for eid, e in self.entities.items()}
        }
