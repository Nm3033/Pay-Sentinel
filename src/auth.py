"""Support-staff authentication (stdlib only).

- Passwords: PBKDF2-HMAC-SHA256, per-officer random salt, constant-time compare. Never stored in clear.
- Lockout: OFFICER_MAX_FAILED bad attempts => locked for OFFICER_LOCKOUT_MINUTES.
- Sessions: HMAC-SHA256-signed token {officer_id, expiry}; verified by workflow.staff_decide() AND
  again by services.update_payment_state() before any staff action executes.
- Store: data/officers.json (gitignored). Create with:  python -m src.auth init

CLI:
  python -m src.auth init [--force]        create the premade staff accounts with random passwords (printed once)
  python -m src.auth set-password SUP-01   set your own password (prompted, hidden)
  python -m src.auth list | unlock SUP-01 | disable SUP-01
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path

from src import config

PREMADE_OFFICERS = {  # the brief's two-person campus support team (fictional)
    "SUP-01": "Campus Support Lead",
    "SUP-02": "Campus Support Agent",
}
_ITER = 200_000
MIN_PASSWORD_LEN = 10


class AuthError(Exception):
    """Authentication failed (message is safe to show to users)."""


# ---- store ------------------------------------------------------------------------------------------
def _path() -> Path:
    return Path(config.OFFICERS_PATH)


def store_exists() -> bool:
    return _path().exists()


def ensure_store_from_env() -> bool:
    """Hosting support (e.g. Streamlit Community Cloud, where you can't run the CLI):
    if no account store exists yet, create the premade staff accounts from secrets/env vars
    STAFF_PASSWORD_SUP_01, STAFF_PASSWORD_SUP_02 (or one STAFF_PASSWORD for all). Passwords are
    hashed on creation; they are never written to disk in clear. Returns True if a store was created."""
    if store_exists():
        return False
    shared = os.getenv("STAFF_PASSWORD", "")
    pw = {oid: os.getenv(f"STAFF_PASSWORD_{oid.replace('-', '_')}", shared) for oid in PREMADE_OFFICERS}
    if not all(len(p) >= MIN_PASSWORD_LEN for p in pw.values()):
        return False
    _save(dict(token_secret=secrets.token_hex(32),
               officers={oid: _record(t, pw[oid]) for oid, t in PREMADE_OFFICERS.items()}))
    return True


def _load() -> dict:
    if not store_exists():
        raise AuthError("Officer store not initialised. Run: python -m src.auth init")
    return json.loads(_path().read_text(encoding="utf-8"))


def _save(store: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=1), encoding="utf-8")
    os.replace(tmp, p)  # atomic


def _hash(password: str, salt: bytes, iterations: int = _ITER) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()


def _record(title: str, password: str) -> dict:
    salt = secrets.token_bytes(16)
    return dict(title=title, salt=salt.hex(), hash=_hash(password, salt), iterations=_ITER,
                active=True, failed=0, locked_until=0)


def init_store(force: bool = False) -> dict[str, str]:
    """Create the store with the premade officers; returns {officer_id: generated_password}."""
    if store_exists() and not force:
        raise AuthError(f"{_path()} already exists (use --force to replace it)")
    passwords = {oid: secrets.token_urlsafe(12) for oid in PREMADE_OFFICERS}
    _save(dict(token_secret=secrets.token_hex(32),
               officers={oid: _record(t, passwords[oid]) for oid, t in PREMADE_OFFICERS.items()}))
    return passwords


def set_password(officer_id: str, password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LEN} characters")
    store = _load()
    if officer_id not in store["officers"]:
        raise AuthError(f"Unknown officer {officer_id}")
    title = store["officers"][officer_id]["title"]
    store["officers"][officer_id] = _record(title, password)
    _save(store)


def list_officers() -> list[dict]:
    if not store_exists():
        return []
    now = time.time()
    return [dict(officer_id=oid, title=r["title"], active=r["active"], locked=r["locked_until"] > now)
            for oid, r in sorted(_load()["officers"].items())]


# ---- login & sessions ------------------------------------------------------------------------------
def login(officer_id: str, password: str) -> str:
    """Verify credentials and return a signed session token. Generic error messages on purpose."""
    store = _load()
    rec = store["officers"].get(officer_id)
    now = time.time()
    if rec is None or not rec["active"]:
        _hash(password, b"dummy-salt-0000", _ITER)  # equalise timing for unknown IDs
        raise AuthError("Invalid staff ID or password")
    if rec["locked_until"] > now:
        mins = int((rec["locked_until"] - now) // 60) + 1
        raise AuthError(f"Account locked after repeated failures. Try again in {mins} min.")
    ok = hmac.compare_digest(_hash(password, bytes.fromhex(rec["salt"]), rec["iterations"]), rec["hash"])
    if not ok:
        rec["failed"] += 1
        if rec["failed"] >= config.OFFICER_MAX_FAILED:
            rec["failed"], rec["locked_until"] = 0, now + config.OFFICER_LOCKOUT_MINUTES * 60
        _save(store)
        raise AuthError("Invalid staff ID or password")
    if rec["failed"]:
        rec["failed"] = 0
        _save(store)
    return _issue(store["token_secret"], officer_id)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _issue(secret: str, officer_id: str) -> str:
    payload = _b64(json.dumps({"sub": officer_id, "exp": int(time.time() + config.OFFICER_SESSION_MINUTES * 60),
                               "jti": secrets.token_hex(8)}).encode())
    sig = hmac.new(bytes.fromhex(secret), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_token(token: str) -> str:
    """Return the officer_id for a valid, unexpired token of an active officer; else raise AuthError."""
    try:
        payload, sig = (token or "").split(".")
        store = _load()
        good = hmac.new(bytes.fromhex(store["token_secret"]), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except AuthError:
        raise
    except Exception:  # noqa: BLE001 - any malformed token is simply invalid
        raise AuthError("Invalid session: please sign in again") from None
    if claims["exp"] < time.time():
        raise AuthError("Session expired: please sign in again")
    rec = store["officers"].get(claims["sub"])
    if rec is None or not rec["active"]:
        raise AuthError("Officer account disabled")
    return claims["sub"]


def token_expiry(token: str) -> int:
    payload = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]


# ---- CLI -------------------------------------------------------------------------------------------
def _admin(officer_id: str, **changes) -> None:
    store = _load()
    if officer_id not in store["officers"]:
        raise AuthError(f"Unknown officer {officer_id}")
    store["officers"][officer_id].update(changes)
    _save(store)


def main() -> None:
    ap = argparse.ArgumentParser(description="Pay Sentinel officer accounts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--force", action="store_true")
    for name in ("set-password", "unlock", "disable"):
        sub.add_parser(name).add_argument("officer_id")
    sub.add_parser("list")
    a = ap.parse_args()
    try:
        if a.cmd == "init":
            pw = init_store(a.force)
            print(f"Created {_path()}\n\nInitial passwords. Shown ONCE; store them safely, or change them with set-password:\n")
            for oid, secret in pw.items():
                print(f"  {oid:<8} {PREMADE_OFFICERS[oid]:<36} {secret}")
        elif a.cmd == "set-password":
            p1 = getpass.getpass(f"New password for {a.officer_id}: ")
            if p1 != getpass.getpass("Repeat: "):
                raise AuthError("Passwords do not match")
            set_password(a.officer_id, p1)
            print("Password updated.")
        elif a.cmd == "unlock":
            _admin(a.officer_id, failed=0, locked_until=0); print("Unlocked.")
        elif a.cmd == "disable":
            _admin(a.officer_id, active=False); print("Disabled.")
        else:
            for o in list_officers():
                print(f"  {o['officer_id']:<8} {o['title']:<36} active={o['active']} locked={o['locked']}")
    except AuthError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
