"""Phase 1: weekly history snapshot of upcoming shows (HANDOVER §6.1).

    SHEET_XLSX=work/input/live_export.xlsx python scripts/history_snapshot.py     # offline dry run
    python scripts/history_snapshot.py                                             # live sheet, dry run
    DRY_RUN=0 TARGET=test python scripts/history_snapshot.py                       # writes the history tab

Snapshot date = Monday of the current week (Europe/Berlin), so re-runs during the week are no-ops
and growth always compares week with week. One row per upcoming show (Termin >= snapshot date),
shows without ticketing (///) excluded. Idempotent: if the snapshot date is already in `history`,
nothing is written. Dry run writes work/history_<date>.csv. Only ever appends to `history`.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys

from common import (WORK, ConfigError, rel, NotANumber, RunReport, load_grid, load_settings, load_tab_rows,
                    open_spreadsheet, parse_count, parse_date, runtime_from_env, today)
from sheet_model import Show, assert_header, build_model

HISTORY_COLUMNS = ["snapshot_date", "show_key", "row_number", "termin", "artist", "venue",
                   "dice", "cts", "tickettoaster", "band", "andere", "vvk_total", "kon_total", "pct"]
PROVIDERS = ["dice", "cts", "tickettoaster", "band", "andere"]


def monday_of(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _count_or_none(v, what: str, warnings: list[str]) -> int | None:
    try:
        return parse_count(v)
    except NotANumber:
        warnings.append(f"{what}: non-numeric value {v!r}, left empty")
        return None


def _pct(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None  # '#DIV/0!', empty, text
    return round(float(v), 4)


def snapshot_rows(shows: list[Show], snapshot_date: dt.date) -> tuple[list[dict], list[str]]:
    rows, warnings = [], []
    for s in shows:
        if s.termin < snapshot_date or s.no_ticketing:
            continue
        label = f"row {s.row_number} {s.termin} {s.artist}"
        row = {"snapshot_date": snapshot_date.isoformat(), "show_key": s.identity(),
               "row_number": s.row_number, "termin": s.termin.isoformat(),
               "artist": s.artist, "venue": s.venue}
        for p in PROVIDERS:
            pc = s.providers[p]
            if pc.sold_text:
                warnings.append(f"{label}: {p} cell contains text {pc.sold_raw!r}, left empty")
            row[p] = pc.sold
        row["vvk_total"] = _count_or_none(s.vvk, f"{label} VVK", warnings)
        row["kon_total"] = _count_or_none(s.kon, f"{label} KON", warnings)
        row["pct"] = _pct(s.pct)
        rows.append(row)
    return rows, warnings


def existing_snapshot_dates(history: list[list]) -> set[str]:
    """Dates already present in the history tab (first column), normalized to ISO."""
    out = set()
    for r in history[1:]:
        if r and r[0] not in (None, ""):
            d = parse_date(r[0])
            out.add(d.isoformat() if d else str(r[0]).strip())
    return out


def check_history_header(history: list[list]) -> str:
    """'empty' (no header yet), 'ok', or raise on a header that doesn't match."""
    if not history or not any(v not in (None, "") for v in history[0]):
        if any(any(v not in (None, "") for v in r) for r in history[1:]):
            raise ConfigError("history tab has data but no header row; fix it by hand")
        return "empty"
    header = [str(v).strip() if v is not None else "" for v in history[0]]
    while header and header[-1] == "":
        header.pop()
    if header != HISTORY_COLUMNS:
        raise ConfigError(f"history tab header doesn't match.\n  expected: {HISTORY_COLUMNS}\n  found:    {header}")
    return "ok"


def to_cells(row: dict) -> list:
    return ["" if row[c] is None else row[c] for c in HISTORY_COLUMNS]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot-date", type=dt.date.fromisoformat,
                    help="override the snapshot date (default: Monday of the current week)")
    args = ap.parse_args(argv)

    settings = load_settings()
    rt = runtime_from_env()
    snap = args.snapshot_date or monday_of(today(settings))
    report = RunReport("history_snapshot", settings, rt)
    tab = settings["tabs"]["history"]

    # 1. existing history (idempotency) — read before the main tab
    history = load_tab_rows(settings, rt, tab)
    if history is None:
        if rt.can_write:
            raise ConfigError(f"tab {tab!r} does not exist. Create it by hand (never automatically), then re-run.")
        report.warnings.append(f"tab {tab!r} does not exist yet; planning as if it were empty")
        history, header_state = [], "empty"
    else:
        header_state = check_history_header(history)
    done = existing_snapshot_dates(history)
    report.read.append(f"history tab: {'missing' if not history else f'{len(history) - 1} rows'}, "
                       f"{len(done)} snapshot dates")

    if snap.isoformat() in done:
        report.info.append(f"snapshot {snap} already exists, nothing to do")
        path = report.write()
        print(f"Snapshot {snap} already exists in {tab!r}, nothing to do. Report: {path}")
        return 0

    # 2. main tab, fresh
    grid, source = load_grid(settings, rt)
    report.read.append(source)
    assert_header(grid, settings)
    model = build_model(grid, settings)
    report.warnings.extend(model.anomalies)
    rows, warnings = snapshot_rows(model.shows, snap)
    report.warnings.extend(warnings)
    skipped = [s for s in model.shows if s.termin >= snap and s.no_ticketing]
    for s in skipped:
        report.info.append(f"skipped (no ticketing ///): row {s.row_number} {s.artist}")
    report.read.append(f"{len(rows)} upcoming shows with ticketing (Termin >= {snap})")

    # 3. always write the local CSV (it's the plan in dry run and the receipt in a live run)
    work = sys.modules[__name__].WORK
    work.mkdir(exist_ok=True)
    csv_path = work / f"history_{snap.isoformat()}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    if not rt.can_write:
        if header_state == "empty":
            report.planned.append(f"would write header row to {tab!r}")
        report.planned.append(f"would append {len(rows)} rows for snapshot {snap} to {tab!r} (see {csv_path.name})")
    else:
        ws = open_spreadsheet(settings, rt).worksheet(tab)
        # re-check right before writing: another run may have finished in between
        fresh = ws.get(value_render_option="UNFORMATTED_VALUE", date_time_render_option="FORMATTED_STRING")
        if snap.isoformat() in existing_snapshot_dates(fresh):
            report.info.append(f"snapshot {snap} appeared while running, nothing written")
        else:
            payload = [to_cells(r) for r in rows]
            if check_history_header(fresh) == "empty":
                payload.insert(0, HISTORY_COLUMNS)
            ws.append_rows(payload, value_input_option="RAW", table_range="A1")
            report.planned.append(f"appended {len(rows)} rows for snapshot {snap} to {tab!r}")

    path = report.write()
    mode = "DRY RUN" if rt.dry_run else "LIVE"
    print(f"[{mode}] snapshot {snap}: {len(rows)} rows -> {rel(csv_path)}")
    for line in report.planned:
        print(f"  {line}")
    if report.warnings:
        print(f"  {len(report.warnings)} warnings, see {rel(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
