"""Match Over results screen - auto-returns to lobby on MATCH_RESET."""

from __future__ import annotations

import pygame

from client.gui.scene_manager import BaseScene
from client.gui.theme import (
    THEME,
    draw_hud_metric_card,
    draw_scene_background,
    get_font,
)
from client.gui.widgets import Panel


class MatchOverScene(BaseScene):
    def __init__(self, manager, client):
        super().__init__(manager)
        self.client = client

    def update(self, dt):
        _ = dt
        phase_sync_pending = getattr(self.client, "phase_sync_pending", lambda: False)
        if (
            not self.client.game_started_by_server
            and self.client.match_winner_id is None
            and not phase_sync_pending()
        ):
            from client.gui.scenes.lobby import LobbyScene

            self.mgr.replace(
                LobbyScene(self.mgr, client=self.client, host=self.client.host_mode)
            )

    def draw(self, surface: pygame.Surface):
        width, height = surface.get_size()
        draw_scene_background(surface, accent=(54, 42, 28))

        title = get_font(36, bold=True, family="display").render(
            "MATCH OVER", True, THEME["text"]
        )
        surface.blit(title, title.get_rect(center=(width // 2, 110)))

        winner_id = self.client.match_winner_id
        if winner_id == self.client.client_id:
            winner_text = "YOU WIN!"
            winner_color = THEME["success"]
        else:
            winner_text = (
                f"P{winner_id} WINS!" if winner_id is not None else "NO WINNER"
            )
            winner_color = THEME["text_gold"]

        winner_badge = pygame.Rect(width // 2 - 116, 154, 232, 44)
        pygame.draw.rect(surface, THEME["panel_bg_alt"], winner_badge, border_radius=18)
        pygame.draw.rect(surface, winner_color, winner_badge, width=1, border_radius=18)
        winner = get_font(26, bold=True).render(winner_text, True, winner_color)
        surface.blit(winner, winner.get_rect(center=winner_badge.center))

        panel_rect = pygame.Rect(width // 2 - 210, height // 2 - 40, 420, 240)
        panel = Panel(
            panel_rect,
            title="FINAL SCOREBOARD",
            accent_key="accent_warm",
        )
        panel.draw(surface)

        rows = sorted(
            self.client.scores.items(),
            key=lambda item: (-item[1], item[0]),
        )
        if not rows:
            empty = get_font(16).render("No score data", True, THEME["text_dim"])
            surface.blit(empty, empty.get_rect(center=panel_rect.center))
        else:
            row_step = 30
            footer_height = 24
            content_top = panel_rect.y + 52
            content_bottom = panel_rect.bottom - 18
            available_height = max(0, content_bottom - content_top)
            max_rows = max(1, available_height // row_step)
            if len(rows) > max_rows:
                max_rows = max(1, (available_height - footer_height) // row_step)

            display_rows = rows[:max_rows]
            for index, (entity_id, kills) in enumerate(display_rows):
                y_pos = content_top + index * row_step
                label = f"P{entity_id}"
                if entity_id == self.client.client_id:
                    label += " (YOU)"
                draw_hud_metric_card(
                    surface,
                    pygame.Rect(panel_rect.x + 18, y_pos, panel_rect.width - 36, 26),
                    label,
                    f"{kills} kill{'s' if kills != 1 else ''}",
                    accent=(
                        THEME["accent_secondary"]
                        if entity_id == self.client.client_id
                        else THEME["text_gold"]
                        if index == 0
                        else THEME["text_accent"]
                    ),
                    value_family="sans",
                    value_size=15,
                    label_size=10,
                )

            hidden_count = len(rows) - len(display_rows)
            if hidden_count > 0:
                more = get_font(14).render(
                    f"+{hidden_count} more players", True, THEME["text_dim"]
                )
                surface.blit(
                    more,
                    more.get_rect(center=(panel_rect.centerx, panel_rect.bottom - 26)),
                )

        visible = (pygame.time.get_ticks() // 500) % 2 == 0
        if visible:
            returning = get_font(16).render(
                "Returning to lobby...", True, THEME["text_dim"]
            )
            surface.blit(
                returning, returning.get_rect(center=(width // 2, height - 54))
            )
