"""Report mail format shared by every mail source (IMAP now, Gmail connector/API in tests and later).

One JSON file per message in work/mail/<key>.json:

    {"key", "message_id", "source", "subject", "from", "date",
     "forwarded": {"from", "date", "subject"} | null,     # inner message of a forward, if any
     "effective_from", "effective_date",                   # inner if forwarded, else outer
     "body_text", "attachments": [{"filename", "mime_type", "text"}],
     "content_hash"}

Read-only helpers; nothing here talks to a mailbox.
"""
from __future__ import annotations

import email.utils
import hashlib
import html
import re
from typing import Any

# "Von: x@y" / "From: x@y" inside a forwarded block. Apple Mail, Gmail, Outlook, German and English.
_FWD_MARKERS = re.compile(
    r"(anfang der weitergeleiteten nachricht|begin forwarded message|-{5,}\s*(forwarded message|weitergeleitete nachricht)\s*-{5,}"
    r"|urspr[üu]ngliche nachricht|original message)",
    re.IGNORECASE)
_HDR = re.compile(r"^[>\s*]*(von|from|betreff|subject|datum|date|gesendet|sent)\s*:\s*\**\s*(.+?)\s*\**\s*$",
                  re.IGNORECASE | re.MULTILINE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def address(s: str | None) -> str:
    """'Name <a@b.de>' -> 'a@b.de' (lowercase)."""
    if not s:
        return ""
    name, addr = email.utils.parseaddr(s)
    if not addr or "@" not in addr:
        m = _EMAIL.search(s)
        addr = m.group(0) if m else ""
    return addr.strip().lower()


def detect_forward(body: str) -> dict | None:
    """Find the headers of a forwarded message in a plain-text body. Returns {from, date, subject} or None."""
    if not body:
        return None
    m = _FWD_MARKERS.search(body)
    if not m:
        return None
    block = body[m.end(): m.end() + 1500]
    out: dict[str, str] = {}
    for h in _HDR.finditer(block):
        key = h.group(1).lower()
        val = h.group(2).strip()
        if key in ("von", "from") and "from" not in out:
            out["from"] = val
        elif key in ("betreff", "subject") and "subject" not in out:
            out["subject"] = val
        elif key in ("datum", "date", "gesendet", "sent") and "date" not in out:
            out["date"] = val
    if "from" not in out or not address(out["from"]):
        return None
    out["from"] = address(out["from"])
    return out


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", s)
    s = re.sub(r"(?i)</t[dh]>", "\t", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[  ]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def content_hash(sender: str, date: str, body: str, attachments: list[dict]) -> str:
    """Fallback identity when a message has no usable Message-ID: sender + date + normalized content."""
    norm = re.sub(r"\s+", " ", (body or "")).strip().lower()
    att = "|".join(f"{a.get('filename')}:{re.sub(r'\s+', ' ', a.get('text') or '')}" for a in attachments)
    return hashlib.sha256(f"{sender}|{date}|{norm}|{att}".encode()).hexdigest()[:16]


def safe_key(message_id: str) -> str:
    k = re.sub(r"[^A-Za-z0-9._-]+", "_", message_id.strip("<>"))
    return k[:80] or "no-id"


def normalize_message(*, message_id: str | None, source: str, subject: str, sender: str, date: str,
                      body_text: str, attachments: list[dict] | None = None,
                      inner: dict | None = None) -> dict[str, Any]:
    """Build the canonical record. `inner` = headers of an attached original (message/rfc822), if any;
    otherwise a forward is detected from the body text."""
    attachments = attachments or []
    fwd = inner or detect_forward(body_text)
    eff_from = (fwd or {}).get("from") or address(sender)
    eff_date = (fwd or {}).get("date") or date
    h = content_hash(eff_from, eff_date, body_text, attachments)
    mid = (message_id or "").strip() or f"hash:{h}"
    return {
        "key": safe_key(mid),
        "message_id": mid,
        "source": source,
        "subject": subject or "",
        "from": address(sender),
        "date": date,
        "forwarded": fwd,
        "effective_from": eff_from,
        "effective_date": eff_date,
        "body_text": body_text or "",
        "attachments": attachments,
        "content_hash": h,
    }


def provider_from_sender(addr: str, settings: dict) -> str | None:
    """Provider key from a sender address via settings.providers.*.sender_hints, else None (unknown)."""
    a = (addr or "").lower()
    if not a:
        return None
    for key, spec in settings["providers"].items():
        for hint in spec.get("sender_hints") or []:
            if hint.lower() in a:
                return key
    for hint in settings.get("review_senders", []):
        if hint.lower() in a:
            return "review:" + hint
    return None
