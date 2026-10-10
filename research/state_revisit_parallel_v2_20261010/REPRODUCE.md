# Reproduce the frozen Atomic2 candidate

Evidence scope: **synthetic, deterministic, in-process mechanism tests only**.
The report's `frozen_candidate_mechanism_only` status does not represent a real-API
benchmark, official evaluator result, fault-discovery improvement, or SOTA claim.
All four original Atomic2 snapshot files are preserved byte-for-byte.

## Dependencies and layout

Use Python 3.10 or newer and its standard library. No package installation,
network request, credentials, real HTTP service, or additional input is needed.
Run the following from the root of a checkout of this research branch containing
both snapshot directories. The v1 files are preserved from commit
`369b3ee094296bc6860b0c86b6a2fae0b0f8f1f2`.

The frozen v2 loader expects a sibling directory named
`rest_state_revisit_parallel`. Copy the snapshots into a fresh temporary directory
with the exact names below; the repository files remain unchanged.

```sh
workdir="$(mktemp -d)"
cp -R research/state_revisit_parallel_20261010 "$workdir/rest_state_revisit_parallel"
cp -R research/state_revisit_parallel_v2_20261010 "$workdir/rest_state_revisit_parallel_v2"
cd "$workdir/rest_state_revisit_parallel_v2"
python3 -m unittest -v tests_atomic_feedback
```

Expected result: 26 tests pass, with no failures or errors. To print the
deterministic equal-budget comparisons, run in the same directory:

```sh
python3 tests_atomic_feedback.py --compare
```

## Interpretation and retained negative control

- Nonconsuming, cap 6: Atomic2 reaches the fixture's 422 state-validation outcome;
  Slot1 does not
- Consuming, cap 8: both policies reach 422 with all replay requests charged
- Consuming, cap 6: neither reaches 422; Atomic2 loses Slot1's one successful
  revisit because its full replay-plus-variant recipe is unaffordable

A 422 is not a server fault. Atomicity here means no client baseline interleaving
during a feedback job, not a server transaction or isolation from other clients.
The loss case is part of the frozen result and must not be omitted.

`LiveState.ready` must come from public response evidence or a declared API
contract, never from hidden service state or a test's expected answer. This
fixture uses its declared contract that a successful U makes the resource ready;
the comparison only demonstrates scheduling effects when that condition holds.
If a real API lacks observable ready/version evidence or a declared read-only
contract, use the conservative unknown-effect path and its evidence requirements.
Do not treat this synthetic adapter as a general REST strategy.

The report records source SHA-256 digests, frozen v1 dependency digests, complete
comparison traces, the original test summary, and failure conditions. Timing in
a rerun will differ from the original recorded test invocation.
