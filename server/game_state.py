"""
Authoritative game state owned by the server.
All game logic runs here — clients only send inputs.
"""

import random

from common.config import (
    CONTACT_DAMAGE_PER_SEC,
    DAMAGE_BOOST_DURATION_TICKS,
    DAMAGE_BOOST_MULTIPLIER,
    DASH_COOLDOWN,
    DASH_COOLDOWN_BUFF_DURATION_TICKS,
    DASH_COOLDOWN_REDUCTION_FACTOR,
    DASH_CONTACT_DAMAGE_MULTIPLIER,
    DASH_DURATION,
    DASH_SPEED_MULTIPLIER,
    EFFECT_FLAG_DAMAGE_BOOST,
    EFFECT_FLAG_DASH_COOLDOWN,
    EFFECT_FLAG_INVINCIBILITY,
    INVINCIBILITY_DURATION_TICKS,
    KILLS_TO_WIN,
    MAX_MODIFIERS,
    MODIFIER_DAMAGE_BOOST,
    MODIFIER_DASH_COOLDOWN,
    MODIFIER_INVINCIBILITY,
    MODIFIER_PICKUP_RADIUS,
    MODIFIER_RADIUS,
    MODIFIER_SPAWN_INTERVAL_TICKS,
    PLAYER_RADIUS,
    PLAYER_SPEED,
    RESPAWN_DELAY_TICKS,
    RESPAWN_HEALTH,
    WORLD_HEIGHT,
    WORLD_WIDTH,
)
from common.snapshot import EntityState, ModifierState, Snapshot


