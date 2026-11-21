# Roadmap — adopting fetch-compute-apply

Staged delivery plan for bringing this charm onto the pattern used by
pi-hole-operator: a pure functional core (`catalogue_state.py`), an imperative
workload shell, and a single reconciler. Each stage lands green
(`fmt`, `lint`, `static`, `unit`) and records its acceptance here. Treat
anything unlisted in a stage as unstarted work, not a defect.

## Stage 0 — Toolchain and agent alignment

Align `pyproject.toml` with the conventions `AGENTS.md` declares, and port the
`.opencode/` apparatus.

- `pyproject.toml`: drop `[tool.black]`; enable `E501` + `W505` (99/72), `ANN`,
  `D`, `PLC0415`; pyright `typeCheckingMode = "strict"`, `pythonVersion = "3.14"`,
  over `src` **and** `tests`; `tox.ini` gains `ruff format` in `fmt` and a
  `work_dir` outside the tree. **Done 2026-11-20** — pyproject.toml, tox.ini,
  and all code fixes landed; the owned lib stays at pyright standard via
  `pyright-lib/`; `tox -e fmt,lint,static,unit` green.
- Port `.opencode/` per the three-layer filter in `AGENTS.md` (agents, skills,
  `opencode.json` references). **Done 2026-11-20** — agents, the skill set,
  and `opencode.json` are in place; with the item above, stage 0 is complete.
- Acceptance: `tox -e fmt,lint,static,unit` green on the tightened config;
  agents load and cite the new skills.

## Stage 1 — Pure core extraction

Introduce the decide/act split without changing observable behaviour.

- `src/catalogue_state.py`: `CatalogueIntent` (config + relation-derived
  intent), frozen `CatalogueState` snapshot, `CatalogueOutcome` ADT, `fetch`
  (via a `Facts` protocol), `compute`, and the status decisions
  (`status_plan`, `status_without_intent`).
- `src/workload.py`: all Pebble container operations (push/pull/remove_path,
  layers, autostart, replan, restart, get_plan); never imports `ops`.
- `src/nginx_config.py` stays pure; `charm.py` keeps its current handlers but
  routes them through fetch/compute/apply.
- Acceptance: `_update_*` bool-returning mutators gone; unit tests for the
  core need no mocks; existing behaviour unchanged.

## Stage 2 — Single reconciler

- Collapse the per-event handlers into `_reconcile` + `collect_unit_status`;
  dedicated handlers remain only for the non-deferrable set (actions, `stop`,
  `remove`, `secret_*`, `collect_*`). Pebble events defer until
  `can_connect()`.
- Acceptance: one `_reconcile` in `charm.py`; every step safe to run twice and
  safe to never run; `charm-reviewer` audit passes rules 1 and 7.

## Stage 3 — Test layers

- Core tests: pure functions, no mocks. Charm tests: mock `workload`, never
  the container. Workload tests: the `ops.testing` container simulation.
- Coverage gate (`fail_under`) added to `tox -e unit`.
- Acceptance: no test of `charm.py` patches `Container` or Pebble directly.

## Stage 4 — Docs and decisions

- `docs/overview.md` (two-minute map), `docs/pattern.md` (taught with a small
  example that is not Catalogue), `docs/workload-constraints.md` (the
  Kubernetes lies catalog: `can_connect`, ephemeral cert storage, restart
  semantics, ingress databag vs route).
- ADR-0001 (adopting fetch-compute-apply), ADR-0002 (toolchain and repo
  architecture).
- Acceptance: `AGENTS.md` "Read this first" links resolve; ADRs cited from
  code where a rule's reason is no longer obvious.
