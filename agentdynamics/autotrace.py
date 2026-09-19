"""One-line instrumentation.

    import agentdynamics
    agentdynamics.init()            # that's it

`init()` looks at what is installed and wires up everything it finds:
  * LangChain / LangGraph  -> redirects LangSmith tracing to AgentDynamics (unless you already trace to LangSmith)
  * OpenTelemetry SDK      -> adds an OTLP exporter to your tracer provider (OpenInference, OpenLLMetry, GenAI semconv)
  * Anthropic SDK          -> records every messages.create call (sync, async, streaming) with tokens, latency, stop reason
  * OpenAI SDK             -> records chat.completions.create and responses.create

Optional, for richer data:
    @agentdynamics.trace                  # group everything a function does into one task / workflow
    @agentdynamics.tool                   # record a function as a tool call
    with agentdynamics.span("retrieve"):  # mark a graph node / stage

Telemetry is sent from a background thread; your code never waits on it and never fails because of it.
Settings come from arguments or environment variables:
    AGENTDYNAMICS_URL (default http://127.0.0.1:8787), AGENTDYNAMICS_API_KEY, AGENTDYNAMICS_PROJECT,
    AGENTDYNAMICS_ENVIRONMENT, AGENTDYNAMICS_CAPTURE_CONTENT (1/0), AGENTDYNAMICS_DISABLED (1)
"""
import atexit
import contextvars
import functools
import inspect
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

_cfg = {"url": None, "key": None, "project": None, "environment": None, "content": True, "on": False}
_current = contextvars.ContextVar("agentdynamics_run", default=None)
_node = contextvars.ContextVar("agentdynamics_node", default=None)
_q = queue.Queue(maxsize=10000)
_sender = None
_patched = set()
_warned = set()

# Extension points used by integrations (e.g. agentdynamics.integrations.aegis):
#   llm_gates: objects with before(provider, model, kwargs) -> handle   (may raise to block the call)
#                           and after(handle, usd, tokens, error)        (settle what before() reserved)
#   step(run, step)          observe every recorded step (watchdogs)
#   run_meta(run) -> dict    extra fields for the run payload
_hooks = {"llm_gates": [], "step": [], "run_meta": []}


def current_run():
    """The task being traced in this context, or None."""
    return _current.get()


def current_node():
    return _node.get()


def _warn(key, msg):
    if key not in _warned:
        _warned.add(key)
        print(f"[agentdynamics] {msg}", file=sys.stderr)


def _clip(v, n=600):
    if not _cfg["content"]:
        return ""
    if v is None:
        return ""
    if not isinstance(v, str):
        try:
            v = json.dumps(v, default=str)
        except (TypeError, ValueError):
            v = str(v)
    return v[:n]


# ---------------------------------------------------------------- transport

def _send_loop():
    while True:
        batch = [_q.get()]
        try:
            while len(batch) < 50:
                batch.append(_q.get_nowait())
        except queue.Empty:
            pass
        items = [b for b in batch if b is not None]
        if items:
            _post(items)
        for _ in batch:
            _q.task_done()


def _post(items):
    headers = {"Content-Type": "application/json"}
    if _cfg["key"]:
        headers["Authorization"] = f"Bearer {_cfg['key']}"
    data = json.dumps(items, default=str).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(_cfg["url"] + "/api/ingest", data=data, headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=10).read()
            return
        except urllib.error.HTTPError as ex:
            if ex.code in (401, 403):
                _warn("auth", f"server rejected telemetry ({ex.code}); check AGENTDYNAMICS_API_KEY has the 'ingest' role")
                return
        except OSError:
            pass
        time.sleep(0.5 * (attempt + 1))
    _warn("down", f"could not reach {_cfg['url']}; telemetry is being dropped (your app is unaffected)")


def _emit(payload):
    if not _cfg["on"]:
        return
    try:
        _q.put_nowait(payload)
    except queue.Full:
        _warn("full", "telemetry queue full; dropping runs")


def flush(timeout=5.0):
    """Block until queued telemetry is sent (called automatically at exit)."""
    if not _cfg["on"]:
        return
    end = time.time() + timeout
    while _q.unfinished_tasks and time.time() < end:
        time.sleep(0.05)


# ---------------------------------------------------------------- runs, spans, tools

