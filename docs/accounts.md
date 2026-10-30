# Set up multiple Claude and Codex accounts

Use the default `claude` or `codex` profile for a normal single-account setup.
For multiple subscriptions or workspaces, define a named profile for each native
configuration directory. Agent-hub selects that directory at launch. Each CLI
manages its own sign-in, settings, and history.

1. Create a profiles file at `~/.config/agent-hub/profiles.toml`.
   If it already exists, merge new entries into it. If `XDG_CONFIG_HOME` is set,
   use `$XDG_CONFIG_HOME/agent-hub/profiles.toml` instead.

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
   ```

   Use your existing directory paths if the accounts are already configured.
   Add more named profiles for additional accounts. Names can contain letters,
   numbers, underscores, and hyphens. The hub also accepts an explicit config
   path through `AGENT_HUB_PROFILES=/absolute/path/profiles.toml`.

2. Sign in to each new account through its native interactive CLI.

   ```sh
   CLAUDE_CONFIG_DIR="$HOME/.claude-work" claude
   CLAUDE_CONFIG_DIR="$HOME/.claude-personal" claude
   CODEX_HOME="$HOME/.codex-work" codex
   ```

   Complete one sign-in and exit that session before opening the next command.
   Existing authenticated directories do not need to be signed in again.

3. Check the configured profiles.

   ```sh
   agent-hub profiles
   agent-hub models codex-work
   ```

   The model cache belongs to the selected Codex account. If it does not exist,
   start native Codex for that directory first. Claude model aliases are shared
   by its profiles. Availability still depends on the selected account.

4. Start the profiles in your project directory.

   ```sh
   agent-hub spawn claude-work reviewer 'Review this project' --cwd "$PWD"
   agent-hub spawn claude-personal second-reviewer 'Review this project' --cwd "$PWD"
   agent-hub spawn codex-work builder 'Summarize this project' --cwd "$PWD"
   ```

   `ls` shows each helper's profile name. Model switching uses the family and
   settings directory captured when the helper started, even if you later edit
   its profile. Inspect the returned screen for native dialogs or errors.

5. Close your helpers when finished.

   ```sh
   agent-hub kill reviewer
   agent-hub kill second-reviewer
   agent-hub kill builder
   ```

## Keep existing launchers

A profile can use a wrapper executable that is already on PATH:

```toml
[profiles.claude-office]
kind = "claude"
command = "claude-office"
home = "~/.claude-office"
```

`command` is one executable name or path, without shell arguments. The wrapper
must launch the selected family interactively and use the same account directory
as `home`. A wrapper that unconditionally sets a different directory overrides
the environment supplied by the hub. Keep the profile and wrapper consistent.

The built-in `codex1` and `codex2` profiles call those executables unchanged.
See [the existing Codex launcher guide](codex-accounts.md) for their legacy
cache-directory variables. You can also override a built-in profile in the
profiles file with an explicit `kind`, `command`, and `home`.

## Native authentication still applies

Profiles contain directory paths, not credentials. The hub preserves the caller's
native authentication environment. An exported API key or OAuth token can take
precedence over the login in a selected directory. Check your provider's native
authentication status when you need to verify which subscription is active.

Claude documents configuration directories as its multiple-account mechanism.
Its Console sign-ins without an API key have an exception because that sign-in
state lives outside the directory. See [Claude's account authentication guide](https://code.claude.com/docs/en/authentication#log-in-with-multiple-accounts).

Store the shared skill in each account's configuration directory under
`skills/agent-hub` if that account needs to call the hub. The profile count is
not fixed. Concurrent helper and nesting limits still apply as documented in
[the reference](reference.md).
