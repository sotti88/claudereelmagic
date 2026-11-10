#!/usr/bin/env python3
"""Read current B-roll orders; schema 2 production is created by broll_contract.

Historical schema 1 jobs retain their source-lineage mapping for existing media.
New preview orders include hook/CTA and use actual speech duration.
"""
import json, os, re, subprocess, sys, datetime
from pathlib import Path
from cutter_provider import atomic_json, file_lock

HERE = os.path.dirname(os.path.abspath(__file__))
BEAT_DUR = 5.0


def job_snapshot(workdir):
    """Read the immutable file-to-clip mapping, never regenerate a running job."""
    jobs = []
    for path in (Path(workdir) / "broll_jobs").glob("*/job.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        jobs.append((doc, path.parent))
    if not jobs:
        return None
    active = [item for item in jobs if item[0].get("status") in ("queued", "running")]
    job, directory = max(active or jobs, key=lambda item: item[0].get("createdAt", 0))
    path = directory / "auftrag.json"
    if not path.exists():
        raise ValueError("B-Roll-Job %s hat keinen eingefrorenen Clip-Auftrag. "
                         "Zuordnung aus dem Start-Snapshot wiederherstellen; "
                         "Dateinummern nicht aus der aktuellen Timeline ableiten." % job["id"])
    return json.loads(path.read_text(encoding="utf-8"))


def freeze_for_job(workdir, job, doc):
    """Legacy compatibility only. Version 2 freezes before enqueue launch."""
    path = Path(workdir) / "broll_jobs" / job["id"] / "auftrag.json"
    frozen = dict(doc, job_id=job["id"], snapshot_version=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.parent / ".auftrag.lock"):
        if path.exists():
            return
        if job.get("duplicate"):
            raise ValueError("Bestehender B-Roll-Job ohne Start-Snapshot; nicht neu zuordnen")
        atomic_json(path, frozen)


def _lineage(clip_id):
    return re.sub(r"(?:#s\d+)+$", "", str(clip_id))


def rebind_snapshot(workdir, frozen):
    """Follow trimmed/split descendants, retaining the produced filename.

    An index is not identity. A changed take (even with a reused ID) is not
    eligible, and an ambiguous source overlap is left for an explicit new job.
    """
    path = Path(workdir) / "cockpit_overrides.json"
    clips = json.loads(path.read_text(encoding="utf-8")).get("timeline_clips") or []
    orders, skipped, unmapped = [], [], []
    t = 0.0
    for idx, clip in enumerate(clips):
        start, end = float(clip["in"]), float(clip["out"])
        dur = round(end - start, 4)
        cut_start, cut_end = round(t, 4), round(t + dur, 4)
        t += dur
        if idx == 0 or idx == len(clips) - 1:
            role = "hook" if idx == 0 else "cta"
            skipped.append({"clip_idx": idx, "role": role, "dur": dur,
                            "grund": "First-Frame-Image statt B-Roll" if role == "hook" else "nur Text (Call-to-Action)",
                            "text": ""})
            continue
        matches = []
        for order in frozen.get("auftraege", []):
            if _lineage(order.get("clip_id")) != _lineage(clip.get("id")):
                continue
            if "src_in" not in order or "src_out" not in order:
                continue
            a, b = float(order["src_in"]), float(order["src_out"])
            # One frame of edge padding is harmless; newly included speech is not.
            if start >= a - 1 / 30 and end <= b + 1 / 30 and min(end, b) > max(start, a):
                matches.append(order)
        exact = [order for order in matches if order.get("clip_id") == clip.get("id")]
        matches = exact or matches
        if len(matches) != 1:
            unmapped.append({"clip_id": clip.get("id"), "clip_idx": idx,
                             "reason": "new_or_changed_take" if not matches else "ambiguous_lineage"})
            continue
        original = matches[0]
        orders.append(dict(original, original_clip_id=original["clip_id"],
                           clip_id=clip.get("id"), clip_idx=idx, clip_dur=dur,
                           cut_start=cut_start, cut_end=cut_end,
                           speed_fit=round(float(frozen.get("beat_dur", BEAT_DUR)) / dur, 4)))
    return dict(frozen, clip_count=len(clips), auftrag_count=len(orders),
                auftraege=orders, uebersprungen=skipped, unmapped=unmapped)


def clips_lesen(workdir):
    """clips.json holen; wenn es fehlt oder veraltet ist, neu erzeugen."""
    subprocess.run([sys.executable, os.path.join(HERE, "clip_register.py"), workdir],
                   check=True, capture_output=True, text=True)
    with open(os.path.join(workdir, "clips.json"), encoding="utf-8") as f:
        return json.load(f)


def bauen(workdir, status_only=False):
    frozen = job_snapshot(workdir)
    if frozen is not None:
        if frozen.get("schema_version") == 2:
            # New contracts never silently remap onto edited clips.
            return frozen
        return rebind_snapshot(workdir, frozen)
    if status_only:
        # Polling does not rewrite clips.json or launch a Python child process.
        from clip_register import build
        reg = build(workdir)
    else:
        reg = clips_lesen(workdir)
    name = reg["workdir"]
    auftraege, uebersprungen = [], []
    nr = 0
    for c in reg["clips"]:
        nr += 1
        auftraege.append({
            "nr": nr,
            "clip_idx": c["idx"],
            "clip_id": c["id"],
            "src_in": c["src_in"],
            "src_out": c["src_out"],
            "clip_dur": c["dur"],
            "cut_start": c["cut_start"],
            "cut_end": c["cut_end"],
            "speed_fit": 1,
            "role": c["role"],
            "duration": c["dur"],
            "text": c["text"],
            "out": f"broll/{name}-{nr:02d}.mp4",
            "status": "offen",
        })
    return {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "workdir": name,
        "beat_dur": None,
        "clip_count": reg["clip_count"],
        "auftrag_count": len(auftraege),
        "auftraege": auftraege,
        "uebersprungen": uebersprungen,
    }


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    workdir = os.path.abspath(sys.argv[1])
    status_only = "--status" in sys.argv
    try:
        doc = bauen(workdir, status_only=status_only)
    except ValueError as error:
        raise SystemExit(str(error))
    p = os.path.join(workdir, "broll_auftrag.json")
    if not status_only and "job_id" not in doc:
        tmp = p + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        os.makedirs(os.path.join(workdir, "broll"), exist_ok=True)

    if "--json" in sys.argv:
        print(json.dumps(doc, ensure_ascii=False))
        return
    print(f"[broll_auftrag] {doc['auftrag_count']} Auftraege aus "
          f"{doc['clip_count']} Clips -> broll_auftrag.json")
    for a in doc["auftraege"]:
        print(f"  {a['nr']:>2}. Clip {a['clip_idx']:>2}  {a['clip_dur']:>5.2f}s  "
              f"Speed {a['speed_fit']:>4.2f}x  {a['text'][:52]}")
    for u in doc["uebersprungen"]:
        print(f"   -  Clip {u['clip_idx']:>2}  {u['dur']:>5.2f}s  "
              f"[{u['role']}] {u['grund']}")


if __name__ == "__main__":
    main()
