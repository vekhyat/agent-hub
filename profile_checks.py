"""Named account profiles, native environments, and per-account model defaults."""
import contextlib
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_hub as hub


class ProfileChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-hub-profiles-")
        self.root = Path(self.temp.name)
        self.config = self.root / "profiles.toml"
        self.config.write_text("[profiles]\n")
        self.env = patch.dict(os.environ, {"AGENT_HUB_PROFILES": str(self.config)})
        self.env.start()
        self.records = patch.object(hub, "HELPER_RECORDS", str(self.root / "helpers"))
        self.records.start()

    def tearDown(self):
        self.records.stop()
        self.env.stop()
        self.temp.cleanup()

    def accounts(self):
        entries = []
        for family in ("claude", "codex"):
            for number in range(1, 4):
                home = self.root / f"{family} account {number}"
                home.mkdir()
                entries.append(f'[profiles.{family}-account{number}]\nkind="{family}"\nhome={json.dumps(str(home))}\n')
        self.config.write_text("\n".join(entries))

    def test_multiple_claude_and_codex_accounts_have_independent_homes(self):
        self.accounts()
        captured = []
        with patch.object(hub, "request", side_effect=lambda op, **req: {"reports": []} if op == "reports" else captured.append(req) or {"name": req["name"], "profile": req["profile"]}), \
             patch.object(hub, "wait_ready", return_value="READY"), \
             patch.object(hub.shutil, "which", side_effect=lambda command: "/bin/" + command):
            for family in ("claude", "codex"):
                for number in range(1, 4):
                    name = f"{family}-account{number}"
                    hub.spawn_agent(name, name, self.temp.name, "hello", "test-model", "high", visible=False)
                    req = captured[-1]
                    key = "CLAUDE_CONFIG_DIR" if family == "claude" else "CODEX_HOME"
                    self.assertEqual((req["kind"], req["profile"]), (family, name))
                    self.assertEqual(req["env"][key], str(self.root / f"{family} account {number}"))
                    self.assertEqual(req["argv"][0], "/bin/" + family)
                    self.assertEqual(req["argv"][-2], "--")
                    self.assertTrue(req["argv"][-1].startswith("hello\n\n[agent-hub] REQUIRED"))
                    self.assertEqual(req["cwd"], self.temp.name)

    def test_named_codex_models_use_that_accounts_cache(self):
        self.accounts()
        for number in range(1, 4):
            home = self.root / f"codex account {number}"
            (home / "models_cache.json").write_text(json.dumps({"models": [{
                "slug": f"account{number}-model", "visibility": "list",
            }]}))
            self.assertEqual(hub.available_models(f"codex-account{number}"), f"account{number}-model  efforts: ")

    def test_existing_account_wrappers_are_preserved(self):
        captured = []
        with patch.object(hub, "request", side_effect=lambda op, **req: {"reports": []} if op == "reports" else captured.append(req) or {"name": req["name"], "profile": req["profile"]}), \
             patch.object(hub, "wait_ready", return_value="READY"), \
             patch.object(hub.shutil, "which", side_effect=lambda command: "/bin/" + command):
            self.config.write_text('[profiles.codex1]\nkind="codex"\ncommand="codex1"\n'
                                   '[profiles.codex2]\nkind="codex"\ncommand="codex2"\n')
            for name in ("codex1", "codex2"):
                hub.spawn_agent(name, name, self.temp.name, visible=False)
                self.assertEqual(captured[-1]["argv"][0], "/bin/" + name)

    def test_custom_launcher_and_home_expansion(self):
        self.config.write_text('[profiles.claude-work]\nkind="claude"\ncommand="claude-work"\nhome="~/.claude-work"\n')
        profile = hub.get_profile("claude-work")
        self.assertEqual(profile["command"], "claude-work")
        self.assertEqual(profile["home"], str(Path.home() / ".claude-work"))

    def test_native_default_claude_does_not_force_a_new_config_directory(self):
        captured = []
        with patch.dict(os.environ, {}):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            with patch.object(hub, "request", side_effect=lambda op, **req: {"reports": []} if op == "reports" else captured.append(req) or {"name": req["name"], "profile": req["profile"]}), \
                 patch.object(hub, "wait_ready", return_value="READY"), \
                 patch.object(hub.shutil, "which", return_value="/bin/claude"):
                hub.spawn_agent("claude", "default", self.temp.name, visible=False)
        self.assertNotIn("CLAUDE_CONFIG_DIR", captured[0]["env"])

    def test_bad_profiles_fail_before_launch(self):
        cases = [
            '[profiles.bad]\nkind="shell"\n',
            '[profiles.bad]\nkind="claude"\ncommand=["claude","--dangerously-skip-permissions"]\n',
            '[profiles.bad]\nkind="claude"\nhome="relative/path"\n',
            '[profiles.bad]\nkind="grok"\nhome="/tmp/account"\n',
            '[profiles.bad]\nkind="claude"\npermission_mode="bypass"\n',
            '[profiles."bad name"]\nkind="claude"\n',
            '[profiles.bad]\nkind="claude"\nhome=42\n',
            '[profiles.bad]\nkind="claude"\ncommand="--print"\n',
            '[profiles.bad\n',
        ]
        with patch.object(hub, "request") as request:
            for text in cases:
                with self.subTest(text=text), self.assertRaises(ValueError):
                    self.config.write_text(text)
                    hub.spawn_agent("bad", "bad", self.temp.name, visible=False)
            request.assert_not_called()

    def test_missing_explicit_config_is_an_error(self):
        self.config.unlink()
        with self.assertRaisesRegex(ValueError, "profiles file does not exist"):
            hub.load_profiles()

    def test_old_daemon_cannot_silently_ignore_new_profile_options(self):
        operations = []
        def request(op, **args):
            operations.append(op)
            return {"name": "test"} if op == "spawn" else {}
        with patch.object(hub, "request", request), patch.object(hub, "wait_ready") as wait, \
             patch.object(hub.shutil, "which", return_value="/bin/claude"):
            with self.assertRaisesRegex(RuntimeError, "running daemon does not support"):
                hub.spawn_agent("claude", "test", self.temp.name, visible=False)
        self.assertEqual(operations, ["reports", "spawn", "kill"])
        wait.assert_not_called()

    def test_profile_settings_restore_leaves_other_account_untouched(self):
        first = self.root / "first.json"
        second = self.root / "second.json"
        first.write_text('{"model":"first-original"}')
        second.write_text('{"model":"second-original","other":"before"}')
        with patch.dict(hub.MODEL_DEFAULTS_FILE, claude=str(first)), \
             patch.object(hub, "RUNTIME", self.temp.name), \
             patch.object(hub, "SETTINGS_LOCK", str(self.root / "settings.lock")):
            with hub.keep_user_default("claude", "new-model", path=str(second)):
                second.write_text('{"model":"new-model","other":"user-edit"}')
        self.assertEqual(json.loads(first.read_text()), {"model": "first-original"})
        self.assertEqual(json.loads(second.read_text()), {"model": "second-original", "other": "user-edit"})

    def test_switch_uses_running_sessions_account_after_profile_changes(self):
        original_settings = str(self.root / "original-account" / "settings.json")
        self.config.write_text('[profiles.claude-work]\nkind="codex"\nhome="~/.changed-later"\n')
        with patch.object(hub, "request", return_value={"kind": "claude", "settings_file": original_settings, "text": "READY"}), \
             patch.object(hub, "keep_user_default", return_value=contextlib.nullcontext()) as restore, \
             patch.object(hub, "run_command") as command, patch.object(hub, "screen", return_value="READY"), \
             patch.object(hub.time, "sleep"):
            hub.switch_model("running-helper", "haiku", "")
        restore.assert_called_once_with("claude", "haiku", "", path=original_settings)
        command.assert_called_once_with("running-helper", "/model haiku")


    def test_spawn_keep_reaches_the_daemon(self):
        captured = {}
        def request(op, **args):
            if op == "reports":
                return {"reports": []}
            captured.update(args)
            return {"name": args["name"], "profile": args.get("profile"), "workspace": ""}
        argv = ["agent-hub", "spawn", "claude", "helper", "task", "--cwd", self.temp.name, "--no-window", "--keep"]
        with patch.object(hub, "request", request), patch.object(hub, "wait_ready", return_value="READY"), \
             patch.object(hub.shutil, "which", return_value="/bin/claude"), patch.object(sys, "argv", argv), \
             patch("builtins.print"):
            hub.main()
        self.assertTrue(captured["keep"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
