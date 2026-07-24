"""Minimal, zero-dependency MCP server (stdio, JSON-RPC 2.0, newline-delimited).

Exposes the VertIR engine so Claude Code / OpenCode can drive it as an MCP tool
without installing the `mcp` SDK. Implements initialize / tools/list / tools/call
/ ping. For production you'd swap in the official SDK; this is enough to load and
use today.

Register in Claude Code (.mcp.json):
    {"mcpServers": {"vertir": {"command": "python3", "args": ["-m", "vertir", "mcp"]}}}
"""
from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable

from . import ir as I
from . import probe as P
from . import transcript as T
from . import validate as V
from . import render as R
from . import edit as E

PROTOCOL = "2025-06-18"

TOOLS: list[dict] = [
    {
        "name": "ingest",
        "description": "Probe a media file (ffprobe) and return its content-addressed asset block.",
        "inputSchema": {"type": "object", "required": ["media"],
                        "properties": {"media": {"type": "string", "description": "path to a video/audio/image file"}}},
    },
    {
        "name": "build_short",
        "description": "Full core pipeline: talking-head footage + word-level transcript JSON -> filler-cut + 9:16 reframe + word-highlight captions -> validated IR -> rendered final.mp4 + preview.mp4. Returns the validation report and output paths.",
        "inputSchema": {"type": "object", "required": ["hero", "transcript", "out_dir"],
                        "properties": {
                            "hero": {"type": "string", "description": "path to the talking-head video"},
                            "transcript": {"type": "string", "description": "path to a word-level transcript JSON ({words:[{sourceAtUs,sourceEndUs,text}]})"},
                            "out_dir": {"type": "string"},
                            "bgm": {"type": "string", "description": "optional background music file"}}},
    },
    {
        "name": "validate",
        "description": "Run the fail-closed validator on a Timeline IR JSON file. Returns errors/warnings.",
        "inputSchema": {"type": "object", "required": ["ir"],
                        "properties": {"ir": {"type": "string", "description": "path to a timeline IR JSON"}}},
    },
    {
        "name": "render",
        "description": "Render a Timeline IR JSON to MP4 (validates first). Set proxy=true for a fast low-res preview.",
        "inputSchema": {"type": "object", "required": ["ir", "out"],
                        "properties": {"ir": {"type": "string"}, "out": {"type": "string"},
                                       "proxy": {"type": "boolean"}}},
    },
    {
        "name": "add_broll",
        "description": "Add a source-anchored b-roll cutaway to an existing IR: it covers the frame over the speech window [sourceAtUs,sourceEndUs) (original-recording time) while the talking-head audio keeps playing. Re-validates and saves the IR.",
        "inputSchema": {"type": "object", "required": ["ir", "asset", "sourceAtUs", "sourceEndUs"],
                        "properties": {
                            "ir": {"type": "string", "description": "path to the IR JSON to mutate"},
                            "asset": {"type": "string", "description": "path to the b-roll video/image"},
                            "sourceAtUs": {"type": "integer"}, "sourceEndUs": {"type": "integer"},
                            "brollStartUs": {"type": "integer"}, "brollEndUs": {"type": "integer"}}},
    },
    {
        "name": "add_logo",
        "description": "Set a program-anchored corner logo/watermark for the whole video on an existing IR. Re-validates and saves.",
        "inputSchema": {"type": "object", "required": ["ir", "asset"],
                        "properties": {
                            "ir": {"type": "string"}, "asset": {"type": "string", "description": "logo image (png with alpha)"},
                            "corner": {"type": "string", "enum": ["top-left", "top-right", "bottom-left", "bottom-right"]},
                            "scale": {"type": "number"}, "opacity": {"type": "number"}}},
    },
    {
        "name": "add_title",
        "description": "Add a program-anchored full-screen title card (intro/outro/hook) to an existing IR. Use position='intro' (starts at 0), 'outro' (ends at program end), or explicit atUs/endUs. Re-validates and saves.",
        "inputSchema": {"type": "object", "required": ["ir", "text"],
                        "properties": {
                            "ir": {"type": "string"}, "text": {"type": "string"},
                            "position": {"type": "string", "enum": ["intro", "outro", "custom"]},
                            "durUs": {"type": "integer", "description": "duration for intro/outro (default 1.5s)"},
                            "atUs": {"type": "integer"}, "endUs": {"type": "integer"},
                            "background": {"type": "string", "enum": ["transparent", "solid", "color", "blurredSource"]},
                            "bgColor": {"type": "string"}}},
    },
    {
        "name": "demo",
        "description": "Generate a synthetic source + transcript and render an end-to-end example short (incl. b-roll + logo + intro/outro + music ducking). No footage needed.",
        "inputSchema": {"type": "object", "required": ["out_dir"],
                        "properties": {"out_dir": {"type": "string"}}},
    },
]


