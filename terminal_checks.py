"""Terminal selection and literal argv forwarding without a desktop or AI calls."""
import os
import io
import json
import unittest
from unittest.mock import patch

import agent_hub as hub


class TerminalChecks(unittest.TestCase):
    def test_origin_uses_ancestor_window_instead_of_an_unrelated_window(self):
        clients = [{'pid': 40, 'workspace': {'id': 7, 'name': '7'}},
                   {'pid': 99, 'workspace': {'id': 2, 'name': '2'}}]
        parents = {80: 60, 60: 40}
        def stat(path):
            pid = int(path.split('/')[2])
            return io.StringIO(f'{pid} (command with spaces) S {parents[pid]} 0')
        with patch('builtins.open', stat):
            self.assertEqual(hub.process_workspace(clients, 80), '7')

    def test_workspace_selectors_keep_named_and_special_workspaces(self):
        self.assertEqual(hub.window_workspace({'workspace': {'id': -2, 'name': 'project'}}), 'name:project')
        self.assertEqual(hub.window_workspace({'workspace': {'id': -99, 'name': 'special:agents'}}), 'special:agents')

    def test_detached_origin_requires_an_explicit_workspace(self):
        env = {'HYPRLAND_INSTANCE_SIGNATURE': 'test', 'AGENT_HUB_WORKSPACE': '8'}
        with patch.object(hub, 'hyprland_clients', return_value=[]):
            self.assertEqual(hub.caller_workspace(env), '8')
        with self.assertRaisesRegex(RuntimeError, 'spawning agent'):
            hub.background_terminal(['/bin/foot'], '', env)

    def test_background_rule_is_installed_before_launch_with_literal_workspace(self):
        for name in hub.TERMINALS:
            with self.subTest(name=name), patch.object(hub, 'hyprland_request', return_value='ok') as request:
                terminal = ['/bin/' + name, *hub.TERMINALS[name][1:]]
                command = hub.background_terminal(terminal, 'name:project "x"', {'HYPRLAND_INSTANCE_SIGNATURE': 'test'})
                code = request.call_args.args[0][1]
                self.assertIn('no_initial_focus=true', code)
                self.assertIn(json.dumps('name:project "x" silent'), code)
                option = '--class' if name == 'wezterm' else '-T' if name == 'xterm' else '--title'
                self.assertTrue(any(arg.startswith(option) for arg in command))
                if '-e' in command:
                    self.assertLess(next(i for i, arg in enumerate(command) if arg.startswith(option)), command.index('-e'))

    def test_other_desktops_keep_the_original_terminal_command(self):
        terminal = ['/bin/foot']
        with patch.object(hub, 'hyprland_request', side_effect=AssertionError('must not call Hyprland')):
            self.assertEqual(hub.background_terminal(terminal, '', {}), terminal)

    def test_each_supported_terminal_resolves_an_executable(self):
        for name in hub.TERMINALS:
            with self.subTest(terminal=name), patch.object(hub.shutil, "which", return_value="/opt/bin/" + name):
                command = hub.terminal_command(name, {"PATH": "/opt/bin"})
                self.assertEqual(command[0], "/opt/bin/" + name)
                self.assertTrue(all(isinstance(arg, str) for arg in command))
                if name == "ghostty":
                    self.assertIn("-e", command)

    def test_auto_selection_falls_back_to_an_available_terminal(self):
        with patch.object(hub.shutil, "which", side_effect=lambda command, **kw: "/usr/bin/kitty" if command == "kitty" else None):
            self.assertEqual(hub.terminal_command(env={"PATH": "/usr/bin"}), ["/usr/bin/kitty"])

    def test_foot_is_not_auto_selected_in_an_x11_only_environment(self):
        def which(command, **kwargs):
            return "/bin/" + command if command in ("foot", "alacritty") else None
        with patch.object(hub.shutil, "which", side_effect=which):
            self.assertEqual(hub.terminal_command(env={"DISPLAY": ":0"})[0], "/bin/alacritty")
            self.assertEqual(hub.terminal_command(env={"WAYLAND_DISPLAY": "wayland-0"})[0], "/bin/foot")

    def test_explicit_choice_wins_over_environment(self):
        with patch.object(hub.shutil, "which", side_effect=lambda command, **kw: "/bin/" + command):
            self.assertEqual(hub.terminal_command("kitty", {"AGENT_HUB_TERMINAL": "ghostty"})[0], "/bin/kitty")
            self.assertEqual(hub.terminal_command(env={"AGENT_HUB_TERMINAL": "alacritty"})[0], "/bin/alacritty")

    def test_missing_or_flag_shaped_choice_is_not_silently_replaced(self):
        with patch.object(hub.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "not available"):
                hub.terminal_command("kitty")
        for name in ("kitty -e sh", "--print", "/bin/sh"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unknown terminal"):
                hub.terminal_command(name)

    def test_no_window_needs_no_terminal(self):
        with patch.dict(os.environ, {"AGENT_HUB_TERMINAL": "not-installed"}), \
             patch.object(hub, "get_profile", return_value={"kind": "claude", "command": "claude", "home": ""}), \
             patch.object(hub.shutil, "which", return_value="/bin/claude"), \
             patch.object(hub, "terminal_command", side_effect=AssertionError("should not look for a terminal")), \
             patch.object(hub, "request", return_value={"name": "test", "profile": "claude"}), \
             patch.object(hub, "wait_ready", return_value="READY"):
            self.assertIn("spawned claude", hub.spawn_agent("claude", "test", "/tmp", visible=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
