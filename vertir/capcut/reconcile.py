"""Pull a human's CapCut edits back into the IR — by patching, never re-parsing.

The tempting implementation reads the edited draft with `timeline` / `segments` /
`materials` and rebuilds an IR from it. That is wrong, and it is the whole reason
this module exists.

The IR carries **intent**; a draft carries only **result**:

    reframe {mode:"cover", focusX, focusY}   ->  a crop rect        (lost: "follow the face")
    source-anchored b-roll (via the cut-map) ->  a program timestamp (lost: the anchoring)
    duck {enabled, targetDb:-26}             ->  volume keyframes    (lost: the rule)

Rebuilding recovers positions and destroys the why, and the next LLM mutation
would then be operating on denormalised garbage.

So instead: we snapshot the draft as exported, ask `capcut diff` what the human
actually changed, and translate only those deltas onto the IR we already have.
Intent is never lost because the IR is never regenerated.

    IR --export--> draft@t0  (+ provenance)
                      |  [ human edits in CapCut ]
                   draft@t1
                      |  capcut diff draft@t0 draft@t1
                   deltas ---> applied ON TOP of the existing IR
"""
from __future__ import annotations

from typing import Any

from .. import edit as E
from .. import anim as A
from .. import ir as I
from .. import validate as V
from . import bridge
from .provenance import Provenance

# Segment fields the diff may report, and whether we know how to carry them back.
MAPPED_FIELDS = {"start_us", "duration_us", "speed", "volume", "opacity"}


def _db_from_volume(vol: float) -> float:
    """Linear multiplier -> dB. The inverse of the export's `_gain_to_volume`."""
    import math
    if vol <= 0:
        return -60.0
    return round(20 * math.log10(float(vol)), 2)


def _index_segments(draft_dir: str) -> dict[str, dict]:
    """Every segment in the draft, by id. Read-side values are microseconds —
    note the asymmetry with the compile spec, which writes seconds."""
    res = bridge.run(["segments", draft_dir])
    bridge.check_error(res, "capcut segments")
    rows = res["json"] or []
    if isinstance(rows, dict):
        rows = rows.get("segments", [])
    return {r["id"]: r for r in rows if isinstance(r, dict) and r.get("id")}


def _index_texts(draft_dir: str) -> dict[str, dict]:
    res = bridge.run(["texts", draft_dir])
    if not res["ok"]:
        return {}
    rows = res["json"] or []
    if isinstance(rows, dict):
        rows = rows.get("texts", [])
    return {r["id"]: r for r in rows if isinstance(r, dict) and r.get("id")}


def _find_clip(ir: dict, clip_id: str) -> tuple[dict | None, dict | None]:
    """(clip, track) for an IR clip id, searching every track."""
    for t in ir.get("tracks", []):
        for c in t.get("clips", []):
            if c.get("id") == clip_id:
                return c, t
    return None, None


# ------------------------------------------------------------------ delta application
def _apply_segment_delta(ir: dict, clip: dict, track: dict, fields: list[str],
                         after: dict, report: dict) -> None:
    applied: list[str] = []

    for field in fields:
        if field not in MAPPED_FIELDS:
            report["pinnedInCapCut"].append({
                "clip": clip["id"], "field": field,
                "reason": "the IR does not model this field; it stays in the draft "
                          "and is not managed here"})
            continue

        if field == "speed":
            clip["speed"] = float(after.get("speed", clip.get("speed", 1.0)))
            applied.append("speed")

        elif field == "volume":
            vol = float(after.get("volume", 1.0))
            if track.get("kind") == "audio":
                clip["gainDb"] = _db_from_volume(vol)
            else:
                clip.setdefault("audio", {})["gainDb"] = _db_from_volume(vol)
            applied.append("volume")

        elif field == "opacity":
            clip.setdefault("transform", {})["opacity"] = float(after.get("opacity", 1.0))
            applied.append("opacity")

        elif field == "duration_us":
            # A trim changes how much SOURCE is used; the program window is
            # derived, so we move the source out-point and let derive() redo the rest.
            src = clip.get("source")
            if not src:
                report["pinnedInCapCut"].append({
                    "clip": clip["id"], "field": field,
                    "reason": "clip has no source range to trim"})
                continue
            new_dur = int(after.get("duration_us", 0))
            speed = float(clip.get("speed", 1.0)) or 1.0
            src["endUs"] = src["startUs"] + int(round(new_dur * speed))
            applied.append("duration")
            # a shorter clip cannot keep keyframes past its new end: they would
            # fail validation and the whole reconcile would refuse to persist
            gone = A.drop_beyond(clip, new_dur)
            if gone:
                report["warnings"].append(
                    f"clip {clip['id']}: dropped {gone} keyframe(s) left outside "
                    "the trimmed clip")

        elif field == "start_us":
            if track.get("kind") == "audio" or clip.get("anchor") == "program":
                clip["atUs"] = int(after.get("start_us", 0))
                applied.append("start")
            else:
                # Main-track program starts are derived from clip order and
                # duration; a reorder is not a per-clip delta and re-deriving would
                # silently discard it.
                report["pinnedInCapCut"].append({
                    "clip": clip["id"], "field": field,
                    "reason": "main-track program start is engine-derived; reordering "
                              "in CapCut is not reconciled (change the plan instead)"})

    if applied:
        report["applied"].append({"clip": clip["id"], "fields": applied})


