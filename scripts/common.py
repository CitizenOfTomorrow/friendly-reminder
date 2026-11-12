"""Shared helpers: config, runtime safety rails, sheet sources, parsing, run reports.

Nothing in here sends email. Nothing in here writes to a sheet yet (Phase 0 is read-only).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "settings.yaml"
WORK = ROOT / "work"


class ConfigError(SystemExit):
    """Fatal configuration / safety problem. Exits non-zero with a clear message."""

    def __init__(self, msg: str):
        super().__init__(f"FATAL: {msg}")


# --------------------------------------------------------------------------- config

def load_settings(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclasses.dataclass(frozen=True)
class Runtime:
    dry_run: bool
    target: str
    sheet_xlsx: Path | None  # offline read-only source

    @property
    def can_write(self) -> bool:
        return not self.dry_run and self.sheet_xlsx is None


def runtime_from_env(env: dict[str, str] | None = None) -> Runtime:
    env = dict(os.environ) if env is None else env
    dry_run = env.get("DRY_RUN", "1").strip() != "0"  # anything but an explicit 0 is dry run
    target = env.get("TARGET", "test").strip() or "test"
    if target not in ("test", "prod"):
        raise ConfigError(f"TARGET must be 'test' or 'prod', got {target!r}")
    if target == "prod" and not dry_run and env.get("I_UNDERSTAND_PROD") != "yes":
        raise ConfigError("TARGET=prod with DRY_RUN=0 requires I_UNDERSTAND_PROD=yes")
    xlsx = env.get("SHEET_XLSX", "").strip()
    sheet_xlsx = Path(xlsx) if xlsx else None
    if sheet_xlsx is not None and not dry_run:
        raise ConfigError("SHEET_XLSX is a read-only offline source; it cannot be used with DRY_RUN=0")
    return Runtime(dry_run=dry_run, target=target, sheet_xlsx=sheet_xlsx)


def tz(settings: dict) -> ZoneInfo:
    return ZoneInfo(settings.get("timezone", "Europe/Berlin"))


def today(settings: dict) -> dt.date:
    return dt.datetime.now(tz(settings)).date()


def run_id(settings: dict) -> str:
    return dt.datetime.now(tz(settings)).strftime("%Y%m%d-%H%M%S")


# --------------------------------------------------------------------------- columns

def col_to_idx(col: str) -> int:
    """'A' -> 1, 'AA' -> 27."""
    n = 0
    for ch in col.upper():
        if not "A" <= ch <= "Z":
            raise ValueError(f"bad column letter {col!r}")
        n = n * 26 + (ord(ch) - 64)
    return n


def idx_to_col(i: int) -> str:
    s = ""
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


# --------------------------------------------------------------------------- sheet grid

@dataclasses.dataclass(frozen=True)
class Cell:
    raw: Any        # what was entered: a formula string ("=...") or a literal
    value: Any      # computed / displayed value (unformatted)

    @property
    def is_formula(self) -> bool:
        return isinstance(self.raw, str) and self.raw.startswith("=")

    @property
    def is_empty(self) -> bool:
        return self.raw is None or (isinstance(self.raw, str) and self.raw.strip() == "")


EMPTY = Cell(None, None)


class Grid:
    """Rows of cells keyed by 1-based row number and column letter."""

    def __init__(self, rows: dict[int, dict[str, Cell]]):
        self.rows = rows

    def cell(self, row: int, col: str) -> Cell:
        return self.rows.get(row, {}).get(col.upper(), EMPTY)

    def row_numbers(self) -> list[int]:
        return sorted(self.rows)

    @classmethod
    def from_lists(cls, raw: list[list[Any]], values: list[list[Any]]) -> "Grid":
        rows: dict[int, dict[str, Cell]] = {}
        for r, raw_row in enumerate(raw, start=1):
            val_row = values[r - 1] if r - 1 < len(values) else []
            cells = {}
            for c, rv in enumerate(raw_row, start=1):
                vv = val_row[c - 1] if c - 1 < len(val_row) else None
                if rv in (None, "") and vv in (None, ""):
                    continue
                cells[idx_to_col(c)] = Cell(rv, vv)
            rows[r] = cells
        return cls(rows)


def load_grid(settings: dict, rt: Runtime) -> tuple[Grid, str]:
    """Read the main tab fresh. Returns (grid, human readable source description)."""
    tab = settings["tabs"]["main"]
    if rt.sheet_xlsx is not None:
        return _load_xlsx(rt.sheet_xlsx, tab), f"xlsx export {rt.sheet_xlsx.name} / tab {tab!r}"
    return _load_gsheet(settings, rt, tab)


def _load_xlsx(path: Path, tab: str) -> Grid:
    import openpyxl

    if not path.exists():
        raise ConfigError(f"SHEET_XLSX not found: {path}")
    wf = openpyxl.load_workbook(path, data_only=False)
    wv = openpyxl.load_workbook(path, data_only=True)
    if tab not in wf.sheetnames:
        raise ConfigError(f"tab {tab!r} not in {path.name}; tabs: {wf.sheetnames}")
    sf, sv = wf[tab], wv[tab]
    raw = [[c.value for c in row] for row in sf.iter_rows()]
    vals = [[c.value for c in row] for row in sv.iter_rows()]
    return Grid.from_lists(raw, vals)


def google_credentials():
    """Service account credentials, from GOOGLE_APPLICATION_CREDENTIALS or GOOGLE_SA_JSON."""
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    inline = os.environ.get("GOOGLE_SA_JSON")
    if inline:
        import json
        return Credentials.from_service_account_info(json.loads(inline), scopes=scopes)
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not path:
        raise ConfigError("set GOOGLE_APPLICATION_CREDENTIALS (or GOOGLE_SA_JSON), or SHEET_XLSX for offline reading")
    return Credentials.from_service_account_file(path, scopes=scopes)


def spreadsheet_id(settings: dict, rt: Runtime) -> str:
    sid = settings["targets"][rt.target]["spreadsheet_id"]
    if not sid or sid.startswith("TODO"):
        raise ConfigError(f"spreadsheet_id for target {rt.target!r} is not set in config/settings.yaml")
    return sid


def open_spreadsheet(settings: dict, rt: Runtime):
    import gspread

    gc = gspread.authorize(google_credentials())
    return gc.open_by_key(spreadsheet_id(settings, rt))


def load_tab_rows(settings: dict, rt: Runtime, tab: str) -> list[list[Any]] | None:
    """All values of a helper tab (log, history, ...) as lists, or None if the tab doesn't exist."""
    if rt.sheet_xlsx is not None:
        import openpyxl
        wb = openpyxl.load_workbook(rt.sheet_xlsx, data_only=True, read_only=True)
        if tab not in wb.sheetnames:
            return None
        return [list(r) for r in wb[tab].iter_rows(values_only=True)]
    import gspread
    sh = open_spreadsheet(settings, rt)
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        return None
    return ws.get(value_render_option="UNFORMATTED_VALUE", date_time_render_option="FORMATTED_STRING")


