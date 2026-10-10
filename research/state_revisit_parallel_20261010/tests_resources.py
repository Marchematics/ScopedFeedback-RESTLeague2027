"""Small stdlib-only counterexamples for resource identity and prefix replay."""
import unittest

from resources import (
    BindingError, ConstructivePrefix, Lifecycle, LifecycleError, OutputRef,
    PrefixStep, ResourceKey, ResourceStore, StepResult,
)


class ResourceStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = ResourceStore()
        self.parent_a = self.store.create("account", "A")
        self.parent_b = self.store.create("account", "B")
        self.child_a = self.store.create("order", "7", parent=self.parent_a,
                                         server_version="a1", joint_fields={"owner": "alice", "sku": "x"})
        self.child_b = self.store.create("order", "7", parent=self.parent_b,
                                         server_version="b1", joint_fields={"owner": "bob", "sku": "y"})

    def test_same_child_id_under_different_parents_isolated(self):
        self.assertEqual(self.child_a.resource_id, self.child_b.resource_id)
        self.assertNotEqual(self.child_a.key, self.child_b.key)
        self.assertIsInstance(self.child_a.key, tuple)
        self.assertIsInstance(self.child_a.key, ResourceKey)
        self.assertEqual(len({self.child_a.key, self.child_b.key}), 2)
        self.store.complete_mutation(self.child_b.key, "change", server_version="b2")
        self.assertEqual(self.store.get(self.child_a.key).revision, 0)
        self.assertEqual(self.store.get(self.child_b.key).revision, 1)

    def test_child_delete_keeps_parent_and_equal_id_sibling(self):
        deleted = self.store.confirm_delete(self.child_a.key, "delete-A")
        self.assertEqual(deleted.lifecycle, Lifecycle.DELETED)
        self.assertEqual(self.store.get(self.parent_a.key).lifecycle, Lifecycle.ACTIVE)
        self.assertEqual(self.store.get(self.child_b.key).lifecycle, Lifecycle.ACTIVE)
        self.assertEqual(self.store.bind(self.parent_a.key, ["id"]), {"id": "A"})

    def test_recreation_requires_confirmed_delete_and_isolates_old_generation(self):
        same = self.store.create("order", "7", parent=self.parent_a, server_version="a2")
        self.assertEqual(same.key, self.child_a.key)
        pending = self.store.begin_delete(self.child_a.key, event_id="d")
        still_same = self.store.create("order", "7", parent=self.parent_a)
        self.assertEqual(still_same.key, pending.key)
        self.assertEqual(still_same.lifecycle, Lifecycle.DELETE_PENDING)
        deleted = self.store.confirm_delete(self.child_a.key, "d")
        recreated = self.store.create("order", "7", parent=self.parent_a, server_version="a-new")
        self.assertEqual(recreated.generation, deleted.generation + 1)
        self.assertNotEqual(recreated.key, deleted.key)
        self.assertEqual(recreated.revision, 0)
        self.store.confirm_delete(self.child_a.key, "d")
        self.assertEqual(self.store.get(recreated.key).lifecycle, Lifecycle.ACTIVE)
        with self.assertRaises(LifecycleError):
            self.store.observe(deleted.key, server_version="stale")

    def test_202_mutation_completion_is_idempotent(self):
        pending = self.store.complete_mutation(self.child_a.key, "u", status_code=202,
                                               server_version="premature")
        self.assertEqual(pending.revision, 0)
        self.assertEqual(pending.server_version, "a1")
        self.assertEqual(pending.pending_event_ids, {"u"})
        finished = self.store.complete_mutation(self.child_a.key, "u", server_version="a2")
        self.assertEqual(finished.revision, 1)
        self.assertFalse(finished.pending_event_ids)
        duplicate = self.store.complete_mutation(self.child_a.key, "u", server_version="stale")
        self.assertEqual(duplicate.revision, 1)
        self.assertEqual(duplicate.server_version, "a2")
        self.store.complete_mutation(self.child_b.key, "u")
        self.assertEqual(self.store.get(self.child_b.key).revision, 1)

    def test_202_delete_does_not_delete_or_advance(self):
        pending = self.store.confirm_delete(self.child_a.key, "d", status_code=202)
        self.assertEqual(pending.lifecycle, Lifecycle.DELETE_PENDING)
        self.assertEqual(pending.revision, 0)
        with self.assertRaises(LifecycleError):
            self.store.bind(pending.key, ["id"])
        finished = self.store.confirm_delete(pending.key, "d", status_code=204)
        self.assertEqual(finished.lifecycle, Lifecycle.DELETED)
        self.assertEqual(finished.revision, 1)
        self.assertEqual(self.store.confirm_delete(pending.key, "d").revision, 1)

    def test_412_refresh_is_per_instance_and_not_causal_revision(self):
        evidence = self.store.record_failure(self.child_a.key, 412)
        self.assertEqual(evidence.scope, "version")
        with self.assertRaises(BindingError):
            self.store.bind(self.child_a.key, {"If-Match": "version"})
        self.assertEqual(self.store.bind(self.child_b.key, {"If-Match": "version"}),
                         {"If-Match": "b1"})
        refreshed = self.store.observe(self.child_a.key, server_version="a2")
        self.assertEqual(refreshed.revision, 0)
        self.assertFalse(refreshed.needs_version_refresh)
        self.assertEqual(self.store.bind(self.child_a.key, {"If-Match": "version"}),
                         {"If-Match": "a2"})
        self.assertEqual(self.store.get(self.child_b.key).server_version, "b1")

    def test_none_is_not_a_refreshed_validator(self):
        self.store.record_failure(self.child_a.key, 412)
        observed = self.store.observe(self.child_a.key, server_version=None)
        self.assertTrue(observed.needs_version_refresh)
        with self.assertRaises(BindingError):
            self.store.bind(self.child_a.key, ["version"])
        self.assertEqual(self.store.bind(self.child_a.key, ["id"]), {"id": "7"})
        with self.assertRaises(BindingError):
            self.store.bind(self.parent_a.key, ["version"])

    def test_404_is_relation_negative_not_global_instance_deletion(self):
        with self.assertRaises(ValueError):
            self.store.record_failure(self.child_a.key, 404)
        evidence = self.store.record_failure(self.child_a.key, 404, relation="get-invoice")
        self.assertEqual(evidence.scope, "relation")
        self.assertEqual(self.store.get(self.child_a.key).lifecycle, Lifecycle.ACTIVE)
        self.assertIn((self.child_a.key, "get-invoice"), self.store.relation_negatives)
        self.assertNotIn((self.child_b.key, "get-invoice"), self.store.relation_negatives)
        self.assertEqual(self.store.create("order", "7", parent=self.parent_a).generation, 0)
        with self.assertRaises(ValueError):
            self.store.confirm_delete(self.child_a.key, "bad", status_code=404)

    def test_403_authorization_context_isolated(self):
        other_auth = self.store.create("order", "7", parent=self.parent_a, auth_key="viewer")
        evidence = self.store.record_failure(other_auth.key, 403, relation="edit-order")
        self.assertEqual(evidence.auth_key, "viewer")
        self.assertEqual(evidence.scope, "auth_context")
        self.assertNotEqual(other_auth.key, self.child_a.key)
        self.assertEqual(self.store.get(other_auth.key).lifecycle, Lifecycle.ACTIVE)
        self.assertEqual(self.store.get(self.child_a.key).revision, 0)

    def test_410_is_target_only(self):
        self.store.record_failure(self.child_a.key, 410, relation="get-order")
        self.assertEqual(self.store.get(self.child_a.key).lifecycle, Lifecycle.DELETED)
        self.assertEqual(self.store.get(self.parent_a.key).lifecycle, Lifecycle.ACTIVE)
        self.assertEqual(self.store.get(self.child_b.key).lifecycle, Lifecycle.ACTIVE)
        self.assertEqual(self.store.get(self.child_a.key).revision, 0)

    def test_full_parent_chain_and_parent_generation_are_identity(self):
        leaf = self.store.create("line", "1", parent=self.child_a)
        self.assertEqual(leaf.parent_chain, (self.parent_a.key, self.child_a.key))
        with self.assertRaises(ValueError):
            self.store.create("line", "bad", parent_chain=(self.child_a.key,))
        self.store.confirm_delete(self.child_a.key, "delete-parent")
        with self.assertRaises(LifecycleError):
            self.store.bind(leaf.key, ["id"])
        replacement = self.store.create("order", "7", parent=self.parent_a)
        new_leaf = self.store.create("line", "1", parent=replacement)
        self.assertNotEqual(new_leaf.key, leaf.key)

    def test_exact_type_names_are_not_collapsed(self):
        other_type = self.store.create("Order", "7", parent=self.parent_a)
        self.assertNotEqual(other_type.key, self.child_a.key)

    def test_joint_binding_never_mixes_instances_or_partial_observations(self):
        binding = self.store.bind(self.child_a.key, {"user": "owner", "product": "sku"})
        self.assertEqual(binding, {"user": "alice", "product": "x"})
        self.store.observe(self.child_a.key, joint_fields={"owner": "carol"})
        with self.assertRaises(BindingError):
            self.store.bind(self.child_a.key, {"user": "owner", "product": "sku"})
        self.assertEqual(self.store.bind(self.child_b.key, ["owner", "sku"]),
                         {"owner": "bob", "sku": "y"})
        with self.assertRaises(ValueError):
            self.store.observe(self.child_a.key, joint_fields={"id": "wrong"})

    def test_snapshot_is_immutable_and_old_revision_binding_rejected(self):
        details = {"details": {"tags": ["old"]}}
        child = self.store.create("item", "x", joint_fields=details)
        details["details"]["tags"].append("mutated")
        self.assertEqual(child.joint_fields["details"]["tags"], ("old",))
        with self.assertRaises(TypeError):
            child.joint_fields["new"] = 1
        self.store.complete_mutation(child.key, "event")
        self.assertEqual(child.revision, 0)
        with self.assertRaises(BindingError):
            self.store.bind(child.key, ["id"], expected_revision=0)


