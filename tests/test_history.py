"""Phase 1 tests: snapshot rows, idempotency, header handling, and the live write path against a fake sheet."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import history_snapshot as hs  # noqa: E402
from common import Cell, ConfigError, Grid  # noqa: E402
from sheet_model import build_model  # noqa: E402
from test_phase0 import S, header_row, label, make_grid, show  # noqa: E402

SNAP = dt.date(2026, 11, 9)


def test_monday_of():
    assert hs.monday_of(dt.date(2026, 11, 12)) == SNAP   # Thursday
    assert hs.monday_of(dt.date(2026, 11, 9)) == SNAP    # Monday itself
    assert hs.monday_of(dt.date(2026, 11, 15)) == SNAP   # Sunday


def test_snapshot_rows_basic():
    rows, warnings = hs.snapshot_rows(build_model(make_grid(), S).shows, SNAP)
    assert [r["row_number"] for r in rows] == [4, 5, 7]          # past + typo row excluded
    alpha = rows[0]
    assert alpha["snapshot_date"] == "2026-11-09" and alpha["termin"] == "2026-11-20"
    assert alpha["cts"] == 134 and alpha["dice"] == 3 and alpha["band"] is None
    assert alpha["show_key"] == "2026-11-20|Alpha|Neue Zukunft"
    assert set(rows[0]) == set(hs.HISTORY_COLUMNS)
    assert any("tickettoaster cell contains text 'w'" in w for w in warnings)  # row 5


def test_snapshot_includes_show_between_monday_and_today():
    g = make_grid()
    g.rows[3]["N"] = Cell(dt.datetime(2026, 11, 10), dt.datetime(2026, 11, 10))  # Tuesday of snapshot week
    rows, _ = hs.snapshot_rows(build_model(g, S).shows, SNAP)
    assert rows[0]["row_number"] == 3


def test_no_ticketing_skipped_and_pct_errors_empty():
    g = make_grid()
    g.rows[7]["V"] = Cell("///", "///")
    g.rows[4]["Y"] = Cell("=V4/W4", "#DIV/0!")
    g.rows[5]["Y"] = Cell("=V5/W5", 0.123456)
    rows, _ = hs.snapshot_rows(build_model(g, S).shows, SNAP)
    assert [r["row_number"] for r in rows] == [4, 5]
    assert rows[0]["pct"] is None and rows[1]["pct"] == 0.1235


def test_existing_dates_normalized():
    hist = [hs.HISTORY_COLUMNS, ["2026-11-02", "k"], [dt.datetime(2026, 11, 9), "k"], ["", ""], []]
    assert hs.existing_snapshot_dates(hist) == {"2026-11-02", "2026-11-09"}


def test_history_header_states():
    assert hs.check_history_header([]) == "empty"
    assert hs.check_history_header([[None, ""]]) == "empty"
    assert hs.check_history_header([hs.HISTORY_COLUMNS + ["", ""]]) == "ok"
    with pytest.raises(ConfigError):
        hs.check_history_header([["snapshot_date", "termin"]])
    with pytest.raises(ConfigError):
        hs.check_history_header([[None], ["2026-11-02", "x"]])


# ----------------------------------------------------------------- live path against a fake sheet

class FakeWorksheet:
    def __init__(self, rows):
        self.rows = rows
        self.appends = 0

    def get(self, **_):
        return [list(r) for r in self.rows]

    def append_rows(self, payload, value_input_option, table_range):
        assert value_input_option == "RAW"
        self.rows.extend(payload)
        self.appends += 1


class FakeSpreadsheet:
    def __init__(self, ws):
        self.ws = ws

    def worksheet(self, name):
        assert name == S["tabs"]["history"], "may only ever touch the history tab"
        return self.ws


@pytest.fixture
def live(monkeypatch, tmp_path):
    ws = FakeWorksheet([])
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("TARGET", "test")
    monkeypatch.delenv("SHEET_XLSX", raising=False)
    monkeypatch.setattr(hs, "WORK", tmp_path)
    monkeypatch.setattr("common.WORK", tmp_path)
    monkeypatch.setattr(hs, "load_grid", lambda s, rt: (make_grid(), "fake"))
    monkeypatch.setattr(hs, "load_tab_rows", lambda s, rt, tab: ws.get())
    monkeypatch.setattr(hs, "open_spreadsheet", lambda s, rt: FakeSpreadsheet(ws))
    return ws


def test_live_writes_once_then_noop(live):
    assert hs.main(["--snapshot-date", "2026-11-09"]) == 0
    assert live.appends == 1
    assert live.rows[0] == hs.HISTORY_COLUMNS and len(live.rows) == 1 + 3
    assert hs.main(["--snapshot-date", "2026-11-09"]) == 0   # same day again
    assert live.appends == 1 and len(live.rows) == 4
    assert hs.main(["--snapshot-date", "2026-11-16"]) == 0   # next week: appended, no 2nd header
    assert live.appends == 2 and len(live.rows) == 7
    assert sum(1 for r in live.rows if r[0] == "snapshot_date") == 1


def test_live_missing_tab_aborts(live, monkeypatch):
    monkeypatch.setattr(hs, "load_tab_rows", lambda s, rt, tab: None)
    with pytest.raises(ConfigError):
        hs.main([])
    assert live.appends == 0


def test_dry_run_never_opens_sheet_for_writing(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setattr(hs, "WORK", tmp_path)
    monkeypatch.setattr("common.WORK", tmp_path)
    monkeypatch.setattr(hs, "load_grid", lambda s, rt: (make_grid(), "fake"))
    monkeypatch.setattr(hs, "load_tab_rows", lambda s, rt, tab: None)

    def boom(*a):
        raise AssertionError("dry run must not open the sheet for writing")
    monkeypatch.setattr(hs, "open_spreadsheet", boom)
    assert hs.main(["--snapshot-date", "2026-11-09"]) == 0
    assert (tmp_path / "history_2026-11-09.csv").read_text().count("\n") == 4
