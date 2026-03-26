"""GUI scene and settings tests."""

import json
import os
import random
import subprocess
import sys
import time

import pytest

from common.snapshot import EntityState, Snapshot


def _pygame():
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    return pytest.importorskip("pygame")


class _DummyClient:
    def __init__(self):
        from client.client import ConnState

        self.server_host = "127.0.0.1"
        self.server_port = 9000
        self.server_addr = (self.server_host, self.server_port)
        self.client_id: int | None = None
        self.server_host_client_id: int | None = None
        self.conn_state = ConnState.DISCONNECTED
        self.host_mode = False
        self.host_server_proc = None
        self.session_token: str | None = None
        self.room_key = ""
        self.server_certificate_fingerprint: str | None = None
        self.server_snapshots = []
        self.current_rtt = 0.0
        self.current_jitter = 0.0
        self.current_fps = 60.0
        self.player_name = "Player"
        self.player_names = {}
        self.game_started_by_server = False
        self.connected = False
        self.show_debug_stats = True
        self.scores = {}
        self.match_winner_id = None
        self.ui_notice: str | None = None
        self.kick_requests = []
        self.start_requests = 0
        self.phase_resync_until = 0.0
        self.visual_state = {"x": 120.0, "y": 140.0, "health": 100.0}
        self.local_state = {"dash_cooldown": 0.0, "effect_flags": 0}
        self.pending_inputs = []
        self.match_elapsed = 0.0
        self.last_server_tick = 0
        self.trusted_hosts_cleared = False

    def connect(self):
        self.conn_state = type(self.conn_state).CONNECTING

    def disconnect(self, clear_session_token=True):
        self.conn_state = type(self.conn_state).DISCONNECTED
        if clear_session_token:
            self.session_token = None

    def apply_settings(self, settings, update_connection=True):
        if update_connection:
            self.server_host = settings.get("host", self.server_host)
            self.server_port = settings.get("port", self.server_port)
            self.server_addr = (self.server_host, self.server_port)

    def set_room_key(self, room_key):
        self.room_key = room_key

    def display_name_for(self, client_id):
        if client_id is None or client_id <= 0:
            return self.player_name
        if self.client_id is not None and client_id == self.client_id:
            return self.player_name
        return self.player_names.get(client_id, f"P{client_id}")

    def clear_trusted_hosts(self):
        self.trusted_hosts_cleared = True

    def stop_host_server(self, wait_timeout: float = 2.0):
        _ = wait_timeout
        self.host_server_proc = None

    def begin_new_session(self):
        self.disconnect(clear_session_token=True)

    def request_kick_player(self, target_client_id):
        self.kick_requests.append(target_client_id)
        return True

    def request_game_start(self):
        self.start_requests += 1
        return True

    def phase_sync_pending(self):
        return time.perf_counter() < self.phase_resync_until

    def get_remote_states(self):
        return {}

    def get_metrics_display(self):
        return {"Loss": "0.0%"}


def test_scene_manager_push_pop():
    pygame = _pygame()
    from client.gui.scene_manager import BaseScene, SceneManager

    events = []

    class Scene(BaseScene):
        def __init__(self, manager, name):
            super().__init__(manager)
            self.name = name

        def on_enter(self):
            events.append(f"{self.name}:enter")

        def on_exit(self):
            events.append(f"{self.name}:exit")

        def on_pause(self):
            events.append(f"{self.name}:pause")

        def on_resume(self):
            events.append(f"{self.name}:resume")

    manager = SceneManager(pygame.Surface((16, 16)))
    first = Scene(manager, "first")
    second = Scene(manager, "second")

    manager.push(first)
    manager.push(second)
    manager.pop()

    assert manager.current is first
    assert events == [
        "first:enter",
        "first:pause",
        "second:enter",
        "second:exit",
        "first:resume",
    ]


def test_scene_manager_replace():
    pygame = _pygame()
    from client.gui.scene_manager import BaseScene, SceneManager

    events = []

    class Scene(BaseScene):
        def __init__(self, manager, name):
            super().__init__(manager)
            self.name = name

        def on_enter(self):
            events.append(f"{self.name}:enter")

        def on_exit(self):
            events.append(f"{self.name}:exit")

    manager = SceneManager(pygame.Surface((16, 16)))
    first = Scene(manager, "first")
    second = Scene(manager, "second")

    manager.push(first)
    manager.replace(second)

    assert manager.current is second
    assert events == ["first:enter", "first:exit", "second:enter"]


