"""Settings scene and config persistence."""

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
from client.gui import config_store as _config_store
from client.gui.widgets import Button, Label, Panel, TextInput

CONFIG_PATH = _config_store.CONFIG_PATH
DEFAULT_CONFIG = _config_store.DEFAULT_CONFIG


def load_config() -> dict:
    _config_store.CONFIG_PATH = CONFIG_PATH
    return _config_store.load_config()


def save_config(cfg: dict):
    _config_store.CONFIG_PATH = CONFIG_PATH
    _config_store.save_config(cfg)


def normalize_config(cfg: dict | None) -> dict:
    return _config_store.normalize_config(cfg)


class SettingsScene(BaseScene):
    def __init__(self, manager, client):
        super().__init__(manager)
        self.client = client
        self.error_message = ""
        self.panel = Panel(
            (140, 60, 520, 480),
            title="SETTINGS",
            accent_key="accent_primary",
        )
        self.fields = {
            "host": TextInput((340, 140, 220, 32), placeholder="127.0.0.1", max_len=39),
            "port": TextInput((340, 190, 180, 32), placeholder="9000", max_len=5),
            "name": TextInput((340, 240, 180, 32), placeholder="Player", max_len=16),
            "fps": TextInput((340, 290, 180, 32), placeholder="60", max_len=3),
            "interp_ms": TextInput((340, 340, 180, 32), placeholder="100", max_len=3),
        }
        self.show_debug = True
        self.save_button = Button(
            (220, 470, 120, 40),
            "SAVE",
            self._save,
            variant="primary",
        )
        self.cancel_button = Button(
            (360, 470, 120, 40),
            "CANCEL",
            self._cancel,
            variant="ghost",
        )
        self.debug_button = Button(
            (340, 390, 180, 32),
            "ON",
            self._toggle_debug,
            variant="primary",
        )

    def _ordered_fields(self) -> list[TextInput]:
        return list(self.fields.values())

    def _blur_all_fields(self):
        for field in self._ordered_fields():
            field.blur()

    def _cycle_focus(self, reverse: bool = False):
        ordered_fields = self._ordered_fields()
        focused_index = next(
            (index for index, field in enumerate(ordered_fields) if field.focused),
            -1,
        )
        if focused_index >= 0:
            ordered_fields[focused_index].blur()
        next_index = (focused_index + (-1 if reverse else 1)) % len(ordered_fields)
        ordered_fields[next_index].focus(len(ordered_fields[next_index].text))

    def _info_message(self) -> str:
        if self.client.conn_state.name != "DISCONNECTED":
            return (
                "Host and port apply next session. Name, FPS, buffer, and debug update now."
            )
        return "Saved locally for new sessions."

    def _footer_layout(self) -> dict[str, pygame.Rect]:
        footer_bottom = self.save_button.rect.top - 14
        footer_layout: dict[str, pygame.Rect] = {}
        if self.error_message:
            footer_layout["error"] = pygame.Rect(
                self.panel.rect.x + 36,
                footer_bottom - 46,
                self.panel.rect.width - 72,
                46,
            )
            footer_bottom = footer_layout["error"].top - 10
        footer_layout["info"] = pygame.Rect(
            self.panel.rect.x + 36,
            footer_bottom - 34,
            self.panel.rect.width - 72,
            34,
        )
        return footer_layout

    def _layout(self, surface: pygame.Surface):
        width, height = surface.get_size()
        panel_width = min(width - 24, max(420, min(640, width - 40)))
        panel_height = min(height - 24, max(500, min(560, height - 40)))
        self.panel.rect = pygame.Rect(0, 0, panel_width, panel_height)
        self.panel.rect.center = (width // 2, height // 2)

        label_x = self.panel.rect.x + 34
        label_width = min(180, max(120, self.panel.rect.width // 3))
        field_x = label_x + label_width + 18
        field_width = self.panel.rect.right - field_x - 34
        row_start = self.panel.rect.y + 92
        row_gap = 48
        input_height = 36

        self.fields["host"].rect = pygame.Rect(
            field_x, row_start, field_width, input_height
        )
        self.fields["port"].rect = pygame.Rect(
            field_x, row_start + row_gap, field_width, input_height
        )
        self.fields["name"].rect = pygame.Rect(
            field_x, row_start + row_gap * 2, field_width, input_height
        )
        self.fields["fps"].rect = pygame.Rect(
            field_x, row_start + row_gap * 3, field_width, input_height
        )
        self.fields["interp_ms"].rect = pygame.Rect(
            field_x, row_start + row_gap * 4, field_width, input_height
        )
        self.debug_button.rect = pygame.Rect(
            field_x, row_start + row_gap * 5, field_width, input_height
        )

        button_width = 150
        button_gap = 20
        buttons_y = self.panel.rect.bottom - 62
        total_buttons_width = button_width * 2 + button_gap
        buttons_x = self.panel.rect.centerx - total_buttons_width // 2
        self.save_button.rect = pygame.Rect(buttons_x, buttons_y, button_width, 44)
        self.cancel_button.rect = pygame.Rect(
            buttons_x + button_width + button_gap,
            buttons_y,
            button_width,
            44,
        )

    def on_enter(self):
        config = load_config()
        self._blur_all_fields()
        self.fields["host"].set_text(str(config.get("host", DEFAULT_CONFIG["host"])))
        self.fields["port"].set_text(str(config.get("port", DEFAULT_CONFIG["port"])))
        self.fields["name"].set_text(str(config.get("name", DEFAULT_CONFIG["name"])))
        self.fields["fps"].set_text(str(config.get("fps", DEFAULT_CONFIG["fps"])))
        self.fields["interp_ms"].set_text(
            str(config.get("interp_ms", DEFAULT_CONFIG["interp_ms"]))
        )
        self.show_debug = bool(config.get("show_debug", True))
        self._sync_debug_button()
        self.error_message = ""
        self.fields["host"].focus(len(self.fields["host"].text))

    def on_pause(self):
        self._blur_all_fields()

    def on_exit(self):
        self._blur_all_fields()

    def _sync_debug_button(self):
        self.debug_button.label = "ON" if self.show_debug else "OFF"
        self.debug_button.variant = "primary" if self.show_debug else "ghost"

    def _toggle_debug(self):
        self.show_debug = not self.show_debug
        self._sync_debug_button()

    def _validate(self) -> dict | None:
        host = self.fields["host"].text.strip()
        try:
            port = int(self.fields["port"].text or self.fields["port"].placeholder)
            fps = int(self.fields["fps"].text or self.fields["fps"].placeholder)
            interp_ms = int(
                self.fields["interp_ms"].text or self.fields["interp_ms"].placeholder
            )
        except ValueError:
            self.error_message = "Port, FPS, and interp buffer must be integers."
            return None

        if not host:
            self.error_message = "Default server IP cannot be empty."
            return None
        if not is_valid_host(host):
            self.error_message = "Enter a valid IP address or hostname."
            return None

        if not 1024 <= port <= 65535:
            self.error_message = "Server port must be in the range 1024-65535."
            return None
        if not 10 <= fps <= 240:
            self.error_message = "Target FPS must be in the range 10-240."
            return None
        if not 0 <= interp_ms <= 500:
            self.error_message = "Interp buffer must be in the range 0-500 ms."
            return None

        self.error_message = ""
        return {
            "host": host,
            "port": port,
            "name": (self.fields["name"].text or self.fields["name"].placeholder)[:16]
            or "Player",
            "fps": fps,
            "interp_ms": interp_ms,
            "show_debug": self.show_debug,
        }

    def _save(self):
        config = self._validate()
        if config is None:
            return
        self._blur_all_fields()
        try:
            save_config(config)
        except OSError as exc:
            self.error_message = f"Could not save settings: {exc.strerror or exc}"
            return
        self.client.apply_settings(
            config,
            update_connection=self.client.conn_state.name == "DISCONNECTED",
        )
        self.mgr.pop()

    def _cancel(self):
        self._blur_all_fields()
        self.mgr.pop()

    def handle_event(self, event):
        self._layout(self.mgr.screen)
        if event.type == pygame.KEYDOWN and event.key == pygame.K_TAB:
            self._cycle_focus(reverse=bool(event.mod & pygame.KMOD_SHIFT))
            return
        if event.type == pygame.KEYDOWN and event.mod & pygame.KMOD_CTRL:
            if event.key == pygame.K_s:
                self._blur_all_fields()
                self._save()
                return
        if event.type == pygame.KEYDOWN and event.key == pygame.K_RETURN:
            self._blur_all_fields()
            self._save()
            return
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self._cancel()
            return

        for field in self._ordered_fields():
            field.handle_event(event)
        self.save_button.handle_event(event)
        self.cancel_button.handle_event(event)
        self.debug_button.handle_event(event)

    def draw(self, surface: pygame.Surface):
        self._layout(surface)
        width, _height = surface.get_size()
        draw_scene_background(surface, accent=(36, 60, 90))
        self.panel.draw(surface)

        title = get_font(30, bold=True, family="display").render(
            "SETTINGS", True, THEME["text"]
        )
        surface.blit(title, title.get_rect(center=(width // 2, self.panel.rect.y + 34)))

        rows = [
            ("Default Server Host", "host", self.fields["host"].rect.y),
            ("Server Port", "port", self.fields["port"].rect.y),
            ("Local Name", "name", self.fields["name"].rect.y),
            ("Target FPS", "fps", self.fields["fps"].rect.y),
            ("Interp Buffer (ms)", "interp_ms", self.fields["interp_ms"].rect.y),
        ]
        for label_text, key, y_pos in rows:
            Label(
                (
                    self.panel.rect.x + 34,
                    y_pos + self.fields[key].rect.height // 2,
                    180,
                    20,
                ),
                label_text,
                size=16,
            ).draw(surface)
            self.fields[key].draw(surface)

        Label(
            (
                self.panel.rect.x + 34,
                self.debug_button.rect.y + self.debug_button.rect.height // 2,
                180,
                20,
            ),
            "Show Debug Stats",
            size=16,
        ).draw(surface)
        self.debug_button.draw(surface)
        self.save_button.draw(surface)
        self.cancel_button.draw(surface)

        footer_layout = self._footer_layout()
        draw_wrapped_text(
            surface,
            self._info_message(),
            footer_layout["info"],
            THEME["text_dim"],
            size=13,
            align="center",
        )

        if self.error_message:
            draw_status_banner(
                surface,
                footer_layout["error"],
                self.error_message,
                THEME["dot_disconnected"],
                size=14,
            )