class GameState:
    """
    The single source of truth for the game world.
    Maintains all entity states and applies physics.
    """

    def __init__(self):
        self.entities = {}
        self.tick = 0
        self.game_started = False
        self.respawn_timers: dict[int, int] = {}
        self.scores: dict[int, int] = {}
        self.last_damager: dict[int, int] = {}
        self.modifiers: dict[int, ModifierState] = {}
        self.next_modifier_id = 1
        self.invincibility_until: dict[int, int] = {}
        self.damage_boost_until: dict[int, int] = {}
        self.dash_cooldown_until: dict[int, int] = {}

    def reset(self):
        """Reset match state between sessions."""
        self.entities.clear()
        self.respawn_timers.clear()
        self.scores.clear()
        self.last_damager.clear()
        self.modifiers.clear()
        self.next_modifier_id = 1
        self.invincibility_until.clear()
        self.damage_boost_until.clear()
        self.dash_cooldown_until.clear()
        self.tick = 0
        self.game_started = False

    def _spawn_position(self, entity_id: int) -> tuple[float, float]:
        spawn_range = max(1, WORLD_WIDTH - 200)
        # Distribute spawn positions evenly without collision wrapping
        x_pos = 100 + (entity_id % (spawn_range // 150)) * 150
        return float(x_pos), WORLD_HEIGHT / 2.0

    def add_entity(
        self, entity_id: int, x: float | None = None, y: float | None = None
    ) -> EntityState:
        """Add a new entity (player) to the world."""
        if entity_id in self.entities:
            return self.entities[entity_id]

        if x is None or y is None:
            spawn_x, spawn_y = self._spawn_position(entity_id)
            if x is None:
                x = spawn_x
            if y is None:
                y = spawn_y

        entity = EntityState(entity_id, x, y, 0.0, 0.0, RESPAWN_HEALTH)
        self.entities[entity_id] = entity
        self.scores.setdefault(entity_id, 0)
        self.respawn_timers.pop(entity_id, None)
        self.last_damager.pop(entity_id, None)
        self._clear_entity_effects(entity_id)
        return entity

    def remove_entity(self, entity_id: int):
        """Remove an entity from the world."""
        self.entities.pop(entity_id, None)
        self.respawn_timers.pop(entity_id, None)
        self.last_damager.pop(entity_id, None)
        self.scores.pop(entity_id, None)
        self._clear_entity_effects(entity_id)

    def _clear_entity_effects(self, entity_id: int):
        self.invincibility_until.pop(entity_id, None)
        self.damage_boost_until.pop(entity_id, None)
        self.dash_cooldown_until.pop(entity_id, None)

    def _is_invincible(self, entity_id: int) -> bool:
        return self.invincibility_until.get(entity_id, 0) > self.tick

    def _damage_multiplier(self, entity_id: int) -> float:
        return (
            DAMAGE_BOOST_MULTIPLIER
            if self.damage_boost_until.get(entity_id, 0) > self.tick
            else 1.0
        )

    def _dash_cooldown_factor(self, entity_id: int) -> float:
        return (
            DASH_COOLDOWN_REDUCTION_FACTOR
            if self.dash_cooldown_until.get(entity_id, 0) > self.tick
            else 1.0
        )

    def _effect_flags(self, entity_id: int) -> int:
        flags = 0
        if self._is_invincible(entity_id):
            flags |= EFFECT_FLAG_INVINCIBILITY
        if self.damage_boost_until.get(entity_id, 0) > self.tick:
            flags |= EFFECT_FLAG_DAMAGE_BOOST
        if self.dash_cooldown_until.get(entity_id, 0) > self.tick:
            flags |= EFFECT_FLAG_DASH_COOLDOWN
        return flags

    def _random_modifier_position(self) -> tuple[float, float]:
        margin = 80.0
        # Scale retries based on how many entities are on the map
        max_retries = min(100, 20 + len(self.entities) // 5)
        for _ in range(max_retries):
            x_pos = random.uniform(margin, WORLD_WIDTH - margin)
            y_pos = random.uniform(margin, WORLD_HEIGHT - margin)
            if all(
                (entity.x - x_pos) ** 2 + (entity.y - y_pos) ** 2
                > float((PLAYER_RADIUS + MODIFIER_PICKUP_RADIUS + 10) ** 2)
                for entity in self.entities.values()
            ) and all(
                (modifier.x - x_pos) ** 2 + (modifier.y - y_pos) ** 2
                > float((MODIFIER_RADIUS * 3) ** 2)
                for modifier in self.modifiers.values()
            ):
                return x_pos, y_pos
        return WORLD_WIDTH / 2.0, WORLD_HEIGHT / 2.0

    def spawn_modifiers(self):
        if not self.game_started or len(self.modifiers) >= MAX_MODIFIERS:
            return
        if self.tick == 0 or self.tick % MODIFIER_SPAWN_INTERVAL_TICKS != 0:
            return

        modifier_type = random.choice(
            [MODIFIER_INVINCIBILITY, MODIFIER_DAMAGE_BOOST, MODIFIER_DASH_COOLDOWN]
        )
        x_pos, y_pos = self._random_modifier_position()
        self.modifiers[self.next_modifier_id] = ModifierState(
            modifier_type, x_pos, y_pos
        )
        self.next_modifier_id += 1

    def _apply_modifier(self, entity_id: int, modifier_type: int):
        if modifier_type == MODIFIER_INVINCIBILITY:
            self.invincibility_until[entity_id] = (
                self.tick + INVINCIBILITY_DURATION_TICKS
            )
        elif modifier_type == MODIFIER_DAMAGE_BOOST:
            self.damage_boost_until[entity_id] = self.tick + DAMAGE_BOOST_DURATION_TICKS
        elif modifier_type == MODIFIER_DASH_COOLDOWN:
            self.dash_cooldown_until[entity_id] = (
                self.tick + DASH_COOLDOWN_BUFF_DURATION_TICKS
            )

    def collect_modifiers(self):
        if not self.modifiers:
            return

        pickup_dist_sq = float(MODIFIER_PICKUP_RADIUS**2)
        for modifier_id, modifier in list(self.modifiers.items()):
            for entity in self.entities.values():
                if entity.health <= 0:
                    continue
                dx = entity.x - modifier.x
                dy = entity.y - modifier.y
                if dx * dx + dy * dy > pickup_dist_sq:
                    continue
                self._apply_modifier(entity.entity_id, modifier.modifier_type)
                del self.modifiers[modifier_id]
                break

    def _collision_winner(
        self,
        entity_a: EntityState,
        entity_b: EntityState,
        damage_to_b: float,
        damage_to_a: float,
        pre_health_a: float,
        pre_health_b: float,
    ) -> tuple[EntityState, EntityState]:
        priority_a = (
            damage_to_b,
            pre_health_a,
            self.scores.get(entity_a.entity_id, 0),
            -entity_a.entity_id,
        )
        priority_b = (
            damage_to_a,
            pre_health_b,
            self.scores.get(entity_b.entity_id, 0),
            -entity_b.entity_id,
        )
        if priority_a >= priority_b:
            return entity_a, entity_b
        return entity_b, entity_a

    def apply_input(
        self, entity_id: int, move_x: float, move_y: float, actions: int, dt: float
    ):
        """Apply a player's input to the simulation."""
        entity = self.entities.get(entity_id)
        if entity is None:
            return

        entity.dash_cooldown = max(0.0, entity.dash_cooldown - dt)
        entity.dash_timer = max(0.0, entity.dash_timer - dt)

        if entity.health <= 0:
            entity.vx = 0.0
            entity.vy = 0.0
            return

        mag = (move_x**2 + move_y**2) ** 0.5
        if mag > 1.0:
            move_x /= mag
            move_y /= mag

        if (actions & 0x01) and entity.dash_cooldown <= 0.0 and mag > 0.01:
            entity.dash_cooldown = DASH_COOLDOWN * self._dash_cooldown_factor(entity_id)
            entity.dash_timer = DASH_DURATION

        speed_multiplier = (
            DASH_SPEED_MULTIPLIER if entity.dash_timer > 0.0 and mag > 0.01 else 1.0
        )

        entity.vx = move_x * PLAYER_SPEED * speed_multiplier
        entity.vy = move_y * PLAYER_SPEED * speed_multiplier
        entity.x += entity.vx * dt
        entity.y += entity.vy * dt

        entity.x = max(0.0, min(float(WORLD_WIDTH), entity.x))
        entity.y = max(0.0, min(float(WORLD_HEIGHT), entity.y))

    def resolve_collisions(self, dt: float) -> list[tuple[int, int]]:
        """Apply contact damage between overlapping players and return score events."""
        score_events: list[tuple[int, int]] = []
        dead_this_tick: set[int] = set()
        entity_list = list(self.entities.values())
        collision_dist_sq = float((PLAYER_RADIUS * 2) ** 2)

        for index, entity_a in enumerate(entity_list):
            if entity_a.health <= 0:
                continue

            for entity_b in entity_list[index + 1 :]:
                if entity_a.health <= 0:
                    break
                if entity_b.health <= 0:
                    continue

                dx = entity_a.x - entity_b.x
                dy = entity_a.y - entity_b.y
                dist_sq = dx * dx + dy * dy
                if dist_sq >= collision_dist_sq:
                    continue

                dist = dist_sq**0.5
                overlap = PLAYER_RADIUS * 2 - dist
                MIN_DIST = 1e-6
                if dist > MIN_DIST:
                    nx = dx / dist
                    ny = dy / dist
                else:
                    # When too close, use raw direction to avoid huge vectors
                    nx = 1.0 if abs(dx) >= abs(dy) else 0.0
                    ny = 1.0 if abs(dy) > abs(dx) else 0.0

                half = overlap * 0.5 + 0.5
                entity_a.x = max(0.0, min(float(WORLD_WIDTH), entity_a.x + nx * half))
                entity_a.y = max(0.0, min(float(WORLD_HEIGHT), entity_a.y + ny * half))
                entity_b.x = max(0.0, min(float(WORLD_WIDTH), entity_b.x - nx * half))
                entity_b.y = max(0.0, min(float(WORLD_HEIGHT), entity_b.y - ny * half))

                pre_health_a = entity_a.health
                pre_health_b = entity_b.health
                damage_to_a = 0.0
                damage_to_b = 0.0
                if not self._is_invincible(entity_a.entity_id):
                    damage_to_a = (
                        CONTACT_DAMAGE_PER_SEC
                        * dt
                        * self._damage_multiplier(entity_b.entity_id)
                        * (
                            DASH_CONTACT_DAMAGE_MULTIPLIER
                            if entity_b.dash_timer > 0.0
                            else 1.0
                        )
                    )
                if not self._is_invincible(entity_b.entity_id):
                    damage_to_b = (
                        CONTACT_DAMAGE_PER_SEC
                        * dt
                        * self._damage_multiplier(entity_a.entity_id)
                        * (
                            DASH_CONTACT_DAMAGE_MULTIPLIER
                            if entity_a.dash_timer > 0.0
                            else 1.0
                        )
                    )

                entity_a.health = max(0.0, entity_a.health - damage_to_a)
                entity_b.health = max(0.0, entity_b.health - damage_to_b)
                if damage_to_a > 0.0:
                    self.last_damager[entity_a.entity_id] = entity_b.entity_id
                if damage_to_b > 0.0:
                    self.last_damager[entity_b.entity_id] = entity_a.entity_id

                if entity_a.health <= 0 and entity_b.health <= 0:
                    winner, loser = self._collision_winner(
                        entity_a,
                        entity_b,
                        damage_to_b,
                        damage_to_a,
                        pre_health_a,
                        pre_health_b,
                    )
                    winner.health = max(1.0, winner.health)
                    loser.health = 0.0
                    self.last_damager[loser.entity_id] = winner.entity_id
                    self.last_damager.pop(winner.entity_id, None)

                if entity_a.health <= 0 and entity_a.entity_id not in dead_this_tick:
                    dead_this_tick.add(entity_a.entity_id)
                    attacker_id = self.last_damager.get(entity_a.entity_id)
                    if attacker_id is not None and attacker_id != entity_a.entity_id:
                        self.scores[attacker_id] = self.scores.get(attacker_id, 0) + 1
                        score_events.append((attacker_id, entity_a.entity_id))

                if entity_b.health <= 0 and entity_b.entity_id not in dead_this_tick:
                    dead_this_tick.add(entity_b.entity_id)
                    attacker_id = self.last_damager.get(entity_b.entity_id)
                    if attacker_id is not None and attacker_id != entity_b.entity_id:
                        self.scores[attacker_id] = self.scores.get(attacker_id, 0) + 1
                        score_events.append((attacker_id, entity_b.entity_id))

        return score_events

    def queue_respawns(self):
        for entity_id, entity in self.entities.items():
            if entity.health <= 0 and entity_id not in self.respawn_timers:
                self.respawn_timers[entity_id] = RESPAWN_DELAY_TICKS
                entity.vx = 0.0
                entity.vy = 0.0
                self._clear_entity_effects(entity_id)

    def tick_respawns(self):
        for entity_id in list(self.respawn_timers.keys()):
            self.respawn_timers[entity_id] -= 1
            if self.respawn_timers[entity_id] > 0:
                continue

            del self.respawn_timers[entity_id]
            entity = self.entities.get(entity_id)
            if entity is None:
                continue

            entity.health = RESPAWN_HEALTH
            entity.vx = 0.0
            entity.vy = 0.0
            entity.dash_cooldown = 0.0
            entity.dash_timer = 0.0
            entity.x, entity.y = self._spawn_position(entity_id)
            self.last_damager.pop(entity_id, None)
            self._clear_entity_effects(entity_id)

    def check_win_condition(self) -> int | None:
        for entity_id, kills in self.scores.items():
            if kills >= KILLS_TO_WIN:
                return entity_id
        return None

    def get_snapshot(self) -> Snapshot:
        """Create a snapshot of the current world state."""
        entities_copy = {}
        for entity_id, entity in self.entities.items():
            entity_copy = entity.copy()
            entity_copy.respawn_ticks_remaining = self.respawn_timers.get(entity_id, 0)
            entity_copy.effect_flags = self._effect_flags(entity_id)
            entities_copy[entity_id] = entity_copy
        modifiers_copy = {
            modifier_id: modifier.copy()
            for modifier_id, modifier in self.modifiers.items()
        }
        return Snapshot(self.tick, entities_copy, modifiers_copy)
