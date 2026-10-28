"""Cross-file association tests use original evidence identities and times."""
from copy import deepcopy
import unittest

from apps.control_api.domain.association import associate_facts


def fact(name, kind, time="2026-10-01T08:00:00", face="W1", event=None, fields=None):
    return {"fact_id": name, "kind": kind, "time": time, "workface": face,
            "event_id": event, "fields": fields or {},
            "source_role": "requirement" if kind in {"threshold", "requirements"} else "actual_record",
            "evidence_refs": [f"evidence-{name}"]}


class AssociationTests(unittest.TestCase):
    def test_no_common_id_complete_chain_and_auditable_links(self):
        facts = [fact("trigger", "event"), fact("stop", "action", "2026-10-01T08:01:00"),
                 fact("unload", "action", "2026-10-01T08:10:00"),
                 fact("measure", "measurement", "2026-10-01T08:20:00")]
        original = deepcopy(facts)
        result = associate_facts(facts, observation={"start": "2026-10-01", "end": "2026-10-01"})
        self.assertEqual(facts, original)
        self.assertFalse(result["gaps"])
        self.assertEqual(len({f["event_id"] for f in result["facts"]}), 1)
        self.assertIsNone(result["facts"][0]["source_event_id"])
        stop = next(l for l in result["links"] if l["fact_id"] == "stop")
        self.assertEqual(stop["evidence_refs"], ["evidence-stop", "evidence-trigger"])
        self.assertIn("temporal_partition", stop)
        self.assertTrue(stop["limitations"])

    def test_stable_generated_id_does_not_depend_on_order(self):
        facts = [fact("trigger", "event"), fact("action", "action", "2026-10-01T09:00:00")]
        first = associate_facts(facts)
        second = associate_facts(list(reversed(facts)))
        self.assertEqual(first["facts"][0]["event_id"], second["facts"][1]["event_id"])

    def test_two_same_face_events_use_half_open_partitions(self):
        result = associate_facts([fact("first", "event"),
                                  fact("second", "event", "2026-10-01T10:00:00"),
                                  fact("before", "action", "2026-10-01T09:00:00"),
                                  fact("boundary", "action", "2026-10-01T10:00:00"),
                                  fact("after", "measurement", "2026-10-01T10:05:00")])
        facts = {f["fact_id"]: f for f in result["facts"]}
        self.assertEqual(facts["before"]["event_id"], facts["first"]["event_id"])
        self.assertEqual(facts["boundary"]["event_id"], facts["second"]["event_id"])
        self.assertEqual(facts["after"]["event_id"], facts["second"]["event_id"])
        self.assertFalse(result["gaps"])

    def test_face_details_are_not_erased(self):
        for face in ("W1进风巷", "W1回风巷", "W2"):
            with self.subTest(face=face):
                result = associate_facts([fact("event", "event"), fact("action", "action", "2026-10-01T09:00:00", face)])
                self.assertIsNone(result["facts"][1]["event_id"])
                self.assertTrue(result["gaps"])

    def test_nfkc_and_whitespace_face_normalization_preserves_source(self):
        result = associate_facts([fact("event", "event", face="Ｗ１"),
                                  fact("action", "action", "2026-10-01T09:00:00", "  W1\t")])
        self.assertFalse(result["gaps"])
        self.assertEqual(result["facts"][0]["workface"], "W1")
        self.assertEqual(result["facts"][0]["source_workface"], "Ｗ１")

    def test_date_only_event_or_fact_cannot_invent_minutes(self):
        for event_time, action_time in (("2026-10-01", "2026-10-01T09:00:00"),
                                        ("2026-10-01T08:00:00", "2026-10-01")):
            with self.subTest(event=event_time, action=action_time):
                result = associate_facts([fact("event", "event", event_time), fact("action", "action", action_time)])
                self.assertIsNone(result["facts"][1]["event_id"])
                self.assertEqual(result["facts"][1]["time"], action_time)
                self.assertTrue(result["gaps"])

    def test_overlapping_event_times_cannot_select_last_event(self):
        for second_time in ("2026-10-01T08:00:00", "2026-10-01"):
            result = associate_facts([fact("first", "event"), fact("second", "event", second_time),
                                      fact("action", "action", "2026-10-01T09:00:00")])
            self.assertIsNone(result["facts"][2]["event_id"])
            self.assertTrue(result["gaps"])

    def test_earlier_next_day_and_absent_trigger_do_not_bind(self):
        for when in ("2026-10-01T07:59:00", "2026-10-02T09:00:00", "2026-10-31T09:00:00"):
            result = associate_facts([fact("event", "event"), fact("action", "action", when)])
            self.assertIsNone(result["facts"][1]["event_id"])
            self.assertTrue(result["gaps"])
        result = associate_facts([fact("action", "action")])
        self.assertEqual(len(result["facts"]), 1)
        self.assertIsNone(result["facts"][0]["event_id"])

    def test_observation_excludes_outside_fact_and_invalid_bounds(self):
        for observation in ({"start": "2026-10-01T08:00:00", "end": "2026-10-01T08:30:00"},
                            {"start": "2026-10-02", "end": "2026-10-01"}, {}):
            result = associate_facts([fact("event", "event"), fact("action", "action", "2026-10-01T09:00:00")],
                                     observation=observation)
            self.assertIsNone(result["facts"][1]["event_id"])
            self.assertTrue(result["gaps"])

    def test_explicit_id_preserved_even_for_inconsistent_scope_or_sequence(self):
        for face, when in (("W2", "2026-10-01T09:00:00"), ("W1", "2026-10-01T07:00:00")):
            result = associate_facts([fact("event", "event", event="E1"),
                                      fact("action", "action", when, face, "E1")])
            self.assertEqual(result["facts"][1]["event_id"], "E1")
            self.assertTrue(result["gaps"])

    def test_same_explicit_id_on_different_faces_reports_conflict(self):
        result = associate_facts([fact("first", "event", event="E1"),
                                  fact("second", "event", face="W2", event="E1")])
        self.assertTrue(any("multiple workfaces" in gap for gap in result["gaps"]))

    def test_duplicate_source_refs_do_not_duplicate_link_evidence(self):
        event, action = fact("event", "event"), fact("action", "action", "2026-10-01T09:00:00")
        event["evidence_refs"] *= 2
        action["evidence_refs"] = ["evidence-action", "evidence-action", "evidence-event"]
        result = associate_facts([event, action])
        self.assertEqual(result["links"][-1]["evidence_refs"], ["evidence-action", "evidence-event"])

    def test_requirement_reused_across_period_events_with_unique_provenance(self):
        requirement = fact("threshold", "threshold", fields={"valid_from": "2026-09-01", "valid_to": "2026-11-01", "value": 10})
        result = associate_facts([fact("first", "event"), fact("second", "event", "2026-10-03T08:00:00"), requirement])
        thresholds = [f for f in result["facts"] if f["kind"] == "threshold"]
        self.assertEqual(len(thresholds), 2)
        self.assertEqual(len({f["fact_id"] for f in thresholds}), 2)
        self.assertEqual({f["source_fact_id"] for f in thresholds}, {"threshold"})
        self.assertEqual({f["event_id"] for f in thresholds}, {f["event_id"] for f in result["facts"] if f["kind"] == "event"})
        self.assertTrue(all(f["evidence_refs"] == ["evidence-threshold"] for f in thresholds))
        self.assertFalse(result["gaps"])

    def test_future_expired_missing_start_missing_end_or_wrong_face_not_borrowed(self):
        bounds = ({"valid_from": "2026-10-02", "valid_to": "2026-10-30"},
                  {"valid_from": "2026-09-01", "valid_to": "2026-09-30"},
                  {"valid_to": "2026-10-30"}, {"valid_from": "2026-09-01"},
                  {"valid_from": "invalid", "valid_to": "2026-10-30"},
                  {"valid_from": "2026-11-01", "valid_to": "2026-10-01"})
        for fields in bounds:
            with self.subTest(fields=fields):
                result = associate_facts([fact("event", "event"), fact("threshold", "threshold", fields=fields)])
                self.assertIsNone(result["facts"][1]["event_id"])
                self.assertTrue(result["gaps"])
        result = associate_facts([fact("event", "event"), fact("threshold", "threshold", face="W2", fields={
            "valid_from": "2026-09-01", "valid_to": "2026-11-01"})])
        self.assertIsNone(result["facts"][1]["event_id"])

    def test_conflicting_versions_both_survive_no_latest_or_strictest_selection(self):
        fields = {"valid_from": "2026-09-01", "valid_to": "2026-11-01"}
        result = associate_facts([fact("event", "event"),
                                  fact("older", "threshold", fields={**fields, "value": 10}),
                                  fact("newer", "threshold", fields={**fields, "value": 12})])
        self.assertEqual({f["fields"]["value"] for f in result["facts"] if f["kind"] == "threshold"}, {10, 12})

    def test_threshold_can_start_after_event_but_before_measurement(self):
        result = associate_facts([fact("event", "event"), fact("measurement", "measurement", "2026-10-01T10:00:00"),
                                  fact("threshold", "threshold", fields={"valid_from": "2026-10-01T09:00:00", "valid_to": "2026-10-02"})])
        self.assertTrue(result["facts"][-1]["event_id"])
        self.assertEqual(result["facts"][-1]["source_fact_id"], "threshold")

    def test_explicit_requirement_keeps_original_identity_without_validity(self):
        requirement = fact("threshold", "threshold", event="E1")
        result = associate_facts([fact("event", "event", event="E1"), requirement])
        self.assertEqual(result["facts"][1], requirement)

    def test_aware_and_naive_times_are_not_coerced(self):
        result = associate_facts([fact("event", "event", "2026-10-01T08:00:00+08:00"),
                                  fact("action", "action", "2026-10-01T09:00:00")])
        self.assertIsNone(result["facts"][1]["event_id"])
        self.assertTrue(result["gaps"])

    def test_unscoped_unknown_fact_is_preserved_and_reported(self):
        result = associate_facts([fact("unknown", "unknown")])
        self.assertEqual(result["facts"][0]["fact_id"], "unknown")
        self.assertTrue(result["gaps"])


if __name__ == "__main__":
    unittest.main()
