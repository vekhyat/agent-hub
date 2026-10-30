# Keep existing Codex account launchers

For new account setups, [named profiles](accounts.md) use any number of Claude
or Codex accounts without wrapper scripts. This page covers existing launcher
executables, such as `codex1` and `codex2`, that each start Codex with their own
`CODEX_HOME`.

Earlier versions had built-in `codex1` and `codex2` profiles. They no longer
exist; each launcher is now a profile. Shell aliases and functions are not
executables and cannot be launched by the hub.

1. Define a profile for each launcher in `~/.config/agent-hub/profiles.toml`.

   ```toml
   [profiles.codex1]
   kind = "codex"
   command = "codex1"
   home = "~/.codex"

   [profiles.codex2]
   kind = "codex"
   command = "codex2"
   home = "~/.codex2"
   ```

   Replace each `home` with the directory its launcher selects. The hub sets
   `CODEX_HOME` to it and reads `models_cache.json` there for `agent-hub models`.
   A launcher that unconditionally sets a different directory overrides the hub,
   so keep the two consistent. Always set `home`: without it, `resume` looks
   for that account's sessions in the `CODEX_HOME` the helper inherited, or
   `~/.codex`, and `agent-hub models` in `${CODEX_HOME:-~/.codex}`.
   Alternatively, drop the launchers and use profiles with only `kind` and
   `home`.

2. Check the profiles and their model caches.

   ```sh
   agent-hub profiles
   agent-hub models codex1
   agent-hub models codex2
   ```

   Start native Codex in an account first if its cache does not exist.

3. Launch either account in your project.

   ```sh
   agent-hub spawn codex1 first 'Reply READY' --cwd "$PWD"
   agent-hub spawn codex2 second 'Reply READY' --cwd "$PWD"
   agent-hub wait --timeout 300
   ```

   Each helper closes itself after reporting with `done`. Use
   `agent-hub kill NAME` for any still listed by `agent-hub ls`.

`AGENT_HUB_CODEX1_HOME` and `AGENT_HUB_CODEX2_HOME` are no longer read. Use each
profile's `home` instead.
