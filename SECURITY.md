# Security

AgentDynamics holds what your agents saw and did: prompts, tool arguments and results, errors, costs. Treat an
install like any system that stores customer conversations.

## Reporting a vulnerability

Report privately through GitHub's **Report a vulnerability** button on the Security tab. Please don't open a public
issue first. These are the classes I most want to hear about:

- a key or a signed-in person scoped to some projects reading anything from another project;
- a request without the role it needs (`ingest` < `read` < `admin`) changing state, or a request from another
  site doing so through someone's browser;
- the Aegis integration or a server directive giving an agent authority back (it may only revoke and restrict);
- prompt or tool text reaching the store, an alert or the console unredacted where redaction is configured.

## Supported versions

Security fixes land on the latest minor release. Pin an exact version, and verify what you install:

```bash
gh attestation verify agentdynamics-*.whl --repo Aditya31398/agentdynamics
gh attestation verify oci://ghcr.io/aditya31398/agentdynamics:<version> --repo Aditya31398/agentdynamics
```

## What has been reviewed, and what hasn't

**There has been no external security review or penetration test.** Until there is, weigh it as you would any
0.x project: deploy it behind your own controls (below) rather than on the open internet.

What there is:

- **Tests that attack it.** `tests/test_security.py` signs in against a local identity provider and tries forged,
  expired and swapped sessions, ID tokens for another app or issuer or sign-in, login CSRF, open redirects,
  lookalike domains, cross-site requests, DNS rebinding, gzip bombs, negative and chunked lengths, and keys in odd
  characters. `tests/test_scoped_keys.py` calls every route in `server.py` with a scoped key and fails if another
  project's data appears anywhere. Each test is checked to fail when the defence it covers is removed.
- **An internal review of the HTTP layer** (0.10). What it found and fixed:
  - a gzip or deflate body was decompressed without a limit (a few KB could become gigabytes in memory);
  - a negative `Content-Length` made the server read until the client hung up;
  - a chunked body wasn't read, and its bytes became the next request on the connection;
  - with auth off, any web page could POST to the API through the browser (CSRF: change rules, revoke agents) and,
    with DNS rebinding, read it;
  - nothing stopped another site framing the console (its buttons revoke agents);
  - `keys.json` held keys in clear; a key with non-ASCII characters caused a server error instead of a 401;
  - `agentdynamics keys revoke --name ad` removed every key;
  - idle connections held a thread forever;
  - with auth on, the console's sign-in card was replaced by an error before anyone could use it.

## Threat model

| Who | Can | Trusted for |
|---|---|---|
| `ingest` key | send telemetry and state outcomes, in its projects if scoped | what its agents did: the numbers are only as honest as the senders |
| `read` key or person | read everything in scope, prompts included | nothing written |
| `admin` key or person | edit rules, SLOs and targets, revoke and restrict agents, judge incidents | install-wide changes (never scoped) |
| identity provider | say who a person is (`[auth.oidc]`) | the ID token, fetched from its token endpoint over TLS |
| sign-in proxy | say who a person is (`[auth.proxy]`) | its header, only on connections from `trusted` addresses |

Not covered, by design or not yet:

- **No rate limiting.** Keys from `agentdynamics keys create` are 192 bits, so guessing is not the risk; a short key
  in the TOML file is (the server warns at start).
- **Sessions end at their expiry** (`session_hours`, 12 by default), or for everyone at once when
  `AGENTDYNAMICS_SESSION_SECRET` changes. One session can't be ended early. Access rules are read on every request,
  so removing someone's rule takes effect at the next restart.
- **Reads aren't audited.** Grades, verdicts and directives record who made them; viewing doesn't.
- **The checker and the reviewer send task content to Anthropic's API** when you turn them on (`[checker]`,
  `review=`). Both are off by default.
- **Aegis protects only the tools it mediates**: tools called through a kernel, MCP servers behind `aegis gateway`,
  Claude Code's tools under `aegis hook`. AgentDynamics shows which observed calls were governed.

## Deploying it safely

1. **Turn auth on.** Keys for machines (`agentdynamics keys create --role ingest`); people sign in with your
   identity provider (`[auth.oidc]`) or through a sign-in proxy (`[auth.proxy]`), and `[[auth.access]]` gives them a
   role, first match wins, nobody else gets in. Scope `read` access to projects where teams shouldn't see each
   other's data.
2. **Serve TLS.** `agentdynamics serve --tls-cert cert.pem --tls-key key.pem`, or keep a TLS proxy in front and the
   port closed to everything else. Session cookies are `Secure` whenever the console's address is `https`.
3. **Name the server.** `[alerts] console_url` (the public address: sign-in redirects and same-site checks use
   it) and, with auth off, `[server] allowed_hosts`.
4. **Keep less.** `[privacy] redact` masks secrets at ingest; `store_content = false` keeps sizes and metadata only;
   `[retention] days` purges old spans.
5. **On Postgres,** connect over TLS with a user that owns only AgentDynamics' schema.
6. **Read the startup warnings.** `serve` prints what is unsafe about how it was started: auth off on a reachable
   address, keys in clear over the network, a short key, a proxy trusted from anywhere, a rule that lets in anyone.

## How sign-in works

- **Single sign-on** is OpenID Connect's authorization code flow with PKCE. The state, nonce and verifier travel in
  a signed, short-lived cookie; the ID token comes straight from the provider's token endpoint over TLS, so the TLS
  check stands in for its signature (OpenID Connect Core 3.1.3.7), and its issuer, audience, expiry and nonce are
  checked. An address the provider marks unverified is ignored.
- **Sessions** are HMAC-SHA256-signed cookies holding who the person is (never their role): `HttpOnly`,
  `SameSite=Strict`, `Secure` over https. The key comes from `AGENTDYNAMICS_SESSION_SECRET`, or is made once and kept
  in the store, so instances sharing a Postgres schema share it.
- **Requests from other sites** that would change something are refused (`Sec-Fetch-Site`, else `Origin`), cookie
  or not. With auth off, the `Host` header must name this server, so a rebinding page can't read the API.
- **Every response** carries a strict Content-Security-Policy (no inline script, nothing from elsewhere, no framing),
  `X-Frame-Options: DENY`, `nosniff` and `no-referrer`; over TLS, HSTS on a named host.
- **Request bodies** are bounded before and after decompression (64 MB), chunked uploads included.
