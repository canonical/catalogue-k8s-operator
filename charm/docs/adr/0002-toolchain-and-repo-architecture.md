# ADR-0002: Toolchain and repo architecture

**Status:** Accepted
**Date:** 2026-11-21
**Related:** [ADR-0001: Adopting fetch-compute-apply](0001-fetch-compute-apply.md)

---

## 1. Context

`pyproject.toml` and `tox.ini` arrived from upstream in a 2021-era
state: `[tool.black]` alongside ruff, `E501` ignored (so the 99-char
limit was unenforced), `pydocstyle` configured under a mistyped
`[lint.pydocstyle]` table that ruff silently ignored (it emitted
D203/D211 incompatibility warnings instead — the google convention
was never active), `ANN`/`PLC0415` absent, and pyright pinned to
`pythonVersion = "3.8"` against `requires-python = "~=3.14.0"`. The
vendored `lib/charms/` tree mixes never-edited Charmhub libraries with
one this repo owns and publishes (`catalogue_k8s`, whose every edit
must bump `LIBAPI`/`LIBPATCH` — enforced by `tox -e static`).

## 2. Problem breakdown

1. **Enforcement vs declaration.** AGENTS.md declares conventions
   (99/72 line lengths, annotations, no function-level imports) that
   the config did not check. A gate that does not enforce its own
   rules is worse than no gate — it launders opinion into
   "compliance".
2. **The owned library is public API.** `lib/charms/catalogue_k8s` is
   published on Charmhub; style churn there forces version churn on
   consumers (see `tox -e static`'s LIBPATCH gate).
3. **Two test frameworks, one venv.** ops 3.x moved Scenario to the
   `ops-scenario` package (`scenario` module); `ops.testing`
   re-exports it only when installed.

## 3. Approaches

### A. Lint and strictly type everything, including `lib/charms/`

One ruff/pyright scope over the whole tree.

**Cons (measured 2026-11-21)**
- 81 ruff findings and 190 pyright strict errors in the owned library
  alone — a published artifact, where every fix is a `LIBPATCH` bump
  forced on consumers for zero behavior change. The never-edited
  vendored libraries are worse and must not be touched at all
  ([workload-constraints §10](../workload-constraints.md)).

### B. Per-tool excludes, config drift between scopes

Ruff/pyright each get hand-maintained exclusion lists.

**Cons**
- Exclusion lists drift; a new directory is forgotten until it
  ships findings. The scopes differ by *policy*, and policy belongs in
  one place.

### C. Scope by tool, config per scope (chosen)

- ruff (`fmt`/`lint`): `src/` + `tests/` only — passed explicitly on
  the tox command line.
- pyright strict: `src/` + `tests/` via `pyproject.toml`.
- pyright standard: the owned library via its own project file,
  `pyright-lib/pyrightconfig.json`, invoked as the second command of
  `tox -e static`.
- coverage: `src/` only, `fail_under = 90`.

## 4. Recommendation

**C**, with the ruff rule set from the `python-style` skill
(`E,W,F,I,N,UP,B,C4,SIM,RUF,ANN,D,PLC0415`; `E501`/`W505` at 99/72
enforced, not ignored; `[tool.black]` deleted — `ruff format` is the
formatter). A is recorded so "just lint the lib too" is not
rediscovered; the measurement above is the answer. B is recorded
because it is what the tree would drift into without the decision.

## 5. Design

### 5.1 The pyright split, and why a directory outside `lib/`

Pyright 1.1.410 does **not** support `typeCheckingMode` inside
`executionEnvironments` (verified; also that strict-mode overrides
inside a `strict`-paths config may only raise severity — which would
kill the `reportPrivateUsage = false` the tests environment needs).
So the modes split across two configs.

Why `pyright-lib/` and not a config *inside*
`lib/charms/catalogue_k8s/`: the LIBPATCH gate fails any file under
that path whose diff lacks a `+LIBPATCH`/`+LIBAPI` line — a config
file added there would fail the gate the moment it is committed
(simulated and confirmed). The `--project` discovery rule (pyright
searches upward from the target and would find `charm/pyproject.toml`,
typing the lib *strict* — measured: 190 errors) forces a dedicated
config; its location is chosen by the gate.

The tests executionEnvironment carries `reportPrivateUsage = false`:
unit tests legitimately invoke private handlers — that is policy, and
policy lives in config, not scattered per-line suppressions.

### 5.2 Suppression discipline is machine-checked

`enableTypeIgnoreComments = false` (mypy's spelling suppresses
nothing here) and `reportUnnecessaryTypeIgnoreComment = "error"` (a
stale suppression fails `tox -e static`). Both settings already paid
for themselves: the ops 3.10 bump made two suppressions stale and the
gate named them.

### 5.3 Dependency policy

Runtime and dev dependencies in `pyproject.toml` only; never
`charmcraft.yaml`'s `charm-libs` (that key is only for Charmhub-hosted
libraries). `uv.lock` committed; `tox -e lock` regenerates. The
`uv` charmcraft plugin requires the lock (`UV_FROZEN` defaults true).

