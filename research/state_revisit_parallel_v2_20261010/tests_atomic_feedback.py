"""Deterministic Atomic2 unit tests and equal-budget Slot1 controls.

The maximum job size is imported from the implementation, fixed at six before
these tests were written. The fixture and parameter generator are unchanged v1
components. Their declared consuming/read-only modes are inputs to both policies,
never discovered by inspecting the fixture's internal resource dictionary.
"""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import unittest

from atomic_feedback import (AtomicFeedback, Budget, BudgetRefused, LiveState,
                             MAX_JOB_REQUESTS, PhysicalCall, Reply, TargetContract)

FROZEN = Path(__file__).resolve().parent.parent / "rest_state_revisit_parallel"
sys.path.insert(0, str(FROZEN))
from fixtures import (BASELINE_OPERATIONS, InProcessAPI, SharedEntities,
                      SharedParameters, run_policy as frozen_run_policy)
from scheduler import Scheduler


SCOPE = (("account", "A", 0), ("auth", "test"))
STATE = LiveState((SCOPE, "order", "7", 0), SCOPE, 1, '"v2"')
READ_ONLY = TargetContract("read_only", "v1 InProcessAPI declared nonconsuming T contract")
CONSUMES = TargetContract("consumes", "v1 InProcessAPI declared consuming T contract")


def target(label="T"):
    return lambda state: PhysicalCall(label, state, state.version)