class ConstructivePrefixTests(unittest.TestCase):
    def recipe(self):
        return ConstructivePrefix([
            PrefixStep("C", {"name": "literal-create-only-name"}, ("id", "version")),
            PrefixStep("U", {"id": OutputRef("C", "id"),
                             "If-Match": OutputRef("C", "version")}, ("version",)),
            PrefixStep("T", {"id": OutputRef("C", "id"),
                             "If-Match": OutputRef("U", "version")}),
            PrefixStep("cleanup", {"id": OutputRef("C", "id")}),
        ], "T")

    def test_consuming_target_excluded_and_new_id_version_rebound(self):
        prefix = self.recipe()
        calls = []

        def execute(step, inputs):
            calls.append((step.step_id, inputs))
            if step.step_id == "C":
                return StepResult(201, {"id": "fresh-99", "version": "fresh-v1"})
            self.assertEqual(inputs, {"id": "fresh-99", "If-Match": "fresh-v1"})
            return StepResult(200, {"version": "fresh-v2"})

        result = prefix.replay(execute)
        self.assertTrue(result.completed)
        self.assertEqual(result.physical_requests, 2)
        self.assertEqual([name for name, _ in calls], ["C", "U"])
        self.assertEqual(result.target_inputs, {"id": "fresh-99", "If-Match": "fresh-v2"})
        self.assertEqual([step.step_id for step in prefix.steps], ["C", "U"])

    def test_each_replay_uses_its_own_outputs(self):
        prefix = self.recipe()
        for identifier in ("first", "second"):
            def execute(step, inputs):
                if step.step_id == "C":
                    return StepResult(201, {"id": identifier, "version": "v1"})
                self.assertEqual(inputs["id"], identifier)
                return StepResult(204, {"version": "v2"})
            self.assertEqual(prefix.replay(execute).target_inputs["id"], identifier)

    def test_over_cap_rejected_instead_of_truncated(self):
        steps = [PrefixStep(str(index)) for index in range(6)]
        with self.assertRaisesRegex(ValueError, "four"):
            ConstructivePrefix(steps, "5")
        prefix = ConstructivePrefix(steps, "4")
        self.assertEqual(len(prefix.steps), 4)
        result = prefix.replay(lambda step, inputs: StepResult(204))
        self.assertTrue(result.completed)
        self.assertEqual(result.physical_requests, 4)

    def test_failed_step_and_callback_exception_both_cost_one_request(self):
        prefix = self.recipe()
        failed = prefix.replay(lambda step, inputs: StepResult(500))
        self.assertFalse(failed.completed)
        self.assertEqual(failed.physical_requests, 1)
        self.assertEqual(failed.failed_step, "C")
        def throw(step, inputs):
            raise OSError("connection failed after dispatch")
        raised = prefix.replay(throw)
        self.assertEqual(raised.physical_requests, 1)
        self.assertIn("OSError", raised.error)

    def test_failed_second_step_includes_both_costs(self):
        def execute(step, inputs):
            if step.step_id == "C":
                return StepResult(201, {"id": "new", "version": "v1"})
            return StepResult(412)
        result = self.recipe().replay(execute)
        self.assertFalse(result.completed)
        self.assertEqual(result.physical_requests, 2)
        self.assertEqual(result.failed_step, "U")

    def test_202_does_not_allow_later_consumer_dispatch(self):
        result = self.recipe().replay(lambda step, inputs: StepResult(202, {"id": "pending"}))
        self.assertFalse(result.completed)
        self.assertEqual(result.physical_requests, 1)
        self.assertIsNone(result.target_inputs)

    def test_budget_reservation_precedes_callback_and_no_cost_when_refused(self):
        charges, calls = [], []
        def reserve(step):
            charges.append(step.step_id)
            return len(charges) <= 1
        def execute(step, inputs):
            calls.append(step.step_id)
            return StepResult(201, {"id": "fresh", "version": "v1"})
        result = self.recipe().replay(execute, before_request=reserve)
        self.assertFalse(result.completed)
        self.assertEqual(result.physical_requests, 1)
        self.assertEqual(charges, ["C", "U"])
        self.assertEqual(calls, ["C"])

    def test_missing_outputs_do_not_fall_back_to_old_values(self):
        result = self.recipe().replay(lambda step, inputs: StepResult(201, {"id": "new"}))
        self.assertFalse(result.completed)
        self.assertEqual(result.physical_requests, 1)
        self.assertIsNone(result.target_inputs)
        prefix = ConstructivePrefix([PrefixStep("C"),
            PrefixStep("T", {"id": OutputRef("C", "id")})], "T")
        missing = prefix.replay(lambda step, inputs: StepResult(201))
        self.assertFalse(missing.completed)
        self.assertEqual(missing.physical_requests, 1)
        self.assertEqual(missing.failed_step, "T")

    def test_forward_reference_duplicate_ids_and_unknown_target_rejected(self):
        with self.assertRaises(BindingError):
            ConstructivePrefix([PrefixStep("C", {"id": OutputRef("T", "id")}),
                                PrefixStep("T")], "T")
        with self.assertRaises(ValueError):
            ConstructivePrefix([PrefixStep("C"), PrefixStep("C")], "C")
        with self.assertRaises(ValueError):
            ConstructivePrefix([PrefixStep("C")], "unknown")

    def test_invalid_recipe_container_types_rejected(self):
        with self.assertRaises(TypeError):
            PrefixStep("C", ["wrong"])
        with self.assertRaises(TypeError):
            PrefixStep("C", expected_outputs="id")

    def test_zero_step_prefix_does_not_execute_target(self):
        prefix = ConstructivePrefix([PrefixStep("T", {"id": "existing"})], "T")
        def never(step, inputs):
            self.fail("target must never be dispatched by its constructive prefix")
        result = prefix.replay(never)
        self.assertTrue(result.completed)
        self.assertEqual(result.physical_requests, 0)
        self.assertEqual(result.target_inputs, {"id": "existing"})


if __name__ == "__main__":
    unittest.main()
