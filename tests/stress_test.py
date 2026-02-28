"""
Stress test: measure performance with increasing client counts.
"""

import os
import time
import threading
import struct
import socket
import random
import json

from common.packet import Packet, PacketType, INPUT_FORMAT
from common.snapshot import Snapshot
from common.config import DEFAULT_PORT, DEFAULT_BUFFER_SIZE


class StressBotClient:
    """Lightweight bot for stress testing."""

    def __init__(self, host: str = '127.0.0.1', port: int = DEFAULT_PORT):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.server_addr = (host, port)
        self.client_id = None
        self.connected = False
        self.input_seq = 0
        self.snapshots_received = 0
        self.bytes_received = 0

    def connect(self):
        pkt = Packet(PacketType.CONNECT_REQ, 0, 0, 0)
        self.sock.sendto(pkt.serialize(), self.server_addr)

    def send_input(self):
        if not self.connected:
            return
        self.input_seq += 1
        mx = random.uniform(-1.0, 1.0)
        my = random.uniform(-1.0, 1.0)
        payload = struct.pack(INPUT_FORMAT, self.input_seq, mx, my, 0)
        pkt = Packet(PacketType.INPUT, self.input_seq & 0xFFFF, 0, 0, payload)
        self.sock.sendto(pkt.serialize(), self.server_addr)

    def receive(self):
        for _ in range(50):
            try:
                data, _ = self.sock.recvfrom(DEFAULT_BUFFER_SIZE)
                self.bytes_received += len(data)
                pkt = Packet.deserialize(data)
                if pkt.packet_type == PacketType.CONNECT_ACK:
                    self.client_id = struct.unpack('!B', pkt.payload[:1])[0]
                    self.connected = True
                elif pkt.packet_type == PacketType.SNAPSHOT:
                    self.snapshots_received += 1
            except (BlockingIOError, OSError, ValueError):
                break

    def close(self):
        pkt = Packet(PacketType.DISCONNECT, 0, 0, 0)
        try:
            self.sock.sendto(pkt.serialize(), self.server_addr)
        except OSError:
            pass
        self.sock.close()


def run_stress_test(num_clients: int, duration: float = 10.0,
                    tick_rate: int = 20) -> dict:
    """Run a stress test with N clients for a given duration."""
    from server.server import GameServer

    port = random.randint(10000, 60000)
    server = GameServer(port=port, tick_rate=tick_rate, verbose=False)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    time.sleep(0.3)

    # Create and connect bots
    bots = []
    for _ in range(num_clients):
        bot = StressBotClient(port=port)
        bot.connect()
        bots.append(bot)

    # Wait for connections
    for _ in range(30):
        time.sleep(0.1)
        for bot in bots:
            bot.receive()
        if all(b.connected for b in bots):
            break

    connected = sum(1 for b in bots if b.connected)
    print(f"  Connected: {connected}/{num_clients}")

    # Run for duration
    start = time.perf_counter()
    ticks = 0
    while time.perf_counter() - start < duration:
        for bot in bots:
            if bot.connected:
                bot.send_input()
        time.sleep(1.0 / tick_rate)
        for bot in bots:
            bot.receive()
        ticks += 1

    elapsed = time.perf_counter() - start

    # Collect results
    total_snapshots = sum(b.snapshots_received for b in bots)
    total_bytes_recv = sum(b.bytes_received for b in bots)

    result = {
        'clients': num_clients,
        'connected': connected,
        'duration_s': round(elapsed, 2),
        'server_ticks': server.current_tick,
        'total_snapshots': total_snapshots,
        'avg_snapshots_per_client': round(total_snapshots / max(connected, 1), 1),
        'total_bytes_recv_kb': round(total_bytes_recv / 1024, 1),
        'server_bytes_sent_kb': round(server.total_bytes_sent / 1024, 1),
        'server_bytes_recv_kb': round(server.total_bytes_recv / 1024, 1),
    }

    # Server tick times
    tick_times = [t['duration_ms'] for t in server.metrics.data.get('tick_times', [])]
    if tick_times:
        result['avg_tick_time_ms'] = round(sum(tick_times) / len(tick_times), 4)
        result['max_tick_time_ms'] = round(max(tick_times), 4)

    # Cleanup
    for bot in bots:
        bot.close()
    server.running = False
    time.sleep(0.5)

    return result


def main():
    """Run stress tests with increasing client counts."""
    import argparse
    parser = argparse.ArgumentParser(description='Stress test')
    parser.add_argument('--bots', type=int, default=0,
                        help='Single bot count (overrides sweep)')
    parser.add_argument('--duration', type=float, default=3.0,
                        help='Duration per test in seconds')
    args = parser.parse_args()

    CLIENT_COUNTS = [args.bots] if args.bots > 0 else [2, 4, 8, 16]
    DURATION = args.duration
    TICK_RATE = 20

    print("=" * 70)
    print("  Stress Test: Multiplayer Networking Engine")
    print(f"  Duration: {DURATION}s per test | Tick Rate: {TICK_RATE} Hz")
    print("=" * 70)

    results = []
    for n in CLIENT_COUNTS:
        print(f"\n--- Testing with {n} clients ---")
        result = run_stress_test(n, duration=DURATION, tick_rate=TICK_RATE)
        results.append(result)
        print(f"  Server ticks:      {result['server_ticks']}")
        print(f"  Snapshots/client:  {result['avg_snapshots_per_client']}")
        print(f"  Server sent:       {result['server_bytes_sent_kb']} KB")
        print(f"  Avg tick time:     {result.get('avg_tick_time_ms', 'N/A')} ms")
        print(f"  Max tick time:     {result.get('max_tick_time_ms', 'N/A')} ms")

    # Save results
    output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'analysis', 'logs', 'stress_test_results.json')
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(results, f, indent=2)
    print(f"\n[STRESS] Results saved to {output_path}")

    # Summary table
    print("\n" + "=" * 70)
    print(f"{'Clients':<10} {'Connected':<12} {'Ticks':<10} "
          f"{'Snap/Client':<14} {'Sent KB':<10} {'Tick ms':<10}")
    print("-" * 70)
    for r in results:
        print(f"{r['clients']:<10} {r['connected']:<12} "
              f"{r['server_ticks']:<10} "
              f"{r['avg_snapshots_per_client']:<14} "
              f"{r['server_bytes_sent_kb']:<10} "
              f"{r.get('avg_tick_time_ms', 'N/A'):<10}")
    print("=" * 70)


if __name__ == '__main__':
    main()
