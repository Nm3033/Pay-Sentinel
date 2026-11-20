"""Checking several payments at the same time (workflow.triage_many) + AI rate-limit fail-fast."""
import time

import pytest

from src import config, seed_data, services, workflow
from src.schemas import PaymentState
from tests.fakes import FakeLLM


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "p.sqlite"
    seed_data.build_database(p, seed=42)
    monkeypatch.setattr(config, "DB_PATH", p)
    monkeypatch.setattr(workflow, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path)
    return p


def _alerts(n=None):
    return [a.record_id for a in services.list_alerts(limit=n)]


def test_parallel_matches_sequential(db):
    ids = _alerts(60)
    seq = {c.alert.record_id: c.final_route for c in
           (workflow.run_case(a, use_llm=False, commit=False, save=False) for a in ids)}
    par, errors = workflow.triage_many(ids, workers=6, use_llm=False)
    assert not errors and [c.alert.record_id for c in par] == ids          # original order kept
    assert {c.alert.record_id: c.final_route for c in par} == seq           # same decisions
    states = services.list_payment_states()
    assert len(states) == 60 and len({s.txn_id for s in states}) == 60     # every payment saved exactly once
    assert len(services.list_resolutions(limit=1000)) == 60                # one audit row each


def test_duplicates_and_finished_payments_are_skipped(db, officer_token):
    ids = _alerts(8)
    first, _ = workflow.triage_many(ids + ids[:3], workers=4, use_llm=False)   # duplicates in the request
    assert len(first) == 8
    escalated = [c for c in first if c.state == PaymentState.ESCALATED]
    pending = [c for c in first if c.state == PaymentState.PENDING_CONFIRMATION]
    again, errors = workflow.triage_many(ids, workers=4, use_llm=False)
    finished = {c.alert.record_id for c in first if c.state == PaymentState.CLEARED}
    assert not errors and not finished & {c.alert.record_id for c in again}   # cleared ones not re-run
    for c in again:                                                             # waiting ones: no second prompt
        assert services.get_payment_state(c.transaction.record_id).prompts_sent <= 1
    assert len(services.list_resolutions(limit=1000)) == 8                     # nothing re-committed


def test_one_failure_does_not_stop_the_batch(db, monkeypatch):
    ids = _alerts(10)
    bad = ids[3]
    real = workflow.run_case

    def flaky(alert_id, **kw):
        if alert_id == bad:
            raise RuntimeError("boom")
        return real(alert_id, **kw)
    monkeypatch.setattr(workflow, "run_case", flaky)
    seen = []
    cases, errors = workflow.triage_many(ids, workers=3, use_llm=False,
                                         on_done=lambda a, c, e, d, t: seen.append((a, d, t)))
    assert len(cases) == 9 and list(errors) == [bad] and "boom" in errors[bad]
    assert sorted(d for _, d, _ in seen) == list(range(1, 11)) and {t for *_, t in seen} == {10}


def test_ai_usage_never_mixes_between_parallel_payments(db):
    ids = _alerts()
    msg_alerts = [a for a in ids if services.get_transaction(services.get_alert(a).txn_id).message_id][:20]
    plain = [a for a in ids if not services.get_transaction(services.get_alert(a).txn_id).message_id
             and not services.get_transaction(services.get_alert(a).txn_id).memo][:10]
    llm = FakeLLM()
    cases, errors = workflow.triage_many(msg_alerts + plain, workers=8, llm=llm, commit=False, save=False)
    assert not errors
    by_id = {c.alert.record_id: c for c in cases}
    for a in plain:     # no text -> no sanitizer/assessor; cleared ones need no writer
        assert all(u["purpose"] == "writer" for u in by_id[a].llm_usage)
    for a in msg_alerts:
        assert sum(u["purpose"] == "assessor" for u in by_id[a].llm_usage) == 1   # exactly its own call
    assert sum(len(c.llm_usage) for c in cases) == len(llm.usage_log)          # nothing lost or double-counted


def test_quota_exhausted_fails_fast(monkeypatch):
    """A 'try again in 10 minutes' answer stops further AI calls instead of retrying for minutes."""
    import openai
    from src.llm import LLMClient, LLMError

    client = LLMClient(base_url="http://localhost:1", api_key="x")
    calls = []

    class FakeResp:
        status_code, headers, request = 429, {}, None

    def create(**kw):
        calls.append(1)
        raise openai.RateLimitError("Rate limit reached ... Please try again in 10m1.3s.",
                                    response=type("R", (), {"status_code": 429, "headers": {}, "request": None})(),
                                    body=None)
    monkeypatch.setattr(client._client.chat.completions, "create", create)
    t0 = time.perf_counter()
    with pytest.raises(LLMError, match="rate limited for 10 min"):
        client._complete([{"role": "user", "content": "hi"}], "assessor")
    with pytest.raises(LLMError, match="quota exhausted"):     # second call: no API request at all
        client._complete([{"role": "user", "content": "hi"}], "assessor")
    assert len(calls) == 1 and time.perf_counter() - t0 < 2
