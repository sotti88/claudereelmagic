"""Persist each B-roll job's provider before launch; serialize duplicate starts."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from cutter_provider import atomic_json, file_lock, provider_name, read_json, session_name


def headless_argv(provider, resolver=shutil.which, role_file=None):
    provider_name(provider)
    binary = resolver(provider)
    if not binary:
        raise RuntimeError(provider + " CLI nicht gefunden")
    if provider == "claude":
        return [binary, "-p", "--output-format", "stream-json", "--verbose", "--agent", "broll-ersteller", "--settings", '{"disableAllHooks": true}',
                "--strict-mcp-config", "--mcp-config", '{"mcpServers": {}}']
    # Keep the installed broll-ersteller profile, account and permission settings.
    layer = Path.home() / ".codex/broll-ersteller.config.toml"
    if not layer.is_file():
        raise RuntimeError("Codex B-Roll layer missing; run codex-sync-agents before production")
    role = Path(role_file or (Path.home() / ".claude/agents/broll-ersteller.md"))
    # Disabled MCP entries are still deserialized by Codex. Ignore unrelated
    # base config entirely, retaining its account and only explicit execution
    # choices. The generated role layer carries the role's reasoning effort.
    argv = [binary, "exec", "--ignore-user-config", "-p", "broll-ersteller", "--config",
            "developer_instructions=" + json.dumps(role.read_text(encoding="utf-8"))]
    config = Path.home() / ".codex/config.toml"
    base = tomllib.loads(config.read_text()) if config.exists() else {}
    for key in ("model", "sandbox_mode", "approval_policy", "service_tier"):
        if isinstance(base.get(key), str):
            argv.extend(["--config", key + "=" + json.dumps(base[key])])
    if base.get("model_provider") not in (None, "openai"):
        raise RuntimeError("Custom model provider needs a dedicated validated B-roll execution layer")
    return argv + ["--json", "-"]


def process_group_alive(directory, doc):
    """A dead runner can leave its CLI alive; check the isolated process group."""
    launch = read_json(Path(directory) / "launch.json")
    pgid = launch.get("processGroup") or doc.get("pid")
    if not pgid:
        return None  # Legacy queued jobs have no reliable owner identity.
    try:
        os.killpg(int(pgid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def enqueue(workdir, project_dir, provider, text=None, popen=subprocess.Popen, request=None):
    provider_name(provider)
    root = Path(workdir).resolve() / "broll_jobs"
    root.mkdir(exist_ok=True)
    if request is not None:
        from broll_contract import assert_snapshot_current
        if request.get("schema_version") != 2 or Path(request["workdir"]).resolve() != Path(workdir).resolve():
            raise ValueError("Invalid frozen B-roll request")
        # Provider is not part of media identity: switching it must not duplicate a paid retry.
        digest = request["snapshot_id"]
    else:
        if not text:
            raise ValueError("B-roll request missing")
        digest = hashlib.sha256((provider + "\0" + text).encode()).hexdigest()
    with file_lock(root / ".start.lock"):
        # Identical retries must not create a second paid job, including after restart.
        for existing in root.glob("*/job.json"):
            doc = read_json(existing)
            if doc.get("status") in ("queued", "running"):
                alive = process_group_alive(existing.parent, doc)
                age = time.time() - doc.get("createdAt", time.time())
                if alive is False and (doc.get("status") == "running" or age > 120):
                    doc.update(status="failed", error="B-Roll process group ended before completion", finishedAt=time.time())
                    atomic_json(existing, doc)
                    continue
                # One writer per workdir. Switching UI provider never starts a competing media job.
                return dict(doc, duplicate=True, busy=doc.get("fingerprint") != digest)
            if doc.get("fingerprint") == digest and doc.get("status") != "failed":
                return dict(doc, duplicate=True, busy=False)
        # A resumed B-roll terminal is another potential writer. The cutting
        # terminal stays independent; never stop or replace either session here.
        tmux = shutil.which("tmux")
        if tmux:
            active = subprocess.run([tmux, "list-sessions", "-F", "#{session_name}"],
                                    capture_output=True, text=True, timeout=5)
            live_names = set(active.stdout.splitlines()) if active.returncode == 0 else set()
            for existing in root.glob("*/job.json"):
                previous = read_json(existing)
                if session_name(existing.parent, previous.get("provider", "claude"), True) in live_names:
                    raise ValueError("Eine B-Roll-Session ist noch geöffnet. Arbeite dort weiter oder beende sie mit /exit, bevor du einen neuen Auftrag startest.")
        if request is not None:
            assert_snapshot_current(workdir, request)
        job_id = uuid.uuid4().hex
        directory = root / job_id
        directory.mkdir(mode=0o700)
        role = Path.home() / ".claude/agents/broll-ersteller.md"
        role_text = role.read_text(encoding="utf-8")
        if role_text.startswith("---\n"):
            role_text = role_text.split("---", 2)[2].lstrip()
        role_file = directory / "role.md"
        role_file.write_text(role_text, encoding="utf-8")
        argv = headless_argv(provider, role_file=role_file)
        native_id = str(uuid.uuid4()) if provider == "claude" else None
        if native_id:
            argv.extend(["--session-id", native_id])
        if provider == "codex":
            argv[2:2] = ["--add-dir", str(Path(workdir).resolve())]
        if request is not None:
            frozen = dict(request, job_id=job_id, result_path=str(directory / "result.json"))
            atomic_json(directory / "auftrag.json", frozen)
            from broll_brief import build_brief
            text = build_brief(frozen)
        brief = directory / "brief.txt"
        fd = os.open(brief, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text + "\n")
        doc = {"version": 1, "id": job_id, "provider": provider, "profile": "broll-ersteller",
               "fingerprint": digest, "status": "queued", "createdAt": time.time(),
               "argv": argv, "cwd": str(Path(project_dir).resolve()), "brief": str(brief),
               "nativeThreadId": native_id,
               "schema_version": 2 if request else 1, "snapshot_id": request["snapshot_id"] if request else None}
        atomic_json(directory / "job.json", doc)
        try:
            env = dict(os.environ)
            for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CUTTER_WORKDIR", "CUTTER_PROJECT"):
                env.pop(key, None)
            with open(directory / "launcher.log", "ab") as log:
                child = popen([sys.executable, str(Path(__file__).resolve()), "--run", str(directory)],
                              stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                              cwd=doc["cwd"], env=env, start_new_session=True)
            atomic_json(directory / "launch.json", {"launcherPid": child.pid, "processGroup": child.pid,
                                                    "createdAt": time.time()})
            return dict(doc, launcherPid=child.pid, duplicate=False)
        except Exception as error:
            doc.update(status="failed", error=str(error))
            atomic_json(directory / "job.json", doc)
            raise


def execute(directory):
    directory = Path(directory).resolve()
    path = directory / "job.json"
    with file_lock(directory / ".run.lock"):
        doc = read_json(path)
        if doc.get("status") != "queued":
            return 0
        doc.update(status="running", startedAt=time.time(), pid=os.getpid())
        atomic_json(path, doc)
        try:
            with open(doc["brief"], "rb") as source, open(directory / "output.log", "ab") as output:
                result = subprocess.Popen(doc["argv"], stdin=source, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, cwd=doc["cwd"])
                for line in iter(result.stdout.readline, b""):
                    output.write(line)
                    output.flush()
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    sid = (event.get("session_id") if doc["provider"] == "claude" else
                           event.get("thread_id") if event.get("type") == "thread.started" else None)
                    if sid and sid != doc.get("nativeThreadId"):
                        doc["nativeThreadId"] = sid
                        atomic_json(path, doc)
                result.stdout.close()
                result.wait()
            if result.returncode == 0 and doc.get("schema_version") == 2:
                from broll_contract import validate_result
                validated = validate_result(read_json(directory / "auftrag.json"), read_json(directory / "result.json"))
                atomic_json(directory / "validation.json", {"valid": True, "scene_count": len(validated["scenes"]),
                                                          "sfx_count": len(validated["sfx_events"]), "snapshot_id": validated["snapshot_id"]})
            # Legacy jobs remain readable, but exit 0 alone is no production proof.
            if result.returncode == 0 and doc.get("schema_version") != 2:
                raise ValueError("Legacy job completed without validated result manifest; inspect existing media explicitly")
            doc.update(status="done" if result.returncode == 0 else "failed", exitCode=result.returncode)
            if result.returncode:
                doc.update(error="B-Roll CLI failed with exit code %s" % result.returncode,
                           log_path=str(directory / "output.log"))
        except Exception as error:
            doc.update(status="failed", error=str(error), exitCode=1)
        doc["finishedAt"] = time.time()
        atomic_json(path, doc)
        return doc.get("exitCode", 1)




def submit_marker(workdir, provider, request_id, brief, send):
    """Exactly-once dispatch per explicit browser request ID, with provider frozen."""
    provider_name(provider)
    request_id = str(request_id or uuid.uuid4())
    key = hashlib.sha256((provider + '\0' + request_id).encode()).hexdigest()
    directory = Path(workdir).resolve() / 'broll_requests'
    directory.mkdir(exist_ok=True)
    path = directory / ('req-' + key + '.json')
    with file_lock(directory / '.request.lock'):
        if path.exists():
            doc = read_json(path)
            return str(path), doc.get('terminal') is True, True
        doc = dict(brief, provider=provider, requestId=request_id, terminal=None)
        atomic_json(path, doc)
        sent = bool(send(str(path)))
        doc['terminal'] = sent
        atomic_json(path, doc)
        return str(path), sent, False


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--run":
        raise SystemExit("Usage: cutter_broll_jobs.py --run JOB_DIRECTORY")
    raise SystemExit(execute(sys.argv[2]))
