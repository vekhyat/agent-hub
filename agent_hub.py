#!/usr/bin/env python3
"""agent-hub: let any agent spawn, drive and close other agents in real terminals.

One file, three roles:
  agent-hub --help         on-demand CLI for agents and humans
  agent_hub.py daemon      long-lived process that owns every agent's PTY (auto-started)
  agent_hub.py attach N    terminal client: watch / type into agent N (Ctrl-] detaches)
  agent_hub.py ls | kill N human helpers

The daemon keeps a pyte screen per agent so other agents can read what it shows,
and streams raw output to attached Foot windows.

CLI only: no MCP registration, SDK, loaded tool schemas or per-agent server.
The daemon exits when no sessions remain for 10 seconds.

Agents see only helpers owned by their conversation or standalone CLI process.
Humans, through `attach`, `ls` and `kill`, can reach every agent. Any process running as
this user can still talk to the socket directly; this is not a security boundary.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import shutil
import json
import os
import re
import select
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
WARNINGS: list[str] = []
DEPTH = int(os.environ.get("AGENT_HUB_DEPTH", "0"))
PARENT = os.environ.get("AGENT_HUB_NAME", "")  # set when this process runs inside a hub agent
MAX_DEPTH = int(os.environ.get("AGENT_HUB_MAX_DEPTH", "2"))
MAX_AGENTS = int(os.environ.get("AGENT_HUB_MAX_AGENTS", "8"))
TERMINAL = ["foot"]
def process_start(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


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
            if executable in {"foot", "footclient", "ghostty", "kitty", "alacritty", "konsole", "wezterm-gui"}:
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
    "codex1": (["codex1"], lambda p: ["--", p]),
    "codex2": (["codex2"], lambda p: ["--", p]),
    "opencode": (["opencode", "--standalone"], lambda p: [f"--prompt={p}"]),
    "grok": (["grok"], lambda p: ["--", p]),
}
AGENT_PROCESS_NAMES = set(INTERACTIVE) | {"codex", "pi", "agy", "gemini", "cursor-agent", "amp", "t3"}
CODEX_HOMES = {
    "codex1": os.path.expanduser(os.environ.get("AGENT_HUB_CODEX1_HOME", "~/.codex")),
    "codex2": os.path.expanduser(os.environ.get("AGENT_HUB_CODEX2_HOME", "~/.codex2")),
}
SAFE_VALUE = re.compile(r"[A-Za-z0-9._:/#@\[\]-]{1,100}")  # model / effort / flag values
# Lowercase text each TUI shows near the bottom only while it is busy generating / running tools.
BUSY_MARKERS = {
    "claude": ["esc to interrupt"],
    "codex1": ["esc to interrupt"],
    "codex2": ["esc to interrupt"],
    "grok": ["ctrl+c:cancel"],
    "opencode": ["esc interrupt", "esc to interrupt"],
}
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
        self._close_viewers(f"[{self.name} exited with status {self.exit_status}]" if natural else f"[{self.name} killed]")
        if self.viewer_process and self.viewer_process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                self.viewer_process.terminate()
        await finish_session(self)

    def save_screen(self):
        directory = archive_directory()
        os.makedirs(directory, mode=0o700, exist_ok=True)
        item = {"name": self.name, "owner": self.owner, "kind": self.kind, "cwd": self.cwd,
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
    if not SESSIONS and STOP_EVENT is not None:
        STOP_EVENT.set()


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
        session.viewer_process = subprocess.Popen(
            [*TERMINAL, sys.executable, os.path.abspath(__file__), "attach", session.name],
            env=viewer_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=diagnostics, start_new_session=True)
        deadline = time.monotonic() + VIEWER_TIMEOUT
        while not session.attached.is_set():
            if session.viewer_process.poll() is not None or time.monotonic() >= deadline:
                diagnostics.seek(0)
                detail = diagnostics.read(2000).decode(errors="replace").strip()
                raise RuntimeError("Foot terminal viewer failed to attach" + (": " + detail if detail else ""))
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
        session = Session(name, req["kind"], req["argv"], cwd, env, req.get("owner"), depth)
        session.parent = parent.name if parent else ""
        SESSIONS[name] = session
        reset_idle()
        try:
            if req.get("visible", False):
                await open_viewer(session, env)
        except (OSError, RuntimeError):
            await session.kill()
            raise
        return {"name": name}
    if op == "list":
        return {"agents": [{"name": s.name, "kind": s.kind, "alive": s.alive, "status": s.exit_status,
                             "cwd": s.cwd, "viewers": len(s.viewers)}
                            for s in SESSIONS.values() if req.get("owner") in (None, s.owner)]}
    try:
        s = get(req)
    except KeyError:
        cached = archived(req["name"], req.get("owner")) if op in ("screen", "info", "kill") else None
        if not cached:
            raise
        return {"text": "\n".join(cached["text"].splitlines()[-req.get("lines", 80):])} if op == "screen" else cached
    if op == "info":
        return {"kind": s.kind, "alive": s.alive, "status": s.exit_status,
                "quiet_for": time.time()-s.last_output, "paste_mode": s.paste_mode, "pid": s.proc.pid}
    if op == "screen":
        return {"text": s.render(req.get("lines", 80))}
    if op == "write":
        await s.write(req["data"].encode())
        return {}
    if op == "paste":
        await s.paste(req["text"])
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
        except (KeyError, ValueError, RuntimeError, OSError, subprocess.SubprocessError) as e:
            response = {"error": str(e).strip("'\"")}
        writer.write(frame(b"J", json.dumps(response).encode()))
        await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError, ValueError):
        pass
    finally:
        writer.close()


async def watch_owners():
    while True:
        await asyncio.sleep(5)
        for session in list(SESSIONS.values()):
            if session.proc.poll() is not None and session.kill_task is None:
                session.kill(natural=True)
            if session.owner and session.owner.startswith("proc:"):
                _, pid, started, *_ = session.owner.split(":")
                if process_start(int(pid)) != started:
                    session.kill()


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


def wait_idle(name: str, timeout: float, settle: float) -> str:
    if timeout < 0 or settle < 0:
        raise ValueError("timeout and settle must be nonnegative")
    deadline = time.monotonic() + timeout
    while True:
        info = request("info", name=name)
        text = request("screen", name=name, lines=60)["text"]
        output = "\n".join(compact(text).splitlines()[-20:])
        if not info["alive"]:
            return f"[{name} exited with status {info['status']}]\n{output}"
        if is_idle(info["kind"], text, info["quiet_for"], settle):
            return f"[{name} idle]\n{output}"
        if time.monotonic() >= deadline:
            return f"[{name} still busy after {timeout:.0f}s; use wait or read]\n{output}"
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


def send_text(name: str, text: str, submit: bool) -> None:
    request("paste", name=name, text=text)
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
def keep_user_default(kind, model=None, effort=None):
    """Restore only model fields, preserving unrelated edits and settings symlinks."""
    path = MODEL_DEFAULTS_FILE.get(kind)
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
    kind = request("info", name=name)["kind"]
    warning_start = len(WARNINGS)
    with keep_user_default(kind, model, effort):
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
    if kind == "claude":
        return ("aliases: opus, sonnet, haiku, fable (or full IDs like claude-opus-5-5)\n"
                "efforts: low, medium, high, xhigh, max")
    if kind in CODEX_HOMES:
        with open(os.path.join(CODEX_HOMES[kind], "models_cache.json")) as f:
            models = json.load(f)["models"]
        return "\n".join(
            f"{m['slug']}  efforts: {', '.join(r['effort'] for r in m.get('supported_reasoning_levels', []))}"
            for m in models if m.get("visibility") == "list"
        )
    if kind in ("opencode", "grok"):
        binary = shutil.which(kind) or (os.path.join(HOME, ".opencode/bin/opencode") if kind == "opencode" else kind)
        r = subprocess.run([binary, "models"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        return r.stdout.strip() or r.stderr.strip()
    raise ValueError(f"unknown kind {kind!r}; use one of {sorted(LAUNCH_MODEL) + ['grok', 'opencode']}")


def spawn_agent(
    kind: str,
    name: str = "",
    cwd: str = "",
    prompt: str = "",
    model: str = "",
    effort: str = "",
    visible: bool = True,
) -> str:
    """Start an interactive agent in its own terminal (a Foot window opens if visible).

    kind: claude | codex1 | codex2 | opencode | grok. prompt is its first message. model / effort:
    see list_models (opencode has no effort). Waits until the agent is ready and returns its screen;
    answer any dialog on it with send_keys.
    """
    if DEPTH >= MAX_DEPTH:
        raise RuntimeError(f"agent-hub depth limit reached ({DEPTH}/{MAX_DEPTH}); do the work yourself")
    if kind not in INTERACTIVE:
        raise ValueError(f"unknown kind {kind!r}; use one of {', '.join(INTERACTIVE)}")
    check_value("model", model)
    check_value("effort", effort)
    if kind == "opencode" and effort:
        raise ValueError("opencode has no effort switch; pick a model variant instead")
    name = re.sub(r"[^A-Za-z0-9_-]", "-", name) or f"{kind}-{uuid.uuid4().hex[:4]}"
    cwd = project_directory(cwd)
    argv, prompt_args = INTERACTIVE[kind]
    binary = shutil.which(argv[0])
    if not binary:
        candidate = os.path.join(HOME, ".local/bin", argv[0])
        binary = candidate if os.access(candidate, os.X_OK) else None
    if not binary and kind == "opencode":
        candidate = os.path.join(HOME, ".opencode/bin/opencode")
        binary = candidate if os.path.isfile(candidate) else None
    if not binary:
        raise ValueError(f"{kind} is not installed or not on PATH")
    argv = [binary, *argv[1:]]
    if visible and not shutil.which("foot"):
        raise ValueError("Foot is not installed")
    context_keys = {"AGENT_HUB_CALLER_ID", "AGENT_HUB_VIEW_OWNER", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "PI_SESSION_ID", "OPENCODE_SESSION_ID"}
    env = {k: v for k, v in os.environ.items() if not PARENT_ENV.match(k) and k not in context_keys}
    env["PATH"] = os.pathsep.join(dict.fromkeys([
        *os.get_exec_path(env), os.path.join(HOME, ".local/bin"),
        os.path.join(HOME, ".local/share/mise/command-wrappers/bin"),
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
    argv = argv + (prompt_args(prompt) if prompt else [])
    name = request("spawn", name=name, kind=kind, argv=argv, cwd=cwd, env=env, depth=DEPTH, parent=PARENT, visible=visible)["name"]
    return f"spawned {kind} as {name!r} in {cwd}\n{wait_ready(name, kind)}"


def send_agent(name: str, text: str, submit: bool = True) -> str:
    """Type text into an agent's prompt (multi-line safe) and press Enter unless submit is false."""
    send_text(name, text, submit)
    return "sent"


