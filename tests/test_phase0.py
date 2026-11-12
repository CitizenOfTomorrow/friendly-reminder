"""Phase 0 unit tests. Synthetic data only; never the real export."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from common import (Cell, ConfigError, Grid, NotANumber, col_to_idx, extract_emails, formula_shape,  # noqa: E402
                    idx_to_col, load_settings, norm_text, parse_count, parse_date, runtime_from_env)
from sheet_model import build_model, check_header, flag_upcoming, is_show_row  # noqa: E402

S = load_settings()


# ----------------------------------------------------------------- fixtures

def header_row() -> dict[str, Cell]:
    row = {}
    for spec in S["columns"].values():
        if spec["header"]:
            row[spec["col"]] = Cell(spec["header"], spec["header"])
    for p in S["providers"].values():
        for k in ("sold", "alloc"):
            row[p[k]["col"]] = Cell(p[k]["header"], p[k]["header"])
    row["M"] = Cell("Andere", "Andere")  # the duplicate header
    return row


def show(r, date, artist, venue, **cells):
    row = {"N": Cell(date, date), "P": Cell(artist, artist), "Q": Cell(venue, venue),
           "V": Cell(f"=AA{r}+AE{r}+AC{r}+AG{r}", 0), "W": Cell(f"=AB{r}+AF{r}+AD{r}+AH{r}", 0),
           "X": Cell(f"=V{r}-W{r}", 0), "Y": Cell(f"=V{r}/W{r}", 0)}
    for col, v in cells.items():
        row[col] = v if isinstance(v, Cell) else Cell(v, v)
    return row


def label(text):
    return {"N": Cell(text, text)}


def make_grid():
    d = dt.datetime
    rows = {
        1: header_row(),
        2: label("NOVEMBER"),
        3: show(3, d(2026, 11, 3), "Past Act", "Privatclub", AA=5, AB=10),
        4: show(4, d(2026, 11, 20), "Alpha", "Neue Zukunft", AA=3, AB=20, AC=Cell("=115+19", 134), AD=100,
                AK="a@x.de, B@Y.de "),
        5: show(5, d(2026, 11, 25), "Beta & Gamma", "Kantine am Berghain", AE=Cell("w", "w"), W=Cell(120, 120),
                AK="siehe Tour alles an Scott"),
        6: label("FEBRUAR"),
        7: show(7, d(2027, 2, 16), "Delta", "Heimathafen", AC=60, AD=50, AK="d@x.de"),
        8: show(8, d(2026, 2, 20), "Typo", "Heimathafen", AK="t@x.de"),
        9: {},
        10: label("MASTER"),
        11: show(11, "xx", "xx", "xx"),
    }
    return Grid(rows)


# ----------------------------------------------------------------- parsing

def test_col_letters_roundtrip():
    for c in ["A", "N", "Z", "AA", "AK", "AN", "AZ", "BA"]:
        assert idx_to_col(col_to_idx(c)) == c
    assert col_to_idx("AA") == 27


@pytest.mark.parametrize("v,want", [
    (dt.datetime(2027, 4, 10), dt.date(2027, 4, 10)),
    ("10.04.2027", dt.date(2027, 4, 10)),
    ("2027-04-10", dt.date(2027, 4, 10)),
    (46487, dt.date(2027, 4, 10)),          # Sheets serial number
    ("NOVEMBER", None), ("xx", None), (None, None), (134, None), (True, None), ("31.02.2027", None),
])
def test_parse_date(v, want):
    assert parse_date(v) == want


@pytest.mark.parametrize("v,want", [(134, 134), (134.0, 134), ("134", 134), ("1.234", 1234), (" 7 ", 7),
                                    (None, None), ("", None), ("  ", None)])
def test_parse_count(v, want):
    assert parse_count(v) == want


@pytest.mark.parametrize("v", ["mart", "w", ">", "///", 1.5, "1,5", True])
def test_parse_count_rejects_text(v):
    with pytest.raises(NotANumber):
        parse_count(v)


def test_extract_emails():
    assert extract_emails("a@x.de, B@Y.de ") == ["a@x.de", "b@y.de"]
    assert extract_emails("siehe Tour alles an Scott") == []
    assert extract_emails("tickets@target-concerts.de ") == ["tickets@target-concerts.de"]
    assert extract_emails(None) == []


def test_norm_text():
    assert norm_text("Felix Meyer & Projekt ìle") == "felix meyer projekt ile"
    assert norm_text("pøjpøj") == "pojpoj"
    assert norm_text("Kantine Am Berghain") == norm_text("kantine am  berghain")


def test_formula_shape():
    assert formula_shape("=AA254+AE254+AC254+AG254") == formula_shape("=AA298+AE298+AC298+AG298")
    assert formula_shape("=SUM(AE2,AC2,AG2,AI2)") != formula_shape("=AA2+AE2+AC2+AG2")


# ----------------------------------------------------------------- runtime safety

def test_dry_run_default():
    rt = runtime_from_env({})
    assert rt.dry_run and rt.target == "test" and not rt.can_write


@pytest.mark.parametrize("v", ["1", "true", "no", "", "00", "false"])
def test_anything_but_zero_is_dry_run(v):
    assert runtime_from_env({"DRY_RUN": v}).dry_run


def test_prod_live_requires_confirmation():
    with pytest.raises(ConfigError):
        runtime_from_env({"DRY_RUN": "0", "TARGET": "prod"})
    assert runtime_from_env({"DRY_RUN": "0", "TARGET": "prod", "I_UNDERSTAND_PROD": "yes"}).can_write


def test_xlsx_source_cannot_be_live():
    with pytest.raises(ConfigError):
        runtime_from_env({"DRY_RUN": "0", "SHEET_XLSX": "x.xlsx"})


def test_bad_target():
    with pytest.raises(ConfigError):
        runtime_from_env({"TARGET": "staging"})


# ----------------------------------------------------------------- header check

def test_header_ok():
    assert check_header(make_grid(), S) == []


def test_header_mismatch_detected():
    g = make_grid()
    g.rows[1]["AC"] = Cell("Eventim", "Eventim")      # renamed
    del g.rows[1]["AK"]                                # column moved away
    g.rows[1]["X"] = Cell("Diff", "Diff")              # previously empty header
    problems = check_header(g, S)
    assert len(problems) == 3
    assert any(p.startswith("AC1") for p in problems)


def test_header_whitespace_and_case_tolerated():
    g = make_grid()
    g.rows[1]["AB"] = Cell(" dice  (k) ", " dice  (k) ")
    assert check_header(g, S) == []


# ----------------------------------------------------------------- model

def test_show_row_detection():
    g = make_grid()
    assert [r for r in g.row_numbers() if is_show_row(g, r, S)] == [3, 4, 5, 7, 8]


def test_model_master_and_anomalies():
    m = build_model(make_grid(), S)
    assert m.master_row == 11
    assert m.master_formulas["V"] == "=AA#+AE#+AC#+AG#"
    assert any("row 8 Typo 2026-02-20" in a for a in m.anomalies)


def test_upcoming_and_flags():
    m = build_model(make_grid(), S)
    up = flag_upcoming(m, S, dt.date(2026, 11, 12))
    assert [s.row_number for s in up] == [4, 5, 7]          # past act and the 2026 typo row drop out
    alpha, beta, delta = up
    assert alpha.providers["cts"].sold == 134 and alpha.providers["cts"].sold_is_formula
    assert any("AC (cts) is a formula" in f for f in alpha.flags)
    assert alpha.contact_emails == ["a@x.de", "b@y.de"]
    assert any("AE (tickettoaster) contains text" in f for f in beta.flags)
    assert any("W is a typed value" in f for f in beta.flags)
    assert any("no email address" in f for f in beta.flags)
    assert any("sold 60 > allocation 50" in f for f in delta.flags)


def test_formula_differing_from_master_flagged():
    g = make_grid()
    g.rows[7]["V"] = Cell("=SUM(AE7,AC7,AG7,AI7)", 60)
    up = flag_upcoming(build_model(g, S), S, dt.date(2026, 11, 12))
    assert any("differs from MASTER" in f for f in up[2].flags)


def test_month_separator_mismatch():
    g = make_grid()
    g.rows[7]["N"] = Cell(dt.datetime(2027, 6, 18), dt.datetime(2027, 6, 18))  # under FEBRUAR
    m = build_model(g, S)
    s = next(x for x in m.shows if x.row_number == 7)
    assert any("under month separator FEBRUAR" in f for f in s.flags)


def test_xlsx_loader_roundtrip(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    from common import _load_xlsx
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = S["tabs"]["main"]
    ws["N1"], ws["N2"], ws["P2"], ws["AC2"] = "Termin", dt.datetime(2027, 1, 1), "Act", "=1+2"
    p = tmp_path / "t.xlsx"
    wb.save(p)
    g = _load_xlsx(p, S["tabs"]["main"])
    assert g.cell(2, "AC").is_formula and g.cell(2, "AC").raw == "=1+2"
    assert parse_date(g.cell(2, "N").value) == dt.date(2027, 1, 1)
