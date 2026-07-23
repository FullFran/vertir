"""VertIR — the declarative Timeline IR.

The IR is plain JSON (dicts). This module is the single source of truth for its
shape: constants, builder helpers to construct valid documents, and load/dump.

Spec: docs/timeline-ir-v1.md (v1.1). Time is always integer microseconds (us).
"""
from __future__ import annotations

import json
import uuid
from typing import Any

IR_VERSION = "1.1.0"

# ---- enums / vocab (kept as plain strings so the IR stays pure JSON) ----
TRACK_KINDS = {"video", "image", "caption", "title", "audio"}
VIDEO_ROLES = {"main", "broll", "logo", "overlay"}
AUDIO_ROLES = {"bgm", "voiceover", "sfx"}
REFRAME_MODES = {"cover", "contain", "crop"}
TRANSITION_TYPES = {"cut", "dissolve"}
EASES = {"linear", "easeInOut", "hold"}
KEYFRAME_PROPS = {"scale", "x", "y", "opacity", "gainDb"}
CAPTION_PRESETS = {"word-highlight", "karaoke", "block"}
SAFE_AREAS = {"tiktok", "reels", "shorts", "generic"}

WHOLE_PROGRAM = -1  # endUs sentinel: element spans the whole program (logo, bgm)


# ------------------------------------------------------------------ helpers
def new_id(prefix: str = "id") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def fps(num: int = 30, den: int = 1) -> dict:
    return {"num": int(num), "den": int(den)}


def fps_value(f: dict) -> float:
    return f["num"] / f["den"]


def frame_dur_us(f: dict) -> int:
    return round(1_000_000 * f["den"] / f["num"])


def default_output(profile: str = "tiktok") -> dict:
    return {
        "deliveryProfile": profile,
        "loudnessLufs": -14.0,
        "truePeakDb": -1.0,
        "videoCodec": "h264",
        "crf": 18,
        "pixFmt": "yuv420p",
        "colorPrimaries": "bt709",
        "transfer": "bt709",
        "audioCodec": "aac",
        "audioBitrateKbps": 192,
    }


def new_ir(title: str = "untitled", fps_num: int = 30, fps_den: int = 1,
           w: int = 1080, h: int = 1920, profile: str = "tiktok") -> dict:
    """Create an empty but valid IR skeleton (a single empty main video track)."""
    return {
        "irVersion": IR_VERSION,
        "project": {
            "id": new_id("proj"),
            "title": title,
            "fps": fps(fps_num, fps_den),
            "canvas": {"w": int(w), "h": int(h)},
            "output": default_output(profile),
            "cutAudioFadeUs": 30000,
            # durationUs is engine-derived; kept for readers, never trusted from the LLM
            "durationUs": 0,
        },
        "assets": {},
        "tracks": [
            {"id": "main", "kind": "video", "role": "main", "clips": []},
        ],
        "transitions": [],
    }


# ------------------------------------------------------------------ asset ops
def add_asset(ir: dict, asset_id: str, sha256: str, path: str, kind: str,
              probe: dict | None = None) -> str:
    ir["assets"][asset_id] = {
        "sha256": sha256, "path": path, "kind": kind, "probe": probe or {},
    }
    return asset_id


# ------------------------------------------------------------------ track ops
def get_track(ir: dict, track_id: str) -> dict | None:
    for t in ir["tracks"]:
        if t["id"] == track_id:
            return t
    return None


def main_track(ir: dict) -> dict:
    for t in ir["tracks"]:
        if t.get("kind") == "video" and t.get("role") == "main":
            return t
    raise ValueError("IR has no main video track")


def ensure_track(ir: dict, track_id: str, kind: str, role: str | None = None,
                 **extra) -> dict:
    t = get_track(ir, track_id)
    if t is None:
        t = {"id": track_id, "kind": kind}
        if role is not None:
            t["role"] = role
        t.update(extra)
        if kind in ("video", "image", "audio", "title"):
            t.setdefault("clips", [])
        ir["tracks"].append(t)
    return t


# ------------------------------------------------------------------ clip builders
def main_clip(asset: str, source_start_us: int, source_end_us: int,
              reframe_mode: str = "cover", focus_x: float = 0.5, focus_y: float = 0.4,
              gain_db: float = 0.0, clip_id: str | None = None) -> dict:
    """A clip on the main track. timeline.* is engine-derived (not set here)."""
    return {
        "id": clip_id or new_id("c"),
        "asset": asset,
        "source": {"startUs": int(source_start_us), "endUs": int(source_end_us)},
        "speed": 1.0,
        "reframe": {"mode": reframe_mode, "focusX": focus_x, "focusY": focus_y},
        "transform": {"scale": 1.0, "x": 0, "y": 0, "opacity": 1.0},
        "audio": {"gainDb": gain_db, "mute": False},
        "fadeInUs": 0,
        "fadeOutUs": 0,
        "transitionIn": {"type": "cut", "durUs": 0},
        "keyframes": [],
    }


def caption_style(**over) -> dict:
    style = {
        "preset": "word-highlight",
        "fontFamily": "Montserrat",
        "fontWeight": 800,
        "fontSizePx": 76,
        "fillColor": "#FFFFFF",
        "highlightColor": "#FFE000",
        "strokeColor": "#000000",
        "strokePx": 8,
        "uppercase": True,
        "maxWordsPerLine": 3,
        "autoFit": True,
        "position": {"anchor": "blockCenter", "yPct": 0.74},
        "safeArea": "tiktok",
    }
    style.update(over)
    return style


def caption_track(style: dict | None = None, track_id: str = "caps") -> dict:
    return {"id": track_id, "kind": "caption", "style": style or caption_style(),
            "lines": []}


def bgm_clip(asset: str, gain_db: float = -18.0, source_start_us: int = 0,
             source_end_us: int | None = None, duck: bool = True,
             fade_in_us: int = 500000, fade_out_us: int = 1200000) -> dict:
    clip = {
        "id": new_id("m"),
        "asset": asset,
        "anchor": "program",
        "atUs": 0,
        "endUs": WHOLE_PROGRAM,
        "gainDb": gain_db,
        "duck": {"enabled": duck, "targetDb": -26.0},
        "fadeInUs": fade_in_us,
        "fadeOutUs": fade_out_us,
        "keyframes": [],
    }
    if source_end_us is not None:
        clip["source"] = {"startUs": int(source_start_us), "endUs": int(source_end_us)}
    return clip


# ------------------------------------------------------------------ io
def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def dump(ir: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ir, fh, ensure_ascii=False, indent=2)


def dumps(ir: dict) -> str:
    return json.dumps(ir, ensure_ascii=False, indent=2)