class _Run:
    def __init__(self, name, prompt=None, thread_id=None, user_id=None, metadata=None):
        self.id = f"{_cfg['project'] or 'app'}-{uuid.uuid4().hex[:16]}"
        self.name = name
        self.steps = [{"kind": "prompt", "ts": time.time(), "text": _clip(prompt if prompt is not None else name, 2000) or name}]
        self.error = None
        self.thread_id = thread_id
        self.user_id = user_id
        self.metadata = metadata or {}
        self.feedback = []
        self._lock = threading.Lock()

    def add(self, step):
        with self._lock:
            node = _node.get()
            if node and "node" not in step:
                step["node"] = node
            self.steps.append(step)
        for hook in list(_hooks["step"]):
            try:
                hook(self, step)
            except Exception as ex:  # observers never break the agent
                _warn(f"step-hook:{id(hook)}", f"step hook failed: {ex!r}")

    def payload(self):
        out = {"id": self.id, "agent": self.name, "workflow": self.name, "project": _cfg["project"] or "default",
               "environment": _cfg["environment"], "source": "sdk", "framework": "agentdynamics-sdk",
               "thread_id": self.thread_id, "user_id": self.user_id, "status": "error" if self.error else "ok",
               "error": self.error, "complete": True, "feedback": self.feedback, "steps": self.steps}
        for hook in list(_hooks["run_meta"]):
            try:
                out.update(hook(self) or {})
            except Exception as ex:
                _warn(f"meta-hook:{id(hook)}", f"run metadata hook failed: {ex!r}")
        return out


class trace:
    """Group work into one task. Use as @trace, @trace("name"), or `with trace("name", prompt=...)`."""

    def __init__(self, name=None, prompt=None, thread_id=None, user_id=None, **metadata):
        self._fn = None
        if callable(name):
            self._fn, name = name, name.__name__
            functools.update_wrapper(self, self._fn)
        self.name, self.prompt, self.thread_id, self.user_id, self.metadata = name, prompt, thread_id, user_id, metadata
        self.run = None

    # context manager
    def __enter__(self):
        parent = _current.get()
        if parent is not None:  # nested trace -> a node inside the parent workflow
            self._span = span(self.name or "step")
            self._span.__enter__()
            self.run = parent
            return self
        self.run = _Run(self.name or "task", self.prompt, self.thread_id, self.user_id, self.metadata)
        self._tok = _current.set(self.run)
        return self

    def __exit__(self, et, ev, tb):
        if hasattr(self, "_span"):
            self._span.__exit__(et, ev, tb)
            del self._span
            return False
        if ev is not None:
            self.run.error = f"{et.__name__}: {ev}"[:400]
        _current.reset(self._tok)
        _emit(self.run.payload())
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, et, ev, tb):
        return self.__exit__(et, ev, tb)

    def feedback(self, key, score):
        """Attach a user/eval score (0..1) to this task."""
        self.run.feedback.append({"key": key, "score": score})

    # decorator
    def __call__(self, *args, **kwargs):
        if self._fn is None:  # used as @trace("name")
            fn = args[0]
            return trace(self.name or fn.__name__, self.prompt, self.thread_id, self.user_id, **self.metadata).__wrap(fn)
        return self.__wrap(self._fn)(*args, **kwargs)

    def __wrap(self, fn):
        name = self.name or fn.__name__

        def prompt_of(args, kwargs):
            for v in list(args) + list(kwargs.values()):
                if isinstance(v, str) and v.strip():
                    return v
            return None

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def aw(*a, **k):
                async with trace(name, self.prompt or prompt_of(a, k), self.thread_id, self.user_id, **self.metadata):
                    return await fn(*a, **k)
            return aw

        @functools.wraps(fn)
        def w(*a, **k):
            with trace(name, self.prompt or prompt_of(a, k), self.thread_id, self.user_id, **self.metadata):
                return fn(*a, **k)
        return w


