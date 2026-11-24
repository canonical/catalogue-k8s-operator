---
name: charm-functional-style
description: >-
  Use when designing or writing src/catalogue_state.py, src/workload.py, or the
  reconciler — the functional core / imperative shell split, outcome ADTs,
  frozen dataclasses, exhaustive match with assert_never, and errors as data.
  Load before writing a function that both decides something and performs an
  effect.
metadata:
  verified: "2026-10-07"
  source: "canonical/fp-edge-canonical (workshop material, see caveats)"
---

# Functional style for charms

## Where this comes from, and how much to trust it

`canonical/fp-edge-canonical` is the reference. Be precise about what it is:

- It is **workshop material** by an external presenter (`ncreep` / Daniel
  Beskin), forked by Canonical in March 2026. 13 commits, all from the author.
  0 stars, 0 human PRs since the fork, issues disabled.
- Its own `code/README.md` says: *"After all the edits the code cannot be used in
  any way as an actual charmed operator. The only purpose of this repo is for
  educational purposes."*
- **No Canonical production charm uses this.** It is a talk, not an
  architectural direction.
- Its central contradiction: the repo whose thesis is *"this makes tests easy"*
  contains **49 tests, all of the `Result` type itself, and zero tests of any
  charm logic**. The testability argument is demonstrated only in slides, on a
  function (`rules_filename`) that does not exist in the repo.
- The refactor is partial and leaks: `compute.py` does `from charm import
  PROMETHEUS_DIR`; `fetch_impl` calls `charm._generate_command()`, which does
  lightkube IO and mutates `_stored.status` in four branches; the entire
  `retention_size` logic is byte-for-byte identical to the unrefactored baseline.

So: **take the patterns, not the infrastructure.** The patterns below are all
Python 3.12 stdlib — frozen dataclasses, `match`, `assert_never`, PEP 695 type
unions. They need no FP library and they reinforce this repo's non-negotiables.
What follows the patterns is a list of things from that repo we deliberately do
**not** adopt.

The repo is available as the `fp-edge` reference if you want to read the source.

## The thesis worth keeping

From the slides:

```
    ┌─────────┐       ┌───────────┐      ┌─────────┐
    │  Fetch  ├──────►│  Compute  ├─────►│  Apply  │
    └─────────┘       └───────────┘      └─────────┘
         ▲                  ▲                 ▲
         │           Functional core          │
         └───────── Imperative shell ─────────┘

    Make the core maximally smart.
    Make the shell maximally dumb.
```

The concrete defect it attacks: a method that **decides and acts at the same
time**. In the fp-edge baseline, `_generate_prometheus_config()` pushes the
file *and* returns `should_reload`. In this charm's baseline,
`_update_web_server_config()` pushes the nginx config *and* returns whether it
changed, and `_configure()` threads the answers into a restart decision. You
cannot test the decision without performing the effect. Every mock-heavy charm
test is a symptom of this.

This maps directly onto this repo's non-negotiable #2 (charm logic vs workload
logic) and #6 (never trust a success signal) — it just draws the boundary in
**data** rather than only in modules.

## Pattern 1 — Model the input as a union, not as optional fields

Do not start the reconciler with an early return. Make "the container is not
connectable" a *case of the input type*.

```python
from dataclasses import dataclass
from typing import final


@final
@dataclass(frozen=True)
class ContainerNotReady:
    """The Pebble API is not reachable yet. Defer or wait; decide nothing."""


@final
@dataclass(frozen=True)
class ContainerReady:
    """The Pebble API answered. Every field is a fact read from the container."""

    running_nginx_config: str
    running_catalogue_config: Mapping[str, object]
    service_active: bool
    tls: TlsState
    ingress_url: str | None
    items: Sequence[CatalogueItem]
    # … the full set of facts will live in src/catalogue_state.py (roadmap
    # stage 1) — when it exists, read the file, do not copy from here.


type CatalogueState = ContainerNotReady | ContainerReady
```

