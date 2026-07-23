"""Ingest: ffprobe wrapper + content-addressing.

Fills the `probe` block the renderer/validator rely on. Zero deps (subprocess).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import Any


def sha256_file(path: str, _chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(_chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _parse_rational(r: str | None) -> dict:
    if not r or r in ("0/0", "N/A"):
        return {"num": 30, "den": 1}
    if "/" in r:
        num, den = r.split("/", 1)
        num, den = int(num), int(den)
    else:
        num, den = int(round(float(r))), 1
    if den == 0:
        den = 1
    return {"num": num, "den": den}


def ffprobe(path: str) -> dict:
    """Return the raw ffprobe JSON for `path`."""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def probe(path: str) -> dict:
    """Return the IR `probe` block for a media file."""
    raw = ffprobe(path)
    streams = raw.get("streams", [])
    fmt = raw.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    dur_us = None
    if fmt.get("duration") not in (None, "N/A"):
        dur_us = int(round(float(fmt["duration"]) * 1_000_000))

    out: dict[str, Any] = {"hasAudio": a is not None}
    if dur_us is not None:
        out["durationUs"] = dur_us
    if v is not None:
        out["w"] = int(v.get("width", 0))
        out["h"] = int(v.get("height", 0))
        out["fps"] = _parse_rational(v.get("avg_frame_rate") or v.get("r_frame_rate"))
        if v.get("duration") not in (None, "N/A") and "durationUs" not in out:
            out["durationUs"] = int(round(float(v["duration"]) * 1_000_000))
    if a is not None:
        out["sampleRateHz"] = int(a.get("sample_rate", 48000))
    return out


def kind_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return "image"
    if ext in (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"):
        return "audio"
    return "video"


def ingest(path: str, asset_id: str | None = None) -> tuple[str, dict]:
    """Return (asset_id, asset_dict) for a media file on disk."""
    kind = kind_for(path)
    pr = {} if kind == "image" and False else probe(path)
    asset = {
        "sha256": sha256_file(path),
        "path": path,
        "kind": kind,
        "probe": pr,
    }
    aid = asset_id or os.path.splitext(os.path.basename(path))[0]
    return aid, asset
