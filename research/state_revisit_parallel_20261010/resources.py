"""Scoped resource snapshots and bounded, callback-only constructive replay.

No networking or scheduling is performed here. Callers serialize ResourceStore
updates and pass ``resource.key`` and ``resource.revision`` to their scheduler.
A callback supplied to ConstructivePrefix represents exactly one physical
request; callbacks must not hide redirects, retries, or other request dispatches.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence


_UNSET = object()
_RESERVED_FIELDS = frozenset({"id", "version", "server_version"})


def _freeze(value: Any) -> Any:
    """Copy JSON-like data into immutable containers, preserving scalars."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool, bytes)):
        return value
    raise TypeError("resource data must contain only immutable scalars or containers")


def _copy_out(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _copy_out(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_copy_out(item) for item in value)
    if isinstance(value, frozenset):
        return frozenset(_copy_out(item) for item in value)
    return value


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _fields(values: Mapping[str, Any] | None) -> Mapping[str, Any]:
    values = {} if values is None else values
    if not isinstance(values, Mapping):
        raise TypeError("joint_fields must be a mapping")
    if _RESERVED_FIELDS.intersection(values):
        raise ValueError("joint_fields cannot override id or server-version fields")
    if any(not isinstance(key, str) for key in values):
        raise TypeError("joint field names must be strings")
    return _freeze(values)


class Lifecycle(str, Enum):
    ACTIVE = "active"
    DELETE_PENDING = "delete_pending"
    DELETED = "deleted"


class ResourceKey(NamedTuple):
    """Opaque, hashable instance identity; never match by ID string alone.

    ``parent_chain`` is ordered root-to-immediate-parent and contains complete
    ancestor keys, including their generations. ``auth_key`` is an opaque
    authorization-context label, never an access token or other credential.
    IDs and type names use exact nonempty strings; no normalization is applied.
    """

    resource_type: str
    parent_chain: tuple["ResourceKey", ...]
    resource_id: str
    generation: int
    auth_key: str


@dataclass(frozen=True)
class Resource:
    """Immutable snapshot. Read the store again after a mutation or observation.

    ``causal_revision`` counts distinct completed mutation events for this exact
    key. It is independent of ``server_version`` and does not assert that an
    observable value changed. Joint fields come from one observed entity and
    are replaced as a unit; partial observations never merge cross-field values.
    """

    key: ResourceKey
    joint_fields: Mapping[str, Any]
    causal_revision: int = 0
    server_version: Any = None
    lifecycle: Lifecycle = Lifecycle.ACTIVE
    needs_version_refresh: bool = False
    pending_event_ids: frozenset[str] = field(default_factory=frozenset)

    @property
    def revision(self) -> int:
        return self.causal_revision

    @property
    def resource_type(self) -> str:
        return self.key.resource_type

    @property
    def resource_id(self) -> str:
        return self.key.resource_id

    @property
    def parent_chain(self) -> tuple[ResourceKey, ...]:
        return self.key.parent_chain

    @property
    def generation(self) -> int:
        return self.key.generation

    @property
    def auth_key(self) -> str:
        return self.key.auth_key


@dataclass(frozen=True)
class FailureEvidence:
    """Historical evidence, scoped to a concrete key and authorization context."""

    resource_key: ResourceKey
    status_code: int
    scope: str
    relation: str | None = None

    @property
    def auth_key(self) -> str:
        return self.resource_key.auth_key


class LifecycleError(ValueError):
    """An operation would reuse a tombstone or bind an unavailable instance."""


class BindingError(ValueError):
    """A joint binding cannot be satisfied by one exact resource snapshot."""


class ResourceStore:
    """In-memory per-instance state; methods are synchronous, not thread-safe.

    Only create() may assign a generation. Re-observing an active or pending
    instance retains its generation; create() after confirmed deletion (including
    exact-target 410 evidence) assigns the next generation. A 404 never creates
    a tombstone. Ancestors, siblings, other auth contexts, and old generations
    are never implicitly invalidated. Callers must report cascading deletion
    evidence for each affected key separately.
    """

    def __init__(self) -> None:
        self._records: dict[ResourceKey, Resource] = {}
        self._current: dict[tuple[Any, ...], ResourceKey] = {}
        self._completed_events: dict[ResourceKey, set[str]] = {}
        self._failures: list[FailureEvidence] = []

    def get(self, key: ResourceKey) -> Resource:
        """Return the exact generation's current immutable snapshot."""
        return self._records[key]

    @property
    def failures(self) -> tuple[FailureEvidence, ...]:
        return tuple(self._failures)

    @property
    def relation_negatives(self) -> frozenset[tuple[ResourceKey, str]]:
        """Historical 404 evidence; this is not an instance-liveness blacklist."""
        return frozenset((item.resource_key, item.relation) for item in self._failures
                         if item.status_code == 404 and item.relation is not None)

    def create(
        self, resource_type: str, resource_id: str, *,
        parent: Resource | ResourceKey | None = None,
        parent_chain: Sequence[ResourceKey] = (), auth_key: str = "default",
        joint_fields: Mapping[str, Any] | None = None, server_version: Any = _UNSET,
    ) -> Resource:
        """Record an observed successful creation, never a merely proposed ID.

        Supply either parent or a complete ordered parent_chain. All supplied
        ancestors must exist in this store and be active. IDs are exact strings.
        Existing live/pending identities keep their generation. A deleted latest
        identity plus this confirmed creation produces a new generation starting
        at revision zero; old keys remain tombstones for in-flight evidence.
        """
        _identifier(resource_type, "resource_type")
        _identifier(resource_id, "resource_id")
        _identifier(auth_key, "auth_key")
        chain = tuple(parent_chain)
        if parent is not None:
            if chain:
                raise ValueError("supply parent or parent_chain, not both")
            parent_key = parent.key if isinstance(parent, Resource) else parent
            if not isinstance(parent_key, ResourceKey):
                raise TypeError("parent must be a Resource or ResourceKey")
            chain = parent_key.parent_chain + (parent_key,)
        for index, ancestor in enumerate(chain):
            if not isinstance(ancestor, ResourceKey) or ancestor.parent_chain != chain[:index]:
                raise ValueError("parent_chain must contain the complete ordered ancestry")
            if self.get(ancestor).lifecycle != Lifecycle.ACTIVE:
                raise LifecycleError("cannot create under an unavailable ancestor")
        base = (resource_type, chain, resource_id, auth_key)
        previous_key = self._current.get(base)
        if previous_key is not None:
            previous = self.get(previous_key)
            if previous.lifecycle != Lifecycle.DELETED:
                return self.observe(previous_key, joint_fields=joint_fields,
                                    server_version=server_version)
            generation = previous_key.generation + 1
        else:
            generation = 0
        key = ResourceKey(resource_type, chain, resource_id, generation, auth_key)
        record = Resource(key, _fields(joint_fields), server_version=(
            None if server_version is _UNSET else _freeze(server_version)))
        self._records[key] = record
        self._current[base] = key
        self._completed_events[key] = set()
        return record

    def observe(self, key: ResourceKey, *, joint_fields: Mapping[str, Any] | None = None,
                server_version: Any = _UNSET) -> Resource:
        """Record successful read evidence without advancing causal revision.

        joint_fields replaces the entire observed tuple; None preserves it.
        A supplied non-None server version clears this key's 412 refresh requirement.
        Neither operation revives a deleted generation or completes a pending
        mutation. Use create() for confirmed recreation and complete_mutation()
        or confirm_delete() for explicit completion evidence.
        """
        record = self.get(key)
        if record.lifecycle == Lifecycle.DELETED:
            raise LifecycleError("deleted generations cannot be revived; record a creation")
        changes: dict[str, Any] = {}
        if joint_fields is not None:
            changes["joint_fields"] = _fields(joint_fields)
        if server_version is not _UNSET:
            changes["server_version"] = _freeze(server_version)
            changes["needs_version_refresh"] = server_version is None
        updated = replace(record, **changes)
        self._records[key] = updated
        return updated

    def begin_delete(self, key: ResourceKey, *, event_id: str | None = None) -> Resource:
        """Record accepted/pending deletion, retaining identity and revision.

        Acceptance (for example HTTP 202) is not deletion completion. A repeated
        begin on a tombstone is harmless. Bind refuses pending-deletion objects.
        """
        record = self.get(key)
        if event_id is not None:
            _identifier(event_id, "event_id")
        if record.lifecycle == Lifecycle.DELETED:
            return record
        pending = record.pending_event_ids
        if event_id is not None and event_id not in self._completed_events[key]:
            pending = pending | {event_id}
        updated = replace(record, lifecycle=Lifecycle.DELETE_PENDING,
                          pending_event_ids=pending)
        self._records[key] = updated
        return updated

    def confirm_delete(self, key: ResourceKey, event_id: str, *, status_code: int = 204) -> Resource:
        """Complete one exact deletion event; a 202 only marks it pending.

        Only completed 2xx evidence is accepted here. A lookup's 404 must instead
        go through record_failure(), where it cannot delete anything. Repeated
        completion for the same (key, event_id) is idempotent and cannot affect a
        replacement generation, parent, or sibling.
        """
        return self._complete(key, event_id, status_code=status_code, deleted=True)

    def complete_mutation(
        self, key: ResourceKey, event_id: str, *, status_code: int = 200,
        server_version: Any = _UNSET, joint_fields: Mapping[str, Any] | None = None,
    ) -> Resource:
        """Advance one causal revision for a distinct completed mutation event.

        A 202 records pending_event_ids without changing representation, version,
        revision, generation, or availability. Reusing its event_id on completion
        advances exactly once. Duplicate completions ignore stale payloads.
        This method never auto-clears historical 403/404 evidence. Use
        confirm_delete(), rather than this method, for deletion completion.
        """
        return self._complete(key, event_id, status_code=status_code, deleted=False,
                              server_version=server_version, joint_fields=joint_fields)

    def _complete(self, key: ResourceKey, event_id: str, *, status_code: int,
                  deleted: bool, server_version: Any = _UNSET,
                  joint_fields: Mapping[str, Any] | None = None) -> Resource:
        _identifier(event_id, "event_id")
        if not isinstance(status_code, int) or not 200 <= status_code < 300:
            raise ValueError("completion requires a successful 2xx status; use record_failure")
        record = self.get(key)
        if event_id in self._completed_events[key]:
            return record
        if record.lifecycle == Lifecycle.DELETED:
            raise LifecycleError("cannot apply a new mutation to a deleted generation")
        if status_code == 202:
            if deleted:
                return self.begin_delete(key, event_id=event_id)
            updated = replace(record, pending_event_ids=record.pending_event_ids | {event_id})
        else:
            changes: dict[str, Any] = {
                "causal_revision": record.causal_revision + 1,
                "pending_event_ids": record.pending_event_ids - {event_id},
            }
            if deleted:
                changes["lifecycle"] = Lifecycle.DELETED
                changes["pending_event_ids"] = frozenset()
            if joint_fields is not None:
                changes["joint_fields"] = _fields(joint_fields)
            if server_version is not _UNSET:
                changes["server_version"] = _freeze(server_version)
                changes["needs_version_refresh"] = server_version is None
            updated = replace(record, **changes)
            self._completed_events[key].add(event_id)
        self._records[key] = updated
        return updated

    def record_failure(self, key: ResourceKey, status_code: int, *,
                       relation: str | None = None) -> FailureEvidence:
        """Record failure at its narrowest supported scope.

        404 requires an exact lookup/relation label and changes no lifecycle.
        403 stores authorization-context evidence without deleting an instance.
        410 tombstones only the exact target, never ancestors or equal-ID peers.
        412 marks only this instance's validator stale and leaves revision alone.
        Other 4xx/5xx statuses are request evidence only. Evidence is historical;
        the scheduler must separately decide whether a later event repairs it.
        """
        record = self.get(key)
        if not isinstance(status_code, int) or not 400 <= status_code < 600:
            raise ValueError("failure status must be 4xx or 5xx")
        if relation is not None:
            _identifier(relation, "relation")
        if status_code == 404 and relation is None:
            raise ValueError("404 evidence requires the exact failed relation")
        scope = {403: "auth_context", 404: "relation", 410: "target", 412: "version"}.get(
            status_code, "request")
        evidence = FailureEvidence(key, status_code, scope, relation)
        if status_code == 410:
            self._records[key] = replace(record, lifecycle=Lifecycle.DELETED,
                                         pending_event_ids=frozenset())
        elif status_code == 412:
            self._records[key] = replace(record, needs_version_refresh=True)
        self._failures.append(evidence)
        return evidence

    def bind(self, key: ResourceKey, fields: Mapping[str, str] | Iterable[str] | None = None,
             *, expected_revision: int | None = None) -> dict[str, Any]:
        """Resolve all requested consumer fields from one exact live snapshot.

        Mapping fields is consumer_name -> source_name; an iterable keeps names.
        Sources are id, version/server_version, and this entity's joint_fields.
        No fallback pool or cross-instance/cross-observation field merge occurs.
        Missing fields or stale requested versions fail the entire binding.
        """
        record = self.get(key)
        if record.lifecycle != Lifecycle.ACTIVE:
            raise LifecycleError("cannot bind a deleted or pending-deletion instance")
        if any(self.get(parent).lifecycle != Lifecycle.ACTIVE for parent in key.parent_chain):
            raise LifecycleError("cannot bind through an unavailable ancestor")
        if expected_revision is not None and record.revision != expected_revision:
            raise BindingError("resource revision changed before binding")
        values = {"id": record.resource_id, "version": record.server_version,
                  "server_version": record.server_version, **record.joint_fields}
        if fields is None:
            names = {name: name for name in values}
        elif isinstance(fields, Mapping):
            names = dict(fields)
        else:
            if isinstance(fields, str):
                raise TypeError("fields must be a mapping or iterable of field names, not a string")
            names = {name: name for name in fields}
        if any(not isinstance(name, str) or not isinstance(source, str)
               for name, source in names.items()):
            raise TypeError("binding names must be strings")
        missing = set(names.values()) - values.keys()
        if missing:
            raise BindingError(f"joint entity lacks required fields: {sorted(missing)}")
        requests_version = {"version", "server_version"}.intersection(names.values())
        if requests_version and (record.needs_version_refresh or record.server_version is None):
            raise BindingError("this instance needs a server-version refresh")
        return {name: _copy_out(values[source]) for name, source in names.items()}


@dataclass(frozen=True)
class OutputRef:
    """A value produced by a particular earlier physical request in this replay."""

    producer_step: str
    field: str


@dataclass(frozen=True)
class PrefixStep:
    """One physical request recipe; inputs may contain nested OutputRef values."""

    step_id: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    expected_outputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.step_id, "step_id")
        if not isinstance(self.inputs, Mapping):
            raise TypeError("step inputs must be a mapping")
        if isinstance(self.expected_outputs, str):
            raise TypeError("expected_outputs must be a sequence of field names")
        object.__setattr__(self, "inputs", _freeze_recipe(self.inputs))
        object.__setattr__(self, "expected_outputs", tuple(self.expected_outputs))
        for name in self.expected_outputs:
            _identifier(name, "output name")


