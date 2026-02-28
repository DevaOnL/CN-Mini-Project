"""
Metrics logging for performance analysis.
Logs RTT, jitter, packet loss, bandwidth, and prediction errors.
"""

import json
import os
import time


class MetricsLogger:
    """Collects and persists network/game performance metrics."""

    def __init__(self, log_dir: str = 'analysis/logs'):
        os.makedirs(log_dir, exist_ok=True)
        self.log_dir = log_dir
        self.start_time = time.time()
        self.data = {
            'rtt': [],
            'jitter': [],
            'packet_loss': [],
            'bandwidth': [],
            'prediction_error': [],
            'tick_times': [],
        }
        # Running jitter computation (RFC 3550)
        self._prev_rtt = None
        self._smoothed_jitter = 0.0

    def log_rtt(self, rtt_ms: float):
        """Log a round-trip time sample."""
        t = time.time() - self.start_time
        self.data['rtt'].append({'t': round(t, 4), 'rtt_ms': round(rtt_ms, 3)})

        # Compute jitter
        if self._prev_rtt is not None:
            diff = abs(rtt_ms - self._prev_rtt)
            self._smoothed_jitter += (diff - self._smoothed_jitter) / 16.0
            self.data['jitter'].append({
                't': round(t, 4),
                'jitter_ms': round(self._smoothed_jitter, 3),
                'instant_jitter_ms': round(diff, 3)
            })
        self._prev_rtt = rtt_ms

    def log_packet_loss(self, loss_rate: float):
        t = time.time() - self.start_time
        self.data['packet_loss'].append({
            't': round(t, 4), 'loss': round(loss_rate, 6)
        })

    def log_bandwidth(self, bytes_sent: int, bytes_recv: int):
        t = time.time() - self.start_time
        self.data['bandwidth'].append({
            't': round(t, 4),
            'sent_bytes': bytes_sent,
            'recv_bytes': bytes_recv
        })

    def log_prediction_error(self, error_px: float):
        t = time.time() - self.start_time
        self.data['prediction_error'].append({
            't': round(t, 4), 'error_px': round(error_px, 3)
        })

    def log_tick_time(self, tick: int, duration_ms: float):
        self.data['tick_times'].append({
            'tick': tick, 'duration_ms': round(duration_ms, 4)
        })

    def save(self, filename: str = 'metrics.json'):
        path = os.path.join(self.log_dir, filename)
        with open(path, 'w') as f:
            json.dump(self.data, f, indent=2)
        print(f"[METRICS] Saved to {path}")
        return path

    def get_summary(self) -> dict:
        """Compute summary statistics."""
        summary = {}
        rtts = [r['rtt_ms'] for r in self.data['rtt']]
        if rtts:
            rtts_sorted = sorted(rtts)
            summary['rtt_mean'] = sum(rtts) / len(rtts)
            summary['rtt_min'] = min(rtts)
            summary['rtt_max'] = max(rtts)
            summary['rtt_p50'] = rtts_sorted[len(rtts_sorted) // 2]
            summary['rtt_p95'] = rtts_sorted[int(len(rtts_sorted) * 0.95)]
            summary['rtt_p99'] = rtts_sorted[int(len(rtts_sorted) * 0.99)]

        jitters = [j['jitter_ms'] for j in self.data['jitter']]
        if jitters:
            summary['jitter_mean'] = sum(jitters) / len(jitters)

        losses = [l['loss'] for l in self.data['packet_loss']]
        if losses:
            summary['loss_rate_mean'] = sum(losses) / len(losses)

        ticks = [t['duration_ms'] for t in self.data['tick_times']]
        if ticks:
            summary['tick_time_mean'] = sum(ticks) / len(ticks)
            summary['tick_time_max'] = max(ticks)

        return summary