def run_comparison_policy(policy, cap, *, consuming=False):
    """Same baseline/entities/parameters/observations; only feedback slot differs.

    This small adapter reads only HTTP-like response fields and its local ledger.
    It never reads API.instances, generations, pending state, or target internals.
    All observations (including replay) pass through the shared physical budget.
    A consumes contract requires the existing C,U constructive recipe.
    """
    if policy not in {"slot1", "atomic2"}:
        raise ValueError("unknown policy")
    server = InProcessAPI(consuming_target=consuming)
    request_once = server.request
    params = SharedParameters()
    budget = Budget(cap, 1.0, clock=lambda: 0.0)
    scheduler = Scheduler()
    contract = CONSUMES if consuming else READ_ONLY
    current = None
    known_entities = {}
    creation_count = 0
    receipts = []
    refused = []
    baseline = 0

    def create_factory(role):
        def build(_):
            nonlocal creation_count
            entities = SharedEntities(resource_id=str(7 + creation_count))
            creation_count += 1
            return PhysicalCall(("C", 1, role, entities), None)
        return build

    def operation_factory(operation, limit, role):
        def build(state):
            return PhysicalCall((operation, limit, role, known_entities[state.resource_key]),
                                state, state.version)
        return build

    def transport(call):
        nonlocal current
        operation, limit, role, entities = call.payload
        request = entities.request(operation, limit=limit, role=role, if_match=call.if_match)
        response = request_once(request)
        previous = call.binding
        if operation == "C" and response.status == 201:
            key = (SCOPE, "order", response.body["id"], response.generation)
            current = LiveState(key, SCOPE, 0, response.version, ready=False)
            known_entities[key] = SharedEntities(resource_id=response.body["id"])
        elif previous is not None:
            current = previous
            if operation == "U" and response.completed:
                current = replace(previous, revision=previous.revision + 1,
                                  version=response.version, ready=True)
            elif operation == "D" and response.completed:
                current = replace(previous, revision=previous.revision + 1,
                                  version=response.version, ready=False, live=False)
            elif operation == "T" and consuming and response.completed:
                current = replace(previous, revision=previous.revision + 1,
                                  version=response.version, ready=False)
        reply = Reply(response.status, current)
        receipts.append((operation, limit, role, response, reply))
        return reply

    executor = AtomicFeedback(budget, transport)
    rebuild = (create_factory("prefix"), operation_factory("U", 1, "prefix"))

    def charged(factory, state, origin, plan=None):
        return budget.send(transport, factory(state), origin=origin, plan=plan)

    while budget.remaining > 0:
        operation = BASELINE_OPERATIONS[baseline % len(BASELINE_OPERATIONS)]
        factory = create_factory("baseline") if operation == "C" else operation_factory(operation, 1, "baseline")
        reply = charged(factory, current, "ordinary")
        response = receipts[-1][3]
        if operation == "T" and reply.status == 409:
            scheduler.register_failure("T:1", current.resource_key, current.revision)
        if operation in {"U", "D"} and response.completed:
            scheduler.completed_mutation(current.resource_key, current.revision, response.event_id)
        baseline += 1
        scheduler.baseline_completed(baseline)
        executor.baseline_completed(baseline)
        job = scheduler.next_feedback()
        if job is None:
            continue
        if policy == "atomic2":
            if current.resource_key != job["resource_key"]:
                refused.append("stale_resource")
                scheduler.mark_result(job, False)
                continue
            outcome = executor.run(current, operation_factory("T", 1, "revisit"),
                                   operation_factory("T", params.local_variant(1), "variant"),
                                   contract=contract, rebuild_prefix=rebuild)
            if not outcome.admitted:
                refused.append(outcome.reason)
            scheduler.mark_result(job, outcome.revisit_status == 200)
            continue

        limit = int(job["target_key"].split(":")[1])
        replay = job["kind"] == "variant" and (not current.live or not current.ready
                                                 or current.resource_key != job["resource_key"])
        try:
            plan = budget.plan_feedback(2 if replay else 0, 0, target_origin=job["kind"])
        except BudgetRefused:
            refused.append("complete_recipe_budget_refused")
            scheduler.mark_result(job, False)
            continue
        try:
            if replay:
                for step in rebuild:
                    reply = charged(step, current, "prefix", plan)
                    if not reply.completed_success:
                        raise AssertionError("this fixed fixture's constructive prefix unexpectedly failed")
            reply = charged(operation_factory("T", limit, job["kind"]), current, job["kind"], plan)
            scheduler.mark_result(job, reply.completed_success)
            if job["kind"] == "revisit" and reply.completed_success:
                scheduler.observe_result("T:1", current.resource_key, current.revision,
                                         "success-" + str(len(receipts)), reply.status,
                                         origin="revisit", mutated=consuming,
                                         variant_target_key="T:" + str(params.local_variant(1)))
        finally:
            plan.close()
    trace = [{"operation": operation, "limit": limit, "role": role, "status": response.status,
              "id": None if reply.state is None else reply.state.resource_key[2],
              "version": response.version}
             for operation, limit, role, response, reply in receipts]
    return {"policy": policy, "cap": cap, "consuming": consuming,
            "physical_requests": len(receipts), "budget_spent": budget.spent,
            "baseline_turns": baseline,
            "state_validation_seen": any(row["status"] == 422 for row in trace),
            "successful_targets": sum(row["operation"] == "T" and row["status"] == 200 for row in trace),
            "refused": refused, "trace": trace}


