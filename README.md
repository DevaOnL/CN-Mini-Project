# Multiplayer Engine

## Project Overview

`multiplayer-engine` is a Python 3.10+ real-time multiplayer arena demo built on
raw UDP sockets and `pygame-ce`. It demonstrates an authoritative server,
client-side prediction, reconciliation, interpolation, reconnectable session
tokens, a DTLS-secured UDP transport layer, a scene-based GUI,
reliable gameplay events, and lightweight network analysis tooling.

Tech stack:

- Python 3.10+
- Raw UDP sockets with a custom binary protocol
- DTLS over UDP (`pyOpenSSL`) with self-signed host certificates
- TOFU host fingerprint pinning plus app-level room-key authorization
- `pygame-ce` for the client GUI and renderer
- `matplotlib` + `numpy` for metrics analysis

## Architecture

**Protocol layer** - `common/packet.py`, `common/net.py`, `common/dtls.py`, `common/snapshot.py`, and `common/config.py` define the 15-byte gameplay packet header, DTLS transport/session handling, certificate generation, trusted-host pinning, ack/bitfield logic, snapshot encoding, reconnect tokens, and shared runtime constants. Reliable events ride on top of normal UDP packets using retransmission and duplicate suppression.

**Server loop** - `server/server.py`, `server/game_state.py`, `server/client_manager.py`, and `server/session_manager.py` implement the authoritative fixed-tick simulation. The server owns movement, dash cooldowns, collision damage, respawns, scoring, match win detection, reconnect/session mapping, and snapshot broadcast cadence.

**Client loop** - `client/client.py`, `client/prediction.py`, `client/reconciliation.py`, and `client/interpolation.py` resolve hostnames, connect over UDP, predict the local player, reconcile against authoritative snapshots, interpolate remote entities, process reliable match events, and keep gameplay state synchronized with the server.

**GUI scene graph** - `client/gui/scene_manager.py` drives the scene stack. `client/gui/scenes/main_menu.py`, `client/gui/scenes/join_dialog.py`, `client/gui/scenes/lobby.py`, `client/gui/scenes/game_hud.py`, `client/gui/scenes/match_over.py`, and `client/gui/scenes/settings.py` provide the full host/join/lobby/in-game/match-over/settings flow.

## Quick Start

```bash
git clone https://github.com/DevaOnL/multiplayer-engine.git
cd multiplayer-engine
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

Run the server:

```bash
multiplayer-engine-server --room-key "shared secret"
```

Run the client:

```bash
multiplayer-engine-client
```

Headless host + join from two terminals:

```bash
# Terminal 1
multiplayer-engine-server --port 9000 --room-key "shared secret"

