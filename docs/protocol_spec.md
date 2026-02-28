# Protocol Specification

## Overview

The Real-Time Multiplayer Game Networking Engine uses a custom binary protocol
over UDP. All multi-byte values use **network byte order** (big-endian).

## Packet Header (15 bytes)

Every UDP datagram begins with this fixed-size header:

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 4 bytes | Protocol ID | Magic number `0x47414D45` ("GAME" in ASCII) |
| 4 | 2 bytes | Sequence Number | Sender's outgoing packet sequence (wraps at 65535) |
| 6 | 2 bytes | Ack Number | Latest sequence number received from remote |
| 8 | 4 bytes | Ack Bitfield | Bitfield: bit N = received packet (ack - 1 - N) |
| 12 | 1 byte | Packet Type | Identifies the payload format |
| 13 | 2 bytes | Payload Length | Length of the variable payload in bytes |
| 15 | variable | Payload | Type-specific data |

## Packet Types

### 0x01 — CONNECT_REQ (Client → Server)

Request to join the game session.

- **Payload**: Empty (0 bytes)
- **Response**: Server sends CONNECT_ACK

### 0x02 — CONNECT_ACK (Server → Client)

Accept connection and assign a client ID.

| Offset | Size | Field |
|--------|------|-------|
| 0 | 1 byte | Client ID (1–255) |

### 0x03 — DISCONNECT (Either direction)

Graceful disconnection notification.

- **Payload**: Empty (0 bytes)

### 0x04 — INPUT (Client → Server)

Player input for the current tick. Supports redundant encoding.

#### Single Input Format (13 bytes):

| Offset | Size | Field |
|--------|------|-------|
| 0 | 4 bytes | Input Sequence Number (uint32) |
| 4 | 4 bytes | Move X (-1.0 to 1.0, float32) |
| 8 | 4 bytes | Move Y (-1.0 to 1.0, float32) |
| 12 | 1 byte | Actions bitfield (bit0=action1, bit1=action2, ...) |

#### Redundant Input Format (1 + N × 13 bytes):

| Offset | Size | Field |
|--------|------|-------|
| 0 | 1 byte | Input Count (N) |
| 1 | N × 13 | Array of Input entries (oldest first) |

Including redundant recent inputs makes the protocol tolerant to packet loss:
if one packet is dropped, the next packet contains the missing input data.

### 0x05 — SNAPSHOT (Server → Client)

World state snapshot at a specific server tick.

| Offset | Size | Field |
|--------|------|-------|
| 0 | 4 bytes | Server Tick (uint32) |
| 4 | 1 byte | Entity Count (N) |
| 5 | N × 21 | Array of Entity States |

#### Entity State (21 bytes):

| Offset | Size | Field |
|--------|------|-------|
| 0 | 1 byte | Entity ID (uint8) |
| 1 | 4 bytes | Position X (float32) |
| 5 | 4 bytes | Position Y (float32) |
| 9 | 4 bytes | Velocity X (float32) |
| 13 | 4 bytes | Velocity Y (float32) |
| 17 | 4 bytes | Health (float32) |

### 0x06 — PING (Client → Server)

Latency measurement request.

| Offset | Size | Field |
|--------|------|-------|
| 0 | 8 bytes | Client Timestamp (float64, `time.perf_counter()`) |

### 0x07 — PONG (Server → Client)

Latency measurement response (echoes client's timestamp).

| Offset | Size | Field |
|--------|------|-------|
| 0 | 8 bytes | Echoed Client Timestamp (float64) |

The client computes RTT as `time.perf_counter() - echoed_timestamp`.

### 0x08 — RELIABLE_EVENT (Either direction)

Guaranteed-delivery event for critical game events (e.g., player death, score change).
Uses the sequence/ack mechanism for delivery confirmation with retransmission.

- **Payload**: Application-defined event data

### 0x09 — HEARTBEAT (Either direction)

Keep-alive signal to prevent timeout disconnection.

- **Payload**: Empty (0 bytes)

## Reliability Mechanism

### Sequence Numbers

Each sender maintains a 16-bit sequence counter incremented per packet sent.
The receiver tracks the latest sequence received and a 32-bit ack bitfield.

### Ack Bitfield

The `ack_bitfield` encodes receipt of up to 32 packets prior to the `ack` number:
- Bit 0: received packet `ack - 1`
- Bit 1: received packet `ack - 2`
- ...
- Bit 31: received packet `ack - 32`

This piggybacked acknowledgement allows both sides to detect packet loss
without dedicated ACK packets.

### Unreliable vs Reliable Data

| Data Type | Strategy |
|-----------|----------|
| Snapshots | Unreliable — newer snapshots supersede lost ones |
| Player Inputs | Unreliable with redundancy — last N inputs sent per packet |
| Critical Events | Reliable — ack tracking with retransmission |
| Ping/Pong | Unreliable — individual samples can be lost |

## Bandwidth Estimates

| Scenario | Snapshot Size | Packets/sec | Bandwidth per client |
|----------|--------------|-------------|---------------------|
| 4 players, 20 Hz | 15 + 5 + 4×21 = 104 bytes | 20 | ~2.1 KB/s |
| 8 players, 20 Hz | 15 + 5 + 8×21 = 188 bytes | 20 | ~3.8 KB/s |
| 8 players, 60 Hz | 188 bytes | 60 | ~11.3 KB/s |
| 16 players, 20 Hz | 15 + 5 + 16×21 = 356 bytes | 20 | ~7.1 KB/s |
