# Set up agent-hub for your user

Use this guide when the user asks you to install or configure agent-hub. Work
within that request. Preserve existing logins, executable launchers, permission
settings, and unrelated configuration. Credentials remain in the native CLIs.

1. Inspect the environment and identify the user's accounts.

   Check Linux, Python 3.12 or newer, the systemd user manager, cgroup v2,
   and available terminals: Foot, Ghostty, Kitty, Alacritty, WezTerm, or xterm.
   Foot requires Wayland. Check installed agent executables and any existing
   `agent-hub` command. Find configuration directory paths from the user's
   existing launchers or supplied account names. Read path metadata only.
   Ask for any missing account choice or project directory. Do not inspect or
   copy credential files, invent accounts, or alter native permission modes.

2. Install the project or reuse an existing suitable installation.

   ```sh
   git clone https://github.com/vekhyat/agent-hub.git
   cd agent-hub
   uv tool install .
   agent-hub --help
   ```

   Reuse an existing checkout instead of cloning over it. Inspect a pre-existing
   command before replacing it. For a requested upgrade of this uv-installed
   package, use `uv tool install --reinstall .` from the updated checkout.
   Do not force an install over an unrelated executable or local launcher.
   If uv is unavailable, use the virtual-environment steps in the README.
   Ensure the calling agent can find the command on PATH.

3. Configure the accounts and terminal the user chose.

   Read [account setup](accounts.md) and `examples/profiles.toml` in the checkout.
   Merge named profiles into the user's existing profiles file. Claude uses
   `CLAUDE_CONFIG_DIR`; Codex uses `CODEX_HOME`. Existing launchers such as
   `codex1` are no longer built-in profiles; keep working ones by adding a profile
   with `command` set to the launcher, as in [the launcher guide](codex-accounts.md). Use native interactive sign-in only when a chosen
   directory is new. Return browser authentication steps to the user.
   Pick a supported installed terminal or let the hub select one automatically.
   `--terminal ghostty` or `--terminal kitty` overrides the choice for one spawn.
   `AGENT_HUB_TERMINAL` sets the caller's default without editing terminal config.

4. Make the skill discoverable to the requested callers.

   Link or copy this checkout's `skill` directory into each chosen account's
   skills directory as `agent-hub`. Claude and Codex use `skills/agent-hub` under
   their selected account configuration directory. Reuse a matching existing
   link. Preserve other existing files and ask before replacing a conflicting
   skill installation. Other callers can use `agent-hub --help` directly or
   install the same skill in their own discovery directory.
   On shared Codex app-server hosts, use `CODEX_THREAD_ID` or a unique
   `AGENT_HUB_CALLER_ID` per conversation. See [the reference](reference.md).

5. Verify the result and report exact paths.

   Run `agent-hub profiles`. With a signed-in profile chosen by the user, spawn
   a helper with a small prompt such as `Reply READY`, the user's required
   `--cwd /absolute/project/path`, and the selected terminal. Inspect the returned
   screen, then run `agent-hub wait --timeout 120`. A `[NAME done; closed]` report
   shows the helper reached the hub. Quiet output can be a permission or sign-in
   dialog. Native approvals stay with the user unless the current request already
   authorizes them. Close a helper that did not report with `agent-hub kill NAME`. Report the checkout path,
   executable path, profiles file, skill links, and any remaining sign-in step.
   Distinguish account routing from proof of which subscription authenticated.

This tool runs interactive agent CLIs in real PTYs. It does not require tmux,
headless print modes, an MCP server, or a permanently running background service.
The daemon starts on demand and exits after its last helper has finished.
