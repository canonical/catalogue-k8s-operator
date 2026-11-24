# Catalogue K8s — a two-minute map

A Juju **Kubernetes** charm deploying the [Catalogue][cos] web UI — a
Pebble-managed nginx container serving a static service directory —
as the landing page of the Canonical Observability Stack (COS). There
is no snap, no systemd: Pebble, via `ops.model.Container`, is the only
interface to the workload (`charmcraft.yaml` declares the
`catalogue` sidecar and its oci-image resource).

[cos]: https://documentation.ubuntu.com/observability

The public interface — relations, config options, the `get-url`
action — is what `charmcraft.yaml` declares; that YAML is the list.

## Source layout

```
src/charm.py             # events → _reconcile; applies outcomes; reports status
src/catalogue_state.py   # pure core: state/intent/outcome/status types, fetch/compute
src/workload.py          # every Pebble touch: Facts reads + verified effects
src/nginx_config.py      # pure nginx config rendering
```

The import rule that makes it testable:
`catalogue_state` imports neither `ops` nor `workload` (it reaches the
container only through the `Facts` protocol); `workload` imports `ops`
only under `TYPE_CHECKING`; `charm` is the only place both meet.

## Event flow

Every deferrable event — `pebble_ready`, `config_changed`,
`upgrade_charm`, `update_status`, `leader_elected`, `items_changed`,
ingress `ready`/`revoked`, `certificate_available` — routes through
**one** `_reconcile`: fetch the real container state, compute a tuple
of named outcomes purely, apply them one by one, then a declarative
ingress databag write. The dedicated handlers are exactly the
non-deferrable set: `get_url` (an action) and `_on_collect_status`
(observed for both `collect_unit_status` and leader-only
`collect_app_status`, which fire at the end of every hook).

Statuses: a failed reconcile stores a Blocked-with-remedy message that
the collector reports (it cannot be re-derived — the service may be
healthy while a push failed); otherwise the collector re-derives
Waiting/Active from a read-only fetch. See ADR-0001 §5.2 for the two
recorded status decisions.

## The pattern

The charm runs [fetch → compute → apply][pattern] with decisions as
data: a frozen state union (`ContainerNotReady | ContainerReady`), a
frozen outcome ADT (`PushCerts | RemoveCerts | PushNginxConfig |
PushCatalogueConfig | EnsureLayer | RestartNginx | Noop`), exhaustive
`match`es with `assert_never`, and no function that both performs an
effect and returns a decision flag. [Workload constraints][lie-catalog]
records which Kubernetes/Pebble signals lie and what the charm does
about each.

[pattern]: pattern.md
[lie-catalog]: workload-constraints.md

## Tests

Three layers, mirroring the source split (`charm-testing` skill):

1. **Pure core** — plain pytest, zero mocks: construct dataclasses,
   compare with `==`.
2. **Charm** (`ops.testing` Scenario): events in, `State` out; the
   workload module may be patched at its seam, never the container.
   Assertions are on *artifacts* (pushed config.json, certs on disk) —
   the status collector reports Active for any connectable container,
   so status-only assertions prove nothing about routing.
3. **Workload** — a hand-rolled `FakeContainer` at the constructor
   seam, lying the way Pebble lies (`PathError("not-found")` from
   pull, a service that stays down after restart).

## Gates

```
tox -e fmt         # ruff format + check --fix-only (src, tests)
tox -e lint        # ruff check
tox -e static      # pyright strict (src, tests) + standard (owned lib) + LIBPATCH gate
tox -e unit        # pytest under coverage, fail_under = 90 (src only)
tox -e flaplint    # advisory: relation-databag ordering churn
```

Where to go deeper: [`docs/roadmap.md`](roadmap.md) (what landed,
when), [`docs/pattern.md`](pattern.md) (the pattern taught small),
[ADR-0001](adr/0001-fetch-compute-apply.md),
[ADR-0002](adr/0002-toolchain-and-repo-architecture.md).