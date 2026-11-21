"""Round 3 dress rehearsal: the 7-minute stage path from DEMO.md, in order and on ONE shared database, starting from
the Start page's buttons exactly as on stage. Fake AI providers only (never the real ones)."""
import pytest

from src import config, seed_data, services, workflow
from src.llm import FallbackLLM
from src.schemas import PaymentState
from tests.test_resilience import Named


@pytest.fixture
def fresh_db(tmp_path):
    p = tmp_path / "demo.sqlite"
    seed_data.build_database(p, seed=42)
    return p


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch, fresh_db):
    monkeypatch.setattr(workflow, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", fresh_db)


def _start(officer_passwords):
    from tests.test_curveballs import _app
    at = _app(officer_passwords)
    at.session_state["use_llm"] = True
    return at


def test_round3_stage_path(fresh_db, officer_passwords, monkeypatch):
    import src.llm
    fakes = FallbackLLM([Named("openai/gpt-oss-120b"), Named("qwen/qwen3.8-27b")])
    monkeypatch.setattr(src.llm, "get_llm", lambda: fakes)

    # Session 1 · Add payments. Area 2: five back to back with the main AI down and one broken input
    at = _start(officer_passwords)
    at.button(key="try5").click().run(); assert not at.exception, at.exception
    at.selectbox(key="ai_mode").set_value("primary").run()
    at.button(key="btb_go").click().run(); assert not at.exception, at.exception
    assert any("4 of 5 payments checked" in w.value for w in at.warning)
    assert any("backup AI (qwen/qwen3.8-27b) took over" in m.value for m in at.markdown)
    at.selectbox(key="ai_mode").set_value("normal").run(); assert not at.exception, at.exception
    assert not any("Failure test on" in w.value for w in at.sidebar.warning)      # switched back off

    # ... same session. Area 1: type a payment -> journey -> staff hand-off -> cancel -> back -> outcome + report
    next(b for b in at.button if b.label.startswith("Use example: Ticket scam")).click().run()
    next(b for b in at.button if b.label == "➕ Add payment").click().run(); assert not at.exception, at.exception
    at.button(key="follow_new").click().run(); assert not at.exception, at.exception
    at.button(key="j_staff").click().run(); assert not at.exception, at.exception
    at.button(key="STAFF_CANCEL").click().run(); assert not at.exception, at.exception
    at.button(key="journey_back").click().run(); assert not at.exception, at.exception
    assert any("Sent to the student" in s.value for s in at.success)

    # Session 2 · Start page -> Safety evidence. Area 3: the three cases, live, each with its log entry
    at = _start(officer_passwords)
    at.button(key="try9").click().run(); assert not at.exception, at.exception
    at.button(key="run_safety_cases").click().run(); assert not at.exception, at.exception
    m = {x.label: int(x.value) for x in at.metric
         if x.label in ("✅ Safe → automatic", "⏸️ Risky → paused", "🛡️ Attacks blocked")}
    assert len(m) == 3 and all(v >= 1 for v in m.values())

    # Session 3 · Start page -> Pilot plan. Area 4: priced from what was just measured
    at = _start(officer_passwords)
    at.button(key="try10").click().run(); assert not at.exception, at.exception
    labels = {x.label: x.value for x in at.metric}
    assert labels["AI cost per payment"].startswith("$") and labels["Per month (22 school days)"].startswith("$")

    # nothing unsafe happened along the way: no payment to the scam account was ever sent
    conn = services.connect(fresh_db)
    sent = conn.execute("SELECT COUNT(*) FROM payment_states p JOIN transactions t ON t.txn_id = p.txn_id "
                        "WHERE t.recipient_handle = '@campus_tix_99' AND p.state IN (?, ?, ?)",
                        (PaymentState.CLEARED.value, PaymentState.SENT.value, PaymentState.RELEASED.value)
                        ).fetchone()[0]
    conn.close()
    assert sent == 0
