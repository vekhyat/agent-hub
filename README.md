# agent-hub

Let your coding agent start other coding agents in visible terminal windows, give
them tasks, and collect their results. Use Claude, Codex, OpenCode, or Grok with
the logins you already have.

```sh
agent-hub spawn codex reviewer 'Review this project and list the top issues' --cwd "$PWD"
agent-hub wait --timeout 900          # returns the reviewer's report
agent-hub resume reviewer 'Now fix the first issue'
```

Every helper gets its own terminal window you can watch and type into. Helpers
report back with a command when they finish, so the agent that started them
doesn't have to guess. Named profiles let you mix several Claude or Codex
accounts. There is no tmux session, MCP server, or always-on service.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/vekhyat/agent-hub/main/install.sh | sh
```

The script:

- installs the `agent-hub` command with [uv](https://docs.astral.sh/uv/) if you
  have it, otherwise pipx, otherwise a private Python virtual environment;
- never replaces an `agent-hub` command it did not install;
- runs `agent-hub setup`, which checks your machine and links the agent-hub skill
  into the agents it finds (Claude, Codex, Grok, OpenCode, Pi, Gemini and
  `~/.agents`), keeping any skill you already have there.

Run the same command again to upgrade. If `setup` reports that `agent-hub` is not
on your PATH, run `uv tool update-shell` (for uv installs) or add `~/.local/bin`
to PATH, then restart your terminal and agents.

**You need:** Linux with a graphical session, Python 3.12 or newer (uv fetches it
if missing), at least one agent CLI you've signed in to, and a supported terminal:
Foot, Ghostty, Kitty, Alacritty, WezTerm, or xterm. A systemd user session with
cgroup v2 gives reliable cleanup of helper processes; without it cleanup is best
effort. Other platforms are untested.

<details>
<summary>Install by hand, check, or uninstall</summary>

```sh
uv tool install "agent-hub @ https://github.com/vekhyat/agent-hub/archive/refs/heads/main.tar.gz"
# or: pipx install https://github.com/vekhyat/agent-hub/archive/refs/heads/main.tar.gz
agent-hub setup          # check requirements and link the skill
agent-hub setup --no-skills   # only check
```

Uninstall with `uv tool uninstall agent-hub` or `pipx uninstall agent-hub`. For
the script's fallback install, remove `~/.local/share/agent-hub-venv` and
`~/.local/bin/agent-hub`. Then remove the `skills/agent-hub` links that `setup`
listed.

</details>

## Point your agent at it

The easiest setup: paste this into the coding agent you already use.

```text
Install agent-hub for me by running
curl -fsSL https://raw.githubusercontent.com/vekhyat/agent-hub/main/install.sh | sh
then follow https://raw.githubusercontent.com/vekhyat/agent-hub/main/docs/agent-setup.md.
Keep my existing logins, launchers, and permission settings. Ask me which
accounts to set up and which project directory to test in, then prove it works
by starting one helper and collecting its report.
```

Your agent installs the command, sets up any extra accounts you choose, and runs a
test helper. Sign-ins stay in each agent's own browser flow; your agent never
needs to read credential files.

Once it's set up, ask your agent in plain words and name agent-hub, for example:

- "Use agent-hub to have Codex review this branch while you write the tests."
- "Use agent-hub to start a Claude helper on my work account to fix the lint errors."
- "Use agent-hub to resume the reviewer and ask it to check your fix."

The skill only triggers when you mention agent-hub, so ordinary requests behave as
before.

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

`agent-hub setup` links the [shared skill](skill/SKILL.md) into each installed
agent's skill directory, and into the `skills` directory of every profile with a
`home`. Run it again after adding an agent or a profile. For any other caller,
link `skill/` from a checkout yourself, or let it use `agent-hub --help` directly.

Agent integrations on shared hosts must pass a distinct conversation ID. See
[ownership and configuration](docs/reference.md).

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

Create a development environment in a checkout:

```sh
git clone https://github.com/vekhyat/agent-hub.git && cd agent-hub
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