def send_keys(name: str, keys: list[str]) -> str:
    """Send keys: names like Enter, Escape, Tab, Up, Down, C-c, or literal text like "y"."""
    request("write", name=name, data="".join(key_bytes(k) for k in keys))
    return "ok"


def set_model(name: str, model: str = "", effort: str = "") -> str:
    """Switch a running agent's model and/or effort (only that agent; the user's default stays)."""
    return switch_model(name, model, effort)


def list_models(kind: str) -> str:
    """List models and efforts for claude, codex1, codex2, opencode or grok."""
    return available_models(kind)


def read_agent(name: str, lines: int = 40, raw: bool = False) -> str:
    """Return the end of an agent's screen. raw keeps borders and blank lines."""
    text = request("screen", name=name, lines=lines)["text"]
    return text if raw else compact(text)


def wait_agent(name: str, timeout: float = 600, settle: float = 4) -> str:
    """Block until the agent is idle (or exits / times out), then return its screen."""
    return wait_idle(name, timeout, settle)


def ask_agent(name: str, text: str, timeout: float = 600, settle: float = 4) -> str:
    """Send a message and wait for the agent's reply: send_agent + wait_agent."""
    send_text(name, text, True)
    time.sleep(2)
    return wait_idle(name, timeout, settle)


def list_agents() -> str:
    """List the agents you spawned: name, kind, alive/exited, open windows, cwd."""
    agents = request("list")["agents"]
    return "\n".join(
        f"{a['name']}\t{a['kind']}\t{'alive' if a['alive'] else f'exited({a['status']})'}\t"
        f"{a['viewers']} window(s)\t{a['cwd']}"
        for a in agents
    ) or "no agents"


