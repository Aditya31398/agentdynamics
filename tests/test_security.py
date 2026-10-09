"""The HTTP layer under attack, and people signing in.

  * single sign-on (OIDC, auth.py) against a real local identity provider: the code flow with PKCE, the ID token's
    checks, sessions that can't be forged, and roles from [[auth.access]];
  * a reverse proxy that signs people in, trusted only from its own addresses;
  * requests from other sites (CSRF) and DNS rebinding, which with auth off are all that stands between a web page
    and the API;
  * bodies that lie: a negative length, a gzip bomb, chunked uploads;
  * TLS, served by the process itself; keys kept only as hashes.

No real identity provider: the one here is a local server that checks what a real one checks (the client's
secret, the redirect, the PKCE verifier) and signs nothing, since the ID token is taken straight from it.
"""
import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import warnings
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentdynamics import auth, config  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import MAX_BODY, Api, Handler, make_server, warnings_for  # noqa: E402

NOW = time.time() - 600


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class FakeIdP(BaseHTTPRequestHandler):
    """Discovery, authorize (answers straight away, as if the person signed in), token and userinfo."""
    issued = {}                     # code -> what the authorize request carried
    claims = {}                     # overrides for the next ID token
    userinfo = {}
    token_calls = []

    def log_message(self, *a):
        pass

    def send_json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path == "/.well-known/openid-configuration":
            return self.send_json(200, {"issuer": self.base, "authorization_endpoint": self.base + "/authorize",
                                        "token_endpoint": self.base + "/token", "userinfo_endpoint": self.base + "/userinfo"})
        if u.path == "/authorize":
            code = f"code-{len(self.issued)}"
            type(self).issued[code] = q
            self.send_response(302)
            self.send_header("Location", f"{q['redirect_uri']}?code={code}&state={urllib.parse.quote(q['state'])}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if u.path == "/userinfo":
            return self.send_json(200, dict({"sub": "u-alice"}, **self.userinfo))
        self.send_json(404, {})

    def do_POST(self):
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
        type(self).token_calls.append(form)
        if self.headers.get("Authorization") != "Basic " + base64.b64encode(b"agentdynamics:s3cret").decode():
            return self.send_json(401, {"error": "invalid_client"})
        req = type(self).issued.pop(form.get("code"), None)
        if not req or form.get("redirect_uri") != req["redirect_uri"]:
            return self.send_json(400, {"error": "invalid_grant"})
        if b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest()) != req["code_challenge"]:
            return self.send_json(400, {"error": "invalid_grant", "error_description": "PKCE verifier"})
        claims = {"iss": self.base, "aud": "agentdynamics", "sub": "u-alice", "email": "alice@example.com",
                  "email_verified": True, "nonce": req["nonce"], "exp": time.time() + 300, "iat": time.time()}
        claims.update(self.claims)
        claims = {k: v for k, v in claims.items() if v is not None}
        header = {"alg": claims.pop("_alg", "RS256"), "typ": "JWT"}
        tok = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}.{b64(b'not-checked')}"
        self.send_json(200, {"id_token": tok, "access_token": "at-1", "token_type": "Bearer"})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPEN = urllib.request.build_opener(NoRedirect)


