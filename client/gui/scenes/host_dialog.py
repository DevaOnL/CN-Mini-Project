"""Host dialog scene for collecting the in-memory room key."""

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
from client.gui.widgets import Button, Label, Panel, TextInput


class HostDialogScene(BaseScene):
    def __init__(self, manager, client, on_submit):
        super().__init__(manager)
        self.client = client
        self.on_submit = on_submit
        self.error_message = ""
        self.panel = Panel(
            (180, 120, 440, 320),
            title="HOST GAME",
            accent_key="accent_primary",
        )
        self.room_key_input = TextInput(
            (340, 225, 220, 34),
            placeholder="shared room key",
            max_len=64,
        )
        self.start_button = Button(
            (245, 350, 140, 40),
            "START HOST",
            self._start_host,
            variant="primary",
        )
        self.back_button = Button(
            (415, 350, 140, 40),
            "BACK",
            self._back,
            variant="ghost",
        )

    def _blur_all_fields(self):
        self.room_key_input.blur()

    def _layout(self, surface: pygame.Surface):
        width, height = surface.get_size()
        self.panel.rect.size = (min(470, width - 120), 330)
        self.panel.rect.center = (width // 2, height // 2)
        self.room_key_input.rect.topleft = (
            self.panel.rect.x + 160,
            self.panel.rect.y + 108,
        )
        self.room_key_input.rect.size = (self.panel.rect.width - 200, 36)
        self.start_button.rect.topleft = (
            self.panel.rect.x + 64,
            self.panel.rect.bottom - 74,
        )
        self.start_button.rect.size = (140, 42)
        self.back_button.rect.topright = (
            self.panel.rect.right - 64,
            self.panel.rect.bottom - 74,
        )
        self.back_button.rect.size = (140, 42)

    def on_enter(self):
        self.error_message = ""
        self.room_key_input.set_text(getattr(self.client, "room_key", ""))
        self.room_key_input.focus(len(self.room_key_input.text))

    def on_pause(self):
        self._blur_all_fields()

    def on_exit(self):
        self._blur_all_fields()

    def _start_host(self):
        room_key = self.room_key_input.text.strip()
        if not room_key:
            self.error_message = "Room key is required."
            return

        self._blur_all_fields()
        error = self.on_submit(room_key)
        if error:
            self.error_message = error
            return
        self.mgr.pop()

    def _back(self):
        self._blur_all_fields()
        self.mgr.pop()

    def handle_event(self, event):
        self._layout(self.mgr.screen)
        if event.type == pygame.KEYDOWN and event.key == pygame.K_RETURN:
            self._start_host()
            return
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self._back()
            return
        self.room_key_input.handle_event(event)
        self.start_button.handle_event(event)
        self.back_button.handle_event(event)

    def draw(self, surface: pygame.Surface):
        self._layout(surface)
        width, _height = surface.get_size()
        lan_ip = detect_lan_ipv4("0.0.0.0")
        local_join = f"Same PC: 127.0.0.1:{self.client.server_port}"
        if lan_ip == "127.0.0.1":
            remote_join = "Other devices: no LAN IPv4 detected"
        else:
            remote_join = f"Other devices: {lan_ip}:{self.client.server_port}"

        draw_scene_background(surface, accent=(28, 58, 92))
        self.panel.draw(surface)

        title = get_font(28, bold=True, family="display").render(
            "HOST GAME", True, THEME["text"]
        )
        surface.blit(title, title.get_rect(center=(width // 2, self.panel.rect.y + 40)))

        Label(
            (self.panel.rect.x + 36, self.panel.rect.y + 126, 110, 20),
            "Room Key",
            size=16,
        ).draw(surface)
        self.room_key_input.draw(surface)

        self.start_button.draw(surface)
        self.back_button.draw(surface)

        draw_wrapped_text(
            surface,
            f"{local_join}. {remote_join}. Use the same room key and the DTLS fingerprint shown in the lobby.",
            pygame.Rect(
                self.panel.rect.x + 28,
                self.panel.rect.bottom - 146,
                self.panel.rect.width - 56,
                40,
            ),
            THEME["text_dim"],
            size=13,
            align="center",
        )

        if self.error_message:
            draw_status_banner(
                surface,
                pygame.Rect(
                    self.panel.rect.x + 28,
                    self.panel.rect.bottom - 116,
                    self.panel.rect.width - 56,
                    40,
                ),
                self.error_message,
                THEME["dot_disconnected"],
                size=14,
            )
