# Command and runtime reference

## Commands

| Command | Behavior |
| --- | --- |
| `spawn KIND NAME [PROMPT] --cwd DIR` | Starts an interactive helper and a Foot window. |
| `ask NAME TEXT` | Sends a prompt and waits for a quiet screen. |
| `send NAME TEXT` | Sends a prompt without waiting for a reply. |
| `read NAME --lines 100` | Reads the end of the captured screen. `--raw` preserves decoration. |
| `wait NAME --timeout 60 --settle 3` | Waits for a quiet screen, exit, or timeout. |
| `keys NAME Enter` | Sends named keys or literal characters. Supports `Escape`, `C-c`, arrows, and other keys listed in the source. |
| `model NAME MODEL --effort LEVEL` | Changes the helper's model and optional effort through its native UI. |
| `models KIND` | Lists available models. Codex uses that account's local cache. |
| `attach NAME` | Attaches the current terminal. Ctrl-] detaches without stopping the agent. |
| `kill NAME` | Stops the helper and its managed descendants before returning. |
| `ls` | Lists visible sessions without starting a daemon. |

`KIND` is one of `claude`, `codex1`, `codex2`, `opencode`, or `grok`. Spawning
requires that profile's executable on PATH. Every `spawn` requires an existing
`--cwd` directory. The hub resolves symlinks and sets both the child's working
directory and `PWD` to the resolved path.

`spawn` accepts `--model ID` and `--effort LEVEL`. OpenCode requires a model
variant instead of an effort flag. `--no-window` keeps the interactive PTY
without opening Foot. It does not use an agent's noninteractive print mode.

`wait` and `ask` cannot prove that a task succeeded. Their result includes
dialogs, failures, and quiet output. Native model IDs and effort values depend
on the installed agent and account. `model` returns the resulting screen and
any observed settings conflict warnings.

## Ownership

Automated callers receive process ownership plus a conversation identifier when
one is available. On shared Codex app-server processes, `CODEX_THREAD_ID` or a
unique `AGENT_HUB_CALLER_ID` is required. `CODEX_SESSION_ID` alone is insufficient
on a shared app-server. A custom integration must reuse one caller ID within a
conversation and use a different ID for every other conversation.

Unknown automated callers receive private process ownership. Direct human
terminal commands can access all helpers. Helpers close when their owning
process exits. The daemon checks that process every five seconds. Closing a
parent helper also closes its nested helpers.

Socket ownership is a convenience boundary for normal clients, not an operating
system security boundary. The raw protocol trusts processes running as the same
Unix user.

## Environment

| Variable | Purpose |
| --- | --- |
| `AGENT_HUB_CODEX1_HOME` | Cache directory for `models codex1`. Defaults to `~/.codex`. |
| `AGENT_HUB_CODEX2_HOME` | Cache directory for `models codex2`. Defaults to `~/.codex2`. |
| `AGENT_HUB_CALLER_ID` | Conversation identity for integrations. Must be unique per conversation. |
| `AGENT_HUB_MAX_AGENTS` | Maximum concurrent sessions. Defaults to `8`. |
| `AGENT_HUB_MAX_DEPTH` | Maximum nesting depth. Defaults to `2`. |

The last two values must also be in the daemon's environment when it starts.
The systemd path starts the daemon using the user manager's environment.
Spawned agent processes receive the calling CLI's environment after the hub
removes parent-agent session markers. The hub sets `AGENT_HUB_NAME` and
`AGENT_HUB_DEPTH` for nested helpers and `AGENT_HUB_VIEW_OWNER` for its viewers.

## Runtime files

The runtime directory is `/run/user/<uid>` when present. The fallback is
`/tmp/agent-hub-<uid>`.

| Path inside the runtime directory | Contents |
| --- | --- |
| `agent-hub.sock` | Private Unix socket while the daemon runs. |
| `agent-hub.sock.lock` | Daemon startup lock. |
| `agent-hub-settings.lock` | Lock used around hub-driven model setting changes. |
| `agent-hub.log` | Appended daemon diagnostics. No built-in log rotation. |
| `agent-hub-screens/` | At most 32 completed screen records. Directory mode `0700`, file mode `0600`. |

The transient systemd user unit is `agent-hub-<socket-path-hash>.service`.
It delegates a cgroup to each helper. Shutdown uses `cgroup.kill` to include
late-forked children and detached descendants. The daemon exits 10 seconds
after the last active or terminating session ends. No persistent service is
installed. Logs, locks, and completed screen records can remain after exit.

## Settings affected by model changes

The hub observes these native files around a model switch:

| Agent | Native settings file |
| --- | --- |
| Claude | `~/.claude/settings.json` |
| Grok | `~/.grok/config.toml` |
| OpenCode | `${XDG_STATE_HOME:-~/.local/state}/opencode/model.json` |

It restores attributable model fields while preserving unrelated changes and
symlinks. Replacement uses a temporary file beside the resolved target.
Concurrent or unattributed model changes produce warnings. Restoration is not
transactional with arbitrary external writers. If the switching CLI is killed
mid-operation, or an agent writes after the observation period, defaults can
remain changed. Restoring an absent Grok model field can leave a valid empty
`[models]` table.