def _load_gsheet(settings: dict, rt: Runtime, tab: str) -> tuple[Grid, str]:
    import gspread

    sh = open_spreadsheet(settings, rt)
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        raise ConfigError(f"tab {tab!r} not found; tabs: {[w.title for w in sh.worksheets()]}")
    raw = ws.get(value_render_option="FORMULA", date_time_render_option="SERIAL_NUMBER")
    vals = ws.get(value_render_option="UNFORMATTED_VALUE", date_time_render_option="SERIAL_NUMBER")
    return Grid.from_lists(raw, vals), f"Google Sheet {sh.title!r} / tab {tab!r} (target {rt.target})"


# --------------------------------------------------------------------------- parsing

_SHEETS_EPOCH = dt.date(1899, 12, 30)
_DE_DATE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})\s*$")
_ISO_DATE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")


def parse_date(v: Any) -> dt.date | None:
    """datetime/date, Sheets serial number, '10.04.2027' or ISO -> date. Anything else -> None."""
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if 20000 < v < 80000:  # ~1954..2119; guards against plain numbers
            return _SHEETS_EPOCH + dt.timedelta(days=int(v))
        return None
    if isinstance(v, str):
        m = _DE_DATE.match(v)
        if m:
            d, mo, y = (int(x) for x in m.groups())
            if y < 100:
                y += 2000
            try:
                return dt.date(y, mo, d)
            except ValueError:
                return None
        m = _ISO_DATE.match(v)
        if m:
            try:
                return dt.date(*(int(x) for x in m.groups()))
            except ValueError:
                return None
    return None


