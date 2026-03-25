"""
Snapshot serialization/deserialization for game state.
"""

import struct

from common.packet import (
    ENTITY_STATE_FORMAT,
    ENTITY_STATE_SIZE,
    MODIFIER_STATE_FORMAT,
    MODIFIER_STATE_SIZE,
    SNAPSHOT_HEADER_FORMAT,
    SNAPSHOT_HEADER_SIZE,
)


class EntityState:
    """Represents one entity's state in a snapshot."""

    __slots__ = (
        "entity_id",
        "x",
        "y",
        "vx",
        "vy",
        "health",
        "ping_ms",
        "respawn_ticks_remaining",
        "effect_flags",
        "dash_cooldown",
        "dash_timer",
    )

    def __init__(
        self,
        entity_id: int = 0,
        x: float = 0.0,
        y: float = 0.0,
        vx: float = 0.0,
        vy: float = 0.0,
        health: float = 100.0,
        ping_ms: int = 0,
        respawn_ticks_remaining: int = 0,
        effect_flags: int = 0,
        dash_cooldown: float = 0.0,
        dash_timer: float = 0.0,
    ):
        self.entity_id = entity_id
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.health = health
        self.ping_ms = ping_ms
        self.respawn_ticks_remaining = respawn_ticks_remaining
        self.effect_flags = effect_flags
        # Dash state is server/local-prediction only and intentionally omitted from snapshots.
        self.dash_cooldown = dash_cooldown
        self.dash_timer = dash_timer

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "x": self.x,
            "y": self.y,
            "vx": self.vx,
            "vy": self.vy,
            "health": self.health,
            "ping_ms": self.ping_ms,
            "respawn_ticks_remaining": self.respawn_ticks_remaining,
            "effect_flags": self.effect_flags,
            "dash_cooldown": self.dash_cooldown,
            "dash_timer": self.dash_timer,
        }

    def copy(self) -> "EntityState":
        return EntityState(
            self.entity_id,
            self.x,
            self.y,
            self.vx,
            self.vy,
            self.health,
            self.ping_ms,
            self.respawn_ticks_remaining,
            self.effect_flags,
            self.dash_cooldown,
            self.dash_timer,
        )


class ModifierState:
    __slots__ = ("modifier_type", "x", "y")

    def __init__(self, modifier_type: int = 0, x: float = 0.0, y: float = 0.0):
        self.modifier_type = modifier_type
        self.x = x
        self.y = y

    def to_dict(self) -> dict:
        return {
            "modifier_type": self.modifier_type,
            "x": self.x,
            "y": self.y,
        }

    def copy(self) -> "ModifierState":
        return ModifierState(self.modifier_type, self.x, self.y)


class Snapshot:
    """A full game state snapshot at a specific tick."""

    def __init__(
        self,
        tick: int = 0,
        entities: dict | None = None,
        modifiers: dict | None = None,
    ):
        self.tick = tick
        self.entities = entities or {}
        self.modifiers = modifiers or {}

    def serialize(self) -> bytes:
        """Serialize snapshot to binary payload."""
        entity_list = list(self.entities.values())
        modifier_list = list(self.modifiers.values())
        buf = struct.pack(
            SNAPSHOT_HEADER_FORMAT,
            self.tick,
            len(entity_list),
            len(modifier_list),
        )
        for entity in entity_list:
            buf += struct.pack(
                ENTITY_STATE_FORMAT,
                entity.entity_id,
                entity.x,
                entity.y,
                entity.vx,
                entity.vy,
                entity.health,
                int(max(0, min(65535, entity.ping_ms))),
                int(max(0, min(65535, entity.respawn_ticks_remaining))),
                int(max(0, min(255, entity.effect_flags))),
                entity.dash_cooldown,
                entity.dash_timer,
            )
        for modifier in modifier_list:
            buf += struct.pack(
                MODIFIER_STATE_FORMAT,
                modifier.modifier_type,
                modifier.x,
                modifier.y,
            )
        return buf

    def serialized_size(self) -> int:
        """Return the byte-size of the serialized snapshot (without trailer)."""
        return (
            SNAPSHOT_HEADER_SIZE
            + len(self.entities) * ENTITY_STATE_SIZE
            + len(self.modifiers) * MODIFIER_STATE_SIZE
        )

    @staticmethod
    def deserialize(data: bytes) -> "Snapshot":
        """Deserialize binary payload to Snapshot."""
        if len(data) < SNAPSHOT_HEADER_SIZE:
            raise ValueError("Snapshot data too short")

        tick, count, modifier_count = struct.unpack(
            SNAPSHOT_HEADER_FORMAT, data[:SNAPSHOT_HEADER_SIZE]
        )
        offset = SNAPSHOT_HEADER_SIZE
        entities = {}
        modifiers = {}

        for _ in range(count):
            if offset + ENTITY_STATE_SIZE > len(data):
                raise ValueError(
                    f"Snapshot truncated: expected {count} entities, got {len(entities)}"
                )
            (
                entity_id,
                x,
                y,
                vx,
                vy,
                health,
                ping_ms,
                respawn_ticks_remaining,
                effect_flags,
                dash_cooldown,
                dash_timer,
            ) = struct.unpack(
                ENTITY_STATE_FORMAT,
                data[offset : offset + ENTITY_STATE_SIZE],
            )
            entities[entity_id] = EntityState(
                entity_id,
                x,
                y,
                vx,
                vy,
                health,
                ping_ms,
                respawn_ticks_remaining,
                effect_flags,
                dash_cooldown,
                dash_timer,
            )
            offset += ENTITY_STATE_SIZE

        for modifier_id in range(modifier_count):
            if offset + MODIFIER_STATE_SIZE > len(data):
                raise ValueError(
                    f"Snapshot truncated: expected {modifier_count} modifiers, got {len(modifiers)}"
                )
            modifier_type, x, y = struct.unpack(
                MODIFIER_STATE_FORMAT,
                data[offset : offset + MODIFIER_STATE_SIZE],
            )
            modifiers[modifier_id] = ModifierState(modifier_type, x, y)
            offset += MODIFIER_STATE_SIZE

        return Snapshot(tick, entities, modifiers)

    def to_dict(self) -> dict:
        return {
            "tick": self.tick,
            "entities": {
                entity_id: entity.to_dict()
                for entity_id, entity in self.entities.items()
            },
            "modifiers": {
                modifier_id: modifier.to_dict()
                for modifier_id, modifier in self.modifiers.items()
            },
        }
