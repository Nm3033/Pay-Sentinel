"""Human flows (student + support staff) and the Streamlit app."""
from pathlib import Path

import pytest

from src import auth, config, seed_data, services, workflow
from src.schemas import PaymentState
from tests.fakes import FakeLLM

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fresh_db(tmp_path):
    p = tmp_path / "h.sqlite"
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


def run(conn, txn_id):
    return workflow.run_case(services.get_alert_for_txn(txn_id, conn=conn).record_id, use_llm=False, conn=conn)


def _demo(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT ordinal, txn_id FROM demo_scenarios")}


def test_student_confirms_textbook(conn):
    c = run(conn, _demo(conn)[5])
    assert c.state == PaymentState.PENDING_CONFIRMATION
    done = workflow.student_respond(c.case_id, "STUDENT_CONFIRM", c.transaction.student_id, conn=conn)
    assert done.state == PaymentState.CONFIRMED_CANCELLABLE and done.trace[-1].startswith("[HUMAN]")
    with pytest.raises(services.ApprovalRequired):   # someone else's reply is refused
        workflow.student_respond(run(conn, _demo(conn)[12]).case_id, "STUDENT_CONFIRM", "STU-0999", conn=conn)


def test_student_report_sends_to_staff(conn):
    c = run(conn, _demo(conn)[5])
    assert workflow.student_respond(c.case_id, "STUDENT_REPORT", c.transaction.student_id,
                                    conn=conn).state == PaymentState.ESCALATED


def test_staff_cancel_and_ask_student(conn, officer_token):
    scam = run(conn, _demo(conn)[4])
    assert scam.state == PaymentState.ESCALATED
    done = workflow.staff_decide(scam.case_id, "STAFF_CANCEL", officer_token, "fake ticket", conn=conn)
    assert done.state == PaymentState.CANCELLED and "| staff:SUP-01 | STAFF_CANCEL |" in done.report_markdown
    unclear = run(conn, _demo(conn)[8])
    back = workflow.staff_decide(unclear.case_id, "STAFF_ASK_STUDENT", officer_token, conn=conn)
    assert back.state == PaymentState.PENDING_CONFIRMATION


def test_reminder_cap_through_workflow(conn):
    c = run(conn, _demo(conn)[5])
    for _ in range(config.MAX_STUDENT_PROMPTS - 1):
        workflow.remind_student(c.case_id, conn=conn)
    with pytest.raises(services.InvalidTransition):
        workflow.remind_student(c.case_id, conn=conn)


def test_expire_holds_updates_case_files(conn):
    from datetime import datetime, timedelta
    c = run(conn, _demo(conn)[5])
    later = datetime.now() + timedelta(hours=config.HOLD_EXPIRY_HOURS + 1)
    assert workflow.expire_holds(later, conn=conn) == [c.transaction.record_id]
    assert workflow.load_saved_case(c.case_id).state == PaymentState.EXPIRED


# ---- Streamlit app ------------------------------------------------------------------------------------
def _app():
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=90)


def _signed_in(officer_passwords):
    at = _app()
    at.session_state["token"] = auth.login("SUP-01", officer_passwords["SUP-01"])
    return at.run()


def test_signed_out_shows_only_login():
    at = _app().run()
    assert not at.exception, at.exception
    assert [t.label for t in at.text_input] == ["Staff ID", "Password"]
    assert not at.metric and not at.dataframe


def test_login_wrong_password_rejected():
    at = _app().run()
    at.text_input(key="login_id").input("SUP-01")
    at.text_input(key="login_pw").input("definitely-wrong")
    at.button[0].click().run()
    assert at.session_state["token"] is None
    assert any("Invalid staff ID or password" in e.value for e in at.error)
    auth._admin("SUP-01", failed=0, locked_until=0)


def test_login_then_start_page(officer_passwords):
    at = _app().run()
    at.text_input(key="login_id").input("sup-01")
    at.text_input(key="login_pw").input(officer_passwords["SUP-01"])
    at.button[0].click().run()
    assert not at.exception, at.exception
    assert at.session_state["token"] and any("What each tab does" in md.value for md in at.markdown)


@pytest.mark.parametrize("key,heading", [("try2", "Payment details"), ("try3", "Student app"), ("try4", "Staff review"),
                                         ("try5", "Add payments"), ("try6", "Results"),
                                         ("try7", "Scam alerts")])
def test_every_guided_page_renders(conn, officer_passwords, key, heading):
    for n in (4, 5):   # put one case with staff and one with a student so those pages have content
        run(conn, _demo(conn)[n])
    at = _signed_in(officer_passwords)
    at.button(key=key).click().run()
    assert not at.exception, at.exception
    assert any(heading in md.value for md in at.markdown), heading


def test_staff_page_lists_only_escalated(conn, officer_passwords, officer_token):
    d = _demo(conn)
    a, b, hold = run(conn, d[4]), run(conn, d[6]), run(conn, d[5])
    workflow.staff_decide(a.case_id, "STAFF_CANCEL", officer_token, conn=conn)
    at = _signed_in(officer_passwords)
    at.button(key="try4").click().run()
    opts = [o for sb in at.selectbox for o in sb.options]
    assert any(b.alert.record_id in o for o in opts)
    assert not any(a.alert.record_id in o for o in opts) and not any(hold.alert.record_id in o for o in opts)


def test_run_12_scenarios_in_parallel_from_the_app(conn, officer_passwords):
    at = _app()
    at.session_state["token"] = auth.login("SUP-01", officer_passwords["SUP-01"])
    at.session_state["use_llm"] = False            # rules only: no AI quota used by the test suite
    at.session_state["workers"] = 4
    at.run()
    at.button(key="try1").click().run()           # ▶ Run 12 scenarios -> triage_many, 4 at a time
    assert not at.exception, at.exception
    assert any("Checked **12** payment(s), 4 at a time" in s.value for s in at.success), [s.value for s in at.success]
    assert len(services.list_payment_states(conn=conn)) == 12


def test_queue_starred_first_and_details_recent_first(conn, officer_passwords):
    d = _demo(conn)
    alerts = [a.record_id for a in services.list_alerts(conn=conn)]
    starred = services.list_starred(conn=conn)
    plain = [a for a in alerts if a not in starred]
    services.set_starred([plain[-1]], True, "SUP-01", conn=conn)            # star the very last payment
    first, second = run(conn, d[1]), run(conn, d[2])                         # second is checked most recently
    at = _signed_in(officer_passwords)
    at.button(key="try2").click().run()                                      # Payment details
    assert not at.exception, at.exception
    opts = next(s for s in at.selectbox if s.label == "Payment").options
    assert second.alert.record_id in opts[0] and first.alert.record_id in opts[1]
    assert plain[-1] in opts[2] or any(plain[-1] in o for o in opts[2:15])  # starred, unchecked: before the rest
    assert opts.index(next(o for o in opts if plain[-1] in o)) < opts.index(next(o for o in opts if plain[0] in o))
