# Stability, versions, and limits

AgentDynamics is 0.x. This page says what that means in practice: what you can build on, how a change that
would break you is handled, the limits of a single install, and what 1.0 is waiting for.

## What you can build on

These are contracts. A change to one is a breaking change, made only as described under **Deprecation**:

| Contract | Kept by |
|---|---|
| REST field names the console reads (`/api/*`) | `tests/test_api_contract.py`: every GET route against the fields in `tests/api_contract.json` |
| Health-rule ids (saved `rules.json`, alert consumers) | the same contract file (`rules`) |
| Prometheus metric names (`/metrics`) | the same contract file (`/metrics`) |
| Ingestion formats: `/api/ingest`, `/api/ingest/records`, `/v1/traces` (OTLP JSON and protobuf), `/langsmith/*` | `tests/test_integrations.py`, and `tests/test_ecosystem.py` against each SDK's newest release, nightly |
| The alert webhook body `{"source", "events"}` | `tests/test_alerts.py` |
| The Python API exported from `agentdynamics/__init__.py` | its tests, and the examples |
| The config file's keys (`agentdynamics.toml`) | documented in `agentdynamics/config.py` |

A new field, rule, metric or config key is not a breaking change, and arrives in any release. Code that reads the
API should ignore fields it doesn't know.

Not contracts: the console's look and layout, anything under a leading underscore, the store's tables (they are
derived and rebuilt; the durable ones change only through migrations that run on start), log output, and the exact
values of scores and baselines, which improve between releases (each release's CHANGELOG says which figures move).

## Versions

- **Before 1.0**, a breaking change bumps the minor version (0.9 to 0.10) and is listed under **Upgrading** in
  the CHANGELOG with what to do. A patch release never breaks anything.
- **From 1.0**, semantic versioning: a breaking change waits for 2.0.
- **Upgrading** never needs a migration step of yours. A changed derived schema (`SCHEMA_VERSION`) is rebuilt from
  the sources on first start; durable tables gain columns in place, on SQLite and Postgres alike. Pin an exact
  version, read **Upgrading**, and roll forward; to roll back, restore the store's backup (a rebuild runs again).

## Deprecation

A field, route or config key is never renamed in place. It is added under its new name, and the old one keeps
working, marked **Deprecated** in the CHANGELOG, for at least one minor release (before 1.0) or one major (after).
Only then is it removed, the contract file updated in the same commit, and the removal listed under **Upgrading**.

## Limits of one install

- **One instance analyses.** Several instances can share a Postgres schema: all of them serve the console and take
  ingest, one holds an advisory lock and runs the analysis, and another takes over within one refresh interval if
  it goes away. Ingest and reads scale out; the analysis doesn't.
- **What one analyser keeps up with** is measured in [BENCHMARKS.md](BENCHMARKS.md): an incremental refresh that
  absorbs new traffic costs about 13 µs per task held, so 100,000 tasks take about 1.3 s to absorb 100 new ones, and
  memory grows with the tasks held. Retention bounds both: days past `[retention] days` are rolled up into daily
  totals (kept, and counted in every total and percentile) and dropped from memory.
- **Past that**, run an install per group of projects (scoped keys and SSO rules keep teams apart within one), or
  shorten retention. Sharding the analysis itself is on the 1.0 list below.

## What 1.0 is waiting for

1. **An external security review** of the server, the sign-in paths and the Aegis integration
   ([SECURITY.md](../SECURITY.md) lists what has been reviewed internally).
2. **The analysis scaling past one instance**: incremental passes for threads and time-dependent outcomes (the
   remaining full pass), then sharding by project.
3. **One deprecation cycle run end to end**, so the process above has been exercised, not just written down.
4. **A reference for every route** generated from the contract file, so the contract is readable without the code.
