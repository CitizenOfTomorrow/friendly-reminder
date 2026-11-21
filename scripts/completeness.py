"""Phase 3: who reported what this week, and the drafts that follow from it (HANDOVER §6.5).

    python scripts/completeness.py [--now 2026-11-10T13:00] [--fresh]

Reads the main tab, `log` (what was reported since Monday 00:00 Europe/Berlin), `history` (growth) and
`needs_review`. Produces drafts via outbox.py (eml files only for now) and a run report.
Never writes to the sheet. Never sends.

Per upcoming show:
  expected providers = those with allocation (K) > 0, else settings.default_expected_providers
  complete           = every expected provider has a log row this week (changed or unchanged)
  sold out           = VVK >= KON > 0            -> no update draft
  no allocation      = KON empty/0                -> skipped, listed in the summary
Drafts:
  contact_update  complete shows only; incomplete shows are listed in the summary (with
                  drafts.incomplete_updates: true, after the cutoff also incomplete ones, listing what's missing)
  band_reminder   after the cutoff, band numbers expected but missing; to the email in AL, else AK
  weekly_summary  when anything in it changed, to settings.drafts.summary_to

Idempotency (HANDOVER §6.5): every draft has a key (draft_type|ISO week|termin|artist|venue). A key that is
already recorded is not drafted again; if the show's VVK changed since, the summary says so instead.
  DRY_RUN=0 TARGET=test  keys are read from and appended to the `log` tab ("draft created: <key>")
  dry run                keys from `log` plus work/drafts/<week>/drafted_<target>_<source>.json
                         (--fresh ignores and replaces that local file; log keys always count)
A new weekly summary is drafted whenever its content changed since the last one in the same cutoff phase.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import sys

from apply_sales import LOG_COLUMNS, PROCESSED_NOTE, REVIEW_COLUMNS, check_tab_header
from common import (ROOT, RunReport, extract_emails, load_grid, load_settings, load_tab_rows,
                    open_spreadsheet, parse_date, rel, runtime_from_env, tz)
from history_snapshot import HISTORY_COLUMNS
from outbox import get_backend, render, slug
from sheet_model import Show, assert_header, build_model

DE_DAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ORDER = ["dice", "cts", "tickettoaster", "band", "andere"]
DRAFT_NOTE = "draft created"


# --------------------------------------------------------------------------- time

def week_start(now: dt.datetime) -> dt.datetime:
    d = now.date() - dt.timedelta(days=now.weekday())
    return dt.datetime(d.year, d.month, d.day, tzinfo=now.tzinfo)


def cutoff_of(ws: dt.datetime, settings: dict) -> dt.datetime:
    c = settings["cutoff"]
    h, m = (int(x) for x in str(c["time"]).split(":"))
    return ws + dt.timedelta(days=WEEKDAYS.index(c["weekday"].lower()), hours=h, minutes=m)


# --------------------------------------------------------------------------- inputs

def reported_since(log_rows: list[list] | None, since: dt.datetime, settings: dict,
                   until: dt.datetime | None = None) -> dict[str, set[str]]:
    """show_key -> providers with a log row (applied or unchanged) in [since, until]."""
    col_to_provider = {p["sold"]["col"]: k for k, p in settings["providers"].items()}
    out: dict[str, set[str]] = {}
    if not log_rows:
        return out
    idx = {c: LOG_COLUMNS.index(c) for c in ("timestamp", "dry_run", "termin", "artist", "venue", "column", "note")}
    for r in log_rows[1:]:
        r = list(r) + [""] * (len(LOG_COLUMNS) - len(r))
        col = str(r[idx["column"]])
        if col not in col_to_provider or str(r[idx["dry_run"]]) == "1":
            continue
        if str(r[idx["note"]]).startswith(PROCESSED_NOTE):
            continue
        try:
            ts = dt.datetime.fromisoformat(str(r[idx["timestamp"]]))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=since.tzinfo)
        if ts < since or (until is not None and ts > until):
            continue
        termin = parse_date(r[idx["termin"]])
        key = f"{termin.isoformat() if termin else r[idx['termin']]}|{r[idx['artist']]}|{r[idx['venue']]}"
        out.setdefault(key, set()).add(col_to_provider[col])
    return out


def last_snapshot_vvk(history: list[list] | None, on_or_before: dt.date) -> tuple[str | None, dict[str, int]]:
    """VVK per show_key from the most recent snapshot on or before the given date."""
    if not history or len(history) < 2:
        return None, {}
    i_date, i_key, i_vvk = (HISTORY_COLUMNS.index(c) for c in ("snapshot_date", "show_key", "vvk_total"))
    dates = sorted({str(r[i_date]) for r in history[1:] if r and r[i_date] and str(r[i_date]) <= on_or_before.isoformat()})
    if not dates:
        return None, {}
    snap = dates[-1]
    out = {}
    for r in history[1:]:
        if r and str(r[i_date]) == snap and len(r) > i_vvk and isinstance(r[i_vvk], (int, float)):
            out[str(r[i_key])] = int(r[i_vvk])
    return snap, out


# --------------------------------------------------------------------------- draft ledger (idempotency)

def drafts_root(settings: dict):
    return ROOT / settings["drafts"]["out_dir"]


def ledger_path(settings: dict, rt, week: str):
    """Local dry-run ledger, one per target and source, so an xlsx dry run can't hide drafts from a sheet run."""
    src = f"xlsx-{slug(rt.sheet_xlsx.stem)}" if rt.sheet_xlsx is not None else "sheet"
    return drafts_root(settings) / week / f"drafted_{rt.target}_{src}.json"


