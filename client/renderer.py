"""
Minimal game renderer using pygame.
Draws players as colored circles on a 2D arena with an optional HUD.
"""

import time as _time
from typing import Any

pygame: Any

try:
    import pygame as _pygame

    pygame = _pygame
    PYGAME_AVAILABLE = True
except ImportError:
    pygame = None
    PYGAME_AVAILABLE = False

if PYGAME_AVAILABLE:
    from client.gui.theme import THEME as GUI_THEME
else:
    GUI_THEME = {
        "text": (236, 242, 248),
        "text_dim": (146, 164, 188),
        "text_accent": (120, 220, 255),
        "text_gold": (255, 218, 118),
        "success": (94, 226, 142),
        "warning": (244, 193, 88),
        "panel_bg": (17, 24, 36),
        "panel_glow": (78, 146, 190),
        "panel_border": (88, 116, 156),
        "panel_shadow": (4, 8, 16),
        "danger": (255, 106, 106),
    }

from common.config import (
    EFFECT_FLAG_DAMAGE_BOOST,
    EFFECT_FLAG_DASH_COOLDOWN,
    EFFECT_FLAG_INVINCIBILITY,
    MODIFIER_DAMAGE_BOOST,
    MODIFIER_DASH_COOLDOWN,
    MODIFIER_INVINCIBILITY,
    MODIFIER_RADIUS,
    PLAYER_RADIUS,
    PLAYER_SPEED,
    WORLD_HEIGHT,
    WORLD_WIDTH,
)


COLORS = [
    (255, 116, 108),
    (104, 160, 255),
    (96, 228, 166),
    (255, 205, 96),
    (255, 140, 196),
    (104, 232, 232),
    (255, 170, 104),
    (168, 138, 255),
]
DEAD_COLOR = (104, 114, 132)


