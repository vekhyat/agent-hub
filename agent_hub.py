#!/usr/bin/env python3
"""agent-hub: let any agent spawn, drive and close other agents in real terminals.

One file, three roles:
  agent-hub --help         on-demand CLI for agents and humans
  agent_hub.py daemon      long-lived process that owns every agent's PTY (auto-started)
  agent_hub.py attach N    terminal client: watch / type into agent N (Ctrl-] detaches)
  agent_hub.py ls | kill N human helpers

The daemon keeps a pyte screen per agent so other agents can read what it shows,
and streams raw output to attached terminal windows.

CLI only: no MCP registration, SDK, loaded tool schemas or per-agent server.
The daemon exits when no sessions remain for 10 seconds.

Agents see only helpers owned by their conversation or standalone CLI process.
Helpers report back with `agent-hub tell` and `agent-hub done`; `done` closes them. Spawners
collect reports with `agent-hub wait`. Forgotten idle helpers are closed after AGENT_HUB_MAX_IDLE.
`agent-hub resume` reopens a closed helper with its conversation.

Humans, through `attach`, `ls` and `kill`, can reach every agent. Any process running as
this user can still talk to the socket directly; this is not a security boundary.
"""

from __future__ import annotations

import contextlib
import fcntl
import glob
import hashlib
import shutil
import json
import os
import re
import select
import shlex
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tty
import uuid

# Fixed path: callers with different environments must find the same daemon.
RUNTIME = f"/run/user/{os.getuid()}" if os.path.isdir(f"/run/user/{os.getuid()}") else f"/tmp/agent-hub-{os.getuid()}"
SOCK = os.path.join(RUNTIME, "agent-hub.sock")
LOG = os.path.join(RUNTIME, "agent-hub.log")
SETTINGS_LOCK = os.path.join(RUNTIME, "agent-hub-settings.lock")
IDLE_SECONDS = 10
TERMINATE_GRACE = 3
VIEWER_TIMEOUT = 8
DONE_GRACE = 2  # lets `agent-hub done` return before its own session closes
MAX_REPORT = 32 * 1024
REPORT_TTL = 3600  # unread helper reports are dropped after this long
BLOCKED_AFTER = 30  # a quiet helper that has not reported by then is likely on a dialog
WARNINGS: list[str] = []
DEPTH = int(os.environ.get("AGENT_HUB_DEPTH", "0"))
PARENT = os.environ.get("AGENT_HUB_NAME", "")  # set when this process runs inside a hub agent
MAX_DEPTH = int(os.environ.get("AGENT_HUB_MAX_DEPTH", "2"))
MAX_AGENTS = int(os.environ.get("AGENT_HUB_MAX_AGENTS", "8"))
MAX_IDLE = int(os.environ.get("AGENT_HUB_MAX_IDLE", "1800"))  # close forgotten helpers; 0 disables
TERMINALS = {
    "foot": ["foot"],
    "ghostty": ["ghostty", "--gtk-single-instance=false", "-e"],
    "kitty": ["kitty"],
    "alacritty": ["alacritty", "-e"],
    "wezterm": ["wezterm", "start", "--always-new-process", "--"],
    "xterm": ["xterm", "-e"],
}


def terminal_command(choice: str = "", env: dict | None = None) -> list[str]:
    env = os.environ if env is None else env
    choice = choice or env.get("AGENT_HUB_TERMINAL") or "auto"
    if choice != "auto" and choice not in TERMINALS:
        raise ValueError(f"unknown terminal {choice!r}; choose auto or {', '.join(TERMINALS)}")
    for name in TERMINALS if choice == "auto" else [choice]:
        if choice == "auto" and name == "foot" and not env.get("WAYLAND_DISPLAY"):
            continue
        command, *args = TERMINALS[name]
        binary = shutil.which(command, path=env.get("PATH", os.defpath))
        if binary:
            return [binary, *args]
    raise ValueError(f"terminal {choice!r} is not available; install a supported terminal or use --no-window")


