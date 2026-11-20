import json
import time

import pytest

from src import auth, config, workflow


def test_premade_officers_exist(officer_passwords):
    ids = [o["officer_id"] for o in auth.list_officers()]
    assert ids == sorted(auth.PREMADE_OFFICERS) and set(officer_passwords) == set(ids)


def test_passwords_are_hashed_not_stored(officer_passwords):
    raw = config.OFFICERS_PATH.read_text(encoding="utf-8")
    assert all(pw not in raw for pw in officer_passwords.values())
    rec = json.loads(raw)["officers"]["SUP-01"]
    assert len(rec["salt"]) == 32 and len(rec["hash"]) == 64 and rec["iterations"] >= 100_000


def test_login_ok_and_token_roundtrip(officer_passwords):
    tok = auth.login("SUP-02", officer_passwords["SUP-02"])
    assert auth.verify_token(tok) == "SUP-02"
    assert auth.token_expiry(tok) > time.time()


@pytest.mark.parametrize("oid,pw", [("SUP-01", "wrong-password"), ("CO-999", "whatever123")])
def test_bad_credentials_generic_error(oid, pw):
    with pytest.raises(auth.AuthError, match="Invalid staff ID or password"):
        auth.login(oid, pw)


def test_lockout_after_repeated_failures(officer_passwords, monkeypatch):
    monkeypatch.setattr(config, "OFFICER_MAX_FAILED", 3)
    for _ in range(3):
        with pytest.raises(auth.AuthError):
            auth.login("SUP-02", "nope-nope-nope")
    with pytest.raises(auth.AuthError, match="locked"):  # even the right password is refused
        auth.login("SUP-02", officer_passwords["SUP-02"])
    auth._admin("SUP-02", failed=0, locked_until=0)  # unlock for other tests
    assert auth.verify_token(auth.login("SUP-02", officer_passwords["SUP-02"])) == "SUP-02"


def test_expired_and_tampered_tokens(officer_passwords, monkeypatch):
    tok = auth.login("SUP-01", officer_passwords["SUP-01"])
    payload, sig = tok.split(".")
    tampered = auth._b64(json.dumps({"sub": "SUP-02", "exp": 9_999_999_999, "jti": "x"}).encode()) + "." + sig
    with pytest.raises(auth.AuthError):
        auth.verify_token(tampered)
    monkeypatch.setattr(config, "OFFICER_SESSION_MINUTES", -1)
    with pytest.raises(auth.AuthError, match="expired"):
        auth.verify_token(auth.login("SUP-01", officer_passwords["SUP-01"]))


def test_set_password_policy(officer_passwords):
    with pytest.raises(auth.AuthError, match="at least"):
        auth.set_password("SUP-02", "short")
    auth.set_password("SUP-02", "a-much-longer-passphrase")
    assert auth.verify_token(auth.login("SUP-02", "a-much-longer-passphrase")) == "SUP-02"
    auth.set_password("SUP-02", officer_passwords["SUP-02"])  # restore


def test_staff_decide_requires_session(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow, "CASES_DIR", tmp_path)
    with pytest.raises(auth.AuthError):
        workflow.staff_decide("CASE-00001", "STAFF_RELEASE", "not-a-token")


def test_accounts_from_env_on_fresh_server(tmp_path, monkeypatch):
    """Hosting: no CLI available, so accounts are created from STAFF_PASSWORD_* secrets (hashed)."""
    monkeypatch.setattr(config, "OFFICERS_PATH", tmp_path / "officers.json")
    monkeypatch.delenv("STAFF_PASSWORD", raising=False)
    monkeypatch.setenv("STAFF_PASSWORD_SUP_01", "short")                 # too short -> nothing created
    assert not auth.ensure_store_from_env() and not auth.store_exists()
    monkeypatch.setenv("STAFF_PASSWORD_SUP_01", "judges-demo-pass-01")
    monkeypatch.setenv("STAFF_PASSWORD_SUP_02", "judges-demo-pass-02")
    assert auth.ensure_store_from_env() and not auth.ensure_store_from_env()   # only once
    assert "judges-demo-pass" not in (tmp_path / "officers.json").read_text(encoding="utf-8")
    assert auth.verify_token(auth.login("SUP-02", "judges-demo-pass-02")) == "SUP-02"
