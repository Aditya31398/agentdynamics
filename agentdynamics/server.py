"""HTTP API + static web console (stdlib only)."""
import gzip
import hmac
import json
import mimetypes
import os
import traceback
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .analysis import OUTCOMES
from .api.assess import AssessMixin
from .api.base import DAY, ApiBase, mcp_group  # noqa: F401  (re-exported)
from .api.diagnose import DiagnoseMixin
from .api.governance import GovernanceMixin
from .api.monitor import MonitorMixin
from .api.ops import OpsMixin
from .engine import IngestScope, ScopeError

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


class Api(MonitorMixin, DiagnoseMixin, AssessMixin, GovernanceMixin, OpsMixin, ApiBase):
    """Every endpoint the console and CLI read. The methods live in agentdynamics/api/, one module
    per area of the console; this class only composes them, so `from agentdynamics.server import
    Api` keeps working."""


# ---------------------------------------------------------------- HTTP layer

ROLE_FOR = {"ingest": 1, "read": 2, "admin": 3}
# ingest keys only write telemetry, read keys only read, admin can do everything
CAN = {1: {"ingest"}, 2: {"read"}, 3: {"ingest", "read", "admin"}}
MAX_BODY = 64 * 1024 * 1024
# A client hanging up mid-request (a reload, a closed tab, an aborted fetch) is routine, not a server error.
# Caught only around I/O on the client's socket: the same errors raised by our own code are still real errors.
CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class ClientGone(Exception):
    """The client hung up while its request body was being read: there is no one left to answer."""


