"""Phase 2: validate Claude's extractions and apply them to the provider columns (HANDOVER §6.2–6.4).

    python scripts/apply_sales.py                      # dry run: plan -> work/, nothing written
    DRY_RUN=0 TARGET=test python scripts/apply_sales.py

Inputs: work/mail/<key>.json (fetch_reports.py or the Gmail connector import) and
work/extractions/<key>.json (written by Claude, schema in HANDOVER §6.3).
A mail without an extraction file is left pending. A mail whose message_id or content_hash is already
in `log` or `needs_review` is skipped. The only cells ever written are provider "sold" columns.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from common import (ROOT, ConfigError, RunReport, load_grid, load_settings, load_tab_rows, norm_text,
                    open_spreadsheet, parse_date, rel, runtime_from_env, today)
from mail_model import provider_from_sender
from sheet_model import Show, assert_header, build_model

LOG_COLUMNS = ["timestamp", "run_id", "dry_run", "message_id", "row_number", "termin", "artist", "venue",
               "provider", "column", "old", "new", "evidence", "note", "content_hash"]
REVIEW_COLUMNS = ["timestamp", "run_id", "message_id", "subject", "sender", "reason", "extracted_json",
                  "content_hash"]
PROCESSED_NOTE = "message processed"


# --------------------------------------------------------------------------- matching

def _tokens(s: str, stop: set[str]) -> list[str]:
    return [t for t in norm_text(s).split() if t not in stop]


def artist_matches(email_artist: str, sheet_artist: str) -> bool:
    a, b = norm_text(email_artist), norm_text(sheet_artist)
    if not a or not b:
        return False
    if a == b:
        return True
    # joint bills "A & B" / "A + B": a match on either name is enough (the date is checked separately)
    import re
    parts = lambda s: {norm_text(p) for p in re.split(r"\s*(?:&|\+|\bund\b|\band\b|,)\s*", s) if norm_text(p)}  # noqa: E731
    pa, pb = parts(email_artist), parts(sheet_artist)
    return bool(pa & pb) or a in pb or b in pa


def venue_matches(email_venue: str | None, sheet_venue: str, settings: dict) -> bool:
    if not email_venue:
        return True  # venue not given in the report: nothing to contradict
    stop = set(settings.get("venue_stopwords", []))
    e, s = " ".join(_tokens(email_venue, stop)), " ".join(_tokens(sheet_venue, stop))
    aliases = {" ".join(_tokens(k, stop)): " ".join(_tokens(v, stop))
               for k, v in (settings.get("venue_aliases") or {}).items()}
    e = aliases.get(e, e)
    if e == s:
        return True
    et, st = set(e.split()), set(s.split())
    return bool(et and st) and (st <= et or et <= st)


# --------------------------------------------------------------------------- decisions

@dataclasses.dataclass
class Decision:
    kind: str                 # apply | noop | skip_zero | review
    message_id: str
    item: dict | None
    show: Show | None = None
    provider: str | None = None
    column: str | None = None
    old: int | None = None
    new: int | None = None
    reason: str = ""
    note: str = ""


def _hint_for_unmatched(item: dict, shows: list[Show]) -> str:
    """For a show that isn't upcoming: is there a row with the same artist and day/month (typo in the year)?"""
    d = parse_date(item.get("date"))
    for s in shows:
        if artist_matches(item.get("artist") or "", s.artist) and d and (s.termin.month, s.termin.day) == (d.month, d.day):
            return f"; row {s.row_number} has {s.artist} on {s.termin.isoformat()} (typo in the sheet?)"
    return ""


def _bandshop_on_tickettoaster(show: Show) -> bool:
    info = (show.bandshop_info or "").lower()
    return "bandshop tickettoaster" in info or "tickettoaster.de/apv" in info


