"""Interpretation of the main ticketing tab: header check, which rows are shows, per-show values, anomalies.

Read-only. Later phases use the same model right before writing (rows move, so always re-read).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

from common import (Cell, ConfigError, Grid, NotANumber, extract_emails, formula_shape,
                    iter_column_specs, parse_count, parse_date)


# --------------------------------------------------------------------------- header check

def _norm_header(v: Any) -> str:
    if v is None:
        return ""
    return " ".join(str(v).split()).casefold()


def check_header(grid: Grid, settings: dict) -> list[str]:
    """Return a list of mismatches between config and the live header row (empty = OK)."""
    hr = settings.get("header_row", 1)
    problems = []
    for name, col, expected in iter_column_specs(settings):
        actual = grid.cell(hr, col).raw
        if _norm_header(actual) != _norm_header(expected):
            problems.append(f"{col}{hr} ({name}): expected {expected!r}, found {actual!r}")
    return problems


def assert_header(grid: Grid, settings: dict) -> None:
    problems = check_header(grid, settings)
    if problems:
        raise ConfigError("header check failed; columns may have moved. Fix config/settings.yaml "
                          "or the sheet, nothing was done:\n  " + "\n  ".join(problems))


# --------------------------------------------------------------------------- model

@dataclasses.dataclass
class ProviderCell:
    sold: int | None
    alloc: int | None
    sold_raw: Any
    alloc_raw: Any
    sold_is_formula: bool
    alloc_is_formula: bool
    sold_text: bool     # non-numeric text in the sold cell
    alloc_text: bool


@dataclasses.dataclass
class Show:
    row_number: int
    termin: dt.date
    artist: str
    venue: str
    agentur: str | None
    support: str | None
    einlass: str | None
    beginn: str | None
    on_sale: str | None
    tl: str | None
    contacts_raw: str | None
    contact_emails: list[str]
    bandshop_info: str | None
    vvk: Any
    kon: Any
    pct: Any
    vvk_raw: Any
    kon_raw: Any
    no_ticketing: bool
    providers: dict[str, ProviderCell]
    flags: list[str] = dataclasses.field(default_factory=list)

    def identity(self) -> str:
        return f"{self.termin.isoformat()}|{self.artist}|{self.venue}"

    def to_json(self) -> dict:
        return {
            "row_number": self.row_number,
            "termin": self.termin.isoformat(),
            "artist": self.artist,
            "venue": self.venue,
            "agentur": self.agentur,
            "on_sale": self.on_sale,
            "tl": self.tl,
            "contact_emails": self.contact_emails,
            "contacts_raw": self.contacts_raw,
            "bandshop_info": self.bandshop_info,
            "no_ticketing": self.no_ticketing,
            "vvk": _jsonable(self.vvk),
            "kon": _jsonable(self.kon),
            "pct": _jsonable(self.pct),
            "providers": {
                k: {"sold": p.sold, "alloc": p.alloc, "sold_is_formula": p.sold_is_formula,
                    "sold_raw": _jsonable(p.sold_raw) if (p.sold_is_formula or p.sold_text) else None}
                for k, p in self.providers.items()
            },
            "flags": self.flags,
        }


def _jsonable(v: Any) -> Any:
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _text(cell: Cell) -> str | None:
    v = cell.value if cell.value is not None else cell.raw
    if v is None:
        return None
    s = str(v).strip()
    return s or None


@dataclasses.dataclass
class SheetModel:
    shows: list[Show]                 # every show row, in sheet order
    master_row: int | None            # the template row below the MASTER marker
    master_formulas: dict[str, str]   # col -> formula shape, e.g. {"V": "=AA#+AE#+AC#+AG#"}
    anomalies: list[str]              # sheet-wide findings (date order etc.)

    def upcoming(self, as_of: dt.date) -> list[Show]:
        return [s for s in self.shows if s.termin >= as_of]


def is_show_row(grid: Grid, r: int, settings: dict) -> bool:
    c = settings["columns"]
    ph = settings["placeholder_value"].casefold()
    termin = parse_date(grid.cell(r, c["termin"]["col"]).value)
    artist = _text(grid.cell(r, c["artist"]["col"]))
    venue = _text(grid.cell(r, c["venue"]["col"]))
    return (termin is not None and bool(artist) and bool(venue)
            and artist.casefold() != ph and venue.casefold() != ph)


def _provider_cell(grid: Grid, r: int, sold_col: str, alloc_col: str) -> ProviderCell:
    def one(col: str) -> tuple[int | None, Any, bool, bool]:
        cell = grid.cell(r, col)
        try:
            # value is the computed result (also for formulas like =115+19)
            n = parse_count(cell.value)
            return n, cell.raw, cell.is_formula, False
        except NotANumber:
            return None, cell.raw, cell.is_formula, True

    s, s_raw, s_f, s_t = one(sold_col)
    a, a_raw, a_f, a_t = one(alloc_col)
    return ProviderCell(s, a, s_raw, a_raw, s_f, a_f, s_t, a_t)


def build_model(grid: Grid, settings: dict) -> SheetModel:
    c = settings["columns"]
    col = lambda name: c[name]["col"]  # noqa: E731
    nt = settings["no_ticketing_marker"]
    hr = settings.get("header_row", 1)
    months = [m.casefold() for m in settings["month_names"]]

    shows: list[Show] = []
    anomalies: list[str] = []
    master_marker_row = None
    current_month_label: str | None = None

    for r in grid.row_numbers():
        if r <= hr:
            continue
        n_cell = grid.cell(r, col("termin"))
        n_text = _text(n_cell)
        if n_text and n_text.casefold() == settings["master_marker"].casefold():
            master_marker_row = r
            continue
        if n_text and n_text.casefold() in months and parse_date(n_cell.value) is None:
            current_month_label = n_text
            continue
        if not is_show_row(grid, r, settings):
            continue

        termin = parse_date(n_cell.value)
        providers = {k: _provider_cell(grid, r, p["sold"]["col"], p["alloc"]["col"])
                     for k, p in settings["providers"].items()}
        vvk_cell, kon_cell = grid.cell(r, col("vvk")), grid.cell(r, col("kon"))
        no_ticketing = any(_text(grid.cell(r, col(x))) == nt for x in ("vvk", "kon")) or all(
            (p.sold_raw == nt) for p in providers.values())
        show = Show(
            row_number=r, termin=termin,
            artist=_text(grid.cell(r, col("artist"))), venue=_text(grid.cell(r, col("venue"))),
            agentur=_text(grid.cell(r, col("agentur"))), support=_text(grid.cell(r, col("support"))),
            einlass=_text(grid.cell(r, col("einlass"))), beginn=_text(grid.cell(r, col("beginn"))),
            on_sale=_text(grid.cell(r, col("on_sale"))), tl=_text(grid.cell(r, col("tl"))),
            contacts_raw=_text(grid.cell(r, col("contacts"))),
            contact_emails=extract_emails(grid.cell(r, col("contacts")).value),
            bandshop_info=_text(grid.cell(r, col("bandshop_info"))),
            vvk=vvk_cell.value, kon=kon_cell.value, pct=grid.cell(r, col("pct")).value,
            vvk_raw=vvk_cell.raw, kon_raw=kon_cell.raw,
            no_ticketing=no_ticketing, providers=providers,
        )
        if current_month_label:
            want = months.index(current_month_label.casefold()) + 1
            if termin.month != want:
                show.flags.append(f"date {termin.isoformat()} is under month separator {current_month_label}")
        shows.append(show)

    # date order: a show dated before the show above it is suspicious (typo in year/month)
    for prev, cur in zip(shows, shows[1:]):
        if cur.termin < prev.termin:
            msg = (f"date out of order: row {cur.row_number} {cur.artist} {cur.termin.isoformat()} "
                   f"comes after row {prev.row_number} {prev.artist} {prev.termin.isoformat()}")
            anomalies.append(msg)
            cur.flags.append("date out of order (typo?)")

    # MASTER template row: first row after the marker with the placeholder in Termin
    master_row, master_formulas = None, {}
    if master_marker_row is None:
        anomalies.append("no MASTER row found; Phase 4 cannot take formulas from it")
    else:
        ph = settings["placeholder_value"].casefold()
        for r in grid.row_numbers():
            if r > master_marker_row and (_text(grid.cell(r, col("termin"))) or "").casefold() == ph:
                master_row = r
                break
        if master_row is None:
            anomalies.append(f"MASTER marker in row {master_marker_row} but no template row below it")
        else:
            for name in ("vvk", "kon", "diff", "pct"):
                cell = grid.cell(master_row, col(name))
                if cell.is_formula:
                    master_formulas[col(name)] = formula_shape(cell.raw)
                else:
                    anomalies.append(f"MASTER template row {master_row}: {col(name)} is not a formula")

    return SheetModel(shows=shows, master_row=master_row, master_formulas=master_formulas,
                      anomalies=anomalies)


def flag_upcoming(model: SheetModel, settings: dict, as_of: dt.date) -> list[Show]:
    """Add per-show flags that matter for automation. Returns the upcoming shows."""
    c = settings["columns"]
    upcoming = model.upcoming(as_of)
    for s in upcoming:
        if s.no_ticketing:
            s.flags.append("no ticketing (///)")
            continue
        for key, col_name in (("vvk", "vvk"), ("kon", "kon")):
            raw = s.vvk_raw if key == "vvk" else s.kon_raw
            letter = c[col_name]["col"]
            if isinstance(raw, str) and raw.startswith("="):
                want = model.master_formulas.get(letter)
                if want and formula_shape(raw) != want:
                    s.flags.append(f"{letter} formula {raw} differs from MASTER ({want})")
            elif raw is not None:
                s.flags.append(f"{letter} is a typed value ({raw!r}), not a formula")
        for k, p in s.providers.items():
            sc = settings["providers"][k]["sold"]["col"]
            ac = settings["providers"][k]["alloc"]["col"]
            if p.sold_is_formula:
                s.flags.append(f"{sc} ({k}) is a formula {p.sold_raw} -> imports go to review")
            if p.sold_text:
                s.flags.append(f"{sc} ({k}) contains text {p.sold_raw!r} -> imports go to review")
            if p.alloc_text:
                s.flags.append(f"{ac} ({k} K) contains text {p.alloc_raw!r}")
            if p.sold is not None and p.alloc is not None and p.alloc > 0 and p.sold > p.alloc:
                s.flags.append(f"{k}: sold {p.sold} > allocation {p.alloc}")
        if not s.contact_emails:
            s.flags.append(f"no email address in Ticketzahlen an ({s.contacts_raw!r})")
    return upcoming
