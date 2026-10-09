"""Ingestion + analysis engine.

Pipeline:
  push (OTLP, LangSmith-compatible API, SDK, webhook)  ─┐
  pull (LangSmith API, Langfuse API, inbox file tailer) ─┼─> spans_raw (durable, idempotent upserts by span id)
  files (Claude Code transcripts, SDK run files) ───────┘            │
                                                                     ▼
            trace assembly (late spans simply re-assemble their trace) -> normalized runs
                                                                     ▼
            per-run analysis (cached; only dirty runs recomputed) -> cross-run finalize
            (baselines, scores, events) -> SQLite -> API / console / alerts / Prometheus
"""
import hashlib
import json
import os
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict

from . import alerts as alertmod, analysis, checker as checkmod, config as cfgmod, incidents as incmod, pricing
from . import slo as slomod, store
from . import tripwires as tripmod, trust as trustmod
from .collectors import aegis_audit, claude_code, generic, inbox, langfuse, langsmith, otlp, spans as spanmod
from .privacy import Redactor


class SourceStatus:
    def __init__(self, name, kind, detail=""):
        self.d = {"name": name, "type": kind, "detail": detail, "status": "idle", "last_ok": None, "last_error": None,
                  "last_error_at": None, "items": 0, "last_batch": 0}

    def ok(self, n=0):
        self.d.update(status="ok", last_ok=time.time(), last_batch=n)
        self.d["items"] += n

    def fail(self, err):
        self.d.update(status="error", last_error=str(err)[:400], last_error_at=time.time())


def _midnight(ts):
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _next_midnight(ts):
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))   # mktime rolls the month


class ScopeError(Exception):
    """A project-scoped ingest key tried to write outside its projects. Nothing was written."""

    def __init__(self, reason, ids=()):
        super().__init__(reason)
        self.reason, self.ids = reason, sorted({str(i) for i in ids})[:50]


class IngestScope:
    """What a project-scoped ingest key may write.

    Ingestion is keyed by ids the client chooses -- run, span and trace ids, LangSmith PATCHes -- so a key
    scoped to one project could otherwise write into another's traces, overwrite its runs or amend them.
    Rules: a single-project key has its project stamped onto everything it sends (an exporter's
    service.name need not match); a multi-project key must name one of its projects on everything; and
    no scoped key may touch data that already exists in a project outside its scope. The whole request is
    checked before any of it is written.
    """

    def __init__(self, projects):
        self.projects = sorted(set(projects))

    @property
    def stamp(self):
        return self.projects[0] if len(self.projects) == 1 else None

    def allows(self, project):
        return (project or "default") in self.projects


def _doc_project(source, doc):
    """The project a stored document belongs to, read the way assembly reads it."""
    if source == "langsmith":
        return doc.get("session_name") or ((doc.get("extra") or {}).get("metadata") or {}).get("project")
    if source == "aegis":
        return ((doc.get("details") or {}).get("ctx") or {}).get("project") or "aegis"
    return doc.get("project")


def restrict_spec(tools=(), budget=None):
    """What a restrict directive takes away, checked: tool names, and/or the share of the remaining budget to
    keep (0 <= budget < 1). Raises ValueError for one that takes nothing, or would give something."""
    tools = sorted({str(x).strip() for x in (tools or ()) if str(x).strip()})
    spec = {}
    if tools:
        spec["tools"] = tools
    if budget is not None and budget != "":
        b = float(budget)
        if not 0.0 <= b < 1.0:
            raise ValueError("budget is the share of what remains to keep: at least 0 and below 1")
        spec["budget"] = b
    if not spec:
        raise ValueError("a restriction takes something away: name tools, or a budget share below 1")
    return spec


