"""Stack-based scene manager for the pygame GUI."""

from __future__ import annotations

import pygame


class SceneManager:
    def __init__(self, screen: pygame.Surface):
        self.screen = screen
        self._stack: list[BaseScene] = []

    def push(self, scene: "BaseScene"):
        if self._stack:
            self._stack[-1].on_pause()
        self._stack.append(scene)
        scene.on_enter()

    def pop(self):
        if self._stack:
            self._stack[-1].on_exit()
            self._stack.pop()
        if self._stack:
            self._stack[-1].on_resume()

    def replace(self, scene: "BaseScene"):
        if self._stack:
            self._stack[-1].on_exit()
            self._stack[-1] = scene
        else:
            self._stack.append(scene)
        scene.on_enter()

    def reset(self, scene: "BaseScene"):
        while self._stack:
            self._stack[-1].on_exit()
            self._stack.pop()
        self._stack.append(scene)
        scene.on_enter()

    def handle_event(self, event):
        if self._stack:
            self._stack[-1].handle_event(event)

    def update(self, dt: float):
        if self._stack:
            self._stack[-1].update(dt)

    def draw(self):
        if self._stack:
            self._stack[-1].draw(self.screen)
        pygame.display.flip()

    @property
    def current(self):
        return self._stack[-1] if self._stack else None


class BaseScene:
    def __init__(self, manager: SceneManager):
        self.mgr = manager

    def on_enter(self):
        pass

    def on_exit(self):
        pass

    def on_pause(self):
        pass

    def on_resume(self):
        pass

    def handle_event(self, event):
        pass

    def update(self, dt: float):
        pass

    def draw(self, surface: pygame.Surface):
        pass
