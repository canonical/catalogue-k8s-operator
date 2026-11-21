---
name: charm-relations
description: >-
  Use when adding, changing, or reviewing a relation endpoint in charmcraft.yaml
  — choosing an interface, deciding which library owns it, optionality, limits,
  databag schemas, and failure modes. Also covers which charm libraries are
  recommended versus deprecated in 2026. Load before writing provides/requires.
metadata:
  verified: "2026-11-20"
---

# Relations

## Decide before you add

Work through this checklist and write the answers into the PR description. If you
cannot answer one, the relation is not ready.

```markdown
# Interface: <interface-name>

## Role
provides | requires | peers — is this charm the producer or the consumer?

## Existing library
Which published library already implements this? Check the official interface
library index before writing anything. If none exists, why does this interface
need to exist, and which repo owns the canonical implementation?

## Optionality
Can the charm reach ActiveStatus without this relation? If yes -> optional: true.
If no -> write the exact BlockedStatus message that tells the operator what to do.

## Limit
limit: 1 or unbounded? The default is unbounded. Be explicit.

## Databag schema
Pydantic model. App-level or unit-level? Versioned? Additions must stay
backwards-compatible within a major version.

## Failure modes
What happens on relation-broken? On relation-changed mid-rotation? Cross-model?
Across a charm upgrade? Does _reconcile cope with this relation appearing and
disappearing on every single event?
```

Default answer to optionality for this charm: **`optional: true`**. Catalogue
serves plain HTTP via nginx and must reach `ActiveStatus` with zero relations.
Anything else needs an argument.

