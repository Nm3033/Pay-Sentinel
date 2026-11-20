from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from src import config, seed_data, services
from src.schemas import Action, PaymentState, StaffApproval, Transaction
from src.services import ApprovalRequired, InvalidTransition, VerificationTimeout


@pytest.fixture
def fresh(tmp_path):
    """A private DB for tests that change payment states."""
    p = tmp_path / "s.sqlite"
    seed_data.build_database(p, seed=42)
    c = services.connect(p)
    yield c
    c.close()


# ---- Dataset ---------------------------------------------------------------------------------
def test_mix_and_scenarios(conn, demo):
    mix = dict(conn.execute("SELECT ground_truth, COUNT(*) FROM transactions GROUP BY ground_truth").fetchall())
    assert mix == {"SAFE": 148, "SCAM": 40, "UNCERTAIN": 12}
    assert sum(mix.values()) == sum(seed_data.COUNTS.values()) + 12 == 200
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 200
    assert len(demo) == 12 and sum(e == "ESCALATE" for _, e in demo.values()) >= 3
    titles = [d["title"] for d in services.list_demo_scenarios(conn=conn)]
    assert sum(t.startswith("Unclear slang") for t in titles) == 3          # brief: 3 uncertain cases
    assert "expected_route" not in services.list_demo_scenarios(conn=conn)[0]  # eval column hidden


def test_deterministic(tmp_path, conn):
    other = tmp_path / "again.sqlite"
    seed_data.build_database(other, seed=42)
    c2 = services.connect(other)
    q = "SELECT * FROM transactions ORDER BY txn_id"
    assert [tuple(r) for r in conn.execute(q)] == [tuple(r) for r in c2.execute(q)]
    c2.close()


def test_brief_message_in_dataset(conn):
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE text = ?", (seed_data.BRIEF_SCAM_MESSAGE,)).fetchone()[0]


def test_transaction_model_rejects_eval_columns(conn):
    t = services.get_transaction("TXN-00001", conn=conn)
    with pytest.raises(ValidationError):
        Transaction(**t.model_dump(), ground_truth="SAFE")


# ---- Tool 1: get_transaction_context ----------------------------------------------------------------
def test_context_has_student_device_message_velocity(conn, demo):
    txn_id = demo[4][0]   # the brief's ticket scam
    x = services.get_transaction_context(txn_id, conn=conn)
    assert x.student.record_id == x.transaction.student_id and x.message is not None
    assert x.message.text == seed_data.BRIEF_SCAM_MESSAGE and x.transaction.device_id in {d.record_id for d in x.devices}
    assert txn_id in x.velocity.txn_ids and {"STU", "TXN", "MSG", "VEL"} <= {i.split("-")[0] for i in x.record_ids()}


def test_not_found(conn):
    with pytest.raises(services.NotFound):
        services.get_transaction_context("TXN-99999", conn=conn)


# ---- Tool 2: verify_campus_recipient ---------------------------------------------------------------------
def test_verify_vendor_peer_scam_and_unknown(conn):
    v = services.verify_campus_recipient("@campus_canteen", conn=conn)
    assert v.found and v.verified_vendor and v.recipient.record_id.startswith("RCP-")
    s = services.verify_campus_recipient("@campus_tix_99", as_of=datetime(2026, 11, 12).date(), conn=conn)
    assert not s.verified_vendor and s.recipient.scam_reports >= config.REPORTS_ESCALATE and s.account_age_days < 30
    u = services.verify_campus_recipient("@nobody_here", conn=conn)
    assert not u.found and u.recipient is None


def test_verification_outage_raises_timeout(conn):
    with pytest.raises(VerificationTimeout):
        services.verify_campus_recipient("@hoodie_swap_ali", conn=conn)      # upstream marked as timing out
    with pytest.raises(VerificationTimeout):
        services.verify_campus_recipient("@campus_canteen", simulate_outage=True, conn=conn)


def test_campaign_spread(conn, demo):
    t = services.get_transaction(demo[4][0], conn=conn)
    sp = services.compute_recipient_spread("@campus_tix_99", t.ts, conn=conn)
    assert sp.distinct_students >= config.CAMPAIGN_MIN_STUDENTS


