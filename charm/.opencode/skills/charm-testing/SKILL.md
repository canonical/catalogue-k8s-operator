---
name: charm-testing
description: >-
  Use when writing or reviewing tests — unit tests with ops.testing (Scenario)
  for a Kubernetes charm, containers in State, conftest fixtures, coverage
  gates, or integration tests with jubilant on a k8s cloud. Covers the
  two-layer mocking strategy and the pebble_ready trigger. Load before writing
  any test file.
metadata:
  verified: "2026-11-20"
---

# Testing a Kubernetes charm

Three layers, mirroring the source design. Do not mix them.

| Layer | Tool | What you mock |
|---|---|---|
| Pure decision (`compute`, config mapping) | plain `pytest` | **nothing** |
| State transition (`charm.py`) | `ops.testing` `Context` + `State` | the workload module — whole |
| Workload (`workload.py`) | plain `pytest` | the constructor seam — a fake `Container` that lies the way Pebble lies |
| Integration | `jubilant` + `pytest-jubilant` on a k8s cloud | nothing |

## Layer 0 — pure functions need no test infrastructure

This is the payoff of the functional split (see `charm-functional-style`). A
function with the shape `compute(state: CatalogueState, intent:
CatalogueIntent) -> Sequence[CatalogueOutcome]` has no `self`, no IO, and no
exceptions used for control flow. Testing it is construction and `==`, because
frozen dataclasses give you `__eq__`:

```python
def test_not_ready_container_yields_nothing():
    # GIVEN a container whose Pebble API is not reachable
    state = ContainerNotReady()

    # WHEN the outcome is computed
    outcomes = compute(state, INTENT)

    # THEN nothing is applied — the status handler reports Waiting
    assert outcomes == (Noop(),)


def test_unchanged_config_yields_noop():
    # GIVEN a running container whose nginx and catalogue configs match intent
    state = ContainerReady(
        running_nginx_config=EXPECTED_NGINX,
        running_catalogue_config=EXPECTED_CONFIG,
        service_active=True,
        ...
    )

    # WHEN the outcome is computed with the same intent
    outcomes = compute(state, INTENT)

    # THEN nothing happens — this is the "safe to run twice" proof
    assert outcomes == (Noop(),)
```

**Put as much logic as possible in this layer.** Every decision that lives here is
a decision tested without a mock, and mocks are where charm test suites rot.

**If a test at this layer needs `monkeypatch`, the function is not pure** — the
decide/act split is wrong. Fix the code, not the test.

Coverage tends to concentrate here naturally, which is the right shape: the
failure paths are decisions, and decisions are cheap to enumerate.

## The trap that will get you at layer 1

`ops.testing.Model.type` is `Literal['kubernetes', 'lxd']` and **defaults to
`'kubernetes'`** — which is correct here. The trap is inverted from a machine
charm: copy-pasting `Model(type='lxd')` from a machine-charm test puts this
charm in the wrong environment. Either rely on the default or be explicit:

```python
state = testing.State(model=testing.Model(type="kubernetes"), leader=True)
```

The container is a first-class citizen of `State`. Verified field names — note
the plurals:

```python
container = testing.Container(
    name="catalogue",
    can_connect=True,
    layers={"catalogue": pebble.Layer(...)},          # Mapping[str, pebble.Layer]
    service_statuses={"catalogue": pebble.ServiceStatus.ACTIVE},
)
state = testing.State(leader=True, containers=[container])
```

`can_connect=False` (the default) makes the real `Container.can_connect()`
return `False` inside the charm — the simulation honours it, so the
not-ready path is testable directly.

`pebble_ready` fires through the context, not by hand:

```python
state_out = ctx.run(ctx.on.pebble_ready(container), state_in)
```

## Layer 1 — state transition tests

```python
# tests/unit/conftest.py — the real file is the authority; this is a sketch.
from unittest.mock import MagicMock

import pytest
from ops import testing

import charm


@pytest.fixture
def mock_workload(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the workload module's class with a converged fake.

    The facts are real values, so fetch and compute run for real and the
    transition tests exercise actual decisions. See the real conftest.py
    for the full set of return values.
    """
    mock = MagicMock()
    mock.running_nginx_config.return_value = EXPECTED_NGINX
    mock.running_catalogue_config.return_value = EXPECTED_CONFIG
    mock.service_active.return_value = True
    mock.tls_state.return_value = TlsAbsent()
    # Patch via the module reference, not charm.Workload:
    monkeypatch.setattr(charm.workload, "Workload", lambda: mock)
    return mock


@pytest.fixture
def ctx() -> testing.Context[charm.CatalogueCharm]:
    return testing.Context(charm.CatalogueCharm)


@pytest.fixture
def base_state() -> testing.State:
    """A kubernetes model with the container connected."""
    return testing.State(
        leader=True,
        containers=[testing.Container(name="catalogue", can_connect=True)],
    )
```