def resolve_provider(mail: dict, extraction: dict, settings: dict) -> tuple[str | None, str, str | None]:
    """(provider, note, review_reason). Sender decides first (code), Claude's classification only as fallback."""
    code = provider_from_sender(mail.get("effective_from", ""), settings)
    ext = (extraction.get("provider") or "unknown").lower()
    if code and code.startswith("review:"):
        return None, "", f"sender {mail.get('effective_from')} ({code[7:]}) is not decided yet (open question 1)"
    if code:
        if code == "tickettoaster" and ext == "band":
            return None, "", "Tickettoaster mail classified as band shop: TT (AE) or Band (AG)? A human decides"
        note = "" if ext in (code, "unknown") else f"classified as {ext}, sender says {code}; sender wins"
        return code, note, None
    if ext in ("", "unknown") or ext not in settings["providers"]:
        return None, "", f"unknown provider (sender {mail.get('effective_from') or '?'} not recognized, content unclear)"
    if ext == "andere":
        return None, "", "provider 'andere' (e.g. Rausgegangen) is not decided yet (open question 1)"
    return ext, f"provider from content ({ext}); sender {mail.get('effective_from')} unknown", None


def decide(mail: dict, extraction: dict, model_shows: list[Show], upcoming: dict[int, Show],
           settings: dict) -> list[Decision]:
    mid = mail["message_id"]
    provider, pnote, preview = resolve_provider(mail, extraction, settings)
    items = extraction.get("shows") or []
    if not items:
        return [Decision("review", mid, None, reason="no sales figures found")]
    if preview:
        return [Decision("review", mid, it, reason=preview) for it in items]

    thr = float(settings["confidence_threshold"])
    sold_col = settings["providers"][provider]["sold"]["col"]
    alloc_col = settings["providers"][provider]["alloc"]["col"]
    out: list[Decision] = []

    # same row reported twice with different numbers -> all of them to review
    by_row: dict[Any, set] = {}
    for it in items:
        by_row.setdefault(it.get("row_number"), set()).add(it.get("tickets_sold"))
    conflicting = {r for r, v in by_row.items() if r is not None and len(v) > 1}
    seen: set = set()

    for it in items:
        def review(reason: str, show: Show | None = None) -> None:
            out.append(Decision("review", mid, it, show, provider, sold_col, reason=reason))

        r = it.get("row_number")
        new = it.get("tickets_sold")
        if (r, new) in seen:
            continue
        seen.add((r, new))
        if not isinstance(new, int) or isinstance(new, bool) or new < 0:
            review(f"tickets_sold is not a non-negative integer: {new!r}")
            continue
        if r is None:
            review("no matching show (row_number is null)" + _hint_for_unmatched(it, model_shows))
            continue
        show = upcoming.get(r)
        if show is None:
            review(f"row {r} is not an upcoming show (any more)" + _hint_for_unmatched(it, model_shows))
            continue
        if r in conflicting:
            review(f"row {r} reported twice in this mail with different numbers {sorted(by_row[r])}", show)
            continue
        d = parse_date(it.get("date"))
        if d != show.termin:
            review(f"date {it.get('date')} != sheet Termin {show.termin.isoformat()}", show)
            continue
        if not artist_matches(it.get("artist") or "", show.artist):
            review(f"artist {it.get('artist')!r} doesn't match sheet {show.artist!r}", show)
            continue
        if not venue_matches(it.get("venue"), show.venue, settings):
            review(f"venue mismatch: email {it.get('venue')!r} vs sheet {show.venue!r} "
                   "(add an alias in settings.yaml if it's the same place)", show)
            continue
        conf = it.get("confidence")
        if not isinstance(conf, (int, float)) or conf < thr:
            review(f"confidence below {thr} ({conf})", show)
            continue
        if provider == "tickettoaster" and _bandshop_on_tickettoaster(show):
            review(f"show has a Tickettoaster band shop ({show.bandshop_info!r}): TT (AE) or Band (AG)?", show)
            continue
        pc = show.providers[provider]
        if pc.sold_is_formula:
            review(f"target cell {sold_col}{r} contains a formula ({pc.sold_raw}); not overwritten", show)
            continue
        if pc.sold_text:
            review(f"target cell {sold_col}{r} contains text ({pc.sold_raw!r}); not overwritten", show)
            continue
        old = pc.sold
        if old is not None and new < old:
            review(f"decrease: {old} -> {new} (refund or an older report?)", show)
            continue
        base = dict(message_id=mid, item=it, show=show, provider=provider, column=sold_col, old=old, new=new)
        if old == new:
            out.append(Decision("noop", **base))
            continue
        if new == 0 and old is None and not pc.alloc:
            out.append(Decision("skip_zero", **base, note=f"0 reported, {sold_col} empty and no allocation in {alloc_col}"))
            continue
        notes = [n for n in (pnote,) if n]
        if pc.alloc is not None and pc.alloc > 0 and new > pc.alloc:
            notes.append(f"over allocation (K = {pc.alloc})")
        out.append(Decision("apply", **base, note="; ".join(notes)))
    return out


