"""Shared Shortform settings, revisions and deterministic text artwork."""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from PIL import Image, ImageDraw, ImageFont
import caption_style

DEFAULT_LAYOUT = {
    "video_yoff": 0,
    "card": {"w": 1316, "h": 740, "x": -118, "y": 0},
    "title": {}, "pill": {}, "hook": {},
    "audio": {"mode": "peak", "target_peak_db": -1.5},
    "caption": {"y_body": 787, "y_full": 1205},
}
_LOCK = threading.RLock()


class RevisionConflict(Exception):
    """The browser is editing an older project revision."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read_json(path, fallback):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return deepcopy(fallback)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


@contextmanager
def project_lock(workdir):
    with _LOCK:
        path = Path(workdir) / ".sf-settings.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def resolve_layout(layout):
    out = deepcopy(DEFAULT_LAYOUT)
    for key, value in (layout or {}).items():
        if key in {"card", "audio", "caption"} and isinstance(value, dict):
            out[key].update(deepcopy(value))
        else:
            out[key] = deepcopy(value)
    return out


def _load(workdir):
    root = Path(workdir)
    value = {
        "overrides": read_json(root / "cockpit_overrides.json", {}),
        "layout": resolve_layout(read_json(root / "sf_layout.json", {})),
        "captions": read_json(root / "captions_sf.json", []),
    }
    value["revision"] = digest(value)
    return value


def load_settings(workdir):
    with project_lock(workdir):
        return _load(workdir)


def save_settings(workdir, payload):
    """Save all editable layers before acknowledging a single revision."""
    root = Path(workdir)
    with project_lock(root):
        current = _load(root)
        expected = payload.get("revision")
        if expected and expected != current["revision"]:
            atomic_json(root / "backups" / ("sf-conflict-" + uuid.uuid4().hex + ".json"), payload)
            raise RevisionConflict("Dieser Projektstand wurde zwischenzeitlich geändert. Bitte neu laden.")
        ovr = deepcopy(current["overrides"])
        incoming = payload.get("overrides")
        if isinstance(incoming, dict):
            ovr.update(incoming)
            ovr.pop("base_mtime", None)
        caps = payload.get("captions", current["captions"])
        if not isinstance(caps, list):
            raise ValueError("captions must be a list")
        layout = resolve_layout(payload.get("layout", current["layout"]))
        next_value = {"overrides": ovr, "layout": layout, "captions": caps}
        if digest(next_value) != current["revision"]:
            stamp = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
            atomic_json(root / "backups" / "sf_settings" / (stamp + ".json"), current)
            # Keep a write-ahead transaction for recovery after a process interruption.
            atomic_json(root / ".sf-settings-transaction.json", next_value)
            atomic_json(root / "cockpit_overrides.json", ovr)
            atomic_json(root / "captions_sf.json", caps)
            atomic_json(root / "sf_layout.json", layout)
            atomic_json(root / ".sf-settings-transaction.json", {"committed": digest(next_value)})
        result = _load(root)
        overrides_path = root / "cockpit_overrides.json"
        result["mtime"] = overrides_path.stat().st_mtime if overrides_path.exists() else None
        return result


def recover_settings(workdir):
    """Finish only our own interrupted save; never rewrite ordinary legacy edits."""
    root = Path(workdir)
    with project_lock(root):
        pending = read_json(root / ".sf-settings-transaction.json", {})
        if all(k in pending for k in ("overrides", "layout", "captions")):
            atomic_json(root / "cockpit_overrides.json", pending["overrides"])
            atomic_json(root / "sf_layout.json", pending["layout"])
            atomic_json(root / "captions_sf.json", pending["captions"])
            atomic_json(root / ".sf-settings-transaction.json", {"committed": digest(pending)})


def init_project(workdir):
    """Create a text-free Shortform layout without copying another project."""
    root = Path(workdir)
    with project_lock(root):
        if not (root / "sf_layout.json").exists():
            atomic_json(root / "sf_layout.json", DEFAULT_LAYOUT)
        if not (root / "captions_sf.json").exists():
            atomic_json(root / "captions_sf.json", [])
        if not (root / "cockpit_overrides.json").exists():
            atomic_json(root / "cockpit_overrides.json", {})
        overrides = read_json(root / "cockpit_overrides.json", {})
        segments = read_json(root / "segments_v5_repaired.json", {}).get("segments", [])
        # Seed a genuinely new project once. Existing edits, including an
        # explicitly empty timeline, always remain authoritative.
        if not overrides and segments:
            overrides["timeline_clips"] = [
                {"id": str(segment.get("first_id", index)),
                 "in": float(segment["in"]), "out": float(segment["out"]),
                 "gain": 0, "sat": 5, "exp": -5}
                for index, segment in enumerate(segments)
            ]
            atomic_json(root / "cockpit_overrides.json", overrides)
    return load_settings(root)


def legacy_layers(layout, clips):
    duration = sum(max(0, float(c["out"]) - float(c["in"])) for c in clips)
    hook_end = max(0, float(clips[0]["out"]) - float(clips[0]["in"])) if clips else 0
    out = []
    for kind in ("title", "pill"):
        layer = (layout or {}).get(kind) or {}
        if (layer.get("lines") if kind == "title" else layer.get("text")):
            start = 0 if kind == "title" else max(0, duration - float(layer.get("from_end_s", 2.34)))
            end = min(duration, float(layer.get("show_s", 2.33))) if kind == "title" else duration
            out.append({"id": "layout-" + kind, "kind": kind, "start": start, "end": end, "layer": layer})
    for kind, layer in ((layout or {}).get("hook") or {}).items():
        if isinstance(layer, dict) and layer.get("file"):
            out.append({"id": "layout-hook-" + kind, "kind": "hook", "key": kind,
                        "start": 0, "end": hook_end, "layer": layer})
    return out


def text_bitmap(kind, layer, layout=None):
    """The exact full-size transparent artwork used by preview and export."""
    width, height = 1080, 1920
    layout = resolve_layout(layout)
    if kind == "caption":
        style = caption_style.load_style(layout)
        style.update(layer.get("style") or {})
        x = round(float(layer.get("x", .5)) * width)
        y = round(float(layer.get("y", 1205 / height)) * height)
        return caption_style.draw_caption(width, height, str(layer.get("text", "")).split(),
                                         int(layer.get("hot_idx", -1)), int(layer.get("size", 62)),
                                         style, x, y)
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(layer.get("font", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
                             int(layer.get("size", 69 if kind == "title" else 66)))
    if kind == "title":
        y = int(layer.get("y", 268))
        for line in layer.get("lines", []):
            tw = draw.textlength(line, font=font)
            draw.text(((width - tw) / 2, y), line, font=font, fill=(0, 0, 0, 255),
                      stroke_width=8, stroke_fill=(255, 255, 255, 255))
            y += int(layer.get("line_h", 93))
    elif kind == "pill":
        txt = layer.get("text", "")
        if txt:
            tw = draw.textlength(txt, font=font)
            asc, desc = font.getmetrics()
            cx, cy = width / 2, int(layer.get("y", 300))
            draw.rounded_rectangle([cx-tw/2-46, cy-(asc+desc)/2-26,
                                    cx+tw/2+46, cy+(asc+desc)/2+26], radius=30,
                                   fill=(255, 255, 255, 255))
            draw.text((cx-tw/2, cy-(asc+desc)/2), txt, font=font, fill=(0, 0, 0, 255))
    else:
        raise ValueError("Unknown text layer")
    return image


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3 or sys.argv[1] != "--init":
        raise SystemExit("Usage: sf_project.py --init WORKDIR")
    print(json.dumps(init_project(sys.argv[2]), ensure_ascii=False))
