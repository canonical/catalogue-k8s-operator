---
name: charm-k8s-workload
description: >-
  Use when writing or reviewing src/charm.py, src/workload.py, or any code
  that touches the Pebble container — layers, plans, services, file push and
  pull, health checks. Covers the ops.Container API, the reconciler skeleton,
  status semantics, and the two-layer design that makes the charm testable.
  Load before writing an event handler or any container interaction.
metadata:
  verified: "2026-11-20"
---

# Kubernetes charm workload management

## Pebble is the workload API

There is no snap, no systemd, no `charmlibs-*` here. The container is managed
exclusively through Pebble via `ops.model.Container`, and `lightkube` is only
for charms that genuinely need Kubernetes API objects — this one does not; it
is a pure sidecar, and adding a Kubernetes API dependency is a design decision
that needs an ADR.

The API surface, verified against `ops/model.py`:

| Method | Notes |
|---|---|
| `can_connect()` | point-in-time bool — see the lies catalog below |
| `add_layer(label, layer, *, combine=False)` | `combine` is keyword-only |
| `get_plan()` | returns the combined `pebble.Plan`, reflects `add_layer` immediately |
| `replan()` / `autostart()` | start enabled services; `replan` diffs the plan |
| `start(*names)` / `stop(*names)` / `restart(*names)` | raise `TypeError` on zero names |
| `get_services(*names)` | `Mapping[str, pebble.ServiceInfo]`; empty → all |
| `push(path, source, *, encoding='utf-8', make_dirs=False, ...)` | keyword-only after `source` |
| `pull(path, *, encoding='utf-8')` | use as a context manager; raises `pebble.PathError` |
| `remove_path(path, *, recursive=False)` | raises `PathError` on non-empty without `recursive` |
| `exists(path)` / `isdir(path)` / `make_dir(path, *, make_parents=False)` | |
| `exec(command, *, ...)` | escape hatch; prefer a layer or a file push |

`Layer`, `Plan` and `Service` all implement `__eq__` (via `to_dict()`), so
comparing the running plan against the desired layer with `==` is supported.
`ServiceInfo` carries `.current` (`active`/`inactive`/`error`) and `.startup`,
plus `is_running()`.

The error hierarchy, all under `ops.pebble.Error` (which subclasses
`Exception`): `ConnectionError` (socket unreachable), `ProtocolError`,
`PathError` (`.kind`, `.message`), `APIError` (`.code`, `.body`),
`ChangeError` (`.err` — a change completed with an error), `ExecError`,
`TimeoutError`. **`ops.pebble.LayerValidationError`, `ChangeInProgress` and
`pebble.FileNotFoundError` do not exist** — the builtin
`FileNotFoundError` is what a missing socket surfaces as, and `can_connect()`
already catches it.

## The two-layer design

