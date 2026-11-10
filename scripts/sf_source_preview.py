"""Read-only identity for an already aligned microphone recording.

The browser assembles its cut preview in memory. Resolving playback must never
write media, launch ffmpeg, or depend on spare disk space. Export retains the
existing sf_audio pipeline and its final-timeline normalization.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import sf_audio

POLICY = "stable-during-edit"


def video_for_key(workdir, src, audio, key, playback_map=None):
    """Retired full-video mux route; never build or expose duplicate media."""
    return None


def source_key(identity):
    return sf_audio._hash({"algorithm": "sf-source-preview-v1", "source": identity})


def source_for_key(workdir, src, audio, key):
    """Resolve on every request; stale URLs never serve replacement bytes."""
    if not audio.get("source_file"):
        return None
    identity = sf_audio.voice_source_identity(workdir, src, audio)
    return identity["path"] if source_key(identity) == key else None


def _cached(path, key):
    # Read only one tiny existing manifest. Missing/corrupt cache is harmless:
    # the browser measures the selected PCM in memory instead.
    try:
        if path.stat().st_size > 16384:
            return None
        result = json.loads(path.read_text())
        if result.get("key") == key and math.isfinite(float(result["base_gain_db"])):
            return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def state(workdir, src, audio, overrides, start=False, playback_map=None):
    """Return immediately, independent of cut edits, proxies and disk capacity."""
    try:
        if not audio.get("source_file"):
            raise ValueError("No aligned microphone source configured")
        identity = sf_audio.voice_source_identity(workdir, src, audio)
        resolved = sf_audio.resolve_gain(audio, (overrides or {}).get("audio"))
        norm = {"mode": resolved["mode"], "target_peak_db": resolved["target_peak_db"],
                "fixed_gain_db": resolved["base_gain_db"] if resolved["mode"] == "fixed" else None}
    except (OSError, ValueError, TypeError) as error:
        return {"status": "failed", "ready": False, "direct_source": True,
                "preview_gain_policy": POLICY, "error": str(error)}
    skey = source_key(identity)
    key = sf_audio._hash({"source_key": skey, "normalization": norm, "policy": POLICY})
    path = Path(workdir).resolve() / ".sf-source-preview" / (key + ".json")
    cached = _cached(path, key)
    base = float(cached["base_gain_db"]) if cached else resolved["base_gain_db"]
    manual = resolved["manual_gain_db"]
    return {"status": "ready", "ready": True, "direct_source": True,
            "native_mic": False, "preview_gain_policy": POLICY,
            "source_key": skey, "source_url": "/media/sf-source/%s.wav" % skey,
            "key": key, "base_gain_db": base, "manual_gain_db": manual,
            "total_gain_db": base + manual, "gain_db": base + manual,
            "gain_cached": cached is not None or resolved["mode"] == "fixed",
            "mode": resolved["mode"], "target_peak_db": resolved["target_peak_db"],
            "normalization": norm}
