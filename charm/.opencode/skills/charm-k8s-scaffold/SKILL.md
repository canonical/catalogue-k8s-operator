---
name: charm-k8s-scaffold
description: >-
  Use when creating or editing charmcraft.yaml, pyproject.toml, tox.ini, or
  deciding the repo layout for this Kubernetes charm. Covers the
  containers/resources (oci-image) sidecar wiring, platforms syntax, the uv
  part plugin, config options, actions (including additionalProperties), and
  where dependencies belong. Load before hand-writing any of those files.
metadata:
  verified: "2026-11-20"
---

# Kubernetes charm scaffolding

Reference implementation: `canonical/operator` → `examples/k8s-5-observe/`,
the fullest sidecar example in the `ops` reference — containers, an
oci-image resource, config, `requires`/`provides`, and `charm-libs` together
in one file. Read it before inventing structure.

Everything quoted below — `charmcraft.yaml`, `pyproject.toml`, `tox.ini`, the
layout tree — is a **dated snapshot** (see the frontmatter) that shows the
shape. The live files are the authority: read them before editing, and never
restore anything from these blocks.

## `charmcraft.yaml`

`bases:` is **deprecated**. Use `base:` plus `platforms:`.
([charmcraft.yaml reference](https://canonical.com/juju/docs/charmcraft/stable/reference/files/charmcraft-yaml-file/))
This charm's real file goes one step further and skips the separate `base:`
key entirely, using the long-form platform key directly:

```yaml
platforms:
  ubuntu@26.04:amd64:
```

That is the multi-base notation the reference reserves for supporting more
than one series at once, used here for a single platform — functionally
equivalent to `base: ubuntu@26.04` + `platforms: {amd64:}`, just spelled out.
Do not "simplify" it back to the shorthand without checking whether a second
platform was the reason it was written this way.

```yaml
name: catalogue-k8s
type: charm
summary: Catalogue for discovering charm UIs
description: |
  Catalogue K8s is a Juju charm that deploys a web-based service directory on
  Kubernetes, providing a central landing page for discovering and
  navigating to the user interfaces of other charms deployed alongside it.
  It is part of the Canonical Observability Stack (COS).

links:
  documentation: https://documentation.ubuntu.com/observability/latest/
  website: https://charmhub.io/catalogue-k8s
  source: https://github.com/canonical/catalogue-k8s-operator
  issues: https://github.com/canonical/catalogue-k8s-operator/issues

assumes:
  - k8s-api
  - juju >= 3.6

platforms:
  ubuntu@26.04:amd64:

parts:
  charm:
    source: .
    plugin: uv
    build-packages: [git]
    build-snaps: [astral-uv]

containers:
  catalogue:
    resource: catalogue-image

resources:
  catalogue-image:
    type: oci-image
    description: OCI image for the service catalogue
    upstream-source: ghcr.io/canonical/catalogue-k8s-operator:0.15

config:
  options: {}

provides: {}
requires: {}
peers: {}
actions: {}
```

That is the shape, trimmed; `charmcraft.yaml` in this repo is the list for
every relation, config option, and action — this skill does not restate it.

Notes:

- The `platforms:` shorthand (`amd64:` with a null value) expands to
  `build-on: [amd64], build-for: [amd64]`.
  ([platforms reference](https://canonical.com/juju/docs/charmcraft/stable/reference/platforms/))
- Supported bases today: `ubuntu@22.04`, `ubuntu@24.04`, `ubuntu@24.10`,
  `ubuntu@25.04`, `ubuntu@25.10`, `ubuntu@26.04`, `almalinux@9`.
- **`parts:` is not optional in practice.** Omit it and charmcraft applies
  the legacy `charm` plugin, which builds from `requirements.txt` — not what
  we want with `uv`. The available plugins are `charm` (legacy default),
  `python`, `poetry`, and `uv`.
- `UV_FROZEN` defaults to `true` in the `uv` plugin, so `uv.lock` **must**
  exist and is the only source of truth.
- The `description` is rendered on Charmhub. Treat it as a product page: lead
  with what the workload does, then bullet the charm's features. `summary`
  must be 78 characters or fewer.
- `charm-libs:` is **only** for Charmhub-hosted libraries — the reference
  says so verbatim. Regular PyPI packages go in `pyproject.toml`. This repo's
  twist: most vendored libraries under `lib/charms/` (`istio_beacon_k8s`,
  `loki_k8s`, `observability_libs`, `tempo_coordinator_k8s`,
  `tls_certificates_interface`, `traefik_k8s`) are consumed, never edited —
  but `lib/charms/catalogue_k8s` is **owned and published by this repo**, so
  it *is* edited here, and every edit must bump `LIBAPI`/`LIBPATCH` (`tox -e
  static` checks this — see the `tox.ini` section below). `version` must be a
  **string** (`"0"`, not `0`). Note that unlike the `charm` plugin, **the
  `uv` plugin does not install transitive `PYDEPS` of Charmhub libraries** —
  add them to `pyproject.toml` manually.

### Rules that still apply on `ubuntu@26.04`

- **Never put `/` in a part name.** Forbidden on 26.04 and later bases. Use a
  hyphen.
- **Never switch to the `charm` plugin.** It **does not exist** on the 26.04
  base. `uv` is the only forward-compatible choice, which we already use.
- **Python 3.14, no fallback.** 26.04 ships Python 3.14 and **no other
  Python in the archive**. `requires-python = "~=3.14.0"`, `ruff
  target-version = "py314"`, `pyright pythonVersion = "3.14"` — the last one
  is stale in the live `pyproject.toml` today; see the `pyproject.toml`
  section below and `docs/roadmap.md` stage 0.
- **Do not use anything that exists only in 3.12 or only in 3.14.** The charm
  runs on exactly one interpreter, so code that works on one but not the
  other is broken in production.

`charmcraft init --profile kubernetes` generates this shape, including the
`uv` plugin, the `astral-uv` build-snap, and a single `containers:` /
`resources:` pair to adapt.

## Containers and resources — the sidecar pattern

A machine charm forbids `containers:` and `resources: {type: oci-image}`
outright, because there is no container to run one in. **A Kubernetes charm
is the opposite: `containers:` is required** (except for proxy charms that
run on Kubernetes but manage no workload container of their own), and each
container needs a matching `oci-image` resource:

```yaml
containers:
  catalogue:
    resource: catalogue-image

resources:
  catalogue-image:
    type: oci-image
    description: OCI image for the service catalogue
    upstream-source: ghcr.io/canonical/catalogue-k8s-operator:0.15
```

`upstream-source` is **ignored by both Charmcraft and Juju** — it exists
purely as a convention so integration tests (and humans) can read the
default image out of `charmcraft.yaml`'s rendered metadata instead of
hard-coding it twice. `tests/integration/test_charm.py` reading
`METADATA["resources"]["catalogue-image"]["upstream-source"]` is the pattern
(see `k8s-5-observe/tests/integration/test_charm.py` for the reference
shape) — do not delete `upstream-source` thinking it is decorative to Juju;
it is decorative to Juju and load-bearing to the test suite.

Pebble, reached through the `ops.Container` API, is the **only** interface
to the `catalogue` container — there is no snap, no systemd unit, no file on
the charm's own filesystem to manage (AGENTS.md non-negotiable 2; see
`charm-k8s-workload` for the Pebble-specific detail). `lightkube` is allowed in this charm where the Kubernetes API is
genuinely needed, and it already appears in `pyproject.toml` because the
service-mesh and ingress libraries need it. That does **not** make a new,
direct Kubernetes API call from `src/charm.py` or the workload module free:
adding a dependency on the Kubernetes API beyond what the vendored libraries
already do is a design decision, not a detail — write it up with `new-adr`
before reaching for `lightkube` directly.

## `pyproject.toml`

Dependencies go here, never in `charmcraft.yaml`'s `charm-libs:` — that key
is for Charmhub-hosted libraries only, and the libraries this charm consumes
from PyPI (`lightkube`, `pydantic`, `cosl`, ...) are not among them. The real
file today:

```toml
[project]
name = "catalogue-k8s"
version = "0.0"
requires-python = "~=3.14.0"

dependencies = [
  "ops[tracing]>=2.20.0",
  "lightkube",
  "lightkube-models",
  "jsonschema",
  "cryptography",
  "cosl",
  "pydantic>=2",
  "charmed-service-mesh-helpers>=0.2.0",
  "lightkube-extensions",
]

[project.optional-dependencies]
dev = [
  "ruff",
  "codespell",
  "pyright",
  "pytest",
  "coverage[toml]",
  "jubilant>=1.8,<2",
  "pytest-jubilant>=2,<3",
  # ...
]
```

`pyproject.toml` is the list for the exact dependency set; the shape worth
noting is that `ops[tracing]`, the Kubernetes client trio
(`lightkube`/`lightkube-models`/`lightkube-extensions`), and the
service-mesh helper library are all runtime dependencies because the charm
libraries it instantiates need them — not because `charm.py` reaches for
them directly.

**Dev dependencies live in `[project.optional-dependencies] dev = [...]`**,
not in a `[dependency-groups]` table. `tox.ini` consumes this via
`uv_flags = --frozen --isolated --extra=dev` rather than the
`uv-venv-lock-runner` + `dependency_groups` combination — both are valid uv
patterns; this repo uses the extras-based one. `uv.lock` is committed;
regenerate with `tox -e lock`, never edit by hand.

**The lint/static sections below are stale, not a model to copy.** AGENTS.md
and `docs/roadmap.md` stage 0 both call this out explicitly — read those
before touching this file, not this snapshot:

```toml
[tool.black]
line-length = 99
target-version = ["py38"]

[tool.ruff.lint]
select = ["E", "W", "F", "C", "N", "R", "D", "I001"]
ignore = ["E501", "D107"]

[tool.pyright]
pythonVersion = "3.8"
```

Every one of those lines is wrong for where this charm is going:
`[tool.black]` should not exist at all (`AGENTS.md` Toolchain: "no
`black`/`isort`/`flake8`" — `ruff format` replaces it); `E501` must be
enabled together with `W505` at 72 characters for comments/docstrings, not
ignored (the PEP 8 exception is granted on that condition, not for free);
`ANN`, `D`, and `PLC0415` are missing from `select` and must be added;
`pythonVersion` must be `"3.14"`, matching `requires-python`, and
`typeCheckingMode` must be `"strict"` over **both** `src` and `tests`. See
the `python-style` skill for the rule-family mapping and rationale, and
`docs/roadmap.md` stage 0 for the acceptance criterion
(`tox -e fmt,lint,static,unit` green on the tightened config).

## `tox.ini`

```ini
[tox]
skipsdist = True
skip_missing_interpreters = True
envlist = lint, static, unit

[vars]
src_path = {toxinidir}/src
tst_path = {toxinidir}/tests
lib_path = {toxinidir}/lib/charms/catalogue_k8s
all_path = {[vars]src_path} {[vars]tst_path} {[vars]lib_path}
uv_flags = --frozen --isolated --extra=dev

[testenv]
allowlist_externals = uv
setenv =
  PYTHONPATH = {toxinidir}:{toxinidir}/lib:{[vars]src_path}

[testenv:fmt]
commands =
    uv run {[vars]uv_flags} ruff check --fix-only {[vars]all_path}

[testenv:lint]
commands =
    uv run {[vars]uv_flags} ruff check {[vars]all_path}

[testenv:static]
commands =
    uv run {[vars]uv_flags} pyright {[vars]all_path}
    # ... plus the LIBPATCH/LIBAPI bump check, see below

[testenv:unit]
commands =
    uv run {[vars]uv_flags} coverage run --source={[vars]src_path},{[vars]lib_path} \
        -m pytest {[vars]tst_path}/unit {posargs}
    uv run {[vars]uv_flags} coverage report

[testenv:integration]
commands =
    uv run {[vars]uv_flags} pytest --exitfirst {[vars]tst_path}/integration {posargs}

[testenv:lock]
commands =
  uv lock --upgrade --no-cache
```

Two things worth knowing before editing this file:

- **`envlist` omits `fmt`.** The `fmt` env exists and runs `ruff check
  --fix-only`, but a bare `tox` invocation does not format anything — `tox
  -e fmt` must be named explicitly. Roadmap stage 0 also adds `ruff format`
  to that command; today it only auto-fixes lint findings, it does not
  reformat.
- **`tox -e static` also enforces the vendored-library version bump.** It
  shells out to a loop over `git diff main --name-only` on
  `lib/charms/catalogue_k8s` and fails if a changed file's diff does not
  contain `+LIBPATCH` or `+LIBAPI`. This is the mechanism behind AGENTS.md's
  "every edit must bump `LIBAPI`/`LIBPATCH`" rule for that one vendored
  library this repo owns — it is not advisory, it is in the gate.
- `work_dir` is not set today, unlike the pihole-operator `tox.ini`. Roadmap
  stage 0 adds one outside the tree, for the same reason it exists there:
  tox builds its venvs with symlinks, and the half-built `.tox/` it leaves
  behind on failure would otherwise land in `charmcraft pack`'s build
  context.

`flaplint` is deliberately **not** in `envlist`. It catches a defect class
ruff and pyright cannot see, but it is a single-maintainer project with no
PyPI release — see `python-style` for the full risk assessment. Run it
explicitly when a change touches a databag write, a file write, or a hash.

## Layout

`AGENTS.md`'s Layout section is the list; the shape it describes:

```
charmcraft.yaml           # assumes k8s-api; containers: catalogue; platforms: ubuntu@26.04:amd64
pyproject.toml            # ops[tracing], lightkube, pydantic, cosl, ...
uv.lock
tox.ini
docs/
  roadmap.md
  adr/
src/
  charm.py                # CatalogueCharm: today per-event handlers -> _reconcile; target: single _reconcile + collect_unit_status
  nginx_config.py         # pure nginx config rendering — no ops imports
  catalogue_state.py      # pure core: intent, state, outcome ADT, fetch/compute
  workload.py             # Pebble container operations; never imports ops
lib/charms/               # vendored Charmhub-hosted interface libraries
tests/
  unit/
  integration/
```

Repo-root siblings `workload/` (the OCI image source) and `terraform/` (the
upstream terraform module) sit next to this `charm/` directory and are out
of scope for anything in `src/`. **`src/` is on `PYTHONPATH`** (`tox.ini`
sets it, as does the charm venv), so imports are flat: `import
nginx_config`, never `from src.nginx_config import ...`.

Two files the machine-charm layout carries that this repo currently lacks:
no `icon.svg`, no `.jujuignore`. Neither is required by charmcraft, but a
`.jujuignore` (same purpose as `.gitignore`, scoped to what `charmcraft
pack` excludes from the packed charm — `/venv`, `/.tox`, `/tests`, caches)
is worth adding if the pack ever starts picking up dev-only files; it is not
charm-type-specific, so there is nothing to invert here, only a gap to
notice.

## Config options

Every option needs a `description` and, where meaningful, a `default`.
Types: `string`, `int`, `float`, `boolean`, `secret` — same set regardless
of charm type. The real options today:

```yaml
config:
  options:
    title:
      type: string
      description: Title to display in the navigation bar.
      default: Service Catalogue
    tagline:
      type: string
      description: A one-liner that communicates the core value or purpose.
      default: Easy access to Charmed UIs
    description:
      type: string
      description: A longer description of the bundle the catalogue displays.
    links:
      type: string
      description: >-
        JSON string containing the links to display in the catalogue.
      default: |
        [{"category": "Documentation", "items": [...]}]
    override_hostname:
      type: string
      description: >-
        Replaces the base hostname for Catalogue items coming from relation
        data. Only works with path routing, not subdomain routing.
```

These five are display content the operator owns — legitimate residue after
the AGENTS.md rule 4 test: does another charm own this data (→ relation)?
is it network placement? is it deployment shape the operator sets outside
the charm? `links` is the one to note specifically: it arrives as a JSON
string, not structured YAML, so it is parsed at runtime — that parsing
belongs to `pydantic`, not hand-rolled `json.loads` plus manual validation.

For `type: secret`, the value is a secret URI; the charm calls
`self.model.get_secret(id=...)` and must observe `secret_changed`.

## Actions

Actions are the escape hatch for imperative operations that do not belong in
the reconciler. **Always set `additionalProperties` explicitly** — the
default differs between Juju 3 (`true`) and Juju 4 (`false`), so omitting it
means the behaviour changes under you. This is generic charmcraft guidance,
same for every charm type, which is exactly why the live file failing it is
worth flagging:

```yaml
actions:
  get-url:
    description: |
      Gets the external endpoint of the workload in case its proxied by an
      ingress. Otherwise, returns the workload's cluster fqdn.
```

`get-url` has no `params` and, today, no `additionalProperties` key either —
the live file does not follow its own best practice. When next touching this
action, add `additionalProperties: false` rather than leaving the Juju-3-vs-4
default to decide the behaviour silently.

Other valid action keys the reference lists: `parallel` (boolean) and
`execution-group` (string). `required` is a list of **parameter names** —
the example on the `charmcraft.yaml` reference page is buggy (it lists a
filename); the `actions.yaml` page has it right.

Action handlers are the one place per-event handlers are correct: actions
are non-deferrable, which is the official test for "deserves its own
handler" (AGENTS.md non-negotiable 1).

## `assumes` and the keys that are specific to this charm type

`assumes` takes a list of strings and supports only two features: `juju <op>
<version>` and `k8s-api`, composable with `any-of` / `all-of` (top level is
an implicit `all-of`). The real block:

```yaml
assumes:
  - k8s-api
  - juju >= 3.6
```

The reference calls `assumes` *"Recommended for Kubernetes charms"* — and
here `k8s-api` is not incidental, it is the point: without it, Juju would
attempt to deploy this charm onto a machine model, where there is no pod to
put the `catalogue` container's sidecar into and the deployment cannot work.
A machine charm has no equivalent feature to assert "do not deploy me on
Kubernetes" — the asymmetry runs only one way.

**Keys that are specific to Kubernetes, and what this charm does with each:**

| Key | Status here |
|---|---|
| `containers:` | Required — one entry, `catalogue`, see above |
| `resources: {type: oci-image}` | Required, one per container — `catalogue-image` |
| `charm-user:` | Optional, k8s-only, *"has no effect on machine charms"* per the reference; not set, so the charm runs as root by default |
| `devices:` | Optional, GPU requests; not used — nginx serving static files has no GPU need |
| `storage:` | Not used today; if added, a container's `mounts:` subkey is how a storage claim gets attached inside the pod |
| `build-base:` | Only valid when `base` is a development base (`ubuntu@devel`) — type-agnostic, not k8s-specific despite living in the same block |

For `links.documentation`, the reference is explicit: link the *charm's*
docs, not the application's — here that already points at
`documentation.ubuntu.com/observability`, which is the COS docs, not
nginx's.

## `charmcraft` linters run during pack

`charmcraft pack` runs analyzers implicitly, and a linter in error state
**blocks the pack** unless `--force`. Two attributes matter here:

- `language` must resolve to `python` — dispatch must execute a `.py`
  entrypoint.
- `framework` must resolve to `operator` — requires `venv/ops` present in
  the packed charm and `import ops` in the entrypoint.

**Both of these currently FAIL for a `plugin: uv` charm, verified
2026-08-08 with charmcraft 4.3.1.** Two independent charmcraft defects, and
neither is specific to machine or Kubernetes charms — both affect any charm
built with the `uv` plugin:

1. **`entrypoint` / `language`.** `charmcraft/dispatch.py` emits
   `exec "${python_path}" "${dispatch_path}/src/charm.py"`, but
   `linters.py::get_entrypoint_from_dispatch` takes
   `shlex.split(last_line)[-1]` and joins it to the base directory **without
   expanding the shell variable**. It then looks for a literal
   `${dispatch_path}/src/charm.py` and fails.
2. **`framework`.** `_check_operator` requires `basedir/venv/ops` to be a
   *directory*. The `uv` plugin builds a real virtualenv, so `ops` lands at
   `venv/lib/pythonX.Y/site-packages/ops`. There is no `venv/ops`, so
   `framework` can never resolve to `operator`.

Consequences: `manifest.yaml` records `language: unknown` and `framework:
unknown`, so this affects what Charmhub sees, not just CLI output.
**`charmcraft pack` is not blocked** — the analysers are advisory unless a
linter is in error state.

Do **not** vendor a hand-written `dispatch` to work around #1 without
reading the `uv` plugin first: it deletes `venv/bin/python*` during build,
so the template's `ln -s $(which python3)` and `LD_LIBRARY_PATH` setup are
load-bearing at runtime. And #2 cannot be fixed from inside a charm repo at
all.

One thing that *is* ours: the entrypoint must be executable (`chmod +x
src/charm.py`), because `check_dispatch_with_python_entrypoint` calls
`os.access(entrypoint, os.X_OK)`.

Linters can be silenced via `analysis: {ignore: {attributes: [...], linters:
[...]}}`.