def test_scene_manager_reset_clears_entire_stack():
    pygame = _pygame()
    from client.gui.scene_manager import BaseScene, SceneManager

    events = []

    class Scene(BaseScene):
        def __init__(self, manager, name):
            super().__init__(manager)
            self.name = name

        def on_enter(self):
            events.append(f"{self.name}:enter")

        def on_exit(self):
            events.append(f"{self.name}:exit")

        def on_pause(self):
            events.append(f"{self.name}:pause")

    manager = SceneManager(pygame.Surface((16, 16)))
    first = Scene(manager, "first")
    second = Scene(manager, "second")
    third = Scene(manager, "third")

    manager.push(first)
    manager.push(second)
    manager.reset(third)

    assert manager.current is third
    assert events == [
        "first:enter",
        "first:pause",
        "second:enter",
        "second:exit",
        "first:exit",
        "third:enter",
    ]


def test_settings_load_save(tmp_path, monkeypatch):
    _pygame()
    from client.gui.scenes import settings as settings_module

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(settings_module, "CONFIG_PATH", config_path)

    config = {
        "host": "192.168.1.50",
        "port": 9010,
        "name": "Tester",
        "fps": 75,
        "interp_ms": 120,
        "show_debug": False,
    }
    settings_module.save_config(config)

    assert settings_module.load_config() == config


def test_settings_load_normalizes_invalid_values(tmp_path, monkeypatch):
    _pygame()
    from client.gui.scenes import settings as settings_module

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(settings_module, "CONFIG_PATH", config_path)
    config_path.write_text(
        json.dumps(
            {
                "host": "",
                "port": "bad",
                "name": "   ",
                "fps": 999,
                "interp_ms": -50,
                "show_debug": "false",
            }
        )
    )

    assert settings_module.load_config() == {
        "host": "127.0.0.1",
        "port": 9000,
        "name": "Player",
        "fps": 240,
        "interp_ms": 0,
        "show_debug": False,
    }


def test_settings_load_normalizes_mistyped_ipv4_host(tmp_path, monkeypatch):
    _pygame()
    from client.gui.scenes import settings as settings_module

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(settings_module, "CONFIG_PATH", config_path)
    config_path.write_text(json.dumps({"host": "127.0.0.1s", "port": 9000}))

    loaded = settings_module.load_config()

    assert loaded["host"] == "127.0.0.1"


def test_settings_validation():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.settings import SettingsScene

    manager = SceneManager(pygame.Surface((800, 600)))
    scene = SettingsScene(manager, client=_DummyClient())
    scene.on_enter()

    scene.fields["host"].text = ""
    assert scene._validate() is None
    assert scene.error_message

    scene.fields["host"].text = "127.0.0.1"
    scene.fields["port"].text = "80"
    assert scene._validate() is None
    assert scene.error_message

    scene.fields["port"].text = "9000"
    scene.fields["fps"].text = "500"
    assert scene._validate() is None
    assert scene.error_message

    scene.fields["fps"].text = "60"
    scene.fields["interp_ms"].text = "900"
    assert scene._validate() is None
    assert scene.error_message


def test_settings_save_surfaces_filesystem_errors(monkeypatch):
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.settings import SettingsScene

    manager = SceneManager(pygame.Surface((800, 600)))
    scene = SettingsScene(manager, client=_DummyClient())
    manager.push(scene)
    monkeypatch.setattr(
        "client.gui.scenes.settings.save_config",
        lambda cfg: (_ for _ in ()).throw(OSError(28, "disk full")),
    )

    scene._save()

    assert manager.current is scene
    assert "Could not save settings" in scene.error_message


def test_settings_clear_trusted_hosts_updates_notice():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.settings import SettingsScene

    client = _DummyClient()
    manager = SceneManager(pygame.Surface((800, 600)))
    scene = SettingsScene(manager, client=client)
    scene.on_enter()

    scene._clear_trusted_hosts()

    assert client.trusted_hosts_cleared is True
    assert scene.notice_message == "Trusted hosts cleared."
    assert scene.error_message == ""


