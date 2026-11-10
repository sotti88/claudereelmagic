"""Pure, conservative raw-Cockpit to rendered-frame screen-slot mapping.

Call before processing overrides in broll_sync_pass, then keep the returned
objects in memory. Never persist the returned overrides to the live Cockpit.
Unsupported/ambiguous input returns the original objects and an audit reason.
No source discovery, media probing, or resurrection of absent slots occurs.
"""
from copy import deepcopy
import math

FPS = 30
EPS = 0.000002  # Serialized effective edges have six decimal places.


class _Ineligible(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise _Ineligible(reason)


def _number(value):
    _require(not isinstance(value, bool), "non_numeric_timing")
    value = float(value)
    _require(math.isfinite(value), "non_finite_timing")
    return value


def frame_timed_screen_slots(cfg, overrides, effective):
    """Return (render_cfg, render_overrides, audit), without changing inputs.

    audit.applied is false for legacy, free/shifted, J-cut or ambiguous projects.
    Full main-clip edges follow its quantized frame count. Partial slot/window
    edges move inward to whole frames, retaining source offsets and removals.
    """
    try:
        return _retime(cfg, overrides, effective)
    except (_Ineligible, KeyError, TypeError, ValueError, OverflowError) as error:
        return cfg, overrides, {"applied": False, "reason": str(error)}


def _retime(cfg, overrides, effective):
    _require(all(isinstance(obj, dict) for obj in (cfg, overrides, effective)), "invalid_input_objects")
    _require(cfg.get("mirror_sources") is False, "requires_materialized_sources")
    _require(not cfg.get("exact_screen_frames"), "already_frame_timed")
    clips, segs = overrides["timeline_clips"], effective["segments"]
    slots, sources = overrides["broll"], cfg["sources"]
    _require(isinstance(clips, list) and isinstance(segs, list)
             and clips and len(clips) == len(segs), "timeline_segment_mismatch")
    _require(isinstance(slots, list) and isinstance(sources, list), "invalid_slots_or_sources")
    _require(not overrides.get("broll_sync_off") and not overrides.get("broll_sync_holes"),
             "additional_sync_exclusions")
    rows, raw, cursor = [], 0.0, 0
    for clip, seg in zip(clips, segs):
        _require(isinstance(clip, dict) and isinstance(seg, dict), "invalid_segment_object")
        _require(not _number(clip.get("jcut", 0)) and not _number(seg.get("jcut", 0)), "jcut")
        a, b = _number(clip["in"]), _number(clip["out"])
        _require(b > a and a >= 0, "invalid_clip_edges")
        _require(abs(a - _number(seg["in"])) <= EPS and
                 abs(b - _number(seg["out"])) <= EPS, "different_segment_edges")
        va, vb, vs = (_number(seg[k]) for k in ("v_in", "v_out", "v_start"))
        count = round((vb - va) * FPS)
        _require(count > 0 and abs((vb - va) * FPS - count) <= EPS * FPS
                 and abs(vs * FPS - cursor) <= EPS * FPS, "invalid_frame_schedule")
        _require(abs(va - a) <= EPS and count == round((b - a) * FPS), "retimed_main_segment")
        if "v_frames" in seg:
            _require(_number(seg["v_frames"]) == count, "different_frame_count")
        rows.append((raw, raw + b - a, cursor, cursor + count, va))
        raw += b - a
        cursor += count

    result, mapped, audit_rows = deepcopy(overrides), [], []
    occupied = []
    for index, old in enumerate(slots):
        _require(isinstance(old, dict), "invalid_slot_object")
        _require(old.get("kind") == "screen", "non_screen_slot")
        _require(_number(old.get("shift", 0)) == 0, "shifted_slot")
        sa, sb, anchor, src_in = (_number(old[k]) for k in ("start", "end", "anchor", "src_in"))
        _require(sa >= -EPS and sb <= raw + EPS and sb > sa and src_in >= -EPS,
                 "slot_outside_timeline")
        _require(all(sb <= a + EPS or sa >= b - EPS for a, b in occupied), "overlapping_slots")
        occupied.append((sa, sb))
        offset = anchor - src_in
        if "source_offset" in old:
            _require(abs(_number(old["source_offset"]) - offset) <= EPS, "slot_offset_mismatch")
        pieces = 0
        for ra, rb, fa, fb, va in rows:
            lo, hi = max(sa, ra), min(sb, rb)
            if hi - lo <= EPS:
                continue
            expected_anchor = va + lo - ra
            _require(abs(anchor + lo - sa - expected_anchor) <= EPS, "slot_anchor_mismatch")
            candidates = []
            for source in sources:
                _require(isinstance(source, dict), "invalid_source_object")
                if source.get("file") != old.get("file") or not old.get("file"):
                    continue
                if abs(_number(source["src_offset"]) - offset) > EPS:
                    continue
                window = source.get("window")
                _require(isinstance(window, (list, tuple)) and len(window) == 2, "missing_source_window")
                wa, wb = map(_number, window)
                _require(wb > wa, "invalid_source_window")
                if wa - EPS <= expected_anchor and expected_anchor + hi - lo <= wb + EPS:
                    candidates.append((source, wa, wb))
            _require(len(candidates) == 1, "ambiguous_source_window")
            source, wa, wb = candidates[0]
            if "source_name" in old:
                _require(old["source_name"] == source.get("name"), "source_name_mismatch")
            start = fa if abs(lo - ra) <= EPS else fa + math.ceil((lo - ra) * FPS - EPS * FPS)
            end = fb if abs(hi - rb) <= EPS else fa + math.floor((hi - ra) * FPS + EPS * FPS)
            # Even a rounded full-clip edge must stay inside the source window.
            start = max(start, fa + math.ceil((max(wa, offset) - va) * FPS - EPS * FPS))
            end = min(end, fa + math.floor((wb - va) * FPS + EPS * FPS))
            _require(end > start, "subframe_slot_fragment")
            new_anchor = va + (start - fa) / FPS
            slot = deepcopy(old)
            slot.update(start=start / FPS, end=end / FPS, anchor=new_anchor,
                        src_in=new_anchor - offset)
            mapped.append(slot)
            audit_rows.append({"old_slot": index, "start_frame": start, "end_frame": end,
                               "frames": end - start, "start_shift_ms": (start / FPS - lo) * 1000})
            pieces += 1
        _require(pieces > 0, "unmapped_slot")
    result["broll"] = mapped
    render_cfg = deepcopy(cfg)
    render_cfg.update(exact_screen_frames=True, force_two_pass=True)
    return render_cfg, result, {
        "applied": True, "reason": "materialized_screen_slots_on_effective_frame_grid",
        "scope": "render_only", "total_frames": cursor, "input_slots": len(slots),
        "screen_pieces": len(mapped), "mapping": audit_rows,
        "max_start_shift_ms": max((abs(row["start_shift_ms"]) for row in audit_rows), default=0),
    }
