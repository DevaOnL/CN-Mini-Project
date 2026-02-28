"""
Entity interpolation for rendering remote players smoothly.
Remote entities are rendered slightly in the past,
interpolating between two server snapshots.
"""


class Interpolator:
    """
    Interpolates remote entity positions between server snapshots.
    Renders at (current_time - interp_delay) for smooth motion.
    """

    def __init__(self, tick_rate: int = 20, interp_ticks: int = 2):
        """
        Args:
            tick_rate: server tick rate in Hz
            interp_ticks: number of ticks to render behind (typically 2)
        """
        self.tick_rate = tick_rate
        self.interp_ticks = interp_ticks
        self.tick_duration = 1.0 / tick_rate

    def interpolate(self, snapshots: list, current_tick_estimate: float,
                    local_entity_id: int) -> dict:
        """
        Interpolate entity positions for all remote entities.

        Args:
            snapshots: list of Snapshot objects, ordered by tick
            current_tick_estimate: estimated current server tick (float)
            local_entity_id: skip interpolation for local player

        Returns:
            dict of entity_id -> {'x', 'y', 'vx', 'vy', 'health'}
        """
        if not snapshots:
            return {}

        # Target render tick is behind the current estimate
        target_tick = current_tick_estimate - self.interp_ticks

        # Find two snapshots that bracket the target tick
        before = None
        after = None

        for i in range(len(snapshots) - 1):
            s0 = snapshots[i]
            s1 = snapshots[i + 1]
            if s0.tick <= target_tick <= s1.tick:
                before = s0
                after = s1
                break

        if before is None or after is None:
            # Not enough data — use the latest snapshot
            latest = snapshots[-1]
            result = {}
            for eid, e in latest.entities.items():
                if eid == local_entity_id:
                    continue
                result[eid] = e.to_dict()
            return result

        # Compute interpolation factor (0.0 = before, 1.0 = after)
        tick_range = after.tick - before.tick
        if tick_range > 0:
            alpha = (target_tick - before.tick) / tick_range
        else:
            alpha = 0.0
        alpha = max(0.0, min(1.0, alpha))

        result = {}
        for eid in after.entities:
            if eid == local_entity_id:
                continue

            e1 = after.entities[eid]

            if eid in before.entities:
                e0 = before.entities[eid]
                result[eid] = {
                    'x': e0.x + (e1.x - e0.x) * alpha,
                    'y': e0.y + (e1.y - e0.y) * alpha,
                    'vx': e1.vx,
                    'vy': e1.vy,
                    'health': e1.health,
                }
            else:
                result[eid] = e1.to_dict()

        return result