def kill_agent(name: str) -> str:
    """Close an agent and everything it started (its windows close with it)."""
    request("kill", name=name)
    return f"killed {name}"


def main():
    # Viewers stay alive; keep CLI-parser and asyncio imports out of them.
    global OWNER
    if len(sys.argv) == 3 and sys.argv[1] == "attach":
        OWNER = json.loads(os.environ["AGENT_HUB_VIEW_OWNER"]) if "AGENT_HUB_VIEW_OWNER" in os.environ else caller_owner()
        run_attach(sys.argv[2])
        return
    if sys.argv[1:] == ["daemon"]:
        run_daemon()
        return
    import argparse

    parser = argparse.ArgumentParser(description="Drive real agent terminals. Foot windows; no MCP or tmux.",
        epilog="Example: agent-hub spawn claude helper 'Reply READY' --cwd ~/Projects")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("spawn", help="start a visible interactive agent")
    p.add_argument("kind", choices=INTERACTIVE)
    p.add_argument("name")
    p.add_argument("prompt", nargs="?", default="")
    p.add_argument("--cwd", required=True, help="existing project directory; never inferred from the caller")
    p.add_argument("--model", default="")
    p.add_argument("--effort", default="")
    p.add_argument("--no-window", action="store_true", help="keep the PTY; attach later")
    for command, help_text in (("read", "read a short screen"), ("wait", "wait for quiet; inspect dialogs/errors too"),
                               ("ask", "send a prompt and wait"), ("send", "send a prompt"),
                               ("keys", "send keys: Enter, Escape, C-c, ..."),
                               ("model", "change model/effort"), ("attach", "attach here; Ctrl-] detaches"),
                               ("kill", "stop a session and its children")):
        p = sub.add_parser(command, help=help_text)
        p.add_argument("name")
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
    p = sub.add_parser("models", help="list available models")
    p.add_argument("kind", choices=INTERACTIVE)
    sub.add_parser("ls", help="list sessions (does not start a daemon)")
    a = parser.parse_args()
    OWNER = caller_owner() if a.command != "daemon" else None
    if a.command == "daemon":
        run_daemon()
        return
    if a.command == "spawn":
        result = spawn_agent(a.kind, a.name, a.cwd, a.prompt, a.model, a.effort, visible=not a.no_window)
    elif a.command == "models":
        result = available_models(a.kind)
    elif a.command == "ls":
        result = list_agents() if os.path.exists(SOCK) else "no agents"
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
