import pytest

from src import auth, config, seed_data, services


_TEST_OFFICERS: dict = {}


@pytest.fixture(scope="session", autouse=True)
def officer_passwords(tmp_path_factory):
    """Isolated staff-account store for the whole test run; returns {staff_id: password}."""
    original = config.OFFICERS_PATH
    config.OFFICERS_PATH = _TEST_OFFICERS["path"] = tmp_path_factory.mktemp("auth") / "officers.json"
    pw = auth.init_store()
    yield pw
    config.OFFICERS_PATH = original


@pytest.fixture(autouse=True)
def _never_touch_real_data(officer_passwords, db_path, monkeypatch):
    """Safety net: before EVERY test, point accounts and data at the private test copies, so no test (even one
    that misbehaves) can ever reach the real data/officers.json or the real database."""
    monkeypatch.setattr(config, "OFFICERS_PATH", config.OFFICERS_PATH if "pytest" in str(config.OFFICERS_PATH)
                        else _TEST_OFFICERS["path"])
    monkeypatch.setattr(config, "DB_PATH", db_path)


@pytest.fixture(autouse=True)
def _never_call_real_ai(monkeypatch):
    """Safety net: no test may reach a real AI provider (cost, quota, flaky network). Tests use tests/fakes.py;
    any real chat call fails loudly instead of silently spending the team's API quota."""
    from openai.resources.chat.completions import Completions

    def blocked(*a, **kw):
        raise RuntimeError("tests must not call a real AI provider; use tests/fakes.py")
    monkeypatch.setattr(Completions, "create", blocked)


@pytest.fixture
def officer_token(officer_passwords):
    return auth.login("SUP-01", officer_passwords["SUP-01"])


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "test.sqlite"
    seed_data.build_database(path, seed=42)
    return path


@pytest.fixture
def conn(db_path):
    c = services.connect(db_path)
    yield c
    c.close()


@pytest.fixture
def scenario_txns(conn):
    """Evaluation-only lookup (tests may read ground truth; agents never do)."""
    def _get(scenario_prefix: str) -> list[str]:
        rows = conn.execute("SELECT txn_id FROM transactions WHERE scenario LIKE ? ORDER BY txn_id",
                            (scenario_prefix + "%",)).fetchall()
        return [r[0] for r in rows]
    return _get


@pytest.fixture
def demo(conn):
    """{ordinal: (txn_id, expected_route)} — evaluation-only."""
    return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT ordinal, txn_id, expected_route FROM demo_scenarios")}
