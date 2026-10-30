# agent-hub

Start coding agents in visible terminal windows. Send prompts, read their screens,
switch models, and close their processes from another agent or your terminal.

```sh
agent-hub spawn claude reviewer 'Review this project' --cwd "$PWD"
agent-hub ask reviewer 'Which issue should I fix first?'
agent-hub read reviewer --lines 100
agent-hub kill reviewer
```

Use Claude, Codex, OpenCode, or Grok with your existing logins. Add named
profiles for multiple Claude or Codex subscriptions, such as `claude-work` and
`codex-personal`. Each helper receives its own PTY and terminal window.
Supported terminals are Foot, Ghostty, Kitty, Alacritty, WezTerm, and xterm.
There is no tmux session or MCP server.

## Point your agent at it

Paste this into the coding agent you already use:

```text
Read https://raw.githubusercontent.com/vekhyat/agent-hub/main/docs/agent-setup.md
and set up agent-hub for my installed agents and accounts. Preserve my existing
logins, launchers, and permission settings. Configure the accounts I choose,
use an available terminal, and verify it in the project directory I specify.
```

The [agent setup guide](docs/agent-setup.md) covers installation, account
profiles, terminal selection, and skill discovery. Browser sign-ins remain
interactive. Your agent does not need to read or copy credential files.

## Install

The supported environment is **Linux with a graphical session, a supported
terminal, a systemd user manager, and delegated cgroup v2 with `cgroup.kill`**.
Python 3.12 or newer is required. Foot needs Wayland. Other listed terminals
can run under Wayland or X11, depending on their installed build.
Install and sign in to whichever agent CLIs you want to launch. Other platforms
are untested. The process cleanup fallback without systemd is best effort.

1. Clone this repository.

   ```sh
   git clone https://github.com/vekhyat/agent-hub.git
   cd agent-hub
   ```

2. Install the CLI in an isolated Python environment with `uv`.

   ```sh
   uv tool install .
   ```

   Alternatively, use a virtual environment.

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install .
   export PATH="$PWD/.venv/bin:$PATH"
   ```

3. Check that the command is available to the calling agent.

   ```sh
   agent-hub --help
   ```

   If your shell cannot find a uv-installed command, run `uv tool update-shell`
   and restart the terminal. Restart existing agent apps to pick up PATH changes.

## Start a helper

1. Choose an existing project directory. `--cwd` is required for every spawn.

   ```sh
   agent-hub spawn claude helper 'Reply READY' --cwd "$PWD"
   ```

   A terminal window opens. The command returns the helper's screen, including any
   trust, sign-in, or permission dialog. Inspect that screen before continuing.

2. Send work to the helper.

   ```sh
   agent-hub ask helper 'Summarize the files in this directory'
   ```

3. Close the helper when the task ends.

   ```sh
   agent-hub kill helper
   ```

   The daemon stops 10 seconds after the last helper finishes. Completed screens
   remain readable from the private runtime cache.

For a default Codex installation, use `agent-hub spawn codex ...`. Existing
`codex1` and `codex2` executable launchers also remain supported.

## Use multiple accounts

Define named profiles in `~/.config/agent-hub/profiles.toml`. Each profile selects
an agent family and its account's configuration directory. Add as many profiles
as you need. A minimal configuration looks like this:

```toml
[profiles.claude-work]
kind = "claude"
home = "~/.claude-work"

[profiles.claude-personal]
kind = "claude"
home = "~/.claude-personal"

[profiles.codex-work]
kind = "codex"
home = "~/.codex-work"

