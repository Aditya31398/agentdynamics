# Roadmap

Ordered by whether it unblocks a user, not by how interesting it is. Phase 1 (ship it) is done:
both packages are on PyPI, CI runs the pinned matrix plus a nightly job against the newest
LangGraph / LangSmith / OpenTelemetry / OpenInference / Anthropic / OpenAI releases.

The ranked weak areas in [CLAUDE.md](CLAUDE.md) are the source for most of this. Where an item
fixes one, it says so.

## Phase 2 — make the numbers worth trusting

Everything here is about the gap between *recorded* and *true*. The tool already shows a number
for each of these; the work is making that number defensible to someone who will act on it.

- [x] [#1](https://github.com/Aditya31398/agentdynamics/issues/1) **Grade outcomes instead of inferring them** (weak area 2). `outcome` is derived from
      signals — errors, interrupts, corrections, feedback. That is a guess, and the Apdex, success
      rate and process score all inherit it. Add an explicit outcome API (`agentdynamics.outcome()`
      / a `/api/tasks/{id}/outcome` PATCH), let recorded feedback override inference, and mark
      which tasks are graded versus guessed so the console can show both.
- [x] [#2](https://github.com/Aditya31398/agentdynamics/issues/2) **Input-token accounting with cache reads** (weak area 7). `collectors/spans.py` assumes a
      provider reports `input_tokens` inclusive of cache hits when `input >= cache_read`. That
      heuristic is the only thing standing between the cost column and quiet double counting.
      Pin it per provider, and report *unknown* rather than guessing when the shape is unfamiliar
      — the same rule `pricing.py` already follows for unpriced models.
- [x] [#3](https://github.com/Aditya31398/agentdynamics/issues/3) **Policy coverage report.** `govern.replay` (0.4.1) answers "would this candidate deny
      anything it has seen?". Extend it to report *how much* traffic each tool and argument
      constraint would deny, so a tightening can be judged before deploying rather than only
      rejected when it is obviously broken. This is the check neither `aegis ratify` nor
      `aegis drift` can perform, because only this side has the call history.
- [x] **Task typing beyond keywords** (weak area 3). Coding-task classification is keyword rules;
      traced apps already bypass it via the entry-point name. Prefer explicit workflow names, fall
      back to keywords, and say which one produced the label.
- [x] [#4](https://github.com/Aditya31398/agentdynamics/issues/4) **UI tests** (weak area 5). Pages are checked by hand. A headless pass that loads each page
      against a fixture dataset and asserts the tables render with the expected columns would have
      caught the "6 of 5" grants count, which shipped in 0.4.0 and was found by a person looking
      at the screen.

## Phase 3 — hold up under real volume

- [x] [#5](https://github.com/Aditya31398/agentdynamics/issues/5) **Incremental finalize** (weak area 1, the big one). Each refresh rewrites the whole `tasks`
      table and `finalize()` runs over every run in memory. That is fine to roughly 100k tasks and
      then it is not. Recompute only dirty runs and the baselines they touch.
- [ ] [#7](https://github.com/Aditya31398/agentdynamics/issues/7) **A store backend behind `store.py`.** SQLite is right for a laptop and wrong for a fleet.
      The interface is already narrow; the work is keeping `SCHEMA_VERSION` semantics (derived
      tables drop and rebuild) while a Postgres or ClickHouse backend holds the same model.
- [ ] **Downsampling and rollups.** Retention currently purges. Keep daily aggregates past the
      raw-span horizon so a quarter-old cost trend survives without the spans behind it.
- [x] [#6](https://github.com/Aditya31398/agentdynamics/issues/6) **A benchmark with a number in it.** "Fine to about 100k tasks" is an estimate, not a
      measurement. Generate a corpus, publish ingest and refresh timings per size, and make the
      nightly job fail when a change regresses them.

## Phase 4 — more than one person using it

- [ ] [#8](https://github.com/Aditya31398/agentdynamics/issues/8) **Server-side enforcement** (weak area 6). The watchdog revokes in-process; a server that
      sees the same probing across runs can alert but cannot stop anything. Out-of-process
      revocation needs a channel back to the kernel — the honest version of this is an Aegis-side
      feature, not something AgentDynamics can bolt on.
- [x] [#9](https://github.com/Aditya31398/agentdynamics/issues/9) **Split `server.py` and `web/app.js`** (weak area 4). Both are large single files. This
      unblocks the two items above it more than it stands on its own.
- [ ] **Teams and projects.** API-key roles (ingest / read / admin) are the whole authorization
      model. Multi-tenant use needs project scoping on keys at minimum.
- [ ] **Alert routing.** Webhooks exist; PagerDuty/Slack/Opsgenie shapes and SLO burn-rate alerts
      are what turns health rules into something someone is actually paged for.

## Not planned

- **Hosting it for you.** Self-hosted is the point: prompts and traces stay in your network. A
  hosted demo with synthetic data is worth having for the README; a hosted *service* is a
  different product with a different threat model.
- **Grading agent output quality.** AgentDynamics measures how the work was done — cost, path,
  loops, refusals, policy. Whether the answer was *good* is an eval question, and there are
  better tools for it. The two meet at recorded feedback, which is already ingested.