def process_start(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def hyprland_request(args: list[str], env: dict) -> str:
    try:
        result = subprocess.run(["hyprctl", *args], env=env, capture_output=True,
                                text=True, timeout=3, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"cannot place agent terminal through Hyprland: {error}") from error
    if result.returncode:
        raise RuntimeError("cannot place agent terminal through Hyprland: " +
                           (result.stderr or result.stdout).strip())
    return result.stdout.strip()


def hyprland_clients(env: dict) -> list[dict]:
    try:
        clients = json.loads(hyprland_request(["-j", "clients"], env))
        if isinstance(clients, list):
            return clients
    except ValueError:
        pass
    raise RuntimeError("Hyprland returned an invalid window list")


def window_workspace(window: dict) -> str:
    workspace = window.get("workspace", {})
    number = workspace.get("id", 0)
    name = workspace.get("name", "")
    if number > 0:
        return str(number)
    return name if name.startswith("special:") else "name:" + name if name else ""


def process_workspace(clients: list[dict], pid: int, viewer: bool = False) -> str:
    """Match process ancestry, never the currently focused window."""
    windows = {window["pid"]: window for window in clients if window.get("mapped", True)}
    for start in windows if viewer else [pid]:
        current, seen = start, set()
        while current > 1 and current not in seen:
            seen.add(current)
            if (viewer and current == pid) or (not viewer and current in windows):
                return window_workspace(windows[start if viewer else current])
            try:
                with open(f"/proc/{current}/stat") as stream:
                    current = int(stream.read().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                break
    return ""


def caller_workspace(env: dict) -> str:
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return ""
    return (process_workspace(hyprland_clients(env), os.getpid()) or
            env.get("AGENT_HUB_WORKSPACE", ""))


def background_terminal(terminal: list[str], workspace: str, env: dict) -> list[str]:
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return terminal
    if not workspace:
        raise RuntimeError("cannot locate the spawning agent's Hyprland window; "
                           "set AGENT_HUB_WORKSPACE to its workspace or use --no-window")
    if not re.fullmatch(r"(?:[1-9][0-9]*|(?:name|special):[^\x00-\x1f]+)", workspace):
        raise ValueError("AGENT_HUB_WORKSPACE must be a workspace number, name:NAME, or special:NAME")
    token = "agent-hub-" + hashlib.sha256(workspace.encode()).hexdigest()[:16]
    name = os.path.basename(terminal[0])
    # WezTerm exposes --class on start; the others can retain their normal app ID.
    prop = "initial_class" if name == "wezterm" else "initial_title"
    code = ("hl.window_rule({name=" + json.dumps(token + "-" + prop) +
            ",match={" + prop + "=" + json.dumps("^" + token + "$") + "},workspace=" +
            json.dumps(workspace + " silent", ensure_ascii=False) + ",no_initial_focus=true})")
    reply = hyprland_request(["eval", code], env)
    if reply != "ok":
        raise RuntimeError("cannot set background terminal rule (requires Hyprland 0.56+): " + reply)
    options = {
        "foot": ["--title=" + token], "ghostty": ["--title=" + token],
        "kitty": ["--title", token], "alacritty": ["--title", token],
        "wezterm": ["--class", token], "xterm": ["-T", token],
    }
    index = 2 if name == "wezterm" else 1
    return [*terminal[:index], *options[name], *terminal[index:]]


def helper_brief(report_id: str, keep: bool, hub: str = "") -> str:
    return (HELPER_BRIEF.format(hub=hub or HUB_COMMAND, id=report_id) +
            ("." if keep else "; it delivers the result and closes this session."))


def caller_owner() -> str | None:
    """Conversation identity where available; private process identity for automation."""
    pid = os.getppid()
    fallback = None
    terminal = False
    context = next((os.environ[k] for k in (
        "AGENT_HUB_CALLER_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_SESSION_ID", "OPENCODE_SESSION_ID", "PI_SESSION_ID",
    ) if os.environ.get(k)), "")
    while pid > 1:
        try:
            proc = f"/proc/{pid}"
            with open(proc + "/stat") as f:
                stat = f.read().rsplit(")", 1)[1].split()
            with open(proc + "/cmdline", "rb") as f:
                all_args = f.read().split(b"\0")
                args = all_args[:2]
            executable = os.path.basename(os.fsdecode(args[0])).lower()
            if any(os.path.basename(os.fsdecode(a)).lower() in AGENT_PROCESS_NAMES
                   or "/pi-coding-agent/" in os.fsdecode(a) for a in args):
                if executable == "codex" and b"app-server" in all_args:
                    context = os.environ.get("AGENT_HUB_CALLER_ID") or os.environ.get("CODEX_THREAD_ID", "")
                    if not context:
                        raise RuntimeError("Codex app-server requires a conversation ID (CODEX_THREAD_ID or AGENT_HUB_CALLER_ID)")
                suffix = ":" + hashlib.sha256(context.encode()).hexdigest()[:24] if context else ""
                return f"proc:{pid}:{stat[19]}{suffix}"
            if executable in {*TERMINALS, "footclient", "konsole", "wezterm-gui"}:
                terminal = fallback is None
            elif fallback is None and executable not in {"bash", "sh", "zsh", "fish", "dash", "env", "timeout", "setsid"}:
                fallback = (pid, stat[19])
            pid = int(stat[1])
        except (OSError, ValueError, IndexError):
            break
    if terminal and not context and sys.stdin.isatty() and sys.stdout.isatty():
        return None
    anchor, started = fallback or (os.getppid(), process_start(os.getppid()) or "unknown")
    suffix = ":" + hashlib.sha256(context.encode()).hexdigest()[:24] if context else ""
    return f"proc:{anchor}:{started}{suffix}"


OWNER: str | None = None
# Ctrl-] as a raw byte, as a kitty keyboard protocol key, or as an xterm modifyOtherKeys key.
DETACH = re.compile(rb"\x1d|\x1b\[93(?::\d+)*;5(?::[12])?u|\x1b\[27;5;93~")
# Replies the viewer's terminal sends to capability queries (XTVERSION, DA, DECRQM, kitty
# keyboard/graphics, OSC colors, cell size). Forwarding them makes TUIs read stray "u"/"c"
# keystrokes, so viewers only pass real input and agents see a plain terminal.
# String bodies are [^\x1b]* rather than .*? so a flood of unterminated ones stays linear.
TERMINAL_REPLIES = re.compile(
    rb"\x1bP[^\x1b]*\x1b\\|\x1b_[^\x1b]*\x1b\\|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    rb"|\x1b\[[?>][\d;]*[cu]|\x1b\[\??[\d;]*\$y|\x1b\[\d+;\d+;\d+t"
)
# An escape sequence cut off at the end of a read; held back briefly so a split reply is
# filtered whole. A lone ESC (the Escape key) matches too and is flushed after 50 ms.
INCOMPLETE_ESC = re.compile(rb"\x1b(?:[P_\]][^\x07\x1b]*\x1b?|\[[\x30-\x3f]*[\x20-\x2f]*)?\Z")
# Output sequences pyte does not parse and would print as text: DCS and APC strings
# (kitty graphics "Ga=d,..."), and CSI with <, > or = prefixes (kitty keyboard push/pop "u").

# kind -> (argv, how to pass an initial prompt). "--" / "=" keep a prompt from parsing as a flag.
INTERACTIVE = {
    "claude": (["claude"], lambda p: ["--", p]),
    "codex": (["codex"], lambda p: ["--", p]),
    "opencode": (["opencode", "--standalone"], lambda p: [f"--prompt={p}"]),
    "grok": (["grok"], lambda p: ["--", p]),
}
# kind -> (args naming a new session, args reopening one). Codex names its own sessions.
SESSION_ARGS = {
    "claude": (lambda i: ["--session-id", i], lambda i: ["--resume", i]),
    "codex": (lambda i: [], lambda i: ["resume", i]),
    "opencode": (lambda i: ["--session", i], lambda i: ["--session", i]),
    "grok": (lambda i: ["--session-id", i], lambda i: ["--resume", i]),
}
HELPER_RECORDS = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
                              "agent-hub/helpers")
MAX_RECORDS = 200
AGENT_PROCESS_NAMES = set(INTERACTIVE) | {"codex", "pi", "agy", "gemini", "cursor-agent", "amp", "t3"}
CODEX_HOMES = {
    "codex": os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex")),
}
SAFE_VALUE = re.compile(r"[A-Za-z0-9._:/#@\[\]-]{1,100}")  # model / effort / flag values
# Lowercase text each TUI shows near the bottom only while it is busy generating / running tools.
BUSY_MARKERS = {
    "claude": ["esc to interrupt"],
    "codex": ["esc to interrupt"],
    "grok": ["ctrl+c:cancel"],
    "opencode": ["esc interrupt", "esc to interrupt"],
}
# Spelled out in the prompt because Codex runs shell commands from a shared server without this session's env.
HELPER_BRIEF = ("\n\n[agent-hub] REQUIRED: another agent gave you this task and cannot see your chat replies. "
                "To reach it you must run a shell command. To ask a question, run "
                "`{hub} tell {id} \"<message>\"` and end your turn; the reply arrives as your next message. "
                "Your final action, even for a trivial task, must be running `{hub} done {id} \"<your full answer>\"`")
# Absolute, because helpers run it from shells that may not share the caller's PATH.
# An installed module uses the console script beside its interpreter; a checkout runs itself.
# Either way it is this copy, not whichever agent-hub comes first on PATH.
_HUB_SCRIPT = shutil.which("agent-hub", path=os.path.dirname(sys.executable))
_INSTALLED = os.path.realpath(__file__).startswith(os.path.realpath(sys.prefix) + os.sep)
HUB_ARGV = ([os.path.abspath(_HUB_SCRIPT)] if _HUB_SCRIPT and _INSTALLED
            else [sys.executable, os.path.abspath(__file__)])
HUB_COMMAND = shlex.join(HUB_ARGV)
BUSY_SCAN_LINES = 10  # only the status area: an old marker higher in scrollback must not count
# Variables a parent agent sets that make a nested agent think it is a child/tool process.
PARENT_ENV = re.compile(r"^(CLAUDECODE|CLAUDE_CODE_|CLAUDE_PID$|CLAUDE_EFFORT$|CODEX_(THREAD|SANDBOX|SESSION)"
                        r"|OPENCODE$|OPENCODE_SESSION)")
HOME = os.path.expanduser("~")
# How each CLI takes a model / reasoning effort at launch. grok and opencode use config
# overlays in spawn_agent instead (grok's -m flag saves itself as the user's default).
LAUNCH_MODEL = {
    "claude": lambda m, e: (["--model", m] if m else []) + (["--effort", e] if e else []),
    **{kind: lambda m, e: (["-m", m] if m else []) + (["-c", f"model_reasoning_effort={e}"] if e else [])
       for kind in CODEX_HOMES},
}
# Files that an in-session /model or /effort rewrites as the user's new default.
# set_model restores them so a switch only affects that one agent.
MODEL_DEFAULTS_FILE = {
    "claude": os.path.join(HOME, ".claude/settings.json"),
    "grok": os.path.join(HOME, ".grok/config.toml"),
    "opencode": os.path.join(os.environ.get("XDG_STATE_HOME", os.path.join(HOME, ".local/state")),
                             "opencode/model.json"),
}


def load_profiles() -> dict[str, dict[str, str]]:
    """Read user-defined account profiles only when a CLI operation needs them."""
    profiles = {name: {"kind": "codex" if name in CODEX_HOMES else name,
                       "command": argv[0], "home": ""}
                for name, (argv, _) in INTERACTIVE.items()}
    path = os.path.expanduser(os.environ.get("AGENT_HUB_PROFILES") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "agent-hub", "profiles.toml"))
    try:
        with open(path, "rb") as f:
            import tomllib
            document = tomllib.load(f)
    except FileNotFoundError:
        if os.environ.get("AGENT_HUB_PROFILES"):
            raise ValueError(f"profiles file does not exist: {path}") from None
        return profiles
    except ValueError as e:
        raise ValueError(f"invalid profiles file {path}: {e}") from e
    configured = document.get("profiles", {})
    if set(document) != {"profiles"} or not isinstance(configured, dict):
        raise ValueError(f"{path}: expected [profiles.NAME] tables")
    for name, values in configured.items():
        label = f"{path}: profile {name!r}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            raise ValueError(f"{label}: use letters, digits, underscores or hyphens")
        if not isinstance(values, dict) or set(values) - {"kind", "command", "home"}:
            raise ValueError(f"{label}: expected kind, command and optional home")
        base = profiles.get(name, {})
        kind = values.get("kind", base.get("kind"))
        if kind not in ("claude", "codex", "grok", "opencode"):
            raise ValueError(f"{label}: kind must be claude, codex, grok or opencode")
        command = values.get("command", base.get("command", kind) if kind == base.get("kind") else kind)
        home = values.get("home", base.get("home", ""))
        if not isinstance(command, str) or not command or command.startswith("-") or "\0" in command:
            raise ValueError(f"{label}: command must be one executable name or path")
        if not isinstance(home, str) or "\0" in home:
            raise ValueError(f"{label}: home must be a directory path")
        if home:
            home = os.path.expanduser(home)
            if kind not in ("claude", "codex") or not os.path.isabs(home):
                raise ValueError(f"{label}: home needs an absolute or ~/ path for Claude or Codex")
        profiles[name] = {"kind": kind, "command": os.path.expanduser(command), "home": home}
    return profiles


def get_profile(name: str) -> dict[str, str]:
    profiles = load_profiles()
    if name not in profiles:
        raise ValueError(f"unknown profile {name!r}; use agent-hub profiles")
    return profiles[name]


def list_profiles() -> str:
    return "\n".join(f"{name}\t{p['kind']}\t{p['command']}\t{p['home'] or '(native defaults)'}"
                     for name, p in load_profiles().items())


KEYS = {
    "Enter": "\r", "Escape": "\x1b", "Tab": "\t", "BTab": "\x1b[Z", "BSpace": "\x7f", "Space": " ",
    "Up": "\x1b[A", "Down": "\x1b[B", "Right": "\x1b[C", "Left": "\x1b[D",
    "Home": "\x1b[H", "End": "\x1b[F", "PageUp": "\x1b[5~", "PageDown": "\x1b[6~", "Delete": "\x1b[3~",
}
# Screen lines that carry no text for an agent reading them: borders, block-art logos, braille art.
DECORATION = re.compile(r"[\s─-▟⠀-⣿]*")

# ---------------------------------------------------------------- wire format
# Every message is a frame: 1-byte type + 4-byte length + payload.
# J = JSON request/response, O = output bytes, I = input bytes, R = resize, X = session ended.
HDR = struct.Struct("!cI")


def frame(kind: bytes, payload: bytes) -> bytes:
    return HDR.pack(kind, len(payload)) + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    kind, n = HDR.unpack(await reader.readexactly(HDR.size))
    if n > 4 * 1024 * 1024:
        raise ValueError("frame exceeds 4 MiB")
    return kind, await reader.readexactly(n)


def recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("agent-hub daemon closed the connection")
        buf += chunk
    return buf


def daemon_unit() -> str:
    return "agent-hub-" + hashlib.sha256(SOCK.encode()).hexdigest()[:12]


def start_daemon() -> bool:
    os.makedirs(RUNTIME, mode=0o700, exist_ok=True)
    argv = [sys.executable, os.path.abspath(__file__), "daemon"]
    manager = f"/run/user/{os.getuid()}/systemd/private"
    if os.path.exists(manager) and shutil.which("systemd-run"):
        result = subprocess.run([
            "systemd-run", "--user", "--quiet", "--collect", "--unit=" + daemon_unit(),
            "--property=Type=exec", "--property=KillMode=mixed", "--property=TimeoutStopSec=10s",
            "--property=Delegate=yes",
            "--property=StandardOutput=append:" + LOG, "--property=StandardError=append:" + LOG,
            *argv,
        ], capture_output=True, text=True, timeout=8)
        if result.returncode:
            if "already loaded" in result.stderr or "already exists" in result.stderr:
                return False
            active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", daemon_unit()], timeout=3)
            if active.returncode:
                raise RuntimeError("cannot start isolated agent-hub service: " + result.stderr.strip())
            return False
    else:
        with open(LOG, "a") as log:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             cwd="/", start_new_session=True)
    return True


def connect(autostart: bool = False) -> socket.socket:
    """Connect to the daemon, starting it if it is not running."""
    started = False
    deadline = time.monotonic() + 10
    while True:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(20)
        try:
            s.connect(SOCK)
            return s
        except (FileNotFoundError, ConnectionRefusedError):
            s.close()
            if not autostart:
                raise RuntimeError("no running agents; use agent-hub spawn first")
            if time.monotonic() > deadline:
                raise RuntimeError(f"agent-hub daemon did not start; see {LOG}")
            if not started:
                started = start_daemon()
            time.sleep(0.1)


def request(op: str, **args) -> dict:
    if OWNER:
        args["owner"] = OWNER
    deadline = time.monotonic() + 25
    while True:
        try:
            with connect(autostart=op == "spawn") as connection:
                connection.sendall(frame(b"J", json.dumps({"op": op, **args}).encode()))
                _, size = HDR.unpack(recv_exact(connection, HDR.size))
                if size > 4 * 1024 * 1024:
                    raise RuntimeError("response exceeds 4 MiB")
                response = json.loads(recv_exact(connection, size))
        except RuntimeError:
            if op == "list":
                return {"agents": []}
            cached = archived(args.get("name", ""), OWNER) if op in ("screen", "info", "kill") else None
            if cached:
                return {"text": "\n".join(cached["text"].splitlines()[-args.get("lines", 80):])} if op == "screen" else cached
            raise
        if response.get("retry") and op == "spawn":
            if time.monotonic() >= deadline:
                raise RuntimeError("daemon is still shutting down; retry spawn")
            time.sleep(.1)
            continue
        if "error" in response:
            raise RuntimeError(response["error"])
        return response


# ---------------------------------------------------------------- daemon


def set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def descendants(pid: int) -> list[int]:
    """Every process below pid, including ones that left its process group with setsid."""
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                with open(f"/proc/{entry}/stat") as f:
                    ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
            children.setdefault(ppid, []).append(int(entry))
    found, todo = [], [pid]
    while todo:
        kids = children.get(todo.pop(), [])
        found += kids
        todo += kids
    return found


class ScreenFilter:
    """Discard unsupported strings/CSI incrementally, without buffering string bodies."""
    def __init__(self):
        self.state = "text"
        self.sequence = bytearray()
        self.overflow = False

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        for b in data:
            if self.state != "text" and b in (24, 26):
                self.state = "text"
                self.sequence.clear()
            elif self.state == "string":
                if b == 27:
                    self.state = "string_escape"
            elif self.state == "string_escape":
                self.state = "text" if b == 92 else "string_escape" if b == 27 else "string"
            elif self.state == "escape":
                if b in (80, 95):
                    self.state = "string"
                elif b == 91:
                    self.sequence = bytearray(b"\x1b[")
                    self.overflow = False
                    self.state = "csi"
                else:
                    out.extend((27, b))
                    self.state = "text"
            elif self.state == "csi":
                if b == 27:
                    self.state = "escape"
                    self.sequence.clear()
                    continue
                if len(self.sequence) < 256:
                    self.sequence.append(b)
                else:
                    self.overflow = True
                if 64 <= b <= 126:
                    if not self.overflow and self.sequence[2:3] not in (b"<", b">", b"="):
                        out.extend(self.sequence)
                    self.sequence.clear()
                    self.state = "text"
            elif b == 27:
                self.state = "escape"
            else:
                out.append(b)
        return bytes(out)


def archive_directory() -> str:
    return os.path.join(RUNTIME, "agent-hub-screens")


def archive_path(name: str, owner: str | None) -> str:
    key = hashlib.sha256(json.dumps([owner, name]).encode()).hexdigest()
    return os.path.join(archive_directory(), key + ".json")


def session_cgroup_base():
    try:
        with open("/proc/self/cgroup") as f:
            relative = next(line[3:].strip() for line in f if line.startswith("0::"))
    except (OSError, StopIteration):
        return None
    if re.search(r"/agent-hub-[0-9a-f]{12}\.service(?:/|$)", relative):
        return "/sys/fs/cgroup" + relative
    return None


def create_session_cgroup():
    base = session_cgroup_base()
    if base is None:
        return None
    path = os.path.join(base, "helper-" + uuid.uuid4().hex)
    try:
        os.mkdir(path, 0o700)
        if not os.access(os.path.join(path, "cgroup.kill"), os.W_OK):
            os.rmdir(path)
            raise OSError("cgroup.kill is not writable")
    except OSError as e:
        raise RuntimeError("managed helper cgroups need delegation; close old sessions and restart the hub") from e
    return path


def cgroup_populated(path):
    try:
        with open(os.path.join(path, "cgroup.events")) as f:
            return "populated 1" in f.read()
    except FileNotFoundError:
        return False


def cgroup_members(path):
    members = []
    for root, _, _ in os.walk(path):
        with open(os.path.join(root, "cgroup.procs")) as f:
            members.extend(int(pid) for pid in f.read().split())
    return members


def archived(name: str, owner: str | None) -> dict | None:
    paths = [archive_path(name, owner)] if owner is not None else []
    if owner is None:
        with contextlib.suppress(FileNotFoundError):
            paths = [os.path.join(archive_directory(), p) for p in os.listdir(archive_directory())]
    found = []
    for path in paths:
        try:
            with open(path) as f:
                item = json.load(f)
            if item["name"] == name and (owner is None or item["owner"] == owner):
                found.append(item)
        except (OSError, ValueError, KeyError):
            continue
    return max(found, key=lambda x: x["saved_at"]) if found else None


class Session:
    def __init__(self, name, kind, argv, cwd, env, owner, depth, cols=120, rows=36):
        import pyte
        self.name, self.kind, self.cwd, self.owner, self.depth = name, kind, cwd, owner, depth
        self.profile = kind
        self.settings_file = MODEL_DEFAULTS_FILE.get(kind)
        if kind == "claude":
            directory = os.path.expanduser(env.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude"))
            self.settings_file = os.path.normpath(os.path.join(cwd, directory, "settings.json"))
        self.parent = ""
        self.screen = pyte.HistoryScreen(cols, rows, history=200)
        self.stream = pyte.ByteStream(self.screen)
        self.filter = ScreenFilter()
        self.viewers = set()
        self.attached = asyncio.Event()
        self.last_output = time.time()
        self.exit_status = None
        self.kill_task = None
        self.viewer_process = None
        self.workspace = ""
        self.terminal_argv = []
        self.keep = False
        self.briefed = False
        self.report_id = ""
        self.reported = False  # told its spawner something during this turn
        self.announced = 0.0  # last_output when `wait` last showed this helper as quiet
        self.hub = ""  # the spawner's agent-hub command, for briefs sent from the daemon
        self.cgroup = None
        self.master, slave = os.openpty()
        try:
            self.cgroup = create_session_cgroup()
            set_winsize(self.master, rows, cols)
            def child_setup():
                if self.cgroup:
                    fd = os.open(os.path.join(self.cgroup, "cgroup.procs"), os.O_WRONLY)
                    try:
                        os.write(fd, str(os.getpid()).encode())
                    finally:
                        os.close(fd)
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                                         start_new_session=True, preexec_fn=child_setup)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            os.close(self.master)
            if self.cgroup:
                os.rmdir(self.cgroup)
            raise
        finally:
            os.close(slave)
        self.started = process_start(self.proc.pid)
        os.set_blocking(self.master, False)
        asyncio.get_running_loop().add_reader(self.master, self._on_output)

    @property
    def alive(self):
        return self.master is not None

    def _close_pty(self):
        if self.master is not None:
            asyncio.get_running_loop().remove_reader(self.master)
            os.close(self.master)
            self.master = None

    def _on_output(self):
        try:
            data = os.read(self.master, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            self._close_pty()
            asyncio.create_task(self._reap())
            return
        self.last_output = time.time()
        try:
            self.stream.feed(self.filter.feed(data))
        except Exception as e:
            print(f"[{self.name}] pyte: {e!r}", file=sys.stderr)
        for writer in list(self.viewers):
            if writer.is_closing() or writer.transport.get_write_buffer_size() > 1024 * 1024:
                writer.close()
                self.viewers.discard(writer)
            else:
                writer.write(frame(b"O", data))

    async def _reap(self):
        self.exit_status = await asyncio.to_thread(self.proc.wait)
        if self.kill_task is None:
            await self.kill(natural=True)

    def _close_viewers(self, message):
        for writer in list(self.viewers):
            if not writer.is_closing():
                writer.write(frame(b"X", message.encode()))
                writer.close()
        self.viewers.clear()

    async def write(self, data):
        if not self.alive or self.kill_task is not None:
            raise RuntimeError(f"{self.name} is stopped or stopping")
        deadline = time.monotonic() + 10
        view = memoryview(data)
        while view:
            if time.monotonic() > deadline:
                raise RuntimeError(f"{self.name} is not reading terminal input")
            try:
                view = view[os.write(self.master, view):]
            except BlockingIOError:
                await asyncio.sleep(.01)

    @property
    def paste_mode(self):
        return (2004 << 5) in self.screen.mode

    async def paste(self, text):
        data = text.encode()
        if self.paste_mode:
            data = b"\x1b[200~" + data + b"\x1b[201~"
        await self.write(data)

    def resize(self, cols, rows):
        cols, rows = max(2, min(cols, 500)), max(2, min(rows, 200))
        # pyte clips top rows on shrink; retain them even if the application never redraws.
        for y in range(max(0, self.screen.lines - rows)):
            self.screen.history.top.append(self.screen.buffer[y].copy())
        self.screen.resize(rows, cols)
        if self.alive:
            set_winsize(self.master, rows, cols)

    def render(self, lines):
        cols = self.screen.columns
        rows = ["".join(line.get(x, self.screen.default_char).data for x in range(cols)).rstrip()
                for line in self.screen.history.top]
        rows += [line.rstrip() for line in self.screen.display]
        while rows and not rows[-1]:
            rows.pop()
        return "\n".join(rows[-max(1, lines):])

    def kill(self, natural=False):
        if self.kill_task is None:
            self.kill_task = asyncio.create_task(self._terminate(natural))
        return self.kill_task

    async def _terminate(self, natural=False):
        deadline = time.monotonic() + TERMINATE_GRACE
        pids = cgroup_members(self.cgroup) if self.cgroup else [self.proc.pid, *descendants(self.proc.pid)]
        tracked = {pid: process_start(pid) for pid in pids}
        if process_start(self.proc.pid) == self.started:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGHUP)
        for pid, started in tracked.items():
            if started and process_start(pid) == started:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGHUP)
        try:
            await asyncio.wait_for(asyncio.to_thread(self.proc.wait), timeout=TERMINATE_GRACE)
        except asyncio.TimeoutError:
            pass
        while time.monotonic() < deadline:
            survivors = cgroup_populated(self.cgroup) if self.cgroup else [pid for pid, started in tracked.items()
                         if pid != self.proc.pid and started and process_start(pid) == started]
            if not survivors:
                break
            await asyncio.sleep(.03)
        # Re-scan surviving children before their parent is killed/reparented.
        for pid, started in list(tracked.items()):
            if started and process_start(pid) == started:
                for child in descendants(pid):
                    tracked.setdefault(child, process_start(child))
        if self.cgroup:
            if cgroup_populated(self.cgroup):
                with open(os.path.join(self.cgroup, "cgroup.kill"), "w") as f:
                    f.write("1")
            while cgroup_populated(self.cgroup):
                if time.monotonic() > deadline + 3:
                    raise RuntimeError(f"{self.name}: managed processes have not exited")
                await asyncio.sleep(.02)
            for directory, _, _ in os.walk(self.cgroup, topdown=False):
                os.rmdir(directory)
            self.cgroup = None
        else:
            for pid, started in tracked.items():
                if started and process_start(pid) == started:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
        self.exit_status = await asyncio.to_thread(self.proc.wait)
        await asyncio.sleep(0)
        self._close_pty()
        if natural:
            add_report(self, f"exited with status {self.exit_status}", compact(self.render(20)))
        self._close_viewers(f"[{self.name} exited with status {self.exit_status}]" if natural else f"[{self.name} killed]")
        if self.viewer_process and self.viewer_process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                self.viewer_process.terminate()
        await finish_session(self)

    def save_screen(self):
        directory = archive_directory()
        os.makedirs(directory, mode=0o700, exist_ok=True)
        item = {"name": self.name, "owner": self.owner, "kind": self.kind, "cwd": self.cwd,
                "profile": self.profile, "settings_file": self.settings_file,
                "alive": False, "status": self.exit_status, "quiet_for": 0, "paste_mode": False,
                "text": self.render(236), "saved_at": time.time()}
        fd, temporary = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(item, f)
            os.replace(temporary, archive_path(self.name, self.owner))
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
        paths = [os.path.join(directory, p) for p in os.listdir(directory) if p.endswith(".json")]
        for path in sorted(paths, key=os.path.getmtime)[:-32]:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)


