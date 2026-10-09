"""Who is calling: an API key, a person signed in through your identity provider (OIDC), or a person a reverse
proxy signed in. Standard library only.

  * Keys. `agentdynamics keys create` stores only a key's SHA-256 (`hash`); keys in the TOML file or in
    AGENTDYNAMICS_API_KEYS are compared as given. Every record is compared in constant time.
  * People. `[auth.oidc]` signs them in with the authorization code flow and PKCE. The ID token comes straight
    from the token endpoint over TLS, so the TLS server check stands in for its signature (OpenID Connect Core
    3.1.3.7, step 6); its issuer, audience, expiry and nonce are checked. `[auth.proxy]` instead trusts a header
    set by a proxy that already signed them in (oauth2-proxy, Cloudflare Access, an ingress), from the proxy's
    addresses only. Either way `[[auth.access]]` decides the role, first match wins, nobody else gets in. The
    session is a signed cookie holding who they are, never their role, so a changed rule applies to every
    session at the next restart.
  * Requests from other sites. A state-changing request from another site (Sec-Fetch-Site, else Origin) is
    refused, cookie or not, and with auth off the Host header must name this server (DNS rebinding).
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SESSION_COOKIE = "ad_session"
STATE_COOKIE = "ad_oidc"
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
ROLES = ("ingest", "read", "admin")


class AuthError(Exception):
    """A sign-in that must not go through. The message is safe to show the person."""


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------------------------------------------------------------- API keys

def hash_key(key):
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def find_key(presented, keys):
    """The key record `presented` matches, or None. Every record is compared, as bytes (a header can hold any
    character), so how long it takes doesn't say how close a guess came."""
    if not presented:
        return None
    raw, digest, found = presented.encode("utf-8", "surrogateescape"), hash_key(presented).encode(), None
    for k in keys:
        if k.get("hash"):
            ok = hmac.compare_digest(digest, str(k["hash"]).encode())
        else:
            ok = hmac.compare_digest(raw, str(k.get("key", "")).encode("utf-8"))
        if ok and found is None:
            found = k
    return found


# ---------------------------------------------------------------- signed values (cookies)

