"""In-game HUD scene plus pause overlay."""

from __future__ import annotations

import pygame

from common.config import (
    DASH_COOLDOWN,
    EFFECT_FLAG_DAMAGE_BOOST,
    EFFECT_FLAG_DASH_COOLDOWN,
    EFFECT_FLAG_INVINCIBILITY,
    MATCH_DURATION_SECS,
)
from client.gui.scene_manager import BaseScene
from client.gui.theme import (
    THEME,
    draw_hud_metric_card,
    draw_hud_pill,
    draw_meter_bar,
    draw_status_banner,
    get_font,
)
from client.gui.widgets import Button, Panel, StatusDot
from client.gui.scenes.settings import SettingsScene
from client.renderer import GameRenderer


class GameHUDScene(BaseScene):
    def __init__(self, manager, client, host: bool):
        super().__init__(manager)
        self.client = client
        self.host = host
        self.renderer = GameRenderer(screen=manager.screen)
        self.show_scoreboard = False

    def get_input(self) -> dict:
        return self.renderer.get_input()

    def handle_event(self, event):
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.mgr.push(
                PauseOverlayScene(
                    self.mgr, client=self.client, parent_scene=self, host=self.host
                )
            )
        elif event.type == pygame.KEYDOWN and event.key == pygame.K_F3:
            self.client.show_debug_stats = not self.client.show_debug_stats
        elif event.type == pygame.KEYDOWN and event.key == pygame.K_TAB:
            self.show_scoreboard = True
        elif event.type == pygame.KEYUP and event.key == pygame.K_TAB:
            self.show_scoreboard = False

    def _draw_controls_hint(
        self, surface: pygame.Surface, *, reserve_above: pygame.Rect | None = None
    ):
        hint_rect = pygame.Rect(0, 0, 280, 30)
        bottom = surface.get_height() - 10
        if reserve_above is not None:
            bottom = reserve_above.y - 10
        hint_rect.midbottom = (surface.get_width() // 2, bottom)
        draw_hud_pill(
            surface,
            hint_rect,
            "Tab scoreboard   F3 debug   Esc pause",
            accent=THEME["accent_primary"],
            text_color=THEME["text_dim"],
            size=12,
            bold=False,
        )

    def update(self, dt: float):
        _ = dt
        phase_sync_pending = getattr(self.client, "phase_sync_pending", lambda: False)
        if self.client.match_winner_id is not None:
            from client.gui.scenes.match_over import MatchOverScene

            self.mgr.replace(MatchOverScene(self.mgr, client=self.client))
        elif (
            not self.client.game_started_by_server
            and self.client.connected
            and not phase_sync_pending()
        ):
            from client.gui.scenes.lobby import LobbyScene

            self.mgr.replace(
                LobbyScene(self.mgr, client=self.client, host=self.client.host_mode)
            )

    def _draw_dash_bar(
        self, surface: pygame.Surface, rect: pygame.Rect, dash_cd: float
    ):
        max_cooldown = DASH_COOLDOWN * self.client.local_state.get(
            "dash_cooldown_factor", 1.0
        )
        fill = 1.0 - min(1.0, dash_cd / max_cooldown) if max_cooldown > 0 else 1.0
        meter_rect = pygame.Rect(rect.x + 16, rect.bottom - 24, rect.width - 32, 10)
        draw_meter_bar(surface, meter_rect, fill, THEME["text_gold"])

    def _ranked_players(self, players: list[int], latest_snapshot) -> list[int]:
        active_players = set(players)
        entities = latest_snapshot.entities if latest_snapshot else {}

        def rank_key(entity_id: int):
            is_active = entity_id in active_players
            is_alive = is_active and entities[entity_id].health > 0.0
            bucket = 0 if is_alive else 1 if is_active else 2
            return (bucket, -self.client.scores.get(entity_id, 0), entity_id)

        return sorted(set(players) | set(self.client.scores.keys()), key=rank_key)

    def _draw_leaderboard(
        self,
        surface: pygame.Surface,
        players: list[int],
        rect: pygame.Rect,
        latest_snapshot,
    ):
        ranked_players = self._ranked_players(players, latest_snapshot)
        display_players = ranked_players[:5]
        panel_rect = pygame.Rect(
            rect.x,
            rect.y,
            rect.width,
            max(rect.height, 64 + len(display_players) * 38),
        )
        panel = Panel(
            panel_rect,
            title="LEADERBOARD",
            accent_key="accent_primary",
        )
        panel.draw(surface)
        if not display_players:
            text = get_font(14).render(
                "Waiting for players...", True, THEME["text_dim"]
            )
            surface.blit(text, text.get_rect(center=panel_rect.center))
            return

        active_players = set(players)
        for index, entity_id in enumerate(display_players, start=1):
            row_rect = pygame.Rect(
                panel_rect.x + 14,
                panel_rect.y + 52 + (index - 1) * 38,
                panel_rect.width - 28,
                30,
            )
            label = f"#{index}  P{entity_id}"
            if entity_id == self.client.client_id:
                label += " (YOU)"
            elif entity_id not in active_players:
                label += " (OUT)"
            score = self.client.scores.get(entity_id, 0)
            accent = (
                THEME["text_gold"]
                if index == 1
                else THEME["accent_secondary"]
                if entity_id == self.client.client_id
                else THEME["hud_value"]
            )
            draw_hud_metric_card(
                surface,
                row_rect,
                label,
                str(score),
                accent=accent,
                value_family="mono",
                value_size=17,
            )

    def _draw_respawn_overlay(
        self, surface: pygame.Surface, respawn_ticks_remaining: int
    ):
        overlay = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 120))
        surface.blit(overlay, (0, 0))

        width, height = surface.get_size()
        seconds = max(0.0, respawn_ticks_remaining * self.client.dt)
        title = get_font(30, bold=True).render(
            "YOU DIED", True, THEME["dot_disconnected"]
        )
        subtitle = get_font(18).render(
            f"Respawning in {seconds:.1f}s...", True, THEME["text"]
        )
        surface.blit(title, title.get_rect(center=(width // 2, height // 2 - 30)))
        surface.blit(subtitle, subtitle.get_rect(center=(width // 2, height // 2 + 12)))

    def _draw_buff_chips(
        self, surface: pygame.Surface, rect: pygame.Rect, buffs: list[str]
    ):
        if not buffs:
            return

        chip_x = rect.x + 16
        chip_y = rect.bottom - 54
        for buff in buffs:
            if buff == "INV":
                bg = THEME["accent_primary"]
                label = "INVULN"
            elif buff == "DMG":
                bg = THEME["accent_danger"]
                label = "BOOST"
            else:
                bg = THEME["accent_warm"]
                label = "QUICK"
            chip_rect = pygame.Rect(
                chip_x, chip_y, max(64, len(label) * 9 + 18), 24
            )
            draw_hud_pill(
                surface,
                chip_rect,
                label,
                accent=bg,
                text_color=THEME["hud_value"],
                size=11,
            )
            chip_x += chip_rect.width + 8

    def _current_connection_notice(self) -> tuple[str, tuple[int, int, int]] | None:
        notice = getattr(self.client, "last_connection_error", None)
        if self.client.conn_state.name == "RECONNECTING":
            notice = "Reconnecting to the server..."
        elif self.client.conn_state.name == "CONNECTING":
            notice = "Connecting to the server..."

        if not notice:
            return None

        color = (
            THEME["dot_disconnected"]
            if getattr(self.client, "last_connection_error", None)
            else THEME["dot_connecting"]
        )
        return notice, color

    def _draw_connection_banner(self, surface: pygame.Surface):
        current_notice = self._current_connection_notice()
        if current_notice is None:
            return
        notice, color = current_notice
        width, _height = surface.get_size()
        draw_status_banner(
            surface,
            pygame.Rect(width // 2 - 210, 18, 420, 42),
            notice,
            color,
            size=14,
        )

    def _draw_match_timer(self, surface: pygame.Surface):
        if self._current_connection_notice() is not None:
            return

        if MATCH_DURATION_SECS > 0:
            remaining = max(0.0, MATCH_DURATION_SECS - self.client.match_elapsed)
            timer_text = f"{int(remaining // 60):02d}:{int(remaining % 60):02d}"
        else:
            timer_text = f"{int(self.client.match_elapsed // 60):02d}:{int(self.client.match_elapsed % 60):02d}"
        timer_rect = pygame.Rect(surface.get_width() // 2 - 74, 18, 148, 32)
        draw_hud_pill(
            surface,
            timer_rect,
            timer_text,
            accent=THEME["accent_warm"],
            text_color=THEME["hud_value"],
            size=16,
            family="mono",
        )

    def _draw_stats_panel(
        self,
        surface: pygame.Surface,
        rect: pygame.Rect,
        local: dict,
        dash_cd: float,
        buffs: list[str],
    ):
        Panel(rect, title="YOU", accent_key="accent_secondary").draw(surface)

        health = float(local.get("health", 0.0))
        health_color = (
            THEME["success"]
            if health >= 70
            else THEME["warning"]
            if health >= 35
            else THEME["danger"]
        )
        dash_value = "READY" if dash_cd <= 0.0 else f"{dash_cd:.1f}s"
        dash_color = THEME["text_gold"] if dash_cd <= 0.0 else THEME["warning"]
        rows = [
            ("Player", f"P{self.client.client_id or 0}", THEME["text_accent"], "sans"),
            (
                "Position",
                f"{local.get('x', 0.0):.0f}, {local.get('y', 0.0):.0f}",
                THEME["hud_value"],
                "mono",
            ),
            ("Health", f"{health:.0f}", health_color, "mono"),
            ("Dash", dash_value, dash_color, "sans"),
        ]
        for index, (label, value, accent, family) in enumerate(rows):
            row_rect = pygame.Rect(
                rect.x + 16,
                rect.y + 52 + index * 36,
                rect.width - 32,
                30,
            )
            draw_hud_metric_card(
                surface,
                row_rect,
                label,
                value,
                accent=accent,
                value_family=family,
            )

        self._draw_buff_chips(surface, rect, buffs)
        self._draw_dash_bar(surface, rect, dash_cd)

    def _draw_debug_strip(self, surface: pygame.Surface, rect: pygame.Rect):
        Panel(rect, title="DEBUG", accent_key="accent_primary").draw(surface)
        state_key = self.client.conn_state.name.lower()
        state_label = self.client.conn_state.name.replace("_", " ")
        loss_text = self.client.get_metrics_display().get("Loss", "0.0%")
        cards = [
            ("State", state_label, THEME[f"dot_{state_key}"], "sans"),
            ("RTT", f"{self.client.current_rtt:.1f} ms", THEME["text_accent"], "mono"),
            ("Loss", loss_text, THEME["warning"], "mono"),
            ("FPS", f"{self.client.current_fps:.0f}", THEME["accent_secondary"], "mono"),
            ("Tick", str(self.client.last_server_tick), THEME["hud_value"], "mono"),
            (
                "Jitter",
                f"{self.client.current_jitter:.1f} ms",
                THEME["accent_warm"],
                "mono",
            ),
        ]
        gap = 8
        content_width = rect.width - 32
        card_width = (content_width - gap * (len(cards) - 1)) // len(cards)
        for index, (label, value, accent, family) in enumerate(cards):
            card_rect = pygame.Rect(
                rect.x + 16 + index * (card_width + gap),
                rect.y + 52,
                card_width,
                32,
            )
            draw_hud_metric_card(
                surface,
                card_rect,
                label,
                value,
                accent=accent,
                value_family=family,
                value_size=16,
            )
        StatusDot((rect.x + 28, rect.y + 68), radius=5).draw(surface, state_key)

    def draw(self, surface: pygame.Surface):
        width, height = surface.get_size()
        margin = 18
        leaderboard_rect = pygame.Rect(margin, margin, 258, 238)
        stats_rect = pygame.Rect(width - 294, margin, 276, 238)
        debug_rect = pygame.Rect(margin, height - 118, width - margin * 2, 100)

        remote_states = self.client.get_remote_states()
        metrics = (
            self.client.get_metrics_display() if self.client.show_debug_stats else {}
        )
        latest_snapshot = (
            self.client.server_snapshots[-1] if self.client.server_snapshots else None
        )
        self.renderer.render(
            self.client.visual_state,
            remote_states,
            self.client.client_id or 0,
            metrics,
            scores=self.client.scores,
            modifiers=latest_snapshot.modifiers if latest_snapshot else {},
            draw_hud=False,
            present=False,
        )

        players = sorted(latest_snapshot.entities) if latest_snapshot else []
        self._draw_leaderboard(surface, players, leaderboard_rect, latest_snapshot)
        self._draw_match_timer(surface)

        local = self.client.visual_state or {"x": 0.0, "y": 0.0, "health": 100.0}
        dash_cd = self.client.local_state.get("dash_cooldown", 0.0)
        effect_flags = int(self.client.local_state.get("effect_flags", 0))
        buffs = []
        if effect_flags & EFFECT_FLAG_INVINCIBILITY:
            buffs.append("INV")
        if effect_flags & EFFECT_FLAG_DAMAGE_BOOST:
            buffs.append("DMG")
        if effect_flags & EFFECT_FLAG_DASH_COOLDOWN:
            buffs.append("DASH")
        self._draw_stats_panel(surface, stats_rect, local, dash_cd, buffs)

        controls_anchor = None
        if self.client.show_debug_stats:
            self._draw_debug_strip(surface, debug_rect)
            controls_anchor = debug_rect

        if self.show_scoreboard:
            self._draw_scoreboard(surface, players, latest_snapshot)

        self._draw_connection_banner(surface)
        self._draw_controls_hint(surface, reserve_above=controls_anchor)

        if self.client.visual_state and local.get("health", 100.0) <= 0.0:
            self._draw_respawn_overlay(
                surface, int(local.get("respawn_ticks_remaining", 0))
            )

    def _draw_scoreboard(
        self, surface: pygame.Surface, players: list[int], latest_snapshot
    ):
        overlay = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 110))
        surface.blit(overlay, (0, 0))

        width, height = surface.get_size()
        panel_rect = pygame.Rect(width // 2 - 190, height // 2 - 130, 380, 250)
        panel = Panel(panel_rect, title="SCOREBOARD", accent_key="accent_warm")
        panel.draw(surface)

        active_players = self._ranked_players(players, latest_snapshot)

        if not active_players:
            text = get_font(16).render("No players connected", True, THEME["text_dim"])
            surface.blit(text, text.get_rect(center=(width // 2, height // 2)))
            return

        row_step = 34
        footer_height = 24
        content_top = panel_rect.y + 54
        content_bottom = panel_rect.bottom - 18
        available_height = max(0, content_bottom - content_top)
        max_rows = max(1, available_height // row_step)
        if len(active_players) > max_rows:
            max_rows = max(1, (available_height - footer_height) // row_step)
        display_players = active_players[:max_rows]
        for index, entity_id in enumerate(display_players, start=1):
            row_rect = pygame.Rect(
                panel_rect.x + 18,
                content_top + (index - 1) * row_step,
                panel_rect.width - 36,
                28,
            )
            label = f"#{index}  P{entity_id}"
            if entity_id == self.client.client_id:
                label += " (YOU)"
            elif entity_id not in players:
                label += " (OUT)"
            kills = self.client.scores.get(entity_id, 0)
            accent = (
                THEME["accent_secondary"]
                if entity_id == self.client.client_id
                else THEME["text_gold"]
                if index == 1
                else THEME["hud_value"]
            )
            draw_hud_metric_card(
                surface,
                row_rect,
                label,
                f"{kills} K",
                accent=accent,
                value_family="mono",
            )

        hidden_count = len(active_players) - len(display_players)
        if hidden_count > 0:
            more = get_font(14).render(
                f"+{hidden_count} more players", True, THEME["text_dim"]
            )
            surface.blit(
                more,
                more.get_rect(center=(panel_rect.centerx, panel_rect.bottom - 26)),
            )


class PauseOverlayScene(BaseScene):
    def __init__(self, manager, client, parent_scene: GameHUDScene, host: bool):
        super().__init__(manager)
        self.client = client
        self.parent_scene = parent_scene
        self.host = host
        self.resume_button = Button(
            (320, 240, 160, 38),
            "RESUME",
            self._resume,
            variant="primary",
        )
        self.settings_button = Button(
            (320, 290, 160, 38),
            "SETTINGS",
            self._settings,
            variant="secondary",
        )
        self.disconnect_button = Button(
            (320, 340, 160, 38),
            "DISCONNECT",
            self._disconnect,
            variant="danger",
        )

    def _layout(self, surface: pygame.Surface):
        width, height = surface.get_size()
        panel_x = width // 2 - 170
        panel_y = height // 2 - 126
        self.resume_button.rect = pygame.Rect(panel_x + 90, panel_y + 54, 160, 40)
        self.settings_button.rect = pygame.Rect(panel_x + 90, panel_y + 106, 160, 40)
        self.disconnect_button.rect = pygame.Rect(panel_x + 90, panel_y + 158, 160, 40)

    def _resume(self):
        self.mgr.pop()

    def _settings(self):
        self.mgr.push(SettingsScene(self.mgr, client=self.client))

    def _disconnect(self):
        from client.gui.scenes.main_menu import MainMenuScene

        self.client.disconnect()
        if self.host:
            self.client.stop_host_server()
        self.client.host_mode = False
        while self.mgr.current is not None and not isinstance(
            self.mgr.current, MainMenuScene
        ):
            self.mgr.pop()

        if self.mgr.current is None:
            self.mgr.push(MainMenuScene(self.mgr, client=self.client))

    def handle_event(self, event):
        self._layout(self.mgr.screen)
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_ESCAPE, pygame.K_RETURN):
                self._resume()
                return
            if event.key == pygame.K_s:
                self._settings()
                return
            if event.key == pygame.K_d:
                self._disconnect()
                return
        self.resume_button.handle_event(event)
        self.settings_button.handle_event(event)
        self.disconnect_button.handle_event(event)

    def update(self, dt: float):
        _ = dt
        phase_sync_pending = getattr(self.client, "phase_sync_pending", lambda: False)
        if self.client.match_winner_id is not None:
            from client.gui.scenes.match_over import MatchOverScene

            self.mgr.pop()
            if self.mgr.current is self.parent_scene:
                self.mgr.replace(MatchOverScene(self.mgr, client=self.client))
            else:
                self.mgr.push(MatchOverScene(self.mgr, client=self.client))
        elif (
            not self.client.game_started_by_server
            and self.client.connected
            and not phase_sync_pending()
        ):
            from client.gui.scenes.lobby import LobbyScene

            self.mgr.reset(
                LobbyScene(self.mgr, client=self.client, host=self.client.host_mode)
            )

    def draw(self, surface: pygame.Surface):
        self._layout(surface)
        self.parent_scene.draw(surface)
        overlay = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 160))
        surface.blit(overlay, (0, 0))

        width, height = surface.get_size()
        panel = Panel(
            (width // 2 - 170, height // 2 - 126, 340, 236),
            title="PAUSED",
            accent_key="accent_primary",
        )
        panel.draw(surface)
        self.resume_button.draw(surface)
        self.settings_button.draw(surface)
        self.disconnect_button.draw(surface)
        hint = get_font(12).render(
            "Enter resume   S settings   D disconnect",
            True,
            THEME["text_dim"],
        )
        surface.blit(hint, hint.get_rect(center=(width // 2, height // 2 + 96)))