SESSIONS = {}
REPORTS = []  # helper messages not yet read by their spawner
IDLE_HANDLE = None
STOP_EVENT = None


def project_directory(cwd):
    if not cwd:
        raise ValueError("project directory required; pass --cwd /path/to/project")
    path = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(path):
        raise ValueError(f"project directory does not exist or is not a directory: {path}")
    return path


def reset_idle():
    global IDLE_HANDLE
    if IDLE_HANDLE is not None:
        IDLE_HANDLE.cancel()
        IDLE_HANDLE = None
    if not SESSIONS and STOP_EVENT is not None and not STOP_EVENT.is_set():
        IDLE_HANDLE = asyncio.get_running_loop().call_later(IDLE_SECONDS, stop_if_empty)


def stop_if_empty():
    if not SESSIONS and not REPORTS and STOP_EVENT is not None:
        STOP_EVENT.set()


def by_report_id(report_id):
    return next((s for s in SESSIONS.values() if s.report_id == report_id), None)


def add_report(session, title, text=""):
    REPORTS.append({"owner": session.owner, "name": session.name, "title": title,
                    "text": text, "time": time.time()})


async def finish_session(session):
    children = [child for child in list(SESSIONS.values()) if child.parent == session.name and child is not session]
    if children:
        await asyncio.gather(*(child.kill() for child in children))
    try:
        session.save_screen()
    except OSError as e:
        print(f"[{session.name}] cannot archive screen: {e}", file=sys.stderr)
    if SESSIONS.get(session.name) is session:
        del SESSIONS[session.name]
    reset_idle()


