"""Editorial Plan: schema, validation, and deterministic application."""
import unittest

from vertir import ir as I
from vertir import edit as E
from vertir import plan as PL
from vertir import render as R
from vertir import anim as A
from vertir import validate as V


def make_transcript(n_words: int = 40, word_us: int = 400_000) -> dict:
    """A dense, gap-free transcript so nothing is cut mechanically by accident."""
    words = []
    for i in range(n_words):
        words.append({"sourceAtUs": i * word_us,
                      "sourceEndUs": (i + 1) * word_us,
                      "text": f"w{i}"})
    return {"assetId": "hero", "words": words}


def make_ir(duration_us: int = 16_000_000) -> dict:
    doc = I.new_ir(title="t")
    doc["assets"]["hero"] = {
        "sha256": "0" * 64, "path": "/tmp/hero.mp4", "kind": "video",
        "probe": {"durationUs": duration_us, "hasAudio": True,
                  "fps": {"num": 30, "den": 1}, "w": 1080, "h": 1920},
    }
    return doc


class TestSpanAlgebra(unittest.TestCase):
    def test_merges_overlapping_spans(self):
        merged = PL._norm_spans([PL.span(0, 1000), PL.span(500, 2000), PL.span(5000, 6000)])
        self.assertEqual(merged, [(0, 2000), (5000, 6000)])

    def test_subtract_splits_a_span(self):
        out = PL._subtract([(0, 10_000)], [(3_000, 5_000)])
        self.assertEqual(out, [(0, 3_000), (5_000, 10_000)])

    def test_subtract_drops_fully_covered(self):
        self.assertEqual(PL._subtract([(2_000, 4_000)], [(0, 10_000)]), [])


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.tx = make_transcript()

    def test_baseline_plan_validates(self):
        plan = PL.baseline_plan(self.tx)
        self.assertTrue(PL.validate_plan(plan, self.tx)["ok"])

    def test_rejects_unknown_plan_version(self):
        plan = PL.new_plan()
        plan["planVersion"] = "9.0.0"
        rep = PL.validate_plan(plan, self.tx)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["errors"][0]["code"], "plan-version")

    def test_rejects_overlapping_keeps(self):
        plan = PL.new_plan()
        plan["keep"] = [PL.span(0, 5_000_000), PL.span(3_000_000, 8_000_000)]
        rep = PL.validate_plan(plan, self.tx)
        self.assertFalse(rep["ok"])
        self.assertIn("keep-overlap", [e["code"] for e in rep["errors"]])

    def test_rejects_repeat_hook(self):
        """`repeat` is out of v1 on purpose: a duplicated source range makes
        source->program lookup ambiguous."""
        plan = PL.new_plan()
        plan["hook"] = dict(PL.span(2_000_000, 4_000_000), mode="repeat")
        rep = PL.validate_plan(plan, self.tx)
        self.assertFalse(rep["ok"])
        self.assertIn("hook-mode", [e["code"] for e in rep["errors"]])

    def test_rejects_out_of_range_intensity(self):
        plan = PL.new_plan()
        plan["beats"] = [PL.punch_in(1_000_000, intensity=5.0)]
        rep = PL.validate_plan(plan, self.tx)
        self.assertFalse(rep["ok"])
        self.assertIn("beat-intensity", [e["code"] for e in rep["errors"]])

    def test_duration_overrun_beyond_tolerance_is_an_error(self):
        plan = PL.new_plan(target_duration_us=2_000_000)
        plan["keep"] = [PL.span(0, 16_000_000)]
        rep = PL.validate_plan(plan, self.tx)
        self.assertFalse(rep["ok"])
        self.assertIn("duration-overrun", [e["code"] for e in rep["errors"]])

    def test_small_overrun_is_only_a_warning(self):
        plan = PL.new_plan(target_duration_us=10_000_000)
        plan["keep"] = [PL.span(0, 10_500_000)]
        rep = PL.validate_plan(plan, self.tx)
        self.assertTrue(rep["ok"])
        self.assertIn("duration-over", [w["code"] for w in rep["warnings"]])

    def test_beat_in_cut_material_warns(self):
        plan = PL.new_plan()
        plan["keep"] = [PL.span(0, 4_000_000)]
        plan["beats"] = [PL.punch_in(9_000_000)]
        rep = PL.validate_plan(plan, self.tx)
        self.assertTrue(rep["ok"])
        self.assertIn("beat-in-cut", [w["code"] for w in rep["warnings"]])

    def test_empty_program_is_an_error(self):
        plan = PL.new_plan()
        plan["keep"] = [PL.span(0, 4_000_000)]
        plan["drop"] = [PL.span(0, 4_000_000)]
        rep = PL.validate_plan(plan, self.tx)
        self.assertFalse(rep["ok"])
        self.assertIn("empty-program", [e["code"] for e in rep["errors"]])


