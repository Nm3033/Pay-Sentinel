import pytest

from src import config, seed_data, services, workflow
from src.schemas import Route
from src.telemetry import compute_telemetry


@pytest.fixture(scope="module")
def intake_db(tmp_path_factory):
    """Own database: these tests insert rows, which must not leak into the shared dataset."""
    path = tmp_path_factory.mktemp("intake") / "intake.sqlite"
    seed_data.build_database(path, seed=42)
    return path


@pytest.fixture
def conn(intake_db):
    c = services.connect(intake_db)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path, intake_db):
    monkeypatch.setattr(config, "DB_PATH", intake_db)
    monkeypatch.setattr(workflow, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path)


GOOD = dict(student_id="stu-0020", amount_aed="12.50", txn_type="purchase", channel="qr",
            recipient_handle="Campus_Canteen", message="", memo="lunch", device="Known")
SCAM = dict(student_id="STU-0021", amount_aed="50", txn_type="P2P", channel="LINK", recipient_handle="@campus_tix_99",
            message=seed_data.BRIEF_SCAM_MESSAGE)


def test_validation_normalizes_and_reports_errors(conn):
    rows = [GOOD,
            dict(GOOD, student_id="STU-9999"),                 # unknown student
            dict(GOOD, amount_aed="-5"),                       # negative
            dict(GOOD, txn_type="CRYPTO"),                     # bad enum
            dict(GOOD, recipient_handle="@a"),                 # handle too short
            dict(GOOD, message="x" * 501),                     # too long
            dict(GOOD, amount_aed="6000"),                     # above wallet limit
            dict(GOOD, unexpected_column="ignored", memo=float("nan"))]
    valid, errors = services.validate_new_transactions(rows, conn=conn)
    assert len(valid) == 2 and [e["row"] for e in errors] == [2, 3, 4, 5, 6, 7]
    v = valid[0]
    assert v.student_id == "STU-0020" and v.recipient_handle == "@campus_canteen" and v.txn_type == "PURCHASE"
    assert v.device == "known" and v.amount_aed == 12.5


def test_row_limit(conn):
    _, errors = services.validate_new_transactions([GOOD] * (services.INTAKE_MAX_ROWS + 1), conn=conn)
    assert errors and "limit" in errors[0]["error"]


def test_add_creates_txn_alert_message_and_flows_through(conn):
    items, _ = services.validate_new_transactions([GOOD, SCAM], conn=conn)
    out = services.add_transactions(items, "SUP-01", conn=conn)
    snack = services.get_transaction_context(out[0]["txn_id"], conn=conn)
    scam = services.get_transaction_context(out[1]["txn_id"], conn=conn)
    assert snack.message is None and scam.message.text == seed_data.BRIEF_SCAM_MESSAGE
    assert services.get_alert(out[0]["alert_id"], conn=conn).legacy_rule == "MANUAL_ENTRY"
    c1 = workflow.run_case(out[0]["alert_id"], use_llm=False, conn=conn, commit=False)
    c2 = workflow.run_case(out[1]["alert_id"], use_llm=False, conn=conn, commit=False)
    assert c1.final_route == Route.CLEAR and c2.final_route == Route.ESCALATE
    t = compute_telemetry([c1, c2], conn=conn)
    assert t.unlabeled == 2 and t.safe_total == 0 and t.scams_total == 0


def test_unknown_recipient_and_new_phone_hold(conn):
    items, _ = services.validate_new_transactions([dict(GOOD, txn_type="P2P", recipient_handle="@brand_new_person",
                                                        device="new", memo="")], conn=conn)
    out = services.add_transactions(items, "SUP-01", conn=conn)
    c = workflow.run_case(out[0]["alert_id"], use_llm=False, conn=conn, commit=False)
    assert c.final_route == Route.HOLD_FOR_CONFIRMATION and not c.recipient.found


def test_add_is_atomic(conn):
    items, _ = services.validate_new_transactions([GOOD, GOOD], conn=conn)
    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    items[1] = None
    with pytest.raises(Exception):
        services.add_transactions(items, "SUP-01", conn=conn)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before
