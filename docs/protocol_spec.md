# Protocol Specification

## Overview

`multiplayer-engine` uses a custom UDP protocol with a fixed 15-byte header,
authoritative server snapshots, redundant input delivery, piggybacked acks,
DTLS transport security, app-level room-key authorization, and reliable
gameplay/lifecycle events layered on top of raw UDP.

All multi-byte fields use network byte order (big-endian).

## Datagram Header (15 bytes)

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| 0 | 4 bytes | Protocol ID | `0x47414D45` (`"GAME"`) |
| 4 | 2 bytes | Sequence | Sender packet sequence (`u16`) |
| 6 | 2 bytes | Ack | Latest remote sequence seen (`u16`) |
| 8 | 4 bytes | Ack Bitfield | Bit `n` acks `ack - 1 - n` |
| 12 | 1 byte | Packet Type | `PacketType` code |
| 13 | 2 bytes | Payload Length | Variable payload size |

## Packet Types

| Value | Name | Direction | Payload |
|-------|------|-----------|---------|
| `0x01` | `CONNECT_REQ` | Client -> Server | `!16sIB + room_key_utf8` (`session_token`, `connect_nonce`, `room_key_len`, `room_key`) |
| `0x02` | `CONNECT_ACK` | Server -> Client | `!HI16sI` (`client_id`, `connection_epoch`, `session_token`, `connect_nonce`) |
| `0x03` | `DISCONNECT` | Either | Disconnect reason (`!B`), epoch-wrapped after connect |
| `0x04` | `INPUT` | Client -> Server | `!I` epoch + `!B` count + `N * !IffB` inputs |
| `0x05` | `SNAPSHOT` | Server -> Client | `!I` epoch + snapshot body + trailer |
| `0x06` | `PING` | Client -> Server | `!I` epoch + `!d` client timestamp |
| `0x07` | `PONG` | Server -> Client | `!I` epoch + `!d` echoed client timestamp |
| `0x08` | `RELIABLE_EVENT` | Either | `!I` epoch + event-specific payload |
| `0x09` | `HEARTBEAT` | Either | `!I` epoch |

## DTLS Transport

The transport is secured with DTLS over the existing UDP sockets. The custom
15-byte gameplay packet header and payload format stay the same above DTLS, but
the old app-layer `SECURE_HELLO` / `SECURE_HELLO_ACK` handshake is gone.

### Host Identity

- The host auto-generates a self-signed ECDSA P-256 certificate on first run.
- Default paths:
  - `~/.multiplayer_engine/certs/server_cert.pem`
  - `~/.multiplayer_engine/certs/server_key.pem`
- The lobby shows the host SHA-256 certificate fingerprint.

### Client Trust Model

- Clients use TOFU pinning keyed by `host:port`.
- First successful connection stores the observed fingerprint locally.
- Later fingerprint changes abort the join before `CONNECT_REQ`.
- Trusted pins are stored in `~/.multiplayer_engine_known_hosts.json`.

### Authorization Flow

1. Client resolves the server address and completes a DTLS handshake.
2. Client validates or stores the host fingerprint.
3. Client sends `CONNECT_REQ` inside DTLS with:
   - `session_token(16 bytes, ASCII, zero padded)`
   - `connect_nonce(u32)`
   - `room_key_len(u8)`
   - `room_key_utf8(room_key_len bytes)`
4. Server compares the room key to its configured key.
5. Server replies with `CONNECT_ACK` or `DISCONNECT(AUTH_FAILED)`.

All gameplay packets (`CONNECT_REQ`, `CONNECT_ACK`, `DISCONNECT`, `INPUT`,
`SNAPSHOT`, `PING`, `PONG`, `RELIABLE_EVENT`, and `HEARTBEAT`) are protected by
DTLS once the handshake completes.

## Connection Freshness

All gameplay packets after `CONNECT_ACK` are wrapped with a 32-bit
`connection_epoch`. The server rotates this epoch on successful connect or
reconnect so stale packets from older sessions can be rejected even if UDP
reorders them.

`CONNECT_REQ` carries a 32-bit `connect_nonce` so stale reconnect attempts can
be rejected. Reconnects still use the session-token model, but every reconnect
must complete a fresh DTLS handshake before sending the new `CONNECT_REQ`.

## Input Payload

### Single Input Entry (`!IffB`, 13 bytes)

| Field | Type | Description |
|-------|------|-------------|
| `input_seq` | `u32` | Monotonic client input sequence |
| `move_x` | `f32` | Horizontal axis |
| `move_y` | `f32` | Vertical axis |
| `actions` | `u8` | Bitfield (`bit0 = dash`) |

