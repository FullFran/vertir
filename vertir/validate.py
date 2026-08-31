"""Fail-closed validator (spec §8). Returns {"errors": [...], "warnings": [...]}.

`errors` block rendering/export; `warnings` do not. No `force` flag exists.
Forward-compat: unknown optional fields are ignored (never an error).
"""
from __future__ import annotations

from typing import Any

from . import ir as I
from . import edit as E
from . import anim as A

SUPPORTED_MAJOR = 1


class Report:
    def __init__(self) -> None:
        self.errors: list[dict] = []
        self.warnings: list[dict] = []

    def err(self, code: str, msg: str, where: str = "") -> None:
        self.errors.append({"code": code, "msg": msg, "where": where})

    def warn(self, code: str, msg: str, where: str = "") -> None:
        self.warnings.append({"code": code, "msg": msg, "where": where})

    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"ok": self.ok(), "errors": self.errors, "warnings": self.warnings}


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _clip_dur_us(clip: dict, prog_us: int) -> int | None:
    """A clip's own length in program time, or None when it cannot be derived."""
    tl = clip.get("timeline")
    if tl and _is_int(tl.get("startUs")) and _is_int(tl.get("endUs")):
        return tl["endUs"] - tl["startUs"]
    at, end = clip.get("atUs"), clip.get("endUs")
    if _is_int(at) and _is_int(end):
        return max(0, prog_us - at) if end == I.WHOLE_PROGRAM else end - at
    sa, se = clip.get("sourceAtUs"), clip.get("sourceEndUs")
    if _is_int(sa) and _is_int(se):
        return se - sa
    return None


def _check_keyframes(r: "Report", clip: dict, ctx: str, dur_us: int | None,
                     where: str) -> None:
    """Spec section 5. A keyframe the engine cannot execute is reported, never
    dropped in silence: an IR that validates has to be an IR that renders."""
    ks = clip.get("keyframes")
    if not ks:
        return
    cid = clip.get("id", where)
    if not isinstance(ks, list):
        r.err("kf-shape", f"clip {cid} keyframes must be a list", cid)
        return
    last_at: dict[str, int] = {}
    for n, k in enumerate(ks):
        if not isinstance(k, dict):
            r.err("kf-shape", f"clip {cid} keyframe {n} is not an object", cid)
            continue
        prop = k.get("prop")
        if prop not in I.KEYFRAME_PROPS:
            r.err("kf-prop", f"clip {cid} keyframe {n} has unknown prop {prop!r}", cid)
            continue
        v = k.get("v")
        if not _is_num(v):
            r.err("kf-value", f"clip {cid} keyframe {n} ({prop}) value is not a number", cid)
        elif prop == "scale" and v < 1.0:
            # spec section 6: transform is a delta ON TOP of the reframe; going
            # under it uncovers the canvas, which is black bars
            r.err("kf-scale-under",
                  f"clip {cid} keyframe {n} scale {v} drops below the reframe base", cid)
        at = k.get("atUs")
        if not _is_int(at):
            r.err("kf-range", f"clip {cid} keyframe {n} ({prop}) atUs must be an integer", cid)
        else:
            if at < 0 or (dur_us is not None and at > dur_us):
                r.err("kf-range",
                      f"clip {cid} keyframe {n} ({prop}) atUs {at} outside [0, {dur_us}]", cid)
            if prop in last_at and at < last_at[prop]:
                r.err("kf-order",
                      f"clip {cid} keyframe {n} ({prop}) atUs {at} goes back in time", cid)
            last_at[prop] = at
        ease = k.get("ease", "linear")
        if ease not in I.EASES:
            r.err("kf-ease", f"clip {cid} keyframe {n} ({prop}) unknown ease {ease!r}", cid)
    unrendered = A.props_used(ks) - A.rendered_props(ctx)
    if unrendered:
        r.warn("kf-unrendered",
               f"clip {cid} keyframes on {sorted(unrendered)} are valid IR but are "
               "not rendered in v1", cid)


def _interval_ok(rng: dict) -> bool:
    return (_is_int(rng.get("startUs")) and _is_int(rng.get("endUs"))
            and rng["startUs"] >= 0 and rng["startUs"] < rng["endUs"])