class span:
    """Mark a stage / graph node. LLM and tool calls inside it are attributed to the node."""

    def __init__(self, name, kind="node"):
        self.name, self.kind = name, kind

    def __enter__(self):
        self.t0 = time.time()
        self.tok = _node.set(self.name)
        return self

    def __exit__(self, et, ev, tb):
        _node.reset(self.tok)
        run = _current.get()
        if run is not None:
            run.add({"kind": "span", "name": self.name, "node": self.name, "span_kind": self.kind, "ts": self.t0,
                     "start_ts": self.t0, "end_ts": time.time(), "is_error": ev is not None,
                     "error": f"{et.__name__}: {ev}"[:300] if ev else None})
            # keep execution order by start time
            run.steps.sort(key=lambda s: (s.get("kind") != "prompt", s.get("ts") or 0))
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, et, ev, tb):
        return self.__exit__(et, ev, tb)


def tool(fn=None, *, name=None):
    """Record a function as a tool call (@tool or @tool(name="search"))."""
    def deco(f):
        tname = name or f.__name__

        def record(t0, args, kwargs, result=None, err=None):
            run = _current.get()
            if run is None:
                return
            inp = {**{f"arg{i}": a for i, a in enumerate(args)}, **kwargs}
            run.add({"kind": "tool", "name": tname, "ts": t0, "end_ts": time.time(), "input": inp if _cfg["content"] else {},
                     "is_error": err is not None, "error": f"{type(err).__name__}: {err}"[:300] if err else None,
                     "output_chars": len(_clip(result, 10**7)) if result is not None else 0, "text": _clip(result, 300)})

        if inspect.iscoroutinefunction(f):
            @functools.wraps(f)
            async def aw(*a, **k):
                t0 = time.time()
                try:
                    r = await f(*a, **k)
                except Exception as ex:
                    record(t0, a, k, err=ex)
                    raise
                record(t0, a, k, r)
                return r
            return aw

        @functools.wraps(f)
        def w(*a, **k):
            t0 = time.time()
            try:
                r = f(*a, **k)
            except Exception as ex:
                record(t0, a, k, err=ex)
                raise
            record(t0, a, k, r)
            return r
        return w
    return deco(fn) if fn else deco


def _llm_begin(provider, model, kwargs):
    """Ask every gate before the request is sent. A gate may raise to block it; gates that already
    reserved something are released so nothing leaks."""
    done = []
    for gate in list(_hooks["llm_gates"]):
        try:
            done.append((gate, gate.before(provider, model, kwargs)))
        except Exception as ex:
            for g, h in done:
                g.after(h, 0.0, 0, ex)
            raise
    return done


def _is_denial(err):
    v = getattr(err, "verdict", None)
    return v is not None and getattr(v, "allowed", True) is False


def _record_llm(model, it, ot, cr, cw, stop, t0, t1, text="", err=None, ttft=None, provider=None, handles=()):
    from .pricing import cost as _price
    usd = _price(model, it or 0, ot or 0, cr or 0, cw or 0, 0)
    step = {"kind": "llm", "model": model, "ts": t0, "end_ts": t1, "input_tokens": it or 0, "output_tokens": ot or 0,
            "cache_read": cr or 0, "cache_write": cw or 0, "stop_reason": stop, "text": _clip(text), "provider": provider,
            "cost": usd}
    if ttft is not None:
        step["ttft_ms"] = ttft
    if err is not None and _is_denial(err):  # blocked before it was sent (budget / revoked grant)
        step["denied"] = True
        step["rule"] = err.verdict.rule
        step["guard"] = getattr(err.verdict, "guard", None)
        step["error"] = str(err)[:300]
    elif err is not None:
        step["is_error"] = True
        step["error"] = f"{type(err).__name__}: {err}"[:300]
        step["rate_limited"] = getattr(err, "status_code", None) in (429, 529) or "rate" in str(err).lower()
    tokens = (it or 0) + (ot or 0) + (cr or 0) + (cw or 0)
    for gate, handle in handles:
        try:
            gate.after(handle, usd, tokens, err)
        except Exception as ex:
            _warn(f"gate:{id(gate)}", f"model gate settle failed: {ex!r}")
    run = _current.get()
    if run is not None:
        run.add(step)
    else:  # a model call outside any @trace becomes its own small task
        r = _Run(f"{provider or 'llm'}.call")
        r.add(step)
        if err is not None:
            r.error = step["error"]
        _emit(r.payload())