# --------------------------------------------------------------------------- IO helpers

def load_json_dir(d: Path) -> dict[str, dict]:
    out = {}
    if d.exists():
        for p in sorted(d.glob("*.json")):
            data = json.loads(p.read_text(encoding="utf-8"))
            out[data.get("key") or p.stem] = data
    return out


def check_tab_header(rows: list[list] | None, columns: list[str], tab: str) -> None:
    if rows is None:
        raise ConfigError(f"tab {tab!r} does not exist. Run scripts/setup_tabs.py first.")
    header = [str(v).strip() if v is not None else "" for v in (rows[0] if rows else [])]
    while header and header[-1] == "":
        header.pop()
    if header != columns:
        raise ConfigError(f"tab {tab!r} header doesn't match.\n  expected: {columns}\n  found:    {header}\n"
                          "Run scripts/setup_tabs.py (renames old prototype tabs).")


def already_seen(rows: list[list] | None, columns: list[str]) -> tuple[set[str], set[str]]:
    ids, hashes = set(), set()
    if not rows:
        return ids, hashes
    i_mid, i_hash = columns.index("message_id"), columns.index("content_hash")
    for r in rows[1:]:
        if len(r) > i_mid and r[i_mid]:
            ids.add(str(r[i_mid]))
        if len(r) > i_hash and r[i_hash]:
            hashes.add(str(r[i_hash]))
    return ids, hashes


def review_key(d: Decision) -> tuple:
    """Same case = same reason, provider, show and number. Entered into needs_review only once."""
    if d.item is None:          # mail-level case (e.g. no figures): one per mail
        return (d.reason, d.message_id)
    it = d.item
    return (d.reason, d.provider or "", norm_text(it.get("artist") or ""), str(it.get("date") or ""),
            str(it.get("tickets_sold")))


def existing_review_keys(rows: list[list] | None) -> set[tuple]:
    out: set[tuple] = set()
    if not rows or [str(v).strip() for v in rows[0][:len(REVIEW_COLUMNS)]] != REVIEW_COLUMNS:
        return out
    i_reason, i_json = REVIEW_COLUMNS.index("reason"), REVIEW_COLUMNS.index("extracted_json")
    for r in rows[1:]:
        try:
            item = json.loads(r[i_json]) if len(r) > i_json and r[i_json] else None
        except (ValueError, TypeError):
            item = None
        d = Decision("review", str(r[REVIEW_COLUMNS.index("message_id")]) if len(r) > 2 else "", item,
                     reason=str(r[i_reason]) if len(r) > i_reason else "",
                     provider=(item or {}).get("provider"))
        out.add(review_key(d))
    return out


def log_row(d: Decision, ts: str, run_id: str, dry: bool, mail: dict) -> list:
    s = d.show
    return [ts, run_id, "1" if dry else "0", d.message_id, s.row_number if s else "", s.termin.isoformat() if s else "",
            s.artist if s else "", s.venue if s else "", d.provider or "", d.column or "",
            "" if d.old is None else d.old, "" if d.new is None else d.new,
            ((d.item or {}).get("evidence") or "")[:300], d.note, mail.get("content_hash", "")]


