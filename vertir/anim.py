"""Keyframe curves: the IR's animation, evaluated two ways.

`sample()` walks a curve in Python — the reference semantics, and what a CapCut
export or a web-tweaker preview needs. `expr()` compiles the same curve into a
closed-form FFmpeg expression that the renderer evaluates once per frame. The
two must agree: `sample()` is the spec, `expr()` is the renderer's copy of it.

A closed-form expression is used rather than a `sendcmd` script because the IR's
keyframes are sparse and their easing is defined: interpolating in the
expression is exact, needs no temporary file, and stays deterministic. Note that
`crop` cannot animate its w/h (those expressions are evaluated once, at
configuration time), which is why an animated scale rides `zoompan` instead.

Spec: docs/timeline-ir-v1.md §5 (keyframes) and §6 (transform stack). `atUs` is
relative to the start of the clip in program time; `ease` describes the segment
*leaving* a keyframe, which is what makes `hold` mean "keep this value until the
next one".
"""
from __future__ import annotations

from typing import Any

from . import ir as I

# Props the renderer can actually execute today, by context. Anything else in
# KEYFRAME_PROPS is still valid IR — the validator warns that it will not show.
RENDERED_VISUAL = {"scale", "x", "y"}
RENDERED_AUDIO = {"gainDb"}


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _num(v: float, nd: int) -> str:
    """Fixed-point (never scientific): FFmpeg's expression parser has no 1e-05."""
    return f"{v:.{nd}f}"


def curve(keyframes: list[dict] | None, prop: str) -> list[dict]:
    """The keyframes for `prop`, in time order, with ties resolved last-wins.

    Malformed entries are dropped rather than raised on: the validator is what
    reports them, and a renderer must never crash on IR it was handed.
    """
    ks = [k for k in (keyframes or [])
          if isinstance(k, dict) and k.get("prop") == prop
          and _is_num(k.get("atUs")) and _is_num(k.get("v"))]
    ks.sort(key=lambda k: k["atUs"])
    out: list[dict] = []
    for k in ks:
        if out and out[-1]["atUs"] == k["atUs"]:
            out[-1] = k          # spec §5: on a tie the last keyframe wins
        else:
            out.append(k)
    return out


def scaled(keyframes: list[dict] | None, prop: str, factor: float) -> list[dict]:
    """Same curve with its values multiplied — pixel props under a proxy canvas."""
    return [dict(k, v=float(k["v"]) * factor) for k in curve(keyframes, prop)]


def _ease_u(u: float, ease: str) -> float:
    if ease == "hold":
        return 0.0
    if ease == "easeInOut":
        return u * u * (3.0 - 2.0 * u)   # smoothstep: deterministic, no overshoot
    return u


def sample(keyframes: list[dict] | None, prop: str, at_us: int,
           default: float = 0.0) -> float:
    """The value of `prop` at `at_us` (clip-relative microseconds)."""
    ks = curve(keyframes, prop)
    if not ks:
        return float(default)
    if at_us <= ks[0]["atUs"]:
        return float(ks[0]["v"])
    if at_us >= ks[-1]["atUs"]:
        return float(ks[-1]["v"])
    for a, b in zip(ks, ks[1:]):
        if a["atUs"] <= at_us < b["atUs"]:
            span = b["atUs"] - a["atUs"]
            v0, v1 = float(a["v"]), float(b["v"])
            u = _ease_u((at_us - a["atUs"]) / span, a.get("ease", "linear"))
            return v0 + (v1 - v0) * u
    return float(ks[-1]["v"])


