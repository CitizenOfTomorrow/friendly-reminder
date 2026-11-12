"""Import mails read through the Gmail connector into work/mail/ (same format as fetch_reports.py).

    python scripts/import_gmail_json.py work/gmail_raw/*.json

Each input file holds what the connector returned for one message:
    {"id", "subject", "sender", "date", "plaintextBody", "attachments": [{"filename", "mimeType", "text"}]}
The Gmail message id is used as message_id ("gmail:<id>"), since the connector doesn't expose the
RFC Message-ID header. Read-only with respect to the mailbox: this only reads local files.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from common import ROOT, load_settings, rel
from fetch_reports import write_mail
from mail_model import normalize_message


def convert(raw: dict) -> dict:
    atts = [{"filename": a.get("filename", ""), "mime_type": a.get("mimeType", ""), "text": a.get("text", "")}
            for a in raw.get("attachments") or []]
    rec = normalize_message(message_id=f"gmail:{raw['id']}", source="gmail-connector",
                            subject=raw.get("subject", ""), sender=raw.get("sender", ""),
                            date=raw.get("date", ""), body_text=raw.get("plaintextBody", ""), attachments=atts)
    if raw.get("note"):
        rec["import_note"] = raw["note"]
    return rec


def main(argv: list[str]) -> int:
    settings = load_settings()
    mail_dir = ROOT / settings["mail_dir"]
    n = 0
    for p in argv:
        rec = convert(json.loads(Path(p).read_text(encoding="utf-8")))
        if write_mail(rec, mail_dir):
            n += 1
            fwd = f" (forwarded from {rec['forwarded']['from']})" if rec["forwarded"] else ""
            print(f"  {rec['key']}: {rec['subject']!r} from {rec['from']}{fwd}")
    print(f"{n} new mails in {rel(mail_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
