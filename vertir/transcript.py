"""Word-level transcript: the LLM's primary reasoning surface.

Normalized shape (all times in integer microseconds, source-time of the asset):

    {"assetId": "hero",
     "words": [{"sourceAtUs": 0, "sourceEndUs": 500000, "text": "hola"}, ...]}

Transcription is a *pluggable adapter*. The core path loads a JSON transcript
(so the pipeline runs with no ML deps); `from_whisper_cpp_json` is provided for
when Whisper.cpp is available.
"""
from __future__ import annotations

import json
import re
from typing import Iterable

# Spanish + English filler words the cutter removes by default.
DEFAULT_FILLERS = {
    "eh", "ehh", "em", "este", "esto", "osea", "o", "sea", "tipo", "digamos",
    "um", "uh", "uhh", "hmm", "like", "you", "know", "so", "well", "actually",
}


def normalize_word(w: dict) -> dict:
    return {
        "sourceAtUs": int(w["sourceAtUs"]),
        "sourceEndUs": int(w["sourceEndUs"]),
        "text": str(w["text"]).strip(),
    }


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return normalize(data)


def normalize(data: dict) -> dict:
    words = [normalize_word(w) for w in data.get("words", [])]
    words.sort(key=lambda w: w["sourceAtUs"])
    # share boundaries to avoid 1us overlaps from independent rounding
    for i in range(1, len(words)):
        if words[i]["sourceAtUs"] < words[i - 1]["sourceEndUs"]:
            words[i]["sourceAtUs"] = words[i - 1]["sourceEndUs"]
    return {"assetId": data.get("assetId", ""), "words": words}


def is_filler(text: str, fillers: set[str] | None = None) -> bool:
    fillers = fillers if fillers is not None else DEFAULT_FILLERS
    clean = re.sub(r"[^\wáéíóúñü]", "", text.lower())
    return clean in fillers or clean == ""


def from_whisper_cpp_json(path: str, asset_id: str = "") -> dict:
    """Parse a whisper.cpp `--output-json-full` file into a normalized transcript.

    whisper.cpp emits token offsets in milliseconds. Best-effort; falls back to
    segment-level timing when per-token timing is absent.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    words: list[dict] = []
    for seg in data.get("transcription", []):
        toks = seg.get("tokens")
        if toks:
            for t in toks:
                txt = t.get("text", "").strip()
                if not txt or txt.startswith("[") or txt.startswith("<"):
                    continue
                off = t.get("offsets", {})
                words.append({
                    "sourceAtUs": int(off.get("from", 0)) * 1000,
                    "sourceEndUs": int(off.get("to", 0)) * 1000,
                    "text": txt,
                })
        else:
            off = seg.get("offsets", {})
            for tok in seg.get("text", "").split():
                words.append({
                    "sourceAtUs": int(off.get("from", 0)) * 1000,
                    "sourceEndUs": int(off.get("to", 0)) * 1000,
                    "text": tok,
                })
    return normalize({"assetId": asset_id, "words": words})


def synthetic(asset_id: str, phrases: Iterable[str], word_us: int = 380000,
              gap_us: int = 60000, start_us: int = 0) -> dict:
    """Build a deterministic transcript from phrases (for demos/tests).

    Inserts an explicit silence gap between phrases so the filler-cutter has
    something to remove.
    """
    words: list[dict] = []
    t = start_us
    for pi, phrase in enumerate(phrases):
        for tok in phrase.split():
            words.append({"sourceAtUs": t, "sourceEndUs": t + word_us, "text": tok})
            t += word_us + gap_us
        t += 700000  # silence between phrases
    return normalize({"assetId": asset_id, "words": words})
