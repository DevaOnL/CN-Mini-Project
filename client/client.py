"""
Main game client — connects to server, sends inputs, receives snapshots,
handles prediction, reconciliation, and interpolation.
"""

import socket
import struct
import time
import os

from common.packet import (
    Packet, PacketType,
    INPUT_FORMAT, INPUT_SIZE, PING_FORMAT, PING_SIZE
)
from common.net import create_client_socket, AckTracker, NetworkSimulator
from common.snapshot import Snapshot
from common.config import (
    DEFAULT_PORT, DEFAULT_TICK_RATE, DEFAULT_BUFFER_SIZE,
    PING_INTERVAL, INPUT_REDUNDANCY, INTERPOLATION_TICKS,
    CONNECT_RETRY_INTERVAL
)
from common.metrics_logger import MetricsLogger
from client.prediction import Predictor
from client.reconciliation import Reconciler, smooth_correction
from client.interpolation import Interpolator
from client.renderer import GameRenderer


class GameClient:
    """
    Game client that communicates with the authoritative server.
    Implements client-side prediction, server reconciliation,
    and entity interpolation.
    """

    def __init__(self, server_host: str = '127.0.0.1',
                 server_port: int = DEFAULT_PORT,
                 tick_rate: int = DEFAULT_TICK_RATE,
                 headless: bool = False,
                 loss_sim: float = 0.0, latency_sim: float = 0.0):
        self.server_addr = (server_host, server_port)
        self.tick_rate = tick_rate
        self.dt = 1.0 / tick_rate
        self.running = False
        self.headless = headless

        # Socket
        self.sock = create_client_socket()

        # Optional network simulation
        self.net_sim = None
        if loss_sim > 0 or latency_sim > 0:
            self.net_sim = NetworkSimulator(
                self.sock, loss_rate=loss_sim,
                min_latency=latency_sim * 0.5,
                max_latency=latency_sim * 1.5
            )

        # Connection state
        self.client_id = None
        self.connected = False

        # Sequence tracking
        self.ack_tracker = AckTracker()

        # Game state
        self.local_state = {}           # Predicted local entity state
        self.visual_state = {}          # Smoothed visual state for rendering
        self.server_snapshots = []      # Buffer of received Snapshot objects
        self.pending_inputs = []        # Inputs sent but not yet server-confirmed
        self.input_sequence = 0

        # Core systems
        self.predictor = Predictor(dt=self.dt)
        self.reconciler = Reconciler(self.predictor)
        self.interpolator = Interpolator(tick_rate=tick_rate,
                                         interp_ticks=INTERPOLATION_TICKS)

        # Metrics
        self.metrics = MetricsLogger()
        self.rtt_samples = []
        self.current_rtt = 0.0
        self.current_jitter = 0.0
        self.last_server_tick = 0

        # Bandwidth
        self.total_bytes_sent = 0
        self.total_bytes_recv = 0

        # Input history for redundancy
        self.input_history = []

    def _sendto(self, data: bytes, addr: tuple = None):
        """Send data to the server."""
        addr = addr or self.server_addr
        if self.net_sim:
            self.net_sim.sendto(data, addr)
        else:
            try:
                self.sock.sendto(data, addr)
            except (BlockingIOError, OSError):
                pass
        self.total_bytes_sent += len(data)

    def connect(self):
        """Send connection request to server."""
        pkt = Packet(PacketType.CONNECT_REQ, 0, 0, 0)
        self._sendto(pkt.serialize())
        print(f"[CLIENT] Connecting to {self.server_addr}...")

    def disconnect(self):
        """Send disconnect notification and close socket."""
        if self.connected:
            pkt = Packet(PacketType.DISCONNECT, 0, 0, 0)
            self._sendto(pkt.serialize())
            self.connected = False
            print("[CLIENT] Disconnected")
        try:
            self.sock.close()
        except OSError:
            pass

    def receive_packets(self):
        """Drain all pending packets from the socket."""
        for _ in range(1000):
            try:
                data, addr = self.sock.recvfrom(DEFAULT_BUFFER_SIZE)
                self.total_bytes_recv += len(data)
                self._handle_packet(data)
            except BlockingIOError:
                break
            except OSError:
                break

    def _handle_packet(self, data: bytes):
        """Route received packet to proper handler."""
        try:
            pkt = Packet.deserialize(data)
        except ValueError:
            return

        if pkt.packet_type == PacketType.CONNECT_ACK:
            self._handle_connect_ack(pkt)
        elif pkt.packet_type == PacketType.SNAPSHOT:
            self._handle_snapshot(pkt)
        elif pkt.packet_type == PacketType.PONG:
            self._handle_pong(pkt)

        # Update ack tracking
        self.ack_tracker.on_packet_received(pkt.sequence)
        if pkt.ack > 0:
            self.ack_tracker.on_ack_received(pkt.ack, pkt.ack_bitfield)

    def _handle_connect_ack(self, pkt: Packet):
        """Handle connection acceptance from server."""
        if len(pkt.payload) >= 1:
            self.client_id = struct.unpack('!B', pkt.payload[:1])[0]
            self.connected = True
            # Local state initialized on first snapshot to match server spawn
            print(f"[CLIENT] Connected! Assigned ID: {self.client_id}")

    def _handle_snapshot(self, pkt: Packet):
        """Handle world state snapshot from server."""
        try:
            snapshot = Snapshot.deserialize(pkt.payload)
        except ValueError:
            return

        # Extract per-client last_processed_input_seq appended after snapshot
        last_input_seq = 0
        snapshot_data_len = snapshot.serialized_size()
        if len(pkt.payload) >= snapshot_data_len + 4:
            last_input_seq = struct.unpack(
                '!I', pkt.payload[snapshot_data_len:snapshot_data_len + 4]
            )[0]

        self.server_snapshots.append(snapshot)
        self.last_server_tick = snapshot.tick

        # Keep buffer bounded
        if len(self.server_snapshots) > 60:
            self.server_snapshots = self.server_snapshots[-60:]

        # Reconcile local player state
        if self.client_id and self.client_id in snapshot.entities:
            server_entity = snapshot.entities[self.client_id]
            server_state = server_entity.to_dict()

            # Initialize local state from first snapshot (match server spawn)
            if not self.local_state:
                self.local_state = server_state.copy()
                self.visual_state = server_state.copy()

            corrected, remaining, error = self.reconciler.reconcile(
                server_state, last_input_seq, self.pending_inputs
            )
            self.pending_inputs = remaining
            self.local_state = corrected

            # Smooth visual correction
            if self.visual_state:
                self.visual_state = smooth_correction(
                    self.visual_state, self.local_state, smoothing=0.3
                )
            else:
                self.visual_state = self.local_state.copy()

            # Log prediction error
            if error > 0.01:
                self.metrics.log_prediction_error(error)

    def _handle_pong(self, pkt: Packet):
        """Calculate RTT from echoed timestamp."""
        if len(pkt.payload) >= PING_SIZE:
            sent_time = struct.unpack(PING_FORMAT, pkt.payload[:PING_SIZE])[0]
            rtt = time.perf_counter() - sent_time
            rtt_ms = rtt * 1000.0

            self.current_rtt = rtt_ms
            self.rtt_samples.append(rtt_ms)
            if len(self.rtt_samples) > 200:
                self.rtt_samples = self.rtt_samples[-200:]

            self.metrics.log_rtt(rtt_ms)

    def send_input(self, inp: dict):
        """Pack and send client input to server."""
        if not self.connected:
            return

        self.input_sequence += 1
        seq = self.ack_tracker.next_sequence()

        # Build redundant input payload
        input_record = {
            'sequence': self.input_sequence,
            'move_x': inp['move_x'],
            'move_y': inp['move_y'],
            'actions': inp['actions']
        }

        # Add to history for redundancy
        self.input_history.append(input_record)
        if len(self.input_history) > INPUT_REDUNDANCY * 2:
            self.input_history = self.input_history[-(INPUT_REDUNDANCY * 2):]

        # Pack redundant inputs
        recent = self.input_history[-INPUT_REDUNDANCY:]
        payload = struct.pack('!B', len(recent))
        for rec in recent:
            payload += struct.pack(INPUT_FORMAT,
                                   rec['sequence'],
                                   rec['move_x'],
                                   rec['move_y'],
                                   rec['actions'])

        pkt = Packet(
            PacketType.INPUT, seq,
            self.ack_tracker.remote_sequence,
            self.ack_tracker.ack_bitfield,
            payload
        )
        data = pkt.serialize()
        self._sendto(data)
        self.ack_tracker.on_packet_sent(seq)

        # Save for reconciliation
        self.pending_inputs.append({
            'sequence': self.input_sequence,
            'input': inp,
            'predicted_state': self.local_state.copy()
        })

        # Trim old pending inputs
        if len(self.pending_inputs) > 60:
            self.pending_inputs = self.pending_inputs[-60:]

    def send_ping(self):
        """Send a PING to measure RTT."""
        timestamp = time.perf_counter()
        payload = struct.pack(PING_FORMAT, timestamp)
        seq = self.ack_tracker.next_sequence()
        pkt = Packet(PacketType.PING, seq, 0, 0, payload)
        self._sendto(pkt.serialize())

    def predict_local(self, inp: dict):
        """Apply client-side prediction."""
        if not self.connected or not self.local_state:
            return

        self.local_state = self.predictor.predict(self.local_state, inp)

        # Smooth visual towards predicted
        if self.visual_state:
            self.visual_state = smooth_correction(
                self.visual_state, self.local_state, smoothing=0.5
            )
        else:
            self.visual_state = self.local_state.copy()

    def get_remote_states(self) -> dict:
        """Get interpolated remote entity states for rendering."""
        if not self.server_snapshots:
            return {}

        # Estimate current server tick
        current_tick = self.last_server_tick + 1.0

        return self.interpolator.interpolate(
            self.server_snapshots, current_tick,
            self.client_id or -1
        )

    def get_metrics_display(self) -> dict:
        """Get metrics dict for HUD display."""
        metrics = {}
        metrics['RTT'] = f"{self.current_rtt:.1f} ms"

        if len(self.rtt_samples) >= 2:
            jitters = [abs(self.rtt_samples[i] - self.rtt_samples[i - 1])
                       for i in range(1, len(self.rtt_samples))]
            self.current_jitter = sum(jitters[-10:]) / min(10, len(jitters))
            metrics['Jitter'] = f"{self.current_jitter:.1f} ms"

        loss = self.ack_tracker.get_loss_rate()
        metrics['Loss'] = f"{loss * 100:.1f}%"
        metrics['Tick'] = str(self.last_server_tick)
        metrics['Pending'] = str(len(self.pending_inputs))
        metrics['Players'] = str(
            len(self.server_snapshots[-1].entities) if self.server_snapshots else 0
        )

        return metrics

    def run(self):
        """Main client loop."""
        self.running = True

        # Create renderer
        if self.headless:
            renderer = GameRenderer.__new__(GameRenderer)
            renderer.headless = True
        else:
            renderer = GameRenderer()

        # Connect
        self.connect()

        send_interval = self.dt
        next_send_time = time.perf_counter()
        next_ping_time = time.perf_counter()
        connect_retry_time = time.perf_counter() + CONNECT_RETRY_INTERVAL
        last_bandwidth_time = time.perf_counter()
        last_bytes_sent = 0
        last_bytes_recv = 0

        try:
            while self.running:
                now = time.perf_counter()

                # Check quit
                if not self.headless and renderer.check_quit():
                    break

                # Retry connection if not connected
                if not self.connected and now >= connect_retry_time:
                    self.connect()
                    connect_retry_time = now + CONNECT_RETRY_INTERVAL
                    continue

                # Flush network simulator
                if self.net_sim:
                    self.net_sim.flush()

                # 1. Receive packets
                self.receive_packets()

                # 2. Get input
                inp = renderer.get_input() if not self.headless else \
                    {'move_x': 0.0, 'move_y': 0.0, 'actions': 0}

                # 3. Predict locally
                self.predict_local(inp)

                # 4. Send input at tick rate
                if now >= next_send_time and self.connected:
                    self.send_input(inp)
                    next_send_time += send_interval

                # 5. Interpolate remote entities
                remote_states = self.get_remote_states()

                # 6. Render
                if not self.headless:
                    metrics = self.get_metrics_display()
                    renderer.render(
                        self.visual_state, remote_states,
                        self.client_id or 0, metrics
                    )

                # 7. Periodic ping
                if now >= next_ping_time and self.connected:
                    self.send_ping()
                    next_ping_time += PING_INTERVAL

                # 8. Periodic bandwidth logging
                if now - last_bandwidth_time >= 1.0:
                    self.metrics.log_bandwidth(
                        self.total_bytes_sent - last_bytes_sent,
                        self.total_bytes_recv - last_bytes_recv
                    )
                    loss = self.ack_tracker.get_loss_rate()
                    self.metrics.log_packet_loss(loss)
                    # Detect lost packets
                    self.ack_tracker.detect_lost_packets()

                    last_bytes_sent = self.total_bytes_sent
                    last_bytes_recv = self.total_bytes_recv
                    last_bandwidth_time = now

                # Yield CPU
                time.sleep(0.001)

        except KeyboardInterrupt:
            print("\n[CLIENT] Interrupted")
        finally:
            self.disconnect()
            self.running = False
            if not self.headless:
                renderer.close()

            # Save metrics
            self.metrics.save(f'client_{self.client_id or 0}_metrics.json')
            summary = self.metrics.get_summary()
            if summary:
                print(f"[CLIENT] Metrics summary: {summary}")


def main():
    """Entry point for running the client standalone."""
    import argparse
    parser = argparse.ArgumentParser(description='Game Networking Engine Client')
    parser.add_argument('--host', default='127.0.0.1', help='Server address')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help='Server port')
    parser.add_argument('--tick-rate', type=int, default=DEFAULT_TICK_RATE,
                        help='Client tick rate (Hz)')
    parser.add_argument('--headless', action='store_true',
                        help='Run without pygame (for bots/testing)')
    parser.add_argument('--loss', type=float, default=0.0,
                        help='Simulated packet loss rate (0.0-1.0)')
    parser.add_argument('--latency', type=float, default=0.0,
                        help='Simulated base latency (seconds)')
    args = parser.parse_args()

    client = GameClient(
        server_host=args.host, server_port=args.port,
        tick_rate=args.tick_rate, headless=args.headless,
        loss_sim=args.loss, latency_sim=args.latency
    )
    client.run()


if __name__ == '__main__':
    main()
