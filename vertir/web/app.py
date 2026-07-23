"""Minimal web-tweaker (stdlib http.server, zero deps).

The primary human-finish surface: shows the proxy preview and lets a human do the
common finishing edits (fix caption text, adjust music volume) that write back to
the IR and re-render. Works in any browser, including a phone's.

    python -m vertir web --ir out/timeline.ir.json --dir out
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import ir as I
from .. import edit as E
from .. import validate as V
from .. import render as R

STATE: dict[str, str] = {"ir_path": "", "work_dir": "."}
_HERE = os.path.dirname(os.path.abspath(__file__))


def _caption_lines(doc: dict) -> list[dict]:
    cap = E.caption_track_of(doc)
    if not cap:
        return []
    out = []
    for i, line in enumerate(cap["lines"]):
        out.append({"i": i, "text": " ".join(w["text"] for w in line["words"])})
    return out


def _bgm_gain(doc: dict):
    for t in doc["tracks"]:
        if t.get("kind") == "audio" and t.get("role") == "bgm" and t.get("clips"):
            return t["clips"][0].get("gainDb", -18.0)
    return None


def _apply_edits(doc: dict, edits: dict) -> None:
    cap = E.caption_track_of(doc)
    if cap:
        for e in edits.get("captions", []):
            i = e["i"]
            if not (0 <= i < len(cap["lines"])):
                continue
            words = cap["lines"][i]["words"]
            toks = e["text"].split()
            if len(toks) == len(words):
                for w, tok in zip(words, toks):
                    w["text"] = tok
            elif toks and words:
                span0, span1 = words[0]["sourceAtUs"], words[-1]["sourceEndUs"]
                step = max(1, (span1 - span0) // len(toks))
                cap["lines"][i]["words"] = [
                    {"sourceAtUs": span0 + k * step,
                     "sourceEndUs": span0 + (k + 1) * step, "text": tok}
                    for k, tok in enumerate(toks)
                ]
    if "bgmGainDb" in edits:
        for t in doc["tracks"]:
            if t.get("kind") == "audio" and t.get("role") == "bgm" and t.get("clips"):
                t["clips"][0]["gainDb"] = float(edits["bgmGainDb"])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        if not os.path.exists(path):
            self.send_error(404)
            return
        data = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._file(os.path.join(_HERE, "static", "index.html"), "text/html; charset=utf-8")
        elif path == "/api/ir":
            doc = I.load(STATE["ir_path"])
            self._json({"captions": _caption_lines(doc), "bgmGainDb": _bgm_gain(doc),
                        "durationUs": doc["project"].get("durationUs")})
        elif path == "/preview.mp4":
            self._file(os.path.join(STATE["work_dir"], "preview.mp4"), "video/mp4")
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/save":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", 0))
        edits = json.loads(self.rfile.read(n) or b"{}")
        doc = I.load(STATE["ir_path"])
        _apply_edits(doc, edits)
        E.derive(doc)
        rep = V.validate(doc)
        if not rep["ok"]:
            self._json({"ok": False, "report": rep})
            return
        I.dump(doc, STATE["ir_path"])
        R.render(doc, os.path.join(STATE["work_dir"], "preview.mp4"), proxy=True)
        self._json({"ok": True, "report": rep})


def serve(ir_path: str, work_dir: str, port: int = 8747) -> None:
    STATE["ir_path"] = os.path.abspath(ir_path)
    STATE["work_dir"] = os.path.abspath(work_dir)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"vertir web-tweaker on http://localhost:{port}  (ir={ir_path})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
