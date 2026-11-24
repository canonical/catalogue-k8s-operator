# How this charm decides what to do

This charm is built as a **reconciler** using **fetch → compute → apply**.
This document teaches the shape with a small example that has nothing to
do with Catalogue, so you can learn it before reading the real code.

Why this charm adopted the pattern, and which alternatives were rejected,
is recorded in [ADR-0001](adr/0001-fetch-compute-apply.md). This document
explains the shape; the ADR explains the choice.

Lineage: the pattern, the rules, and the skills in this repo are ported
from the `pi-hole-operator` charm (a machine charm), adapted to
Kubernetes. The ideas themselves come from the `fp-edge-canonical`
workshop material — from which we take the patterns, not the
infrastructure (see [§8](#8-what-we-do-not-take-from-the-workshop-material)).

---

## 1. The problem

A charm is not an install script. Three facts about how Juju runs a charm,
and one about workloads, force the design:

1. **A hook runs once and exits.** No process survives between events.
   Every hook starts from nothing and must find out what to do by looking.
2. **You cannot assume order, and you do not see every change.** The
   [Juju hook reference](https://canonical.com/juju/docs/juju-cli/3.6/reference/hook/):
   *"Generally, no assumptions can be made about the order of hook
   execution."* Juju also coalesces events. Code that runs only on
   one specific event is code whose work sometimes never happens.
3. **A hook can fail and be re-run from scratch.** Whatever it does must be
   safe to repeat.
4. **The workload's success signals lie.** On Kubernetes: `can_connect()`
   races the next call, `restart()` returns before the service is healthy,
   and the container filesystem is wiped when the container restarts. The
   full, verified list is [workload-constraints](workload-constraints.md).

Facts 1–3 have one answer: do not ask *"what just happened?"*, ask *"what
is true now, and what should be true?"* — then close the gap. Fact 4 has
another: after you change something, read it back.

So every piece of code here must answer two questions, and both answers
must be *"nothing"*:

- What breaks if this runs twice?
- What breaks if this never runs?

---

## 2. The split

The charm is split in two halves, and the split is the main idea:

- One half **decides** what should happen. It works only with plain
  values and never touches the workload, because it does not import
  anything that can.
- The other half **reads and writes the workload**. It contains no
  decisions: it translates the workload into values on the way in, and
  values into effects on the way out.

Three kinds of value cross the line:

| Value | Answers | Built from |
|---|---|---|
| **Intent** | What *should* be true | Juju: config, relations, secrets |
| **State** | What *is* true | One read of the workload, frozen |
| **Outcome** | What to *do* about the difference | `compute(state, intent)` |

And three functions do the work:

| Function | Reads? | Writes? | Job |
|---|---|---|---|
| `fetch` | yes | no | Collect every fact the decision needs, in **one** pass |
| `compute` | no | no | Compare state to intent; return an **ordered** tuple of outcomes |
| `_apply` | no | yes | Perform **one** outcome; contains no decisions |

Intent and state come from different places and neither is built from
the other. In particular, **nothing desired enters `fetch`**: reading what
is true must not need to know what should be true. If `fetch` took the
intent, a later edit could start deciding inside the function whose only
job is observing.

It is a **loop**, and that is the point. `_apply` changes the workload;
the next event — any event, including the timer-driven `update_status` —
reads the workload again; this time `compute` finds nothing to do and
returns `Noop`. That is what *converge* means.

---

## 3. A small example: `greeter`

A Kubernetes charm for a tiny HTTP server, `greeter`, that answers every
request with a greeting. The server reads `/etc/greeter/greeter.toml`
**once, at startup** — so a changed config only takes effect after a
restart, and the config must be on disk *before* the restart. That gives
the example a real ordering constraint and a real reason to restart.

Three files, and the import rule between them is the design:

| File | Holds | Never imports |
|---|---|---|
| `src/greeter_state.py` | intent, state, outcomes, `Facts`, `fetch`, `compute` | `ops`, the workload module |
| `src/workload.py` | the class that implements `Facts` and performs effects | `ops` at runtime |
| `src/charm.py` | `_reconcile`, `_apply`, status reporting | — |

The "never imports" column is mechanically checkable — grep the import
block. `greeter_state.py` importing the workload is what would make its
tests need mocks.

### 3.1 State: a union, not optional fields

```python
# src/greeter_state.py
@dataclass(frozen=True)
class ContainerNotReady:
    """Pebble is not reachable. Decide nothing; pebble_ready will fire."""


@dataclass(frozen=True)
class ContainerReady:
    """Facts read from the container. Every field is observed."""

    running_config: str  # "" when the file does not exist
    plan_services: Mapping[str, Mapping[str, str]]
    service_running: bool


type GreeterState = ContainerNotReady | ContainerReady
```

`plan_services` is the `services` section of the running Pebble **plan** —
the result of combining every layer in the container — not the layer the
charm adds. Comparing it with the services the charm *wants* answers "is
our layer applied?", because the charm adds its layer with
`combine=True` and `override: replace`. It arrives as plain dicts, not
`ops.pebble` objects: the core cannot import `ops`, so the workload
translates at the boundary (`get_plan().services`, each converted with
`Service.to_dict()`).

The easy version is one class with `reachable: bool` and
`running_config: str | None`. That version lets you build
`reachable=False, running_config="..."` — a state that cannot happen —
and forces a `cast()` or a `None` check at every use. As two classes, the
impossible state cannot be built at all.

`ContainerNotReady` is deliberately empty and there is nothing to do with
it: on Kubernetes the workload already exists in the OCI image, so there
is nothing to install. The charm waits; `pebble_ready` fires when the
container comes up, and that event reconciles. (Do not `defer()` — it
queues handlers that all redo the same work; see the
`charm-k8s-workload` skill.)

### 3.2 Intent: the desired world, as plain data

```python
# src/greeter_state.py, continued
LAYER_SERVICES: Mapping[str, Mapping[str, str]] = {
    "greeter": {
        "override": "replace",
        "command": "greeter --config /etc/greeter/greeter.toml",
        "startup": "enabled",
    }
}


@dataclass(frozen=True)
class GreeterIntent:
    config: str  # the rendered greeter.toml
    layer_services: Mapping[str, Mapping[str, str]]


def intent_from(greeting: str) -> GreeterIntent:
    """Build the intent from Juju config. Pure: values in, value out."""
    return GreeterIntent(
        config=f'greeting = "{greeting}"\n',
        layer_services=LAYER_SERVICES,
    )
```

`layer_services` on the intent is what goes under the `"services"` key of
the layer the charm adds. One trap in comparing it against
`plan_services`: `Service.to_dict()` drops fields whose value is empty or
false. Declare a field as `""` here and the plan will never return it, the
comparison never matches, and every reconcile re-applies the layer and
restarts the service. Declare only fields that have a value.

### 3.3 Outcomes: the decision, reified

Instead of booleans threaded through control flow, name every thing the
charm can decide to do:

```python
# src/greeter_state.py, continued
@dataclass(frozen=True)
class PushConfig:
    body: str


@dataclass(frozen=True)
class EnsureLayer:
    services: Mapping[str, Mapping[str, str]]


@dataclass(frozen=True)
class Restart:
    reason: str


@dataclass(frozen=True)
class Noop:
    pass


type GreeterOutcome = PushConfig | EnsureLayer | Restart | Noop
```

An outcome **describes** an act; it does not **perform** one. The names
are imperative on purpose — each outcome is an instruction — but
`PushConfig("...")` is only a value: you can compare it, log it, put it
in a tuple, and assert on it in a test, and nothing happens to the
workload until `_apply` carries it out. Deciding produces instructions;
exactly one function executes them. (This is the classic *command
object*: the request is reified as data, separate from its execution.)

Name each outcome for the effect it has on the workload — a config file
on disk, a service definition in place, a service restarted — and keep
the API calls that produce it out of the core: `compute` never knows that
`EnsureLayer` means `add_layer` plus `autostart`; only `_apply` and the
workload do. The names here still speak Pebble's vocabulary (*layer*),
which is fine for a Kubernetes-only charm; a charm that must stay
portable between machine and Kubernetes would pick a neutral name such
as `EnsureServiceDefinition`.

`Restart` carries a required `reason`, so the cause of every restart
reaches the log.

### 3.4 Fetch: the one read path

`fetch` has to read the container, but the file that holds `compute` must
not import anything that touches it. A `Protocol` solves this:
`greeter_state.py` describes the reads it needs and never imports the code
that performs them.

```python
# src/greeter_state.py, continued
class Facts(Protocol):
    """The reads fetch needs. The workload implements it; so can a stub."""

    def can_connect(self) -> bool: ...
    def config(self) -> str: ...
    def plan_services(self) -> Mapping[str, Mapping[str, str]]: ...
    def service_running(self) -> bool: ...


def fetch(facts: Facts) -> GreeterState:
    """Read every fact the decision needs, exactly once."""
    if not facts.can_connect():
        return ContainerNotReady()
    return ContainerReady(
        running_config=facts.config(),
        plan_services=facts.plan_services(),
        service_running=facts.service_running(),
    )
```

This is the only place that reads. If a second function starts reading
the container, the split is broken and the tests will need mocks again.

`can_connect()` is a point-in-time check: Pebble can go away between it
and the next read. So a read can still raise `ops.pebble.Error`, and the
caller must catch it (§5).

### 3.5 Compute: the charm's specification

```python
# src/greeter_state.py, continued
def compute(
    state: GreeterState, intent: GreeterIntent
) -> tuple[GreeterOutcome, ...]:
    """Decide what to do. No reading, no writing, no exceptions."""
    match state:
        case ContainerNotReady():
            return (Noop(),)
        case ContainerReady():
            return _compute_ready(state, intent)
        case _ as unreachable:  # pragma: no cover
            assert_never(unreachable)


def _compute_ready(
    state: ContainerReady, intent: GreeterIntent
) -> tuple[GreeterOutcome, ...]:
    outcomes: list[GreeterOutcome] = []
    reasons: list[str] = []

    if state.running_config != intent.config:
        outcomes.append(PushConfig(intent.config))
        reasons.append("config changed")
    if state.plan_services != intent.layer_services:
        outcomes.append(EnsureLayer(intent.layer_services))
        reasons.append("layer changed")
    if not reasons and not state.service_running:
        reasons.append("service not running")

    # The order is the correctness constraint, written down once: the
    # server reads its config at startup, so the push must come first.
    if reasons:
        outcomes.append(Restart("; ".join(reasons)))

    return tuple(outcomes) or (Noop(),)
```

**This function is the charm's behaviour, in one place**, with no
container, no `ops`, and no mocks. "Under exactly which conditions does
the greeter restart?" is answered by reading it — and ordering is a
literal sequence here, not something that emerges from the order events
arrive in.

Notice how the restart is *derived from the same comparisons* that
produced the pushes, never from "did the push succeed?". That is the
difference from the shape this pattern replaces (§6).

**How `assert_never` works.** In `case _ as unreachable`, pyright narrows
`unreachable` to the union members not matched by the arms above. When
every member has an arm, that type is `Never` and the call type-checks.
Add a member to the union and forget its arm, and `unreachable` has that
member's type — **pyright fails `tox -e static`**, by file and line. The
arm can never run, so it is marked `# pragma: no cover`: its test is the
type checker, not pytest.

### 3.6 Apply: dull on purpose

```python
# src/charm.py
def _apply(self, outcome: greeter_state.GreeterOutcome) -> None:
    """Perform one decided outcome. Exhaustive by construction."""
    match outcome:
        case greeter_state.PushConfig(body=body):
            self._workload.push("/etc/greeter/greeter.toml", body)
        case greeter_state.EnsureLayer(services=services):
            self._workload.ensure_layer("greeter", services)
        case greeter_state.Restart(reason=reason):
            logger.info("restarting greeter: %s", reason)
            self._workload.restart("greeter")
        case greeter_state.Noop():
            logger.debug("converged: nothing to do.")
        case _ as unreachable:  # pragma: no cover
            assert_never(unreachable)
```

One `match`, one call per arm, no `if`, no arithmetic. If you find
yourself deciding something here, it belongs in `compute`.

The workload methods verify what they did. `restart` does not trust
Pebble's return:

```python
# src/workload.py
def restart(self, service: str) -> None:
    self._container.restart(service)  # returning is not evidence
    info = self._container.get_services(service)[service]
    if not info.is_running():
        raise ServiceNotRunningError(service=service, current=str(info.current))
```

### 3.7 The reconciler: the loop itself

```python
# src/charm.py
def __init__(self, framework: ops.Framework) -> None:
    super().__init__(framework)
    self._workload = Workload(self.unit.get_container("greeter"))
    self._reconcile_failure: ops.StatusBase | None = None

    framework.observe(self.on.collect_unit_status, self._on_collect_status)
    for event in (
        self.on.greeter_pebble_ready,
        self.on.config_changed,
        self.on.upgrade_charm,
        self.on.update_status,
    ):
        framework.observe(event, self._reconcile)


def _reconcile(self, _: ops.EventBase) -> None:
    """Converge the container toward intent. Safe twice, safe never."""
    intent = greeter_state.intent_from(str(self.config["greeting"]))
    try:
        state = greeter_state.fetch(self._workload)  # the only read
        for outcome in greeter_state.compute(state, intent):
            self._apply(outcome)
    except (ops.pebble.Error, WorkloadError) as err:
        self._reconcile_failure = ops.BlockedStatus(
            f"workload operation failed: {err}; "
            "check the container with `pebble services`"
        )
```

Every deferrable event goes to the same handler, because none of them
tells you enough to act on by itself: `config_changed` does not say
*which* key changed, `relation_changed` does not say *how* the data
changed. All any of them means is "the world may have changed, go and
look". The events that get their own handler are exactly the ones that
**cannot be deferred**: actions (they must answer now), `stop`/`remove`
(the last chance to clean up), the `secret_*` lifecycle events, and the
`collect_*_status` events (which must not change anything). This is
the official "holistic" approach, see
[Holistic vs delta charms](https://canonical.com/juju/docs/ops/latest/explanation/holistic-vs-delta-charms/).

`update_status` fires on a timer and carries no information at all.
That makes it a good test of the design: if the reconciler is correct, a
periodic "go and look again" repairs any drift — including the one left
by an event you missed, or by a failed hook.

### 3.8 Testing it

The deciding half is tested by building values and comparing values —
frozen dataclasses give `__eq__` for free:

```python
def test_converged_container_yields_noop() -> None:
    # GIVEN a container already matching intent
    intent = intent_from("hello")
    state = ContainerReady(
        running_config=intent.config,
        plan_services=intent.layer_services,
        service_running=True,
    )

    # WHEN the outcome is computed
    # THEN nothing happens — the "safe to run twice" proof
    assert compute(state, intent) == (Noop(),)


def test_config_change_pushes_before_restarting() -> None:
    # GIVEN a running container with an old greeting
    intent = intent_from("hello")
    state = ContainerReady(
        running_config='greeting = "hi"\n',
        plan_services=intent.layer_services,
        service_running=True,
    )

    # THEN the push comes first, then the restart that reads it
    assert compute(state, intent) == (
        PushConfig(intent.config),
        Restart("config changed"),
    )
```

No mocks, no `ops.testing`, no container. **If a test needs a mock to
reach a decision, the split is broken somewhere above it.**

The converged case matters especially on Kubernetes: `ops.testing`
(Scenario) starts every `ctx.run()` with an empty container filesystem
([workload-constraints §9](workload-constraints.md)), so a *converged*
container cannot be represented in a charm-level test at all. It is
representable here, by construction — which is why the comparison logic
must live in this half.

---

## 4. Statuses: push and pull

Status is a separate question from convergence, and it has two kinds of
answer. This is an `ops`/Juju property, the same on machine and
Kubernetes charms.

- A **pull status** can be worked out at any time by reading state: the
  container is not reachable → Waiting; everything reads fine → Active.
- A **push status** is only knowable by *attempting* an operation. If
  `_apply` raised while pushing the config, the service may still be
  running the old config happily — nothing in the state the collector can
  read says the operator's change was never applied. The failure was loud
  inside `_reconcile`, and then the hook moved on.

The reconciler carries the push status across to the status handler in a
plain instance attribute (`_reconcile_failure` in §3.7). That works
because `ops/_main.py` runs the event handler and then evaluates status
**in the same process, on the same charm instance**. It lives for one
hook and is gone, so it is not cross-hook state — it does not violate the
"no `StoredState`" rule.

```python
# src/charm.py
def _on_collect_status(self, event: ops.CollectStatusEvent) -> None:
    """Report status. Never mutates, never raises."""
    if self._reconcile_failure is not None:
        event.add_status(self._reconcile_failure)  # push: carried over
        return
    try:
        state = greeter_state.fetch(self._workload)  # pull: re-derived
    except ops.pebble.Error:
        event.add_status(ops.WaitingStatus("Waiting for Pebble ready"))
        return
    match state:
        case greeter_state.ContainerNotReady():
            event.add_status(ops.WaitingStatus("Waiting for Pebble ready"))
        case greeter_state.ContainerReady():
            event.add_status(ops.ActiveStatus())
        case _ as unreachable:  # pragma: no cover
            assert_never(unreachable)
```

Three rules:

- **The collector never raises.** `collect_unit_status` fires at the end
  of *every* hook, including hooks that did nothing else; an exception
  there sends the unit to error state for no reason.
- **Prefer pull over push where it is cheap.** A status you can re-derive
  from state is always current; a pushed one is only as fresh as the last
  reconcile.
- **A `BlockedStatus` must name the remedy.** It exists to tell a human
  what to do.

(Catalogue maps the state to a status through a small pure function and
a status union in the core, so the mapping is testable without `ops`.)

---

## 5. Errors are data

A workload failure is an exception that carries its context, so the
status handler can build an informative message without going back to
the workload:

```python
# src/workload.py
@dataclass  # NOT frozen — see below
class ServiceNotRunningError(WorkloadError):
    service: str
    current: str
```

Exceptions are the one exception to "frozen dataclasses everywhere":
`ops` assigns `exc.__traceback__` as the event context unwinds, and a
frozen exception raises `FrozenInstanceError` there, burying the real
failure. Each exception module carries a test that its exceptions survive
being raised.

Catch exactly what you can handle — `ops.pebble.Error` and the workload's
own errors — at one boundary, `_reconcile`. A bug in our own code is
allowed to raise: it *is* an error, and the traceback is the point.

---

## 6. Three ways to break it

**Deciding and acting in the same function.** A function that performs an
effect *and* returns a value describing what it decided cannot be tested
without performing the effect. Catalogue's baseline had
`_update_web_server_config()`, which pushed the nginx config *and*
returned whether it changed; `_configure()` then threaded those flags into
a restart decision. Split it: `compute` returns the outcome, `_apply`
performs it.

**A boolean argument that turns effects on and off.** Catalogue's baseline
had `_configure(items, push_certs=False)`: `pebble_ready` passed `True`,
`config_changed` did not. Whether a call path wrote certificates depended
on an argument at a call site, invisible to the type checker. The fix was
not a better flag but no flag: certificates-on-disk became a *fact* in
the state, TLS material became part of the intent, and `PushCerts` /
`RemoveCerts` became outcomes computed from the pair. The reconcile now
re-pushes whenever the container does not show them — which is exactly
what ephemeral container storage demands.

Not every boolean parameter is this defect: `combine=True` passed to
`add_layer` configures an effect that happens either way. The test is
whether flipping the flag changes *whether* the function mutates
anything.

**Believing a return code.** Calling an operation is not proof it
happened: `restart()` returns before the service is healthy. Read back
the state it was supposed to produce (§3.6). This is cheap, because
`fetch` on the next event does exactly that and `compute` will decide to
do the work again.

---

## 7. Machine charms and Kubernetes charms

Only the outer half changes between them. `Intent`, `State`, `Outcome`
and `compute` never import or call the workload API, so the *mechanics*
of a machine or a Kubernetes charm never reach them; `fetch` reads
through the `Facts` protocol, so only the class implementing it changes.
Their *vocabulary* may still name the platform (`ContainerNotReady`,
`EnsureLayer` here) — that is a naming choice, and moving the core to
the other platform means renaming those cases, not rewriting the
decisions.

| | Machine charm (e.g. pi-hole) | Kubernetes charm (this repo) |
|---|---|---|
| The workload exists | after an `Install` outcome (snap, apt) | always — it is in the OCI image |
| The "nothing known yet" state | not installed | Pebble not reachable |
| What triggers the first real work | `install`, `start` | `<container>_pebble_ready` |
| Read or write a file | `pathlib` | `container.pull` / `container.push` |
| Start or restart a service | `systemd`, `snap restart` | a Pebble layer, `container.restart` |
| Check it is running | read real state back | `get_services(...)[name].is_running()` |
| Typical lie | a tool exits 0 for work it did not do | `can_connect()` races; files vanish on container restart |

The last row is where Kubernetes adds work rather than removing it: a
machine's disk keeps what you wrote, a container's does not. So on
Kubernetes, *every* file the workload needs is compared and re-pushed on
every reconcile — the pattern does that for free, because it never
remembers what it pushed; it only reads what is there.

---

## 8. What we do not take from the workshop material

`fp-edge-canonical` is educational material — its own README says the
code *"cannot be used in any way as an actual charmed operator"*. We take
its patterns, all plain stdlib, and reject its infrastructure:

- **A hand-rolled `Result[E, A]`.** About 325 lines from a talk, with
  pyright suppressions and boilerplate duplicated per arity. Exceptions
  caught at one boundary (§5) do the job; where several validation errors
  must be collected, pydantic already does it (pi-hole parses its config
  that way; Catalogue has it as future work, ADR-0001 §6).
- **A monadic pipeline.** `fetch().flat_map(compute).flat_map(apply)`
  short-circuits: the first error aborts everything after it. A failed
  push must not prevent the status from being reported. Keep the
  fetch/compute/apply *shape*; let `collect_*_status` report.
- **`from x import *`.** Every functional module there does it, and that
  repo disables the F403/F405 lint rules to compensate. It contradicts
  this repo's import rules.
- **`StoredState` as a status channel.** That repo writes status tuples
  into a shared mutable dict that nothing reads. The status channel here
  is the single-hook attribute of §4.
- **A single `observe(config_changed)`.** That repo has no reconciler and
  no `collect_*_status`.

---

## 9. The same thing in Catalogue

| In the example | In Catalogue |
|---|---|
| `GreeterIntent`, `intent_from` | `CatalogueIntent`, `intent_from` in [`src/catalogue_state.py`](../src/catalogue_state.py) |
| `GreeterState` | `CatalogueState` = `ContainerNotReady \| ContainerReady` |
| `GreeterOutcome` | `CatalogueOutcome` — adds `PushCerts`, `RemoveCerts`, `PushNginxConfig`, `PushCatalogueConfig` |
| `Facts`, `fetch`, `compute` | same names, same file |
| the workload class | `Workload` in [`src/workload.py`](../src/workload.py) |
| `_reconcile`, `_apply`, `_on_collect_status` | same names in [`src/charm.py`](../src/charm.py); the collector is also observed for leader-only `collect_app_status` |

Catalogue adds the details the example leaves out: certificates as
compared state, an ordering constraint of its own (certs on disk before
the TLS nginx config that references them), and a declarative ingress
databag write at the end of each reconcile. The constraints the workload
imposes are in [`workload-constraints.md`](workload-constraints.md).
