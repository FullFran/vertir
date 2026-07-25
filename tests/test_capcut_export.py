"""CapCut export: the transpiler and the loss report.

These tests run with the `capcut` CLI absent on purpose — CI has no Node. They
exercise the pure spec dict, which is where every mapping decision actually
lives; the bridge is a subprocess wrapper with nothing to assert offline.
"""
import json
import os
import tempfile
import unittest

from vertir import ir as I
from vertir import edit as E
from vertir import plan as PL
from vertir.capcut import bridge
from vertir.capcut import spec as CS
from vertir.capcut.provenance import Provenance

from tests.test_plan import make_transcript


def make_ir(with_extras: bool = False) -> dict:
    doc = I.new_ir(title="export me")
    doc["assets"]["hero"] = {
        "sha256": "0" * 64, "path": "/tmp/hero.mp4", "kind": "video",
        "probe": {"durationUs": 16_000_000, "hasAudio": True,
                  "fps": {"num": 30, "den": 1}, "w": 1080, "h": 1920},
    }
    tx = make_transcript()
    plan = PL.new_plan()
    plan["keep"] = [PL.span(0, 12_000_000)]
    plan["beats"] = [PL.punch_in(5_000_000, intensity=0.15)]
    plan["emphasis"] = [PL.span(2_000_000, 2_800_000)]
    plan["outro"] = {"text": "FOLLOW", "durUs": 1_000_000}
    PL.apply_plan(doc, plan, tx)

    if with_extras:
        doc["assets"]["bgm"] = {"sha256": "1" * 64, "path": "/tmp/bgm.m4a",
                                "kind": "audio", "probe": {"durationUs": 60_000_000}}
        I.ensure_track(doc, "bgmTrack", "audio", role="bgm")
        I.get_track(doc, "bgmTrack")["clips"] = [I.bgm_clip("bgm")]
        doc["assets"]["logo"] = {"sha256": "2" * 64, "path": "/tmp/logo.png",
                                 "kind": "image", "probe": {"durationUs": 0}}
        E.add_logo(doc, "logo")
    E.derive(doc)
    return doc


class TestSpecShape(unittest.TestCase):
    def setUp(self):
        self.spec, self.losses = CS.build_spec(make_ir())

    def test_canvas_and_fps(self):
        self.assertEqual(self.spec["width"], 1080)
        self.assertEqual(self.spec["height"], 1920)
        self.assertEqual(self.spec["fps"], 30)

    def test_main_track_is_first_and_populated(self):
        t = self.spec["tracks"][0]
        self.assertEqual(t["type"], "video")
        self.assertEqual(t["name"], "main")
        self.assertTrue(t["items"])

    def test_times_are_seconds_not_microseconds(self):
        """The draft store is microsecond-based but the compile spec takes
        seconds; getting this backwards would produce a 10^6x long draft."""
        item = self.spec["tracks"][0]["items"][0]
        self.assertEqual(item["start"], 0.0)
        self.assertEqual(item["duration"], 12.0)
        self.assertIsInstance(item["duration"], float)

    def test_every_item_has_a_ref(self):
        for track in self.spec["tracks"]:
            for item in track["items"]:
                self.assertIn("ref", item, f"{track['name']} item without ref")

    def test_track_types_are_valid(self):
        for track in self.spec["tracks"]:
            self.assertIn(track["type"], {"video", "audio", "text"})

    def test_spec_is_json_serialisable(self):
        json.dumps(self.spec)

    def test_punch_in_becomes_a_keyframe_operation(self):
        """CapCut names the property `uniform_scale`; sending `scale` is rejected
        by compile (and NOT caught by `compile --check`)."""
        ops = [o for o in self.spec.get("operations", []) if o["op"] == "keyframe"]
        self.assertEqual(len(ops), 2)
        self.assertEqual({o["property"] for o in ops}, {"uniform_scale"})
        self.assertEqual(ops[0]["time"], 5.0)
        self.assertAlmostEqual(ops[1]["value"], 1.15)

    def test_easing_uses_capcut_vocabulary(self):
        """The IR says `easeInOut`; CapCut only accepts kebab-case names."""
        ops = [o for o in self.spec.get("operations", []) if o.get("easing")]
        self.assertTrue(ops)
        for o in ops:
            self.assertIn(o["easing"], {"linear", "ease-in", "ease-out", "ease-in-out"})

    def test_ratio_matches_a_vertical_canvas(self):
        self.assertEqual(self.spec["ratio"], "9:16")

    def test_caption_y_is_below_centre(self):
        """CapCut's y is centre-anchored and negative-up, so low captions are
        positive. Getting the sign wrong puts every caption off the top."""
        caps = [t for t in self.spec["tracks"] if t.get("name") == "captions"][0]
        self.assertGreater(caps["items"][0]["y"], 0)

    def test_titles_become_a_text_track(self):
        titles = [t for t in self.spec["tracks"] if t.get("name") == "titles"]
        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0]["items"][0]["text"], "FOLLOW")

    def test_captions_become_one_cue_per_line(self):
        caps = [t for t in self.spec["tracks"] if t.get("name") == "captions"][0]
        self.assertTrue(caps["items"])
        # each cue holds a whole line, not a single word
        self.assertIn(" ", caps["items"][0]["text"])


