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
import json
import os
import sys
import threading
import time
import traceback
from collections import defaultdict

from . import alerts as alertmod, analysis, config as cfgmod, pricing, slo as slomod, store
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


class Engine:
    def __init__(self, data_dir, claude_root=claude_code.DEFAULT_ROOT, cfg=None):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.cfg = cfg or cfgmod.load(data_dir)
        self.claude_root = claude_root
        self.runs_dir = os.path.join(data_dir, "runs")
        os.makedirs(self.runs_dir, exist_ok=True)
        pricing.load_overrides(data_dir)
        self.db_path = os.path.join(data_dir, "agentdynamics.db")
        self.con = store.connect(self.db_path)
        self.lock = threading.RLock()
        self.redactor = Redactor(self.cfg["privacy"])
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
        self.stats = {"spans_ingested": 0, "refreshes": 0, "alerts_sent": 0, "alerts_dropped": 0, "alerts_retried": 0}
        self.alert_log = {}        # destination id -> recent delivery results, for /api/alerts and the console
        self._alert_wake = threading.Event()
        self._alert_problems = set()
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

    def _scoped_generic(self, payload, scope):
        """Stamp or check an SDK run for a scoped key; raises ScopeError before anything is written."""
        run = generic.normalize(payload)
        path = self._run_path(run["id"])
        if os.path.exists(path):           # re-sending a run id overwrites that run: it must be ours
            try:
                with open(path, encoding="utf-8") as f:
                    old = generic.normalize(json.load(f))["project"]
            except (OSError, ValueError):
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

    # ------------------------------------------------------------------ health rules config
    @property
    def rules_path(self):
        return os.path.join(self.data_dir, "rules.json")

    def rules(self):
        if os.path.exists(self.rules_path):
            with open(self.rules_path, encoding="utf-8") as f:
                saved = json.load(f)
            known = {r["id"] for r in saved}
            return saved + [r for r in analysis.DEFAULT_RULES if r["id"] not in known]  # new built-in rules appear automatically
        return analysis.DEFAULT_RULES

    def save_rules(self, rules):
        with open(self.rules_path, "w", encoding="utf-8") as f:
            json.dump(rules, f, indent=2)
        self.refresh(force=True)

    # ------------------------------------------------------------------ refresh
    def _scan_files(self):
        """Returns (changed runs, removed run ids) for file-based sources."""
        found = []
        if self.claude_root and os.path.isdir(self.claude_root):
            found += [("cc", p) for p in claude_code.discover(self.claude_root)]
        try:
            names, runs_dir_gone = os.listdir(self.runs_dir), False
        except FileNotFoundError:
            # The directory is ours and was created at startup. If something removed it, make it
            # again rather than raising out of every refresh from here on.
            os.makedirs(self.runs_dir, exist_ok=True)
            names, runs_dir_gone = [], True
        found += [("sdk", os.path.join(self.runs_dir, fn)) for fn in names if fn.endswith(".json")]
        changed, seen = [], set()
        for kind, p in found:
            try:
                st = os.stat(p)
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
        if "claude_code" in self.sources and any(k == "cc" for k, _ in found):
            self.sources["claude_code"].d.update(status="ok", last_ok=time.time(), items=sum(1 for k, _ in found if k == "cc"))
        return changed, removed

    def _assemble_traces(self):
        since = self._span_since
        now = time.time()
        touched = store.all_traces(self.con) if since == 0 else store.traces_updated_since(self.con, since - 2)
        runs = []
        sdk_ids = {f[2]["id"] for f in self._files.values()}
        for source, trace_id in touched:
            docs = store.trace_spans(self.con, source, trace_id)
            if source == "aegis":
                if trace_id in sdk_ids:
                    continue  # already recorded in-process by agentdynamics.integrations.aegis
                run = generic.normalize(aegis_audit.build_payload(trace_id, [d for _, d in docs]))
                run["source"] = "aegis"
                runs.append(run)
                continue
            canon = [c for c in (langsmith.to_span(d) if fmt == "langsmith" else d for fmt, d in docs) if c]
            run = spanmod.build_run(trace_id, canon, source)
            if run:
                runs.append(run)
        self._span_since = now
        return runs

    def refresh(self, force=False):
        with self.lock:
            t0 = time.time()
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
            # retention
            days = float(self.cfg["retention"].get("days") or 0)
            if days > 0:
                cutoff = time.time() - days * 86400
                store.purge_spans_before(self.con, cutoff)
                for rid, r in list(self._runs.items()) + list(dirty.items()):
                    if (r.get("ended") or r.get("started") or time.time()) < cutoff:
                        removed.append(rid)
                        dirty.pop(rid, None)
            dirty = {k: v for k, v in dirty.items() if v["steps"]}
            if not dirty and not removed and not force and not self._first and not self._regrade:
                return False
            self._regrade = False
            full = force or self._first
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
                self._tasks[rid] = analysis.run_tasks(run)
            runs = list(self._runs.values())
            tasks, baselines, events = analysis.finalize(runs, self._tasks, self.rules(), now=self._clock(),
                                                         grades=store.get_grades(self.con),
                                                         cache=self._cache, dirty=dirty.keys(),
                                                         redact=self.redactor.text)
            insights = analysis.process_insights(tasks)
            meta = {"refreshed": time.time(), "runs": len(runs), "tasks": len(tasks), "insights": insights}
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
            self._first = False
            self.last_refresh = time.time()
            self.last_duration = round(self.last_refresh - t0, 2)
            self.last_refresh_error, self.failed_refreshes = None, 0
            self.stats["refreshes"] += 1
            return True

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
        resolves. Runs on the alert tick, not only on refresh: a burn stops as time passes with no traffic."""
        with self.lock:
            dests = self.alert_destinations()
            if not any("slos" in d["kinds"] for d in dests):
                return []
            now = self._clock()
            live = [t for ts in self._tasks.values() for t in ts
                    if not t.get("is_subagent") and (t.get("llm_calls") or t.get("tool_calls"))]
            state = store.alert_state(self.con)
            firing = slomod.alert_conditions(live, slomod.load(self.data_dir), now,
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
        retried holds back the ones queued after it, so a resolve never overtakes its trigger."""
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
                self.deliver_alerts()
            except Exception:
                traceback.print_exc()
