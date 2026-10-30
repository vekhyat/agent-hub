"""Local regression checks: real PTYs, isolation, input validation, shutdown."""
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import agent_hub as hub
hub.asyncio = asyncio


class HubChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-hub-verify-")
        self.sessions = []
        self.runtime_patch = patch.object(hub, "RUNTIME", self.temp.name)
        self.runtime_patch.start()
        self.records_patch = patch.object(hub, "HELPER_RECORDS", os.path.join(self.temp.name, "helpers"))
        self.records_patch.start()

    async def asyncTearDown(self):
        tasks = [session.kill() for session in self.sessions]
        await asyncio.gather(*(task for task in tasks if task is not None))
        for session in self.sessions:
            await asyncio.to_thread(session.proc.wait, 5)
        hub.SESSIONS.clear()
        hub.REPORTS.clear()
        self.records_patch.stop()
        self.runtime_patch.stop()
        self.temp.cleanup()

    async def spawn(self, name="one", owner="agent-a"):
        code = "import os,tty; tty.setraw(0); os.write(1,b'\\x1b[31mREADY\\x1b[0m\\r\\n'); data=os.read(0,4096); os.write(1,b'GOT:'+data+b'\\r\\n'); os.read(0,1)"
        session = hub.Session(name, "claude", [sys.executable, "-c", code], self.temp.name,
                              dict(os.environ), owner, 1)
        session.report_id = "id-" + name
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
            [sys.executable, str(Path(hub.__file__)), "spawn", "codex", "must-not-spawn"],
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
            if op == "reports":
                return {"reports": []}
            if op == "info":
                return {"kind": "codex", "alive": True, "quiet_for": 100}
            return {"text": "Working (esc to interrupt)"}
        with patch.object(hub, "request", request):
            self.assertIn("still busy", hub.wait_idle("test", timeout=0, settle=3))

    async def test_tell_reaches_only_the_spawner_and_is_read_once(self):
        await self.spawn(owner="agent-a")
        await hub.dispatch({"op": "report", "id": "id-one", "text": "need a decision"})
        other = await hub.dispatch({"op": "reports", "owner": "agent-b"})
        self.assertEqual(other["reports"], [])
        mine = await hub.dispatch({"op": "reports", "owner": "agent-a"})
        self.assertEqual(hub.format_reports(mine["reports"]), "[one says]\nneed a decision")
        self.assertEqual((await hub.dispatch({"op": "reports", "owner": "agent-a"}))["reports"], [])
        self.assertTrue(hub.SESSIONS["one"].alive)

    async def test_done_delivers_the_result_and_closes_the_helper(self):
        session = await self.spawn()
        with patch.object(hub, "DONE_GRACE", 0):
            await hub.dispatch({"op": "report", "id": "id-one", "text": "fixed it", "done": True})
            await asyncio.sleep(.05)
            await session.kill_task
        self.assertNotIn("one", hub.SESSIONS)
        reports = (await hub.dispatch({"op": "reports", "owner": "agent-a", "name": "one"}))["reports"]
        self.assertEqual(hub.format_reports(reports), "[one done; closed]\nfixed it")

    async def test_keep_stays_open_after_done(self):
        session = await self.spawn()
        session.keep = True
        await hub.dispatch({"op": "report", "id": "id-one", "text": "result", "done": True})
        await asyncio.sleep(.05)
        self.assertIsNone(session.kill_task)
        reports = (await hub.dispatch({"op": "reports", "owner": "agent-a"}))["reports"]
        self.assertEqual(reports[0]["title"], "done; still open")

    async def test_natural_exit_reports_to_the_spawner(self):
        code = "print('bye', flush=True)"
        session = hub.Session("gone", "claude", [sys.executable, "-c", code], self.temp.name,
                              dict(os.environ), "agent-a", 1)
        hub.SESSIONS["gone"] = session
        for _ in range(200):
            if "gone" not in hub.SESSIONS:
                break
            await asyncio.sleep(.02)
        reports = (await hub.dispatch({"op": "reports", "owner": "agent-a"}))["reports"]
        self.assertEqual(reports[0]["title"], "exited with status 0")
        self.assertIn("bye", reports[0]["text"])

    async def test_oversized_and_unknown_reports_are_rejected(self):
        await self.spawn()
        with self.assertRaisesRegex(ValueError, "write it to a file"):
            await hub.dispatch({"op": "report", "id": "id-one", "text": "x" * (hub.MAX_REPORT + 1)})
        with self.assertRaisesRegex(KeyError, "no running helper with ID"):
            await hub.dispatch({"op": "report", "id": "ghost", "text": "hi"})

    async def test_daemon_outlives_unread_reports(self):
        session = await self.spawn()
        hub.add_report(session, "says", "unread")
        hub.SESSIONS.clear()
        stop = asyncio.Event()
        with patch.object(hub, "STOP_EVENT", stop):
            hub.stop_if_empty()
            self.assertFalse(stop.is_set())
            hub.REPORTS.clear()
            hub.stop_if_empty()
            self.assertTrue(stop.is_set())
        hub.SESSIONS["one"] = session

    async def test_first_message_is_briefed_once(self):
        session = await self.spawn()
        pasted = []
        async def paste(text):
            pasted.append(text)
        with patch.object(session, "paste", paste):
            for text in ("first", "second"):
                await hub.dispatch({"op": "paste", "name": "one", "owner": "agent-a", "text": text, "brief": True})
        self.assertIn(hub.HUB_COMMAND + " done id-one", pasted[0])
        self.assertEqual(pasted[1], "second")

    async def test_slash_commands_and_briefed_spawns_are_not_briefed(self):
        session = await self.spawn()
        pasted = []
        async def paste(text):
            pasted.append(text)
        with patch.object(session, "paste", paste):
            await hub.dispatch({"op": "paste", "name": "one", "owner": "agent-a", "text": "/model x"})
            session.briefed = True
            await hub.dispatch({"op": "paste", "name": "one", "owner": "agent-a", "text": "next", "brief": True})
        self.assertEqual(pasted, ["/model x", "next"])

    async def test_only_forgotten_automated_helpers_are_closed(self):
        session = await self.spawn()
        session.last_output = 0
        self.assertTrue(hub.forgotten(session))
        with patch.object(session, "keep", True):
            self.assertFalse(hub.forgotten(session))
        with patch.object(session, "owner", None):
            self.assertFalse(hub.forgotten(session))
        with patch.object(hub, "MAX_IDLE", 0):
            self.assertFalse(hub.forgotten(session))
        with patch.object(hub, "shows_busy", lambda *_: True):
            self.assertFalse(hub.forgotten(session))
        session.last_output = time.time()
        self.assertFalse(hub.forgotten(session))
        session.last_output = 0
        child = await self.spawn(name="child")
        child.parent = "one"
        self.assertFalse(hub.forgotten(session))

    def test_wait_returns_a_report_without_polling_the_screen(self):
        calls = []
        def request(op, **args):
            calls.append(op)
            if op == "reports":
                return {"reports": [{"name": "one", "title": "done; closed", "text": "ok"}]}
            raise AssertionError(op)
        with patch.object(hub, "request", request):
            self.assertEqual(hub.wait_idle("one", 1, 0), "[one done; closed]\nok")
            self.assertEqual(hub.wait_any(1, 0), "[one done; closed]\nok")
        self.assertEqual(calls, ["reports", "reports"])

    def test_wait_any_needs_every_helper_quiet(self):
        def request(op, **args):
            if op == "reports":
                return {"reports": []}
            if op == "list":
                return {"agents": [{"name": "a", "alive": True}, {"name": "b", "alive": True}]}
            if op == "screen":
                return {"text": "idle screen"}
            return {"kind": "claude", "quiet_for": 10 if args["name"] == "a" else 0}
        with patch.object(hub, "request", request):
            self.assertIn("a, b still busy", hub.wait_any(0, 3))
            self.assertIn("[a idle]", hub.wait_any(0, 0))

    def test_wait_any_shows_a_helper_stuck_on_a_dialog_once(self):
        announced = set()
        def request(op, **args):
            if op == "reports":
                return {"reports": []}
            if op == "list":
                return {"agents": [{"name": "stuck", "alive": True}, {"name": "busy", "alive": True}]}
            if op == "screen":
                return {"text": "Allow this command? [y/n]" if args["name"] == "stuck" else "Working (esc to interrupt)"}
            if op == "announce":
                announced.add(args["name"])
                return {}
            return {"kind": "claude", "quiet_for": 100 if args["name"] == "stuck" else 0,
                    "announced": args["name"] in announced}
        with patch.object(hub, "request", request):
            first = hub.wait_any(5, 3)
            self.assertIn("[stuck idle]\nAllow this command?", first)
            self.assertIn("[still working: busy]", first)
            self.assertIn("still busy", hub.wait_any(0, 3))  # already shown; nothing new

    async def test_new_output_makes_a_shown_helper_new_again(self):
        session = await self.spawn()
        await hub.dispatch({"op": "announce", "name": "one", "owner": "agent-a"})
        self.assertTrue((await hub.dispatch({"op": "info", "name": "one", "owner": "agent-a"}))["announced"])
        await session.write(b"x")
        for _ in range(100):
            if "GOT:" in session.render(20):
                break
            await asyncio.sleep(.02)
        self.assertFalse((await hub.dispatch({"op": "info", "name": "one", "owner": "agent-a"}))["announced"])

    async def test_list_shows_busy_or_quiet_time(self):
        await self.spawn()
        agent, = (await hub.dispatch({"op": "list", "owner": "agent-a"}))["agents"]
        self.assertFalse(agent["busy"])
        with patch.object(hub, "request", lambda op, **a: {"agents": [dict(agent, quiet_for=125)]}):
            self.assertIn("\tquiet 2m05s\t", hub.list_agents())
        with patch.object(hub, "request", lambda op, **a: {"agents": [dict(agent, busy=True)]}):
            self.assertIn("\tbusy\t", hub.list_agents())

    def test_reusing_a_name_keeps_the_closed_helper_resumable(self):
        hub.save_record({"name": "reviewer", "report_id": "aaaa1111", "session_id": "s1", "cwd": "/a",
                         "task": "old", "started": 1000})
        hub.keep_closed_record("reviewer")
        self.assertEqual(hub.load_record("reviewer-aaaa1111")["cwd"], "/a")
        self.assertEqual(os.path.getmtime(hub.record_path("reviewer-aaaa1111")), 1000)

    def test_old_daemon_gets_no_brief_and_a_warning(self):
        captured = {}
        def request(op, **args):
            if op == "reports":
                raise RuntimeError("no agent named '' of yours; yours: none")
            if op == "spawn":
                captured.update(args)
            return {"name": "test", "profile": args.get("profile"), "workspace": ""}
        with patch.object(hub, "request", request), patch.object(hub, "wait_ready", lambda *_: "ready"), \
                patch.object(hub.shutil, "which", lambda *_: "/bin/true"), patch("sys.stderr") as stderr:
            hub.spawn_agent("claude", "test", cwd="/tmp", prompt="task", visible=False)
        self.assertNotIn("agent-hub done", " ".join(captured["argv"]))
        self.assertTrue(stderr.write.called)

    def test_no_daemon_is_not_outdated(self):
        def request(op, **args):
            raise RuntimeError("no running agents; use agent-hub spawn first")
        with patch.object(hub, "request", request):
            self.assertFalse(hub.daemon_outdated())

    def spawn_argv(self, kind, resume=None):
        captured = {}
        def request(op, **args):
            if op == "reports":
                return {"reports": []}
            captured.update(args)
            return {"name": args["name"], "profile": args.get("profile"), "workspace": ""}
        with patch.object(hub, "request", request), patch.object(hub, "wait_ready", lambda *_: "ready"), \
                patch.object(hub.shutil, "which", lambda name: "/bin/" + name):
            hub.spawn_agent(kind, "helper", cwd=self.temp.name, prompt="task", visible=False, resume=resume)
        return captured

    def test_new_helpers_get_a_session_id_their_cli_accepts(self):
        for kind in ("claude", "grok"):
            argv = self.spawn_argv(kind)["argv"]
            self.assertEqual(argv[1], "--session-id")
            uuid.UUID(argv[2])
        argv = self.spawn_argv("opencode")["argv"]
        self.assertEqual(argv[1], "--session")
        self.assertTrue(argv[2].startswith("ses_"))
        self.assertNotIn("resume", self.spawn_argv("codex")["argv"])

    def test_resume_reopens_the_recorded_session_in_each_cli(self):
        expected = {"claude": ["--resume", "the-id"], "grok": ["--resume", "the-id"],
                    "opencode": ["--session", "the-id"], "codex": ["resume", "the-id"]}
        for kind, args in expected.items():
            record = {"session_id": "the-id", "report_id": "old", "task": "first task"}
            captured = self.spawn_argv(kind, record)
            self.assertEqual(captured["argv"][1:3], args)
            self.assertEqual(captured["previous"], "old")
            self.assertEqual(hub.load_record("helper")["session_id"], "the-id")

    def test_resume_keeps_directory_model_and_task(self):
        hub.save_record({"name": "fixer", "kind": "claude", "session_id": "the-id", "report_id": "old",
                         "cwd": self.temp.name, "model": "opus", "effort": "high", "started": 1, "task": "fix it"})
        calls = {}
        def spawn(kind, name, cwd, prompt, model, effort, visible, keep, resume):
            calls.update(kind=kind, cwd=cwd, model=model, effort=effort, resume=resume)
            return "spawned"
        with patch.object(hub, "spawn_agent", spawn):
            hub.resume_agent("fixer", "next", effort="low")
        self.assertEqual((calls["cwd"], calls["model"], calls["effort"]), (self.temp.name, "opus", "low"))
        self.assertEqual(calls["resume"]["task"], "fix it")

    def test_resume_without_a_prompt_keeps_the_task(self):
        self.spawn_argv("claude")
        first = hub.load_record("helper")
        with patch.object(hub, "request", lambda op, **a: {"reports": [], "name": a.get("name"), "profile": a.get("profile"), "workspace": ""}), \
                patch.object(hub, "wait_ready", lambda *_: "ready"), \
                patch.object(hub.shutil, "which", lambda name: "/bin/" + name):
            hub.spawn_agent("claude", "helper", cwd=self.temp.name, visible=False, resume=first)
        self.assertEqual(hub.load_record("helper")["task"], "task")
        self.assertNotEqual(hub.load_record("helper")["report_id"], first["report_id"])

    def test_codex_session_is_found_by_the_brief_not_by_tool_output(self):
        day = Path(self.temp.name)/"sessions/2026/10/30"
        day.mkdir(parents=True)
        spawner = "11111111-1111-1111-1111-111111111111"
        helper = "22222222-2222-2222-2222-222222222222"
        (day/f"rollout-2026-10-30T13-00-00-{spawner}.jsonl").write_text(
            json.dumps({"payload": {"type": "function_call_output", "output": "done abc123"}}) + "\n")
        (day/f"rollout-2026-10-30T13-00-01-{helper}.jsonl").write_text(
            json.dumps({"payload": {"role": "user", "content": "done abc123"}}) + "\n")
        with patch.dict(hub.CODEX_HOMES, {"codex": self.temp.name}):
            self.assertEqual(hub.codex_session_id({"name": "n", "report_id": "abc123", "started": 0}), helper)
            with self.assertRaisesRegex(ValueError, "codex resume"):
                hub.codex_session_id({"name": "n", "report_id": "missing", "started": 0})

    async def test_an_open_helper_cannot_be_resumed_twice(self):
        await self.spawn()
        with self.assertRaisesRegex(ValueError, "^'one' is still open"):
            await hub.dispatch({"op": "spawn", "kind": "claude", "name": "one", "cwd": self.temp.name, "env": {},
                                "owner": "agent-b", "previous": "id-one"})

    def test_records_reject_unsafe_names_and_missing_helpers(self):
        with self.assertRaisesRegex(ValueError, "invalid helper name"):
            hub.load_record("../secrets")
        with self.assertRaisesRegex(ValueError, "no saved helper"):
            hub.load_record("ghost")

    def test_old_records_are_pruned(self):
        with patch.object(hub, "MAX_RECORDS", 2):
            for index in range(4):
                hub.save_record({"name": f"h{index}"})
                os.utime(hub.record_path(f"h{index}"), (index, index))
        self.assertEqual(len(hub.load_records()), 2)

    def test_closed_list_hides_running_helpers(self):
        for name in ("open", "closed"):
            hub.save_record({"name": name, "kind": "codex", "started": 1, "cwd": "/p", "task": "t"})
        with patch.object(hub, "SOCK", __file__), \
                patch.object(hub, "request", lambda op, **a: {"agents": [{"name": "open"}]}):
            listing = hub.list_closed()
        self.assertIn("closed", listing)
        self.assertNotIn("open", listing)

    async def test_turn_end_delivers_the_reply_when_the_helper_forgot_to_report(self):
        await self.spawn()
        await hub.dispatch({"op": "turn_end", "id": "id-one", "text": "the answer"})
        reports = (await hub.dispatch({"op": "reports", "owner": "agent-a"}))["reports"]
        self.assertEqual(hub.format_reports(reports), "[one ended its turn without reporting]\nthe answer")

    async def test_turn_end_falls_back_to_the_screen_without_a_reply(self):
        await self.spawn()
        await hub.dispatch({"op": "turn_end", "id": "id-one", "text": ""})
        self.assertIn("READY", (await hub.dispatch({"op": "reports", "owner": "agent-a"}))["reports"][0]["text"])

    async def test_turn_end_after_a_report_adds_nothing(self):
        await self.spawn()
        await hub.dispatch({"op": "report", "id": "id-one", "text": "question"})
        await hub.dispatch({"op": "turn_end", "id": "id-one", "text": "question"})
        reports = (await hub.dispatch({"op": "reports", "owner": "agent-a"}))["reports"]
        self.assertEqual([r["title"] for r in reports], ["says"])
        await hub.dispatch({"op": "turn_end", "id": "id-one", "text": "next turn"})
        reports = (await hub.dispatch({"op": "reports", "owner": "agent-a"}))["reports"]
        self.assertEqual([r["text"] for r in reports], ["next turn"])

    async def test_turn_end_for_a_closed_helper_is_ignored(self):
        self.assertEqual(await hub.dispatch({"op": "turn_end", "id": "gone", "text": "x"}), {})

    def test_claude_stop_hook_payload(self):
        sent = {}
        with patch.object(hub, "request", lambda op, **a: sent.update(op=op, **a)), \
                patch("sys.stdin", io.StringIO(json.dumps({"last_assistant_message": "ALPHA"}))):
            hub.turn_end("abc")
        self.assertEqual((sent["op"], sent["id"], sent["text"]), ("turn_end", "abc", "ALPHA"))

    def test_codex_notify_ignores_the_title_thread(self):
        day = Path(self.temp.name)/"sessions/2026/10/30"
        day.mkdir(parents=True)
        (day/"rollout-2026-10-30T13-00-00-main-thread.jsonl").write_text("")
        sent = []
        event = {"thread-id": "main-thread", "last-assistant-message": "BRAVO"}
        with patch.object(hub, "request", lambda op, **a: sent.append(a["text"])), \
                patch.dict(hub.CODEX_HOMES, {"codex": self.temp.name}):
            hub.turn_end("abc", json.dumps(dict(event, **{"thread-id": "title-thread"})))
            hub.turn_end("abc", json.dumps(event))
        self.assertEqual(sent, ["BRAVO"])

    def test_turn_end_never_fails_the_helper(self):
        def request(op, **args):
            raise RuntimeError("no running agents")
        with patch.object(hub, "request", request):
            self.assertEqual(hub.turn_end("abc", '{"last_assistant_message": "x"}'), "")
            self.assertEqual(hub.turn_end("abc", "not json"), "")
            self.assertEqual(hub.turn_end("abc", "[1]"), "")

    def test_codex_helpers_never_offer_to_update(self):
        self.assertIn("check_for_update_on_startup=false", self.spawn_argv("codex")["argv"])
        record = {"session_id": "the-id", "report_id": "old", "task": ""}
        self.assertEqual(self.spawn_argv("codex", record)["argv"][1:3], ["resume", "the-id"])

    def test_hooks_are_set_per_launch_only_for_supported_agents(self):
        claude = self.spawn_argv("claude")["argv"]
        hook = json.loads(claude[claude.index("--settings") + 1])["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertIn(" turn-end ", hook)
        codex = self.spawn_argv("codex")["argv"]
        self.assertTrue(any(arg.startswith("notify=") for arg in codex))
        self.assertEqual(hub.turn_hook_args("grok", "x"), [])
        self.assertEqual(hub.turn_hook_args("opencode", "x"), [])

    def test_codex_uses_the_normal_cli(self):
        self.assertEqual(hub.INTERACTIVE["codex"][0], ["codex"])
        self.assertNotIn("codex1", hub.INTERACTIVE)
        self.assertNotIn("codex2", hub.INTERACTIVE)
        self.assertIn("-m", hub.LAUNCH_MODEL["codex"]("test-model", "high"))
        self.assertTrue(hub.shows_busy("codex", "Working (esc to interrupt)"))
        self.assertEqual(hub.CODEX_HOMES["codex"], os.path.expanduser("~/.codex"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