Note the container types: `Mapping`, `FrozenSet`, `Sequence` — never `dict`,
`set`, `list` in a signature or a frozen field. That is not decoration: `set`
iteration order is a real source of relation-databag churn, which is exactly what
`flaplint` exists to catch (see the `python-style` skill).

Why a union beats `Optional` fields: the antipattern is a dataclass where half
the fields are `None` and the invariants live in comments, forcing `cast()` at
every use site. **Make illegal states unrepresentable.**

## Pattern 2 — Reify the decision as an outcome ADT

This is the highest-value pattern for this charm. Instead of booleans threaded
through control flow, name every thing the charm can decide to do:

```python
@final
@dataclass(frozen=True)
class PushNginxConfig:
    """Rendered config body to push to NGINX_CONFIG_PATH."""


@final
@dataclass(frozen=True)
class PushCatalogueConfig:
    """The config.json body: charm config plus the catalogue items."""

    body: Mapping[str, object]


@final
@dataclass(frozen=True)
class PushCerts:
    """TLS material from the certificates relation; container storage is
    ephemeral, so this outcome recurs whenever the state does not show them."""


@final
@dataclass(frozen=True)
class RemoveCerts:
    pass


@final
@dataclass(frozen=True)
class RestartNginx:
    reason: str


@final
@dataclass(frozen=True)
class Noop:
    pass


type CatalogueOutcome = (
    PushNginxConfig | PushCatalogueConfig | PushCerts | RemoveCerts | RestartNginx | Noop
)
```

The outcome type *answers questions*: "under what conditions does nothing
happen?", "when exactly do we restart nginx?", "when are certs re-pushed?".
Those are the questions where bugs hide, and they become a type instead of a
comment. Note what dies with this pattern: the `push_certs` boolean on
`_configure` — whether certs are pushed becomes a case of the state and an
outcome, not a permission flag threaded from a call site.

## Pattern 3 — Compute is pure and exhaustive

```python
from typing import assert_never


def compute(state: CatalogueState, intent: CatalogueIntent) -> Sequence[CatalogueOutcome]:
    """Decide what to do. No IO. No exceptions raised for control flow."""
    match state:
        case ContainerNotReady():
            return (Noop(),)  # status handler reports Waiting; nothing to apply
        case ContainerReady():
            return _compute_ready(state, intent)
        case _ as unreachable:
            assert_never(unreachable)
```

`assert_never` with a PEP 695 `type X = A | B` union is the load-bearing
mechanism, not a stylistic flourish: **pyright fails the build** if you add a case
to the union and forget a `match` branch. That is a real guarantee, and it is why
`tox -e static` matters more here than in a typical charm.

`compute` takes plain data and returns plain data. Testing it needs no
`ops.testing`, no mocks, no `monkeypatch` — construct the dataclass, compare the
result with `==` (frozen dataclasses give you `__eq__` for free).

## Pattern 4 — Fetch once, at one boundary

All reads of the world happen in one function that returns the frozen snapshot.
Nothing downstream touches Juju, the container, or the relation data.

```python
def fetch(facts: CatalogueFacts) -> CatalogueState:
    """Read every fact the decision depends on. The only impure read path."""
```

If a second function starts reading the container or the relations, the boundary
has broken and the tests will start needing mocks again.

## Pattern 5 — Effects as a Protocol of atomic, verifying operations

Declare the effects as an interface, one method per atomic operation. Crucially
for this charm, **each one verifies real state and returns a value the caller
cannot accidentally ignore**:

```python
class CatalogueActions(Protocol):
    def push(self, path: str, body: str) -> None: ...
    def remove_path(self, path: str, *, recursive: bool) -> None: ...
    def ensure_layer(self, layer: Mapping[str, object]) -> None: ...
    def restart(self, service: str) -> None: ...
```

The fp-edge repo's `reload_config` is literally this charm's problem:

```python
def reload_config(self) -> Result[ReloadError, None]:
    reloaded = self._charm._prometheus_client.reload_configuration()
    return Ok(None) if reloaded is True else Err(ReloadError())
```

