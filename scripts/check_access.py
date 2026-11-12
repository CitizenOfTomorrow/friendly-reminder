"""Phase 0: read-only credentials and connectivity check.

    TARGET=test python scripts/check_access.py

Checks, each independently: env vars present (values never printed), Google Sheets access and the
expected tabs, IMAP login + read-only folder select + Drafts folder. Exit code 0 only if all
configured checks pass. Writes nothing anywhere except the local run report.
"""
from __future__ import annotations

import datetime as dt
import os
import socket
import sys

from common import RunReport, load_settings, runtime_from_env, spreadsheet_id, google_credentials

ENV_VARS = ["DRY_RUN", "TARGET", "IMAP_HOST", "IMAP_USER", "IMAP_PASS", "IMAP_FOLDER",
            "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_SA_JSON", "SHEET_XLSX"]


def check_google(settings, rt) -> tuple[str, str]:
    if not (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("GOOGLE_SA_JSON")):
        return "SKIPPED", "no Google credentials set"
    import gspread
    try:
        gc = gspread.authorize(google_credentials())
        sh = gc.open_by_key(spreadsheet_id(settings, rt))
        tabs = [w.title for w in sh.worksheets()]
    except SystemExit as e:
        return "FAIL", str(e)
    except Exception as e:  # noqa: BLE001 - report any failure plainly
        return "FAIL", f"{type(e).__name__}: {e}"
    wanted = settings["tabs"]
    missing = [t for t in wanted.values() if t not in tabs]
    if wanted["main"] in missing:
        return "FAIL", f"opened {sh.title!r} but main tab {wanted['main']!r} is missing; tabs: {tabs}"
    note = f"opened {sh.title!r}; tabs: {tabs}"
    if missing:
        note += f"; not yet created (needed in later phases): {missing}"
    return "OK", note


def check_imap(settings, rt) -> tuple[str, str]:
    host, user, pw = (os.environ.get(k) for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASS"))
    if not (host and user and pw):
        return "SKIPPED", "IMAP_HOST / IMAP_USER / IMAP_PASS not all set"
    folder = os.environ.get("IMAP_FOLDER") or settings["targets"][rt.target]["imap_folder"]
    drafts = settings["targets"][rt.target]["drafts_folder"]
    try:
        socket.create_connection((host, 993), timeout=10).close()
    except OSError as e:
        return "FAIL", f"cannot reach {host}:993 ({e}). In the cloud, see HANDOVER §11 (Gmail API fallback)"
    from imapclient import IMAPClient
    try:
        with IMAPClient(host, port=993, ssl=True, timeout=30) as c:
            c.login(user, pw)
            info = c.select_folder(folder, readonly=True)
            since = dt.date.today() - dt.timedelta(days=settings["lookback_days"])
            n_recent = len(c.search(["SINCE", since]))
            folders = [f[2] for f in c.list_folders()]
            drafts_ok = drafts in folders
    except Exception as e:  # noqa: BLE001
        return "FAIL", f"{type(e).__name__}: {e}"
    note = (f"folder {folder!r}: {info.get(b'EXISTS')} messages, {n_recent} in the last "
            f"{settings['lookback_days']} days")
    if not drafts_ok:
        cands = [f for f in folders if "draft" in f.lower() or "entw" in f.lower()]
        return "FAIL", note + f"; drafts folder {drafts!r} not found (candidates: {cands})"
    return "OK", note + f"; drafts folder {drafts!r} exists"


def main() -> int:
    settings = load_settings()
    rt = runtime_from_env()
    report = RunReport("check_access", settings, rt)
    print(f"mode: {'DRY RUN' if rt.dry_run else 'LIVE'}, target: {rt.target}")
    for k in ENV_VARS:
        state = "set" if os.environ.get(k) else "-"
        print(f"  {k:32} {state}")
        report.read.append(f"env {k}: {state}")
    results = {"google": check_google(settings, rt), "imap": check_imap(settings, rt)}
    ok = True
    for name, (status, note) in results.items():
        print(f"{name:7} {status:8} {note}")
        (report.warnings if status != "OK" else report.info).append(f"{name}: {status}: {note}")
        ok &= status == "OK"
    print(f"report: {report.write()}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