# ------------------------------------------------------------------ handlers
def h_ingest(args: dict) -> str:
    aid, asset = P.ingest(args["media"])
    return json.dumps({aid: asset}, ensure_ascii=False, indent=2)


def h_build_short(args: dict) -> str:
    from .pipeline import build_short
    tx = T.load_json(args["transcript"])
    res = build_short(args["hero"], tx, args["out_dir"], bgm_path=args.get("bgm"))
    return json.dumps({"report": res["report"], "paths": res["paths"]}, ensure_ascii=False, indent=2)


def h_validate(args: dict) -> str:
    doc = I.load(args["ir"])
    E.derive(doc)
    return json.dumps(V.validate(doc), ensure_ascii=False, indent=2)


def h_render(args: dict) -> str:
    doc = I.load(args["ir"])
    E.derive(doc)
    rep = V.validate(doc)
    if not rep["ok"]:
        return json.dumps({"error": "validation failed", "report": rep}, indent=2)
    receipt = R.render(doc, args["out"], proxy=bool(args.get("proxy", False)))
    return json.dumps(receipt, ensure_ascii=False, indent=2)


def h_add_broll(args: dict) -> str:
    doc = I.load(args["ir"])
    aid, asset = P.ingest(args["asset"], args.get("assetId"))
    doc["assets"][aid] = asset
    kw = {}
    if args.get("brollStartUs") is not None:
        kw["broll_start_us"] = int(args["brollStartUs"])
    if args.get("brollEndUs") is not None:
        kw["broll_end_us"] = int(args["brollEndUs"])
    clip = E.add_broll(doc, aid, int(args["sourceAtUs"]), int(args["sourceEndUs"]), **kw)
    E.derive(doc)
    I.dump(doc, args["ir"])
    return json.dumps({"clipId": clip["id"], "assetId": aid, "report": V.validate(doc)},
                      ensure_ascii=False, indent=2)


def h_add_logo(args: dict) -> str:
    doc = I.load(args["ir"])
    aid, asset = P.ingest(args["asset"], args.get("assetId"))
    doc["assets"][aid] = asset
    E.add_logo(doc, aid, corner=args.get("corner", "top-right"),
               scale=float(args.get("scale", 0.16)), opacity=float(args.get("opacity", 0.9)))
    E.derive(doc)
    I.dump(doc, args["ir"])
    return json.dumps({"assetId": aid, "report": V.validate(doc)}, ensure_ascii=False, indent=2)


def h_add_title(args: dict) -> str:
    doc = I.load(args["ir"])
    kw = {}
    if args.get("background"):
        kw["background"] = args["background"]
    if args.get("bgColor"):
        kw["bg_color"] = args["bgColor"]
    dur = int(args.get("durUs", 1_500_000))
    pos = args.get("position", "custom")
    if pos == "intro":
        clip = E.add_intro(doc, args["text"], dur_us=dur, **kw)
    elif pos == "outro":
        clip = E.add_outro(doc, args["text"], dur_us=dur, **kw)
    else:
        clip = E.add_title(doc, args["text"], int(args["atUs"]), int(args["endUs"]), **kw)
    E.derive(doc)
    I.dump(doc, args["ir"])
    return json.dumps({"clipId": clip["id"], "report": V.validate(doc)}, ensure_ascii=False, indent=2)


def h_demo(args: dict) -> str:
    from .demo import run
    res = run(args["out_dir"])
    return json.dumps({"report": res["report"], "paths": res["paths"]}, ensure_ascii=False, indent=2)


HANDLERS: dict[str, Callable[[dict], str]] = {
    "ingest": h_ingest, "build_short": h_build_short, "validate": h_validate,
    "render": h_render, "add_broll": h_add_broll, "add_logo": h_add_logo,
    "add_title": h_add_title, "demo": h_demo,
}


# ------------------------------------------------------------------ jsonrpc
def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _result(rid: Any, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def handle(msg: dict) -> None:
    method = msg.get("method")
    rid = msg.get("id")
    is_request = "id" in msg

    if method == "initialize":
        ver = (msg.get("params") or {}).get("protocolVersion", PROTOCOL)
        _result(rid, {"protocolVersion": ver,
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "vertir", "version": "0.1.0"}})
        return
    if method in ("notifications/initialized", "notifications/cancelled"):
        return  # notification, no reply
    if method == "ping":
        _result(rid, {})
        return
    if method == "tools/list":
        _result(rid, {"tools": TOOLS})
        return
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            _result(rid, {"content": [{"type": "text", "text": f"unknown tool {name!r}"}], "isError": True})
            return
        try:
            text = fn(args)
            _result(rid, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:  # noqa: BLE001 - report tool errors, don't crash the loop
            tb = traceback.format_exc()
            _result(rid, {"content": [{"type": "text", "text": f"{exc}\n\n{tb}"}], "isError": True})
        return

    if is_request:
        _error(rid, -32601, f"method not found: {method}")


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle(msg)


if __name__ == "__main__":
    serve()
