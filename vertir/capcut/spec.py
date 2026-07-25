"""IR -> `capcut compile` spec, with an explicit loss report.

The mapping is lossy, and saying so is the feature. An export that silently drops
loudness normalisation, side-chain ducking or word-level caption highlighting is
worse than no export at all: the draft looks finished and is not. Every construct
this transpiler cannot carry across is reported, with a severity.

Time base: the IR uses integer microseconds. The CapCut *draft store* is also
microsecond-based, but the `compile` spec interface takes **seconds** as floats,
so every boundary is converted here and only here.
"""
from __future__ import annotations

import json
import os
import shutil

from .. import ir as I
from .. import edit as E
from .. import validate as V
from .provenance import Provenance

# severities
DROPPED = "dropped"        # the construct does not survive at all
DEGRADED = "degraded"      # it survives in a weaker form
UNVERIFIED = "unverified"  # mapped, but the convention could not be verified here

# IR ease vocabulary (spec 5) -> capcut keyframe easing names. `hold` (a step)
# has no counterpart: CapCut always interpolates, so it degrades to linear.
EASE_MAP = {"linear": None, "easeInOut": "ease-in-out"}

# IR keyframe props (spec 5) -> capcut keyframe property names. `scale` is
# uniform in the IR, so it maps to uniform_scale rather than scale_x/scale_y.
PROP_MAP = {"scale": "uniform_scale", "x": "position_x", "y": "position_y",
            "opacity": "alpha"}


def _s(us: int) -> float:
    """Microseconds -> seconds, at the precision the spec parser accepts."""
    return round(us / 1_000_000, 6)


def _y(y_pct: float) -> float:
    """Top-anchored 0..1 (IR) -> centre-anchored -1..1 (CapCut), y negative up."""
    return round((y_pct - 0.5) * 2, 4)


def _ratio(w: int, h: int) -> str:
    """The canvas ratio label CapCut expects. Left as `original` when the canvas
    is not one of the presets, rather than inventing a string it may reject."""
    return {(9, 16): "9:16", (16, 9): "16:9", (1, 1): "1:1",
            (4, 5): "4:5", (3, 4): "3:4", (4, 3): "4:3"}.get(
                _reduce(w, h), "original")


def _reduce(w: int, h: int) -> tuple[int, int]:
    from math import gcd
    g = gcd(int(w), int(h)) or 1
    return int(w) // g, int(h) // g


def _gain_to_volume(db: float) -> float:
    """dB -> the linear multiplier CapCut stores in `segment.volume`."""
    return round(10 ** (float(db) / 20.0), 4)


class _Losses:
    """Collects losses, grouping repeats by code.

    A per-clip loss like `reframe` fires once per clip; printing it forty times
    buries the four distinct problems underneath. Repeats collapse into one entry
    that carries every affected id in `where`.
    """

    def __init__(self) -> None:
        self._by_code: dict[str, dict] = {}

    def add(self, code: str, construct: str, detail: str,
            severity: str = DEGRADED, where: str = "") -> None:
        entry = self._by_code.get(code)
        if entry is None:
            self._by_code[code] = {"code": code, "construct": construct,
                                   "detail": detail, "severity": severity,
                                   "where": [where] if where else [], "count": 1}
            return
        entry["count"] += 1
        if where:
            entry["where"].append(where)

    def to_list(self) -> list[dict]:
        return list(self._by_code.values())


# ------------------------------------------------------------------ track builders
def _main_items(ir: dict, losses: _Losses, prov: Provenance) -> list[dict]:
    items: list[dict] = []
    assets = ir["assets"]
    for c in I.main_track(ir)["clips"]:
        tl = c.get("timeline") or {}
        asset = assets.get(c["asset"], {})
        ref = f"main_{c['id']}"
        item = {
            "ref": ref,
            "path": asset.get("path", ""),
            "start": _s(tl.get("startUs", 0)),
            "duration": _s(tl.get("endUs", 0) - tl.get("startUs", 0)),
            "sourceStart": _s(c["source"]["startUs"]),
        }
        if abs(float(c.get("speed", 1.0)) - 1.0) > 1e-6:
            item["speed"] = float(c["speed"])
        audio = c.get("audio", {})
        if audio.get("mute"):
            item["volume"] = 0.0
        elif abs(float(audio.get("gainDb", 0.0))) > 1e-6:
            item["volume"] = _gain_to_volume(audio["gainDb"])

        rf = c.get("reframe", {})
        if rf and rf.get("mode") != "contain":
            # the compile spec has no crop/focus field; CapCut will fit by its own
            # rule instead of honouring focusX/focusY
            losses.add("reframe", "reframe.focus",
                       f"clip {c['id']} reframe {rf.get('mode')} "
                       f"focus=({rf.get('focusX')},{rf.get('focusY')}) has no compile-spec "
                       "equivalent; set the crop in CapCut or via `capcut crop`",
                       DROPPED, c["id"])
        if c.get("fadeInUs") or c.get("fadeOutUs"):
            losses.add("video-fade", "clip.fadeIn/fadeOut",
                       f"clip {c['id']} video fades are not expressible in the compile spec "
                       "(only audio-fade is)", DROPPED, c["id"])
        tin = (c.get("transitionIn") or {}).get("type", "cut")
        if tin != "cut":
            losses.add("transition", "clip.transitionIn",
                       f"clip {c['id']} transition {tin!r} is not mapped in v1; "
                       "add it in CapCut or via `capcut transition`", DROPPED, c["id"])

        prov.bind(c["id"], ref)
        items.append(item)
    return items


