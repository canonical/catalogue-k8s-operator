# catalogue-k8s

Juju **Kubernetes charm** that deploys the Catalogue web UI — a Pebble-managed
nginx container serving a static service directory — as part of the Canonical
Observability Stack.

This is not a machine charm. There are no snaps, no systemd, and no
`charmlibs-snap`/`charmlibs-apt`. Pebble is the workload API, and `lightkube` is
allowed where the Kubernetes API is genuinely needed.

## Read this first

1. [`docs/roadmap.md`](docs/roadmap.md) — what is built, what is unstarted, and
   the staged plan for adopting the fetch-compute-apply pattern.
2. The non-negotiables below — the contract that no gate fully checks for you.

`docs/overview.md` and `docs/pattern.md` (the pattern taught with a small
example that is not Catalogue) are deliverables of the refactor, recorded in
the roadmap. And the precedence rule for everything in this repo: **where a
skill and this file disagree, the skill is newer and wins — this file is then
the thing to fix.**

## Where we are

The charm works upstream: events funnel into a single `_reconcile`, which
builds intent, fetches running state via the `Workload` module, computes
outcomes purely in `catalogue_state.py`, and applies them. Status is
reported by `_on_collect_status` (observed for both `collect_unit_status`
and `collect_app_status`). The decide/act split landed 2026-11-21 (stage 1);
the single-reconciler collapse landed 2026-11-21 (stage 2). Not yet true:
the test-layer split (stage 3) and the docs/ADRs (stage 4).

The public interface — relations, config options, the `get-url` action — is
what `charmcraft.yaml` declares; that YAML is the list, and this file
deliberately does not restate it. Several relations carry `limit: 1`; `limit`
is enforced by Juju and permanent — adding one later breaks `juju refresh`
for existing users.

## Non-negotiables

Only rule 3 is machine-checked. **A green `tox -e lint,static,unit` is not
evidence of compliance with 1, 2, 4, 5, 6, 7, or 8.** Rule 5 in particular
looks checkable and is not: Juju ignores `optional`, and no tool reads it. All
of them are audited by `charm-reviewer`. Do not treat a passing gate as a
review.

1. **One reconciler.** Every observed event routes to a single `_reconcile`.
   Every reconcile step must be safe to run twice and safe to never run.
   The test for "deserves its own handler" is objective, from the ops docs:
   *an event that cannot be deferred needs a dedicated handler.* That set is
   exactly: actions, `stop`, `remove`, `secret_rotate`, `secret_remove`,
   `secret_expired`, and the `collect_*_status` lifecycle events. Everything
   deferrable — including `config_changed`, `upgrade_charm`, relation events,
   and Pebble events (defer until `can_connect()`) — goes through `_reconcile`.
   The one allowed exception is `upgrade_charm` *if* it needs migration logic
   distinct from convergence. The collapse landed 2026-11-21; new code must
   not add per-event handlers.
2. **Charm logic and workload logic are separate modules.** `src/charm.py`
   only observes events, maps config and relation data to intent, and reports
   status. Everything that touches the Pebble container — `push`/`pull`/
   `remove_path`, layers, `autostart`, `replan`, `restart`, `get_plan` — lives
   in the workload module and never imports `ops`. `src/catalogue_state.py`
   sits between them as the pure core and imports neither `ops` nor the
   workload; it reaches the container only through a `Facts` protocol.
   `src/nginx_config.py` renders nginx config and is already pure — it must
   stay that way. This is what makes unit tests possible: tests of `charm.py`
   mock the workload module, never the container. No linter checks this; if a
   test of `charm.py` patches `Container` or Pebble calls directly, the
   boundary has already broken. (Workload tests exercise the container
   simulation — that is their job.)
3. **No imports inside functions.** PEP 8 already says imports go at the top of
   the file; the reason it is restated here as absolute is charm-specific. A Juju
   hook runs once and exits, so an import inside a rarely-taken branch fails in
   production, in that branch, with a venv that differs from the one the tests
   ran against. An import at module top fails on the first hook instead.
   Enforced by `E402` **and `PLC0415`** — `E402` alone only catches late
   module-level imports, not function-level ones. `if TYPE_CHECKING:` blocks are
   at module top and therefore fine; if you need a function-level import to
   break a cycle, the `charm.py`/workload layering has been violated — fix that
   instead.
4. **Relations over config options.** A config option is a permanent public
   API: removing or renaming one breaks every existing deployment, the same
   class of irreversibility as `limit`. So before adding one, check the three
   alternatives in order — does another charm own this data (a relation)? is it
   network placement? is it deployment shape the operator sets outside the
   charm (constraints, placement, their own deployment tooling)? Config options
   are the residue, not the default. The existing options (`title`, `tagline`,
   `description`, `links`, `override_hostname`) are display content the
   operator owns — legitimate residue. Note that `links` arrives as a JSON
   string and is parsed at runtime: parsed input belongs to pydantic.
