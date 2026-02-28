"""
Minimal game renderer using pygame.
Draws players as colored circles on a 2D arena with a HUD.
"""

import sys

try:
    import pygame
    PYGAME_AVAILABLE = True
except ImportError:
    PYGAME_AVAILABLE = False

from common.config import WORLD_WIDTH, WORLD_HEIGHT, PLAYER_RADIUS


# Player colors
COLORS = [
    (255, 80, 80),    # Red
    (80, 80, 255),    # Blue
    (80, 255, 80),    # Green
    (255, 255, 80),   # Yellow
    (255, 80, 255),   # Magenta
    (80, 255, 255),   # Cyan
    (255, 160, 80),   # Orange
    (160, 80, 255),   # Purple
]


class GameRenderer:
    """Pygame-based renderer for the game demo."""

    def __init__(self, width: int = WORLD_WIDTH, height: int = WORLD_HEIGHT):
        if not PYGAME_AVAILABLE:
            print("[RENDERER] pygame not available — running headless")
            self.headless = True
            return

        self.headless = False
        pygame.init()
        self.width = width
        self.height = height
        self.screen = pygame.display.set_mode((width, height))
        pygame.display.set_caption("Multiplayer Networking Engine Demo")
        self.font = pygame.font.SysFont('monospace', 14)
        self.big_font = pygame.font.SysFont('monospace', 20, bold=True)
        self.clock = pygame.time.Clock()

    def render(self, local_state: dict, remote_states: dict,
               local_id: int, metrics: dict):
        """Render one frame."""
        if self.headless:
            return

        self.screen.fill((30, 30, 30))

        # Draw grid
        for x in range(0, self.width, 50):
            pygame.draw.line(self.screen, (45, 45, 45), (x, 0), (x, self.height))
        for y in range(0, self.height, 50):
            pygame.draw.line(self.screen, (45, 45, 45), (0, y), (self.width, y))

        # Draw world border
        pygame.draw.rect(self.screen, (80, 80, 80),
                         (0, 0, self.width, self.height), 2)

        # Draw remote entities
        for eid, state in remote_states.items():
            color = COLORS[eid % len(COLORS)]
            pos = (int(state['x']), int(state['y']))
            # Shadow
            pygame.draw.circle(self.screen, (20, 20, 20),
                               (pos[0] + 2, pos[1] + 2), PLAYER_RADIUS)
            pygame.draw.circle(self.screen, color, pos, PLAYER_RADIUS)
            # Label
            label = self.font.render(f"P{eid}", True, (200, 200, 200))
            self.screen.blit(label, (pos[0] - 8, pos[1] - PLAYER_RADIUS - 16))

        # Draw local entity (with white outline)
        if local_state and 'x' in local_state:
            color = COLORS[local_id % len(COLORS)]
            pos = (int(local_state['x']), int(local_state['y']))
            # White outline
            pygame.draw.circle(self.screen, (255, 255, 255), pos,
                               PLAYER_RADIUS + 2)
            pygame.draw.circle(self.screen, color, pos, PLAYER_RADIUS)
            # Label
            label = self.font.render(f"YOU (P{local_id})", True,
                                     (255, 255, 255))
            self.screen.blit(label, (pos[0] - 30, pos[1] - PLAYER_RADIUS - 20))

        # Draw HUD
        self._draw_hud(metrics)

        pygame.display.flip()
        self.clock.tick(60)

    def _draw_hud(self, metrics: dict):
        """Draw heads-up display with network metrics."""
        # Background panel
        panel_h = 20 + len(metrics) * 18
        panel = pygame.Surface((200, panel_h))
        panel.set_alpha(180)
        panel.fill((0, 0, 0))
        self.screen.blit(panel, (5, 5))

        y = 10
        title = self.font.render("Network Stats", True, (150, 255, 150))
        self.screen.blit(title, (10, y))
        y += 20

        for key, val in metrics.items():
            text = self.font.render(f"{key}: {val}", True, (200, 200, 200))
            self.screen.blit(text, (10, y))
            y += 18

    def get_input(self) -> dict:
        """Read keyboard input and return as normalized movement."""
        if self.headless:
            return {'move_x': 0.0, 'move_y': 0.0, 'actions': 0}

        keys = pygame.key.get_pressed()
        mx = (1.0 if keys[pygame.K_d] or keys[pygame.K_RIGHT] else 0.0) - \
             (1.0 if keys[pygame.K_a] or keys[pygame.K_LEFT] else 0.0)
        my = (1.0 if keys[pygame.K_s] or keys[pygame.K_DOWN] else 0.0) - \
             (1.0 if keys[pygame.K_w] or keys[pygame.K_UP] else 0.0)

        actions = 0
        if keys[pygame.K_SPACE]:
            actions |= 0x01  # Shoot/action

        return {'move_x': mx, 'move_y': my, 'actions': actions}

    def check_quit(self) -> bool:
        """Check if user wants to quit."""
        if self.headless:
            return False

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return True
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                return True
        return False

    def close(self):
        if not self.headless:
            pygame.quit()