def test_settings_tab_cycles_focus_between_fields():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.settings import SettingsScene

    manager = SceneManager(pygame.Surface((800, 600)))
    scene = SettingsScene(manager, client=_DummyClient())
    scene.on_enter()

    assert scene.fields["host"].focused is True

    scene.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_TAB, mod=0))
    assert scene.fields["port"].focused is True

    scene.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_TAB, mod=0))
    assert scene.fields["name"].focused is True


def test_settings_ctrl_s_saves(monkeypatch):
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.settings import SettingsScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    scene = SettingsScene(manager, client=client)
    manager.push(scene)
    saved = {}
    monkeypatch.setattr(
        "client.gui.scenes.settings.save_config",
        lambda cfg: saved.update(cfg),
    )

    scene.fields["name"].set_text("QoL")
    scene.handle_event(
        pygame.event.Event(pygame.KEYDOWN, key=pygame.K_s, mod=pygame.KMOD_CTRL)
    )

    assert saved["name"] == "QoL"
    assert manager.current is None


def test_join_dialog_validation():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.join_dialog import JoinDialogScene

    manager = SceneManager(pygame.Surface((800, 600)))
    scene = JoinDialogScene(manager, client=_DummyClient())
    scene.on_enter()

    scene.host_input.text = "   "
    scene.port_input.text = "99999"
    scene._connect()
    assert scene.error_message

    scene.host_input.text = "999.999.999.999"
    scene.port_input.text = "9000"
    scene.room_key_input.text = "shared-key"
    scene._connect()
    assert scene.error_message

    scene = JoinDialogScene(manager, client=_DummyClient())
    scene.on_enter()
    scene.host_input.text = "127.0.0.1"
    scene.port_input.text = "9000"
    scene.room_key_input.text = ""
    scene._connect()
    assert scene.error_message == "Room key is required."

    scene.error_message = ""
    scene.host_input.text = "myserver.local"
    scene.port_input.text = "9000"
    scene.room_key_input.text = "shared-key"
    scene._connect()
    assert scene.error_message == ""

    scene = JoinDialogScene(manager, client=_DummyClient())
    scene.on_enter()
    scene.host_input.text = "game.example.com"
    scene.port_input.text = "9001"
    scene.room_key_input.text = "shared-key"
    scene._connect()
    assert scene.error_message == ""

    scene = JoinDialogScene(manager, client=_DummyClient())
    scene.on_enter()
    scene.host_input.text = "not a host!"
    scene.port_input.text = "9000"
    scene.room_key_input.text = "shared-key"
    scene._connect()
    assert scene.error_message


def test_join_dialog_enter_key_triggers_connect():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.join_dialog import JoinDialogScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    scene = JoinDialogScene(manager, client=client)
    scene.on_enter()
    scene.host_input.text = "127.0.0.1"
    scene.port_input.text = "9000"
    scene.room_key_input.text = "shared-key"

    scene.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN))

    assert scene.error_message == ""


def test_join_dialog_focuses_host_field_on_enter():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.join_dialog import JoinDialogScene

    manager = SceneManager(pygame.Surface((800, 600)))
    scene = JoinDialogScene(manager, client=_DummyClient())

    scene.on_enter()

    assert scene.host_input.focused is True
    assert scene.host_input.cursor_pos == len(scene.host_input.text)


def test_game_client_apply_settings_normalizes_dirty_values():
    from client.client import GameClient

    client = GameClient(headless=True)
    try:
        client.apply_settings(
            {
                "host": "",
                "port": "oops",
                "name": "",
                "fps": "500",
                "interp_ms": -20,
                "show_debug": "false",
            }
        )

        assert client.server_addr == ("127.0.0.1", 9000)
        assert client.player_name == "Player"
        assert client.target_fps == 240
        assert client.interpolator.interp_ticks == 0
        assert client.show_debug_stats is False
    finally:
        client.disconnect(close_socket=True)


