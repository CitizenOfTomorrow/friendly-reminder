"""Phase 2: fetch report mails via IMAP, strictly read-only, into work/mail/<key>.json.

    IMAP_HOST=... IMAP_USER=... IMAP_PASS=... [IMAP_FOLDER=n8n] python scripts/fetch_reports.py [--days 14]

select_folder(readonly=True) and BODY.PEEK[] only: no flags change, nothing is moved, deleted or sent.
Forwarded originals attached as message/rfc822 are unwrapped (their From/Date/Message-ID count).
Also refreshes work/upcoming_shows.json (unless --no-sheet).
"""
from __future__ import annotations

import argparse
import datetime as dt
import email
import email.policy
import io
import json
import os
import sys
from email.message import EmailMessage

from common import ROOT, ConfigError, RunReport, load_settings, rel, runtime_from_env
from mail_model import address, html_to_text, normalize_message


def _part_text(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except Exception:  # noqa: BLE001 - odd charsets
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def _attachment_text(filename: str, mime: str, data: bytes) -> str:
    fn = (filename or "").lower()
    if fn.endswith(".csv") or mime == "text/csv" or mime.startswith("text/plain"):
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
    if fn.endswith(".xlsx"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        out = []
        for ws in wb.worksheets:
            out.append(f"# sheet {ws.title}")
            for row in ws.iter_rows(values_only=True):
                if any(v not in (None, "") for v in row):
                    out.append(",".join("" if v is None else str(v) for v in row))
        return "\n".join(out)
    if fn.endswith(".pdf"):
        try:
            from pypdf import PdfReader  # optional dependency
            return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
        except Exception:  # noqa: BLE001
            return f"[PDF {filename}: text extraction not available]"
    return f"[attachment {filename} ({mime}) not converted]"


def parse_message(raw: bytes, source: str = "imap") -> dict:
    """RFC822 bytes -> canonical mail record (see mail_model)."""
    msg: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)
    inner_msg = None
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822":
            payload = part.get_payload()
            inner_msg = payload[0] if isinstance(payload, list) else payload
            break
    src = inner_msg or msg

    plain, htmls, attachments = [], [], []
    for part in src.walk():
        ctype = part.get_content_type()
        disp = (part.get_content_disposition() or "").lower()
        if part.is_multipart() or ctype == "message/rfc822":
            continue
        if disp == "attachment" or part.get_filename():
            data = part.get_payload(decode=True) or b""
            attachments.append({"filename": part.get_filename() or "", "mime_type": ctype,
                                "text": _attachment_text(part.get_filename() or "", ctype, data)})
        elif ctype == "text/plain":
            plain.append(_part_text(part))
        elif ctype == "text/html":
            htmls.append(html_to_text(_part_text(part)))
    body = "\n".join(plain) if plain else "\n".join(htmls)

    inner = None
    if inner_msg is not None:
        inner = {"from": address(str(inner_msg.get("From", ""))), "date": str(inner_msg.get("Date", "")),
                 "subject": str(inner_msg.get("Subject", "")),
                 "message_id": str(inner_msg.get("Message-ID", "")).strip()}
    mid = (inner or {}).get("message_id") or str(msg.get("Message-ID", "")).strip()
    rec = normalize_message(message_id=mid, source=source, subject=str(msg.get("Subject", "")),
                            sender=str(msg.get("From", "")), date=str(msg.get("Date", "")),
                            body_text=body, attachments=attachments, inner=inner)
    rec["outer_message_id"] = str(msg.get("Message-ID", "")).strip()
    return rec


def write_mail(rec: dict, mail_dir) -> bool:
    mail_dir.mkdir(parents=True, exist_ok=True)
    p = mail_dir / f"{rec['key']}.json"
    if p.exists():
        return False
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, help="lookback in days (default from settings)")
    ap.add_argument("--no-sheet", action="store_true", help="don't refresh work/upcoming_shows.json")
    args = ap.parse_args(argv)
    settings = load_settings()
    rt = runtime_from_env()
    report = RunReport("fetch_reports", settings, rt)
    host, user, pw = (os.environ.get(k) for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASS"))
    if not (host and user and pw):
        raise ConfigError("IMAP_HOST, IMAP_USER and IMAP_PASS must be set")
    folder = os.environ.get("IMAP_FOLDER") or settings["targets"][rt.target]["imap_folder"]
    days = args.days or settings["lookback_days"]
    since = dt.date.today() - dt.timedelta(days=days)
    mail_dir = ROOT / settings["mail_dir"]

    from imapclient import IMAPClient
    new = 0
    with IMAPClient(host, port=993, ssl=True, timeout=60) as c:
        c.login(user, pw)
        c.select_folder(folder, readonly=True)          # read-only: nothing can change
        uids = c.search(["SINCE", since])
        report.read.append(f"{host} / {folder}: {len(uids)} messages since {since}")
        for uid, data in c.fetch(uids, ["BODY.PEEK[]"]).items():   # PEEK: doesn't set \\Seen
            rec = parse_message(data[b"BODY[]"])
            if write_mail(rec, mail_dir):
                new += 1
                report.planned.append(f"saved {rec['key']}: {rec['subject']!r} from {rec['effective_from']}")
    report.info.append(f"{new} new mails saved to {rel(mail_dir)}")

    if not args.no_sheet:
        import sheet_dump
        sheet_dump.main(["--quiet"])
    path = report.write()
    print(f"{new} new mails in {rel(mail_dir)}; report {rel(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
