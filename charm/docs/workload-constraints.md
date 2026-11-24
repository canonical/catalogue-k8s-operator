# Workload constraints — the Kubernetes lies catalog

Verified facts about this charm's workload. True regardless of what we
decide — decisions live in `docs/adr/`, sequencing in `docs/roadmap.md`.
Everything here was verified against `ops` 3.10.0 / `ops-scenario`
8.10.0 (the locked versions) unless marked otherwise.

## The lies — what success signals do not mean

### §1 Pebble is the workload API, `can_connect()` is point-in-time

There is no snap, no systemd unit, no file on the charm's own
filesystem. `ops.model.Container` is the only interface to the
`catalogue` container.

`can_connect()` is a point-in-time check. The ops docstring warns it
*"does not guard against the Pebble API becoming unavailable, and
should be treated as a 'point in time' status only"*, and the how-to
guide is blunter: guarding later Pebble operations with it *"introduces
a race condition where Pebble might be responsive when `can_connect()`
is called, but is not when the later operation is executed"*.

Consequence: guard nothing with it; wrap Pebble operations in
`try/except ops.pebble.Error`. Both the reconciler and the status
collector do.

### §2 `restart()` / `replan()` returning is not health

The method returning means Pebble accepted the change, not that nginx
answers. Read `get_services(name)[name].is_running()` back before
believing a restart. `src/workload.py::restart` does this and raises
`ServiceNotRunningError` when the service stays down.

### §3 The container filesystem is ephemeral

Anything pushed — certs especially — is gone when the container
restarts or the charm upgrades. Pushed files are cache, not state: the
reconcile re-pushes whenever the fetched state does not show them.
Never cache "pushed" in charm state across hooks (also why
`ops.StoredState` is avoided: it duplicates state that already lives
in Juju config, relation data, and the container).

### §4 A certificate "available" on the relation is not a file on disk

The `certificates` relation hands the charm PEM strings; until they are
pushed and read back, nginx has no TLS. Combined with §3: after a
container restart the relation can say *available* while the disk
says *gone* — the reconcile decides from the disk.

### §5 An ingress URL in a databag is not a reachable route

Traefik publishes the URL when its side is configured; DNS and the
route catch up later. Never assert reachability from relation data
alone; the `get-url` action reports it, it does not promise it.

### §6 `pull()` raises `pebble.PathError("not-found")`, not `FileNotFoundError`

A missing file inside the container surfaces as
`ops.pebble.PathError` with `kind="not-found"`. The builtin
`FileNotFoundError` is what a missing *socket* surfaces as
(`can_connect()` already handles that). Consequence:
`workload.py` checks `exists()` before pulling rather than naming the
pebble exception class — which it cannot import, because the module
layering pins `ops` to `TYPE_CHECKING` (ADR-0001 §5.1).

## Verified API facts

### §7 `add_layer` takes the whole layer mapping

`Container.add_layer(label, layer, *, combine=False)` accepts a
YAML string, a `pebble.LayerDict`, or a `pebble.Layer` — but
`Layer.__init__` reads **only the `services` key** of the mapping.
Passing a services mapping `{"catalogue": {...}}` as the layer
silently defines an *empty* layer: `Layer.to_dict()` drops the
unknown key, `autostart()` starts nothing, and the unit sits Blocked
on every reconcile. The layer dict must be
`{"services": {...}}` — `src/workload.py::ensure_layer` wraps it.
This exact bug shipped to a green gate once; the regression
assertion is in `tests/unit/test_charm.py` (funnel + the
Blocked-vs-Active artifact assertions).

### §8 Status collection fires at the end of every hook

`ops/_main.py` emits `collect_unit_status` (every unit) and
`collect_app_status` (**leader only**) after each event dispatch.
Both events are `CollectStatusEvent`; there is no
`CollectAppStatusEvent` class. A status handler therefore runs even
in hooks whose event handler did nothing (library-owned relation
events, actions) — it must be cheap, read-only, and must never raise
into error state.

### §9 Scenario does not persist the container filesystem between runs

Verified empirically against `ops-scenario` 8.10.0: a file pushed in
one `ctx.run()` is absent in the next, and `ops.testing.Container`
takes no `files` parameter — pre-seeding the filesystem is not
possible. Host-backed `scenario.state.Mount(location=..., source=...)`
(mounts are a named `Mapping[str, Mount]` on the Container) is the
only way to carry files across runs — used by
`test_certificate_relation_removed_cleans_certs` to observe the
`RemoveCerts` outcome for real.

Consequence: the *converged* state (running config == intent, no
outcomes) is not representable end-to-end at the charm test layer —
every Scenario run sees an empty filesystem and always pushes. The
comparison logic lives in the pure core (`catalogue_state.compute`),
where converged, empty, and stale states are all constructible by
hand. That is the layering working as designed, not a gap.

### §10 Vendored libraries are not linted, one of them is published

`lib/charms/` holds Charmhub-hosted interface libraries fetched with
`charmcraft fetch-libs` — never edited, never linted — except
`lib/charms/catalogue_k8s`, which this repo owns and publishes: every
edit there must bump `LIBAPI`/`LIBPATCH`, enforced by `tox -e static`.
Style-only edits would force version churn on a published library, so
ruff's scope is `src/` and `tests/` only (ADR-0002).