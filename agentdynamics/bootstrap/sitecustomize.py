"""Loaded automatically by `agentdynamics run <command>` (placed first on PYTHONPATH).

Instruments the child Python process before any user code runs, like `ddtrace-run` or `opentelemetry-instrument`.
"""
import os
import sys

try:
    import agentdynamics

    agentdynamics.init(quiet=os.environ.get("AGENTDYNAMICS_QUIET") == "1")
except Exception as ex:  # never break the user's program
    print(f"[agentdynamics] auto-instrumentation disabled: {ex}", file=sys.stderr)

# Chain to any sitecustomize that was shadowed by ours.
_here = os.path.dirname(os.path.abspath(__file__))
for _p in sys.path:
    if _p and os.path.abspath(_p) != _here and os.path.isfile(os.path.join(_p, "sitecustomize.py")):
        import importlib.util

        _spec = importlib.util.spec_from_file_location("_user_sitecustomize", os.path.join(_p, "sitecustomize.py"))
        _mod = importlib.util.module_from_spec(_spec)
        try:
            _spec.loader.exec_module(_mod)
        except Exception as ex:
            print(f"[agentdynamics] user sitecustomize failed: {ex}", file=sys.stderr)
        break