def sign(secret, purpose, data):
    """`data` as a tamper-evident string for `purpose`: a state cookie can never pass as a session cookie."""
    body = _b64(json.dumps(data, separators=(",", ":")).encode())
    mac = hmac.new(secret, f"{purpose}.{body}".encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def unsign(secret, purpose, token, now=None):
    """What `sign` signed, or None: a bad signature, a malformed value, or past its `exp`."""
    try:
        body, mac = (token or "").split(".")
        want = hmac.new(secret, f"{purpose}.{body}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(mac), want):
            return None
        data = json.loads(_unb64(body))
        if not isinstance(data, dict) or float(data.get("exp") or 0) < (now or time.time()):
            return None
    except (ValueError, TypeError):
        return None
    return data


def session_secret(engine):
    """The key sessions are signed with: AGENTDYNAMICS_SESSION_SECRET, else one made on first use and kept in the
    store (instances sharing a Postgres schema share it; `store copy` carries it)."""
    env = os.environ.get("AGENTDYNAMICS_SESSION_SECRET")
    if env:
        return env.encode()
    from . import store
    with engine.lock:
        st = store.get_state(engine.con, "auth:session")
        if not st.get("secret"):
            st = {"secret": secrets.token_hex(32), "created": int(time.time())}
            store.set_state(engine.con, "auth:session", st)
    return bytes.fromhex(st["secret"])


# ---------------------------------------------------------------- people: who gets which role

def access_for(person, rules):
    """The caller record for a signed-in person under [[auth.access]]: {name, role, projects?}. The first rule that
    matches wins: an email, "*@example.com", "group:<name>", or "*" (anyone the provider signs in). No match:
    a record with no role, which the server answers 403 with the reason."""
    email = (person.get("email") or "").strip().lower()
    groups = {str(g).strip().lower() for g in person.get("groups") or ()}
    name = person.get("email") or person.get("name") or person.get("sub") or "unknown"
    for r in rules or ():
        m = str(r.get("match") or "").strip().lower()
        if m.startswith("group:"):
            hit = m[6:] in groups
        elif m.startswith("*@"):
            hit = bool(email) and email.endswith(m[1:])
        elif m == "*":
            hit = True
        else:
            hit = bool(m) and email == m
        if not hit:
            continue
        role = r.get("role")
        if role not in ROLES:
            return {"name": name, "role": None, "person": True, "invalid": f"the access rule for {m} has no valid role"}
        if role == "admin" and "projects" in r:
            return {"name": name, "role": None, "person": True,
                    "invalid": "admin access cannot be scoped to projects; use read"}
        out = {"name": name, "role": role, "person": True}
        if isinstance(r.get("projects"), list):
            out["projects"] = [str(p) for p in r["projects"]]
        return out
    return {"name": name, "role": None, "person": True,
            "invalid": f"signed in as {name}, who has no access here: an admin can add an [[auth.access]] rule"}


def relevant_groups(groups, rules):
    """Only the groups some rule names: a provider can send hundreds, and a cookie holds about 4 KB."""
    named = {str(r.get("match") or "")[6:].strip().lower() for r in rules or ()
             if str(r.get("match") or "").lower().startswith("group:")}
    return sorted({str(g) for g in groups or () if str(g).strip().lower() in named})


def _in_networks(addr, networks):
    try:
        ip = ipaddress.ip_address(addr)
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return any(ip in ipaddress.ip_network(str(n), strict=False) for n in networks or ())
    except ValueError:
        return False


def from_proxy(conf, peer, headers):
    """The person a trusted proxy says is calling, or None. The header counts only when the connection comes from
    one of `trusted`: from anywhere else anyone could send it."""
    if not conf or not _in_networks(peer, conf.get("trusted")):
        return None
    user = (headers.get(conf.get("user_header") or "X-Forwarded-Email") or "").strip()
    if not user:
        return None
    gh = conf.get("groups_header")
    groups = [g.strip() for g in (headers.get(gh) or "").split(",") if g.strip()] if gh else []
    return {"email": user if "@" in user else None, "name": user, "sub": user, "groups": groups}


# ---------------------------------------------------------------- requests from other sites

def host_name(host_header):
    """'example.com:8787' -> 'example.com', '[::1]:80' -> '::1'."""
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        return h[1:h.index("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def is_loopback(host):
    h = host_name(host)
    if h in LOOPBACK or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def cross_site(headers, own_hosts):
    """Whether a browser sent this request from another site. Sec-Fetch-Site says so directly (every current
    browser sends it); without it, an Origin that isn't one of this server's names. Clients that aren't browsers
    send neither."""
    sfs = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if sfs:
        return sfs not in ("same-origin", "none")
    origin = (headers.get("Origin") or "").strip()
    if not origin:
        return False
    # "null" (a sandboxed frame, a file) names no host, so it is never one of ours
    return urllib.parse.urlparse(origin).netloc.lower() not in {h.lower() for h in own_hosts if h}


# ---------------------------------------------------------------- OIDC

def _local(url):
    return is_loopback(urllib.parse.urlparse(url).hostname or "")


class Oidc:
    """The authorization code flow with PKCE against one provider. `open` is urllib.request.urlopen (tests pass
    their own only to count calls; the provider in tests is a real local server)."""

    def __init__(self, conf, secret, open_url=urllib.request.urlopen):
        self.conf = conf
        self.secret = secret
        self.issuer = str(conf.get("issuer") or "").rstrip("/")
        self.client_id = str(conf.get("client_id") or "")
        env = conf.get("client_secret_env")
        self.client_secret = os.environ.get(env) if env else conf.get("client_secret")
        self.scopes = conf.get("scopes") or "openid email profile"
        self.groups_claim = conf.get("groups_claim") or "groups"
        self.session_hours = float(conf.get("session_hours") or 12)
        self._open = open_url
        self._meta, self._meta_at = None, 0
        self._lock = threading.Lock()
        if not self.issuer or not self.client_id:
            raise AuthError("[auth.oidc] needs issuer and client_id")
        if not self.issuer.startswith("https://") and not _local(self.issuer):
            raise AuthError("[auth.oidc] issuer must be https (http only for a provider on this machine)")

    def redirect_uri(self, fallback_base):
        return self.conf.get("redirect_url") or fallback_base.rstrip("/") + "/auth/callback"

    def _fetch(self, url, data=None, headers=None):
        if not url.startswith("https://") and not _local(url):
            raise AuthError("the identity provider's endpoints must be https")
        req = urllib.request.Request(url, data=data, headers=dict({"Accept": "application/json"}, **(headers or {})))
        try:
            with self._open(req, timeout=15) as r:
                return json.loads(r.read(1 << 20) or b"{}")
        except urllib.error.HTTPError as ex:
            detail = ex.read(2000).decode("utf-8", "replace")
            raise AuthError(f"the identity provider refused ({ex.code}): {detail[:300]}") from None
        except (urllib.error.URLError, OSError, ValueError) as ex:
            raise AuthError(f"cannot reach the identity provider: {ex}") from None

    def meta(self):
        """The provider's discovery document, kept for an hour. Its issuer must be the one configured."""
        with self._lock:
            if self._meta and time.time() - self._meta_at < 3600:
                return self._meta
        m = self._fetch(self.issuer + "/.well-known/openid-configuration")
        if str(m.get("issuer") or "").rstrip("/") != self.issuer:
            raise AuthError(f"the provider at {self.issuer} says its issuer is {m.get('issuer')!r}")
        for k in ("authorization_endpoint", "token_endpoint"):
            if not m.get(k):
                raise AuthError(f"the provider's discovery document has no {k}")
        with self._lock:
            self._meta, self._meta_at = m, time.time()
        return m

    def login(self, base, next_path="/"):
        """Where to send the browser, and the state cookie's value (state, nonce and PKCE verifier, for 10 min)."""
        m = self.meta()
        st = {"state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24),
              "verifier": secrets.token_urlsafe(48), "next": _safe_next(next_path), "exp": time.time() + 600}
        challenge = _b64(hashlib.sha256(st["verifier"].encode()).digest())
        q = {"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri(base),
             "scope": self.scopes, "state": st["state"], "nonce": st["nonce"],
             "code_challenge": challenge, "code_challenge_method": "S256"}
        sep = "&" if "?" in m["authorization_endpoint"] else "?"
        return m["authorization_endpoint"] + sep + urllib.parse.urlencode(q), sign(self.secret, "oidc-state", st)

    def callback(self, base, query, state_cookie):
        """The person, from the provider's redirect back: (person, next path). Raises AuthError."""
        st = unsign(self.secret, "oidc-state", state_cookie)
        if not st:
            raise AuthError("the sign-in expired or was started in another browser: sign in again")
        if query.get("error"):
            raise AuthError(f"the identity provider said: {query.get('error_description') or query['error']}")
        if not hmac.compare_digest(str(query.get("state") or ""), st["state"]) or not query.get("code"):
            raise AuthError("this sign-in response does not belong to the sign-in this browser started")
        m = self.meta()
        form = {"grant_type": "authorization_code", "code": query["code"], "redirect_uri": self.redirect_uri(base),
                "code_verifier": st["verifier"], "client_id": self.client_id}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.client_secret:
            if self.conf.get("token_auth") == "post":
                form["client_secret"] = self.client_secret
            else:                    # client_secret_basic, the default (RFC 6749 2.3.1: each part form-encoded)
                pair = f"{urllib.parse.quote(self.client_id, safe='')}:{urllib.parse.quote(self.client_secret, safe='')}"
                headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        tok = self._fetch(m["token_endpoint"], urllib.parse.urlencode(form).encode(), headers)
        claims = self.check_id_token(tok.get("id_token"), st["nonce"], m["issuer"])
        groups = claims.get(self.groups_claim)
        if groups is None and m.get("userinfo_endpoint") and tok.get("access_token"):
            info = self._fetch(m["userinfo_endpoint"], headers={"Authorization": f"Bearer {tok['access_token']}"})
            if info.get("sub") == claims.get("sub"):
                groups = info.get(self.groups_claim)
        if isinstance(groups, str):
            groups = [g for g in groups.replace(",", " ").split() if g]
        email = claims.get("email") if claims.get("email_verified", True) is not False else None
        person = {"sub": str(claims["sub"]), "email": email, "groups": list(groups or ()),
                  "name": email or claims.get("preferred_username") or claims.get("name") or str(claims["sub"])}
        return person, st.get("next") or "/"

    def check_id_token(self, token, nonce, issuer, now=None):
        """The ID token's claims, once its issuer, audience, expiry and nonce check out."""
        now = now or time.time()
        try:
            h, c, _sig = (token or "").split(".")
            header, claims = json.loads(_unb64(h)), json.loads(_unb64(c))
        except (ValueError, TypeError):
            raise AuthError("the provider sent no usable ID token") from None
        if str(header.get("alg", "")).lower() == "none":
            raise AuthError("the provider sent an unsigned ID token")
        if str(claims.get("iss") or "").rstrip("/") != str(issuer).rstrip("/"):
            raise AuthError("the ID token is from another issuer")
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if self.client_id not in auds or (len(auds) > 1 and claims.get("azp") not in (None, self.client_id)):
            raise AuthError("the ID token is for another application")
        if float(claims.get("exp") or 0) < now - 60:
            raise AuthError("the ID token has expired")
        if not hmac.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise AuthError("the ID token was not issued for this sign-in")
        if not claims.get("sub"):
            raise AuthError("the ID token names no one")
        return claims

    def session(self, person, rules):
        """The session cookie's value: who they are (only the groups a rule names), for session_hours."""
        now = time.time()
        return sign(self.secret, "session", {"sub": person["sub"], "email": person.get("email"),
                                             "name": person.get("name"),
                                             "groups": relevant_groups(person.get("groups"), rules),
                                             "iat": int(now), "exp": now + self.session_hours * 3600})


def read_session(secret, value):
    return unsign(secret, "session", value)


def _safe_next(path):
    """Only a path on this server: a sign-in must never forward anyone elsewhere."""
    p = str(path or "/")
    return p if p.startswith("/") and not p.startswith("//") and "\\" not in p else "/"


def cookie(name, value, path="/", max_age=None, secure=False, same_site="Strict"):
    parts = [f"{name}={value}", f"Path={path}", "HttpOnly", f"SameSite={same_site}"]
    if max_age is not None:
        parts.append(f"Max-Age={int(max_age)}")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)
