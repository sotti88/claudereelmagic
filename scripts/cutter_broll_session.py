"""Isolated, exact-session B-roll pickup terminals; never replace the cutting chat."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import threading

import cutter_provider as cp
from cutter_broll_jobs import process_group_alive

_MANAGERS = {}
_LOCK = threading.RLock()
_ID = re.compile(r"[A-Za-z0-9_-]+")
# Merker im Projekt-Workdir: welche Pickup-Sessions offen waren. Beim Oeffnen
# des Projekts startet der Server sie wieder (Session-Lifecycle, 18.09.2026).
MARKER = "cutter_open_sessions.json"


def _remember(project_workdir, job_id):
    path = Path(project_workdir).resolve() / MARKER
    with cp.file_lock(str(path) + ".lock"):
        doc = cp.read_json(path, {}) or {}
        jobs = [j for j in doc.get("broll_jobs", []) if j != job_id] + [job_id]
        cp.atomic_json(path, {"version": 1, "broll_jobs": jobs})


def open_jobs(workdir):
    doc = cp.read_json(Path(workdir).resolve() / MARKER, {}) or {}
    return [j for j in doc.get("broll_jobs", []) if isinstance(j, str) and _ID.fullmatch(j)]


def restore(workdir, project_dir, log=None):
    """Startet die B-Roll-Pickup-Sessions wieder, die beim letzten Schliessen offen waren."""
    log = log or (lambda text: None)
    started = []
    for job_id in open_jobs(workdir):
        try:
            manager = manager_for(workdir, project_dir, job_id)
            if manager.session().alive:
                continue
            manager.start()
            started.append(job_id)
            log("[cockpit] B-Roll-Session %s wieder gestartet (Resume)" % job_id)
        except (ValueError, RuntimeError, OSError) as error:
            log("[cockpit] B-Roll-Session %s nicht wiederhergestellt: %s" % (job_id, error))
    return started


def managers():
    with _LOCK:
        return dict(_MANAGERS)


def job_directory(workdir, job_id=None):
    root = Path(workdir).resolve() / "broll_jobs"
    if job_id is None:
        candidates = [(cp.read_json(p), p.parent) for p in root.glob("*/job.json")]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0].get("createdAt", 0))[1]
    if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
        raise ValueError("Ungültige B-Roll-Auftrags-ID")
    directory = (root / job_id).resolve()
    if directory.parent != root or not (directory / "job.json").is_file():
        raise ValueError("B-Roll-Auftrag gehört nicht zu diesem Projekt")
    return directory


def _native_identity(directory, doc):
    identity = cp.read_json(directory / "pickup_identity.json")
    sid = doc.get("nativeThreadId") or identity.get("nativeThreadId")
    if sid:
        if not isinstance(sid, str) or not _ID.fullmatch(sid):
            raise ValueError("Ungültige native B-Roll-Session-ID")
        return sid
    # Legacy Claude jobs printed only their final answer. Recover by exact first
    # user prompt in this cwd, never by --last, name similarity, or latest chat.
    if doc.get("provider") != "claude":
        return None
    cwd = str(Path(doc["cwd"]).resolve())
    project = Path.home() / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)
    brief = (directory / "brief.txt").read_text(encoding="utf-8").strip()
    found = []
    for path in project.glob("*.jsonl"):
        if path.stat().st_mtime < doc.get("createdAt", 0) - 120:
            continue
        with path.open(encoding="utf-8") as stream:
            for _ in range(32):
                line = stream.readline(524288)
                if not line:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") != "user":
                    continue
                content = event.get("message", {}).get("content", "")
                if isinstance(content, list):
                    content = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
                if (str(content).strip() == brief and event.get("cwd") == cwd
                        and not event.get("isSidechain") and event.get("sessionId") == path.stem):
                    found.append((path.stem, path))
                break
    if len(found) > 1:
        raise ValueError("Mehrere Sessions zum B-Roll-Auftrag gefunden; keine automatisch gewählt")
    if not found:
        return None
    sid, transcript = found[0]
    if not _ID.fullmatch(sid):
        raise ValueError("Ungültige wiedergefundene B-Roll-Session-ID")
    cp.atomic_json(directory / "pickup_identity.json", {
        "provider": "claude", "nativeThreadId": sid, "job_id": doc["id"],
        "identityStatus": "verified", "evidence": str(transcript), "match": "exact_first_user_brief_and_cwd"})
    return sid


def readable_log(directory, limit=48000):
    path = directory / "output.log"
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        size = path.stat().st_size
        stream.seek(max(0, size - 262144))
        raw = stream.read().decode("utf-8", errors="replace")
    text = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            if not line.lstrip().startswith("{"):
                text.append(line)
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    text.append("[" + block.get("name", "Tool") + "]")
        elif event.get("type") == "result":
            text.append(event.get("result", ""))
        elif event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") in ("agent_message", "reasoning"):
                text.append(item.get("text", ""))
            elif item.get("type") == "command_execution":
                text.append("[Befehl abgeschlossen]")
    return "\n".join(text)[-limit:]


def _active_production(workdir):
    for path in (Path(workdir).resolve() / "broll_jobs").glob("*/job.json"):
        doc = cp.read_json(path)
        if doc.get("status") in ("queued", "running") and process_group_alive(path.parent, doc) is not False:
            return doc["id"]
    return None


def session_state(workdir, job_id=None):
    directory = job_directory(workdir, job_id)
    if directory is None:
        return {"job_id": None, "can_resume": False, "log": ""}
    doc = cp.read_json(directory / "job.json")
    result = {"job_id": doc["id"], "provider": doc["provider"], "status": doc.get("status"),
              "profile": "broll-ersteller", "nativeThreadId": None, "can_resume": False,
              "log": readable_log(directory)}
    try:
        sid = _native_identity(directory, doc)
        result["nativeThreadId"] = sid
        running = _active_production(workdir)
        result["running_job_id"] = running
        result["can_resume"] = bool(sid and not running and doc.get("status") not in ("queued", "running"))
        if not sid and not running:
            result["error"] = "Die ursprüngliche B-Roll-Session konnte noch nicht eindeutig zugeordnet werden."
    except (ValueError, OSError) as error:
        result["error"] = str(error)
    return result


def _launch(workdir, project_dir, provider, shortform=False):
    directory = Path(workdir).resolve()
    doc = cp.read_json(directory / "job.json")
    if provider != doc["provider"]:
        raise ValueError("Der Anbieter dieses B-Roll-Auftrags bleibt unverändert")
    sid = _native_identity(directory, doc)
    if not sid:
        raise ValueError("Keine eindeutig zugeordnete B-Roll-Session zum Fortsetzen")
    if provider == "codex":
        cp.atomic_json(directory / "codex_session.json", {
            "provider": provider, "nativeThreadId": sid, "identityStatus": "verified", "hasUserTurn": True,
            "profile": "broll-ersteller", "tabId": cp.session_name(directory, provider, True)})
        launch = cp.terminal_launch(directory, doc["cwd"], provider, True)
        return cp.Launch(tuple("broll-ersteller" if arg == "shortform-cutting" else arg for arg in launch.argv),
                         launch.cwd, launch.env, provider)
    binary = shutil.which("claude")
    if not binary:
        raise RuntimeError("Claude CLI nicht gefunden")
    env = dict(os.environ)
    env.update(TERM="xterm-256color", CUTTER_WORKDIR=str(directory), CUTTER_PROJECT=directory.name, CUTTER_PROVIDER=provider)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    cp.atomic_json(directory / "claude_session.json", {"session_id": sid, "agent": "broll-ersteller"})
    return cp.Launch((binary, "--agent", "broll-ersteller", "--resume", sid,
                      "--settings", '{"disableAllHooks": true}', "--strict-mcp-config", "--mcp-config", '{"mcpServers": {}}'),
                     doc["cwd"], env, provider)


class BrollTerminalManager(cp.TerminalManager):
    def __init__(self, directory, project_dir):
        self.job = cp.read_json(directory / "job.json")
        self.project_workdir = directory.parent.parent
        super().__init__(directory, project_dir, shortform=True, launch_builder=_launch,
                         profile="broll-ersteller", resume_fallback=False)

    def selected(self):
        return self.job["provider"]

    def select(self, provider):
        if provider != self.selected():
            raise ValueError("Der Anbieter dieses B-Roll-Auftrags bleibt unverändert")
        return self.state()

    def session(self, provider=None):
        if provider and provider != self.selected():
            raise ValueError("Falscher Anbieter für die B-Roll-Session")
        return super().session(provider)

    def start(self, provider=None, cols=120, rows=32):
        # Serialize with production enqueue: never resume the writer's native
        # thread while a background generation for this project is still active.
        with cp.file_lock(self.project_workdir / "broll_jobs/.start.lock"):
            state = session_state(self.project_workdir, self.job["id"])
            if not state["can_resume"]:
                raise ValueError(state.get("error") or "B-Roll wird noch erstellt. Die Session ist danach fortsetzbar.")
            session = super().start(provider, cols, rows)
            _remember(self.project_workdir, self.job["id"])
            return session

    def end_and_new(self, provider):
        raise ValueError("Die ursprüngliche B-Roll-Session bleibt erhalten; keine neue Session als Ersatz")


def manager_for(workdir, project_dir, job_id):
    directory = job_directory(workdir, job_id)
    if directory is None:
        raise ValueError("Kein B-Roll-Auftrag vorhanden")
    with _LOCK:
        key = str(directory)
        if key not in _MANAGERS:
            _MANAGERS[key] = BrollTerminalManager(directory, project_dir)
        return _MANAGERS[key]


def close_all():
    with _LOCK:
        for manager in _MANAGERS.values():
            manager.close()
        _MANAGERS.clear()
