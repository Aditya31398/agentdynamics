# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/). Public contracts: the Python API exported from
`agentdynamics/__init__.py`, the ingestion formats (`/api/ingest`, `/api/ingest/records`, `/v1/traces`,
`/langsmith/*`), health-rule ids, and the REST API field names used by the console. Before 1.0, a breaking change
bumps the minor version.

## [Unreleased]

**Upgrading.** `SCHEMA_VERSION` 13: tasks and steps record tripwires and what each agent did in a task, and tool
arguments are stored redacted. The derived tables are rebuilt from the
sources on first start, as after any schema change; nothing to migrate. The new durable `incidents` tables are
created then too, and the first refresh opens incidents from the security events already held -- without
alerting, as history never is.

### Added
- **A review before calls that can't be undone** (`instrument(..., review={"tools": [...]})`, `pip install
  anthropic`). For the tools listed -- a refund, an email -- a model checks each call against the request the run
  was traced with, as the last guard in the Aegis kernel. It can only refuse (`review.blocked`, audited like any
  denial); it sees the request and the proposed call, never the documents and tool results an injection would come
  from; and it fails closed: no request, no SDK, an error or an answer outside the schema refuses the call. Each
  review is a sub-agent run of the conversation (`aegis.review`), so its cost is the task's sub-agent cost and not
  one of the agent's turns. The governed demo's new refund scenarios show it refusing a refund a retrieved note
  added. SDK runs gain `parent_id` for sub-agent runs.
- **The checker: a model's view of each incident, in shadow mode** (`[checker]`, `pip install anthropic`). For each
  incident a model (`claude-opus-5-5` by default) says what it thinks happened -- prompt injection, probing, an
  exfiltration attempt, a policy too narrow for the agent, an honest error -- and what it would do; with
  `grade_outcomes` it also grades tasks whose outcome was only inferred. Nothing acts on it: reviews are shown on
  the incident and in the list, grades sit beside the outcome, and the Incidents page keeps its record against
  your verdicts and grades. It sees the stored, redacted copy, gets the evidence as one fenced JSON document its
  instructions call data, must answer in a fixed schema, and is held to `max_per_hour`. `GET /api/checker`.
- **Restrict instead of revoke.** A directive can now take some of an agent's authority away and leave it the rest:
  tools, and all but a share of what remains of its budget (`agentdynamics revoke --tools ... / --budget ...`,
  `{"tools": [...], "budget": ...}` in `POST /api/revocations`, the directives card, or **Take ... away** on an
  incident, which offers the tools the agent misused there and recommends it when the evidence is refused calls
  rather than a tripwire). With aegis-kernel 0.6's new `Kernel.restrict` the grant and its subtree lose them in
  place and Aegis refuses a call to one; with an older Aegis the integration revokes a grant the moment it calls a
  tool that was taken. `[enforcement.trust]` restricts an agent whose trust falls below `restrict_below`, taking the
  tools it misused, renewed while it stays low. Directives gain `kind` and `spec`; a store from before gains the
  columns on start, and its directives read as revocations.
- **A process starts held.** `instrument(..., revocations=True)` now fetches directives before it returns (at most
  3 seconds), so a process restarted while its agent is revoked or restricted no longer gets its first
  conversations free; the demo showed exactly that.
- **Untrusted input** (aegis-kernel 0.6): refusals under Aegis's new `integrity.untrusted_input` show up like any
  other, and the governed demo gains a phishing scenario -- the agent fetches the page a ticket links to and does
  what it says, and the call is refused.
- **Trust: what each agent's own behaviour says about it.** A score from 0 to 100 per agent, on the Incidents
  page, on each incident, and in Prometheus (`agentdynamics_agent_trust`). It loses points for tasks in which
  the agent touched a tripwire (40) or probed its policy (15) and for the share of its calls the policy refused
  (up to 20); evidence counts half as much a week on, so trust comes back with good behaviour. It is about
  behaviour, not competence: failed tasks and tool errors don't lower it (success rate is shown beside it).
  Each agent is scored on its own steps, even when a root and its sub-agents share a task. A person's verdict
  wins: evidence in an incident resolved as a false alarm stops counting, and in one confirmed as real counts
  1.5 times. Weights and bands are `[trust]` settings. `GET /api/trust`.
- **Incidents: one thing to judge per agent.** The security signals about one agent in one project -- a
  tripwire touched, probing, refused calls, a grant revoked mid-run, a directive -- are grouped into an
  incident, which gathers them until the agent has been quiet for a day (`[incidents] gap_hours`) and stays
  open until someone resolves it as real or a false alarm. The new **Incidents** page shows each one's
  signals task by task, the steps that are its evidence, and what to do: revoke the agent (recommended when
  the evidence is of intent), tighten its policy, or give the verdict (an admin key). Alert destinations can
  take `incidents` instead of `events`: one alert when an incident opens, one when it escalates, one when it
  is resolved. Incidents are durable, like grades, and derived the same way by every store fed the same
  traffic; new evidence after a verdict opens a new incident. `GET /api/incidents`, `GET /api/incident/<id>`,
  `POST /api/incidents/<id>/verdict`.
- **Tripwires: decoys no legitimate agent touches.** A decoy tool, or a **canary** -- a value planted where no
  agent has reason to look, like a fake credential in a config file. Unlike the watchdog and probing detection,
  there is no threshold: one touch is certain evidence. In process (`instrument(..., tripwires={...})`), every
  governed call is checked before it runs; a decoy tool or a canary in the arguments revokes the run's grant tree
  first, so Aegis refuses the call and the canary never leaves, and a canary in a tool's result or a model
  response stops everything after it. On the server (`[enforcement.tripwires]`), touches are marked on every
  source, the new `tripwire` health rule raises a critical event, and an agent that touches them in 2 runs within
  an hour (configurable) is revoked wherever it runs -- not on one touch, since a single planted document would
  otherwise let an attacker switch an agent off for everyone. Events, alerts and the console name a canary, never
  its value. The Governance page lists every touch and the task timeline tags the step. The governed demo agent
  gains an exfiltration scenario: an injected ticket sends it to a planted credentials file the policy lets it
  read, and the email that would carry the key out is refused.
- **Moving an install to Postgres keeps its history.** `agentdynamics store copy --to postgresql://...` copies
  a SQLite store into a Postgres schema: every span, the SDK run files, grades, the daily rollups of days
  retention has purged, revocation directives, alert state and the alert queue, pull cursors, and the health
  rules and SLOs saved in the data directory. The Postgres instances rebuild the analysis from it on their
  first refresh. It reads one consistent snapshot, so it can run while the old server is up; copy again after
  stopping it to pick up the rest -- the second copy also drops what the source dropped meanwhile, so an
  alert delivered in between is not sent twice. It refuses a schema holding another store's data, or one a
  Postgres instance has already run on, unless told to merge (`--force`). `agentdynamics store verify` compares
  the two table by table. A test moves a store with purged history, grades, directives and queued alerts,
  starts Postgres from an empty data directory, and checks that every console route answers as it did before
  the move.

