"""
Main game server — authoritative, tick-based, UDP.

Handles:
- Client connections/disconnections
- Input processing
- Game simulation at a fixed tick rate
- Snapshot broadcasting
- Ping/pong for latency measurement
"""

import socket
import struct
import time
import os

from common.packet import (
    Packet, PacketType, HEADER_SIZE,
    INPUT_FORMAT, INPUT_SIZE, PING_FORMAT, PING_SIZE
)
from common.net import create_server_socket, AckTracker, NetworkSimulator
from common.snapshot import Snapshot
from common.config import (
    DEFAULT_HOST, DEFAULT_PORT, DEFAULT_TICK_RATE,
    DEFAULT_BUFFER_SIZE
)
from common.metrics_logger import MetricsLogger
from server.game_state import GameState
from server.client_manager import ClientManager


class GameServer:
    """
    Authoritative game server.
    Runs a fixed-timestep simulation loop, processes client inputs,
    and broadcasts state snapshots over UDP.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 tick_rate: int = DEFAULT_TICK_RATE,
                 loss_sim: float = 0.0, latency_sim: float = 0.0,
                 verbose: bool = True):
        self.host = host
        self.port = port
        self.tick_rate = tick_rate
        self.dt = 1.0 / tick_rate
        self.running = False
        self.verbose = verbose

        # Socket
        self.sock = create_server_socket(host, port)

        # Optional network simulation
        self.net_sim = None
        if loss_sim > 0 or latency_sim > 0:
            self.net_sim = NetworkSimulator(
                self.sock, loss_rate=loss_sim,
                min_latency=latency_sim * 0.5,
                max_latency=latency_sim * 1.5
            )

        # Core systems
        self.game_state = GameState()
        self.client_mgr = ClientManager()
        self.metrics = MetricsLogger()

        # Statistics
        self.current_tick = 0
        self.total_bytes_sent = 0
        self.total_bytes_recv = 0

    def _log(self, msg: str):
        """Print a message if verbose mode is enabled."""
        if self.verbose:
            print(msg, flush=True)

    def _sendto(self, data: bytes, addr: tuple):
        """Send data through the socket or network simulator."""
        if self.net_sim:
            self.net_sim.sendto(data, addr)
        else:
            try:
                self.sock.sendto(data, addr)
            except (BlockingIOError, OSError):
                pass
        self.total_bytes_sent += len(data)

    def receive_all_packets(self):
        """Drain all pending datagrams from the socket."""
        for _ in range(1000):  # Safety limit
            try:
                data, addr = self.sock.recvfrom(DEFAULT_BUFFER_SIZE)
                self.total_bytes_recv += len(data)
                self._handle_packet(data, addr)
            except BlockingIOError:
                break
            except OSError:
                break

    def _handle_packet(self, data: bytes, addr: tuple):
        """Route an incoming packet to the appropriate handler."""
        try:
            pkt = Packet.deserialize(data)
        except ValueError as e:
            return

        if pkt.packet_type == PacketType.CONNECT_REQ:
            self._handle_connect(addr)
        elif pkt.packet_type == PacketType.INPUT:
            self._handle_input(pkt, addr)
        elif pkt.packet_type == PacketType.DISCONNECT:
            self._handle_disconnect(addr)
        elif pkt.packet_type == PacketType.PING:
            self._handle_ping(pkt, addr)
        elif pkt.packet_type == PacketType.HEARTBEAT:
            self._handle_heartbeat(addr)

        # Update client's ack tracker
        client = self.client_mgr.get_by_address(addr)
        if client:
            client.touch()
            client.bytes_received += len(data)
            client.ack_tracker.on_packet_received(pkt.sequence)

    def _handle_connect(self, addr: tuple):
        """Handle a new client connection request."""
        if self.client_mgr.has_address(addr):
            # Re-send ACK in case the first one was lost
            client = self.client_mgr.get_by_address(addr)
            payload = struct.pack('!B', client.client_id)
            ack_pkt = Packet(PacketType.CONNECT_ACK, 0, 0, 0, payload)
            self._sendto(ack_pkt.serialize(), addr)
            return

        client = self.client_mgr.add_client(addr)
        self.game_state.add_entity(client.client_id)

        # Send CONNECT_ACK with assigned client_id
        payload = struct.pack('!B', client.client_id)
        ack_pkt = Packet(PacketType.CONNECT_ACK, 0, 0, 0, payload)
        self._sendto(ack_pkt.serialize(), addr)
        self._log(f"[SERVER] Client {client.client_id} connected from {addr}")

    def _handle_input(self, pkt: Packet, addr: tuple):
        """Handle an input packet from a client."""
        client = self.client_mgr.get_by_address(addr)
        if client is None:
            return

        # Parse possibly redundant inputs (multiple inputs per packet)
        offset = 0
        payload = pkt.payload

        # Check if first byte is a count (redundant input format)
        if len(payload) > 0 and len(payload) != INPUT_SIZE:
            # Redundant format: count byte + N inputs
            count = payload[0]
            offset = 1
            for _ in range(count):
                if offset + INPUT_SIZE > len(payload):
                    break
                tick, mx, my, actions = struct.unpack(
                    INPUT_FORMAT, payload[offset:offset + INPUT_SIZE]
                )
                # Only add if we haven't processed this tick's input yet
                if not any(inp[0] == tick for inp in client.pending_inputs):
                    client.pending_inputs.append((tick, mx, my, actions))
                offset += INPUT_SIZE
        elif len(payload) >= INPUT_SIZE:
            # Single input format
            tick, mx, my, actions = struct.unpack(
                INPUT_FORMAT, payload[:INPUT_SIZE]
            )
            client.pending_inputs.append((tick, mx, my, actions))

    def _handle_ping(self, pkt: Packet, addr: tuple):
        """Echo back a PONG with the client's timestamp."""
        pong = Packet(PacketType.PONG, 0, pkt.sequence, 0, pkt.payload)
        self._sendto(pong.serialize(), addr)

    def _handle_heartbeat(self, addr: tuple):
        """Just update the last-heard time (already done in _handle_packet)."""
        pass

    def _handle_disconnect(self, addr: tuple):
        """Handle a graceful disconnect."""
        client = self.client_mgr.get_by_address(addr)
        if client:
            cid = client.client_id
            self.game_state.remove_entity(cid)
            self.client_mgr.remove_client(cid)
            self._log(f"[SERVER] Client {cid} disconnected")

    def simulate_tick(self):
        """Run one simulation step: apply inputs, check timeouts."""
        tick_start = time.perf_counter()
        self.game_state.tick = self.current_tick

        # Apply all pending inputs for each client
        for client in self.client_mgr.all_clients():
            for (input_tick, mx, my, actions) in client.pending_inputs:
                self.game_state.apply_input(
                    client.client_id, mx, my, actions, self.dt
                )
                client.last_processed_input_seq = max(
                    client.last_processed_input_seq, input_tick
                )
            client.pending_inputs.clear()

        # Check for timed-out clients
        timed_out = self.client_mgr.check_timeouts()
        for cid in timed_out:
            self.game_state.remove_entity(cid)
            self._log(f"[SERVER] Client {cid} timed out and removed")

        tick_duration = (time.perf_counter() - tick_start) * 1000.0
        self.metrics.log_tick_time(self.current_tick, tick_duration)

    def send_snapshots(self):
        """Broadcast the current world state to all connected clients."""
        snapshot = self.game_state.get_snapshot()
        base_payload = snapshot.serialize()

        for client in self.client_mgr.all_clients():
            seq = client.ack_tracker.next_sequence()
            # Append per-client last-processed input seq (4 bytes)
            payload = base_payload + struct.pack(
                '!I', client.last_processed_input_seq
            )
            pkt = Packet(
                PacketType.SNAPSHOT,
                seq,
                client.ack_tracker.remote_sequence,
                client.ack_tracker.ack_bitfield,
                payload
            )
            data = pkt.serialize()
            self._sendto(data, client.address)
            client.bytes_sent += len(data)
            client.ack_tracker.on_packet_sent(seq)

    def run(self):
        """Main server loop with fixed timestep."""
        self.running = True
        self._log(f"[SERVER] Started on {self.host}:{self.port} "
                f"@ {self.tick_rate} Hz (dt={self.dt:.4f}s)")

        next_tick_time = time.perf_counter()
        last_stats_time = time.perf_counter()
        stats_interval = 5.0  # Print stats every 5 seconds

        try:
            while self.running:
                now = time.perf_counter()

                # Process all incoming packets
                self.receive_all_packets()

                # Flush delayed packets (network simulator)
                if self.net_sim:
                    self.net_sim.flush()

                # Advance simulation at fixed rate
                while now >= next_tick_time:
                    self.simulate_tick()
                    self.send_snapshots()
                    self.current_tick += 1
                    next_tick_time += self.dt

                # Periodic stats
                if now - last_stats_time >= stats_interval:
                    clients = self.client_mgr.count
                    self._log(f"[SERVER] Tick {self.current_tick} | "
                          f"Clients: {clients} | "
                          f"Sent: {self.total_bytes_sent / 1024:.1f} KB | "
                          f"Recv: {self.total_bytes_recv / 1024:.1f} KB")
                    last_stats_time = now

                # Sleep briefly to avoid busy-wait
                sleep_time = next_tick_time - time.perf_counter()
                if sleep_time > 0.001:
                    time.sleep(sleep_time * 0.8)
                elif sleep_time > 0:
                    time.sleep(0.0001)

        except KeyboardInterrupt:
            self._log("\n[SERVER] Shutting down...")
        finally:
            self.running = False
            self.sock.close()
            self.metrics.save('server_metrics.json')
            summary = self.metrics.get_summary()
            if summary:
                self._log(f"[SERVER] Metrics summary: {summary}")


def main():
    """Entry point for running the server standalone."""
    import argparse
    parser = argparse.ArgumentParser(description='Game Networking Engine Server')
    parser.add_argument('--host', default=DEFAULT_HOST, help='Bind address')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help='Bind port')
    parser.add_argument('--tick-rate', type=int, default=DEFAULT_TICK_RATE,
                        help='Server tick rate (Hz)')
    parser.add_argument('--loss', type=float, default=0.0,
                        help='Simulated packet loss rate (0.0-1.0)')
    parser.add_argument('--latency', type=float, default=0.0,
                        help='Simulated base latency (seconds)')
    args = parser.parse_args()

    server = GameServer(
        host=args.host, port=args.port, tick_rate=args.tick_rate,
        loss_sim=args.loss, latency_sim=args.latency
    )
    server.run()


if __name__ == '__main__':
    main()
