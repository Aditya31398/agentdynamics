"""Classify agent actions into process phases.

Phases model *how* an agent works a task, the way AppDynamics splits a
transaction into tiers: explore -> plan -> edit -> verify, plus delegate,
communicate and other.
"""
import re

EXPLORE_TOOLS = {
    "Read", "Grep", "Glob", "LS", "WebSearch", "WebFetch", "ToolSearch", "NotebookRead",
    "ListMcpResourcesTool", "ReadMcpResourceTool",
}
PLAN_TOOLS = {
    "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "EnterPlanMode", "ExitPlanMode",
    "AskUserQuestion", "Skill",
}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
DELEGATE_TOOLS = {"Agent", "Task", "Workflow", "SendMessage", "TaskOutput", "TaskStop"}
COMMUNICATE_TOOLS = {"Artifact", "SendUserFile", "PushNotification"}
SHELL_TOOLS = {"Bash", "PowerShell"}

VERIFY_CMD = re.compile(
    r"\b(pytest|unittest|jest|vitest|mocha|go test|cargo (test|build|check|clippy)|npm (run )?(test|build|lint)|"
    r"yarn (test|build)|pnpm (test|build)|tsc\b|mypy|ruff|eslint|flake8|pylint|make( test)?\b|mvn|gradle|dotnet (test|build)|"
    r"python[0-9.]* -m (pytest|unittest|py_compile)|node --check|py_compile|curl\b|Invoke-WebRequest|playwright)"
)
RUN_CMD = re.compile(r"\b(python[0-9.]*|node|npm|npx|deno|go run|cargo run|uvicorn|flask|docker)\b")
EXPLORE_CMD = re.compile(
    r"^\s*(cd [^&;]+(&&|;)\s*)?(ls|dir|cat|head|tail|find|grep|rg|tree|wc|pwd|echo|type|Get-ChildItem|Get-Content|"
    r"Select-String|git (status|log|diff|show|branch|remote)|du|stat|which|where)\b"
)
EDIT_CMD = re.compile(r"(\bsed -i\b|>\s*[\w./\\-]+\.\w+\s*<<|\bcat\s*>|Set-Content|Out-File|\bmv\b|\bcp\b|\brm\b|mkdir)")
ADHOC_CMD = re.compile(r"\b(python[0-9.]*\s+(-\s|-c\b|-\s*<<)|node -e\b)")
GIT_CMD =re.compile(r"\bgit (add|commit|push|pull|merge|rebase|checkout|init|clone)|\bgh\b")


def classify(tool_name, tool_input):
    """Return (phase, target) for a tool call."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    name = tool_name or ""
    target = (
        inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or inp.get("url")
        or inp.get("pattern") or inp.get("query") or inp.get("command") or inp.get("description")
        or inp.get("skill") or ""
    )
    target = str(target)[:300]
    if name in EDIT_TOOLS:
        return "edit", target
    if name in EXPLORE_TOOLS:
        return "explore", target
    if name in PLAN_TOOLS:
        return "plan", target
    if name in DELEGATE_TOOLS:
        return "delegate", target
    if name in COMMUNICATE_TOOLS or name.startswith("mcp__visualize"):
        return "communicate", target
    if name in SHELL_TOOLS:
        cmd = str(inp.get("command", ""))
        if ADHOC_CMD.search(cmd):
            return "execute", target
        if VERIFY_CMD.search(cmd):
            return "verify", target
        if GIT_CMD.search(cmd):
            return "vcs", target
        if EXPLORE_CMD.search(cmd):
            return "explore", target
        if EDIT_CMD.search(cmd):
            return "edit", target
        if RUN_CMD.search(cmd):
            return "verify", target
        return "execute", target
    if name.startswith("mcp__"):
        low = name.lower()
        if any(k in low for k in ("screenshot", "preview", "console", "read_page", "get_page_text", "network", "find", "logs")):
            return "verify", target
        if any(k in low for k in ("navigate", "computer", "click", "form_input", "javascript")):
            return "verify", target
        return "execute", target
    return "other", target


PHASES = ["explore", "plan", "edit", "verify", "execute", "vcs", "delegate", "communicate", "other"]
