"""
Entity interpolation for rendering remote players smoothly.
Remote entities are rendered slightly in the past to hide packet jitter.
"""

import time


class Interpolator:
    """
    Interpolates remote entity positions between server snapshots.
    Uses wall-clock snapshot arrival times instead of a guessed server tick.
    """

    def __init__(self, tick_rate: int = 20, interp_ticks: int = 2):
        self.tick_rate = tick_rate
        self.interp_ticks = interp_ticks
        self.tick_duration = 1.0 / tick_rate

    def _snapshot_to_remote_states(self, snapshot, local_entity_id: int) -> dict:
        states = {}
        for entity_id, entity in snapshot.entities.items():
            if entity_id == local_entity_id:
                continue
            states[entity_id] = entity.to_dict()
        return states

    def interpolate(
        self,
        snapshots: list,
        snapshot_recv_times: dict[int, float],
        local_entity_id: int,
    ) -> dict:
        if not snapshots:
            return {}

        latest_tick = snapshots[-1].tick
        cutoff_tick = latest_tick - 10
        filtered_snapshots = [snapshot for snapshot in snapshots if snapshot.tick >= cutoff_tick]

        # Ensure we keep at least 2 snapshots for interpolation
        if filtered_snapshots:
            snapshots = filtered_snapshots
        elif len(snapshots) >= 2:
            snapshots = snapshots[-2:]  # Keep last 2 if none meet cutoff
        elif not snapshots:
            return {}

        if len(snapshots) < 2:
            return self._snapshot_to_remote_states(snapshots[-1], local_entity_id)

        interp_delay = self.interp_ticks * self.tick_duration
        render_time = time.perf_counter() - interp_delay

        before = None
        after = None
        t_before = 0.0
        t_after = 0.0

        for index in range(len(snapshots) - 1):
            s0 = snapshots[index]
            s1 = snapshots[index + 1]
            recv0 = snapshot_recv_times.get(s0.tick)
            recv1 = snapshot_recv_times.get(s1.tick)
            if recv0 is None or recv1 is None:
                # Log when interpolation cannot proceed due to missing timing data
                if recv0 is None:
                    print(f"[INTERP] Missing recv_time for snapshot tick {s0.tick}")
                if recv1 is None:
                    print(f"[INTERP] Missing recv_time for snapshot tick {s1.tick}")
                continue
            if recv0 <= render_time <= recv1:
                before = s0
                after = s1
                t_before = recv0
                t_after = recv1
                break

        if before is None or after is None or t_after <= t_before:
            return self._snapshot_to_remote_states(snapshots[-1], local_entity_id)

        alpha = (render_time - t_before) / (t_after - t_before)
        alpha = max(0.0, min(1.0, alpha))

        result = {}
        for entity_id, entity_after in after.entities.items():
            if entity_id == local_entity_id:
                continue

            if entity_id in before.entities:
                entity_before = before.entities[entity_id]
                result[entity_id] = {
                    "x": entity_before.x + (entity_after.x - entity_before.x) * alpha,
                    "y": entity_before.y + (entity_after.y - entity_before.y) * alpha,
                    "vx": entity_after.vx,
                    "vy": entity_after.vy,
                    "health": entity_after.health,
                    "ping_ms": entity_after.ping_ms,
                    "respawn_ticks_remaining": entity_after.respawn_ticks_remaining,
                    "effect_flags": entity_after.effect_flags,
                    "dash_cooldown": entity_after.dash_cooldown,
                    "dash_timer": entity_after.dash_timer,
                }
            else:
                result[entity_id] = entity_after.to_dict()

        return result
