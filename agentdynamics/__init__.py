"""AgentDynamics: application performance monitoring for AI agents.

    import agentdynamics
    agentdynamics.init()      # auto-instruments LangChain/LangGraph, OpenTelemetry, Anthropic and OpenAI

See agentdynamics.autotrace for @trace / @tool / span.
"""
__version__ = "0.6.0"

from .autotrace import flush, init, llm_call, outcome, record_llm, span, tool, trace  # noqa: E402,F401

__all__ = ["init", "trace", "tool", "span", "llm_call", "record_llm", "outcome", "flush", "__version__"]
