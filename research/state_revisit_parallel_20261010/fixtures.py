"""Deterministic, in-process mechanism fixtures; these are not HTTP benchmarks.

The synthetic fault is deliberately specified here, outside the policies. Every
policy receives the same operation, parameter and entity components.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Request:
    operation: str
    parent: str = "A"
    resource_id: str = "7"
    limit: int = 1
    if_match: Optional[str] = None
    async_completion: bool = False
    role: str = "baseline"


@dataclass(frozen=True)
class Response:
    status: int
    body: Dict[str, Any]
    version: Optional[str] = None
    event_id: Optional[str] = None
    completed: bool = False
    generation: Optional[int] = None


@dataclass
class Instance:
    generation: int
    items: int = 0
    version: int = 1


@dataclass(frozen=True)
class LedgerEntry:
    number: int
    request: Request
    response: Response

    def as_dict(self):
        return {
            "number": self.number,
            "operation": self.request.operation,
            "role": self.request.role,
            "parent": self.request.parent,
            "resource_id": self.request.resource_id,
            "limit": self.request.limit,
            "if_match": self.request.if_match,
            "async_completion": self.request.async_completion,
            "status": self.response.status,
            "body": self.response.body,
            "version": self.response.version,
            "generation": self.response.generation,
            "event_id": self.response.event_id,
            "completed": self.response.completed,
        }


class InProcessAPI:
    """C creates empty; T(1) observes; U adds; D deletes.

    Empty always returns 409 first; ready returns 422 when items exceed limit,
    otherwise 200. Limit zero is schema-valid. In the explicitly different
    consuming fixture, a successful T(1) removes the one item. ID 7
    is intentionally reusable and is scoped by parent and creation generation.
    A timestamp-like field changes on every read without changing server state.
    """

    def __init__(self, *, consuming_target=False):
        self.consuming_target = consuming_target
        self.instances: Dict[Tuple[str, str], Instance] = {}
        self.generations: Dict[Tuple[str, str], int] = {}
        self.pending: Dict[str, Tuple[str, Tuple[str, str], int]] = {}
        self.completed_events = set()
        self.ledger: List[LedgerEntry] = []
        self.event_sequence = 0

    def _event(self):
        self.event_sequence += 1
        return "event-%d" % self.event_sequence

    def request(self, request: Request) -> Response:
        key = (request.parent, request.resource_id)
        current = self.instances.get(key)
        now = len(self.ledger) + 1

        def result(status, body=None, *, event_id=None, completed=False):
            instance = self.instances.get(key)
            response = Response(
                status,
                dict(body or {}, observed_at=now),
                ('"v%d"' % instance.version) if instance else None,
                event_id,
                completed,
                instance.generation if instance else (current.generation if current else None),
            )
            self.ledger.append(LedgerEntry(now, request, response))
            return response

        if request.operation == "C":
            if current is not None:
                return result(409, {"error": "already exists"})
            generation = self.generations.get(key, -1) + 1
            self.generations[key] = generation
            self.instances[key] = Instance(generation)
            event = self._event()
            self.completed_events.add(event)
            return result(201, {"id": request.resource_id, "items": 0}, event_id=event, completed=True)
        if current is None:
            return result(404, {"error": "missing"})
        if request.if_match is not None and request.if_match != '"v%d"' % current.version:
            return result(412, {"error": "validator mismatch"})
        if request.operation == "G":
            return result(200, {"id": request.resource_id, "items": current.items})
        if request.operation == "T":
            if current.items == 0:
                return result(409, {"error": "empty"})
            if self.synthetic_fault and request.limit == 2:
                return result(500, {"error": "synthetic local-variant fault"})
            if current.items > request.limit:
                return result(422, {"error": "items exceed limit"})
            if self.consuming_target:
                current.items -= 1
                current.version += 1
                event = self._event()
                self.completed_events.add(event)
                return result(200, {"items_returned": 1}, event_id=event, completed=True)
            return result(200, {"items_returned": min(request.limit, current.items)})
        if request.operation in ("U", "D"):
            event = self._event()
            if request.async_completion:
                self.pending[event] = (request.operation, key, current.generation)
                return result(202, {"state": "pending"}, event_id=event)
            if request.operation == "U":
                current.items += 1
                current.version += 1
            else:
                del self.instances[key]
            self.completed_events.add(event)
            return result(200 if request.operation == "U" else 204,
                          {"id": request.resource_id} if request.operation == "U" else {},
                          event_id=event, completed=True)
        raise ValueError("unknown fixture operation: %s" % request.operation)

    def complete_event(self, event_id: str) -> bool:
        """External completion notification, not a hidden physical API request.

        If production observes completion by polling, that poll must instead be
        dispatched and charged as an ordinary physical request.
        """
        pending = self.pending.pop(event_id, None)
        if pending is None:
            return False
        operation, key, generation = pending
        current = self.instances.get(key)
        if current is None or current.generation != generation:
            return False
        if operation == "U":
            current.items += 1
            current.version += 1
        else:
            del self.instances[key]
        self.completed_events.add(event_id)
        return True

    synthetic_fault = False


class SyntheticFaultAPI(InProcessAPI):
    """Separate deliberately planted T(2) fault, never an official result."""

    synthetic_fault = True


@dataclass(frozen=True)
class SharedParameters:
    """Common parameter generator, with no access to fixture outcomes."""

    initial_limit: int = 1
    minimum: int = 0
    maximum: int = 3
    offset: int = -1

    def local_variant(self, successful_limit: int) -> Optional[int]:
        candidate = successful_limit + self.offset
        return candidate if self.minimum <= candidate <= self.maximum else None


@dataclass(frozen=True)
class SharedEntities:
    parent: str = "A"
    resource_id: str = "7"

    def request(self, operation, *, parameters=None, limit=None, role="baseline", **kwargs):
        parameters = parameters or SharedParameters()
        return Request(operation, self.parent, self.resource_id,
                       parameters.initial_limit if limit is None else limit,
                       role=role, **kwargs)


BASELINE_OPERATIONS = ("C", "T", "U", "D")


def execute_policy_trace(*, consuming=False, fixture_name="state_validation"):
    """A hand-specified six/eight-request illustration, not integration output."""
    api = (SyntheticFaultAPI if fixture_name == "synthetic_fault" else InProcessAPI)(consuming_target=consuming)
    components = SharedEntities()
    variant_limit = 2 if fixture_name == "synthetic_fault" else 0
    plan = [("C", 1), ("T", 1), ("U", 1), ("T", 1)]
    if consuming:
        plan += [("C", 1), ("U", 1)]
    plan += [("T", variant_limit), ("D", 1)]
    creation_count = 0
    for operation, limit in plan:
        if operation == "C":
            components = SharedEntities(resource_id=str(7 + creation_count))
            creation_count += 1
        api.request(components.request(operation, limit=limit, role="hand_policy_trace"))
    return api.ledger


def run_policy(mode, request_cap, *, consuming=False, fixture_name="state_validation"):
    """Exercise the real scheduler against common components and transport.

    Baseline C,T,U,D order is identical in A/B/C. Every baseline completion is
    followed by at most one feedback job, never by draining the feedback queue.
    Revisit success can enqueue a variant, but cannot run it in the same slot.
    """
    from resources import ConstructivePrefix, OutputRef, PrefixStep, ResourceStore, StepResult
    from scheduler import Budget, BudgetRefused, Scheduler

    if mode not in ("A", "B", "C"):
        raise ValueError("mode must be A, B, or C")
    if fixture_name not in ("state_validation", "synthetic_fault"):
        raise ValueError("unknown fixture")
    api = (SyntheticFaultAPI if fixture_name == "synthetic_fault" else InProcessAPI)(consuming_target=consuming)
    entities = SharedEntities()
    parameters = SharedParameters(minimum=1, offset=1) if fixture_name == "synthetic_fault" else SharedParameters()
    resources = ResourceStore()
    parent = resources.create("account", entities.parent)
    scheduler = Scheduler() if mode != "A" else None
    budget = Budget(request_cap, deadline=1.0, clock=lambda: 0.0)
    current = None
    usable = False
    baseline_turn = 0
    creation_count = 0
    rejected_feedback = []

    def dispatch(operation, limit=1, role="baseline", plan=None, *, bound_inputs=None, charged=False):
        nonlocal current, usable, entities, creation_count
        origin = "ordinary" if role == "baseline" else role
        if not charged:
            if plan is None:
                budget.dispatch(origin)
            else:
                plan.dispatch(origin)
        if operation == "C":
            entities = SharedEntities(resource_id=str(7 + creation_count))
            creation_count += 1
        if bound_inputs is None:
            request = entities.request(operation, parameters=parameters, limit=limit, role=role)
        else:
            request = Request(operation, entities.parent, bound_inputs["id"], limit,
                              if_match=bound_inputs.get("version"), role=role)
        response = api.request(request)
        if operation == "C" and response.status == 201:
            current = resources.create("order", entities.resource_id, parent=parent.key,
                                       server_version=response.version,
                                       joint_fields={"items": 0})
            usable = True
        elif operation == "U" and response.completed:
            current = resources.complete_mutation(current.key, response.event_id,
                                                  status_code=response.status,
                                                  server_version=response.version,
                                                  joint_fields={"items": 1})
        elif operation == "D" and response.completed:
            current = resources.confirm_delete(current.key, response.event_id,
                                               status_code=response.status)
            usable = False
        elif operation == "T" and consuming and response.completed:
            current = resources.complete_mutation(current.key, response.event_id,
                                                  status_code=response.status,
                                                  server_version=response.version,
                                                  joint_fields={"items": 0})

        if scheduler is not None and current is not None:
            target = "%s:%d" % (operation, limit)
            if operation == "T" and response.status == 409 and origin in ("ordinary", "revisit"):
                scheduler.register_failure(target, current.key, current.revision)
            variant = None
            if operation == "T" and mode == "C":
                next_limit = parameters.local_variant(limit)
                variant = "%s:%d" % (operation, next_limit) if next_limit is not None else None
            scheduler.observe_result(target, current.key, current.revision,
                                     response.event_id or "request-%d" % len(api.ledger),
                                     response.status, origin=origin,
                                     mutated=response.completed and operation in ("U", "D", "T"),
                                     variant_target_key=variant)
        if operation == "T" and response.status == 200 and consuming:
            usable = False
        return response

    while len(api.ledger) < request_cap:
        operation = BASELINE_OPERATIONS[baseline_turn % len(BASELINE_OPERATIONS)]
        try:
            dispatch(operation)
        except BudgetRefused:
            break
        baseline_turn += 1
        if scheduler is None:
            continue
        scheduler.baseline_completed(baseline_turn)
        job = scheduler.next_feedback()
        if job is None:
            continue
        target_operation, target_limit_text = job["target_key"].split(":")
        target_limit = int(target_limit_text)
        replay = job["kind"] == "variant" and (
            not usable or current.key != job["resource_key"])
        prefix_length = 2 if replay else 0
        if job["kind"] == "revisit" and current.key != job["resource_key"]:
            rejected_feedback.append({"kind": job["kind"], "reason": "stale generation"})
            scheduler.mark_result(job, False)
            continue
        try:
            plan = budget.plan_feedback(prefix_length, repairs=0, target_origin=job["kind"])
        except BudgetRefused as error:
            rejected_feedback.append({"kind": job["kind"], "reason": str(error)})
            scheduler.mark_result(job, False)
            continue
        try:
            target_inputs = None
            if replay:
                recipe = ConstructivePrefix([
                    PrefixStep("C", {}, ("id", "version")),
                    PrefixStep("U", {"id": OutputRef("C", "id"),
                                     "version": OutputRef("C", "version")}, ("id", "version")),
                    PrefixStep("T", {"id": OutputRef("C", "id"),
                                     "version": OutputRef("U", "version")}),
                ], "T")

                def replay_transport(step, inputs):
                    response = dispatch(step.step_id, role="prefix", plan=plan,
                                        bound_inputs=inputs if inputs else None, charged=True)
                    outputs = {"id": response.body.get("id"), "version": response.version}
                    return StepResult(response.status, outputs)

                replay_result = recipe.replay(replay_transport,
                                              before_request=lambda step: bool(plan.dispatch("prefix")))
                if not replay_result.completed:
                    rejected_feedback.append({"kind": job["kind"], "reason": replay_result.error})
                    scheduler.mark_result(job, False)
                    continue
                target_inputs = replay_result.target_inputs
            response = dispatch(target_operation, target_limit, role=job["kind"], plan=plan,
                                bound_inputs=target_inputs)
            scheduler.mark_result(job, 200 <= response.status < 300)
        finally:
            plan.close()
    return {
        "mode": mode,
        "fixture": fixture_name,
        "request_cap": request_cap,
        "consuming": consuming,
        "physical_requests": len(api.ledger),
        "budget_spent": budget.spent,
        "baseline_turns": baseline_turn,
        "synthetic_fault_seen": any(entry.response.status == 500 for entry in api.ledger),
        "state_validation_seen": any(entry.response.status == 422 for entry in api.ledger),
        "rejected_feedback": rejected_feedback,
        "trace": [entry.as_dict() for entry in api.ledger],
    }