def _segment(a: dict, b: dict, tvar: str, nd: int) -> str:
    v0, v1 = float(a["v"]), float(b["v"])
    ease = a.get("ease", "linear")
    span = b["atUs"] - a["atUs"]
    if ease == "hold" or span <= 0 or abs(v1 - v0) < 10 ** -nd:
        return _num(v0, nd)
    t0 = a["atUs"] / 1_000_000
    dt = span / 1_000_000
    # clip() pins the ends, so the first segment also covers everything before it
    u = f"clip(({tvar}-{t0:.6f})/{dt:.6f},0,1)"
    if ease == "easeInOut":
        u = f"({u}*{u}*(3-2*{u}))"
    return f"({_num(v0, nd)}+({_num(v1 - v0, nd)})*{u})"


def expr(keyframes: list[dict] | None, prop: str, default: float = 0.0,
         tvar: str = "t", nd: int = 4) -> str | None:
    """Compile a curve into an FFmpeg expression, or None when there is nothing
    to render — no keyframes, or a constant curve already equal to `default`."""
    ks = curve(keyframes, prop)
    if not ks:
        return None
    vals = [float(k["v"]) for k in ks]
    eps = 10 ** -nd
    if max(vals) - min(vals) < eps:
        # constant: still emitted when it is a static offset, skipped when identity
        return None if abs(vals[-1] - float(default)) < eps else _num(vals[-1], nd)
    out = _num(vals[-1], nd)                       # value past the last keyframe
    for a, b in zip(reversed(ks[:-1]), reversed(ks[1:])):
        out = f"if(lt({tvar},{b['atUs'] / 1_000_000:.6f}),{_segment(a, b, tvar, nd)},{out})"
    return out


# ------------------------------------------------------------------ renderers
def zoompan_filter(clip: dict, cw: int, ch: int, scale_f: float,
                   fps_r: str) -> str | None:
    """The animated `transform` delta for a main-track clip, or None.

    Placed *after* the reframe (spec §6: transform is a delta on top of the base
    framing, never a replacement), so the filter's input is already the canvas:
    iw == cw and ih == ch. `zoom` is therefore the magnification directly, and a
    content shift of X canvas pixels to the right is a crop-window shift of
    X/zoom input pixels to the left.
    """
    ks = clip.get("keyframes") or []
    z = expr(ks, "scale", default=1.0, tvar="in_time", nd=4)
    # x/y are canvas pixels, so a half-size proxy canvas halves them
    x = expr(scaled(ks, "x", scale_f), "x", default=0.0, tvar="in_time", nd=3)
    y = expr(scaled(ks, "y", scale_f), "y", default=0.0, tvar="in_time", nd=3)
    if z is None and x is None and y is None:
        return None
    z = z or "1.0000"
    xe = f"(iw-iw/zoom)/2" + (f"-({x})/zoom" if x else "")
    ye = f"(ih-ih/zoom)/2" + (f"-({y})/zoom" if y else "")
    # clip() keeps the window inside the frame: an over-pushed pan would
    # otherwise expose the edge of the source
    return (f"zoompan=z='{z}':x='clip({xe},0,iw-iw/zoom)':"
            f"y='clip({ye},0,ih-ih/zoom)':d=1:s={cw}x{ch}:fps={fps_r}")


def volume_filter(clip: dict, default_db: float) -> str:
    """A `volume` filter for an audio clip, animated when it has gainDb curves.

    dB is converted to linear amplitude in the expression: the `NdB` suffix is a
    literal-only form and does not survive an expression.
    """
    e = expr(clip.get("keyframes"), "gainDb", default=default_db, tvar="t", nd=1)
    if e is None:
        return f"volume={default_db:.2f}dB"
    return f"volume=volume='pow(10,({e})/20)':eval=frame"


def rendered_props(context: str) -> set[str]:
    """Which keyframe props the renderer executes in a given clip context."""
    if context == "main":
        # a main clip carries both its picture and its own audio
        return RENDERED_VISUAL | RENDERED_AUDIO
    if context == "audio":
        return set(RENDERED_AUDIO)
    return set()


def props_used(keyframes: list[dict] | None) -> set[str]:
    return {k["prop"] for k in (keyframes or [])
            if isinstance(k, dict) and k.get("prop") in I.KEYFRAME_PROPS}
