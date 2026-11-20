"""Deterministic stand-in for LLMClient (same chat_json contract)."""
from src.llm import LLMError, record_usage
from src.schemas import InvestigatorOutput, MessageAssessment, SanitizerVerdict


class FakeLLM:
    def __init__(self, sanitizer=None, assessor=None, writer=None, fail: set[str] = frozenset()):
        self.sanitizer = sanitizer or SanitizerVerdict(is_injection=False, reason="looks like a normal message")
        self.assessor = assessor or MessageAssessment(scam_risk="LOW", confidence=0.9, summary="(fake) reads fine")
        self.writer = writer  # InvestigatorOutput | callable(user)->InvestigatorOutput | None
        self.fail = set(fail)
        self.calls: list[str] = []
        self.seen: dict[str, list[str]] = {}
        self.usage_log: list[dict] = []

    def chat_json(self, system, user, model_cls, *, purpose=""):
        self.calls.append(purpose)
        self.seen.setdefault(purpose, []).append(user)
        entry = dict(purpose=purpose, model="fake", latency_s=0.0, prompt_tokens=len(user) // 4, completion_tokens=50)
        self.usage_log.append(entry)
        record_usage(entry)
        if purpose in self.fail:
            raise LLMError(f"fake {purpose} failure")
        if model_cls is SanitizerVerdict:
            return self.sanitizer
        if model_cls is MessageAssessment:
            return self.assessor
        if model_cls is InvestigatorOutput:
            w = self.writer or InvestigatorOutput(findings=[], student_explanation="(fake) we paused it",
                                                  staff_summary="(fake)", suggested_route="HOLD_FOR_CONFIRMATION")
            return w(user) if callable(w) else w
        raise AssertionError(model_cls)
