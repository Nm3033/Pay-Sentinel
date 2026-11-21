"""Round 3, Area 1: one end-to-end user journey, run the way a judge would.

Every payment is TYPED IN through the console path (validate -> add_transactions), checked by the normal workflow,
answered by a person through the normal role functions, and followed with src/journey.py until the student gets
the final outcome and the decision report exists. Nothing here uses the 12 named demo scenarios."""
from datetime import datetime, timedelta

import pytest

from src import config, seed_data, services, workflow
from src.journey import build_journey, who
from src.schemas import NewTransaction, PaymentState, Route
from tests.test_curveballs import new_unverified, quiet_student


@pytest.fixture
def fresh_db(tmp_path):
    p = tmp_path / "journey.sqlite"
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


def typed(conn, **row) -> dict:
    """A payment entered on the Add payments page: validated first, exactly like the form does."""
    row.setdefault("txn_type", "P2P")
    valid, errors = services.validate_new_transactions([row], conn=conn)
    assert not errors, errors
    return services.add_transactions(valid, "SUP-01", conn=conn)[0]


def journey(conn, alert_id: str):
    alert = services.get_alert(alert_id, conn=conn)
    txn = services.get_transaction(alert.txn_id, conn=conn)
    case_id = alert_id.replace("ALR-", "CASE-")
    case = workflow.load_saved_case(case_id) if (workflow.CASES_DIR / f"{case_id}.json").exists() else None
    return build_journey(alert, txn, case, services.get_payment_state(txn.record_id, conn=conn))


def status(j) -> dict:
    return {s.key: s.status for s in j.steps}


def test_typed_payment_full_journey_ends_in_notice_and_report(conn):
    stu = quiet_student(conn)
    ids = typed(conn, student_id=stu, amount_aed=40, recipient_handle=new_unverified(conn),
                message="hey it's sami from class, send the 40 for the hoodie")
    j = journey(conn, ids["alert_id"])                      # trigger only: not checked yet
    assert j.waiting_for == "CHECK" and status(j)["trigger"] == "done" and status(j)["checked"] == "current"
    assert "Add payments" in j.steps[0].detail and not j.finished

    case = workflow.run_case(ids["alert_id"], use_llm=False, conn=conn)
    assert case.final_route == Route.HOLD_FOR_CONFIRMATION
    j = journey(conn, ids["alert_id"])
    assert j.waiting_for == "STUDENT" and status(j)["person"] == "current" and "Waiting for the student" in j.steps[2].detail
    assert j.notice                                         # the student already sees what to do

    workflow.student_respond(case.case_id, "STUDENT_CONFIRM", stu, conn=conn)
    j = journey(conn, ids["alert_id"])
    assert status(j)["decided"] == "current" and "Wait, cancel it" in j.notice

    rec = services.get_payment_state(ids["txn_id"], conn=conn)
    assert ids["txn_id"] in workflow.release_due(rec.expires_at + timedelta(seconds=1), conn=conn)
    j = journey(conn, ids["alert_id"])
    assert j.finished and j.waiting_for == "NONE"
    assert all(s.status == "done" for s in j.steps), status(j)
    assert "has been sent" in j.notice and f"Student {stu} confirmed they know the seller" in j.steps[2].detail
    saved = workflow.load_saved_case(case.case_id)
    assert saved.report_markdown and ids["txn_id"] in saved.report_markdown    # the shareable output exists


def test_safe_payment_skips_the_person_step(conn):
    ids = typed(conn, student_id=quiet_student(conn), amount_aed=12, txn_type="PURCHASE", channel="QR",
                recipient_handle="@campus_canteen", memo="lunch")
    workflow.run_case(ids["alert_id"], use_llm=False, conn=conn)
    j = journey(conn, ids["alert_id"])
    assert j.finished and status(j)["person"] == "skipped" and "went through" in j.notice


def test_scam_escalated_then_cancelled_by_staff(conn, officer_token):
    ids = typed(conn, student_id=quiet_student(conn), amount_aed=50, channel="LINK", recipient_handle="@campus_tix_99",
                message="Bro urgent! Selling my Front Row Fest pass for 50 AED, click here to claim! "
                        "frontrow-fest.co/claim")
    case = workflow.run_case(ids["alert_id"], use_llm=False, conn=conn)
    assert case.final_route == Route.ESCALATE
    assert journey(conn, ids["alert_id"]).waiting_for == "STAFF"
    workflow.staff_decide(case.case_id, "STAFF_CANCEL", officer_token, "fake ticket seller", conn=conn)
    j = journey(conn, ids["alert_id"])
    assert j.finished and "still in your wallet" in j.notice and "support team stopped it" in j.notice
    assert "Support staff SUP-01 cancelled it" in j.steps[2].detail


