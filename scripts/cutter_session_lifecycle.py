"""Session-Lifecycle des Cut-Cockpits (18.09.2026).

Projekt zu  -> Claude-Session (tmux cutter-*) wird beendet.
Projekt auf -> Session wird ueber claude_session.json resumed (TerminalManager.start).

"Projekt offen" heisst: die Cockpit-Seite meldet sich. Entweder haengt der
SSE-Terminal-Stream, oder einer der Poll-Endpunkte (/api/term/state alle 3 s,
/api/agent_status alle 10 s, /api/broll_session alle 4 s) wurde in den letzten
IDLE_SECONDS getroffen. Bleibt beides aus, ist die Seite zu.

Nie beendet wird eine Session, die sichtbar arbeitet (TerminalManager.working):
Hook-Status "arbeitet" oder frische tmux-Aktivitaet. Sie wird beim naechsten
Durchlauf geholt, sobald sie fertig ist.

Player, Render und Overrides bleiben unberuehrt. Hier geht es nur um die
CLI-Sessions (Haupt-Cutter + B-Roll-Pickups). Nur Stdlib.
"""
from __future__ import annotations
import os
import threading
import time

from cutter_provider import PROVIDERS

IDLE_SECONDS = int(os.environ.get("CUTTER_SESSION_IDLE_SECONDS", "300"))
TICK_SECONDS = int(os.environ.get("CUTTER_SESSION_TICK_SECONDS", "30"))
# tmux-Aktivitaet juenger als das = Session arbeitet (Claude-Spinner schreibt laufend)
ACTIVITY_SECONDS = int(os.environ.get("CUTTER_SESSION_ACTIVITY_SECONDS", "120"))


class Lifecycle:
    def __init__(self, managers, idle_seconds=None, log=None, clock=time.time, activity_seconds=None):
        self.managers = managers            # callable -> [(label, TerminalManager)]
        self.idle_seconds = IDLE_SECONDS if idle_seconds is None else idle_seconds
        self.activity_seconds = ACTIVITY_SECONDS if activity_seconds is None else activity_seconds
        self.log = log or (lambda text: None)
        self.clock = clock
        self.lock = threading.Lock()
        self.last_seen = clock()

    def touch(self):
        with self.lock:
            self.last_seen = self.clock()

    def viewers(self):
        return sum(manager.viewers() for _, manager in self.managers())

    def project_open(self, now=None):
        now = self.clock() if now is None else now
        if self.viewers():
            return True
        with self.lock:
            return now - self.last_seen < self.idle_seconds

    def sweep(self, reason="idle", force=False):
        """Beendet alle nicht arbeitenden Sessions, wenn das Projekt zu ist.

        force=True (Server-Ende) ignoriert das Idle-Fenster, nie den Arbeits-Guard.
        Gibt die Liste der beendeten Sessions ("label/provider") zurueck.
        """
        now = self.clock()
        if not force and self.project_open(now):
            return []
        ended = []
        for label, manager in self.managers():
            for provider in PROVIDERS:
                exists, _, _ = manager.tmux_info(provider)
                if not exists:
                    continue
                if manager.working(provider, now, activity_window=self.activity_seconds):
                    self.log("[cockpit] Session %s/%s arbeitet noch, bleibt (%s)" % (label, provider, reason))
                    continue
                if manager.kill_session(provider, reason):
                    ended.append(label + "/" + provider)
                    self.log("[cockpit] Session %s/%s beendet (%s), Resume beim naechsten Oeffnen"
                             % (label, provider, reason))
        return ended

    def run_forever(self, tick=None):
        tick = TICK_SECONDS if tick is None else tick
        while True:
            time.sleep(tick)
            try:
                self.sweep()
            except Exception as error:  # noqa: BLE001 — der Watchdog darf nie sterben
                self.log("[cockpit] Session-Watchdog Fehler: %r" % (error,))
