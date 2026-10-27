"""Local regression checks: real PTYs, isolation, input validation, shutdown."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_hub as hub
hub.asyncio = asyncio


class HubChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-hub-verify-")
        self.sessions = []
        self.runtime_patch = patch.object(hub, "RUNTIME", self.temp.name)
        self.runtime_patch.start()

    async def asyncTearDown(self):
        tasks = [session.kill() for session in self.sessions]
        await asyncio.gather(*(task for task in tasks if task is not None))
        for session in self.sessions:
            await asyncio.to_thread(session.proc.wait, 5)
        hub.SESSIONS.clear()
        self.runtime_patch.stop()
        self.temp.cleanup()

    async def spawn(self, name="one", owner="agent-a"):
        code = "import os,tty; tty.setraw(0); os.write(1,b'\\x1b[31mREADY\\x1b[0m\\r\\n'); data=os.read(0,4096); os.write(1,b'GOT:'+data+b'\\r\\n'); os.read(0,1)"
        session = hub.Session(name, "claude", [sys.executable, "-c", code], self.temp.name,
                              dict(os.environ), owner, 1)
        self.sessions.append(session)
        hub.SESSIONS[name] = session
        for _ in range(100):
            if "READY" in session.render(20):
                return session
            await asyncio.sleep(.02)
        self.fail("PTY did not produce READY")

    async def test_real_pty_screen_and_input(self):
        session = await self.spawn()
        self.assertNotIn("\x1b", session.render(20))
        await hub.dispatch({"op": "paste", "name": "one", "owner": "agent-a", "text": "hello"})
        for _ in range(100):
            if "GOT:hello" in session.render(20):
                break
            await asyncio.sleep(.02)
        self.assertIn("GOT:hello", session.render(20))
        session.resize(80, 24)
        self.assertEqual(session.screen.columns, 80)

    async def test_other_agent_cannot_read_write_or_kill(self):
        session = await self.spawn()
        for op in ("screen", "write", "kill"):
            with self.subTest(op=op), self.assertRaises(KeyError):
                await hub.dispatch({"op": op, "name": "one", "owner": "agent-b", "data": "x"})
        visible = await hub.dispatch({"op": "list", "owner": "agent-b"})
        self.assertEqual(visible["agents"], [])
        self.assertTrue(session.alive)
        self.assertIn("READY", (await hub.dispatch({"op": "screen", "name": "one"}))["text"])

    async def test_spawn_rejects_shell_and_excess_nesting(self):
        with self.assertRaises(ValueError):
            await hub.dispatch({"op": "spawn", "kind": "shell", "name": "bad"})
        with self.assertRaises(ValueError):
            await hub.dispatch({"op": "spawn", "kind": "claude", "name": "bad", "depth": hub.MAX_DEPTH,
                                "cwd": self.temp.name, "env": {}})

    async def test_spawn_rejects_missing_or_invalid_project_directory(self):
        file = Path(self.temp.name)/"a-file"
        file.write_text("not a directory")
        for cwd in ("", str(file), str(Path(self.temp.name)/"missing")):
            with self.subTest(cwd=cwd), self.assertRaisesRegex(ValueError, "project directory"):
                await hub.dispatch({"op": "spawn", "kind": "claude", "name": "bad", "cwd": cwd, "env": {}})
        self.assertFalse(hub.SESSIONS)

    async def test_spawn_uses_requested_directory_and_pwd(self):
        project = Path(self.temp.name)/"project with spaces"
        project.mkdir()
        link = Path(self.temp.name)/"project-link"
        link.symlink_to(project, target_is_directory=True)
        code = "import os,time; os.write(1, ('CWD='+os.getcwd()+'\\r\\nPWD='+os.environ['PWD']+'\\r\\n').encode()); time.sleep(60)"
        await hub.dispatch({"op": "spawn", "kind": "claude", "name": "cwd-check", "cwd": str(link),
                            "env": dict(os.environ, PWD="/wrong-directory"),
                            "argv": [sys.executable, "-c", code], "owner": "agent-a", "depth": 0})
        session = hub.SESSIONS["cwd-check"]
        self.sessions.append(session)
        expected = str(project.resolve())
        for _ in range(100):
            if "PWD=" in session.render(20):
                break
            await asyncio.sleep(.02)
        self.assertEqual(session.cwd, expected)
        self.assertIn("CWD="+expected, session.render(20))
        self.assertIn("PWD="+expected, session.render(20))

    def test_cli_requires_project_directory(self):
        result = subprocess.run(
            [sys.executable, str(Path(hub.__file__)), "spawn", "codex1", "must-not-spawn"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--cwd", result.stderr)

    async def test_kill_reaps_process(self):
        session = await self.spawn()
        await hub.dispatch({"op": "kill", "name": "one", "owner": "agent-a"})
        await asyncio.to_thread(session.proc.wait, 5)
        self.assertIsNotNone(session.proc.returncode)
        self.assertNotIn("one", hub.SESSIONS)

    async def test_parent_kill_also_closes_nested_sessions(self):
        parent = await self.spawn("parent")
        child = await self.spawn("child", owner="agent-child")
        child.parent = "parent"
        await hub.dispatch({"op": "kill", "name": "parent", "owner": "agent-a"})
        await asyncio.to_thread(child.proc.wait, 5)
        self.assertIsNotNone(child.proc.returncode)
        self.assertNotIn("child", hub.SESSIONS)

    def test_command_injection_rejected(self):
        for value in ("sonnet\n/permissions", "--dangerously-skip-permissions;sh", "model x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                hub.check_value("model", value)

    def test_busy_wait_timeout_is_not_reported_as_idle(self):
        def request(op, **kwargs):
            if op == "info":
                return {"kind": "codex2", "alive": True, "quiet_for": 100}
            return {"text": "Working (esc to interrupt)"}
        with patch.object(hub, "request", request):
            self.assertIn("still busy", hub.wait_idle("test", timeout=0, settle=3))

    def test_codex_account_launchers_are_distinct(self):
        for kind in ("codex1", "codex2"):
            self.assertEqual(hub.INTERACTIVE[kind][0], [kind])
            self.assertIn("-m", hub.LAUNCH_MODEL[kind]("test-model", "high"))
            self.assertTrue(hub.shows_busy(kind, "Working (esc to interrupt)"))
        self.assertNotIn("codex", hub.INTERACTIVE)
        self.assertIn("codex", hub.AGENT_PROCESS_NAMES)
        self.assertNotEqual(hub.CODEX_HOMES["codex1"], hub.CODEX_HOMES["codex2"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
