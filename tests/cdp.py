"""A small Chrome DevTools Protocol driver for the console tests: standard library only.

`--dump-dom` can render a page but not use it. This drives a headless Chrome or Edge over the DevTools
protocol instead: navigate, click, fill a field, choose an option, wait for the page to change, and read
it back. It also records, for the whole session, every console error, uncaught exception and HTTP
response of 400 or more, so a test can require an interaction left none -- the "{}GET" 501 that followed a
body-less POST was invisible on screen and obvious in that list.

The protocol runs over a WebSocket; the standard library has no client, so a minimal one is here (RFC 6455:
the client handshake, masked text frames out, text/continuation/ping/close frames in).
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlsplit

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

CHROME_CANDIDATES = [
    os.environ.get("AGENTDYNAMICS_BROWSER"),
    shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser"),
    shutil.which("chrome"), shutil.which("msedge"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/microsoft-edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_browser():
    return next((c for c in CHROME_CANDIDATES if c and os.path.exists(c)), None)


# ---------------------------------------------------------------- the server under test

def serve(data_dir, timeout=60):
    """`agentdynamics serve` on a free port, as its own process (see test_console_ui for why not a
    thread). Returns (process, base url); the caller stops the process."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "agentdynamics", "--data", data_dir, "--claude-root", "",
                             "serve", "--port", str(port), "--interval", "2"],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=2) as r:
                if json.load(r).get("status") == "ok":
                    return proc, url
        except OSError:
            pass
        time.sleep(0.3)
    proc.terminate()
    raise RuntimeError("agentdynamics serve did not become healthy")


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------- WebSocket (client side)

class WebSocket:
    def __init__(self, url, timeout=30):
        u = urlsplit(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path}{'?' + u.query if u.query else ''} HTTP/1.1\r\nHost: {u.netloc}\r\n"
                           f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("DevTools closed the connection during the handshake")
            head += chunk
        status, _, rest = head.partition(b"\r\n\r\n")
        if b" 101 " not in status.split(b"\r\n")[0]:
            raise ConnectionError(f"WebSocket handshake refused: {status[:200]!r}")
        self._buf = rest
        self._lock = threading.Lock()

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise ConnectionError("DevTools connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text):
        self._frame(0x1, text.encode())

    def _frame(self, opcode, payload):
        head = bytes([0x80 | opcode])                     # FIN + opcode (a client never fragments here)
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 1 << 16:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._lock:
            self.sock.sendall(head + mask + masked)

    def recv(self):
        """The next whole text message (continuation frames joined); None when the socket closes."""
        parts = []
        while True:
            b1, b2 = self._read(2)
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x8:                                 # close
                return None
            if op == 0x9:                                 # ping: answer, keep reading
                self._frame(0xA, data)                    # pong
                continue
            if op in (0x1, 0x2, 0x0):
                parts.append(data)
                if b1 & 0x80:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- the browser and a page

class Browser:
    """A headless Chrome/Edge with a fresh profile and remote debugging on a port it chooses."""

    def __init__(self, path, attempts=3, wait=45):
        # A cold CI runner sometimes starts Chrome slowly or not at all (a nightly Windows run never got its
        # DevToolsActivePort in 30 s). Each attempt gets a fresh profile; a failure says what Chrome said.
        errors = []
        for _ in range(attempts):
            self.profile = tempfile.mkdtemp(prefix="ad-cdp-")
            log = open(os.path.join(self.profile, "chrome-stderr.log"), "wb")
            self.proc = subprocess.Popen([path, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
                                          "--no-default-browser-check", "--remote-debugging-port=0",
                                          f"--user-data-dir={self.profile}", "about:blank"],
                                         stdout=subprocess.DEVNULL, stderr=log)
            port_file = os.path.join(self.profile, "DevToolsActivePort")
            deadline = time.time() + wait
            while time.time() < deadline and self.proc.poll() is None:
                if os.path.exists(port_file) and os.path.getsize(port_file):
                    with open(port_file, encoding="utf-8") as f:
                        self.port = int(f.read().split()[0])
                    log.close()
                    return
                time.sleep(0.1)
            self.proc.kill()
            self.proc.wait(timeout=10)
            log.close()
            with open(os.path.join(self.profile, "chrome-stderr.log"), "rb") as f:
                errors.append(f"exit {self.proc.returncode}: {f.read()[-400:].decode('utf-8', 'replace').strip()}")
            shutil.rmtree(self.profile, ignore_errors=True)
        raise RuntimeError(f"{path} did not open a DevTools port in {attempts} attempts: " + " | ".join(errors))

    def page(self):
        """A page (tab) to drive: the one the browser opened with."""
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=10) as r:
            tabs = [t for t in json.load(r) if t.get("type") == "page"]
        return Page(tabs[0]["webSocketDebuggerUrl"])

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