def validate(ir: dict) -> dict:
    r = Report()

    # 1. version
    ver = str(ir.get("irVersion", "0"))
    try:
        major = int(ver.split(".")[0])
    except ValueError:
        major = 0
    if major != SUPPORTED_MAJOR:
        r.err("version", f"irVersion major {major} unsupported (expected {SUPPORTED_MAJOR})", "irVersion")

    # 6. exactly one non-empty main track
    mains = [t for t in ir.get("tracks", [])
             if t.get("kind") == "video" and t.get("role") == "main"]
    if len(mains) != 1:
        r.err("main-track", f"expected exactly 1 main video track, found {len(mains)}", "tracks")
        return r.to_dict()
    main = mains[0]
    if not main.get("clips"):
        r.err("main-empty", "main track has no clips", main["id"])
        return r.to_dict()

    # 5. globally-unique clip ids
    seen: set[str] = set()
    for t in ir.get("tracks", []):
        for c in t.get("clips", []):
            cid = c.get("id")
            if cid in seen:
                r.err("dup-id", f"duplicate clip id {cid!r}", t["id"])
            seen.add(cid)

    # 2/3/4. assets + intervals + source-in-duration (main clips)
    assets = ir.get("assets", {})
    prev_end = None
    for c in main["clips"]:
        aid = c.get("asset")
        a = assets.get(aid)
        if a is None:
            r.err("missing-asset", f"clip {c['id']} references unknown asset {aid!r}", c["id"])
            continue
        if not a.get("sha256") or a.get("probe") is None:
            r.warn("asset-meta", f"asset {aid!r} missing sha256/probe", aid)
        src = c.get("source", {})
        if not _interval_ok(src):
            r.err("bad-interval", f"clip {c['id']} has invalid source interval", c["id"])
            continue
        dur = a.get("probe", {}).get("durationUs")
        if dur is not None and src["endUs"] > dur:
            r.err("source-oob", f"clip {c['id']} source endUs {src['endUs']} exceeds asset duration {dur}", c["id"])
        # 6. intra-track (main) non-overlap by timeline (derived)
        tl = c.get("timeline")
        if tl and prev_end is not None and tl["startUs"] < prev_end:
            r.err("overlap", f"clip {c['id']} overlaps previous on main track", c["id"])
        if tl:
            prev_end = tl["endUs"]

    # captions
    cap = E.caption_track_of(ir)
    if cap:
        prog_end = ir.get("project", {}).get("durationUs", 0)
        last_line_end = None
        for li, line in enumerate(cap.get("lines", [])):
            words = line.get("words", [])
            # 10. monotonic words + shared boundaries
            for wi in range(1, len(words)):
                if words[wi]["sourceAtUs"] < words[wi - 1]["sourceEndUs"]:
                    r.warn("word-order", f"caption line {li} word {wi} overlaps previous", cap["id"])
            # 11. no overlapping lines (by source time)
            if words:
                ls, le = words[0]["sourceAtUs"], words[-1]["sourceEndUs"]
                if last_line_end is not None and ls < last_line_end:
                    r.warn("line-overlap", f"caption line {li} overlaps previous line", cap["id"])
                last_line_end = le
        # 12. safe-area (coarse): block must fit above the UI zone
        st = cap.get("style", {})
        y = st.get("position", {}).get("yPct", 0.75)
        if y > 0.86:
            r.warn("safe-area", f"caption yPct {y} intrudes into the platform UI zone", cap["id"])
        # 9. coverage: any surviving events?
        if prog_end and not E.resolve_caption_events(ir):
            r.warn("caption-empty", "no caption words survive the cut-map (all in cuts)", cap["id"])

    # 14. audio tracks
    for t in ir.get("tracks", []):
        if t.get("kind") != "audio":
            continue
        for c in t.get("clips", []):
            g = c.get("gainDb", 0.0)
            if not isinstance(g, (int, float)) or g > 12 or g < -60:
                r.warn("gain", f"audio clip {c.get('id')} gainDb {g} out of sane range", t["id"])
            for f in ("fadeInUs", "fadeOutUs"):
                if c.get(f, 0) < 0:
                    r.err("fade", f"{f} must be >= 0", c.get("id", t["id"]))

    # overlays: b-roll (source-anchored) + logo (program-anchored)
    for t in ir.get("tracks", []):
        role = t.get("role")
        if role == "broll":
            for c in t.get("clips", []):
                a = assets.get(c.get("asset"))
                if a is None:
                    r.err("missing-asset", f"b-roll {c.get('id')} references unknown asset {c.get('asset')!r}", t["id"])
                    continue
                if not (_is_int(c.get("sourceAtUs")) and _is_int(c.get("sourceEndUs"))
                        and c["sourceAtUs"] < c["sourceEndUs"]):
                    r.err("broll-anchor", f"b-roll {c.get('id')} has invalid source anchor", c.get("id"))
                src = c.get("source", {})
                dur = a.get("probe", {}).get("durationUs")
                if a.get("kind") != "image" and _interval_ok(src) and dur is not None and src["endUs"] > dur:
                    r.err("broll-source-oob", f"b-roll {c.get('id')} source exceeds asset duration", c.get("id"))
        elif role == "logo":
            for c in t.get("clips", []):
                a = assets.get(c.get("asset"))
                if a is None:
                    r.err("missing-asset", f"logo references unknown asset {c.get('asset')!r}", t["id"])
                    continue
                if a.get("kind") != "image":
                    r.warn("logo-kind", f"logo asset {c.get('asset')!r} is not an image", t["id"])
                tr = c.get("transform", {})
                if not (0 < tr.get("scale", 0.16) <= 1):
                    r.warn("logo-scale", "logo scale should be in (0,1]", c.get("id"))
                if not (0 <= tr.get("opacity", 0.9) <= 1):
                    r.warn("logo-opacity", "logo opacity should be in [0,1]", c.get("id"))

    wins = E.resolve_broll_windows(ir)
    resolved = {w["clip"]["id"] for w in wins}
    for t in ir.get("tracks", []):
        if t.get("role") == "broll":
            for c in t.get("clips", []):
                if c.get("id") not in resolved:
                    r.warn("broll-in-cut", f"b-roll {c.get('id')} falls entirely in a cut; will not show", t["id"])
    # b-roll clips share one track -> intra-track non-overlap (program time)
    swins = sorted(wins, key=lambda w: w["progStartUs"])
    for i in range(1, len(swins)):
        if swins[i]["progStartUs"] < swins[i - 1]["progEndUs"]:
            r.err("broll-overlap",
                  f"b-roll {swins[i]['clip']['id']} overlaps {swins[i - 1]['clip']['id']} in program time",
                  "brollTrack")
    # content vs window duration mismatch -> b-roll will freeze (short) or truncate (long)
    for w in wins:
        src = w["clip"].get("source", {})
        if _is_int(src.get("startUs")) and _is_int(src.get("endUs")):
            content = src["endUs"] - src["startUs"]
            window = w["progEndUs"] - w["progStartUs"]
            if abs(content - window) > 40000:
                r.warn("broll-duration",
                       f"b-roll {w['clip']['id']} content {content}us != window {window}us "
                       "(will freeze or truncate)", w["clip"]["id"])

    # title cards (intro/outro)
    prog_end = ir.get("project", {}).get("durationUs", 0)
    for t in ir.get("tracks", []):
        if t.get("kind") != "title":
            continue
        for c in t.get("clips", []):
            if not (c.get("text", {}).get("content") or "").strip():
                r.err("title-empty", f"title {c.get('id')} has empty text", c.get("id"))
            if not (_is_int(c.get("atUs")) and _is_int(c.get("endUs")) and c["atUs"] < c["endUs"]):
                r.err("title-interval", f"title {c.get('id')} has invalid interval", c.get("id"))
                continue
            if prog_end and (c["atUs"] < 0 or c["endUs"] > prog_end):
                r.warn("title-oob", f"title {c.get('id')} extends outside the program", c.get("id"))
            bt = c.get("background", {}).get("type", "transparent")
            if bt not in ("transparent", "solid", "color", "blurredSource"):
                r.warn("title-bg", f"title {c.get('id')} unknown background {bt!r}", c.get("id"))

    # keyframes, per clip, with the context that decides what the engine honors
    prog_us = ir.get("project", {}).get("durationUs", 0)
    for t in ir.get("tracks", []):
        if t.get("kind") == "video" and t.get("role") == "main":
            ctx = "main"
        elif t.get("kind") == "audio":
            ctx = "audio"
        else:
            ctx = "overlay"
        for c in t.get("clips", []):
            _check_keyframes(r, c, ctx, _clip_dur_us(c, prog_us), t.get("id", ""))

    tclips = sorted(
        (c for t in ir.get("tracks", []) if t.get("kind") == "title"
         for c in t.get("clips", []) if _is_int(c.get("atUs")) and _is_int(c.get("endUs"))),
        key=lambda c: c["atUs"])
    for i in range(1, len(tclips)):
        if tclips[i]["atUs"] < tclips[i - 1]["endUs"]:
            r.warn("title-overlap",
                   f"title {tclips[i].get('id')} overlaps {tclips[i - 1].get('id')}", "titles")

    return r.to_dict()
