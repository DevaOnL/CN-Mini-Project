"""Reusable pygame GUI widgets."""

from __future__ import annotations

import math

import pygame

from client.gui.theme import (
    THEME,
    blend_color,
    draw_soft_glow,
    draw_vertical_gradient,
    fit_text,
    get_font,
    with_alpha,
)


class Button:
    NORMAL = "normal"
    HOVER = "hover"
    PRESSED = "pressed"
    DISABLED = "disabled"

    def __init__(
        self,
        rect,
        label,
        on_click,
        disabled: bool = False,
        variant: str = "primary",
    ):
        self.rect = pygame.Rect(rect)
        self.label = label
        self.on_click = on_click
        self.disabled = disabled
        self.variant = variant
        self._pressed = False

    def _variant_style(self) -> dict[str, tuple[int, int, int]]:
        styles = {
            "primary": {
                "top": THEME["btn_hover"],
                "bottom": THEME["btn_bg"],
                "border": THEME["btn_border"],
                "glow": THEME["accent_primary"],
            },
            "secondary": {
                "top": THEME["panel_bg_highlight"],
                "bottom": THEME["panel_bg_alt"],
                "border": THEME["panel_border"],
                "glow": THEME["panel_glow"],
            },
            "danger": {
                "top": (120, 60, 67),
                "bottom": (84, 31, 40),
                "border": THEME["accent_danger"],
                "glow": THEME["accent_danger"],
            },
            "ghost": {
                "top": blend_color(THEME["panel_bg_alt"], THEME["bg_alt"], 0.35),
                "bottom": THEME["panel_bg"],
                "border": THEME["panel_border_soft"],
                "glow": THEME["accent_secondary"],
            },
        }
        return styles.get(self.variant, styles["primary"])

    def handle_event(self, event) -> bool:
        if self.disabled:
            return False

        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if self.rect.collidepoint(event.pos):
                self._pressed = True
                return False

        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            was_pressed = self._pressed
            self._pressed = False
            if was_pressed and self.rect.collidepoint(event.pos):
                self.on_click()
                return True

        return False

    def draw(self, surface: pygame.Surface):
        mouse_pos = pygame.mouse.get_pos()
        if self.disabled:
            state = self.DISABLED
        elif self._pressed and self.rect.collidepoint(mouse_pos):
            state = self.PRESSED
        elif self.rect.collidepoint(mouse_pos):
            state = self.HOVER
        else:
            state = self.NORMAL

        style = self._variant_style()
        text_color = THEME["btn_text"] if not self.disabled else THEME["btn_text_dis"]
        if state == self.DISABLED:
            top_color = THEME["btn_disabled"]
            bottom_color = blend_color(THEME["btn_disabled"], THEME["panel_shadow"], 0.35)
            border_color = THEME["panel_border_soft"]
        elif state == self.PRESSED:
            top_color = blend_color(style["top"], THEME["panel_shadow"], 0.28)
            bottom_color = blend_color(style["bottom"], THEME["panel_shadow"], 0.36)
            border_color = style["border"]
        elif state == self.HOVER:
            top_color = blend_color(style["top"], THEME["text"], 0.08)
            bottom_color = blend_color(style["bottom"], THEME["text"], 0.03)
            border_color = blend_color(style["border"], THEME["text"], 0.12)
        else:
            top_color = style["top"]
            bottom_color = style["bottom"]
            border_color = style["border"]

        shadow_rect = self.rect.move(0, 5)
        pygame.draw.rect(surface, THEME["panel_shadow"], shadow_rect, border_radius=14)
        if state in (self.HOVER, self.PRESSED):
            draw_soft_glow(
                surface,
                self.rect,
                style["glow"],
                alpha=28 if state == self.HOVER else 18,
                inflate_x=24,
                inflate_y=18,
                border_radius=18,
            )
        draw_vertical_gradient(
            surface,
            self.rect,
            top_color,
            bottom_color,
            border_radius=14,
        )
        pygame.draw.rect(surface, border_color, self.rect, width=1, border_radius=14)
        highlight_rect = pygame.Rect(
            self.rect.x + 2, self.rect.y + 2, self.rect.width - 4, 13
        )
        gloss = pygame.Surface(
            (highlight_rect.width, highlight_rect.height), pygame.SRCALPHA
        )
        gloss.fill(with_alpha(THEME["text"], 22 if not self.disabled else 6))
        surface.blit(gloss, highlight_rect.topleft)
        accent_rect = pygame.Rect(
            self.rect.x + 8,
            self.rect.y + 8,
            4,
            max(8, self.rect.height - 16),
        )
        pygame.draw.rect(
            surface,
            style["glow"] if not self.disabled else THEME["panel_border_soft"],
            accent_rect,
            border_radius=3,
        )

        text_bounds = pygame.Rect(
            self.rect.x + 18,
            self.rect.y + 6,
            self.rect.width - 32,
            self.rect.height - 12,
        )
        font, fitted_label = fit_text(
            self.label,
            max_width=max(1, text_bounds.width),
            size=18,
            min_size=13,
            bold=True,
            family="display",
        )
        text = font.render(fitted_label, True, text_color)
        text_rect = text.get_rect(
            center=(
                self.rect.centerx + 6,
                self.rect.centery - (1 if state == self.PRESSED else 0),
            )
        )
        previous_clip = surface.get_clip()
        surface.set_clip(text_bounds)
        surface.blit(text, text_rect)
        surface.set_clip(previous_clip)