# Terminal 2
multiplayer-engine-client --headless --host 127.0.0.1 --port 9000 --room-key "shared secret"
```

You can also host directly from the GUI main menu. GUI host and join now both
require a room key, and GUI-hosted servers bind to `0.0.0.0` so a second
device on the same LAN can join with the shown LAN IPv4 address, shared room
key, and DTLS host fingerprint. The first successful join stores the host
fingerprint locally; later certificate changes are rejected until the trusted
host entry is cleared from Settings.

## Gameplay

- `WASD` / arrow keys - move
- `SPACE` - dash (1 second cooldown)
- Contact with another player deals damage over time
- Dead players respawn after 3 seconds
- First player to 5 kills wins, or highest score wins when the match timer ends
- `TAB` - toggle scoreboard overlay
- `ESC` - pause menu

## Configuration

Core runtime settings live in `common/config.py`.

| Constant | Default | Description |
|----------|---------|-------------|
| `WORLD_WIDTH` | `800` | Arena width |
| `WORLD_HEIGHT` | `600` | Arena height |
| `PLAYER_SPEED` | `200.0` | Base movement speed |
| `PLAYER_RADIUS` | `15` | Player collision/render radius |
| `DASH_SPEED_MULTIPLIER` | `3.5` | Dash speed multiplier |
| `DASH_DURATION` | `0.12` | Dash duration in seconds |
| `DASH_COOLDOWN` | `1.0` | Dash cooldown in seconds |
| `CONTACT_DAMAGE_PER_SEC` | `20.0` | Collision damage per second |
| `RESPAWN_HEALTH` | `100.0` | Health after respawn |
| `RESPAWN_DELAY_TICKS` | `60` | Respawn delay at 20 Hz |
| `KILLS_TO_WIN` | `5` | Score target to win |
| `MATCH_DURATION_SECS` | `120.0` | Match time limit (`0` disables) |
| `MATCH_OVER_DISPLAY_SECS` | `5.0` | Results-screen duration |
| `DEFAULT_HOST` | `0.0.0.0` | Default server bind address |
| `DEFAULT_PORT` | `9000` | Default server port |
| `DEFAULT_TICK_RATE` | `20` | Server/client simulation tick rate |
| `DEFAULT_BUFFER_SIZE` | `4096` | UDP recv buffer size |
| `CLIENT_TIMEOUT` | `10.0` | Session timeout threshold |
| `CONNECT_RETRY_INTERVAL` | `1.0` | Reconnect retry interval |
| `PING_INTERVAL` | `1.0` | Ping interval |
| `INTERPOLATION_TICKS` | `2` | Remote interpolation delay |
| `INPUT_REDUNDANCY` | `3` | Inputs bundled per packet |
| `RELIABLE_MAX_RETRIES` | `5` | Reliable resend cap |
| `RELIABLE_RETRY_INTERVAL` | `0.2` | Reliable resend interval |

The GUI settings screen persists host, port, player name, FPS target,
interpolation buffer, and HUD debug visibility in
`~/.multiplayer_engine_config.json`. The room key is never persisted there and
stays in memory only for the current process/session.

## Protocol Reference

### Packet Types

| Byte | Name | Direction | Payload Summary |
|------|------|-----------|-----------------|
| `0x01` | `CONNECT_REQ` | Client -> Server | `!16sIB + room_key_utf8` (`session_token`, `connect_nonce`, `room_key_len`, `room_key`) |
| `0x02` | `CONNECT_ACK` | Server -> Client | `!HI16sI` (`client_id`, `connection_epoch`, `session_token`, `connect_nonce`) |
| `0x03` | `DISCONNECT` | Either | `!B` reason, epoch-wrapped once a connection is established |
| `0x04` | `INPUT` | Client -> Server | `!I` epoch + `!B` count + `N * !IffB` inputs |
| `0x05` | `SNAPSHOT` | Server -> Client | `!I` epoch + `!IH + N * !HfffffH + !Idf` |
| `0x06` | `PING` | Client -> Server | `!I` epoch + `!d` client timestamp |
| `0x07` | `PONG` | Server -> Client | `!I` epoch + `!d` echoed timestamp |
| `0x08` | `RELIABLE_EVENT` | Either | `!I` epoch + event-specific reliable payload |
| `0x09` | `HEARTBEAT` | Either | `!I` epoch |

### Transport Security

All gameplay packets travel inside DTLS datagrams. The custom 15-byte header is
still part of the gameplay packet format, but it is now protected by DTLS
instead of the old app-layer secure-hello flow.

Server security defaults:

- the host auto-generates a self-signed ECDSA certificate the first time it runs
- the cert/key are stored under `~/.multiplayer_engine/certs/`
- the lobby shows the SHA-256 fingerprint so players can verify the host

Client trust model:

- first successful connection silently trusts and stores `host:port -> fingerprint`
- later fingerprint changes are rejected before `CONNECT_REQ`
- Settings includes a `Clear Trusted Hosts` action for local recovery

### Reliable Event Types

| Byte | Name | Direction | Payload |
|------|------|-----------|---------|
| `0x01` | `JOIN` | Server -> Client | `!BH` |
| `0x02` | `LEAVE` | Server -> Client | `!BH` |
| `0x03` | `GAME_START` | Client -> Server request, Server -> Client broadcast | `!BH` |
| `0x04` | `SCORE_UPDATE` | Server -> Client | `!BHH` |
| `0x05` | `SCORE_SYNC` | Server -> Client | `!B` + repeated `!HH` pairs |
| `0x06` | `MATCH_OVER` | Server -> Client | `!BH` |
| `0x07` | `MATCH_RESET` | Server -> Client | `!B` |
| `0x08` | `KICK_PLAYER` | Client -> Server request | `!BH` |
| `0x09` | `KICKED` | Server -> Client | `!BH` |

For the full wire format, see `docs/protocol_spec.md`.

## Network Simulation

Both server and client support synthetic bad-network testing:

```bash
multiplayer-engine-server --loss 0.10 --latency 0.05
multiplayer-engine-client --host 127.0.0.1 --port 9000 --loss 0.02 --latency 0.02
```

- `--loss` is packet drop probability (`0.0` to `1.0`)
- `--latency` is base one-way delay in seconds

## Analysis

Metrics JSON files are written under `analysis/logs/`.

```bash
multiplayer-engine-analyze analysis/logs/server_metrics.json
```

Generated plots:

- `analysis/network_analysis.png`
- `analysis/prediction_error_analysis.png`
- `analysis/bandwidth_analysis.png`
- `analysis/tick_time_analysis.png`

## Running Tests

```bash
pytest tests -q
python tests/stress_test.py
```

The automated suite covers packet encoding, snapshots, prediction,
reconciliation, GUI scene transitions, DTLS certificate/pinning behavior,
reconnect behavior, authoritative game state, integration behavior, and
duplicate reliable-event suppression.

## Known Limitations

- Hostname validation accepts IPv6-like input, but the runtime resolver currently uses `AF_INET`, so true IPv6 transport is not supported yet.
- Server-side per-player RTT is inferred from client ping timestamps and is good for UI display, but it is not a perfect per-peer latency model.
- Score history for players who leave a match is intentionally dropped with their entity; persistent player identity across multiple matches is out of scope.

## License

MIT. See `LICENSE`.