MISSING_PREFIX = "missing: "


def drafted_from_log(log_rows: list[list] | None) -> dict[str, dict]:
    """key -> {"vvk": int|None, "missing": [...]} for every live (dry_run 0) "draft created: <key>" row in `log`."""
    out: dict[str, dict] = {}
    if not log_rows:
        return out
    i_dry, i_new, i_ev, i_note = (LOG_COLUMNS.index(c) for c in ("dry_run", "new", "evidence", "note"))
    prefix = f"{DRAFT_NOTE}: "
    for r in log_rows[1:]:
        r = list(r) + [""] * (len(LOG_COLUMNS) - len(r))
        note = str(r[i_note] or "")
        if str(r[i_dry]) == "1" or not note.startswith(prefix):
            continue
        ev = str(r[i_ev] or "")
        missing = [m for m in ev[len(MISSING_PREFIX):].split(",") if m] if ev.startswith(MISSING_PREFIX) else []
        out[note[len(prefix):]] = {"vvk": _int(r[i_new]), "missing": missing}
    return out


def draft_log_row(rec: dict, ts: str, run_id: str) -> list:
    r = [""] * len(LOG_COLUMNS)
    vals = dict(timestamp=ts, run_id=run_id, dry_run="0", termin=rec.get("termin", ""), artist=rec.get("artist", ""),
                venue=rec.get("venue", ""), new="" if rec.get("vvk") is None else rec["vvk"],
                evidence=MISSING_PREFIX + ",".join(rec.get("missing") or []),
                note=f"{DRAFT_NOTE}: {rec['key']}")
    for k, v in vals.items():
        r[LOG_COLUMNS.index(k)] = v
    return r


# --------------------------------------------------------------------------- evaluation

@dataclasses.dataclass
class Status:
    show: Show
    state: str                 # complete | incomplete | sold_out | no_allocation | no_ticketing
    expected: list[str]
    missing: list[str]
    vvk: int | None
    kon: int | None
    zuwachs: int | None
    contacts: list[str]


def _int(v) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def expected_providers(show: Show, settings: dict) -> list[str]:
    exp = [p for p in ORDER if (show.providers[p].alloc or 0) > 0]
    return exp or list(settings["default_expected_providers"])


