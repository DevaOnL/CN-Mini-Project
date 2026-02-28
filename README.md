# Real-Time Multiplayer Game Networking Engine

A **from-scratch** implementation of a real-time multiplayer game networking engine built with Python and UDP sockets. Designed as a Computer Networks course project, this engine demonstrates all the core techniques used by professional game engines — authoritative servers, client-side prediction, server reconciliation, entity interpolation, and a custom binary protocol.

> **Course:** Computer Networks (CN)  
> **Project:** Sl. No. 5 — Real-Time Multiplayer Game Networking Engine

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Quick Start](#quick-start)
- [Usage](#usage)
  - [Running the Server](#running-the-server)
  - [Running a Client](#running-a-client)
  - [Command-Line Options](#command-line-options)
- [Controls](#controls)
- [Testing](#testing)
- [Network Analysis](#network-analysis)
- [Protocol Specification](#protocol-specification)
- [Stress Test Results](#stress-test-results)
- [Technical Deep Dive](#technical-deep-dive)
- [License](#license)

---

## Features

| Category | Details |
|----------|---------|
| **Custom Binary Protocol** | 15-byte header with protocol ID, sequence numbers, piggybacked acks, and 9 packet types |
| **Authoritative Server** | Fixed tick-rate simulation (default 20 Hz), server owns all game state |
| **Client-Side Prediction** | Inputs applied instantly for zero-latency feel |
| **Server Reconciliation** | Server corrections replayed with unacked inputs to avoid visual snapping |
| **Entity Interpolation** | Remote players rendered 2 ticks behind for smooth motion |
| **Packet Loss Resilience** | Redundant inputs (last 3 per packet), piggybacked ack bitfield |
| **Network Simulation** | Built-in configurable packet loss and latency injection |
| **Performance Metrics** | RTT, jitter (RFC 3550), packet loss, bandwidth, tick times — all logged to JSON |
| **Analysis Tooling** | Matplotlib-based visualization with 4-panel latency analysis, CDF plots, bandwidth charts |
| **Stress Testing** | Automated load tests with 2–16 concurrent bot clients |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        GAME CLIENT                              │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────┐  ┌────────┐  │
│  │  Input    ├──►  Prediction  ├──►  Reconciler   │  │Renderer│  │
│  │  Handler  │  │  (local sim) │  │  (on snapshot)│  │(pygame)│  │
│  └──────────┘  └──────────────┘  └───────┬───────┘  └───▲────┘  │
│                                          │              │       │
│                    ┌─────────────────────┐│              │       │
│                    │  Interpolator       ││              │       │
│                    │  (remote entities)  ├┘──────────────┘       │
│                    └─────────────────────┘                       │
└──────────────────────────┬───────────────────────────────────────┘
                           │ UDP (Custom Binary Protocol)
                           │
┌──────────────────────────▼───────────────────────────────────────┐
│                      GAME SERVER (Authoritative)                │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────────────┐   │
│  │  Input Queue  ├──►  Physics /   ├──►  Snapshot Broadcast  │   │
│  │  (per-client) │  │  Game State  │  │  (to all clients)   │   │
│  └──────────────┘  └──────────────┘  └─────────────────────┘   │
│  ┌──────────────┐  ┌──────────────┐                             │
│  │  Client Mgr  │  │  Metrics     │                             │
│  │  (timeouts)  │  │  Logger      │                             │
│  └──────────────┘  └──────────────┘                             │
└──────────────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
multiplayer-engine/
├── common/                     # Shared networking library
│   ├── __init__.py             #   Package exports
│   ├── config.py               #   Game constants & network defaults
│   ├── packet.py               #   Binary protocol — serialize/deserialize
│   ├── net.py                  #   Socket helpers, AckTracker, NetworkSimulator
│   ├── snapshot.py             #   Game state snapshot format
│   └── metrics_logger.py       #   RTT/jitter/loss/bandwidth logging
│
├── server/                     # Authoritative game server
│   ├── __init__.py
│   ├── game_state.py           #   Physics simulation & world state
│   ├── client_manager.py       #   Connected client tracking & timeouts
│   └── server.py               #   Main loop — receive, simulate, broadcast
│
├── client/                     # Game client
│   ├── __init__.py
│   ├── prediction.py           #   Client-side prediction (matches server physics)
│   ├── reconciliation.py       #   Server reconciliation & visual smoothing
│   ├── interpolation.py        #   Entity interpolation for remote players
│   ├── renderer.py             #   Pygame 2D visualization + HUD
│   └── client.py               #   Main loop — input, predict, send, render
│
├── analysis/                   # Performance analysis
│   ├── __init__.py
│   ├── plot_results.py         #   Matplotlib charts from metrics JSON
│   └── logs/                   #   Auto-generated metrics data
│
├── tests/                      # Test suite
│   ├── __init__.py
│   ├── test_packet.py          #   9 unit tests — packet protocol
│   ├── test_prediction.py      #   13 unit tests — prediction & reconciliation
│   ├── test_snapshot.py        #   9 unit tests — snapshots & game state
│   ├── test_integration.py     #   3 integration tests — full server+client
│   └── stress_test.py          #   Load test — 2/4/8/16 concurrent clients
│
├── docs/
│   └── protocol_spec.md        #   Full binary protocol specification
│
├── pyproject.toml              #   Project metadata & dependencies
├── requirements.txt            #   Pip requirements
├── .gitignore
├── LICENSE                     #   MIT License
├── README.md                   #   ← You are here
├── RUNNING.md                  #   Step-by-step run & reproduce guide
└── GITHUB_PUSH.md              #   How to push to GitHub
```

---

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/<your-username>/multiplayer-engine.git
cd multiplayer-engine

# 2. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
pip install pytest   # for testing

# 4. Install the project in editable mode (fixes all imports)
pip install -e .

# 5. Start the server (Terminal 1)
python -m server.server

# 6. Start a client (Terminal 2)
python -m client.client
```

---

## Usage

### Running the Server

```bash
python -m server.server [OPTIONS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `0.0.0.0` | Bind address |
| `--port` | `9000` | Bind port |
| `--tick-rate` | `20` | Simulation ticks per second (Hz) |
| `--loss` | `0.0` | Simulated packet loss rate (0.0–1.0) |
| `--latency` | `0.0` | Simulated base latency in seconds |

### Running a Client

```bash
python -m client.client [OPTIONS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `127.0.0.1` | Server address |
| `--port` | `9000` | Server port |
| `--tick-rate` | `20` | Client tick rate (Hz) |
| `--headless` | `false` | Run without pygame window (for bots) |
| `--loss` | `0.0` | Client-side simulated packet loss |
| `--latency` | `0.0` | Client-side simulated latency (s) |

### Command-Line Options

**Example: Test with simulated bad network conditions**
```bash
# Server with 10% loss and 50ms latency
python -m server.server --loss 0.1 --latency 0.05

# Client connecting to it
python -m client.client --loss 0.05 --latency 0.03
```

---

## Controls

| Key | Action |
|-----|--------|
| `W` / `↑` | Move up |
| `A` / `←` | Move left |
| `S` / `↓` | Move down |
| `D` / `→` | Move right |
| `Space` | Action (reserved) |
| `Escape` | Quit |

---

## Testing

```bash
# Run all unit tests (31 tests)
python -m pytest tests/test_packet.py tests/test_prediction.py tests/test_snapshot.py -v

# Run integration tests (3 tests — spawns real server + bot clients)
python -m pytest tests/test_integration.py -v

# Run ALL tests at once (34 tests)
python -m pytest tests/ -v

# Run stress test (2/4/8/16 concurrent bot clients)
python tests/stress_test.py
```

### Test Coverage

| Test File | Tests | What It Covers |
|-----------|-------|----------------|
| `test_packet.py` | 9 | Binary serialization, protocol ID validation, sequence wrapping, edge cases |
| `test_prediction.py` | 13 | Client-side prediction physics, diagonal normalization, bounds clamping, reconciliation |
| `test_snapshot.py` | 9 | Snapshot round-trip, entity state, game state physics, add/remove entities |
| `test_integration.py` | 3 | Full connection lifecycle, multi-client state convergence, packet loss tolerance |
| `stress_test.py` | 4 scenarios | Performance under 2/4/8/16 concurrent clients |

---

## Network Analysis

After running the server/client, metrics are saved to `analysis/logs/`. Analyze them:

```bash
python analysis/plot_results.py analysis/logs/server_metrics.json
```

This generates:
- **`network_analysis.png`** — RTT over time, jitter, RTT distribution with P50/P95/P99, packet loss rate
- **`prediction_error_analysis.png`** — Prediction error time series + CDF
- **`bandwidth_analysis.png`** — Send/receive bandwidth over time
- **`tick_time_analysis.png`** — Server tick processing time per tick

---

## Protocol Specification

Full specification in [`docs/protocol_spec.md`](docs/protocol_spec.md).

### Header Format (15 bytes)

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                     Protocol ID (0x47414D45)                  |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|       Sequence Number         |        Ack Number             |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                       Ack Bitfield                            |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|  Packet Type  |       Payload Length          |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

### Packet Types

| Code | Name | Direction | Purpose |
|------|------|-----------|---------|
| `0x01` | CONNECT_REQ | Client → Server | Join request |
| `0x02` | CONNECT_ACK | Server → Client | Accept + assign ID |
| `0x03` | DISCONNECT | Either | Graceful leave |
| `0x04` | INPUT | Client → Server | Player input (with redundancy) |
| `0x05` | SNAPSHOT | Server → Client | World state broadcast |
| `0x06` | PING | Client → Server | Latency probe |
| `0x07` | PONG | Server → Client | Latency echo |
| `0x08` | RELIABLE_EVENT | Either | Guaranteed-delivery events |
| `0x09` | HEARTBEAT | Either | Keep-alive signal |

---

## Stress Test Results

Tested on a single machine (localhost) with Python 3.14:

| Clients | Connected | Server Ticks | Snapshots/Client | Server Sent | Avg Tick Time |
|---------|-----------|-------------|------------------|-------------|---------------|
| 2 | 2 | 69 | 62.0 | 7.5 KB | 0.019 ms |
| 4 | 4 | 69 | 62.0 | 25.2 KB | 0.023 ms |
| 8 | 8 | 69 | 62.0 | 91.2 KB | 0.032 ms |
| 16 | 16 | 69 | 62.2 | 345.4 KB | 0.041 ms |

All scenarios maintained sub-0.06 ms tick times with 60+ snapshots delivered per client.

---

## Technical Deep Dive

### Client-Side Prediction
The client applies inputs **locally** before the server confirms them. The prediction physics (`client/prediction.py`) is an exact mirror of the server physics (`server/game_state.py`) — same speed constants, same diagonal normalization, same boundary clamping. This ensures prediction errors are minimal (usually zero under ideal conditions).

### Server Reconciliation
When a snapshot arrives, the client:
1. Reads the server's `last_processed_input_seq` appended to the snapshot
2. Discards all pending inputs the server has already processed
3. Starts from the server's authoritative state
4. Re-applies any remaining unprocessed inputs
5. Smooths the visual state toward the corrected state to avoid jarring snaps

### Entity Interpolation
Remote players are rendered **2 ticks behind** the latest server state. The interpolator finds two bracketing snapshots and performs linear interpolation between them, producing smooth motion even at 20 Hz server tick rate.

### Reliability via Redundancy
Instead of implementing TCP-like reliable delivery (which adds latency), the engine sends the **last 3 inputs** in each packet. If one packet is lost, the next packet still contains the missing data. This provides loss tolerance without adding round-trip latency.

### Piggybacked Acknowledgements
Every packet header carries the sender's latest received remote sequence number plus a 32-bit bitfield encoding receipt of the previous 32 packets. This enables both sides to detect packet loss without dedicated ACK packets.

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
