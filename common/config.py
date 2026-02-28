"""
Game constants and configuration.
"""

# World bounds
WORLD_WIDTH = 800
WORLD_HEIGHT = 600

# Physics
PLAYER_SPEED = 200.0       # units per second
PLAYER_RADIUS = 15         # pixels for rendering

# Network defaults
DEFAULT_HOST = '0.0.0.0'
DEFAULT_PORT = 9000
DEFAULT_TICK_RATE = 20      # Server ticks per second
DEFAULT_BUFFER_SIZE = 4096

# Timeouts
CLIENT_TIMEOUT = 10.0       # Seconds before disconnecting idle client
CONNECT_RETRY_INTERVAL = 1.0
PING_INTERVAL = 1.0         # Seconds between pings

# Interpolation
INTERPOLATION_TICKS = 2     # Render N ticks behind server time

# Input redundancy
INPUT_REDUNDANCY = 3         # Send last N inputs in each packet

# Reliable channel
RELIABLE_MAX_RETRIES = 5
RELIABLE_RETRY_INTERVAL = 0.2
