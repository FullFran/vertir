"""The Editorial Plan — the layer where the model's judgement lives.

The IR says *what the video is*. The plan says *why it is that way*: which
moment is the hook, where the pace tightens, which beats get a visual punch,
which words carry the emphasis.

    transcript --> [ LLM ] --> plan.json --> apply_plan() --> IR --> render
                              (judgement)   (deterministic)

Same split that makes the rest of this codebase work: the model judges, the code
executes. The model never emits an IR — it emits a plan, and `apply_plan` turns
that plan into IR mutations. The plan stays small enough for a human to read,
diff and replay, and it is persisted next to the receipts so future planners can
learn from what actually performed.

All times are **source** microseconds of the hero asset, so a plan is invariant
under re-cutting — exactly like b-roll anchors and captions already are.
"""
from __future__ import annotations

from typing import Any

from . import ir as I
from . import edit as E
from . import transcript as T

PLAN_VERSION = "1.0.0"
SUPPORTED_PLAN_MAJOR = 1

BEAT_KINDS = {"punchIn"}
HOOK_MODES = {"move"}

DEFAULT_PUNCH_INTENSITY = 0.10
DEFAULT_PUNCH_RAMP_US = 400_000
MIN_PUNCH_INTENSITY = 0.02
MAX_PUNCH_INTENSITY = 0.60

# How far a plan may overshoot targetDurationUs before it stops being a warning.
DURATION_TOLERANCE = 0.10


# ------------------------------------------------------------------ construction
def new_plan(target_duration_us: int | None = None) -> dict:
    """An empty but valid plan. `keep` empty means "use the mechanical cut"."""
    return {
        "planVersion": PLAN_VERSION,
        "targetDurationUs": target_duration_us,
        "hook": None,
        "keep": [],
        "drop": [],
        "beats": [],
        "emphasis": [],
        "intro": None,
        "outro": None,
    }


def span(source_at_us: int, source_end_us: int, **extra) -> dict:
    d = {"sourceAtUs": int(source_at_us), "sourceEndUs": int(source_end_us)}
    d.update(extra)
    return d


def punch_in(source_at_us: int, intensity: float = DEFAULT_PUNCH_INTENSITY,
             ramp_us: int = DEFAULT_PUNCH_RAMP_US) -> dict:
    return {"kind": "punchIn", "sourceAtUs": int(source_at_us),
            "intensity": float(intensity), "rampUs": int(ramp_us)}


def baseline_plan(transcript: dict, *, target_duration_us: int | None = None,
                  beat_every_us: int = 5_000_000,
                  intensity: float = DEFAULT_PUNCH_INTENSITY,
                  max_gap_us: int = 450_000, outro_text: str | None = None) -> dict:
    """A sane, fully deterministic plan built with no LLM at all.

    This is the graceful-degradation path: it keeps the mechanical filler cut and
    adds a regular punch-in cadence so the pipeline still produces a watchable
    short when no model is driving it. It deliberately picks no hook and no
    emphasis — those are judgement calls, and guessing them badly is worse than
    leaving them out.
    """
    plan = new_plan(target_duration_us)
    segs = E.kept_segments(transcript.get("words", []), max_gap_us=max_gap_us)
    plan["keep"] = [span(s, e) for s, e in segs]

    if beat_every_us > 0:
        # Walk program time, but place beats at the source time that program
        # time corresponds to, so the plan stays source-anchored.
        prog = 0
        next_beat = beat_every_us
        for s, e in segs:
            dur = e - s
            while next_beat < prog + dur:
                plan["beats"].append(punch_in(s + (next_beat - prog), intensity))
                next_beat += beat_every_us
            prog += dur

    if outro_text:
        plan["outro"] = {"text": outro_text, "durUs": 1_500_000}
    return plan


