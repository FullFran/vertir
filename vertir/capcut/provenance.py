"""The provenance map: which IR clip became which CapCut segment.

This is what makes the round trip survivable. Reconciliation must never rebuild
the IR from a draft — the IR carries *intent* (`reframe.focusX`, source-anchored
b-roll, `duck` rules) and a draft only carries *result*, so re-parsing recovers
positions and destroys the why. Instead we remember what we wrote, diff the draft
against that, and patch only the deltas back onto the IR.

None of that is possible without a stable identity linking the two sides, which
is this file.
"""
from __future__ import annotations

import json
import os

PROVENANCE_VERSION = "1.0.0"


class Provenance:
    """A persisted `ir_clip_id <-> capcut_ref` mapping plus the export snapshot."""

    FILENAME = "capcut.provenance.json"

    def __init__(self, ir_version: str = "", draft_path: str = "",
                 refs: dict[str, str] | None = None,
                 snapshot_path: str | None = None) -> None:
        self.ir_version = ir_version
        self.draft_path = draft_path
        # ir clip id -> capcut ref (the ref we declared in the compile spec)
        self.refs: dict[str, str] = dict(refs or {})
        # a copy of the draft exactly as exported, so `capcut diff` has a baseline
        self.snapshot_path = snapshot_path

    # ------------------------------------------------------------------ mapping
    def bind(self, ir_clip_id: str, capcut_ref: str) -> None:
        self.refs[ir_clip_id] = capcut_ref

    def to_ir(self, capcut_ref: str) -> str | None:
        for cid, ref in self.refs.items():
            if ref == capcut_ref:
                return cid
        return None

    def to_capcut(self, ir_clip_id: str) -> str | None:
        return self.refs.get(ir_clip_id)

    # ------------------------------------------------------------------ io
    def to_dict(self) -> dict:
        return {
            "provenanceVersion": PROVENANCE_VERSION,
            "irVersion": self.ir_version,
            "draftPath": self.draft_path,
            "snapshotPath": self.snapshot_path,
            "refs": self.refs,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Provenance":
        return cls(ir_version=d.get("irVersion", ""),
                   draft_path=d.get("draftPath", ""),
                   refs=d.get("refs") or {},
                   snapshot_path=d.get("snapshotPath"))

    def dump(self, out_dir: str) -> str:
        path = os.path.join(out_dir, self.FILENAME)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
        return path

    @classmethod
    def load(cls, path: str) -> "Provenance":
        if os.path.isdir(path):
            path = os.path.join(path, cls.FILENAME)
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))