Translated: `restart` calls `Container.restart()`, **reads
`get_services()` back and checks `.is_running()`**, and raises a
`WorkloadError` naming the service if it did not come up — `restart()` returning
without raising is not evidence that nginx serves. Non-negotiable #6 expressed as
a signature.

## Pattern 6 — Errors are data that carry context

```python
@final
@dataclass
class ServiceNotRunningError(Exception):
    service: str
    current: str
```

Note the missing `frozen=True` — not an oversight. Exceptions are the one
carve-out from the frozen-dataclass habit: `ops` assigns `exc.__traceback__` as
the event context unwinds, and a frozen exception dies with
`FrozenInstanceError`, burying the real failure. Plain `@dataclass`, plus the
guard test each exception module in this repo carries asserting it survives
being raised.

Subclassing `Exception` keeps `logger.error(..., exc_info=err)` and
`ExceptionGroup` working. Carrying the context inside the error means the status
handler can build an informative `BlockedStatus` without going back to the
workload for more information.

## Pattern 7 — Composition over inheritance, and where injection is possible

`ops` and the charm libraries are object-oriented. That is not a conflict with the
patterns above, but it does constrain *where* dependency injection works. Verified
against the `ops` source, not assumed:

**Constructor injection into the charm is not available.** `ops/_main.py`:

```python
self.charm = self._charm_class(self.framework)
```

Only the framework is passed. `ops.testing.Context.__init__` takes
`charm_type: type[CharmType]` — a type, not a factory — and reuses the same
machinery via `_ops_main_mock.py`. There is no `charm_factory`, `charm_kwargs`, or
`charm_args` hook anywhere in `ops` or `ops.testing`. So at the charm boundary the
seam is a module attribute replaced with `monkeypatch`, and there is no
alternative. Do not invent a factory indirection to pretend otherwise.

**Inject below the charm, where the untestable code lives.** The workload module
is a plain class and takes its collaborators:

```python
class Workload:
    """Own every effect on the container. Knows nothing about ops."""

    def __init__(self, container: ops.model.Container) -> None:
        self._container = container
```

The container itself is handed to you by `ops` (`unit.get_container`), so the
constructor seam here is thin — and that is fine. What the seam buys is a
**fake that lies the way Pebble lies** — `can_connect()` true but
`get_services()` reporting `inactive`, a push that "succeeds" while the pulled
file shows stale content — so the read-back verification of non-negotiable #6
can be tested directly rather than by patching `Container` methods and
asserting on call order.

**Compose the charm libraries, never subclass them.** `CatalogueProvider(self,
...)`, `IngressPerAppRequirer(charm=self, ...)` are instantiated in `__init__`.
Note that they take the whole charm: the ops ecosystem injects the god object.
That is a wart to live with, not a pattern to copy — **our own functions take the
narrowest thing they need**, never `charm: CatalogueCharm`.

**Where injection is not worth it.** This is a process that runs once per hook and
exits. A constructor with eight collaborators and a factory to assemble them is
ceremony with no payoff. A `Protocol` earns its place for one of two reasons:
**(a)** a second implementation exists — a real one and a fake, as with
`CatalogueFacts`, implemented by the workload module and by `FactsStub` in the
tests; or **(b)** it inverts an import the layering forbids — that same
`CatalogueFacts` is what lets `catalogue_state.py` describe the reads it needs
without importing the workload into the pure core. Wanting testability alone is
not a reason: for that, replacing a module attribute is enough. The fp-edge
slides say as much about their own `ConfigActions`: *"You don't have to use
abstract interfaces like this. But phrasing your 'atomic' actions helps with
design."*

Rule of thumb: **inject the effect boundary, not every collaborator.**

## Pattern 8 — A boolean must not gate whether a function has effects

Pattern 2 removes booleans that carry a *decision*. This one removes the boolean
that carries *permission to mutate*. The shape looks harmless, and it is in this
repo's baseline today:

