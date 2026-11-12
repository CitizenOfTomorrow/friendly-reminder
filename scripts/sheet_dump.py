"""Phase 0: read-only dump of upcoming shows.

    SHEET_XLSX=work/input/sheet_export.xlsx python scripts/sheet_dump.py [--as-of YYYY-MM-DD]
    python scripts/sheet_dump.py              # live sheet (needs Google credentials + spreadsheet_id)

Writes work/upcoming_shows.json and work/run_<id>_sheet_dump.md. Never writes to the sheet.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from common import WORK, RunReport, load_grid, load_settings, runtime_from_env, today
from sheet_model import assert_header, build_model, flag_upcoming

PROVIDER_ORDER = ["dice", "cts", "tickettoaster", "band", "andere"]
SHORT = {"dice": "Dice", "cts": "CTS", "tickettoaster": "TT", "band": "Band", "andere": "And"}


def fmt_pair(p) -> str:
    if p.sold is None and p.alloc is None and not p.sold_text:
        return "-"
    s = "?" if p.sold_text else ("" if p.sold is None else str(p.sold))
    if p.sold_is_formula:
        s += "ƒ"
    a = "" if p.alloc is None else str(p.alloc)
    return f"{s}/{a}"


def _n(v) -> str:
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--as-of", type=dt.date.fromisoformat, help="treat this date as today (default: today in Europe/Berlin)")
    ap.add_argument("--quiet", action="store_true", help="don't print the table")
    args = ap.parse_args(argv)

    settings = load_settings()
    rt = runtime_from_env()
    as_of = args.as_of or today(settings)
    report = RunReport("sheet_dump", settings, rt)

    grid, source = load_grid(settings, rt)
    report.read.append(source)
    assert_header(grid, settings)
    report.info.append("header check passed for all configured columns")

    model = build_model(grid, settings)
    upcoming = flag_upcoming(model, settings, as_of)
    report.read.append(f"{len(model.shows)} show rows in total, {len(upcoming)} upcoming (Termin >= {as_of})")
    if model.master_row:
        report.info.append(f"MASTER template row {model.master_row}: " +
                           ", ".join(f"{k} {v}" for k, v in model.master_formulas.items()))
    report.warnings.extend(model.anomalies)
    for s in upcoming:
        for f in s.flags:
            report.warnings.append(f"row {s.row_number} {s.termin} {s.artist}: {f}")

    WORK.mkdir(exist_ok=True)
    out = WORK / "upcoming_shows.json"
    payload = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "as_of": as_of.isoformat(),
               "source": source, "master_row": model.master_row, "master_formulas": model.master_formulas,
               "anomalies": model.anomalies, "shows": [s.to_json() for s in upcoming]}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report.planned.append(f"wrote {out.relative_to(WORK.parent)} (local file only)")
    path = report.write()

    if not args.quiet:
        print(f"Source: {source}")
        print(f"As of {as_of}: {len(upcoming)} upcoming shows   (sold/alloc, ƒ = formula cell)\n")
        hdr = f"{'row':>4}  {'date':10}  {'artist':28} {'venue':22} " + " ".join(f"{SHORT[p]:>9}" for p in PROVIDER_ORDER) + f" {'VVK/KON':>10}  flags"
        print(hdr)
        print("-" * len(hdr))
        for s in upcoming:
            cells = " ".join(f"{fmt_pair(s.providers[p]):>9}" for p in PROVIDER_ORDER)
            tot = f"{_n(s.vvk)}/{_n(s.kon)}" if not s.no_ticketing else "///"
            print(f"{s.row_number:>4}  {s.termin.isoformat()}  {s.artist[:28]:28} {s.venue[:22]:22} {cells} {tot:>10}  {len(s.flags) or ''}")
        print()
        if model.anomalies:
            print("Sheet anomalies:")
            for a in model.anomalies:
                print(f"  ! {a}")
        print(f"\nFlags per show and full details: {path.relative_to(WORK.parent)}")
        print(f"JSON: {out.relative_to(WORK.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