def get(req):
    session = SESSIONS.get(req["name"])
    if session is None or req.get("owner") not in (None, session.owner):
        mine = sorted(n for n, s in SESSIONS.items() if req.get("owner") in (None, s.owner))
        raise KeyError(f"no agent named {req['name']!r} of yours; yours: {mine or 'none'}")
    return session


async def open_viewer(session, env):
    with tempfile.TemporaryFile() as diagnostics:
        viewer_env = dict(env, AGENT_HUB_VIEW_OWNER=json.dumps(session.owner))
        terminal = session.terminal_argv or terminal_command(env=env)
        session.viewer_process = subprocess.Popen(
            [*terminal, sys.executable, os.path.abspath(__file__), "attach", session.name],
            env=viewer_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=diagnostics, start_new_session=True)
        deadline = time.monotonic() + VIEWER_TIMEOUT
        while not session.attached.is_set():
            if session.viewer_process.poll() is not None or time.monotonic() >= deadline:
                diagnostics.seek(0)
                detail = diagnostics.read(2000).decode(errors="replace").strip()
                raise RuntimeError(os.path.basename(terminal[0]) + " terminal viewer failed to attach" + (": " + detail if detail else ""))
            await asyncio.sleep(.02)


async def dispatch(req):
    op = req["op"]
    if op == "spawn":
        if STOP_EVENT is not None and STOP_EVENT.is_set():
            return {"retry": True}
        if req["kind"] not in INTERACTIVE:
            raise ValueError("use " + ", ".join(INTERACTIVE))
        cwd = project_directory(req.get("cwd", ""))
        name = req["name"]
        open_helper = next((s for s in SESSIONS.values() if req.get("previous") and s.report_id == req["previous"]), None)
        if open_helper is not None:
            raise ValueError(f"{open_helper.name!r} is still open; send it a message instead of resuming it")
        if name in SESSIONS:
            if req.get("owner") == SESSIONS[name].owner:
                raise ValueError(f"an agent named {name!r} already exists")
            name += "-" + uuid.uuid4().hex[:6]
        parent = SESSIONS.get(req.get("parent", ""))
        if parent is not None and (not parent.alive or parent.kill_task is not None):
            raise ValueError("parent agent is stopping")
        depth = max(req.get("depth", 0), parent.depth if parent else 0) + 1
        if depth > MAX_DEPTH:
            raise ValueError(f"depth limit reached ({depth} > {MAX_DEPTH})")
        if len(SESSIONS) >= MAX_AGENTS:
            raise ValueError(f"{MAX_AGENTS} agents are already running; kill one first")
        env = dict(req["env"], PWD=cwd, AGENT_HUB_NAME=name, AGENT_HUB_DEPTH=str(depth))
        workspace = req.get("workspace", env.get("AGENT_HUB_WORKSPACE", ""))
        if env.get("HYPRLAND_INSTANCE_SIGNATURE") and parent:
            workspace = (process_workspace(hyprland_clients(env), parent.viewer_process.pid, viewer=True)
                         if parent.viewer_process and parent.viewer_process.poll() is None else "") or parent.workspace
        if workspace:
            env["AGENT_HUB_WORKSPACE"] = workspace
        terminal = terminal_command(req.get("terminal", ""), env) if req.get("visible", False) else []
        if terminal:
            terminal = background_terminal(terminal, workspace, env)
        session = Session(name, req["kind"], req["argv"], cwd, env, req.get("owner"), depth)
        session.terminal_argv = terminal
        session.workspace = workspace
        session.profile = req.get("profile", req["kind"])
        session.keep = req.get("keep", False)
        session.briefed = req.get("briefed", False)
        session.report_id = req.get("report_id", "")
        session.hub = req.get("hub", "")
        session.parent = parent.name if parent else ""
        SESSIONS[name] = session
        reset_idle()
        try:
            if req.get("visible", False):
                await open_viewer(session, env)
        except (OSError, RuntimeError, ValueError):
            await session.kill()
            raise
        return {"name": name, "profile": session.profile, "workspace": session.workspace}
    if op == "list":
        return {"agents": [{"name": s.name, "kind": s.kind, "alive": s.alive, "status": s.exit_status,
                             "profile": s.profile, "cwd": s.cwd, "workspace": s.workspace, "viewers": len(s.viewers),
                             "busy": shows_busy(s.kind, s.render(60)), "quiet_for": time.time() - s.last_output}
                            for s in SESSIONS.values() if req.get("owner") in (None, s.owner)]}
    if op == "report":
        # Sent from inside a helper, whose owner differs from its spawner's.
        s = by_report_id(req["id"])
        if s is None:
            raise KeyError(f"no running helper with ID {req['id']!r}")
        if len(req["text"]) > MAX_REPORT:
            raise ValueError(f"report exceeds {MAX_REPORT} characters; write it to a file and report the path")
        s.reported = True
        if not req.get("done"):
            add_report(s, "says", req["text"])
        elif s.keep:
            add_report(s, "done; still open", req["text"])
        else:
            add_report(s, "done; closed", req["text"])
            asyncio.get_running_loop().call_later(DONE_GRACE, s.kill)
        return {}
    if op == "turn_end":
        s = by_report_id(req["id"])
        if s and not s.reported:
            add_report(s, "ended its turn without reporting", req["text"] or compact(s.render(20)))
        if s:
            s.reported = False
        return {}
    if op == "reports":
        mine = [r for r in REPORTS if req.get("owner") in (None, r["owner"])
                and req.get("name") in (None, r["name"])]
        REPORTS[:] = [r for r in REPORTS if r not in mine]
        reset_idle()
        return {"reports": mine}
    try:
        s = get(req)
    except KeyError:
        cached = archived(req["name"], req.get("owner")) if op in ("screen", "info", "kill") else None
        if not cached:
            raise
        return {"text": "\n".join(cached["text"].splitlines()[-req.get("lines", 80):])} if op == "screen" else cached
    if op == "info":
        return {"kind": s.kind, "alive": s.alive, "status": s.exit_status,
                "profile": s.profile, "settings_file": s.settings_file, "workspace": s.workspace,
                "quiet_for": time.time()-s.last_output, "paste_mode": s.paste_mode, "pid": s.proc.pid,
                "announced": s.announced >= s.last_output}
    if op == "screen":
        return {"text": s.render(req.get("lines", 80))}
    if op == "announce":
        s.announced = s.last_output
        return {}
    if op == "write":
        await s.write(req["data"].encode())
        return {}
    if op == "paste":
        text = req["text"]
        if req.get("brief") and not s.briefed:
            s.briefed = True
            text += helper_brief(s.report_id, s.keep, s.hub)
        await s.paste(text)
        return {}
    if op == "kill":
        await s.kill()
        return {}
    raise ValueError(f"unknown operation {op!r}")