# ---- Tool 3: update_payment_state ------------------------------------------------------------------------------
def _txn_for(c, handle):
    return c.execute("SELECT txn_id FROM transactions WHERE recipient_handle = ? ORDER BY txn_id LIMIT 1",
                     (handle,)).fetchone()[0]


def test_freezing_a_wallet_is_impossible(fresh):
    tid = _txn_for(fresh, "@campus_tix_99")
    services.update_payment_state(tid, Action.ESCALATE, conn=fresh)
    for bad in ("FREEZE_WALLET", "BLOCK_ACCOUNT", "freeze"):
        with pytest.raises(InvalidTransition, match="never freeze"):
            services.update_payment_state(tid, bad, conn=fresh)
    assert "FREEZE" not in " ".join(a.value for a in Action)


def test_student_confirm_flow_and_identity(fresh):
    tid = _txn_for(fresh, "@new_classmate_sami")
    student = services.get_transaction(tid, conn=fresh).student_id
    rec = services.update_payment_state(tid, Action.HOLD_FOR_CONFIRMATION, conn=fresh)
    assert rec.state == PaymentState.PENDING_CONFIRMATION and rec.awaiting == "STUDENT" and rec.prompts_sent == 1
    assert rec.expires_at is not None
    with pytest.raises(ApprovalRequired):        # another student can't confirm someone else's payment
        services.update_payment_state(tid, Action.STUDENT_CONFIRM, actor="student:STU-0999", conn=fresh)
    now = datetime(2026, 11, 20, 12, 0, 0)
    done = services.update_payment_state(tid, Action.STUDENT_CONFIRM, actor=f"student:{student}", now=now,
                                         conn=fresh)
    assert done.state == PaymentState.CONFIRMED_CANCELLABLE and done.awaiting == "NONE"     # cancel window open
    assert done.expires_at == now + timedelta(seconds=config.CANCEL_WINDOW_SECONDS)
    assert services.release_due(now + timedelta(seconds=60), conn=fresh) == []             # not yet
    assert services.release_due(now + timedelta(minutes=3), conn=fresh) == [tid]
    assert services.get_payment_state(tid, conn=fresh).state == PaymentState.RELEASED
    with pytest.raises(InvalidTransition):       # finished payments can't change again
        services.update_payment_state(tid, Action.STUDENT_CANCEL, actor=f"student:{student}", conn=fresh)


def test_reminders_are_capped_no_loops(fresh):
    tid = _txn_for(fresh, "@new_classmate_sami")
    services.update_payment_state(tid, Action.HOLD_FOR_CONFIRMATION, conn=fresh)
    for _ in range(config.MAX_STUDENT_PROMPTS - 1):
        services.update_payment_state(tid, Action.REMIND, conn=fresh)
    with pytest.raises(InvalidTransition, match="never asks"):
        services.update_payment_state(tid, Action.REMIND, conn=fresh)
    assert services.get_payment_state(tid, conn=fresh).prompts_sent == config.MAX_STUDENT_PROMPTS


def test_unanswered_holds_expire_and_refund(fresh):
    tid = _txn_for(fresh, "@new_classmate_sami")
    services.update_payment_state(tid, Action.HOLD_FOR_CONFIRMATION, conn=fresh)
    assert services.expire_stale_holds(datetime.now(), conn=fresh) == []                    # not due yet
    later = datetime.now() + timedelta(hours=config.HOLD_EXPIRY_HOURS + 1)
    assert services.expire_stale_holds(later, conn=fresh) == [tid]
    assert services.get_payment_state(tid, conn=fresh).state == PaymentState.EXPIRED


