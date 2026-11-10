#!/usr/bin/env python3
"""B-Roll-SYNC-Pass (bc2+, saas7: MULTI-SOURCE): parallel aufgenommene
Screen-Recordings (und/oder erzeugte Grafik-B-Rolls) ueber das GESCHNITTENE
Video legen, quellzeit-gekoppelt, mit Facecam-Kreis unten rechts.

Konzept: Jede Quelle lief parallel zum Hauptvideo ab Source-Zeit `src_offset`
(Quelle t=0 == Hauptvideo t=src_offset). Fuer jedes Keep-Segment, das mit einem
Quell-Fenster ueberlappt, wird das identische Stueck aus der Quelle geschnitten
(gespiegelte Cuts -> Screen bleibt synchron zur Narration) und fullscreen ueber
die Cut-Timeline gelegt. Obendrauf: Facecam als Kreis (Crop aus dem
geschnittenen Video selbst) + weicher Drop-Shadow.

Config (broll_sync.json) — NEU multi-source (Legacy-Form wird konvertiert):
{
  "sources": [
    {"name": "Grafik", "file": "/abs/a.mp4", "src_offset": 704.516,
     "window": [704.516, 993.464]},        # optional, default: offset+dauer
    {"name": "Screen 1", "file": "/abs/b.mp4", "src_offset": 86.867}
  ],
  "pip": {"diameter_frac": 0.221, "margin_right_frac": 0.0146,
          "margin_bottom_frac": 0.037,
          "crop_frac": {"x": 0.3047, "y": 0.0208, "size_of_h": 0.9167},
          "shadow": true}
}
Bei ueberlappenden Fenstern gewinnt die FRUEHERE Quelle in der Liste.

Usage: broll_sync_pass.py <base_cut.mp4> <segments.json> <broll_sync.json>
                          <out.mp4> [--mode proxy|final]
"""
import sys, os, json, subprocess, tempfile

import numpy as np
import cv2
from frame_timed_screen_slots import frame_timed_screen_slots


def facecam_enable(expression, slots):
    """Suppress circle and shadow only inside explicitly opted-in free slots."""
    hidden = [f"gte(t,{s['a']:.6f})*lt(t,{s['b']:.6f})"
              for s in slots if s.get("hide_facecam") is True]
    return f"({expression})*not({'+'.join(hidden)})" if hidden else expression


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write((r.stderr or "")[-4000:])
        # returncode IMMER mitloggen: ein negativer Code = Signal/Crash —
        # dann ist stderr leer und ohne den Code debuggt man blind (15.08.).
        raise SystemExit(f"Kommando fehlgeschlagen (rc={r.returncode}): {' '.join(cmd[:4])} ...")
    return r


def probe_wh_dur(path):
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-show_entries",
             "format=duration", "-of", "json", path])
    d = json.loads(r.stdout)
    st = d["streams"][0]
    return int(st["width"]), int(st["height"]), float(d["format"]["duration"])


