"""Deterministic scam & slang scanner for chat messages and payment notes (pure Python; no LLM).

It does two jobs the gate relies on:
1. Spot well-known scam patterns (urgency, phishing links, impersonating PocketPay, too-good deals...).
2. List slang terms the code cannot interpret, so a message full of them is treated as UNCERTAIN
   and escalated instead of guessed (the AI assessor may still read it; low AI confidence also escalates).
"""
from __future__ import annotations

import re
import unicodedata

from src.schemas import MessageSignals

SAFE_LINK_DOMAINS = {"pocketpay.app", "campus-portal.school"}   # official PocketPay / school sites

_PATTERNS: list[tuple[str, re.Pattern]] = [(n, re.compile(p, re.I)) for n, p in [
    ("URGENCY", r"\b(urgent|asap|right now|hurry|quick(ly)?|fast|last chance|only today|tonight only|"
                r"first come|before (i|someone|they) (give|sell)|expires? (soon|today|in))\b"),
    ("CLICK_BAIT", r"\b(click (here|the link|this)|tap (here|the link)|claim (it|your|now|here)?|"
                   r"verify (at|here|now|your)|log ?in (at|here))\b"),
    ("IMPERSONATION", r"\b(pocket ?pay (support|security|team|admin|help ?desk)|"
                      r"(your )?(wallet|account) (is|has been|was) (locked|suspended|frozen|limited)|"
                      r"school (admin|office|finance)|student council official)\b"),
    ("TOO_GOOD", r"\b(free|giveaway|you('ve)? won|winner|\d{2}% off|half price|discounted|"
                 r"super cheap|airpods|front row)\b"),
    ("PRESSURE", r"\b(don'?t tell|keep (it|this) (between us|secret)|no refunds?|send (it )?first|"
                 r"pay first|deposit (now|first)|or (i|we) (sell|give) it)\b"),
    ("EXAM_LEAK", r"\b((exam|final|test|quiz|midterm)s? (paper|answers?|questions?)|answers? (are |got )?leaked|"
                  r"leaked (paper|answers?|exam|questions?))\b"),
]]
_LINK = re.compile(r"(https?://[^\s]+|www\.[^\s]+|\b[a-z0-9-]+\.(?:co|com|net|xyz|io|me|link|site|app|"
                   r"school|ly)(?:/[^\s]*)?)", re.I)

# Slang the rules can't interpret reliably. Several of these in one message => uncertain.
UNCLEAR_SLANG = ["zesty", "drip", "plug", "slide", "skrrt", "bussin", "lowkey", "ngl", "fr fr", "sesh",
                 "bread", "rizz", "finna", "opp", "sauce", "yeet", "gyat", "fanum", "the fit", "on god",
                 "tuck shop runner", "whole vibe", "say less", "bag secured", "dub"]
_SLANG = [(t, re.compile(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", re.I)) for t in UNCLEAR_SLANG]


def _domain(link: str) -> str:
    d = re.sub(r"^https?://", "", link.lower()).split("/")[0]
    return d[4:] if d.startswith("www.") else d


def scan_text(*texts: str) -> MessageSignals:
    text = unicodedata.normalize("NFKC", " ".join(t for t in texts if t)).replace("​", "")
    signals = [name for name, rx in _PATTERNS if rx.search(text)]
    links = [m.group(0).rstrip(".,!?)") for m in _LINK.finditer(text)]
    if any(_domain(l) not in SAFE_LINK_DOMAINS for l in links):
        signals.append("PHISHING_LINK")
    unclear = [t for t, rx in _SLANG if rx.search(text)]
    return MessageSignals(signals=signals, unclear_terms=unclear, links=links)


SIGNAL_TEXT = {
    "URGENCY": "pressure to pay fast", "CLICK_BAIT": "a “click here / claim” link", "PHISHING_LINK": "an unofficial link",
    "IMPERSONATION": "pretends to be PocketPay or the school", "TOO_GOOD": "a deal that's too good to be true",
    "PRESSURE": "pushes you to pay first or keep it secret", "EXAM_LEAK": "offers leaked exam answers",
}
