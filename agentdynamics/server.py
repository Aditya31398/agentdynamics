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

from .api.assess import AssessMixin
from .api.base import DAY, ApiBase, mcp_group  # noqa: F401  (re-exported)
from .api.diagnose import DiagnoseMixin
from .api.governance import GovernanceMixin
from .api.monitor import MonitorMixin
from .api.ops import OpsMixin

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


class Handler(BaseHTTPRequestHandler):
    api = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

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
        self.end_headers()
        self.wfile.write(data)

    # --- auth: API keys with roles ingest < read < admin
    def _role(self):
        auth = self.api.e.cfg["auth"]
        if not auth.get("enabled"):
            return 3
        key = self.headers.get("x-api-key") or ""
        h = self.headers.get("Authorization") or ""
        if h.lower().startswith("bearer "):
            key = h[7:].strip()
        for k in auth.get("keys") or []:
            if key and hmac.compare_digest(key, str(k.get("key", ""))):
                return ROLE_FOR.get(k.get("role"), 0)
        return 0

    def _key_name(self):
        """Who is calling, for the audit trail on a grade: the matched key's name, or 'local'."""
        auth = self.api.e.cfg["auth"]
        if not auth.get("enabled"):
            return "local"
        key = self.headers.get("x-api-key") or ""
        h = self.headers.get("Authorization") or ""
        if h.lower().startswith("bearer "):
            key = h[7:].strip()
        for k in auth.get("keys") or []:
            if key and hmac.compare_digest(key, str(k.get("key", ""))):
                return k.get("name") or k.get("role")
        return None

    def _require(self, need):
        r = self._role()
        if need in CAN.get(r, set()):
            return True
        self._send(401 if r == 0 else 403, {"error": "unauthorized" if r == 0 else f"requires '{need}' role"},
                   extra_headers={"WWW-Authenticate": "Bearer"} if r == 0 else None)
        return False

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("payload too large")
        body = self.rfile.read(n) if n else b""
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
                if not self._require("read"):
                    return
            if p == "/metrics":
                return self._send(200, api.prometheus(), "text/plain; version=0.0.4")
            routes = {"/api/filters": api.filters, "/api/overview": api.overview, "/api/types": api.types,
                      "/api/tasks": api.task_list, "/api/sessions": api.sessions, "/api/flowmap": api.flowmap,
                      "/api/tools": api.tools, "/api/models": api.models, "/api/events": api.events,
                      "/api/process": api.process, "/api/analytics": api.analytics, "/api/compare": api.compare,
                      "/api/workflows": api.workflows, "/api/workflow": api.workflow, "/api/slos": api.slos,
                      "/api/sources": api.sources, "/api/config": api.config, "/api/connect": api.connect,
                      "/api/governance": api.governance, "/api/governance/policy": api.export_policy}
            if p in routes:
                r = routes[p](q)
                return self._send(200 if r is not None else 404, r if r is not None else {"error": "not found"})
            if p.startswith("/api/task/"):
                r = api.task(unquote(p[len("/api/task/"):]))
                return self._send(200 if r else 404, r or {"error": "not found"})
            if p == "/api/rules":
                return self._send(200, {"rules": api.e.rules()})
            if p == "/api/whoami":
                return self._send(200, {"role": {3: "admin", 2: "read", 1: "ingest"}.get(self._role())})
            if p.startswith("/api/") or p.startswith("/langsmith/"):
                return self._send(404 if p.startswith("/api/") else 200, {"error": "not found"} if p.startswith("/api/") else {})
        except Exception as ex:  # surface errors to the console instead of a dropped connection
            traceback.print_exc()
            return self._send(500, {"error": str(ex)})
        # static console
        rel = "index.html" if p in ("/", "") else p.lstrip("/")
        path = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not path.startswith(WEB_DIR) or not os.path.isfile(path):
            path = os.path.join(WEB_DIR, "index.html")
        with open(path, "rb") as f:
            self._send(200, f.read(), mimetypes.guess_type(path)[0] or "application/octet-stream")

    def do_PATCH(self):
        u = urlparse(self.path)
        try:
            if u.path.startswith("/langsmith/") and "/runs/" in u.path:
                if not self._require("ingest"):
                    return
                rid = u.path.rstrip("/").rsplit("/", 1)[-1]
                patch = json.loads(self._body() or b"{}")
                patch["id"] = rid
                self.api.e.ingest_langsmith([], [patch])
                return self._send(200, {})
        except Exception as ex:
            return self._send(400, {"error": str(ex)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        e = self.api.e
        try:
            # ---- telemetry ingestion (role: ingest)
            if p in ("/v1/traces", "/otlp/v1/traces"):
                if not self._require("ingest"):
                    return
                ctype = self.headers.get("Content-Type") or ""
                n = e.ingest_otlp(self._body(), ctype)
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
                    e.ingest_langsmith(posts, patches)
                elif sub == "/runs/multipart":
                    posts, patches, fb = ls.parse_multipart(body, self.headers.get("Content-Type") or "")
                    e.ingest_langsmith(posts, patches, fb)
                elif sub == "/runs":
                    e.ingest_langsmith([json.loads(body or b"{}")], [])
                elif sub == "/feedback":
                    e.ingest_langsmith([], [], [json.loads(body or b"{}")])
                else:
                    return self._send(200, {})  # accept and ignore other LangSmith calls (datasets, sessions...)
                return self._send(202, {})
            if p == "/api/ingest":
                if not self._require("ingest"):
                    return
                payload = json.loads(self._body() or b"{}")
                runs = payload if isinstance(payload, list) else [payload]
                return self._send(200, {"ok": True, "ids": [e.ingest(r) for r in runs]})
            if p == "/api/ingest/records":
                # log pipelines (Fluent Bit / Vector / Logstash HTTP outputs): JSON array or NDJSON of any supported format
                if not self._require("ingest"):
                    return
                from .collectors.inbox import detect, unwrap
                body = self._body().strip()
                recs = json.loads(body) if body.startswith(b"[") else [json.loads(x) for x in body.splitlines() if x.strip()]
                recs = [unwrap(r) for r in recs]
                n = e.ingest_records([(detect(r), r) for r in recs if detect(r)])
                return self._send(200, {"ok": True, "accepted": n, "received": len(recs)})
            if p == "/api/policy/check":
                # reads recorded traffic, writes nothing: a CI job with a read key can gate a policy change
                if not self._require("read"):
                    return
                body = json.loads(self._body() or b"{}")
                q = {k: str(v) for k, v in body.items() if k in ("workflow", "project", "environment", "days", "policy") and v}
                q["candidate_doc"] = body.get("candidate")
                res = self.api.check_policy(q)
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
                who, graded, cleared = self._key_name(), 0, 0
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
                return self._send(200, {"ok": True, "changed": changed, "seconds": e.last_duration})
            if p == "/api/rules":
                if not self._require("admin"):
                    return
                e.save_rules(json.loads(self._body())["rules"])
                return self._send(200, {"ok": True})
            if p == "/api/slos":
                if not self._require("admin"):
                    return
                from . import slo
                slo.save(e.data_dir, json.loads(self._body())["slos"])
                return self._send(200, {"ok": True})
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