class TestLossReport(unittest.TestCase):
    """The report is the feature: a silent downgrade is worse than no export."""

    def setUp(self):
        self.spec, self.losses = CS.build_spec(make_ir(with_extras=True))
        self.codes = {loss["code"] for loss in self.losses}

    def test_reports_lost_loudness(self):
        self.assertIn("loudness", self.codes)

    def test_reports_lost_ducking(self):
        self.assertIn("duck", self.codes)

    def test_reports_degraded_karaoke_captions(self):
        self.assertIn("caption-karaoke", self.codes)

    def test_reports_lost_word_emphasis(self):
        self.assertIn("caption-emphasis", self.codes)

    def test_reports_lost_reframe_focus(self):
        self.assertIn("reframe", self.codes)

    def test_every_loss_has_a_known_severity(self):
        for loss in self.losses:
            self.assertIn(loss["severity"], {CS.DROPPED, CS.DEGRADED, CS.UNVERIFIED})
            self.assertTrue(loss["detail"], f"{loss['code']} has no explanation")

    def test_a_real_ir_never_exports_losslessly(self):
        self.assertTrue(self.losses)


class TestAudioMapping(unittest.TestCase):
    def setUp(self):
        self.spec, _ = CS.build_spec(make_ir(with_extras=True))

    def test_gain_db_becomes_a_linear_volume(self):
        music = [t for t in self.spec["tracks"] if t.get("name") == "music"][0]
        # bgm_clip defaults to -18 dB -> 10 ** (-18/20) ~= 0.1259
        self.assertAlmostEqual(music["items"][0]["volume"], 0.1259, places=3)

    def test_fades_become_audio_fade_operations(self):
        fades = [o for o in self.spec.get("operations", []) if o["op"] == "audio-fade"]
        self.assertEqual(len(fades), 1)
        self.assertAlmostEqual(fades[0]["fadeIn"], 0.5)
        self.assertAlmostEqual(fades[0]["fadeOut"], 1.2)

    def test_whole_program_bgm_is_clamped_to_the_program(self):
        music = [t for t in self.spec["tracks"] if t.get("name") == "music"][0]
        self.assertGreater(music["items"][0]["duration"], 0)
        self.assertLessEqual(music["items"][0]["duration"], 13.0)


class TestProvenance(unittest.TestCase):
    def test_refs_round_trip_through_disk(self):
        spec, _ = CS.build_spec(make_ir(with_extras=True))
        prov = Provenance(ir_version="1.1.0", refs=CS._collect_refs(spec))
        self.assertTrue(prov.refs)
        with tempfile.TemporaryDirectory() as d:
            path = prov.dump(d)
            self.assertTrue(os.path.exists(path))
            back = Provenance.load(d)
        self.assertEqual(back.refs, prov.refs)

    def test_mapping_is_bidirectional(self):
        spec, _ = CS.build_spec(make_ir())
        prov = Provenance(refs=CS._collect_refs(spec))
        clip_id = next(iter(prov.refs))
        self.assertEqual(prov.to_ir(prov.to_capcut(clip_id)), clip_id)

    def test_main_clips_are_all_bound(self):
        doc = make_ir()
        spec, _ = CS.build_spec(doc)
        refs = CS._collect_refs(spec)
        for clip in I.main_track(doc)["clips"]:
            self.assertIn(clip["id"], refs)


class TestGracefulDegradation(unittest.TestCase):
    """Node is an optional dependency: absent, the core must still work and the
    failure must be actionable."""

    def test_available_never_raises(self):
        self.assertIsInstance(bridge.available(), bool)

    def test_require_raises_a_typed_actionable_error(self):
        if bridge.available():
            self.skipTest("capcut CLI is installed here")
        with self.assertRaises(bridge.CapCutUnavailable) as cm:
            bridge.require()
        self.assertIn("npm install -g capcut-cli", str(cm.exception))

    def test_export_still_writes_the_spec_without_the_cli(self):
        if bridge.available():
            self.skipTest("capcut CLI is installed here")
        with tempfile.TemporaryDirectory() as d:
            res = CS.export(make_ir(), d)
        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "cli-missing")
        self.assertTrue(os.path.basename(res["specPath"]), "capcut.spec.json")
        self.assertTrue(res["losses"], "losses must be reported even when unusable")

    def test_export_refuses_an_invalid_ir_before_touching_the_cli(self):
        doc = I.new_ir(title="empty")   # no clips: the IR validator must reject it
        with tempfile.TemporaryDirectory() as d:
            res = CS.export(doc, d)
        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "ir-validate")


if __name__ == "__main__":
    unittest.main()
