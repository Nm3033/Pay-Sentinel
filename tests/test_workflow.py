import re

import pytest

from src import config, seed_data, services, workflow
from src.schemas import Finding, InvestigatorOutput, MessageAssessment, PaymentState, Route
from tests.fakes import FakeLLM


@pytest.fixture(autouse=True)
def _tmp_cases_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(workflow.config, "REPORTS_DIR", tmp_path)


@pytest.fixture
def fresh(tmp_path):
    p = tmp_path / "w.sqlite"
    seed_data.build_database(p, seed=42)
    c = services.connect(p)
    yield c
    c.close()


def alert_of(conn, txn_id):
    return services.get_alert_for_txn(txn_id, conn=conn).record_id


def test_all_12_demo_scenarios_code_only(conn, demo):
    for n, (txn_id, expected) in sorted(demo.items()):
        c = workflow.run_case(alert_of(conn, txn_id), use_llm=False, conn=conn, commit=False, save=False)
        assert c.final_route.value == expected, (n, c.gate.reasons)
        assert len([s for s in c.trace if s.startswith("[CODE]")]) >= 3          # >= 3 autonomous steps logged
        assert c.investigation and c.investigation.student_explanation          # student-friendly explanation


def test_full_dataset_scorecard_code_only(conn):
    rows = conn.execute("SELECT a.alert_id, t.ground_truth FROM alerts a JOIN transactions t USING(txn_id)").fetchall()
    out = {gt: [] for gt in ("SAFE", "SCAM", "UNCERTAIN")}
    for aid, gt in rows:
        out[gt].append(workflow.run_case(aid, use_llm=False, conn=conn, commit=False, save=False).final_route)
    assert Route.CLEAR not in out["SCAM"]                                         # no scam goes through
    assert all(r == Route.ESCALATE for r in out["UNCERTAIN"])                     # uncertain -> escalate
    assert sum(r == Route.CLEAR for r in out["SAFE"]) / len(out["SAFE"]) >= 0.8   # snacks don't wait


def test_brief_scam_with_ai_and_citation_check(conn, demo):
    def writer(user):
        tid = re.search(r'"record_id": "(TXN-\d{5})"', user).group(1)
        return InvestigatorOutput(findings=[Finding(claim="Payment under review.", citations=[tid]),
                                            Finding(claim="Seller has a criminal record.", citations=["STU-9999"])],
                                  student_explanation="This looks like a fake ticket. Don't pay [RCP-9999].",
                                  staff_summary=f"Ticket scam [{tid}]", suggested_route=Route.CLEAR)
    llm = FakeLLM(assessor=MessageAssessment(scam_risk="HIGH", confidence=0.95, summary="fake ticket"), writer=writer)
    c = workflow.run_case(alert_of(conn, demo[4][0]), llm=llm, conn=conn, commit=False)
    assert c.final_route == Route.ESCALATE                                        # AI's CLEAR suggestion ignored
    assert [f.citations for f in c.dropped_findings] == [["STU-9999"]]
    assert "[UNVERIFIED-ID]" in c.investigation.student_explanation
    assert llm.calls == ["sanitizer", "assessor", "writer"]
    assert "## 1. What the student sees" in c.report_markdown


def test_low_ai_confidence_escalates_instead_of_guessing(conn, demo):
    llm = FakeLLM(assessor=MessageAssessment(scam_risk="LOW", confidence=0.3, unclear_terms=["skrrt"]))
    c = workflow.run_case(alert_of(conn, demo[9][0]), llm=llm, conn=conn, commit=False)
    assert c.final_route == Route.ESCALATE and c.gate.uncertain
    assert any("won't guess" in s for s in c.trace)


def test_ai_can_raise_but_not_lower(conn, demo):
    calm = FakeLLM(assessor=MessageAssessment(scam_risk="LOW", confidence=0.99))
    assert workflow.run_case(alert_of(conn, demo[4][0]), llm=calm, conn=conn, commit=False).final_route == Route.ESCALATE
    worried = FakeLLM(assessor=MessageAssessment(scam_risk="HIGH", confidence=0.9))
    c = workflow.run_case(alert_of(conn, demo[3][0]), llm=worried, conn=conn, commit=False)   # friend payment
    assert c.final_route == Route.ESCALATE and any("raised the caution" in s for s in c.trace)


def test_ai_never_sees_raw_hidden_instructions(conn, demo):
    llm = FakeLLM()
    workflow.run_case(alert_of(conn, demo[11][0]), llm=llm, conn=conn, commit=False)
    for user in llm.seen.get("assessor", []) + llm.seen.get("writer", []):
        assert "Ignore previous instructions" not in user and "ground_truth" not in user


def test_ai_outage_falls_back_safely(conn, demo):
    llm = FakeLLM(fail={"sanitizer", "assessor", "writer"})
    c = workflow.run_case(alert_of(conn, demo[5][0]), llm=llm, conn=conn, commit=False)
    assert c.final_route.severity >= Route.HOLD_FOR_CONFIRMATION.severity
    assert c.investigation.student_explanation and any("unavailable" in s for s in c.trace)


def test_verification_timeout_pauses_never_crashes(conn, demo):
    c = workflow.run_case(alert_of(conn, demo[12][0]), use_llm=False, conn=conn, commit=False)
    assert c.recipient is None and c.final_route == Route.HOLD_FOR_CONFIRMATION
    canteen = workflow.run_case(alert_of(conn, demo[1][0]), use_llm=False, conn=conn, commit=False,
                                simulate_outage=True)
    assert canteen.final_route == Route.HOLD_FOR_CONFIRMATION                    # outage toggle: pause, not crash


def test_rerun_is_idempotent_no_second_prompt(fresh, demo):
    aid = alert_of(fresh, demo[5][0])
    first = workflow.run_case(aid, use_llm=False, conn=fresh)
    assert first.state == PaymentState.PENDING_CONFIRMATION
    again = workflow.run_case(aid, use_llm=False, conn=fresh)
    assert again.state == PaymentState.PENDING_CONFIRMATION and any("not asking again" in s for s in again.trace)
    assert services.get_payment_state(demo[5][0], conn=fresh).prompts_sent == 1


def test_hold_upgrades_to_escalate_on_rerun(fresh, demo):
    aid = alert_of(fresh, demo[5][0])
    workflow.run_case(aid, use_llm=False, conn=fresh)
    worried = FakeLLM(assessor=MessageAssessment(scam_risk="HIGH", confidence=0.95))
    assert workflow.run_case(aid, llm=worried, conn=fresh).state == PaymentState.ESCALATED


def test_trace_is_plain_language(conn, demo):
    seen = []
    workflow.run_case(alert_of(conn, demo[4][0]), use_llm=False, conn=conn, commit=False, progress=seen.append)
    assert seen and all("()" not in s and "Stage " not in s and "regex" not in s.lower() for s in seen)
