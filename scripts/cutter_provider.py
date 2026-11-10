"""Provider-bound cutter PTYs. No live configuration mutation or guessed IDs."""
from __future__ import annotations
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from urllib.parse import parse_qs, urlparse
import uuid

PROVIDERS = ("claude", "codex")
BUFFER_LIMIT = 400_000
# LEAN-START (18.09.2026): video-cutter, shortform-cutting und broll-ersteller
# nutzen keine MCP-Tools (alles Skripte/CLI). Ohne diese Flags zog jede
# Cockpit-Session ~14 MCP-Server hoch (gemessen: 13 Sessions = 189 Prozesse,
# Swap 93 %). Codex hat sein Pendant in cutter_codex_runtime.py.
LEAN_ARGS = ("--strict-mcp-config", "--mcp-config", '{"mcpServers": {}}')


def provider_name(value):
    if value not in PROVIDERS:
        raise ValueError("Anbieter muss claude oder codex sein")
    return value


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return {} if default is None else default


def atomic_json(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class ProviderStore:
    def __init__(self, workdir):
        self.workdir = Path(workdir).resolve()
        self.path = self.workdir / "cutter_providers.json"

    def read(self):
        doc = read_json(self.path, {"version": 1, "provider": "claude"})
        if not isinstance(doc, dict) or doc.get("version", 1) != 1:
            raise ValueError("Cutter-Anbieterdatei ungültig; Original bleibt erhalten")
        provider_name(doc.get("provider", "claude"))
        return doc

    def select(self, provider):
        provider_name(provider)
        with file_lock(str(self.path) + ".lock"):
            doc = self.read()
            doc.update(version=1, provider=provider)
            atomic_json(self.path, doc)
        return doc

    def identity_path(self, provider):
        return self.workdir / ("claude_session.json" if provider == "claude" else "codex_session.json")

    def status(self, provider):
        return read_json(self.workdir / ("agent_status.json" if provider == "claude" else "codex_session.json"), {})

    def fresh(self, provider):
        path = self.identity_path(provider)
        if path.exists():
            archive = self.workdir / ".cutter-session-history"
            archive.mkdir(exist_ok=True)
            path.rename(archive / (provider + "-" + uuid.uuid4().hex + ".json"))


@dataclass(frozen=True)
class Launch:
    argv: tuple[str, ...]
    cwd: str
    env: dict[str, str]
    provider: str


def process_tree(roots):
    """roots plus alle Nachfahren laut ps. Fuer den Session-Kill: obsidian-mcp und
    whatsapp-mcp ignorieren SIGHUP und blieben sonst als Waisen zurueck (18.09.2026:
    100 + 72 Stueck gefunden)."""
    try:
        listed = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return set(roots)
    children = {}
    for line in listed.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    seen, stack = set(), list(roots)
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
    return seen


def terminate_tree(pids, grace=2.0, sleep=time.sleep):
    """SIGTERM an alle pids, nach grace Sekunden SIGKILL an Ueberlebende. Gibt die
    Ueberlebenden nach SIGKILL zurueck (normalerweise leer)."""
    alive = set()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            alive.add(pid)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + grace
    while alive and time.time() < deadline:
        sleep(0.1)
        for pid in list(alive):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive.discard(pid)
    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return alive


def session_name(workdir, provider, shortform=False):
    workdir = str(Path(workdir).resolve())
    base = re.sub(r"[^A-Za-z0-9_-]", "_", Path(workdir).name)
    if provider == "claude" and not shortform:
        return "cutter-" + base
    digest = hashlib.sha256(workdir.encode()).hexdigest()[:10]
    return "cutter-" + provider + ("-sf-" if shortform else "-") + base[:45] + "-" + digest


def terminal_launch(workdir, project_dir, provider, shortform=False, resolver=shutil.which):
    provider_name(provider)
    store = ProviderStore(workdir)
    cwd = str(Path(project_dir).resolve())
    env = dict(os.environ)
    env.update(TERM="xterm-256color", CUTTER_WORKDIR=str(store.workdir),
               CUTTER_PROJECT=store.workdir.name, CUTTER_PROVIDER=provider)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    identity = read_json(store.identity_path(provider), {})
    if provider == "claude":
        binary = resolver("claude")
        if not binary:
            raise RuntimeError("Claude CLI nicht gefunden. PATH prüfen.")
        args = [binary, "--agent", "shortform-cutting" if shortform else "video-cutter", *LEAN_ARGS]
        sid = str(identity.get("session_id") or "")
        # Resume nur, wenn in der Session geschrieben wurde (Hook setzt hasUserTurn).
        # Legacy-Datei ohne Feld: Resume wie bisher, Fallback auf frisch macht der Wrapper.
        if sid and identity.get("hasUserTurn") is not False:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
                raise ValueError("Ungültige Claude-Session-ID")
            args += ["--resume", sid]
        return Launch(tuple(args), cwd, env, provider)
    if identity.get("provider", "codex") != "codex":
        raise ValueError("Codex-Metadaten gehören zu einem anderen Anbieter")
    if identity.get("tabId") and identity["tabId"] != session_name(workdir, provider, shortform):
        raise ValueError("Codex-Metadaten gehören zu einem anderen Cutter-Workdir")
    binary = resolver("codex")
    if not binary:
        raise RuntimeError("Codex CLI nicht gefunden. PATH prüfen.")
    bridge = os.environ.get("CUTTER_CODEX_BRIDGE") or str(Path(__file__).with_name("codex-terminal-bridge.cjs"))
    if not bridge or not Path(bridge).is_file():
        raise RuntimeError("Codex-Terminalbridge fehlt. CUTTER_CODEX_BRIDGE auf das geprüfte Bridge-Skript setzen.")
    node = resolver("node")
    if not node:
        raise RuntimeError("Node.js für Codex-Terminalbridge nicht gefunden")
    profile = "shortform-cutting" if shortform else "video-cutter"
    runtime = ["--no-alt-screen", "--add-dir", str(Path(workdir).resolve()), "-p", profile, "-C", cwd]
    sid = None
    if identity.get("identityStatus") == "verified" and identity.get("hasUserTurn") is True:
        sid = identity.get("nativeThreadId")
    elif identity.get("resumeThreadId"):
        # Explicit previously verified target survives an unsuccessful startup.
        sid = identity["resumeThreadId"]
    elif identity.get("nativeThreadId") and identity.get("hasUserTurn") is not False:
        raise ValueError("Codex-Thread-ID noch nicht als fortsetzbar verifiziert")
    if sid:
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
            raise ValueError("Ungültige Codex-Thread-ID")
        runtime += ["resume", sid]
    wrapper = Path(__file__).with_name("cutter_codex_runtime.py").resolve()
    if not wrapper.is_file():
        raise RuntimeError("Codex Cutter runtime wrapper missing")
    env["CUTTER_CODEX_REAL_BIN"] = binary
    env["CODEX_BIN"] = str(wrapper)
    args = (node, str(Path(bridge).resolve()), "--metadata", str(store.identity_path("codex")),
            "--launch-id", str(uuid.uuid4()), "--tab-id", session_name(workdir, provider, shortform), "--", *runtime)
    return Launch(args, cwd, env, provider)


class TerminalSession:
    def __init__(self, provider):
        self.provider = provider
        self.lock = threading.RLock()
        self.fd = self.pid = None
        self.buf = b""
        self.subs = []
        self.alive = self.tmux = False
        self.generation = 0
        self.exit_code = None
        self.profile = None
        self.last_viewer_at = None

    def append(self, chunk):
        with self.lock:
            self.buf = (self.buf + chunk)[-BUFFER_LIMIT:]
            for subscriber in list(self.subs):
                try:
                    subscriber.put_nowait(chunk)
                except queue.Full:
                    self.subs.remove(subscriber)
                    while not subscriber.empty():
                        subscriber.get_nowait()
                    subscriber.put_nowait(None)

    def attach(self):
        subscriber = queue.Queue(maxsize=128)
        with self.lock:
            replay = self.buf
            self.subs.append(subscriber)
            if not self.alive:
                subscriber.put_nowait(None)
        return replay, subscriber

    def detach(self, subscriber):
        with self.lock:
            if subscriber in self.subs:
                self.subs.remove(subscriber)
            if not self.subs:
                self.last_viewer_at = time.time()

    def write(self, data):
        with self.lock:
            if self.fd is None or not self.alive:
                return False
            try:
                os.write(self.fd, data)
                return True
            except OSError:
                return False

    def resize(self, cols=120, rows=32):
        cols, rows = max(20, min(500, int(cols))), max(5, min(200, int(rows)))
        with self.lock:
            if self.fd is not None:
                try:
                    fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                except OSError:
                    pass

    def stop_attach(self):
        with self.lock:
            pid = self.pid
            self.pid = self.fd = None
            self.alive = False
            self.generation += 1
            for subscriber in list(self.subs):
                try:
                    subscriber.put_nowait(None)
                except queue.Full:
                    pass
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def reader(self, fd, pid, generation):
        try:
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                with self.lock:
                    if generation != self.generation:
                        break
                    self.append(chunk)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                _, status = os.waitpid(pid, 0)
                exit_code = os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                exit_code = None
            with self.lock:
                if generation == self.generation:
                    self.alive = False
                    self.fd = self.pid = None
                    self.exit_code = exit_code
                    for subscriber in list(self.subs):
                        try:
                            subscriber.put_nowait(None)
                        except queue.Full:
                            pass


class TerminalManager:
    def __init__(self, workdir, project_dir, shortform=False, launch_builder=terminal_launch,
                 profile=None, resume_fallback=True):
        self.store = ProviderStore(workdir)
        self.project_dir = str(Path(project_dir).resolve())
        self.shortform = shortform
        self.launch_builder = launch_builder
        self.profile = profile or ("shortform-cutting" if shortform else "video-cutter")
        self.resume_fallback = resume_fallback
        self.sessions = {provider: TerminalSession(provider) for provider in PROVIDERS}
        self.start_lock = threading.RLock()

    def selected(self):
        return self.store.read().get("provider", "claude")

    def select(self, provider):
        self.store.select(provider_name(provider))
        return self.state()

    def session(self, provider=None):
        return self.sessions[provider_name(provider or self.selected())]

    def start(self, provider=None, cols=120, rows=32):
        provider = provider_name(provider or self.selected())
        session = self.session(provider)
        with self.start_lock, session.lock:
            if session.alive:
                return session
            launch = self.launch_builder(str(self.store.workdir), self.project_dir, provider, self.shortform)
            command = list(launch.argv)
            if self.resume_fallback and provider == "claude" and "--resume" in command:
                ai, ri = command.index("--agent"), command.index("--resume")
                rid, role = command[ri + 1], command[ai + 1]
                extra = [arg for index, arg in enumerate(command) if index not in (0, ai, ai + 1, ri, ri + 1)]
                command = [sys.executable, str(Path(__file__).with_name("cutter_claude_resume.py")), command[0], role, rid, *extra]
            tmux = shutil.which("tmux")
            name = session_name(self.store.workdir, provider, self.shortform)
            use_tmux = False
            actual_profile = self.profile
            if tmux:
                with file_lock(self.store.workdir / (".cutter-start-" + provider + ".lock")):
                    exists = subprocess.run([tmux, "has-session", "-t", "=" + name], capture_output=True, timeout=5).returncode == 0
                    if not exists:
                        args = [tmux, "new-session", "-d", "-s", name, "-x", str(cols), "-y", str(rows), "-c", launch.cwd]
                        for key in ("CUTTER_WORKDIR", "CUTTER_PROJECT", "CUTTER_PROVIDER", "TERM", "PATH", "CODEX_BIN", "CUTTER_CODEX_REAL_BIN"):
                            if key in launch.env:
                                args += ["-e", key + "=" + launch.env[key]]
                        subprocess.run(args + command, env=launch.env, check=True, capture_output=True, timeout=10)
                        subprocess.run([tmux, "set-option", "-t", name, "status", "off"], capture_output=True, timeout=5)
                        for key, value in (("@cutter-workdir", str(self.store.workdir)), ("@cutter-provider", provider),
                                           ("@cutter-profile", actual_profile)):
                            subprocess.run([tmux, "set-option", "-t", name, key, value], capture_output=True, timeout=5)
                    else:
                        # Attaching does not change the role of an already running CLI.
                        found = subprocess.run([tmux, "show-options", "-v", "-t", name, "@cutter-profile"],
                                               capture_output=True, text=True, timeout=5)
                        recorded = found.stdout.strip() if found.returncode == 0 else ""
                        actual_profile = recorded if recorded in ("shortform-cutting", "video-cutter", "broll-ersteller") else None
                    use_tmux = True
            argv = [tmux, "attach-session", "-t", "=" + name] if use_tmux else command
            pid, fd = pty.fork()
            if pid == 0:
                try:
                    os.chdir(launch.cwd)
                    os.execvpe(argv[0], argv, launch.env)
                except Exception as error:
                    os.write(2, ("\r\nTerminalstart fehlgeschlagen: " + str(error) + "\r\n").encode())
                    os._exit(1)
            session.fd, session.pid = fd, pid
            session.profile = actual_profile
            atomic_json(self.store.workdir / ("cutter_terminal_role_" + provider + ".json"),
                        {"profile": actual_profile, "session": name, "provider": provider,
                         "status": "configured" if actual_profile else "legacy_unknown"})
            session.buf, session.alive, session.tmux = b"", True, use_tmux
            session.generation += 1
            generation = session.generation
            session.resize(cols, rows)
            threading.Thread(target=session.reader, args=(fd, pid, generation), daemon=True).start()
            return session

    def reconnect(self, provider):
        session = self.session(provider_name(provider))
        return session if session.alive else self.start(provider)

    def end_and_new(self, provider):
        provider_name(provider)
        with self.start_lock:
            session = self.session(provider)
            session.stop_attach()
            tmux = shutil.which("tmux")
            if tmux and session.tmux:
                subprocess.run([tmux, "kill-session", "-t", "=" + session_name(self.store.workdir, provider, self.shortform)], capture_output=True, timeout=5)
            self.store.fresh(provider)
            return self.start(provider)

    # --- Session-Lifecycle (18.09.2026) -------------------------------------
    # Projekt zu -> tmux-Session (Claude) endet; Projekt auf -> start() resumed.
    # Siehe cutter_session_lifecycle.py fuer die Entscheidung, hier nur Werkzeuge.

    def tmux_info(self, provider):
        """(existiert, letzte Aktivitaet als Epoch, Client haengt) der tmux-Session."""
        tmux = shutil.which("tmux")
        if not tmux:
            return False, None, False
        name = session_name(self.store.workdir, provider, self.shortform)
        try:
            found = subprocess.run([tmux, "list-sessions", "-F", "#{session_name}|#{session_activity}|#{session_attached}"],
                                   capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False, None, False
        if found.returncode != 0:
            return False, None, False
        for line in found.stdout.splitlines():
            parts = line.split("|")
            if parts[0] != name:
                continue
            try:
                activity = int(parts[1])
            except (IndexError, ValueError):
                activity = None
            return True, activity, len(parts) > 2 and parts[2] not in ("", "0")
        return False, None, False

    def working(self, provider, now=None, activity_window=120, status_window=1800):
        """True, solange die Session sichtbar arbeitet: Hook-Status 'arbeitet' (juenger als
        status_window) oder tmux-Aktivitaet juenger als activity_window. Claude schreibt beim
        Arbeiten laufend (Spinner), idle gar nicht (gemessen 18.09.2026: idle = Stunden ohne Output)."""
        now = time.time() if now is None else now
        status = self.store.status(provider) if provider == "claude" else {}
        if status.get("status") == "arbeitet":
            try:
                stamp = time.mktime(time.strptime(str(status.get("ts", "")), "%Y-%m-%d %H:%M:%S"))
            except (TypeError, ValueError, OverflowError):
                stamp = None
            if stamp is None or now - stamp < status_window:
                return True
        exists, activity, _ = self.tmux_info(provider)
        return bool(exists and activity is not None and now - activity < activity_window)

    def viewers(self):
        return sum(len(session.subs) for session in self.sessions.values())

    def pane_pids(self, provider):
        tmux = shutil.which("tmux")
        if not tmux:
            return []
        name = session_name(self.store.workdir, provider, self.shortform)
        try:
            found = subprocess.run([tmux, "list-panes", "-t", "=" + name, "-F", "#{pane_pid}"],
                                   capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return []
        return [int(p) for p in found.stdout.split() if p.isdigit()] if found.returncode == 0 else []

    def kill_session(self, provider, reason=""):
        """Beendet die persistente tmux-Session (und damit die CLI) dieses Providers samt
        Prozessbaum. Anders als end_and_new bleibt die Identity-Datei: der naechste start() resumed."""
        provider_name(provider)
        with self.start_lock:
            session = self.session(provider)
            exists, _, _ = self.tmux_info(provider)
            tree = process_tree(self.pane_pids(provider)) if exists else set()
            if session.alive:
                session.stop_attach()
            if exists:
                subprocess.run([shutil.which("tmux"), "kill-session", "-t",
                                "=" + session_name(self.store.workdir, provider, self.shortform)],
                               capture_output=True, timeout=5)
            if tree:
                time.sleep(0.5)             # SIGHUP durch tmux wirken lassen, dann den Rest holen
                terminate_tree(tree)
            return exists

    def close(self):
        for session in self.sessions.values():
            session.stop_attach()

    def state(self):
        providers = {}
        for provider, session in self.sessions.items():
            identity = read_json(self.store.identity_path(provider), {})
            status = self.store.status(provider)
            intended = self.profile
            role = read_json(self.store.workdir / ("cutter_terminal_role_" + provider + ".json"), {})
            if role and role.get("session") != session_name(self.store.workdir, provider, self.shortform):
                role = {}
            profile = session.profile or (role.get("profile") if role else identity.get("profile") or identity.get("agent"))
            has_prior_session = bool(identity.get("nativeThreadId") or identity.get("session_id") or role or session.alive)
            providers[provider] = {
                "provider": provider, "profile": profile if has_prior_session else intended,
                "intendedProfile": intended, "profileStatus": "known" if profile or not has_prior_session else "legacy_unknown",
                "alive": session.alive, "persistent": session.tmux,
                "nativeThreadId": identity.get("nativeThreadId") if provider == "codex" else identity.get("session_id"),
                "status": status.get("status") or ("bereit" if session.alive else "beendet"),
                "session": session_name(self.store.workdir, provider, self.shortform),
            }
        return {"provider": self.selected(), "providers": providers, "workdir": str(self.store.workdir)}


class TerminalHandlerMixin:
    def terminal_manager(self):
        raise NotImplementedError

    def request_provider(self, body=None):
        value = (body or {}).get("provider") or (parse_qs(urlparse(self.path).query).get("provider") or [None])[0]
        return provider_name(value or self.terminal_manager().selected())

    def terminal_state(self):
        return self._send_json(self.terminal_manager().state())

    def terminal_select(self):
        if not self._term_origin_ok():
            return self._send_json({"error": "forbidden"}, 403)
        try:
            return self._send_json(self.terminal_manager().select(self._read_post_json().get("provider")))
        except (ValueError, TypeError) as error:
            return self._send_json({"error": str(error)}, 400)

    def serve_term_stream(self):
        if not self._term_origin_ok():
            return self._send_json({"error": "forbidden"}, 403)
        try:
            session = self.terminal_manager().start(self.request_provider())
        except (RuntimeError, ValueError, subprocess.SubprocessError) as error:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            chunk = base64.b64encode(("\r\n" + str(error) + "\r\n").encode()).decode()
            self.wfile.write(("data: " + chunk + "\n\nevent: exit\ndata: 1\n\n").encode())
            self.close_connection = True
            return
        replay, subscriber = session.attach()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if replay:
                self.wfile.write(("data: " + base64.b64encode(replay).decode() + "\n\n").encode())
                self.wfile.flush()
            while True:
                try:
                    data = subscriber.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if data is None:
                    self.wfile.write(b"event: exit\ndata: 1\n\n")
                    return
                self.wfile.write(("data: " + base64.b64encode(data).decode() + "\n\n").encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.close_connection = True
            session.detach(subscriber)

    def term_input(self):
        if not self._term_origin_ok():
            return self._send_json({"error": "forbidden"}, 403)
        body = self._read_post_json()
        try:
            data = base64.b64decode(body.get("data", ""), validate=True)
            ok = self.terminal_manager().session(self.request_provider(body)).write(data)
            return self._send_json({"ok": ok})
        except (ValueError, TypeError) as error:
            return self._send_json({"error": str(error)}, 400)

    def term_resize_route(self):
        if not self._term_origin_ok():
            return self._send_json({"error": "forbidden"}, 403)
        body = self._read_post_json()
        self.terminal_manager().session(self.request_provider(body)).resize(body.get("cols", 120), body.get("rows", 32))
        return self._send_json({"ok": True})

    def term_restart(self):
        if not self._term_origin_ok():
            return self._send_json({"error": "forbidden"}, 403)
        self.terminal_manager().reconnect(self.request_provider(self._read_post_json()))
        return self._send_json({"ok": True})

    def term_kill_session_route(self):
        if not self._term_origin_ok():
            return self._send_json({"error": "forbidden"}, 403)
        self.terminal_manager().end_and_new(self.request_provider(self._read_post_json()))
        return self._send_json({"ok": True})