### Changed
- **The console is tested by using it.** A new test drives headless Chrome through the console as a person
  would -- every sidebar link, the time-window and project filters (and that they survive a reload), opening a
  task and going back, the task list's filters and search, regrouping Analytics, Refresh, issuing and clearing
  a revocation directive, switching a health rule off, and adding an SLO -- and fails on any console error,
  uncaught exception or HTTP error along the way. It drives Chrome over the DevTools protocol with the standard
  library alone (`tests/cdp.py`). Eleven breakages, from the 501 after a body-less POST to a filter that stops
  filtering, were each checked to fail it.

### Fixed
- **Tool-call arguments were stored unredacted.** A tool's input preview was redacted, but its full arguments
  (`args_json`, which policy export learns from) kept emails, card numbers and keys verbatim, and were kept even
  with `store_content = false`. They are now redacted like the rest of a step, and not stored at all with content
  off; `SCHEMA_VERSION` 12 rebuilds existing stores without the old values. Policy export treats a redacted
  value as unknowable: it no longer learns `one_of: ["[REDACTED]"]` (which would refuse every real address), and
  its coverage check no longer counts a call as refused on the strength of a placeholder.
- **Claude Opus 5.5 was priced as Claude Opus 5.** Prices match by longest prefix, so `claude-opus-5-5` took
  Opus 5's $5 / $25 instead of its own $4 / $20 (cache reads $0.20). It and Claude Sonnet 5.5 are now listed.
