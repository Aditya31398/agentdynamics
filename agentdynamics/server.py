"""HTTP API + static web console (stdlib only)."""
import html
import json
import mimetypes
import os
import ssl
import sys
import threading
import traceback
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import auth as authn
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


class BadRequest(ValueError):
    """A request body that can't be read as sent (its length, its encoding, its size): the client's error,
    answered 400 without a traceback in the server's log."""


# On every response. The console loads nothing from elsewhere and runs no inline script, so the policy can be
# strict: a script injected into a page has nowhere to come from, and no other site may frame the console (its
# buttons revoke agents).
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
                               "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def _inflate(body, wbits):
    """Decompress at most MAX_BODY bytes: a few kilobytes of gzip can expand to gigabytes."""
    out = bytearray()
    while body:
        d = zlib.decompressobj(wbits)
        try:
            out += d.decompress(body, MAX_BODY + 1 - len(out))
        except zlib.error as ex:
            raise BadRequest(f"corrupt compressed body: {ex}") from None
        if len(out) > MAX_BODY or d.unconsumed_tail:
            raise BadRequest("payload too large")
        if not d.eof:
            raise BadRequest("truncated compressed body")
        body = d.unused_data if wbits > zlib.MAX_WBITS else b""     # gzip may hold several members
    return bytes(out)


class AuthState:
    """What signing people in needs, made once per server on first use: the session key and the OIDC client."""

    def __init__(self, engine):
        self.e = engine
        self._lock = threading.Lock()
        self._secret = None
        self._oidc = None

    def secret(self):
        with self._lock:
            if self._secret is None:
                self._secret = authn.session_secret(self.e)
            return self._secret

    def oidc(self):
        conf = self.e.cfg["auth"].get("oidc")
        if not conf:
            return None
        secret = self.secret()
        with self._lock:
            if self._oidc is None or self._oidc.conf is not conf:
                self._oidc = authn.Oidc(conf, secret)
            return self._oidc


