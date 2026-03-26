"""Stress test helpers for DTLS-secured multiplayer sessions."""

from __future__ import annotations

import json
import random
import time

from client.client import GameClient


TEST_ROOM_KEY = "stress-test-room-key"


def _pump(server, bots, duration: float):
    deadline = time.perf_counter() + duration
    while time.perf_counter() < deadline:
        for bot in bots:
            bot.receive_packets()
        server.receive_all_packets()
        server.simulate_tick()
        server.send_snapshots()
        server.current_tick += 1
        time.sleep(0.02)


def run_stress_test(
    num_clients: int, duration: float = 10.0, tick_rate: int = 20
) -> dict:
    """Run a stress test with N DTLS clients for a given duration."""
    from server.server import GameServer

    port = random.randint(10000, 60000)
    server = GameServer(
        host="127.0.0.1",
        port=port,
        tick_rate=tick_rate,
        verbose=False,
        room_key=TEST_ROOM_KEY,
    )

    bots = [
        GameClient(
            server_host="127.0.0.1",
            server_port=port,
            headless=True,
            room_key=TEST_ROOM_KEY,
        )
        for _ in range(num_clients)
    ]

    try:
        for bot in bots:
            bot.connect()

        connect_deadline = time.perf_counter() + 5.0
        while time.perf_counter() < connect_deadline:
            _pump(server, bots, 0.1)
            if all(bot.connected for bot in bots):
                break

        connected = sum(1 for bot in bots if bot.connected)
        print(f"  Connected: {connected}/{num_clients}")

        if bots and bots[0].connected:
            bots[0].request_game_start()

        start_deadline = time.perf_counter() + 3.0
        while time.perf_counter() < start_deadline:
            _pump(server, bots, 0.1)
            if all((not bot.connected) or bot.game_started_by_server for bot in bots):
                break

        start = time.perf_counter()
        ticks = 0
        while time.perf_counter() - start < duration:
            for bot in bots:
                if bot.connected and bot.game_started_by_server:
                    bot.send_input(
                        {
                            "move_x": random.uniform(-1.0, 1.0),
                            "move_y": random.uniform(-1.0, 1.0),
                            "actions": 0,
                        }
                    )
            _pump(server, bots, 1.0 / tick_rate)
            ticks += 1

        elapsed = time.perf_counter() - start
        total_snapshots = sum(len(bot.server_snapshots) for bot in bots)
        total_bytes_recv = sum(bot.total_bytes_recv for bot in bots)

        result = {
            "clients": num_clients,
            "connected": connected,
            "duration_s": round(elapsed, 2),
            "server_ticks": server.current_tick,
            "total_snapshots": total_snapshots,
            "avg_snapshots_per_client": round(total_snapshots / max(connected, 1), 1),
            "total_bytes_recv_kb": round(total_bytes_recv / 1024, 1),
            "server_bytes_sent_kb": round(server.total_bytes_sent / 1024, 1),
            "server_bytes_recv_kb": round(server.total_bytes_recv / 1024, 1),
        }

        tick_times = [t["duration_ms"] for t in server.metrics.data.get("tick_times", [])]
        if tick_times:
            result["avg_tick_time_ms"] = round(sum(tick_times) / len(tick_times), 4)
            result["max_tick_time_ms"] = round(max(tick_times), 4)
        return result
    finally:
        for bot in bots:
            try:
                bot.disconnect(close_socket=True)
            except Exception:
                pass
        server.dtls_transport.close()
        try:
            server.sock.close()
        except OSError:
            pass


def main():
    """Run stress tests with increasing client counts."""
    import argparse

    parser = argparse.ArgumentParser(description="Stress test the game server")
    parser.add_argument(
        "--bots", type=int, default=10, help="Number of bot clients to simulate"
    )
    parser.add_argument(
        "--duration", type=float, default=10.0, help="Test duration in seconds"
    )
    parser.add_argument(
        "--tick-rate", type=int, default=20, help="Server tick rate"
    )
    args = parser.parse_args()

    result = run_stress_test(args.bots, args.duration, args.tick_rate)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
