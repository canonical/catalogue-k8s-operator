# ADR-0001: Adopting fetch-compute-apply

**Status:** Accepted
**Date:** 2026-11-21
**Related:** [ADR-0002: Toolchain and repo architecture](0002-toolchain-and-repo-architecture.md), [Workload constraints reference](../workload-constraints.md)

---

## 1. Context

The baseline charm (upstream `canonical/catalogue-k8s-operator` as of
2026-10) funneled seven per-event handlers into `_configure(items,
push_certs)`, which decided and acted in the same breath:

- `_update_web_server_config()` / `_update_catalogue_config()` /
  `_update_pebble_layer()` pushed files *and returned* changed-flags,
  which `_configure` threaded into a restart decision.
- The `push_certs` boolean gated whether the function mutated at all —
  whether a given path wrote anything depended on an argument at a call
  site, invisible to the type checker.
- Error handling was incoherent by accident: pull failures were
  swallowed into `""`/`{}` (deciding from ghost diffs), push failures
  escaped into unit error state, and cert/restart failures mapped to
  Blocked.
- Statuses were set from inside the reconcile, so a status handler
  re-deriving anything raced the reconcile's own failure paths.

The test suite was mock-heavy as a direct result: every decision was
entangled with the effects it chose, so testing a decision meant
patching the container and asserting call order. Two bugs shipped
through that green suite during this refactor's own staging: an empty
Pebble layer ([workload-constraints §7](../workload-constraints.md))
and a `certificate_available` funnel leg that never fired while its
`ActiveStatus` assertion passed — the collector reports Active for any
connectable container (§8). Both were caught only by artifact-based
tests that the decide/act split made writable.

## 2. Problem breakdown

Three distinct facets, each constraining the answer:

1. **Decide-and-act is untestable.** A method that performs an effect
   and returns a flag describing what it decided cannot be tested
   without performing the effect. Every mock in the baseline suite
   was a symptom of this, not a choice.
2. **Pebble's signals lie.** `can_connect()` races the next call,
   `restart()` returns before health, and the container filesystem is
   ephemeral ([workload-constraints
   §1-§4](../workload-constraints.md)). Decisions that trust a
   success signal instead of reading real state back converge on
   fiction.
3. **Kubernetes status semantics are a push/pull split.** A failed
   push with a healthy service cannot be re-derived by a status
   handler — the reconcile must carry it, or the unit reports Active
   while the operator's config was never applied.

## 3. Approaches

### A. Keep the baseline shape (per-event handlers, mutator flags)

Hardening the existing structure: more precise mocks, retry logic in
the mutators, stricter status branches.

**Pros**
- Smallest diff against upstream; no rebase drift in `charm.py`.
- No new names to learn; upstream familiarity transfers.

**Cons**
- The defect class survives: decide-and-act stays untestable, and the
  two shipped bugs above remain representable. Mock seams keep lying
  (a mocked `add_layer` cannot catch a wrong-shaped layer).
- Every new decision adds another flag or another call-order
  assumption.

### B. Result-type monadic pipeline (the `fp-edge-canonical` route)

`fetch().flat_map(compute).flat_map(apply).on_error(handler)` with a
hand-rolled `Result[E, A]`, as in the workshop material's repo.

**Pros**
- Errors flow through the pipeline; no exceptions for control flow.

**Cons**
- `flat_map` short-circuits: a cert-push failure would abort before the
  nginx status is reported, and this reconciler must be safe to run
  twice *and* safe to never run — partial application plus full status
  is the requirement, `collect_*_status` is the right mechanism for
  that, not a monad chain.
- The reference repo is educational material (its own README: the code
  *"cannot be used in any way as an actual charmed operator"*), its
  `Result` is ~325 lines with pyright suppressions, and its test suite
  covers the `Result` type itself, not any charm logic.

### C. Fetch-compute-apply with an outcome ADT (chosen)

One impure read path (`fetch`) producing a frozen snapshot of real
state; pure functions (`intent_from`, `compute`) turning config +
snapshot into a union of named outcomes; one imperative applier
consuming outcomes one by one; statuses derived by a collector that
runs at the end of every hook.

**Pros**
- Decisions become data: `compute(state, intent) -> tuple[Outcome,
  ...]` is testable by construction and `==` — no mocks, no container,
  no `ops.testing`. The pure core sits at 100% coverage with honest
  pragmas, and the outcome union answers "under what conditions does
  nginx restart?" as a type instead of a comment.
- The push/pull status split closes naturally: the reconcile stores
  its failure on the instance (one hook, one process — §8 guarantees
  the collector sees it), the collector re-derives everything else
  from a read-only fetch.
- Every effect can be verified by reading state back, because
  verification is an apply-time concern, not a decision-time one.

**Cons**
- More types and names to invent and maintain (see §5.1).
- The applier's exhaustiveness (`assert_never`) means a new outcome
  variant fails `tox -e static` until handled — friction that is also
  the guarantee.

## 4. Recommendation

