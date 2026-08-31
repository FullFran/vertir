"""Unit tests (stdlib unittest, no pytest needed):

    python -m unittest discover -s tests -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vertir import ir as I
from vertir import edit as E
from vertir import validate as V
from vertir import transcript as T
from vertir import render as R
from vertir import anim as A


def sample_ir():
    doc = I.new_ir(title="t")
    doc["assets"]["hero"] = {
        "sha256": "x", "path": "/tmp/hero.mp4", "kind": "video",
        "probe": {"durationUs": 20_000_000, "w": 1280, "h": 720,
                  "fps": {"num": 30, "den": 1}, "hasAudio": True, "sampleRateHz": 48000},
    }
    return doc


class TestTranscript(unittest.TestCase):
    def test_shared_boundaries(self):
        tx = T.normalize({"words": [
            {"sourceAtUs": 0, "sourceEndUs": 500001, "text": "a"},
            {"sourceAtUs": 500000, "sourceEndUs": 900000, "text": "b"},  # overlaps by 1us
        ]})
        self.assertEqual(tx["words"][1]["sourceAtUs"], 500001)

    def test_filler(self):
        self.assertTrue(T.is_filler("eh"))
        self.assertTrue(T.is_filler("O SEA".split()[0]))
        self.assertFalse(T.is_filler("motor"))


class TestEdit(unittest.TestCase):
    def test_kept_segments_splits_on_silence(self):
        words = [
            {"sourceAtUs": 0, "sourceEndUs": 400000, "text": "uno"},
            {"sourceAtUs": 450000, "sourceEndUs": 800000, "text": "dos"},
            # big silence gap here (>450ms)
            {"sourceAtUs": 3_000_000, "sourceEndUs": 3_400_000, "text": "tres"},
        ]
        segs = E.kept_segments(words, max_gap_us=450000, pad_us=0)
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0][0], 0)

    def test_cut_map_and_derive(self):
        doc = sample_ir()
        tx = {"words": [
            {"sourceAtUs": 1_000_000, "sourceEndUs": 2_000_000, "text": "hola"},
            {"sourceAtUs": 2_050_000, "sourceEndUs": 3_000_000, "text": "mundo"},
            {"sourceAtUs": 8_000_000, "sourceEndUs": 9_000_000, "text": "chau"},
        ]}
        E.cut_fillers(doc, T.normalize(tx), "hero", pad_us=0)
        cmap = E.build_cut_map(doc)
        self.assertEqual(len(cmap), 2)  # two speech segments
        # program time is contiguous starting at 0
        self.assertEqual(cmap[0]["progStartUs"], 0)
        self.assertEqual(cmap[1]["progStartUs"], cmap[0]["progEndUs"])
        self.assertEqual(doc["project"]["durationUs"], cmap[-1]["progEndUs"])

    def test_source_to_program_maps_and_drops(self):
        doc = sample_ir()
        tx = {"words": [
            {"sourceAtUs": 1_000_000, "sourceEndUs": 2_000_000, "text": "hola"},
            {"sourceAtUs": 8_000_000, "sourceEndUs": 9_000_000, "text": "chau"},
        ]}
        E.cut_fillers(doc, T.normalize(tx), "hero", pad_us=0)
        cmap = E.build_cut_map(doc)
        # a source time inside the first kept segment maps
        self.assertIsNotNone(E.source_to_program(cmap, 1_500_000))
        # a source time in the cut gap does not
        self.assertIsNone(E.source_to_program(cmap, 5_000_000))

    def test_captions_resolve(self):
        doc = sample_ir()
        tx = T.normalize({"words": [
            {"sourceAtUs": 1_000_000, "sourceEndUs": 1_500_000, "text": "hola"},
            {"sourceAtUs": 1_500_000, "sourceEndUs": 2_000_000, "text": "mundo"},
        ]})
        E.cut_fillers(doc, tx, "hero", pad_us=0)
        E.captions_from_transcript(doc, tx)
        events = E.resolve_caption_events(doc)
        self.assertTrue(events)
        self.assertEqual(events[0]["words"][0]["progAtUs"], 0)  # first kept word at program 0


class TestValidate(unittest.TestCase):
    def test_valid_ir_passes(self):
        doc = sample_ir()
        tx = T.normalize({"words": [
            {"sourceAtUs": 1_000_000, "sourceEndUs": 2_000_000, "text": "hola"},
            {"sourceAtUs": 2_100_000, "sourceEndUs": 3_000_000, "text": "mundo"},
        ]})
        E.cut_fillers(doc, tx, "hero", pad_us=0)
        E.captions_from_transcript(doc, tx)
        rep = V.validate(doc)
        self.assertTrue(rep["ok"], rep)

    def test_missing_asset_fails(self):
        doc = sample_ir()
        t = I.main_track(doc)
        t["clips"] = [I.main_clip("ghost", 0, 1_000_000)]
        E.derive(doc)
        rep = V.validate(doc)
        self.assertFalse(rep["ok"])
        self.assertTrue(any(e["code"] == "missing-asset" for e in rep["errors"]))

    def test_source_out_of_bounds_fails(self):
        doc = sample_ir()
        t = I.main_track(doc)
        t["clips"] = [I.main_clip("hero", 0, 999_000_000)]  # exceeds 20s asset
        E.derive(doc)
        rep = V.validate(doc)
        self.assertTrue(any(e["code"] == "source-oob" for e in rep["errors"]))

    def test_empty_main_fails(self):
        doc = sample_ir()
        rep = V.validate(doc)
        self.assertFalse(rep["ok"])


class TestRenderCommand(unittest.TestCase):
    def test_build_command_has_concat_and_map(self):
        doc = sample_ir()
        tx = T.normalize({"words": [
            {"sourceAtUs": 1_000_000, "sourceEndUs": 2_000_000, "text": "hola"},
            {"sourceAtUs": 2_100_000, "sourceEndUs": 3_000_000, "text": "mundo"},
        ]})
        E.cut_fillers(doc, tx, "hero", pad_us=0)
        E.captions_from_transcript(doc, tx)
        built = R.build_command(doc, "/tmp/out.mp4", ass_path="/tmp/x.ass")
        fc = built["args"][built["args"].index("-filter_complex") + 1]
        self.assertIn("concat=", fc)
        self.assertIn("subtitles=", fc)
        self.assertIn("loudnorm=", fc)
        self.assertIn("[aout]", built["args"])

    def test_ass_has_highlight(self):
        doc = sample_ir()
        tx = T.normalize({"words": [
            {"sourceAtUs": 1_000_000, "sourceEndUs": 1_500_000, "text": "hola"},
            {"sourceAtUs": 1_500_000, "sourceEndUs": 2_000_000, "text": "mundo"},
        ]})
        E.cut_fillers(doc, tx, "hero", pad_us=0)
        E.captions_from_transcript(doc, tx)
        events = E.resolve_caption_events(doc)
        ass = R.generate_ass(doc, events, 1080, 1920)
        self.assertIn("Dialogue:", ass)
        self.assertIn("HOLA", ass)  # uppercase applied

    def test_reframe_cover_filter(self):
        f = R.reframe_filter("cover", 1080, 1920, 0.5, 0.4)
        self.assertIn("force_original_aspect_ratio=increase", f)
        self.assertIn("crop=1080:1920", f)


def overlay_ir():
    doc = sample_ir()
    doc["assets"]["broll1"] = {"sha256": "y", "path": "/tmp/b.mp4", "kind": "video",
                               "probe": {"durationUs": 5_000_000, "hasAudio": False}}
    doc["assets"]["logo"] = {"sha256": "z", "path": "/tmp/logo.png", "kind": "image",
                             "probe": {"w": 200, "h": 200}}
    tx = T.normalize({"words": [
        {"sourceAtUs": 1_000_000, "sourceEndUs": 2_000_000, "text": "hola"},
        {"sourceAtUs": 2_100_000, "sourceEndUs": 3_000_000, "text": "mundo"},
    ]})
    E.cut_fillers(doc, tx, "hero", pad_us=0)
    return doc


class TestOverlays(unittest.TestCase):
    def test_add_broll_resolves_to_program_time(self):
        doc = overlay_ir()
        E.add_broll(doc, "broll1", 1_500_000, 2_500_000, broll_start_us=0, broll_end_us=1_000_000)
        wins = E.resolve_broll_windows(doc)
        self.assertEqual(len(wins), 1)
        self.assertIsNotNone(wins[0]["progStartUs"])
        self.assertTrue(V.validate(doc)["ok"], V.validate(doc))

    def test_broll_fully_in_cut_warns(self):
        doc = overlay_ir()
        # 4.0s-4.5s is in the cut gap between the two kept words -> no window
        E.add_broll(doc, "broll1", 4_000_000, 4_500_000, broll_start_us=0, broll_end_us=500_000)
        self.assertEqual(len(E.resolve_broll_windows(doc)), 0)
        rep = V.validate(doc)
        self.assertTrue(any(w["code"] == "broll-in-cut" for w in rep["warnings"]))

    def test_add_logo_valid(self):
        doc = overlay_ir()
        E.add_logo(doc, "logo", corner="bottom-left", scale=0.2, opacity=0.8)
        self.assertIsNotNone(E.logo_clip_of(doc))
        self.assertTrue(V.validate(doc)["ok"])

    def test_build_command_composites_broll_and_logo(self):
        doc = overlay_ir()
        E.add_broll(doc, "broll1", 1_500_000, 2_500_000, broll_start_us=0, broll_end_us=1_000_000)
        E.add_logo(doc, "logo", corner="top-right", scale=0.15, opacity=0.9)
        built = R.build_command(doc, "/tmp/out.mp4")
        fc = built["args"][built["args"].index("-filter_complex") + 1]
        self.assertIn("[bpre0]", fc)            # b-roll prepared
        self.assertIn("overlay=0:0", fc)        # b-roll composited over main
        self.assertIn("colorchannelmixer=aa=0.9", fc)  # logo opacity
        self.assertIn("[vlogo]", built["args"])  # final video label mapped
        # each media asset became one ffmpeg input: hero + broll + logo (no bgm here)
        self.assertEqual(built["args"].count("-i"), 3)

    def test_broll_anchor_ending_in_cut_clamps_not_overshoots(self):
        doc = sample_ir()
        doc["assets"]["broll1"] = {"sha256": "y", "path": "/tmp/b.mp4", "kind": "video",
                                   "probe": {"durationUs": 5_000_000, "hasAudio": False}}
        tx = T.normalize({"words": [
            {"sourceAtUs": 0, "sourceEndUs": 1_000_000, "text": "a"},          # segment A
            {"sourceAtUs": 5_000_000, "sourceEndUs": 6_000_000, "text": "b"},  # segment B (cut between)
        ]})
        E.cut_fillers(doc, tx, "hero", pad_us=0)
        # anchor starts in A (kept) but ends inside the removed [1s,5s) gap
        E.add_broll(doc, "broll1", 500_000, 1_500_000, broll_start_us=0, broll_end_us=1_000_000)
        w = E.resolve_broll_windows(doc)[0]
        self.assertEqual(w["progStartUs"], 500_000)
        self.assertEqual(w["progEndUs"], 1_000_000)  # clamped to A's end, NOT 1_500_000

    def test_overlapping_broll_is_an_error(self):
        doc = overlay_ir()  # one kept segment [1s,3s) -> program [0,2s)
        E.add_broll(doc, "broll1", 1_200_000, 1_800_000, broll_start_us=0, broll_end_us=600_000)
        E.add_broll(doc, "broll1", 1_500_000, 2_200_000, broll_start_us=0, broll_end_us=700_000)
        rep = V.validate(doc)
        self.assertFalse(rep["ok"])
        self.assertTrue(any(e["code"] == "broll-overlap" for e in rep["errors"]))


class TestTitlesAndDucking(unittest.TestCase):
    def _doc(self):
        doc = sample_ir()
        tx = T.normalize({"words": [
            {"sourceAtUs": 1_000_000, "sourceEndUs": 2_000_000, "text": "hola"},
            {"sourceAtUs": 2_100_000, "sourceEndUs": 3_000_000, "text": "mundo"},
        ]})
        E.cut_fillers(doc, tx, "hero", pad_us=0)
        return doc

    def _with_bgm(self, doc, duck):
        doc["assets"]["bgm"] = {"sha256": "m", "path": "/tmp/m.m4a", "kind": "audio",
                                "probe": {"durationUs": 20_000_000, "hasAudio": True}}
        I.ensure_track(doc, "bgmTrack", "audio", role="bgm")
        I.get_track(doc, "bgmTrack")["clips"] = [I.bgm_clip("bgm", duck=duck)]
        return doc

    def test_add_intro_outro_windows(self):
        doc = self._doc()
        E.add_intro(doc, "HOLA", dur_us=500_000)
        E.add_outro(doc, "CHAU", dur_us=500_000)
        clips = E.title_track_of(doc)["clips"]
        self.assertEqual((clips[0]["atUs"], clips[0]["endUs"]), (0, 500_000))
        end = doc["project"]["durationUs"]
        self.assertEqual((clips[1]["atUs"], clips[1]["endUs"]), (end - 500_000, end))
        self.assertTrue(V.validate(doc)["ok"], V.validate(doc))

    def test_empty_title_errors(self):
        doc = self._doc()
        E.add_intro(doc, "   ", dur_us=500_000)
        rep = V.validate(doc)
        self.assertTrue(any(e["code"] == "title-empty" for e in rep["errors"]))

    def test_build_command_composites_title(self):
        doc = self._doc()
        clip = E.add_intro(doc, "HOLA", dur_us=500_000, background="blurredSource")
        built = R.build_command(doc, "/tmp/o.mp4", title_overlays=[{"clip": clip, "path": "/tmp/t.png"}])
        fc = built["args"][built["args"].index("-filter_complex") + 1]
        self.assertIn("boxblur", fc)          # blurred background
        self.assertIn("[ttext0]", fc)         # title text stream
        self.assertIn("eof_action=repeat", fc)  # single frame held over the window
        self.assertIn("[vt0]", built["args"])

    def test_ducking_emitted_when_enabled(self):
        built = R.build_command(self._with_bgm(self._doc(), True), "/tmp/o.mp4")
        fc = built["args"][built["args"].index("-filter_complex") + 1]
        self.assertIn("sidechaincompress", fc)

    def test_no_ducking_when_disabled(self):
        built = R.build_command(self._with_bgm(self._doc(), False), "/tmp/o.mp4")
        fc = built["args"][built["args"].index("-filter_complex") + 1]
        self.assertNotIn("sidechaincompress", fc)


def _cut_doc():
    doc = sample_ir()
    tx = T.normalize({"words": [
        {"sourceAtUs": 1_000_000, "sourceEndUs": 2_000_000, "text": "a"},
        {"sourceAtUs": 2_100_000, "sourceEndUs": 3_000_000, "text": "b"},
    ]})
    E.cut_fillers(doc, tx, "hero", pad_us=0)
    return doc


class TestReviewFixesR3(unittest.TestCase):
    def test_pango_escape_is_numeric(self):
        self.assertEqual(R._pango_escape("Q&A"), "Q&#38;A")
        self.assertEqual(R._pango_escape("a<b>c"), "a&#60;b&#62;c")

    def test_render_refuses_invalid_ir(self):
        with self.assertRaises(RuntimeError):
            R.render(sample_ir(), "/tmp/vertir_should_not_render.mp4")  # empty main -> invalid

    def test_empty_title_png_skipped(self):
        doc = _cut_doc()
        E.add_intro(doc, "   ", dur_us=500_000)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(R.render_title_pngs(doc, d, 1.0), [])  # skipped, no convert crash

    def test_title_overlap_warns(self):
        doc = _cut_doc()
        E.add_title(doc, "A", 0, 1_000_000)
        E.add_title(doc, "B", 500_000, 1_500_000)
        rep = V.validate(doc)
        self.assertTrue(any(w["code"] == "title-overlap" for w in rep["warnings"]))

    def test_title_with_ampersand_renders(self):
        if not (shutil.which("convert") or shutil.which("magick")):
            self.skipTest("ImageMagick not available")
        doc = _cut_doc()
        E.add_intro(doc, "TIPS & TRICKS <2026>", dur_us=500_000)
        with tempfile.TemporaryDirectory() as d:
            pngs = R.render_title_pngs(doc, d, 0.5)
            self.assertEqual(len(pngs), 1)
            self.assertTrue(os.path.exists(pngs[0]["path"]))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- keyframes
def kf(prop, at_us, v, ease="linear"):
    return {"prop": prop, "atUs": at_us, "v": v, "ease": ease}


def keyframed_ir(keyframes, prop_target="main"):
    """One 4s main clip carrying `keyframes` (or a bgm clip when targeting bgm)."""
    doc = sample_ir()
    doc["assets"]["bgm"] = {"sha256": "b", "path": "/tmp/bgm.m4a", "kind": "audio",
                            "probe": {"durationUs": 60_000_000, "hasAudio": True}}
    clip = I.main_clip("hero", 0, 4_000_000)
    I.main_track(doc)["clips"] = [clip]
    if prop_target == "main":
        clip["keyframes"] = keyframes
    else:
        I.ensure_track(doc, "bgmTrack", "audio", role="bgm")
        b = I.bgm_clip("bgm")
        b["keyframes"] = keyframes
        I.get_track(doc, "bgmTrack")["clips"] = [b]
    E.derive(doc)
    return doc


class TestAnimSample(unittest.TestCase):
    """The pure interpolator — the reference the ffmpeg expression must match."""

    def test_no_keyframes_returns_default(self):
        self.assertEqual(A.sample([], "scale", 0, default=1.0), 1.0)

    def test_constant_before_first_and_after_last(self):
        ks = [kf("scale", 1_000_000, 1.0), kf("scale", 2_000_000, 1.5)]
        self.assertAlmostEqual(A.sample(ks, "scale", 0, default=1.0), 1.0)
        self.assertAlmostEqual(A.sample(ks, "scale", 9_000_000, default=1.0), 1.5)

    def test_linear_midpoint(self):
        ks = [kf("scale", 0, 1.0), kf("scale", 2_000_000, 2.0)]
        self.assertAlmostEqual(A.sample(ks, "scale", 1_000_000, default=1.0), 1.5)

    def test_hold_is_a_step(self):
        ks = [kf("scale", 0, 1.0, "hold"), kf("scale", 2_000_000, 2.0)]
        self.assertAlmostEqual(A.sample(ks, "scale", 1_900_000, default=1.0), 1.0)
        self.assertAlmostEqual(A.sample(ks, "scale", 2_000_000, default=1.0), 2.0)

    def test_ease_in_out_is_smoothstep(self):
        ks = [kf("scale", 0, 0.0, "easeInOut"), kf("scale", 1_000_000, 1.0)]
        # smoothstep(0.25) = 0.25^2 * (3 - 2*0.25) = 0.15625
        self.assertAlmostEqual(A.sample(ks, "scale", 250_000, default=0.0), 0.15625)
        self.assertAlmostEqual(A.sample(ks, "scale", 500_000, default=0.0), 0.5)

    def test_other_props_are_ignored(self):
        ks = [kf("x", 0, 100.0), kf("scale", 0, 2.0)]
        self.assertAlmostEqual(A.sample(ks, "scale", 0, default=1.0), 2.0)

    def test_ties_last_wins(self):
        ks = [kf("scale", 1_000_000, 1.0), kf("scale", 1_000_000, 3.0)]
        self.assertAlmostEqual(A.sample(ks, "scale", 1_000_000, default=1.0), 3.0)


class TestAnimExpr(unittest.TestCase):
    def test_none_when_prop_absent(self):
        self.assertIsNone(A.expr([kf("x", 0, 1.0)], "scale"))

    def test_none_when_curve_is_the_identity(self):
        # constant AND equal to the default: nothing to render
        self.assertIsNone(A.expr([kf("scale", 0, 1.0), kf("scale", 2_000_000, 1.0)],
                                 "scale", default=1.0))

    def test_constant_non_default_still_emits(self):
        # a flat 1.3 is a static 1.3x push, not a no-op
        self.assertIsNotNone(A.expr([kf("scale", 0, 1.3), kf("scale", 2_000_000, 1.3)],
                                    "scale", default=1.0))

    def test_linear_expression_is_clamped(self):
        e = A.expr([kf("scale", 0, 1.0), kf("scale", 2_000_000, 1.5)], "scale")
        self.assertIn("clip(", e)
        self.assertNotIn(";", e)   # would break the filtergraph
        self.assertNotIn('"', e)

    def test_hold_segment_has_no_interpolation_term(self):
        e = A.expr([kf("scale", 0, 1.0, "hold"), kf("scale", 2_000_000, 2.0)], "scale")
        self.assertIn("if(", e)

    def test_uses_given_time_variable(self):
        e = A.expr([kf("scale", 0, 1.0), kf("scale", 1_000_000, 2.0)], "scale", tvar="in_time")
        self.assertIn("in_time", e)
        self.assertNotIn("(t-", e)


class TestKeyframeValidation(unittest.TestCase):
    def test_valid_keyframes_pass(self):
        doc = keyframed_ir([kf("scale", 0, 1.0), kf("scale", 2_000_000, 1.12, "easeInOut")])
        rep = V.validate(doc)
        self.assertTrue(rep["ok"], rep)

    def test_unknown_prop_is_an_error(self):
        doc = keyframed_ir([kf("rotDeg", 0, 10.0)])
        rep = V.validate(doc)
        self.assertTrue(any(e["code"] == "kf-prop" for e in rep["errors"]), rep)

    def test_unknown_ease_is_an_error(self):
        doc = keyframed_ir([kf("scale", 0, 1.0, "spring"), kf("scale", 1_000_000, 1.2)])
        rep = V.validate(doc)
        self.assertTrue(any(e["code"] == "kf-ease" for e in rep["errors"]), rep)

    def test_atus_beyond_clip_duration_is_an_error(self):
        # clip is 4s; a keyframe at 9s can never fire
        doc = keyframed_ir([kf("scale", 0, 1.0), kf("scale", 9_000_000, 1.2)])
        rep = V.validate(doc)
        self.assertTrue(any(e["code"] == "kf-range" for e in rep["errors"]), rep)

    def test_negative_atus_is_an_error(self):
        doc = keyframed_ir([kf("scale", -1, 1.0)])
        rep = V.validate(doc)
        self.assertTrue(any(e["code"] == "kf-range" for e in rep["errors"]), rep)

    def test_non_monotonic_atus_is_an_error(self):
        doc = keyframed_ir([kf("scale", 2_000_000, 1.0), kf("scale", 1_000_000, 1.2)])
        rep = V.validate(doc)
        self.assertTrue(any(e["code"] == "kf-order" for e in rep["errors"]), rep)

    def test_non_numeric_value_is_an_error(self):
        doc = keyframed_ir([kf("scale", 0, "big")])
        rep = V.validate(doc)
        self.assertTrue(any(e["code"] == "kf-value" for e in rep["errors"]), rep)

    def test_scale_below_reframe_is_an_error(self):
        # spec section 6: a scale keyframe may never drop below the reframe base,
        # that is what produces black bars
        doc = keyframed_ir([kf("scale", 0, 1.0), kf("scale", 1_000_000, 0.8)])
        rep = V.validate(doc)
        self.assertTrue(any(e["code"] == "kf-scale-under" for e in rep["errors"]), rep)

    def test_opacity_keyframes_warn_as_unrendered(self):
        doc = keyframed_ir([kf("opacity", 0, 1.0), kf("opacity", 1_000_000, 0.5)])
        rep = V.validate(doc)
        self.assertTrue(rep["ok"], rep)
        self.assertTrue(any(w["code"] == "kf-unrendered" for w in rep["warnings"]), rep)

    def test_gain_keyframes_on_bgm_pass(self):
        doc = keyframed_ir([kf("gainDb", 0, -18.0), kf("gainDb", 1_000_000, -30.0)],
                           prop_target="bgm")
        rep = V.validate(doc)
        self.assertTrue(rep["ok"], rep)
        self.assertFalse(any(w["code"] == "kf-unrendered" for w in rep["warnings"]), rep)

    def test_keyframes_on_overlay_warn_as_unrendered(self):
        doc = overlay_ir()
        E.add_broll(doc, "broll1", 1_200_000, 1_800_000)
        for t in doc["tracks"]:
            if t.get("role") == "broll":
                t["clips"][0]["keyframes"] = [kf("scale", 0, 1.0), kf("scale", 500_000, 1.2)]
        rep = V.validate(doc)
        self.assertTrue(any(w["code"] == "kf-unrendered" for w in rep["warnings"]), rep)


class TestKeyframeRender(unittest.TestCase):
    def test_no_keyframes_emits_no_zoompan(self):
        doc = keyframed_ir([])
        fc = R.build_command(doc, "/tmp/o.mp4")["args"]
        fc = fc[fc.index("-filter_complex") + 1]
        self.assertNotIn("zoompan", fc)

    def test_scale_keyframes_emit_zoompan_at_canvas_size(self):
        doc = keyframed_ir([kf("scale", 0, 1.0), kf("scale", 2_000_000, 1.12, "easeInOut")])
        fc = R.build_command(doc, "/tmp/o.mp4")["args"]
        fc = fc[fc.index("-filter_complex") + 1]
        self.assertIn("zoompan=", fc)
        self.assertIn("s=1080x1920", fc)
        self.assertIn("in_time", fc)

    def test_pan_only_keyframes_still_emit_zoompan(self):
        doc = keyframed_ir([kf("x", 0, 0.0), kf("x", 2_000_000, 60.0)])
        fc = R.build_command(doc, "/tmp/o.mp4")["args"]
        fc = fc[fc.index("-filter_complex") + 1]
        self.assertIn("zoompan=", fc)

    def test_proxy_scales_pixel_offsets(self):
        doc = keyframed_ir([kf("x", 0, 0.0), kf("x", 2_000_000, 100.0)])
        full = R.build_command(doc, "/tmp/o.mp4")["args"]
        half = R.build_command(doc, "/tmp/o.mp4", proxy=True)["args"]
        full = full[full.index("-filter_complex") + 1]
        half = half[half.index("-filter_complex") + 1]
        self.assertIn("s=1080x1920", full)
        self.assertIn("s=540x960", half)
        self.assertIn("100.000", full)
        self.assertIn("50.000", half)   # pixel offsets follow the proxy canvas

    def test_gain_keyframes_emit_per_frame_volume(self):
        doc = keyframed_ir([kf("gainDb", 0, -18.0), kf("gainDb", 1_000_000, -30.0)],
                           prop_target="bgm")
        fc = R.build_command(doc, "/tmp/o.mp4")["args"]
        fc = fc[fc.index("-filter_complex") + 1]
        self.assertIn("eval=frame", fc)

    def test_opacity_keyframes_do_not_reach_the_filtergraph(self):
        doc = keyframed_ir([kf("opacity", 0, 1.0), kf("opacity", 1_000_000, 0.4)])
        fc = R.build_command(doc, "/tmp/o.mp4")["args"]
        fc = fc[fc.index("-filter_complex") + 1]
        self.assertNotIn("zoompan", fc)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "ffmpeg/ffprobe not on PATH")
class TestKeyframeRealRender(unittest.TestCase):
    """The filtergraph has to survive a real ffmpeg, not just a string assertion."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vertir-kf-")
        self.src = os.path.join(self.tmp, "src.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=red:s=1280x720:d=4:r=30,"
             "drawgrid=w=64:h=36:t=3:c=white",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
             "-shortest", self.src], check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _render(self, keyframes):
        doc = sample_ir()
        doc["assets"]["hero"] = {
            "sha256": "x", "path": self.src, "kind": "video",
            "probe": {"durationUs": 4_000_000, "w": 1280, "h": 720,
                      "fps": {"num": 30, "den": 1}, "hasAudio": True},
        }
        clip = I.main_clip("hero", 0, 3_000_000)
        clip["keyframes"] = keyframes
        I.main_track(doc)["clips"] = [clip]
        E.derive(doc)
        self.assertTrue(V.validate(doc)["ok"], V.validate(doc))
        out = os.path.join(self.tmp, f"out{len(keyframes)}{keyframes and keyframes[0]['ease']}.mp4")
        R.render(doc, out, proxy=True)
        return out

    def _frame(self, video, at_s, name):
        png = os.path.join(self.tmp, name)
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", str(at_s), "-i", video, "-frames:v", "1", png],
                       check=True)
        with open(png, "rb") as fh:
            return fh.read()

    def test_zoom_keyframes_actually_move_the_frame(self):
        out = self._render([kf("scale", 0, 1.0), kf("scale", 2_500_000, 1.4)])
        self.assertTrue(os.path.exists(out))
        self.assertNotEqual(self._frame(out, 0.1, "a.png"),
                            self._frame(out, 2.4, "b.png"),
                            "zoom keyframes rendered a static frame")

    def test_hold_keyframes_do_not_move_the_frame(self):
        # linear would have zoomed between 0.5s and 2.0s; hold must not
        out = self._render([kf("scale", 0, 1.2, "hold"), kf("scale", 2_500_000, 1.6)])
        self.assertEqual(self._frame(out, 0.5, "c.png"),
                         self._frame(out, 2.0, "d.png"),
                         "a held curve should render identical frames")

    def test_mixed_keyframed_and_plain_clips_concat(self):
        """concat demands identical parameters on every input. Animating only
        SOME clips must not leave the track with two different SARs."""
        doc = sample_ir()
        doc["assets"]["hero"] = {
            "sha256": "x", "path": self.src, "kind": "video",
            "probe": {"durationUs": 4_000_000, "w": 1280, "h": 720,
                      "fps": {"num": 30, "den": 1}, "hasAudio": True},
        }
        a = I.main_clip("hero", 0, 1_500_000)
        a["keyframes"] = [kf("scale", 0, 1.0), kf("scale", 1_400_000, 1.3)]
        b = I.main_clip("hero", 2_000_000, 3_500_000)   # no keyframes at all
        I.main_track(doc)["clips"] = [a, b]
        E.derive(doc)
        self.assertTrue(V.validate(doc)["ok"], V.validate(doc))
        out = os.path.join(self.tmp, "mixed.mp4")
        R.render(doc, out, proxy=True)
        self.assertTrue(os.path.getsize(out) > 0)

    def test_every_main_clip_declares_a_square_sar(self):
        doc = keyframed_ir([kf("scale", 0, 1.0), kf("scale", 2_000_000, 1.2)])
        fc = R.build_command(doc, "/tmp/o.mp4")["args"]
        fc = fc[fc.index("-filter_complex") + 1]
        chains = [c for c in fc.split(";") if "concat=" not in c and "]trim=" in c]
        self.assertTrue(chains)
        for c in chains:
            self.assertIn("setsar=1", c, f"main clip chain without setsar: {c}")
