"""Headless smoke tests for the console.

Every page is checked by hand today, which is how "GRANTS USED: 6 of 5" shipped in 0.4.0. These
tests load each page in headless Chrome against a fixture dataset and assert that it rendered:
the heading is right, the tables people read are populated, and nothing threw on the way.

They skip when no Chrome or Edge is installed, the way the ecosystem tests skip without their
libraries, so the pinned matrix stays dependency-light.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agentdynamics.engine import Engine  # noqa: E402

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
    for c in CHROME_CANDIDATES:
        if c and os.path.exists(c):
            return c
    return None


BROWSER = find_browser()

# One traced request through a governed agent: enough to populate every page under test.
# Timestamps are relative to now: the console asks for the last 30 days by default, so a fixture
# dated 1970 renders an empty window and looks exactly like a page that failed to load.
T = time.time() - 3600
RUN = {
    "id": "ui-run-1", "project": "ui", "workflow": "support_flow", "environment": "production",
    "agent": "support", "policy_version": "support@v1#abc123",
    "policy": {"name": "support", "doc": None},   # filled in below, once POLICY_DOC exists
    "steps": [
        {"kind": "prompt", "ts": T + 0, "text": "Where is my refund?"},
        {"kind": "span", "ts": T + 0, "end_ts": T + 4, "span_kind": "agent", "name": "plan", "node": "plan"},
        {"kind": "llm", "ts": T + 0, "end_ts": T + 2, "model": "claude-sonnet-5",
         "input_tokens": 1200, "output_tokens": 300, "stop_reason": "tool_use"},
        {"kind": "tool", "ts": T + 2, "end_ts": T + 3, "name": "orders.lookup", "governed": 1,
         "rule": "kernel.admitted", "args_json": json.dumps({"order_id": "ORD-1"})},
        {"kind": "tool", "ts": T + 3, "end_ts": T + 3, "name": "payments.refund", "denied": 1,
         "governed": 1, "rule": "capability.arg_max_value", "error": "over the cap",
         "args_json": json.dumps({"amount": 950})},
        # Two plain @tool functions no kernel mediates. With the old counting, `used` was every
        # traced tool: {orders.lookup, draft_reply, format_reply} = 3 against 2 granted, "3 of 2".
        {"kind": "tool", "ts": T + 4, "end_ts": T + 5, "name": "draft_reply"},
        {"kind": "tool", "ts": T + 5, "end_ts": T + 5, "name": "format_reply"},
        {"kind": "llm", "ts": T + 5, "end_ts": T + 7, "model": "claude-haiku-4-5",
         "input_tokens": 400, "output_tokens": 120, "stop_reason": "end_turn"},
    ],
}

POLICY_DOC = {
    "name": "support", "version": 1,
    "tools": {"allow": [{"name": "orders.lookup"}, {"name": "payments.refund"}]},
    "budget": {"usd": 1.0, "tokens": 100000, "wall_clock_s": 60, "tool_calls": 50},
}

PAGES = {
    "overview": "Agent Overview",
    "flow": "Flow Map",
    "workflows": "Workflows",
    "types": "Task Types",
    "tasks": "Tasks",
    "tools": "Tools",
    "models": "Models",
    "events": "Events",
    "governance": "Governance",
    "process": "Process Review",
    "analytics": "Analytics",
    "start": "Get started",
}


def table_rows(html, header):
    """Rows of the first table whose headers include `header`, as {header: cell text} dicts."""
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tables, self.cur, self.cell, self.in_cell = [], None, [], False

        def handle_starttag(self, tag, attrs):
            if tag == "table":
                self.cur = {"head": [], "rows": []}
            elif tag == "tr" and self.cur is not None:
                self.cur["rows"].append([])
            elif tag in ("td", "th") and self.cur is not None:
                self.in_cell, self.cell = True, []

        def handle_endtag(self, tag):
            if tag in ("td", "th") and self.cur is not None and self.in_cell:
                text = " ".join("".join(self.cell).split())
                (self.cur["head"] if tag == "th" else self.cur["rows"][-1]).append(text)
                self.in_cell = False
            elif tag == "table" and self.cur is not None:
                self.tables.append(self.cur)
                self.cur = None

        def handle_data(self, data):
            if self.in_cell:
                self.cell.append(data)

    p = P()
    p.feed(html)
    for t in p.tables:
        if header in t["head"]:
            return [dict(zip(t["head"], r)) for r in t["rows"] if r]
    return []


@unittest.skipUnless(BROWSER, "no Chrome/Edge found (set AGENTDYNAMICS_BROWSER)")
class ConsoleUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        data = os.path.join(cls.tmp, "data")
        eng = Engine(data, None)
        run = json.loads(json.dumps(RUN))
        run["policy"]["doc"] = POLICY_DOC    # gives the Governance page a "Policies in use" row
        eng.ingest(run)
        eng.refresh(force=True)
        eng.con.close()
        # The server runs as its own process, the way users run it. On a thread inside this
        # process it raced Chrome's DOM dump and the page was captured mid-boot on "Loading",
        # intermittently -- a flaky test that blames the app. Five out of five renders complete
        # against a separate process.
        with socket.socket() as s_:
            s_.bind(("127.0.0.1", 0))
            port = s_.getsockname()[1]
        cls.srv = subprocess.Popen(
            [sys.executable, "-m", "agentdynamics", "--data", data, "--claude-root", "",
             "serve", "--port", str(port)],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.url = f"http://127.0.0.1:{port}"
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(cls.url + "/healthz", timeout=2) as r:
                    if json.load(r).get("status") == "ok":
                        break
            except OSError:
                pass
            time.sleep(0.3)
        else:
            cls.srv.terminate()
            raise RuntimeError("agentdynamics serve did not become healthy")

    @classmethod
    def tearDownClass(cls):
        cls.srv.terminate()
        try:
            cls.srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.srv.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def render(self, route):
        """Return the page's DOM after the SPA has drawn it."""
        out = os.path.join(self.tmp, f"{route}.html")
        # A fresh profile per launch. Reusing one makes every launch after the first hit Chrome's
        # singleton lock, hand off to the running instance and exit at once, which dumps the empty
        # shell -- the page looks stuck on "Loading" and the failure points at the app, not at us.
        profile = tempfile.mkdtemp(dir=self.tmp)
        # --dump-dom runs the page and prints the DOM, so it sees what the router rendered
        # rather than the empty shell index.html ships.
        proc = subprocess.run(
            [BROWSER, "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={profile}", "--virtual-time-budget=15000",
             "--dump-dom", f"{self.url}/#/{route}"],
            capture_output=True, timeout=120,
            # Explicit utf-8: the console renders x, /, em-dashes and the like, and decoding the
            # DOM with the Windows default codepage kills the reader thread and yields None.
            encoding="utf-8", errors="replace")
        html = proc.stdout or ""
        self.assertTrue(html.strip(), f"#/{route} produced no DOM (exit {proc.returncode})")
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        return html

    @staticmethod
    def text(html):
        import re
        body = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", body))

    def test_every_page_renders(self):
        for route, heading in PAGES.items():
            with self.subTest(route=route):
                html = self.render(route)
                txt = self.text(html)
                self.assertIn(heading, txt, f"#/{route} did not render its heading")
                self.assertNotIn("Something went wrong", txt)
                self.assertNotIn("undefined undefined", txt)

    def test_overview_shows_the_recorded_numbers(self):
        txt = self.text(self.render("overview"))
        self.assertIn("support_flow", txt)          # the workflow reached the task-type table
        # 1200 + 300 + 400 + 120 tokens across the two model calls
        self.assertIn("2.0k tokens processed", txt)
        self.assertIn("claude-sonnet-5", self.text(self.render("models")))

    def test_governance_grants_used_never_exceeds_granted(self):
        """The "6 of 5" bug: `used` counted tools no kernel mediates.

        Verified to fail with that bug reintroduced -- an earlier version of this test passed
        against the broken code, because its fixture could not produce used > granted and its
        regex matched the first "N of M" anywhere on the page.
        """
        rows = table_rows(self.render("governance"), "Grants used")
        self.assertTrue(rows, "the Policies in use table should have a row")
        row = rows[0]
        used, _, granted = row["Grants used"].partition(" of ")
        self.assertEqual((int(used), int(granted)), (1, 2),
                         f"grants used cell reads {row['Grants used']!r}")
        # the tools nothing mediated are reported in their own column instead
        self.assertIn("draft_reply", row["Ungoverned tools"])
        self.assertIn("format_reply", row["Ungoverned tools"])
        # payments.refund was only ever refused, so it was granted but never successfully used
        self.assertIn("payments.refund", row["Unused grants"])

    def test_no_console_errors_or_remote_requests(self):
        """Invariant 8: the console works offline. Nothing may be fetched from a CDN."""
        html = self.render("overview")
        for bad in ("https://cdn", "http://cdn", "unpkg.com", "googleapis.com", "jsdelivr"):
            self.assertNotIn(bad, html, f"the console must not load {bad}")


if __name__ == "__main__":
    unittest.main()