class TextInput:
    def __init__(self, rect, placeholder: str = "", max_len: int = 64):
        self.rect = pygame.Rect(rect)
        self.placeholder = placeholder
        self.max_len = max_len
        self.text = ""
        self.focused = False
        self.cursor_pos = 0
        self._pending_textinput_echo = ""

    def set_text(self, value: str):
        self.text = str(value)[: self.max_len]
        self.cursor_pos = min(len(self.text), self.cursor_pos or len(self.text))

    def focus(self, cursor_pos: int | None = None):
        self.focused = True
        self.cursor_pos = len(self.text) if cursor_pos is None else cursor_pos
        self.cursor_pos = max(0, min(len(self.text), self.cursor_pos))
        pygame.key.start_text_input()

    def blur(self):
        self.focused = False
        self._pending_textinput_echo = ""
        pygame.key.stop_text_input()

    def handle_event(self, event):
        font = get_font(16)
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if self.rect.collidepoint(event.pos):
                self.focus()
                relative_x = max(0, event.pos[0] - self.rect.x - 6)
                self.cursor_pos = len(self.text)
                for index in range(len(self.text) + 1):
                    prefix_width = font.render(
                        self.text[:index], True, THEME["text"]
                    ).get_width()
                    if prefix_width >= relative_x:
                        self.cursor_pos = index
                        break
            else:
                self.blur()

        if event.type == pygame.TEXTINPUT and self.focused:
            if self._pending_textinput_echo and event.text == self._pending_textinput_echo:
                self._pending_textinput_echo = ""
                return
            self._insert_text(event.text)
            return

        if event.type != pygame.KEYDOWN or not self.focused:
            return

        if event.key == pygame.K_BACKSPACE:
            if self.cursor_pos > 0:
                self.text = (
                    self.text[: self.cursor_pos - 1] + self.text[self.cursor_pos :]
                )
                self.cursor_pos -= 1
        elif event.key == pygame.K_DELETE:
            if self.cursor_pos < len(self.text):
                self.text = (
                    self.text[: self.cursor_pos] + self.text[self.cursor_pos + 1 :]
                )
        elif event.key == pygame.K_LEFT:
            self.cursor_pos = max(0, self.cursor_pos - 1)
        elif event.key == pygame.K_RIGHT:
            self.cursor_pos = min(len(self.text), self.cursor_pos + 1)
        elif event.key == pygame.K_HOME:
            self.cursor_pos = 0
        elif event.key == pygame.K_END:
            self.cursor_pos = len(self.text)
        elif (
            event.unicode
            and event.unicode.isprintable()
            and not (event.mod & (pygame.KMOD_CTRL | pygame.KMOD_ALT | pygame.KMOD_META))
        ):
            # Some Linux/SDL locale setups emit KEYDOWN unicode but never deliver
            # TEXTINPUT events, so keep a direct-key fallback for simple text fields.
            self._pending_textinput_echo = event.unicode
            self._insert_text(event.unicode)

    def _insert_text(self, text: str):
        if not text or len(self.text) >= self.max_len:
            return
        available = self.max_len - len(self.text)
        inserted = text[:available]
        self.text = (
            self.text[: self.cursor_pos] + inserted + self.text[self.cursor_pos :]
        )
        self.cursor_pos = min(len(self.text), self.cursor_pos + len(inserted))

    def draw(self, surface: pygame.Surface):
        border_color = THEME["input_focused"] if self.focused else THEME["input_border"]
        shadow_rect = self.rect.move(0, 3)
        pygame.draw.rect(surface, THEME["panel_shadow"], shadow_rect, border_radius=10)
        if self.focused:
            draw_soft_glow(
                surface,
                self.rect,
                THEME["input_focused"],
                alpha=24,
                inflate_x=18,
                inflate_y=14,
                border_radius=14,
            )
        draw_vertical_gradient(
            surface,
            self.rect,
            blend_color(THEME["input_bg"], THEME["panel_bg_highlight"], 0.28),
            THEME["input_bg"],
            border_radius=10,
        )
        pygame.draw.rect(surface, border_color, self.rect, width=1, border_radius=10)
        pygame.draw.line(
            surface,
            with_alpha(THEME["text"], 24),
            (self.rect.x + 10, self.rect.y + 8),
            (self.rect.right - 10, self.rect.y + 8),
            1,
        )

        display_text = self.text
        color = THEME["text"]
        if not self.text and not self.focused:
            display_text = self.placeholder
            color = THEME["text_muted"]
        text_surface = get_font(16).render(display_text, True, color)
        clip_rect = self.rect.inflate(-14, -10)
        prefix_width = (
            get_font(16).render(self.text[: self.cursor_pos], True, color).get_width()
        )
        max_width = max(1, self.rect.width - 24)
        scroll_x = max(0, prefix_width - max_width)

        previous_clip = surface.get_clip()
        surface.set_clip(clip_rect)
        text_pos = (
            self.rect.x + 8 - scroll_x,
            self.rect.y + (self.rect.height - text_surface.get_height()) // 2,
        )
        surface.blit(text_surface, text_pos)

        if self.focused and (pygame.time.get_ticks() // 500) % 2 == 0:
            cursor_x = self.rect.x + 6 + prefix_width - scroll_x
            cursor_y = self.rect.y + 6
            pygame.draw.line(
                surface,
                THEME["cursor"],
                (cursor_x, cursor_y),
                (cursor_x, self.rect.bottom - 6),
                1,
            )

        surface.set_clip(previous_clip)


class Label:
    def __init__(
        self,
        rect,
        text,
        color_key: str = "text",
        bold: bool = False,
        size: int = 14,
        align: str = "left",
        family: str = "sans",
    ):
        self.rect = pygame.Rect(rect)
        self.text = text
        self.color_key = color_key
        self.bold = bold
        self.size = size
        self.align = align
        self.family = family

    def draw(self, surface: pygame.Surface):
        font, fitted_text = fit_text(
            str(self.text),
            max_width=max(1, self.rect.width),
            size=self.size,
            min_size=max(10, self.size - 4),
            bold=self.bold,
            family=self.family,
        )
        text_surface = font.render(fitted_text, True, THEME[self.color_key])
        text_rect = text_surface.get_rect()
        if self.align == "center":
            text_rect.center = self.rect.center
        elif self.align == "right":
            text_rect.midright = self.rect.midright
        else:
            text_rect.midleft = self.rect.midleft
        previous_clip = surface.get_clip()
        surface.set_clip(self.rect)
        surface.blit(text_surface, text_rect)
        surface.set_clip(previous_clip)


class Panel:
    def __init__(
        self,
        rect,
        title: str | None = None,
        *,
        accent_key: str = "panel_glow",
    ):
        self.rect = pygame.Rect(rect)
        self.title = title
        self.accent_key = accent_key

    def draw(self, surface: pygame.Surface):
        accent = THEME.get(self.accent_key, THEME["panel_glow"])
        draw_soft_glow(
            surface,
            self.rect,
            accent,
            alpha=18,
            inflate_x=24,
            inflate_y=22,
            border_radius=22,
        )
        shadow_rect = self.rect.move(0, 6)
        pygame.draw.rect(surface, THEME["panel_shadow"], shadow_rect, border_radius=18)
        draw_vertical_gradient(
            surface,
            self.rect,
            blend_color(THEME["panel_bg_highlight"], accent, 0.08),
            THEME["panel_bg"],
            border_radius=18,
        )
        pygame.draw.rect(surface, THEME["panel_border"], self.rect, width=1, border_radius=18)
        inner_rect = self.rect.inflate(-2, -2)
        pygame.draw.rect(
            surface,
            THEME["panel_border_soft"],
            inner_rect,
            width=1,
            border_radius=16,
        )
        if self.title:
            title_font, title_text = fit_text(
                self.title,
                max_width=max(1, self.rect.width - 64),
                size=15,
                min_size=11,
                bold=True,
                family="display",
            )
            title = title_font.render(title_text, True, THEME["text_accent"])
            pill_width = min(self.rect.width - 36, title.get_width() + 34)
            title_bar = pygame.Rect(self.rect.x + 18, self.rect.y + 14, pill_width, 30)
            draw_vertical_gradient(
                surface,
                title_bar,
                blend_color(THEME["panel_header"], accent, 0.18),
                THEME["panel_bg_alt"],
                border_radius=15,
            )
            pygame.draw.rect(
                surface,
                blend_color(THEME["panel_border"], accent, 0.25),
                title_bar,
                width=1,
                border_radius=15,
            )
            previous_clip = surface.get_clip()
            surface.set_clip(title_bar.inflate(-12, -4))
            surface.blit(title, title.get_rect(center=title_bar.center))
            surface.set_clip(previous_clip)
            divider_y = title_bar.centery
            pygame.draw.line(
                surface,
                with_alpha(accent, 88),
                (title_bar.right + 10, divider_y),
                (self.rect.right - 18, divider_y),
                2,
            )


class StatusDot:
    def __init__(self, center, radius: int = 7):
        self.center = center
        self.radius = radius

    def draw(self, surface: pygame.Surface, state_str: str):
        color = {
            "connected": THEME["dot_connected"],
            "connecting": THEME["dot_connecting"],
            "reconnecting": THEME["dot_reconnecting"],
            "disconnected": THEME["dot_disconnected"],
        }.get(state_str, THEME["dot_disconnected"])
        if state_str in ("connecting", "reconnecting"):
            pulse = (math.sin(pygame.time.get_ticks() / 500.0 * math.pi) + 1) / 2
            alpha = int(120 + pulse * 135)
            dot_surface = pygame.Surface(
                (self.radius * 2, self.radius * 2), pygame.SRCALPHA
            )
            pygame.draw.circle(
                dot_surface,
                (*color, alpha),
                (self.radius, self.radius),
                self.radius,
            )
            surface.blit(
                dot_surface,
                (self.center[0] - self.radius, self.center[1] - self.radius),
            )
        else:
            pygame.draw.circle(surface, color, self.center, self.radius)


class ScrollList:
    def __init__(self, rect, items: list[str] | None = None, item_height: int = 28):
        self.rect = pygame.Rect(rect)
        self.items = items or []
        self.item_height = item_height
        self.scroll_offset = 0
        self.selected_index: int | None = None

    def set_items(self, items: list[str]):
        self.items = items
        max_offset = max(0, len(items) * self.item_height - self.rect.height)
        self.scroll_offset = max(0, min(self.scroll_offset, max_offset))
        if self.selected_index is not None and self.selected_index >= len(items):
            self.selected_index = None
        if self.selected_index is not None:
            self.ensure_visible(self.selected_index)

    def ensure_visible(self, index: int):
        if not 0 <= index < len(self.items):
            return
        item_top = index * self.item_height
        item_bottom = item_top + self.item_height
        view_top = self.scroll_offset
        view_bottom = self.scroll_offset + self.rect.height
        max_offset = max(0, len(self.items) * self.item_height - self.rect.height)
        if item_top < view_top:
            self.scroll_offset = item_top
        elif item_bottom > view_bottom:
            self.scroll_offset = item_bottom - self.rect.height
        self.scroll_offset = max(0, min(self.scroll_offset, max_offset))

    def select_index(self, index: int | None):
        if index is None or not self.items:
            self.selected_index = None
            return
        self.selected_index = max(0, min(len(self.items) - 1, index))
        self.ensure_visible(self.selected_index)

    def move_selection(self, step: int):
        if not self.items:
            self.selected_index = None
            return
        if self.selected_index is None:
            next_index = 0 if step >= 0 else len(self.items) - 1
        else:
            next_index = self.selected_index + step
        self.select_index(next_index)

    def handle_event(self, event):
        if event.type == pygame.MOUSEWHEEL:
            if self.rect.collidepoint(pygame.mouse.get_pos()):
                max_offset = max(
                    0, len(self.items) * self.item_height - self.rect.height
                )
                self.scroll_offset = max(
                    0, min(max_offset, self.scroll_offset - event.y * self.item_height)
                )

        if (
            event.type == pygame.MOUSEBUTTONDOWN
            and event.button == 1
            and self.rect.collidepoint(event.pos)
        ):
            index = (
                event.pos[1] - self.rect.y + self.scroll_offset
            ) // self.item_height
            if 0 <= index < len(self.items):
                self.select_index(index)

    def visible_entries(self) -> list[tuple[int, pygame.Rect, str]]:
        entries = []
        start_index = self.scroll_offset // self.item_height
        y_offset = -(self.scroll_offset % self.item_height)

        index = start_index
        while index < len(self.items) and y_offset < self.rect.height:
            item_rect = pygame.Rect(
                self.rect.x, self.rect.y + y_offset, self.rect.width, self.item_height
            )
            entries.append((index, item_rect, self.items[index]))
            y_offset += self.item_height
            index += 1

        return entries

    def draw(self, surface: pygame.Surface):
        previous_clip = surface.get_clip()
        surface.set_clip(self.rect)
        mouse_pos = pygame.mouse.get_pos()
        for index, item_rect, item in self.visible_entries():
            row_rect = item_rect.inflate(-4, -4)
            is_selected = index == self.selected_index
            is_hovered = row_rect.collidepoint(mouse_pos)
            if is_selected:
                draw_vertical_gradient(
                    surface,
                    row_rect,
                    blend_color(THEME["panel_bg_highlight"], THEME["accent_primary"], 0.12),
                    THEME["panel_bg_alt"],
                    border_radius=10,
                )
                pygame.draw.rect(
                    surface,
                    THEME["panel_border"],
                    row_rect,
                    width=1,
                    border_radius=10,
                )
            else:
                color = THEME["panel_bg_alt"] if is_hovered else THEME["panel_bg"]
                pygame.draw.rect(surface, color, row_rect, border_radius=10)
                pygame.draw.rect(
                    surface,
                    THEME["panel_border_soft"],
                    row_rect,
                    width=1,
                    border_radius=10,
                )
            if index == self.selected_index:
                pygame.draw.rect(
                    surface,
                    THEME["text_accent"],
                    (row_rect.x + 8, row_rect.y + 6, 4, row_rect.height - 12),
                    border_radius=2,
                )
            text_font, fitted_item = fit_text(
                item,
                max_width=max(1, row_rect.width - 38),
                size=15,
                min_size=11,
            )
            text = text_font.render(fitted_item, True, THEME["text"])
            surface.blit(
                text,
                (
                    item_rect.x + 28,
                    item_rect.y + (item_rect.height - text.get_height()) // 2,
                ),
            )
        surface.set_clip(previous_clip)
        pygame.draw.rect(surface, THEME["panel_border"], self.rect, width=1, border_radius=10)
