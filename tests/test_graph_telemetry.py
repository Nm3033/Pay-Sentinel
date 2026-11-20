import pytest

from src import config, services, workflow
from src.graph import campaign_dot, campaign_verdict, case_outcome, detect_scam_campaigns
from src.telemetry import campaign_precision, compute_telemetry


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(workflow, "CASES_DIR", tmp_path / "cases")


def test_ticket_campaign_detected(conn):
    camps = detect_scam_campaigns(conn=conn)
    tix = next(c for c in camps if c.handle == "@campus_tix_99")
    assert len(tix.student_ids) >= config.CAMPAIGN_MIN_STUDENTS and tix.links
    assert all(len(c.student_ids) >= config.CAMPAIGN_MIN_STUDENTS for c in camps)
    assert all(c.network_id.startswith("NET-") for c in camps)
    good, total = campaign_precision([tix], conn=conn)
    assert (good, total) == (1, 1)            # the ticket campaign is made of real scams only


def test_vendors_and_established_classmates_never_flagged(conn):
    camps = detect_scam_campaigns(conn=conn)
    assert not any(c.handle in {"@campus_canteen", "@bookstore_hub"} for c in camps)
    assert not any(c.established for c in camps)


def test_campaign_dot(conn):
    dot = campaign_dot(detect_scam_campaigns(conn=conn)[:1], "dark")
    assert dot.startswith("digraph") and "->" in dot and dot.rstrip().endswith("}")


@pytest.mark.parametrize("outcomes,code,complete", [
    (["UNTRIAGED"] * 3, "NOT_TRIAGED", False),
    (["CANCELLED", "UNTRIAGED"], "PARTIAL", False),
    (["AWAITING_STAFF", "UNTRIAGED"], "AWAITING_STAFF", False),
    (["AWAITING_STUDENT", "CANCELLED"], "AWAITING_STUDENT", True),
    (["CANCELLED", "EXPIRED"], "STOPPED", True),
    (["CANCELLED", "SENT"], "MONEY_SENT", True),
])
def test_campaign_verdict(outcomes, code, complete):
    v = campaign_verdict(outcomes)
    assert v["code"] == code and v["complete"] is complete and v["label"] and v["explain"]


def test_case_outcome_from_real_case(conn):
    c = workflow.run_case(services.get_alert_for_txn(
        conn.execute("SELECT txn_id FROM demo_scenarios WHERE ordinal=4").fetchone()[0], conn=conn).record_id,
        use_llm=False, conn=conn, commit=False, save=False)
    assert case_outcome(None) == "UNTRIAGED" and case_outcome(c) == "AWAITING_STAFF"


def test_telemetry_code_only(conn):
    cases = [workflow.run_case(a.record_id, use_llm=False, conn=conn, commit=False, save=False)
             for a in services.list_alerts(conn=conn)]
    t = compute_telemetry(cases, conn=conn)
    assert t.triaged == 250 and sum(r["count"] for r in t.outcome) == 250
    assert t.scams_cleared == 0 and t.injections_cleared == 0 and t.uncertain_escalated == t.uncertain_total == 15
    assert t.auto_clear_rate >= 0.8 and t.queue_precision > t.legacy_precision
    assert t.hours_saved > 0 and t.llm_calls == 0
