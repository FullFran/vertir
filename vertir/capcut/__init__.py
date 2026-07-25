"""CapCut / JianYing adapter.

The IR stays the contract. This package is an *adapter*, not a second renderer:
FFmpeg remains the only compositor for final pixels, and the model keeps mutating
the IR without ever touching a draft.

    timeline.ir.json --> spec.json --> `capcut compile` --> CapCut draft
       (the contract)     (spec.py)      (bridge.py)      + provenance map

Everything here is an *optional* dependency: `capcut-cli` needs Node >= 18, and
the core promise of this project is stdlib Python + ffmpeg. Nothing in this
package is imported eagerly by the pipeline, and every entry point degrades to a
clear error rather than a broken draft when the CLI is absent.
"""
from .bridge import CapCutUnavailable, available, doctor, version  # noqa: F401
from .spec import build_spec, export  # noqa: F401
from .provenance import Provenance  # noqa: F401
