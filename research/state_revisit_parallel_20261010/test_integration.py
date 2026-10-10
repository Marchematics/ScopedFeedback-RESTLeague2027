"""Mechanism and counterexample tests. Run: python -m unittest -v.

Raw observed traces: python test_integration.py --traces
"""

import json
import sys
import unittest

from fixtures import (InProcessAPI, SyntheticFaultAPI, Request, SharedEntities, SharedParameters,
                      execute_policy_trace, run_policy)
from resources import (ConstructivePrefix, OutputRef, PrefixStep, ResourceStore,
                       StepResult)
from scheduler import Budget, BudgetRefused, Scheduler


class FixtureTests(unittest.TestCase):
    def test_nonconsuming_target_and_timestamp_noise(self):
        api = InProcessAPI()
        api.request(Request("C"))
        empty = api.request(Request("T"))
        self.assertEqual(empty.status, 409)
        api.request(Request("U"))
        first = api.request(Request("T"))
        second = api.request(Request("T"))
        self.assertEqual((first.status, second.status), (200, 200))
        self.assertNotEqual(first.body["observed_at"], second.body["observed_at"])
        self.assertEqual(first.version, second.version)
        self.assertEqual(api.instances[("A", "7")].items, 1)

    def test_hand_trace_is_explicitly_six_and_consuming_replay_eight(self):
        ordinary = execute_policy_trace()
        consuming = execute_policy_trace(consuming=True)
        self.assertEqual([e.response.status for e in ordinary], [201, 409, 200, 200, 422, 204])
        self.assertEqual(len(ordinary), 6)
        self.assertEqual(len(consuming), 8)
        self.assertEqual([e.request.operation for e in consuming], ["C", "T", "U", "T", "C", "U", "T", "D"])
        self.assertEqual(consuming[-2].response.status, 422)
        self.assertTrue(all(e.request.role == "hand_policy_trace" for e in ordinary + consuming))

    def test_consuming_target_without_replay_does_not_reach_fault(self):
        api = SyntheticFaultAPI(consuming_target=True)
        for operation in ("C", "U", "T"):
            api.request(Request(operation))
        self.assertEqual(api.request(Request("T", limit=2)).status, 409)

    def test_limit_zero_is_valid_and_state_check_precedes_422(self):
        api = InProcessAPI()
        api.request(Request("C"))
        self.assertEqual(api.request(Request("T", limit=0)).status, 409)
        api.request(Request("U"))
        self.assertEqual(api.request(Request("T", limit=0)).status, 422)

    def test_planted_500_fixture_is_named_separately(self):
        trace = execute_policy_trace(fixture_name="synthetic_fault")
        self.assertEqual(trace[-2].response.status, 500)

    def test_same_identifier_under_different_parents_is_independent(self):
        api = InProcessAPI()
        for parent in ("A", "B"):
            api.request(Request("C", parent=parent))
        api.request(Request("U", parent="B"))
        self.assertEqual(api.request(Request("T", parent="A")).status, 409)
        self.assertEqual(api.request(Request("T", parent="B")).status, 200)

    def test_pending_delete_does_not_mean_deleted(self):
        api = InProcessAPI()
        api.request(Request("C"))
        pending = api.request(Request("D", async_completion=True))
        self.assertEqual(pending.status, 202)
        self.assertFalse(pending.completed)
        self.assertEqual(api.request(Request("G")).status, 200)
        self.assertTrue(api.complete_event(pending.event_id))
        self.assertFalse(api.complete_event(pending.event_id))
        self.assertEqual(api.request(Request("G")).status, 404)

    def test_412_validator_is_instance_specific(self):
        api = InProcessAPI()
        a = api.request(Request("C", parent="A"))
        b = api.request(Request("C", parent="B"))
        api.request(Request("U", parent="A"))
        self.assertEqual(api.request(Request("U", parent="A", if_match=a.version)).status, 412)
        self.assertEqual(api.request(Request("U", parent="B", if_match=b.version)).status, 200)


