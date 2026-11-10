"""Cached, sample-exact Shortform audio with one constant master gain.

No loudness normalizer, limiter or gate is used. Optional SFX are mixed after
voice gain, with constant attenuation only when needed for peak headroom.
Importing this module performs
no work; prepare_audio() is intended for the renderer or a background worker.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import subprocess

from cutter_provider import atomic_json, file_lock

SAMPLE_RATE = 48000
ALGORITHM = "sf-audio-presentation-pcm-v1"
DEFAULT_FADE = .03
DEFAULT_PEAK = -1.5


def _finite(value, name):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("%s must be finite" % name)
    return number


def _hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def source_identity(path):
    source = Path(path).expanduser().resolve()
    stat = source.stat()
    return {"path": str(source), "device": stat.st_dev, "inode": stat.st_ino,
            "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def timing_plan(clips, fps=60, pauses=None):
    """Use the same cumulative frame and sample rounding for picture and sound."""
    fps = _finite(fps, "fps")
    if fps <= 0:
        raise ValueError("fps must be positive")
    if any(i and c.get("jcutMode") == "manual" and float(c.get("jcut", 0) or 0) > .001
           for i, c in enumerate(clips)):
        return _manual_timing_plan(clips, fps, pauses)
    plan, source_total, start_frame = [], 0.0, 0
    for index, clip in enumerate(clips):
        a, b = _finite(clip["in"], "clip.in"), _finite(clip["out"], "clip.out")
        if a < 0 or b <= a:
            raise ValueError("Clip %d has invalid source boundaries" % index)
        source_total += b - a
        end_frame = round(source_total * fps)
        frames = end_frame - start_frame
        if frames <= 0:
            raise ValueError("Clip %d has no output frame" % index)
        start_sample = round(start_frame * SAMPLE_RATE / fps)
        end_sample = round(end_frame * SAMPLE_RATE / fps)
        samples = end_sample - start_sample
        duration = frames / fps
        gain = _finite(clip.get("gain", clip.get("gain_db", 0)) or 0, "clip.gain")
        fade_in = _finite(clip.get("fade_in", DEFAULT_FADE), "clip.fade_in")
        fade_out = _finite(clip.get("fade_out", DEFAULT_FADE), "clip.fade_out")
        fade_in, fade_out = min(duration, max(0., fade_in)), min(duration, max(0., fade_out))
        plan.append({"clip": clip, "index": index, "source_in": a, "source_out": b,
                     "source_duration": b - a, "start_frame": start_frame,
                     "end_frame": end_frame, "frames": frames, "start_sample": start_sample,
                     "end_sample": end_sample, "samples": samples, "duration": duration,
                     "cut_start": start_frame / fps, "cut_end": end_frame / fps,
                     "gain_db": gain, "fade_in": fade_in, "fade_out": fade_out})
        start_frame = end_frame
    if not plan:
        raise ValueError("The audio timeline is empty")
    return plan


def _manual_timing_plan(clips, fps, pauses):
    # Shared renderer geometry, never a second implementation of overlap limits.
    from rerender import apply_manual_jcut_edges
    segs = []
    for i, c in enumerate(clips):
        a, b = _finite(c["in"], "clip.in"), _finite(c["out"], "clip.out")
        if a < 0 or b <= a:
            raise ValueError("Clip %d has invalid source boundaries" % i)
        segs.append(dict(c, **{"in": a, "out": b}))
    apply_manual_jcut_edges(segs, pauses=pauses, fps=fps)
    plan = []
    for i, (c, seg) in enumerate(zip(clips, segs)):
        start_frame = round(seg["v_start"] * fps)
        frames = seg["v_frames"]
        if frames <= 0:
            raise ValueError("Clip %d has no output frame" % i)
        a_samples = round(seg["a_dur"] * SAMPLE_RATE)
        a_start = round(seg["a_start"] * SAMPLE_RATE)
        duration = frames / fps
        plan.append({"clip": c, "index": i, "manual_timeline": True,
                     "source_in": seg["v_in"], "source_out": seg["v_out"],
                     "source_duration": seg["v_out"] - seg["v_in"],
                     "start_frame": start_frame, "end_frame": start_frame + frames,
                     "frames": frames, "duration": duration,
                     "cut_start": start_frame / fps, "cut_end": (start_frame + frames) / fps,
                     "start_sample": a_start, "end_sample": a_start + a_samples,
                     "samples": a_samples, "audio_source_in": float(c["in"]),
                     "audio_source_out": float(c["out"]), "jcut": seg["jcut"],
                     "gain_db": _finite(c.get("gain", c.get("gain_db", 0)) or 0, "clip.gain"),
                     "fade_in": min(seg["a_dur"], max(0., _finite(c.get("fade_in", DEFAULT_FADE), "fade_in"))),
                     "fade_out": min(seg["a_dur"], max(0., _finite(c.get("fade_out", DEFAULT_FADE), "fade_out")))})
    return plan


def _audio_item(item):
    if not item.get("manual_timeline"):
        return item
    return dict(item, source_in=item["audio_source_in"], source_out=item["audio_source_out"],
                source_duration=item["audio_source_out"] - item["audio_source_in"])


def _overlap_pcm(paths, plan, target):
    # Sum exact source samples at explicit timeline offsets in bounded blocks.
    import numpy as np
    import soundfile as sf
    from contextlib import ExitStack
    total = max(item["end_sample"] for item in plan)
    with ExitStack() as stack:
        readers = [stack.enter_context(sf.SoundFile(p)) for p in paths]
        out = stack.enter_context(sf.SoundFile(target, "w", samplerate=SAMPLE_RATE,
                                               channels=1, subtype="FLOAT", format="WAV"))
        for start in range(0, total, 65536):
            end = min(total, start + 65536)
            block = np.zeros(end - start, dtype=np.float32)
            for reader, item in zip(readers, plan):
                lo, hi = max(start, item["start_sample"]), min(end, item["end_sample"])
                if hi > lo:
                    reader.seek(lo - item["start_sample"])
                    data = reader.read(hi - lo, dtype="float32")
                    if len(data) != hi - lo:
                        raise RuntimeError("Incomplete source audio segment")
                    block[lo-start:hi-start] += data
            out.write(block)
    return total


def resolve_gain(layout_audio=None, override_audio=None, peak_db=None):
    """Resolve the fixed mode immediately, or peak mode after measuring the cut."""
    layout_audio, override_audio = layout_audio or {}, override_audio or {}
    manual = min(0., _finite(override_audio.get("gain_db", 0) or 0, "audio.gain_db"))
    mode = "fixed" if layout_audio.get("mode") == "fixed" else "peak"
    # Legacy loudnorm's `peak` is not the new peak mode's target.
    target = min(0., _finite(layout_audio.get("target_peak_db", DEFAULT_PEAK), "target_peak_db"))
    if mode == "fixed":
        base = _finite(layout_audio.get("gain_db", 0) or 0, "fixed gain_db")
    else:
        # A completely silent PCM produces -inf. Its JSON representation is None.
        base = target - peak_db if peak_db is not None and math.isfinite(peak_db) else 0.
    return {"mode": mode, "base_gain_db": base, "manual_gain_db": manual,
            "gain_db": base + manual, "total_gain_db": base + manual,
            "target_peak_db": target, "peak_db": peak_db}


def _run(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Audio processing failed: " + result.stderr[-3000:])
    return result


def _complete(directory, key):
    try:
        doc = json.loads((directory / "complete.json").read_text())
        if doc.get("cache_key") == key and (directory / "audio.wav").is_file():
            return doc
    except (OSError, ValueError):
        pass
    return None


def _segment_spec(item):
    return {name: item[name] for name in
            ("source_in", "source_out", "samples", "gain_db", "fade_in", "fade_out")}


def _render_segment(source, item, target):
    samples = item["samples"]
    filters = ["aresample=48000:async=1:first_pts=0",
               "aformat=sample_fmts=flt:channel_layouts=mono",
               "atrim=end_sample=%d" % samples, "apad=whole_len=%d" % samples,
               "atrim=end_sample=%d" % samples, "asetpts=PTS-STARTPTS"]
    if item["gain_db"]:
        filters.append("volume=%.10fdB:precision=float" % item["gain_db"])
    fade_in = min(samples, round(item["fade_in"] * SAMPLE_RATE))
    fade_out = min(samples, round(item["fade_out"] * SAMPLE_RATE))
    if fade_in:
        filters.append("afade=t=in:ss=0:ns=%d" % fade_in)
    if fade_out:
        filters.append("afade=t=out:ss=%d:ns=%d" % (samples - fade_out, fade_out))
    _run(["ffmpeg", "-hide_banner", "-nostats", "-v", "error", "-y",
          "-ss", "%.10f" % item["source_in"], "-t", "%.10f" % item["source_duration"],
          "-i", source, "-map", "0:a:0", "-vn", "-sn", "-dn", "-af", ",".join(filters),
          "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_f32le", "-f", "wav", str(target)])


def _concat_pcm(paths, target):
    # Stream bounded float32 blocks, including WAVs longer than RAM can hold.
    import soundfile as sf
    samples = 0
    with sf.SoundFile(target, "w", samplerate=SAMPLE_RATE, channels=1, subtype="FLOAT", format="WAV") as out:
        for path in paths:
            with sf.SoundFile(path) as source:
                for block in source.blocks(blocksize=65536, dtype="float32"):
                    out.write(block)
                    samples += len(block)
    return samples


def _measure_peak(path):
    # 4x oversampling finds intersample peaks; astats retains sub-decibel precision.
    result = _run(["ffmpeg", "-hide_banner", "-nostats", "-v", "info", "-i", str(path),
                   "-map", "0:a:0", "-af", "aresample=192000:osf=flt,astats=metadata=0:reset=0",
                   "-f", "null", "-"])
    values = re.findall(r"Peak level dB:\s*(-?inf|[-+0-9.eE]+)", result.stderr)
    if not values:
        raise RuntimeError("Audio peak measurement returned no peak")
    peaks = [float(value) for value in values]
    finite = [peak for peak in peaks if math.isfinite(peak)]
    return max(finite) if finite else None


def _apply_gain(source, target, gain_db):
    import numpy as np
    import soundfile as sf
    factor = np.float32(10 ** (gain_db / 20.))
    if not np.isfinite(factor):
        raise ValueError("Audio gain cannot be represented as float32")
    with sf.SoundFile(source) as inp, sf.SoundFile(
            target, "w", samplerate=SAMPLE_RATE, channels=inp.channels, subtype="FLOAT", format="WAV") as out:
        for block in inp.blocks(blocksize=65536, dtype="float32"):
            block *= factor
            out.write(block)


def sfx_identity(workdir, layout_audio):
    """Resolve only the explicitly configured project SFX file."""
    value = (layout_audio or {}).get("sfx_file")
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(workdir) / path
    return source_identity(path)


def sfx_event_plan(workdir, layout_audio, plan):
    """Bind events to exact stable IDs; missing IDs are silent, including Undo gaps."""
    by_id = {item["clip"].get("id"): item for item in plan if item["clip"].get("id") is not None}
    events = []
    for event in (layout_audio or {}).get("sfx_events", []):
        item = by_id.get(event.get("clip_id"))
        if item is None or not event.get("file"):
            continue
        offset = _finite(event.get("offset", 0), "sfx.offset")
        if event.get("source_anchor") is not None:
            offset += _finite(event["source_anchor"], "sfx.source_anchor") - item["source_in"]
        skip = max(0, round(-offset * SAMPLE_RATE))
        start = max(0, round(offset * SAMPLE_RATE))
        available = round(item["duration"] * SAMPLE_RATE) - start
        if event.get("duration") is not None:
            available = min(available, round(_finite(event["duration"], "sfx.duration") * SAMPLE_RATE) - skip)
        if available <= 0:
            continue
        path = Path(event["file"]).expanduser()
        if not path.is_absolute():
            path = Path(workdir) / path
        events.append({"clip_id": event["clip_id"], "source": source_identity(path),
                       "source_sample": skip, "start_sample": round(item["cut_start"] * SAMPLE_RATE) + start,
                       "samples": available, "gain_db": _finite(event.get("gain_db", 0), "sfx.gain_db")})
    return events


def sfx_sources(workdir, layout_audio, clips, fps=60):
    # Server cache identity includes only active events, just like the renderer.
    return {"global": sfx_identity(workdir, layout_audio),
            "events": sfx_event_plan(workdir, layout_audio, timing_plan(clips, fps))}


def _mix_sfx(root, voice_path, voice_key, workdir, layout_audio, samples, plan):
    identity = sfx_identity(workdir, layout_audio)
    events = sfx_event_plan(workdir, layout_audio, plan)
    if identity is None and not events:
        return None
    sfx_gain = _finite(layout_audio.get("sfx_gain_db", 0), "sfx_gain_db")
    ceiling = min(-1., _finite(layout_audio.get("mix_peak_db", -1.), "mix_peak_db"))
    key = _hash({"algorithm": "sf-sfx-stereo-events-v2", "voice": voice_key,
                 "sfx": identity, "events": events, "sfx_gain_db": sfx_gain,
                 "ceiling": ceiling, "samples": samples})
    directory = root / "sfx_mix" / key
    directory.mkdir(parents=True, exist_ok=True)
    with file_lock(directory / ".lock"):
        meta = _complete(directory, key)
        if meta is None:
            part = directory / "sum.part.wav"
            inputs = []
            if identity:
                inputs.append({"source": identity, "source_sample": 0, "start_sample": 0,
                               "samples": samples, "gain_db": 0})
            inputs.extend(events)
            args = ["ffmpeg", "-v", "error", "-y", "-i", str(voice_path)]
            graph = ["[0:a]pan=stereo|c0=c0|c1=c0[v]"]
            for i, event in enumerate(inputs, 1):
                args.extend(["-i", event["source"]["path"]])
                graph.append(("[%d:a]aresample=48000,aformat=sample_fmts=flt:channel_layouts=stereo,"
                              "atrim=start_sample=%d:end_sample=%d,asetpts=PTS-STARTPTS,"
                              "volume=%.10fdB:precision=float,adelay=%dS:all=1,"
                              "apad=whole_len=%d,atrim=end_sample=%d[s%d]") %
                             (i, event["source_sample"], event["source_sample"] + event["samples"],
                              sfx_gain + event["gain_db"], event["start_sample"], samples, samples, i))
            graph.append("[v]" + "".join("[s%d]" % i for i in range(1, len(inputs)+1)) +
                         "amix=inputs=%d:duration=first:normalize=0:dropout_transition=0[m]" % (len(inputs)+1))
            _run(args + ["-filter_complex", ";".join(graph), "-map", "[m]", "-c:a", "pcm_f32le", str(part)])
            peak = _measure_peak(part)
            attenuation = min(0., ceiling - peak) if peak is not None else 0.
            final = directory / "audio.part.wav"
            _apply_gain(part, final, attenuation)
            if sfx_identity(workdir, layout_audio) != identity or sfx_event_plan(workdir, layout_audio, plan) != events:
                raise RuntimeError("SFX source changed during processing")
            final.replace(directory / "audio.wav")
            meta = {"cache_key": key, "sfx_source": identity, "sfx_events": events, "sfx_gain_db": sfx_gain,
                    "mix_gain_db": attenuation, "mix_peak_db": None if peak is None else peak + attenuation,
                    "mix_ceiling_db": ceiling}
            atomic_json(directory / "complete.json", meta)
    return dict(meta, processed_path=str(directory / "audio.wav"))


def voice_source_identity(workdir, src, layout_audio=None):
    """Resolve an opt-in, already presentation-aligned microphone recording.

    Bind it to its actual video so copying a layout cannot silently put a
    different recording under the picture. No source override means legacy audio.
    """
    settings = layout_audio or {}
    value = settings.get("source_file")
    if not value:
        return source_identity(src)
    root = Path(workdir).expanduser().resolve()
    def local(value):
        path = Path(value).expanduser()
        return path if path.is_absolute() else root / path
    aligned = settings.get("aligned_video")
    if not aligned or not local(aligned).samefile(Path(src).expanduser()):
        raise ValueError("Microphone audio is not bound to this source video")
    return source_identity(local(value))


def prepare_audio(workdir, src, clips, layout_audio=None, override_audio=None, fps=60, *, storage_dir=None):
    """Prepare the selected presentation timeline once, then resolve its gain.

    pcm_path includes clip gains and edge fades. processed_path additionally
    includes gain_db; callers must not apply that gain a second time.
    """
    workdir = Path(workdir).expanduser().resolve()
    source = voice_source_identity(workdir, src, layout_audio)
    from rerender import load_pause_map
    manual = any(i and c.get("jcutMode") == "manual" and float(c.get("jcut", 0) or 0) > .001
                 for i, c in enumerate(clips))
    plan = timing_plan(clips, fps, load_pause_map(str(workdir)) if manual else None)
    audio_plan = [_audio_item(item) for item in plan]
    root = (Path(storage_dir).expanduser().resolve() if storage_dir is not None else
            Path.home() / "Movies" / "_Sicherungen" / "Cutting" / _hash(str(workdir))[:16] / "audio")
    payload = {"algorithm": ALGORITHM, "source": source, "sample_rate": SAMPLE_RATE,
               "fps": float(fps), "segments": [_segment_spec(item) for item in audio_plan]}
    if manual:
        payload["overlap_starts"] = [item["start_sample"] for item in plan]
        payload["manual_algorithm"] = "sample-overlap-v1"
    pcm_key = _hash(payload)
    pcm_dir = root / "pcm" / pcm_key
    pcm_dir.mkdir(parents=True, exist_ok=True)
    with file_lock(pcm_dir / ".lock"):
        pcm_meta = _complete(pcm_dir, pcm_key)
        if pcm_meta is None:
            parts = []
            for item in audio_plan:
                segment_key = _hash({"algorithm": ALGORITHM, "source": source,
                                     "fps": float(fps), "segment": _segment_spec(item)})
                directory = root / "segments" / segment_key
                directory.mkdir(parents=True, exist_ok=True)
                with file_lock(directory / ".lock"):
                    if _complete(directory, segment_key) is None:
                        part = directory / "audio.part.wav"
                        _render_segment(source["path"], item, part)
                        part.replace(directory / "audio.wav")
                        atomic_json(directory / "complete.json", {"cache_key": segment_key,
                                    "samples": item["samples"], "spec": _segment_spec(item)})
                parts.append(directory / "audio.wav")
            part = pcm_dir / "audio.part.wav"
            samples = _overlap_pcm(parts, plan, part) if manual else _concat_pcm(parts, part)
            expected = max(item["end_sample"] for item in plan)
            if samples != expected:
                raise RuntimeError("Audio construction yielded %d samples instead of %d" % (samples, expected))
            peak = _measure_peak(part)
            if voice_source_identity(workdir, src, layout_audio) != source:
                raise RuntimeError("Audio source changed during processing")
            part.replace(pcm_dir / "audio.wav")
            pcm_meta = {"cache_key": pcm_key, "peak_db": peak, "samples": samples,
                        "frames": plan[-1]["end_frame"], "duration": samples / SAMPLE_RATE,
                        "source": source, "algorithm": ALGORITHM}
            atomic_json(pcm_dir / "complete.json", pcm_meta)
    gain = resolve_gain(layout_audio, override_audio, pcm_meta["peak_db"])
    processed_key = _hash({"algorithm": ALGORITHM, "pcm_key": pcm_key, "gain_db": gain["gain_db"]})
    pcm_path = pcm_dir / "audio.wav"
    processed_path = pcm_path
    if gain["gain_db"] != 0:
        directory = root / "gain" / processed_key
        directory.mkdir(parents=True, exist_ok=True)
        with file_lock(directory / ".lock"):
            if _complete(directory, processed_key) is None:
                part = directory / "audio.part.wav"
                _apply_gain(pcm_path, part, gain["gain_db"])
                part.replace(directory / "audio.wav")
                atomic_json(directory / "complete.json", {"cache_key": processed_key,
                            "pcm_cache_key": pcm_key, "gain_db": gain["gain_db"]})
        processed_path = directory / "audio.wav"
    result = {**gain, "pcm_path": str(pcm_path), "processed_path": str(processed_path),
            "cache_key": processed_key, "pcm_cache_key": pcm_key,
            "duration": pcm_meta["duration"], "samples": pcm_meta["samples"],
            "frames": pcm_meta["frames"], "fps": float(fps), "sample_rate": SAMPLE_RATE}
    mix = _mix_sfx(root, processed_path, processed_key, workdir, layout_audio or {}, pcm_meta["samples"], plan)
    if mix is not None:
        result.update(mix, voice_processed_path=str(processed_path), sfx_enabled=True)
    return result
