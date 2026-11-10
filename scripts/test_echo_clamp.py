#!/usr/bin/env python3
"""Cross-System-Test Echo-Clamp (01.09.2026).

Beweist an ECHTEN Projektdaten, dass Player (index.html, jcutEffOf) und
Renderer (rerender.apply_jcut_edges) auf jeder Naht denselben effektiven
jcut errechnen — sonst klingt das Cockpit anders als der Render (die
teuerste Fehlerklasse dieses Systems).

1. Extrahiert die drei puren JS-Funktionen aus index.html und laesst sie
   in Node gegen pause_map + timeline_clips laufen.
2. Laesst apply_jcut_edges (Python) mit denselben Daten laufen.
3. Vergleicht jcut-effektiv pro Naht (Toleranz 1ms) + Invarianten:
   - a_dur == vdur_q + jcut (Ueberlapp-Semantik, kein Roll-Kappen)
   - nur DIREKTE Nachbarn ueberlappen
   - Telescoping: letzte a_start+a_dur == Summe der Bilddauern + jcut(1)? nein:
     max(a_end) == v_gesamt (Ton endet mit dem letzten Bild)

Aufruf: .venv312/bin/python scripts/test_echo_clamp.py work/yt1b
"""
import json
import os
import re
import subprocess
import sys

V = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(V, "scripts"))
import rerender as rr  # noqa: E402


def js_effective_jcuts(workdir):
    html = open(os.path.join(V, "scripts", "cockpit", "index.html")).read()

    def grab(name):
        m = re.search(r"(function %s\([^)]*\)\{.*?\n  \})" % name, html, re.S)
        if not m:
            raise SystemExit(f"JS-Funktion {name} nicht extrahierbar — Test anpassen!")
        return m.group(1)

    js = "\n".join([
        "var LCUT_EPS=0.001, LCUT_MIN_USEFUL=0.04, LCUT_SIL_MARGIN=0.02;",
        "var pausesFile=null, clips=[];",
        grab("initPauseMap"), grab("_pauseIdxBefore"),
        grab("tailSilenceAt"), grab("headSilenceAt"),
        grab("jcutOf"), grab("jcutEffOf"),
        """
        var fs=require('fs');
        var pm=JSON.parse(fs.readFileSync(process.argv[2]));
        var ovr=JSON.parse(fs.readFileSync(process.argv[3]));
        initPauseMap(pm);
        clips=ovr.timeline_clips.filter(function(c){return c.out-c.in>=0.04;})
          .map(function(c){return {inS:c.in, outS:c.out, jcut:+(c.jcut||0)};});
        if(clips.length) clips[0].jcut=0;
        var out=clips.map(function(c,i){return i===0?0:jcutEffOf(i);});
        console.log(JSON.stringify(out));
        """,
    ])
    tmp = "/tmp/echo_clamp_test.js"
    open(tmp, "w").write(js)
    res = subprocess.run(
        ["node", tmp, os.path.join(workdir, "pause_map.json"),
         os.path.join(workdir, "cockpit_overrides.json")],
        capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"Node-Lauf scheiterte:\n{res.stderr}")
    return json.loads(res.stdout)


def main():
    workdir = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "work/yt1b")
    ovr = json.load(open(os.path.join(workdir, "cockpit_overrides.json")))
    pauses = rr.load_pause_map(workdir)
    assert pauses, "pause_map.json fehlt — erst scripts/pause_map.py laufen lassen"

    # Renderer-Seite (identischer Aufbau wie der Timeline-Pfad in rerender.py)
    segs = []
    for tc in ovr["timeline_clips"]:
        if tc.get("out", 0) - tc.get("in", 0) < 0.04:
            continue
        segs.append({"in": round(float(tc["in"]), 4), "out": round(float(tc["out"]), 4),
                     "jcut": max(0.0, float(tc.get("jcut", 0) or 0))})
    if segs:
        segs[0]["jcut"] = 0.0
    rr.apply_jcut_edges(segs, pauses=pauses)

    # Player-Seite
    js_eff = js_effective_jcuts(workdir)
    assert len(js_eff) == len(segs), f"Clip-Zahl divergiert: js {len(js_eff)} vs py {len(segs)}"

    # 1) Effektive jcuts identisch?
    worst, n_diff = 0.0, 0
    for i, s in enumerate(segs):
        d = abs(float(s["jcut"]) - float(js_eff[i]))
        worst = max(worst, d)
        if d > 0.001:
            n_diff += 1
            if n_diff <= 5:
                print(f"  DIFF Naht {i}: renderer {s['jcut']} vs player {js_eff[i]}")
    print(f"[1] Player==Renderer eff-jcut: {len(segs)-n_diff}/{len(segs)} identisch, "
          f"max Abweichung {worst*1000:.2f}ms -> {'PASS' if n_diff==0 else 'FAIL'}")

    # 2) Ueberlapp-Semantik: a_dur == vdur_q + jcut (kein Roll-Kappen)
    bad = [i for i, s in enumerate(segs)
           if abs(s["a_dur"] - ((s["v_out"] - s["v_in"]) + s["jcut"])) > 1e-4]
    print(f"[2] a_dur = Bilddauer + jcut (voller Ton): "
          f"{'PASS' if not bad else 'FAIL bei ' + str(bad[:5])}")

    # 3) Nur direkte Nachbarn ueberlappen
    bad3 = []
    for i in range(2, len(segs)):
        if segs[i]["a_start"] < segs[i-2]["a_start"] + segs[i-2]["a_dur"] - 1e-4:
            bad3.append(i)
    print(f"[3] Nur Nachbar-Ueberlappung: {'PASS' if not bad3 else 'FAIL bei ' + str(bad3[:5])}")

    # 4) Telescoping: Ton endet mit dem letzten Bild
    v_total = sum(s["v_out"] - s["v_in"] for s in segs)
    a_end = max(s["a_start"] + s["a_dur"] for s in segs)
    print(f"[4] Ton-Ende == Bild-Ende: diff {abs(a_end-v_total)*1000:.2f}ms -> "
          f"{'PASS' if abs(a_end-v_total) < 0.002 else 'FAIL'}")

    # Zusammenfassung Clamp-Wirkung
    wish = [max(0.0, float(tc.get("jcut", 0) or 0)) for tc in ovr["timeline_clips"]][:len(segs)]
    n_wish = sum(1 for x in wish if x > 1e-3)
    n_eff = sum(1 for s in segs if s["jcut"] > 1e-3)
    print(f"[i] L-Cuts: {n_wish} gewuenscht, {n_eff} effektiv aktiv, "
          f"{n_wish-n_eff} Echo-geklemmt")
    ok = n_diff == 0 and not bad and not bad3 and abs(a_end - v_total) < 0.002
    print("=> GESAMT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
