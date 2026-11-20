"""Lookalike-vendor check (pure Python; no LLM).

Scammers register handles that imitate verified campus shops: "@campus_canteen_", "@CampusCanteen0",
"@campus.canteen". We normalise every handle (lowercase, strip symbols, swap look-alike digits for letters)
and compare it with the verified vendor list. A close match that is NOT the exact verified handle is high risk.
"""
from __future__ import annotations

import re

# digits / symbols commonly used to imitate letters
_SWAPS = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "$": "s", "@": "a"})
MIN_LEN = 5            # very short names are too generic to compare
MAX_EDITS = 2          # "campuscanteeno" vs "campuscanteen" = 1 edit


def normalise(handle: str) -> str:
    """'@CampusCanteen0' -> 'campuscanteeno'; '@campus.canteen' -> 'campuscanteen'."""
    h = (handle or "").strip().lower().lstrip("@")
    return re.sub(r"[^a-z]", "", h.translate(_SWAPS))


def _edits(a: str, b: str) -> int:
    """Levenshtein distance (small strings only)."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def find_lookalike(handle: str, verified_handles: list[str]) -> tuple[str, float] | None:
    """Return (verified handle it imitates, similarity 0..1) or None. Exact verified handles are never lookalikes."""
    h = (handle or "").strip().lower()
    if h in {v.lower() for v in verified_handles}:
        return None
    n = normalise(h)
    if len(n) < MIN_LEN:
        return None
    best: tuple[str, float] | None = None
    for v in verified_handles:
        nv = normalise(v)
        if len(nv) < MIN_LEN:
            continue
        d = _edits(n, nv)
        contains = (nv in n or n in nv) and abs(len(n) - len(nv)) <= 8      # "campuscanteenofficial"
        if d <= MAX_EDITS or contains:
            score = round(1 - d / max(len(n), len(nv)), 3)
            if best is None or score > best[1]:
                best = (v, score)
    return best
