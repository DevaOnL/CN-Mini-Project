# Protocol Specification

## Overview

`multiplayer-engine` uses a custom UDP protocol with a fixed 15-byte header,
authoritative server snapshots, redundant input delivery, piggybacked acks,
mandatory room-key-secured payload encryption, and reliable
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
| `0x01` | `CONNECT_REQ` | Client -> Server | `!16sI` (`session_token`, `connect_nonce`) |
| `0x02` | `CONNECT_ACK` | Server -> Client | `!HI16sI` (`client_id`, `connection_epoch`, `session_token`, `connect_nonce`) |
| `0x03` | `DISCONNECT` | Either | Disconnect reason or handshake-cancel payload |
| `0x04` | `INPUT` | Client -> Server | `!I` epoch + `!B` count + `N * !IffB` inputs |
| `0x05` | `SNAPSHOT` | Server -> Client | `!I` epoch + snapshot body + trailer |
| `0x06` | `PING` | Client -> Server | `!I` epoch + `!d` client timestamp |
| `0x07` | `PONG` | Server -> Client | `!I` epoch + `!d` echoed client timestamp |
| `0x08` | `RELIABLE_EVENT` | Either | `!I` epoch + event-specific payload |
| `0x09` | `HEARTBEAT` | Either | `!I` epoch |
| `0x0A` | `SECURE_HELLO` | Client -> Server | `!B16s32s` (`version`, `client_nonce`, `client_proof`) |
| `0x0B` | `SECURE_HELLO_ACK` | Server -> Client | `!B16s32s` (`version`, `server_nonce`, `server_proof`) |

## Secure Handshake

The transport is secured at the application layer rather than with TCP/TLS or
DTLS.

1. Both peers derive a 32-byte PSK from the room key using `Scrypt`.
2. Client sends `SECURE_HELLO`:
   - `version(u8)`
   - `client_nonce(16)`
   - `client_proof(32) = HMAC-SHA256(psk, b"client" + client_nonce)`
3. Server verifies the proof, generates `server_nonce(16)`, and replies with
   `SECURE_HELLO_ACK`:
   - `version(u8)`
   - `server_nonce(16)`
   - `server_proof(32) = HMAC-SHA256(psk, b"server" + client_nonce + server_nonce)`
4. Both sides derive the per-session packet key with:

```text
HKDF-SHA256(
  psk,
  salt = client_nonce + server_nonce,
  info = b"multiplayer-engine-secure-v1"
)
```

The server keeps pending handshake state for 5 seconds, keyed by source
address, until the encrypted `CONNECT_REQ` arrives.

## Encrypted Payload Format

After `SECURE_HELLO_ACK`, the following packet types must be encrypted:

- `CONNECT_REQ`
- `CONNECT_ACK`
- `DISCONNECT`
- `INPUT`
- `SNAPSHOT`
- `PING`
- `PONG`
- `RELIABLE_EVENT`
- `HEARTBEAT`

Only the 15-byte packet header stays plaintext. The payload is encoded as:

```text
nonce(12) + ciphertext_and_tag
```

Encryption uses `ChaCha20Poly1305`, and the serialized header bytes are passed
as AEAD additional authenticated data.

## Connection Freshness

All non-handshake gameplay packets are wrapped with a 32-bit `connection_epoch`.
The server rotates this epoch on successful connect/reconnect so stale packets from
older sessions can be rejected even if UDP reorders them.

`CONNECT_REQ` carries a 32-bit `connect_nonce` so stale reconnect attempts and
cancel/disconnect messages can be matched to the current handshake attempt.
Reconnects still use the session token model, but every reconnect must first
complete a fresh secure hello exchange and derive a fresh packet key.

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

1. Client sends `SECURE_HELLO`.
2. Server replies with `SECURE_HELLO_ACK`.
3. Client sends encrypted `CONNECT_REQ` with a zero-padded token (or zeros for
   first join) plus a fresh `connect_nonce`.
4. Server replies with encrypted `CONNECT_ACK` containing `client_id`,
   `connection_epoch`, the 16-byte token, and the echoed `connect_nonce`.
5. Client stores the token locally.
6. If the server goes silent, the client reconnects using the stored token and a
   newer `connect_nonce`, but still performs a fresh secure hello first.
7. Server matches the token to the prior session, validates the nonce freshness,
   rotates the `connection_epoch`, rebinds the new `(ip, port)`, and reuses the
   same player identity while rejecting stale packets from the old session.

`DISCONNECT` also has handshake-time variants used before an epoch is fully
established:

- `!16sIB` (`session_token`, `connect_nonce`, `reason_code`) for handshake-time
  rejection / cancellation
- legacy token+nonce `!16sI` disconnect payloads are still accepted for stale
  reconnect cancellation cleanup

Disconnect reasons now include:

- `0x00` - `NONE`
- `0x01` - `KICKED`
- `0x02` - `SECURE_REQUIRED`
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
