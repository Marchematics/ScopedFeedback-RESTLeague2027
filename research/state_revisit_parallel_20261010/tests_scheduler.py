"""Hand-built counterexamples; pure stdlib, no transport/network/dependencies."""

import unittest

from scheduler import AdmissionRefused, Budget, BudgetRefused, FeedbackPlan, Scheduler


R = (("tenant", "A", 2), ("parent", "P", 3), ("child", "X", 4), ("auth", "u"))


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.s = Scheduler()

    def take(self, turn=0):
        self.s.baseline_completed(turn)
        job = self.s.next_feedback()
        self.assertIsNotNone(job)
        return job

    def test_failure_alone_and_same_revision_never_retry(self):
        self.s.register_failure("close", R, 3)
        self.s.baseline_completed(0)
        self.assertIsNone(self.s.next_feedback())
        self.s.completed_mutation(R, 3, "same")
        self.s.completed_mutation(R, 2, "old")
        self.assertIsNone(self.s.next_feedback())

    def test_round_robin_does_not_retry_registration_head_forever(self):
        for target in ("a", "b", "c"):
            self.s.register_failure(target, R, 0)
        got = []
        for revision in range(1, 7):
            self.s.completed_mutation(R, revision, f"m{revision}")
            job = self.take(revision)
            got.append(job["target_key"])
            self.s.mark_result(job, False)
        self.assertEqual(got, ["a", "b", "c", "a", "b", "c"])

    def test_duplicate_event_admits_only_one_target(self):
        for target in ("a", "b", "c"):
            self.s.register_failure(target, R, 0)
        for _ in range(5):
            self.s.completed_mutation(R, 1, "same-event")
        self.assertEqual(self.s.pending_count, 1)
        job = self.take()
        self.s.mark_result(job, False)
        self.s.baseline_completed(1)
        self.assertIsNone(self.s.next_feedback())

    def test_reservation_rejects_different_event_same_target_resource_revision(self):
        self.s.register_failure("a", R, 0)
        self.s.completed_mutation(R, 1, "m1")
        self.s.completed_mutation(R, 1, "m2")
        job = self.take()
        self.s.mark_result(job, False)
        self.s.register_failure("a", R, 0)  # An out-of-order failure cannot rewind it.
        self.s.completed_mutation(R, 1, "m3")
        self.assertEqual(self.s.pending_count, 0)

    def test_full_parent_chain_generation_and_auth_are_never_collapsed(self):
        self.s.register_failure("a", R, 0)
        others = [
            (("tenant", "B", 2),) + R[1:],
            R[:1] + (("parent", "Q", 3),) + R[2:],
            R[:1] + (("parent", "P", 9),) + R[2:],
            R[:2] + (("child", "X", 9),) + R[3:],
            R[:3] + (("auth", "other"),),
        ]
        for i, other in enumerate(others):
            self.s.completed_mutation(other, 1, f"unrelated{i}")
        self.assertEqual(self.s.pending_count, 0)
        self.s.completed_mutation(R, 1, "exact")
        self.assertEqual(self.take()["resource_key"], R)

    def test_auth_and_version_need_corresponding_explicit_notifications(self):
        self.s.register_failure("a403", R, 0, "403")
        self.s.register_failure("b412", R, 0, "412")
        self.s.completed_mutation(R, 1, "ordinary")
        self.assertEqual(self.s.pending_count, 0)
        self.s.notify_version_change(R, 1, "version")
        first = self.take()
        self.assertEqual(first["target_key"], "b412")
        self.s.mark_result(first, True)
        self.s.notify_auth_change(R, 1, "auth")
        self.assertEqual(self.take(1)["target_key"], "a403")

    def test_explicit_header_refresh_can_wake_without_state_revision_change(self):
        self.s.register_failure("conditional", R, 4, "version")
        self.s.notify_version_change(R, 4, "refreshed-header")
        job = self.take()
        self.assertEqual(job["revision"], 4)
        self.s.mark_result(job, False)
        self.s.notify_version_change(R, 4, "another-header")
        self.assertEqual(self.s.pending_count, 0)

    def test_pending_202_and_failed_mutations_do_not_unlock(self):
        self.s.register_failure("a", R, 0)
        for code in (202, 302, 400, 403, 412, 500):
            self.s.completed_mutation(R, 1, "event", status_code=code)
        self.assertEqual(self.s.pending_count, 0)
        self.s.completed_mutation(R, 1, "event", status_code=204)
        self.assertEqual(self.s.pending_count, 1)

    def test_nonordinary_mutations_do_not_unlock_state_failure(self):
        self.s.register_failure("a", R, 0)
        for origin in ("revisit", "variant", "prefix", "repair", "cleanup"):
            self.s.completed_mutation(R, 1, f"m-{origin}", origin=origin)
        self.assertEqual(self.s.pending_count, 0)

    def test_no_feedback_before_baseline_and_one_between_baselines(self):
        for target in ("a", "b"):
            self.s.register_failure(target, R, 0)
        self.s.completed_mutation(R, 1, "m1")
        self.s.completed_mutation(R, 2, "m2")
        self.assertIsNone(self.s.next_feedback())
        first = self.take()
        self.assertIsNone(self.s.next_feedback())
        self.s.mark_result(first, False)
        self.assertIsNone(self.s.next_feedback())
        self.s.baseline_completed(0)  # Duplicate ack cannot refill credit.
        self.assertIsNone(self.s.next_feedback())
        self.assertEqual(self.take(1)["target_key"], "b")

    def test_baseline_credits_do_not_accumulate(self):
        for target in ("a", "b"):
            self.s.register_failure(target, R, 0)
        self.s.completed_mutation(R, 1, "m1")
        self.s.completed_mutation(R, 2, "m2")
        self.s.baseline_completed(0)
        self.s.baseline_completed(1)
        job = self.s.next_feedback()
        self.s.mark_result(job, False)
        self.assertIsNone(self.s.next_feedback())

    def test_late_success_does_not_erase_newer_failure(self):
        self.s.register_failure("a", R, 0)
        self.s.completed_mutation(R, 1, "m1")
        job = self.take()
        self.s.register_failure("a", R, 3)
        self.s.mark_result(job, True)
        self.s.completed_mutation(R, 4, "m4")
        self.assertEqual(self.take(1)["target_key"], "a")

    def test_success_removes_old_failure_without_recursive_jobs(self):
        self.s.register_failure("a", R, 0)
        self.s.completed_mutation(R, 1, "m1")
        job = self.take()
        self.s.mark_result(job, True)
        self.s.completed_mutation(R, 2, "m2")
        self.assertEqual(self.s.pending_count, 0)

    def test_same_event_id_with_different_context_is_rejected(self):
        self.s.completed_mutation(R, 1, "m")
        with self.assertRaises(ValueError):
            self.s.completed_mutation(R, 2, "m")
        with self.assertRaises(ValueError):
            self.s.notify_auth_change(R, 1, "m")

    def test_event_before_failure_is_not_replayed_retroactively(self):
        self.s.completed_mutation(R, 1, "m")
        self.s.register_failure("a", R, 0)
        self.s.completed_mutation(R, 1, "m")
        self.assertEqual(self.s.pending_count, 0)

    def test_one_variant_per_success_and_no_recursive_variant(self):
        self.s.observe_result("base", R, 0, "ok", 200, origin="ordinary", variant_target_key="v")
        self.s.observe_result("base", R, 0, "ok", 200, origin="ordinary", variant_target_key="other-v")
        self.assertEqual(self.s.pending_count, 1)
        job = self.take()
        self.assertEqual(job["kind"], "variant")
        self.s.mark_result(job, True)
        self.s.observe_result("v", R, 1, "v-ok", 200, origin="variant", mutated=True,
                              variant_target_key="recursive")
        self.assertEqual(self.s.pending_count, 0)

    def test_revisit_success_can_seed_one_variant_but_never_wake_state(self):
        self.s.register_failure("waiting", R, 0)
        self.s.observe_result("revisited", R, 1, "rv", 201, origin="revisit", mutated=True,
                              variant_target_key="v")
        self.assertEqual(self.take()["target_key"], "v")
        self.assertEqual(self.s.pending_count, 0)

    def test_completed_mutation_and_variant_compete_for_single_event_slot(self):
        self.s.register_failure("waiting", R, 0)
        self.s.observe_result("base", R, 1, "ok", 200, origin="ordinary", mutated=True,
                              variant_target_key="v")
        self.assertEqual(self.s.pending_count, 1)
        self.assertEqual(self.take()["target_key"], "waiting")

    def test_auxiliary_successes_cannot_seed_variants(self):
        for origin in ("prefix", "repair", "cleanup", "variant"):
            self.s.observe_result("base", R, 1, origin, 200, origin=origin,
                                  mutated=True, variant_target_key="v")
        self.assertEqual(self.s.pending_count, 0)

    def test_variant_cannot_bypass_existing_auth_or_version_failure(self):
        for kind in ("auth", "version", "state"):
            self.s.register_failure(kind, R, 0, kind)
            self.s.observe_result("base", R, 1, "variant-" + kind, 200,
                                  origin="ordinary", variant_target_key=kind)
        self.assertEqual(self.s.pending_count, 0)

    def test_queued_state_job_cannot_bypass_later_403(self):
        self.s.register_failure("a", R, 0)
        self.s.completed_mutation(R, 1, "m1")
        self.s.register_failure("a", R, 1, "auth")
        self.s.baseline_completed(0)
        self.assertIsNone(self.s.next_feedback())
        self.s.notify_auth_change(R, 2, "auth-refresh")
        job = self.s.next_feedback()
        self.assertEqual(job["failure_kind"], "auth")

    def test_newer_failure_invalidates_old_queued_job_without_spending_credit(self):
        self.s.register_failure("a", R, 0)
        self.s.completed_mutation(R, 1, "m1")
        self.s.register_failure("a", R, 3)
        self.s.baseline_completed(0)
        self.assertIsNone(self.s.next_feedback())
        self.s.completed_mutation(R, 4, "m4")
        self.assertEqual(self.s.next_feedback()["revision"], 4)

    def test_202_cannot_seed_variant_before_completion(self):
        self.s.observe_result("base", R, 0, "ok", 202, origin="ordinary", variant_target_key="v")
        self.assertEqual(self.s.pending_count, 0)
        self.s.observe_result("base", R, 0, "ok", 204, origin="ordinary", variant_target_key="v")
        self.assertEqual(self.s.pending_count, 1)

    def test_capacity_refusal_preserves_dedupe(self):
        s = Scheduler(max_pending=1)
        s.register_failure("a", R, 0)
        s.register_failure("b", R, 0)
        s.completed_mutation(R, 1, "m1")
        with self.assertRaises(AdmissionRefused):
            s.completed_mutation(R, 2, "m2")
        s.baseline_completed(0)
        job = s.next_feedback()
        s.mark_result(job, False)
        s.completed_mutation(R, 2, "m2")
        self.assertEqual(s.pending_count, 0)

    def test_finite_ledgers_refuse_instead_of_evicting(self):
        s = Scheduler(max_events=1, max_failures=1, max_reservations=1)
        s.register_failure("a", R, 0)
        with self.assertRaises(AdmissionRefused):
            s.register_failure("b", R, 0)
        s.completed_mutation(R, 1, "m1")
        with self.assertRaises(AdmissionRefused):
            s.completed_mutation(R, 2, "m2")

    def test_job_mutation_and_duplicate_result_are_rejected(self):
        self.s.register_failure("a", R, 0)
        self.s.completed_mutation(R, 1, "m1")
        job = self.take()
        with self.assertRaises(ValueError):
            self.s.mark_result(dict(job, revision=9), True)
        self.s.mark_result(job, False)
        with self.assertRaises(ValueError):
            self.s.mark_result(job, False)

    def test_validation_rejects_unhashable_or_incomplete_types(self):
        with self.assertRaises(TypeError):
            self.s.register_failure("a", [], 0)
        with self.assertRaises(TypeError):
            self.s.register_failure("a", ([],), 0)
        with self.assertRaises(ValueError):
            self.s.register_failure("a", R, True)
        with self.assertRaises(ValueError):
            self.s.register_failure("a", R, 0, "any-error")