```python
# tests/unit/test_charm.py
def test_config_changed_applies_title(ctx, base_state, mock_workload):
    # GIVEN a unit configured with a title
    state_in = dataclasses.replace(base_state, config={"title": "My Catalogue"})

    # WHEN config-changed fires
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    # THEN the workload module receives the rendered config and the unit goes active
    pushed = mock_workload.push.call_args.kwargs["body"]
    assert '"title": "My Catalogue"' in pushed
    assert state_out.unit_status == testing.ActiveStatus()


def test_reconcile_is_idempotent(ctx, base_state, mock_workload):
    # GIVEN a converged unit
    state = ctx.run(ctx.on.config_changed(), base_state)

    # WHEN the same event fires again
    state = ctx.run(ctx.on.config_changed(), state)

    # THEN nothing regresses
    assert state_out.unit_status == testing.ActiveStatus()
```

Test both directions of the non-negotiable: **what breaks if it runs twice**
(the test above) and **what breaks if it never runs** (assert the status is a
not-ready status, never `Active`, when a step has not happened — e.g. the
container not connected must yield `WaitingStatus` or `MaintenanceStatus`,
whichever ADR-0001 settles on, and never `ActiveStatus`).

**The boundary rule:** a test of `charm.py` mocks the workload module whole.
If it patches `Container` methods or `ops.pebble` directly, the layer
separation has already broken (AGENTS.md rule 2) — fix the code, not the test.

## Layer 2 — workload module tests

Fake the container at the workload module's constructor seam. The point of the
seam is a fake that **lies the way Pebble lies**, so the read-back verification
of non-negotiable #6 is tested directly:

```python
def test_restart_raises_when_service_stays_down():
    # GIVEN a container whose service does not come up after restart —
    # restart() returning is not evidence the service runs
    fake = FakeContainer(services={"catalogue": pebble.ServiceStatus.INACTIVE})
    workload = Workload(container=fake)

    # WHEN nginx is restarted
    # THEN the charm refuses to believe the return value
    with pytest.raises(ServiceNotRunningError, match="catalogue"):
        workload.restart("catalogue")
```

```python
def test_push_certs_writes_all_three_paths():
    # GIVEN a container recording pushes
    fake = FakeContainer()
    workload = Workload(container=fake)

    # WHEN certs are pushed
    workload.push_certs(cert=..., key=..., ca=...)

    # THEN all three land — and the test asserts the paths, not the call count
    assert fake.files[CERT_PATH] == cert
    assert fake.files[KEY_PATH] == key
    assert fake.files[CA_CERT_PATH] == ca
```

Every workload interaction deserves a lie-test like the first one: `can_connect`
true but service `inactive`; a pull that returns yesterday's config after a
push; certs that vanish from storage (the ephemeral-filesystem case — the
reconcile must re-push, not assume).

## Testing actions and status

**`ctx.run_action` does not exist.** It was removed and raises `AttributeError`
with the replacement in the message. Actions go through `ctx.run` like everything
else:

```python
def test_get_url_action(ctx, base_state, mock_workload):
    # GIVEN a running unit
    # WHEN the action runs
    ctx.run(ctx.on.action("get-url"), base_state)

    # THEN the result is reported to the operator
    assert ctx.action_results is not None
    assert "url" in ctx.action_results
```

- `ctx.on.action(name, params=...)` — `name` uses **dashes**, as in the metadata.
- `ctx.action_results` is `None` if the charm never called `set_results`.
- `ctx.action_logs` is a list of strings.
- If the charm calls `event.fail(...)`, `ctx.run` raises `testing.ActionFailed`.

**Testing `collect_unit_status`: prefer the indirect route.** The framework emits
it after every hook, so the resolved status is already in the output state. That
exercises the reconciler and the status handler together, with their real
interaction:

```python
state_out = ctx.run(ctx.on.config_changed(), state_in)
assert state_out.unit_status == testing.WaitingStatus("Waiting for Pebble ready")
```

`ctx.run(ctx.on.collect_unit_status(), state)` exists for isolated parametric
tables, but do not make it the primary way you test status.

`ctx.unit_status_history` gives the full sequence of intermediate statuses — useful
for asserting the unit passes through `MaintenanceStatus` during a config push rather
than jumping straight to Active.