async def serve_viewer(req, reader, writer):
    s = get(req)
    writer.write(frame(b"O", b"\x1b[2J\x1b[H" + s.render(req["rows"]).replace("\n", "\r\n").encode()))
    if not s.alive:
        writer.write(frame(b"X", f"[{s.name} exited with status {s.exit_status}]".encode()))
        return
    s.viewers.add(writer)
    s.attached.set()
    s.resize(req["cols"]-1, req["rows"])
    asyncio.get_running_loop().call_later(.1, s.resize, req["cols"], req["rows"])
    try:
        while True:
            kind, payload = await read_frame(reader)
            if kind == b"I":
                await s.write(payload)
            elif kind == b"R":
                s.resize(*json.loads(payload))
    finally:
        s.viewers.discard(writer)


async def handle(reader, writer):
    try:
        _, payload = await read_frame(reader)
        req = json.loads(payload)
        try:
            if req["op"] == "attach":
                await serve_viewer(req, reader, writer)
                return
            response = await dispatch(req)
        except KeyError as e:  # str() of a KeyError is its quoted repr
            response = {"error": e.args[0] if e.args else str(e)}
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as e:
            response = {"error": str(e)}
        writer.write(frame(b"J", json.dumps(response).encode()))
        await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError, ValueError):
        pass
    finally:
        writer.close()


def owner_alive(owner):
    if not owner or not owner.startswith("proc:"):
        return True
    _, pid, started, *_ = owner.split(":")
    return process_start(int(pid)) == started


def forgotten(session):
    """An automated helper left idle: no output, not working, and nothing of its own still running."""
    return (MAX_IDLE and session.owner and not session.keep and session.kill_task is None
            and time.time() - session.last_output >= MAX_IDLE
            and not shows_busy(session.kind, session.render(60))
            and not any(child.parent == session.name for child in SESSIONS.values()))


async def watch_owners():
    while True:
        await asyncio.sleep(5)
        for session in list(SESSIONS.values()):
            if session.proc.poll() is not None and session.kill_task is None:
                session.kill(natural=True)
            if not owner_alive(session.owner):
                session.kill()
            elif forgotten(session):
                add_report(session, f"closed after {MAX_IDLE // 60} min idle", compact(session.render(20)))
                session.kill()
        fresh = [r for r in REPORTS if owner_alive(r["owner"]) and time.time() - r["time"] < REPORT_TTL]
        if len(fresh) != len(REPORTS):
            REPORTS[:] = fresh
            reset_idle()


def run_daemon():
    global asyncio
    import asyncio
    os.makedirs(RUNTIME, mode=0o700, exist_ok=True)
    if os.path.exists(f"/run/user/{os.getuid()}/systemd/private") and session_cgroup_base() is None:
        raise RuntimeError("start the isolated daemon with agent-hub spawn, not a caller-owned daemon process")
    lock = open(SOCK + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    with contextlib.suppress(FileNotFoundError):
        os.unlink(SOCK)

    async def main():
        global STOP_EVENT
        STOP_EVENT = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, STOP_EVENT.set)
        server = await asyncio.start_unix_server(handle, SOCK)
        os.chmod(SOCK, 0o600)
        print(f"agent-hub daemon listening on {SOCK}", file=sys.stderr, flush=True)
        monitor = asyncio.create_task(watch_owners())
        reset_idle()
        try:
            await STOP_EVENT.wait()
        finally:
            server.close()
            await server.wait_closed()
            monitor.cancel()
            await asyncio.gather(*(s.kill() for s in list(SESSIONS.values())))
            with contextlib.suppress(asyncio.CancelledError):
                await monitor

    try:
        asyncio.run(main())
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(SOCK)


# ---------------------------------------------------------------- attach client


def run_attach(name: str) -> int:
    s = connect()
    cols, rows = os.get_terminal_size()
    s.sendall(frame(b"J", json.dumps({"op": "attach", "name": name, "cols": cols, "rows": rows, "owner": OWNER}).encode()))
    out = sys.stdout.fileno()
    os.write(out, f"\x1b]0;agent: {name}\x07".encode())
    old = termios.tcgetattr(0)
    old_winch = signal.getsignal(signal.SIGWINCH)
    tty.setraw(0)
    # SIGWINCH only wakes select through this pipe; sending from the handler itself could
    # land a resize frame in the middle of an input frame.
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_w, False)
    old_wakeup = signal.set_wakeup_fd(wake_w)
    signal.signal(signal.SIGWINCH, lambda *_: None)
    buf, pending, ended = b"", b"", None
    try:
        while ended is None:
            ready, _, _ = select.select([0, s, wake_r], [], [], 0.05 if pending else None)
            if not ready:  # a held-back escape never completed: it was a real key, send it
                s.sendall(frame(b"I", pending))
                pending = b""
            if wake_r in ready:
                os.read(wake_r, 512)
                s.sendall(frame(b"R", json.dumps(os.get_terminal_size()).encode()))
            if 0 in ready:
                incoming = os.read(0, 4096)
                if not incoming:
                    ended = f"[detached from {name}; it is still running]"
                    break
                data, pending = pending + incoming, b""
                if DETACH.search(data):
                    ended = f"[detached from {name}; it is still running]"
                    break
                data = TERMINAL_REPLIES.sub(b"", data)
                if m := INCOMPLETE_ESC.search(data):
                    data, pending = data[:m.start()], data[m.start():]
                if data:
                    s.sendall(frame(b"I", data))
            if s in ready:
                chunk = s.recv(65536)
                if not chunk:
                    ended = "[agent-hub daemon went away]"
                buf += chunk
                while len(buf) >= HDR.size:
                    kind, n = HDR.unpack(buf[:HDR.size])
                    if len(buf) < HDR.size + n:
                        break
                    payload, buf = buf[HDR.size:HDR.size + n], buf[HDR.size + n:]
                    if kind == b"O":
                        os.write(out, payload)
                    else:  # X ends the session, J here is an error such as an unknown name
                        ended = payload.decode() if kind == b"X" else json.loads(payload).get("error", "")
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, old)
        signal.set_wakeup_fd(old_wakeup)
        signal.signal(signal.SIGWINCH, old_winch)
        os.close(wake_r)
        os.close(wake_w)
        s.close()
        os.write(out, b"\x1b[?2004l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[?25h")
    print(f"\r\n{ended}")
    # Keep the window open only when something went wrong, so the error stays readable.
    if re.search(r"exited with status (?!0\])|daemon went away|^no agent", ended):
        input("press Enter to close")
    return 0


# ---------------------------------------------------------------- CLI operations


def is_idle(kind: str, screen_text: str, quiet_for: float, settle: float) -> bool:
    """Decide whether an agent has finished its turn and is waiting for input.

    screen_text is what the agent's terminal currently shows, quiet_for is how many
    seconds since it last printed anything, settle is the caller's requested quiet period.
    """
    if quiet_for < settle:
        return False
    return not shows_busy(kind, screen_text)


def shows_busy(kind: str, screen_text: str) -> bool:
    tail = "\n".join(screen_text.splitlines()[-BUSY_SCAN_LINES:]).lower()
    return any(marker in tail for marker in BUSY_MARKERS.get(kind, []))


def compact(text: str) -> str:
    """Screen text without borders, logos and blank lines: what an agent needs, in fewer tokens."""
    lines = []
    for line in text.splitlines():
        line = line.strip().strip("│┃║▏▕").strip()
        if not DECORATION.fullmatch(line):
            lines.append(re.sub(r" {3,}", "  ", line))
    return "\n".join(lines)


def screen(name: str, lines: int = 20) -> str:
    return compact(request("screen", name=name, lines=lines)["text"])


def take_reports(name: str | None = None) -> list[dict]:
    try:
        return request("reports", **({"name": name} if name else {}))["reports"]
    except RuntimeError:  # no daemon, or one from before reports existed
        return []


def format_reports(reports: list[dict]) -> str:
    return "\n".join(f"[{r['name']} {r['title']}]" + (f"\n{r['text']}" if r["text"] else "") for r in reports)


def announce(name: str) -> None:
    """Mark a quiet helper as shown, so `wait` with no name moves on to the others."""
    with contextlib.suppress(RuntimeError):  # gone, or a daemon from before announcements
        request("announce", name=name)


def wait_idle(name: str, timeout: float, settle: float) -> str:
    if timeout < 0 or settle < 0:
        raise ValueError("timeout and settle must be nonnegative")
    deadline = time.monotonic() + timeout
    while True:
        reports = take_reports(name)
        if reports:
            return format_reports(reports)
        info = request("info", name=name)
        text = request("screen", name=name, lines=60)["text"]
        output = "\n".join(compact(text).splitlines()[-20:])
        if not info["alive"]:
            return f"[{name} exited with status {info['status']}]\n{output}"
        if is_idle(info["kind"], text, info["quiet_for"], settle):
            announce(name)
            return f"[{name} idle]\n{output}"
        if time.monotonic() >= deadline:
            return f"[{name} still busy after {timeout:.0f}s; use wait or read]\n{output}"
        time.sleep(1)


