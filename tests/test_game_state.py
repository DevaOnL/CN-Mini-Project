"""Focused tests for authoritative gameplay state."""

from common.config import (
    MODIFIER_DAMAGE_BOOST,
    PLAYER_RADIUS,
    RESPAWN_DELAY_TICKS,
    RESPAWN_HEALTH,
)
from common.snapshot import ModifierState
from server.game_state import GameState


def test_collision_separation_prevents_stacking():
    gs = GameState()
    gs.add_entity(1, x=100.0, y=100.0)
    gs.add_entity(2, x=100.0, y=100.0)
    gs.resolve_collisions(dt=0.05)
    entity1, entity2 = gs.entities[1], gs.entities[2]
    dist = ((entity1.x - entity2.x) ** 2 + (entity1.y - entity2.y) ** 2) ** 0.5
    assert dist >= PLAYER_RADIUS * 2 - 0.01


def test_collision_damage_on_overlap():
    gs = GameState()
    gs.add_entity(1, x=100.0, y=100.0)
    gs.add_entity(2, x=108.0, y=100.0)
    gs.resolve_collisions(dt=0.05)
    assert gs.entities[1].health < 100.0
    assert gs.entities[2].health < 100.0


def test_no_damage_when_far_apart():
    gs = GameState()
    gs.add_entity(1, x=100.0, y=100.0)
    gs.add_entity(2, x=300.0, y=300.0)
    gs.resolve_collisions(dt=0.05)
    assert gs.entities[1].health == 100.0
    assert gs.entities[2].health == 100.0


def test_dead_entity_not_damaged():
    gs = GameState()
    gs.add_entity(1, x=100.0, y=100.0)
    gs.add_entity(2, x=100.0, y=100.0)
    gs.entities[1].health = 0.0
    gs.resolve_collisions(dt=0.05)
    assert gs.entities[2].health == 100.0


def test_respawn_restores_health():
    gs = GameState()
    gs.add_entity(1, x=100.0, y=100.0)
    gs.entities[1].health = 0.0
    gs.queue_respawns()
    for _ in range(RESPAWN_DELAY_TICKS + 1):
        gs.tick_respawns()
    assert gs.entities[1].health == RESPAWN_HEALTH


def test_scores_increment_on_kill():
    gs = GameState()
    gs.add_entity(1, x=100.0, y=100.0)
    gs.add_entity(2, x=100.0, y=100.0)
    gs.entities[2].health = 0.01
    gs.last_damager[2] = 1
    events = gs.resolve_collisions(dt=0.05)
    assert any(killer == 1 for killer, _ in events)


def test_game_state_reset():
    gs = GameState()
    gs.add_entity(1)
    gs.scores[1] = 5
    gs.game_started = True
    gs.reset()
    assert not gs.entities
    assert not gs.scores
    assert gs.game_started is False
    assert gs.tick == 0


def test_dash_collision_breaks_mutual_kill_tie():
    gs = GameState()
    gs.add_entity(1, x=100.0, y=100.0)
    gs.add_entity(2, x=100.0, y=100.0)
    gs.entities[1].health = 0.5
    gs.entities[2].health = 0.5
    gs.entities[1].dash_timer = 0.1

    events = gs.resolve_collisions(dt=0.05)

    assert events == [(1, 2)]
    assert gs.entities[1].health > 0.0
    assert gs.entities[2].health == 0.0


def test_collect_modifier_applies_effect_and_removes_pickup():
    gs = GameState()
    gs.game_started = True
    gs.add_entity(1, x=100.0, y=100.0)
    gs.modifiers[1] = ModifierState(MODIFIER_DAMAGE_BOOST, 100.0, 100.0)

    gs.collect_modifiers()

    assert not gs.modifiers
    assert gs.damage_boost_until[1] > gs.tick


def test_snapshot_includes_respawn_ticks_and_modifiers():
    gs = GameState()
    gs.add_entity(1, x=100.0, y=100.0)
    gs.entities[1].health = 0.0
    gs.queue_respawns()
    gs.modifiers[1] = ModifierState(MODIFIER_DAMAGE_BOOST, 200.0, 220.0)

    snapshot = gs.get_snapshot()

    assert snapshot.entities[1].respawn_ticks_remaining == RESPAWN_DELAY_TICKS
    assert snapshot.modifiers[1].modifier_type == MODIFIER_DAMAGE_BOOST