5. **Optional by default.** Every `requires`/`provides` entry gets
   `optional: true` unless the charm physically cannot reach `ActiveStatus`
   without it. The charm must come up clean with zero relations — Catalogue
   serves plain HTTP with none. Note that Juju does **not** enforce `optional` —
   it is documentation. The guarantee lives in `_reconcile` and
   `collect_unit_status`, so a correct `charmcraft.yaml` is not evidence.
6. **Never trust a success signal you did not verify.** The Kubernetes shape of
   the lie: `can_connect()` returns true long before nginx serves anything;
   `restart()` and `replan()` returning does not mean the service is healthy; a
   certificate "available" on the relation is not a file on disk — and the
   container filesystem is ephemeral, so certs pushed yesterday are gone after
   an upgrade (the existing `push_certs=True` on `pebble_ready` and
   `upgrade_charm` exists for exactly that reason — keep it); an ingress URL
   in a databag is not a reachable route. In every case, read back the state
   the operation was supposed to produce. The charm already pulls the running
   nginx and catalogue configs before deciding to restart — that is the right
   instinct; the refactor moves the comparison into the pure core so an exit
   code or a returned flag is never the evidence.
7. **Decide, then act — never both in one function.** A function that performs
   an effect *and* returns a flag describing what it decided cannot be tested
   without running the effect. Pure functions compute an outcome value; impure
   functions consume it. The detection signal is cheap: **if a test needs a
   mock to reach a decision, this rule was broken.** The existing
   `_update_pebble_layer`, `_update_catalogue_config`, and
   `_update_web_server_config` mutate *and* return changed-flags — exactly the
   shape the refactor eliminates: fetch the running state, compute outcomes
   purely, apply them.
8. **Inheritance only where a framework demands it.** `ops.CharmBase` is the
   one mandatory subclass; charm libraries (`CatalogueProvider`/
   `CatalogueConsumer`, `IngressPerAppRequirer`, `TLSCertificatesRequiresV4`,
   `ServiceMeshConsumer`, `LogForwarder`, `ops_tracing.Tracing`) are
   instantiated, never extended. Everything else is composition — but note the
   verified constraint: **constructor injection into the charm is
   impossible.** `ops` instantiates it as `charm_class(framework)` and
   `ops.testing.Context` takes a type, not a factory. So inject *below* the
   charm, and do not invent a factory indirection to work around it. Pass the
   narrowest collaborator a function needs, never the charm itself.

## Toolchain

`uv` + `tox` + `ruff` + `pyright`. No `pip`, no `poetry`, no `black`/`isort`/
`flake8`.

```
tox -e fmt         # ruff format + ruff check --fix
tox -e lint        # ruff check
tox -e static      # pyright (+ the upstream LIBPATCH bump check on lib/charms/catalogue_k8s)
tox -e unit        # pytest tests/unit
tox -e integration # pytest tests/integration (needs a juju k8s model)
tox -e lock        # regenerate uv.lock after changing pyproject.toml
tox -e flaplint    # advisory: relation-databag ordering churn. Not in envlist.
```

The config now enforces the conventions declared above: `E501`/`W505`
(99/72), `ANN`, `D`, `PLC0415`, and pyright strict at Python 3.14 over `src`
and `tests`. A green `tox -e fmt,lint,static,unit` is still not evidence of
compliance with rules 1, 2, 4, 5, 6, 7, or 8 — those are audited by
`charm-reviewer`, not by any gate. Run `flaplint` as well when the change
touches a databag write, a file write, or a hash.

`uv.lock` is committed. Dependencies go in `pyproject.toml`, never in
`charmcraft.yaml`'s `charm-libs` — that key is only for Charmhub-hosted
libraries, which is exactly what the vendored libraries under `lib/charms/`
are.

## Layout

```
charmcraft.yaml           # assumes k8s-api; containers: catalogue; platforms: ubuntu@26.04:amd64
pyproject.toml            # ops[tracing], lightkube, pydantic, cosl, ...
uv.lock
tox.ini
pyright-lib/              # pyright project for lib/charms/catalogue_k8s (standard mode)
docs/
  roadmap.md              # staged delivery plan (overview.md and pattern.md land with the refactor)
  adr/                    # numbered decision records; starts with the refactor. Load `new-adr` first.
src/
  charm.py                # CatalogueCharm: single _reconcile + _on_collect_status
  nginx_config.py         # pure nginx config rendering — no ops imports, must stay that way
  catalogue_state.py      # pure core: intent, state, outcome ADT, fetch/compute, status
  workload.py             # Pebble container operations; never imports ops
lib/charms/               # vendored Charmhub-hosted interface libraries
tests/
  unit/                   # ops.testing; charm tests mock the workload module
  integration/            # jubilant + pytest-jubilant on a k8s model
```

Repo-root siblings, out of scope for the pattern: `workload/` (the OCI image
source) and `terraform/` (the upstream terraform module).

**`src/` is on `PYTHONPATH`** (`tox.ini` sets it, as does the charm venv), so
imports are flat: `import nginx_config`, never `from src.nginx_config import
...`.

## Python conventions