def _freeze_recipe(value: Any) -> Any:
    if isinstance(value, OutputRef):
        _identifier(value.producer_step, "producer_step")
        _identifier(value.field, "field")
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_recipe(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_recipe(item) for item in value)
    return _freeze(value)


def _references(value: Any) -> Iterable[OutputRef]:
    if isinstance(value, OutputRef):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _references(item)
    elif isinstance(value, tuple):
        for item in value:
            yield from _references(item)


def _resolve(value: Any, outputs: Mapping[str, Mapping[str, Any]]) -> Any:
    if isinstance(value, OutputRef):
        try:
            return _copy_out(outputs[value.producer_step][value.field])
        except KeyError as error:
            raise BindingError(f"missing replay output {value.producer_step}.{value.field}") from error
    if isinstance(value, Mapping):
        return {key: _resolve(item, outputs) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_resolve(item, outputs) for item in value)
    return _copy_out(value)


@dataclass(frozen=True)
class StepResult:
    """Observed result of one dispatch; outputs must be actual returned values."""

    status_code: int
    outputs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status_code, int) or not 100 <= self.status_code < 600:
            raise ValueError("status_code must be a valid HTTP status integer")
        if not isinstance(self.outputs, Mapping):
            raise TypeError("outputs must be a mapping")
        object.__setattr__(self, "outputs", _freeze(self.outputs))

    @property
    def successful(self) -> bool:
        return 200 <= self.status_code < 300 and self.status_code != 202


