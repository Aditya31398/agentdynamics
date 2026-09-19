"""A LangGraph-style support agent traced with the *real* LangSmith SDK, pointed at AgentDynamics.

    python -m agentdynamics serve                 # in another terminal
    python examples/langgraph_style_app.py 40     # send 40 conversations

No code changes are needed in an existing LangChain/LangGraph app. These three variables are enough:
    LANGSMITH_TRACING=true
    LANGSMITH_ENDPOINT=http://127.0.0.1:8787/langsmith
    LANGSMITH_API_KEY=<AgentDynamics ingest key, any value when auth is off>

The graph is simulated with LangChain runnables carrying LangGraph's metadata (langgraph_node,
langgraph_step, thread_id), which is exactly what LangGraph itself emits, so it runs without
installing langgraph.
"""
import os
import random
import sys
import uuid

os.environ.setdefault("LANGSMITH_TRACING", "true")
os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
os.environ.setdefault("LANGSMITH_ENDPOINT", "http://127.0.0.1:8787/langsmith")
os.environ.setdefault("LANGCHAIN_ENDPOINT", os.environ["LANGSMITH_ENDPOINT"])
os.environ.setdefault("LANGSMITH_API_KEY", "local-dev")
os.environ.setdefault("LANGCHAIN_API_KEY", os.environ["LANGSMITH_API_KEY"])
os.environ.setdefault("LANGSMITH_PROJECT", "support-bot-prod")
os.environ.setdefault("LANGCHAIN_PROJECT", os.environ["LANGSMITH_PROJECT"])

from langchain_core.callbacks import CallbackManagerForRetrieverRun  # noqa: E402
from langchain_core.documents import Document  # noqa: E402
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.retrievers import BaseRetriever  # noqa: E402
from langchain_core.runnables import RunnableConfig, RunnableLambda  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

QUESTIONS = ["Where is my order #{n}?", "I was charged twice for order #{n}", "How do I reset my password?",
             "Cancel my subscription", "Refund order #{n} please", "Change my shipping address for #{n}"]


class KB(BaseRetriever):
    miss_rate: float = 0.15

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun):
        if random.random() < self.miss_rate:
            return []
        return [Document(page_content=f"Policy snippet {i} for: {query[:30]}") for i in range(random.randint(1, 4))]


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order in the order system."""
    if random.random() < 0.08:
        raise TimeoutError("orders-api timed out after 10s")
    return f"order {order_id}: shipped"


@tool
def issue_refund(order_id: str, amount: float) -> str:
    """Refund an order."""
    if random.random() < 0.05:
        raise PermissionError("refund exceeds agent limit, needs human approval")
    return f"refunded {amount} on {order_id}"


def llm(model, text, stop="end_turn", in_tok=None, out_tok=None, cache=0):
    msg = AIMessage(content=text, usage_metadata={
        "input_tokens": in_tok or random.randint(800, 4000), "output_tokens": out_tok or random.randint(60, 500),
        "total_tokens": 0, "input_token_details": {"cache_read": cache}},
        response_metadata={"stop_reason": stop, "model": model})
    return GenericFakeChatModel(messages=iter([msg])).with_config(metadata={"ls_model_name": model, "ls_provider": "anthropic"})


def node(name, fn, step, thread):
    return RunnableLambda(fn).with_config(run_name=name, metadata={"langgraph_node": name, "langgraph_step": step,
                                                                   "thread_id": thread, "environment": "production"})


def conversation(question, thread, flaky=False, run_id=None):
    retriever = KB()
    state = {"q": question, "steps": 0}

    def router(s, config: RunnableConfig):
        return llm("claude-haiku-4-5", "intent: support", out_tok=12).invoke(s["q"], config=config)

    def retrieve(s, config: RunnableConfig):
        return retriever.invoke(s["q"], config=config)

    def agent(s, config: RunnableConfig):
        stop = "max_tokens" if random.random() < 0.04 else "tool_use"
        return llm("claude-sonnet-5", "calling tool", stop=stop, cache=random.choice([0, 2000, 3000])).invoke(s["q"], config=config)

    def tools(s, config: RunnableConfig):
        oid = str(random.randint(1000, 9999))
        if "Refund" in s["q"] or "charged" in s["q"]:
            return issue_refund.invoke({"order_id": oid, "amount": 20.0}, config=config)
        return lookup_order.invoke({"order_id": oid}, config=config)

    def respond(s, config: RunnableConfig):
        return llm("claude-sonnet-5", "Here is what I found...", stop="end_turn").invoke(s["q"], config=config)

    def graph(s, config: RunnableConfig):
        step = 1
        node("router", router, step, thread).invoke(s, config=config)
        step += 1
        node("retrieve", retrieve, step, thread).invoke(s, config=config)
        loops = random.choice([1, 1, 1, 2, 2, 3]) + (5 if flaky else 0)
        for _ in range(loops):
            step += 1
            node("agent", agent, step, thread).invoke(s, config=config)
            step += 1
            node("tools", tools, step, thread).invoke(s, config=config)
        step += 1
        return node("respond", respond, step, thread).invoke(s, config=config)

    cfg = {"run_id": run_id} if run_id else {}
    return RunnableLambda(graph).with_config(run_name="support_graph", metadata={"thread_id": thread}).invoke(state, config=cfg)


def run_demo(n=30, seed=7):
    random.seed(seed)
    from langsmith import Client
    client = Client()
    ids = []
    for i in range(n):
        thread = str(uuid.uuid4())
        q = random.choice(QUESTIONS).format(n=random.randint(100, 999))
        run_id = uuid.uuid4()
        try:
            conversation(q, thread, flaky=(i % 9 == 4), run_id=run_id)
        except Exception as ex:  # failed runs are part of the demo
            print("run failed:", type(ex).__name__, ex)
        ids.append(run_id)
        if i % 5 == 2:  # a follow-up turn in the same thread that corrects the agent
            try:
                conversation("That's not what I asked, still not working", thread)
            except Exception:
                pass
        if i % 4 == 0:
            try:
                client.create_feedback(run_id, key="user_rating", score=random.choice([0.0, 1.0, 1.0]))
            except Exception as ex:
                print("feedback failed:", ex)
    from langchain_core.tracers.langchain import wait_for_all_tracers
    wait_for_all_tracers()
    return ids


if __name__ == "__main__":
    run_demo(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
    print("done")
