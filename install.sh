#!/bin/sh
# Install or upgrade agent-hub for the current user:
#   curl -fsSL https://raw.githubusercontent.com/vekhyat/agent-hub/main/install.sh | sh
# Uses uv, then pipx, then a private virtual environment. Never replaces a command it did not install.
set -eu

SOURCE="${AGENT_HUB_SOURCE:-https://github.com/vekhyat/agent-hub/archive/refs/heads/main.tar.gz}"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/agent-hub-venv"
BIN="$HOME/.local/bin"

say() { printf '==> %s\n' "$*"; }
fail() { printf 'agent-hub install: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || fail "agent-hub supports Linux only"

existing=$(command -v agent-hub 2>/dev/null || true)

if command -v uv >/dev/null 2>&1; then
    say "installing with uv"
    uv tool install --reinstall "agent-hub @ $SOURCE" \
        || fail "uv could not install it; if another agent-hub is in the way, remove it and run again"
    installed="$(uv tool dir --bin)/agent-hub"
elif command -v pipx >/dev/null 2>&1; then
    say "installing with pipx"
    if pipx list --short 2>/dev/null | grep -q '^agent-hub '; then
        pipx install --force "$SOURCE"
    else
        pipx install "$SOURCE" || fail "pipx could not install it; if another agent-hub is in the way, remove it"
    fi
    installed="$(pipx environment --value PIPX_BIN_DIR)/agent-hub"
else
    python=""
    for candidate in python3.14 python3.13 python3.12 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
                "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
            python=$candidate
            break
        fi
    done
    [ -n "$python" ] || fail "needs Python 3.12 or newer, or uv (https://docs.astral.sh/uv/) to fetch one"
    if [ -e "$BIN/agent-hub" ] && [ "$(readlink -f "$BIN/agent-hub")" != "$(readlink -f "$DATA/bin/agent-hub")" ]; then
        fail "$BIN/agent-hub already exists and was not installed by this script; remove it and run again"
    fi
    say "installing into $DATA with $python"
    "$python" -m venv "$DATA" || fail "cannot create a virtual environment; install your distribution's python venv package"
    "$DATA/bin/python" -m pip install --quiet --upgrade "$SOURCE"
    mkdir -p "$BIN"
    ln -sf "$DATA/bin/agent-hub" "$BIN/agent-hub"
    installed="$BIN/agent-hub"
fi

[ -x "$installed" ] || fail "installed, but $installed is missing"
if [ -n "$existing" ] && [ "$(readlink -f "$existing")" != "$(readlink -f "$installed")" ]; then
    say "note: another agent-hub comes first on PATH: $existing"
fi

say "checking this machine and linking the skill for installed agents"
"$installed" setup || true
say "done. Upgrade later by running the same command again."