class FairIntegrationTests(unittest.TestCase):
    def test_shared_components_have_no_result_access(self):
        parameters = SharedParameters()
        entities = SharedEntities()
        self.assertEqual(parameters.local_variant(1), 0)
        self.assertEqual(entities.request("T").limit, 1)
        self.assertEqual(entities.request("T", limit=2).limit, 2)

    def test_A_fixed_round_robin_first_six(self):
        result = run_policy("A", 6)
        self.assertEqual([x["operation"] for x in result["trace"]], ["C", "T", "U", "D", "C", "T"])
        self.assertEqual(result["physical_requests"], 6)
        self.assertFalse(result["synthetic_fault_seen"])
        self.assertFalse(result["state_validation_seen"])

    def test_B_revisit_without_local_variant(self):
        result = run_policy("B", 8)
        self.assertTrue(any(x["role"] == "revisit" and x["status"] == 200 for x in result["trace"]))
        self.assertFalse(any(x["role"] == "variant" for x in result["trace"]))
        self.assertFalse(result["synthetic_fault_seen"])

    def test_C_has_no_assumed_six_request_win(self):
        result = run_policy("C", 6)
        self.assertEqual(result["physical_requests"], 6)
        self.assertFalse(result["synthetic_fault_seen"])
        self.assertTrue(result["rejected_feedback"])

    def test_C_replay_is_physically_charged(self):
        for consuming in (False, True):
            result = run_policy("C", 8, consuming=consuming)
            self.assertEqual(result["physical_requests"], 8)
            self.assertTrue(result["state_validation_seen"])
            self.assertFalse(result["synthetic_fault_seen"])
            self.assertEqual([x["operation"] for x in result["trace"] if x["role"] == "prefix"], ["C", "U"])
            self.assertEqual(result["trace"][-1]["role"], "variant")

    def test_separate_synthetic_fault_requires_eight_under_fair_policy(self):
        six = run_policy("C", 6, fixture_name="synthetic_fault")
        eight = run_policy("C", 8, fixture_name="synthetic_fault")
        self.assertFalse(six["synthetic_fault_seen"])
        self.assertTrue(eight["synthetic_fault_seen"])

    def test_every_budget_cutoff_is_hard(self):
        for cap in range(21):
            for mode in ("A", "B", "C"):
                result = run_policy(mode, cap)
                self.assertLessEqual(result["physical_requests"], cap)
                self.assertEqual(result["physical_requests"], result["budget_spent"])
                self.assertEqual([x["number"] for x in result["trace"]], list(range(1, result["physical_requests"] + 1)))

    def test_baseline_remains_fair_in_real_dispatches(self):
        result = run_policy("C", 40)
        baseline = [x["operation"] for x in result["trace"] if x["role"] == "baseline"]
        self.assertEqual(baseline, ["C", "T", "U", "D"] * (len(baseline) // 4) + ["C", "T", "U", "D"][:len(baseline) % 4])
        feedback_targets_since_baseline = 0
        for request in result["trace"]:
            if request["role"] == "baseline":
                feedback_targets_since_baseline = 0
            elif request["role"] in ("revisit", "variant"):
                feedback_targets_since_baseline += 1
                self.assertLessEqual(feedback_targets_since_baseline, 1)


class ScopedCounterexampleTests(unittest.TestCase):
    def setUp(self):
        self.store = ResourceStore()
        self.parent_a = self.store.create("account", "A")
        self.parent_b = self.store.create("account", "B")
        self.a = self.store.create("order", "7", parent=self.parent_a.key, server_version='"v1"')
        self.b = self.store.create("order", "7", parent=self.parent_b.key, server_version='"v1"')

    def test_colliding_id_and_old_generation_cannot_activate_failure(self):
        scheduler = Scheduler()
        scheduler.register_failure("T:1", self.a.key, self.a.revision)
        updated_b = self.store.complete_mutation(self.b.key, "B-update")
        scheduler.completed_mutation(updated_b.key, updated_b.revision, "B-update")
        scheduler.baseline_completed(1)
        self.assertIsNone(scheduler.next_feedback())
        self.store.confirm_delete(self.a.key, "A-delete")
        newer_a = self.store.create("order", "7", parent=self.parent_a.key)
        self.assertNotEqual(newer_a.key, self.a.key)
        newer_a = self.store.complete_mutation(newer_a.key, "A-new-generation")
        scheduler.completed_mutation(newer_a.key, newer_a.revision, "A-new-generation")
        scheduler.baseline_completed(2)
        self.assertIsNone(scheduler.next_feedback())

    def test_pending_completion_and_duplicate_completion(self):
        scheduler = Scheduler()
        scheduler.register_failure("T", self.a.key, self.a.revision)
        pending = self.store.complete_mutation(self.a.key, "async-U", status_code=202)
        self.assertEqual(pending.revision, self.a.revision)
        scheduler.completed_mutation(pending.key, pending.revision, "async-U", status_code=202)
        scheduler.baseline_completed(1)
        self.assertIsNone(scheduler.next_feedback())
        completed = self.store.complete_mutation(self.a.key, "async-U", status_code=200)
        self.assertEqual(completed.revision, self.a.revision + 1)
        scheduler.completed_mutation(completed.key, completed.revision, "async-U")
        scheduler.baseline_completed(2)
        job = scheduler.next_feedback()
        self.assertIsNotNone(job)
        scheduler.mark_result(job, False)
        duplicate = self.store.complete_mutation(self.a.key, "async-U")
        scheduler.completed_mutation(duplicate.key, duplicate.revision, "async-U")
        scheduler.baseline_completed(3)
        self.assertEqual(duplicate.revision, completed.revision)
        self.assertIsNone(scheduler.next_feedback())

    def test_timestamp_observation_is_not_causal_mutation(self):
        updated = self.store.observe(self.a.key, joint_fields={"timestamp": "later"})
        self.assertEqual(updated.revision, self.a.revision)

    def test_412_only_released_by_same_instance_version_evidence(self):
        scheduler = Scheduler()
        scheduler.register_failure("T", self.a.key, self.a.revision, failure_kind="version")
        scheduler.completed_mutation(self.a.key, self.a.revision + 1, "ordinary-U")
        scheduler.notify_version_change(self.b.key, self.b.revision + 1, "B-version")
        scheduler.baseline_completed(1)
        self.assertIsNone(scheduler.next_feedback())
        scheduler.notify_version_change(self.a.key, self.a.revision + 1, "A-version")
        scheduler.baseline_completed(2)
        job = scheduler.next_feedback()
        self.assertIsNotNone(job)
        self.assertEqual(job["resource_key"], self.a.key)

    def test_prefix_rebinds_new_outputs_and_counts_failure(self):
        prefix = ConstructivePrefix([
            PrefixStep("C", {}, ("id", "version")),
            PrefixStep("U", {"id": OutputRef("C", "id"), "version": OutputRef("C", "version")}, ()),
            PrefixStep("T", {}, ()),
        ], "T")
        calls = []

        def transport(step, inputs):
            calls.append((step.step_id, inputs))
            if step.step_id == "C":
                return StepResult(201, {"id": "replayed-42", "version": "fresh-v"})
            return StepResult(500, {})

        budget = Budget(3, deadline=1.0, clock=lambda: 0.0)
        with budget.plan_feedback(2, repairs=0) as plan:
            replay = prefix.replay(transport, before_request=lambda step: bool(plan.dispatch("prefix")))
        self.assertEqual(replay.physical_requests, 2)
        self.assertFalse(replay.completed)
        self.assertEqual(calls[1][1], {"id": "replayed-42", "version": "fresh-v"})

    def test_long_prefix_is_rejected_without_truncation_or_dispatch(self):
        steps = [PrefixStep("setup-%d" % i, {}, ()) for i in range(5)] + [PrefixStep("T", {}, ())]
        with self.assertRaises(ValueError):
            ConstructivePrefix(steps, "T")
        budget = Budget(20, deadline=1.0, clock=lambda: 0.0)
        with self.assertRaises(BudgetRefused):
            budget.plan_feedback(5, repairs=0)

    def test_feedback_origins_cannot_generate_feedback_recursion(self):
        scheduler = Scheduler()
        for index, origin in enumerate(("prefix", "repair", "cleanup", "variant")):
            scheduler.observe_result("T", self.a.key, self.a.revision, "seed-%d" % index,
                                     200, origin=origin, mutated=True, variant_target_key="T2")
        scheduler.baseline_completed(1)
        self.assertIsNone(scheduler.next_feedback())


def raw_traces():
    return {
        "label": "synthetic in-process mechanism traces; no official evaluator claims",
        "hand_policy_trace_nonconsuming": [e.as_dict() for e in execute_policy_trace()],
        "hand_policy_trace_consuming": [e.as_dict() for e in execute_policy_trace(consuming=True)],
        "real_integration": [run_policy(mode, cap) for cap in (6, 8) for mode in ("A", "B", "C")],
        "separate_planted_fault_integration": [run_policy(mode, cap, fixture_name="synthetic_fault") for cap in (6, 8) for mode in ("A", "B", "C")],
    }


if __name__ == "__main__":
    if "--traces" in sys.argv:
        print(json.dumps(raw_traces(), indent=2, sort_keys=True))
    else:
        unittest.main()
