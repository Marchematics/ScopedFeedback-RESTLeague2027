"""Atomic2: one bounded revisit/variant job between baseline turns.

This is a separate mechanism experiment, not a replacement for the frozen v1
policy. Its fixed maximum is SIX physical requests: one revisit, at most four
constructive-prefix requests, and at most one deterministic local variant. It
performs no HTTP itself and never reads server internals.

Algorithm (fixed before comparative tests):
1. Take one baseline credit and reserve the complete job from the shared v1
   Budget before sending anything. Refuse an unaffordable/overlong recipe.
2. Execute exactly one revisit on the supplied observed live, ready binding.
3. Only after completed success, execute at most one local variant. A declared
   read-only contract and unchanged observed exact identity/revision/version
   permit reuse. An unexpected change aborts the variant. Consuming or unknown
   contracts instead require a fully charged, predeclared constructive prefix.
4. Return to baseline. Prefix and variant outcomes never seed further work.

"Atomic" means uninterrupted by this client's baseline scheduler, not a server
transaction or isolation from concurrent clients. Conditional validators are
sent on reused bindings; races can still produce 412. There are no hidden reads,
free restores, inferred nonconsumption, or uncharged automatic retries.

Bindings come only from observed responses and declared API contracts. The
opaque scope_key contains the parent chain/generations/auth context; resource_key
also includes the concrete resource generation. A constructive prefix may create
a fresh resource in the same scope, but may never cross scopes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib.util
from pathlib import Path
import sys
from typing import Any, Callable


# Load, but never edit, the frozen physical-dispatch Budget implementation.
_FROZEN_MODULE = "_scoped_feedback_frozen_v1_scheduler"
if _FROZEN_MODULE not in sys.modules:
    _path = Path(__file__).resolve().parent.parent / "rest_state_revisit_parallel" / "scheduler.py"
    _spec = importlib.util.spec_from_file_location(_FROZEN_MODULE, _path)
    if _spec is None or _spec.loader is None:
        raise ImportError("the sibling frozen v1 scheduler.py is required")
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_FROZEN_MODULE] = _module
    _spec.loader.exec_module(_module)
Budget = sys.modules[_FROZEN_MODULE].Budget
BudgetRefused = sys.modules[_FROZEN_MODULE].BudgetRefused

MAX_JOB_REQUESTS = 6
MAX_REBUILD_REQUESTS = 4
MAX_LOCAL_VARIANTS = 1


@dataclass(frozen=True)
class LiveState:
    """Joint binding inferred from one response/contract, never a server peek."""
    resource_key: tuple
    scope_key: tuple
    revision: int
    version: str | None
    live: bool = True
    ready: bool = True

    def __post_init__(self):
        if not isinstance(self.resource_key, tuple) or not isinstance(self.scope_key, tuple):
            raise TypeError("resource and scope keys must be opaque tuples")
        hash(self.resource_key)
        hash(self.scope_key)
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a nonnegative integer")
        if self.version is not None and (not isinstance(self.version, str) or not self.version):
            raise ValueError("version must be a nonempty validator or None")
        if type(self.live) is not bool or type(self.ready) is not bool:
            raise TypeError("live and ready must be bool")


@dataclass(frozen=True)
class TargetContract:
    """A declared effect with provenance; absence of evidence means unknown.

    A successful response with unchanged-looking fields alone does not establish
    read-only behavior. Consumers must identify the API contract they relied on.
    """
    effect: str = "unknown"
    source: str = ""

    def __post_init__(self):
        if self.effect not in {"read_only", "consumes", "unknown"}:
            raise ValueError("effect must be read_only, consumes, or unknown")
        if self.effect != "unknown" and not self.source.strip():
            raise ValueError("a declared effect requires contract provenance")


@dataclass(frozen=True)
class PhysicalCall:
    """One adapter request; transport must honor binding and if_match exactly.

    Payload is opaque to this executor. A creation prefix can use binding=None;
    target/variant calls must use the exact binding provided to their factory.
    """
    payload: Any
    binding: LiveState | None
    if_match: str | None = None


@dataclass(frozen=True)
class Reply:
    status: int
    state: LiveState | None

    def __post_init__(self):
        if type(self.status) is not int or not 100 <= self.status <= 599:
            raise ValueError("status must be an HTTP status integer")
        if self.state is not None and not isinstance(self.state, LiveState):
            raise TypeError("state must be an observed LiveState or None")

    @property
    def completed_success(self) -> bool:
        return 200 <= self.status < 300 and self.status != 202


@dataclass(frozen=True)
class AtomicResult:
    admitted: bool
    reason: str
    physical_requests: int
    reserved_cost: int
    revisit_status: int | None = None
    variant_status: int | None = None
    reused_live_state: bool = False
    rebuilt: bool = False
    trace: tuple[tuple[str, int | None], ...] = field(default_factory=tuple)


Factory = Callable[[LiveState | None], PhysicalCall]


class AtomicFeedback:
    """One Atomic2 job per baseline turn, sharing the unchanged v1 Budget.

    The caller serializes this executor with baseline dispatches. Factories only
    build calls; all physical I/O goes through transport exactly once per call.
    No baseline credit accrues while a job is running, and duplicate baseline
    acknowledgements cannot grant another job. There is no retry on 403/412/202.
    """

    def __init__(self, budget: Budget, transport: Callable[[PhysicalCall], Reply]):
        self.budget = budget
        self.transport = transport
        self._baseline = -1
        self._credit = False
        self._running = False

    def baseline_completed(self, turn_id: int) -> None:
        if type(turn_id) is not int or turn_id < 0:
            raise ValueError("turn_id must be a nonnegative integer")
        if self._running:
            raise RuntimeError("baseline cannot interleave with an atomic feedback job")
        if turn_id > self._baseline:
            self._baseline = turn_id
            self._credit = True

    def run(self, state: LiveState, revisit: Factory, variant: Factory | None, *,
            contract: TargetContract, rebuild_prefix: tuple[Factory, ...] = ()) -> AtomicResult:
        """Run a bounded recipe; failures return evidence, never speculative work.

        Unknown/consuming targets require a nonempty rebuild prefix when a
        variant is requested. Read-only reuse requires a known validator before
        admission. If that evidence is absent, the same rebuild rule applies.
        A job without a proposed local variant may execute one revisit only.
        The prefix is supplied by the existing resource planner; this module
        never synthesizes generic mutations or searches for favorable recipes.
        """
        if self._running or not self._credit:
            return AtomicResult(False, "baseline_slot_required", 0, 0)
        self._credit = False
        if not isinstance(rebuild_prefix, tuple):
            raise TypeError("rebuild_prefix must be a fixed tuple of request factories")
        if not state.live or not state.ready:
            return AtomicResult(False, "revisit_binding_not_live_ready", 0, 0)
        reuse = variant is not None and contract.effect == "read_only" and state.version is not None
        needs_rebuild = variant is not None and not reuse
        prefix = rebuild_prefix if needs_rebuild else ()
        if needs_rebuild and not prefix:
            return AtomicResult(False, "rebuild_evidence_required", 0, 0)
        if len(prefix) > MAX_REBUILD_REQUESTS:
            return AtomicResult(False, "complete_prefix_too_long", 0, 0)
        cost = 1 + (1 + len(prefix) if variant is not None else 0)
        if cost > MAX_JOB_REQUESTS:
            return AtomicResult(False, "atomic_job_cap", 0, cost)
        primary_plan = None
        secondary_plan = None
        try:
            # Both reservations must succeed before any prefix/target request.
            primary_plan = self.budget.plan_feedback(0, 0, target_origin="revisit")
            if variant is not None:
                secondary_plan = self.budget.plan_feedback(len(prefix), 0, target_origin="variant")
        except BudgetRefused:
            if primary_plan is not None:
                primary_plan.close()
            return AtomicResult(False, "complete_recipe_budget_refused", 0, cost)

        self._running = True
        records: list[tuple[str, int | None]] = []
        primary_status = None
        variant_status = None
        reused = False
        rebuilt = False

        def result(reason: str) -> AtomicResult:
            return AtomicResult(True, reason, len(records), cost, primary_status,
                                variant_status, reused, rebuilt, tuple(records))

        def send(factory: Factory, binding: LiveState | None, origin: str, plan, *, target=False) -> Reply:
            call = factory(binding)
            if not isinstance(call, PhysicalCall):
                raise TypeError("request factory must return PhysicalCall")
            if call.binding is not None:
                if call.binding.scope_key != state.scope_key or call.binding != binding:
                    raise ValueError("request must preserve the observed binding and authorized scope")
                if call.if_match != call.binding.version:
                    raise ValueError("request must preserve the observed validator")
            if target and (call.binding != binding or call.if_match != binding.version):
                raise ValueError("target factory must preserve the exact binding and validator")

            def physical_transport():
                records.append((origin, None))
                response = self.transport(call)
                if not isinstance(response, Reply):
                    raise TypeError("transport must return Reply")
                records[-1] = (origin, response.status)
                return response

            return self.budget.send(physical_transport, origin=origin, plan=plan)

        try:
            primary = send(revisit, state, "revisit", primary_plan, target=True)
            primary_status = primary.status
            if not primary.completed_success:
                return result("revisit_not_completed_success")
            if variant is None:
                return result("revisit_only")
            if reuse:
                if primary.state != state:
                    return result("live_binding_changed_or_unproven")
                bound = primary.state
                reused = True
            else:
                bound = primary.state
                if bound is not None and bound.scope_key != state.scope_key:
                    return result("revisit_scope_changed")
                for step in prefix:
                    replay = send(step, bound, "prefix", secondary_plan)
                    if not replay.completed_success:
                        return result("rebuild_not_completed_success")
                    bound = replay.state
                    if bound is None or bound.scope_key != state.scope_key:
                        return result("rebuild_scope_unproven_or_changed")
                if (bound is None or not bound.live or not bound.ready or bound.version is None):
                    return result("rebuilt_binding_not_proven_ready")
                rebuilt = True
            varied = send(variant, bound, "variant", secondary_plan, target=True)
            variant_status = varied.status
            return result("variant_attempted")
        except BudgetRefused:
            return result("dispatch_budget_refused")
        except Exception as error:
            # A failing transport attempt remains charged; no implicit retries.
            return result("request_error:" + type(error).__name__)
        finally:
            primary_plan.close()
            if secondary_plan is not None:
                secondary_plan.close()
            self._running = False
