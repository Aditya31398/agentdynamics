"""AgentDynamics: application performance monitoring for AI agents.

    import agentdynamics
    agentdynamics.init()      # auto-instruments LangChain/LangGraph, OpenTelemetry, Anthropic and OpenAI

See agentdynamics.autotrace for @trace / @tool / span.
"""
__version__ = "0.3.0"

from .autotrace import flush, init, span, tool, trace  # noqa: E402,F401

__all__ = ["init", "trace", "tool", "span", "flush", "__version__"]