class llm_call:
    """Record (and gate) a call to any model client the SDK doesn't patch: local models, other SDKs, raw HTTP.

        with agentdynamics.llm_call("my-model", max_tokens=512, input=messages) as call:
            resp = my_client.generate(...)
            call.usage(input_tokens=resp.in_tok, output_tokens=resp.out_tok, stop_reason=resp.stop)

    Integrations such as Aegis budget gating run before the body, so an exhausted budget stops the call.
    """

    def __init__(self, model, provider="custom", max_tokens=None, input=None):
        self.model, self.provider = model, provider
        self.kwargs = {"model": model, "max_tokens": max_tokens, "messages": input}
        self._u = {"it": 0, "ot": 0, "cr": 0, "cw": 0, "stop": None, "text": ""}

    def usage(self, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0, stop_reason=None, text=""):
        self._u.update(it=input_tokens, ot=output_tokens, cr=cache_read, cw=cache_write, stop=stop_reason, text=text)

    def __enter__(self):
        self.t0 = time.time()
        try:
            self.h = _llm_begin(self.provider, self.model, self.kwargs)
        except Exception as ex:
            _record_llm(self.model, 0, 0, 0, 0, None, self.t0, time.time(), err=ex, provider=self.provider)
            raise
        return self

    def __exit__(self, et, ev, tb):
        u = self._u
        _record_llm(self.model, u["it"], u["ot"], u["cr"], u["cw"], u["stop"], self.t0, time.time(), u["text"], ev,
                    provider=self.provider, handles=self.h)
        return False


def record_llm(model, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0, stop_reason=None,
               start=None, end=None, text="", provider="custom"):
    """Record a model call after the fact (no gating). Prefer `llm_call` when you can wrap the call."""
    end = end or time.time()
    _record_llm(model, input_tokens, output_tokens, cache_read, cache_write, stop_reason, start or end, end, text,
                provider=provider)


# ---------------------------------------------------------------- Anthropic

def _anthropic_usage(msg):
    u = getattr(msg, "usage", None)
    g = (lambda k: getattr(u, k, 0) or 0) if u is not None else (lambda k: 0)
    text = "".join(getattr(b, "text", "") for b in getattr(msg, "content", None) or [] if getattr(b, "type", "") == "text")
    return g("input_tokens"), g("output_tokens"), g("cache_read_input_tokens"), g("cache_creation_input_tokens"), getattr(msg, "stop_reason", None), text


class _AnthropicStream:
    """Wraps a stream=True iterator, accumulating usage from message_start / message_delta events."""

    def __init__(self, inner, model, t0, handles=()):
        self._inner, self._model, self._t0, self._handles = inner, model, t0, handles
        self._u = {"i": 0, "o": 0, "cr": 0, "cw": 0}
        self._stop, self._ttft, self._text, self._done = None, None, [], False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            ev = next(self._inner)
        except StopIteration:
            self._finish()
            raise
        except Exception as ex:
            self._finish(ex)
            raise
        self._see(ev)
        return ev

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            ev = await self._inner.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except Exception as ex:
            self._finish(ex)
            raise
        self._see(ev)
        return ev

    def _see(self, ev):
        t = getattr(ev, "type", "")
        if t == "message_start":
            m = ev.message
            self._model = getattr(m, "model", self._model)
            u = m.usage
            self._u.update(i=u.input_tokens or 0, cr=getattr(u, "cache_read_input_tokens", 0) or 0,
                           cw=getattr(u, "cache_creation_input_tokens", 0) or 0, o=u.output_tokens or 0)
        elif t == "content_block_delta":
            if self._ttft is None:
                self._ttft = (time.time() - self._t0) * 1000
            d = getattr(ev, "delta", None)
            if getattr(d, "type", "") == "text_delta" and sum(map(len, self._text)) < 600:
                self._text.append(d.text)
        elif t == "message_delta":
            self._stop = getattr(ev.delta, "stop_reason", None) or self._stop
            if getattr(ev, "usage", None) is not None:
                self._u["o"] = ev.usage.output_tokens or self._u["o"]

    def _finish(self, err=None):
        if not self._done:
            self._done = True
            _record_llm(self._model, self._u["i"], self._u["o"], self._u["cr"], self._u["cw"], self._stop, self._t0, time.time(),
                        "".join(self._text), err, self._ttft, "anthropic", self._handles)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._finish()
        return getattr(self._inner, "__exit__", lambda *x: False)(*a)

    def __getattr__(self, k):
        return getattr(self._inner, k)