```python
# Don't. The name cannot answer "does this mutate?"
def _configure(self, items, push_certs: bool = False):
    ...
    if push_certs:
        self._push_certs()   # a write, reachable only via the flag
```

Two callers pass different values: `pebble_ready` and `certificate_available`
want the write, `config_changed` does not. So whether a given path mutates
depends on an argument at a call site, not on anything the type checker can see.
One wrong `True` in a future refactor makes a status handler mutate state, and no
gate catches it.

Split it so each name is the answer:

```python
def _read_tls_state(self) -> TlsState:
    """Read the assigned certificate from the relation. Reads only."""
    ...


def _ensure_tls_files(self) -> None:
    """Push the assigned cert and key to the container, or remove stale ones."""
    ...
```

Now the call sites choose the effect by choosing the function. And note the
deeper fix once Pattern 2 lands: certs-on-disk becomes a fact in
`CatalogueState`, `PushCerts`/`RemoveCerts` become outcomes computed from it,
and the boolean has nowhere left to live — the reconcile re-pushes certs
whenever the state does not show them, which is exactly what ephemeral container
storage demands anyway.

Not every boolean parameter is this defect. `combine=True` passed straight
through to `add_layer` is fine — it configures an effect that happens either
way. The test is whether flipping the flag changes *whether* the function
mutates anything.

## What we deliberately do not adopt

**A hand-rolled `Result[E, A]`.** The fp-edge repo implements one in `result.py`
(~145 lines) plus `result_combinators.py` (~180 lines of mechanical boilerplate
duplicated for arities 2–5, because Python has no variadic generics). It has
`# pyright: ignore[reportGeneralTypeIssues]` three times for broken covariance.
Its own slides call the resulting code *"Extreme Nestiness"* and point at
`Result.do` as the fix — **which is commented out because they never implemented
it**. Copying 325 lines from a talk with no integration tests is debt that buys
nothing here. If a concrete multi-error validation case appears, use `returns`
from PyPI, or note that **pydantic v2 already accumulates validation errors**,
which covers the "invalid `links` JSON" case without any `Result` at all.

**A monadic pipeline as the reconciler.** `fetch().flat_map(compute)
.flat_map(apply).on_error(handler)` is elegant, but `flat_map` short-circuits: the
first error aborts everything after it. This reconciler must be safe to run twice
*and* safe to never run, and a failure pushing certs must not prevent reporting
the nginx service's status. `collect_unit_status` with multiple partial
statuses is the right mechanism. Keep the fetch/compute/apply *shape*; do not
chain it monadically.

**`from x import *`.** Every functional module in that repo does it, and its
`pyproject.toml` ignores `F403`/`F405` to compensate. It contradicts
non-negotiable #3.

**`StoredState` as the status channel.** That repo writes status tuples into a
shared mutable dict that nothing reads — the exact "spooky action at a distance"
its own slides denounce. We have `collect_unit_status`.

**A single `observe(config_changed)`.** That repo has no reconciler and no
`collect_unit_status`. Non-negotiable #1 is stricter and better.

**`requires-python = ">=3.13"`.** Everything they use (PEP 695 `type X`,
`def map[B]`, `assert_never`, `ExceptionGroup`) is Python 3.12 or earlier. The
pin is arbitrary; this repo is Python 3.14 only either way (the only interpreter
in `ubuntu@26.04`), so the lesson is simply: do not write code that merely
tolerates older interpreters.

## The honest trade-off

Reifying decisions as ADTs costs code: more types, more `match` statements, more
names to invent. It pays off when the decision is genuinely complex — and for
this charm it is: ephemeral container storage that silently loses certs, a
point-in-time `can_connect()` that races the next call, TLS state arriving over a
relation, ingress URLs that exist in databags before they exist in DNS, and
three written artifacts (nginx config, config.json, certs) whose drift from
intent must each be detected. That is the case where "which conditions cause a
restart?" deserves to be a type.

It does not pay off for trivially linear code. Do not wrap a two-line function in
an ADT to look principled. The slides are right about this much:

> Not a magic bullet. You don't have to go "all in".
