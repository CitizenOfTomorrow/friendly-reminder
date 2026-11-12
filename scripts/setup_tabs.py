"""One-time setup of the Phase 2 helper tabs (`log`, `needs_review`).

    python scripts/setup_tabs.py                    # dry run: prints what it would do
    DRY_RUN=0 TARGET=test python scripts/setup_tabs.py

If a tab exists with the old prototype columns, it is renamed to `<name>_prototype` (nothing deleted).
Then the tab is created with the header from apply_sales.py. A tab that already has the right header is
left alone. Any other existing header aborts.
"""
from __future__ import annotations

import sys

from apply_sales import LOG_COLUMNS, REVIEW_COLUMNS
from common import ConfigError, RunReport, load_settings, open_spreadsheet, runtime_from_env

PROTOTYPE_HEADERS = {
    "log": ["timestamp", "message_id", "show_id", "field", "old", "new", "evidence"],
    "needs_review": ["timestamp", "message_id", "subject", "reason", "extracted_json"],
}


def _header(ws) -> list[str]:
    row = ws.row_values(1)
    while row and row[-1] == "":
        row.pop()
    return [str(v).strip() for v in row]


def main() -> int:
    settings = load_settings()
    rt = runtime_from_env()
    if rt.sheet_xlsx is not None:
        raise ConfigError("setup_tabs needs the live sheet (unset SHEET_XLSX)")
    report = RunReport("setup_tabs", settings, rt)
    sh = open_spreadsheet(settings, rt)
    existing = {ws.title: ws for ws in sh.worksheets()}

    for kind, columns in (("log", LOG_COLUMNS), ("needs_review", REVIEW_COLUMNS)):
        name = settings["tabs"][kind]
        ws = existing.get(name)
        if ws is not None:
            hdr = _header(ws)
            if hdr == columns:
                report.info.append(f"{name!r} already has the right header, left alone")
                continue
            if hdr != PROTOTYPE_HEADERS[kind]:
                raise ConfigError(f"tab {name!r} has an unexpected header {hdr}; not touching it")
            target = f"{name}_prototype"
            if target in existing:
                raise ConfigError(f"cannot rename {name!r}: {target!r} already exists")
            report.planned.append(f"rename {name!r} -> {target!r} (prototype columns, kept as is)")
            if rt.can_write:
                ws.update_title(target)
        report.planned.append(f"create {name!r} with header {columns}")
        if rt.can_write:
            new = sh.add_worksheet(title=name, rows=1000, cols=len(columns))
            new.update([columns], "A1", value_input_option="RAW")
            new.freeze(rows=1)

    path = report.write()
    print(f"[{'DRY RUN' if rt.dry_run else 'LIVE'}]")
    for line in report.planned + report.info:
        print(f"  {line}")
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