class BudgetTests(unittest.TestCase):
    def make(self, cap=10):
        self.now = 0.0
        return Budget(cap, 10.0, clock=lambda: self.now)

    def test_shared_cap_counts_baseline_prefix_target_repair_and_cleanup(self):
        budget = self.make(8)
        self.assertEqual(budget.dispatch("ordinary"), 1)
        with budget.plan_feedback(4, 1) as plan:
            for _ in range(4):
                plan.dispatch("prefix")
            plan.dispatch("repair")
            plan.dispatch("revisit")
        budget.dispatch("cleanup")
        self.assertEqual(budget.spent, 8)
        with self.assertRaises(BudgetRefused):
            budget.dispatch("ordinary")

    def test_long_prefix_or_multiple_repairs_refused_not_truncated(self):
        budget = self.make(100)
        with self.assertRaises(BudgetRefused):
            budget.plan_feedback(5)
        with self.assertRaises(BudgetRefused):
            budget.plan_feedback(0, 2)
        self.assertEqual(budget.spent, 0)
        self.assertEqual(budget.remaining, 100)

    def test_insufficient_full_recipe_refuses_before_any_prefix(self):
        budget = self.make(5)
        with self.assertRaises(BudgetRefused):
            budget.plan_feedback(4, 1)
        self.assertEqual(budget.spent, 0)
        self.assertEqual(budget.remaining, 5)

    def test_reservations_prevent_baseline_from_stealing_admitted_recipe(self):
        budget = self.make(3)
        with budget.plan_feedback(1, 1) as plan:
            self.assertEqual(budget.remaining, 0)
            with self.assertRaises(BudgetRefused):
                budget.dispatch("ordinary")
            plan.dispatch("prefix")
            plan.dispatch("revisit")
        self.assertEqual(budget.spent, 2)
        self.assertEqual(budget.remaining, 1)

    def test_deadline_checked_before_every_physical_send(self):
        budget = self.make(6)
        with budget.plan_feedback(4, 1) as plan:
            plan.dispatch("prefix")
            self.now = 10.0
            with self.assertRaises(BudgetRefused):
                plan.dispatch("prefix")
        self.assertEqual(budget.spent, 1)
        with self.assertRaises(BudgetRefused):
            budget.dispatch("ordinary")

    def test_erroring_transport_is_charged_and_denied_send_never_calls_transport(self):
        budget = self.make(1)
        calls = []

        def transport():
            calls.append(1)
            raise OSError("failed physical attempt")

        with self.assertRaises(OSError):
            budget.send(transport)
        with self.assertRaises(BudgetRefused):
            budget.send(transport)
        self.assertEqual(calls, [1])
        self.assertEqual(budget.spent, 1)

    def test_prefix_repair_and_target_each_have_strict_allowances(self):
        budget = self.make(100)
        with budget.plan_feedback(1, 1) as plan:
            for origin in ("prefix", "repair", "revisit"):
                plan.dispatch(origin)
                with self.assertRaises(BudgetRefused):
                    plan.dispatch(origin)
        self.assertEqual(budget.spent, 3)

    def test_feedback_cannot_bypass_plan_and_variants_use_same_cap(self):
        budget = self.make(1)
        for origin in ("prefix", "repair", "revisit", "variant"):
            with self.assertRaises(BudgetRefused):
                budget.dispatch(origin)
        with budget.plan_feedback(0, 0, target_origin="variant") as plan:
            budget.send(lambda: 204, origin="variant", plan=plan)
        self.assertEqual(budget.spent, 1)
        with self.assertRaises(BudgetRefused):
            budget.plan_feedback(0, 0)

    def test_foreign_and_closed_plans_refused_without_count_corruption(self):
        budget = self.make(2)
        other = self.make(2)
        plan = budget.plan_feedback(0, 1)
        with self.assertRaises(BudgetRefused):
            other.dispatch("revisit", plan=plan)
        plan.close()
        plan.close()
        with self.assertRaises(BudgetRefused):
            plan.dispatch("revisit")
        self.assertEqual(budget.remaining, 2)
        self.assertEqual(other.remaining, 2)

    def test_invalid_numeric_bounds_fail_without_dispatch(self):
        for cap in (-1, True, 1.2):
            with self.assertRaises(ValueError):
                Budget(cap, 10)
        for deadline in (float("inf"), float("nan"), True):
            with self.assertRaises(ValueError):
                Budget(1, deadline)
        budget = self.make()
        with self.assertRaises(ValueError):
            budget.plan_feedback(True)

    def test_manually_constructed_plan_cannot_bypass_shared_cap(self):
        budget = self.make(0)
        with self.assertRaises(BudgetRefused):
            FeedbackPlan(budget, {"revisit": 100})
        self.assertEqual(budget.spent, 0)


if __name__ == "__main__":
    unittest.main()