def test_unanswered_hold_expires_and_money_returns(conn):
    stu = quiet_student(conn)
    ids = typed(conn, student_id=stu, amount_aed=40, recipient_handle=new_unverified(conn))
    workflow.run_case(ids["alert_id"], use_llm=False, conn=conn)
    later = datetime.now() + timedelta(hours=config.HOLD_EXPIRY_HOURS + 1)
    assert ids["txn_id"] in workflow.expire_holds(later, conn=conn)
    j = journey(conn, ids["alert_id"])
    assert j.finished and services.get_payment_state(ids["txn_id"], conn=conn).state == PaymentState.EXPIRED
    assert "back in your wallet" in j.notice and "Nobody answered" in j.steps[2].detail


def test_missing_case_file_still_gives_a_coherent_journey(conn):
    """If only the stored state survives (e.g. the case file was deleted), the journey follows the state."""
    stu = quiet_student(conn)
    ids = typed(conn, student_id=stu, amount_aed=40, recipient_handle=new_unverified(conn))
    workflow.run_case(ids["alert_id"], use_llm=False, conn=conn)
    (workflow.CASES_DIR / f"{ids['alert_id'].replace('ALR-', 'CASE-')}.json").unlink()
    j = journey(conn, ids["alert_id"])
    assert status(j)["checked"] == "done" and status(j)["person"] == "current" and j.waiting_for == "STUDENT"
    assert [s.status for s in j.steps].count("current") == 1 and j.notice


def test_journey_works_for_any_payment_in_the_dataset(conn):
    """No hard-coded shortcuts: every flagged payment, not only the demo ones, gets a coherent journey."""
    alerts = [a.record_id for a in services.list_alerts(limit=40, status="NEW", conn=conn)]
    workflow.triage_many(alerts, use_llm=False)
    for a in alerts:
        j = journey(conn, a)
        assert [s.key for s in j.steps] == ["trigger", "checked", "person", "decided", "output"]
        assert status(j)["checked"] == "done" and j.notice
        assert j.finished == (j.waiting_for == "NONE")


def test_app_journey_from_typed_payment_to_outcome(conn, officer_passwords):
    """The live demo path in the web app: type a payment → follow its journey → hand off to staff → staff cancel →
    back to the journey, which ends with the student's outcome and the report. No page searching by hand."""
    from tests.test_curveballs import _app
    at = _app(officer_passwords)
    at.button(key="try5").click().run()                                    # → Add payments
    assert not at.exception, at.exception
    next(b for b in at.button if b.label.startswith("Use example: Ticket scam")).click().run()
    next(b for b in at.button if b.label == "➕ Add payment").click().run()   # typed in + checked straight away
    assert not at.exception, at.exception
    new = conn.execute("SELECT alert_id FROM alerts WHERE legacy_rule = 'MANUAL_ENTRY' ORDER BY alert_id DESC").fetchone()[0]

    at.button(key="follow_new").click().run()                              # → Payment details, this payment
    assert not at.exception, at.exception
    assert any("This payment's journey" in md.value for md in at.markdown)
    at.button(key="j_staff").click().run()                                 # hand-off: staff page, already picked
    assert not at.exception, at.exception
    assert at.selectbox(key="gate_sel").value == new
    at.button(key="STAFF_CANCEL").click().run()
    assert not at.exception, at.exception
    txn = services.get_alert(new, conn=conn).txn_id
    assert services.get_payment_state(txn, conn=conn).state == PaymentState.CANCELLED

    at.button(key="journey_back").click().run()                            # back to the finished journey
    assert not at.exception, at.exception
    assert any("Sent to the student" in s.value and "still in your wallet" in s.value for s in at.success)


def test_staff_action_after_hand_off_hits_the_handed_payment_only(conn, officer_passwords):
    """Regression (found in the Round 3 rehearsal): with TWO payments waiting for staff, the hand-off opened the
    right one, but the click's rerun reset the dropdown to the first payment, so Cancel hit the WRONG payment."""
    from tests.test_curveballs import _app
    scam = dict(amount_aed=50, channel="LINK", recipient_handle="@campus_tix_99",
                message="Bro urgent! Selling my Front Row Fest pass, click here! frontrow-fest.co/claim")
    first = typed(conn, student_id=quiet_student(conn), **scam)
    second = typed(conn, student_id=quiet_student(conn), **scam)
    for ids in (first, second):
        workflow.run_case(ids["alert_id"], use_llm=False, conn=conn)
    at = _app(officer_passwords)
    at.session_state["gate_pick"] = second["alert_id"]                      # what "Review as support staff" does
    at.session_state["journey_return"] = second["alert_id"]
    at.button(key="try4").click().run()                                     # Start page → Staff review
    assert not at.exception, at.exception
    assert at.selectbox(key="gate_sel").value == second["alert_id"]
    at.button(key="STAFF_CANCEL").click().run()
    assert not at.exception, at.exception
    state = lambda ids: services.get_payment_state(ids["txn_id"], conn=conn).state   # noqa: E731
    assert state(second) == PaymentState.CANCELLED                          # the handed-over payment
    assert state(first) == PaymentState.ESCALATED                           # untouched


def test_who_names_every_actor():
    assert who("student:STU-0004") == "Student STU-0004" and who("staff:SUP-01") == "Support staff SUP-01"
    assert who("guardian:GRD-0001") == "Parent/guardian GRD-0001" and who("system") == "Pay Sentinel (automatic)"
