"""Join dialog scene for entering a remote server address."""

from __future__ import annotations

import pygame

from client.gui.scene_manager import BaseScene
from client.gui.theme import (
    THEME,
    draw_scene_background,
    draw_status_banner,
    draw_wrapped_text,
    get_font,
)
from client.gui.validation import is_valid_host
from client.gui.widgets import Button, Label, Panel, TextInput
from client.gui.scenes.settings import load_config


class JoinDialogScene(BaseScene):
    def __init__(self, manager, client):
        super().__init__(manager)
        self.client = client
        self.error_message = ""
        self.panel = Panel(
            (180, 120, 440, 370),
            accent_key="accent_secondary",
        )
        self.host_input = TextInput(
            (340, 205, 200, 34), placeholder="127.0.0.1", max_len=39
        )
        self.port_input = TextInput((340, 265, 120, 34), placeholder="9000", max_len=5)
        self.room_key_input = TextInput(
            (340, 325, 200, 34),
            placeholder="shared room key",
            max_len=64,
        )
        self.connect_button = Button(
            (245, 390, 120, 40),
            "CONNECT",
            self._connect,
            variant="primary",
        )
        self.back_button = Button(
            (435, 390, 120, 40),
            "BACK",
            self._back,
            variant="ghost",
        )

    def _ordered_fields(self) -> list[TextInput]:
        return [self.host_input, self.port_input, self.room_key_input]

    def _blur_all_fields(self):
        for field in self._ordered_fields():
            field.blur()

    def _cycle_focus(self, reverse: bool = False):
        fields = self._ordered_fields()
        focused_index = next(
            (index for index, field in enumerate(fields) if field.focused),
            -1,
        )
        if focused_index >= 0:
            fields[focused_index].blur()
        next_index = (focused_index + (-1 if reverse else 1)) % len(fields)
        fields[next_index].focus(len(fields[next_index].text))

    def _layout(self, surface: pygame.Surface):
        width, height = surface.get_size()
        self.panel.rect.size = (min(460, width - 120), 370)
        self.panel.rect.center = (width // 2, height // 2)
        self.host_input.rect.topleft = (self.panel.rect.x + 160, self.panel.rect.y + 74)
        self.host_input.rect.size = (self.panel.rect.width - 200, 36)
        self.port_input.rect.topleft = (
            self.panel.rect.x + 160,
            self.panel.rect.y + 122,
        )
        self.port_input.rect.size = (140, 36)
        self.room_key_input.rect.topleft = (
            self.panel.rect.x + 160,
            self.panel.rect.y + 170,
        )
        self.room_key_input.rect.size = (self.panel.rect.width - 200, 36)
        self.connect_button.rect.topleft = (
            self.panel.rect.x + 64,
            self.panel.rect.bottom - 56,
        )
        self.connect_button.rect.size = (140, 42)
        self.back_button.rect.topright = (
            self.panel.rect.right - 64,
            self.panel.rect.bottom - 56,
        )
        self.back_button.rect.size = (140, 42)

    def _hint_rect(self) -> pygame.Rect:
        return pygame.Rect(
            self.panel.rect.x + 28,
            self.panel.rect.bottom - 148,
            self.panel.rect.width - 56,
            30,
        )

    def _error_banner_rect(self) -> pygame.Rect:
        return pygame.Rect(
            self.panel.rect.x + 28,
            self.panel.rect.bottom - 108,
            self.panel.rect.width - 56,
            40,
        )

    def on_enter(self):
        config = load_config()
        default_host = str(config.get("host", "127.0.0.1")).strip() or "127.0.0.1"
        default_port = str(config.get("port", 9000))

        self._blur_all_fields()
        self.host_input.set_text(default_host)
        self.port_input.set_text(default_port)
        self.room_key_input.set_text(getattr(self.client, "room_key", ""))
        self.host_input.focus(len(self.host_input.text))

        self.error_message = ""

    def on_pause(self):
        self._blur_all_fields()

    def on_exit(self):
        self._blur_all_fields()

    def _connect(self):
        host = self.host_input.text.strip()
        port_text = self.port_input.text.strip() or self.port_input.placeholder

        if not host:
            self.error_message = "Server IP cannot be empty."
            return

        if not is_valid_host(host):
            self.error_message = "Enter a valid IP address or hostname."
            return

        try:
            port = int(port_text)
        except ValueError:
            self.error_message = "Port must be an integer."
            return

        if not 1024 <= port <= 65535:
            self.error_message = "Port must be in the range 1024-65535."
            return

        room_key = self.room_key_input.text.strip()
        if not room_key:
            self.error_message = "Room key is required."
            return

        self.error_message = ""
        self._blur_all_fields()
        self.client.begin_new_session()
        if hasattr(self.client, "set_room_key"):
            self.client.set_room_key(room_key)
        else:
            self.client.room_key = room_key
        self.client.server_host = host
        self.client.server_port = port
        self.client.server_addr = (host, port)

        from client.gui.scenes.lobby import LobbyScene

        self.mgr.push(LobbyScene(self.mgr, client=self.client, host=False))

    def _back(self):
        self._blur_all_fields()
        self.mgr.pop()

    def handle_event(self, event):
        self._layout(self.mgr.screen)
        if event.type == pygame.KEYDOWN and event.key == pygame.K_TAB:
            self._cycle_focus(reverse=bool(event.mod & pygame.KMOD_SHIFT))
            return
        if event.type == pygame.KEYDOWN and event.key == pygame.K_RETURN:
            self._blur_all_fields()
            self._connect()
            return
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self._back()
            return
        self.host_input.handle_event(event)
        self.port_input.handle_event(event)
        self.room_key_input.handle_event(event)
        self.connect_button.handle_event(event)
        self.back_button.handle_event(event)

    def draw(self, surface: pygame.Surface):
        self._layout(surface)
        width, _height = surface.get_size()
        draw_scene_background(surface, accent=(28, 58, 92))
        self.panel.draw(surface)

        title = get_font(28, bold=True, family="display").render(
            "JOIN GAME", True, THEME["text"]
        )
        surface.blit(title, title.get_rect(center=(width // 2, self.panel.rect.y + 40)))

        Label(
            (self.panel.rect.x + 36, self.panel.rect.y + 92, 110, 20),
            "Host",
            size=16,
        ).draw(surface)
        self.host_input.draw(surface)

        Label(
            (self.panel.rect.x + 36, self.panel.rect.y + 140, 110, 20),
            "Port",
            size=16,
        ).draw(surface)
        self.port_input.draw(surface)

        Label(
            (self.panel.rect.x + 36, self.panel.rect.y + 188, 110, 20),
            "Room Key",
            size=16,
        ).draw(surface)
        self.room_key_input.draw(surface)

        self.connect_button.draw(surface)
        self.back_button.draw(surface)

        draw_wrapped_text(
            surface,
            "Use 127.0.0.1 for the same PC. The room key is checked after the DTLS handshake.",
            self._hint_rect(),
            THEME["text_dim"],
            size=13,
            align="center",
        )

        if self.error_message:
            draw_status_banner(
                surface,
                self._error_banner_rect(),
                self.error_message,
                THEME["dot_disconnected"],
                size=14,
            )