- **Policy export answered 500** when the governed runs in view named no policy (an Aegis audit log, an older
  integration). It now builds a policy from scratch, as it does with no base.

## [0.8.0] - 2026-09-27

Server-side revocation (#8): the server can stop an agent wherever it runs, when an operator asks or when it sees
the agent probing its policy across runs, through Aegis's own `Kernel.revoke`. Also a fix for requests that
followed a body-less POST on the same connection.

**Upgrading.** No schema change and nothing to migrate: the new `revocations` table is created on first start.
Revocation is off on both sides until you turn it on: agents opt in with
`instrument(kernel, root, revocations=True)`, and the server issues directives on its own only with
`[enforcement] probing`. Works with aegis-kernel 0.4.0 and 0.5.0.

### Added
- **Server-side revocation** (#8). A **directive** revokes an agent (or every agent) in a project (or every
  project) until a given time. Operators issue one with `agentdynamics revoke`, the Revocation directives card
  on the Governance page, or `POST /api/revocations` (admin). The server can also issue them itself:
  `[enforcement] probing` revokes an agent whose calls the policy keeps refusing across several runs, which the
  in-process watchdog, seeing one run, can't. Processes that opt in with
  `governance.instrument(kernel, root, revocations=True)` poll for directives and apply them through
  `Kernel.revoke`, in every grant tree they have seen and before any governed call or spawn, so the kernel
  enforces and audits. A directive can only take privileges away, and clearing one restores nothing (Aegis
  revocation is permanent). If the server is unreachable nothing is revoked and the agent carries on. New
  directives are announced to alert destinations with `kinds = ["revocations"]`. See
  [docs/GOVERNANCE.md](docs/GOVERNANCE.md#3b-enforce-from-the-server-revocation-directives).

### Fixed
- **A POST whose body the route didn't read broke the next request on the connection.** The console sends `{}`
  to routes that take no body (Refresh, and now Clear); left in the socket, it prefixed the next request on the
  keep-alive connection, which the server answered `501 Unsupported method ('{}GET')`. Unread small bodies are
  now read off, and a large one closes the connection.

## [0.7.0] - 2026-09-27

For teams and for volume: project-scoped API keys, alert routing with SLO burn-rate pages, daily totals kept past
retention, an optional Postgres store that several instances can share, and an incremental refresh about twice as
fast at 100,000 tasks. It also fixes a privacy bug: a health-rule message could carry user text that redaction
keeps out of storage, and alert webhooks sent it unredacted. Upgrade if you use redaction or `store_content =
false` together with alert webhooks.

**Upgrading.** No schema change and nothing to migrate: the new durable tables (alert delivery, alert state,
daily rollups) are created on first start, and the first refresh rebuilds every task as usual, rewriting stored
event messages with the redaction applied.

- **Retention now deletes SDK run files** (`runs/*.json`) older than the window, as the documentation always
  said it did. Copy them first if something else reads them. Each day's totals are kept in `rollup_daily` before
  it is purged, from this release on; days purged before the upgrade have none. Retention now waits for the
  second refresh after a start.
- **Existing `[[alerts.webhooks]]` keep working unchanged**: the JSON body has the same shape, and SLO alerts
  go only to destinations that set `kinds = ["slos"]`. Delivery is now queued and retried.
- **Process Review's unfiltered view leaves out subagent tasks**, as the filtered views always did.
- **Postgres is optional.** A new install can start on it (`pip install "agentdynamics[postgres]"`,
  `AGENTDYNAMICS_DB_URL`). Moving an existing SQLite install starts from an empty store: nothing imports the
  SQLite file yet, and only sources that are read again (Claude Code transcripts, LangSmith/Langfuse pulls) come
  back.

### Added
- **A Postgres store** (#7). `[store] url = "postgresql://..."` (or `AGENTDYNAMICS_DB_URL`) keeps the store in
  a Postgres schema instead of the SQLite file; the driver is the optional extra `agentdynamics[postgres]`
  and the Docker image includes it. The model, the `SCHEMA_VERSION` rules and every query are the same:
  a translation layer turns SQLite's dialect into Postgres's, and a new test loads the same traffic into
  both stores and requires every console route to answer identically. Several instances can share one
  schema: SDK runs join spans in the shared store, one instance (holding an advisory lock) writes the
  analysis, sends alerts and pulls from APIs, the others serve the console and take ingest, and one takes
  over if the writer goes away. Grades, health rules and SLOs saved on any instance reach the writer.
  CI runs the whole suite against Postgres. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#the-store-sqlite-or-postgres).
- **Daily rollups past retention.** Before retention purges a day, its totals are kept in a durable
  `rollup_daily` table: tasks, spend, tokens, calls, errors, durations, scores and Apdex, per project,
  environment, framework, source, workflow, task type and outcome. The Overview's daily spend, Analytics by
  day, week, project, type, outcome or source, and the Prometheus `*_total` counters include them, so a
  quarter-old cost trend survives a 30-day retention and counters no longer drop when retention runs.
  Views that need per-task detail say they cover only the tasks still held. A test holds every total
  unchanged across a purge, a restart and days going by. Details:
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#retention-and-rollups).
- **Alert routing.** Destinations can be Slack, PagerDuty (Events API v2) or JSON webhooks, each filtered by
  project, rule, severity and kind (`kinds = ["events", "slos"]`). **SLO burn-rate alerts** page when an
  objective's error budget is burning fast (multi-window, multi-burn-rate, as in the Google SRE Workbook:
  a page at 2% of the budget in an hour or 5% in six, a ticket at 10% in three days, scaled to the SLO's
  window) and resolve when it stops. PagerDuty and Slack group a burst of one violation into one alert.
  Delivery is durable and ordered, retrying 429/5xx/network failures for a day. `agentdynamics alerts
  status|test`, `/api/alerts`, an Alert destinations card on the Integrations page, firing alerts and burn
  rates on the SLOs page, and `agentdynamics_slo_burn_rate` / `_burn_threshold` / `_alert_firing` and
  alert-delivery metrics on `/metrics`. Existing `[[alerts.webhooks]]` entries keep working unchanged: the
  JSON body keeps its shape, and SLO alerts are sent only to destinations that ask for `"slos"`. Details:
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#alerting).
- **Project-scoped API keys.** `agentdynamics keys create --role read --project checkout` (repeat `--project`
  for several) makes a key that sees and sends only those projects. Reads go through a per-request, read-only
  connection on which `tasks`, `runs`, `steps` and `events` are views limited to the key's projects, so every
  endpoint is scoped, including ones added later. `/metrics`, `/api/sources` and `/api/config` cover the whole
  install and refuse a scoped key, and SLOs are shown only when they cover the key's projects. Writes are
  checked before anything is stored: a single-project key's project is stamped onto everything it sends, a
  multi-project key must name one of its projects, and no scoped key can add to, overwrite, PATCH or rate
  another project's traces or runs (403, with the ids). `/api/whoami` and `/api/filters` report the scope, and
  the console's project filter says whose projects "All" means. Admin keys can't be scoped. Limits, including
  install-wide baselines, are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#project-scoped-keys).

### Changed
- **An incremental refresh is about twice as fast at scale.** Absorbing 100 new tasks into a 100,000-task
  store took 2.96 s and now takes 1.32 s; 1% more traffic, 3.43 s and now 1.72 s. Three passes that ran
  over every task on every refresh no longer do: Process Review insights are computed when the page asks
  (every filter recomputed them anyway), a baseline is re-computed only when its sample could have
  changed, and a run's subagent spawns are read once rather than on every pass. Each reuse condition is
  held to a full rebuild by a new test, and each was checked to fail when removed. On the SDK path,
  finding which run files changed no longer stats each one: 0.6 s of every refresh at 10,000 runs on
  Windows, now 0.05 s.
- **Process Review's unfiltered view computes its insights from the tasks it shows**, as every filtered
  view already did. It used to show a figure computed over every task, subagents included.
- **`bench/bench.py` times garbage collection apart.** A full collection scans the whole in-memory store;
  after a bulk load one could fall inside a timed refresh and add half again to it. Incremental timings now
  start from a collected heap, and a "full GC" column reports what one costs (0.77 s at 100,000 tasks, an
  occasional pause rather than a per-refresh cost).
- **`server.py` and `web/app.js` are split by area of the console** (#9). The read endpoints live in
  `agentdynamics/api/` as one mixin per area (monitor, diagnose, assess, governance, ops); `server.Api` composes
  them, so `from agentdynamics.server import Api` is unchanged. The console's pages live in `web/pages/<area>.js`
  and use `app.js`'s helpers through `window.AD`; the router now starts on `DOMContentLoaded`, after every page
  script has registered. No behaviour change: moved mechanically, checked member-for-member against the old
  class, and every page and its main interactions exercised in a browser with no errors. A new `package` CI job
  installs the built wheel and fails if any script `index.html` loads is missing from it.
- **Editing `rules.json` by hand takes effect without a restart.** The rules are fingerprinted each
  refresh and every task is re-scored when they change (it used to take a restart, or a save from the console).

### Fixed
- **An "in progress" task stayed that way on a quiet install.** Outcomes settle as time passes, but a
  refresh with no new traffic returned early, so a task showed "in progress" until something else
  arrived (a rebuild said "unknown"). Found by the incremental-equals-rebuild test when it ran on Postgres.
- **Lists could come out in a different order from one load to the next** where their SQL left the order
  open: sessions, the flow map's nodes, tool error samples, ties in rankings. Each now has a defined
  order and a tie-breaker.
- **Retention never deleted SDK run files.** `runs/*.json` older than retention stayed on disk, and every
  restart re-read them. They are now deleted with the rest of the run (Claude Code transcripts, which are
  yours, are never touched). Retention also no longer runs on a process's first refresh.
- **Health-rule messages could carry user text that redaction keeps out of storage.** The `rework` rule
  quotes the correcting message; its event message skipped `store_content = false` in the database, and
  skipped redaction entirely in alert webhooks, which sent the unredacted text. Messages are now built from
  the redacted text.
- **An alert whose webhook failed was lost**: it was marked sent before the request. Alerts now go through a
  durable queue and are retried.
- **Conversation threads and subagent parents no longer cross projects.** Two projects' runs with the same
  client-chosen `thread_id` were one conversation, so one project's follow-up could make another's task its
  next message and turn it into `rework`. A run naming another project's run as its parent added its cost to
  that task. Both are now linked only within a project.
- **`/api/slos` returned a 500 when a filter left an SLO with no tasks.** An empty window now reads `unknown`.
- **A client hanging up mid-request printed tracebacks.** Reloading the console, closing a tab or restarting
  the server while a page loaded logged `ConnectionAbortedError` / `BrokenPipeError` / `ConnectionResetError`,
  twice per request (the server then tried to send a 500 on the closed connection), plus socketserver's
  "Exception occurred during processing of request". Disconnects on the client's connection are now closed
  quietly; errors from the server's own code are still logged and answered with 500/400.
- `/api/refresh` no longer reports the install-wide count of changed runs to a project-scoped key.

## [0.6.0] - 2026-09-25

Incremental refresh (#5), and three bugs its exactness test found. The one users can see: an earlier task in
a conversation could stay marked `rework` because of a follow-up message that had since been deleted.

**Upgrading.** No schema change and nothing to migrate. The first start recomputes every task, as each
process's first refresh always does, so baselines move to the new stepped sample then; baseline figures and
the scores measured against them can shift slightly. Tasks in conversations whose follow-up was deleted,
edited or moved lose a `rework` outcome they should not have had.

### Changed
- **An incremental refresh re-scores and rewrites only the tasks whose inputs changed** (#5), instead of
  every task in the store. Absorbing 1% more traffic took 13.5 s at 100,000 tasks and now takes 4.2 s,
  re-scoring exactly the 1,000 new tasks. Scoring and writing are now proportional to new traffic, but a light
  pass still runs over every task each refresh (outcomes, conversation threads, grades, baselines,
  insights), about 30 µs a task, so a refresh still grows with the store: absorbing 100 tasks takes 0.33 s
  at 10,000 and 2.9 s at 100,000. An incrementally maintained store equals a full rebuild of the
  same sources, every column of every row: `tests/test_incremental.py` checks that over randomized histories
  of ingests, updates, deletions, grades, threads, subagents and time passing.
- **Baselines are computed from a type's earliest tasks, in 5% steps.** A baseline that moved with every
  arrival would make every refresh re-score its whole type. It now uses the type's earliest M tasks (by start
  time), where M is exact below about 40 tasks and otherwise advances only when the type has grown 5%: so it
  leaves out at most the newest 5% of a type's history, never oscillates, and depends on the data alone, so a
  full rebuild agrees. Baseline figures, and scores measured against them, can move slightly on upgrade.

### Fixed
- **A conversation's earlier task could keep a follow-up that no longer existed.** Linking traced tasks in a
  thread set each task's `next_prompt` and `rework` from its successor but never cleared them, and the engine
  keeps unchanged tasks between refreshes: when the follow-up was deleted, edited or moved to another thread,
  the earlier task kept it, and could stay marked `rework`, until a full rebuild. Found by the incremental
  property test, and present before #5.
- Tasks in a thread that started at the same instant were ordered by whichever order the engine happened
  to hold runs in, which differed between a long-running process and a fresh rebuild. Ties now break by id.
- Process insights depended on that same order: floating-point sums rounded differently and example lists
  were cut to different first entries. They are now computed in a fixed order.

## [0.5.0] - 2026-09-25

Phase 2 of the roadmap: making the numbers worth trusting. Three fixes matter to anyone on 0.4.x
today -- cache writes were charged twice, every span source had a quadratic rebuild, and the policy-export
check missed most refusals -- so this is worth taking promptly.

**Upgrading.** The first start rebuilds the derived tables from your sources (schema 9); nothing needs
migrating. Span sources rebuild at about 1,600 tasks a second (100,000 in a minute on a laptop; see
docs/BENCHMARKS.md). SDK run files depend on the machine: on Linux they are as fast, but on Windows with
real-time antivirus each file open was measured at 9 ms, so 10,000 runs took about a minute and a half. After the rebuild, expect three
numbers to move, all of them corrections: cost goes **down** for cached traffic from OpenTelemetry GenAI,
OpenInference and LangSmith; success rate, failed-run rate and Apdex can shift because recorded feedback now
settles an outcome; and tasks with feedback show it as their outcome's source.

### Added
- **Graded outcomes** (#1). An outcome can now be stated instead of inferred: `agentdynamics.outcome()`
  (and `trace(...).outcome()`) inside a run, `POST /api/tasks/{id}/outcome` after the fact, or
  `POST /api/outcomes` in bulk for eval pipelines. Every task records `outcome_source` (`graded`,
  `feedback` or `inferred`) and `outcome_reason`, the Overview says how much of the success rate is
  graded versus guessed, and `kpis.outcomes_by_source` carries the counts. Grades are durable: they
  survive schema upgrades, and a grade that arrives before its task applies when the task does.
- **`bench/bench.py`** (#6): ingest, full-rebuild and incremental-refresh times at any size, for the SDK
  and span paths. A `scaling` CI job fails when cost grows faster than linearly with the store, measured as
  time at 4k tasks over time at 1k in one process, so runner speed cancels out. Numbers in
  [docs/BENCHMARKS.md](docs/BENCHMARKS.md).
- **Every task says how its type was decided** (`task_type_source`: `workflow`, `prompt kind`, `follow-up`,
  `keywords` or `unmatched`, plus `task_type_match`, the word that matched). A traced app's workflow name is a
  fact; a coding session's type is a keyword guess, and the Task Types page now says which, with the words.
- **Policy coverage** (#3): how much of the traffic that actually ran would a policy refuse, per tool and per
  rule. `agentdynamics policy check --candidate policy.yaml` judges any candidate against recorded traffic,
  `POST /api/policy/check` does the same with a `read` key, and `--max-denied-fraction` (default 0) turns it
  into a CI gate. `policy export` and the Generate tightened policy panel report it for what they generate,
  and the export API gains a `coverage` field.

### Changed
- **Recorded feedback now settles the outcome** rather than being one signal among several. A score
  of 0.5 or more makes a task `completed` even if the run ended in an error; below 0.5 makes it
  `rework`. Success rate, failed-run rate and Apdex can shift for existing data on upgrade: on the
  demo dataset, failed runs moved from 16.0% to 13.8% and Apdex from 0.69 to 0.72.
- `SCHEMA_VERSION` 9 (steps record whether a call went through an Aegis kernel; tasks record how their
  outcome and type were decided). Derived tables rebuild from sources on first start; nothing needs
  migrating.

### Fixed
- **A full refresh was O(n²) for every span source** (OTLP, LangSmith, Langfuse, log ingestion). With no
  `ANALYZE` statistics, which is every fresh install, SQLite answered "the spans of this trace" by walking
  the primary key on `source` alone, visiting every span once per trace. A full rebuild took 6.8 s at 1,000
  traces and didn't finish in 10 minutes at 10,000; it now takes 0.6 s and 5.5 s. Each incremental refresh
  also scanned the whole span table to find what had changed. Both queries now name their index, so the
  plan no longer depends on statistics. Found by the new benchmark (#6).
- **The 0.4.1 policy-export check missed most refusals.** It re-checked argument constraints only, so a
  generated policy that dropped a tool still in use, or required an argument calls never sent, passed as
  safe. On the coverage test's candidate it flagged 2 of the 7 calls a real kernel refuses. Calls are now
  judged by Aegis's own `CapabilityGuard`, and a test replays them through a real kernel and requires the
  same verdict for every call.
- **Prompt-cache writes were charged twice** for traces from OpenTelemetry GenAI, OpenInference and
  LangSmith (#2). All three document their input count as including cache reads *and* cache writes;
  the collector subtracted only the reads, so every cached write was also billed as uncached input.
  Each collector now reads its format's documented rule. Formats with no rule we can cite (the older
  `gen_ai.usage.prompt_tokens` naming, Langfuse) keep the previous estimate but are counted as
  `tokens_unverified` and shown under Spend, the way unpriced models already are. Costs for cached
  traffic from the three documented formats go down on upgrade; that is the correction.

## [0.4.1] - 2026-09-24

Governance fixes found by running a governed agent against the published packages. Two of them
mean an enforcement control was not doing its job, so this is worth taking promptly.

### Changed
- `/healthz` returns `degraded` (not `ok`) while analysis refreshes are failing. Anything alerting
  on the literal string `ok` will now see the difference, which is the point.

### Added
- `policy export` replays the observed traffic against the policy it just generated and refuses
  to stay quiet about calls the candidate would now deny (`regressions` in the API, a warning and
  a non-zero exit in the CLI). A synthesized policy can be strictly narrower than its base,
  constitutional, free of drift, and still refuse everything; `aegis ratify` and `aegis drift`
  compare declarations, so only the recorded calls can catch that.

### Fixed
- The watchdog's `max_repeated_denials` counted refusals of the *same* tool in a row, so an agent
  alternating between two forbidden tools was never revoked however often it was refused -- the
  shape a prompt-injected agent produces on its own ("do X, then confirm by Y"). Consecutive
  refusals are now counted across tools as well, ending at the first call that succeeds. The same
  blind spot in the server-side `repeated_denials` metric (the Governance page's boundary-probing
  count) is fixed too, so that number can rise for existing data.
- Two `Watchdog`s on one run shared their per-run state, so the first to trip silently switched
  off every other one.
- `policy export` turned observed *numeric* arguments into a `one_of` list of those values,
  written as strings. Aegis compares the raw value, so the generated policy denied every call
  including the ones it was built from. Numbers now tighten `max_value` instead, which keeps the
  next legitimate amount working; `one_of` is still inferred for genuinely categorical arguments.
- The Governance page and `policy report` could show more grants used than granted ("6 of 5").
  `used` counted every traced tool in the task, including plain `@tool` functions no kernel
  mediates. It now counts only granted capabilities that were exercised, and tools called outside
  the policy are reported separately as **ungoverned** — a tool nothing mediates is its own finding.
- Removing `<data>/runs` while the server ran raised `FileNotFoundError` out of every subsequent
  refresh, so ingestion stopped for good while `/healthz` kept reporting `ok` from the last
  successful timestamp. The directory is recreated, and a directory that cannot be read is no
  longer treated as "every run in it was deleted" (deleting an individual run file still removes
  that run). `/healthz` reports `degraded` with `failed_refreshes` and `last_refresh_error` when
  refreshes are failing, and `agentdynamics_refresh_failures` is exported to Prometheus.

## [0.4.0] - 2026-09-24

First public release.

### Added
- **Governance with Aegis** (`pip install aegis-kernel`; `agentdynamics.integrations.aegis.instrument`). Aegis decisions become steps in
  their task. Model calls reserve and settle against the Aegis budget, so an exhausted budget blocks the call.
  A `Watchdog` revokes the grant when one tool is refused N times in a row, on loops, and on cost or call caps.
  `agentdynamics policy export|report` and the Governance page generate a tightened, least-privilege policy from
  observed behaviour. Aegis audit JSONL can also be ingested out of process. There are four governance health
  rules, a compliance score, and compare-by-policy-version.
- `agentdynamics.llm_call` / `record_llm` for model clients the SDK doesn't patch.
- LangSmith receiver: zstd-compressed multipart ingestion when `zstandard` is installed. `/info` advertises it
  only then; otherwise the SDK uses `/runs/batch`.
- Ecosystem test suite (`tests/test_ecosystem.py`) running a real LangGraph graph, the OpenTelemetry OTLP
  exporter and the OpenInference LangChain instrumentor, plus a nightly CI job on the latest releases and an
  optional live Anthropic smoke test.
- `agentdynamics --version`, CLAUDE.md, a release workflow with trusted publishing, provenance and a container
  image.

### Fixed
- Probing detection keyed on (tool, rule) missed agents that vary their payload. It is now keyed on the tool.

## [0.3.0] - 2026-09-19

### Added
- One-line integration: `agentdynamics.init()` auto-instruments the Anthropic and OpenAI SDKs (sync, async,
  streaming), LangChain/LangGraph (via the LangSmith endpoint) and OpenTelemetry. `@trace`, `span`, `@tool`.
- `agentdynamics run <cmd>` for zero-code instrumentation, plus `connect`, `doctor` and `keys` commands.
- A Get-started page in the console that detects the first trace live.

## [0.2.0] - 2026-09-19

### Added
- Receivers: OTLP/HTTP (JSON and protobuf, with no dependencies; GenAI semconv, OpenInference, OpenLLMetry), a
  LangSmith-compatible API, and log-pipeline records. Pull connectors for LangSmith and Langfuse. Inbox file
  tailing.
- Agent-flow metrics (paths, loops, critical node, handoffs, TTFT, truncations, rate limits, empty retrievals),
  the Workflows page (process mining), SLOs with error budgets, role-based API keys, redaction, retention,
  alert webhooks, Prometheus `/metrics`, and Docker plus OTel Collector deployment.

## [0.1.0] - 2026-09-19

### Added
- Claude Code transcript analysis: tasks, baselines, Apdex, health rules, flow map, process scorecard, and the
  web console.
