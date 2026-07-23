"""High-level core pipeline: raw footage + transcript -> IR -> rendered MP4.

This is the deterministic assembly the agent's plan flows through. Each step is a
small, testable function from the other modules.
"""
from __future__ import annotations

import os
from typing import Any

from . import ir as I
from . import probe as P
from . import edit as E
from . import validate as V
from . import render as R


def build_ir(hero_path: str, transcript: dict, *, title: str = "short",
             bgm_path: str | None = None, focus_y: float = 0.4,
             max_gap_us: int = 450000) -> dict:
    """Assemble a validated IR for a talking-head short (no rendering)."""
    aid, asset = P.ingest(hero_path, "hero")
    doc = I.new_ir(title=title)
    fps = asset.get("probe", {}).get("fps")
    if fps:
        doc["project"]["fps"] = fps
    doc["assets"][aid] = asset

    E.cut_fillers(doc, transcript, aid, max_gap_us=max_gap_us, focus_y=focus_y)
    E.captions_from_transcript(doc, transcript)

    if bgm_path:
        baid, basset = P.ingest(bgm_path, "bgm")
        doc["assets"][baid] = basset
        I.ensure_track(doc, "bgmTrack", "audio", role="bgm")
        I.get_track(doc, "bgmTrack")["clips"] = [I.bgm_clip(baid)]

    E.derive(doc)
    return doc


def build_short(hero_path: str, transcript: dict, out_dir: str, *,
                title: str = "short", bgm_path: str | None = None,
                focus_y: float = 0.4, proxy: bool = True) -> dict:
    """Full pipeline. Returns {ir, report, receipts, paths}."""
    os.makedirs(out_dir, exist_ok=True)
    doc = build_ir(hero_path, transcript, title=title, bgm_path=bgm_path, focus_y=focus_y)

    report = V.validate(doc)
    ir_path = os.path.join(out_dir, "timeline.ir.json")
    I.dump(doc, ir_path)
    if not report["ok"]:
        return {"ir": doc, "report": report, "receipts": {}, "paths": {"ir": ir_path}}

    receipts: dict[str, Any] = {}
    paths: dict[str, str] = {"ir": ir_path}
    final_path = os.path.join(out_dir, "final.mp4")
    receipts["final"] = R.render(doc, final_path, proxy=False)
    paths["final"] = final_path
    if proxy:
        proxy_path = os.path.join(out_dir, "preview.mp4")
        receipts["preview"] = R.render(doc, proxy_path, proxy=True)
        paths["preview"] = proxy_path

    return {"ir": doc, "report": report, "receipts": receipts, "paths": paths}