def evaluate(show: Show, reported: set[str], settings: dict, prev_vvk: int | None) -> Status:
    vvk, kon = _int(show.vvk), _int(show.kon)
    exp = expected_providers(show, settings)
    missing = [p for p in exp if p not in reported]
    zuwachs = vvk - prev_vvk if vvk is not None and prev_vvk is not None else None
    if show.no_ticketing:
        state = "no_ticketing"
    elif not kon:
        state = "no_allocation"
    elif vvk is not None and vvk >= kon:
        state = "sold_out"
    else:
        state = "incomplete" if missing else "complete"
    return Status(show, state, exp, missing, vvk, kon, zuwachs, show.contact_emails)


def band_reminder_target(show: Show) -> tuple[list[str], str]:
    """(recipients, where). Email in AL wins; AL empty -> AK; AL with URL/channel name -> manual check."""
    al = show.bandshop_info or ""
    emails = extract_emails(al)
    if emails:
        return emails, al
    if not al.strip():
        return show.contact_emails, "Ticketzahlen an"
    return [], al


def de_date(d: dt.date) -> str:
    return d.strftime("%d.%m.%Y")


def plan_drafts(statuses: list[Status], *, after_cutoff: bool, now: dt.datetime, cutoff: dt.datetime,
                review_lines: list[str], settings: dict, drafted: dict[str, dict] | None = None):
    """Returns (new drafts, summary, week, records). Keys already in `drafted` are not drafted again
    (summary["skipped"] lists them); `records` describes each new draft for the ledger.

    Keys: contact_update|week|show for a complete show, ...|incomplete for an update listing missing providers
    (so a show that becomes complete later gets its one complete update), band_reminder|week|show, and
    weekly_summary|week|phase|<fingerprint>: a new summary whenever its content changed since the last one."""
    drafted = drafted or {}
    iso = now.isocalendar()
    kw, week = iso.week, f"{iso.year}-W{iso.week:02d}"
    records: list[dict] = []
    skipped: list[str] = []

    def add(draft, show: Show | None = None, vvk: int | None = None, missing: list[str] | None = None) -> bool:
        if draft.key in drafted:
            skipped.append(draft.key)
            return False
        drafts.append(draft)
        rec = {"key": draft.key, "template": draft.template, "vvk": vvk, "missing": list(missing or [])}
        if show is not None:
            rec.update(termin=show.termin.isoformat(), artist=show.artist, venue=show.venue)
        records.append(rec)
        return True

    labels = {k: p["label_kontingent"] for k, p in settings["providers"].items()}
    sig = settings["drafts"]["signature"]
    incomplete_updates = bool(settings["drafts"].get("incomplete_updates", False))
    drafts, summary = [], {k: [] for k in ("complete", "incomplete", "sold_out", "band_missing", "no_contact",
                                           "no_allocation", "newer")}
    for st in statuses:
        s = st.show
        base = {"datum": de_date(s.termin), "artist": s.artist, "venue": s.venue, "vvk": st.vvk, "kon": st.kon,
                "zuwachs": st.zuwachs, "missing": [labels[m] for m in st.missing], "on_sale": s.on_sale,
                "raw": s.contacts_raw, "to": st.contacts, "draft": False, "drafted_earlier": False,
                "supersedes": False}
        if st.state == "no_ticketing":
            continue
        if st.state in ("no_allocation", "sold_out"):
            summary[st.state].append(base)
            continue
        wants_update = st.state == "complete" or (after_cutoff and incomplete_updates)
        if not st.contacts and (wants_update or after_cutoff):
            summary["no_contact"].append(base)
        elif wants_update:
            shown = [p for p in ORDER if p in st.expected or (s.providers[p].sold or 0) > 0]
            ctx = {"artist": s.artist, "datum": de_date(s.termin), "venue": s.venue, "kw": kw,
                   "provider_zahlen": [{"name": labels[p], "verkauft": s.providers[p].sold or 0,
                                       "stand": "Stand Vorwoche" if p in st.missing else ""} for p in shown],
                   "vvk_total": st.vvk, "kon_total": st.kon,
                   "pct": round(100 * st.vvk / st.kon) if st.vvk is not None and st.kon else 0,
                   "zuwachs": st.zuwachs, "fehlend": [labels[m] for m in st.missing], "signature": sig}
            key = f"contact_update|{week}|{s.identity()}"
            incomplete_key = f"{key}|incomplete"
            if st.missing:
                key = incomplete_key
            if add(render("contact_update", ctx, to=st.contacts, key=key), s, st.vvk, st.missing):
                base["draft"] = True
                base["supersedes"] = not st.missing and incomplete_key in drafted
            else:
                base["drafted_earlier"] = True
                before = drafted[key].get("vvk")
                before_missing = [m for m in drafted[key].get("missing") or [] if m in labels]
                if before != st.vvk or before_missing != st.missing:
                    summary["newer"].append({**base, "before": before,
                                             "before_missing": [labels[m] for m in before_missing]})
        summary["complete" if st.state == "complete" else "incomplete"].append(base)
        if after_cutoff and "band" in st.missing:
            to, where = band_reminder_target(s)
            summary["band_missing"].append({**base, "to": to, "where": where})
            if to:
                add(render("band_reminder", {"artist": s.artist, "datum": de_date(s.termin), "venue": s.venue,
                                             "signature": sig},
                           to=to, key=f"band_reminder|{week}|{s.identity()}"), s, st.vvk, st.missing)
    phase = "after_cutoff" if after_cutoff else "before_cutoff"
    ctx = {**summary, "kw": kw, "stand": now.strftime("%d.%m.%Y %H:%M"), "after_cutoff": after_cutoff,
           "cutoff": f"{DE_DAYS[cutoff.weekday()]} {cutoff:%d.%m. %H:%M}", "review": review_lines}
    add(render("weekly_summary", ctx, to=list(settings["drafts"]["summary_to"]),
               key=f"weekly_summary|{week}|{phase}|{summary_fingerprint(summary, review_lines)}"))
    summary["skipped"] = skipped
    return drafts, summary, week, records


