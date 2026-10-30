# agent-hub

Start coding agents in visible terminal windows. Give them tasks, collect their
reports, read their screens, switch models, and close them from another agent or
your terminal.

```sh
agent-hub spawn claude reviewer 'Review this project and list the top issues' --cwd "$PWD"
agent-hub wait --timeout 900
agent-hub resume reviewer 'Now fix the first issue'
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
   Put the whole task in the spawn prompt.

   ```sh
   agent-hub spawn claude helper 'Summarize the files in this directory' --cwd "$PWD"
   ```

   A terminal window opens. The command returns the helper's screen, including any
   trust, sign-in, or permission dialog. Inspect that screen before continuing.

   The hub appends a short brief to the helper's first prompt. It tells the helper
   to run `agent-hub tell ID "message"` to ask the spawner a question and
   `agent-hub done ID "result"` to finish, using the hub's absolute path.

2. Wait for the helper's report.

   ```sh
   agent-hub wait --timeout 900
   ```

   Without a name, `wait` returns the first report from any of your helpers. It
   also returns when a helper has been quiet for 30 seconds without reporting,
   which usually means a permission dialog, or once all your helpers are quiet.
   Answer a `tell` question with `agent-hub send helper 'answer'`, then wait again.

3. `done` delivers the result and closes the helper. To continue later, reopen
   its conversation instead of starting over:

   ```sh
   agent-hub ls --closed
   agent-hub resume helper 'Now write the summary to NOTES.md'
   ```

   Use `agent-hub kill helper` to stop a helper early. Helpers spawned with
   `--keep` stay open after `done` and must be killed.

Claude and Codex helpers also get a per-launch turn-end hook. A helper that ends
its turn without running `done` appears in `wait` as
`[helper ended its turn without reporting]` with its last reply. Grok and
OpenCode have no hook; `wait` falls back to quiet-screen detection for them.

The daemon stops 10 seconds after the last helper closes and its reports have
been collected. Completed screens remain readable from the private runtime cache.

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
use existing executable launchers, such as `command = "codex1"`.
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

Workspace placement is optional. On Hyprland 0.56 or newer, helper windows open
on the spawning agent's workspace without taking focus or switching your workspace.
Nested helpers follow their parent's current terminal workspace, including after
you move it. Detached callers
with no identifiable window must set `AGENT_HUB_WORKSPACE=2` (or `name:project` /
`special:agents`) or use `--no-window`. Other desktops retain their normal placement.

Foot, Ghostty, and Alacritty have passed live window tests. Kitty, WezTerm, and
xterm use their native command-line adapters and have selection tests; their live
windows have not been tested yet.

## Let another agent use the CLI

The [shared skill](skill/SKILL.md) teaches the command workflow on demand. Copy
or link its directory into the caller's skill directory. For example, from this
checkout, if these destinations do not already exist:

```sh
mkdir -p "$HOME/.codex/skills" "$HOME/.claude/skills"
ln -s "$PWD/skill" "$HOME/.codex/skills/agent-hub"
ln -s "$PWD/skill" "$HOME/.claude/skills/agent-hub"
```

Installing the package also places a copy at `<prefix>/share/agent-hub/skill/`
(for `uv tool install`, inside the tool's environment), so you can link that
instead of a checkout.

For another Claude or Codex account, place the same skill in that account's
configuration directory under `skills/agent-hub`. Other callers can use the CLI directly with
`agent-hub --help`. Agent integrations must pass a distinct conversation ID on
shared hosts. See [ownership and configuration](docs/reference.md).

## Resource use and limits

The hub runs one shared daemon and one Python viewer per attached terminal.
It loads `pyte` to keep a readable terminal screen. Agents and terminal emulators
consume their own resources. There is no permanently running background service
when all helpers have finished. This tool does consume memory and CPU while active.

Helper reports are the main signal used by `wait`; quiet output is the fallback.
A permission dialog, error, or finished reply can all look quiet. A report is the
helper's own claim, so check the work before treating a task as done. Model
switching depends on each agent's terminal UI and may need updates when those UIs
change. Inspect the returned screen.

Forgotten idle helpers owned by an agent are closed after `AGENT_HUB_MAX_IDLE`
seconds (default 1800; `0` disables). `wait` reports the closure. `--keep` helpers
are exempt. At most 8 helpers run at once and nesting stops at depth 2 by default.

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
GitHub Actions installs the package and runs the local checks against that install,
from outside the checkout, on Python 3.12, 3.13, and 3.14. It does not claim to
validate the desktop integration.

## Files and license

`agent_hub.py` contains the runtime. `pyproject.toml` declares the console command
and its three Python dependencies. Tests, documentation, and the optional skill
live beside it. See [the reference](docs/reference.md) for runtime paths and commands.

MIT licensed. This repository contains source and synthetic tests. It excludes
account files, machine inventories, transcripts, and local review artifacts.
