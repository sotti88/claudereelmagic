#!/usr/bin/env python3
"""Baut die All-Intra-Playback-Proxys fuer ein Workdir — portabel (Mac/Win/Linux).

Ersetzt den Shell-Weg (build_intra_all.sh, Pfad-verdrahtet + stiller Skip bei
abweichendem B-Roll-Dateinamen) durch eine robuste, ueberall lauffaehige
Fassung. Warum es die Proxys braucht: Das Cockpit spielt sonst die Long-GOP-
Quelle, wo jeder Naht-Seek 250-3000 ms kostet -> Playhead stockt an jedem
Schnitt (genau das "Stocken", das mehrfach 'zurueckkam', weil neue Projekte
den Proxy nie bekamen).

Regeln (aus teuren Fehlschlaegen destilliert, siehe Kommentare):
- g=1 all-intra, -r 30 PFLICHT (VFR-Quellen melden 60/1 -> doppelte Datei)
- .part-Namen waehrend des Encodes (der Cockpit-Server mappt sonst die
  halbfertige Datei)
- START_TIME-ERHALT: der Encode wirft Stream-Startzeiten weg; die
  A/V-Relation der Quelle wird gemessen und per Remux wiederhergestellt
  (sonst laeuft das Bild im Cockpit gegen den Ton — passiert bei jeder
  QuickTime-Aufnahme mit audio_start != 0)
- Dauer-Gate am Ende

Usage:  python scripts/build_playback_proxy.py work/<name> [--only main|broll]
"""
import argparse
import json
import os
import subprocess
import sys


def probe_starts(f):
    out = {}
    try:
        for line in subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_entries",
                 "stream=codec_type,start_time", "-of", "csv=p=0", f],
                text=True).strip().splitlines():
            p = line.split(",")
            if len(p) >= 2:
                try:
                    out[p[0]] = float(p[1])
                except ValueError:
                    pass
    except Exception:
        pass
    return out


def probe_dur(f):
    try:
        return float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", f], text=True).strip().splitlines()[0])
    except Exception:
        return None


def videotoolbox_filters(src, max_side):
    """Scale before rotating, then download small frames for unchanged x264 output."""
    doc = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_streams", "-of", "json", src], text=True))
    stream = doc["streams"][0]
    width, height = int(stream["width"]), int(stream["height"])
    rotation = float(next((s["rotation"] for s in stream.get("side_data_list", [])
                           if "rotation" in s), 0))
    if abs(rotation / 90 - round(rotation / 90)) > 0.001:
        raise ValueError("Videotoolbox preview requires orthogonal source rotation")
    if stream.get("pix_fmt") not in ("yuv420p", "nv12"):
        raise ValueError("Videotoolbox preview currently supports verified 8-bit sources only")
    if max_side is not None:
        factor = min(max_side / width, max_side / height)
        width = max(2, int(width * factor) // 2 * 2)
        height = max(2, int(height * factor) // 2 * 2)
    filters = [f"scale_vt=w={width}:h={height}"]
    # Display-matrix angles are counter-clockwise; neutralize metadata at input.
    direction = {90: "cclock", 180: "reversal", 270: "clock"}.get(round(rotation) % 360)
    if direction:
        filters.append("transpose_vt=dir=" + direction)
    filters.extend(["hwdownload", "format=nv12"])
    return ",".join(filters)


def encode(src, dst, gop, extra_audio, max_side=None, hwaccel=None, crf=23):
    part = dst + ".part.mp4"
    if max_side is not None and (max_side < 240 or max_side % 2):
        raise ValueError("max_side must be an even integer >= 240")
    if not 0 <= crf <= 51:
        raise ValueError("crf must be between 0 and 51")
    input_args = []
    if hwaccel == "videotoolbox":
        input_args = ["-hwaccel", "videotoolbox", "-hwaccel_output_format", "videotoolbox_vld",
                      "-display_rotation:v:0", "0", "-noautorotate"]
    cmd = ["ffmpeg", "-hide_banner", "-y"] + input_args + ["-i", src,
           "-c:v", "libx264", "-g", str(gop), "-bf", "0",
           "-crf", str(crf), "-preset", "veryfast", "-tune", "fastdecode",
           "-r", "30", "-vsync", "cfr"] + extra_audio + \
          ["-movflags", "+faststart", "-f", "mp4", part]
    if hwaccel == "videotoolbox":
        cmd[cmd.index("-c:v"):cmd.index("-c:v")] = ["-vf", videotoolbox_filters(src, max_side)]
    elif max_side is not None:
        cmd[cmd.index("-c:v"):cmd.index("-c:v")] = [
            "-vf", f"scale={max_side}:{max_side}:force_original_aspect_ratio=decrease:force_divisible_by=2"]
    print("[proxy] encode:", os.path.basename(src), "->", os.path.basename(dst), flush=True)
    subprocess.run(cmd, check=True)
    os.replace(part, dst)


def fix_start_relation(src, dst):
    """A/V-Startrelation der Quelle im Proxy wiederherstellen (Remux, bitgleich)."""
    s, p = probe_starts(src), probe_starts(dst)
    soll = s.get("video", 0.0) - s.get("audio", 0.0)
    ist = p.get("video", 0.0) - p.get("audio", 0.0)
    print(f"[proxy] A/V-Startrelation quelle {soll*1000:+.0f} ms, proxy {ist*1000:+.0f} ms", flush=True)
    if abs(soll - ist) <= 0.005:
        return
    off = round(soll - ist, 6)
    tmp = dst + ".fix.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-itsoffset", str(off), "-i", dst,
                    "-i", dst, "-map", "0:v", "-map", "1:a", "-c", "copy",
                    "-movflags", "+faststart", "-f", "mp4", "-y", tmp], check=True)
    os.replace(tmp, dst)
    p2 = probe_starts(dst)
    ist2 = p2.get("video", 0.0) - p2.get("audio", 0.0)
    print(f"[proxy] nach Korrektur-Remux: {ist2*1000:+.0f} ms", flush=True)
    if abs(soll - ist2) > 0.005:
        raise SystemExit("A/V-Startrelation liess sich nicht herstellen!")


