"""Reconcile: CapCut edits patched onto the IR, never re-parsed into one.

The bridge is stubbed so these run with no Node and no CapCut — what matters is
the translation layer, not the subprocess call.
"""
import copy
import unittest
from unittest import mock

from vertir import edit as E
from vertir import ir as I
from vertir import plan as PL
from vertir.capcut import reconcile as RC
from vertir.capcut.provenance import Provenance

from tests.test_plan import make_transcript


def make_ir() -> dict:
    doc = I.new_ir(title="round trip")
    doc["assets"]["hero"] = {
        "sha256": "0" * 64, "path": "/tmp/hero.mp4", "kind": "video",
        "probe": {"durationUs": 16_000_000, "hasAudio": True,
                  "fps": {"num": 30, "den": 1}, "w": 1080, "h": 1920},
    }
    tx = make_transcript()
    plan = PL.new_plan()
    plan["keep"] = [PL.span(0, 12_000_000)]
    plan["beats"] = [PL.punch_in(5_000_000, intensity=0.15)]
    plan["outro"] = {"text": "FOLLOW", "durUs": 1_000_000}
    PL.apply_plan(doc, plan, tx)

    doc["assets"]["bgm"] = {"sha256": "1" * 64, "path": "/tmp/bgm.m4a",
                            "kind": "audio", "probe": {"durationUs": 60_000_000}}
    I.ensure_track(doc, "bgmTrack", "audio", role="bgm")
    I.get_track(doc, "bgmTrack")["clips"] = [I.bgm_clip("bgm")]
    E.derive(doc)
    return doc


def make_prov(ir: dict) -> Provenance:
    """Provenance as the export would have written it."""
    refs, segments = {}, {}
    for c in I.main_track(ir)["clips"]:
        refs[c["id"]] = f"main_{c['id']}"
        segments[f"main_{c['id']}"] = f"seg-{c['id']}"
    for c in (E.title_track_of(ir) or {}).get("clips", []):
        refs[c["id"]] = f"title_{c['id']}"
        segments[f"title_{c['id']}"] = f"seg-{c['id']}"
    return Provenance(ir_version=ir["irVersion"], refs=refs, segments=segments,
                      snapshot_path="/tmp/snapshot")


def stub_bridge(diff_payload: dict, segments: list[dict] | None = None,
                texts: list[dict] | None = None):
    """Patch the bridge so reconcile sees a canned draft."""
    def fake_run(args, **kw):
        if args[0] == "segments":
            return {"ok": True, "json": segments or [], "stdout": "", "stderr": "",
                    "exitCode": 0}
        if args[0] == "texts":
            return {"ok": True, "json": texts or [], "stdout": "", "stderr": "",
                    "exitCode": 0}
        return {"ok": True, "json": {}, "stdout": "", "stderr": "", "exitCode": 0}

    return mock.patch.multiple(
        RC.bridge,
        diff=mock.Mock(return_value={"ok": True, "json": diff_payload,
                                     "stdout": "", "stderr": "", "exitCode": 0}),
        run=mock.Mock(side_effect=fake_run),
        check_error=mock.Mock(return_value=None),
    )


class TestVolumeConversion(unittest.TestCase):
    def test_db_round_trips_through_linear_volume(self):
        from vertir.capcut.spec import _gain_to_volume
        for db in (0.0, -6.0, -18.0, 6.0):
            self.assertAlmostEqual(RC._db_from_volume(_gain_to_volume(db)), db, places=1)

    def test_silence_clamps_rather_than_diverging(self):
        self.assertEqual(RC._db_from_volume(0.0), -60.0)


class TestNoChange(unittest.TestCase):
    def test_unchanged_draft_is_a_no_op(self):
        doc = make_ir()
        before = copy.deepcopy(doc)
        with stub_bridge({"changed": False}):
            out, rep = RC.reconcile(doc, "/tmp/draft", make_prov(doc))
        self.assertTrue(rep["ok"])
        self.assertFalse(rep["changed"])
        self.assertEqual(out["tracks"], before["tracks"])

    def test_missing_snapshot_is_a_clear_error(self):
        doc = make_ir()
        prov = make_prov(doc)
        prov.snapshot_path = None
        out, rep = RC.reconcile(doc, "/tmp/draft", prov)
        self.assertFalse(rep["ok"])
        self.assertIn("snapshot", rep["errors"][0])


