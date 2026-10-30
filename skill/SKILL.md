---
name: agent-hub
description: Start and control coding agents and named account profiles in visible terminals when the user requests cross-agent work or agent-hub.
---

Run `agent-hub --help` for commands. The executable must be on the caller's PATH and works from any project.

Run `agent-hub profiles` to find built-in and configured account profiles. Choose the account requested by the user. Named Claude and Codex profiles select their own configuration directory. Existing `codex1` and `codex2` launchers remain supported.

Use `spawn --terminal ghostty` or `--terminal kitty` when the user requests a terminal. Otherwise the hub selects an available supported terminal. `AGENT_HUB_TERMINAL` sets the caller's default.

Every `spawn` requires `--cwd /path/to/project`. Select the directory required by the task. The hub rejects missing or nonexistent directories and resolves symlinks before launch. Then use `ask`, `read`, `wait`, `keys`, `model`, and `kill` as needed. `wait` reports quiet screens, including permission dialogs and errors. Check the reply before treating a task as done. Use `read --lines 100` for longer output.

Close helpers you started with `kill`; their nested helpers close too. Human users attach with `attach` and detach with Ctrl-]. The shared daemon stops when unused.

Ownership uses a conversation ID on shared Codex hosts. A host without one must provide a unique `AGENT_HUB_CALLER_ID` per conversation. Other automated callers receive private process ownership; direct human terminal commands can manage all sessions. Helpers are closed if their owning process exits.

On supported Linux systems, the daemon and its viewers run in a transient user systemd service separate from the first caller's terminal. No persistent service is installed. The last 32 completed screens remain in private runtime files so `read` can retrieve an exited helper's output without restarting the daemon.

Use the caller's existing permissions and the user's authorized scope. This CLI is not a sandbox or an authorization bypass.
