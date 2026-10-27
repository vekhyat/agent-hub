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
               if k not in {"AGENT_HUB_CODEX1_HOME", "AGENT_HUB_CODEX2_HOME"}}
        env.update(values)
        result = subprocess.run(
            [sys.executable, "-c", "import json,agent_hub;print(json.dumps(agent_hub.CODEX_HOMES))"],
            env=env, text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_codex_account_defaults_are_portable(self):
        self.assertEqual(self.homes({}), {
            "codex1": str(Path.home() / ".codex"),
            "codex2": str(Path.home() / ".codex2"),
        })

    def test_both_codex_account_overrides_expand_home(self):
        self.assertEqual(self.homes({
            "AGENT_HUB_CODEX1_HOME": "~/first-account",
            "AGENT_HUB_CODEX2_HOME": "~/second account",
        }), {
            "codex1": str(Path.home() / "first-account"),
            "codex2": str(Path.home() / "second account"),
        })

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
            for kind in ("codex1", "codex2"):
                home = Path(temp) / kind
                home.mkdir()
                (home / "models_cache.json").write_text(json.dumps({"models": [{
                    "slug": kind + "-model", "visibility": "list",
                    "supported_reasoning_levels": [{"effort": "high"}],
                }]}))
                with patch.dict(hub.CODEX_HOMES, {kind: str(home)}):
                    self.assertEqual(hub.available_models(kind), kind + "-model  efforts: high")


if __name__ == "__main__":
    unittest.main(verbosity=2)
