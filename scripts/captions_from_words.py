#!/usr/bin/env python3
"""CapCut-artige Caption-Generierung fuer den SF-Modus (01.09.2026).

Baut captions_sf.json DETERMINISTISCH aus words_aai.json + dem aktuellen
Schnitt (timeline_clips inkl. Echo-Clamp-Achse) — vorher entstanden die
Captions ad-hoc pro Session und drifteten nach jedem Schnitt.

CapCut-Prinzipien (Recherche 01.09.):
- Wort-Highlight braucht ABSOLUTE Wort-Timestamps — gleichmaessiges Verteilen
  oder Umskalieren auf das Seitenfenster "spuert der Zuschauer sofort".
- Kurze Seiten (1-4 Woerter), Bruch an Sprechpausen + Satzenden.
- Seite erscheint am Wort-Onset, haelt kurz nach dem letzten Wort, kettet
  nahtlos an die naechste Seite, wenn die Luecke klein ist.

Praezisions-Anker dieses Systems (besser als CapCut):
- Seiten-Onsets werden an die WAVEFORM-gemessenen Sprech-Onsets der
  pause_map geklemmt (ASR-Zeiten luegen an Naehten, Gesetz pausen_immer_messen)
  — aber nur bis SNAP_MAX, nie blind (Gesetz 10: ASR-Einzelwort-Misstrauen).
- Die Zeitachse ist EXAKT die des Players/Renderers (rerender.apply_jcut_edges
  inkl. Echo-Clamp) — Captions sitzen auch nach L-Cuts richtig.

Schreibt words[] mit ABSOLUTEN t0/t1 auf der Cut-Achse; Player und
rebuild_sf haben dafuer einen Fastpath (kein Rescale mehr).

Usage: captions_from_words.py <workdir> [--dry]
Bestehende captions_sf.json wird nach backups/ gesichert (User-Edits!).
"""
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rerender as rr  # noqa: E402  (Achse + pause_map — EINE Implementierung)

# --- CapCut-Parameter -------------------------------------------------------
WORDS_MAX = 4        # Woerter pro Seite
CHARS_MAX = 20       # Zeichen pro Seite (grosse Reel-Typo)
GAP_BREAK = 0.30     # gemessene Pause > x s bricht die Seite
LEAD = 0.06          # Seite erscheint kurz VOR dem ersten Wort
HOLD = 0.12          # Seite haelt kurz NACH dem letzten Wort
CHAIN = 0.35         # Luecke < x s -> Seiten ketten nahtlos
SNAP_MAX = 0.25      # Onset-Snap an die Waveform, max. Korrektur
SENT_END = (".", "!", "?", "…")
LEAD_ALL = 0.10      # ALLE Zeiten so viel frueher (Sebastian: "einen Ticken frueher") — sf_layout caption.lead_s ueberschreibt


def load(p, d=None):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return d


