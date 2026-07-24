"""Unit tests (stdlib unittest, no pytest needed):

    python -m unittest discover -s tests -v
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vertir import ir as I
from vertir import edit as E
from vertir import validate as V
from vertir import transcript as T
from vertir import render as R


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