def test_text_input_click_sets_caret_position():
    pygame = _pygame()
    from client.gui.widgets import TextInput

    input_box = TextInput((100, 100, 160, 34), placeholder="Host")
    input_box.text = "abcdefgh"
    input_box.cursor_pos = len(input_box.text)

    input_box.handle_event(
        pygame.event.Event(
            pygame.MOUSEBUTTONDOWN,
            button=1,
            pos=(input_box.rect.x + 28, input_box.rect.y + 12),
        )
    )

    assert input_box.focused is True
    assert input_box.cursor_pos < len(input_box.text)


def test_text_input_keydown_unicode_inserts_text_without_textinput_event():
    pygame = _pygame()
    from client.gui.widgets import TextInput

    input_box = TextInput((100, 100, 160, 34), placeholder="Host")
    input_box.focus()

    input_box.handle_event(
        pygame.event.Event(pygame.KEYDOWN, key=pygame.K_a, unicode="a", mod=0)
    )

    assert input_box.text == "a"
    assert input_box.cursor_pos == 1


def test_text_input_dedupes_following_textinput_echo_after_keydown_fallback():
    pygame = _pygame()
    from client.gui.widgets import TextInput

    input_box = TextInput((100, 100, 160, 34), placeholder="Host")
    input_box.focus()

    input_box.handle_event(
        pygame.event.Event(pygame.KEYDOWN, key=pygame.K_b, unicode="b", mod=0)
    )
    input_box.handle_event(pygame.event.Event(pygame.TEXTINPUT, text="b"))

    assert input_box.text == "b"
    assert input_box.cursor_pos == 1


def test_host_validation_rejects_ipv6_literals():
    from client.gui.validation import is_valid_host

    assert is_valid_host("127.0.0.1") is True
    assert is_valid_host("game.example.com") is True
    assert is_valid_host("::1") is False
    assert is_valid_host("[2001:db8::1]") is False


def test_main_menu_host_game_reports_port_conflict(monkeypatch):
    pygame = _pygame()
    from client.client import GameClient
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.main_menu import MainMenuScene

    port = random.randint(10000, 60000)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "server.server",
            "--port",
            str(port),
            "--room-key",
            "occupied-port",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.2)

    manager = SceneManager(pygame.Surface((800, 600)))
    client = GameClient(headless=True)
    scene = MainMenuScene(manager, client=client)
    monkeypatch.setattr(
        "client.gui.scenes.main_menu.load_config",
        lambda: {
            "host": "127.0.0.1",
            "port": port,
            "name": "Player",
            "fps": 60,
            "interp_ms": 100,
            "show_debug": True,
        },
    )

    manager.push(scene)
    try:
        error = scene._start_host_with_room_key("shared-key")

        assert error
        assert client.host_mode is False
        assert client.conn_state.name == "DISCONNECTED"
    finally:
        client.disconnect(close_socket=True)
        if (
            client.host_server_proc is not None
            and client.host_server_proc.poll() is None
        ):
            client.host_server_proc.terminate()
            client.host_server_proc.wait(timeout=3)
        proc.terminate()
        proc.wait(timeout=3)


def test_main_menu_shows_client_notice():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.main_menu import MainMenuScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    client.ui_notice = "You were kicked by the host."
    scene = MainMenuScene(manager, client=client)

    manager.push(scene)

    assert scene.error_message == "You were kicked by the host."


def test_main_menu_resume_preserves_pending_host_startup():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.main_menu import MainMenuScene

    class _Proc:
        @staticmethod
        def poll():
            return None

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    scene = MainMenuScene(manager, client=client)
    manager.push(scene)

    client.session_token = "keep-token"
    scene._host_starting = True
    scene._host_start_deadline = time.monotonic() + 1.0
    scene._sync_button_state()
    client.host_mode = True
    client.host_server_proc = _Proc()

    scene.on_resume()

    assert scene._host_starting is True
    assert client.host_mode is True
    assert client.session_token == "keep-token"
    assert scene.host_button.label == "STARTING..."


def test_main_menu_hero_layout_keeps_title_inside_panel():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.main_menu import MainMenuScene

    surface = pygame.Surface((800, 600))
    manager = SceneManager(surface)
    scene = MainMenuScene(manager, client=_DummyClient())

    hero = scene._build_hero_layout(surface)

    assert hero["hero_panel"].inflate(-28, -20).contains(hero["title_rect"])
    assert hero["hero_panel"].inflate(-28, -14).contains(hero["subtitle_rect"])
    assert hero["hero_panel"].inflate(-28, -12).contains(hero["tagline_rect"])
    assert all(surface.get_rect().contains(rect) for _, rect in hero["feature_rects"])


