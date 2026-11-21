"""The check as a live flowchart: every payment's check reports each stage (working -> done / skipped / fallback)
as it happens, so the app can show the AI and the rules at work in real time. Fake AI providers only."""
import pytest

from src import config, seed_data, services, workflow
from src.llm import FallbackLLM, with_simulated_outage
from tests.test_resilience import Named, scam_alert
from tests.test_curveballs import quiet_student

KEYS = [k for k, *_ in workflow.FLOW]


@pytest.fixture
def fresh_db(tmp_path):
    p = tmp_path / "flow.sqlite"
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


def status(case) -> dict:
    return {s["key"]: s["status"] for s in case.stages}


def test_live_updates_show_working_before_done_for_every_stage(conn):
    seen: list[list[dict]] = []
    case = workflow.run_case(scam_alert(conn), llm=FallbackLLM([Named("openai/gpt-oss-120b")]), conn=conn,
                             on_flow=lambda st: seen.append(st))
    assert [s["key"] for s in case.stages] == KEYS
    assert all(v == "done" for v in status(case).values()), status(case)
    for k in KEYS:                                   # each box went "working" first, then "done", in order
        states = [next(s["status"] for s in snap if s["key"] == k) for snap in seen]
        assert "working" in states and states.index("working") < states.index("done")
    ai = next(s for s in case.stages if s["key"] == "ai_read")
    assert ai["who"] == "AI" and "answered by openai/gpt-oss-120b" in ai["text"] and ai["seconds"] is not None


def test_safe_payment_without_a_message_skips_what_it_doesnt_need(conn):
    valid, _ = services.validate_new_transactions([dict(student_id=quiet_student(conn), amount_aed=12,
                                                        txn_type="PURCHASE", channel="QR",
                                                        recipient_handle="@campus_canteen")], conn=conn)
    ids = services.add_transactions(valid, "SUP-01", conn=conn)[0]
    case = workflow.run_case(ids["alert_id"], llm=FallbackLLM([Named("openai/gpt-oss-120b")]), conn=conn)
    st = status(case)
    assert st["read"] == st["scan"] == st["ai_read"] == st["ai_write"] == "skipped"
    assert st["facts"] == st["rules"] == st["decide"] == "done"


def test_ai_down_shows_the_rules_taking_over(conn):
    chain = with_simulated_outage(FallbackLLM([Named("openai/gpt-oss-120b")]), "all")
    case = workflow.run_case(scam_alert(conn), llm=chain, conn=conn)
    st = status(case)
    assert st["ai_read"] == st["ai_write"] == "fallback" and st["rules"] == "done"


def test_rules_only_and_stages_are_saved_with_the_payment(conn):
    case = workflow.run_case(scam_alert(conn), use_llm=False, conn=conn)
    assert status(case)["ai_read"] == "skipped" and "AI switched off" in case.stages[2]["text"]
    assert workflow.load_saved_case(case.case_id).stages == case.stages       # the details page can redraw it


def test_app_home_and_details_show_the_flowchart(conn, officer_passwords):
    from tests.test_curveballs import _app
    at = _app(officer_passwords)
    assert not at.exception, at.exception
    assert any('<div class="ps-flow">' in m.value for m in at.markdown)                      # home: how it works
    assert {"try1", "try2", "try3", "try4", "try5", "try6", "try7", "try8", "try9", "try10", "try11"} <= \
        {b.key for b in at.button}                                             # every tab explained, one click away
    at.button(key="cta_watch").click().run()                                   # → the ticket scam, details page
    assert not at.exception, at.exception
    at.button[[b.label for b in at.button].index("🔍 Check payment")].click().run()
    assert not at.exception, at.exception
    flows = [m.value for m in at.markdown if '<div class="ps-flow">' in m.value]
    assert flows and "Decision saved" in flows[0] and "ps-node done" in flows[0]