**C.** The deciding factor: the two bugs that shipped through the
baseline's green gates ([workload-constraints
§7](../workload-constraints.md)) are not representable in C — an
artifact-based test suite catches both without a single container
patch, and the pure core makes the decision table enumerable.
Approach A is recorded here so "harden the mocks" is not rediscovered
as a shortcut; approach B so the workshop material is not mistaken for
a Canonical direction.

## 5. Design

As landed 2026-11-21 (stages 1-2 of `docs/roadmap.md`):

### 5.1 Module layering

| Module | May import | Never imports |
|---|---|---|
| `src/catalogue_state.py` | stdlib, `nginx_config` | `ops`, `workload` |
| `src/workload.py` | stdlib, `nginx_config`; `ops` only under `TYPE_CHECKING` | `charm`, `catalogue_state` |
| `src/charm.py` | everything | — |

`catalogue_state.Facts` is the protocol the pure core sees instead of
the workload; `Workload` implements it structurally. Everything
downstream of `fetch` is data. The cost: the state union, the outcome
union, and the status union are three vocabularies to keep aligned —
`assert_never` in both `compute` and the applier makes drift fail
static analysis rather than silently skipping a case.

### 5.2 Status policy

Two decisions recorded here because they were genuinely open:

**Container-not-ready reports `WaitingStatus("Waiting for Pebble
ready")` — upstream's exact message, not `MaintenanceStatus`.** The
strict docstring reading (the sidecar is *this unit's own* workload,
"waiting for something under its control" → Maintenance) loses to
three factors: upstream parity (the baseline message is exactly this
string), and — asserted by convention, NOT VERIFIED — the message
being the de-facto idiom across Kubernetes charms; the unit is not
*busy* — it is idle until `pebble_ready` fires, which is what Waiting
reads as; and both clear on their own, so the operator-facing
difference is cosmetic. Both readings clear without human action, so
neither violates the Blocked criterion. Rejected-and-recorded:
switching to `MaintenanceStatus` is a deliberate divergence decision,
not a cleanup.

**Any workload failure maps to `BlockedStatus` naming a remedy — no
in-hook retry.** The remedy string names the action (`juju exec --unit
{unit} -- pebble services`) because a Blocked message exists to direct
a human. In-hook retry (a bounded loop on `ConnectionError`) is
rejected-and-recorded: a hook runs once and exits, so Juju's own
`update_status` cadence (observed — stage 2) *is* the retry, at
worst five minutes of a truthful Blocked; a retry loop buys back only
the rarest window (Pebble recovering mid-hook within seconds) at the
price of burning hook time and masking a sustained outage as a slow
hook. Transient vs sustained is not distinguishable from inside one
hook, so we do not pretend it is.

The collector itself never raises: a Pebble drop between reconcile and
collect ([workload-constraints §1](../workload-constraints.md)) maps
to the same Waiting message, not error state.

### 5.3 Certificates are compared state

The `push_certs` boolean is gone. TLS material is a fact in
`CatalogueIntent`; certs-on-disk is a fact in `CatalogueState`;
`PushCerts`/`RemoveCerts` are outcomes computed from the pair. The
reconcile re-pushes whenever the state does not show them — which is
exactly what ephemeral container storage
([§3](../workload-constraints.md)) demands, and what the old
`pebble_ready`/`upgrade_charm` call sites were hand-simulating.

## 6. Future Work (Out of Scope)

- `set_ports(80)` never opens 443 under TLS, so a TLS-serving unit
  publishes `is_port_open=False` to ingress. Trigger: ingress/TLS
  integration work.
- Adopt `self.load_config()` / `event.load_params()` (ops pydantic
  integration) for the `links` JSON string and `get-url`; replaces
  hand `json.loads` at the boundary. Trigger: next config-option or
  action change.
- The `collect_unit_status` handler fetches the full container state
  on every hook; cheapen if hook runtime ever matters.

## 7. Consequences

### Positive

- Decisions are tested as data: `catalogue_state.py` needs no mocks,
  no `ops.testing`; `src/` sits at 100% coverage where every excluded
  line declares its exclusion at the exclusion site.
- The funnel is mutation-tested: deleting any `observe` line fails an
  artifact-based assertion (stage 3).
- One reconciler, idempotent by construction — every step safe twice
  and safe never, with `update_status`/`leader_elected` observed as
  the self-heal cadence and new-leader republish.

### Negative

- Three type vocabularies (state, outcome, status) where the baseline
  had booleans — more names to invent, more `match` arms to maintain.
- Per-event INFO logs died with the handlers ("This app's ingress
  URL: %s" ...); the funnel tests assert artifacts instead.
- The unit-test layers can no longer exercise a *converged* reconcile
  end-to-end ([workload-constraints §9](../workload-constraints.md)) —
  that state lives in the pure core only.
- Divergence from upstream `charm.py` is now structural (the module
  split, annotations, Scenario tests); every upstream rebase touches
  moved lines. This is the standing cost of the fork's direction.