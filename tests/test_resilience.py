"""Round 3, Area 2: workload resilience. Several inputs back to back, one failing; the main AI down (backup takes
over); every AI down (rules only); a slow AI hitting the per-payment time limit; and plain-language messages."""
import time

import pytest

from src import config, seed_data, services, workflow
from src.llm import CASE_DEADLINE, FallbackLLM, LLMClient, LLMError, with_simulated_outage
from src.schemas import Route
from tests.fakes import FakeLLM
from tests.test_curveballs import new_unverified, quiet_student

SCAM = ("Bro urgent! Selling my Front Row Fest pass for 50 AED, click here to claim! frontrow-fest.co/claim")


class Named(FakeLLM):
    def __init__(self, model, delay=0.0, **kw):
        super().__init__(**kw)
        self.model, self.delay, self._blocked_until = model, delay, 0.0

    def chat_json(self, *a, **kw):
        time.sleep(self.delay)
        return super().chat_json(*a, **kw)


@pytest.fixture
def fresh_db(tmp_path):
    p = tmp_path / "res.sqlite"
    seed_data.build_database(p, seed=42)
    return p


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch, fresh_db):
    monkeypatch.setattr(workflow, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", fresh_db)


@pytest.fixture
def conn(fresh_db):
    c = services.connect(fresh_db)
    yield c
    c.close()


def scam_alert(conn) -> str:
    valid, errors = services.validate_new_transactions([dict(
        student_id=quiet_student(conn), amount_aed=50, txn_type="P2P", channel="LINK",
        recipient_handle="@campus_tix_99", message=SCAM)], conn=conn)
    assert not errors
    return services.add_transactions(valid, "SUP-01", conn=conn)[0]["alert_id"]


# ---- failover ---------------------------------------------------------------------------------------------------
def test_main_ai_down_backup_takes_over_and_says_so(conn):
    chain = with_simulated_outage(FallbackLLM([Named("main-ai"), Named("backup-ai")]), "primary")
    case = workflow.run_case(scam_alert(conn), llm=chain, conn=conn)
    assert case.final_route == Route.ESCALATE
    assert case.llm_usage and all(u["model"] == "backup-ai" for u in case.llm_usage)
    assert any("backup AI (backup-ai) took over" in n and "simulated outage" in n for n in case.notices)
    assert any("backup AI backup-ai" in s for s in case.trace)


def test_every_ai_down_rules_still_decide_safely(conn):
    chain = with_simulated_outage(FallbackLLM([Named("main-ai"), Named("backup-ai")]), "all")
    case = workflow.run_case(scam_alert(conn), llm=chain, conn=conn)
    assert case.final_route == Route.ESCALATE and not case.llm_usage      # never a less careful decision
    assert any("Every AI was switched off" in n for n in case.notices)
    assert case.investigation and case.investigation.student_explanation  # the rules still explain it


def test_slow_ai_hits_the_time_limit_and_rules_finish(conn, monkeypatch):
    monkeypatch.setattr(config, "CASE_TIME_LIMIT_S", 1.0)
    chain = FallbackLLM([Named("slow-ai", delay=0.4)])
    t0 = time.perf_counter()
    case = workflow.run_case(scam_alert(conn), llm=chain, conn=conn)
    assert time.perf_counter() - t0 < 3.0                                 # bounded, no hang
    assert case.final_route == Route.ESCALATE
    assert any("longer than 1 seconds" in n for n in case.notices)
    assert any("Time limit of 1 s reached" in s for s in case.trace)


def test_real_client_skips_the_call_once_time_is_up():
    """The real AI client never even contacts the provider when this payment's time is already used up."""
    client = LLMClient(base_url="http://127.0.0.1:9/v1", model="unreachable", api_key="x")
    token = CASE_DEADLINE.set(time.monotonic() - 1)
    try:
        with pytest.raises(LLMError, match="time limit"):
            client.chat_json("s", "u", workflow.MessageAssessment, purpose="assessor")
    finally:
        CASE_DEADLINE.reset(token)


def test_the_phase0_rate_limit_message_is_explained_plainly():
    raw = ("writer: all AI providers unavailable: openai/gpt-oss-120b (rate limited: Error code: 429 - Rate limit "
           "reached for model `openai/gpt-oss-120); qwen/qwen3.8-27b (rate limited for 0 min: Error code: 429")
    assert "free-tier rate limit" in workflow.explain_problem(raw) and "not affected" in workflow.explain_problem(raw)
    both = ("writer: all AI providers unavailable: qwen/qwen3.8-27b (rate limited, and waiting 8 s would pass the time "
            "limit: Error code: 429)")
    assert "waiting for it would have taken too long" in workflow.explain_problem(both)
    assert "longer than" not in workflow.explain_problem(both)        # it was the rate limit, not a slow check
    assert "recipient directory" in workflow.explain_problem(
        "verify_campus_recipient: VerificationTimeout: campus recipient directory did not respond within 3s")


# ---- several inputs back to back, one failing ---------------------------------------------------------------------
def rows(conn) -> list[dict]:
    s = quiet_student(conn)
    return [dict(student_id=s, amount_aed=12, txn_type="PURCHASE", channel="QR", recipient_handle="@campus_canteen"),
            dict(student_id=s, amount_aed=50, channel="LINK", recipient_handle="@campus_tix_99", message=SCAM),
            dict(student_id=s, amount_aed=-5, recipient_handle="campus canteen"),           # broken on purpose
            dict(student_id=s, amount_aed=45, recipient_handle=new_unverified(conn), message="ngl slide the bread"),
            dict(student_id=s, amount_aed=15, txn_type="PURCHASE", recipient_handle="@campus_canteen_")]


def test_five_back_to_back_one_broken_the_rest_finish(conn):
    seen: list[list[str]] = []
    out = workflow.run_back_to_back(rows(conn), "SUP-01", use_llm=False,
                                    on_update=lambda r: seen.append([x["status"] for x in r]))
    assert [r["status"] for r in out] == ["done", "done", "failed", "done", "done"]
    assert "Refused at entry" in out[2]["note"] and not out[2]["alert_id"]
    assert out[3]["seconds"] is not None and out[4]["route"] == "ESCALATE"        # it kept going after the failure
    assert seen[0] == ["waiting"] * 5 and seen[1][0] == "checking"                # live updates, in order
    assert sum(1 for s in seen if "checking" in s) == 5


def _workload_in_app(officer_passwords, ai_mode=None, **state):
    from tests.test_curveballs import _app
    at = _app(officer_passwords)
    for k, v in state.items():
        at.session_state[k] = v
    at.button(key="try5").click().run()                              # → Add payments
    assert not at.exception, at.exception
    if ai_mode:                                                     # choose it in the sidebar, like a user
        at.selectbox(key="ai_mode").set_value(ai_mode).run()
        assert at.selectbox(key="ai_mode").value == ai_mode
    at.button(key="btb_go").click().run()
    assert not at.exception, at.exception
    return at


def _status_table(at):
    return next(d.value for d in at.dataframe if "What happened" in d.value.columns)


def test_app_workload_test_runs_five_with_one_failure(conn, officer_passwords):
    at = _workload_in_app(officer_passwords)
    assert any("4 of 5 payments checked" in w.value and "failed safely" in w.value for w in at.warning)
    table = _status_table(at)
    assert list(table["Status"]).count("✅ Done") == 4 and "Refused at entry" in " ".join(table["What happened"])


def test_app_every_ai_down_is_handled_and_flagged(conn, officer_passwords, monkeypatch):
    import src.llm
    fakes = FallbackLLM([Named("main-ai"), Named("backup-ai")])      # NEVER the real providers in a test
    monkeypatch.setattr(src.llm, "get_llm", lambda: fakes)
    at = _workload_in_app(officer_passwords, use_llm=True, ai_mode="all")
    assert not any(c.calls for c in fakes.clients)                   # "all down": no AI was even called
    assert any("Failure test on" in w.value for w in at.warning)    # the demo state is always visible
    notes = " ".join(_status_table(at)["What happened"])
    assert "Every AI was switched off" in notes
    assert any("4 of 5 payments checked" in w.value for w in at.warning)


def test_an_unexpected_crash_is_isolated(conn, monkeypatch):
    real = workflow.run_case

    def flaky(alert_id, **kw):
        if flaky.calls == 1:
            flaky.calls += 1
            raise RuntimeError("disk full")
        flaky.calls += 1
        return real(alert_id, **kw)
    flaky.calls = 0
    monkeypatch.setattr(workflow, "run_case", flaky)
    good = [r for r in rows(conn) if r["amount_aed"] > 0][:3]
    out = workflow.run_back_to_back(good, "SUP-01", use_llm=False)
    assert [r["status"] for r in out] == ["done", "failed", "done"]
    assert "Unexpected problem (RuntimeError)" in out[1]["note"]