def _apply_text_delta(ir: dict, prov: Provenance, texts_after: dict,
                      report: dict) -> None:
    """Title cards are the only text the IR owns by identity; captions are
    generated from the transcript, so an edited caption is reported, not merged."""
    track = E.title_track_of(ir)
    for clip in (track or {}).get("clips", []):
        seg_id = prov.segment_for_ir(clip["id"])
        row = texts_after.get(seg_id) if seg_id else None
        if not row:
            continue
        new_text = row.get("text", "")
        if new_text and new_text != clip.get("text", {}).get("content"):
            clip.setdefault("text", {})["content"] = new_text
            report["applied"].append({"clip": clip["id"], "fields": ["text"]})


# ------------------------------------------------------------------ entry point
def reconcile(ir: dict, draft_dir: str, prov: Provenance, *,
              snapshot_dir: str | None = None) -> tuple[dict, dict]:
    """Patch `ir` with the human's edits to `draft_dir`. Returns (ir, report).

    Fail-closed: the patched IR must still validate, or the report says so and
    the caller must not persist it.
    """
    report: dict[str, Any] = {
        "ok": False, "changed": False,
        "applied": [], "pinnedInCapCut": [], "unknownSegments": [],
        "errors": [], "warnings": [],
    }

    baseline = snapshot_dir or prov.snapshot_path
    if not baseline:
        report["errors"].append(
            "no export snapshot recorded; reconcile needs the draft as exported "
            "to diff against. Re-run the export to produce one.")
        return ir, report

    d = bridge.diff(baseline, draft_dir)
    bridge.check_error(d, "capcut diff")
    delta = d["json"] or {}
    if not delta.get("changed"):
        report.update(ok=True, changed=False)
        return ir, report
    report["changed"] = True

    after = _index_segments(draft_dir)
    segs = delta.get("segments", {})

    for entry in segs.get("changed", []):
        seg_id = entry.get("id")
        clip_id = prov.ir_for_segment(seg_id)
        if not clip_id:
            report["unknownSegments"].append(
                {"segment": seg_id, "fields": entry.get("fields", []),
                 "reason": "segment was added in CapCut or is not in the provenance map"})
            continue
        clip, track = _find_clip(ir, clip_id)
        if clip is None:
            report["unknownSegments"].append(
                {"segment": seg_id, "clip": clip_id,
                 "reason": "provenance names an IR clip that no longer exists"})
            continue
        _apply_segment_delta(ir, clip, track, entry.get("fields", []),
                             after.get(seg_id, {}), report)

    for seg_id in segs.get("added", []):
        sid = seg_id.get("id") if isinstance(seg_id, dict) else seg_id
        report["pinnedInCapCut"].append({
            "segment": sid,
            "reason": "added in CapCut; it stays in the draft but the IR does not "
                      "manage it (a re-export would drop it)"})

    for seg_id in segs.get("removed", []):
        sid = seg_id.get("id") if isinstance(seg_id, dict) else seg_id
        clip_id = prov.ir_for_segment(sid)
        clip, track = _find_clip(ir, clip_id) if clip_id else (None, None)
        if clip is not None and track is not None:
            track["clips"] = [c for c in track["clips"] if c["id"] != clip_id]
            report["applied"].append({"clip": clip_id, "fields": ["removed"]})
        else:
            report["pinnedInCapCut"].append(
                {"segment": sid, "reason": "removed in CapCut but not found in the IR"})

    _apply_text_delta(ir, prov, _index_texts(draft_dir), report)

    if delta.get("materials", {}).get("changed"):
        report["warnings"].append(
            "materials changed in CapCut (filters, effects, styling). These are "
            "not modelled by the IR and are left in the draft untouched.")

    E.derive(ir)
    v = V.validate(ir)
    report["validation"] = v
    if not v["ok"]:
        report["errors"].append("the patched IR does not validate; do not persist it")
        return ir, report

    report["ok"] = True
    return ir, report


def reconcile_files(ir_path: str, draft_dir: str, provenance_path: str, *,
                    out_path: str | None = None) -> dict:
    """File-level wrapper: load, reconcile, and write the patched IR back."""
    doc = I.load(ir_path)
    E.derive(doc)
    prov = Provenance.load(provenance_path)
    doc, report = reconcile(doc, draft_dir, prov)
    if report["ok"] and report["changed"]:
        I.dump(doc, out_path or ir_path)
        report["irPath"] = out_path or ir_path
    return report