class Page:
    def __init__(self, ws_url):
        self.ws = WebSocket(ws_url)
        self._id = 0
        self._waiting = {}                                # message id -> [Event, reply]
        self._mu = threading.Lock()
        self.console_errors, self.exceptions, self.http_errors = [], [], []
        threading.Thread(target=self._reader, daemon=True).start()
        for domain in ("Page", "Runtime", "Network", "Log"):
            self.call(f"{domain}.enable")

    # -- protocol
    def _reader(self):
        while True:
            try:
                raw = self.ws.recv()
            except (ConnectionError, OSError):
                raw = None
            if raw is None:
                for ev, slot in list(self._waiting.values()):
                    slot.append({"error": {"message": "DevTools connection closed"}})
                    ev.set()
                return
            msg = json.loads(raw)
            if "id" in msg:
                with self._mu:
                    w = self._waiting.pop(msg["id"], None)
                if w:
                    w[1].append(msg)
                    w[0].set()
            else:
                self._event(msg.get("method"), msg.get("params") or {})

    def _event(self, method, p):
        if method == "Runtime.consoleAPICalled" and p.get("type") == "error":
            self.console_errors.append(" ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", [])))
        elif method == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails") or {}
            self.exceptions.append((d.get("exception") or {}).get("description") or d.get("text"))
        elif method == "Log.entryAdded" and (p.get("entry") or {}).get("level") == "error":
            self.console_errors.append(p["entry"].get("text"))
        elif method == "Network.responseReceived":
            r = p.get("response") or {}
            if r.get("status", 0) >= 400:
                self.http_errors.append(f"{r.get('status')} {r.get('url')} {r.get('statusText', '')}")

    def call(self, method, params=None, timeout=30):
        with self._mu:
            self._id += 1
            mid = self._id
            slot = self._waiting[mid] = [threading.Event(), []]
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        if not slot[0].wait(timeout):
            raise TimeoutError(f"{method} got no reply in {timeout}s")
        reply = slot[1][0]
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result") or {}

    # -- what a test uses
    def eval(self, expr):
        """Evaluate JavaScript in the page; awaits a promise; returns the value."""
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise RuntimeError(f"page threw: {(d.get('exception') or {}).get('description') or d.get('text')}")
        return (r.get("result") or {}).get("value")

    def wait(self, expr, timeout=15, what=None):
        """Poll until `expr` is truthy in the page; returns its value."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                last = self.eval(expr)
                if last:
                    return last
            except RuntimeError:
                pass
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for {what or expr} (last value {last!r})")

    def goto(self, url):
        self.call("Page.navigate", {"url": url})
        self.wait("document.readyState === 'complete'", what="the page to load")

    def settle(self, timeout=15):
        """Wait until the router has drawn: no 'Loading' placeholder, and no fetch in flight for a moment."""
        self.wait("!document.querySelector('#page .loading') && document.querySelector('#page').children.length > 0",
                  timeout, "the page to draw")
        time.sleep(0.3)

    def click(self, selector):
        self.eval(f"(() => {{ const e = document.querySelector({json.dumps(selector)});"
                  f" if (!e) throw new Error('no element ' + {json.dumps(selector)}); e.click(); }})()")

    def fill(self, selector, value):
        self.eval(f"(() => {{ const e = document.querySelector({json.dumps(selector)});"
                  f" if (!e) throw new Error('no element ' + {json.dumps(selector)});"
                  f" e.value = {json.dumps(value)}; e.dispatchEvent(new Event('input', {{bubbles: true}}));"
                  f" e.dispatchEvent(new Event('change', {{bubbles: true}})); }})()")

    def select(self, selector, value):
        self.fill(selector, value)

    def text(self, selector="#page"):
        return self.eval(f"(document.querySelector({json.dumps(selector)}) || {{}}).innerText || ''")

    def problems(self):
        """Everything that went wrong in the page so far: console errors, exceptions, HTTP errors."""
        return self.console_errors + [f"exception: {e}" for e in self.exceptions] + \
            [f"http: {h}" for h in self.http_errors]

    def close(self):
        self.ws.close()
