# Set up Codex account launchers

For new account setups, [named profiles](accounts.md) use any number of Claude
or Codex accounts without wrapper scripts. This page covers existing
`codex1` and `codex2` launchers.

The hub calls executable commands named `codex1` and `codex2`. Each command must
launch interactive Codex with its own `CODEX_HOME`. Shell aliases and functions
are not executables and cannot be launched by the hub.

1. If you already have working `codex1` and `codex2` executables, keep them.
   Set the hub's cache paths to match their account directories. For example:

   ```sh
   export AGENT_HUB_CODEX1_HOME="$HOME/.codex"
   export AGENT_HUB_CODEX2_HOME="$HOME/.codex2"
   ```

   Replace these values with your actual directories. Put the exports in the
   environment of the agent that calls the hub. These variables tell
   `agent-hub models` where to read the model caches. Your launchers remain
   responsible for selecting the account used by the spawned process.

2. If you have no account launchers, install the examples from this checkout.
   Do this only when both destination files are absent.

   ```sh
   mkdir -p "$HOME/.local/bin"
   install -m 755 examples/codex1 "$HOME/.local/bin/codex1"
   install -m 755 examples/codex2 "$HOME/.local/bin/codex2"
   ```

   Make sure `$HOME/.local/bin` and the native `codex` executable are on PATH.
   The examples use `~/.codex` and `~/.codex2` by default. They also honor the
   two exports above, so launching and cache lookup use the same directories.

3. Sign in to each account interactively if it is not already configured.

   ```sh
   codex1
   codex2
   ```

   Complete the first session and exit before opening the second. The hub does
   not copy credentials or add permission-skipping flags.

4. Launch either account in your project.

   ```sh
   agent-hub spawn codex1 first 'Reply READY' --cwd "$PWD"
   agent-hub spawn codex2 second 'Reply READY' --cwd "$PWD"
   ```

5. Close both helpers after checking their replies.

   ```sh
   agent-hub kill first
   agent-hub kill second
   ```

`agent-hub models codex1` and `agent-hub models codex2` read each account's
`models_cache.json`. Start native Codex in that account first if its cache does
not exist. A custom wrapper can place the account anywhere, provided the hub's
corresponding `AGENT_HUB_CODEX1_HOME` or `AGENT_HUB_CODEX2_HOME` matches it.