def main():
    wd = os.path.abspath(sys.argv[1])
    dry = "--dry" in sys.argv
    # Gesetz "Cut-Text schlaegt Quell-Text": --words zeigt auf eine Wortdatei,
    # die aus der Re-Transkription des SCHNITTS auf die Quellachse zurueck-
    # gerechnet wurde. Ohne die Option bleibt es beim Quell-ASR (Alt-Verhalten).
    wsrc = "words_aai.json"
    if "--words" in sys.argv:
        wsrc = sys.argv[sys.argv.index("--words") + 1]
    words = load(os.path.join(wd, wsrc), [])
    print(f"[captions] Wortquelle: {wsrc}")
    if isinstance(words, dict):
        words = words.get("words", [])
    ovr = load(os.path.join(wd, "cockpit_overrides.json"), {}) or {}
    clips = [c for c in (ovr.get("timeline_clips") or [])
             if float(c.get("out", 0)) - float(c.get("in", 0)) >= 0.04]
    if not clips or not words:
        sys.exit("[captions] timeline_clips oder words_aai.json fehlen")

    # Achse EXAKT wie Player/Renderer (inkl. Echo-Clamp)
    segs = [{"in": float(c["in"]), "out": float(c["out"]),
             "jcut": max(0.0, float(c.get("jcut", 0) or 0))} for c in clips]
    segs[0]["jcut"] = 0.0
    pauses = rr.load_pause_map(wd)
    rr.apply_jcut_edges(segs, pauses=pauses)

    # Wort-Achse: words_aai liegt auf der WAV-Achse; pause_map.json weiss, ob
    # die WAV praesentations-aligniert ist (sonst audio_start addieren).
    pm = load(os.path.join(wd, "pause_map.json"), {}) or {}
    w_off = 0.0
    if pm.get("aligned") is False and isinstance(pm.get("audio_start"), (int, float)):
        w_off = float(pm["audio_start"])

    def speech_onset_near(t_src):
        """Waveform-Onset nahe t_src (File-Achse) oder None."""
        if not pauses:
            return None
        import bisect
        starts = [p[0] for p in pauses]
        i = bisect.bisect_right(starts, t_src) - 1
        cand = []
        if i >= 0:
            cand.append(pauses[i][1])      # Ende der Pause davor = Onset
        if i + 1 < len(pauses):
            cand.append(pauses[i + 1][1])
        best = min(cand, key=lambda x: abs(x - t_src)) if cand else None
        return best if best is not None and abs(best - t_src) <= SNAP_MAX else None

    # Woerter in die Cut-Timeline mappen (AUDIO-Achse: a_start + (w - in))
    out_words = []
    for s in segs:
        a0, i0, i1 = s["a_start"], s["in"], s["out"]
        for w in words:
            ws, we = float(w["start"]) + w_off, float(w["end"]) + w_off
            mid = (ws + we) / 2.0
            if i0 <= mid < i1:
                out_words.append({
                    "w": str(w.get("text") or w.get("w") or "").strip(),
                    "t0": round(a0 + max(0.0, ws - i0), 3),
                    "t1": round(a0 + min(i1 - i0, we - i0), 3),
                    "src": ws,
                })
    out_words = [w for w in out_words if w["w"]]
    out_words.sort(key=lambda w: w["t0"])
    if not out_words:
        sys.exit("[captions] keine Woerter im Schnitt gefunden")

    # Seiten bilden (CapCut-Segmentierung)
    pages, cur = [], []
    for k, w in enumerate(out_words):
        if cur:
            gap = w["t0"] - cur[-1]["t1"]
            chars = sum(len(x["w"]) + 1 for x in cur) + len(w["w"])
            prev_txt = cur[-1]["w"]
            if (gap > GAP_BREAK or len(cur) >= WORDS_MAX or chars > CHARS_MAX
                    or prev_txt.endswith(SENT_END)):
                pages.append(cur)
                cur = []
        cur.append(w)
    if cur:
        pages.append(cur)

    # Fenster + Onset-Snap + Ketten
    caps = []
    for pg in pages:
        t0 = pg[0]["t0"]
        snap = speech_onset_near(pg[0]["src"])
        if snap is not None:
            # Onset auf die Cut-Achse uebersetzen: gleiche Verschiebung wie das Wort
            t0 = pg[0]["t0"] + (snap - pg[0]["src"])
            pg[0]["t0"] = round(min(t0, pg[0]["t1"] - 0.02), 3)
        start = max(0.0, t0 - LEAD)
        end = pg[-1]["t1"] + HOLD
        caps.append({
            "start": round(start, 3), "end": round(end, 3),
            "text": " ".join(x["w"] for x in pg),
            "words": [{"w": x["w"], "t0": x["t0"], "t1": x["t1"]} for x in pg],
        })
    for k in range(len(caps) - 1):
        gap = caps[k + 1]["start"] - caps[k]["end"]
        if 0 < gap < CHAIN:
            caps[k]["end"] = caps[k + 1]["start"]           # nahtlose Kette
        elif gap < 0:
            caps[k]["end"] = caps[k + 1]["start"]           # nie ueberlappen

    # VORLAUF (01.09.): Highlight soll am WORT-ANFANG sitzen, nicht auf dem
    # Kern. ASR-Startzeiten haengen systematisch etwas hinterher -> alles um
    # LEAD_ALL nach vorn (Seiten + Woerter), konfigurierbar per sf_layout.
    lead = float(((load(os.path.join(wd, "sf_layout.json"), {}) or {}).get("caption") or {}).get("lead_s", LEAD_ALL))
    for c in caps:
        c["start"] = round(max(0.0, c["start"] - lead), 3)
        c["end"] = round(max(c["start"] + 0.1, c["end"] - lead), 3)
        for w in c["words"]:
            w["t0"] = round(max(0.0, w["t0"] - lead), 3)
            w["t1"] = round(max(w["t0"] + 0.03, w["t1"] - lead), 3)

    # Stil-Felder (size/x/y) der bisherigen Captions als Default uebernehmen
    old = load(os.path.join(wd, "captions_sf.json"), []) or []
    style = {}
    for o in old:
        for f in ("size", "x", "y"):
            if o.get(f) is not None and f not in style:
                style[f] = o[f]
    for c in caps:
        c.update(style)

    print(f"[captions] {len(out_words)} Woerter -> {len(caps)} Seiten "
          f"(Median {sorted(len(p['words']) for p in caps)[len(caps)//2]} W/Seite, "
          f"pause_map={'ja' if pauses else 'NEIN — kein Onset-Snap!'})")
    if dry:
        for c in caps[:8]:
            print(f"   {c['start']:7.2f}-{c['end']:7.2f}  {c['text']}")
        return

    dst = os.path.join(wd, "captions_sf.json")
    if os.path.exists(dst):
        bdir = os.path.join(wd, "backups")
        os.makedirs(bdir, exist_ok=True)
        bak = os.path.join(bdir, "captions_sf.%s.json" % time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(dst, bak)
        print(f"[captions] Backup: {bak}")
    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(caps, f, ensure_ascii=False, indent=1)
    os.replace(tmp, dst)
    print(f"[captions] geschrieben: {dst}")


if __name__ == "__main__":
    main()