class Handler(BaseHTTPRequestHandler):
    api = None
    tls = False                      # served over TLS by this process (make_server)
    protocol_version = "HTTP/1.1"
    timeout = 120                    # a connection silent this long is closed: idle threads aren't held forever

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        # Covers the socket I/O http.server does itself: the request line and headers (a keep-alive
        # connection reset while idle) and its own error replies. Ours goes through _send and _body.
        # A TLS handshake that fails (plain HTTP to the TLS port, a client that doesn't trust the certificate)
        # is the client's problem, not a server error.
        self._who = None
        try:
            super().handle_one_request()
        except CLIENT_GONE + (ssl.SSLError,):
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
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        if self.tls and not authn.is_loopback(self.headers.get("Host") if self.headers else ""):
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in (extra_headers.items() if isinstance(extra_headers, dict) else extra_headers or ()):
            self.send_header(k, v)
        try:
            self.end_headers()
            self.wfile.write(data)
        except CLIENT_GONE:
            self.close_connection = True

    # --- auth: API keys with roles ingest < read < admin, and people (auth.py)
    def _authstate(self):
        api = self.api
        st = api.__dict__.get("_authstate")
        if st is None:
            st = api.__dict__.setdefault("_authstate", AuthState(api.e))
        return st

    def _key(self):
        """Who this request is, or None: a key record or a signed-in person ({name, role, projects?, via}).
        Auth off: the local admin. Worked out once per request."""
        if getattr(self, "_who", None) is None:
            self._who = self._authenticate() or False
        return self._who or None

    def _authenticate(self):
        auth = self.api.e.cfg["auth"]
        if not auth.get("enabled"):
            return {"name": "local", "role": "admin", "via": "local"}
        key = self.headers.get("x-api-key") or ""
        h = self.headers.get("Authorization") or ""
        if h.lower().startswith("bearer "):
            key = h[7:].strip()
        if key:                                  # a wrong key is refused, not rescued by a cookie
            k = authn.find_key(key, auth.get("keys") or [])
            if not k:
                return None
            if k.get("role") == "admin" and "projects" in k:
                # admin edits install-wide rules and SLOs; a "scoped admin" can't mean anything
                # safe, so the key is refused rather than guessed at
                return {"name": k.get("name"), "role": None,
                        "invalid": "admin keys cannot be scoped to projects; use a read or ingest key"}
            return dict(k, via="key")
        person, via = None, None
        if auth.get("oidc"):
            c = self._cookie(authn.SESSION_COOKIE)
            person = authn.read_session(self._authstate().secret(), c) if c else None
            via = "sso"
        if person is None and auth.get("proxy"):
            person, via = authn.from_proxy(auth["proxy"], self.client_address[0], self.headers), "proxy"
        if person is None:
            return None
        return dict(authn.access_for(person, auth.get("access")), via=via)

    def _cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
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
        body = {"error": "unauthorized" if r == 0 else f"requires {want} role"}
        if r == 0 and self.api.e.cfg["auth"].get("oidc"):
            body["sso"] = "/auth/login"          # the console offers to sign in with the identity provider
        self._send(401 if r == 0 else 403, body, extra_headers={"WWW-Authenticate": "Bearer"} if r == 0 else None)
        return False

    # --- requests from elsewhere
    def _own_hosts(self):
        """The names this server answers to: the Host header, a proxy's X-Forwarded-Host, and configured ones."""
        cfg = self.api.e.cfg
        hosts = [self.headers.get("Host"), self.headers.get("X-Forwarded-Host")]
        hosts += [str(h) for h in cfg["server"].get("allowed_hosts") or ()]
        for url in (cfg["alerts"].get("console_url"), (cfg["auth"].get("oidc") or {}).get("redirect_url")):
            if url:
                hosts.append(urlparse(url).netloc)
        return hosts

    def _host_refused(self):
        """Answers 403 if the Host header names a server this isn't. With auth off nothing else stands between
        a web page and the API, and DNS rebinding points the page's own domain at 127.0.0.1: so on a server bound
        to one address the Host must be a loopback name, that address, or one in [server] allowed_hosts."""
        cfg = self.api.e.cfg
        allowed = [str(h).lower() for h in cfg["server"].get("allowed_hosts") or ()]
        bound = str(self.server.server_address[0])
        if not allowed and (cfg["auth"].get("enabled") or bound in ("0.0.0.0", "::", "")):
            return False                 # keys guard the API; a wildcard bind could be reached by any name
        name = authn.host_name(self.headers.get("Host"))
        if name in allowed or "*" in allowed or name == bound.lower() or authn.is_loopback(name):
            return False
        self._send(403, {"error": f"this server does not answer to {name!r}: add it to [server] allowed_hosts"})
        return True

    def _cross_site_refused(self):
        """Answers 403 for a state-changing request a browser sent from another site (CSRF): with auth off, or a
        session cookie the browser adds by itself, it would act with the person's authority."""
        if not authn.cross_site(self.headers, self._own_hosts()):
            return False
        self._send(403, {"error": "refused: a request from another site"})
        return True

    # --- signing people in (OIDC)
    def _base_url(self):
        url = self.api.e.cfg["alerts"].get("console_url")
        if url:
            return url.rstrip("/")
        return f"{'https' if self.tls else 'http'}://{self.headers.get('Host') or 'localhost'}"

    def _secure_cookies(self):
        return self.tls or self._base_url().startswith("https://") or str(
            (self.api.e.cfg["auth"].get("oidc") or {}).get("redirect_url") or "").startswith("https://")

    def _auth_page(self, code, message):
        page = (f"<!doctype html><meta charset=utf-8><title>AgentDynamics</title><link rel=stylesheet href=/style.css>"
                f"<div class=card style='max-width:460px;margin:60px auto'><h2>Sign in</h2>"
                f"<p>{html.escape(message)}</p><p><a href=/auth/login>Try again</a></p></div>")
        self._send(code, page, "text/html; charset=utf-8",
                   extra_headers=[("Set-Cookie", authn.cookie(authn.STATE_COOKIE, "", "/auth", 0, self._secure_cookies(), "Lax"))])

    def _auth_get(self, p, q):
        try:
            oidc = self._authstate().oidc()
        except authn.AuthError as ex:
            return self._auth_page(500, str(ex))
        if not oidc:
            return self._send(404, {"error": "single sign-on is not configured ([auth.oidc])"})
        try:
            if p == "/auth/login":
                where, state = oidc.login(self._base_url(), q.get("next") or "/")
                return self._send(302, b"", "text/plain", extra_headers=[
                    ("Location", where),
                    ("Set-Cookie", authn.cookie(authn.STATE_COOKIE, state, "/auth", 600, self._secure_cookies(), "Lax"))])
            person, nxt = oidc.callback(self._base_url(), q, self._cookie(authn.STATE_COOKIE))
        except authn.AuthError as ex:
            return self._auth_page(400, str(ex))
        rules = self.api.e.cfg["auth"].get("access")
        who = authn.access_for(person, rules)
        if not who.get("role"):
            return self._auth_page(403, who.get("invalid") or "no access")
        secure = self._secure_cookies()
        return self._send(302, b"", "text/plain", extra_headers=[
            ("Location", nxt),
            ("Set-Cookie", authn.cookie(authn.SESSION_COOKIE, oidc.session(person, rules), "/",
                                        oidc.session_hours * 3600, secure)),
            ("Set-Cookie", authn.cookie(authn.STATE_COOKIE, "", "/auth", 0, secure, "Lax"))])

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

    def _content_length(self):
        """The declared body length; -1 if it isn't a number of bytes (the body can't be found then)."""
        v = (self.headers.get("Content-Length") or "").strip()
        if not v:
            return 0
        return int(v) if v.isdigit() else -1

    def _chunked(self):
        return (self.headers.get("Transfer-Encoding") or "").strip().lower() not in ("", "identity")

    def _read_chunked(self):
        """A Transfer-Encoding: chunked body, at most MAX_BODY bytes."""
        out, total = [], 0
        while True:
            size = self.rfile.readline(1024).split(b";")[0].strip()
            if not size or any(c not in b"0123456789abcdefABCDEF" for c in size):
                raise BadRequest("malformed chunked body")
            n = int(size, 16)
            if n == 0:
                while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):    # trailers
                    pass
                return b"".join(out)
            total += n
            if total > MAX_BODY:
                raise BadRequest("payload too large")
            out.append(self.rfile.read(n))
            self.rfile.readline(4)                                            # the chunk's CRLF

    def _body(self):
        try:
            if self._chunked():
                if (self.headers.get("Transfer-Encoding") or "").strip().lower() != "chunked":
                    self.close_connection = True
                    raise BadRequest("unsupported Transfer-Encoding")
                self._body_read = True                   # whatever happens next, the connection can't be reused
                self.close_connection = True
                body = self._read_chunked()
                self.close_connection = False
            else:
                n = self._content_length()
                if n < 0:
                    raise BadRequest("bad Content-Length")
                if n > MAX_BODY:
                    raise BadRequest("payload too large")
                body = self.rfile.read(n) if n else b""
                self._body_read = True
        except CLIENT_GONE as ex:
            self.close_connection = True
            raise ClientGone() from ex
        enc = (self.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            body = _inflate(body, 16 + zlib.MAX_WBITS)
        elif enc == "deflate":
            body = _inflate(body, zlib.MAX_WBITS)
        elif enc == "zstd":
            from .collectors.langsmith import zstd_available, zstd_decompress
            if not zstd_available():
                raise BadRequest("Content-Encoding zstd needs `pip install zstandard`")
            body = zstd_decompress(body, MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise BadRequest("payload too large")
        elif enc and enc != "identity":
            raise BadRequest(f"unsupported Content-Encoding {enc}")
        return body

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items() if v and v[0] != ""}
        p = u.path
        api = self.api
        if self._host_refused():
            return
        if p in ("/auth/login", "/auth/callback"):
            return self._auth_get(p, q)
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
                      "/api/process": api.process, "/api/calibration": api.calibration, "/api/billing": api.billing, "/api/analytics": api.analytics, "/api/compare": api.compare,
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
                k = self._key() or {}
                return self._send(200, {"role": {3: "admin", 2: "read", 1: "ingest"}.get(self._role()),
                                        "projects": self._scope(), "name": k.get("name"), "via": k.get("via")})
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
        try:
            inside = os.path.commonpath([path, WEB_DIR]) == WEB_DIR
        except ValueError:                 # another drive on Windows
            inside = False
        if not inside or not os.path.isfile(path):
            path = os.path.join(WEB_DIR, "index.html")
        with open(path, "rb") as f:
            self._send(200, f.read(), mimetypes.guess_type(path)[0] or "application/octet-stream")

    def _unread_body_closes(self, handler):
        """Run a POST/PATCH handler. A body it didn't read (a route that takes none, or a request refused before
        reading it) would stay in the socket and become the start of the next request on a keep-alive
        connection -- "{}GET /api/..." answered 501 -- so that connection is closed after the response."""
        self._body_read = False
        try:
            if self._host_refused() or self._cross_site_refused():
                return None
            return handler()
        finally:
            n = self._content_length()
            if not self._body_read and (n < 0 or self._chunked()):
                self.close_connection = True     # where this body ends is unknown: the connection can't continue
            elif not self._body_read and n > 0:
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
        if p == "/auth/logout":
            return self._send(200, {"ok": True}, extra_headers=[
                ("Set-Cookie", authn.cookie(authn.SESSION_COOKIE, "", "/", 0, self._secure_cookies()))])
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
        except BadRequest as ex:
            return self._send(400, {"error": str(ex)})
        except Exception as ex:
            traceback.print_exc()
            return self._send(400, {"error": str(ex)})
        self._send(404, {"error": "not found"})


def make_server(engine, host="127.0.0.1", port=8787, tls_cert=None, tls_key=None):
    """The console and receivers on (host, port); over TLS when given a certificate (PEM; the key may be in the
    same file). TLS 1.2 or later. The handshake happens on the connection's own thread, so a client that stalls in
    it holds up no one else."""
    handler = type("Handler", (Handler,), {"api": Api(engine), "tls": bool(tls_cert)})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    if tls_cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(tls_cert, tls_key or None)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    return httpd


def warnings_for(cfg, host, tls):
    """What is unsafe about serving this way, in words for the operator."""
    out, auth = [], cfg["auth"]
    exposed = not authn.is_loopback(host)
    if exposed and not auth.get("enabled"):
        out.append(f"auth is off and the server listens on {host}: anyone who can reach it can read every prompt and "
                   "revoke agents. Create a key (agentdynamics keys create) or sign people in ([auth.oidc]).")
    if exposed and auth.get("enabled") and not tls:
        out.append("keys and session cookies cross the network in clear: serve with --tls-cert/--tls-key, or keep a "
                   "TLS proxy in front and the port closed to everything else.")
    for k in auth.get("keys") or []:
        if k.get("key") and len(str(k["key"])) < 20:
            out.append(f"the key named {k.get('name')!r} is short enough to guess: make one with agentdynamics keys create")
    for n in (auth.get("proxy") or {}).get("trusted") or ():
        if str(n).endswith("/0"):
            out.append(f"[auth.proxy] trusts {n}: anyone can then send the user header and sign in as anyone")
    if auth.get("oidc") and auth.get("access") and any(str(r.get("match")).strip() == "*" for r in auth["access"]):
        out.append("[[auth.access]] match = \"*\" lets in anyone your identity provider signs in")
    if (auth.get("oidc") or auth.get("proxy")) and not auth.get("access"):
        out.append("people can sign in but no [[auth.access]] rule gives anyone a role")
    return out


def serve(engine, host="127.0.0.1", port=8787, tls_cert=None, tls_key=None):
    httpd = make_server(engine, host, port, tls_cert, tls_key)
    scheme = "https" if tls_cert else "http"
    a = engine.cfg["auth"]
    how = [w for w, on in (("keys", a.get("keys")), ("single sign-on", a.get("oidc")), ("proxy sign-in", a.get("proxy"))) if on]
    print(f"AgentDynamics console: {scheme}://{host}:{port}")
    print(f"  OTLP/HTTP traces : {scheme}://{host}:{port}/v1/traces")
    print(f"  LangSmith API    : {scheme}://{host}:{port}/langsmith   (set LANGSMITH_ENDPOINT to this)")
    print(f"  auth             : {'on (' + ', '.join(how or ['keys']) + ')' if a['enabled'] else 'off (local mode)'}")
    for w in warnings_for(engine.cfg, host, bool(tls_cert)):
        print(f"  WARNING: {w}", file=sys.stderr)
    httpd.serve_forever()