def test_staff_actions_need_a_signed_session(fresh, officer_token, officer_passwords):
    from src import auth
    tid = _txn_for(fresh, "@campus_tix_99")
    services.update_payment_state(tid, Action.ESCALATE, conn=fresh)
    with pytest.raises(ApprovalRequired):
        services.update_payment_state(tid, Action.STAFF_RELEASE, conn=fresh)
    other = auth.login("SUP-02", officer_passwords["SUP-02"])
    for tok, name in [("", "SUP-01"), ("garbage", "SUP-01"), (other, "SUP-01")]:
        with pytest.raises(ApprovalRequired):
            services.update_payment_state(tid, Action.STAFF_CANCEL, conn=fresh,
                                          approval=StaffApproval(staff_id=name, session_token=tok))
    rec = services.update_payment_state(tid, Action.STAFF_CANCEL, conn=fresh,
                                        approval=StaffApproval(staff_id="SUP-01", session_token=officer_token))
    assert rec.state == PaymentState.CANCELLED and rec.last_actor == "staff:SUP-01"


def test_student_can_cancel_escalated_but_not_release(fresh):
    tid = _txn_for(fresh, "@campus_tix_99")
    student = services.get_transaction(tid, conn=fresh).student_id
    services.update_payment_state(tid, Action.ESCALATE, conn=fresh)
    with pytest.raises(InvalidTransition):
        services.update_payment_state(tid, Action.STUDENT_CONFIRM, actor=f"student:{student}", conn=fresh)
    assert services.update_payment_state(tid, Action.STUDENT_CANCEL, actor=f"student:{student}",
                                         conn=fresh).state == PaymentState.CANCELLED


def test_independent_guard_refuses_unsafe_auto_clear(fresh):
    with pytest.raises(ApprovalRequired, match="scam reports"):
        services.update_payment_state(_txn_for(fresh, "@campus_tix_99"), Action.CLEAR, conn=fresh)
    with pytest.raises(ApprovalRequired, match="automatic limit"):
        big = fresh.execute("SELECT txn_id FROM transactions WHERE scenario='big_p2p_legit' LIMIT 1").fetchone()[0]
        services.update_payment_state(big, Action.CLEAR, conn=fresh)


def test_audit_log_and_token_never_serialized(fresh, officer_token):
    tid = _txn_for(fresh, "@campus_tix_99")
    services.update_payment_state(tid, Action.ESCALATE, reasons=["REPORTED_RECIPIENT"], conn=fresh)
    last = services.latest_resolution(tid, conn=fresh)
    assert last.action == Action.ESCALATE and last.to_state == PaymentState.ESCALATED and last.actor == "system"
    a = StaffApproval(staff_id="SUP-01", session_token=officer_token)
    assert officer_token not in a.model_dump_json()


def test_scam_broadcast_needs_staff(fresh, officer_token):
    with pytest.raises(ApprovalRequired):
        services.broadcast_scam_alert("@campus_tix_99", "warning", StaffApproval(staff_id="SUP-01"), conn=fresh)
    services.broadcast_scam_alert("@campus_tix_99", "Don't pay @campus_tix_99",
                                  StaffApproval(staff_id="SUP-01", session_token=officer_token), conn=fresh)
    assert services.list_broadcasts(conn=fresh)[0]["staff_id"] == "SUP-01"


# ---- stars (staff bookmarks) -----------------------------------------------------------------------
def test_stars_start_on_the_scenarios_and_can_be_toggled(conn, demo):
    starred = services.list_starred(conn=conn)
    scenario_alerts = {services.get_alert_for_txn(t, conn=conn).record_id for t, _ in demo.values()}
    assert starred == scenario_alerts and len(starred) == 12
    other = next(a.record_id for a in services.list_alerts(conn=conn) if a.record_id not in starred)
    assert services.set_starred([other, "ALR-99999"], True, "SUP-01", conn=conn) == 1   # unknown IDs ignored
    assert other in services.list_starred(conn=conn)
    assert services.set_starred([other], True, "SUP-01", conn=conn) == 0                 # already starred
    assert services.set_starred([other, *scenario_alerts], False, "SUP-01", conn=conn) == 13
    assert services.list_starred(conn=conn) == set()


def test_stars_table_created_for_older_databases(conn):
    conn.execute("DROP TABLE starred")
    conn.commit()
    assert len(services.list_starred(conn=conn)) == 12     # recreated, scenarios starred again
