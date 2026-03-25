"""Shared GUI theme values and drawing helpers."""

from __future__ import annotations

import pygame

THEME = {
    "bg": (6, 12, 22),
    "bg_alt": (18, 42, 61),
    "bg_horizon": (23, 58, 79),
    "bg_glow": (69, 148, 190),
    "panel_bg": (14, 22, 34),
    "panel_bg_alt": (22, 33, 48),
    "panel_bg_highlight": (28, 46, 67),
    "panel_header": (27, 44, 63),
    "panel_border": (98, 136, 175),
    "panel_border_soft": (53, 73, 98),
    "panel_glow": (94, 176, 214),
    "panel_shadow": (3, 7, 15),
    "divider": (49, 67, 93),
    "accent_primary": (113, 220, 255),
    "accent_secondary": (122, 232, 181),
    "accent_warm": (255, 198, 113),
    "accent_danger": (255, 118, 118),
    "text": (238, 243, 249),
    "text_dim": (146, 168, 191),
    "text_muted": (111, 131, 154),
    "text_accent": (128, 223, 255),
    "text_gold": (255, 221, 124),
    "hud_label": (139, 160, 182),
    "hud_value": (242, 247, 252),
    "hud_row_bg": (16, 25, 39),
    "hud_row_border": (66, 89, 116),
    "hud_track": (23, 31, 45),
    "hud_hint_bg": (11, 18, 29),
    "success": (101, 226, 153),
    "warning": (244, 193, 88),
    "danger": (255, 106, 106),
    "btn_bg": (34, 57, 84),
    "btn_hover": (48, 75, 106),
    "btn_press": (23, 37, 55),
    "btn_disabled": (22, 29, 38),
    "btn_text": (236, 242, 248),
    "btn_text_dis": (98, 110, 128),
    "btn_border": (109, 154, 196),
    "dot_connected": (94, 226, 142),
    "dot_connecting": (244, 193, 88),
    "dot_reconnecting": (244, 146, 72),
    "dot_disconnected": (255, 106, 106),
    "input_bg": (13, 20, 30),
    "input_border": (72, 96, 129),
    "input_focused": (124, 207, 255),
    "cursor": (212, 228, 255),
}

_fonts = {}
_scene_background_cache: dict[
    tuple[tuple[int, int], tuple[int, int, int]], pygame.Surface
] = {}
_FONT_FAMILIES = {
    "display": "trebuchetms",
    "sans": "dejavusans",
    "mono": "dejavusansmono",
}


def get_font(size: int, bold: bool = False, family: str = "sans") -> pygame.font.Font:
    if not pygame.font.get_init():
        pygame.font.init()
    key = (size, bold, family)
    if key not in _fonts:
        _fonts[key] = pygame.font.SysFont(
            _FONT_FAMILIES.get(family, _FONT_FAMILIES["sans"]),
            size,
            bold=bold,
        )
    return _fonts[key]


