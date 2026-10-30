---
name: agent-hub
description: Spawn and drive other coding agents in visible terminals with the agent-hub CLI. Use only when the user explicitly names agent-hub; general requests for delegation, subagents, or parallel work do not invoke it.
---

Run `agent-hub --help` and `agent-hub CMD --help` for exact syntax. The executable must be on PATH; it works from any project.

1. Run `agent-hub profiles` and use the profile the user asked for (built-ins: `claude`, `codex`, `opencode`, `grok`; others are named accounts).
2. Spawn with the whole task in the prompt. `--cwd` is required: pass the directory the task needs.
   `agent-hub spawn PROFILE NAME 'full task' --cwd /path/to/project`
   Optional: `--model`, `--effort` (see `agent-hub models PROFILE`), `--terminal`, `--no-window`. Inspect the returned screen for trust, sign-in, or permission dialogs.
3. Make one long `wait` instead of polling: `agent-hub wait --timeout 1800`. Run it in the background if you have other work. With no name it returns the first report from any of your helpers, a helper quiet for 30 s without reporting (usually a permission dialog), or all helpers quiet.
4. The helper asks questions with `tell`; they arrive as `[NAME says]`. Answer with `agent-hub send NAME 'answer'`, then `wait` again.
5. The helper finishes with `done`, which delivers `[NAME done; closed]` and closes it. `[NAME ended its turn without reporting]` shows its last reply instead; read it, then `send` a follow-up or `kill`.
6. Check the reply and the work before treating a task as done. Use `agent-hub read NAME --lines 100` for long output.
7. To continue a closed helper, use `agent-hub resume NAME 'next step'` rather than spawning a new one. It keeps the conversation, directory, and account. `agent-hub ls --closed` lists them.

`--keep` helpers stay open after `done` and idle time; you must `agent-hub kill NAME` them. Other forgotten idle helpers close after 30 minutes by default. `agent-hub ls` shows each helper as `busy` or `quiet`. Killing a helper also closes its nested helpers. Humans can `attach NAME` and detach with Ctrl-].

Helpers belong to the calling conversation. On shared Codex app-server hosts, set `CODEX_THREAD_ID` or a unique `AGENT_HUB_CALLER_ID` per conversation.

This is not a sandbox. Helpers run as the user with their native permissions; stay within the user's authorized scope and do not approve dialogs the user has not authorized.