@dataclass(frozen=True)
class PrefixReplayResult:
    """Replay accounting, including failed dispatches and freshly bound target inputs.

    completed means the pre-target state was constructed. The target itself was
    never dispatched. A blocked budget or unresolved input incurs no request;
    a callback exception or failed response incurs one request. No retry occurs.
    """

    completed: bool
    physical_requests: int
    outputs_by_step: Mapping[str, Mapping[str, Any]]
    target_inputs: Mapping[str, Any] | None = None
    failed_step: str | None = None
    error: str | None = None


class ConstructivePrefix:
    """Retain the recipe strictly before one target, capped at four requests.

    The constructor takes an ordered trace including target_step_id. The target
    and all later steps are excluded from replay. More than four preceding
    physical steps is rejected, never truncated. Later consumers use explicit
    OutputRef edges to freshly observed outputs; no old ID/version is guessed
    from equal strings. Literal inputs remain literal. Creation-only unique
    values, if needed, should be generated explicitly by the caller and must
    never overwrite bound reference values.
    """

    HARD_MAX_PHYSICAL_REQUESTS = 4

    def __init__(self, steps: Sequence[PrefixStep], target_step_id: str) -> None:
        steps = tuple(steps)
        ids = [step.step_id for step in steps]
        if len(set(ids)) != len(ids):
            raise ValueError("step IDs must be unique within a trace")
        if target_step_id not in ids:
            raise ValueError("target must be present in the recorded trace")
        target_index = ids.index(target_step_id)
        if target_index > self.HARD_MAX_PHYSICAL_REQUESTS:
            raise ValueError("constructive prefix exceeds four physical requests")
        self.steps = steps[:target_index]
        self.target_step = steps[target_index]
        prior: set[str] = set()
        for step in self.steps + (self.target_step,):
            for reference in _references(step.inputs):
                if reference.producer_step not in prior:
                    raise BindingError("output references must name an earlier prefix producer")
            prior.add(step.step_id)

    def replay(self, callback: Callable[[PrefixStep, Mapping[str, Any]], StepResult], *,
               before_request: Callable[[PrefixStep], bool] | None = None) -> PrefixReplayResult:
        """Construct pre-target state, charging each callback invocation once.

        before_request(step), when supplied, must reserve one shared-budget
        request and return true, or return false without a dispatch. It is called
        after resolving inputs and immediately before counting/invoking callback.
        Failed responses, exceptions, missing expected outputs and HTTP 202 all
        stop replay. Retry, repair, cleanup and the target need separate budget
        reservations by the caller. No actual HTTP is implemented in this module.
        """
        outputs: dict[str, Mapping[str, Any]] = {}
        requests = 0

        def result(completed: bool, *, target_inputs: Mapping[str, Any] | None = None,
                   failed_step: str | None = None, error: str | None = None) -> PrefixReplayResult:
            return PrefixReplayResult(completed, requests, _freeze(outputs),
                                      None if target_inputs is None else _freeze(target_inputs),
                                      failed_step, error)

        for step in self.steps:
            try:
                resolved = _resolve(step.inputs, outputs)
            except BindingError as error:
                return result(False, failed_step=step.step_id, error=str(error))
            if before_request is not None and not before_request(step):
                return result(False, failed_step=step.step_id, error="request budget exhausted")
            requests += 1
            try:
                observed = callback(step, resolved)
                if not isinstance(observed, StepResult):
                    raise TypeError("callback must return StepResult")
            except Exception as error:
                return result(False, failed_step=step.step_id,
                              error=f"callback failed: {type(error).__name__}: {error}")
            if not observed.successful:
                return result(False, failed_step=step.step_id,
                              error=f"request did not complete successfully: {observed.status_code}")
            if set(step.expected_outputs) - observed.outputs.keys():
                return result(False, failed_step=step.step_id, error="required output missing")
            outputs[step.step_id] = observed.outputs
        try:
            target_inputs = _resolve(self.target_step.inputs, outputs)
        except BindingError as error:
            return result(False, failed_step=self.target_step.step_id, error=str(error))
        return result(True, target_inputs=target_inputs)