class TestDeltaApplication(unittest.TestCase):
    def setUp(self):
        self.doc = make_ir()
        self.prov = make_prov(self.doc)
        self.clip_id = I.main_track(self.doc)["clips"][0]["id"]
        self.seg_id = self.prov.segment_for_ir(self.clip_id)

    def _reconcile(self, fields, after):
        diff = {"changed": True,
                "segments": {"added": [], "removed": [],
                             "changed": [{"id": self.seg_id, "fields": fields}]},
                "materials": {"changed": []}}
        rows = [dict(after, id=self.seg_id)]
        with stub_bridge(diff, segments=rows):
            return RC.reconcile(self.doc, "/tmp/draft", self.prov)

    def test_speed_change_lands_on_the_clip(self):
        out, rep = self._reconcile(["speed"], {"speed": 1.25})
        self.assertTrue(rep["ok"])
        self.assertEqual(I.main_track(out)["clips"][0]["speed"], 1.25)

    def test_volume_change_becomes_gain_db(self):
        out, rep = self._reconcile(["volume"], {"volume": 0.5})
        self.assertAlmostEqual(
            I.main_track(out)["clips"][0]["audio"]["gainDb"], -6.02, places=1)

    def test_trim_moves_the_source_out_point(self):
        clip = I.main_track(self.doc)["clips"][0]
        start = clip["source"]["startUs"]
        out, rep = self._reconcile(["duration_us"], {"duration_us": 900_000})
        self.assertTrue(rep["ok"])
        self.assertEqual(I.main_track(out)["clips"][0]["source"]["endUs"],
                         start + 900_000)

    def test_trim_drops_keyframes_left_outside_the_clip(self):
        """A trim that cuts away the stretch a punch-in lived on takes the
        punch-in with it, and says so — otherwise the patched IR fails its own
        validator and the whole reconcile refuses to persist."""
        clip = I.main_track(self.doc)["clips"][0]
        clip["keyframes"] = [
            {"prop": "scale", "atUs": 0, "v": 1.0, "ease": "easeInOut"},
            {"prop": "scale", "atUs": 5_000_000, "v": 1.15, "ease": "linear"}]
        out, rep = self._reconcile(["duration_us"], {"duration_us": 900_000})
        self.assertTrue(rep["ok"], rep)
        kept = I.main_track(out)["clips"][0].get("keyframes", [])
        self.assertTrue(all(k["atUs"] <= 900_000 for k in kept), kept)
        self.assertTrue(any("keyframe" in w for w in rep["warnings"]), rep["warnings"])

    def test_trim_accounts_for_speed(self):
        clip = I.main_track(self.doc)["clips"][0]
        clip["speed"] = 2.0
        start = clip["source"]["startUs"]
        out, _ = self._reconcile(["duration_us"], {"duration_us": 1_000_000,
                                                   "speed": 2.0})
        # 1s of program at 2x consumes 2s of source
        self.assertEqual(I.main_track(out)["clips"][0]["source"]["endUs"],
                         start + 2_000_000)

    def test_main_track_reorder_is_pinned_not_guessed(self):
        out, rep = self._reconcile(["start_us"], {"start_us": 5_000_000})
        pinned = [p for p in rep["pinnedInCapCut"] if p.get("field") == "start_us"]
        self.assertEqual(len(pinned), 1)
        self.assertIn("engine-derived", pinned[0]["reason"])

    def test_unmodelled_field_is_pinned_never_dropped_silently(self):
        out, rep = self._reconcile(["chroma"], {})
        pinned = [p for p in rep["pinnedInCapCut"] if p.get("field") == "chroma"]
        self.assertEqual(len(pinned), 1)
        self.assertEqual(rep["applied"], [])

    def test_unknown_segment_is_reported(self):
        diff = {"changed": True,
                "segments": {"added": [], "removed": [],
                             "changed": [{"id": "not-ours", "fields": ["speed"]}]},
                "materials": {"changed": []}}
        with stub_bridge(diff, segments=[]):
            out, rep = RC.reconcile(self.doc, "/tmp/draft", self.prov)
        self.assertEqual(len(rep["unknownSegments"]), 1)

    def test_segment_added_in_capcut_is_pinned(self):
        diff = {"changed": True,
                "segments": {"added": ["brand-new"], "removed": [], "changed": []},
                "materials": {"changed": []}}
        with stub_bridge(diff, segments=[]):
            out, rep = RC.reconcile(self.doc, "/tmp/draft", self.prov)
        self.assertTrue(any(p.get("segment") == "brand-new"
                            for p in rep["pinnedInCapCut"]))

    def test_segment_removed_in_capcut_removes_the_clip(self):
        n = len(I.main_track(self.doc)["clips"])
        diff = {"changed": True,
                "segments": {"added": [], "removed": [self.seg_id], "changed": []},
                "materials": {"changed": []}}
        with stub_bridge(diff, segments=[]):
            out, rep = RC.reconcile(self.doc, "/tmp/draft", self.prov)
        self.assertEqual(len(I.main_track(out)["clips"]), n - 1)

    def test_title_text_edit_is_merged(self):
        title = E.title_track_of(self.doc)["clips"][0]
        seg = self.prov.segment_for_ir(title["id"])
        diff = {"changed": True,
                "segments": {"added": [], "removed": [], "changed": []},
                "materials": {"changed": ["m1"]}}
        with stub_bridge(diff, segments=[],
                         texts=[{"id": seg, "text": "SEGUIME PARA MAS"}]):
            out, rep = RC.reconcile(self.doc, "/tmp/draft", self.prov)
        self.assertEqual(E.title_track_of(out)["clips"][0]["text"]["content"],
                         "SEGUIME PARA MAS")


