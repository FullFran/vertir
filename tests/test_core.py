"""Unit tests (stdlib unittest, no pytest needed):

    python -m unittest discover -s tests -v
"""
import os
import sys
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


if __name__ == "__main__":
    unittest.main()