[profiles.codex-personal]
kind = "codex"
home = "~/.codex-personal"
```

Sign in once through the native CLI for each chosen directory. Then run:

```sh
agent-hub profiles
agent-hub spawn claude-work reviewer 'Review this project' --cwd "$PWD"
agent-hub spawn codex-personal builder 'Summarize this project' --cwd "$PWD"
```

Claude profiles set `CLAUDE_CONFIG_DIR`. Codex profiles set `CODEX_HOME`.
No wrapper scripts are required for these named profiles. Profiles can also
use existing executable launchers with `command = "your-launcher"`.
See [account setup and sign-in](docs/accounts.md) for the complete steps.

## Choose a terminal

The hub automatically selects the first installed terminal in this order:
Foot, Ghostty, Kitty, Alacritty, WezTerm, then xterm. It skips Foot outside a
Wayland session. Choose a terminal for one helper with `--terminal`:

```sh
agent-hub spawn claude reviewer 'Review this project' --cwd "$PWD" --terminal ghostty
agent-hub spawn codex builder 'Summarize this project' --cwd "$PWD" --terminal kitty
```

Set `AGENT_HUB_TERMINAL=kitty` in your caller's environment to choose a default.
`--terminal` takes precedence. `--no-window` keeps the interactive PTY and lets
you attach later, without requiring a terminal emulator at spawn time.

On Hyprland 0.56 or newer, helper windows open on the spawning agent's workspace
without taking focus or switching your workspace. Nested helpers follow their
parent's current terminal workspace, including after you move it. Detached callers
with no identifiable window must set `AGENT_HUB_WORKSPACE=2` (or `name:project` /
`special:agents`) or use `--no-window`. Other desktops retain their normal placement.

Foot, Ghostty, and Alacritty have passed live window tests on the maintainer's
Linux desktop. Kitty, WezTerm, and xterm use their native command-line adapters
and have selection tests; their live windows have not been tested there yet.

## Let another agent use the CLI

The [shared skill](skill/SKILL.md) teaches the command workflow on demand. Copy
or link its directory into the caller's skill directory. For example, from this
checkout, if these destinations do not already exist:

```sh
mkdir -p "$HOME/.codex/skills" "$HOME/.claude/skills"
ln -s "$PWD/skill" "$HOME/.codex/skills/agent-hub"
ln -s "$PWD/skill" "$HOME/.claude/skills/agent-hub"
```

For another Claude or Codex account, place the same skill in that account's
configuration directory under `skills/agent-hub`. Other callers can use the CLI directly with
`agent-hub --help`. Agent integrations must pass a distinct conversation ID on
shared hosts. See [ownership and configuration](docs/reference.md).

## Resource use and limits

The hub runs one shared daemon and one Python viewer per attached terminal.
It loads `pyte` to keep a readable terminal screen. Agents and terminal emulators
consume their own resources. There is no permanently running background service
when all helpers have finished. This tool does consume memory and CPU while active.

Quiet output is the signal used by `wait`. A permission dialog, error, or finished
reply can all look quiet. Model switching depends on each agent's terminal UI and
may need updates when those UIs change. Inspect the returned screen.

Ownership isolates normal CLI workflows between conversations. Any process
running as your Unix user can access the raw socket. **The hub is not a sandbox
or a way to grant another agent permissions.** Use it within the caller's
authorized scope. See [the security notes](SECURITY.md).

## Run checks

Create a development environment in the checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m unittest -v verify regressions packaging_checks profile_checks terminal_checks
```

These checks use dummy processes, temporary settings, and real PTYs. They make
no model requests. Desktop integration checks also start temporary systemd user
services and terminal windows for the supported emulators installed on the test machine:

```sh
.venv/bin/python -m unittest -v integration
```

The integration suite needs the supported desktop environment described above.
GitHub Actions runs the local checks on Python 3.12, 3.13, and 3.14. It does not
claim to validate the desktop integration.

## Files and license

`agent_hub.py` contains the runtime. `pyproject.toml` declares the console command
and its three Python dependencies. Tests, documentation, and the optional skill
live beside it. See [the reference](docs/reference.md) for runtime paths and commands.

MIT licensed. This repository contains source and synthetic tests. It excludes
account files, machine inventories, transcripts, and local review artifacts.