ops is pinned `>=3.10.0` (bumped from `>=2.20.0` as a separate change
from this refactor; series 3 is API-compatible with the 2.x surface
this charm uses — verified by the suite). `ops-scenario` is a direct
dev dependency, not a transitive accident: the tests import
`ops.testing.Context`/`State`, which re-export from the `scenario`
package only when installed.

Known cost, recorded: `charmed-service-mesh-helpers` locked **down**
to 0.2.0 (its 0.6.0 pins `ops~=2.5`). Only its pydantic models are
imported (`charms/istio_beacon_k8s/v0/service_mesh.py`); unit and
static gates are green on 0.2.0. NOT VERIFIED: mesh *rendering* beyond
import level — integration-test territory.

### 5.4 Warnings are errors

`filterwarnings = ["error", ...]` with module-scoped ignores for the
two vendored deprecation emitters
(`charms.tls_certificates_interface`, `charms.loki_k8s`). Everything
else a warning is a failure. The Harness deprecation died with the
Scenario migration rather than being ignored.

### 5.5 flaplint is advisory, deliberately out of `envlist`

It catches a defect class ruff/pyright cannot see
(relation-databag ordering churn — sets, globs, `uuid4()` reaching
databag/file/hash sinks), but it is a single-maintainer project, not
on PyPI, one tag, no LICENSE file in the tree. Advisory gate; run it
when a change touches a databag write, a file write, or a hash. The
invocation is pinned (`uvx --python 3.12 --from git+...@v1.1.0`) and
lives in `tox.ini` — flaplint has no config file.

### 5.6 tox `work_dir` outside the tree

`{toxinidir}/../.tox` — tox builds venvs with symlinks, and a
half-built `.tox/` inside the charm tree would land in
`charmcraft pack`'s build context. Covered by the repo-root
`.gitignore`.

## 6. Future Work (Out of Scope)

- Retire `pyright-lib/` if pyright grows per-environment
  `typeCheckingMode`. Trigger: a pyright release note.
- Revisit the `charmed-service-mesh-helpers` floor when a release
  supports ops 3. Trigger: upstream release.

## 7. Consequences

### Positive

- The gates enforce the conventions AGENTS.md declares; a stale
  suppression or a mistyped table fails loudly instead of silently.
- The published library is type-checked at today's strength (standard
  — measured zero errors) without being on the style hook.
- Warnings surfaced the ops 3.x deprecations as errors the moment they
  mattered.

### Negative

- Two pyright configs to keep aligned instead of one — and a
  non-obvious directory (`pyright-lib/`) whose reason is a gate's
  regex; this ADR §5.1 is the place a confused contributor lands.
- The strict-over-tests choice costs honest suppression discipline at
  test seams (casts at fake boundaries) — accepted as the price of
  `reportUnknown*` coverage.
- `tox -e static` runs pyright twice; slower than one invocation.
- On sshfs-like filesystems tox's symlinked `bin/python` breaks venv
  re-creation; the `TOX_WORK_DIR` env var is the machine-local escape
  hatch (not a repo decision — noted here because it is where the
  error surfaces).