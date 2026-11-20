"""Automatic backup-AI chain (src/llm.FallbackLLM) and its effect on the decision log."""
import time

import pytest

from src import config, seed_data, services, workflow
from src.llm import FallbackLLM, LLMError
from src.schemas import MessageAssessment, Route
from tests.fakes import FakeLLM


class Named(FakeLLM):
    def __init__(self, model, **kw):
        super().__init__(**kw)
        self.model = model
        self._blocked_until = 0.0


def test_primary_answers_when_healthy():
    a, b = Named("primary"), Named("backup")
    FallbackLLM([a, b]).chat_json("s", "u", MessageAssessment, purpose="assessor")
    assert a.calls == ["assessor"] and b.calls == []


def test_backup_takes_over_on_failure():
    a, b = Named("primary", fail={"assessor"}), Named("backup")
    out = FallbackLLM([a, b]).chat_json("s", "u", MessageAssessment, purpose="assessor")
    assert isinstance(out, MessageAssessment) and a.calls == ["assessor"] and b.calls == ["assessor"]


def test_rate_limited_provider_is_skipped_without_calling_it():
    a, b = Named("primary"), Named("backup")
    a._blocked_until = time.time() + 600       # "try again in 10 minutes"
    FallbackLLM([a, b]).chat_json("s", "u", MessageAssessment, purpose="assessor")
    assert a.calls == [] and b.calls == ["assessor"]


def test_all_down_raises_so_rules_take_over():
    chain = FallbackLLM([Named("a", fail={"assessor"}), Named("b", fail={"assessor"})])
    with pytest.raises(LLMError, match="all AI providers unavailable"):
        chain.chat_json("s", "u", MessageAssessment, purpose="assessor")


def test_config_reads_backups():
    env = {"LLM_FALLBACK_1_MODEL": "other-groq-model", "LLM_FALLBACK_2_MODEL": "gemini-flash",
           "LLM_FALLBACK_2_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai/",
           "LLM_FALLBACK_2_API_KEY": "g-key"}
    fb1, fb2 = config.read_fallbacks(env)       # pure function: never reloads config (that would reset test paths)
    assert fb1["base_url"] == config.LLM_BASE_URL and fb1["api_key"] == config.LLM_API_KEY   # reuses the primary
    assert fb2["api_key"] == "g-key" and "googleapis" in fb2["base_url"]
    assert config.read_fallbacks({}) == []


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "f.sqlite"
    seed_data.build_database(p, seed=42)
    monkeypatch.setattr(config, "DB_PATH", p)
    monkeypatch.setattr(workflow, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path)
    return p


def test_decision_log_names_the_backup(db):
    tid = {r["ordinal"]: r["txn_id"] for r in services.list_demo_scenarios()}[4]   # the ticket scam
    chain = FallbackLLM([Named("gpt-oss-120b", fail={"assessor", "writer", "sanitizer"}),
                         Named("gemini-flash", assessor=MessageAssessment(scam_risk="HIGH", confidence=0.95))])
    c = workflow.run_case(services.get_alert_for_txn(tid).record_id, llm=chain, commit=False)
    assert c.final_route == Route.ESCALATE and not any(e.startswith("assessor") for e in c.errors)
    line = next(s for s in c.trace if "AI read the message" in s)
    assert "answered by backup AI gemini-flash: gpt-oss-120b unavailable" in line
    assert all(u["model"] == "gemini-flash" for u in c.llm_usage)