def test_main_menu_clears_kicked_session_token():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.main_menu import MainMenuScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    client.ui_notice = "You were kicked by the host."
    client.session_token = "keep-token"
    scene = MainMenuScene(manager, client=client)

    manager.push(scene)

    assert client.session_token is None


def test_join_dialog_starts_fresh_session_before_join():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.join_dialog import JoinDialogScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    client.session_token = "stale-token"
    scene = JoinDialogScene(manager, client=client)
    scene.on_enter()
    scene.host_input.text = "127.0.0.1"
    scene.port_input.text = "9000"
    scene.room_key_input.text = "shared-key"

    scene._connect()

    assert client.session_token is None
    assert client.room_key == "shared-key"


def test_settings_save_does_not_retarget_live_connection(monkeypatch):
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.settings import SettingsScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    client.conn_state = type(client.conn_state).CONNECTED
    client.server_host = "127.0.0.1"
    client.server_port = 9000
    client.server_addr = (client.server_host, client.server_port)
    scene = SettingsScene(manager, client=client)
    monkeypatch.setattr("client.gui.scenes.settings.save_config", lambda cfg: None)
    manager.push(scene)

    scene.fields["host"].text = "192.168.0.42"
    scene.fields["port"].text = "9999"
    scene.fields["fps"].text = "120"
    scene.fields["interp_ms"].text = "80"
    scene._save()

    assert client.server_addr == ("127.0.0.1", 9000)


def test_settings_footer_stays_clear_of_buttons():
    pygame = _pygame()
    from client.client import ConnState
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.settings import SettingsScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    client.conn_state = ConnState.CONNECTED
    scene = SettingsScene(manager, client=client)
    scene.error_message = "Could not save settings"
    scene._layout(manager.screen)
    footer = scene._footer_layout()

    assert footer["info"].bottom < scene.save_button.rect.top
    assert footer["error"].bottom < scene.save_button.rect.top
    assert footer["info"].bottom < footer["error"].top
    assert footer["info"].top > scene.debug_button.rect.bottom


def test_lobby_player_rows_mark_local_player():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.lobby import LobbyScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    client.client_id = 2
    client.player_name = "Bravo"
    client.player_names = {1: "Alpha", 2: "Bravo"}
    client.server_snapshots = [
        Snapshot(
            tick=1,
            entities={
                1: EntityState(1, ping_ms=8),
                2: EntityState(2, ping_ms=0),
            },
        )
    ]

    scene = LobbyScene(manager, client=client, host=False)

    assert scene._build_player_rows() == [
        (1, "Alpha (HOST)"),
        (2, "Bravo (YOU)"),
    ]


def test_lobby_reassigned_host_gets_start_button():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.lobby import LobbyScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    client.client_id = 2
    client.connected = True
    client.server_snapshots = [
        Snapshot(
            tick=1,
            entities={
                2: EntityState(2, ping_ms=5),
                3: EntityState(3, ping_ms=7),
            },
        )
    ]

    scene = LobbyScene(manager, client=client, host=False)
    scene.update(0.016)

    assert scene._is_local_host() is True
    assert scene.start_button.disabled is False


def test_lobby_uses_authoritative_host_id_over_lowest_player_id():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.lobby import LobbyScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    client.client_id = 2
    client.connected = True
    client.server_host_client_id = 3
    client.server_snapshots = [
        Snapshot(
            tick=1,
            entities={
                2: EntityState(2, ping_ms=5),
                3: EntityState(3, ping_ms=7),
            },
        )
    ]

    scene = LobbyScene(manager, client=client, host=False)
    scene.update(0.016)

    assert scene._current_host_id() == 3
    assert scene._is_local_host() is False