def duration_gate(src, dst, tol=0.30):
    o, p = probe_dur(src), probe_dur(dst)
    if o is None or p is None:
        raise SystemExit("Dauer nicht messbar — Gate FAIL")
    print(f"[proxy] Dauer orig {o:.3f}s proxy {p:.3f}s diff {abs(o-p)*1000:.0f} ms", flush=True)
    if abs(o - p) > tol:
        raise SystemExit("Proxy-Dauer weicht zu stark ab — Gate FAIL")


def broll_sources(wd):
    """B-Roll-Quellen aus broll_sync.json (nicht nur der eine Standardname —
    der exakte-Dateiname-Skip hat frueher still 'FERTIG' gemeldet)."""
    doc = {}
    p = os.path.join(wd, "broll_sync.json")
    if os.path.exists(p):
        try:
            doc = json.load(open(p)) or {}
        except Exception:
            doc = {}
    srcs = doc.get("sources") or ([doc] if doc.get("file") else [])
    out = []
    for i, q in enumerate(srcs):
        f = q.get("file")
        if f and os.path.exists(f):
            out.append((f, os.path.join(
                wd, "edit_intra_broll.mp4" if i == 0 else "edit_intra_broll%d.mp4" % (i + 1))))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("--only", choices=["main", "broll"], default=None)
    ap.add_argument("--max-side", type=int, default=None,
                    help="Optional preview-only size cap; original/export source is unchanged")
    ap.add_argument("--crf", type=int, default=23,
                    help="Playback encoding quality, lower is better (default: 23)")
    ap.add_argument("--hwaccel", choices=["videotoolbox"], default=None,
                    help="Opt-in hardware decode/scale for verified 8-bit playback sources")
    args = ap.parse_args()
    wd = os.path.abspath(args.workdir)

    src = None
    ck = os.path.join(wd, "cockpit.json")
    if os.path.exists(ck):
        try:
            src = (json.load(open(ck)) or {}).get("src_video")
        except Exception:
            pass
    if not src or not os.path.exists(src):
        kand = [f for f in os.listdir(wd) if f.startswith(".protect_main")]
        src = os.path.join(wd, kand[0]) if kand else None
    if args.only in (None, "main"):
        if not src or not os.path.exists(src):
            raise SystemExit("Quellvideo nicht gefunden (cockpit.json / .protect_main*)")
        dst = os.path.join(wd, "edit_intra_main.mp4")
        if os.path.exists(dst):
            print("[proxy] edit_intra_main.mp4 existiert — uebersprungen", flush=True)
        else:
            encode(src, dst, gop=1, extra_audio=["-c:a", "copy"], max_side=args.max_side,
                   hwaccel=args.hwaccel, crf=args.crf)
            fix_start_relation(src, dst)
            duration_gate(src, dst)

    if args.only in (None, "broll"):
        got = broll_sources(wd)
        if not got:
            print("[proxy] keine B-Roll-Quelle in broll_sync.json — ok", flush=True)
        for f, dst in got:
            if os.path.exists(dst):
                print(f"[proxy] {os.path.basename(dst)} existiert — uebersprungen", flush=True)
                continue
            encode(f, dst, gop=15, extra_audio=["-an"], crf=args.crf)
    print("[proxy] FERTIG", flush=True)


if __name__ == "__main__":
    main()
