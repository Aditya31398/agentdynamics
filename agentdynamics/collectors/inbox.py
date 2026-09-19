"""File inbox / log tailing: the bridge for log pipelines (Fluent Bit, Vector, Logstash, OTel Collector
file exporter, S3/GCS sync jobs...). Watches a directory of *.jsonl / *.json files, tails growing files
by byte offset, and auto-detects each record's format:

  - OTLP JSON export          {"resourceSpans": [...]}            (OTel Collector `file` exporter)
  - LangSmith run             {"id", "run_type", "trace_id", ...} (LangSmith bulk export / run dumps)
  - Langfuse trace            {"id", "observations": [...]}
  - AgentDynamics generic run {"steps": [...]}
  - Canonical span            {"trace_id", "span_id", "kind", ...}
"""
import json
import os


def detect(rec):
    if not isinstance(rec, dict):
        return None
    if "resourceSpans" in rec:
        return "otlp"
    if "run_type" in rec and ("trace_id" in rec or "id" in rec):
        return "langsmith"
    if "observations" in rec and "id" in rec:
        return "langfuse"
    if "steps" in rec:
        return "generic"
    if "trace_id" in rec and "span_id" in rec:
        return "span"
    return None


def unwrap(rec):
    """Log shippers wrap payloads: {"log": "<json>"}, {"message": {...}}, {"body": ...}."""
    for key in ("log", "message", "body", "record"):
        if detect(rec) is None and isinstance(rec, dict) and key in rec:
            inner = rec[key]
            if isinstance(inner, str):
                try:
                    inner = json.loads(inner)
                except ValueError:
                    continue
            rec = inner
    return rec


class Inbox:
    def __init__(self, path, state):
        self.path = path
        self.state = state  # {"offsets": {file: bytes}}
        os.makedirs(path, exist_ok=True)

    def poll(self, max_bytes=64 * 1024 * 1024):
        """Return list of (format, record) read since last poll."""
        offs = self.state.setdefault("offsets", {})
        out = []
        for fn in sorted(os.listdir(self.path)):
            p = os.path.join(self.path, fn)
            if not os.path.isfile(p) or not fn.endswith((".jsonl", ".json", ".ndjson", ".log")):
                continue
            size = os.path.getsize(p)
            start = offs.get(p, 0)
            if size < start:  # rotated / truncated
                start = 0
            if size == start:
                continue
            with open(p, "rb") as f:
                f.seek(start)
                chunk = f.read(max_bytes)
            if fn.endswith(".json") and start == 0:
                try:
                    doc = json.loads(chunk)
                    recs = doc if isinstance(doc, list) else [doc]
                    out.extend((detect(r), r) for r in recs if detect(r))
                    offs[p] = size
                    continue
                except ValueError:
                    pass
            last_nl = chunk.rfind(b"\n")
            if last_nl < 0:
                continue  # wait for a complete line
            for line in chunk[:last_nl].splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                rec = unwrap(rec)
                fmt = detect(rec)
                if fmt:
                    out.append((fmt, rec))
            offs[p] = start + last_nl + 1
        return out
