"""Editing operations that mutate the IR: filler cutting, the cut-map that
resolves source->program anchoring, and caption generation.

Everything here is deterministic and pure-ish: it reads a transcript + IR and
returns/edits IR dicts. The renderer never runs any of this.
"""
from __future__ import annotations

from typing import Any

from . import ir as I
from . import transcript as T


# --------------------------------------------------------------- filler cutting
def kept_segments(words: list[dict], max_gap_us: int = 450000,
                  fillers: set[str] | None = None,
                  pad_us: int = 60000, min_seg_us: int = 200000) -> list[tuple[int, int]]:
    """Return [(sourceStart, sourceEnd)] speech segments with fillers + long
    silences removed. A new segment starts whenever the gap between kept words
    exceeds `max_gap_us`."""
    kept = [w for w in words if not T.is_filler(w["text"], fillers)]
    segs: list[list[int]] = []
    for w in kept:
        if segs and (w["sourceAtUs"] - segs[-1][1]) <= max_gap_us:
            segs[-1][1] = w["sourceEndUs"]
        else:
            segs.append([w["sourceAtUs"], w["sourceEndUs"]])
    # pad, clamp, drop tiny slivers, merge if padding caused overlap
    out: list[tuple[int, int]] = []
    for s, e in segs:
        s = max(0, s - pad_us)
        e = e + pad_us
        if e - s < min_seg_us:
            continue
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def cut_fillers(ir: dict, transcript: dict, asset_id: str,
                max_gap_us: int = 450000, fillers: set[str] | None = None,
                focus_x: float = 0.5, focus_y: float = 0.4,
                reframe_mode: str = "cover", pad_us: int = 60000,
                min_seg_us: int = 200000) -> list[tuple[int, int]]:
    """Set the main track's clips to the kept speech segments of `transcript`."""
    asset = ir["assets"].get(asset_id)
    dur = asset.get("probe", {}).get("durationUs") if asset else None
    segs = kept_segments(transcript["words"], max_gap_us, fillers,
                         pad_us=pad_us, min_seg_us=min_seg_us)
    if dur:
        segs = [(s, min(e, dur)) for s, e in segs if s < dur]
    track = I.main_track(ir)
    track["clips"] = [
        I.main_clip(asset_id, s, e, reframe_mode, focus_x, focus_y)
        for s, e in segs
    ]
    derive(ir)
    return segs


# --------------------------------------------------------------- cut-map / anchoring
def build_cut_map(ir: dict) -> list[dict]:
    """Derive the program timeline from the main track. Returns entries
    {srcStartUs, srcEndUs, progStartUs, progEndUs} in program order."""
    track = I.main_track(ir)
    cmap: list[dict] = []
    prog = 0
    for clip in track["clips"]:
        s, e = clip["source"]["startUs"], clip["source"]["endUs"]
        dur = int(round((e - s) / clip.get("speed", 1.0)))
        cmap.append({"clipId": clip["id"], "srcStartUs": s, "srcEndUs": e,
                     "progStartUs": prog, "progEndUs": prog + dur})
        prog += dur
    return cmap


def derive(ir: dict) -> dict:
    """Engine-derived fields: per-clip timeline windows + project.durationUs."""
    cmap = build_cut_map(ir)
    track = I.main_track(ir)
    for clip, ent in zip(track["clips"], cmap):
        clip["timeline"] = {"startUs": ent["progStartUs"], "endUs": ent["progEndUs"]}
    ir["project"]["durationUs"] = cmap[-1]["progEndUs"] if cmap else 0
    return ir


def source_to_program(cmap: list[dict], source_us: int) -> int | None:
    for ent in cmap:
        if ent["srcStartUs"] <= source_us < ent["srcEndUs"]:
            return ent["progStartUs"] + (source_us - ent["srcStartUs"])
    return None