class Handler(BaseHTTPRequestHandler):
    api = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        # Covers the socket I/O http.server does itself: the request line and headers (a keep-alive
        # connection reset while idle) and its own error replies. Ours goes through _send and _body.
        try:
            super().handle_one_request()
        except CLIENT_GONE:
            self.close_connection = True

    def _send(self, code, body, ctype="application/json", extra_headers=None):
        if isinstance(body, bytes):
            data = body
        elif isinstance(body, str):
            data = body.encode()
        else:
            data = json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        try:
            self.end_headers()
            self.wfile.write(data)
        except CLIENT_GONE:
            self.close_connection = True

    # --- auth: API keys with roles ingest < read < admin
    def _key(self):
        """The key record this request authenticated with, or None. Auth off: the local admin."""
        auth = self.api.e.cfg["auth"]
        if not auth.get("enabled"):
            return {"name": "local", "role": "admin"}
        key = self.headers.get("x-api-key") or ""
        h = self.headers.get("Authorization") or ""
        if h.lower().startswith("bearer "):
            key = h[7:].strip()
        for k in auth.get("keys") or []:
            if key and hmac.compare_digest(key, str(k.get("key", ""))):
                if k.get("role") == "admin" and "projects" in k:
                    # admin edits install-wide rules and SLOs; a "scoped admin" can't mean anything
                    # safe, so the key is refused rather than guessed at
                    return {"name": k.get("name"), "role": None,
                            "invalid": "admin keys cannot be scoped to projects; use a read or ingest key"}
                return k
        return None

    def _role(self):
        k = self._key()
        return ROLE_FOR.get(k.get("role"), 0) if k else 0

    def _key_name(self):
        """Who is calling, for the audit trail on a grade: the matched key's name, or 'local'."""
        k = self._key()
        return (k.get("name") or k.get("role")) if k else None

    def _scope(self):
        """The projects this key may see, or None for the whole install. Only a missing `projects` field
        means everything: an empty list means nothing, so a hand-edited [] can never widen access."""
        k = self._key()
        return list(k["projects"]) if k and isinstance(k.get("projects"), list) else None

    def _require(self, *need):
        """Whether the key has any of the capabilities in `need`; answers 401/403 if not."""
        k = self._key()
        if k and k.get("invalid"):
            self._send(403, {"error": k["invalid"]})
            return False
        r = self._role()
        if CAN.get(r, set()) & set(need):
            return True
        want = " or ".join(f"'{n}'" for n in need)
        self._send(401 if r == 0 else 403, {"error": "unauthorized" if r == 0 else f"requires {want} role"},
                   extra_headers={"WWW-Authenticate": "Bearer"} if r == 0 else None)
        return False

    def _scoped_api(self):
        """The Api this request may use: the shared one, or a per-request one confined to the key's
        projects. The caller closes a scoped one (it owns a connection)."""
        scope = self._scope()
        return self.api if scope is None else type(self.api)(self.api.e, projects=scope)

    INSTALL_WIDE = ("/metrics", "/api/sources", "/api/config", "/api/alerts")   # nothing here belongs to one project

    def _ingest_scope(self):
        """What this key may write: None for the whole install, else an engine.IngestScope."""
        scope = self._scope()
        return None if scope is None else IngestScope(scope)

    def _refuse(self, ex):
        return self._send(403, {"error": ex.reason, "ids": ex.ids})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("payload too large")
        try:
            body = self.rfile.read(n) if n else b""
            self._body_read = True
        except CLIENT_GONE as ex:
            self.close_connection = True
            raise ClientGone() from ex
        enc = (self.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            body = gzip.decompress(body)
        elif enc == "deflate":
            body = zlib.decompress(body)
        elif enc == "zstd":
            from .collectors.langsmith import zstd_available, zstd_decompress
            if not zstd_available():
                raise ValueError("Content-Encoding zstd needs `pip install zstandard`")
            body = zstd_decompress(body)
            if len(body) > MAX_BODY:
                raise ValueError("payload too large")
        elif enc and enc != "identity":
            raise ValueError(f"unsupported Content-Encoding {enc}")
        return body

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items() if v and v[0] != ""}
        p = u.path
        api = self.api
        try:
            if p == "/healthz":
                return self._send(200, api.healthz())
            if p in ("/langsmith/info", "/langsmith/api/v1/info"):
                from .collectors.langsmith import info
                return self._send(200, info())
            if p.startswith("/api/") or p == "/metrics":
                # the in-process Aegis integration polls revocations with the app's (ingest) key
                if not self._require(*(("read", "ingest") if p == "/api/revocations" else ("read",))):
                    return
                if self._scope() is not None:
                    if p in self.INSTALL_WIDE:
                        return self._send(403, {"error": f"{p} covers the whole install; it needs a key that "
                                                         f"isn't scoped to projects"})
                    api = self._scoped_api()
            if p == "/metrics":
                return self._send(200, api.prometheus(), "text/plain; version=0.0.4")
            routes = {"/api/filters": api.filters, "/api/overview": api.overview, "/api/types": api.types,
                      "/api/tasks": api.task_list, "/api/sessions": api.sessions, "/api/flowmap": api.flowmap,
                      "/api/tools": api.tools, "/api/models": api.models, "/api/events": api.events,
                      "/api/process": api.process, "/api/analytics": api.analytics, "/api/compare": api.compare,
                      "/api/workflows": api.workflows, "/api/workflow": api.workflow, "/api/slos": api.slos,
                      "/api/sources": api.sources, "/api/config": api.config, "/api/connect": api.connect,
                      "/api/alerts": api.alerts, "/api/revocations": api.revocations, "/api/incidents": api.incidents,
                      "/api/trust": api.trust, "/api/checker": api.checker,
                      "/api/governance": api.governance, "/api/governance/policy": api.export_policy}
            if p in routes:
                r = routes[p](q)
                return self._send(200 if r is not None else 404, r if r is not None else {"error": "not found"})
            if p.startswith("/api/task/"):
                r = api.task(unquote(p[len("/api/task/"):]))
                return self._send(200 if r else 404, r or {"error": "not found"})
            if p.startswith("/api/incident/"):
                r = api.incident(unquote(p[len("/api/incident/"):]))
                return self._send(200 if r else 404, r or {"error": "not found"})
            if p == "/api/rules":
                return self._send(200, {"rules": api.e.rules()})
            if p == "/api/whoami":
                return self._send(200, {"role": {3: "admin", 2: "read", 1: "ingest"}.get(self._role()),
                                        "projects": self._scope()})
            if p.startswith("/api/") or p.startswith("/langsmith/"):
                return self._send(404 if p.startswith("/api/") else 200, {"error": "not found"} if p.startswith("/api/") else {})
        except Exception as ex:  # surface errors to the console instead of a dropped connection
            traceback.print_exc()
            return self._send(500, {"error": str(ex)})
        finally:
            # a scoped Api owns its connection; the shared one holds one per request thread (a pooled one on
            # Postgres), so this request's goes back either way
            api.close()
        # static console
        rel = "index.html" if p in ("/", "") else p.lstrip("/")
        path = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not path.startswith(WEB_DIR) or not os.path.isfile(path):
            path = os.path.join(WEB_DIR, "index.html")
        with open(path, "rb") as f:
            self._send(200, f.read(), mimetypes.guess_type(path)[0] or "application/octet-stream")

    def _unread_body_closes(self, handler):
        """Run a POST/PATCH handler. A body it didn't read (a route that takes none, or a request refused before
        reading it) would stay in the socket and become the start of the next request on a keep-alive
        connection -- "{}GET /api/..." answered 501 -- so that connection is closed after the response."""
        self._body_read = False
        try:
            return handler()
        finally:
            n = int(self.headers.get("Content-Length") or 0)
            if not self._body_read and n > 0:
                if n <= 1 << 20:             # small (the console's "{}"): read it off, the connection stays usable
                    try:
                        self.rfile.read(n)
                    except CLIENT_GONE:
                        self.close_connection = True
                else:                        # not worth reading 64 MB to discard: end the connection
                    self.close_connection = True

    def do_PATCH(self):
        return self._unread_body_closes(self._patch)

    def _patch(self):
        u = urlparse(self.path)
        try:
            if u.path.startswith("/langsmith/") and "/runs/" in u.path:
                if not self._require("ingest"):
                    return
                rid = u.path.rstrip("/").rsplit("/", 1)[-1]
                patch = json.loads(self._body() or b"{}")
                patch["id"] = rid
                self.api.e.ingest_langsmith([], [patch], scope=self._ingest_scope())
                return self._send(200, {})
        except ScopeError as ex:
            return self._refuse(ex)
        except ClientGone:
            return
        except Exception as ex:
            return self._send(400, {"error": str(ex)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        return self._unread_body_closes(self._post)

    def _post(self):
        u = urlparse(self.path)
        p = u.path
        e = self.api.e
        try:
            # ---- telemetry ingestion (role: ingest)
            if p in ("/v1/traces", "/otlp/v1/traces"):
                if not self._require("ingest"):
                    return
                ctype = self.headers.get("Content-Type") or ""
                n = e.ingest_otlp(self._body(), ctype, scope=self._ingest_scope())
                if "protobuf" in ctype:
                    return self._send(200, b"", "application/x-protobuf")  # empty ExportTraceServiceResponse
                return self._send(200, {"partialSuccess": {}, "accepted": n})
            if p.startswith("/langsmith/"):
                if not self._require("ingest"):
                    return
                from .collectors import langsmith as ls
                sub = p[len("/langsmith"):].replace("/api/v1", "")
                body = self._body()
                if sub == "/runs/batch":
                    posts, patches = ls.parse_batch(body)
                    e.ingest_langsmith(posts, patches, scope=self._ingest_scope())
                elif sub == "/runs/multipart":
                    posts, patches, fb = ls.parse_multipart(body, self.headers.get("Content-Type") or "")
                    e.ingest_langsmith(posts, patches, fb, scope=self._ingest_scope())
                elif sub == "/runs":
                    e.ingest_langsmith([json.loads(body or b"{}")], [], scope=self._ingest_scope())
                elif sub == "/feedback":
                    e.ingest_langsmith([], [], [json.loads(body or b"{}")], scope=self._ingest_scope())
                else:
                    return self._send(200, {})  # accept and ignore other LangSmith calls (datasets, sessions...)
                return self._send(202, {})
            if p == "/api/ingest":
                if not self._require("ingest"):
                    return
                payload = json.loads(self._body() or b"{}")
                runs = payload if isinstance(payload, list) else [payload]
                return self._send(200, {"ok": True, "ids": e.ingest_runs(runs, scope=self._ingest_scope())})
            if p == "/api/ingest/records":
                # log pipelines (Fluent Bit / Vector / Logstash HTTP outputs): JSON array or NDJSON of any supported format
                if not self._require("ingest"):
                    return
                from .collectors.inbox import detect, unwrap
                body = self._body().strip()
                recs = json.loads(body) if body.startswith(b"[") else [json.loads(x) for x in body.splitlines() if x.strip()]
                recs = [unwrap(r) for r in recs]
                n = e.ingest_records([(detect(r), r) for r in recs if detect(r)], scope=self._ingest_scope())
                return self._send(200, {"ok": True, "accepted": n, "received": len(recs)})
            if p == "/api/policy/check":
                # reads recorded traffic, writes nothing: a CI job with a read key can gate a policy change
                if not self._require("read"):
                    return
                body = json.loads(self._body() or b"{}")
                q = {k: str(v) for k, v in body.items() if k in ("workflow", "project", "environment", "days", "policy") and v}
                q["candidate_doc"] = body.get("candidate")
                api = self._scoped_api()
                try:
                    res = api.check_policy(q)
                finally:
                    api.close()
                return self._send(400 if res.get("error") else 200, res)
            # ---- outcome grades: state what happened instead of leaving it to inference.
            # Needs `ingest`, like feedback: whoever writes telemetry may say how a task ended.
            if p == "/api/outcomes" or (p.startswith("/api/tasks/") and p.endswith("/outcome")):
                if not self._require("ingest"):
                    return
                body = json.loads(self._body() or b"{}")
                if p == "/api/outcomes":
                    items = body if isinstance(body, list) else body.get("grades", [])
                else:
                    items = [dict(body, task_id=unquote(p[len("/api/tasks/"):-len("/outcome")]))]
                # by your own key: {"key": {"ticket_id": "T-1"}, "outcome", "reason", "match": "last" | "all"}
                by_key, items = [it for it in items if "key" in it], [it for it in items if "key" not in it]
                for it in by_key:
                    k = it.get("key")
                    if (not isinstance(k, dict) or len(k) != 1 or not isinstance(next(iter(k)), str)
                            or not isinstance(next(iter(k.values())), (str, int, float)) or isinstance(next(iter(k.values())), bool)
                            or it.get("match", "last") not in ("last", "all")
                            or (it.get("outcome") is not None and it.get("outcome") not in OUTCOMES)):
                        return self._send(400, {"error": "an outcome by key is {\"key\": {name: value}, \"outcome\": "
                                                         f"one of {', '.join(OUTCOMES)} or null, \"match\": \"last\" or \"all\"}}"})
                if self._scope() is not None and items:
                    # A scoped key grades only tasks that exist in its projects. All or nothing, and the same
                    # answer whether an id is in another project or nowhere, so it can't probe for ids.
                    api = self._scoped_api()
                    try:
                        ids = [str(it.get("task_id")) for it in items]
                        seen = {r[0] for i in range(0, len(ids), 500) for r in api.con.execute(
                            f"SELECT id FROM tasks WHERE id IN ({','.join('?' * len(ids[i:i + 500]))})", ids[i:i + 500])}
                    finally:
                        api.close()
                    outside = sorted(set(ids) - seen)
                    if outside:
                        return self._send(403, {"error": "this key may only grade tasks in its projects",
                                                "task_ids": outside})
                who, graded, cleared = self._key_name(), 0, 0
                for it in by_key:                            # a scoped key's reach stops at its projects
                    (k, v), = it["key"].items()
                    if it.get("outcome") is None:
                        cleared += e.ungrade_by_key(k, str(v), self._scope())
                    else:
                        e.grade_by_key(k, str(v), it["outcome"], it.get("reason"), who, it.get("match", "last"), self._scope())
                        graded += 1
                for it in items:
                    if it.get("outcome") is None:          # null clears: back to feedback, then inference
                        cleared += e.ungrade(it["task_id"])
                    else:
                        e.grade(it["task_id"], it["outcome"], it.get("reason"), who)
                        graded += 1
                e.refresh()                                  # one re-finalize for the whole request
                return self._send(200, {"ok": True, "graded": graded, "cleared": cleared})
            # ---- operations
            if p == "/api/refresh":
                if not self._require("read"):
                    return
                changed = e.refresh(force=True)
                out = {"ok": True, "seconds": e.last_duration}
                if self._scope() is None:          # an install-wide count: other projects' activity
                    out["changed"] = changed
                return self._send(200, out)
            if p == "/api/revocations":
                # revoke an agent's grants wherever it runs; applied by the in-process Aegis integration
                if not self._require("admin"):
                    return
                b = json.loads(self._body() or b"{}")
                if not b.get("reason"):
                    return self._send(400, {"error": "say why: a reason goes into the kernel's audit log"})
                # any sign of a restriction makes it one: an empty "tools" must be refused, never become a revoke
                restricting = b.get("tools") is not None or b.get("budget") not in (None, "")
                kind = b.get("kind") or ("restrict" if restricting else "revoke")
                who = dict(agent=b.get("agent") or None, project=b.get("project") or None,
                           reason=f"{b['reason']} ({self._key_name()})", minutes=float(b.get("minutes") or 60),
                           source="operator")
                if kind == "restrict":           # take tools / budget away
                    try:
                        rid = e.restrict(tools=b.get("tools") or (), budget=b.get("budget"), **who)
                    except ValueError as ex:     # nothing to take, or a share that would give: a bad request
                        return self._send(400, {"error": str(ex)})
                elif kind == "revoke":
                    rid = e.revoke(**who)
                else:
                    return self._send(400, {"error": "kind is revoke or restrict"})
                return self._send(200, {"ok": True, "id": rid, "kind": kind})
            if p.startswith("/api/incidents/") and p.endswith("/verdict"):
                # a verdict is a security judgement (the trust score will read it): admin, like a directive
                if not self._require("admin"):
                    return
                iid = unquote(p[len("/api/incidents/"):-len("/verdict")])   # admin keys are never scoped
                b = json.loads(self._body() or b"{}")
                inc = e.incident_verdict(iid, b.get("verdict"), b.get("note"), self._key_name() or "local")
                return self._send(200 if inc else 404, {"ok": True, "incident": inc} if inc else {"error": "not found"})
            if p.startswith("/api/revocations/") and p.endswith("/clear"):
                if not self._require("admin"):
                    return
                n = e.clear_revocation(unquote(p[len("/api/revocations/"):-len("/clear")]))
                return self._send(200 if n else 404, {"ok": bool(n)})
            if p == "/api/rules":
                if not self._require("admin"):
                    return
                e.save_rules(json.loads(self._body())["rules"])
                return self._send(200, {"ok": True})
            if p == "/api/slos":
                if not self._require("admin"):
                    return
                e.save_slos(json.loads(self._body())["slos"])
                return self._send(200, {"ok": True})
            if p == "/api/apdex":
                if not self._require("admin"):
                    return
                try:
                    saved = e.save_apdex_targets((json.loads(self._body() or b"{}") or {}).get("targets"))
                except (ValueError, AttributeError) as ex:
                    return self._send(400, {"error": str(ex)})
                return self._send(200, {"ok": True, "targets": saved})
        except ScopeError as ex:
            return self._refuse(ex)
        except ClientGone:
            return
        except Exception as ex:
            traceback.print_exc()
            return self._send(400, {"error": str(ex)})
        self._send(404, {"error": "not found"})


def serve(engine, host="127.0.0.1", port=8787):
    Handler.api = Api(engine)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"AgentDynamics console: http://{host}:{port}")
    print(f"  OTLP/HTTP traces : http://{host}:{port}/v1/traces")
    print(f"  LangSmith API    : http://{host}:{port}/langsmith   (set LANGSMITH_ENDPOINT to this)")
    print(f"  auth             : {'on' if engine.cfg['auth']['enabled'] else 'off (local mode)'}")
    httpd.serve_forever()