class NotANumber(ValueError):
    pass


_DE_INT = re.compile(r"^\d{1,3}(\.\d{3})+$")


def parse_count(v: Any) -> int | None:
    """Ticket count from a cell value. Empty -> None. Text that isn't a number -> NotANumber.

    Accepts 134, 134.0, '134', '1.234' (German thousands), '1,234'? no: ambiguous -> NotANumber.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        raise NotANumber(repr(v))
    if isinstance(v, (int, float)):
        if float(v) != int(v):
            raise NotANumber(repr(v))
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return None
        if s.isdigit():
            return int(s)
        if _DE_INT.match(s):
            return int(s.replace(".", ""))
        raise NotANumber(repr(v))
    raise NotANumber(repr(v))


_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def extract_emails(v: Any) -> list[str]:
    if not isinstance(v, str):
        return []
    seen, out = set(), []
    for m in _EMAIL.findall(v):
        e = m.strip().strip(".").lower()
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def norm_text(s: Any) -> str:
    """Lowercase, strip diacritics and punctuation, collapse whitespace."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("ø", "o").replace("ß", "ss")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def formula_shape(f: str) -> str:
    """'=AA254+AE254' -> '=AA#+AE#' so formulas can be compared across rows.

    The live API returns formulas in the sheet's locale (German: ';' as argument separator),
    the xlsx export uses ','. Normalize to ','.
    """
    return re.sub(r"([A-Z]{1,3})\d+", r"\1#", f.replace(" ", "").replace(";", ",").upper())


# --------------------------------------------------------------------------- run report

class RunReport:
    """Collects what a run read, planned/did, sent to review, and warnings. Written to work/run_<id>.md."""

    def __init__(self, workflow: str, settings: dict, rt: Runtime):
        self.workflow = workflow
        self.rt = rt
        self.run_id = run_id(settings)
        self.started = dt.datetime.now(tz(settings))
        self.read: list[str] = []
        self.planned: list[str] = []
        self.review: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []

    def write(self, directory: Path | None = None) -> Path:
        directory = directory or sys.modules[__name__].WORK
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"run_{self.run_id}_{self.workflow}.md"
        mode = "DRY RUN" if self.rt.dry_run else "LIVE"
        lines = [
            f"# Run {self.run_id}: {self.workflow}",
            "",
            f"- started: {self.started.isoformat(timespec='seconds')}",
            f"- mode: **{mode}**, target: `{self.rt.target}`",
            "",
        ]
        for title, items in (("Read", self.read), ("Planned / done", self.planned),
                             ("Needs review", self.review), ("Warnings", self.warnings),
                             ("Info", self.info)):
            lines.append(f"## {title} ({len(items)})")
            lines.append("")
            lines.extend(f"- {i}" for i in items) if items else lines.append("- none")
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


def rel(path: Path) -> str:
    """Path relative to the repo when possible, for printing."""
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


def eprint(*a: Any) -> None:
    print(*a, file=sys.stderr)


def iter_column_specs(settings: dict) -> Iterable[tuple[str, str, str]]:
    """(name, letter, expected_header) for every configured column, providers included."""
    for name, spec in settings["columns"].items():
        yield name, spec["col"], spec["header"]
    for p, spec in settings["providers"].items():
        yield f"{p}.sold", spec["sold"]["col"], spec["sold"]["header"]
        yield f"{p}.alloc", spec["alloc"]["col"], spec["alloc"]["header"]