def wait_any(timeout: float, settle: float) -> str:
    """Return on the first helper report, once every helper has gone quiet, or when one helper
    has sat quiet without reporting for BLOCKED_AFTER seconds (a dialog, or a forgotten report)."""
    if timeout < 0 or settle < 0:
        raise ValueError("timeout and settle must be nonnegative")
    deadline = time.monotonic() + timeout
    while True:
        reports = take_reports()
        if reports:
            return format_reports(reports)
        agents = [a["name"] for a in request("list")["agents"] if a["alive"]]
        if not agents:
            return format_reports(take_reports()) or "no agents"
        screens = {name: request("screen", name=name, lines=60)["text"] for name in agents}
        infos = {name: request("info", name=name) for name in agents}
        idle = [name for name in agents if is_idle(infos[name]["kind"], screens[name], infos[name]["quiet_for"], settle)]
        stuck = [name for name in idle if infos[name]["quiet_for"] >= max(settle, BLOCKED_AFTER)
                 and not infos[name].get("announced")]
        if len(idle) == len(agents) or stuck:
            shown = agents if len(idle) == len(agents) else stuck
            for name in shown:
                announce(name)
            busy = [name for name in agents if name not in shown]
            return "\n".join([*(f"[{name} idle]\n" + "\n".join(compact(screens[name]).splitlines()[-20:])
                                 for name in shown),
                               *([f"[still working: {', '.join(busy)}]"] if busy else [])])
        if time.monotonic() >= deadline:
            return f"[{', '.join(agents)} still busy after {timeout:.0f}s; use wait or read]"
        time.sleep(1)


def wait_ready(name: str, kind: str, timeout: float = 30) -> str:
    """Wait until the TUI accepts typing (paste mode on and settled, or working on its prompt).

    Agents that never enable bracketed paste, or sit on a dialog, count as ready once quiet.
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        info = request("info", name=name)
        if not info["alive"]:
            break
        if info["paste_mode"] and (info["quiet_for"] >= 1 or shows_busy(kind, screen(name, 12))):
            break
        if info["quiet_for"] >= 4 and time.monotonic() - start >= 4:
            break
        time.sleep(0.5)
    return screen(name)


def send_text(name: str, text: str, submit: bool, brief: bool = False) -> None:
    request("paste", name=name, text=text, brief=brief)
    if submit:
        time.sleep(0.5)
        request("write", name=name, data="\r")


def key_bytes(key: str) -> str:
    if key in KEYS:
        return KEYS[key]
    if re.fullmatch(r"C-[a-z]", key):
        return chr(ord(key[2]) - 96)
    return key


def run_command(name: str, text: str) -> None:
    """Clear whatever is in the agent's input box (Ctrl-U), then submit a slash command."""
    request("write", name=name, data=key_bytes("C-u"))
    send_text(name, text, True)


def check_value(label: str, value: str) -> None:
    # These end up inside typed slash commands; a "\r" or space would let a caller chain commands.
    if value and (value.startswith("-") or not SAFE_VALUE.fullmatch(value)):
        raise ValueError(f"{label} {value!r} has characters outside [A-Za-z0-9._:/#@[]-]")


MISSING = object()


def config_document(kind, raw):
    if not raw:
        return {}
    if kind == "grok":
        import tomllib
        return tomllib.loads(raw.decode())
    return json.loads(raw)


def setting_value(document, path):
    value = document
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return MISSING
        value = value[key]
    return value


def model_paths(kind, document):
    if kind == "grok":
        return {("models", "default"), ("models", "default_reasoning_effort")}
    if kind == "opencode":
        variants = document.get("variant", {})
        return {("recent",)} | {("variant", key) for key in (variants if isinstance(variants, dict) else {})}
    paths = {("model",), ("effortLevel",)}
    def visit(value, prefix):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("effort", "effortLevel"):
                    paths.add((*prefix, key))
                elif isinstance(child, dict):
                    visit(child, (*prefix, key))
    visit(document.get("modelSettings", {}), ("modelSettings",))
    return paths


def set_setting(document, path, value, baseline):
    parent = document
    for key in path[:-1]:
        if not isinstance(parent.get(key), dict):
            parent[key] = {}
        parent = parent[key]
    if value is MISSING:
        parent.pop(path[-1], None)
        # Remove only empty containers that the switch introduced.
        for n in range(len(path)-1, 0, -1):
            ancestor = path[:n]
            if setting_value(document, ancestor) == {} and setting_value(baseline, ancestor) is MISSING:
                container = document
                for key in ancestor[:-1]:
                    container = container[key]
                container.pop(ancestor[-1], None)
    else:
        parent[path[-1]] = value


def render_grok_settings(raw, document, changes):
    import tomlkit
    parsed = tomlkit.parse(raw.decode())
    if "models" not in parsed:
        parsed["models"] = tomlkit.table()
    for path, value in changes.items():
        key = path[-1]
        if value is MISSING:
            parsed["models"].pop(key, None)
        else:
            parsed["models"][key] = value
    return tomlkit.dumps(parsed).encode()


def atomic_settings_write(path, expected, content):
    """Keep symlinks intact and reject observed concurrent writes before replacement."""
    target = os.path.realpath(path)
    try:
        mode = os.stat(target).st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    fd, temporary = tempfile.mkstemp(prefix=".agent-hub-settings-", dir=os.path.dirname(target))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        try:
            with open(target, "rb") as f:
                current = f.read()
        except FileNotFoundError:
            current = None
        if current != expected or os.path.realpath(path) != target:
            return False
        os.replace(temporary, target)
        return True
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