def review_row(d: Decision, ts: str, run_id: str, mail: dict) -> list:
    return [ts, run_id, d.message_id, mail.get("subject", "")[:200], mail.get("effective_from", ""), d.reason,
            json.dumps({**d.item, "provider": d.provider}, ensure_ascii=False)[:2000] if d.item else "",
            mail.get("content_hash", "")]


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--as-of", type=dt.date.fromisoformat, help="treat this date as today")
    args = ap.parse_args(argv)

    settings = load_settings()
    rt = runtime_from_env()
    report = RunReport("apply_sales", settings, rt)
    as_of = args.as_of or today(settings)
    mail_dir, ext_dir = ROOT / settings["mail_dir"], ROOT / settings["extraction_dir"]
    tabs = settings["tabs"]

    log_rows = load_tab_rows(settings, rt, tabs["log"])
    rev_rows = load_tab_rows(settings, rt, tabs["needs_review"])
    if rt.can_write:
        check_tab_header(log_rows, LOG_COLUMNS, tabs["log"])
        check_tab_header(rev_rows, REVIEW_COLUMNS, tabs["needs_review"])
    seen_ids, seen_hashes = set(), set()
    for rows, cols, tab in ((log_rows, LOG_COLUMNS, tabs["log"]), (rev_rows, REVIEW_COLUMNS, tabs["needs_review"])):
        try:
            check_tab_header(rows, cols, tab)
            a, b = already_seen(rows, cols)
            seen_ids |= a
            seen_hashes |= b
        except ConfigError as e:
            report.warnings.append(f"dedup against {tab!r} not possible in this dry run: {str(e).splitlines()[0][7:]}")

    mails, extractions = load_json_dir(mail_dir), load_json_dir(ext_dir)
    report.read.append(f"{len(mails)} mails in {rel(mail_dir)}, {len(extractions)} extractions in {rel(ext_dir)}")

    # sheet: read fresh, right before deciding/writing
    grid, source = load_grid(settings, rt)
    report.read.append(source)
    assert_header(grid, settings)
    model = build_model(grid, settings)
    upcoming = {s.row_number: s for s in model.upcoming(as_of)}
    report.read.append(f"{len(upcoming)} upcoming shows (Termin >= {as_of})")

    seen_reviews = existing_review_keys(rev_rows)
    allowed = {p["sold"]["col"] for p in settings["providers"].values()}
    planned: dict[tuple[int, str], Decision] = {}
    decisions: list[tuple[dict, list[Decision]]] = []
    run_hashes: dict[str, str] = {}
    for key, mail in sorted(mails.items(), key=lambda kv: kv[1].get("date", "")):
        mid, h = mail["message_id"], mail.get("content_hash", "")
        if mid in seen_ids or (h and h in seen_hashes):
            report.info.append(f"skipped, already processed: {mid} ({mail.get('subject')!r})")
            continue
        if h and h in run_hashes:
            report.info.append(f"skipped, same content as {run_hashes[h]}: {mid}")
            continue
        ext = extractions.get(key)
        if ext is None:
            report.info.append(f"pending, no extraction yet: {mid} ({mail.get('subject')!r})")
            continue
        if ext.get("message_id") != mid:
            raise ConfigError(f"extraction {key} has message_id {ext.get('message_id')!r}, mail has {mid!r}")
        run_hashes[h] = mid
        ds = decide(mail, ext, model.shows, upcoming, settings)
        for d in ds:
            if d.kind == "apply":
                # safety: only ever the provider's own sold column of the matched row
                assert d.column in allowed and d.column == settings["providers"][d.provider]["sold"]["col"], d
                assert d.column not in settings["never_write"], d
                k = (d.show.row_number, d.column)
                prev = planned.get(k)
                if prev is not None:          # an earlier mail in this run already set this cell (mails sorted by date)
                    if d.new == prev.new:
                        d.kind, d.note = "noop", f"same value already planned from {prev.message_id}"
                        continue
                    if d.new < prev.new:
                        d.kind, d.reason = "review", (f"decrease vs. an earlier mail in this run "
                                                      f"({prev.message_id}: {prev.new}) -> {d.new}")
                    else:
                        d.old = prev.old
                        prev.kind, prev.note = "noop", f"superseded by {d.message_id} ({d.new})"
                        planned[k] = d
                        continue
                else:
                    planned[k] = d
            if d.kind == "review":
                rk = review_key(d)
                if rk in seen_reviews:
                    d.kind = "dup_review"
                else:
                    seen_reviews.add(rk)
        decisions.append((mail, ds))
    writes = {k: d for k, d in planned.items() if d.kind == "apply"}

    ts = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    new_log, new_review = [], []
    counts = {"apply": 0, "noop": 0, "skip_zero": 0, "review": 0, "dup_review": 0}
    for mail, ds in decisions:
        c = {k: sum(1 for d in ds if d.kind == k) for k in counts}
        for k in counts:
            counts[k] += c[k]
        for d in ds:
            label = f"{d.show.artist} ({d.show.termin})" if d.show else (d.item or {}).get("artist", "-")
            if d.kind == "apply":
                new_log.append(log_row(d, ts, report.run_id, rt.dry_run, mail))
                report.planned.append(f"{d.column}{d.show.row_number} {label}: {d.old} -> {d.new}"
                                      + (f"  [{d.note}]" if d.note else ""))
            elif d.kind == "review":
                new_review.append(review_row(d, ts, report.run_id, mail))
                report.review.append(f"{mail.get('subject')!r} / {label}: {d.reason}")
            elif d.kind == "skip_zero":
                report.info.append(f"not written: {label}: {d.note}")
        # one marker row per processed mail, so re-runs skip it even if nothing changed
        new_log.append([ts, report.run_id, "1" if rt.dry_run else "0", mail["message_id"], "", "", "", "",
                        ds[0].provider if ds and ds[0].provider else "", "", "", "", "",
                        f"{PROCESSED_NOTE}: {c['apply']} applied, {c['noop']} unchanged, "
                        f"{c['skip_zero']} zero skipped, {c['review']} to review, {c['dup_review']} repeated review cases", mail.get("content_hash", "")])

    # ---- outputs
    import common
    work = common.WORK
    work.mkdir(parents=True, exist_ok=True)
    plan = {"run_id": report.run_id, "dry_run": rt.dry_run, "as_of": as_of.isoformat(), "counts": counts,
            "writes": [{"cell": f"{d.column}{d.show.row_number}", "artist": d.show.artist, "old": d.old, "new": d.new,
                        "message_id": d.message_id, "note": d.note} for d in writes.values()],
            "log_rows": new_log, "review_rows": new_review}
    plan_path = work / f"apply_plan_{report.run_id}.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    if rt.can_write and (writes or new_log or new_review):
        sh = open_spreadsheet(settings, rt)
        main_ws = sh.worksheet(tabs["main"])
        if writes:
            main_ws.batch_update([{"range": f"{c}{r}", "values": [[d.new]]} for (r, c), d in writes.items()],
                                 value_input_option="RAW")
        if new_log:
            sh.worksheet(tabs["log"]).append_rows(new_log, value_input_option="RAW", table_range="A1")
        if new_review:
            sh.worksheet(tabs["needs_review"]).append_rows(new_review, value_input_option="RAW", table_range="A1")
        report.info.append(f"written: {len(writes)} cells, {len(new_log)} log rows, {len(new_review)} review rows")

    path = report.write()
    mode = "DRY RUN" if rt.dry_run else "LIVE"
    print(f"[{mode}] {len(decisions)} mails processed: {counts['apply']} to write, {counts['noop']} unchanged, "
          f"{counts['skip_zero']} zeros skipped, {counts['review']} to review ({counts['dup_review']} repeats not re-entered)")
    for line in report.planned:
        print(f"  write {line}")
    for line in report.review:
        print(f"  review {line}")
    print(f"plan: {rel(plan_path)}   report: {rel(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