Note that `charmcraft.yaml` today does not uniformly reflect this: `tracing`,
`receive-ca-cert`, and `logging` carry `optional: true`, but `ingress`,
`certificates`, `catalogue-item`, `service-mesh`, and `require-cmr-mesh` do not.
Whether each of those is deliberate (TLS and ingress genuinely required for this
deployment's intended use) or residual is a question for the checklist above and
for `charm-reviewer`, not something this skill decides for you — see the next
section for why the YAML key alone is not the answer either way.

## `optional` and `limit` are not the same kind of key

This distinction matters and is easy to get wrong.

**`optional` is not enforced by Juju.** The reference says so verbatim: *"Not
enforced by Juju, but used by other tools and should always be included."* The
field is parsed and persisted (`charm.Relation.Optional` in the Go source) but
appears in no decision path. It is documentation for Charmhub, `charmcraft`,
linters, and humans.

So `optional: true` does **nothing** operationally. The guarantee that the charm
comes up clean with zero relations lives entirely in `_reconcile` (today,
`_configure` and its per-event callers) and `collect_unit_status`. The YAML
states the intent; the code has to keep it. Do not treat a correct
`charmcraft.yaml` as evidence — see the previous section's `certificates`
example.

Note the inverted default: `optional` defaults to `false`, meaning "required". The
reference's own best practice is *"Include the `optional` key in all endpoint
definitions, rather than relying on the default value"* — which is why it is a
non-negotiable here.

**`limit` *is* enforced by Juju, on all three roles.** `AddRelation` calls
`enforceMaxRelationLimit` and returns `QuotaLimitExceeded`. The default is `0`,
meaning unbounded. This charm already uses `limit: 1` on `ingress`,
`certificates`, `tracing`, `receive-ca-cert`, and `service-mesh` — all deliberate
single-relation endpoints.

More importantly, `limit` is **not safely reversible**. There is a
`preUpgradeRelationLimitCheck` in Juju: if you publish a revision that adds
`limit: 1` to an endpoint where a user already has two relations, **you break their
`juju refresh`**. `optional` is free to add or change; `limit` is a compatibility
commitment. Decide it once, at the start.

`limit` is valid in `provides`, `requires`, and `peers` — not only `requires`.

## `scope` and the implicit `juju-info` endpoint

`scope` is `global` (default) or `container`. `scope: container` goes on the
**subordinate** charm's `requires` side. A subordinate is invalid without at least
one `requires` endpoint with container scope — Juju validates this.

Catalogue is the principal, so it declares no scope. But be aware that **every
application implicitly provides a `juju-info` endpoint** (role `provides`,
interface `juju-info`, scope `global`) that cannot be declared or removed, and does
not appear in `juju info`. It exists so subordinates can attach to a principal that
exposes nothing else suitable.

Ambiguity is not limited to the subordinate case, though. This charm already
declares **two endpoints that share the same interface name**: `provides.catalogue`
and `requires.catalogue-item` are both interface `catalogue` — one role lets other
charms register into this catalogue, the other lets this catalogue register into
someone else's. `juju integrate catalogue-k8s another-catalogue-k8s` with no
endpoint names gives Juju two interface-compatible pairs to choose from. **In
integration tests and any deployment tooling, always name the endpoints
explicitly** — `juju integrate catalogue-k8s:catalogue-item
another-catalogue-k8s:catalogue`, not the bare application names.

## Library ecosystem status (2026)

Charmhub-hosted libraries — the single-file modules under `lib/charms/.../vN/`
fetched with `charmcraft fetch-libs` — are being retired
([deprecation explanation](https://documentation.ubuntu.com/charmlibs/explanation/charmhub-libraries-deprecation/)):

> Charmhub-hosted charm libraries [...] are being phased out in favour of standard
> Python packages distributed on PyPI.

Official timeline: charmcraft emits deprecation warnings (current state) → Charmhub
disables uploading **new** libraries (26.10 cycle) → Charmhub disables updates to
existing ones.

**Consequences for this repo:**

- This repo is the documented exception, not the rule: `lib/charms/catalogue_k8s/`
  is both consumed *and* published by this repo for other charms to fetch — every
  edit must bump `LIBAPI`/`LIBPATCH`, and the `static` tox env checks this (see
  `AGENTS.md`'s *Toolchain* and *Ecosystem facts* sections). Do not create any
  *other* `lib/charms/.../v0/*.py` for code this repo owns outside that one
  library.
- Consuming an existing Charmhub library is still fine where no PyPI replacement
  exists. `charm-libs:` in `charmcraft.yaml` is the mechanism, and it is *only*
  for those.
- Everything from PyPI goes in `pyproject.toml`.

Authoritative indexes, worth re-checking rather than trusting memory:

- [general libraries](https://documentation.ubuntu.com/charmlibs/reference/general-libs/)
- [interface libraries](https://documentation.ubuntu.com/charmlibs/reference/interface-libs/)

Badges: ✅ recommended, 🚫 deprecated, no badge = neither.

**Re-checked against that index on 2026-11-20 for this repo's vendored
libraries, two results are worth flagging:** `tls_certificates_interface.
tls_certificates` (what `charms.tls_certificates_interface.v4.tls_certificates`
is) now shows 🚫, "Deprecated in favor of `charmlibs.interfaces.tls_certificates`"
(which is ✅). And `istio_beacon_k8s.service_mesh` (what `ServiceMeshConsumer`
is) shows 🚫, "Deprecated in favour of `charmlibs.interfaces.service_mesh`"
(also ✅). Neither vendored copy is broken today — this is a migration to plan
via an ADR (`new-adr`), not a defect to fix reflexively.

## What this charm plausibly needs

| Interface | Role | Library | Status | Notes |
|---|---|---|---|---|
| `catalogue` | provides | `charms.catalogue_k8s.v1.catalogue` (`CatalogueProvider`) | Charmhub, no badge — owned & published by this repo | The one library this repo both consumes and publishes. See the exception above. |
| `catalogue` (declared as `catalogue-item`) | requires | `charms.catalogue_k8s.v1.catalogue` (`CatalogueConsumer`) | same library, same status | Same vendored file backs both roles — this repo is simultaneously the only producer and a consumer of its own interface. |
| `ingress` | requires, `limit: 1` | `charms.traefik_k8s.v2.ingress` (`IngressPerAppRequirer`) | Charmhub, no badge | No PyPI replacement found in the interface index as of this check — re-check before assuming otherwise. |
| `tls-certificates` | requires, `limit: 1` | `charms.tls_certificates_interface.v4.tls_certificates` | 🚫 deprecated, in favour of `charmlibs.interfaces.tls_certificates` (PyPI, ✅) | Vendored copy still works; migrating is a real decision, not a drop-in swap — raise an ADR first. |
| `tracing` | requires, optional, `limit: 1` | `ops_tracing.Tracing` (`ops[tracing]`, PyPI) | ✅ — already past the Charmhub-library stage for this relation | `tempo_coordinator_k8s` is also vendored under `lib/charms/`, but check its import sites before assuming it also owns this relation — it does not, here. |
| `service_mesh` (and, indirectly, `provide-cmr-mesh` / `require-cmr-mesh`) | requires `limit: 1`; provides + requires | `charms.istio_beacon_k8s.v0.service_mesh` (`ServiceMeshConsumer`) | 🚫 deprecated, in favour of `charmlibs.interfaces.service_mesh` (PyPI, ✅) | `ServiceMeshConsumer(self)` manages the `cross_model_mesh` databags too — grep the library's source before assuming a declared endpoint with no matching import is unimplemented. |
| `loki_push_api` (declared as `logging`) | requires, optional | `charms.loki_k8s.v1.loki_push_api` (`LogForwarder`) | Charmhub, no badge | |
| `certificate_transfer` (declared as `receive-ca-cert`) | requires, optional, `limit: 1` | none vendored here | — | A Charmhub library exists upstream (`certificate_transfer_interface.certificate_transfer`, no badge) but nothing under `lib/charms/` or in `src/charm.py` consumes it yet. Check `docs/roadmap.md` before treating this as a defect rather than unstarted work. |
| `catalogue_replica` | peers | none | — | Needed once multiple units must agree on anything the nginx container and its config can't just recompute independently. |

## Declaring endpoints

```yaml
requires:
  ingress:
    interface: ingress
    limit: 1
  certificates:
    interface: tls-certificates
    limit: 1

peers:
  replicas:
    interface: catalogue_replica
```

## Databags

Use pydantic for anything crossing a relation, in both directions. Never write raw
strings into a databag and never `json.loads` a databag without a model. Reach
for this only where no vendored library already owns the wire format — the
`catalogue`, `ingress`, `tls-certificates`, `tracing`, `loki_push_api`, and
`service_mesh` relations above are all already owned by a library; hand-rolling
a parallel schema for one of those is a sign something is about to be duplicated,
not that a gap was found.

**`ops` has native support for this since 2.23 — prefer it over hand-rolling.**
`Relation.load(cls, src)` and `Relation.save(obj, dst)` take a pydantic model,
honour `Field(alias=...)`, and default to `json.loads`/`json.dumps` with
overridable `decoder`/`encoder`:

```python
class ExampleRelationData(pydantic.BaseModel):
    """Illustrative only — check the real interface schema before using field
    names like these literally for an interface with no vendored library.
    """

    source_app: str
    note: str


# write
relation.save(ExampleRelationData(source_app=self.app.name, note="..."), relation.data[self.app])

# read
data = relation.load(ExampleRelationData, relation.data[remote_app])
```

Only write a manual `dump`/`load` pair if a library forces a wire format that
`Relation.save` cannot produce. Note that the ops docstring for `Relation.load`
contains a bug — it calls `get_secret` positionally, which raises `TypeError`.

## Who can write what

This constrains every reconcile step that touches a databag:

| Relation kind | Unit databag | App databag |
|---|---|---|
| non-peer | each unit reads/writes its own; reads all remote | **leader only** |
| peer | each unit reads/writes its own; **reads all** unit databags | **leader only** |

Any step writing the app databag must be guarded by `self.unit.is_leader()`, or it
raises on non-leaders. And note `leader_elected` must be in the reconciler's event
list, or a newly elected leader never publishes.

## Reconciling relations

The reconciler runs on every event, including events unrelated to a relation.
Every relation step must therefore tolerate:

- the relation not existing at all
- the relation existing with an empty databag
- the relation existing with a databag from an older charm version
- being the non-leader

```python
def _reconcile_example_relation(self) -> None:
    """Publish our identity to every consumer. Safe with zero relations."""
    if not self.unit.is_leader():
        return
    data = ExampleRelationData(source_app=self.app.name, note="...")
    for relation in self.model.relations["example-endpoint"]:
        relation.save(data, relation.data[self.app])
```

No `if relation is None: return` guards at the top of `_reconcile` — iterate over
possibly-empty collections instead. A reconciler that bails early on a missing
relation stops converging everything downstream of it.

Two behaviours of `self.model.relations` worth knowing:

- **`self.model.relations["not-declared"]` raises `KeyError`**, it does not return
  `[]`. Only endpoints declared in `charmcraft.yaml` are valid keys. A declared
  endpoint with zero integrations *does* return `[]`, which is what "optional by
  default" relies on.
- **During `relation-broken`, the breaking relation is excluded from the list.**
  Your reconciler sees the world already without it. That is what you want, but it
  means you cannot inspect the departing relation from inside `_reconcile`.

For a single-relation endpoint, `self.model.get_relation("ingress")` returns
`Relation | None` and is more direct than indexing a list.
