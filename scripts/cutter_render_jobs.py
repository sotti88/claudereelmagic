"""Detached shortform render jobs and completed, content-addressed artifacts.

No media preflight: metadata identities only invalidate caches. ffmpeg remains
responsible for reading inputs. Existing project media are never removed.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
MONTHS = ("", "Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember")
ACTIVE = {"queued", "running"}


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {} if default is None else default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def file_lock(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield stream
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def file_identity(path):
    """Cheap dependency identity, not a media integrity/availability check."""
    path = Path(path).expanduser().resolve()
    try:
        stat = path.stat()
        return {"path": str(path), "device": stat.st_dev, "inode": stat.st_ino,
                "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                "ctime_ns": stat.st_ctime_ns}
    except OSError:
        return {"path": str(path), "unavailable": True}


def renderer_identity():
    result = {}
    for name in ("rebuild_sf.py", "rerender.py", "caption_style.py", "sf_project.py", "cutter_render_jobs.py"):
        result[name] = hashlib.sha256((HERE / name).read_bytes()).hexdigest()
    return result


def storage_root(workdir):
    project = digest(str(Path(workdir).resolve()))[:16]
    base = Path(os.environ.get("CUTTER_RENDER_STORAGE") or
                Path.home() / "Movies" / "_Sicherungen" / "Cutting").expanduser()
    return base / project


def output_directory(workdir):
    if os.environ.get("CUTTER_EXPORT_DIR"):
        return Path(os.environ["CUTTER_EXPORT_DIR"]).expanduser().resolve() / Path(workdir).resolve().name
    now = time.localtime()
    month = f"{now.tm_year}-{now.tm_mon:02d}-{MONTHS[now.tm_mon]}"
    return Path.home() / "Movies" / "Fertig" / month / Path(workdir).resolve().name


def _root(workdir):
    return Path(workdir).resolve() / "render_jobs"


def _worker_locked(root):
    try:
        with file_lock(root / ".run.lock", blocking=False):
            return False
    except BlockingIOError:
        return True


def _status(document, root):
    if not document:
        return {"status": "idle", "export_da": False}
    result = dict(document)
    if result.get("status") in ACTIVE and not _worker_locked(root):
        # Allow the detached child to acquire its lifetime lock after launch.
        if result["status"] == "running" or time.time() - result["createdAt"] > 15:
            result.update(status="interrupted", finishedAt=time.time(), exit=None,
                          error="Renderprozess wurde unterbrochen")
            atomic_json(root / result["id"] / "job.json", result)
    result["export"] = result.get("output")
    result["export_da"] = result.get("status") == "done" and bool(result.get("output"))
    if result["export_da"]:
        try:
            result["export_mb"] = round(Path(result["output"]).stat().st_size / 1048576, 1)
        except OSError:
            result["export_da"] = False
    try:
        with (root / result["id"] / "output.log").open(encoding="utf-8") as log:
            result["log"] = "".join(deque(log, maxlen=8))
    except OSError:
        result["log"] = ""
    return result


def read_status(workdir, job_id=None):
    root = _root(workdir)
    include_latest = job_id is None
    if job_id is None:
        job_id = read_json(root / "index.json").get("active")
    if not job_id or not re.fullmatch(r"[0-9a-f]{32}", str(job_id)):
        return {"status": "idle", "export_da": False}
    result = _status(read_json(root / job_id / "job.json"), root)
    if include_latest:
        previous = read_json(root / "latest.json").get("export") or {}
        latest_id = previous.get("id")
        if latest_id and latest_id != job_id:
            completed = _status(read_json(root / latest_id / "job.json"), root)
            if completed.get("export_da"):
                result["last_success"] = completed
    return result


def latest_output(workdir, mode="export"):
    doc = read_json(_root(workdir) / "latest.json").get(mode) or {}
    return doc.get("output")


def enqueue(workdir, src, mode="export", snapshot=None):
    if mode not in ("export", "proxy"):
        raise ValueError("Unknown render mode: " + str(mode))
    workdir = Path(workdir).resolve()
    root = _root(workdir)
    with file_lock(root / ".start.lock"):
        current = read_status(workdir)
        if current["status"] in ACTIVE:
            return dict(current, duplicate=True)
        if snapshot is None:
            from sf_project import load_settings
            snapshot = load_settings(workdir)
        # JSON round-trip freezes caller-owned dictionaries before launch.
        snapshot = json.loads(json.dumps(snapshot))
        job_id = uuid.uuid4().hex
        directory = root / job_id
        directory.mkdir(parents=True)
        index = read_json(root / "index.json")
        version = int(index.get("nextVersion", 1))
        fps = 60 if mode == "export" else 30
        name = workdir.name
        target_dir = output_directory(workdir) if mode == "export" else storage_root(workdir) / "previews"
        target = target_dir / f"{name}_v{version:03d}_1080p{fps}.mp4"
        document = {
            "version": 1, "id": job_id, "mode": mode, "number": version,
            "workdir": str(workdir), "source": str(Path(src).expanduser().resolve()),
            "sourceIdentity": file_identity(src), "snapshotHash": digest(snapshot),
            "revision": snapshot.get("revision"), "status": "queued", "stage": "queued",
            "completed": 0, "total": 0, "createdAt": time.time(), "startedAt": None,
            "finishedAt": None, "output": str(target), "exit": None,
            "fps": fps, "artifacts": [], "cacheHits": 0, "rendered": 0,
        }
        atomic_json(directory / "snapshot.json", snapshot)
        atomic_json(directory / "job.json", document)
        atomic_json(root / "index.json", {"active": job_id, "nextVersion": version + 1})
        try:
            with (directory / "launcher.log").open("ab") as output:
                child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--run", str(directory)],
                                         stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                         cwd=str(HERE.parent), start_new_session=True, close_fds=True)
            # Reap while this server is alive, without making job ownership
            # depend on the server or retaining a Popen object in API state.
            threading.Thread(target=child.wait, name="render-reaper-" + job_id[:8], daemon=True).start()
        except Exception as error:
            document.update(status="failed", error=str(error), exit=1, finishedAt=time.time())
            atomic_json(directory / "job.json", document)
            raise
        return dict(_status(document, root), duplicate=False)


class JobRuntime:
    def __init__(self, workdir, directory=None):
        self.workdir = Path(workdir).resolve()
        self.directory = Path(directory).resolve() if directory else None
        self.path = self.directory / "job.json" if self.directory else None
        self.document = read_json(self.path) if self.path else {}
        self.identity = renderer_identity()
        self.cache = ArtifactCache(self.workdir, self)

    def update(self, **values):
        self.document.update(values)
        self.document["updatedAt"] = time.time()
        if self.path:
            atomic_json(self.path, self.document)

    def stage(self, name, completed=0, total=0):
        self.update(stage=name, completed=completed, total=total, framesDone=0, framesTotal=0)

    def artifact(self, stage, key, path, reused):
        self.document.setdefault("artifacts", []).append(
            {"stage": stage, "key": key, "path": str(path), "reused": reused})
        field = "cacheHits" if reused else "rendered"
        self.update(**{field: self.document.get(field, 0) + 1})
        print(f"[render-cache] {'HIT' if reused else 'BUILD'} {stage} {key[:12]}", flush=True)


class ArtifactCache:
    def __init__(self, workdir, runtime):
        self.runtime = runtime
        self.data = storage_root(workdir) / "artifacts"
        self.metadata = _root(workdir) / "cache"

    def render(self, stage, parameters, cmd, *, frames=0, on_built=None):
        key = digest({"schema": 1, "renderer": self.runtime.identity, "stage": stage,
                      "parameters": parameters})
        suffix = Path(cmd[-1]).suffix
        target = self.data / stage / (key + suffix)
        meta = self.metadata / stage / (key + ".json")
        completed = read_json(meta)
        if completed.get("key") == key and completed.get("identity") == file_identity(target):
            self.runtime.artifact(stage, key, target, True)
            return str(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(key + ".part-" + uuid.uuid4().hex + suffix)
        command = list(cmd[:-1]) + [str(temporary)]
        run_ffmpeg(command, stage, self.runtime, frames)
        if on_built:
            on_built(str(temporary))
        os.replace(temporary, target)
        atomic_json(meta, {"key": key, "identity": file_identity(target), "completedAt": time.time()})
        self.runtime.artifact(stage, key, target, False)
        return str(target)


def run_ffmpeg(command, label, runtime=None, frames=0):
    command = list(command)
    if Path(command[0]).name == "ffmpeg":
        command[1:1] = ["-nostats", "-progress", "pipe:1", "-stats_period", "1"]
    tail = deque(maxlen=30)
    progress = {}
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    try:
        for line in proc.stdout:
            key, separator, value = line.strip().partition("=")
            if separator and key in {"frame", "fps", "speed", "out_time_us", "out_time_ms",
                                    "out_time", "progress", "total_size", "bitrate", "dup_frames",
                                    "drop_frames", "stream_0_0_q"}:
                progress[key] = value
                if key == "progress" and runtime:
                    runtime.update(framesDone=int(progress.get("frame", 0)), framesTotal=frames,
                                   renderFps=progress.get("fps"), speed=progress.get("speed"))
            else:
                tail.append(line)
                print(line, end="", flush=True)
        code = proc.wait()
    except BaseException:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise
    if code:
        raise RuntimeError(f"{label} fehlgeschlagen (exit {code}): " + "".join(tail)[-2000:])
    return subprocess.CompletedProcess(command, code, stdout="", stderr="")


def execute(directory):
    directory = Path(directory).resolve()
    document = read_json(directory / "job.json")
    root = directory.parent
    with file_lock(root / ".run.lock"):
        if document.get("status") != "queued":
            return 0
        document.update(status="running", stage="starting", startedAt=time.time(), pid=os.getpid(),
                        workerToken=uuid.uuid4().hex)
        atomic_json(directory / "job.json", document)
        cmd = [sys.executable, str(HERE / "rebuild_sf.py"), document["workdir"],
               document["source"], "--job", str(directory), "--out", document["output"]]
        if document["mode"] == "export":
            cmd.append("--export")
        child = None

        def interrupted(signum, frame):
            raise InterruptedError("Renderprozess wurde unterbrochen")

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        try:
            with (directory / "output.log").open("ab") as output:
                child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=output,
                                         stderr=subprocess.STDOUT)
                code = child.wait()
            document = read_json(directory / "job.json")
            document.update(status="done" if code == 0 else "failed", exit=code,
                            finishedAt=time.time())
            if code == 0:
                document.update(stage="done", completed=document.get("total", 0))
                latest = read_json(root / "latest.json")
                latest[document["mode"]] = {"id": document["id"], "output": document["output"],
                                            "revision": document.get("revision"),
                                            "finishedAt": document["finishedAt"]}
                atomic_json(root / "latest.json", latest)
            else:
                document["error"] = "Render fehlgeschlagen. Details im Auftragslog."
        except BaseException as error:
            if child and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
            document = read_json(directory / "job.json")
            document.update(status="interrupted" if isinstance(error, (InterruptedError, KeyboardInterrupt)) else "failed",
                            exit=None, error=str(error), finishedAt=time.time())
        atomic_json(directory / "job.json", document)
        return int(document.get("exit") or (0 if document["status"] == "done" else 1))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--run":
        raise SystemExit("Usage: cutter_render_jobs.py --run JOB_DIRECTORY")
    raise SystemExit(execute(sys.argv[2]))