@contextlib.contextmanager
def keep_user_default(kind, model=None, effort=None, path=None):
    """Restore only model fields, preserving unrelated edits and settings symlinks."""
    path = path or MODEL_DEFAULTS_FILE.get(kind)
    if not path:
        yield
        return
    def read():
        try:
            with open(path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None
    def attributable(key, value):
        if model is None and effort is None:
            return True
        if key[-1] in ("effort", "effortLevel", "default_reasoning_effort"):
            return bool(effort) and value == effort
        if key == ("recent",):
            if not model or not isinstance(value, list) or not value or not isinstance(value[0], dict):
                return False
            return (model == value[0].get("providerID", "") + "/" + value[0].get("modelID", "")
                    or "/" not in model and model == value[0].get("modelID"))
        if key[0] == "variant":
            return bool(model) and key[-1] == model
        return bool(model) and (value == model or isinstance(value, str) and model in ("opus", "sonnet", "haiku", "fable") and value.startswith("claude-" + model + "-"))
    os.makedirs(RUNTIME, exist_ok=True)
    with open(SETTINGS_LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = read()
        baseline = config_document(kind, saved)
        owned = {}
        warned = set()
        try:
            yield
        finally:
            calm, deadline = time.monotonic()+2, time.monotonic()+8
            while time.monotonic() < min(calm, deadline):
                raw = read()
                try:
                    current = config_document(kind, raw)
                except (ValueError, UnicodeError):
                    WARNINGS.append(f"{kind} settings changed to an unreadable format; left untouched: {path}")
                    break
                changes = {}
                for key in model_paths(kind, baseline) | model_paths(kind, current):
                    before, now = setting_value(baseline, key), setting_value(current, key)
                    if before == now:
                        continue
                    if key not in owned and attributable(key, now):
                        owned[key] = now
                    if key in owned and now == owned[key]:
                        changes[key] = before
                    elif key not in warned:
                        WARNINGS.append(f"{kind} model setting changed concurrently; left untouched: {'.'.join(key)}")
                        warned.add(key)
                if changes:
                    import copy
                    patched = copy.deepcopy(current)
                    for key, value in changes.items():
                        set_setting(patched, key, value, baseline)
                    if patched == baseline and saved is not None:
                        content = saved
                    elif kind == "grok":
                        content = render_grok_settings(raw or b"", patched, changes)
                    else:
                        content = (json.dumps(patched, indent=2)+"\n").encode()
                    try:
                        config_document(kind, content)
                    except (ValueError, UnicodeError):
                        WARNINGS.append(f"{kind} model defaults could not be rendered safely; original settings left untouched")
                        break
                    if atomic_settings_write(path, raw, content):
                        calm = time.monotonic()+2
                time.sleep(.1)


def merged_overlay(raw, updates, name):
    try:
        result = json.loads(raw) if raw else {}
    except ValueError as e:
        raise ValueError(f"{name} is not valid JSON; refusing to discard its configuration") from e
    if not isinstance(result, dict):
        raise ValueError(f"{name} must be a JSON object")
    for key, value in updates.items():
        if isinstance(value, dict):
            existing = result.get(key, {})
            if not isinstance(existing, dict):
                raise ValueError(f"{name}.{key} must be an object")
            result[key] = {**existing, **value}
        else:
            result[key] = value
    return json.dumps(result)


def switch_model(name: str, model: str, effort: str) -> str:
    check_value("model", model)
    check_value("effort", effort)
    info = request("info", name=name)
    kind = info["kind"]
    warning_start = len(WARNINGS)
    with keep_user_default(kind, model, effort, path=info.get("settings_file")):
        if kind in ("claude", "grok"):
            for cmd in ([f"/model {model}"] if model else []) + ([f"/effort {effort}"] if effort else []):
                run_command(name, cmd)
                time.sleep(1.5)
                # Claude asks before switching models mid-conversation because the prompt cache resets.
                if "Yes, switch to" in request("screen", name=name, lines=15)["text"]:
                    request("write", name=name, data="\r")
                    time.sleep(1)
        elif kind in CODEX_HOMES:
            if not model:
                raise ValueError("codex switches effort together with a model, e.g. model='gpt-6-astra', effort='high'")
            run_command(name, f"/model {model}" + (f" --reasoning={effort}" if effort else ""))
        elif kind == "opencode":
            if not model:
                raise ValueError("opencode requires a model name")
            if effort:
                raise ValueError("opencode has no effort switch; pick a model variant instead")
            run_command(name, "/models")
            time.sleep(1)
            send_text(name, model.split("/")[-1].replace("-", " "), False)  # the picker searches display names
            time.sleep(1)
            request("write", name=name, data="\r")
        else:
            raise ValueError(f"{kind} agents have no model to switch")
        time.sleep(1.5)
    return "\n".join([*("warning: " + w for w in WARNINGS[warning_start:]), screen(name, 12)])


def available_models(kind: str) -> str:
    name = kind
    profile = get_profile(name)
    kind = profile["kind"]
    if kind == "claude":
        return ("aliases: opus, sonnet, haiku, fable (or full IDs like claude-opus-5-5)\n"
                "efforts: low, medium, high, xhigh, max")
    if kind in CODEX_HOMES:
        home = profile["home"] or CODEX_HOMES.get(name, CODEX_HOMES["codex"])
        try:
            with open(os.path.join(home, "models_cache.json")) as f:
                models = json.load(f)["models"]
        except FileNotFoundError:
            raise ValueError(f"no Codex model list in {home}; start codex once with this account to create it") from None
        return "\n".join(
            f"{m['slug']}  efforts: {', '.join(r['effort'] for r in m.get('supported_reasoning_levels', []))}"
            for m in models if m.get("visibility") == "list"
        )
    if kind in ("opencode", "grok"):
        binary = shutil.which(profile["command"]) or (os.path.join(HOME, ".opencode/bin/opencode")
                  if profile["command"] == "opencode" else profile["command"])
        r = subprocess.run([binary, "models"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        return r.stdout.strip() or r.stderr.strip()
    raise ValueError(f"unknown kind {kind!r}; use one of {sorted(LAUNCH_MODEL) + ['grok', 'opencode']}")


def new_session_id(kind: str) -> str:
    if kind == "codex":
        return ""
    return "ses_" + uuid.uuid4().hex if kind == "opencode" else str(uuid.uuid4())


def record_path(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError(f"invalid helper name {name!r}")
    return os.path.join(HELPER_RECORDS, name + ".json")


def load_records() -> list[dict]:
    """Saved helpers, newest first. They outlive the daemon and reboots, unlike its screens."""
    if not os.path.isdir(HELPER_RECORDS):
        return []
    paths = sorted((os.path.join(HELPER_RECORDS, p) for p in os.listdir(HELPER_RECORDS) if p.endswith(".json")),
                   key=os.path.getmtime, reverse=True)
    records = []
    for path in paths:
        with contextlib.suppress(OSError, ValueError), open(path) as f:
            records.append(json.load(f))
    return records


def load_record(name: str) -> dict:
    try:
        with open(record_path(name)) as f:
            return json.load(f)
    except FileNotFoundError:
        raise ValueError(f"no saved helper named {name!r}; see agent-hub ls --closed") from None


def keep_closed_record(name: str) -> None:
    """A new helper reusing a closed helper's name keeps the old one resumable as NAME-REPORTID."""
    with contextlib.suppress(ValueError):
        old = load_record(name)
        if old.get("report_id"):
            old["name"] = f"{name}-{old['report_id']}"
            save_record(old)
            started = old.get("started", time.time())
            os.utime(record_path(old["name"]), (started, started))  # keeps its place in ls --closed


def save_record(record: dict) -> None:
    os.makedirs(HELPER_RECORDS, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=HELPER_RECORDS)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
        os.replace(temporary, record_path(record["name"]))
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    for old in load_records()[MAX_RECORDS:]:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(record_path(old["name"]))


def codex_session_id(record: dict) -> str:
    """Find the rollout whose user message holds this helper's report ID; its name ends in the session ID."""
    root = os.path.join(record.get("home") or CODEX_HOMES["codex"], "sessions")
    for path in glob.glob(os.path.join(root, "*/*/*/rollout-*.jsonl")):
        if os.path.getmtime(path) < record["started"] - 60:
            continue
        with open(path, errors="replace") as f:
            for line in f:
                if record["report_id"] not in line:
                    continue
                with contextlib.suppress(ValueError):  # a line Codex is still writing
                    if json.loads(line).get("payload", {}).get("role") == "user":
                        return path[-len("00000000-0000-0000-0000-000000000000.jsonl"):-len(".jsonl")]
    raise ValueError(f"cannot find {record['name']!r} in Codex's saved sessions; use `codex resume` to pick it by hand")


def turn_hook_args(kind: str, report_id: str) -> list[str]:
    """Per-launch hook settings; the user's own config files stay untouched."""
    command = [*HUB_ARGV, "turn-end", report_id]
    if kind == "claude":
        hook = {"type": "command", "command": shlex.join(command)}
        return ["--settings", json.dumps({"hooks": {"Stop": [{"hooks": [hook]}]}})]
    return ["-c", "notify=" + json.dumps(command)] if kind == "codex" else []


def daemon_outdated() -> bool:
    """A daemon started before reports existed keeps running until its helpers exit."""
    try:
        request("reports", name="")
    except RuntimeError as error:
        return "no running agents" not in str(error)
    return False


def spawn_agent(
    kind: str,
    name: str = "",
    cwd: str = "",
    prompt: str = "",
    model: str = "",
    effort: str = "",
    visible: bool = True,
    terminal: str = "",
    keep: bool = False,
    resume: dict | None = None,
) -> str:
    """Start an interactive agent in its own terminal (a window opens if visible).

    kind: a built-in or configured profile. prompt is its first message. model / effort:
    see list_models (opencode has no effort). Waits until the agent is ready and returns its screen;
    answer any dialog on it with send_keys. keep stops the hub closing it on done or when idle.
    resume is a saved helper's record; see resume_agent.
    """
    if DEPTH >= MAX_DEPTH:
        raise RuntimeError(f"agent-hub depth limit reached ({DEPTH}/{MAX_DEPTH}); do the work yourself")
    profile_name = kind
    profile = get_profile(profile_name)
    kind = profile["kind"]
    check_value("model", model)
    check_value("effort", effort)
    if kind == "opencode" and effort:
        raise ValueError("opencode has no effort switch; pick a model variant instead")
    name = re.sub(r"[^A-Za-z0-9_-]", "-", name) or f"{kind}-{uuid.uuid4().hex[:4]}"
    cwd = project_directory(cwd)
    argv, prompt_args = INTERACTIVE[kind]
    command = profile["command"]
    binary = shutil.which(command)
    if not binary:
        candidate = os.path.join(HOME, ".local/bin", command)
        binary = candidate if os.access(candidate, os.X_OK) else None
    if not binary and command == "opencode":
        candidate = os.path.join(HOME, ".opencode/bin/opencode")
        binary = candidate if os.path.isfile(candidate) else None
    if not binary:
        raise ValueError(f"profile {profile_name!r}: {command!r} is not installed or not on PATH")
    session_id = resume["session_id"] if resume else new_session_id(kind)
    argv = [binary, *SESSION_ARGS[kind][bool(resume)](session_id), *argv[1:]]
    if kind == "codex":  # its update dialog would swallow the first message, and Enter accepts the update
        argv += ["-c", "check_for_update_on_startup=false"]
    terminal = terminal or os.environ.get("AGENT_HUB_TERMINAL") or "auto"
    if visible:
        terminal_command(terminal)
    workspace = caller_workspace(os.environ) if visible else os.environ.get("AGENT_HUB_WORKSPACE", "")
    context_keys = {"AGENT_HUB_CALLER_ID", "AGENT_HUB_VIEW_OWNER", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "PI_SESSION_ID", "OPENCODE_SESSION_ID"}
    env = {k: v for k, v in os.environ.items() if not PARENT_ENV.match(k) and k not in context_keys}
    if profile["home"]:
        env["CLAUDE_CONFIG_DIR" if kind == "claude" else "CODEX_HOME"] = profile["home"]
    env["PATH"] = os.pathsep.join(dict.fromkeys([
        *os.get_exec_path(env), os.path.join(HOME, ".local/bin"),
        os.path.join(HOME, ".local/share/mise/command-wrappers/bin"),  # mise-installed agent CLIs
        os.path.join(HOME, ".opencode/bin"), "/usr/bin",
    ]))
    env.update(TERM="xterm-256color", COLORTERM="truecolor", PWD=cwd,
               AGENT_HUB_DEPTH=str(DEPTH + 1), AGENT_HUB_NAME=name)
    if kind in LAUNCH_MODEL:
        argv = argv + LAUNCH_MODEL[kind](model, effort)
    elif kind == "grok" and (model or effort):
        models = {"default": model} if model else {}
        if effort:
            models["default_reasoning_effort"] = effort
        env["GROK_CONFIG"] = merged_overlay(env.get("GROK_CONFIG"), {"models": models}, "GROK_CONFIG")
    elif kind == "opencode" and model:
        env["OPENCODE_CONFIG_CONTENT"] = merged_overlay(env.get("OPENCODE_CONFIG_CONTENT"), {"model": model}, "OPENCODE_CONFIG_CONTENT")
    outdated = daemon_outdated()
    report_id = uuid.uuid4().hex[:8]
    if not outdated:
        argv = argv + turn_hook_args(kind, report_id)
    argv = argv + (prompt_args(prompt + ("" if outdated else helper_brief(report_id, keep))) if prompt else [])
    result = request("spawn", name=name, kind=kind, profile=profile_name, argv=argv, cwd=cwd, env=env, depth=DEPTH,
                     parent=PARENT, visible=visible, terminal=terminal, workspace=workspace, keep=keep,
                     briefed=bool(prompt), report_id=report_id, hub=HUB_COMMAND,
                     previous=resume["report_id"] if resume else None)
    name = result["name"]
    if result.get("profile") != profile_name:
        request("kill", name=name)
        raise RuntimeError("running daemon does not support account profiles and terminal selection; "
                           "close existing helpers, wait 10 seconds, and retry")
    if not resume:
        keep_closed_record(name)
    save_record({"name": name, "profile": profile_name, "kind": kind, "home": profile["home"],
                 "session_id": session_id, "report_id": report_id, "cwd": cwd,
                 "model": model, "effort": effort, "started": time.time(),
                 "task": prompt.split("\n")[0][:80] or (resume["task"] if resume else "")})
    if visible and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and "workspace" not in result:
        request("kill", name=name)
        raise RuntimeError("running daemon does not support workspace placement; "
                           "close existing helpers, wait 10 seconds, and retry")
    if outdated:
        print("warning: the running daemon predates helper reports; this helper will not report or "
              "close itself, so kill it when finished", file=sys.stderr)
    return f"spawned {profile_name} as {name!r} in {cwd}\n{wait_ready(name, kind)}"


def resume_agent(name: str, prompt: str = "", model: str = "", effort: str = "",
                 visible: bool = True, keep: bool = False) -> str:
    """Reopen a closed helper with its conversation intact, in its original directory."""
    record = load_record(name)
    record["session_id"] = record["session_id"] or codex_session_id(record)
    return spawn_agent(record.get("profile", record["kind"]), name, record["cwd"], prompt, model or record["model"],
                       effort or record["effort"], visible=visible, keep=keep, resume=record)


def send_agent(name: str, text: str, submit: bool = True) -> str:
    """Type text into an agent's prompt (multi-line safe) and press Enter unless submit is false."""
    send_text(name, text, submit, brief=True)
    return "sent"


def send_keys(name: str, keys: list[str]) -> str:
    """Send keys: names like Enter, Escape, Tab, Up, Down, C-c, or literal text like "y"."""
    request("write", name=name, data="".join(key_bytes(k) for k in keys))
    return "ok"


def set_model(name: str, model: str = "", effort: str = "") -> str:
    """Switch a running agent's model and/or effort (only that agent; the user's default stays)."""
    return switch_model(name, model, effort)


def list_models(kind: str) -> str:
    """List models and efforts for a built-in or named account profile."""
    return available_models(kind)


def read_agent(name: str, lines: int = 40, raw: bool = False) -> str:
    """Return the end of an agent's screen. raw keeps borders and blank lines."""
    text = request("screen", name=name, lines=lines)["text"]
    return text if raw else compact(text)


def wait_agent(name: str | None, timeout: float = 600, settle: float = 4) -> str:
    """Block until the agent reports or is idle (or exits / times out). No name: any of your agents."""
    return wait_idle(name, timeout, settle) if name else wait_any(timeout, settle)


def ask_agent(name: str, text: str, timeout: float = 600, settle: float = 4) -> str:
    """Send a message and wait for the agent's reply: send_agent + wait_agent."""
    send_text(name, text, True, brief=True)
    time.sleep(2)
    return wait_idle(name, timeout, settle)


def list_agents() -> str:
    """List the agents you spawned: name, kind, state, open windows, cwd."""
    def state(a):
        if not a["alive"]:
            return f"exited({a['status']})"
        if "busy" not in a:  # a daemon from before states
            return "alive"
        return "busy" if a["busy"] else f"quiet {int(a['quiet_for']) // 60}m{int(a['quiet_for']) % 60:02d}s"
    agents = request("list")["agents"]
    return "\n".join(
        f"{a['name']}\t{a.get('profile', a['kind'])}\t{state(a)}\t{a['viewers']} window(s)\t{a['cwd']}"
        for a in agents
    ) or "no agents"


def list_closed() -> str:
    """Saved helpers that can be resumed, newest first."""
    running = {a["name"] for a in request("list")["agents"]} if os.path.exists(SOCK) else set()
    return "\n".join(
        f"{r['name']}\t{r['kind']}\t{time.strftime('%b %d %H:%M', time.localtime(r['started']))}\t{r['cwd']}\t{r['task']}"
        for r in load_records() if r["name"] not in running
    ) or "no closed helpers"


def report(report_id: str, text: str, done: bool = False) -> str:
    """Send a message to the agent that spawned this helper. done also ends the helper's task."""
    request("report", id=report_id, text=text, done=done)
    return "delivered"


def turn_end(report_id: str, payload: str = "") -> str:
    """Hook target for a helper's finished turn. Failures stay silent: the helper must not notice."""
    try:
        event = json.loads(payload or sys.stdin.read())
        thread = event.get("thread-id")
        # Codex also notifies for its hidden title thread, which has no saved session.
        home = os.path.expanduser(os.environ.get("CODEX_HOME") or CODEX_HOMES["codex"])  # the helper's account
        if thread and not glob.glob(os.path.join(home, "sessions/*/*/*", f"rollout-*{thread}.jsonl")):
            return ""
        text = event.get("last_assistant_message") or event.get("last-assistant-message") or ""
        request("turn_end", id=report_id, text=text[:MAX_REPORT])
    except (RuntimeError, ValueError, AttributeError, OSError):
        pass
    return ""


def kill_agent(name: str) -> str:
    """Close an agent and everything it started (its windows close with it)."""
    request("kill", name=name)
    return f"killed {name}"


def main():
    # Viewers stay alive; keep CLI-parser and asyncio imports out of them.
    global OWNER
    if len(sys.argv) == 3 and sys.argv[1] == "attach" and sys.argv[2] not in ("-h", "--help"):
        OWNER = json.loads(os.environ["AGENT_HUB_VIEW_OWNER"]) if "AGENT_HUB_VIEW_OWNER" in os.environ else caller_owner()
        run_attach(sys.argv[2])
        return
    if sys.argv[1:] == ["daemon"]:
        run_daemon()
        return
    import argparse

    parser = argparse.ArgumentParser(description="Drive real agent terminals. Visible windows; no MCP or tmux.",
        epilog="Example: agent-hub spawn claude helper 'Reply READY' --cwd ~/Projects")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("spawn", help="start a visible interactive agent")
    p.add_argument("kind", metavar="PROFILE", help="profile name; see agent-hub profiles")
    p.add_argument("name")
    p.add_argument("prompt", nargs="?", default="")
    p.add_argument("--cwd", required=True, help="existing project directory; never inferred from the caller")
    p.add_argument("--model", default="")
    p.add_argument("--effort", default="")
    p.add_argument("--no-window", action="store_true", help="keep the PTY; attach later")
    p.add_argument("--terminal", choices=["auto", *TERMINALS], default="", help="override AGENT_HUB_TERMINAL or automatic selection")
    p.add_argument("--keep", action="store_true", help="stay open after done or when idle, for follow-ups; you must kill it")
    for command, help_text in (("read", "read a short screen"), ("wait", "wait for a report or quiet; no name means any helper"),
                               ("ask", "send a prompt and wait"), ("send", "send a prompt"),
                               ("keys", "send keys: Enter, Escape, C-c, ..."),
                               ("model", "change model/effort"), ("attach", "attach here; Ctrl-] detaches"),
                               ("kill", "stop a session and its children")):
        p = sub.add_parser(command, help=help_text)
        p.add_argument("name", nargs="?" if command == "wait" else None)
        if command in ("ask", "send"):
            p.add_argument("text")
        if command == "keys":
            p.add_argument("keys", nargs="+")
        if command == "read":
            p.add_argument("--lines", type=int, default=20)
            p.add_argument("--raw", action="store_true")
        if command in ("ask", "wait"):
            p.add_argument("--timeout", type=float, default=60)
            p.add_argument("--settle", type=float, default=3)
        if command == "model":
            p.add_argument("model", nargs="?", default="")
            p.add_argument("--effort", default="")
    for command, help_text in (("tell", "helpers: send a message to your spawner"),
                               ("done", "helpers: deliver your result and close")):
        p = sub.add_parser(command, help=help_text)
        p.add_argument("id")
        p.add_argument("text", nargs="?" if command == "done" else None, default="")
    p = sub.add_parser("turn-end")  # called by helper hooks, not by hand
    p.add_argument("id")
    p.add_argument("payload", nargs="?", default="")
    p = sub.add_parser("resume", help="reopen a closed helper with its conversation")
    p.add_argument("name")
    p.add_argument("prompt", nargs="?", default="")
    p.add_argument("--model", default="")
    p.add_argument("--effort", default="")
    p.add_argument("--no-window", action="store_true", help="keep the PTY; attach later")
    p.add_argument("--keep", action="store_true", help="stay open after done or when idle, for follow-ups; you must kill it")
    p = sub.add_parser("models", help="list available models")
    p.add_argument("kind", metavar="PROFILE", help="profile name; see agent-hub profiles")
    sub.add_parser("profiles", help="list built-in and configured account profiles")
    p = sub.add_parser("ls", help="list sessions (does not start a daemon)")
    p.add_argument("--closed", action="store_true", help="list closed helpers you can resume")
    a = parser.parse_args()
    OWNER = caller_owner() if a.command not in ("daemon", "tell", "done", "turn-end") else None
    if a.command == "daemon":
        run_daemon()
        return
    if a.command == "spawn":
        result = spawn_agent(a.kind, a.name, a.cwd, a.prompt, a.model, a.effort,
                             visible=not a.no_window, terminal=a.terminal, keep=a.keep)
    elif a.command == "resume":
        result = resume_agent(a.name, a.prompt, a.model, a.effort, visible=not a.no_window, keep=a.keep)
    elif a.command == "turn-end":
        result = turn_end(a.id, a.payload)
    elif a.command in ("tell", "done"):
        result = report(a.id, a.text, done=a.command == "done")
    elif a.command == "models":
        result = available_models(a.kind)
    elif a.command == "profiles":
        result = list_profiles()
    elif a.command == "ls":
        result = list_closed() if a.closed else list_agents() if os.path.exists(SOCK) else "no agents"
    else:
        if a.command == "attach":
            run_attach(a.name)
            return
        if a.command == "read":
            result = read_agent(a.name, a.lines, a.raw)
        elif a.command == "wait":
            result = wait_agent(a.name, a.timeout, a.settle)
        elif a.command == "ask":
            result = ask_agent(a.name, a.text, a.timeout, a.settle)
        elif a.command == "send":
            result = send_agent(a.name, a.text)
        elif a.command == "keys":
            result = send_keys(a.name, a.keys)
        elif a.command == "model":
            result = set_model(a.name, a.model, a.effort)
        elif a.command == "kill":
            result = kill_agent(a.name)
    print(result)


def cli():
    """Console entry point with the same errors as running this file directly."""
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as e:
        sys.exit(f"agent-hub: {e}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    cli()
