# agent-hub

Start coding agents in visible Foot terminals. Send prompts, read their screens,
switch models, and close their processes from another agent or your terminal.

```sh
agent-hub spawn claude reviewer 'Review this project' --cwd "$PWD"
agent-hub ask reviewer 'Which issue should I fix first?'
agent-hub read reviewer --lines 100
agent-hub kill reviewer
```

The launch profiles are `claude`, `codex1`, `codex2`, `opencode`, and `grok`.
They use the agents' interactive interfaces and existing logins. Each helper
receives its own PTY and a Foot window. There is no tmux session or MCP server.

## Install

The supported environment is **Linux with Wayland, Foot, a systemd user manager,
and delegated cgroup v2 with `cgroup.kill`**. Python 3.12 or newer is required.
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

   A Foot window opens. The command returns the helper's screen, including any
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

For Codex, [set up the account launchers](docs/codex-accounts.md) first. The hub
calls `codex1` and `codex2` explicitly. It does not substitute a generic `codex`
command or create account credentials.

## Let another agent use the CLI

The [shared skill](skill/SKILL.md) teaches the command workflow on demand. Copy
or link its directory into the caller's skill directory. For example, from this
checkout, if these destinations do not already exist:

```sh
mkdir -p "$HOME/.codex/skills" "$HOME/.claude/skills"
ln -s "$PWD/skill" "$HOME/.codex/skills/agent-hub"
ln -s "$PWD/skill" "$HOME/.claude/skills/agent-hub"
```

For a second Codex account, place the same skill in that account's
`CODEX_HOME/skills/agent-hub`. Other callers can use the CLI directly with
`agent-hub --help`. Agent integrations must pass a distinct conversation ID on
shared hosts. See [ownership and configuration](docs/reference.md).

## Resource use and limits

The hub runs one shared daemon and one Python viewer per attached terminal.
It loads `pyte` to keep a readable terminal screen. Agent processes and Foot
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
.venv/bin/python -m unittest -v verify regressions packaging_checks
```

These checks use dummy processes, temporary settings, and real PTYs. They make
no model requests. Desktop integration checks also start temporary systemd user
services and a Foot window:

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
