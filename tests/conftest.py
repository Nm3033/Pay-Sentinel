import pytest

from src import auth, config, seed_data, services


@pytest.fixture(scope="session", autouse=True)
def officer_passwords(tmp_path_factory):
    """Isolated staff-account store for the whole test run; returns {staff_id: password}."""
    original = config.OFFICERS_PATH
    config.OFFICERS_PATH = tmp_path_factory.mktemp("auth") / "officers.json"
    pw = auth.init_store()
    yield pw
    config.OFFICERS_PATH = original


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
