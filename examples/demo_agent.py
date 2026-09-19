"""Simulated custom agent reporting to AgentDynamics through the SDK.

Run the console first (python -m agentdynamics serve), then: python examples/demo_agent.py
"""
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentdynamics.sdk import Tracer

tracer = Tracer(agent="support-bot", project="demo-helpdesk")
TICKETS = ["Refund order #%d" % n for n in range(1, 6)] + ["Why was I charged twice?", "Fix my broken login"]

for ticket in TICKETS:
    with tracer.task(ticket) as task:
        ctx = 3000
        for turn in range(random.randint(2, 5)):
            t0 = time.time()
            time.sleep(0.05)
            ctx += random.randint(500, 3000)
            last = turn == 3 or random.random() < 0.25
            task.llm("claude-sonnet-5", input_tokens=400, cache_read=ctx, output_tokens=random.randint(80, 400),
                     stop_reason="end_turn" if last else "tool_use", start_ts=t0, text="Done." if last else "")
            if last:
                break
            with task.tool(random.choice(["lookup_order", "search_kb", "issue_refund"]), {"ticket": ticket}) as call:
                time.sleep(0.02)
                if random.random() < 0.15:
                    call.error("upstream timeout")
                else:
                    call.output({"status": "ok", "rows": list(range(random.randint(1, 50)))})
    print("sent", ticket)
