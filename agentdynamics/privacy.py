"""PII / secret redaction applied to every stored text field before it reaches the database."""
import re

PATTERNS = {
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "api_key": r"\b(sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|lsv2_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{30,})",
    "bearer": r"[Bb]earer\s+[A-Za-z0-9._~+/=-]{16,}",
    "aws_key": r"\bAKIA[0-9A-Z]{16}\b",
    "credit_card": r"\b(?:\d[ -]?){13,16}\b",
}
TEXT_FIELDS = ("text", "input_preview", "error", "target")
TASK_TEXT_FIELDS = ("prompt", "final_text", "next_prompt", "root_error")   # what a user or model wrote


class Redactor:
    def __init__(self, privacy_cfg):
        self.store_content = privacy_cfg.get("store_content", True)
        pats = [PATTERNS[n] for n in privacy_cfg.get("redact", []) if n in PATTERNS]
        pats += privacy_cfg.get("extra_patterns", [])
        self.rx = re.compile("|".join(f"(?:{p})" for p in pats)) if pats else None

    def text(self, s):
        if not s or not isinstance(s, str):
            return s
        if not self.store_content:
            return f"[content not stored · {len(s)} chars]"
        return self.rx.sub("[REDACTED]", s) if self.rx else s

    def run(self, run):
        """Redact in place. Structural fields (names, timings, tokens) are kept."""
        if self.rx is None and self.store_content:
            return run
        run["title"] = self.text(run.get("title"))
        for s in run.get("steps", []):
            for f in TEXT_FIELDS:
                if s.get(f):
                    # tool targets such as file paths are metadata, keep them unless content storage is off
                    if f == "target" and self.store_content:
                        s[f] = self.rx.sub("[REDACTED]", s[f]) if self.rx else s[f]
                    else:
                        s[f] = self.text(s[f])
        return run
