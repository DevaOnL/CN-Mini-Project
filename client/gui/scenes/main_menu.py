"""Main menu scene."""

from __future__ import annotations

import socket
import subprocess
import sys
import time

import pygame

from common.net import detect_lan_ipv4
from client.gui.scene_manager import BaseScene
from client.gui.theme import (
    THEME,
    blend_color,
    draw_scene_background,
    draw_soft_glow,
    draw_status_banner,
    draw_vertical_gradient,
    get_font,
)
from client.gui.widgets import Button
from client.gui.scenes.settings import SettingsScene, load_config

HOST_START_TIMEOUT_SECS = 3.0


class MainMenuScene(BaseScene):
    def __init__(self, manager, client):
        super().__init__(manager)
        self.client = client
        self.error_message = ""
        self.buttons = []
        self.host_button = None
        self.join_button = None
        self.quit_button = None
        self.settings_button = None
        self._settings = load_config()
        self._host_starting = False
        self._host_start_deadline = 0.0
        self._host_startup_check_count = 0

    def _sync_button_state(self):
        busy = self._host_starting
        if self.host_button is not None:
            self.host_button.disabled = busy
            self.host_button.label = "STARTING..." if busy else "HOST GAME"
        if self.join_button is not None:
            self.join_button.disabled = busy
        if self.settings_button is not None:
            self.settings_button.disabled = busy
        if self.quit_button is not None:
            self.quit_button.disabled = False

    def _clear_host_startup(self):
        self._host_starting = False
        self._host_start_deadline = 0.0
        self._host_startup_check_count = 0
        self._sync_button_state()

    def on_enter(self):
        self._settings = load_config()
        self.error_message = self.client.ui_notice or ""
        self.client.ui_notice = None
        self.client.apply_settings(self._settings)
        self.client.begin_new_session()
        self.client.host_mode = False

        self.host_button = Button(
            (300, 250, 200, 42),
            "HOST GAME",
            self._host_game,
            variant="primary",
        )
        self.join_button = Button(
            (300, 310, 200, 42),
            "JOIN GAME",
            self._join_game,
            variant="secondary",
        )
        self.quit_button = Button(
            (300, 370, 200, 42),
            "QUIT",
            self._quit_game,
            variant="ghost",
        )
        self.buttons = [self.host_button, self.join_button, self.quit_button]
        self.settings_button = Button(
            (650, 545, 120, 32),
            "SETTINGS",
            self._open_settings,
            variant="ghost",
        )
        self._clear_host_startup()

    def on_resume(self):
        if self._host_starting:
            self._sync_button_state()
            return

        self._settings = load_config()
        self.error_message = self.client.ui_notice or ""
        self.client.ui_notice = None
        self.client.apply_settings(self._settings)
        self.client.begin_new_session()
        self._clear_host_startup()

    def _fit_font(
        self,
        text: str,
        *,
        family: str,
        bold: bool,
        start_size: int,
        min_size: int,
        max_width: int,
    ):
        for size in range(start_size, min_size - 1, -2):
            font = get_font(size, bold=bold, family=family)
            if font.size(text)[0] <= max_width:
                return font
        return get_font(min_size, bold=bold, family=family)

    def _build_hero_layout(self, surface: pygame.Surface) -> dict:
        width, height = surface.get_size()
        center_x = width // 2
        title_text = "MULTIPLAYER ENGINE"
        subtitle_text = "Local multiplayer over UDP"
        tagline_text = "Host or join in seconds."
        max_panel_width = max(360, width - 88)
        inner_max_width = max_panel_width - 72

        title_font = self._fit_font(
            title_text,
            family="display",
            bold=True,
            start_size=38,
            min_size=28,
            max_width=inner_max_width,
        )
        subtitle_font = self._fit_font(
            subtitle_text,
            family="sans",
            bold=False,
            start_size=18,
            min_size=15,
            max_width=inner_max_width,
        )
        tagline_font = self._fit_font(
            tagline_text,
            family="sans",
            bold=False,
            start_size=14,
            min_size=12,
            max_width=inner_max_width,
        )
        title = title_font.render(title_text, True, THEME["text"])
        subtitle = subtitle_font.render(subtitle_text, True, THEME["text_dim"])
        tagline = tagline_font.render(tagline_text, True, THEME["text_accent"])
        hero_width = min(
            max_panel_width,
            max(
                360,
                max(title.get_width(), subtitle.get_width(), tagline.get_width()) + 72,
            ),
        )
        hero_height = (
            28
            + title.get_height()
            + 8
            + subtitle.get_height()
            + 10
            + tagline.get_height()
            + 24
        )
        hero_panel = pygame.Rect(
            0, max(44, min(88, height // 8)), hero_width, hero_height
        )
        hero_panel.centerx = center_x

        title_rect = title.get_rect(midtop=(center_x, hero_panel.y + 24))
        subtitle_rect = subtitle.get_rect(midtop=(center_x, title_rect.bottom + 8))
        tagline_rect = tagline.get_rect(midtop=(center_x, subtitle_rect.bottom + 10))

        return {
            "hero_panel": hero_panel,
            "title": title,
            "title_rect": title_rect,
            "subtitle": subtitle,
            "subtitle_rect": subtitle_rect,
            "tagline": tagline,
            "tagline_rect": tagline_rect,
            "feature_rects": [],
            "cards": [],
            "feature_bottom": hero_panel.bottom,
            "cards_bottom": hero_panel.bottom,
        }

    def _layout(self, surface: pygame.Surface):
        width, height = surface.get_size()
        center_x = width // 2
        hero_layout = self._build_hero_layout(surface)
        button_height = 46
        button_gap = 18
        stack_height = (
            len(self.buttons) * button_height
            + max(0, len(self.buttons) - 1) * button_gap
        )
        preferred_top = max(hero_layout["cards_bottom"] + 28, int(height * 0.52))
        top = min(preferred_top, height - 120 - stack_height)
        top = max(hero_layout["cards_bottom"] + 18, top)
        for index, button in enumerate(self.buttons):
            button.rect.size = (252, 48)
            button.rect.topleft = (
                center_x - 126,
                top + index * (button_height + button_gap),
            )
        if self.settings_button is not None:
            self.settings_button.rect.size = (146, 36)
            self.settings_button.rect.bottomright = (width - 28, height - 18)

    def _stop_host_server(self):
        self.client.stop_host_server()
        self._clear_host_startup()

    def _can_bind_local_port(self) -> bool:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind(("0.0.0.0", self.client.server_port))
        except OSError:
            return False
        finally:
            probe.close()
        return True

    def _poll_host_startup(self):
        if not self._host_starting:
            return

        proc = self.client.host_server_proc
        if proc is None:
            self._stop_host_server()
            self.client.host_mode = False
            self.error_message = "Server process not found"
            return

        # Check if process exited prematurely
        poll_result = proc.poll()
        if poll_result is not None:
            # Process exited, read stderr to show error
            stdout_data, stderr_data = proc.communicate()
            stderr_text = stderr_data.decode('utf-8', errors='ignore').strip() if stderr_data else ""
            stdout_text = stdout_data.decode('utf-8', errors='ignore').strip() if stdout_data else ""

            error_detail = stderr_text or stdout_text or "Unknown error"
            if error_detail:
                print(f"[SERVER ERROR] {error_detail}")

            self._stop_host_server()
            self.client.host_mode = False
            self.error_message = f"Server failed to start: {error_detail[:60]}"
            return

        # Give server a bit of time to start before checking if port is bound
        self._host_startup_check_count = getattr(self, '_host_startup_check_count', 0) + 1
        if self._host_startup_check_count < 10:  # Wait ~0.17s (10 frames at 60fps)
            return

        # Now check if port is in use (server has bound)
        if not self._can_bind_local_port():
            from client.gui.scenes.lobby import LobbyScene

            self._clear_host_startup()
            self.client.connect()
            self.mgr.push(LobbyScene(self.mgr, client=self.client, host=True))
            return

        # Check timeout
        if time.monotonic() >= self._host_start_deadline:
            self._stop_host_server()
            self.client.host_mode = False
            self.error_message = (
                f"Server startup timeout on port {self.client.server_port}. "
                "Make sure the port is not in use."
            )
            return

    def _host_game(self):
        from client.gui.scenes.host_dialog import HostDialogScene

        self.mgr.push(
            HostDialogScene(
                self.mgr,
                client=self.client,
                on_submit=self._start_host_with_room_key,
            )
        )

    def _start_host_with_room_key(self, room_key: str) -> str | None:
        self.error_message = ""
        self.client.apply_settings(self._settings)
        self.client.begin_new_session()
        self.client.set_room_key(room_key)
        lan_ip = detect_lan_ipv4("0.0.0.0")
        self.client.server_host = lan_ip
        self.client.server_addr = (lan_ip, self.client.server_port)
        self.client.host_mode = True

        self._stop_host_server()
        if not self._can_bind_local_port():
            self.client.host_mode = False
            return (
                f"Could not host on port {self.client.server_port}; another server may "
                "already be using it."
            )

        self.client.host_server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "server.server",
                "--host",
                "0.0.0.0",
                "--port",
                str(self.client.server_port),
                "--room-key",
                room_key,
            ],
            stdout=subprocess.PIPE,  # Capture output for debugging
            stderr=subprocess.PIPE,
        )
        self._host_starting = True
        self._host_start_deadline = time.monotonic() + HOST_START_TIMEOUT_SECS
        self._host_startup_check_count = 0  # Track poll attempts
        self._sync_button_state()
        return None

    def _join_game(self):
        from client.gui.scenes.join_dialog import JoinDialogScene

        self.error_message = ""
        self.client.apply_settings(self._settings)
        self.client.begin_new_session()
        self.client.host_mode = False
        self.mgr.push(JoinDialogScene(self.mgr, client=self.client))

    def _open_settings(self):
        self.mgr.push(SettingsScene(self.mgr, client=self.client))

    def _quit_game(self):
        self._stop_host_server()
        self.client.running = False
        pygame.quit()
        raise SystemExit

    def handle_event(self, event):
        self._layout(self.mgr.screen)
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_h and not self._host_starting:
                self._host_game()
                return
            if event.key == pygame.K_j and not self._host_starting:
                self._join_game()
                return
            if event.key == pygame.K_s and not self._host_starting:
                self._open_settings()
                return
            if event.key in (pygame.K_ESCAPE, pygame.K_q):
                self._quit_game()
                return
        for button in self.buttons:
            button.handle_event(event)
        if self.settings_button is not None:
            self.settings_button.handle_event(event)

    def update(self, dt: float):
        _ = dt
        self._poll_host_startup()

    def draw(self, surface: pygame.Surface):
        self._layout(surface)
        width, height = surface.get_size()
        center_x = width // 2
        hero_layout = self._build_hero_layout(surface)
        draw_scene_background(surface, accent=(36, 54, 82))

        hero_panel = hero_layout["hero_panel"]
        draw_soft_glow(
            surface,
            hero_panel,
            THEME["accent_primary"],
            alpha=24,
            inflate_x=34,
            inflate_y=32,
            border_radius=28,
        )
        draw_vertical_gradient(
            surface,
            hero_panel,
            blend_color(THEME["panel_bg_highlight"], THEME["accent_primary"], 0.14),
            THEME["panel_bg"],
            border_radius=22,
        )
        pygame.draw.rect(surface, THEME["panel_border"], hero_panel, width=1, border_radius=22)
        highlight = pygame.Rect(
            hero_panel.x + 18, hero_panel.y + 18, hero_panel.width - 36, 2
        )
        pygame.draw.rect(surface, THEME["text_accent"], highlight, border_radius=2)

        surface.blit(hero_layout["title"], hero_layout["title_rect"])
        surface.blit(hero_layout["subtitle"], hero_layout["subtitle_rect"])
        surface.blit(hero_layout["tagline"], hero_layout["tagline_rect"])

        for button in self.buttons:
            button.draw(surface)
        if self.settings_button is not None:
            self.settings_button.draw(surface)

        banner_rect = None
        if self._host_starting or self.error_message:
            banner_height = 42 if self._host_starting and not self.error_message else 48
            banner_y = (
                self.buttons[-1].rect.bottom + 18
                if self.buttons
                else hero_panel.bottom + 18
            )
            max_banner_y = (
                (
                    self.settings_button.rect.top
                    if self.settings_button is not None
                    else height - 24
                )
                - 14
                - banner_height
            )
            banner_rect = pygame.Rect(
                center_x - 230,
                min(banner_y, max_banner_y),
                460,
                banner_height,
            )

        if self._host_starting:
            draw_status_banner(
                surface,
                banner_rect or pygame.Rect(center_x - 210, height - 130, 420, 42),
                f"Starting secure LAN host on 0.0.0.0:{self.client.server_port}...",
                THEME["text_accent"],
                size=14,
            )

        if self.error_message:
            draw_status_banner(
                surface,
                banner_rect or pygame.Rect(center_x - 230, height - 130, 460, 48),
                self.error_message,
                THEME["dot_disconnected"],
                size=14,
            )
