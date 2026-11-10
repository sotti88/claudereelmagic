#!/usr/bin/env python3
"""Shortform-Render-Hook fuers Cut-Cockpit (Fork): baut proxy.mp4 komplett neu
aus den Cockpit-Daten des Workdirs.

Quellen im Workdir:
  cockpit_overrides.json  timeline_clips [{id,in,out,gain}] = Schnitt (Quellzeiten)
                          broll [{start,end,file,src_in,speed?,...}] = Karten (Cut-Timeline)
  captions_sf.json        [{start,end,text,size?,words?:[{t0,t1,w}]}]
  sf_layout.json          Layout-Konstanten (Zonen, Karten-Geometrie, Hook-Assets,
                          Titel, Pill, Caption-Stil)

Kette: Schnitt -> Karten-Zwischenclips (Tempo-Fit via setpts) -> Caption-Layer
(PIL, Wort-Karaoke mit Pink-Box) -> Ein-Pass-Composite -> proxy.mp4 (atomar).
Usage: rebuild_sf.py <workdir> <src_video>
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
import uuid
import signal

from cutter_render_jobs import JobRuntime, file_identity, storage_root, run_ffmpeg
from sf_audio import timing_plan, prepare_audio
from sf_project import text_bitmap

import numpy as np  # noqa: F401  (Haltung: gleiche venv wie Pipeline)
from PIL import Image, ImageDraw, ImageFont

_T0 = time.time()   # Renderdauer, damit Sebastian sie schwarz auf weiss hat

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rerender as _rr  # noqa: E402  (Look-Tabelle — EINE Implementierung)

WORKDIR = os.path.abspath(sys.argv[1])
SRC = os.path.abspath(sys.argv[2])
# EXPORT-MODUS (04.09.): derselbe Renderweg, nur mit 60 fps und einem eigenen
# Ziel statt proxy.mp4. Sebastians Rohmaterial ist 60p — der Talking Head
# bekommt damit echte 60 Bilder, die B-Roll-Karten (HyperFrames, 30p) werden
# verdoppelt. Ohne die Schalter bleibt alles exakt wie vorher.
ARGS = sys.argv[3:]
EXPORT = "--export" in ARGS
FPS = 60 if EXPORT else 30
if "--fps" in ARGS:
    FPS = int(ARGS[ARGS.index("--fps") + 1])
ZIEL = None
if "--out" in ARGS:
    ZIEL = os.path.abspath(ARGS[ARGS.index("--out") + 1])
W, H = 1080, 1920
JOB_DIR = Path(ARGS[ARGS.index("--job") + 1]).resolve() if "--job" in ARGS else None
SNAPSHOT_PATH = (JOB_DIR / "snapshot.json") if JOB_DIR else (
    Path(ARGS[ARGS.index("--snapshot") + 1]).resolve() if "--snapshot" in ARGS else None)
if SNAPSHOT_PATH:
    SETTINGS = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
else:
    from sf_project import load_settings
    SETTINGS = load_settings(WORKDIR)
RUNTIME = JobRuntime(WORKDIR, JOB_DIR)
CACHE = RUNTIME.cache
SOURCE_ID = file_identity(SRC)


def stop_render(signum, frame):
    raise InterruptedError("Renderprozess wurde unterbrochen")


signal.signal(signal.SIGTERM, stop_render)


def load(name, default):
    p = os.path.join(WORKDIR, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def run(cmd, label):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write((r.stderr or "")[-3000:])
        raise SystemExit(f"[rebuild_sf] {label} fehlgeschlagen (rc={r.returncode})")
    return r


def probe_dur(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path], f"probe {path}")
    return float(r.stdout.strip())


def is_still_image(path):
    """Einzelbilder brauchen einen geloopten Eingang, keine Video-Suche."""
    try:
        with Image.open(path) as img:
            return getattr(img, "n_frames", 1) == 1
    except (OSError, ValueError):
        return False


def require_video(path, label):
    # ffmpeg kann bei einem leeren Bild-Eingang erfolgreich eine MP4 ohne
    # Streams schreiben. Den konkreten Slot nennen statt erst im Composite
    # mit einem unlesbaren Filtergraph-Fehler abzubrechen.
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of", "json", path], label)
    if not any(s.get("codec_type") == "video" for s in json.loads(r.stdout).get("streams", [])):
        raise SystemExit(f"[rebuild_sf] {label}: kein Video-Stream in {path}")


ovr = SETTINGS.get("overrides") or {}
clips = ovr.get("timeline_clips") or []
slots = [dict(b) for b in (ovr.get("broll") or [])
         if b.get("file") and ((b.get("end", 0) > b.get("start", 0))
                               or b.get("clip_id") is not None
                               or b.get("clip_idx") is not None)]
caps = SETTINGS.get("captions") or []
lay = SETTINGS.get("layout") or {}
if not clips:
    raise SystemExit("[rebuild_sf] keine timeline_clips in cockpit_overrides.json")
print(f"[rebuild_sf] {len(clips)} Schnitt-Clips, {len(slots)} B-Roll-Slots, {len(caps)} Text-Clips", flush=True)

# Grosse Zwischenprodukte bleiben unter Movies. Pro Lauf eigener Scratch-Pfad;
# nur abgeschlossene Artefakte im separaten Cache werden wiederverwendet.
run_id = JOB_DIR.name if JOB_DIR else uuid.uuid4().hex
tmpdir = str(storage_root(WORKDIR) / "runs" / run_id)
os.makedirs(tmpdir, exist_ok=True)
plan = timing_plan(clips, FPS, _rr.load_pause_map(WORKDIR))
if len(plan) != len(clips):
    raise SystemExit("[rebuild_sf] Ungueltige Schnitt-Clip-Dauer")
total_frames = plan[-1]["end_frame"]
dur_total = total_frames / FPS
seg_bounds = [(c["cut_start"], c["cut_end"]) for c in plan]

# --- 1) Bildschnitt: vom Audiopfad getrennt gecacht --------------------------
seg_files = []
concat_list = os.path.join(tmpdir, "concat.txt")
with open(concat_list, "w") as lf:
    for i, item in enumerate(plan):
        RUNTIME.stage("segments", i, len(plan))
        c = item["clip"]
        a, d, frames = item["source_in"], item["source_duration"], item["frames"]
        vf = (_rr.build_geo_filter(c, W, H) + f",fps={FPS},"
              f"tpad=stop_mode=clone:stop_duration=1,trim=end_frame={frames},"
              "setpts=PTS-STARTPTS" + _rr.build_look_filter(c))
        out = CACHE.render("video", {"source": SOURCE_ID, "in": a, "duration": d,
                                    "frames": frames, "fps": FPS, "filter": vf},
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{a:.8f}", "-t", f"{d:.8f}", "-i", SRC,
             "-vf", vf, "-an", "-c:v", "libx264", "-crf", "17", "-preset", "fast",
             "-threads", "8", "-pix_fmt", "yuv420p", "-r", str(FPS), "-fps_mode", "cfr",
             os.path.join(tmpdir, f"seg{i:02d}.mp4")], frames=frames)
        lf.write(f"file '{out}'\n")
        seg_files.append(out)
        print(f"[rebuild_sf] seg{i:02d}: {a:.2f}+{d:.2f} -> {frames} Frames", flush=True)
RUNTIME.stage("segments", len(plan), len(plan))
cut = CACHE.render("video_concat", {"segments": seg_files, "fps": FPS},
    ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", concat_list,
     "-an", "-c", "copy", os.path.join(tmpdir, "cut_video.mp4")], frames=total_frames)

# PCM, Clip-Gains und Gesamt-Gain kommen aus derselben Berechnung wie die UI.
RUNTIME.stage("audio", 0, 1)
audio = prepare_audio(WORKDIR, SRC, clips, lay.get("audio"), ovr.get("audio"), FPS,
                      storage_dir=storage_root(WORKDIR) / "audio")
audio_encoded = CACHE.render("audio_encode", {"audio": audio["cache_key"], "fps": FPS,
                                             "samples": audio["samples"]},
    ["ffmpeg", "-y", "-loglevel", "error", "-i", audio["processed_path"],
     "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{audio['duration']:.8f}",
     os.path.join(tmpdir, "audio.m4a")])
RUNTIME.stage("audio", 1, 1)
print(f"[rebuild_sf] Schnitt: {dur_total:.2f}s, konstanter Ton-Gain {audio['gain_db']:.2f} dB", flush=True)

# --- clip-gebundene Slots (03.09.) -----------------------------------------
# Ein B-Roll gehoert zu genau einem Clip und fuellt ihn exakt aus. Die Grenzen
# kommen deshalb IMMER aus dem geschnittenen Clip (seg_bounds = gemessene
# Dauern nach dem Schnitt), nie aus gespeicherten Zeiten. Korrigiert Sebastian
# den Schnitt nach, passt speed:"fit" das Tempo automatisch an die neue Laenge
# an — ohne das B-Roll neu zu bauen.
_by_id = {c.get("id"): i for i, c in enumerate(clips) if c.get("id") is not None}
slots = [b for b in slots if b.get("clip_id") is None or b["clip_id"] in _by_id]
_gebunden = 0
for b in slots:
    ci = _by_id.get(b.get("clip_id"))
    if ci is None and b.get("clip_id") is None:
        ci = b.get("clip_idx")
    if ci is None or not (0 <= ci < len(seg_bounds)):
        continue
    if b.get("binding") == "source_interval":
        # New production scenes retain their authored duration, including range
        # jobs inside one clip. Match the Cockpit's raw-to-video anchor mapping.
        duration = float(b["end"]) - float(b["start"])
        anchor = float(b.get("anchor", plan[ci]["source_in"]))
        containing = next((p for p in plan if p["source_in"] <= anchor <= p["source_out"]), None)
        if containing:
            start = containing["cut_start"] + anchor - containing["source_in"]
        else:
            following = [p for p in plan if p["source_in"] > anchor]
            start = min(following, key=lambda p: p["source_in"])["cut_start"] if following else dur_total
        b["start"] = round(start + float(b.get("shift", 0)), 3)
        b["end"] = round(b["start"] + duration, 3)
    else:
        b["start"], b["end"] = seg_bounds[ci]
    _gebunden += 1
if _gebunden:
    print(f"[rebuild_sf] {_gebunden} Slot(s) an ihren Clip gebunden", flush=True)

# --- Layout-Zonen ----------------------------------------------------------
body_t0 = seg_bounds[0][1] if len(seg_bounds) > 1 else 0.0
body_t1 = seg_bounds[-1][0] if len(seg_bounds) > 1 else dur_total
video_yoff = int(lay.get("video_yoff", 572))
card = lay.get("card", {"w": 1316, "h": 740, "x": -118, "y": 0})

# --- 2) Karten-Zwischenclips ----------------------------------------------
card_specs = []
for i, b in enumerate(slots):
    RUNTIME.stage("cards", i, len(slots))
    f = b["file"]
    if not os.path.exists(f):
        print(f"[rebuild_sf] WARNUNG: Slot-Datei fehlt, übersprungen: {f}", flush=True)
        continue
    # Auch frei platzierte Bilder folgen dem Ausgabe-Raster. Sonst ragt etwa
    # ein Hook bis 4.957s noch in den ersten Body-Frame bei 4.95s hinein.
    slot_start = max(0, round(float(b["start"]) * FPS))
    slot_end = min(total_frames, round(float(b["end"]) * FPS))
    t0, t1 = slot_start / FPS, slot_end / FPS
    d = t1 - t0
    if d <= 0.05:
        continue
    src_in = max(0.0, float(b.get("src_in") or 0))
    out = os.path.join(tmpdir, f"card{i:02d}.mp4")
    # FREIE GEOMETRIE (03.09.): importierte Medien tragen geo {x,y,w} in
    # normalisierten Design-Koordinaten (0..1, x/y = Mittelpunkt, w = Breite
    # relativ zu 1080). Ohne geo gilt die Karten-Standardlage aus sf_layout.
    geo = b.get("geo") or None
    if geo:
        gw = max(0.05, min(3.0, float(geo.get("w", 0.8))))
        ziel_w = int(round(gw * W / 2) * 2)
        ziel_h = -2                                   # Seitenverhaeltnis behalten
    else:
        ziel_w, ziel_h = card["w"], card["h"]
    still = is_still_image(f)
    if still:
        # -ss (selbst 0) kann bei Einzelbildern den einzigen Frame verwerfen.
        # Framerate und Loop vor -i setzen, damit das Bild den ganzen Slot hat.
        source_args = ["-framerate", str(FPS), "-loop", "1", "-i", f]
        vf = f"scale={ziel_w}:{ziel_h},fps={FPS}"
    elif b.get("speed") == "fit":
        source_args = ["-ss", f"{src_in:.3f}", "-i", f]
        rest = max(0.1, probe_dur(f) - src_in)
        factor = d / rest
        vf = (f"scale={ziel_w}:{ziel_h},setpts=PTS*{factor:.6f},fps={FPS}")
        print(f"[rebuild_sf] card{i:02d}: Tempo-Fit x{1/factor:.2f} ({rest:.2f}s -> {d:.2f}s)", flush=True)
    else:
        source_args = ["-ss", f"{src_in:.3f}", "-i", f]
        vf = f"scale={ziel_w}:{ziel_h},tpad=stop_mode=clone:stop_duration=10,fps={FPS}"
    out = CACHE.render("card", {"source": file_identity(f), "src_in": src_in,
                               "fps": FPS, "frames": slot_end-slot_start,
                               "filter": vf},
        ["ffmpeg", "-y", "-loglevel", "error"] + source_args + [
         "-vf", vf, "-t", f"{d:.8f}", "-frames:v", str(slot_end - slot_start), "-an",
         "-c:v", "libx264", "-crf", "17", "-preset", "fast", "-threads", "8", out],
        frames=slot_end-slot_start, on_built=lambda path: require_video(path, f"card{i}"))
    # MASKE (05.09.): Rechteck wie in CapCut — w/h Anteil des Mediums (symmetrisch
    # um den Masken-Mittelpunkt), x/y Versatz des Mittelpunkts, r Eckenrundung,
    # f weiche Kante. Ohne Rundung/Kante reicht ein crop (kein Alpha noetig);
    # sonst PIL-Alphabild + alphamerge, Ausgabe mit Alphakanal (qtrle/argb).
    # Die Karte ist im Design-Raum 1:1 (ziel_w = gw*1080), r/f sind Karten-Pixel.
    # Player rechnet dieselbe Zeichnung in maskAnwenden()/maskDataUrl().
    mask_off = (0, 0)
    mk = (b.get("mask") or {}) if geo else {}
    mw_f = max(0.05, min(1.0, float(mk.get("w", 1) or 1)))
    mh_f = max(0.05, min(1.0, float(mk.get("h", 1) or 1)))
    mx_f = max(-(1-mw_f)/2, min((1-mw_f)/2, float(mk.get("x", 0) or 0)))
    my_f = max(-(1-mh_f)/2, min((1-mh_f)/2, float(mk.get("y", 0) or 0)))
    m_r = max(0.0, min(400.0, float(mk.get("r", 0) or 0)))
    m_f = max(0.0, min(200.0, float(mk.get("f", 0) or 0)))
    maske_aktiv = (mw_f < 0.999 or mh_f < 0.999 or m_r > 0 or m_f > 0
                   or abs(mx_f) > 0.0005 or abs(my_f) > 0.0005)
    if maske_aktiv:
        _pr = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=width,height", "-of", "csv=p=0", out], f"probe card{i}")
        cw, ch = [int(v) for v in _pr.stdout.strip().split(",")[:2]]
        mw = max(2, int(round(cw * mw_f / 2)) * 2)
        mh = max(2, int(round(ch * mh_f / 2)) * 2)
        cx = cw / 2.0 + mx_f * cw
        cy = ch / 2.0 + my_f * ch
        x0 = int(round(cx - mw / 2.0)); y0 = int(round(cy - mh / 2.0))
        x0 = max(0, min(cw - mw, x0)); y0 = max(0, min(ch - mh, y0))
        # Mittelpunkt der Maske relativ zur Karten-Mitte -> Overlay-Versatz
        mask_off = ((x0 + mw / 2.0) - cw / 2.0, (y0 + mh / 2.0) - ch / 2.0)
        masked = os.path.join(tmpdir, f"card{i:02d}_mask.mov")
        if m_r <= 0 and m_f <= 0:
            masked = CACHE.render("mask_crop", {"card": out, "crop": [mw, mh, x0, y0]},
                ["ffmpeg", "-y", "-loglevel", "error", "-i", out,
                 "-vf", f"crop={mw}:{mh}:{x0}:{y0}",
                 "-c:v", "libx264", "-crf", "17", "-preset", "fast", "-pix_fmt", "yuv420p",
                 masked], frames=slot_end-slot_start)
        else:
            from PIL import ImageFilter
            # Feather must leave a nonnegative inner rectangle, even for 2px masks.
            fe = min(m_f, max(0.0, (min(mw, mh) - 1) / 2.0))
            rr = max(0.0, min(m_r, min(mw, mh) / 2.0) - fe)
            rr = min(rr, max(0.0, (mw - 2 * fe) / 2.0), max(0.0, (mh - 2 * fe) / 2.0))
            im = Image.new("L", (mw, mh), 0)
            dr = ImageDraw.Draw(im)
            box = [fe, fe, mw - fe - 1, mh - fe - 1]
            if rr > 0:
                dr.rounded_rectangle(box, radius=rr, fill=255)
            else:
                dr.rectangle(box, fill=255)
            if fe > 0:
                im = im.filter(ImageFilter.GaussianBlur(fe))
            mpng = os.path.join(tmpdir, f"card{i:02d}_mask.png")
            im.save(mpng)
            # -loop 1 ist ein endloser Eingang; -shortest beendet den Lauf damit
            # NICHT zuverlaessig (im Test aufgehaengt, 06.09.). Deshalb harte
            # Dauer -t = Kartenlaenge, dann ist das Ende eindeutig.
            masked = CACHE.render("mask_alpha", {"card": out, "crop": [mw, mh, x0, y0],
                                                  "radius": rr, "feather": fe, "fps": FPS},
                ["ffmpeg", "-y", "-loglevel", "error", "-i", out,
                 "-framerate", str(FPS), "-loop", "1", "-i", mpng,
                 "-filter_complex",
                 f"[0:v]crop={mw}:{mh}:{x0}:{y0},format=rgba[c];[1:v]format=gray[m];[c][m]alphamerge[o]",
                 "-map", "[o]", "-t", f"{d:.4f}", "-r", str(FPS),
                 "-c:v", "qtrle", "-pix_fmt", "argb", masked],
                frames=slot_end-slot_start)
        out = masked
        print(f"[rebuild_sf] card{i:02d}: Maske {mw}x{mh} @({x0},{y0}) r={m_r:.0f} f={m_f:.0f}", flush=True)
    card_specs.append((out, t0, t1, geo, mask_off))

RUNTIME.stage("cards", len(slots), len(slots))

# --- 3) Caption-Layer (PIL, Wort-Karaoke + Pink-Box) -----------------------
capdir = os.path.join(tmpdir, "cap_png")
os.makedirs(capdir, exist_ok=True)
import caption_style as _cs
CAP_STYLE = _cs.load_style(lay)
font_path = CAP_STYLE["font"]
PINK = tuple(lay.get("caption", {}).get("pink", [212, 67, 106])) + (255,)
STROKE = int(lay.get("caption", {}).get("stroke", 7))
Y_BODY = int(lay.get("caption", {}).get("y_body", 787))
Y_FULL = int(lay.get("caption", {}).get("y_full", 1205))


def norm_words(text):
    return [w for w in re.split(r"\s+", (text or "").strip()) if w]


def cap_states(c):
    """Zustaende (words, aktiver Index, t0, t1) einer Text-Gruppe."""
    t0, t1 = float(c["start"]), float(c["end"])
    words = norm_words(c.get("text", ""))
    if not words:
        return []
    style = {**CAP_STYLE, **(c.get("style") or {})}
    if style.get("hot_style") == "none":
        return [(words, -1, t0, t1)]
    stored = c.get("words") or []
    if len(stored) == len(words) and all(
            (s.get("w") or "").strip() == words[k] for k, s in enumerate(stored)):
        st0, st1 = stored[0]["t0"], stored[-1]["t1"]
        if st0 >= t0 - 0.3 and st1 <= t1 + 0.3:
            # ABSOLUTE Wort-Zeiten (CapCut-Prinzip, captions_from_words.py):
            # exakt der Sprache folgen statt aufs Seitenfenster zu skalieren
            # (Skalieren = sichtbarer Karaoke-Drift, sobald das Fenster nicht
            # exakt sitzt). Identische Logik im Player (capWordTimes).
            times = [(max(t0, s["t0"]), min(t1, max(s["t0"] + 0.03, s["t1"])))
                     for s in stored]
        else:
            span = max(0.05, st1 - st0)
            times = [(t0 + (s["t0"] - st0) / span * (t1 - t0),
                      t0 + (s["t1"] - st0) / span * (t1 - t0)) for s in stored]
    else:
        lens = [max(1, len(w)) for w in words]
        tot = sum(lens)
        times, acc = [], t0
        for L in lens:
            nxt = acc + (t1 - t0) * L / tot
            times.append((acc, nxt))
            acc = nxt
    out = []
    for j in range(len(words)):
        s0 = times[j][0] if j else t0
        s1 = times[j + 1][0] - 0.01 if j + 1 < len(words) else t1
        if s1 <= s0:
            s1 = s0 + 0.04
        out.append((words, j, s0, s1))
    return out


_measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
entries = []
blank = os.path.join(capdir, "blank.png")
Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(blank)
for gi, c in enumerate(sorted(caps, key=lambda x: float(x.get("start", 0)))):
    size = int(c.get("size") or 62)
    # Position: normalisierte x/y aus dem Player-Editing, sonst Zonen-Default
    y_mid = round(float(c["y"]) * H) if c.get("y") is not None else (
        Y_BODY if body_t0 <= float(c["start"]) < body_t1 else Y_FULL)
    x_mid = round(float(c["x"]) * W) if c.get("x") is not None else (W // 2)
    for (words, j, s0, s1) in cap_states(c):
        # EINE Zeichenfunktion fuer Render + Vorschau (caption_style.py)
        img = text_bitmap("caption", {**c, "hot_idx": j, "size": size,
                                       "x": x_mid / W, "y": y_mid / H}, lay)
        p = os.path.join(capdir, f"g{gi:03d}_{j}.png")
        img.save(p)
        entries.append((p, s0, s1))

entries.sort(key=lambda e: e[1])
cap_concat = os.path.join(capdir, "concat.txt")
with open(cap_concat, "w") as f:
    cursor = 0.0
    lines = []
    for p, s0, s1 in entries:
        if s0 > cursor + 0.001:
            lines.append((blank, s0 - cursor))
        lines.append((p, max(0.04, s1 - s0)))
        cursor = max(cursor, s1)
    if cursor < dur_total:
        lines.append((blank, dur_total - cursor))
    for p, d in lines:
        f.write(f"file '{p}'\noption framerate {FPS}\nduration {d:.8f}\n")
    f.write(f"file '{lines[-1][0]}'\noption framerate {FPS}\n")
cap_layer = None
if entries:
    cap_layer = os.path.join(tmpdir, "cap_layer.mov")
    cap_layer = CACHE.render("captions", {"captions": caps, "style": CAP_STYLE,
                                          "body": [body_t0, body_t1], "duration": dur_total},
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", cap_concat,
         "-vsync", "vfr", "-c:v", "png", cap_layer])
print(f"[rebuild_sf] Captions: {len(entries)} Zustaende", flush=True)

# --- 4) Statische Assets (Hook + Titel + Pill) -----------------------------
def text_png(kind):
    layer = lay.get(kind) or {}
    if not (layer.get("lines") if kind == "title" else layer.get("text")):
        return None
    path = os.path.join(tmpdir, kind + ".png")
    text_bitmap(kind, layer, lay).save(path)
    return path


def title_png():
    return text_png("title")


def pill_png():
    return text_png("pill")


TITLE, PILL = title_png(), pill_png()
hook = lay.get("hook") or {}

# --- 5) Composite ----------------------------------------------------------
inputs = ["-i", cut]
for spec in card_specs:                 # (pfad, t0, t1, geo, mask_off) — nur der Pfad zaehlt
    inputs += ["-i", spec[0]]
idx = len(card_specs) + 1
extra = []
def add_input(path):
    global idx
    inputs.extend(["-loop", "1", "-i", path])
    i = idx
    idx += 1
    return i

i_hf = add_input(hook["hf"]["file"]) if hook.get("hf", {}).get("file") and os.path.exists(hook.get("hf", {}).get("file", "")) else None
i_logo = add_input(hook["logo"]["file"]) if hook.get("logo", {}).get("file") and os.path.exists(hook.get("logo", {}).get("file", "")) else None
i_title = add_input(TITLE) if TITLE else None
i_pill = add_input(PILL) if PILL else None
i_cap = None
if cap_layer:
    inputs += ["-i", cap_layer]
    i_cap = idx

fc = [f"color=white:s={W}x{H}:r={FPS}:d={dur_total:.8f}[bg]",
      "[0:v]setsar=1[v0]",
      f"[bg][v0]overlay=x=0:y='if(between(t,{body_t0:.3f},{body_t1:.3f}),{video_yoff},0)':shortest=1[base]"]
prev = "[base]"
for i, (cpath, t0, t1, geo, mask_off) in enumerate(card_specs):
    slot_start, slot_end = round(t0 * FPS), round(t1 * FPS)
    fc.append(f"[{i+1}:v]setpts=PTS-STARTPTS+{slot_start}/{FPS}/TB[cd{i}]")
    if geo:
        # Mittelpunkt in Design-Koordinaten -> ffmpeg rechnet die Ecke selbst
        # (overlay_w/h kennt die tatsaechliche Groesse nach dem Skalieren).
        # mask_off verschiebt auf den Masken-Mittelpunkt, sonst (0,0).
        gx = max(-0.5, min(1.5, float(geo.get("x", 0.5))))
        gy = max(-0.5, min(1.5, float(geo.get("y", 0.5))))
        ox = f"{gx * W + mask_off[0]:.0f}-overlay_w/2"
        oy = f"{gy * H + mask_off[1]:.0f}-overlay_h/2"
    else:
        ox, oy = str(card.get("x", -118)), str(card.get("y", 0))
    fc.append(f"{prev}[cd{i}]overlay=x='{ox}':y='{oy}':"
              f"enable='gte(t,{slot_start}/{FPS})*lt(t,{slot_end}/{FPS})':eof_action=pass[b{i}]")
    prev = f"[b{i}]"
if i_hf is not None:
    hf = hook["hf"]
    fc.append(f"[{i_hf}:v]scale={hf.get('scale_w', 919)}:-1[hf]")
    fc.append(f"{prev}[hf]overlay=x={hf.get('x', 80)}:y={hf.get('y', 1215)}:enable='lte(t,{body_t0:.3f})'[hh]")
    prev = "[hh]"
if i_logo is not None:
    lg = hook["logo"]
    fc.append(f"[{i_logo}:v]scale={lg.get('scale_w', 460)}:-1[lg]")
    fc.append(f"{prev}[lg]overlay=x={lg.get('x', 310)}:y={lg.get('y', 1738)}:enable='lte(t,{body_t0:.3f})'[ll]")
    prev = "[ll]"
if i_title is not None:
    fc.append(f"{prev}[{i_title}:v]overlay=0:0:enable='lte(t,{float((lay.get('title') or {}).get('show_s', 2.33)):.2f})'[tt]")
    prev = "[tt]"
if i_pill is not None:
    from_end = float((lay.get("pill") or {}).get("from_end_s", 2.34))
    fc.append(f"{prev}[{i_pill}:v]overlay=0:0:enable='gte(t,{max(0, dur_total - from_end):.3f})'[pp]")
    prev = "[pp]"
if i_cap is not None:
    fc.append(f"{prev}[{i_cap}:v]overlay=0:0:eof_action=pass,format=yuv420p[vout]")
else:
    fc.append(f"{prev}format=yuv420p[vout]")

final_tmp = os.path.join(tmpdir, "proxy_new.mp4")
# CRF haelt die Bildqualitaet; medium spart gegenueber slow Renderzeit bei
# etwas groesseren Dateien. Der Arbeits-Proxy bleibt auf fast.
_crf = "16" if EXPORT else "17"
_preset = "medium" if EXPORT else "fast"
_extra = ["-profile:v", "high", "-level", "4.2", "-movflags", "+faststart",
          "-x264-params", "keyint=120:min-keyint=60"] if EXPORT else []
RUNTIME.stage("composite", 0, 1)
# Audio deliberately excluded: changing gain reuses the expensive picture render.
composite = CACHE.render("composite", {"cut": cut, "cards": card_specs,
                                      "layout": {k: v for k, v in lay.items() if k != "audio"},
                                      "captions": caps, "fps": FPS, "export": EXPORT,
                                      "hook_sources": {k: file_identity(v["file"]) for k, v in hook.items()
                                                       if isinstance(v, dict) and v.get("file")}},
    ["ffmpeg", "-y", "-loglevel", "warning"] + inputs + [
    "-filter_complex", ";".join(fc), "-map", "[vout]", "-an",
    "-c:v", "libx264", "-crf", _crf, "-preset", _preset, "-threads", "8", "-pix_fmt", "yuv420p",
    "-r", str(FPS)] + _extra + [
    "-t", f"{dur_total:.8f}", "-frames:v", str(total_frames), final_tmp], frames=total_frames)
RUNTIME.stage("composite", 1, 1)

proxy = ZIEL or os.path.join(WORKDIR, "proxy.mp4")
os.makedirs(os.path.dirname(proxy), exist_ok=True)
# Target-local temporary allows atomic publication even across volumes.
final_tmp = str(Path(proxy).with_name(Path(proxy).stem + ".part-" + uuid.uuid4().hex + ".mp4"))
RUNTIME.stage("mux", 0, 1)
run_ffmpeg(["ffmpeg", "-y", "-loglevel", "error", "-i", composite, "-i", audio_encoded,
            "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-t", f"{max(dur_total, audio['duration']):.8f}",
            "-movflags", "+faststart", final_tmp], "mux", RUNTIME, total_frames)
os.replace(final_tmp, proxy)
RUNTIME.stage("mux", 1, 1)
print(f"[rebuild_sf] FERTIG: {os.path.basename(proxy)} ({dur_total:.2f}s, "
      f"{FPS} fps) in {time.time()-_T0:.0f}s Renderzeit", flush=True)
if EXPORT and not JOB_DIR:
    subprocess.run(["open", "-R", proxy], check=False)
