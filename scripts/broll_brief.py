#!/usr/bin/env python3
"""Build a context-free production brief from a frozen, versioned contract.

The legacy CLI formatting below is only for old saved orders.
"""
import json, os, sys

def build_brief(doc):
    if doc.get("schema_version") != 2:
        raise ValueError("Production brief requires schema version 2")
    profile = doc["profile"]
    return "\n".join([
        "Produce SKAILE Shortform B-roll from the frozen request. You are the single broll-ersteller job.",
        "Read your shared role, then the versioned workflow and visual/SFX references before authoring.",
        "Profile: " + profile["path"] + " version " + profile["manifest"]["version"],
        "Frozen request: " + str(__import__("pathlib").Path(doc["workdir"]) / "broll_jobs" / doc["job_id"] / "auftrag.json"),
        "Result: " + doc["result_path"],
        "Snapshot ID: " + doc["snapshot_id"],
        "Include every selected clip, including hook and CTA. Their layout stays overhead.",
        "Use the exact cut_start/cut_end and word timestamps; no fixed five-second beats or generic speed-fit.",
        "Output schema_version 2, snapshot_id, audio_owner=separate_sfx, scenes and sfx_events.",
        "Scenes: id, clip_id, absolute cut start/end, layout overhead/fullscreen, file, sha256.",
        "Silent 1080x768 overhead or 1080x1920 fullscreen MP4. Scene duration equals end-start.",
        "SFX: id, clip_id, offset relative to clip video start, duration, file, sha256, gain_db <= 0, cues.",
        "Each cue: approved asset_id/source_sha256, offset within SFX file, duration, visible action.",
        "Use approved clean library audio only. No synthetic fallback, voice or music in SFX/video.",
        "Write complete atomic media and result.json. Do not modify cockpit settings or launch another agent job.",
        "No final export, no publication. Parent imports only after media validation and unchanged revision.",
        "Creative task: meaningful connected canvases, real logos and content, expressive camera moves; read profile details.",
        "Specific user request: " + doc.get("creative_request", ""),
        "Selected cut range: " + str(doc.get("selected_range")) + ". Clamp scenes and SFX to its intersection with each clip; full clip timestamps are context.",
        "Production intervals:",
        *[str(c["clip_idx"]) + " [" + c["role"] + "] " + str(c["cut_start"]) + "–" + str(c["cut_end"]) + "s: " + c["text"]
          for c in doc["clips"] if c["selected"]],
    ])


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    workdir = os.path.abspath(sys.argv[1])
    nur_offen = "--offen-nur" in sys.argv
    p = os.path.join(workdir, "broll_auftrag.json")
    if not os.path.exists(p):
        raise SystemExit("[broll_brief] broll_auftrag.json fehlt — erst 'B-Roll erstellen' druecken")
    with open(p, encoding="utf-8") as f:
        doc = json.load(f)

    if doc.get("schema_version") == 2:
        print(build_brief(doc))
        return
    ziel = os.path.join(workdir, "broll")
    auftraege = []
    for a in doc["auftraege"]:
        fertig = os.path.exists(os.path.join(workdir, a["out"]))
        if nur_offen and fertig:
            continue
        auftraege.append((a, fertig))

    if not auftraege:
        print("[broll_brief] nichts offen — alle Beats liegen schon in broll/")
        return

    zeilen = []
    zeilen.append(f"{len(auftraege)} B-Roll-Beats fuer das Reel '{doc['workdir']}' "
                  f"— einer je Clip, aus dem was Sebastian dort sagt.")
    zeilen.append("")
    zeilen.append(f"Zielordner: {ziel}")
    zeilen.append("")
    for a, fertig in auftraege:
        zeilen.append(f"{a['nr']:>2}. {a['out']}")
        zeilen.append(f"    \"{a['text']}\"")
        zeilen.append("")
    uebersprungen = doc.get("uebersprungen") or []
    if uebersprungen:
        zeilen.append("NICHT bauen (Sebastians Regel):")
        for u in uebersprungen:
            zeilen.append(f"  - Clip {u['clip_idx']} [{u['role']}]: {u['grund']}")
    print("\n".join(zeilen))


if __name__ == "__main__":
    main()