class TestIntentSurvives(unittest.TestCase):
    """The whole reason this module patches instead of re-parsing.

    A rebuilt-from-draft IR would lose every one of these, because CapCut does
    not model them at all — several were even reported as losses on export.
    """

    def test_intent_is_untouched_by_a_round_trip(self):
        doc = make_ir()
        prov = make_prov(doc)
        clip_id = I.main_track(doc)["clips"][0]["id"]
        seg_id = prov.segment_for_ir(clip_id)

        before = copy.deepcopy(doc)
        diff = {"changed": True,
                "segments": {"added": [], "removed": [],
                             "changed": [{"id": seg_id, "fields": ["speed"]}]},
                "materials": {"changed": []}}
        with stub_bridge(diff, segments=[{"id": seg_id, "speed": 1.25}]):
            out, rep = RC.reconcile(doc, "/tmp/draft", prov)
        self.assertTrue(rep["ok"])

        b = I.main_track(before)["clips"][0]
        a = I.main_track(out)["clips"][0]
        self.assertEqual(a["speed"], 1.25, "the human's edit must land")

        # ...and everything CapCut cannot express must be exactly as it was
        self.assertEqual(a["reframe"], b["reframe"])
        self.assertEqual(a["keyframes"], b["keyframes"])
        self.assertEqual(
            I.get_track(out, "bgmTrack")["clips"][0]["duck"],
            I.get_track(before, "bgmTrack")["clips"][0]["duck"])
        self.assertEqual(out["project"]["output"]["loudnessLufs"],
                         before["project"]["output"]["loudnessLufs"])
        # the outro background is DROPPED on export, yet survives here
        self.assertEqual(E.title_track_of(out)["clips"][0]["background"],
                         E.title_track_of(before)["clips"][0]["background"])

    def test_material_changes_warn_rather_than_corrupt(self):
        doc = make_ir()
        diff = {"changed": True,
                "segments": {"added": [], "removed": [], "changed": []},
                "materials": {"changed": ["mat-1"]}}
        with stub_bridge(diff, segments=[]):
            out, rep = RC.reconcile(doc, "/tmp/draft", make_prov(doc))
        self.assertTrue(rep["ok"])
        self.assertTrue(any("materials changed" in w for w in rep["warnings"]))


class TestFailClosed(unittest.TestCase):
    def test_a_patch_that_breaks_the_ir_is_refused(self):
        doc = make_ir()
        prov = make_prov(doc)
        removed = [prov.segment_for_ir(c["id"]) for c in I.main_track(doc)["clips"]]
        diff = {"changed": True,
                "segments": {"added": [], "removed": removed, "changed": []},
                "materials": {"changed": []}}
        with stub_bridge(diff, segments=[]):
            out, rep = RC.reconcile(doc, "/tmp/draft", prov)
        # emptying the main track must not produce an "ok" IR
        self.assertFalse(rep["ok"])
        self.assertTrue(rep["errors"])


if __name__ == "__main__":
    unittest.main()