This is the single most important structural decision, and it comes straight
from the official guidance
([manage the workload container](https://canonical.com/juju/docs/ops/latest/howto/manage-containers/manage-the-workload-container/)):

> Keep charming concerns (event handlers, status, config parsing) in
> `src/charm.py`, and put workload-specific logic in a separate module such as
> `src/myworkload.py`.

**`src/workload.py` rules:**

- Never imports `ops`.
- Takes plain arguments (paths, bodies, service names), returns plain values
  or raises module-specific exceptions.
- Owns every `Container` call: push, pull, remove_path, add_layer, autostart,
  replan, restart, get_plan, get_services.
- Knows nothing about relations, config options, or Juju statuses.

**`src/charm.py` rules:**

- Never calls `Container` methods or `ops.pebble` directly.
- Translates charm config and relation data into arguments for `workload.py`.
- Translates `workload.py` results into Juju statuses.

If you find yourself wanting to patch `Container` in a test of `charm.py`, the
split is wrong.

## The lies catalog — what Pebble signals do not mean

**`can_connect()` is a point-in-time check that races.** The ops docstring says
it *"does not guard against the Pebble API becoming unavailable, and should be
treated as a 'point in time' status only"*, and the how-to is blunter: it
*"should not be used to guard against later Pebble operations, because that
introduces a race condition where Pebble might be responsive when
`can_connect()` is called, but is not when the later operation is executed.
Instead, charms should always include try/except statements around Pebble
operations."* So: guard nothing with it; wrap everything in
`try`/`except ops.pebble.Error`.

**`restart()`/`replan()` returning does not mean the service is healthy.** The
method returning means Pebble accepted the change. Read
`get_services(name).is_running()` back before believing it.

**The container filesystem is ephemeral.** Anything pushed — certs especially —
is gone when the container restarts or the charm upgrades. The existing
`push_certs=True` on `pebble_ready` and `upgrade_charm` exists for exactly this
reason. The reconcile must treat pushed files as cache, not state: re-push
whenever the fetched state does not show them.

**A certificate "available" on the relation is not a file on disk.** The
`certificates` relation hands the charm PEM strings; until they are pushed and
read back, nginx has no TLS.

**An ingress URL in a databag is not a reachable route.** Traefik publishes the
URL when its side is configured; DNS and the route catch up later. Never assert
reachability from the relation data alone.

## Reconciler skeleton

The charm's config is declared in `charmcraft.yaml` (the option list lives
there) and consumed with `self.load_config(CatalogueConfig, errors="blocked")`
— pydantic, which is also what parses the `links` JSON string. The pure core
will live in `src/catalogue_state.py` (roadmap stage 1). The skeleton below is
pedagogical — `src/charm.py` is the authority for the real reconciler.

```python
import logging

import ops

import catalogue_state
import workload
from workload import Workload, WorkloadError

logger = logging.getLogger(__name__)


class CatalogueCharm(ops.CharmBase):
    """Charm the Catalogue web UI on Kubernetes."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self._workload = Workload(self.unit.get_container("catalogue"))
        self._reconcile_failure: ops.StatusBase | None = None

        # Status collection is registered first and only reports.
        framework.observe(self.on.collect_unit_status, self._on_collect_status)

        # Events with genuinely distinct semantics. These cannot be deferred,
        # which per the official guidance is exactly the test for "needs its
        # own handler".
        framework.observe(self.on.get_url_action, self._on_get_url)

        # Everything else converges through one reconciler — including
        # pebble_ready and the charm-library custom events (items_changed,
        # ingress ready/revoked, certificate_available): the libraries own the
        # relation protocol, the charm owns the convergence.
        for event in (
            self.on.catalogue_pebble_ready,
            self.on.config_changed,
            self.on.upgrade_charm,
            self.on.update_status,
            self.on.leader_elected,  # peers exist: a new leader must publish
            self._info.on.items_changed,
            self._ingress.on.ready,
            self._ingress.on.revoked,
            self._cert_requirer.on.certificate_available,
        ):
            framework.observe(event, self._reconcile)

    def _reconcile(self, _: ops.EventBase) -> None:
        """Converge the container toward the desired state.

        Every step must be safe to run twice and safe to never run.
        """
        config = self.load_config(CatalogueConfig, errors="blocked")
        try:
            intent = catalogue_state.intent_from(config, self._relation_facts())
            state = catalogue_state.fetch(self._workload)
            for outcome in catalogue_state.compute(state, intent):
                self._apply(outcome)
        except WorkloadError as err:
            self._reconcile_failure = ops.BlockedStatus(str(err))

    def _apply(self, outcome: catalogue_state.CatalogueOutcome) -> None:
        """Perform one decided outcome. Exhaustive match."""
        match outcome:
            case catalogue_state.PushNginxConfig(body=body):
                self._workload.push(NGINX_CONFIG_PATH, body)
            # … one arm per outcome variant. The union lives in
            # src/catalogue_state.py; pyright fails the build if an arm is
            # missing — read the file, do not copy the list from here.
            case catalogue_state.Noop():
                logger.debug("converged: nothing to do.")
            case _ as unreachable:
                assert_never(unreachable)

    def _on_collect_status(self, event: ops.CollectStatusEvent) -> None:
        """Report status. Must not mutate anything."""
        if self._reconcile_failure is not None:
            event.add_status(self._reconcile_failure)
        # ... pull statuses follow
```

`ops.main(CatalogueCharm)` is the current entrypoint. `from ops.main import
main` is deprecated since 2.16.0.

## Reconciler rules

1. **One `_reconcile`.** The only legitimate separate handlers are
   `collect_unit_status`, actions, `stop`, `remove`, and `upgrade_charm` when
   it needs migration logic distinct from convergence. The official test for
   "needs its own handler" is crisp: *"if an event cannot be deferred, it
   needs a dedicated handler."* Non-deferrable events are actions, `stop`,
   `remove`, `secret_rotate`, `secret_expired`, and the `collect_*_status`
   lifecycle events. `pebble_ready` is deferrable — but do not defer it; see
   rule 6.
2. **Idempotent steps.** `push` with identical content is a no-op in effect.
   `add_layer(..., combine=True)` merges rather than stacking duplicates.
   `set_ports` diffs against `opened_ports()`. The pure core's `Noop` outcome
   when state matches intent is the proof. Lean on that.
3. **Order matters where the workload demands it.** Certs must be on disk
   before the TLS nginx config that references them; the layer must exist
   before `autostart`. Encode those orderings inside `workload.py` (or as
   outcome ordering from the pure core), not in the event graph.
4. **`collect_unit_status` never mutates.** It reads cached or cheap state.
   If you need a container read to determine status, make it cheap and
   side-effect-free (`get_services`), and remember `can_connect()` may lie.
5. **Never report `ActiveStatus` from `can_connect()` or a service status
   alone.** The service being `active` means the process is up, not that nginx
   answers or that the pushed config is the one we intended.
6. **Do not use `defer()`.** It is not deprecated, but the official guidance
   is explicit that deferring while waiting for other parts of the
   configuration is an antipattern — it builds a queue of handlers that all
   redo the same expensive work. When the container is not connectable, set a
   status and return; `pebble_ready` will fire and reconcile.
7. **Do not use `ops.StoredState`.** It is not deprecated, but the official
   guidance is to avoid it: caching a value that already exists in Juju
   config, in the relation data, and in the container *"doubles the number of
   possible states from 8 to 16 without increasing the number of correct
   states."* Read the real state on every reconcile — which non-negotiable #6
   requires anyway.

## Status semantics — get this right, it is counterintuitive

Each settable status answers a different question. From the ops docstrings:

| Status | ops says | The question it answers | Settable |
|---|---|---|---|
| `ActiveStatus` | *"correctly offering all the services it has been asked to offer"* — and *"if the unit is operational but some feature is in a degraded state, set active with an appropriate message"* | is the workload doing its job right now? | yes |
| `MaintenanceStatus` | *"performing an operation such as `apt install`, or waiting for something under its control"* | is **this unit** busy, and will it clear on its own? | yes |
| `WaitingStatus` | *"waiting on a charm it's integrated with"* | am I blocked on **another application**? | yes |
| `BlockedStatus` | *"an administrator has to manually intervene to unblock the charm to let it proceed"* | can a human do something about it? | yes |
| `ErrorStatus` | *"the unit-agent has encountered an error"* — **read-only**, `add_status` raises `InvalidStatusError` | — | no |
| `UnknownStatus` | the state before the first `status-set` — **read-only** | — | no |

**The one genuinely contested call in this charm: container not yet
connectable.** Upstream uses `WaitingStatus("Waiting for Pebble ready")`, and
that is the widespread Kubernetes idiom. The strict docstring reading says the
sidecar is *this unit's own workload* — "waiting for something under its
control" — which leans `MaintenanceStatus`. Both clear on their own and
neither directs a human, so the important rules are: never `Blocked`, and pick
one consciously in the refactor (record it in ADR-0001) rather than inheriting
it by accident. Everything else in this table is not contested.

### `BlockedStatus` is the one that directs a human

The test is a question, not a severity: **can an administrator do something about
it?** If yes, Blocked. If it clears on its own, Maintenance. If it depends on
another app, Waiting. If the service works but something is degraded, Active with
a message.

For this charm:

| Condition | Status | Why |
|---|---|---|
| invalid config (`links` not parseable JSON) | **Blocked** | only a human can fix the config |
| a push or replan failing repeatedly with `ChangeError` | **Blocked** | the operator must look at the container; a retry will hit the same wall |
| pushing config, replanning services | **Maintenance** | this unit is busy |
| waiting for ingress or certificates to arrive | **Waiting** | blocked on another application |
| catalogue serving, some items unreachable | **Active with a message** | the workload *is* offering its service, just degraded |

Because a Blocked message exists to direct a human, it must name the action:

```python
# Bad: states the problem, not the remedy.
ops.BlockedStatus(str(err))

# Good.
ops.BlockedStatus(
    f"failed to push nginx config: {err}; check the container with "
    f"'juju exec --unit {self.unit.name} -- pebble services'"
)
```

### `raise` versus `BlockedStatus` — and the real cost of error state

This is contested territory. The reference discussion is
[*"It's probably ok for a unit to go into error state"*](https://discourse.charmhub.io/t/its-probably-ok-for-a-unit-to-go-into-error-state/13022),
which is worth reading in full because the ops docstrings do not capture the
trade-off. The consensus that emerges:

**Where the thread agrees with a plain "let it raise":**

- Ben Hoyt: *"I agree we should probably error more than we do."* An operation that
  should structurally succeed (a hook precondition) failing is a genuine error.
- The charm going into error is honest signalling: John Meinel frames it as
  *"something is fundamentally off, such that I cannot progress the model of
  the world"* — the charm is saying it can no longer do its job.

**But the costs are concrete and severe, not cosmetic:**

1. **`juju remove` does not work** on a unit in error without `--force`, and
   `--force` skips cleanup. Meinel: *"the charm has abdicated responsibility by
   going into Error. Which means we have no idea whether things will cleanup
   correctly."*
2. **Model migration is blocked, full stop**, and so are some Juju upgrades. Paul
   Goins: *"if there are 'expected' cases where something is going to go into an
   error state, Juju migrations and at least some Juju upgrades are blocked, full
   stop, until those issues are resolved."*
3. **Error detaches the charm from managing the workload.** It stops reacting to
   config and relation changes entirely.

**And the retry you were counting on is not guaranteed.** `automatically-retry-hooks`
is *model* config. It defaults to `true`, but Tom Haddon notes it is *"not set that
way everywhere"*, and Meinel confirms OpenStack CI runs with it **disabled** and
treats reaching Error as a rejection. So "Juju will retry" is not a property you
can design on.

**The trap that decides the config case.** Error preserves the *original* hook
context. Leon: *"correcting a config option or relation data would not help
resolving the charm, because juju will continue to retry the hook with the old
context."* The operator then has to run `juju resolve --no-retry <unit>` and only
*after* making the correction. So raising on bad config produces a unit that retries
forever against the value the operator already fixed. Meinel is blunt: *"Things that
need normal human intervention (bad config, missing relation) should certainly not
be errors, and that is what Blocked is for."*

**The middle ground is retry inside the hook, not raise.** Both Hoyt and Meinel land
here: *"If the API is flakey, do say 3 retries of that operation in a simple loop
(or use a retry decorator)"*, and *"having a small number of retries before waking a
human would be good. If you really couldn't reach pebble after 10 retries you really
should be failing hard."*

### The decision table for this charm

| Situation | Do this | Why |
|---|---|---|
| invalid config | `BlockedStatus` | retry cannot help, and error would retry the *stale* config forever |
| Pebble unreachable mid-reconcile | retry briefly in-hook, then `BlockedStatus` | transient by nature; persistent means the container is sick |
| push/replan fails with `ChangeError` | `BlockedStatus` via the reconciler | a retry will hit the same wall; the operator must look |
| waiting for ingress/certificates | `WaitingStatus` | another application's move |
| a bug in our own code | let it raise | it *is* an error; the traceback is the point |

### The sharper Blocked criterion

Dylan Stephano-Shachter, in the same thread, on why people get this wrong:

> People see "manual intervention" and think *the charm can't recover
> automatically, so a human needs to debug the issue, thus blocked state.* My
> understanding is that it is not actually for the situation above, but for a
> situation where **the charm can tell the human what needs to be done.**

So the test is not just "can a human act?" but "**can we name the action?**" If the
charm cannot say what to do, Blocked is the wrong status — that is an error or a
`MaintenanceStatus`, depending on whether it is our bug or transient.

### The push/pull status problem — a real hole to close

Leon's framing, which the thread converges on:

- **Pull statuses** can be queried at any time. `collect_unit_status` was designed
  for exactly these.
- **Push statuses** are only knowable by *attempting* a mutating operation. They
  cannot be re-derived in the status handler.

Tony Meyer names the resulting race precisely: a check *looks* pullable, *"but that
introduces a race where your main handler failed and your collect status handler
succeeded."*

**This charm has that race.** Consider: `_reconcile` pushes the catalogue
config, the read-back verification fails, and it raises `WorkloadError`.
Meanwhile `_on_collect_status` independently reads `get_services()` — which
may well report `active`, because nginx is fine and only the config push
silently failed. **The unit reports `ActiveStatus` while the config it was
asked for was never applied.** That is precisely the "model departure"
the thread warns about, and it is the exact defect non-negotiable #6 exists to
prevent.

**The fix, and it needs no `StoredState`.** `ops/_main.py` runs
`_emit_charm_event()` and then `_evaluate_status()` in the same method, in the same
process, on the same charm instance:

```python
self._emit_charm_event(self.dispatcher.event_name)
if not self.dispatcher.is_restricted_context():
    _charm._evaluate_status(self.charm)
```

So a plain instance attribute carries a push status from the reconciler to the
status handler:

```python
def __init__(self, framework: ops.Framework):
    super().__init__(framework)
    self._reconcile_failure: ops.StatusBase | None = None
    ...

def _reconcile(self, _: ops.EventBase) -> None:
    try:
        ...
    except WorkloadError as e:
        # A push status: collect_unit_status cannot re-derive this, because
        # the service is healthy and only the push silently failed.
        self._reconcile_failure = ops.BlockedStatus(str(e))

def _on_collect_status(self, event: ops.CollectStatusEvent) -> None:
    if self._reconcile_failure is not None:
        event.add_status(self._reconcile_failure)
    # ... then the pull statuses
```

This is state, but it lives for one hook execution and is gone. It does not violate
the "no `StoredState`" rule, which is about caching across hooks.

**Prefer converting push to pull where you can.** For this charm, "does the
running config match the intent?" *is* pullable — `pull` the file in the
status handler and diff it against the intent. Do that where it is cheap, and
fall back to the instance attribute only where attempting the operation is the
only way to know.

### Precedence

When several statuses are added (`StatusBase._get_highest_priority`):

```
blocked  >  maintenance  >  waiting  >  active
```

Ties go to the first one added. **One spurious Blocked masks every other status the
handler adds**, which is another reason to reserve it for genuine operator
problems.

`add_status` can be called many times, and the docs say *"each code path in a
collect-status handler should call `add_status` at least once"*.

`collect_app_status` also exists and the framework only emits it on the leader, so
you do **not** need an `is_leader()` guard in that handler. This charm has a
`replicas` peer relation, so it matters here.

## Ports

`self.unit.set_ports(*ports)` is declarative and idempotent — it diffs against
`opened_ports()` and closes what is no longer wanted. That makes it a correct
reconcile step, unlike `open_port`/`close_port` which manage ports
individually. This charm serves HTTP: `set_ports(80)` (and 443 when TLS is
served — the pure core answers which ports the intent serves).

**`ops.TCPPort` and `ops.UDPPort` do not exist in `ops`.** They only exist in
`ops.testing`. In production code the type is `ops.Port` — or a bare `int`,
which means TCP.

On Kubernetes, `set_ports` is declarative documentation for cross-charm
communication; external reachability is the ingress relation's job, not
`juju expose`.

## Where the URL comes from

The `get-url` action and the `catalogue-item` relation both need the
external URL, and it has a fallback chain: the ingress URL when Traefik is
related, otherwise the pod's in-cluster FQDN (`scheme://fqdn:port`). The scheme
flips to `https` when TLS is available — which means the URL depends on
relation state, and belongs in the fetched facts, not in a `__init__`-time
`socket.getfqdn()` call.

## Other APIs worth knowing

- **`self.unit.set_workload_version(str)`** — shows the workload's version
  (not the charm's) in `juju status`. Raises `TypeError` if not given a `str`.
- **`self.app.planned_units()`** — how many units Juju *intends* to have.
  The way to distinguish a deliberate scale-down from a failed unit.
- **Logging needs no setup.** `ops.main` wires `logging` to `juju-log`, so
  `logging.getLogger(__name__)` already reaches `juju debug-log`.

## Typed config and params — prefer the native pydantic support

`ops` 2.23+ has first-class pydantic integration. Since this repo mandates
pydantic anyway, use it instead of hand-parsing `self.config` — which today
does `json.loads(self.model.config["links"])` at every call site:

```python
config = self.load_config(CatalogueConfig, errors="blocked")
```

`errors="blocked"` sets `BlockedStatus` with a useful message and exits 0, so Juju
does not retry a hook that can only fail again. Dashes in option names map to
underscores, and `pydantic.Field(alias=...)` is respected.

**Note the pin:** this repo requires `ops>=2.20.0` today; adopting `load_config`
means raising that floor to `>=2.23`. Do it in the same change.

The action equivalent:

```python
params = event.load_params(GetUrlParams, errors="fail")
```

## Health checks — Pebble's, not Kubernetes's

Pebble supports `http`, `tcp` and `exec` checks in the layer, and Juju wires
`level: alive`/`level: ready` checks to Kubernetes liveness/readiness probes
automatically for sidecar containers. The ops docs recommend **against**
`level: alive` for services with long startup — use a plain check with
`on-check-failure: restart` so Pebble, not Kubernetes, controls restarts.

`PebbleCheckFailedEvent` / `PebbleCheckRecoveredEvent` exist (ops 2.15+,
Juju 3.6+), observed via `self.on[container_name].pebble_check_failed`. A
candidate design for this charm — not a mandate — is an `http` check on `/`
so "nginx answers" stops being something the charm infers from service
status. If adopted, it belongs in an ADR.

## Events that do not exist or should not be observed

- `pre_series_upgrade` / `post_series_upgrade` — **removed in Juju 4.0.** Do not
  observe them.
- `leader_settings_changed` — deprecated since ops 2.4.0.
- `collect_metrics` — removed in Juju 3.6.11.
- `*_pebble_ready` — these exist here (unlike a machine charm) and only because
  `charmcraft.yaml` declares `containers:`. They are deferrable, but route them
  through `_reconcile` like everything else.

## Verification pattern

Because the workload's success signals lie — `can_connect()` races,
`restart()` returns before health, pushed files vanish on upgrade — every
apply gets a read-back:

```python
def restart(self, service: str) -> None:
    """Restart a service and verify it came up.

    Raises:
        ServiceNotRunningError: The service did not report active
            after the restart.
    """
    self._container.restart(service)   # returning is not evidence
    info = self._container.get_services(service)[service]
    if not info.is_running():
        # restart() succeeding while the service stays down is the
        # lie this read-back exists to catch (rule 6).
        raise ServiceNotRunningError(service=service, current=info.current)
```

The sketch shows the shape; `src/workload.py` is the authority. The
load-bearing move is the last one: whatever the method reported, the state
must be read back before the step counts as done. And the inverse rule for
pushed files: because the container filesystem is ephemeral, the reconcile
re-pushes whenever the fetched state does not show them — never cache
"pushed" in charm state.