def _keyframe_ops(ir: dict, losses: _Losses, prov: Provenance) -> list[dict]:
    """`scale` keyframes (the planner's punch-in beats) map onto the `keyframe`
    operation, whose supported properties include scale."""
    ops: list[dict] = []
    for c in I.main_track(ir)["clips"]:
        ref = prov.to_capcut(c["id"])
        if not ref:
            continue
        for kf in c.get("keyframes", []):
            if kf.get("prop") != "scale":
                continue
            op = {"op": "keyframe", "target": ref, "property": PROP_MAP["scale"],
                  "time": _s(int(kf.get("atUs", 0))), "value": float(kf.get("v", 1.0))}
            ease = kf.get("ease", "linear")
            if ease in EASE_MAP:
                if EASE_MAP[ease]:
                    op["easing"] = EASE_MAP[ease]
            else:
                losses.add("keyframe-ease", "keyframe.ease",
                           f"ease {ease!r} has no CapCut counterpart (it supports "
                           "linear/ease-in/ease-out/ease-in-out); falls back to linear",
                           DEGRADED, c["id"])
            ops.append(op)
    return ops


def _broll_items(ir: dict, losses: _Losses, prov: Provenance) -> list[dict]:
    items: list[dict] = []
    assets = ir["assets"]
    for w in E.resolve_broll_windows(ir):
        c = w["clip"]
        asset = assets.get(c["asset"], {})
        ref = f"broll_{c['id']}"
        item = {
            "ref": ref,
            "path": asset.get("path", ""),
            "start": _s(w["progStartUs"]),
            "duration": _s(w["progEndUs"] - w["progStartUs"]),
        }
        if asset.get("kind") == "image":
            item["type"] = "photo"
        else:
            item["sourceStart"] = _s(c.get("source", {}).get("startUs", 0))
        if c.get("fadeInUs") or c.get("fadeOutUs"):
            losses.add("video-fade", "broll.fadeIn/fadeOut",
                       f"b-roll {c['id']} fades are not expressible in the compile spec",
                       DROPPED, c["id"])
        prov.bind(c["id"], ref)
        items.append(item)
    return items


def _logo_item(ir: dict, losses: _Losses, prov: Provenance) -> dict | None:
    clip = E.logo_clip_of(ir)
    if not clip:
        return None
    asset = ir["assets"].get(clip["asset"], {})
    tr = clip.get("transform", {})
    corner = clip.get("corner", "top-right")
    # clip.transform is normalised around a centred origin, with NEGATIVE y
    # towards the top of the frame (confirmed against the compile spec's own
    # example, which places a hook line high in frame at y: -0.6).
    x = -0.8 if "left" in corner else 0.8
    y = -0.8 if "top" in corner else 0.8
    ref = f"logo_{clip['id']}"
    losses.add("logo-margin", "logo.marginPx",
               f"logo corner {corner!r} is placed at a fixed normalised inset; the IR's "
               f"marginPx ({clip.get('marginPx', 48)}px) is not expressible, so the exact "
               "offset will differ", DEGRADED, clip["id"])
    losses.add("logo-scale", "logo.transform.scale",
               "IR logo scale is a fraction of canvas width; CapCut scale is a "
               "multiplier of the material's natural size — the size will differ",
               DEGRADED, clip["id"])
    prov.bind(clip["id"], ref)
    return {
        "ref": ref, "path": asset.get("path", ""), "type": "photo",
        "start": 0.0, "duration": _s(ir["project"].get("durationUs", 0)),
        "x": x, "y": y,
        "scale": float(tr.get("scale", 0.16)),
        "opacity": float(tr.get("opacity", 0.9)),
    }


