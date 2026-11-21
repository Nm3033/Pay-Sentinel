"""Round 3, Area 4: deployment reality. Costs are priced per answering model from config, scaled from MEASURED
checks; speeds come from each check's own timing; the Pilot plan page shows it all."""
import pytest

from src import config, pilot, seed_data, services, telemetry, workflow
from src.llm import FallbackLLM, with_simulated_outage
from tests.test_resilience import SCAM, Named, scam_alert
from tests.test_curveballs import quiet_student

MAIN, BACKUP = "openai/gpt-oss-120b", "qwen/qwen3.8-27b"


@pytest.fixture
def fresh_db(tmp_path):
    p = tmp_path / "pilot.sqlite"
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


def test_each_call_is_priced_by_the_model_that_answered(monkeypatch):
    monkeypatch.setattr(config, "LLM_COST_PER_1M_INPUT", 0.0)
    monkeypatch.setattr(config, "LLM_COST_PER_1M_OUTPUT", 0.0)
    usage = [dict(model=MAIN, prompt_tokens=1_000_000, completion_tokens=1_000_000),
             dict(model=BACKUP, prompt_tokens=1_000_000, completion_tokens=0),
             dict(model="mystery-model", prompt_tokens=10, completion_tokens=10)]
    cost, unpriced = pilot.usage_cost(usage)
    assert cost == pytest.approx(0.15 + 0.60 + 0.80) and unpriced == {"mystery-model"}


def test_checks_record_ai_on_and_their_own_duration(conn):
    case = workflow.run_case(scam_alert(conn), llm=FallbackLLM([Named(MAIN)]), conn=conn)
    assert case.ai_on is True and 0 < case.check_seconds < config.CASE_TIME_LIMIT_S
    rules = workflow.run_case(scam_alert(conn), use_llm=False, conn=conn)
    assert rules.ai_on is False and rules.check_seconds is not None


def test_estimate_scales_measured_cost_to_the_pilot_volume(conn):
    llm = FallbackLLM([Named(MAIN), Named(BACKUP)])
    cases = [workflow.run_case(scam_alert(conn), llm=llm, conn=conn) for _ in range(3)]
    cases.append(workflow.run_case(scam_alert(conn), use_llm=False, conn=conn))        # rules-only: not in AI cost
    est = pilot.estimate(cases)
    measured, _ = pilot.usage_cost([u for c in cases for u in c.llm_usage])
    assert est.checked == 4 and est.ai_checked == 3
    assert est.cost_per_payment_usd == pytest.approx(measured / 3, rel=1e-3)
    assert est.cost_per_day_usd == pytest.approx(est.cost_per_payment_usd * config.PILOT_FLAGGED_PER_DAY, rel=1e-2)
    assert est.cost_per_month_usd == pytest.approx(
        est.cost_per_payment_usd * config.PILOT_FLAGGED_PER_DAY * config.PILOT_SCHOOL_DAYS_PER_MONTH, abs=0.01)
    assert est.worst_cost_per_month_usd >= est.cost_per_month_usd                     # backup is dearer
    assert est.seconds_ai is not None and est.seconds_rules is not None and not est.unpriced_models
    assert 0 < est.staff_minutes_saved_per_payment <= config.STAFF_MIN_PER_REVIEW


def test_backup_answers_cost_more_and_it_shows(conn):
    main_only = workflow.run_case(scam_alert(conn), llm=FallbackLLM([Named(MAIN), Named(BACKUP)]), conn=conn)
    via_backup = workflow.run_case(scam_alert(conn), llm=with_simulated_outage(
        FallbackLLM([Named(MAIN), Named(BACKUP)]), "primary"), conn=conn)
    assert pilot.usage_cost(via_backup.llm_usage)[0] > pilot.usage_cost(main_only.llm_usage)[0]


def test_results_page_cost_is_no_longer_na(conn):
    cases = [workflow.run_case(scam_alert(conn), llm=FallbackLLM([Named(MAIN)]), conn=conn)]
    tel = telemetry.compute_telemetry(cases, conn=conn)
    assert tel.cost_usd > 0 and not tel.unpriced_models


def test_the_plan_is_honest_and_complete():
    assert len(pilot.WHO) >= 4 and len(pilot.WHERE) == 3 and len(pilot.LIMITATIONS) >= 6
    text = " ".join(pilot.LIMITATIONS).lower()
    for must in ("synthetic", "login", "wiped", "groq", "security"):
        assert must in text


def test_code_update_reloads_cached_payments(conn, officer_passwords, monkeypatch):
    """Regression (live site, Phase 4): Streamlit Cloud applied a push inside the running server, the cache handed
    back CaseFiles pickled by the OLD code, and the Pilot plan page crashed with "'CaseFile' object has no attribute
    'ai_on'". The cache key now includes the code version, so a code update always reloads them."""
    import src
    from tests.test_curveballs import _app
    workflow.run_case(scam_alert(conn), use_llm=False, conn=conn)
    calls = []
    real = workflow.list_saved_cases
    monkeypatch.setattr(workflow, "list_saved_cases", lambda: calls.append(1) or real())
    monkeypatch.setattr(src, "source_stamp", lambda: 111)
    at = _app(officer_passwords)
    first = len(calls)
    at.run()
    assert len(calls) == first                      # same code: served from the cache
    monkeypatch.setattr(src, "source_stamp", lambda: 222)                 # a push lands in the running server
    at.run()
    assert not at.exception, at.exception
    assert len(calls) == first + 1                  # reloaded with the new code, no stale objects


def test_app_pilot_page(conn, officer_passwords, monkeypatch):
    import src.llm
    monkeypatch.setattr(src.llm, "get_llm", lambda: FallbackLLM([Named(MAIN)]))   # never a real provider
    from tests.test_curveballs import _app
    at = _app(officer_passwords)
    at.button(key="try10").click().run()                                      # Start page → Pilot plan
    assert not at.exception, at.exception
    assert any("nothing to measure" in i.value for i in at.info)              # honest when nothing measured
    workflow.run_case(scam_alert(conn), llm=FallbackLLM([Named(MAIN)]), conn=conn)
    at.session_state["ver"] = at.session_state["ver"] + 1                     # like refresh(): reload the cases
    at.run()
    assert not at.exception, at.exception
    labels = {m.label: m.value for m in at.metric}
    assert labels["AI cost per payment"].startswith("$") and "Per month (22 school days)" in labels