class TestApply(unittest.TestCase):
    def setUp(self):
        self.tx = make_transcript()

    def test_apply_is_deterministic(self):
        """Everything apply_plan produces must be replayable: clip ids included.
        (project.id is minted by new_ir, outside the planner's control.)"""
        import json
        plan = PL.baseline_plan(self.tx, outro_text="FOLLOW")
        plan["intro"] = {"text": "HOOK", "durUs": 1_000_000}
        a, b = make_ir(), make_ir()
        PL.apply_plan(a, plan, self.tx)
        PL.apply_plan(b, plan, self.tx)
        self.assertEqual(json.dumps(a["tracks"], sort_keys=True),
                         json.dumps(b["tracks"], sort_keys=True))

    def test_invalid_plan_raises_rather_than_degrading(self):
        plan = PL.new_plan()
        plan["keep"] = [PL.span(0, 5_000_000), PL.span(3_000_000, 8_000_000)]
        with self.assertRaises(ValueError):
            PL.apply_plan(make_ir(), plan, self.tx)

    def test_drop_removes_material(self):
        plan = PL.new_plan()
        plan["keep"] = [PL.span(0, 12_000_000)]
        plan["drop"] = [PL.span(4_000_000, 6_000_000)]
        doc = make_ir()
        PL.apply_plan(doc, plan, self.tx)
        self.assertEqual(doc["project"]["durationUs"], 10_000_000)
        self.assertEqual(len(I.main_track(doc)["clips"]), 2)

    def test_hook_is_moved_to_the_front(self):
        plan = PL.new_plan()
        plan["keep"] = [PL.span(0, 12_000_000)]
        plan["hook"] = PL.span(8_000_000, 10_000_000)
        doc = make_ir()
        PL.apply_plan(doc, plan, self.tx)
        clips = I.main_track(doc)["clips"]
        self.assertEqual(clips[0]["source"], {"startUs": 8_000_000, "endUs": 10_000_000})
        # moved, not copied: the hook range appears exactly once
        starts = [c["source"]["startUs"] for c in clips]
        self.assertEqual(starts.count(8_000_000), 1)
        # and total duration is unchanged by the reorder
        self.assertEqual(doc["project"]["durationUs"], 12_000_000)

    def test_captions_follow_a_reordered_hook(self):
        """The regression that a naive hook implementation would cause: captions
        are source-anchored, so they must land on the hook's NEW program position."""
        plan = PL.new_plan()
        plan["keep"] = [PL.span(0, 12_000_000)]
        plan["hook"] = PL.span(8_000_000, 10_000_000)
        doc = make_ir()
        PL.apply_plan(doc, plan, self.tx)

        cmap = E.build_cut_map(doc)
        # a word spoken at 8.2s of the source must now play in the first 2s
        self.assertLess(E.source_to_program(cmap, 8_200_000), 2_000_000)
        # while a word from the original opening now plays after the hook
        self.assertGreaterEqual(E.source_to_program(cmap, 100_000), 2_000_000)

        events = E.resolve_caption_events(doc)
        self.assertTrue(events)
        texts = [w["text"] for ev in events for w in ev["words"]]
        self.assertEqual(len(texts), len(set(texts)), "no caption word may appear twice")

    def test_punch_in_becomes_scale_keyframes(self):
        plan = PL.new_plan()
        plan["keep"] = [PL.span(0, 12_000_000)]
        plan["beats"] = [PL.punch_in(5_000_000, intensity=0.15, ramp_us=400_000)]
        doc = make_ir()
        rep = PL.apply_plan(doc, plan, self.tx)
        self.assertEqual(rep["beatsApplied"], 1)
        kfs = I.main_track(doc)["clips"][0]["keyframes"]
        self.assertEqual([k["prop"] for k in kfs], ["scale", "scale"])
        self.assertAlmostEqual(kfs[0]["v"], 1.0)
        self.assertAlmostEqual(kfs[1]["v"], 1.15)
        self.assertEqual(kfs[0]["atUs"], 5_000_000)   # clip-local, per IR spec 5
        self.assertEqual(kfs[1]["atUs"], 5_400_000)

    def test_emphasis_marks_caption_words(self):
        plan = PL.new_plan()
        plan["keep"] = [PL.span(0, 12_000_000)]
        plan["emphasis"] = [PL.span(2_000_000, 2_800_000)]
        doc = make_ir()
        rep = PL.apply_plan(doc, plan, self.tx)
        self.assertEqual(rep["wordsEmphasised"], 2)
        marked = [w for line in E.caption_track_of(doc)["lines"]
                  for w in line["words"] if w.get("emphasis")]
        self.assertEqual([w["text"] for w in marked], ["w5", "w6"])
        # and the flag survives the cut-map into render-ready events
        events = E.resolve_caption_events(doc)
        self.assertEqual(
            sum(1 for ev in events for w in ev["words"] if w.get("emphasis")), 2)

    def test_cards_are_added(self):
        plan = PL.new_plan()
        plan["keep"] = [PL.span(0, 12_000_000)]
        plan["intro"] = {"text": "HOOK", "durUs": 1_000_000}
        plan["outro"] = {"text": "FOLLOW", "durUs": 1_000_000}
        doc = make_ir()
        PL.apply_plan(doc, plan, self.tx)
        titles = E.title_track_of(doc)["clips"]
        self.assertEqual([c["text"]["content"] for c in titles], ["HOOK", "FOLLOW"])

    def test_applied_plan_produces_a_valid_ir(self):
        plan = PL.baseline_plan(self.tx, outro_text="FOLLOW")
        doc = make_ir()
        PL.apply_plan(doc, plan, self.tx)
        rep = V.validate(doc)
        self.assertTrue(rep["ok"], rep["errors"])