def _patch_anthropic():
    try:
        from anthropic.resources.messages import AsyncMessages, Messages
    except ImportError:
        return False
    if "anthropic" in _patched:
        return True
    orig, aorig = Messages.create, AsyncMessages.create

    @functools.wraps(orig)
    def create(self, *a, **k):
        t0 = time.time()
        h = ()
        try:
            h = _llm_begin("anthropic", k.get("model"), k)
            r = orig(self, *a, **k)
        except Exception as ex:
            _record_llm(k.get("model"), 0, 0, 0, 0, None, t0, time.time(), err=ex, provider="anthropic", handles=h)
            raise
        if k.get("stream"):
            return _AnthropicStream(r, k.get("model"), t0, h)
        it, ot, cr, cw, stop, text = _anthropic_usage(r)
        _record_llm(getattr(r, "model", k.get("model")), it, ot, cr, cw, stop, t0, time.time(), text, provider="anthropic", handles=h)
        return r

    @functools.wraps(aorig)
    async def acreate(self, *a, **k):
        t0 = time.time()
        h = ()
        try:
            h = _llm_begin("anthropic", k.get("model"), k)
            r = await aorig(self, *a, **k)
        except Exception as ex:
            _record_llm(k.get("model"), 0, 0, 0, 0, None, t0, time.time(), err=ex, provider="anthropic", handles=h)
            raise
        if k.get("stream"):
            return _AnthropicStream(r, k.get("model"), t0, h)
        it, ot, cr, cw, stop, text = _anthropic_usage(r)
        _record_llm(getattr(r, "model", k.get("model")), it, ot, cr, cw, stop, t0, time.time(), text, provider="anthropic", handles=h)
        return r

    Messages.create, AsyncMessages.create = create, acreate
    _patched.add("anthropic")
    return True


# ---------------------------------------------------------------- OpenAI

def _openai_record(r, k, t0, err=None, handles=()):
    if err is not None:
        _record_llm(k.get("model"), 0, 0, 0, 0, None, t0, time.time(), err=err, provider="openai", handles=handles)
        return
    u = getattr(r, "usage", None)
    it = getattr(u, "prompt_tokens", None) or getattr(u, "input_tokens", 0) or 0
    ot = getattr(u, "completion_tokens", None) or getattr(u, "output_tokens", 0) or 0
    det = getattr(u, "prompt_tokens_details", None) or getattr(u, "input_tokens_details", None)
    cr = getattr(det, "cached_tokens", 0) or 0
    stop, text = None, ""
    if getattr(r, "choices", None):
        stop = r.choices[0].finish_reason
        text = getattr(r.choices[0].message, "content", "") or ""
    elif hasattr(r, "output_text"):
        stop = getattr(r, "status", None)
        text = r.output_text or ""
    _record_llm(getattr(r, "model", k.get("model")), max(0, it - cr), ot, cr, 0, stop, t0, time.time(), text, provider="openai",
                handles=handles)


def _patch_openai():
    try:
        from openai.resources.chat.completions import AsyncCompletions, Completions
        from openai.resources.responses import AsyncResponses, Responses
    except ImportError:
        return False
    if "openai" in _patched:
        return True

    def wrap(cls):
        orig = cls.create

        if inspect.iscoroutinefunction(orig):
            @functools.wraps(orig)
            async def acreate(self, *a, **k):
                t0 = time.time()
                h = ()
                try:
                    h = _llm_begin("openai", k.get("model"), k)
                    r = await orig(self, *a, **k)
                except Exception as ex:
                    _openai_record(None, k, t0, ex, h)
                    raise
                if not k.get("stream"):
                    _openai_record(r, k, t0, handles=h)
                return r
            cls.create = acreate
        else:
            @functools.wraps(orig)
            def create(self, *a, **k):
                t0 = time.time()
                h = ()
                try:
                    h = _llm_begin("openai", k.get("model"), k)
                    r = orig(self, *a, **k)
                except Exception as ex:
                    _openai_record(None, k, t0, ex, h)
                    raise
                if not k.get("stream"):
                    _openai_record(r, k, t0, handles=h)
                return r
            cls.create = create

    for c in (Completions, AsyncCompletions, Responses, AsyncResponses):
        wrap(c)
    _patched.add("openai")
    return True