def _caption_items(ir: dict, losses: _Losses) -> list[dict]:
    """Captions degrade from per-word karaoke to one static text cue per line."""
    events = E.resolve_caption_events(ir)
    if not events:
        return []
    cap = E.caption_track_of(ir) or {}
    st = cap.get("style", {})
    upper = st.get("uppercase", True)
    y_pct = st.get("position", {}).get("yPct", 0.74)

    losses.add("caption-karaoke", "caption.preset",
               f"word-highlight ({st.get('preset')}) degrades to one static cue per "
               "line; CapCut has no per-word highlight in the compile spec",
               DEGRADED, cap.get("id", "caps"))
    if any(w.get("emphasis") for ev in events for w in ev["words"]):
        losses.add("caption-emphasis", "plan.emphasis",
                   "per-word emphasis colour is lost; the whole cue takes the fill "
                   "colour (`capcut text-ranges` could restore it later)",
                   DROPPED, cap.get("id", "caps"))

    items = []
    for i, ev in enumerate(events):
        text = " ".join(w["text"] for w in ev["words"])
        items.append({
            "ref": f"cap_{i:04d}",
            "text": text.upper() if upper else text,
            "start": _s(ev["progAtUs"]),
            "duration": _s(ev["progEndUs"] - ev["progAtUs"]),
            "fontSize": int(st.get("fontSizePx", 76)),
            "color": st.get("fillColor", "#FFFFFF"),
            "y": _y(y_pct),
        })
    return items


def _title_items(ir: dict, losses: _Losses, prov: Provenance) -> list[dict]:
    track = E.title_track_of(ir)
    if not track:
        return []
    items = []
    for c in track.get("clips", []):
        txt = c.get("text", {})
        ref = f"title_{c['id']}"
        bg = (c.get("background") or {}).get("type", "transparent")
        if bg != "transparent":
            losses.add("title-background", "title.background",
                       f"title {c['id']} background {bg!r} is not expressible as a text "
                       "item; only the text crosses over", DROPPED, c["id"])
        prov.bind(c["id"], ref)
        items.append({
            "ref": ref,
            "text": txt.get("content", ""),
            "start": _s(c["atUs"]),
            "duration": _s(c["endUs"] - c["atUs"]),
            "fontSize": int(txt.get("fontSizePx", 104)),
            "color": txt.get("fillColor", "#FFFFFF"),
            "y": _y(txt.get("position", {}).get("yPct", 0.45)),
        })
    return items


def _audio_tracks(ir: dict, losses: _Losses, prov: Provenance) -> tuple[list[dict], list[dict]]:
    """Music/voiceover items plus their audio-fade operations."""
    items: list[dict] = []
    ops: list[dict] = []
    prog_end = ir["project"].get("durationUs", 0)
    for t in ir.get("tracks", []):
        if t.get("kind") != "audio":
            continue
        for c in t.get("clips", []):
            asset = ir["assets"].get(c.get("asset"), {})
            ref = f"aud_{c['id']}"
            end = c.get("endUs", I.WHOLE_PROGRAM)
            end_us = prog_end if end == I.WHOLE_PROGRAM else end
            item = {
                "ref": ref,
                "path": asset.get("path", ""),
                "start": _s(c.get("atUs", 0)),
                "duration": _s(max(0, end_us - c.get("atUs", 0))),
                "volume": _gain_to_volume(c.get("gainDb", 0.0)),
            }
            if c.get("source"):
                item["sourceStart"] = _s(c["source"]["startUs"])
            items.append(item)
            prov.bind(c["id"], ref)

            if c.get("fadeInUs") or c.get("fadeOutUs"):
                ops.append({"op": "audio-fade", "target": ref,
                            "fadeIn": _s(int(c.get("fadeInUs", 0))),
                            "fadeOut": _s(int(c.get("fadeOutUs", 0)))})
            duck = c.get("duck") or {}
            if duck.get("enabled"):
                losses.add("duck", "bgm.duck",
                           f"side-chain ducking to {duck.get('targetDb')}dB is a rule, not a "
                           "curve; CapCut has no side-chain, so the music will play flat "
                           "under speech", DROPPED, c["id"])
    return items, ops