class AtomicUnitTests(unittest.TestCase):
    def make(self, replies, cap=20):
        self.now = 0.0
        self.calls = []
        remaining = iter(replies)

        def transport(call):
            self.calls.append(call)
            value = next(remaining)
            if isinstance(value, Exception):
                raise value
            return value

        self.budget = Budget(cap, 1.0, clock=lambda: self.now)
        self.executor = AtomicFeedback(self.budget, transport)
        self.executor.baseline_completed(0)
        return self.executor

    def test_fixed_cap_and_exact_live_reuse(self):
        self.assertEqual(MAX_JOB_REQUESTS, 6)
        engine = self.make([Reply(200, STATE), Reply(422, STATE)])
        result = engine.run(STATE, target(), target("T:0"), contract=READ_ONLY)
        self.assertEqual((result.physical_requests, result.reserved_cost), (2, 2))
        self.assertTrue(result.reused_live_state)
        self.assertEqual(self.calls[0].binding, self.calls[1].binding)
        self.assertEqual(self.calls[1].if_match, '"v2"')
        self.assertEqual(self.budget.spent, 2)

    def test_consuming_target_rebuilds_and_uses_fresh_joint_binding(self):
        consumed = replace(STATE, revision=2, version='"v3"', ready=False)
        fresh = replace(STATE, resource_key=(SCOPE, "order", "8", 0), revision=0,
                        version='"v1"', ready=False)
        ready = replace(fresh, revision=1, version='"v2"', ready=True)
        engine = self.make([Reply(200, consumed), Reply(201, fresh), Reply(200, ready), Reply(422, ready)])
        prefix = (lambda _: PhysicalCall("C", None), lambda state: PhysicalCall("U", state, state.version))
        result = engine.run(STATE, target(), target("T:0"), contract=CONSUMES, rebuild_prefix=prefix)
        self.assertEqual(result.physical_requests, 4)
        self.assertTrue(result.rebuilt)
        self.assertFalse(result.reused_live_state)
        self.assertEqual(self.calls[-1].binding, ready)
        self.assertEqual(self.budget.spent, 4)

    def test_unknown_effect_does_not_guess_reuse_from_success(self):
        engine = self.make([])
        result = engine.run(STATE, target(), target("T:0"), contract=TargetContract())
        self.assertEqual(result.reason, "rebuild_evidence_required")
        self.assertEqual(self.budget.spent, 0)

    def test_declared_effect_requires_source(self):
        with self.assertRaises(ValueError):
            TargetContract("read_only")

    def test_live_state_change_aborts_variant_without_unplanned_rebuild(self):
        engine = self.make([Reply(200, replace(STATE, revision=2, version='"v3"'))])
        result = engine.run(STATE, target(), target("T:0"), contract=READ_ONLY,
                            rebuild_prefix=(target("would-be-hidden-repair"),))
        self.assertEqual(result.reason, "live_binding_changed_or_unproven")
        self.assertEqual(result.physical_requests, 1)

    def test_missing_validator_requires_rebuild(self):
        engine = self.make([])
        result = engine.run(replace(STATE, version=None), target(), target(), contract=READ_ONLY)
        self.assertEqual(result.reason, "rebuild_evidence_required")

    def test_complete_recipe_reserved_before_revisit(self):
        engine = self.make([], cap=3)
        result = engine.run(STATE, target(), target(), contract=CONSUMES,
                            rebuild_prefix=(target("C"), target("U")))
        self.assertFalse(result.admitted)
        self.assertEqual(result.reserved_cost, 4)
        self.assertEqual(self.budget.remaining, 3)
        self.assertEqual(self.calls, [])

    def test_prefix_more_than_four_refused_not_truncated(self):
        engine = self.make([], cap=100)
        result = engine.run(STATE, target(), target(), contract=CONSUMES,
                            rebuild_prefix=(target("P"),) * 5)
        self.assertEqual(result.reason, "complete_prefix_too_long")
        self.assertEqual(self.budget.spent, 0)

    def test_exact_maximum_six_request_job(self):
        replies = [Reply(200, STATE)] * 6
        engine = self.make(replies, cap=6)
        result = engine.run(STATE, target(), target(), contract=TargetContract(),
                            rebuild_prefix=(target("P"),) * 4)
        self.assertEqual(result.physical_requests, 6)
        self.assertEqual(result.reserved_cost, 6)
        self.assertEqual(self.budget.remaining, 0)

    def test_202_403_412_never_trigger_local_variant(self):
        for code in (202, 403, 412, 409, 500):
            engine = self.make([Reply(code, STATE)])
            result = engine.run(STATE, target(), target(), contract=READ_ONLY)
            self.assertEqual(result.physical_requests, 1)
            self.assertEqual(result.reason, "revisit_not_completed_success")

    def test_rebuild_202_stops_and_charges_prefix(self):
        engine = self.make([Reply(200, STATE), Reply(202, STATE)])
        result = engine.run(STATE, target(), target(), contract=CONSUMES,
                            rebuild_prefix=(target("C"), target("U")))
        self.assertEqual(result.reason, "rebuild_not_completed_success")
        self.assertEqual(self.budget.spent, 2)

    def test_rebuild_cannot_cross_parent_generation_or_auth_scope(self):
        other_scope = (("account", "A", 9), ("auth", "other"))
        engine = self.make([Reply(200, STATE), Reply(201, replace(STATE, scope_key=other_scope))])
        result = engine.run(STATE, target(), target(), contract=CONSUMES,
                            rebuild_prefix=(target("C"),))
        self.assertEqual(result.reason, "rebuild_scope_unproven_or_changed")
        self.assertIsNone(result.variant_status)

    def test_wrong_scope_prefix_request_is_rejected_before_dispatch(self):
        engine = self.make([Reply(200, STATE)])
        wrong = replace(STATE, scope_key=(("auth", "other"),))
        prefix = (lambda _: PhysicalCall("U", wrong, wrong.version),)
        result = engine.run(STATE, target(), target(), contract=CONSUMES, rebuild_prefix=prefix)
        self.assertEqual(result.reason, "request_error:ValueError")
        self.assertEqual(result.physical_requests, 1)
        self.assertEqual(self.budget.spent, 1)

    def test_prefix_final_binding_must_be_ready_and_versioned(self):
        engine = self.make([Reply(200, STATE), Reply(201, replace(STATE, ready=False))])
        result = engine.run(STATE, target(), target(), contract=CONSUMES,
                            rebuild_prefix=(target("C"),))
        self.assertEqual(result.reason, "rebuilt_binding_not_proven_ready")

    def test_target_factory_cannot_swap_binding_or_omit_validator(self):
        engine = self.make([])
        result = engine.run(STATE, lambda state: PhysicalCall("T", state), target(), contract=READ_ONLY)
        self.assertEqual(result.reason, "request_error:ValueError")
        self.assertEqual(self.budget.spent, 0)

    def test_transport_exception_is_charged_and_unused_reservations_released(self):
        engine = self.make([OSError("connection failed")], cap=2)
        result = engine.run(STATE, target(), target(), contract=READ_ONLY)
        self.assertEqual(result.reason, "request_error:OSError")
        self.assertEqual((result.physical_requests, self.budget.spent, self.budget.remaining), (1, 1, 1))

    def test_deadline_can_expire_between_targets_without_second_dispatch(self):
        engine = self.make([Reply(200, STATE)])
        original = engine.transport

        def expires(call):
            reply = original(call)
            self.now = 1.0
            return reply

        engine.transport = expires
        result = engine.run(STATE, target(), target(), contract=READ_ONLY)
        self.assertEqual(result.reason, "dispatch_budget_refused")
        self.assertEqual(self.budget.spent, 1)

    def test_one_atomic_job_per_baseline_and_no_recursive_variants(self):
        engine = self.make([Reply(200, STATE), Reply(200, STATE), Reply(200, STATE)])
        first = engine.run(STATE, target(), target(), contract=READ_ONLY)
        self.assertEqual(first.physical_requests, 2)
        engine.baseline_completed(0)
        second = engine.run(STATE, target(), target(), contract=READ_ONLY)
        self.assertEqual(second.reason, "baseline_slot_required")
        engine.baseline_completed(1)
        self.assertEqual(engine.run(STATE, target(), None, contract=READ_ONLY).reason, "revisit_only")

    def test_baseline_cannot_interleave_during_atomic_job(self):
        engine = self.make([])

        def transport(call):
            engine.baseline_completed(1)
            return Reply(200, STATE)

        engine.transport = transport
        result = engine.run(STATE, target(), target(), contract=READ_ONLY)
        self.assertEqual(result.reason, "request_error:RuntimeError")
        self.assertEqual(result.physical_requests, 1)