Authority is [PEP 8](https://peps.python.org/pep-0008/) and
[PEP 257](https://peps.python.org/pep-0257/), enforced by `ruff`, typed by
`pyright`. The rule-family mapping and the rationale for each setting live in
the `python-style` skill. Target state (stage 0 aligns `pyproject.toml`):

- **PEP 8's 99-character exception, with its proviso.** Comments and
  docstrings stay wrapped at 72 — `E501` and `W505` both enabled.
- **Annotations and docstrings everywhere.** `ANN` and `D` are lint errors,
  not house preferences. `pyright` runs `typeCheckingMode = "strict"` over
  **both `src` and `tests`**.
- **Python 3.14, no fallback.** `requires-python` is already `~=3.14.0`; the
  stale pyright `pythonVersion = "3.8"` gets fixed in stage 0.
- **Only one suppression spelling works.** `# pyright: ignore[rule]` is the
  honoured form; mypy's `# type: ignore[...]` suppresses nothing, and a
  suppression that goes stale fails the gate.
- **Frozen dataclasses everywhere except exceptions.** `ops` assigns
  `exc.__traceback__` as the event context unwinds, so a frozen exception
  raises `FrozenInstanceError` and buries the real failure. Each exception
  module carries a guard test asserting its exceptions survive being raised.
- **Multi-type `except` clauses get an `as err:` binding.** `ruff format`
  rewrites them into PEP 758's unparenthesized form, which `flaplint`'s
  parser cannot read — it then skips the whole module without saying so.

Not machine-checked — the reviewer's job:

- **Prefer the functional style.** Frozen dataclasses, unions as ADTs,
  exhaustive `match` with `assert_never`, `Mapping`/`Sequence`/`FrozenSet` in
  signatures. Functional core, imperative shell. See `charm-functional-style`.
- `pydantic` for anything parsed or serialised: charm config (the `links`
  JSON string first), databags, anything crossing a boundary as text.
- Logging via `logging.getLogger(__name__)`, never `print`.
- No `except Exception:` where a narrower exception is what actually occurs —
  `except (ProtocolError, PathError, Exception)` is `except Exception` with
  extra steps; name what you can handle. This one is judgement.
- Tests use `# GIVEN / # WHEN / # THEN` comments and live in
  `conftest.py`-backed fixtures rather than per-file setup boilerplate.
- **Docstrings say what; ADRs say why.** Cite the ADR (`See ADR-0001
  section 2`) instead of paraphrasing it: a docstring that restates an ADR
  goes stale and then wins by proximity. Keep a rationale inline only where a
  reader would otherwise plausibly "fix" the code, and then as one sentence.
- **`# databag-order: ignore` suppresses one `flaplint` finding on one
  line.** It is legitimate only where the nondeterminism is the point and
  cannot flap. A suppression on a line that runs on every reconcile is a
  defect being silenced — fix the ordering instead.

## Ecosystem facts that bite (2026)

- Charmhub-hosted charm libraries are being phased out in favour of PyPI
  packages. New code this repo owns goes in `src/`, never `lib/charms/`. The
  vendored libraries under `lib/charms/` are shared, Charmhub-hosted interface
  libraries — fetched with `charmcraft fetch-libs`, never edited, never
  linted. The one exception is `lib/charms/catalogue_k8s`: this repo owns and
  publishes it for other charms to consume, so it *is* edited here — and
  every edit must bump `LIBAPI`/`LIBPATCH` (the `static` tox env checks
  this).
- Pebble is the workload API here. The machine-charm rule "no Pebble, no
  lightkube" does not apply — it is inverted: Pebble is the only interface to
  the container, and `lightkube` is allowed where the Kubernetes API is
  genuinely needed.
- Catalogue is itself part of COS: it *provides* the `catalogue` interface
  that other charms use to register their UIs, and consumes `loki_push_api`,
  `tracing`, per-app `ingress`, `tls-certificates` v4, and the service-mesh
  interfaces.

## Agents and skills in this repo

Three agents, three jobs. Design decisions go to `charm-architect`,
implementation to `charm-engineer`, and audits to `charm-reviewer`
(read-only). Research delegates to `explore` (this repo) and `general` (the
upstream references), both on a cheaper model.

**Load the relevant skill instead of guessing.** The skill set is a port of
the pi-hole-operator set, filtered in three layers: pattern-core skills
(`charm-functional-style`, `new-adr`, `python-style`, `charm-relations`,
`charm-testing`) adapted to Kubernetes; machine-charm and snap-specific
skills dropped; k8s workload and scaffold skills (`charm-k8s-workload`,
`charm-k8s-scaffold`) written fresh.

Decisions live in `docs/adr/`, numbered and dated, starting with ADR-0001
(adopting fetch-compute-apply). Load `new-adr` before adding or revising one.

**Never put a count in prose.** "Nine options", "six facts" — a count is
correct for exactly as long as the next commit takes. Describe the shape and
name where the list lives: the YAML, the ADR, the tree. The same instinct is
why docs cite an ADR section instead of paraphrasing it.
