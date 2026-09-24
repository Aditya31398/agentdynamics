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
import threading
import time
import traceback
import urllib.request

from . import analysis, config as cfgmod, pricing, store
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
        self.stats = {"spans_ingested": 0, "refreshes": 0, "alerts_sent": 0}
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

    # ------------------------------------------------------------------ push ingestion
    def ingest_spans(self, spans_, source, count_source=True):
        """Canonical spans (OTLP, Langfuse, inbox) -> durable store."""
        items = [(s["trace_id"], s["span_id"], "canonical", s) for s in spans_ if s.get("trace_id") and s.get("span_id")]
        with self.lock:
            store.upsert_spans(self.con, source, items)
        self.stats["spans_ingested"] += len(items)
        if count_source and source in self.sources:
            self.sources[source].ok(len(items))
        self._wake.set()
        return len(items)

    def ingest_otlp(self, body, content_type):
        return self.ingest_spans(otlp.parse_request(body, content_type), "otlp")

    def ingest_langsmith(self, posts, patches, feedback=(), count_source=True):
        """LangSmith runs: posts create, patches update. Merged per run id, idempotently."""
        with self.lock:
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
                base = merged.get(rid) or existing.get(rid) or {"id": rid, "trace_id": rid, "_stub": True}
                merged[rid] = langsmith.merge(base, {"feedback": [{"key": f.get("key"), "score": f.get("score")}]})
            items = [(str(d.get("trace_id") or rid), rid, "langsmith", d) for rid, d in merged.items()]
            store.upsert_spans(self.con, "langsmith", items)
        self.stats["spans_ingested"] += len(items)
        if count_source:
            self.sources["langsmith"].ok(len(items))
        self._wake.set()
        return len(items)

    def ingest(self, payload):
        """Generic run JSON (SDK / webhook)."""
        run = generic.normalize(payload)  # validate before writing
        path = os.path.join(self.runs_dir, f"{run['id'].replace(':', '_').replace('/', '_')}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        self.sources["sdk"].ok(1)
        self._wake.set()
        return run["id"]

    def ingest_records(self, recs):
        """Auto-detected records from files/log pipelines."""
        by = {"otlp": [], "langsmith": [], "langfuse": [], "span": [], "aegis": []}
        for fmt, r in recs:
            if fmt == "generic":
                self.ingest(r)
            elif fmt in by:
                by[fmt].append(r)
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

    def ingest_aegis(self, records):
        """Aegis audit records (JSONL audit log lines), grouped into runs by correlation id."""
        items = [(aegis_audit.group_key(r), aegis_audit.span_id(r), "aegis", r) for r in records if aegis_audit.is_record(r)]
        with self.lock:
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
            if not dirty and not removed and not force and not self._first:
                return False
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
            tasks, baselines, events = analysis.finalize(runs, self._tasks, self.rules())
            insights = analysis.process_insights(tasks)
            meta = {"refreshed": time.time(), "runs": len(runs), "tasks": len(tasks), "insights": insights}
            store.write_runs(self.con, list(dirty.values()), removed, self.redactor)
            store.write_analysis(self.con, tasks, baselines, events, meta, self.redactor)
            self._dispatch_alerts(events)
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
        self.start_pullers()

    # ------------------------------------------------------------------ alerting
    SEV = {"info": 1, "warning": 2, "critical": 3}

    def _dispatch_alerts(self, events):
        hooks = self.cfg["alerts"].get("webhooks") or []
        sent = {r[0] for r in self.con.execute("SELECT event_id FROM alerts_sent")}
        new = [e for e in events if e["id"] not in sent]
        if not new:
            return
        with self.con:
            self.con.executemany("INSERT OR IGNORE INTO alerts_sent (event_id, ts) VALUES (?, ?)", [(e["id"], time.time()) for e in new])
        if self._first or not hooks:  # never flood a channel with history on first start
            return
        recent = [e for e in new if (e.get("ts") or 0) > time.time() - 3600]
        for h in hooks:
            lvl = self.SEV.get(h.get("min_severity", "warning"), 2)
            batch = [e for e in recent if self.SEV.get(e["severity"], 1) >= lvl]
            if batch:
                threading.Thread(target=self._post_hook, args=(h, batch), daemon=True).start()

    def _post_hook(self, hook, events):
        if hook.get("format", "json") == "slack":
            lines = [f"*{e['severity'].upper()}* · {e['rule']} · {e['project']} / {e['task_type']}\n{e['message']}" for e in events[:10]]
            body = {"text": "AgentDynamics alerts\n" + "\n\n".join(lines)}
        else:
            body = {"source": "agentdynamics", "events": events}
        try:
            req = urllib.request.Request(hook["url"], data=json.dumps(body, default=str).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
            self.stats["alerts_sent"] += len(events)
        except Exception as ex:
            print(f"[agentdynamics] alert webhook failed: {ex}")
