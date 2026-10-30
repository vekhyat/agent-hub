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


if __name__ == "__main__":
    unittest.main(verbosity=2)