class TestPunchRendering(unittest.TestCase):
    """The keyframes must actually reach the filtergraph — an IR field nothing
    consumes is a silent no-op, which is worse than an unimplemented feature.

    The engine now lives in `vertir.anim`, shared with pan and gain curves.
    """

    def test_no_keyframes_means_no_filter(self):
        self.assertEqual(A.transform_filter({"keyframes": []}, 1080, 1920, 1.0), "")

    def test_flat_keyframes_mean_no_filter(self):
        clip = {"keyframes": [{"prop": "scale", "atUs": 0, "v": 1.0},
                              {"prop": "scale", "atUs": 500_000, "v": 1.0}]}
        self.assertEqual(A.transform_filter(clip, 1080, 1920, 1.0), "")

    def test_punch_filter_is_emitted(self):
        clip = {"keyframes": [{"prop": "scale", "atUs": 1_000_000, "v": 1.0, "ease": "easeInOut"},
                              {"prop": "scale", "atUs": 1_400_000, "v": 1.12, "ease": "linear"}]}
        f = A.transform_filter(clip, 1080, 1920, 1.0)
        self.assertIn("scale=w=", f)
        self.assertIn("eval=frame", f)
        self.assertIn("crop=1080:1920", f)
        self.assertIn("max(1,", f)          # never crop more than the frame has
        self.assertNotIn("zoompan", f)      # measured to shake on a subtle push

    def test_curve_holds_before_and_after(self):
        clip = {"keyframes": [{"prop": "scale", "atUs": 1_000_000, "v": 1.0, "ease": "linear"},
                              {"prop": "scale", "atUs": 1_400_000, "v": 1.12, "ease": "linear"}]}
        ks = clip["keyframes"]
        # behaviour, not expression shape: flat before the first, flat after the last
        self.assertAlmostEqual(A.sample(ks, "scale", 0, 1.0), 1.0)
        self.assertAlmostEqual(A.sample(ks, "scale", 900_000, 1.0), 1.0)
        self.assertAlmostEqual(A.sample(ks, "scale", 1_200_000, 1.0), 1.06)
        self.assertAlmostEqual(A.sample(ks, "scale", 9_000_000, 1.0), 1.12)
        self.assertIsNotNone(A.expr(ks, "scale", default=1.0, tvar="t"))

    def test_ignores_non_scale_props(self):
        clip = {"keyframes": [{"prop": "opacity", "atUs": 0, "v": 0.5}]}
        self.assertIsNone(A.expr(clip["keyframes"], "scale", default=1.0))

    def test_planner_easing_leaves_the_starting_keyframe(self):
        """Spec section 5: `ease` governs the segment LEAVING a keyframe, so the
        planner's smoothstep has to sit on the one the ramp starts from."""
        tx = make_transcript()
        doc = make_ir()
        plan = PL.new_plan()
        plan["keep"] = [PL.span(0, 16_000_000)]
        plan["beats"] = [PL.punch_in(1_200_000, intensity=0.12, ramp_us=400_000)]
        PL.apply_plan(doc, plan, tx)
        kfs = [k for k in I.main_track(doc)["clips"][0]["keyframes"]
               if k["prop"] == "scale"]
        self.assertEqual(len(kfs), 2)
        self.assertEqual(kfs[0]["ease"], "easeInOut")
        # midpoint of a smoothstep sits at the midpoint value; a linear ramp
        # would too, so check a quarter in, where the two curves differ
        mid = A.sample(kfs, "scale", kfs[0]["atUs"] + 100_000, 1.0)
        linear = 1.0 + 0.12 * 0.25
        self.assertNotAlmostEqual(mid, linear, places=3)


if __name__ == "__main__":
    unittest.main()