def test_lobby_host_can_select_and_kick_other_player():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.lobby import LobbyScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    client.client_id = 1
    client.connected = True
    client.server_snapshots = [
        Snapshot(
            tick=1,
            entities={
                1: EntityState(1),
                2: EntityState(2),
            },
        )
    ]

    scene = LobbyScene(manager, client=client, host=True)
    scene.update(0.016)
    scene.players_list.selected_index = 1
    scene.handle_event(pygame.event.Event(pygame.NOEVENT))
    scene.update(0.016)
    scene._kick_selected()

    assert scene.kick_button.disabled is False
    assert client.kick_requests == [2]


def test_lobby_keyboard_navigation_can_kick_selected_player():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.lobby import LobbyScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    client.client_id = 1
    client.connected = True
    client.conn_state = type(client.conn_state).CONNECTED
    client.server_snapshots = [
        Snapshot(
            tick=1,
            entities={
                1: EntityState(1),
                2: EntityState(2),
                3: EntityState(3),
            },
        )
    ]

    scene = LobbyScene(manager, client=client, host=True)
    scene.update(0.016)
    scene.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_DOWN))

    assert scene.selected_player_id == 2

    scene.update(0.016)
    scene.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_DELETE))

    assert client.kick_requests == [2]


def test_lobby_enter_starts_game_for_local_host():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.lobby import LobbyScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    client.client_id = 1
    client.connected = True
    client.conn_state = type(client.conn_state).CONNECTED
    client.server_snapshots = [Snapshot(tick=1, entities={1: EntityState(1)})]

    scene = LobbyScene(manager, client=client, host=True)
    scene.update(0.016)
    scene.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN))

    assert client.start_requests == 1


def test_pause_disconnect_on_empty_stack_pushes_main_menu():
    pygame = _pygame()
    from client.client import GameClient
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.game_hud import GameHUDScene, PauseOverlayScene
    from client.gui.scenes.main_menu import MainMenuScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = GameClient(headless=True)
    try:
        hud = GameHUDScene(manager, client=client, host=False)
        manager.push(hud)
        pause = PauseOverlayScene(manager, client=client, parent_scene=hud, host=False)
        manager.push(pause)

        pause._disconnect()

        assert isinstance(manager.current, MainMenuScene)
    finally:
        client.disconnect(close_socket=True)


def test_game_hud_returns_to_lobby_when_match_state_clears():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.game_hud import GameHUDScene
    from client.gui.scenes.lobby import LobbyScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    client.connected = True
    client.conn_state = type(client.conn_state).CONNECTED
    client.game_started_by_server = False
    client.match_winner_id = None

    hud = GameHUDScene(manager, client=client, host=False)
    manager.push(hud)
    hud.update(0.016)

    assert isinstance(manager.current, LobbyScene)


def test_game_hud_waits_for_phase_resync_before_returning_to_lobby():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.game_hud import GameHUDScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    client.connected = True
    client.conn_state = type(client.conn_state).CONNECTED
    client.phase_resync_until = time.perf_counter() + 10.0

    hud = GameHUDScene(manager, client=client, host=False)
    manager.push(hud)
    hud.update(0.016)

    assert manager.current is hud


def test_game_hud_ranking_matches_alive_crown_logic():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.game_hud import GameHUDScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    client.client_id = 2
    client.scores = {1: 5, 2: 4, 3: 1}
    client.server_snapshots = [
        Snapshot(
            tick=1,
            entities={
                1: EntityState(1, health=0.0),
                2: EntityState(2, health=100.0),
                3: EntityState(3, health=100.0),
            },
        )
    ]

    scene = GameHUDScene(manager, client=client, host=False)

    assert scene._ranked_players([1, 2, 3], client.server_snapshots[-1]) == [2, 3, 1]


def test_game_hud_f3_toggles_debug_stats():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.game_hud import GameHUDScene

    manager = SceneManager(pygame.Surface((800, 600)))
    client = _DummyClient()
    scene = GameHUDScene(manager, client=client, host=False)

    scene.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_F3))

    assert client.show_debug_stats is False


def test_game_hud_draw_smoke():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.game_hud import GameHUDScene

    surface = pygame.Surface((800, 600))
    manager = SceneManager(surface)
    client = _DummyClient()
    client.client_id = 1
    client.connected = True
    client.conn_state = type(client.conn_state).CONNECTED
    client.server_snapshots = [
        Snapshot(
            tick=1,
            entities={
                1: EntityState(1, x=120.0, y=140.0, health=90.0),
                2: EntityState(2, x=320.0, y=240.0, health=75.0),
            },
        )
    ]
    client.scores = {1: 2, 2: 3}
    scene = GameHUDScene(manager, client=client, host=False)

    scene.draw(surface)

    assert surface.get_rect().width == 800