# ------------------------------------------------------------------ entry points
def build_spec(ir: dict, *, name: str | None = None) -> tuple[dict, list[dict]]:
    """Transpile an IR into a `capcut compile` spec.

    Returns `(spec, losses)`. Losses are never empty for a non-trivial IR, and
    that is the honest answer — callers must surface them.
    """
    E.derive(ir)
    losses = _Losses()
    prov = Provenance(ir_version=ir.get("irVersion", ""))

    proj = ir["project"]
    fps = proj.get("fps", {"num": 30, "den": 1})
    canvas = proj.get("canvas", {"w": 1080, "h": 1920})

    tracks: list[dict] = [{"type": "video", "name": "main",
                           "items": _main_items(ir, losses, prov)}]

    broll = _broll_items(ir, losses, prov)
    if broll:
        tracks.append({"type": "video", "name": "broll", "items": broll})

    logo = _logo_item(ir, losses, prov)
    if logo:
        tracks.append({"type": "video", "name": "logo", "items": [logo]})

    audio_items, audio_ops = _audio_tracks(ir, losses, prov)
    if audio_items:
        tracks.append({"type": "audio", "name": "music", "items": audio_items})

    captions = _caption_items(ir, losses)
    if captions:
        tracks.append({"type": "text", "name": "captions", "items": captions})

    titles = _title_items(ir, losses, prov)
    if titles:
        tracks.append({"type": "text", "name": "titles", "items": titles})

    spec = {
        "name": name or proj.get("title", "vertir-export"),
        "width": canvas["w"],
        "height": canvas["h"],
        "fps": int(round(fps["num"] / fps["den"])),
        "ratio": _ratio(canvas["w"], canvas["h"]),
        "tracks": tracks,
    }
    ops = _keyframe_ops(ir, losses, prov) + audio_ops
    if ops:
        spec["operations"] = ops

    out = proj.get("output", {})
    if out.get("loudnessLufs") is not None:
        losses.add("loudness", "output.loudnessLufs/truePeakDb",
                   f"delivery loudness ({out.get('loudnessLufs')} LUFS / "
                   f"{out.get('truePeakDb')} dBTP) has no CapCut equivalent; the app's "
                   "export will not be normalised", DROPPED, "project.output")

    return spec, losses.to_list()


def export(ir: dict, out_dir: str, *, name: str | None = None,
           check_only: bool = False, drafts_dir: str | None = None) -> dict:
    """Validate, transpile, check, and write a CapCut draft.

    Fail-closed on both sides: the IR validator must pass before we transpile,
    and `capcut compile --check` must pass before anything is written.
    """
    from . import bridge

    report = V.validate(ir)
    if not report["ok"]:
        return {"ok": False, "stage": "ir-validate", "report": report}

    spec, losses = build_spec(ir, name=name)
    os.makedirs(out_dir, exist_ok=True)
    spec_path = os.path.join(out_dir, "capcut.spec.json")
    with open(spec_path, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, ensure_ascii=False, indent=2)

    if not bridge.available():
        return {"ok": False, "stage": "cli-missing", "specPath": spec_path,
                "losses": losses, "hint": bridge.INSTALL_HINT}

    checked = bridge.compile_spec(spec_path, out_dir, check_only=True,
                                  drafts_dir=drafts_dir)
    if not checked["ok"]:
        return {"ok": False, "stage": "compile-check", "specPath": spec_path,
                "losses": losses,
                "error": (checked["stderr"] or checked["stdout"]).strip()}
    if check_only:
        return {"ok": True, "stage": "check", "specPath": spec_path, "losses": losses}

    # `--check` validates the spec's shape but not every enum (keyframe property
    # names, for one), so the real compile can still fail here. Report it as a
    # stage rather than raising: callers are the CLI and an MCP tool.
    draft_dir = os.path.join(out_dir, "capcut-draft")
    res = bridge.compile_spec(spec_path, draft_dir, drafts_dir=drafts_dir)
    if not res["ok"]:
        return {"ok": False, "stage": "compile", "specPath": spec_path,
                "losses": losses,
                "error": (res["stderr"] or res["stdout"]).strip()}

    # Snapshot the draft exactly as written: reconcile diffs against this, so it
    # can tell a human's edits apart from what we generated.
    snapshot_dir = os.path.join(out_dir, "capcut-draft.exported")
    if os.path.isdir(snapshot_dir):
        shutil.rmtree(snapshot_dir)
    shutil.copytree(draft_dir, snapshot_dir)

    prov = Provenance(ir_version=ir.get("irVersion", ""), draft_path=draft_dir,
                      refs=_collect_refs(spec), snapshot_path=snapshot_dir)
    prov.bind_segments((res["json"] or {}).get("refs", {}))
    prov_path = prov.dump(out_dir)

    return {"ok": True, "stage": "compiled", "specPath": spec_path,
            "draftPath": draft_dir, "provenancePath": prov_path,
            "snapshotPath": snapshot_dir,
            "losses": losses, "result": res["json"]}


def _collect_refs(spec: dict) -> dict[str, str]:
    """ir clip id -> capcut ref, recovered from the spec's ref naming."""
    refs: dict[str, str] = {}
    for track in spec.get("tracks", []):
        for item in track.get("items", []):
            ref = item.get("ref")
            if not ref or "_" not in ref:
                continue
            kind, _, clip_id = ref.partition("_")
            if kind in ("main", "broll", "logo", "title", "aud"):
                refs[clip_id] = ref
    return refs