class Engine:
    def __init__(self, data_dir, claude_root=claude_code.DEFAULT_ROOT, cfg=None):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.cfg = cfg or cfgmod.load(data_dir)
        self.claude_root = claude_root
        self.runs_dir = os.path.join(data_dir, "runs")
        os.makedirs(self.runs_dir, exist_ok=True)
        pricing.load_overrides(data_dir)
        # a SQLite file in data_dir, or a Postgres schema when [store] url is set
        self.db_path, self.db_schema = store.target(self.cfg, data_dir)
        self.con = store.connect(self.db_path, self.db_schema)
        # SDK runs are sources too (invariant 3). With SQLite they are files in runs/; on Postgres they go in
        # the shared store beside the spans, where every instance sees them and a redeploy doesn't lose them
        self._runs_in_store = self.db_schema is not None
        # One engine writes the analysis. On a shared Postgres schema the others serve the API and take
        # ingest (both go through the store), and one of them takes over if the writer goes away.
        self.writer = store.claim_writer(self.con, self.db_schema)
        self._grades_sig = self._rules_sig = None
        self._revocations_seen = None     # newest directive already alerted on (None: not looked yet)
        self._store_sdk_ids = set()           # ids of SDK runs read from the store, as _files holds file ones
        self._store_sdk_sig = {}              # run id -> hash of the payload last read
        self.lock = threading.RLock()
        self.redactor = Redactor(self.cfg["privacy"])
        self.tripwires = tripmod.from_config(self.cfg)
        for name in (self.tripwires.ignored if self.tripwires else ()):
            print(f"[agentdynamics] tripwire canary {name!r} ignored: shorter than {tripmod.MIN_CANARY} characters "
                  "would match ordinary text", file=sys.stderr)
        self._files = {}          # path -> (mtime, size, run)
        self._runs = {}           # run id -> run   (trace/SDK/transcript)
        self._tasks = {}          # run id -> [task]
        self._span_since = 0.0
        self._first = True
        self._wake = threading.Event()
        self.last_refresh = None
        self.last_duration = None
        # A refresh that keeps throwing leaves last_refresh frozen at its last success, so
        # health has to be judged on the failures too, not just on a timestamp existing.
        self.last_refresh_error = None
        self.failed_refreshes = 0
        # A grade changes no run, so without this a new grade would sit unapplied until unrelated
        # traffic happened to dirty something. force=True would also work, but it throws away every
        # cache and re-parses every source to re-settle outcomes that only need re-finalizing.
        self._regrade = False
        # Scores carried between refreshes, so a refresh re-scores and rewrites only the tasks whose
        # inputs changed instead of the whole store (analysis.ScoreCache). The clock is injectable
        # because outcomes depend on it: a recent task is "in progress", an old one is not.
        self._cache = analysis.ScoreCache()
        self._clock = time.time
        self._settle_due = None      # when the earliest "in progress" outcome settles (refresh is due then)
        self.stats = {"spans_ingested": 0, "refreshes": 0, "alerts_sent": 0, "alerts_dropped": 0, "alerts_retried": 0}
        self.alert_log = {}        # destination id -> recent delivery results, for /api/alerts and the console
        self._alert_wake = threading.Event()
        self._alert_problems = set()
        self._checker_client, self._checker_warned, self._checker_backoff = None, False, 0.0
        self._alerts_pruned = 0.0
        self.sources = {}
        if claude_root:
            self.sources["claude_code"] = SourceStatus("claude_code", "file", claude_root)
        self.sources["sdk"] = SourceStatus("sdk", "push", "/api/ingest")
        self.sources["otlp"] = SourceStatus("otlp", "push", "/v1/traces (OTLP/HTTP json+protobuf)")
        self.sources["langsmith"] = SourceStatus("langsmith", "push", "/langsmith (LangSmith-compatible API)")
        self.sources["aegis"] = SourceStatus("aegis", "push", "Aegis audit records (/api/ingest/records, inbox)")
        self.pullers = []
        for i, sc in enumerate(self.cfg.get("sources") or []):
            self._add_source(i, sc)

    # ------------------------------------------------------------------ config-driven sources
    def _add_source(self, i, sc):
        t = sc.get("type")
        name = sc.get("name") or f"{t}-{i}"
        if t == "claude_code":
            if sc.get("path"):
                self.claude_root = sc["path"]
                self.sources["claude_code"] = SourceStatus("claude_code", "file", sc["path"])
            return
        st = SourceStatus(name, t, sc.get("project") or sc.get("host") or sc.get("path") or "")
        self.sources[name] = st
        self.pullers.append((name, t, sc, st))

    def _run_puller(self, name, t, sc, st):
        state = store.get_state(self.con, name)
        try:
            if t == "langsmith_api":
                runs = langsmith.LangSmithPuller(sc, state).pull()
                self.ingest_langsmith(runs, [], count_source=False)
                st.ok(len(runs))
            elif t == "langfuse_api":
                traces = langfuse.LangfusePuller(sc, state).pull()
                items = [s for tr in traces for s in langfuse.trace_to_spans(tr)]
                self.ingest_spans(items, "langfuse", count_source=False)
                st.ok(len(traces))
            elif t == "anthropic_costs":
                from .collectors import billing
                first, last, rows_ = billing.AnthropicCostPuller(sc, state, self._clock).pull()
                with self.lock:
                    store.replace_billing(self.con, "anthropic", first, last, rows_, self._clock())
                st.ok(len(rows_))
            elif t == "inbox":
                recs = inbox.Inbox(sc["path"], state).poll()
                self.ingest_records(recs)
                st.ok(len(recs))
            else:
                raise ValueError(f"unknown source type '{t}'")
        except Exception as ex:  # a failing connector must never stop the others
            st.fail(ex)
        finally:
            with self.lock:
                store.set_state(self.con, name, state)

    def start_pullers(self):
        for name, t, sc, st in self.pullers:
            interval = float(sc.get("interval", 60 if t != "inbox" else 5))

            def loop(name=name, t=t, sc=sc, st=st, interval=interval):
                while True:
                    # pulling an API is the writer's job (every instance would repeat it); an inbox is a local
                    # directory, read wherever it is configured
                    if t != "inbox" and not self.writer:
                        time.sleep(interval)
                        continue
                    self._run_puller(name, t, sc, st)
                    self._wake.set()
                    time.sleep(interval)
            threading.Thread(target=loop, daemon=True, name=f"src-{name}").start()

    # ------------------------------------------------------------------ outcome grades
    def grade(self, task_id, outcome, reason=None, graded_by=None):
        """State a task's outcome, overriding inference. Durable: survives schema rebuilds."""
        with self.lock:
            store.set_grade(self.con, task_id, outcome, reason, graded_by)
            self._regrade = True
        self._wake.set()

    def grade_by_key(self, key, value, outcome, reason=None, graded_by=None, match="last", projects=None):
        """State the outcome of what ran for `key`=`value` in a trace's metadata (store.set_keyed_grade)."""
        with self.lock:
            store.set_keyed_grade(self.con, key, value, outcome, reason, graded_by, match, projects)
            self._regrade = True
        self._wake.set()

    def ungrade_by_key(self, key, value, projects=None):
        with self.lock:
            n = store.delete_keyed_grade(self.con, key, value, projects)
            self._regrade = True
        self._wake.set()
        return n

    def ungrade(self, task_id):
        """Drop a stated outcome; the task falls back to feedback, then to inference."""
        with self.lock:
            n = store.delete_grade(self.con, task_id)
            self._regrade = True
        self._wake.set()
        return n

    # ------------------------------------------------------------------ push ingestion
    def _scoped_spans(self, spans_, source, scope):
        """Stamp or check canonical spans for a scoped key; raises ScopeError before anything is written."""
        spans_ = [dict(s) for s in spans_]
        by_trace = defaultdict(list)
        for s in spans_:
            by_trace[s.get("trace_id")].append(s)
        bad = []
        for tid, ss in by_trace.items():
            if not tid:
                continue
            # a trace that already exists must already be ours: no adding spans to another project's trace
            have = [_doc_project(source, d) for _, d in store.trace_spans(self.con, source, tid)]
            if have and not all(scope.allows(x) for x in have):
                bad.append(tid)
                continue
            if scope.stamp:
                for s in ss:
                    s["project"] = scope.stamp
                continue
            named = [s.get("project") for s in ss if s.get("project")]
            if not all(scope.allows(x) for x in named) or (not named and not have):
                bad.append(tid)          # a multi-project key must say which of its projects this is
        # the same span id stored elsewhere would be overwritten by the upsert
        ids = [s["span_id"] for s in spans_ if s.get("span_id")]
        for sid, d in store.get_span_docs(self.con, source, ids).items():
            if not scope.allows(_doc_project(source, d)):
                bad.append(d.get("trace_id") or sid)
        if bad:
            raise ScopeError(f"these traces belong to, or would land in, projects outside {scope.projects}", bad)
        return spans_

    def ingest_spans(self, spans_, source, count_source=True, scope=None):
        """Canonical spans (OTLP, Langfuse, inbox) -> durable store."""
        with self.lock:
            if scope is not None:
                spans_ = self._scoped_spans(spans_, source, scope)
            items = [(s["trace_id"], s["span_id"], "canonical", s) for s in spans_ if s.get("trace_id") and s.get("span_id")]
            store.upsert_spans(self.con, source, items)
        self.stats["spans_ingested"] += len(items)
        if count_source and source in self.sources:
            self.sources[source].ok(len(items))
        self._wake.set()
        return len(items)

    def ingest_otlp(self, body, content_type, scope=None):
        return self.ingest_spans(otlp.parse_request(body, content_type), "otlp", scope=scope)

    def _scoped_langsmith(self, posts, patches, feedback, scope):
        """Stamp or check LangSmith runs for a scoped key; raises ScopeError before anything is written."""
        posts, patches, feedback = [dict(r) for r in posts], [dict(r) for r in patches], list(feedback)
        ids = [str(r.get("id")) for r in posts + patches] + [str(f.get("run_id")) for f in feedback]
        have = store.get_span_docs(self.con, "langsmith", ids)
        bad = [rid for rid, d in have.items() if not scope.allows(_doc_project("langsmith", d))]
        created = {str(r.get("id")) for r in posts}
        for r in posts:
            if scope.stamp:
                r["session_name"] = scope.stamp
            elif not scope.allows(r.get("session_name")):
                bad.append(r.get("id"))
        for r in patches:
            rid = str(r.get("id"))
            if rid in have or rid in created:
                continue
            if scope.stamp:                   # a PATCH ahead of its POST: the stub it leaves is ours
                r["session_name"] = scope.stamp
            else:
                bad.append(rid)
        # feedback can't name a project. Ahead of its run (the SDK sends feedback directly but batches runs),
        # a single-project key's stub is stamped like a PATCH's; a multi-project key's can't be placed.
        if not scope.stamp:
            bad += [f.get("run_id") for f in feedback
                    if str(f.get("run_id")) not in have and str(f.get("run_id")) not in created]
        if bad:
            raise ScopeError(f"these runs belong to, or would land in, projects outside {scope.projects}", bad)
        return posts, patches, feedback

    def ingest_langsmith(self, posts, patches, feedback=(), count_source=True, scope=None):
        """LangSmith runs: posts create, patches update. Merged per run id, idempotently."""
        with self.lock:
            if scope is not None:
                posts, patches, feedback = self._scoped_langsmith(posts, patches, feedback, scope)
            ids = [str(r["id"]) for r in list(posts) + list(patches) if r.get("id")]
            ids += [str(f.get("run_id")) for f in feedback if f.get("run_id")]
            existing = store.get_span_docs(self.con, "langsmith", ids)
            merged = {}
            for r in list(posts) + list(patches):
                rid = str(r.get("id"))
                base = merged.get(rid) or existing.get(rid) or {}
                merged[rid] = langsmith.merge(base, r)
            for f in feedback:
                rid = str(f.get("run_id"))
                # feedback can arrive before the (asynchronously batched) run: keep a stub that the run merges into
                stub = {"id": rid, "trace_id": rid, "_stub": True}
                if scope is not None and scope.stamp:      # the stub a scoped key leaves is its project's
                    stub["session_name"] = scope.stamp
                base = merged.get(rid) or existing.get(rid) or stub
                merged[rid] = langsmith.merge(base, {"feedback": [{"key": f.get("key"), "score": f.get("score")}]})
            items = [(str(d.get("trace_id") or rid), rid, "langsmith", d) for rid, d in merged.items()]
            store.upsert_spans(self.con, "langsmith", items)
        self.stats["spans_ingested"] += len(items)
        if count_source:
            self.sources["langsmith"].ok(len(items))
        self._wake.set()
        return len(items)

    def _run_path(self, run_id):
        return os.path.join(self.runs_dir, f"{run_id.replace(':', '_').replace('/', '_')}.json")

    def _stored_run(self, run_id):
        """The payload an SDK run was stored with, or None."""
        if self._runs_in_store:
            return store.get_span_docs(self.con, "sdk", [run_id]).get(run_id)
        try:
            with open(self._run_path(run_id), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {}                      # there, but unreadable: belongs to no project

    def _scoped_generic(self, payload, scope):
        """Stamp or check an SDK run for a scoped key; raises ScopeError before anything is written."""
        run = generic.normalize(payload)
        stored = self._stored_run(run["id"])
        if stored is not None:             # re-sending a run id overwrites that run: it must be ours
            try:
                old = generic.normalize(stored)["project"]
            except Exception:
                old = None
            if not scope.allows(old):
                raise ScopeError(f"run belongs to a project outside {scope.projects}", [run["id"]])
        if scope.stamp:
            return dict(payload, project=scope.stamp)
        if not scope.allows(run["project"]):
            raise ScopeError(f"run names project {run['project']!r}, outside {scope.projects}", [run["id"]])
        return payload

    def ingest(self, payload, scope=None):
        """Generic run JSON (SDK / webhook)."""
        with self.lock:
            if scope is not None:
                payload = self._scoped_generic(payload, scope)
            run = generic.normalize(payload)  # validate before writing
            if self._runs_in_store:
                store.upsert_spans(self.con, "sdk", [(run["id"], run["id"], "run", payload)])
            else:
                with open(self._run_path(run["id"]), "w", encoding="utf-8") as f:
                    json.dump(payload, f)
        self.sources["sdk"].ok(1)
        self._wake.set()
        return run["id"]

    def ingest_runs(self, payloads, scope=None):
        """Several SDK runs, all or nothing for a scoped key: every one is checked before any is written."""
        with self.lock:
            if scope is not None:
                payloads = [self._scoped_generic(r, scope) for r in payloads]
            return [self.ingest(r) for r in payloads]

    def ingest_records(self, recs, scope=None):
        """Auto-detected records from files/log pipelines."""
        by = {"otlp": [], "langsmith": [], "langfuse": [], "span": [], "aegis": [], "generic": []}
        for fmt, r in recs:
            if fmt in by:
                by[fmt].append(r)
        if scope is not None:
            with self.lock:
                return self._scoped_records(by, scope)
        for r in by["generic"]:
            self.ingest(r)
        n = 0
        for doc in by["otlp"]:
            n += self.ingest_spans([otlp.map_span(a, b, c) for a, b, c in otlp.decode_json(doc)], "otlp", count_source=False)
        if by["langsmith"]:
            n += self.ingest_langsmith(by["langsmith"], [], count_source=False)
        for tr in by["langfuse"]:
            n += self.ingest_spans(langfuse.trace_to_spans(tr), "langfuse", count_source=False)
        for s in by["span"]:
            n += self.ingest_spans([s], s.get("source") or "inbox", count_source=False)
        if by["aegis"]:
            n += self.ingest_aegis(by["aegis"])
        return n

    def _scoped_records(self, by, scope):
        """A mixed batch from a scoped key: check every record first, write only if all pass."""
        generic_ = [self._scoped_generic(r, scope) for r in by["generic"]]
        batches = [(self._scoped_spans([otlp.map_span(a, b, c) for a, b, c in otlp.decode_json(d)], "otlp", scope), "otlp")
                   for d in by["otlp"]]
        batches += [(self._scoped_spans(langfuse.trace_to_spans(tr), "langfuse", scope), "langfuse")
                    for tr in by["langfuse"]]
        batches += [(self._scoped_spans([sp], sp.get("source") or "inbox", scope), sp.get("source") or "inbox")
                    for sp in by["span"]]
        ls = self._scoped_langsmith(by["langsmith"], [], [], scope) if by["langsmith"] else None
        self._scoped_aegis(by["aegis"], scope)
        for r in generic_:
            self.ingest(r)
        n = sum(self.ingest_spans(sp, src, count_source=False) for sp, src in batches)
        if ls:
            n += self.ingest_langsmith(ls[0], [], count_source=False)
        if by["aegis"]:
            n += self.ingest_aegis(by["aegis"])
        return n + len(generic_)

    def _scoped_aegis(self, records, scope):
        """Aegis audit records are hash-chained, so they are checked, never stamped: rewriting a record's
        project would alter the audit trail. Each must already name a project in scope."""
        bad = []
        for r in records:
            if not aegis_audit.is_record(r):
                continue
            if not scope.allows(_doc_project("aegis", r)):
                bad.append(aegis_audit.span_id(r))
                continue
            have = [_doc_project("aegis", d) for _, d in store.trace_spans(self.con, "aegis", aegis_audit.group_key(r))]
            if not all(scope.allows(x) for x in have):
                bad.append(aegis_audit.span_id(r))
        if bad:
            raise ScopeError(f"these audit records name projects outside {scope.projects}", bad)

    def ingest_aegis(self, records, scope=None):
        """Aegis audit records (JSONL audit log lines), grouped into runs by correlation id."""
        items = [(aegis_audit.group_key(r), aegis_audit.span_id(r), "aegis", r) for r in records if aegis_audit.is_record(r)]
        with self.lock:
            if scope is not None:
                self._scoped_aegis(records, scope)
            store.upsert_spans(self.con, "aegis", items)
        self.stats["spans_ingested"] += len(items)
        if "aegis" in self.sources:
            self.sources["aegis"].ok(len(items))
        self._wake.set()
        return len(items)

    # ------------------------------------------------------------------ server-side revocation (#8)
    # A directive says "revoke this agent's grants in this project until then". The in-process Aegis
    # integration polls for directives and applies them through Kernel.revoke (integrations/aegis.py):
    # the kernel enforces and audits, and a directive can only take privileges away (invariant 6).

    def revoke(self, agent=None, project=None, reason="operator", minutes=60, source="operator"):
        """Issue a directive. `agent` None revokes a process's whole grant tree; `project` None, every project."""
        now = self._clock()
        with self.lock:
            rid = store.add_revocation(self.con, project, agent, reason, source, now, now + float(minutes) * 60)
            if self.writer:              # into the agent's incident now, not at the next alert tick
                self._update_incidents(())
        self._wake.set()
        return rid

    def restrict(self, agent=None, project=None, reason="operator", minutes=60, tools=(), budget=None,
                 source="operator"):
        """Issue a restrict directive: take `tools` away from the agent's grants, and with `budget`, all but that
        share of what remains of their budget -- in place, through Aegis's Kernel.restrict. The agent keeps
        working with what is left: the step between leaving it alone and revoking it."""
        spec = restrict_spec(tools, budget)
        now = self._clock()
        with self.lock:
            rid = store.add_revocation(self.con, project, agent, reason, source, now, now + float(minutes) * 60,
                                       kind="restrict", spec=spec)
            if self.writer:
                self._update_incidents(())
        self._wake.set()
        return rid

    def clear_revocation(self, rid):
        """Stop a directive applying to new grants; ones it revoked stay revoked (Aegis can't un-revoke)."""
        with self.lock:
            return store.clear_revocation(self.con, rid, self._clock())

    def _detect_probing(self):
        """[enforcement] probing: an agent whose calls the policy keeps refusing across several runs is probing
        it. The in-process Watchdog sees one run; this sees them all, and issues a directive. Off unless
        configured: it acts on running agents."""
        p = (self.cfg.get("enforcement") or {}).get("probing")
        if not p:
            return []
        now = self._clock()
        since = now - float(p.get("window_minutes", 30)) * 60
        issued = store.revocations(self.con, now, limit=10000)
        # a directive restarts the count: denials from before it were what it acted on
        last = {}
        for d in issued:
            if d["source"] == "probing":
                last[(d["project"], d["agent"])] = max(last.get((d["project"], d["agent"]), 0), d["created"])
        active = {(d["project"], d["agent"]) for d in issued if d["cleared"] is None and d["expires"] > now}
        seen = defaultdict(lambda: [0, set(), Counter()])
        for run in self._runs.values():
            if (run.get("ended") or run.get("started") or 0) < since:
                continue                   # only recent runs: this runs every refresh
            project = run.get("project") or "default"
            for s in run["steps"]:
                if not (s.get("denied") and s.get("governed") and s.get("agent")):
                    continue
                key = (project, s["agent"])
                if (s.get("ts") or 0) < max(since, last.get(key, 0)):
                    continue
                c = seen[key]
                c[0] += 1
                c[1].add(run["id"])
                c[2][s.get("rule") or "denied"] += 1
        out = []
        for (project, agent), (n, runs, rules) in seen.items():
            if n < int(p.get("denials", 10)) or len(runs) < int(p.get("runs", 3)) or (project, agent) in active:
                continue
            top = ", ".join(f"{r} x{k}" for r, k in rules.most_common(3))
            reason = (f"probing: {n} denied calls across {len(runs)} runs in "
                      f"{float(p.get('window_minutes', 30)):g} min ({top})")
            out.append(store.add_revocation(self.con, project, agent, reason, "probing", now,
                                            now + float(p.get("revoke_minutes", 60)) * 60))
        return out

    def _detect_tripwires(self):
        """[enforcement.tripwires]: an agent that touched a tripwire in `runs` separate runs within the window
        is revoked wherever it runs. One run is not enough: a single planted document can cause one touch, and
        a directive stops the agent for every user (tripwires.py). The touching run itself is stopped in
        process, before the call, by the Aegis integration. Off unless [enforcement.tripwires] is set: it acts on
        running agents -- touches marked in process are counted too, but only once someone has asked for this."""
        t = (self.cfg.get("enforcement") or {}).get("tripwires") or {}
        minutes = float(t.get("revoke_minutes", 60))
        if not t or minutes <= 0:
            return []
        now = self._clock()
        window = float(t.get("window_minutes", 60)) * 60
        issued = store.revocations(self.con, now, limit=10000)
        last = {}                          # a directive restarts the count, as for probing
        for d in issued:
            if d["source"] == "tripwire":
                last[(d["project"], d["agent"])] = max(last.get((d["project"], d["agent"]), 0), d["created"])
        active = {(d["project"], d["agent"]) for d in issued if d["cleared"] is None and d["expires"] > now}
        seen = defaultdict(lambda: [set(), Counter()])
        for run in self._runs.values():
            if (run.get("ended") or run.get("started") or 0) < now - window:
                continue
            project = run.get("project") or "default"
            for s in run["steps"]:
                if not (s.get("tripwire") and s.get("agent")):
                    continue               # a directive names an agent: without one there is nothing to revoke
                key = (project, s["agent"])
                if (s.get("ts") or 0) <= max(now - window, last.get(key, 0)):
                    continue
                seen[key][0].add(run["id"])
                seen[key][1][s["tripwire"]] += 1
        out = []
        for (project, agent), (runs, what) in seen.items():
            if len(runs) < int(t.get("runs", 2)) or (project, agent) in active:
                continue
            reason = (f"tripwire: touched in {len(runs)} runs within {window / 60:g} min "
                      f"({', '.join(f'{w} x{n}' for w, n in what.most_common(3))})")
            out.append(store.add_revocation(self.con, project, agent, reason, "tripwire", now, now + minutes * 60))
        return out

    # ------------------------------------------------------------------ incidents (incidents.py)
    def update_incidents(self):
        """Attach directives issued since the last look (by the CLI, another instance, the console) to their
        agents' incidents. On the alert tick, so one issued while no traffic arrives still counts; health-rule
        events are attached as each refresh produces them."""
        if not self.writer:
            return []
        with self.lock:
            return self._update_incidents(())

    def _update_incidents(self, events):
        """Under the lock, on the writer. Each signal joins an incident once; an incident that opens or
        becomes critical is alerted, unless it is history (the process's first refresh, or over an hour old)."""
        conf = self.cfg.get("incidents") or {}
        rules = set(conf.get("rules", incmod.RULES))
        now = self._clock()
        mine = [e for e in events if e["rule_id"] in rules]
        tasks = {t["id"]: t for e in mine for t in self._tasks.get(e["run_id"], ())}
        sigs = incmod.from_events(mine, self._runs, tasks, rules) + \
            incmod.from_directives(store.revocations(self.con, now, limit=500))
        known = store.known_signals(self.con, [s["ref"] for s in sigs])
        new = [s for s in sigs if s["ref"] not in known]
        if not new:
            return []
        touched = incmod.group(new, store.open_incidents(self.con), float(conf.get("gap_hours", 24)) * 3600,
                               store.open_incidents(self.con, "resolved"))
        alerts = []
        for inc in touched.values():
            if incmod.SEV.get(inc["severity"], 1) <= incmod.SEV.get(inc["alerted"], 0):
                continue
            # a resolved incident a directive joined (incidents.group) is not paged again
            if inc["status"] == "open" and not self._first and inc["updated"] > now - 3600:
                action = "escalate" if inc["alerted"] else "trigger"
                alerts.append((inc, action))
            inc["alerted"] = inc["severity"]          # announced, or history that never will be
        store.write_incidents(self.con, touched.values(), new)
        if alerts:
            sig = store.incident_signals(self.con, [i["id"] for i, _ in alerts])
            self._queue([alertmod.from_incident(i, incmod.title(i, sig[i["id"]]), a) for i, a in alerts], now)
        return list(touched.values())

    def incident_verdict(self, iid, verdict, note=None, who=None):
        """Resolve an incident as real or a false alarm (`verdict`), or reopen it (None). A resolved incident
        that was alerted is resolved at the destinations too. Works on any instance: the store is shared."""
        if verdict is not None and verdict not in incmod.VERDICTS:
            raise ValueError(f"verdict must be one of {', '.join(incmod.VERDICTS)}, or null to reopen")
        with self.lock:
            now = self._clock()
            inc = store.set_incident_verdict(self.con, iid, verdict, (note or "")[:2000] or None, who, now)
            if inc and verdict and inc["alerted"]:
                sig = store.incident_signals(self.con, [iid])[iid]
                self._queue([alertmod.from_incident(inc, incmod.title(inc, sig), "resolve")], now)
            return inc

    def _detect_low_trust(self):
        """[enforcement.trust]: an agent whose trust (trust.py) is below `restrict_below` loses the tools it
        misused, wherever it runs, for `minutes` -- renewed while it stays low. Not revoked: it keeps doing
        everything else. Off unless configured: it acts on running agents."""
        t = (self.cfg.get("enforcement") or {}).get("trust") or {}
        if not t:
            return []
        now = self._clock()
        below = float(t.get("restrict_below", trustmod.DEFAULTS["low"]))
        tasks = [x for ts in self._tasks.values() for x in ts if x.get("agents")]
        scores = trustmod.score(tasks, trustmod.verdicts(store.incident_verdicts(self.con)), now,
                                trustmod.settings(self.cfg))
        held = {(d["project"], d["agent"]) for d in store.revocations(self.con, now, active=True, limit=10000)
                if d["source"] == "trust" or d["kind"] == "revoke"}      # already restricted, or stopped outright
        out = []
        for a in scores:
            if a["trust"] >= below or (a["project"], a["agent"]) in held:
                continue
            # what it misused; if nothing names a tool (a canary quoted in a model response), its budget
            spec = restrict_spec(a["misused"], None if a["misused"] else 0.5)
            reason = (f"trust {a['trust']:g} < {below:g}: takes away "
                      + (", ".join(spec["tools"]) if spec.get("tools") else "half of what remains of its budget"))
            out.append(store.add_revocation(self.con, a["project"], a["agent"], reason, "trust", now,
                                            now + float(t.get("minutes", 60)) * 60, kind="restrict", spec=spec))
        return out

    # ------------------------------------------------------------------ the checker (checker.py), shadow mode
    CHECKER_TICK_S = 60

    def run_checker(self, cl=None):
        """One pass: review the open incidents that need it, then (grade_outcomes) grade inferred outcomes, within
        the hourly budget. Only the writer, and only when [checker] is set. Returns what it did."""
        conf = checkmod.settings(self.cfg)
        did = {"reviewed": 0, "graded": 0, "errors": 0}
        if not conf or not self.writer or self._clock() < self._checker_backoff:
            return did
        if cl is None:
            cl, why = self._checker_client or checkmod.client()
            self._checker_client = (cl, why)
            if cl is None:
                if not self._checker_warned:
                    print(f"[agentdynamics] checker off: {why}", file=sys.stderr)
                    self._checker_warned = True
                return did
        from .server import Api
        api = Api(self)
        budget = int(conf["max_per_hour"]) - store.checker_calls_since(self.con, self._clock() - 3600)
        open_ = store.incidents(self.con, status="open", limit=200)
        latest = store.latest_reviews(self.con, [i["id"] for i in open_])
        for inc in sorted(open_, key=lambda i: (-(i["updated"] or 0), i["id"])):
            last = latest.get(inc["id"])
            # once per incident, and again when it has twice the signals it was reviewed with
            if budget <= 0 or (last and inc["signals"] < 2 * (last["signals"] or 0)):
                continue
            detail = api.incident(inc["id"])
            answer, error, use, transient = checkmod.ask(cl, conf, checkmod.REVIEW_SYSTEM,
                                                         checkmod.incident_evidence(detail), checkmod.REVIEW_SCHEMA)
            if transient:
                self._checker_backoff = self._clock() + 300     # overloaded or unreachable: try again later
                did["errors"] += 1
                return did
            store.add_review(self.con, inc["id"], inc["signals"], self._clock(), use.get("model", conf["model"]),
                             answer, error, use)
            did["reviewed" if answer else "errors"] += 1
            budget -= 1
        if conf.get("grade_outcomes") and budget > 0:
            from .store import rows
            # Calibration first: tasks people graded, graded blind (the evidence never holds an outcome), so the
            # record of agreeing with people -- the only thing that can justify applying its grades -- grows.
            # Half the budget until min_pairs are graded, a tenth after, to keep watching for drift.
            pairs = rows(self.con, "SELECT COUNT(*) AS n FROM model_grades m JOIN tasks t ON t.id = m.task_id "
                                   "WHERE t.outcome_source = 'graded'")[0]["n"]
            calib = (budget + 1) // 2 if pairs < int(conf["min_pairs"]) else max(1, budget // 10)
            todo = rows(self.con, "SELECT * FROM tasks WHERE outcome_source = 'graded' AND is_subagent = 0 "
                                  "AND id NOT IN (SELECT task_id FROM model_grades) ORDER BY ended DESC, id LIMIT ?", (calib,))
            todo += rows(self.con, "SELECT * FROM tasks WHERE outcome_source = 'inferred' AND outcome <> 'in progress' "
                                   "AND id NOT IN (SELECT task_id FROM model_grades) ORDER BY ended DESC, id LIMIT ?",
                         (budget - len(todo),))
            for t in todo:
                steps = rows(self.con, "SELECT error FROM steps WHERE task_id = ? ORDER BY seq", (t["id"],))
                answer, error, use, transient = checkmod.ask(cl, conf, checkmod.GRADE_SYSTEM,
                                                             checkmod.task_evidence(t, steps), checkmod.GRADE_SCHEMA)
                if transient:
                    self._checker_backoff = self._clock() + 300
                    did["errors"] += 1
                    break
                store.add_model_grade(self.con, t["id"], self._clock(), use.get("model", conf["model"]), answer, error, use)
                did["graded" if answer else "errors"] += 1
        return did

    def _checker_loop(self):
        while True:
            time.sleep(self.CHECKER_TICK_S)
            try:
                self.run_checker()
            except Exception:
                traceback.print_exc()

    def _alert_new_revocations(self):
        """Alert on directives issued since the last look, from wherever they came (this process, another
        instance, the CLI): an agent was stopped, and someone should know why. On the alert tick, so a
        directive issued while no traffic arrives is still announced."""
        if not self.writer:
            return 0
        with self.lock:
            return self._alert_new_revocations_locked()

    def _alert_new_revocations_locked(self):
        now = self._clock()
        recent = store.revocations(self.con, now, limit=500)
        newest = max((d["created"] for d in recent), default=0)
        if self._revocations_seen is None:     # a process's first look: that's history
            self._revocations_seen = newest
            return 0
        new = [d for d in recent if d["created"] > self._revocations_seen]
        self._revocations_seen = max(self._revocations_seen, newest)
        return self._queue([alertmod.from_revocation(d) for d in reversed(new)], now) if new else 0

    # ------------------------------------------------------------------ settings saved from the console
    # Health rules and SLOs. With SQLite they are files in the data directory. On Postgres a saved one goes in
    # the store, where every instance reads it (a file in the data directory still works, as the default).
    @property
    def rules_path(self):
        return os.path.join(self.data_dir, "rules.json")

    def _setting(self, name):
        if self.db_schema:
            v = store.get_state(self.con, f"setting:{name}")
            if v:
                return v["value"]
        path = os.path.join(self.data_dir, f"{name}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return None

    def _save_setting(self, name, value):
        if self.db_schema:
            store.set_state(self.con, f"setting:{name}", {"value": value})
            return
        with open(os.path.join(self.data_dir, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2)

    def rules(self):
        saved = self._setting("rules")
        if saved:
            known = {r["id"] for r in saved}
            return saved + [r for r in analysis.DEFAULT_RULES if r["id"] not in known]  # new built-in rules appear automatically
        return analysis.DEFAULT_RULES

    def save_rules(self, rules):
        self._save_setting("rules", rules)
        self.refresh()                    # re-scores everything (the rules changed); a no-op on a reader

    def slos(self):
        return self._setting("slos") or slomod.DEFAULT_SLOS

    def score_weights(self):
        """The fitted weights of the overall process score, when [scores] weights = "fitted" and the last fit
        predicted stated outcomes better than the defaults (calibrate.py); else None: the defaults."""
        if (self.cfg.get("scores") or {}).get("weights") != "fitted":
            return None
        fit = self._setting("score_fit") or {}
        return fit.get("weights") if fit.get("adopted") else None

    def fit_scores(self, tasks=None):
        """Fit the overall score's weights to the stated outcomes and save the fit (the writer, daily; or on
        demand). Adopted only if it predicts them better than the defaults, out of sample. Returns the report."""
        from . import calibrate
        if tasks is None:
            tasks = [t for ts in self._tasks.values() for t in ts]
        rep = calibrate.fit(tasks)
        self._save_setting("score_fit", {"weights": rep["weights"], "adopted": rep["adopt"], "ts": self._clock(),
                                         "n": rep["n"], "auc_default": rep["auc_default"], "auc_fitted": rep["auc_fitted"]})
        return rep

    def apdex_targets(self):
        """{task type: {"latency_s", "cost"}}: what people say a satisfying task is, per type."""
        return self._setting("apdex_targets") or {}

    def save_apdex_targets(self, targets):
        """Set or clear the targets of the types named: {type: {"latency_s", "cost"}}, or {type: None} to clear.
        A target needs a positive latency (seconds of agent time) or cost (USD), or both; with neither, the type's
        target is cleared. Other types keep theirs. Raises ValueError on anything else, saving nothing."""
        if not isinstance(targets, dict):
            raise ValueError("targets is {type: {latency_s, cost} or null}")
        clean = dict(self.apdex_targets())
        for ty, tg in targets.items():
            if tg is None:
                clean.pop(ty, None)
                continue
            if not isinstance(ty, str) or not ty or not isinstance(tg, dict) or set(tg) - {"latency_s", "cost"}:
                raise ValueError(f"a target is {{type: {{latency_s, cost}}}}, not {ty!r}: {tg!r}")
            t = {}
            for k in ("latency_s", "cost"):
                v = tg.get(k)
                if v in (None, ""):
                    continue
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0:
                    raise ValueError(f"{ty}: {k} must be a positive number, not {v!r}")
                t[k] = float(v)
            if t:
                clean[ty] = t
            else:
                clean.pop(ty, None)
        self._save_setting("apdex_targets", clean)
        self.refresh()                    # re-scores the types whose target changed; a no-op on a reader
        return clean

    def save_slos(self, slos):
        self._save_setting("slos", slos)

    # ------------------------------------------------------------------ refresh
    def _scan_files(self):
        """Returns (changed runs, removed run ids) for file-based sources."""
        found = []
        if self.claude_root and os.path.isdir(self.claude_root):
            found += [("cc", p, None) for p in claude_code.discover(self.claude_root)]
        try:
            # scandir, not listdir + stat: on Windows the listing already carries each file's size and
            # mtime, and a stat per file was 0.6 s of every refresh at 10k SDK runs (scandir: 0.03 s)
            with os.scandir(self.runs_dir) as it:
                entries, runs_dir_gone = [e for e in it if e.name.endswith(".json")], False
        except FileNotFoundError:
            # The directory is ours and was created at startup. If something removed it, make it
            # again rather than raising out of every refresh from here on.
            os.makedirs(self.runs_dir, exist_ok=True)
            entries, runs_dir_gone = [], True
        found += [("sdk", e.path, e) for e in entries]
        changed, seen = [], set()
        for kind, p, entry in found:
            try:
                st = entry.stat() if entry is not None else os.stat(p)
            except OSError:
                continue
            seen.add(p)
            c = self._files.get(p)
            if c and c[0] == st.st_mtime and c[1] == st.st_size:
                continue
            try:
                if kind == "cc":
                    run = claude_code.parse_file(p, self.claude_root)
                else:
                    with open(p, encoding="utf-8") as f:
                        run = generic.normalize(json.load(f))
                    run["file"] = p
            except Exception as ex:  # a malformed file must not take down monitoring
                (self.sources.get("claude_code") if kind == "cc" else self.sources["sdk"]).fail(f"{os.path.basename(p)}: {ex}")
                continue
            old = self._files.get(p)
            self._files[p] = (st.st_mtime, st.st_size, run)
            if old and old[2]["id"] != run["id"]:
                changed.append(("remove", old[2]["id"]))
            changed.append(("upsert", run))
        # A file that is gone means its run was deleted -- sources are the system of record.
        # A whole directory that is gone means we cannot see the source at all, which is not the
        # same statement: treating it as "everything was deleted" would drop every SDK run from
        # the DB because a mount blinked. Keep them and let the next scan decide.
        gone = [p for p in list(self._files) if p not in seen
                and not (runs_dir_gone and os.path.dirname(p) == self.runs_dir)]
        removed = [self._files.pop(p)[2]["id"] for p in gone]
        if "claude_code" in self.sources and any(k == "cc" for k, _, _ in found):
            self.sources["claude_code"].d.update(status="ok", last_ok=time.time(), items=sum(1 for k, _, _ in found if k == "cc"))
        return changed, removed

    def _assemble_traces(self):
        since = self._span_since
        now = time.time()
        touched = store.all_traces(self.con) if since == 0 else store.traces_updated_since(self.con, since - 2)
        runs = []

        def batches(pairs):              # a chunk of traces' spans at a time: never the whole store in memory
            for i in range(0, len(pairs), 500):
                chunk = pairs[i:i + 500]
                docs = store.spans_for_traces(self.con, chunk)
                for st in chunk:
                    yield st, docs.get(st, [])
        # SDK runs first: an Aegis audit trail the in-process integration already recorded as one is skipped
        for (source, trace_id), docs in batches([st for st in touched if st[0] == "sdk"]):
            for _, d in docs:
                # re-read inside the reassembly window but unchanged: not new, as an unchanged file isn't
                sig = hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()
                if self._store_sdk_sig.get(trace_id) == sig and trace_id in self._runs:
                    continue
                self._store_sdk_sig[trace_id] = sig
                try:
                    runs.append(generic.normalize(d))
                except Exception as ex:      # one stored run that can't be read must not stop the rest
                    self.sources["sdk"].fail(f"run {trace_id}: {ex}")
        self._store_sdk_ids.update(r["id"] for r in runs)
        sdk_ids = {f[2]["id"] for f in self._files.values()} | self._store_sdk_ids
        for (source, trace_id), docs in batches([st for st in touched if st[0] != "sdk"]):
            if source == "aegis":
                if trace_id in sdk_ids:
                    continue  # already recorded in-process by agentdynamics.integrations.aegis
                try:
                    run = generic.normalize(aegis_audit.build_payload(trace_id, [d for _, d in docs]))
                except Exception as ex:      # noqa: BLE001 -- as below
                    self.sources["sdk"].fail(f"aegis trace {trace_id}: {ex}")
                    continue
                run["source"] = "aegis"
                runs.append(run)
                continue
            try:
                canon = [c for c in (langsmith.to_span(d) if fmt == "langsmith" else d for fmt, d in docs) if c]
                run = spanmod.build_run(trace_id, canon, source)
            except Exception as ex:          # one trace that can't be assembled must not stop the rest
                (self.sources.get(source) or self.sources["sdk"]).fail(f"trace {trace_id}: {ex}")
                continue
            if run:
                runs.append(run)
        self._span_since = now
        return runs

    def refresh(self, force=False):
        with self.lock:
            t0 = time.time()
            if not self.writer:
                self.writer = store.claim_writer(self.con, self.db_schema)
                if not self.writer:
                    return False          # another instance writes the analysis; this one serves it
            # a grade or a rule change can come from another instance, so it is noticed in the store
            gsig = tuple(v for t in ("grades", "outcome_keys", "model_grades")
                         for v in self.con.execute(f"SELECT COUNT(*), MAX(ts) FROM {t}").fetchone())
            if gsig != self._grades_sig:
                self._regrade = self._grades_sig is not None or self._regrade
                self._grades_sig = gsig
            # rules and Apdex targets: either can be saved on another instance
            rsig = hashlib.sha1(json.dumps([self.rules(), self.apdex_targets(), self.score_weights()], sort_keys=True,
                                           default=str).encode()).hexdigest()
            rules_changed = self._rules_sig is not None and rsig != self._rules_sig
            self._rules_sig = rsig
            if rules_changed:             # every task's events depend on the rules: re-score them all
                self._cache = analysis.ScoreCache()
            if force:
                self._files.clear()
                self._span_since = 0
                self._runs.clear()
                self._tasks.clear()
                self._cache = analysis.ScoreCache()
            changed, removed = self._scan_files()
            dirty = {}
            for op, x in changed:
                if op == "remove":
                    removed.append(x)
                else:
                    dirty[x["id"]] = x
            for run in self._assemble_traces():
                dirty[run["id"]] = run
            # Retention. Never on a process's first refresh: the days about to be purged are rolled up from
            # the analysis already written (store.rollup_daily), so it has to be written first -- after an
            # outage, or on a fresh install importing history, it isn't yet.
            days = float(self.cfg["retention"].get("days") or 0)
            if days > 0 and not self._first:
                cutoff = self._clock() - days * 86400
                self._freeze_rollups(cutoff)
                store.purge_spans_before(self.con, cutoff)
                for rid, r in list(self._runs.items()) + list(dirty.items()):
                    if (r.get("ended") or r.get("started") or cutoff) < cutoff:
                        removed.append(rid)
                        dirty.pop(rid, None)
                        self._drop_run_file(r)
            dirty = {k: v for k, v in dirty.items() if v["steps"]}
            if self.tripwires:            # config is read at start, so a new run is the only thing to mark
                for run in dirty.values():
                    self.tripwires.mark(run)
            # an "in progress" outcome settles as time passes, with or without new traffic
            settle = self._settle_due is not None and self._clock() >= self._settle_due
            if (not dirty and not removed and not force and not self._first and not self._regrade and not settle
                    and not rules_changed):
                return False
            self._regrade = False
            full = force or self._first or rules_changed
            # every task id that may no longer exist: those of removed runs, and the previous tasks of
            # runs being re-analysed (a PATCH can re-segment a run into fewer tasks)
            gone = {t["id"] for rid in list(removed) + list(dirty) for t in self._tasks.get(rid, [])}
            for rid in removed:
                self._runs.pop(rid, None)
                self._tasks.pop(rid, None)
            for rid, run in dirty.items():
                for s in run["steps"]:
                    s.pop("flags", None)
                    s.pop("attributed_cost", None)
                self._runs[rid] = run
                run["_tenant_key"] = (self.cfg.get("analysis") or {}).get("tenant_key")   # which metadata key
                self._tasks[rid] = analysis.run_tasks(run)
            runs = list(self._runs.values())
            tasks, baselines, events = analysis.finalize(runs, self._tasks, self.rules(), now=self._clock(),
                                                         grades=store.get_grades(self.con),
                                                         cache=self._cache, dirty=dirty.keys(),
                                                         redact=self.redactor.text, targets=self.apdex_targets(),
                                                         keyed=store.get_keyed_grades(self.con),
                                                         model_grades=store.get_model_grades(self.con),
                                                         promote=checkmod.promotion(self.cfg),
                                                         weights=self.score_weights())
            sc = self.cfg.get("scores") or {}
            if sc.get("weights") == "fitted" and self.writer:
                last = (self._setting("score_fit") or {}).get("ts") or 0
                if self._clock() - last >= float(sc.get("refit_hours", 24)) * 3600:
                    self.fit_scores(tasks)        # applied from the next refresh, which re-scores everything once
            self._settle_due = min((t["ended"] + analysis.IN_PROGRESS_S for t in tasks
                                    if t.get("outcome") == "in progress" and t.get("ended")), default=None)
            # Process Review insights are computed when the page asks, over the tasks it shows: every
            # filter needed that anyway, and computing them here cost 28% of each refresh at 20k tasks.
            meta = {"refreshed": time.time(), "runs": len(runs), "tasks": len(tasks)}
            store.write_runs(self.con, list(dirty.values()), removed, self.redactor)
            if full:
                # the first refresh of a process must replace whatever the last process left behind
                store.write_analysis(self.con, tasks, baselines, events, meta, self.redactor)
            else:
                changed = self._cache.changed
                events = [e for e in events if e["task_id"] in changed]
                store.write_analysis_delta(self.con, [t for t in tasks if t["id"] in changed],
                                           gone - changed, events, baselines, meta, self.redactor)
            self.stats["tasks_rescored"] = len(self._cache.changed)
            self._queue_event_alerts(events)
            self._detect_probing()
            self._detect_tripwires()
            self._detect_low_trust()
            self._update_incidents(events)
            self._first = False
            self.last_refresh = time.time()
            self.last_duration = round(self.last_refresh - t0, 2)
            self.last_refresh_error, self.failed_refreshes = None, 0
            self.stats["refreshes"] += 1
            return True

    def _freeze_rollups(self, cutoff):
        """Roll up every local day that the retention cutoff has reached, once, before anything in it is
        purged. A day is frozen when the cutoff enters it: its tasks are all still here, and all at least
        retention-minus-one days old. Later changes to a frozen day (a backfill older than retention) are
        not reflected."""
        last = _midnight(cutoff)
        through, through_end = store.rollup_boundary(self.con)
        if through_end and through_end > last:
            return
        if through_end:
            start = through_end
        else:
            first = self.con.execute("SELECT MIN(started) FROM tasks").fetchone()[0]
            start = _midnight(min(first, last)) if first else last
        days = []
        while start <= last:
            end = _next_midnight(start)
            days.append((time.strftime("%Y-%m-%d", time.localtime(start)), start, end))
            start = end
        store.freeze_days(self.con, days, days[-1][0], days[-1][2])

    def _drop_run_file(self, run):
        """An SDK run past retention: its file in runs/ is our copy, so it goes too. Files elsewhere (Claude
        Code transcripts) belong to the user and are never touched."""
        path = run.get("file")
        if not path or os.path.dirname(os.path.abspath(path)) != os.path.abspath(self.runs_dir):
            return
        try:
            os.remove(path)
        except OSError:
            pass
        self._files.pop(path, None)

    def watch(self, interval=None):
        interval = interval or float(self.cfg["analysis"].get("interval", 15))

        def loop():
            while True:
                self._wake.wait(interval)
                if self._wake.is_set():
                    time.sleep(1.0)  # debounce bursts of pushed spans
                    self._wake.clear()
                try:
                    self.refresh()
                except Exception as ex:
                    self.last_refresh_error = f"{type(ex).__name__}: {ex}"[:400]
                    self.failed_refreshes += 1
                    traceback.print_exc()
        threading.Thread(target=loop, daemon=True, name="analyzer").start()
        threading.Thread(target=self._alert_loop, daemon=True, name="alerts").start()
        if checkmod.settings(self.cfg):
            threading.Thread(target=self._checker_loop, daemon=True, name="checker").start()
        self.start_pullers()

    # ------------------------------------------------------------------ alerting (alerts.py)
    ALERT_TICK_S = 30

    def alert_destinations(self):
        dests, problems = alertmod.destinations(self.cfg["alerts"])
        for p in problems:
            if p not in self._alert_problems:        # say it once, not every tick
                self._alert_problems.add(p)
                print(f"[agentdynamics] alert destination ignored: {p}", file=sys.stderr)
        return dests

    def _queue(self, alerts, now):
        """Render alerts for every destination that routes them, and queue the bodies."""
        items = []
        console_url = self.cfg["alerts"].get("console_url") or ""
        for d in self.alert_destinations():
            mine = [a for a in alerts if alertmod.routes(d, a)]
            items += [(d["id"], b) for b in alertmod.render(d, mine, console_url)]
        if items:
            store.outbox_add(self.con, items, now)
            self._alert_wake.set()
        return len(items)

    def _queue_event_alerts(self, events):
        """Health-rule events this refresh produced that were never alerted before (called under the lock)."""
        now = self._clock()
        ids = [e["id"] for e in events]
        sent = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            sent |= {r[0] for r in self.con.execute(
                f"SELECT event_id FROM alerts_sent WHERE event_id IN ({','.join('?' * len(chunk))})", chunk)}
        new = [e for e in events if e["id"] not in sent]
        with self.con:
            self.con.executemany("INSERT OR IGNORE INTO alerts_sent (event_id, ts) VALUES (?, ?)",
                                 [(e["id"], now) for e in new])
            if now - self._alerts_pruned > 3600:
                # only events from the last hour are ever sent, so a week of ids is plenty to dedupe against
                self.con.execute("DELETE FROM alerts_sent WHERE ts < ?", (now - 7 * 86400,))
                self._alerts_pruned = now
        if self._first or not new:            # never flood a channel with history on first start
            return 0
        recent = [e for e in new if (e.get("ts") or 0) > now - 3600]
        return self._queue([alertmod.from_event(e) for e in recent], now)

    def check_slos(self):
        """Compare the SLO alerts that should be firing with those that are, and queue triggers and
        resolves. Runs on the alert tick, not only on refresh: a burn stops as time passes with no traffic.
        Only on the writer: a reader holds no tasks, so to it nothing is firing -- it would resolve every page."""
        if not self.writer:
            return []
        with self.lock:
            dests = self.alert_destinations()
            if not any("slos" in d["kinds"] for d in dests):
                return []
            now = self._clock()
            live = [t for ts in self._tasks.values() for t in ts
                    if not t.get("is_subagent") and (t.get("llm_calls") or t.get("tool_calls"))]
            state = store.alert_state(self.con)
            firing = slomod.alert_conditions(live, self.slos(), now,
                                             int(self.cfg["alerts"].get("slo_min_tasks", 10)), firing=set(state))
            started = {k: v for k, v in firing.items() if k not in state}
            stopped = [k for k in state if k not in firing]
            store.set_alert_state(self.con, started, stopped, now)
            out = ([alertmod.from_slo(k, c, "trigger", now) for k, c in started.items()]
                   + [alertmod.from_slo(k, state[k], "resolve", now) for k in stopped])
            self._queue(out, now)
            return out

    def deliver_alerts(self):
        """Send what is due from the outbox. Per destination, strictly in order: a message waiting to be
        retried holds back the ones queued after it, so a resolve never overtakes its trigger. Only the writer
        delivers: the outbox is shared, and two senders would page twice."""
        if not self.writer:
            return 0
        dests = {d["id"]: d for d in self.alert_destinations()}
        with self.lock:
            pending = store.outbox_pending(self.con)
        now, held, sent = self._clock(), set(), 0
        for row in pending:
            d = dests.get(row["dest"])
            if d is None:                      # the destination was removed from the config
                with self.lock:
                    store.outbox_done(self.con, row["id"])
                continue
            if row["dest"] in held:
                continue
            if row["next_try"] > now:
                held.add(row["dest"])
                continue
            ok, retryable, detail = alertmod.send(d, json.loads(row["body"]))
            log = self.alert_log.setdefault(d["id"], {"sent": 0, "dropped": 0, "last_ok": None,
                                                      "last_error": None, "last_error_at": None})
            with self.lock:
                if ok:
                    store.outbox_done(self.con, row["id"])
                    log["sent"] += 1
                    log["last_ok"] = now
                    self.stats["alerts_sent"] += 1
                    sent += 1
                    continue
                log["last_error"], log["last_error_at"] = detail, now
                if retryable and row["created"] > now - alertmod.GIVE_UP_S:
                    store.outbox_retry(self.con, row["id"], row["attempts"] + 1,
                                       now + alertmod.backoff(row["attempts"]), detail)
                    self.stats["alerts_retried"] += 1
                    held.add(row["dest"])
                else:
                    store.outbox_done(self.con, row["id"])
                    log["dropped"] += 1
                    self.stats["alerts_dropped"] += 1
                    print(f"[agentdynamics] alert to {d['id']} dropped: {detail}", file=sys.stderr)
        return sent

    def _alert_loop(self):
        while True:
            self._alert_wake.wait(self.ALERT_TICK_S)
            self._alert_wake.clear()
            try:
                self.check_slos()
                self._alert_new_revocations()
                self.update_incidents()
                self.deliver_alerts()
            except Exception:
                traceback.print_exc()
