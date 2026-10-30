# Command and runtime reference

## Commands

| Command | Behavior |
| --- | --- |
| `spawn PROFILE NAME [PROMPT] --cwd DIR` | Starts an interactive helper and a terminal window. |
| `profiles` | Lists built-in and configured account profiles, agent families, commands, and homes. |
| `ask NAME TEXT` | Sends a prompt and waits for a quiet screen. |
| `send NAME TEXT` | Sends a prompt without waiting for a reply. |
| `read NAME --lines 100` | Reads the end of the captured screen. `--raw` preserves decoration. |
| `wait NAME --timeout 60 --settle 3` | Waits for a quiet screen, exit, or timeout. |
| `keys NAME Enter` | Sends named keys or literal characters. Supports `Escape`, `C-c`, arrows, and other keys listed in the source. |
| `model NAME MODEL --effort LEVEL` | Changes the helper's model and optional effort through its native UI. |
| `models PROFILE` | Lists available models. Codex uses that account's local cache. |
| `attach NAME` | Attaches the current terminal. Ctrl-] detaches without stopping the agent. |
| `kill NAME` | Stops the helper and its managed descendants before returning. |
| `ls` | Lists visible sessions without starting a daemon. |

Built-in profiles are `claude`, `codex`, `codex1`, `codex2`, `opencode`, and `grok`.
Named profiles select a native family and an executable. Spawning
requires that profile's executable on PATH. Every `spawn` requires an existing
`--cwd` directory. The hub resolves symlinks and sets both the child's working
directory and `PWD` to the resolved path.

`spawn` accepts `--model ID`, `--effort LEVEL`, and `--terminal NAME`. OpenCode requires a model
variant instead of an effort flag. `--no-window` keeps the interactive PTY
without opening a window. It does not use an agent's noninteractive print mode.

`wait` and `ask` cannot prove that a task succeeded. Their result includes
dialogs, failures, and quiet output. Native model IDs and effort values depend
on the installed agent and account. `model` returns the resulting screen and
any observed settings conflict warnings.

## Profiles file

The optional file is `${XDG_CONFIG_HOME:-~/.config}/agent-hub/profiles.toml`.
`AGENT_HUB_PROFILES` selects an explicit alternative file. Missing default files
use the built-in profiles. A missing explicitly selected file is an error.

```toml
[profiles.claude-work]
kind = "claude"
command = "claude"
home = "~/.claude-work"
```

| Field | Meaning |
| --- | --- |
| `kind` | Native family: `claude`, `codex`, `grok`, or `opencode`. Required for a new profile. |
| `command` | One executable name or path. Defaults to the family's native executable for new profiles. No shell arguments. |
| `home` | Optional absolute or `~/` account directory for Claude or Codex. Sets `CLAUDE_CONFIG_DIR` or `CODEX_HOME` for the child. |

A profile can override a built-in name. Unspecified fields keep that built-in's
values when its family stays the same. Without a `home`, the executable uses
its native environment or wrapper configuration. The default Claude profile
does not force `CLAUDE_CONFIG_DIR` to a different value. `profiles` reads this
file without starting the daemon. The runtime keeps the selected family and
Claude settings path with each running helper.

After upgrading from a version without profile support, close its existing
helpers and let its daemon stop before using the new CLI. A new CLI rejects an
old daemon's spawn response and closes that newly created helper, so the old
daemon cannot silently ignore account or terminal selections.

## Terminal selection

`--terminal NAME` overrides `AGENT_HUB_TERMINAL`. If neither is set, selection
is `auto`. Auto checks Foot, Ghostty, Kitty, Alacritty, WezTerm, then xterm using
the caller's PATH. It skips Foot when `WAYLAND_DISPLAY` is absent. An explicit
unavailable choice produces an error instead of selecting a different terminal.

The hub launches a separate terminal process. Ghostty uses
`--gtk-single-instance=false -e`; WezTerm uses
`start --always-new-process --`. Kitty and Foot receive the viewer executable
directly. Alacritty and xterm use `-e`. Each viewer attaches to the existing PTY.
Closing a viewer leaves the helper running. `kill` closes the helper and its
viewer. Terminal configurations remain unchanged.

On Hyprland 0.56+, the caller finds its workspace through its process ancestry
and passes it to the daemon. The hub sets a runtime window rule before launching
the viewer: `workspace` with `silent`, plus `no_initial_focus`. The rule matches
only hub viewer titles (WezTerm uses its class), and is reused per workspace.
No desktop configuration files are changed. Nested spawns check the parent's
live viewer window first, so moving a parent also moves the destination of its
future helpers. The session's original workspace is retained when its viewer closes.

If a detached caller has no identifiable window, set `AGENT_HUB_WORKSPACE` to a
number, `name:NAME`, or `special:NAME`. Without an origin, visible spawns fail
before starting the agent rather than opening on the user's current workspace.
`--no-window` still works. On other desktops, normal terminal placement applies.

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
| `AGENT_HUB_PROFILES` | Explicit path to the optional named account profiles file. |
| `AGENT_HUB_TERMINAL` | Default terminal name or `auto`. `spawn --terminal` overrides it. |
| `AGENT_HUB_WORKSPACE` | Hyprland workspace fallback for detached callers; helpers inherit their resolved workspace. |
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

For Claude account profiles, the file is `<account home>/settings.json` instead.
The hub captures that path from the spawned process environment. Switching its
model later uses the captured account, not a different caller's default account.
Named Codex profiles use `<account home>/models_cache.json` for model lookup.
The default `codex` profile uses `${CODEX_HOME:-~/.codex}`. The legacy `codex1`
and `codex2` cache directories remain configurable through the variables above.

It restores attributable model fields while preserving unrelated changes and
symlinks. Replacement uses a temporary file beside the resolved target.
Concurrent or unattributed model changes produce warnings. Restoration is not
transactional with arbitrary external writers. If the switching CLI is killed
mid-operation, or an agent writes after the observation period, defaults can
remain changed. Restoring an absent Grok model field can leave a valid empty
`[models]` table.
