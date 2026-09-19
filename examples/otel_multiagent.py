"""Multi-agent helpdesk traced with OpenTelemetry GenAI semantic conventions, sent as OTLP/HTTP JSON.

This is the shape emitted by the OpenAI Agents SDK, Strands, Semantic Kernel and other gen_ai.* instrumentations:
invoke_agent spans for each agent, chat spans for model calls, execute_tool spans for tools.

    python examples/otel_multiagent.py 40 http://127.0.0.1:8787 [ingest-key]
"""
import json
import random
import sys
import time
import urllib.request
import uuid


def attr(k, v):
    if isinstance(v, bool):
        return {"key": k, "value": {"boolValue": v}}
    if isinstance(v, int):
        return {"key": k, "value": {"intValue": str(v)}}
    if isinstance(v, list):
        return {"key": k, "value": {"arrayValue": {"values": [{"stringValue": str(x)} for x in v]}}}
    return {"key": k, "value": {"stringValue": str(v)}}


class Trace:
    def __init__(self, t0):
        self.tid = uuid.uuid4().hex
        self.spans = []
        self.t = t0

    def span(self, name, parent, dur, attrs, error=None):
        sid = uuid.uuid4().hex[:16]
        s = {"traceId": self.tid, "spanId": sid, "parentSpanId": parent or "", "name": name, "kind": 1,
             "startTimeUnixNano": str(int(self.t * 1e9)), "endTimeUnixNano": str(int((self.t + dur) * 1e9)),
             "attributes": [attr(k, v) for k, v in attrs.items()], "status": {"code": 2, "message": error} if error else {}}
        self.spans.append(s)
        return sid, s

    def advance(self, d):
        self.t += d


def conversation(t0):
    tr = Trace(t0)
    ask = random.choice(["My invoice shows a double charge", "Upgrade my plan to Pro", "The API returns 500 since this morning",
                         "Please delete my account data", "Why is my bill higher this month?"])
    root, root_span = tr.span("invoke_agent triage", None, 0, {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "triage",
                                                               "gen_ai.prompt": ask, "session.id": uuid.uuid4().hex[:8]})
    start = tr.t
    agents = ["triage"]
    route = {"invoice": "billing", "bill": "billing", "Upgrade": "billing", "500": "tech_support", "delete": "privacy"}
    target = next((v for k, v in route.items() if k in ask), "billing")
    agents.append(target)
    if random.random() < 0.2:  # mis-route: bounce back and forth
        agents += ["triage", target]
    failed = None
    for i, ag in enumerate(agents):
        aid, aspan = tr.span(f"invoke_agent {ag}", root, 0, {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": ag})
        a0 = tr.t
        for _ in range(random.randint(1, 3)):
            d = random.uniform(0.6, 3.5)
            rl = random.random() < 0.03
            fin = "max_tokens" if random.random() < 0.03 else "tool_calls"
            model = "claude-haiku-4-5" if ag == "triage" else "claude-sonnet-5"
            tr.span("chat", aid, d, {"gen_ai.operation.name": "chat", "gen_ai.request.model": model, "gen_ai.agent.name": ag,
                                     "gen_ai.usage.input_tokens": random.randint(900, 6000), "gen_ai.usage.output_tokens": random.randint(40, 700),
                                     "gen_ai.response.finish_reasons": [fin],
                                     "gen_ai.response.time_to_first_token": round(random.uniform(0.3, 2.5), 3)},
                    error="529 overloaded_error" if rl else None)
            tr.advance(d)
            if ag != "triage":
                tool = {"billing": random.choice(["get_invoice", "issue_credit"]), "tech_support": random.choice(["query_logs", "status_page"]),
                        "privacy": "delete_user_data"}[ag]
                td = random.uniform(0.1, 1.5)
                err = random.random() < (0.12 if tool == "query_logs" else 0.04)
                tr.span(f"execute_tool {tool}", aid, td, {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool, "gen_ai.agent.name": ag},
                        error=f"{tool}: upstream returned 503" if err else None)
                tr.advance(td)
                if err and random.random() < 0.4:
                    failed = f"{tool} failed after retries"
                    break
        aspan["endTimeUnixNano"] = str(int(tr.t * 1e9))
        aspan["startTimeUnixNano"] = str(int(a0 * 1e9))
        if failed:
            break
    root_span["startTimeUnixNano"] = str(int(start * 1e9))
    root_span["endTimeUnixNano"] = str(int(tr.t * 1e9))
    if failed:
        root_span["status"] = {"code": 2, "message": failed}
    return tr.spans


def main(n=40, url="http://127.0.0.1:8787", key=None):
    random.seed(11)
    now = time.time()
    spans = []
    for i in range(n):
        spans += conversation(now - random.uniform(0, 6 * 86400))
    doc = {"resourceSpans": [{"resource": {"attributes": [attr("service.name", "helpdesk-agents"), attr("deployment.environment.name", "production")]},
                              "scopeSpans": [{"scope": {"name": "openai.agents"}, "spans": spans}]}]}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url.rstrip("/") + "/v1/traces", data=json.dumps(doc).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req) as r:
        print(r.status, r.read().decode()[:200])


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 40, sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8787",
         sys.argv[3] if len(sys.argv) > 3 else None)