def blend_color(
    start: tuple[int, int, int], end: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    return tuple(int(a + (b - a) * t) for a, b in zip(start, end))


def with_alpha(color: tuple[int, int, int], alpha: int) -> tuple[int, int, int, int]:
    return (*color, max(0, min(255, alpha)))


def draw_soft_glow(
    surface: pygame.Surface,
    rect: pygame.Rect,
    color: tuple[int, int, int],
    *,
    alpha: int = 28,
    inflate_x: int = 24,
    inflate_y: int = 20,
    border_radius: int = 20,
):
    glow_rect = rect.inflate(inflate_x, inflate_y)
    glow = pygame.Surface(glow_rect.size, pygame.SRCALPHA)
    pygame.draw.rect(
        glow,
        with_alpha(color, alpha),
        glow.get_rect(),
        border_radius=border_radius,
    )
    surface.blit(glow, glow_rect.topleft)


def draw_vertical_gradient(
    surface: pygame.Surface,
    rect: pygame.Rect,
    top_color: tuple[int, int, int],
    bottom_color: tuple[int, int, int],
    *,
    border_radius: int = 0,
):
    rect = pygame.Rect(rect)
    if rect.width <= 0 or rect.height <= 0:
        return

    gradient = pygame.Surface(rect.size, pygame.SRCALPHA)
    for y_pos in range(rect.height):
        t = y_pos / max(1, rect.height - 1)
        pygame.draw.line(
            gradient,
            blend_color(top_color, bottom_color, t),
            (0, y_pos),
            (rect.width, y_pos),
        )

    if border_radius > 0:
        mask = pygame.Surface(rect.size, pygame.SRCALPHA)
        pygame.draw.rect(mask, (255, 255, 255), mask.get_rect(), border_radius=border_radius)
        gradient.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    surface.blit(gradient, rect.topleft)


def _build_scene_background(
    size: tuple[int, int], accent: tuple[int, int, int]
) -> pygame.Surface:
    width, height = size
    background = pygame.Surface((width, height))
    top = THEME["bg"]
    mid = blend_color(THEME["bg_horizon"], accent, 0.3)
    bottom = blend_color(accent, THEME["bg_alt"], 0.65)
    for y_pos in range(height):
        t = y_pos / max(1, height - 1)
        if t < 0.58:
            color = blend_color(top, mid, t / 0.58)
        else:
            color = blend_color(mid, bottom, (t - 0.58) / 0.42)
        pygame.draw.line(
            background,
            color,
            (0, y_pos),
            (width, y_pos),
        )

    glow_surface = pygame.Surface((width, height), pygame.SRCALPHA)
    pygame.draw.circle(
        glow_surface,
        with_alpha(THEME["bg_glow"], 54),
        (int(width * 0.18), int(height * 0.22)),
        max(120, width // 5),
    )
    pygame.draw.circle(
        glow_surface,
        with_alpha(accent or THEME["panel_glow"], 38),
        (int(width * 0.84), int(height * 0.16)),
        max(110, width // 6),
    )
    pygame.draw.ellipse(
        glow_surface,
        with_alpha(THEME["accent_secondary"], 28),
        (
            int(width * 0.24),
            int(height * 0.68),
            int(width * 0.52),
            int(height * 0.3),
        ),
    )
    pygame.draw.ellipse(
        glow_surface,
        with_alpha(THEME["accent_warm"], 22),
        (
            int(width * 0.62),
            int(height * 0.58),
            int(width * 0.28),
            int(height * 0.18),
        ),
    )
    for x_pos in range(-height // 3, width, 88):
        pygame.draw.line(
            glow_surface,
            with_alpha(THEME["panel_border"], 12),
            (x_pos, 0),
            (x_pos + height // 2, height),
            1,
        )
    for y_pos in range(42, height, 74):
        pygame.draw.line(
            glow_surface,
            with_alpha(THEME["divider"], 18),
            (0, y_pos),
            (width, y_pos),
            1,
        )
    for index in range(max(18, width // 56)):
        dot_x = (index * 73 + width // 9) % max(1, width)
        dot_y = (index * 41 + height // 6) % max(1, height)
        radius = 1 + index % 2
        alpha = 36 + (index * 17) % 38
        pygame.draw.circle(
            glow_surface,
            with_alpha(THEME["text"], alpha),
            (dot_x, dot_y),
            radius,
        )
    pygame.draw.arc(
        glow_surface,
        with_alpha(THEME["text_accent"], 26),
        pygame.Rect(int(width * 0.04), int(height * 0.08), int(width * 0.56), int(height * 0.42)),
        0.2,
        2.65,
        2,
    )
    pygame.draw.arc(
        glow_surface,
        with_alpha(THEME["accent_warm"], 18),
        pygame.Rect(int(width * 0.5), int(height * 0.1), int(width * 0.38), int(height * 0.28)),
        3.45,
        5.7,
        2,
    )
    background.blit(glow_surface, (0, 0))
    return background


def draw_scene_background(
    surface: pygame.Surface, accent: tuple[int, int, int] | None = None
):
    key = (surface.get_size(), accent or THEME["bg_alt"])
    cached = _scene_background_cache.get(key)
    if cached is None:
        cached = _build_scene_background(*key)
        _scene_background_cache[key] = cached
    surface.blit(cached, (0, 0))


def draw_status_banner(
    surface: pygame.Surface,
    rect: pygame.Rect,
    text: str,
    color: tuple[int, int, int],
    *,
    size: int = 14,
):
    draw_soft_glow(surface, rect, color, alpha=24, inflate_x=24, inflate_y=20, border_radius=20)
    draw_vertical_gradient(
        surface,
        rect,
        blend_color(THEME["panel_bg_highlight"], color, 0.14),
        THEME["panel_bg_alt"],
        border_radius=16,
    )
    pygame.draw.rect(surface, color, rect, width=1, border_radius=16)
    pygame.draw.rect(
        surface,
        with_alpha(color, 42),
        (rect.x + 12, rect.y + 10, 10, rect.height - 20),
        border_radius=5,
    )
    draw_wrapped_text(
        surface,
        text,
        rect.inflate(-40, -10),
        color,
        size=size,
        align="center",
    )


def _fit_font_to_width(
    text: str,
    *,
    max_width: int,
    size: int,
    min_size: int = 11,
    bold: bool = False,
    family: str = "sans",
) -> pygame.font.Font:
    for candidate_size in range(size, min_size - 1, -1):
        font = get_font(candidate_size, bold=bold, family=family)
        if font.size(text)[0] <= max_width:
            return font
    return get_font(min_size, bold=bold, family=family)


def ellipsize_text(text: str, font: pygame.font.Font, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if font.size(text)[0] <= max_width:
        return text

    ellipsis = "..."
    if font.size(ellipsis)[0] > max_width:
        return ""

    lo = 0
    hi = len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = text[:mid].rstrip() + ellipsis
        if font.size(candidate)[0] <= max_width:
            lo = mid
        else:
            hi = mid - 1

    return text[:lo].rstrip() + ellipsis


def fit_text(
    text: str,
    *,
    max_width: int,
    size: int,
    min_size: int = 11,
    bold: bool = False,
    family: str = "sans",
) -> tuple[pygame.font.Font, str]:
    font = _fit_font_to_width(
        text,
        max_width=max_width,
        size=size,
        min_size=min_size,
        bold=bold,
        family=family,
    )
    return font, ellipsize_text(text, font, max_width)


def draw_hud_pill(
    surface: pygame.Surface,
    rect: pygame.Rect,
    text: str,
    *,
    accent: tuple[int, int, int] | None = None,
    text_color: tuple[int, int, int] | None = None,
    size: int = 13,
    family: str = "sans",
    bold: bool = True,
):
    rect = pygame.Rect(rect)
    accent = accent or THEME["text_accent"]
    text_color = text_color or THEME["hud_value"]
    radius = min(rect.height // 2, 16)
    draw_soft_glow(
        surface,
        rect,
        accent,
        alpha=14,
        inflate_x=18,
        inflate_y=14,
        border_radius=radius + 6,
    )
    draw_vertical_gradient(
        surface,
        rect,
        blend_color(THEME["panel_bg_highlight"], accent, 0.12),
        blend_color(THEME["hud_hint_bg"], THEME["panel_bg"], 0.42),
        border_radius=radius,
    )
    pygame.draw.rect(
        surface,
        blend_color(THEME["panel_border_soft"], accent, 0.28),
        rect,
        width=1,
        border_radius=radius,
    )
    font, fitted_text = fit_text(
        text,
        max_width=max(1, rect.width - 18),
        size=size,
        min_size=max(10, size - 3),
        bold=bold,
        family=family,
    )
    label = font.render(fitted_text, True, text_color)
    surface.blit(label, label.get_rect(center=rect.center))


def draw_hud_metric_card(
    surface: pygame.Surface,
    rect: pygame.Rect,
    label: str,
    value: str,
    *,
    accent: tuple[int, int, int] | None = None,
    value_family: str = "mono",
    value_size: int = 17,
    label_size: int = 10,
):
    rect = pygame.Rect(rect)
    accent = accent or THEME["hud_value"]
    radius = min(12, max(8, rect.height // 2))
    draw_vertical_gradient(
        surface,
        rect,
        blend_color(THEME["hud_row_bg"], accent, 0.08),
        THEME["panel_bg_alt"],
        border_radius=radius,
    )
    pygame.draw.rect(
        surface,
        blend_color(THEME["hud_row_border"], accent, 0.24),
        rect,
        width=1,
        border_radius=radius,
    )
    accent_bar = pygame.Rect(rect.x + 8, rect.y + 7, 3, max(8, rect.height - 14))
    pygame.draw.rect(surface, accent, accent_bar, border_radius=2)

    label_text = str(label).upper()
    value_text = str(value)
    value_max_width = max(72, int(rect.width * 0.34))
    label_max_width = max(1, rect.width - value_max_width - 38)
    label_font, label_display = fit_text(
        label_text,
        max_width=label_max_width,
        size=label_size,
        min_size=max(9, label_size - 1),
        bold=True,
        family="sans",
    )
    value_font, value_display = fit_text(
        value_text,
        max_width=value_max_width,
        size=value_size,
        min_size=max(11, value_size - 5),
        bold=True,
        family=value_family,
    )
    label_surface = label_font.render(label_display, True, THEME["hud_label"])
    value_surface = value_font.render(value_display, True, accent)
    label_rect = label_surface.get_rect(midleft=(rect.x + 18, rect.centery))
    value_rect = value_surface.get_rect(midright=(rect.right - 12, rect.centery))
    previous_clip = surface.get_clip()
    surface.set_clip(rect.inflate(-10, -4))
    surface.blit(label_surface, label_rect)
    surface.blit(value_surface, value_rect)
    surface.set_clip(previous_clip)


def draw_meter_bar(
    surface: pygame.Surface,
    rect: pygame.Rect,
    progress: float,
    color: tuple[int, int, int],
):
    rect = pygame.Rect(rect)
    if rect.width <= 0 or rect.height <= 0:
        return

    progress = max(0.0, min(1.0, progress))
    radius = min(6, max(3, rect.height // 2))
    draw_vertical_gradient(
        surface,
        rect,
        blend_color(THEME["hud_track"], THEME["panel_bg_highlight"], 0.24),
        THEME["hud_track"],
        border_radius=radius,
    )
    pygame.draw.rect(
        surface,
        THEME["hud_row_border"],
        rect,
        width=1,
        border_radius=radius,
    )
    fill_width = max(0, int((rect.width - 2) * progress))
    if fill_width <= 0:
        return

    fill_rect = pygame.Rect(rect.x + 1, rect.y + 1, fill_width, max(1, rect.height - 2))
    draw_vertical_gradient(
        surface,
        fill_rect,
        blend_color(color, THEME["text"], 0.12),
        blend_color(color, THEME["panel_shadow"], 0.18),
        border_radius=max(2, radius - 1),
    )


def draw_wrapped_text(
    surface: pygame.Surface,
    text: str,
    rect: pygame.Rect,
    color: tuple[int, int, int],
    *,
    size: int = 14,
    bold: bool = False,
    family: str = "sans",
    line_gap: int = 4,
    align: str = "left",
) -> int:
    rect = pygame.Rect(rect)
    if rect.width <= 0 or rect.height <= 0:
        return 0

    font = get_font(size, bold=bold, family=family)
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue

        current = ellipsize_text(words[0], font, rect.width)
        for word in words[1:]:
            fitted_word = ellipsize_text(word, font, rect.width)
            candidate = f"{current} {fitted_word}".strip()
            if font.size(candidate)[0] <= rect.width:
                current = candidate
            else:
                lines.append(current)
                current = fitted_word
        lines.append(current)

    line_height = font.get_linesize()
    line_step = line_height + line_gap
    max_lines = max(1, (rect.height + line_gap) // max(1, line_step))
    truncated = len(lines) > max_lines
    visible_lines = lines[:max_lines]
    if truncated and visible_lines:
        visible_lines[-1] = ellipsize_text(
            visible_lines[-1].rstrip(". ") + "...",
            font,
            rect.width,
        )

    y_pos = rect.y
    previous_clip = surface.get_clip()
    surface.set_clip(rect)
    for line in visible_lines:
        rendered = font.render(line, True, color)
        line_rect = rendered.get_rect()
        if align == "center":
            line_rect.midtop = (rect.centerx, y_pos)
        elif align == "right":
            line_rect.topright = (rect.right, y_pos)
        else:
            line_rect.topleft = (rect.x, y_pos)
        surface.blit(rendered, line_rect)
        y_pos += line_step
    surface.set_clip(previous_clip)
    return min(rect.height, max(0, y_pos - rect.y - line_gap))