class GameRenderer:
    """Pygame-based renderer for the game demo."""

    def __init__(
        self,
        width: int = WORLD_WIDTH,
        height: int = WORLD_HEIGHT,
        headless: bool = False,
        screen=None,
        clock=None,
    ):
        if headless or not PYGAME_AVAILABLE:
            if not PYGAME_AVAILABLE and not headless:
                print("[RENDERER] pygame not available - running headless")
            self.headless = True
            return

        self.headless = False
        if not pygame.get_init():
            pygame.init()

        self.width = width
        self.height = height
        self.screen = (
            screen
            or pygame.display.get_surface()
            or pygame.display.set_mode((width, height))
        )
        self.font = pygame.font.SysFont("dejavusans", 14)
        self.big_font = pygame.font.SysFont("trebuchetms", 20, bold=True)
        self.clock = clock or pygame.time.Clock()
        self._prev_health: dict[int, float] = {}
        self._respawn_flash: dict[int, float] = {}
        self._arena_background_cache_key: tuple[int, int] | None = None
        self._arena_background_cache: Any | None = None

    @staticmethod
    def _with_alpha(
        color: tuple[int, int, int], alpha: int
    ) -> tuple[int, int, int, int]:
        return (*color, max(0, min(255, alpha)))

    @staticmethod
    def _lerp_color(
        start: tuple[int, int, int], end: tuple[int, int, int], t: float
    ) -> tuple[int, int, int]:
        return (
            int(start[0] + (end[0] - start[0]) * t),
            int(start[1] + (end[1] - start[1]) * t),
            int(start[2] + (end[2] - start[2]) * t),
        )

    def _build_arena_background(self) -> Any:
        background = pygame.Surface((self.width, self.height))
        top = (7, 12, 22)
        bottom = (19, 34, 52)
        for y_pos in range(self.height):
            t = y_pos / max(1, self.height - 1)
            pygame.draw.line(
                background,
                self._lerp_color(top, bottom, t),
                (0, y_pos),
                (self.width, y_pos),
            )

        glow_surface = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        pygame.draw.circle(
            glow_surface,
            self._with_alpha(GUI_THEME["panel_glow"], 34),
            (int(self.width * 0.2), int(self.height * 0.24)),
            max(130, self.width // 5),
        )
        pygame.draw.circle(
            glow_surface,
            self._with_alpha(GUI_THEME["text_accent"], 22),
            (int(self.width * 0.82), int(self.height * 0.18)),
            max(100, self.width // 6),
        )
        pygame.draw.ellipse(
            glow_surface,
            self._with_alpha((90, 150, 205), 18),
            (
                int(self.width * 0.22),
                int(self.height * 0.68),
                int(self.width * 0.56),
                90,
            ),
        )
        background.blit(glow_surface, (0, 0))

        grid_color = (34, 56, 80)
        for x_pos in range(28, self.width, 56):
            pygame.draw.line(
                background, grid_color, (x_pos, 0), (x_pos, self.height), 1
            )
        for y_pos in range(28, self.height, 56):
            pygame.draw.line(background, grid_color, (0, y_pos), (self.width, y_pos), 1)

        center_x, center_y = self.width // 2, self.height // 2
        arena_rect = pygame.Rect(14, 14, self.width - 28, self.height - 28)
        pygame.draw.rect(background, (10, 18, 30), arena_rect, border_radius=18)
        pygame.draw.rect(
            background,
            GUI_THEME["panel_border"],
            arena_rect,
            width=2,
            border_radius=18,
        )
        pygame.draw.rect(
            background,
            (42, 70, 98),
            arena_rect.inflate(-10, -10),
            width=1,
            border_radius=14,
        )
        pygame.draw.circle(background, (56, 86, 116), (center_x, center_y), 90, 1)
        pygame.draw.line(
            background,
            (48, 78, 108),
            (center_x, 22),
            (center_x, self.height - 22),
            1,
        )
        pygame.draw.line(
            background,
            (48, 78, 108),
            (22, center_y),
            (self.width - 22, center_y),
            1,
        )

        marker_color = GUI_THEME["text_accent"]
        marker_len = 22
        marker_gap = 18
        for corner_x, corner_y, dx, dy in [
            (22, 22, 1, 1),
            (self.width - 22, 22, -1, 1),
            (22, self.height - 22, 1, -1),
            (self.width - 22, self.height - 22, -1, -1),
        ]:
            pygame.draw.line(
                background,
                marker_color,
                (corner_x, corner_y),
                (corner_x + dx * marker_len, corner_y),
                2,
            )
            pygame.draw.line(
                background,
                marker_color,
                (corner_x, corner_y),
                (corner_x, corner_y + dy * marker_len),
                2,
            )
            pygame.draw.line(
                background,
                (32, 56, 80),
                (corner_x + dx * marker_gap, corner_y),
                (corner_x + dx * (marker_gap + marker_len), corner_y),
                1,
            )
            pygame.draw.line(
                background,
                (32, 56, 80),
                (corner_x, corner_y + dy * marker_gap),
                (corner_x, corner_y + dy * (marker_gap + marker_len)),
                1,
            )

        return background

    def _draw_arena_background(self):
        cache_key = (self.width, self.height)
        if self._arena_background_cache_key != cache_key:
            self._arena_background_cache_key = cache_key
            self._arena_background_cache = self._build_arena_background()
        if self._arena_background_cache is not None:
            self.screen.blit(self._arena_background_cache, (0, 0))

    def _draw_health_bar(self, position: tuple[int, int], health: float):
        bar_w = 30
        bar_h = 4
        health_frac = max(0.0, min(1.0, health / 100.0))
        bar_x = position[0] - bar_w // 2
        bar_y = position[1] + PLAYER_RADIUS + 4
        pygame.draw.rect(
            self.screen, (18, 18, 24), (bar_x, bar_y, bar_w, bar_h), border_radius=3
        )
        pygame.draw.rect(
            self.screen,
            GUI_THEME["success"],
            (bar_x, bar_y, int(bar_w * health_frac), bar_h),
            border_radius=3,
        )

    def _draw_dash_trail(
        self,
        position: tuple[int, int],
        velocity_x: float,
        velocity_y: float,
        color: tuple[int, int, int],
    ):
        speed = (velocity_x**2 + velocity_y**2) ** 0.5
        if speed <= PLAYER_SPEED * 2:
            return

        vx_norm = velocity_x / speed
        vy_norm = velocity_y / speed
        for index in range(1, 4):
            trail_x = int(position[0] - vx_norm * index * 8)
            trail_y = int(position[1] - vy_norm * index * 8)
            alpha = max(30, 180 - index * 50)
            radius = max(3, PLAYER_RADIUS - index * 2)
            trail_surface = pygame.Surface(
                (PLAYER_RADIUS * 2, PLAYER_RADIUS * 2), pygame.SRCALPHA
            )
            pygame.draw.circle(
                trail_surface,
                (*color, alpha),
                (PLAYER_RADIUS, PLAYER_RADIUS),
                radius,
            )
            self.screen.blit(
                trail_surface, (trail_x - PLAYER_RADIUS, trail_y - PLAYER_RADIUS)
            )

    def _draw_crown(self, position: tuple[int, int]):
        crown_y = max(18, position[1])
        shadow_points = [
            (position[0] - 12, crown_y + 2),
            (position[0] - 6, crown_y - 8 + 2),
            (position[0], crown_y - 2 + 2),
            (position[0] + 6, crown_y - 10 + 2),
            (position[0] + 12, crown_y + 2),
        ]
        pygame.draw.polygon(self.screen, (35, 20, 0), shadow_points)
        glow = pygame.Surface((40, 24), pygame.SRCALPHA)
        pygame.draw.ellipse(glow, (255, 210, 90, 40), (0, 4, 40, 16))
        self.screen.blit(glow, (position[0] - 20, crown_y - 16))
        crown_color = (255, 215, 80)
        points = [
            (position[0] - 12, crown_y),
            (position[0] - 6, crown_y - 10),
            (position[0], crown_y - 3),
            (position[0] + 6, crown_y - 12),
            (position[0] + 12, crown_y),
        ]
        pygame.draw.polygon(self.screen, crown_color, points)
        pygame.draw.polygon(self.screen, (80, 50, 0), points, 2)
        pygame.draw.circle(self.screen, (120, 235, 255), (position[0], crown_y - 3), 3)

    def _draw_modifier_icon(
        self,
        position: tuple[int, int],
        modifier_type: int,
        color: tuple[int, int, int],
    ):
        if modifier_type == MODIFIER_INVINCIBILITY:
            points = [
                (position[0], position[1] - 8),
                (position[0] + 7, position[1] - 2),
                (position[0] + 4, position[1] + 8),
                (position[0] - 4, position[1] + 8),
                (position[0] - 7, position[1] - 2),
            ]
            pygame.draw.polygon(self.screen, color, points, 2)
        elif modifier_type == MODIFIER_DAMAGE_BOOST:
            points = [
                (position[0] - 2, position[1] - 8),
                (position[0] + 5, position[1] - 8),
                (position[0] + 1, position[1]),
                (position[0] + 7, position[1]),
                (position[0] - 4, position[1] + 9),
                (position[0], position[1] + 1),
                (position[0] - 6, position[1] + 1),
            ]
            pygame.draw.polygon(self.screen, color, points)
        else:
            pygame.draw.line(
                self.screen,
                color,
                (position[0] - 7, position[1] + 4),
                (position[0] - 1, position[1] - 2),
                3,
            )
            pygame.draw.line(
                self.screen,
                color,
                (position[0] + 1, position[1] + 4),
                (position[0] + 7, position[1] - 2),
                3,
            )

    def _draw_effect_aura(self, position: tuple[int, int], effect_flags: int):
        if effect_flags & EFFECT_FLAG_INVINCIBILITY:
            pygame.draw.circle(
                self.screen, GUI_THEME["text_accent"], position, PLAYER_RADIUS + 6, 2
            )
        if effect_flags & EFFECT_FLAG_DAMAGE_BOOST:
            pygame.draw.circle(
                self.screen, (255, 125, 90), position, PLAYER_RADIUS + 3, 3
            )
        if effect_flags & EFFECT_FLAG_DASH_COOLDOWN:
            pygame.draw.arc(
                self.screen,
                (255, 215, 80),
                (position[0] - 22, position[1] - 22, 44, 44),
                0.5,
                2.6,
                3,
            )

    def _draw_modifier(self, modifier: dict):
        modifier_type = modifier.get("modifier_type", 0)
        position = (int(modifier.get("x", 0.0)), int(modifier.get("y", 0.0)))
        if modifier_type == MODIFIER_INVINCIBILITY:
            color = (120, 220, 255)
        elif modifier_type == MODIFIER_DAMAGE_BOOST:
            color = (255, 120, 90)
        else:
            color = (255, 220, 80)

        glow = pygame.Surface((44, 44), pygame.SRCALPHA)
        pygame.draw.circle(glow, (*color, 45), (22, 22), 18)
        self.screen.blit(glow, (position[0] - 22, position[1] - 22))
        pygame.draw.circle(self.screen, (20, 20, 20), position, MODIFIER_RADIUS + 4)
        pygame.draw.circle(self.screen, color, position, MODIFIER_RADIUS + 1)
        pygame.draw.circle(self.screen, (22, 26, 38), position, MODIFIER_RADIUS - 2)
        self._draw_modifier_icon(position, modifier_type, color)

    def _draw_entity(
        self,
        entity_id: int,
        state: dict,
        label_text: str,
        local: bool = False,
        leader: bool = False,
    ):
        health = state.get("health", 100.0)
        alive = health > 0.0
        color = COLORS[entity_id % len(COLORS)] if alive else DEAD_COLOR
        position = (int(state["x"]), int(state["y"]))
        effect_flags = int(state.get("effect_flags", 0))

        if entity_id in self._respawn_flash:
            flash_age = _time.perf_counter() - self._respawn_flash[entity_id]
            flash_alpha = max(0, int(220 * (1.0 - flash_age / 0.4)))
            flash_surface = pygame.Surface(
                ((PLAYER_RADIUS + 8) * 2, (PLAYER_RADIUS + 8) * 2), pygame.SRCALPHA
            )
            pygame.draw.circle(
                flash_surface,
                (255, 255, 255, flash_alpha),
                (PLAYER_RADIUS + 8, PLAYER_RADIUS + 8),
                PLAYER_RADIUS + 8,
                3,
            )
            self.screen.blit(
                flash_surface,
                (position[0] - PLAYER_RADIUS - 8, position[1] - PLAYER_RADIUS - 8),
            )

        if local and alive:
            self._draw_dash_trail(
                position, state.get("vx", 0.0), state.get("vy", 0.0), color
            )

        if alive and effect_flags:
            self._draw_effect_aura(position, effect_flags)

        pygame.draw.circle(
            self.screen, (12, 16, 24), (position[0] + 2, position[1] + 2), PLAYER_RADIUS
        )
        if local:
            pygame.draw.circle(
                self.screen, GUI_THEME["text"], position, PLAYER_RADIUS + 3
            )
        pygame.draw.circle(self.screen, color, position, PLAYER_RADIUS)
        if leader and alive:
            self._draw_crown((position[0], position[1] - PLAYER_RADIUS - 18))

        label_color = (255, 255, 255) if local else (210, 216, 228)
        label = self.font.render(label_text, True, label_color)
        label_rect = label.get_rect()
        label_rect.midbottom = (position[0], position[1] - PLAYER_RADIUS - 8)
        pill_rect = label_rect.inflate(12, 6)
        pill = pygame.Surface((pill_rect.width, pill_rect.height), pygame.SRCALPHA)
        pill.fill((0, 0, 0, 0))
        pygame.draw.rect(
            pill,
            (18, 24, 36, 210 if local else 170),
            pill.get_rect(),
            border_radius=10,
        )
        pygame.draw.rect(
            pill,
            (255, 255, 255, 50 if local else 24),
            pill.get_rect(),
            width=1,
            border_radius=10,
        )
        self.screen.blit(pill, pill_rect.topleft)
        self.screen.blit(label, label_rect)
        self._draw_health_bar(position, health)

    def render(
        self,
        local_state: dict,
        remote_states: dict,
        local_id: int,
        metrics: dict,
        scores: dict | None = None,
        player_names: dict[int, str] | None = None,
        modifiers: dict | None = None,
        draw_hud: bool = True,
        present: bool = True,
    ):
        if self.headless:
            return

        self.width, self.height = self.screen.get_size()

        self._draw_arena_background()

        all_states = dict(remote_states)
        if local_state and "x" in local_state:
            all_states[local_id] = local_state

        active_ids = set(all_states)
        self._prev_health = {
            entity_id: health
            for entity_id, health in self._prev_health.items()
            if entity_id in active_ids
        }
        self._respawn_flash = {
            entity_id: flash_start
            for entity_id, flash_start in self._respawn_flash.items()
            if entity_id in active_ids
        }

        for entity_id, state in all_states.items():
            current_health = float(state.get("health", 100.0))
            previous_health = float(self._prev_health.get(entity_id, current_health))
            if previous_health <= 0.0 < current_health:
                self._respawn_flash[entity_id] = _time.perf_counter()
            self._prev_health[entity_id] = current_health

        now = _time.perf_counter()
        self._respawn_flash = {
            entity_id: flash_start
            for entity_id, flash_start in self._respawn_flash.items()
            if now - flash_start < 0.4
        }

        if modifiers:
            for modifier in modifiers.values():
                self._draw_modifier(
                    modifier.to_dict() if hasattr(modifier, "to_dict") else modifier
                )

        active_scores = scores or {}
        visible_names = player_names or {}
        leader_id = None
        living_ids = [
            entity_id
            for entity_id, state in all_states.items()
            if float(state.get("health", 0.0)) > 0.0
        ]
        if living_ids:
            leader_id = min(
                living_ids,
                key=lambda entity_id: (-active_scores.get(entity_id, 0), entity_id),
            )

        for entity_id, state in remote_states.items():
            self._draw_entity(
                entity_id,
                state,
                visible_names.get(entity_id, f"P{entity_id}"),
                leader=entity_id == leader_id,
            )

        if local_state and "x" in local_state:
            local_name = visible_names.get(local_id)
            self._draw_entity(
                local_id,
                local_state,
                f"{local_name} (YOU)" if local_name else f"YOU (P{local_id})",
                local=True,
                leader=local_id == leader_id,
            )

        if draw_hud:
            self._draw_hud(metrics)

        if present:
            pygame.display.flip()

    def _draw_hud(self, metrics: dict):
        panel_h = 20 + len(metrics) * 18
        panel = pygame.Surface((200, panel_h), pygame.SRCALPHA)
        panel.fill(self._with_alpha(GUI_THEME["panel_bg"], 210))
        self.screen.blit(panel, (5, 5))

        y_pos = 10
        title = self.font.render("Network Stats", True, GUI_THEME["text_accent"])
        self.screen.blit(title, (10, y_pos))
        y_pos += 20

        for key, value in metrics.items():
            text = self.font.render(f"{key}: {value}", True, GUI_THEME["text"])
            self.screen.blit(text, (10, y_pos))
            y_pos += 18

    def get_input(self) -> dict:
        if self.headless:
            return {"move_x": 0.0, "move_y": 0.0, "actions": 0}

        keys = pygame.key.get_pressed()
        move_x = (1.0 if keys[pygame.K_d] or keys[pygame.K_RIGHT] else 0.0) - (
            1.0 if keys[pygame.K_a] or keys[pygame.K_LEFT] else 0.0
        )
        move_y = (1.0 if keys[pygame.K_s] or keys[pygame.K_DOWN] else 0.0) - (
            1.0 if keys[pygame.K_w] or keys[pygame.K_UP] else 0.0
        )

        actions = 0
        if keys[pygame.K_SPACE]:
            actions |= 0x01

        return {"move_x": move_x, "move_y": move_y, "actions": actions}

    def check_quit(self) -> bool:
        if self.headless:
            return False

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return True
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                return True
        return False

    def close(self):
        if not self.headless and PYGAME_AVAILABLE:
            pygame.quit()
