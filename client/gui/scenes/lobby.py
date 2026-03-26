"""Lobby scene for hosting/joining a session."""

from __future__ import annotations

import pygame

from common.net import detect_lan_ipv4

from client.gui.scene_manager import BaseScene
from client.gui.theme import (
    THEME,
    draw_scene_background,
    draw_status_banner,
    draw_wrapped_text,
    get_font,
)
from client.gui.widgets import Button, Panel, ScrollList, StatusDot


MIN_PLAYERS_TO_START = 1  # Set to 2 to enforce real multiplayer before start.


class LobbyScene(BaseScene):
    def __init__(self, manager, client, host: bool):
        super().__init__(manager)
        self.client = client
        self.host = host
        self.start_requested = False
        self.selected_player_id: int | None = None
        self.left_panel = Panel(
            (60, 120, 400, 350),
            title="CONNECTED PLAYERS",
            accent_key="accent_primary",
        )
        self.right_panel = Panel(
            (490, 120, 250, 350),
            title="SESSION",
            accent_key="accent_warm",
        )
        self.players_list = ScrollList((80, 160, 360, 270), [])
        self.start_button = Button(
            (530, 300, 170, 40),
            "START GAME",
            self._start_game,
            disabled=True,
            variant="primary",
        )
        self.kick_button = Button(
            (530, 350, 170, 40),
            "KICK PLAYER",
            self._kick_selected,
            disabled=True,
            variant="danger",
        )
        self.back_button = Button(
            (530, 400, 170, 40),
            "BACK",
            self._back,
            variant="ghost",
        )

    def _layout(self, surface: pygame.Surface):
        width, height = surface.get_size()
        left_width = min(480, width - 340)
        right_width = 270
        left_x = 40
        right_x = width - right_width - 40
        top = 112
        panel_height = max(320, height - 230)

        self.left_panel.rect = pygame.Rect(left_x, top, left_width, panel_height)
        self.right_panel.rect = pygame.Rect(right_x, top, right_width, panel_height)
        self.players_list.rect = pygame.Rect(
            self.left_panel.rect.x + 18,
            self.left_panel.rect.y + 42,
            self.left_panel.rect.width - 36,
            self.left_panel.rect.height - 74,
        )

        button_w = self.right_panel.rect.width - 44
        button_x = self.right_panel.rect.x + 22
        action_top = min(
            self.right_panel.rect.y + 220,
            self.right_panel.rect.bottom - 170,
        )
        self.start_button.rect = pygame.Rect(
            button_x, action_top, button_w, 42
        )
        self.kick_button.rect = pygame.Rect(
            button_x, action_top + 54, button_w, 42
        )
        self.back_button.rect = pygame.Rect(
            button_x, self.right_panel.rect.bottom - 64, button_w, 42
        )

    def on_enter(self):
        if self.client.conn_state.name == "DISCONNECTED":
            self.client.connect()

    def _start_game(self):
        self.start_requested = self.client.request_game_start()

    def _kick_selected(self):
        if self.selected_player_id is None:
            return
        if self.client.request_kick_player(self.selected_player_id):
            self.selected_player_id = self.client.client_id

    def _back(self):
        self.client.disconnect()
        if self.host:
            self.client.stop_host_server()
        self.client.host_mode = False
        self.mgr.pop()

    def _draw_status_banner(self, surface: pygame.Surface):
        if self.client.conn_state.name not in (
            "RECONNECTING",
            "CONNECTING",
        ) and not getattr(self.client, "last_connection_error", None):
            return

        text = (
            "Reconnecting to server..."
            if self.client.conn_state.name == "RECONNECTING"
            else "Connecting to server..."
        )
        color = THEME["dot_connecting"]
        if getattr(self.client, "last_connection_error", None):
            text = self.client.last_connection_error
            color = THEME["dot_disconnected"]

        banner_rect = pygame.Rect(
            self.left_panel.rect.x,
            self.left_panel.rect.bottom + 14,
            min(560, surface.get_width() - self.left_panel.rect.x - 40),
            48,
        )
        draw_status_banner(surface, banner_rect, text, color, size=14)

    def _state_key(self) -> str:
        return self.client.conn_state.name.lower()

    def _current_host_id(self) -> int | None:
        if self.client.server_host_client_id is not None:
            return self.client.server_host_client_id

        if not self.client.server_snapshots:
            return None

        latest = self.client.server_snapshots[-1]
        if not latest.entities:
            return None
        return min(latest.entities)

    def _is_local_host(self) -> bool:
        host_id = self._current_host_id()
        return host_id is not None and self.client.client_id == host_id

    def _build_player_rows(self) -> list[tuple[int | None, str]]:
        if not self.client.server_snapshots:
            return [(None, "Waiting for players...")]

        latest = self.client.server_snapshots[-1]
        host_id = self._current_host_id()
        rows = []
        for entity_id in sorted(latest.entities):
            label = self.client.display_name_for(entity_id)
            tags = []
            if entity_id == host_id:
                tags.append("HOST")
            if entity_id == self.client.client_id:
                tags.append("YOU")
            if tags:
                label += f" ({', '.join(tags)})"
            rows.append((entity_id, label))
        return rows

    def _sync_selection_from_rows(self, rows: list[tuple[int | None, str]]):
        if (
            self.players_list.selected_index is not None
            and 0 <= self.players_list.selected_index < len(rows)
        ):
            self.selected_player_id = rows[self.players_list.selected_index][0]

    def _move_selection(self, step: int):
        rows = self._build_player_rows()
        if not rows:
            return
        self.players_list.set_items([text for _, text in rows])
        current_index = None
        if self.selected_player_id is not None:
            for index, (entity_id, _) in enumerate(rows):
                if entity_id == self.selected_player_id:
                    current_index = index
                    break
        self.players_list.select_index(current_index)
        self.players_list.move_selection(step)
        self._sync_selection_from_rows(rows)

    def _selection_shortcuts(self) -> str:
        if self._is_local_host():
            return "Enter start   Del kick   Esc back"
        return "Esc back"

    def handle_event(self, event):
        self._layout(self.mgr.screen)
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                self._back()
                return
            if event.key == pygame.K_UP:
                self._move_selection(-1)
                return
            if event.key == pygame.K_DOWN:
                self._move_selection(1)
                return
            if event.key == pygame.K_HOME:
                rows = self._build_player_rows()
                self.players_list.set_items([text for _, text in rows])
                self.players_list.select_index(0)
                self._sync_selection_from_rows(rows)
                return
            if event.key == pygame.K_END:
                rows = self._build_player_rows()
                self.players_list.set_items([text for _, text in rows])
                self.players_list.select_index(len(self.players_list.items) - 1)
                self._sync_selection_from_rows(rows)
                return
            if (
                event.key in (pygame.K_RETURN, pygame.K_SPACE)
                and not self.start_button.disabled
            ):
                self._start_game()
                return
            if (
                event.key in (pygame.K_DELETE, pygame.K_k)
                and not self.kick_button.disabled
            ):
                self._kick_selected()
                return
        self.players_list.handle_event(event)
        rows = self._build_player_rows()
        self._sync_selection_from_rows(rows)
        if self.host or self._is_local_host():
            self.start_button.handle_event(event)
            self.kick_button.handle_event(event)
        self.back_button.handle_event(event)

    def update(self, dt: float):
        self._layout(self.mgr.screen)
        _ = dt
        rows = self._build_player_rows()
        self.players_list.set_items([text for _, text in rows])

        if self.selected_player_id is None:
            self.selected_player_id = self.client.client_id

        self.players_list.selected_index = None
        if self.selected_player_id is not None:
            for index, (entity_id, _) in enumerate(rows):
                if entity_id == self.selected_player_id:
                    self.players_list.selected_index = index
                    break

        if self.players_list.selected_index is None:
            self.selected_player_id = self.client.client_id
            if self.client.client_id is not None:
                for index, (entity_id, _) in enumerate(rows):
                    if entity_id == self.client.client_id:
                        self.players_list.selected_index = index
                        break

        player_count = (
            len(self.client.server_snapshots[-1].entities)
            if self.client.server_snapshots
            else 0
        )
        self.start_button.disabled = not (
            self.client.connected
            and self._is_local_host()
            and player_count >= MIN_PLAYERS_TO_START
        )
        if self.start_requested:
            self.start_button.disabled = True

        self.kick_button.disabled = not (
            self.client.connected
            and self._is_local_host()
            and self.selected_player_id is not None
            and self.selected_player_id != self.client.client_id
            and not self.start_requested
        )

        if self.client.game_started_by_server and self.mgr.current is self:
            from client.gui.scenes.game_hud import GameHUDScene

            self.mgr.replace(GameHUDScene(self.mgr, client=self.client, host=self.host))

    def draw(self, surface: pygame.Surface):
        self._layout(surface)
        width, height = surface.get_size()
        draw_scene_background(surface, accent=(24, 56, 84))

        title = get_font(30, bold=True, family="display").render(
            "SESSION LOBBY", True, THEME["text"]
        )
        subtitle = get_font(15).render(
            "Wait for players, then start.",
            True,
            THEME["text_dim"],
        )
        surface.blit(title, title.get_rect(center=(width // 2, 56)))
        surface.blit(subtitle, subtitle.get_rect(center=(width // 2, 86)))

        self.left_panel.draw(surface)
        roster_count = (
            len(self.client.server_snapshots[-1].entities)
            if self.client.server_snapshots
            else 0
        )
        roster_badge = pygame.Rect(
            self.left_panel.rect.right - 140, self.left_panel.rect.y + 10, 114, 24
        )
        pygame.draw.rect(surface, THEME["panel_bg_alt"], roster_badge, border_radius=12)
        pygame.draw.rect(
            surface,
            THEME["panel_border_soft"],
            roster_badge,
            width=1,
            border_radius=12,
        )
        badge_text = get_font(12, bold=True).render(
            f"PLAYERS {roster_count}", True, THEME["text_accent"]
        )
        surface.blit(badge_text, badge_text.get_rect(center=roster_badge.center))
        if self.client.conn_state.name != "CONNECTED":
            dots = "." * ((pygame.time.get_ticks() // 400) % 4)
            waiting_text = get_font(16).render(
                f"Connecting{dots}", True, THEME["text_dim"]
            )
            surface.blit(
                waiting_text,
                waiting_text.get_rect(center=self.left_panel.rect.center),
            )
        else:
            self.players_list.draw(surface)

            for _, item_rect, _ in self.players_list.visible_entries():
                index = (
                    item_rect.y
                    - self.players_list.rect.y
                    + self.players_list.scroll_offset
                ) // self.players_list.item_height
                if 0 <= index < len(self.players_list.items):
                    StatusDot((item_rect.x + 18, item_rect.y + 14), radius=5).draw(
                        surface, "connected"
                    )

        self.right_panel.draw(surface)

        StatusDot(
            (self.right_panel.rect.x + 24, self.right_panel.rect.y + 64), radius=7
        ).draw(surface, self._state_key())
        state_label = self.client.conn_state.name.replace("_", " ")
        surface.blit(
            get_font(16).render(state_label, True, THEME["text"]),
            (self.right_panel.rect.x + 40, self.right_panel.rect.y + 54),
        )

        role_label = "HOST" if self._is_local_host() else "PLAYER"
        role_color = THEME["text_gold"] if self._is_local_host() else THEME["text_dim"]
        surface.blit(
            get_font(14, bold=True).render(role_label, True, role_color),
            (self.right_panel.rect.x + 22, self.right_panel.rect.y + 86),
        )
        endpoint_host = self.client.server_addr[0]
        if self.host:
            endpoint_host = detect_lan_ipv4(endpoint_host)
        endpoint = f"{endpoint_host}:{self.client.server_addr[1]}"
        draw_wrapped_text(
            surface,
            endpoint,
            pygame.Rect(
                self.right_panel.rect.x + 22,
                self.right_panel.rect.y + 116,
                self.right_panel.rect.width - 44,
                34,
            ),
            THEME["text_dim"],
            size=13,
            align="left",
        )
        secure_label = "DTLS secured"
        surface.blit(
            get_font(12, bold=True).render(secure_label, True, THEME["text_accent"]),
            (self.right_panel.rect.x + 22, self.right_panel.rect.y + 150),
        )
        surface.blit(
            get_font(12).render("Room key required", True, THEME["text_dim"]),
            (self.right_panel.rect.x + 22, self.right_panel.rect.y + 168),
        )
        fingerprint = (
            self.client.server_certificate_fingerprint or "Fingerprint pending..."
        )
        draw_wrapped_text(
            surface,
            f"Fingerprint {fingerprint}",
            pygame.Rect(
                self.right_panel.rect.x + 22,
                self.right_panel.rect.y + 188,
                self.right_panel.rect.width - 44,
                max(28, self.start_button.rect.y - (self.right_panel.rect.y + 188) - 12),
            ),
            THEME["text_dim"],
            size=11,
            align="left",
        )
        self._draw_status_banner(surface)

        if self.host or self._is_local_host():
            self.start_button.draw(surface)
            self.kick_button.draw(surface)
        self.back_button.draw(surface)

        draw_wrapped_text(
            surface,
            self._selection_shortcuts(),
            pygame.Rect(
                self.right_panel.rect.x + 22,
                self.back_button.rect.y - 44,
                self.right_panel.rect.width - 44,
                32,
            ),
            THEME["text_dim"],
            size=12,
            align="center",
        )
