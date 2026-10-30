"""Public package configuration and console-entry behavior; no provider requests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_hub as hub


class PackagingChecks(unittest.TestCase):
    def homes(self, values):
        env = {k: v for k, v in os.environ.items()
               if k not in {"CODEX_HOME", "AGENT_HUB_CODEX1_HOME", "AGENT_HUB_CODEX2_HOME"}}
        env.update(values)
        # The module under test, not whichever agent_hub the child's working directory would import.
        result = subprocess.run(
            [sys.executable, "-c", "import json,runpy,sys;print(json.dumps(runpy.run_path(sys.argv[1])['CODEX_HOMES']))",
             hub.__file__], env=env, text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_codex_home_default_is_portable(self):
        self.assertEqual(self.homes({}), {"codex": str(Path.home() / ".codex")})

    def test_codex_home_override_expands_home(self):
        self.assertEqual(self.homes({"CODEX_HOME": "~/second account"}),
                         {"codex": str(Path.home() / "second account")})

    def test_console_entry_has_concise_errors(self):
        with patch.object(hub, "main", side_effect=RuntimeError("test failure")):
            with self.assertRaises(SystemExit) as error:
                hub.cli()
        self.assertEqual(error.exception.code, "agent-hub: test failure")
        with patch.object(hub, "main", side_effect=KeyboardInterrupt):
            with self.assertRaises(SystemExit) as interrupted:
                hub.cli()
        self.assertEqual(interrupted.exception.code, 130)

    def test_models_uses_each_accounts_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            for kind in ("codex",):
                home = Path(temp) / kind
                home.mkdir()
                (home / "models_cache.json").write_text(json.dumps({"models": [{
                    "slug": kind + "-model", "visibility": "list",
                    "supported_reasoning_levels": [{"effort": "high"}],
                }]}))
                with patch.dict(hub.CODEX_HOMES, {kind: str(home)}):
                    self.assertEqual(hub.available_models(kind), kind + "-model  efforts: high")


    def test_setup_links_the_skill_only_for_agents_present_and_keeps_existing_ones(self):
        with tempfile.TemporaryDirectory() as home:
            Path(home, ".claude").mkdir()
            Path(home, ".grok/skills/agent-hub").mkdir(parents=True)  # the user's own copy
            config = Path(home, "profiles.toml")
            config.write_text(f'[profiles.claude-work]\nkind="claude"\nhome="{home}/claude-work"\n')
            Path(home, "claude-work").mkdir()
            env = {"HOME": home, "AGENT_HUB_PROFILES": str(config), "CODEX_HOME": ""}
            with patch.dict(os.environ, env), patch.object(hub.os.path, "expanduser",
                                                           lambda p: p.replace("~", home, 1)), \
                 patch.object(hub, "find_command", lambda c: "/bin/" + c if c == "claude" else None):
                text, ready = hub.setup()
                again, _ = hub.setup()
            self.assertTrue(ready, text)
            source = os.path.realpath(hub.skill_source())
            for linked in (".claude/skills/agent-hub", "claude-work/skills/agent-hub"):
                self.assertEqual(os.path.realpath(Path(home, linked)), source)
            self.assertFalse(Path(home, ".codex").exists())
            self.assertFalse(Path(home, ".grok/skills/agent-hub").is_symlink())
            self.assertIn("kept an existing skill", text)
            self.assertIn("skill already linked", again)

    def test_setup_is_not_ready_without_an_agent_cli(self):
        with patch.object(hub, "find_command", return_value=None):
            text, ready = hub.setup(skills=False)
        self.assertFalse(ready)
        self.assertIn("MISS agent CLIs: none", text)

if __name__ == "__main__":
    unittest.main(verbosity=2)