# ------------------------------------------------------------------ span algebra
def _norm_spans(spans: list[dict]) -> list[tuple[int, int]]:
    """Sort + merge overlapping/adjacent spans into disjoint (start, end) pairs."""
    pairs = sorted((int(s["sourceAtUs"]), int(s["sourceEndUs"])) for s in spans)
    out: list[tuple[int, int]] = []
    for s, e in pairs:
        if e <= s:
            continue
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _subtract(keeps: list[tuple[int, int]],
              drops: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Remove every drop window from the keep set, splitting spans as needed."""
    out = list(keeps)
    for ds, de in drops:
        nxt: list[tuple[int, int]] = []
        for s, e in out:
            if de <= s or ds >= e:          # disjoint
                nxt.append((s, e))
                continue
            if s < ds:                       # left remainder
                nxt.append((s, ds))
            if de < e:                       # right remainder
                nxt.append((de, e))
        out = nxt
    return out


def _contains(spans: list[tuple[int, int]], us: int) -> bool:
    return any(s <= us < e for s, e in spans)


def resolve_segments(plan: dict, transcript: dict, *,
                     max_gap_us: int = 450_000) -> list[tuple[int, int]]:
    """The plan's final source segments, in program order.

    `keep` empty falls back to the mechanical filler cut, so a plan that only
    specifies a hook still works. The hook is *moved* to the front (see
    `validate_plan` for why repeating is not supported in v1).

    The hook span is taken verbatim rather than intersected with the keeps: a
    hook is one continuous take, and chopping a micro-pause out of the middle of
    it is exactly what makes an opener feel spliced.
    """
    keeps = _norm_spans(plan.get("keep") or [])
    if not keeps:
        keeps = [(s, e) for s, e in
                 E.kept_segments(transcript.get("words", []), max_gap_us=max_gap_us)]
    segs = _subtract(keeps, _norm_spans(plan.get("drop") or []))

    hook = plan.get("hook")
    if not hook:
        return segs

    hs, he = int(hook["sourceAtUs"]), int(hook["sourceEndUs"])
    # The hook is lifted out of its original position and placed first. What
    # remains of the timeline keeps its original order.
    rest = _subtract(segs, [(hs, he)])
    return [(hs, he)] + rest


# ------------------------------------------------------------------ validation
class _Report:
    def __init__(self) -> None:
        self.errors: list[dict] = []
        self.warnings: list[dict] = []

    def err(self, code: str, msg: str, where: str = "") -> None:
        self.errors.append({"code": code, "msg": msg, "where": where})

    def warn(self, code: str, msg: str, where: str = "") -> None:
        self.warnings.append({"code": code, "msg": msg, "where": where})

    def to_dict(self) -> dict:
        return {"ok": not self.errors, "errors": self.errors, "warnings": self.warnings}


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _span_ok(s: Any) -> bool:
    return (isinstance(s, dict) and _is_int(s.get("sourceAtUs"))
            and _is_int(s.get("sourceEndUs")) and 0 <= s["sourceAtUs"] < s["sourceEndUs"])


def validate_plan(plan: dict, transcript: dict, *,
                  source_duration_us: int | None = None) -> dict:
    """Fail-closed plan validation, mirroring `validate.validate` for the IR.

    Errors block application; warnings do not. There is no force flag.
    """
    r = _Report()

    ver = str(plan.get("planVersion", "0"))
    try:
        major = int(ver.split(".")[0])
    except ValueError:
        major = 0
    if major != SUPPORTED_PLAN_MAJOR:
        r.err("plan-version",
              f"planVersion major {major} unsupported (expected {SUPPORTED_PLAN_MAJOR})",
              "planVersion")
        return r.to_dict()

    words = transcript.get("words", [])
    src_end = source_duration_us
    if src_end is None and words:
        src_end = max(w["sourceEndUs"] for w in words)

    def _check_bounds(s: dict, where: str) -> None:
        if src_end is not None and s["sourceAtUs"] >= src_end:
            r.err("span-oob", f"{where} starts at {s['sourceAtUs']}us, past the source end "
                              f"({src_end}us)", where)

    for field in ("keep", "drop", "emphasis"):
        items = plan.get(field) or []
        if not isinstance(items, list):
            r.err("plan-shape", f"{field} must be a list", field)
            continue
        for i, s in enumerate(items):
            if not _span_ok(s):
                r.err("bad-span", f"{field}[{i}] is not a valid source span", f"{field}[{i}]")
            else:
                _check_bounds(s, f"{field}[{i}]")

    # keep spans must not overlap each other: overlapping keeps would emit the
    # same source twice and make source->program lookup ambiguous.
    raw_keeps = [s for s in (plan.get("keep") or []) if _span_ok(s)]
    if len(_norm_spans(raw_keeps)) != len(raw_keeps):
        r.err("keep-overlap", "keep spans overlap or touch; they must be disjoint", "keep")

    hook = plan.get("hook")
    if hook is not None:
        if not _span_ok(hook):
            r.err("bad-hook", "hook is not a valid source span", "hook")
        else:
            _check_bounds(hook, "hook")
            mode = hook.get("mode", "move")
            if mode not in HOOK_MODES:
                # "repeat" (cold open that plays twice) is deliberately not in v1:
                # a duplicated source range makes `edit.source_to_program` ambiguous,
                # so captions and b-roll would silently bind to the wrong copy.
                r.err("hook-mode",
                      f"hook mode {mode!r} unsupported in plan v1 (only {sorted(HOOK_MODES)})",
                      "hook.mode")
            dur = hook["sourceEndUs"] - hook["sourceAtUs"]
            if dur < 500_000:
                r.warn("hook-short", f"hook is only {dur}us; under ~0.5s it reads as a glitch",
                       "hook")
            if dur > 5_000_000:
                r.warn("hook-long", f"hook is {dur}us; a hook past ~5s stops being a hook",
                       "hook")

    segs = resolve_segments(plan, transcript) if not r.errors else []
    if not segs and not r.errors:
        r.err("empty-program", "the plan keeps no material at all", "keep")

    for i, b in enumerate(plan.get("beats") or []):
        where = f"beats[{i}]"
        if not isinstance(b, dict) or b.get("kind") not in BEAT_KINDS:
            r.err("bad-beat", f"{where} has unknown kind {b.get('kind') if isinstance(b, dict) else b!r}",
                  where)
            continue
        if not _is_int(b.get("sourceAtUs")) or b["sourceAtUs"] < 0:
            r.err("bad-beat", f"{where} has an invalid sourceAtUs", where)
            continue
        k = b.get("intensity", DEFAULT_PUNCH_INTENSITY)
        if not isinstance(k, (int, float)) or not (MIN_PUNCH_INTENSITY <= k <= MAX_PUNCH_INTENSITY):
            r.err("beat-intensity",
                  f"{where} intensity {k} outside [{MIN_PUNCH_INTENSITY}, {MAX_PUNCH_INTENSITY}]",
                  where)
        if segs and not _contains(segs, b["sourceAtUs"]):
            r.warn("beat-in-cut", f"{where} lands in cut material; it will not fire", where)

    for i, s in enumerate(plan.get("emphasis") or []):
        if _span_ok(s) and segs and not _contains(segs, s["sourceAtUs"]):
            r.warn("emphasis-in-cut", f"emphasis[{i}] lands in cut material", f"emphasis[{i}]")

    for field in ("intro", "outro"):
        card = plan.get(field)
        if card is None:
            continue
        if not isinstance(card, dict) or not str(card.get("text", "")).strip():
            r.err("card-empty", f"{field} has no text", field)
        elif not _is_int(card.get("durUs", 1_500_000)) or card.get("durUs", 1) <= 0:
            r.err("card-dur", f"{field} durUs must be a positive int", field)

    target = plan.get("targetDurationUs")
    if target is not None and segs:
        total = sum(e - s for s, e in segs)
        if total > target * (1 + DURATION_TOLERANCE):
            r.err("duration-overrun",
                  f"plan runs {total}us against a {target}us target "
                  f"(over the {int(DURATION_TOLERANCE * 100)}% tolerance) — cut more or raise the target",
                  "targetDurationUs")
        elif total > target:
            r.warn("duration-over", f"plan runs {total}us against a {target}us target",
                   "targetDurationUs")

    return r.to_dict()


# ------------------------------------------------------------------ application
def _apply_beats(ir: dict, plan: dict) -> int:
    """Turn punch-in beats into `scale` keyframes on the main clip that contains
    them. Keyframe `atUs` is clip-local, per IR spec §5."""
    cmap = E.build_cut_map(ir)
    by_id = {c["id"]: c for c in I.main_track(ir)["clips"]}
    applied = 0
    for b in plan.get("beats") or []:
        if b.get("kind") != "punchIn":
            continue
        ent = E._seg_containing(cmap, int(b["sourceAtUs"]))
        if ent is None:
            continue
        clip = by_id.get(ent["clipId"])
        if clip is None:
            continue
        clip_dur = ent["progEndUs"] - ent["progStartUs"]
        local = int(b["sourceAtUs"]) - ent["srcStartUs"]
        ramp = max(1, int(b.get("rampUs", DEFAULT_PUNCH_RAMP_US)))
        end = min(clip_dur, local + ramp)
        if end <= local:
            continue
        k = float(b.get("intensity", DEFAULT_PUNCH_INTENSITY))
        kfs = clip.setdefault("keyframes", [])
        # `ease` governs the segment LEAVING a keyframe (spec section 5: "hold =
        # mantiene hasta el proximo kf"), so the smoothstep goes on the keyframe
        # the ramp starts from, not the one it arrives at
        kfs.append({"prop": "scale", "atUs": local, "v": 1.0, "ease": "easeInOut"})
        kfs.append({"prop": "scale", "atUs": end, "v": round(1.0 + k, 4), "ease": "linear"})
        kfs.sort(key=lambda kf: (kf["prop"], kf["atUs"]))
        applied += 1
    return applied


def _apply_emphasis(ir: dict, plan: dict) -> int:
    """Flag caption words overlapping an emphasis window. The renderer colours
    them with `caption.style.emphasisColor`."""
    wins = _norm_spans([s for s in (plan.get("emphasis") or []) if _span_ok(s)])
    if not wins:
        return 0
    track = E.caption_track_of(ir)
    if not track:
        return 0
    n = 0
    for line in track.get("lines", []):
        for w in line.get("words", []):
            # any temporal overlap counts, so a window need not align to word edges
            if any(w["sourceAtUs"] < e and s < w["sourceEndUs"] for s, e in wins):
                w["emphasis"] = True
                n += 1
    return n


def apply_plan(ir: dict, plan: dict, transcript: dict, *,
               asset_id: str | None = None, max_gap_us: int = 450_000,
               focus_x: float = 0.5, focus_y: float = 0.4,
               reframe_mode: str = "cover") -> dict:
    """Apply an editorial plan to an IR that already has its hero asset ingested.

    Deterministic: the same (ir, plan, transcript) yields the same IR every time,
    including clip ids, which are derived from the segment index rather than
    randomly generated. That is what makes a plan replayable and diffable.

    Returns a report of what was applied. Raises ValueError if the plan does not
    validate — this is fail-closed, like the IR validator.
    """
    if asset_id is None:
        asset_id = transcript.get("assetId") or next(iter(ir.get("assets", {})), None)
    if asset_id is None or asset_id not in ir.get("assets", {}):
        raise ValueError(f"apply_plan: asset {asset_id!r} not present in the IR")

    report = validate_plan(plan, transcript,
                           source_duration_us=ir["assets"][asset_id]
                           .get("probe", {}).get("durationUs"))
    if not report["ok"]:
        raise ValueError(f"apply_plan: invalid plan: {report['errors']}")

    segs = resolve_segments(plan, transcript, max_gap_us=max_gap_us)

    dur = ir["assets"][asset_id].get("probe", {}).get("durationUs")
    if dur:
        segs = [(s, min(e, dur)) for s, e in segs if s < dur]

    # Deterministic clip ids: index-based, never uuid.
    track = I.main_track(ir)
    track["clips"] = [
        I.main_clip(asset_id, s, e, reframe_mode, focus_x, focus_y, clip_id=f"c{i:04d}")
        for i, (s, e) in enumerate(segs)
    ]
    E.derive(ir)

    if E.caption_track_of(ir) is None:
        E.captions_from_transcript(ir, transcript)

    beats = _apply_beats(ir, plan)
    emphasised = _apply_emphasis(ir, plan)

    # explicit clip ids: title_clip would otherwise mint a uuid and break replay
    if plan.get("intro"):
        E.add_intro(ir, plan["intro"]["text"],
                    dur_us=int(plan["intro"].get("durUs", 1_500_000)), clip_id="intro")
    if plan.get("outro"):
        E.add_outro(ir, plan["outro"]["text"],
                    dur_us=int(plan["outro"].get("durUs", 1_500_000)), clip_id="outro")

    E.derive(ir)
    ir.setdefault("project", {})["planVersion"] = plan.get("planVersion", PLAN_VERSION)

    return {
        "ok": True,
        "segments": len(segs),
        "durationUs": ir["project"]["durationUs"],
        "hook": bool(plan.get("hook")),
        "beatsApplied": beats,
        "wordsEmphasised": emphasised,
        "warnings": report["warnings"],
    }


# ------------------------------------------------------------------ io
def load(path: str) -> dict:
    import json
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def dump(plan: dict, path: str) -> None:
    import json
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------ model-facing
def plan_brief(transcript: dict, *, target_duration_us: int | None = None) -> dict:
    """Everything a model needs to author a plan, and nothing else.

    Returned by the `propose_plan` MCP tool: the transcript to reason over plus
    the schema and the editorial rules to honour. The model replies with a plan
    document, which then goes through `validate_plan` before it can touch the IR.
    """
    words = transcript.get("words", [])
    src_end = max((w["sourceEndUs"] for w in words), default=0)
    return {
        "planVersion": PLAN_VERSION,
        "sourceDurationUs": src_end,
        "targetDurationUs": target_duration_us,
        "transcript": transcript,
        "schema": {
            "planVersion": "string, must be 1.x",
            "targetDurationUs": "int|null — the length you are editing toward",
            "hook": "{sourceAtUs, sourceEndUs}|null — the strongest 1-3s; it is MOVED to the front",
            "keep": "[{sourceAtUs, sourceEndUs}] — disjoint spans to keep, in order; [] means use the mechanical filler cut",
            "drop": "[{sourceAtUs, sourceEndUs, reason}] — subtracted from keep",
            "beats": "[{kind:'punchIn', sourceAtUs, intensity 0.02-0.6, rampUs}] — pattern interrupts",
            "emphasis": "[{sourceAtUs, sourceEndUs}] — caption words to colour",
            "intro": "{text, durUs}|null",
            "outro": "{text, durUs}|null — the payoff / CTA",
        },
        "rules": [
            "All times are SOURCE microseconds of the hero asset, never program time.",
            "The hook is moved, not copied: it will not play again later.",
            "keep spans must be disjoint and in the order you want them played.",
            "Aim for a visual change every 3-5s; punchIn beats are the cheapest one.",
            "Emphasise the few words that carry the claim, not every noun.",
            "Overrunning targetDurationUs by more than 10% is a hard error.",
        ],
    }
