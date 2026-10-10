"""Portable, single-run feedback scheduling and physical-dispatch budgeting.

This module does not perform HTTP, infer resource ancestry, or repair resources.
Resource keys are opaque, hashable tuples supplied by the resource ledger. They
must already contain the *entire* parent chain, generation, and auth scope.

Integration contract:
* Register actual target failures, then report each ordinary completed mutation
  exactly once. A 202 response is pending, never a completed mutation.
* Acknowledge each baseline turn with ``baseline_completed(turn_id)``. At most
  one feedback job can be obtained before another baseline acknowledgement.
* Execute a feedback job through one ``Budget.plan_feedback`` reservation and
  call ``Budget.send`` immediately around every physical transport invocation,
  including baseline, prefix, repair, cleanup, and automatic transport retries.
  Disable transport-internal retries unless each retry uses the same budget.
* Always ``mark_result`` and close the plan, even after dispatch failure. A
  success cannot seed anything implicitly; use ``observe_result`` with the
  actual origin and, optionally, one variant target.

Scheduler operations are serialized by the caller. Budget accounting is locked.
All deduplication ledgers live for one run. Capacity exhaustion refuses new work
instead of evicting evidence and accidentally admitting duplicate attempts.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import threading
import time
from typing import Callable, Any


ORIGINS = frozenset({"ordinary", "revisit", "variant", "prefix", "repair", "cleanup"})
MAX_PREFIX = 4
MAX_REPAIRS = 1
_PLAN_FACTORY_TOKEN = object()


class AdmissionRefused(RuntimeError):
    """The scheduler's bounded run ledger or queue cannot accept more work."""


class BudgetRefused(RuntimeError):
    """A full recipe or physical dispatch would violate a bound or deadline."""