def summary_fingerprint(summary: dict, review_lines: list[str]) -> str:
    """Hash of what the summary says, minus the run time and minus whether a draft is new or from an earlier
    run, so an unchanged situation gives the same key and a changed one a new summary."""
    def norm(b: dict) -> dict:
        out = {k: v for k, v in b.items() if k not in ("draft", "drafted_earlier", "supersedes")}
        out["has_draft"] = bool(b.get("draft") or b.get("drafted_earlier"))
        return out
    sections = {k: [norm(b) for b in v] for k, v in summary.items() if k != "skipped"}
    blob = json.dumps({"s": sections, "r": review_lines}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


def review_lines_since(rows: list[list] | None, since: dt.datetime) -> list[str]:
    out = []
    if not rows:
        return out
    i_ts, i_subj, i_reason, i_json = (REVIEW_COLUMNS.index(c) for c in ("timestamp", "subject", "reason", "extracted_json"))
    for r in rows[1:]:
        r = list(r) + [""] * (len(REVIEW_COLUMNS) - len(r))
        try:
            ts = dt.datetime.fromisoformat(str(r[i_ts]))
        except ValueError:
            continue
        if (ts if ts.tzinfo else ts.replace(tzinfo=since.tzinfo)) < since:
            continue
        try:
            artist = (json.loads(r[i_json]) or {}).get("artist") if r[i_json] else None
        except ValueError:
            artist = None
        out.append(f"{artist or r[i_subj] or '(ohne Betreff)'}: {r[i_reason]}")
    return out


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--now", type=dt.datetime.fromisoformat, help="pretend it's this local time (Europe/Berlin)")
    ap.add_argument("--fresh", action="store_true",
                    help="dry run only: ignore the local draft ledger and draft everything again")
    args = ap.parse_args(argv)
    settings = load_settings()
    rt = runtime_from_env()
    report = RunReport("completeness", settings, rt)
    zone = tz(settings)
    now = (args.now.replace(tzinfo=zone) if args.now and args.now.tzinfo is None else args.now) or dt.datetime.now(zone)
    ws = week_start(now)
    cutoff = cutoff_of(ws, settings)
    after = now >= cutoff

    grid, source = load_grid(settings, rt)
    report.read.append(source)
    assert_header(grid, settings)
    model = build_model(grid, settings)
    upcoming = model.upcoming(now.date())
    log_rows = load_tab_rows(settings, rt, settings["tabs"]["log"])
    if rt.can_write or log_rows is not None:    # rows are read and appended by position: the header must match
        check_tab_header(log_rows, LOG_COLUMNS, settings["tabs"]["log"])
    history = load_tab_rows(settings, rt, settings["tabs"]["history"])
    rev = load_tab_rows(settings, rt, settings["tabs"]["needs_review"])
    reported = reported_since(log_rows, ws, settings, until=now)
    snap, prev = last_snapshot_vvk(history, now.date())
    report.read.append(f"{len(upcoming)} upcoming shows; log entries this week for {len(reported)} shows; "
                       f"growth vs. snapshot {snap or '(none)'}")

    iso = now.isocalendar()
    ledger = ledger_path(settings, rt, f"{iso.year}-W{iso.week:02d}")
    local: dict[str, dict] = {}
    from_log = drafted_from_log(log_rows)
    if rt.can_write:
        ledger_src = "log tab"
    else:   # dry run: what the log tab already records, plus the local ledger of earlier dry runs
        if not args.fresh and ledger.exists():
            local = json.loads(ledger.read_text("utf-8"))
        ledger_src = "log tab + " + rel(ledger) + (" (ignored: --fresh)" if args.fresh else "")
    drafted = {**from_log, **local}
    report.read.append(f"{len(drafted)} drafts already recorded ({ledger_src})")

    statuses = [evaluate(s, reported.get(s.identity(), set()), settings, prev.get(s.identity())) for s in upcoming]
    drafts, summary, week, records = plan_drafts(statuses, after_cutoff=after, now=now, cutoff=cutoff,
                                                 review_lines=review_lines_since(rev, ws), settings=settings,
                                                 drafted=drafted)
    for s in summary["newer"]:
        report.info.append(f"newer numbers since draft: {s['datum']} {s['artist']}: {s['before']} -> {s['vvk']}"
                           + (f", missing {s['before_missing']} -> {s['missing']}" if s["before_missing"] != s["missing"] else ""))
    # Record first, then write the eml files: if recording fails, no drafts exist that a rerun would duplicate.
    if records and rt.can_write:
        ts = dt.datetime.now(zone).isoformat(timespec="seconds")
        try:
            open_spreadsheet(settings, rt).worksheet(settings["tabs"]["log"]).append_rows(
                [draft_log_row(r, ts, report.run_id) for r in records], value_input_option="RAW", table_range="A1")
        except Exception as e:
            report.warnings.append(f"appending {len(records)} draft rows to the log tab failed, no drafts were "
                                   f"saved: {type(e).__name__}: {e}")
            report.write()
            raise
        report.info.append(f"written: {len(records)} draft rows to the log tab")
    elif records:
        local.update({r["key"]: r for r in records})
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps(local, ensure_ascii=False, indent=2), encoding="utf-8")
    paths = get_backend(settings, rt).save(drafts, subdir=week)
    for d, p in zip(drafts, paths):
        report.planned.append(f"draft {d.template}: {d.subject!r} to {', '.join(d.to) or '-'} -> {rel(p)}")
    for k in summary["skipped"]:
        report.info.append(f"already drafted, not again: {k}")
    for k in ("no_contact", "no_allocation"):
        for s in summary[k]:
            report.warnings.append(f"{k}: {s['datum']} {s['artist']}")
    path = report.write()

    print(f"[{'DRY RUN' if rt.dry_run else 'LIVE'}] {now:%a %d.%m.%Y %H:%M}, week {week}, "
          f"{'after' if after else 'before'} cutoff ({cutoff:%a %H:%M})")
    for k in ("complete", "incomplete", "sold_out", "no_contact", "no_allocation", "band_missing"):
        print(f"  {k:14} {len(summary[k])}")
    print(f"  {'newer':14} {len(summary['newer'])}")
    print(f"  {len(drafts)} new drafts (eml), {len(summary['skipped'])} skipped as already drafted, in {rel(paths[0].parent) if paths else '-'}; report {rel(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