def make_pngs(tmp, D, shadow_grow=0.20, shadow_alpha=0.55):
    """Kreis-Alphamaske (D x D) + weicher Schatten (groesser, verschoben eingesetzt)."""
    ss = 4  # Supersampling gegen Treppen
    big = D * ss
    yy, xx = np.mgrid[0:big, 0:big].astype(np.float32)
    r = np.hypot(xx - (big - 1) / 2.0, yy - (big - 1) / 2.0)
    a = np.clip(((big / 2.0) - r) / ss, 0, 1)  # 1px-AA-Kante (nach Downscale)
    # GRAYSCALE-Maske: alphamerge nimmt den LUMA des zweiten Inputs als Alpha
    # (weisser Kreis auf schwarz). NICHT als RGBA schreiben!
    alpha = cv2.resize((a * 255).astype(np.uint8), (D, D), interpolation=cv2.INTER_AREA)
    mask_p = os.path.join(tmp, "circle_mask.png")
    cv2.imwrite(mask_p, alpha)

    # Schatten: schwarzer Kreis, kraeftig geblurrt, als BGRA
    S = int(D * (1 + shadow_grow))
    S += S % 2
    sh_alpha = np.zeros((S, S), np.float32)
    cv2.circle(sh_alpha, (S // 2, S // 2), int(D * 0.5), 1.0, -1)
    k = int(D * 0.10) | 1
    sh_alpha = cv2.GaussianBlur(sh_alpha, (k, k), 0) * shadow_alpha
    shadow = np.dstack([np.zeros((S, S, 3), np.uint8),
                        (sh_alpha * 255).astype(np.uint8)])
    shadow_p = os.path.join(tmp, "circle_shadow.png")
    cv2.imwrite(shadow_p, shadow)
    return mask_p, shadow_p, S


def load_sources(cfg, converted=None):
    """Config -> Liste nutzbarer Quellen (Legacy-Single-Form wird konvertiert).

    converted: Namen von Quellen, die der Cockpit-User in freie Slots
    umgewandelt hat -> hier ueberspringen (sie werden als broll[]-Slots
    gerendert, nicht mehr source-gespiegelt).
    """
    converted = set(converted or [])
    raw = cfg.get("sources")
    if raw is None:
        raw = [{"name": "B-Roll", "file": cfg["file"],
                "src_offset": cfg["src_offset"]}]
    out = []
    for i, s in enumerate(raw):
        if s.get("name") in converted:
            print(f"[sync] Quelle '{s.get('name')}' in freie Slots umgewandelt "
                  f"-> nicht mehr gespiegelt", flush=True)
            continue
        f = s.get("file", "")
        if not f or not os.path.exists(f):
            print(f"[sync] Quelle {i} fehlt ({f}) -> uebersprungen", flush=True)
            continue
        _, _, dur = probe_wh_dur(f)
        off = float(s["src_offset"])
        win = s.get("window") or [off, off + dur]
        a = max(float(win[0]), off)
        b = min(float(win[1]), off + dur)
        if b - a < 0.04:
            print(f"[sync] Quelle {i} Fenster leer -> uebersprungen", flush=True)
            continue
        out.append({"idx": len(out), "name": s.get("name", f"B-Roll {i}"),
                    "file": f, "off": off, "win_a": a, "win_b": b, "dur": dur})
    return out


def main():
    args = sys.argv[1:]
    base_mp4, segs_path, cfg_path, out_mp4 = args[:4]
    mode = "proxy"
    if "--mode" in args:
        mode = args[args.index("--mode") + 1]

    cfg = json.load(open(cfg_path))
    pip = cfg.get("pip", {})
    # pip.enabled=false -> KEIN Facecam-Kreis, B-Roll laeuft vollflaechig
    # (v8d: Interview-Mitschnitt, waehrend der Grafik soll niemand zu sehen
    # sein). Default bleibt an, damit alle bestehenden Configs unveraendert
    # rendern.
    pip_an = pip.get("enabled") is not False

    # Cockpit-Override zuerst laden (converted-Quellen + sync_off)
    ovr_path = os.path.join(os.path.dirname(os.path.abspath(cfg_path)),
                            "cockpit_overrides.json")
    _ovr_early = {}
    if os.path.exists(ovr_path):
        try:
            _ovr_early = json.load(open(ovr_path))
        except Exception:
            _ovr_early = {}
    effective = json.load(open(segs_path))
    cfg, _ovr_early, frame_audit = frame_timed_screen_slots(cfg, _ovr_early, effective)
    exact_screen_frames = cfg.get("exact_screen_frames") is True
    if exact_screen_frames:
        cfg["force_two_pass"] = True
    if frame_audit["applied"]:
        print(f"[sync] Screen-Slots auf effektives Bildraster abgebildet "
              f"({frame_audit['screen_pieces']} Stuecke, "
              f"max. {frame_audit['max_start_shift_ms']:.3f} ms Korrektur)", flush=True)
    converted = _ovr_early.get("broll_sync_converted", [])
    sync_off = set(_ovr_early.get("broll_sync_off", []))

    # mirror_sources=false (Config): Quellen NICHT mehr source-gespiegelt
    # rendern — die materialisierten broll[]-Slots sind die EINZIGE Wahrheit
    # (Cockpit-Ansicht == Render). Noetig, sobald Sebastian Slots geloescht
    # hat: die Spiegelung wuerde geloeschte Bereiche sonst wieder auffuellen
    # (saas7-Vorfall 27.07.: 74.6s Screen-2-Reste bei 7:47-9:01).
    if cfg.get("mirror_sources", True) is False:
        converted = list({s.get("name") for s in (cfg.get("sources") or [])}
                         | set(converted or []))
        print("[sync] mirror_sources=false -> KEINE Quell-Spiegelung, "
              "nur broll[]-Slots", flush=True)

    sources = load_sources(cfg, converted)

    segs = effective["segments"]
    segs = [s for s in segs if s["out"] > s["in"]]
    W, H, _ = probe_wh_dur(base_mp4)
    # render_size (Config, [W,H]): Composite auf Ziel-Groesse rendern (Base
    # wird hochskaliert, B-Rolls bleiben nativ scharf). crop_frac ist relativ.
    rs = cfg.get("render_size")
    scale_base = False
    if rs and (int(rs[0]) != W or int(rs[1]) != H):
        W, H = int(rs[0]), int(rs[1])
        scale_base = True
        print(f"[sync] render_size: Composite auf {W}x{H} (Base wird "
              f"hochskaliert, B-Rolls nativ)", flush=True)
    if sync_off:
        print(f"[sync] {len(sync_off)} Clip(s) vom Overlay ausgenommen "
              f"(broll_sync_off)", flush=True)

    # Freie B-Roll-Slots (cockpit_overrides.json "broll", Output-Zeit): dort
    # werden die Sync-Overlays AUSGESTANZT (Slot gewinnt — z.B. generierte
    # Grafik-B-Rolls, die Sebastian frei verschiebt), der Facecam-Kreis bleibt
    # aber auch ueber Slots aktiv. Die Slots selbst brennt rerender.py Schritt 5
    # VOR diesem Pass ein.
    slot_defs = []
    sync_holes = []
    if os.path.exists(ovr_path):
        try:
            _ovr = _ovr_early
            for b in _ovr.get("broll", []):
                if (b.get("file") and os.path.exists(b["file"])
                        and float(b.get("end", 0)) > float(b.get("start", 0))):
                    slot_defs.append({"a": float(b["start"]), "b": float(b["end"]),
                                      "file": b["file"],
                                      "hide_facecam": b.get("kind") == "free" and b.get("hide_facecam") is True,
                                      "src_in": float(b.get("src_in", 0) or 0)})
            # Sync-Loecher (SOURCE-Zeit): vom Cockpit ausgeblendete Teile der
            # Screen-B-Rolls (W/Q/E/Entf auf Sync-Stuecke)
            for h in _ovr.get("broll_sync_holes", []):
                if h and len(h) == 2 and float(h[1]) > float(h[0]):
                    sync_holes.append((float(h[0]), float(h[1])))
        except Exception:
            pass
    if sync_holes:
        print(f"[sync] {len(sync_holes)} Sync-Loch/Loecher (Source-Zeit) werden "
              f"ausgestanzt", flush=True)
    # Slots, die auf die DATEI einer konvertierten Sync-Quelle zeigen
    # (materialisierte Screen-Stuecke, yt1b: 184 Stueck), duerfen NICHT als
    # Einzel-Inputs gerendert werden (184 Decoder derselben Datei = OOM).
    # Sie laufen stattdessen als pieces durch die Run-/Two-Pass-Maschinerie.
    # ALLE Source-Dateien (aktiv UND konvertiert): Slots auf diesen Dateien
    # sind materialisierte/arrangierte Screen-Stuecke (saas7: 64 Slots auf 2
    # Quelldateien bei leerem converted) -> pieces-Pipeline statt Einzel-Input.
    src_files = {s.get("file") for s in (cfg.get("sources") or [])}
    if cfg.get("file"):
        src_files.add(cfg.get("file"))
    screen_slot_defs = [sd for sd in slot_defs if sd["file"] in src_files]
    slot_defs = [sd for sd in slot_defs if sd["file"] not in src_files]
    # Ausstanzung + Facecam-Kreis wirken ueber ALLE Slots — Screen-Slots
    # muessen die gespiegelte Sync-Spur weiterhin verdraengen (Slot gewinnt).
    slots = ([(s["a"], s["b"]) for s in screen_slot_defs]
             + [(s["a"], s["b"]) for s in slot_defs])
    if screen_slot_defs:
        print(f"[sync] {len(screen_slot_defs)} materialisierte Screen-Slot(s) "
              f"laufen ueber die Sync-Render-Pipeline", flush=True)
    if slot_defs:
        print(f"[sync] {len(slot_defs)} freie(r) B-Roll-Slot(s): werden HIER "
              f"gerendert (ueber Sync, unter Facecam-Kreis), Sync wird dort "
              f"ausgestanzt", flush=True)

    # 1. Timeline-Mapping: Keep-Segmente x Quellen ->
    #    (timeline_a, timeline_b, src_idx, broll_a, broll_b)
    #    Bei Ueberlapp gewinnt die fruehere Quelle der Liste: wir splitten das
    #    Segment-Intervall an allen Fenstergrenzen und vergeben jedes Stueck an
    #    die erste Quelle, die es abdeckt.
    # Die gespiegelte Bildschirmaufnahme ist eine BILDebene — sie muss also der
    # BILDzeitachse folgen (v_in/v_out), nicht der Tonachse. Ohne L-Cut sind
    # beide identisch, dann aendert sich hier nichts. Mit L-Cut wandert der
    # Screen zusammen mit dem Hauptbild, sonst spraenge er 250ms spaeter als
    # das Bild darunter (und als der Facecam-Kreis, der aus dem Hauptbild kommt).
    pieces, cum = [], 0.0
    for s in segs:
        s_in = float(s.get("v_in", s["in"]))
        s_out = float(s.get("v_out", s["out"]))
        d = float(s.get("v_duration", s_out - s_in))
        if s.get("audio_exact"):
            cum = float(s["v_start"])
        if str(s.get("first_id", "")) in sync_off:
            cum += d
            continue
        bounds = {s_in, s_out}
        for q in sources:
            for b in (q["win_a"], q["win_b"]):
                if s_in < b < s_out:
                    bounds.add(b)
        for ha, hb in sync_holes:
            for b in (ha, hb):
                if s_in < b < s_out:
                    bounds.add(b)
        bl = sorted(bounds)
        for a, b in zip(bl, bl[1:]):
            if b - a < 0.04:
                continue
            mid = (a + b) / 2.0
            if any(ha <= mid <= hb for ha, hb in sync_holes):
                continue   # vom Cockpit ausgeblendetes Sync-Fenster
            owner = next((q for q in sources
                          if q["win_a"] <= a + 0.001 and b - 0.001 <= q["win_b"]), None)
            if owner is None:
                continue
            pieces.append((cum + (a - s_in), cum + (b - s_in),
                           owner["idx"], a - owner["off"], b - owner["off"]))
        cum += d
    # 1b. Slot-Ausstanzung: Sync-Stuecke an Slot-Intervallen klippen
    #     (Quellzeit wandert beim Klippen mit)
    if slots and pieces:
        punched = []
        for (a, b, si, ba, bb) in pieces:
            cur_iv = [(a, b, ba)]
            for (sa, sb) in slots:
                nxt = []
                for (x, y, bx) in cur_iv:
                    if sb <= x or sa >= y:
                        nxt.append((x, y, bx)); continue
                    if x < sa:
                        nxt.append((x, sa, bx))
                    if sb < y:
                        nxt.append((sb, y, bx + (sb - x)))
                cur_iv = nxt
            for (x, y, bx) in cur_iv:
                if y - x > 0.04:
                    punched.append((x, y, si, bx, bx + (y - x)))
        pieces = punched

    # Materialisierte Screen-Slots als pieces einspeisen (synthetische Quelle
    # pro Datei, damit split/remap/Two-Pass identisch funktionieren). Timeline-
    # Position = Slot [a,b], Quellzeit = src_in (Ausstanzung an Grafik-Slots
    # ist bereits bei der Materialisierung passiert).
    if screen_slot_defs:
        file2idx = {}
        for f in sorted({sd["file"] for sd in screen_slot_defs}):
            idx = len(sources)
            file2idx[f] = idx
            sources.append({"idx": idx, "name": f"slot:{os.path.basename(f)}",
                            "file": f, "off": 0.0, "win_a": 0.0, "win_b": 0.0,
                            "dur": 0.0})
        for sd in screen_slot_defs:
            d = sd["b"] - sd["a"]
            pieces.append((sd["a"], sd["b"], file2idx[sd["file"]],
                           sd["src_in"], sd["src_in"] + d))
        pieces.sort(key=lambda p: p[0])

    if not pieces and not slots:
        print("[sync] Keine Ueberlappung mit B-Roll-Fenstern -> Pass uebersprungen", flush=True)
        subprocess.run(["cp", base_mp4, out_mp4], check=True)
        return

    # 2. Runs: zusammenhaengende Timeline-Stuecke gruppieren (quelluebergreifend)
    #
    # ZWEI Gruppierungen, bewusst getrennt (01.08.2026):
    #  - render_runs: zusaetzlich nach ANZAHL begrenzt. Der Two-Pass unten baut
    #    pro Run ein ffmpeg mit split=<Stueckzahl>. Bei durchgehendem Overlay
    #    landen ALLE Stuecke in einem einzigen Run -> split=213 auf einer
    #    4805s-Datei -> ffmpeg puffert 213 Streamzweige und das System
    #    thrasht sich fest (yt1b, 0.2% CPU, 0 MB Fortschritt in 60s).
    #    Der bestehende Two-Pass half hier nicht, weil er nur PRO Run teilt.
    #  - circle_runs: bleibt zusammenhaengend. Die Facecam-Kreis-Transition
    #    haengt an den Run-Grenzen — wuerde sie den kuenstlichen Schnitten
    #    folgen, blendete der Kreis alle paar Sekunden neu ein.
    MAX_PIECES_PER_RUN = 24
    circle_runs, render_runs = [], []
    if pieces:
        cur_c = [pieces[0]]; cur_r = [pieces[0]]
        for p in pieces[1:]:
            contiguous = (round(p[0] * 30) == round(cur_c[-1][1] * 30)
                          if exact_screen_frames else p[0] - cur_c[-1][1] < 0.05)
            if contiguous:
                cur_c.append(p)
            else:
                circle_runs.append(cur_c); cur_c = [p]
            if contiguous and len(cur_r) < MAX_PIECES_PER_RUN:
                cur_r.append(p)
            else:
                render_runs.append(cur_r); cur_r = [p]
        circle_runs.append(cur_c); render_runs.append(cur_r)
    runs = render_runs
    total_ov = sum(p[1] - p[0] for p in pieces)
    per_src = {q["idx"]: 0.0 for q in sources}
    for p in pieces:
        per_src[p[2]] += p[1] - p[0]
    stat = ", ".join(f"{q['name']}={per_src[q['idx']]:.0f}s" for q in sources)
    print(f"[sync] {len(pieces)} B-Roll-Stuecke in {len(runs)} Run(s), "
          f"{total_ov:.1f}s Overlay ({stat})", flush=True)

    # 3. PIP-Geometrie + Masken
    D = int(W * float(pip.get("diameter_frac", 0.221)))
    D += D % 2
    mr = int(W * float(pip.get("margin_right_frac", 0.0146)))
    mb = int(H * float(pip.get("margin_bottom_frac", 0.037)))
    px, py = W - D - mr, H - D - mb
    cf = pip.get("crop_frac", {"x": 0.3047, "y": 0.0208, "size_of_h": 0.9167})
    cw = int(H * float(cf["size_of_h"]))
    cx, cy = int(W * float(cf["x"])), int(H * float(cf["y"]))
    cw -= cw % 2
    cx = max(0, min(cx, W - cw)); cy = max(0, min(cy, H - cw))

    # Zeitabhaengiger Facecam-Ausschnitt (pip.crop_ranges).
    # Grund: Wird an ein Video eine zweite Aufnahme angehaengt, sitzt der
    # Sprecher dort anders im Bild — ein globaler Ausschnitt schneidet dann in
    # einem der Teile den Kopf ab (v5m, 29.07.: Teil 2 sass 106 px hoeher).
    # Format (Zeiten in der GESCHNITTENEN Ausgabe):
    #   "crop_ranges": [{"ab": 701.0, "crop_frac": {"x":…, "y":…, "size_of_h":…}}]
    # Umgesetzt als ffmpeg-Ausdruck, damit nur EIN crop-Filter noetig ist.
    ranges = pip.get("crop_ranges") or []
    def _px(c):
        w2 = int(H * float(c.get("size_of_h", cf["size_of_h"]))); w2 -= w2 % 2
        return (w2,
                max(0, min(int(W * float(c.get("x", cf["x"]))), W - w2)),
                max(0, min(int(H * float(c.get("y", cf["y"]))), H - w2)))
    if ranges:
        gueltig = [r for r in sorted(ranges, key=lambda r: float(r["ab"]))
                   if _px(r.get("crop_frac") or {})[0] == cw]   # Groesse muss gleich bleiben
        verworfen = len(ranges) - len(gueltig)
        if verworfen:
            print("[sync] %d crop_range(s) ignoriert: abweichende size_of_h "
                  "(Zoom darf sich nicht aendern)" % verworfen, flush=True)
        cx_expr, cy_expr = str(cx), str(cy)
        for r in gueltig:
            _, rx, ry = _px(r.get("crop_frac") or {})
            t = float(r["ab"])
            cx_expr = "if(gte(t,%.3f),%d,%s)" % (t, rx, cx_expr)
            cy_expr = "if(gte(t,%.3f),%d,%s)" % (t, ry, cy_expr)
        if gueltig:
            print("[sync] Facecam-Ausschnitt wechselt bei %s (Ausgabe-Zeit)"
                  % ", ".join("%.1fs" % float(r["ab"]) for r in gueltig), flush=True)
    else:
        cx_expr, cy_expr = str(cx), str(cy)
    tmp = tempfile.mkdtemp(prefix="sync_")
    mask_p, shadow_p, S = make_pngs(tmp, D)
    sx = px + (D - S) // 2 + int(D * 0.02)   # Schatten minimal nach rechts/unten
    sy = py + (D - S) // 2 + int(D * 0.035)

    # 4. Filtergraph
    #    Inputs: 0=base, dann NUR die tatsaechlich genutzten Quellen, dann
    #    Maske + Schatten. Ungenutzte Quellen (kein Overlap) bleiben draussen.
    #    Facecam-Kreis-Fenster = Sync-Runs UND freie Slots (Kreis liegt auch
    #    ueber den in Schritt 5 eingebrannten Slot-Grafiken).
    # circle_runs statt runs: die Kreis-Fenster folgen den ECHTEN
    # Overlay-Bereichen, nicht der kuenstlichen Render-Aufteilung.
    circle_win = ([(r[0][0], r[-1][1]) for r in circle_runs] + slots) if pip_an else []
    # Zusammenhaengende Kreis-Fenster mergen (Run|Slot|Run stossen nahtlos
    # aneinander): fuers enable egal, fuer die Slide-Transition PFLICHT —
    # sonst wuerde der Kreis an inneren Kanten kurz raus-/reinfahren.
    circle_win = sorted(circle_win)
    merged_win = []
    for a, b in circle_win:
        if merged_win and a - merged_win[-1][1] <= 0.2:
            merged_win[-1][1] = max(merged_win[-1][1], b)
        else:
            merged_win.append([a, b])
    circle_win = [(a, b) for a, b in merged_win]
    enable = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in circle_win)
    enable = facecam_enable(enable, slot_defs)
    # Rein/Raus-Animation (pip.transition.dur > 0): Kreis+Schatten sliden am
    # Fensterstart von unten rein und am Fensterende wieder raus (ease-out).
    # Sebastian (02.08.): KEINE Rein/Raus-Animation der Facecam. Der Kreis ist
    # einfach da, wenn er da ist — hartes An/Aus ueber enable, kein Slide.
    # Deshalb hart 0.0, egal was in der pip-Config steht; unten greift dann
    # der statische elif-Zweig (overlay mit festem x/y).
    tdur = 0.0
    split_count = {q["idx"]: 0 for q in sources}
    for p in pieces:
        split_count[p[2]] += 1
    used = [q for q in sources if split_count[q["idx"]] > 0]
    remap = {q["idx"]: 1 + i for i, q in enumerate(used)}

    # TWO-PASS ab vielen Stuecken: EIN ffmpeg mit split=N puffert N Zweige des
    # kompletten Quell-Streams -> OOM-Kill ohne stderr (yt1b: 180 Stuecke).
    # Stattdessen: Pass 1 rendert PRO RUN eine fertige Spur (sequenziell,
    # kleiner Graph), Pass 2 legt nur noch die fertigen Run-Videos aufs Base.
    # saas7 (57 Stuecke) bleibt unter der Schwelle -> alter Pfad unveraendert.
    # 31.08.2026: Schwelle 80 -> 40. mod4v7 (65 Stuecke, 2.9GB-Quelle) und
    # mod4v6 (70 Stuecke) starben im Single-Pass-Composite reproduzierbar mit
    # rc=-9 (OOM, auch solo bei 66GB freier Disk) — der Two-Pass-Pfad ist fuer
    # genau diese Klasse gebaut (yt1b: 184 Stuecke fehlerfrei).
    TWO_PASS_THRESHOLD = 40
    run_files = None
    if len(pieces) > TWO_PASS_THRESHOLD or cfg.get("force_two_pass") is True:
        print(f"[sync] {len(pieces)} Stuecke, Schwelle {TWO_PASS_THRESHOLD}, "
              f"force_two_pass={cfg.get('force_two_pass') is True} -> Two-Pass "
              f"(pro Run rendern, sequenziell)", flush=True)
        run_files = []
        for ri, r in enumerate(runs):
            rf = os.path.join(tmp, f"run_{ri:02d}.mp4")
            srcs_in_run = sorted({si for (_, _, si, _, _) in r})
            in_map = {}
            cmd1 = ["ffmpeg", "-y", "-loglevel", "error"]
            for k, si in enumerate(srcs_in_run):
                src = next(q for q in sources if q["idx"] == si)
                cmd1 += ["-i", src["file"]]
                in_map[si] = k
            cnt = {si: 0 for si in srcs_in_run}
            for (_, _, si, _, _) in r:
                cnt[si] += 1
            fc1 = ""
            for si in srcs_in_run:
                if cnt[si] > 1:
                    fc1 += f"[{in_map[si]}:v]split={cnt[si]}" + "".join(
                        f"[s{si}_{k}]" for k in range(cnt[si])) + ";"
                else:
                    fc1 += f"[{in_map[si]}:v]null[s{si}_0];"
            ti = {si: 0 for si in srcs_in_run}
            parts1 = []
            for j1, (tla, tlb, si, bra, brb) in enumerate(r):
                k = ti[si]; ti[si] += 1
                nfr = max(1, round((tlb - tla) * 30))
                timing = (f"trim={bra:.6f}:{bra+nfr/30+0.2:.6f},setpts=PTS-STARTPTS,"
                          f"fps=30,tpad=stop_mode=clone:stop_duration=0.5,"
                          f"trim=end_frame={nfr},setpts=N/30/TB," if exact_screen_frames else
                          f"trim={bra:.4f}:{brb:.4f},setpts=PTS-STARTPTS,fps=30,")
                fc1 += (f"[s{si}_{k}]{timing}scale={W}:{H}:force_original_aspect_ratio=increase,"
                        f"crop={W}:{H},setsar=1[t{j1}];")
                parts1.append(f"[t{j1}]")
            if len(parts1) > 1:
                fc1 += "".join(parts1) + f"concat=n={len(parts1)}:v=1:a=0[out]"
            else:
                fc1 += f"{parts1[0]}null[out]"
            if exact_screen_frames:
                fc1 = fc1.removesuffix("[out]") + ",setpts=N/30/TB[out]"
            # Zwischenprodukt (vorgerenderte Screen-Laeufe). Im max-Modus
            # praktisch verlustfrei, damit die Bildschirmaufnahme — der
            # detailreichste Teil des Videos (Text, Terminal) — nicht schon vor
            # dem finalen Encode Artefakte mitbringt.
            _icrf = "12" if mode == "max" else "15"
            _ipre = "veryfast" if mode == "max" else "ultrafast"
            cmd1 += ["-filter_complex", fc1, "-map", "[out]",
                     "-c:v", "libx264", "-preset", _ipre, "-crf", _icrf,
                     "-pix_fmt", "yuv420p"]
            if exact_screen_frames:
                cmd1 += ["-fps_mode", "passthrough", "-video_track_timescale", "30000"]
            cmd1 += [rf]
            run(cmd1)
            run_files.append(rf)
            print(f"[sync]   Run {ri + 1}/{len(runs)} gerendert "
                  f"({len(r)} Stuecke, {r[-1][1] - r[0][0]:.0f}s)", flush=True)

    mask_in, shadow_in = 1 + len(used), 2 + len(used)
    if run_files is not None:
        # Pass 2: Inputs = base, run-Videos, Maske, Schatten, Slots
        mask_in, shadow_in = 1 + len(run_files), 2 + len(run_files)
        # ohne Facecam-Kreis kein zweiter Zweig — ein unbenutzter [fcsrc]
        # waere ein nicht verbundener Filterausgang und ffmpeg bricht ab
        fc = ((f"[0:v]scale={W}:{H}:flags=lanczos,setsar=1,split=2[bg0][fcsrc];"
               if scale_base else f"[0:v]split=2[bg0][fcsrc];") if pip_an else
              (f"[0:v]scale={W}:{H}:flags=lanczos,setsar=1[bg0];"
               if scale_base else f"[0:v]null[bg0];"))
        tracks = []
        for ri in range(len(runs)):
            placement = (f"{round(runs[ri][0][0]*30)}/30" if exact_screen_frames else
                         f"{runs[ri][0][0]:.3f}")
            fc += f"[{1 + ri}:v]setpts=PTS-STARTPTS+{placement}/TB[trs{ri}];"
            tracks.append(f"[trs{ri}]")
    else:
        # ohne Facecam-Kreis kein zweiter Zweig — ein unbenutzter [fcsrc]
        # waere ein nicht verbundener Filterausgang und ffmpeg bricht ab
        fc = ((f"[0:v]scale={W}:{H}:flags=lanczos,setsar=1,split=2[bg0][fcsrc];"
               if scale_base else f"[0:v]split=2[bg0][fcsrc];") if pip_an else
              (f"[0:v]scale={W}:{H}:flags=lanczos,setsar=1[bg0];"
               if scale_base else f"[0:v]null[bg0];"))
        for q in used:
            n = split_count[q["idx"]]
            if n > 1:
                fc += f"[{remap[q['idx']]}:v]split={n}" + "".join(
                    f"[s{q['idx']}_{k}]" for k in range(n)) + ";"
            else:
                fc += f"[{remap[q['idx']]}:v]null[s{q['idx']}_0];"
        take_i = {q["idx"]: 0 for q in used}
        j = 0
        tracks = []
        for ri, r in enumerate(runs):
            parts = []
            for (tla, tlb, si, bra, brb) in r:
                k = take_i[si]; take_i[si] += 1
                fc += (f"[s{si}_{k}]trim={bra:.4f}:{brb:.4f},setpts=PTS-STARTPTS,"
                       f"fps=30,scale={W}:{H}:force_original_aspect_ratio=increase,"
                       f"crop={W}:{H},setsar=1[t{j}];")
                parts.append(f"[t{j}]"); j += 1
            if len(parts) > 1:
                fc += "".join(parts) + f"concat=n={len(parts)}:v=1:a=0[trk{ri}];"
            else:
                fc += f"{parts[0]}null[trk{ri}];"
            fc += f"[trk{ri}]setpts=PTS-STARTPTS+{r[0][0]:.3f}/TB[trs{ri}];"
            tracks.append(f"[trs{ri}]")
    prev = "bg0"
    for ri in range(len(runs)):
        run_enable = (f"gte(t,{round(runs[ri][0][0]*30)}/30)*lt(t,{round(runs[ri][-1][1]*30)}/30)"
                      if exact_screen_frames else
                      f"between(t,{runs[ri][0][0]:.3f},{runs[ri][-1][1]:.3f})")
        fc += (f"[{prev}]{tracks[ri]}overlay=x=0:y=0:eof_action=pass:"
               f"enable='{run_enable}'[ov{ri}];")
        prev = f"ov{ri}"
    # Freie Slots: eigene Inputs (nach Maske+Schatten), Overlay UEBER den
    # Sync-Tracks aber UNTER dem Facecam-Kreis. fcsrc bleibt das reine Base
    # (Talking-Head) — deshalb duerfen Slots NICHT vorher eingebrannt sein.
    slot_in0 = shadow_in + 1
    for j, sd in enumerate(slot_defs):
        dur = sd["b"] - sd["a"]
        slot_enable = (f"gte(t,{sd['a']:.6f})*lt(t,{sd['b']:.6f})" if sd.get("hide_facecam")
                       else f"between(t,{sd['a']:.3f},{sd['b']:.3f})")
        fc += (f"[{slot_in0 + j}:v]trim={sd['src_in']:.3f}:{sd['src_in'] + dur + 0.5:.3f},"
               f"setpts=PTS-STARTPTS,fps=30,"
               f"scale={W}:{H}:force_original_aspect_ratio=increase,"
               f"crop={W}:{H},setsar=1,setpts=PTS+{sd['a']:.3f}/TB[sl{j}];")
        fc += (f"[{prev}][sl{j}]overlay=x=0:y=0:eof_action=pass:"
               f"enable='{slot_enable}'[os{j}];")
        prev = f"os{j}"
    if not pip_an:
        # Vollflaechig-Modus: kein Facecam-Kreis, kein Schatten. Der letzte
        # Overlay-Zweig wird direkt zum Ausgang durchgereicht.
        fc += f"[{prev}]null[vout]"
        print("[sync] Facecam-Kreis AUS (pip.enabled=false) — B-Roll vollflaechig", flush=True)
    elif True:
        fc += (f"[fcsrc]crop={cw}:{cw}:'{cx_expr}':'{cy_expr}',scale={D}:{D}[fcs];"
               f"[fcs][{mask_in}:v]alphamerge[fca];")
    if pip_an and tdur > 0 and circle_win:
        # Pro gemergtem Fenster eigenes Kreis+Schatten-Overlay mit
        # zeitabhaengigem y: quadratisches Ease-out rein, gespiegelt raus.
        K = H - py + int(D * 0.12)   # Weg bis komplett unter den Frame
        n = len(circle_win)
        fc += f"[fca]split={n}" + "".join(f"[fca{i}]" for i in range(n)) + ";"
        fc += f"[{shadow_in}:v]split={n}" + "".join(f"[shp{i}]" for i in range(n)) + ";"
        for i, (a, b) in enumerate(circle_win):
            window_enable = facecam_enable(f"between(t,{a:.3f},{b:.3f})", slot_defs)
            off = (f"{K}*max(pow(max(0,1-(t-{a:.3f})/{tdur}),2),"
                   f"pow(max(0,1-({b:.3f}-t)/{tdur}),2))")
            fc += (f"[{prev}][shp{i}]overlay=x={sx}:y='{sy}+{off}':eof_action=pass:"
                   f"enable='{window_enable}'[tsh{i}];")
            fc += (f"[tsh{i}][fca{i}]overlay=x={px}:y='{py}+{off}':eof_action=pass:"
                   f"enable='{window_enable}'"
                   + (f"[tci{i}];" if i < n - 1 else "[vout]"))
            prev = f"tci{i}"
        print(f"[sync] Kreis-Transition aktiv: {n} Fenster, slide {tdur:.2f}s", flush=True)
    elif pip_an:
        fc += f"[{prev}][{shadow_in}:v]overlay=x={sx}:y={sy}:eof_action=pass:enable='{enable}'[shd];"
        fc += f"[shd][fca]overlay=x={px}:y={py}:eof_action=pass:enable='{enable}'[vout]"

    if mode == "proxy":
        # HARDWARE-ENCODER (15.08.): finaler Composite im proxy-Mode ueber
        # h264_videotoolbox (M3 Pro, 3-5x schneller). 10 Mbit/s ist fuer
        # 1080p30 Talking-Head+Screen visuell transparent und liegt weit
        # ueber dem Re-Encode-Niveau der Upload-Plattformen.
        # AUSNAHME J-Cut-Projekte: bewiesene Software-Kombination (mod4v1) —
        # J-Cut + VT erzeugte 133 ms konstanten A/V-Versatz (mod4v2, 15.08.).
        _jc = False
        try:
            _segs = json.load(open(sys.argv[2])).get("segments", [])
            _jc = any(float(s.get("jcut", 0) or 0) > 1e-3 for s in _segs)
        except Exception:
            pass
        if _jc:
            venc = ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p"]
        else:
            venc = ["-c:v", "h264_videotoolbox", "-b:v", "10M",
                    "-profile:v", "high", "-pix_fmt", "yuv420p"]
    elif mode == "max":
        # LETZTE Generation — das ist die Datei, die zu YouTube hochgeht.
        # H.264 High Profile ist das, was YouTube fuer den Upload empfiehlt;
        # CRF 16 bei preset slow liegt sichtbar ueber dem, was die spaetere
        # YouTube-Transkodierung ohnehin durchlaesst, also geht durch DIESEN
        # Schritt nichts verloren. bframes/ref etwas hoeher = bessere Effizienz
        # bei gleicher Qualitaet.
        # CRF 14 statt 16: der Bildschirmaufnahme-Anteil zeigt kleinen Text und
        # Terminal-Ausgaben — dort sind Kompressionsartefakte am ehesten
        # sichtbar, und YouTube transkodiert danach nochmal.
        venc = ["-c:v", "libx264", "-preset", "slow", "-crf", "14",
                "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
                "-x264-params", "ref=4:bframes=3:rc-lookahead=60"]
    else:
        venc = ["-c:v", "hevc_videotoolbox", "-q:v", "60", "-tag:v", "hvc1",
                "-pix_fmt", "yuv420p"]
    print(f"[sync] Render Composite ({mode}, {W}x{H}, {len(used)} Quellen, "
          f"PIP D={D}px @ {px},{py}) ...", flush=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", base_mp4]
    if run_files is not None:
        for rf in run_files:
            cmd += ["-i", rf]
    else:
        for q in used:
            cmd += ["-i", q["file"]]
    cmd += ["-loop", "1", "-i", mask_p, "-loop", "1", "-i", shadow_p]
    for sd in slot_defs:
        cmd += ["-i", sd["file"]]
    # -fps_mode passthrough: das Basisvideo hat bereits exakt 30 fps und der Ton
    # wird 1:1 kopiert. Wuerde ffmpeg hier neu rastern (Default bei mp4 = cfr),
    # koennte es einzelne Frames verwerfen — der Ton bliebe gleich lang und das
    # Bild wuerde vorlaufen. Genau dieser Fehler steckte im Basis-Render.
    cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", "0:a",
            *venc, "-fps_mode", "passthrough", "-video_track_timescale", "30000",
            "-c:a", "copy", "-movflags", "+faststart", out_mp4]
    run(cmd)
    print(f"[sync] FERTIG -> {out_mp4}", flush=True)


if __name__ == "__main__":
    main()