def _integer(value: int, name: str, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _resource(value: tuple) -> None:
    if not isinstance(value, tuple):
        raise TypeError("resource_key must be an opaque hashable tuple")
    hash(value)


def _nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")


def _origin(value: str) -> None:
    if value not in ORIGINS:
        raise ValueError(f"unknown origin: {value!r}")


@dataclass
class _Failure:
    revision: int
    kind: str


class Scheduler:
    """Deterministic event-triggered round robin over exact resource identities.

    Every globally unique event ID admits at most one job. Replaying an identical
    event is a no-op; reusing its ID with a different meaning is an error. Events
    that arrive before a failure are not replayed retroactively. A reservation
    for (target, exact resource, revision) is never released within the run.

    Revisit jobs take priority over an optional variant from the same completed
    response. Queue capacity refusal consumes the event but does not reserve an
    attempt. The caller should record the refusal and continue baseline work.
    """

    def __init__(self, *, max_pending: int = 128, max_failures: int = 1024,
                 max_events: int = 8192, max_reservations: int = 8192):
        for name, value in (("max_pending", max_pending), ("max_failures", max_failures),
                            ("max_events", max_events), ("max_reservations", max_reservations)):
            _integer(value, name, 1)
        self._limits = (max_pending, max_failures, max_events, max_reservations)
        self._failures: dict[tuple[str, tuple], _Failure] = {}
        self._rings: dict[tuple[tuple, str], deque[str]] = {}
        self._events: dict[str, tuple] = {}
        self._reserved: set[tuple[str, tuple, int]] = set()
        self._active: set[tuple[str, tuple]] = set()
        self._queue: deque[dict] = deque()
        self._inflight: dict | None = None
        self._last_baseline = -1
        self._credit = False
        self._sequence = 0

    @property
    def pending_count(self) -> int:
        """Queued jobs, excluding the at-most-one job already handed out."""
        return len(self._queue)

    def register_failure(self, target_key: str, resource_key: tuple, revision: int,
                         failure_kind: str = "state") -> None:
        """Remember a failure without creating a retry; stale reports are ignored.

        ``auth``/``403`` only wake through ``notify_auth_change``;
        ``version``/``412`` only wake through ``notify_version_change``.
        Ordinary mutations only wake ``state`` failures at a strictly newer
        revision. New auth identities or resource generations require new exact
        keys; the scheduler never migrates records across such keys.
        """
        _nonempty(target_key, "target_key")
        _resource(resource_key)
        _integer(revision, "revision")
        kind = {"403": "auth", "412": "version"}.get(failure_kind, failure_kind)
        if kind not in {"state", "auth", "version"}:
            raise ValueError("failure_kind must be state, auth/403, or version/412")
        key = (target_key, resource_key)
        old = self._failures.get(key)
        if old is not None and revision < old.revision:
            return
        if old is None and len(self._failures) >= self._limits[1]:
            raise AdmissionRefused("failure ledger is full")
        if old is not None and old.kind != kind:
            self._remove_from_ring(target_key, resource_key, old.kind)
        if old is None or old.kind != kind:
            self._rings.setdefault((resource_key, kind), deque()).append(target_key)
        self._failures[key] = _Failure(revision, kind)

    def baseline_completed(self, turn_id: int) -> None:
        """Grant one feedback slot after an actual completed baseline turn.

        Monotonically increasing IDs cannot accumulate credits. Duplicate/stale
        acknowledgements do nothing, including after the credit was consumed.
        A pending baseline response may still finish a baseline *turn*; it must
        never be reported as a completed mutation merely to unlock state retry.
        """
        _integer(turn_id, "turn_id")
        if turn_id > self._last_baseline:
            self._last_baseline = turn_id
            self._credit = True

    def completed_mutation(self, resource_key: tuple, revision: int, event_id: str,
                           *, origin: str = "ordinary", status_code: int = 200) -> None:
        """Report a completed ordinary mutation, enqueueing at most one revisit.

        The three-argument form asserts a completed ordinary mutation. Adapters
        with HTTP results should pass the real status and origin, or use
        ``observe_result``. Pending 202, failed responses, and all other origins
        are ignored and do not consume the eventual completion event ID.
        """
        self._validate_event(resource_key, revision, event_id)
        _origin(origin)
        _integer(status_code, "status_code", 100)
        if origin != "ordinary" or not self._completed(status_code):
            return
        self._event(resource_key, revision, event_id, "state", origin)

    def notify_auth_change(self, resource_key: tuple, revision: int, event_id: str) -> None:
        """Explicitly report a verified auth change in this exact identity scope.

        ``revision`` must be at least the recorded auth failure revision. This
        is an explicit authorization-state notification, never inferred from a
        resource mutation or a different principal's successful request.
        Equal revision is allowed because an auth refresh need not mutate the
        resource. The per-target/resource/revision reservation still applies.
        """
        self._event(resource_key, revision, event_id, "auth", "auth_notification")

    def notify_version_change(self, resource_key: tuple, revision: int, event_id: str) -> None:
        """Explicitly report refreshed version/precondition evidence after a 412.

        A regular successful mutation does not imply refreshed conditional
        headers; the adapter must call this API only after it has that evidence.
        Equal revision is allowed for a header refresh without state mutation.
        """
        self._event(resource_key, revision, event_id, "version", "version_notification")

    def observe_result(self, target_key: str, resource_key: tuple, revision: int,
                       event_id: str, status_code: int, *, origin: str,
                       mutated: bool = False, variant_target_key: str | None = None) -> None:
        """Admit at most one feedback job from an actual successful response.

        Ordinary completed mutations may wake one state failure. Otherwise a
        successful ordinary/revisit result may seed the explicitly supplied one
        variant. Prefix, repair, cleanup, and variant origins cannot seed jobs or
        mutation wakeups. ``mark_result`` itself never seeds anything. A 202 can
        be reported later with the same event ID once completion is verified.
        """
        _nonempty(target_key, "target_key")
        self._validate_event(resource_key, revision, event_id)
        _origin(origin)
        _integer(status_code, "status_code", 100)
        if not isinstance(mutated, bool):
            raise TypeError("mutated must be bool")
        if variant_target_key is not None:
            _nonempty(variant_target_key, "variant_target_key")
        if origin not in {"ordinary", "revisit"} or not self._completed(status_code):
            return
        category = "state" if origin == "ordinary" and mutated else None
        self._event(resource_key, revision, event_id, category, origin, variant_target_key)

    def next_feedback(self) -> dict | None:
        """Take one FIFO feedback job, only once between baseline turns.

        Registration-order round robin selects targets at event admission; FIFO
        preserves notification ordering across independent resources. There is
        never more than one inflight feedback job. Returned dictionaries must
        be passed unchanged to ``mark_result``.
        """
        if not self._credit or self._inflight is not None:
            return None
        while self._queue:
            job = self._queue.popleft()
            key = (job["target_key"], job["resource_key"])
            failure = self._failures.get(key)
            if job["kind"] == "revisit":
                valid = (failure is not None and failure.kind == job["failure_kind"]
                         and failure.revision <= job["revision"])
                if valid and failure.kind == "state":
                    valid = failure.revision < job["revision"]
            else:
                valid = failure is None
            if not valid:
                # A newer/reclassified failure must not be bypassed by an old
                # queued job. Preserve its attempt reservation, not its credit.
                self._active.remove(key)
                continue
            self._credit = False
            self._inflight = job
            return job.copy()
        return None

    def mark_result(self, job: dict, success: bool) -> None:
        """Finish the current job without creating any further feedback.

        The per-revision reservation remains even after failure. A failed
        revisit advances its failure revision; another *new* matching event is
        required. A stale success cannot erase a newer recorded failure.
        """
        if not isinstance(success, bool):
            raise TypeError("success must be bool")
        if self._inflight is None or job != self._inflight:
            raise ValueError("job is not the unmodified current inflight job")
        key = (job["target_key"], job["resource_key"])
        self._active.remove(key)
        old = self._failures.get(key)
        if job["kind"] == "revisit" and old is not None and old.kind == job["failure_kind"]:
            if old.revision <= job["revision"]:
                if success:
                    del self._failures[key]
                    self._remove_from_ring(key[0], key[1], old.kind)
                else:
                    old.revision = job["revision"]
        self._inflight = None

    @staticmethod
    def _completed(status_code: int) -> bool:
        return 200 <= status_code < 300 and status_code != 202

    @staticmethod
    def _validate_event(resource_key: tuple, revision: int, event_id: str) -> None:
        _resource(resource_key)
        _integer(revision, "revision")
        _nonempty(event_id, "event_id")

    def _remove_from_ring(self, target: str, resource: tuple, kind: str) -> None:
        ring_key = (resource, kind)
        self._rings[ring_key].remove(target)
        if not self._rings[ring_key]:
            del self._rings[ring_key]

    def _event(self, resource: tuple, revision: int, event_id: str, kind: str | None,
               origin: str, variant: str | None = None) -> None:
        self._validate_event(resource, revision, event_id)
        # Variant is deliberately not part of event identity: a replay cannot
        # change a candidate and obtain a second job from the same response.
        signature = (resource, revision, kind, origin)
        if event_id in self._events:
            if self._events[event_id] != signature:
                raise ValueError("event_id reused for a different event")
            return
        if len(self._events) >= self._limits[2]:
            raise AdmissionRefused("event ledger is full")
        self._events[event_id] = signature
        ring = self._rings.get((resource, kind))
        if ring:
            for _ in range(len(ring)):
                target = ring.popleft()
                ring.append(target)
                failure = self._failures[(target, resource)]
                newer = failure.revision < revision if kind == "state" else failure.revision <= revision
                if (newer and (target, resource) not in self._active
                        and (target, resource, revision) not in self._reserved):
                    self._enqueue(target, resource, revision, event_id, "revisit", kind)
                    return
        if (variant is not None and (variant, resource) not in self._failures
                and (variant, resource) not in self._active
                and (variant, resource, revision) not in self._reserved):
            self._enqueue(variant, resource, revision, event_id, "variant", None)

    def _enqueue(self, target: str, resource: tuple, revision: int, event_id: str,
                 kind: str, failure_kind: str | None) -> None:
        if len(self._queue) >= self._limits[0]:
            raise AdmissionRefused("feedback queue is full")
        if len(self._reserved) >= self._limits[3]:
            raise AdmissionRefused("attempt reservation ledger is full")
        self._sequence += 1
        job = {"job_id": self._sequence, "kind": kind, "origin": kind,
               "target_key": target, "resource_key": resource, "revision": revision,
               "failure_kind": failure_kind, "event_id": event_id,
               "prefix_limit": MAX_PREFIX, "repair_limit": MAX_REPAIRS}
        self._reserved.add((target, resource, revision))
        self._active.add((target, resource))
        self._queue.append(job)


class Budget:
    """One physical-request cap and absolute monotonic deadline for the whole run.

    ``spent`` counts every admitted physical dispatch, including unsuccessful
    transport calls. Failed HTTP responses and connection errors never refund it.
    A plan reserves its entire recipe before the first prefix; unused slots are
    released on close, but physical dispatches are never released. ``remaining``
    excludes reserved slots. Deadline expiry refuses even an already reserved
    dispatch. No wall-clock time or per-job request cap can extend this budget.
    """

    def __init__(self, request_cap: int, deadline: float, *, clock: Callable[[], float] = time.monotonic):
        _integer(request_cap, "request_cap")
        if isinstance(deadline, bool) or not math.isfinite(deadline):
            raise ValueError("deadline must be a finite absolute monotonic time")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.request_cap = request_cap
        self.deadline = float(deadline)
        self._clock = clock
        self._spent = 0
        self._reserved = 0
        self._lock = threading.Lock()

    @property
    def spent(self) -> int:
        with self._lock:
            return self._spent

    @property
    def remaining(self) -> int:
        with self._lock:
            return self.request_cap - self._spent - self._reserved

    def _check_deadline(self) -> None:
        now = self._clock()
        if not math.isfinite(now) or now >= self.deadline:
            raise BudgetRefused("monotonic deadline reached")

    def plan_feedback(self, prefix_length: int, repairs: int = 1, *,
                      target_origin: str = "revisit") -> FeedbackPlan:
        """Reserve an entire prefix + one target + optional repair, or refuse.

        Prefixes longer than four are refused, never silently truncated. At most
        one repair is admitted. A repair is a physical request, not an exemption
        from the shared cap. The caller can reserve zero repairs if none is
        possible. Both target retry and variant jobs obey the same bounds.
        """
        _integer(prefix_length, "prefix_length")
        _integer(repairs, "repairs")
        if prefix_length > MAX_PREFIX or repairs > MAX_REPAIRS:
            raise BudgetRefused("feedback prefix/repair bounds exceeded")
        if target_origin not in {"revisit", "variant"}:
            raise ValueError("feedback target_origin must be revisit or variant")
        count = prefix_length + 1 + repairs
        with self._lock:
            self._check_deadline()
            if self._spent + self._reserved + count > self.request_cap:
                raise BudgetRefused("insufficient cap for the complete feedback recipe")
            self._reserved += count
            return FeedbackPlan(self, {"prefix": prefix_length, target_origin: 1, "repair": repairs},
                                _factory_token=_PLAN_FACTORY_TOKEN)

    def dispatch(self, origin: str, *, plan: FeedbackPlan | None = None) -> int:
        """Charge exactly one physical send immediately before transport I/O.

        Returns a one-based sequence number for logging. Baseline and cleanup
        sends are unreserved; bounded feedback sends require their plan. This
        accounting primitive does not itself contact any target.
        """
        _origin(origin)
        with self._lock:
            self._check_deadline()
            if plan is None:
                if origin in {"prefix", "repair", "revisit", "variant"}:
                    raise BudgetRefused("feedback dispatch requires a bounded plan")
                if self._spent + self._reserved >= self.request_cap:
                    raise BudgetRefused("physical request cap reached")
            else:
                if plan._budget is not self or plan._closed:
                    raise BudgetRefused("feedback plan is closed or belongs to another budget")
                if plan._allowances.get(origin, 0) <= 0:
                    raise BudgetRefused("feedback origin allowance exhausted")
                plan._allowances[origin] -= 1
                self._reserved -= 1
            self._spent += 1
            return self._spent

    def send(self, transport: Callable[..., Any], *args: Any, origin: str = "ordinary",
             plan: FeedbackPlan | None = None, **kwargs: Any) -> Any:
        """Charge then invoke one physical transport call; exceptions stay charged.

        ``transport`` must perform exactly one physical attempt. Streaming/polling,
        redirects, or retries are extra calls and must also pass through this
        wrapper. A timeout inside transport must respect the remaining deadline;
        this module prevents late starts but cannot interrupt arbitrary I/O.
        """
        if not callable(transport):
            raise TypeError("transport must be callable")
        self.dispatch(origin, plan=plan)
        return transport(*args, **kwargs)


class FeedbackPlan:
    """An opaque reservation returned by Budget.plan_feedback; close in finally.

    This is a bound on dispatched requests, not a resource recipe builder. It
    deliberately does not replay, truncate, or invent resource prefixes.
    """

    def __init__(self, budget: Budget, allowances: dict[str, int], *, _factory_token: object = None):
        if _factory_token is not _PLAN_FACTORY_TOKEN:
            raise BudgetRefused("create feedback plans with Budget.plan_feedback")
        self._budget = budget
        self._allowances = allowances
        self._closed = False

    def dispatch(self, origin: str) -> int:
        """Charge a single prefix, target, or repair against this reservation."""
        return self._budget.dispatch(origin, plan=self)

    def close(self) -> None:
        """Release only unused reservations; repeated closes are harmless."""
        with self._budget._lock:
            if not self._closed:
                self._budget._reserved -= sum(self._allowances.values())
                self._allowances.clear()
                self._closed = True

    def __enter__(self) -> FeedbackPlan:
        if self._closed:
            raise BudgetRefused("feedback plan is closed")
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
