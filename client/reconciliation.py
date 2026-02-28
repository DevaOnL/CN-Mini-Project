"""
Server reconciliation: corrects client-side prediction errors
by rebasing on the authoritative server state and replaying
unacknowledged inputs.
"""

import math


class Reconciler:
    """
    When a server snapshot arrives, reconcile the local predicted state
    with the authority by replaying un-acknowledged inputs.
    """

    def __init__(self, predictor):
        self.predictor = predictor
        # Track prediction error for metrics
        self.last_error = 0.0

    def reconcile(self, server_entity_state: dict, last_acked_input_seq: int,
                  pending_inputs: list) -> tuple:
        """
        Correct local state using server's authoritative snapshot.

        Args:
            server_entity_state: dict with x, y, vx, vy, health from server
            last_acked_input_seq: the last client input sequence the server processed
            pending_inputs: list of dicts with 'sequence', 'input', 'predicted_state'

        Returns:
            (corrected_state, remaining_pending_inputs, prediction_error)
        """
        # Start from the server's authoritative state
        state = {
            'x': server_entity_state['x'],
            'y': server_entity_state['y'],
            'vx': server_entity_state['vx'],
            'vy': server_entity_state['vy'],
            'health': server_entity_state['health'],
        }

        # Discard inputs already processed by server
        remaining = [
            inp for inp in pending_inputs
            if inp['sequence'] > last_acked_input_seq
        ]

        # Re-apply unprocessed inputs on top of server state
        for inp_record in remaining:
            state = self.predictor.predict(state, inp_record['input'])

        # Calculate prediction error (distance between predicted and corrected)
        if pending_inputs:
            # Find the predicted state at the server's acked input
            for inp in pending_inputs:
                if inp['sequence'] == last_acked_input_seq and 'predicted_state' in inp:
                    pred = inp['predicted_state']
                    dx = pred.get('x', 0) - server_entity_state['x']
                    dy = pred.get('y', 0) - server_entity_state['y']
                    self.last_error = math.sqrt(dx * dx + dy * dy)
                    break

        return state, remaining, self.last_error


def smooth_correction(current_visual: dict, target: dict,
                      smoothing: float = 0.1) -> dict:
    """
    Exponential smoothing toward the corrected state.
    Prevents jarring visual snaps when reconciliation corrects position.
    """
    result = current_visual.copy()
    result['x'] = current_visual['x'] + (target['x'] - current_visual['x']) * smoothing
    result['y'] = current_visual['y'] + (target['y'] - current_visual['y']) * smoothing
    return result