def test_game_hud_scoreboard_stays_within_panel_for_many_players():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.game_hud import GameHUDScene

    surface = pygame.Surface((800, 600))
    manager = SceneManager(surface)
    client = _DummyClient()
    client.client_id = 1
    client.connected = True
    client.conn_state = type(client.conn_state).CONNECTED
    client.server_snapshots = [
        Snapshot(
            tick=1,
            entities={
                entity_id: EntityState(entity_id, x=60.0 * entity_id, y=40.0, health=100.0)
                for entity_id in range(1, 13)
            },
        )
    ]
    client.scores = {entity_id: 12 - entity_id for entity_id in range(1, 13)}
    scene = GameHUDScene(manager, client=client, host=False)
    scene.show_scoreboard = True

    scene.draw(surface)

    assert surface.get_rect().width == 800


def test_match_over_scene_transitions_to_lobby_on_reset():
    pygame = _pygame()
    from client.client import ConnState
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.lobby import LobbyScene
    from client.gui.scenes.match_over import MatchOverScene

    class MockClient:
        client_id = 1
        host_mode = True
        game_started_by_server = False
        match_winner_id = None
        scores = {1: 5}
        conn_state = ConnState.CONNECTED
        server_host = "127.0.0.1"
        server_port = 9000
        server_addr = (server_host, server_port)
        session_token = None
        current_rtt = 0.0
        server_snapshots = []
        connected = True

        def connect(self):
            return None

        def disconnect(self):
            return None

    manager = SceneManager(pygame.Surface((800, 600)))
    scene = MatchOverScene(manager, client=MockClient())
    manager.push(scene)
    scene.update(0.016)
    assert isinstance(manager.current, LobbyScene)


def test_match_over_draw_handles_many_score_rows():
    pygame = _pygame()
    from client.client import ConnState
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.match_over import MatchOverScene

    class MockClient:
        client_id = 1
        host_mode = False
        game_started_by_server = True
        match_winner_id = 1
        scores = {entity_id: 20 - entity_id for entity_id in range(1, 15)}
        conn_state = ConnState.CONNECTED
        server_host = "127.0.0.1"
        server_port = 9000
        server_addr = (server_host, server_port)
        session_token = None
        current_rtt = 0.0
        server_snapshots = []
        connected = True

        @staticmethod
        def display_name_for(entity_id):
            return f"P{entity_id}"

    surface = pygame.Surface((800, 600))
    manager = SceneManager(surface)
    scene = MatchOverScene(manager, client=MockClient())

    scene.draw(surface)

    assert surface.get_rect().height == 600


def test_wrapped_text_stays_inside_rect_bounds():
    pygame = _pygame()
    from client.gui.theme import draw_wrapped_text

    surface = pygame.Surface((220, 90), pygame.SRCALPHA)
    surface.fill((0, 0, 0, 0))
    rect = pygame.Rect(16, 12, 90, 28)

    height = draw_wrapped_text(
        surface,
        "This is a deliberately long line with 127.0.0.1ssss and more text",
        rect,
        (255, 255, 255),
        size=14,
    )

    bounds = surface.get_bounding_rect()
    assert height <= rect.height
    assert bounds.right <= rect.right
    assert bounds.bottom <= rect.bottom


def test_join_dialog_layout_keeps_text_blocks_clear_of_inputs_and_buttons():
    pygame = _pygame()
    from client.gui.scene_manager import SceneManager
    from client.gui.scenes.join_dialog import JoinDialogScene

    surface = pygame.Surface((800, 600))
    manager = SceneManager(surface)
    scene = JoinDialogScene(manager, client=_DummyClient())

    scene._layout(surface)

    assert not scene.room_key_input.rect.colliderect(scene._hint_rect())
    assert not scene.connect_button.rect.colliderect(scene._error_banner_rect())
    assert not scene.back_button.rect.colliderect(scene._error_banner_rect())
