"""VertIR — an AI-first vertical video editor engine.

The LLM is a planner, never a renderer: it emits/mutates the declarative Timeline
IR (see docs/timeline-ir-v1.md); a fail-closed validator gates it; a deterministic
FFmpeg renderer executes it. This package is the engine (Slice 1 / core):
ingest -> transcript -> filler-cut + reframe + word-highlight captions -> validate
-> render proxy + MP4.
"""
from __future__ import annotations

from . import ir, probe, transcript, edit, validate, render
from .pipeline import build_short

__all__ = ["ir", "probe", "transcript", "edit", "validate", "render", "build_short"]
__version__ = "0.1.0"
