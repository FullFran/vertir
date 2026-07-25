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
from . import plan as PL


def build_ir(hero_path: str, transcript: dict, *, title: str = "short",
             bgm_path: str | None = None, focus_y: float = 0.4,
             max_gap_us: int = 450000, plan: dict | None = None) -> dict:
    """Assemble the base IR for a talking-head short (main track + captions + bgm).
    Overlays (b-roll/logo) are added afterwards via edit.add_broll / add_logo.

    With `plan`, the editorial layer decides the cut (hook, drops, punch-in beats,
    emphasis, cards) instead of the mechanical filler cut. Without it, behaviour
    is unchanged.
    """
    aid, asset = P.ingest(hero_path, "hero")
    doc = I.new_ir(title=title)
    fps = asset.get("probe", {}).get("fps")
    if fps:
        doc["project"]["fps"] = fps
    doc["assets"][aid] = asset

    if plan is not None:
        # apply_plan owns the main track and creates the caption track itself,
        # so emphasis can be applied to words that already exist.
        PL.apply_plan(doc, plan, transcript, asset_id=aid,
                      max_gap_us=max_gap_us, focus_y=focus_y)
    else:
        E.cut_fillers(doc, transcript, aid, max_gap_us=max_gap_us, focus_y=focus_y)
        E.captions_from_transcript(doc, transcript)

    if bgm_path:
        baid, basset = P.ingest(bgm_path, "bgm")
        doc["assets"][baid] = basset
        I.ensure_track(doc, "bgmTrack", "audio", role="bgm")
        I.get_track(doc, "bgmTrack")["clips"] = [I.bgm_clip(baid)]

    E.derive(doc)
    return doc


def render_doc(doc: dict, out_dir: str, *, proxy: bool = True,
               plan: dict | None = None) -> dict:
    """Validate, persist the IR, and render (final + optional proxy). Returns
    {ir, report, receipts, paths}.

    `plan` is persisted beside the IR and the receipts. "Viral" is empirical: the
    plan is the only record of *why* this cut was made, so keeping it next to the
    render is what lets a later planner learn from what actually performed.
    """
    os.makedirs(out_dir, exist_ok=True)
    E.derive(doc)
    report = V.validate(doc)
    ir_path = os.path.join(out_dir, "timeline.ir.json")
    I.dump(doc, ir_path)
    plan_path = None
    if plan is not None:
        plan_path = os.path.join(out_dir, "plan.json")
        PL.dump(plan, plan_path)
    if not report["ok"]:
        paths = {"ir": ir_path}
        if plan_path:
            paths["plan"] = plan_path
        return {"ir": doc, "report": report, "receipts": {}, "paths": paths}

    receipts: dict[str, Any] = {}
    paths: dict[str, str] = {"ir": ir_path}
    if plan_path:
        paths["plan"] = plan_path
    final_path = os.path.join(out_dir, "final.mp4")
    receipts["final"] = R.render(doc, final_path, proxy=False)
    paths["final"] = final_path
    if proxy:
        proxy_path = os.path.join(out_dir, "preview.mp4")
        receipts["preview"] = R.render(doc, proxy_path, proxy=True)
        paths["preview"] = proxy_path
    return {"ir": doc, "report": report, "receipts": receipts, "paths": paths}


def build_short(hero_path: str, transcript: dict, out_dir: str, *,
                title: str = "short", bgm_path: str | None = None,
                focus_y: float = 0.4, proxy: bool = True,
                plan: dict | None = None) -> dict:
    """Full core pipeline (no overlays). For b-roll/logo, use build_ir + add_* +
    render_doc, as the demo does."""
    doc = build_ir(hero_path, transcript, title=title, bgm_path=bgm_path,
                   focus_y=focus_y, plan=plan)
    return render_doc(doc, out_dir, proxy=proxy, plan=plan)