# ---------------------------------------------------------------- LangChain / LangGraph, OpenTelemetry

def _setup_langchain():
    import importlib.util
    if not (importlib.util.find_spec("langsmith") or importlib.util.find_spec("langchain_core")):
        return False
    existing = os.environ.get("LANGSMITH_ENDPOINT") or os.environ.get("LANGCHAIN_ENDPOINT")
    tracing_on = (os.environ.get("LANGSMITH_TRACING") or os.environ.get("LANGCHAIN_TRACING_V2") or "").lower() == "true"
    if tracing_on and existing and not existing.startswith(_cfg["url"]):
        _warn("ls", "LangSmith tracing already configured; leaving it alone. Add a 'langsmith_api' source to pull those runs.")
        return "kept-langsmith"
    ep = _cfg["url"] + "/langsmith"
    for ns in ("LANGSMITH", "LANGCHAIN"):
        os.environ[f"{ns}_ENDPOINT"] = ep
        os.environ[f"{ns}_API_KEY"] = _cfg["key"] or "agentdynamics-local"
        if _cfg["project"]:
            os.environ[f"{ns}_PROJECT"] = _cfg["project"]
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    # clients created before init() captured the old settings
    for mod, attr in (("langchain_core.tracers.langchain", "_CLIENT"),):
        m = sys.modules.get(mod)
        if m is not None and hasattr(m, attr):
            setattr(m, attr, None)
    lsu = sys.modules.get("langsmith.utils")
    for fn in ("get_env_var", "get_tracer_project"):
        f = getattr(lsu, fn, None) if lsu else None
        if hasattr(f, "cache_clear"):
            f.cache_clear()
    return True


def _setup_otel():
    try:
        from opentelemetry import trace as ot
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return False
    headers = {"Authorization": f"Bearer {_cfg['key']}"} if _cfg["key"] else {}
    exporter = OTLPSpanExporter(endpoint=_cfg["url"] + "/v1/traces", headers=headers)
    provider = ot.get_tracer_provider()
    if not hasattr(provider, "add_span_processor"):
        attrs = {"service.name": _cfg["project"] or "agent"}
        if _cfg["environment"]:
            attrs["deployment.environment.name"] = _cfg["environment"]
        provider = TracerProvider(resource=Resource.create(attrs))
        ot.set_tracer_provider(provider)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return True


# ---------------------------------------------------------------- entry point

def init(url=None, api_key=None, project=None, environment=None, capture_content=None,
         langchain=True, otel=True, anthropic=True, openai=True, quiet=False):
    """Connect this process to AgentDynamics. Safe to call more than once."""
    if os.environ.get("AGENTDYNAMICS_DISABLED") in ("1", "true"):
        return {}
    env = os.environ.get
    _cfg["url"] = (url or env("AGENTDYNAMICS_URL") or "http://127.0.0.1:8787").rstrip("/")
    _cfg["key"] = api_key or env("AGENTDYNAMICS_API_KEY")
    _cfg["project"] = project or env("AGENTDYNAMICS_PROJECT") or os.path.basename(os.getcwd())
    _cfg["environment"] = environment or env("AGENTDYNAMICS_ENVIRONMENT") or "development"
    cc = capture_content if capture_content is not None else env("AGENTDYNAMICS_CAPTURE_CONTENT", "1") not in ("0", "false")
    _cfg["content"] = cc
    _cfg["on"] = True
    global _sender
    if _sender is None:
        _sender = threading.Thread(target=_send_loop, daemon=True, name="agentdynamics-sender")
        _sender.start()
        atexit.register(flush)
    done = {
        "langchain": _setup_langchain() if langchain else False,
        "opentelemetry": _setup_otel() if otel else False,
        "anthropic": _patch_anthropic() if anthropic else False,
        "openai": _patch_openai() if openai else False,
    }
    if not quiet:
        on = [k for k, v in done.items() if v]
        print(f"[agentdynamics] sending to {_cfg['url']} (project '{_cfg['project']}') · instrumented: {', '.join(on) or 'nothing detected'}",
              file=sys.stderr)
    return done