Ports assert as `testing.TCPPort` / `testing.UDPPort`, which exist **only** in
`ops.testing` (production code uses `ops.Port` or a bare int):

```python
assert state_out.opened_ports == {testing.TCPPort(80)}
```

That asymmetry between `ops.Port` and `testing.TCPPort` is real, not a typo.

## Treat warnings as errors

Run pytest with `-W error`. `ops` itself does this in its own unit tests, and the
official how-to recommends it. It is how you find an ops deprecation while it is
still a warning instead of after it becomes a breakage.

## Coverage

A `fail_under` gate in `[tool.coverage.report]` is roadmap stage 3. Coverage
measures `src/` only. Do not chase the number by testing getters; chase it by
covering failure paths, which is where this charm's risk lives.

If coverage is hard to reach, that is usually a design signal rather than a
testing problem: logic that is expensive to cover is logic sitting on the wrong
side of the decide/act boundary. Move the decision into a pure function and the
coverage follows for free.

## Determinism

Tests must not depend on iteration order. If an assertion compares a serialised
collection, sort at construction (`tuple(sorted(...))`), not in the assertion —
otherwise the test passes while the production code still flaps. This is the same
defect `flaplint` looks for; see `python-style`.

## Integration tests

`jubilant` works on Kubernetes clouds the same way as on machines — the
existing `tests/integration/` in this repo is the authority for environment
setup and helpers; read it before writing a new integration file.

```python
# tests/integration/test_deploy.py
def test_deploy_reaches_active(juju: jubilant.Juju, charm_path):
    # GIVEN a fresh k8s model
    # WHEN the charm is deployed with its OCI image
    juju.deploy(charm_path, "catalogue-k8s", resources={"catalogue-image": IMAGE})

    # THEN it converges without any relations
    juju.wait(jubilant.all_active, timeout=900)
```

### Kubernetes-vs-machine jubilant differences

Verified from the jubilant docs:

- `Juju.remove_unit()` takes the **app name plus `num_units`** on Kubernetes
  (*"individual units are not named"*); on machines it takes unit names.
- `Juju.exec(..., container=...)` is Kubernetes-only; the `machine=` parameter
  is machine-only.
- `Juju.add_machine(...)` and `Juju.deploy(..., attach_storage=...)` do not
  exist on Kubernetes clouds.

### Juju CLI names changed in 3.x — do not copy 2.9 examples

The rename is a trap because the same string means different things:

| Intent | Juju 2.9 | **Juju 3.6** |
|---|---|---|
| run an arbitrary command | `juju run` | **`juju exec`** |
| run a charm action | `juju run-action` | **`juju run`** |
| pick a series/base | `--series` | **`--base`** |

So `juju run` in a 2.9 tutorial is `juju exec` today, and `juju run` today is what
used to be `run-action`. Both still exist, which is why the mistake is silent.

`juju wait-for application|unit|model` is the native alternative to
hand-rolled polling loops, though `jubilant`'s own wait helpers are usually
better inside tests.

### `update-status` interval

Default is **5m**, changed with `juju model-config
update-status-hook-interval=30s`. Lowering it in the test model fixture makes any
test that depends on `update-status` converge much faster.

But note the design signal: **if the charm only reaches `ActiveStatus` via
`update-status`, some event that should trigger a reconcile is not observed.**
Lowering the interval to make a test pass hides that bug rather than fixing it.

### Practical notes for this charm

- Never pack inside the test. Pack once by hand (`charmcraft pack`), export
  `CHARM_PATH`, and reuse it. Packing is slow and every test file would repeat it.
- **Name relation endpoints explicitly.** `juju integrate catalogue-k8s traefik`
  may resolve via an unexpected endpoint. Always write
  `catalogue-k8s:ingress traefik:ingress`.
- Ingress tests need Traefik deployed and related first; TLS tests need a
  certificate provider (the existing `tests/integration/test_ingress.py` shows
  the working shape — follow it).
- Assert on real workload state where possible — `juju exec --unit
  catalogue-k8s/0 -- ...` runs inside the unit — but prefer asserting on the
  catalogue's served page or the `get-url` action over shell state. NOT
  VERIFIED: whether `juju exec --unit` on Kubernetes lands in the charm
  container or the workload container; check before relying on it.
- Environment bootstrap via Concierge (`sudo concierge prepare -p k8s`) is the
  expected equivalent of the machine profile. NOT VERIFIED: the exact profile
  name; check `concierge prepare --help` before documenting it further.