class ComparativeTests(unittest.TestCase):
    def test_frozen_scheduler_digest_unchanged(self):
        self.assertEqual(hashlib.sha256((FROZEN / "scheduler.py").read_bytes()).hexdigest(),
                         "96d3e1c78b26f1ec04c6fdf4043cf6265ae84fc17af688e5e8c8e7ea964743d3")

    def test_slot1_reproduces_frozen_six_and_eight_request_results(self):
        for cap in (6, 8):
            for consuming in (False, True):
                old = frozen_run_policy("C", cap, consuming=consuming)
                current = run_comparison_policy("slot1", cap, consuming=consuming)
                self.assertEqual(old["state_validation_seen"], current["state_validation_seen"])
                self.assertEqual([(r["operation"], r["limit"], r["role"], r["status"]) for r in old["trace"]],
                                 [(r["operation"], r["limit"], r["role"], r["status"]) for r in current["trace"]])

    def test_nonconsuming_atomic_reuses_live_state_before_baseline_delete(self):
        slot1 = run_comparison_policy("slot1", 6)
        atomic = run_comparison_policy("atomic2", 6)
        self.assertFalse(slot1["state_validation_seen"])
        self.assertTrue(atomic["state_validation_seen"])
        self.assertEqual([r["operation"] for r in atomic["trace"]], ["C", "T", "U", "T", "T", "D"])
        self.assertEqual([r["role"] for r in atomic["trace"]][3:5], ["revisit", "variant"])
        self.assertEqual(atomic["physical_requests"], slot1["physical_requests"])

    def test_consuming_atomic_rebuilds_with_fully_charged_requests(self):
        atomic = run_comparison_policy("atomic2", 8, consuming=True)
        self.assertEqual([r["operation"] for r in atomic["trace"]], ["C", "T", "U", "T", "C", "U", "T", "D"])
        self.assertEqual([r["role"] for r in atomic["trace"]][3:7], ["revisit", "prefix", "prefix", "variant"])
        self.assertNotEqual(atomic["trace"][3]["id"], atomic["trace"][6]["id"])
        self.assertEqual(atomic["budget_spent"], 8)

    def test_costly_rebuild_control_atomic_loses_successful_revisit_at_same_cap(self):
        slot1 = run_comparison_policy("slot1", 6, consuming=True)
        atomic = run_comparison_policy("atomic2", 6, consuming=True)
        self.assertEqual(slot1["successful_targets"], 1)
        self.assertEqual(atomic["successful_targets"], 0)
        self.assertIn("complete_recipe_budget_refused", atomic["refused"])
        self.assertFalse(slot1["state_validation_seen"])
        self.assertFalse(atomic["state_validation_seen"])
        self.assertEqual((slot1["budget_spent"], atomic["budget_spent"]), (6, 6))

    def test_first_variant_success_cost_tradeoff_is_explicit(self):
        self.assertTrue(run_comparison_policy("atomic2", 5)["state_validation_seen"])
        self.assertFalse(run_comparison_policy("atomic2", 6, consuming=True)["state_validation_seen"])
        self.assertTrue(run_comparison_policy("atomic2", 7, consuming=True)["state_validation_seen"])

    def test_every_global_budget_cutoff_and_baseline_order(self):
        for policy in ("slot1", "atomic2"):
            for consuming in (False, True):
                for cap in range(21):
                    result = run_comparison_policy(policy, cap, consuming=consuming)
                    self.assertEqual(result["physical_requests"], result["budget_spent"])
                    self.assertLessEqual(result["physical_requests"], cap)
                    baseline = [r["operation"] for r in result["trace"] if r["role"] == "baseline"]
                    self.assertEqual(baseline, [BASELINE_OPERATIONS[i % 4] for i in range(len(baseline))])
                    burst = 0
                    for row in result["trace"]:
                        burst = 0 if row["role"] == "baseline" else burst + 1
                        self.assertLessEqual(burst, MAX_JOB_REQUESTS)


if __name__ == "__main__":
    if "--compare" in sys.argv:
        for consuming in (False, True):
            for cap in (4, 5, 6, 7, 8):
                for policy in ("slot1", "atomic2"):
                    print(json.dumps(run_comparison_policy(policy, cap, consuming=consuming), sort_keys=True))
    else:
        unittest.main()