# --------------------------------------------------------------- captions
def captions_from_transcript(ir: dict, transcript: dict,
                             max_words_per_line: int | None = None,
                             style: dict | None = None) -> dict:
    """Build a source-anchored word-highlight caption track from kept words
    (fillers already excluded), grouped into lines. Adds it to the IR."""
    st = style or I.caption_style()
    mw = max_words_per_line or st.get("maxWordsPerLine", 3)
    kept = [w for w in transcript["words"] if not T.is_filler(w["text"])]

    lines: list[dict] = []
    cur: list[dict] = []
    for w in kept:
        cur.append({"sourceAtUs": w["sourceAtUs"], "sourceEndUs": w["sourceEndUs"],
                    "text": w["text"]})
        if len(cur) >= mw:
            lines.append({"words": cur})
            cur = []
    if cur:
        lines.append({"words": cur})

    track = I.caption_track(st)
    track["lines"] = lines
    ir["tracks"].append(track)
    return track


def caption_track_of(ir: dict) -> dict | None:
    for t in ir["tracks"]:
        if t.get("kind") == "caption":
            return t
    return None


# --------------------------------------------------------------- overlays (b-roll / logo)
def broll_tracks(ir: dict) -> list[dict]:
    return [t for t in ir["tracks"]
            if t.get("kind") in ("video", "image") and t.get("role") == "broll"]


def logo_clip_of(ir: dict) -> dict | None:
    for t in ir["tracks"]:
        if t.get("role") == "logo" and t.get("clips"):
            return t["clips"][0]
    return None


def add_broll(ir: dict, asset_id: str, source_at_us: int, source_end_us: int,
              **kw) -> dict:
    """Add a source-anchored b-roll cutaway over the speech window."""
    I.ensure_track(ir, "brollTrack", "video", role="broll")
    track = I.get_track(ir, "brollTrack")
    clip = I.broll_clip(asset_id, source_at_us, source_end_us, **kw)
    track["clips"].append(clip)
    return clip


def add_logo(ir: dict, asset_id: str, **kw) -> dict:
    """Set (single) program-anchored corner logo/watermark."""
    I.ensure_track(ir, "logoTrack", "image", role="logo")
    track = I.get_track(ir, "logoTrack")
    track["clips"] = [I.logo_clip(asset_id, **kw)]
    return track["clips"][0]


def resolve_broll_windows(ir: dict) -> list[dict]:
    """Map each source-anchored b-roll clip to program time via the cut-map.
    Returns [{clip, asset, progStartUs, progEndUs}]; clips fully inside a cut
    are dropped."""
    cmap = build_cut_map(ir)
    out: list[dict] = []
    for t in broll_tracks(ir):
        for clip in t["clips"]:
            ps = source_to_program(cmap, clip["sourceAtUs"])
            pe = source_to_program(cmap, max(clip["sourceAtUs"], clip["sourceEndUs"] - 1))
            if ps is None:
                continue
            if pe is None:
                pe = ps + (clip["sourceEndUs"] - clip["sourceAtUs"])
            else:
                pe += 1
            out.append({"clip": clip, "asset": clip["asset"],
                        "progStartUs": ps, "progEndUs": pe})
    return out


def resolve_caption_events(ir: dict) -> list[dict]:
    """Map source-anchored caption words to program time via the cut-map.
    Returns render-ready events: {progAtUs, progEndUs, lineWords:[...], activeIdx}
    where each word already carries its own program window. Words landing in a
    cut are dropped; lines with no surviving words vanish."""
    cmap = build_cut_map(ir)
    track = caption_track_of(ir)
    if not track:
        return []
    events: list[dict] = []
    for line in track["lines"]:
        pwords = []
        for w in line["words"]:
            ps = source_to_program(cmap, w["sourceAtUs"])
            pe = source_to_program(cmap, max(w["sourceAtUs"], w["sourceEndUs"] - 1))
            if ps is None:
                continue
            if pe is None:
                pe = ps + (w["sourceEndUs"] - w["sourceAtUs"])
            else:
                pe += 1
            pwords.append({"progAtUs": ps, "progEndUs": pe, "text": w["text"]})
        if not pwords:
            continue
        events.append({
            "progAtUs": pwords[0]["progAtUs"],
            "progEndUs": pwords[-1]["progEndUs"],
            "words": pwords,
        })
    return events