### Redundant Input Packet

`INPUT` payloads are encoded as:

```text
!I + !B + N * !IffB
```

The leading `u32` is the `connection_epoch`, followed by a `u8` input count.
Entries are ordered oldest to newest so dropped packets can be recovered by
later redundant bundles.

## Snapshot Format

### Header (`SNAPSHOT_HEADER_FORMAT = !IH`)

| Field | Type | Description |
|-------|------|-------------|
| `tick` | `u32` | Authoritative server tick |
| `entity_count` | `u16` | Number of serialized entities |

### Entity Layout (`ENTITY_STATE_FORMAT = !H f f f f f H`)

Each entity consumes 24 bytes:

| Offset | Size | Field | Type |
|--------|------|-------|------|
| 0 | 2 bytes | `entity_id` | `u16` |
| 2 | 4 bytes | `x` | `f32` |
| 6 | 4 bytes | `y` | `f32` |
| 10 | 4 bytes | `vx` | `f32` |
| 14 | 4 bytes | `vy` | `f32` |
| 18 | 4 bytes | `health` | `f32` |
| 22 | 2 bytes | `ping_ms` | `u16` |

`dash_cooldown` and `dash_timer` exist on server/local predicted state, but are
not serialized in snapshots.

### Trailer (`SNAPSHOT_TRAILER_FORMAT = !Idf`)

Snapshot payloads end with a 16-byte trailer:

| Field | Type | Description |
|-------|------|-------------|
| `last_processed_input_seq` | `u32` | Last authoritative client input applied for that recipient |
| `server_send_time` | `f64` | `time.perf_counter()` at snapshot send |
| `match_elapsed` | `f32` | Match clock in seconds |

Full payload layout:

```text
!I + !IH + N * !HfffffH + !Idf
```

## Reliable Event Channel

Reliable events are still sent inside normal `RELIABLE_EVENT` packets. Delivery
uses the existing packet sequence/ack system plus retransmission on both sides.
Each reliable-event payload is prefixed by the current `connection_epoch`.

| Value | Name | Direction | Payload |
|-------|------|-----------|---------|
| `0x01` | `JOIN` | Server -> Client | `!BH` (`type`, `client_id`) |
| `0x02` | `LEAVE` | Server -> Client | `!BH` (`type`, `client_id`) |
| `0x03` | `GAME_START` | Client -> Server request, Server -> Client broadcast | `!BH` (`type`, `client_id` or `0`) |
| `0x04` | `SCORE_UPDATE` | Server -> Client | `!BHH` (`type`, `killer_id`, `victim_id`) |
| `0x05` | `SCORE_SYNC` | Server -> Client | `!B` + repeated `!HH` pairs (`entity_id`, `kills`) |
| `0x06` | `MATCH_OVER` | Server -> Client | `!BH` (`type`, `winner_id`) |
| `0x07` | `MATCH_RESET` | Server -> Client | `!B` |
| `0x08` | `KICK_PLAYER` | Client -> Server request | `!BH` (`type`, `target_client_id`) |
| `0x09` | `KICKED` | Server -> Client | `!BH` (`type`, `host_client_id`) |

## Session Token Reconnect Flow

1. Client completes a fresh DTLS handshake.
2. Client sends `CONNECT_REQ` with the stored token and a newer `connect_nonce`.
3. Server matches the token to the prior session, validates nonce freshness,
   rotates the `connection_epoch`, rebinds the new `(ip, port)`, and reuses the
   same player identity while rejecting stale packets from the old session.
4. `CONNECT_ACK` returns the same client identity plus the new epoch.

Disconnect reasons now include:

- `0x00` - `NONE`
- `0x01` - `KICKED`
- `0x03` - `AUTH_FAILED`

## Match Lifecycle

Runtime phases are:

```text
LOBBY -> IN_GAME -> MATCH_OVER -> LOBBY
```

- Lobby snapshots are sent at a reduced rate.
- `GAME_START` moves connected clients into the live match.
- `MATCH_OVER` announces the winner.
- `MATCH_RESET` clears scores/state and returns everyone to the lobby.

## Reliability Notes

- Snapshots are unordered/unreliable; newer snapshots supersede older ones.
- Client inputs are unreliable but redundant.
- `RELIABLE_EVENT` payloads use retransmission and duplicate suppression.
- Heartbeats keep sessions alive and are echoed by the receiver.

## Bandwidth Notes

At 20 Hz, each per-client snapshot is:

```text
15-byte header + (!IH + N * 24-byte entities + 16-byte trailer)
```

The exact payload size scales linearly with entity count.
