"""Plattform-Hilfen (Mac/Linux/Windows) an EINER Stelle.

Erweiterung von claudereelmagic (Copyright (c) 2026 Bianka Steinberger).
Die Basis (claude-video-cutter, MIT, Sebastian Kauffmann / SKAILE) nutzte an
vielen Stellen fcntl/pty/termios, ps, open, SIGKILL und ':' als PATH-Trenner.
Das gibt es nur auf Mac/Linux. Dieses Modul kapselt die Unterschiede, damit
alle Skripte auch unter Windows importierbar sind.

Wichtig: Entscheidung per ImportError statt per os.name, damit die Logik
auch ohne Windows-Rechner pruefbar ist (Module sperren -> Windows-Pfad).
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

try:  # Mac/Linux
    import fcntl  # type: ignore
except ImportError:  # Windows
    fcntl = None

try:
    import msvcrt  # type: ignore  # nur Windows
except ImportError:
    msvcrt = None

IS_WINDOWS = fcntl is None
# Mac/Linux-Terminal (PTY) vorhanden? Auf Windows nein.
try:
    import pty  # noqa: F401
    import termios  # noqa: F401
    HAS_PTY = fcntl is not None
except ImportError:
    HAS_PTY = False

# SIGKILL gibt es auf Windows nicht; SIGTERM ist dort bereits hart.
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)
# SIGHUP gibt es auf Windows nicht.
SIGHUP = getattr(signal, "SIGHUP", None)


def add_to_path(directory: str) -> None:
    """Verzeichnis vorn in PATH eintragen, mit dem richtigen Trenner.

    Die Basis nutzte fest ':' - unter Windows (Trenner ';') wuerde das den
    PATH beschaedigen.
    """
    if not os.path.isdir(directory):
        return
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if directory not in parts:
        os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")


@contextmanager
def file_lock(path, blocking: bool = True):
    """Exklusive Dateisperre als Context-Manager (liefert den offenen Stream).

    blocking=False wirft BlockingIOError, wenn die Sperre schon vergeben ist
    (gleiches Verhalten wie fcntl.LOCK_NB).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            try:
                yield stream
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        else:
            fd = stream.fileno()
            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if not blocking:
                        raise BlockingIOError("Sperre belegt: %s" % path)
                    time.sleep(0.05)
            try:
                yield stream
            finally:
                try:
                    stream.seek(0)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass


def reveal_in_file_manager(path: str) -> bool:
    """Datei im Datei-Manager markieren (Finder / Explorer / Linux)."""
    try:
        if sys.platform == "darwin":
            return subprocess.run(["open", "-R", path], capture_output=True).returncode == 0
        if IS_WINDOWS:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return True  # explorer liefert auch bei Erfolg oft Exit-Code 1
        subprocess.run(["xdg-open", os.path.dirname(path)], capture_output=True)
        return True
    except OSError:
        return False


def open_file(path: str) -> bool:
    """Datei mit dem Standardprogramm oeffnen."""
    try:
        if sys.platform == "darwin":
            return subprocess.run(["open", path], capture_output=True).returncode == 0
        if IS_WINDOWS:
            os.startfile(path)  # type: ignore[attr-defined]
            return True
        return subprocess.run(["xdg-open", path], capture_output=True).returncode == 0
    except OSError:
        return False