def http(url, method="GET", headers=None, body=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        r = OPEN.open(req, timeout=20)
    except urllib.error.HTTPError as e:
        r = e
    with r:
        return r.status, r.headers, r.read().decode("utf-8", "replace")


def set_cookies(headers):
    return {c.split("=", 1)[0]: c for c in headers.get_all("Set-Cookie") or ()}


def cookie_value(set_cookie):
    return set_cookie.split(";", 1)[0].split("=", 1)[1]


def start(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.eng = Engine(os.path.join(self.tmp, "data"), None)
        self.addCleanup(self.eng.con.close)
        self.eng.ingest({"id": "r1", "project": "alpha", "workflow": "w", "steps": [
            {"kind": "prompt", "ts": NOW, "text": "hello"},
            {"kind": "llm", "ts": NOW, "end_ts": NOW + 1, "model": "claude-sonnet-5", "input_tokens": 10, "output_tokens": 5}]})
        self.eng.refresh(force=True)

    def serve(self, **auth_conf):
        if auth_conf:
            self.eng.cfg["auth"] = dict({"enabled": True, "keys": []}, **auth_conf)
        srv, url = start(type("H", (Handler,), {"api": Api(self.eng)}))
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return url


class SingleSignOnTest(Base):
    def setUp(self):
        super().setUp()
        FakeIdP.issued, FakeIdP.claims, FakeIdP.userinfo, FakeIdP.token_calls = {}, {}, {}, []
        idp, self.idp = start(FakeIdP)
        self.addCleanup(idp.server_close)
        self.addCleanup(idp.shutdown)
        os.environ["AD_TEST_OIDC_SECRET"] = "s3cret"
        self.addCleanup(os.environ.pop, "AD_TEST_OIDC_SECRET", None)
        self.url = self.serve(
            oidc={"issuer": self.idp, "client_id": "agentdynamics", "client_secret_env": "AD_TEST_OIDC_SECRET"},
            access=[{"match": "admin@example.com", "role": "admin"},
                    {"match": "group:ops", "role": "admin"},
                    {"match": "*@team.example.com", "role": "read", "projects": ["alpha"]},
                    {"match": "*@example.com", "role": "read"}])

    def sign_in(self, next_path="/#/tasks"):
        """The browser's side of the flow: login, the provider, the callback. Returns the callback's answer."""
        st, h, _ = http(f"{self.url}/auth/login?next={urllib.parse.quote(next_path)}")
        self.assertEqual(st, 302)
        state_cookie = set_cookies(h)[auth.STATE_COOKIE]
        st, h2, _ = http(h["Location"])                            # the provider sends the browser back
        self.assertEqual(st, 302)
        return http(h2["Location"], headers={"Cookie": f"{auth.STATE_COOKIE}={cookie_value(state_cookie)}"})

    def session(self, **claims):
        FakeIdP.claims = claims
        st, h, body = self.sign_in()
        self.assertEqual(st, 302, body)
        return cookie_value(set_cookies(h)[auth.SESSION_COOKIE])

    def who(self, session=None, **headers):
        if session:
            headers["Cookie"] = f"{auth.SESSION_COOKIE}={session}"
        st, _, body = http(f"{self.url}/api/whoami", headers=headers)
        return st, json.loads(body)

    def test_a_person_signs_in_with_the_identity_provider(self):
        st, body = self.who()
        self.assertEqual(st, 401)
        self.assertEqual(body["sso"], "/auth/login", "the console is told it can offer single sign-on")
        st, h, _ = http(f"{self.url}/auth/login")
        where = urllib.parse.urlparse(h["Location"])
        q = {k: v[0] for k, v in urllib.parse.parse_qs(where.query).items()}
        self.assertEqual((q["response_type"], q["code_challenge_method"], q["client_id"]), ("code", "S256", "agentdynamics"))
        self.assertTrue(q["state"] and q["nonce"] and q["code_challenge"])
        c = set_cookies(h)[auth.STATE_COOKIE]
        self.assertIn("HttpOnly", c)
        self.assertIn("SameSite=Lax", c)
        st, h, _ = self.sign_in("/#/tasks")
        self.assertEqual(h["Location"], "/#/tasks", "back where they started")
        c = set_cookies(h)[auth.SESSION_COOKIE]
        self.assertIn("HttpOnly", c)
        self.assertIn("SameSite=Strict", c)
        self.assertEqual(FakeIdP.token_calls[-1]["grant_type"], "authorization_code")
        st, body = self.who(cookie_value(c))
        self.assertEqual((st, body["name"], body["role"], body["via"]), (200, "alice@example.com", "read", "sso"))
        st, _, _ = http(f"{self.url}/api/overview", headers={"Cookie": f"{auth.SESSION_COOKIE}={cookie_value(c)}"})
        self.assertEqual(st, 200)

    def test_roles_come_from_the_first_matching_rule(self):
        self.assertEqual(self.who(self.session(email="admin@example.com"))[1]["role"], "admin")
        self.assertEqual(self.who(self.session(email="carol@example.com", groups=["ops", "x"]))[1]["role"], "admin")
        scoped = self.who(self.session(email="dan@team.example.com"))[1]
        self.assertEqual((scoped["role"], scoped["projects"]), ("read", ["alpha"]))
        st, _, _ = http(f"{self.url}/api/config", headers={"Cookie": f"{auth.SESSION_COOKIE}={self.session(email='dan@team.example.com')}"})
        self.assertEqual(st, 403, "a person scoped to projects is held to them like a scoped key")
        # groups the token doesn't carry come from the userinfo endpoint
        FakeIdP.userinfo = {"groups": ["ops"]}
        self.assertEqual(self.who(self.session(email="erin@elsewhere.org"))[1]["role"], "admin")

    def test_someone_no_rule_names_is_not_let_in(self):
        FakeIdP.claims = {"email": "mallory@elsewhere.org"}
        st, h, body = self.sign_in()
        self.assertEqual(st, 403)
        self.assertIn("no access", body)
        self.assertNotIn(auth.SESSION_COOKIE, set_cookies(h))
        # a lookalike domain is another domain
        FakeIdP.claims = {"email": "eve@example.com.evil.org"}
        self.assertEqual(self.sign_in()[0], 403)
        FakeIdP.claims = {"email": "eve@notexample.com"}
        self.assertEqual(self.sign_in()[0], 403)
        # an address the provider hasn't verified is no address
        FakeIdP.claims = {"email": "admin@example.com", "email_verified": False}
        self.assertEqual(self.sign_in()[0], 403)

    def test_the_id_token_must_be_for_this_sign_in(self):
        for claims, why in (({"aud": "another-app"}, "another application"),
                            ({"iss": "https://evil.example"}, "another issuer"),
                            ({"exp": time.time() - 3600}, "expired"),
                            ({"nonce": "replayed"}, "not issued for this sign-in"),
                            ({"_alg": "none"}, "unsigned")):
            FakeIdP.claims = claims
            st, h, body = self.sign_in()
            self.assertEqual(st, 400, claims)
            self.assertIn(why, body)
            self.assertNotIn(auth.SESSION_COOKIE, set_cookies(h))

    def test_a_response_from_another_sign_in_is_refused(self):
        # login CSRF: the attacker's own code and state, delivered to the victim's browser
        st, h, _ = http(f"{self.url}/auth/login")
        attacker_redirect = http(h["Location"])[1]["Location"]
        st, h, _ = http(f"{self.url}/auth/login")                  # the victim's own sign-in
        victim_cookie = cookie_value(set_cookies(h)[auth.STATE_COOKIE])
        st, h, body = http(attacker_redirect, headers={"Cookie": f"{auth.STATE_COOKIE}={victim_cookie}"})
        self.assertEqual(st, 400)
        self.assertIn("does not belong", body)
        st, _, body = http(attacker_redirect)                      # no state cookie at all
        self.assertEqual(st, 400)

    def test_sessions_cannot_be_forged(self):
        good = self.session()
        body, mac = good.split(".")
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        forged = b64(json.dumps(dict(claims, email="admin@example.com")).encode()) + "." + mac
        self.assertEqual(self.who(forged)[0], 401, "a changed session is refused")
        other = auth.sign(b"another-secret", "session", dict(claims, email="admin@example.com"))
        self.assertEqual(self.who(other)[0], 401, "one signed with another key is refused")
        secret = auth.session_secret(self.eng)
        self.assertEqual(self.who(auth.sign(secret, "session", dict(claims, exp=time.time() - 1)))[0], 401, "expired")
        state = auth.sign(secret, "oidc-state", dict(claims, exp=time.time() + 60))
        self.assertEqual(self.who(state)[0], 401, "a state cookie is not a session")
        self.assertEqual(self.who(good)[0], 200)

    def test_a_wrong_key_is_not_rescued_by_a_session(self):
        st, _ = self.who(self.session(), Authorization="Bearer wrong")
        self.assertEqual(st, 401)

    def test_it_only_ever_sends_people_back_to_this_server(self):
        for nxt in ("https://evil.example/", "//evil.example/x", "/\\evil.example"):
            st, h, _ = self.sign_in(nxt)
            self.assertEqual((st, h["Location"]), (302, "/"), nxt)

    def test_signing_out_ends_the_session_cookie(self):
        st, h, _ = http(f"{self.url}/auth/logout", "POST", {"Cookie": f"{auth.SESSION_COOKIE}={self.session()}"}, b"{}")
        self.assertEqual(st, 200)
        self.assertIn("Max-Age=0", set_cookies(h)[auth.SESSION_COOKIE])

    def test_a_cross_site_request_cannot_use_the_session(self):
        s = self.session(email="admin@example.com")
        rules = json.dumps({"rules": []}).encode()
        for h in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.example"}, {"Origin": "null"}):
            st, _, body = http(f"{self.url}/api/rules", "POST", dict(h, Cookie=f"{auth.SESSION_COOKIE}={s}"), rules)
            self.assertEqual(st, 403, h)
            self.assertIn("another site", body)
        self.assertTrue(self.eng.rules(), "the rules were not replaced")
        host = urllib.parse.urlparse(self.url).netloc
        st, _, _ = http(f"{self.url}/api/refresh", "POST", {"Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin",
                                                            "Cookie": f"{auth.SESSION_COOKIE}={s}"}, b"{}")
        self.assertEqual(st, 200, "the console's own requests go through")


class ProxySignInTest(Base):
    def test_a_trusted_proxy_names_the_person(self):
        url = self.serve(proxy={"user_header": "X-Forwarded-Email", "groups_header": "X-Forwarded-Groups",
                                "trusted": ["127.0.0.1"]},
                         access=[{"match": "group:ops", "role": "admin"}, {"match": "*@example.com", "role": "read"}])
        st, _, body = http(f"{url}/api/whoami", headers={"X-Forwarded-Email": "alice@example.com"})
        self.assertEqual((st, json.loads(body)["role"], json.loads(body)["via"]), (200, "read", "proxy"))
        st, _, body = http(f"{url}/api/whoami", headers={"X-Forwarded-Email": "bob@corp", "X-Forwarded-Groups": "dev, ops"})
        self.assertEqual(json.loads(body)["role"], "admin")
        self.assertEqual(http(f"{url}/api/whoami")[0], 401)

    def test_from_anywhere_else_the_header_means_nothing(self):
        url = self.serve(proxy={"user_header": "X-Forwarded-Email", "trusted": ["10.0.0.0/8"]},
                         access=[{"match": "*", "role": "admin"}])
        st, _, _ = http(f"{url}/api/whoami", headers={"X-Forwarded-Email": "alice@example.com"})
        self.assertEqual(st, 401)


class LocalModeTest(Base):
    """Auth off: the API answers anyone who reaches it, so the browser is the attack surface."""

    def test_another_site_cannot_change_anything(self):
        url = self.serve()
        before = self.eng.rules()
        for h in ({"Sec-Fetch-Site": "cross-site", "Content-Type": "text/plain"},
                  {"Origin": "https://evil.example", "Content-Type": "text/plain"}):
            st, _, _ = http(f"{url}/api/rules", "POST", h, json.dumps({"rules": []}).encode())
            self.assertEqual(st, 403, h)
            st, _, _ = http(f"{url}/api/revocations", "POST", h, json.dumps({"agent": "a", "reason": "x"}).encode())
            self.assertEqual(st, 403, h)
        self.assertEqual(self.eng.rules(), before)
        self.assertEqual(self.eng.con.execute("SELECT COUNT(*) FROM revocations").fetchone()[0], 0)
        # clients that aren't browsers send no Origin: SDKs, exporters, curl
        st, _, _ = http(f"{url}/api/ingest", "POST", {"Content-Type": "application/json"},
                        json.dumps({"id": "r2", "project": "alpha", "steps": []}).encode())
        self.assertEqual(st, 200)

    def test_dns_rebinding_finds_nothing(self):
        url = self.serve()
        port = urllib.parse.urlparse(url).port
        self.assertEqual(http(f"{url}/api/overview", headers={"Host": f"rebind.evil.example:{port}"})[0], 403)
        self.assertEqual(http(f"{url}/api/overview", headers={"Host": f"localhost:{port}"})[0], 200)
        self.assertEqual(http(f"{url}/api/overview")[0], 200)
        self.eng.cfg["server"]["allowed_hosts"] = ["agentdynamics.internal"]
        self.assertEqual(http(f"{url}/api/overview", headers={"Host": "agentdynamics.internal"})[0], 200)

    def test_every_response_carries_the_security_headers(self):
        url = self.serve()
        for path in ("/", "/api/overview"):
            _, h, _ = http(url + path)
            self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
            self.assertIn("script-src 'self'", h["Content-Security-Policy"])
            self.assertEqual(h["X-Frame-Options"], "DENY")
            self.assertIsNone(h["Strict-Transport-Security"], "not over plain HTTP")


class BodiesTest(Base):
    def raw(self, url, data, timeout=10):
        u = urllib.parse.urlparse(url)
        with socket.create_connection((u.hostname, u.port), timeout=timeout) as s:
            s.sendall(data)
            out = b""
            while b"\r\n\r\n" not in out:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
            return out

    def test_a_negative_length_is_refused_without_waiting(self):
        url = self.serve()
        t = time.time()
        out = self.raw(url, b"POST /api/ingest HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: -1\r\n\r\n{}")
        self.assertTrue(out.startswith(b"HTTP/1.1 400"), out[:80])
        self.assertLess(time.time() - t, 5, "it did not wait for a body that never ends")

    def test_a_gzip_bomb_is_stopped_at_the_limit(self):
        url = self.serve()
        z, buf = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS), io.BytesIO()
        zero = b"\0" * (1 << 20)
        for _ in range(MAX_BODY // len(zero) + 8):
            buf.write(z.compress(zero))
        buf.write(z.flush())
        self.assertLess(buf.tell(), 1 << 20, "a small upload")
        st, _, body = http(f"{url}/api/ingest", "POST", {"Content-Encoding": "gzip", "Content-Type": "application/json"},
                           buf.getvalue())
        self.assertEqual(st, 400)
        self.assertIn("too large", body)
        # an honest gzip body still works, several members included
        run = json.dumps({"id": "gz", "project": "alpha", "steps": [{"kind": "prompt", "ts": NOW, "text": "zipped"}]}).encode()
        st, _, _ = http(f"{url}/api/ingest", "POST", {"Content-Encoding": "gzip"}, gzip.compress(run[:9]) + gzip.compress(run[9:]))
        self.assertEqual(st, 200)
        self.eng.refresh(force=True)
        self.assertTrue(self.eng.con.execute("SELECT 1 FROM runs WHERE id = 'gz'").fetchone())

    def test_decompression_stops_at_the_limit_not_after(self):
        import tracemalloc
        from agentdynamics import server
        z = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        bomb = b"".join(z.compress(b"\0" * (1 << 20)) for _ in range(200)) + z.flush()     # 200 MB inflated
        old = server.MAX_BODY
        server.MAX_BODY = 1 << 20
        tracemalloc.start()
        try:
            with self.assertRaises(server.BadRequest):
                server._inflate(bomb, 16 + zlib.MAX_WBITS)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
            server.MAX_BODY = old
        self.assertLess(peak, 20 << 20, "it never held more than the limit's worth")

    def test_a_chunked_upload_is_read(self):
        url = self.serve()
        run = json.dumps({"id": "chunked", "project": "alpha", "steps": [{"kind": "prompt", "ts": NOW, "text": "in pieces"}]}).encode()
        body = b"".join(b"%x\r\n%s\r\n" % (len(run[i:i + 7]), run[i:i + 7]) for i in range(0, len(run), 7)) + b"0\r\n\r\n"
        out = self.raw(url, b"POST /api/ingest HTTP/1.1\r\nHost: 127.0.0.1\r\nTransfer-Encoding: chunked\r\n"
                            b"Content-Type: application/json\r\n\r\n" + body)
        self.assertTrue(out.startswith(b"HTTP/1.1 200"), out[:200])
        self.eng.refresh(force=True)
        self.assertTrue(self.eng.con.execute("SELECT 1 FROM runs WHERE id = 'chunked'").fetchone())
        out = self.raw(url, b"POST /api/ingest HTTP/1.1\r\nHost: 127.0.0.1\r\nTransfer-Encoding: chunked\r\n\r\n-1\r\n")
        self.assertTrue(out.startswith(b"HTTP/1.1 400"), out[:80])

    def test_a_key_in_any_characters_is_just_wrong(self):
        url = self.serve(keys=[{"name": "k", "role": "read", "key": "k-123456789012345678901"}])
        out = self.raw(url, "GET /api/overview HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer ключ\r\n\r\n".encode())
        self.assertTrue(out.startswith(b"HTTP/1.1 401"), out[:80])


class KeysAtRestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def keys(self, *argv):
        from agentdynamics.__main__ import cmd_keys
        a = argparse.Namespace(action=argv[0], role=argv[1] if len(argv) > 1 else "read",
                               name=argv[2] if len(argv) > 2 else None, project=None)
        out = io.StringIO()
        old, sys.stdout = sys.stdout, out
        try:
            cmd_keys(a, self.tmp)
        finally:
            sys.stdout = old
        return out.getvalue()

    def test_only_the_hash_is_kept(self):
        out = self.keys("create", "read", "sre")
        key = next(w for w in out.split() if w.startswith("ad_r_"))
        with open(config.keys_path(self.tmp), encoding="utf-8") as f:
            stored = f.read()
        self.assertNotIn(key, stored)
        self.assertIn(auth.hash_key(key), stored)
        cfg = config.load(self.tmp)
        self.assertEqual(auth.find_key(key, cfg["auth"]["keys"])["name"], "sre")
        self.assertIsNone(auth.find_key(key[:-1] + "x", cfg["auth"]["keys"]))
        self.assertIn(key[:6], json.dumps(config.public_view(cfg)))

    def test_old_keys_in_clear_are_rehashed_and_keep_working(self):
        config.save_keys(self.tmp, [{"name": "old", "role": "admin", "key": "ad_a_plaintext-key-0123456789"}])
        self.assertIn("stored in clear", self.keys("list"))
        self.keys("rehash")
        with open(config.keys_path(self.tmp), encoding="utf-8") as f:
            self.assertNotIn("plaintext-key", f.read())
        self.assertEqual(auth.find_key("ad_a_plaintext-key-0123456789", config.load(self.tmp)["auth"]["keys"])["name"], "old")

    def test_revoking_by_a_short_prefix_revokes_nothing(self):
        self.keys("create", "read", "a")
        self.keys("create", "ingest", "b")
        self.assertIn("Revoked 0", self.keys("revoke", "read", "ad_"))
        self.assertIn("Revoked 1", self.keys("revoke", "read", "a"))
        self.assertEqual([k["name"] for k in config.load_keys(self.tmp)], ["b"])


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a test certificate")
class TlsTest(Base):
    def test_it_serves_https_itself(self):
        cert, key = os.path.join(self.tmp, "c.pem"), os.path.join(self.tmp, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                        "-days", "2", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                       check=True, capture_output=True)
        srv = make_server(self.eng, "127.0.0.1", 0, cert, key)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        # plain HTTP to the TLS port fails for that client only
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            self.assertFalse(s.recv(100).startswith(b"HTTP/1.1 200"))
        ctx = ssl.create_default_context(cafile=cert)
        with urllib.request.urlopen(f"https://127.0.0.1:{port}/healthz", context=ctx, timeout=20) as r:
            self.assertEqual(r.status, 200)
            self.assertIsNone(r.headers["Strict-Transport-Security"], "never pinned to HTTPS on a loopback name")
        self.eng.cfg["server"]["allowed_hosts"] = ["agentdynamics.internal"]
        with urllib.request.urlopen(urllib.request.Request(f"https://127.0.0.1:{port}/healthz",
                                                           headers={"Host": "agentdynamics.internal"}),
                                    context=ctx, timeout=20) as r:
            self.assertIn("max-age", r.headers["Strict-Transport-Security"])
        ctx12 = ssl.create_default_context(cafile=cert)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            ctx12.maximum_version = ssl.TLSVersion.TLSv1_1       # what the server must refuse
        with self.assertRaises((ssl.SSLError, urllib.error.URLError, ConnectionError)):
            urllib.request.urlopen(f"https://127.0.0.1:{port}/healthz", context=ctx12, timeout=20)


class StartupWarningsTest(unittest.TestCase):
    def test_it_says_what_is_unsafe(self):
        cfg = config.load(tempfile.gettempdir() + "/ad-none")
        self.assertIn("auth is off", " ".join(warnings_for(cfg, "0.0.0.0", False)))
        self.assertEqual(warnings_for(cfg, "127.0.0.1", False), [])
        cfg["auth"] = {"enabled": True, "keys": [{"name": "x", "role": "read", "key": "short"}],
                       "proxy": {"trusted": ["0.0.0.0/0"]}}
        w = " ".join(warnings_for(cfg, "0.0.0.0", False))
        for part in ("in clear", "short enough", "0.0.0.0/0", "no [[auth.access]]"):
            self.assertIn(part, w)
        self.assertNotIn("in clear", " ".join(warnings_for(cfg, "0.0.0.0", True)))


if __name__ == "__main__":
    unittest.main()
