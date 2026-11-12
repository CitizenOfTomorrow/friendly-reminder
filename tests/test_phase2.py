"""Phase 2 tests: mail parsing, matching, validation rules, and the live path against a fake sheet."""
import datetime as dt
import json
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import apply_sales as A  # noqa: E402
from common import Cell, Grid  # noqa: E402
from fetch_reports import parse_message  # noqa: E402
from mail_model import detect_forward, normalize_message, provider_from_sender  # noqa: E402
from sheet_model import build_model  # noqa: E402
from test_phase0 import S, header_row, label, show  # noqa: E402

AS_OF = dt.date(2026, 11, 12)
D = dt.datetime


def grid():
    return Grid({
        1: header_row(),
        2: label("NOVEMBER"),
        3: show(3, D(2026, 11, 20), "Alpha", "Neue Zukunft", AA=3, AB=20, AC=10, AD=100, AE=5, AF=50, AK="a@x.de"),
        4: show(4, D(2026, 11, 22), "Romy Hausmann & Fortuna Ehrenfeld", "Heimathafen", AC=Cell("=115+19", 134),
                AD=200, AG=10, AH=100),
        5: show(5, D(2026, 11, 25), "Beta", "Kindl Brauerei Sudhaus", AE=Cell("w", "w"), AF=50, AG=0, AH=20),
        6: show(6, D(2026, 11, 28), "Gamma", "Frannz Club", AE=4, AF=50, AL="Bandshop Tickettoaster"),
        7: show(7, D(2026, 11, 29), "Delta", "Privatclub", AC=None),
        8: show(8, D(2026, 2, 20), "Typo", "Neue Zukunft"),
        9: label("MASTER"),
        10: show(10, "xx", "xx", "xx"),
    })


def model():
    m = build_model(grid(), S)
    return m, {s.row_number: s for s in m.upcoming(AS_OF)}


def mail(sender="x@unknown.de", mid="m1", body="", inner=None):
    return normalize_message(message_id=mid, source="test", subject="s", sender=sender, date="2026-11-09",
                             body_text=body, inner=inner)


def item(row, date, artist, sold, venue=None, conf=0.95):
    return {"row_number": row, "date": date, "artist": artist, "venue": venue, "tickets_sold": sold,
            "evidence": f"{artist} {sold}", "confidence": conf}


def run(m, provider, items):
    sh, up = model()
    return A.decide(m, {"message_id": m["message_id"], "provider": provider, "shows": items}, sh.shows, up, S)


# ----------------------------------------------------------------- mail parsing

def test_forward_detection_apple_gmail_outlook():
    apple = "> Anfang der weitergeleiteten Nachricht:\n>\n> Von: service@tickettoaster.de\n> Betreff: X\n> Datum: 9. November 2026"
    gmail = "---------- Forwarded message ---------\nFrom: DICE <dice-report@dice.fm>\nDate: Mon, 2 Nov 2026\nSubject: Weekly"
    outlook = "-----Ursprüngliche Nachricht-----\nVon: Eventim <reports@eventim.de>\nGesendet: Montag\nBetreff: Report"
    assert detect_forward(apple)["from"] == "service@tickettoaster.de"
    assert detect_forward(gmail) == {"from": "dice-report@dice.fm", "date": "Mon, 2 Nov 2026", "subject": "Weekly"}
    assert detect_forward(outlook)["from"] == "reports@eventim.de"
    assert detect_forward("Hallo, Verkaufte Tickets: 468") is None


def test_rfc822_attachment_is_unwrapped():
    inner = EmailMessage()
    inner["From"], inner["Subject"], inner["Message-ID"] = "dice-report@dice.fm", "Weekly", "<inner@dice>"
    inner["Date"] = "Mon, 02 Nov 2026 11:48:08 +0100"
    inner.set_content("Results are attached")
    inner.add_attachment(b"Event Name,Total Tickets Sold\nAlpha,7\n", maintype="text", subtype="csv", filename="r.csv")
    outer = EmailMessage()
    outer["From"], outer["Subject"], outer["Message-ID"] = "tester@gmail.com", "Fwd: Weekly", "<outer@gmail>"
    outer.set_content("see attached")
    outer.add_attachment(inner)  # message/rfc822
    rec = parse_message(outer.as_bytes())
    assert rec["effective_from"] == "dice-report@dice.fm"
    assert rec["message_id"] == "<inner@dice>" and rec["outer_message_id"] == "<outer@gmail>"
    assert rec["attachments"][0]["filename"] == "r.csv" and "Alpha,7" in rec["attachments"][0]["text"]
    assert provider_from_sender(rec["effective_from"], S) == "dice"


def test_missing_message_id_falls_back_to_content_hash():
    a = normalize_message(message_id="", source="t", subject="s", sender="a@b.de", date="d", body_text="x  y")
    b = normalize_message(message_id=None, source="t", subject="s", sender="a@b.de", date="d", body_text="x y")
    assert a["message_id"].startswith("hash:") and a["message_id"] == b["message_id"]


# ----------------------------------------------------------------- matching

@pytest.mark.parametrize("email_artist,sheet,ok", [
    ("FELSMANN + TILEY", "Felsmann + Tiley", True), ("Fortuna Ehrenfeld", "Romy Hausmann & Fortuna Ehrenfeld", True),
    ("Felix Meyer & Projekt ìle", "Felix Meyer & Projekt ile", True), ("Alpha", "Alphaville", False)])
def test_artist_matches(email_artist, sheet, ok):
    assert A.artist_matches(email_artist, sheet) is ok


@pytest.mark.parametrize("ev,sheet,ok", [
    ("Heimathafen Neukölln", "Heimathafen", True), ("Maschinenhaus (Kulturbrauerei)", "Maschinenhaus", True),
    ("Silent Green, Kuppelhalle", "Silent Green", True), ("Kindl Locations - Sudhaus", "Kindl Brauerei Sudhaus", True),
    ("Kantine am Berghain", "Kantine Am Berghain", True), ("Colosseum", "Colosseum, Saal 7", True),
    (None, "Privatclub", True), ("Kantine Ost, Berlin", "Kantine Am Berghain", False), ("Frannz Club", "Privatclub", False)])
def test_venue_matches(ev, sheet, ok):
    assert A.venue_matches(ev, sheet, S) is ok


# ----------------------------------------------------------------- decisions

def kinds(ds):
    return [(d.kind, d.show.row_number if d.show else None) for d in ds]


def test_eventim_multi_show_applies_to_cts_only():
    ds = run(mail("reports@eventim.de"), "cts", [item(3, "2026-11-20", "Alpha", 12, "Neue Zukunft"),
                                                  item(7, "2026-11-29", "Delta", 8, "Privatclub")])
    assert kinds(ds) == [("apply", 3), ("apply", 7)]
    assert {d.column for d in ds} == {"AC"} and ds[0].old == 10 and ds[1].old is None


def test_134_of_200_is_134_and_no_overallocation():
    # the extraction takes the first number of "134/200"; validation must accept it as is
    ds = run(mail("reports@eventim.de"), "cts", [item(3, "2026-11-20", "Alpha", 134)])
    assert ds[0].kind == "apply" and ds[0].new == 134 and "over allocation" in ds[0].note  # K = 100


def test_agency_band_shop_mail_goes_to_band_column():
    ds = run(mail("booking@some-agency.de"), "band", [item(5, "2026-11-25", "Beta", 7)])
    assert ds[0].kind == "apply" and ds[0].column == "AG" and "provider from content" in ds[0].note


def test_sender_beats_classification():
    ds = run(mail("reports@eventim.de"), "dice", [item(3, "2026-11-20", "Alpha", 12)])
    assert ds[0].column == "AC" and "sender wins" in ds[0].note


def test_forwarded_sender_counts():
    m = mail("ticketing@friendlyreminder.live", inner={"from": "dice-report@dice.fm", "date": "x"})
    assert run(m, "unknown", [item(3, "2026-11-20", "Alpha", 4)])[0].column == "AA"


@pytest.mark.parametrize("sender,provider,items,reason", [
    ("x@unknown.de", "unknown", [item(3, "2026-11-20", "Alpha", 4)], "unknown provider"),
    ("news@rausgegangen.de", "andere", [item(3, "2026-11-20", "Alpha", 4)], "not decided yet"),
    ("x@unknown.de", "andere", [item(3, "2026-11-20", "Alpha", 4)], "not decided yet"),
    ("service@tickettoaster.de", "band", [item(3, "2026-11-20", "Alpha", 4)], "TT (AE) or Band (AG)"),
    ("x@unknown.de", "dice", [], "no sales figures found"),
])
def test_message_level_reviews(sender, provider, items, reason):
    ds = run(mail(sender), provider, items)
    assert all(d.kind == "review" for d in ds) and reason in ds[0].reason


@pytest.mark.parametrize("it,reason", [
    (item(None, "2026-11-21", "The Paper Lanterns", 468), "no matching show"),
    (item(8, "2027-02-20", "Typo", 1), "not an upcoming show"),
    (item(3, "2026-11-21", "Alpha", 4), "!= sheet Termin"),
    (item(3, "2026-11-20", "Omega", 4), "doesn't match sheet"),
    (item(3, "2026-11-20", "Alpha", 4, venue="Frannz Club"), "venue mismatch"),
    (item(3, "2026-11-20", "Alpha", 4, conf=0.4), "confidence below"),
    (item(3, "2026-11-20", "Alpha", 2), "decrease: 3 -> 2"),
    (item(3, "2026-11-20", "Alpha", "4"), "not a non-negative integer"),
    (item(99, "2026-11-20", "Alpha", 4), "not an upcoming show"),
])
def test_item_reviews(it, reason):
    ds = run(mail("dice-report@dice.fm"), "dice", [it])
    assert ds[0].kind == "review" and reason in ds[0].reason, ds[0].reason


def test_typo_hint_points_at_row():
    ds = run(mail("dice-report@dice.fm"), "dice", [item(None, "2027-02-20", "TYPO", 1)])
    assert "row 8 has Typo on 2026-02-20" in ds[0].reason


def test_formula_and_text_cells_are_never_overwritten():
    ds = run(mail("reports@eventim.de"), "cts", [item(4, "2026-11-22", "Fortuna Ehrenfeld", 140)])
    assert ds[0].kind == "review" and "formula" in ds[0].reason
    ds = run(mail("service@tickettoaster.de"), "tickettoaster", [item(5, "2026-11-25", "Beta", 3)])
    assert ds[0].kind == "review" and "contains text" in ds[0].reason


def test_tickettoaster_band_shop_show_goes_to_review():
    ds = run(mail("service@tickettoaster.de"), "tickettoaster", [item(6, "2026-11-28", "Gamma", 9)])
    assert ds[0].kind == "review" and "band shop" in ds[0].reason


def test_same_row_twice_with_different_numbers():
    ds = run(mail("dice-report@dice.fm"), "dice", [item(3, "2026-11-20", "Alpha", 4), item(3, "2026-11-20", "Alpha", 5)])
    assert [d.kind for d in ds] == ["review", "review"]
    ds = run(mail("dice-report@dice.fm"), "dice", [item(3, "2026-11-20", "Alpha", 4), item(3, "2026-11-20", "Alpha", 4)])
    assert [d.kind for d in ds] == ["apply"]


def test_noop_and_zero_skip():
    assert run(mail("dice-report@dice.fm"), "dice", [item(3, "2026-11-20", "Alpha", 3)])[0].kind == "noop"
    ds = run(mail("service@tickettoaster.de"), "tickettoaster", [item(7, "2026-11-29", "Delta", 0)])
    assert ds[0].kind == "skip_zero"


# ----------------------------------------------------------------- end to end against a fake sheet

class FakeWS:
    def __init__(self, rows=None):
        self.rows, self.batch = rows or [], []

    def append_rows(self, payload, value_input_option, table_range):
        assert value_input_option == "RAW"
        self.rows.extend(payload)

    def batch_update(self, updates, value_input_option):
        assert value_input_option == "RAW"
        self.batch.extend(updates)


@pytest.fixture
def live(monkeypatch, tmp_path):
    tabs = {"log": FakeWS([A.LOG_COLUMNS]), "needs_review": FakeWS([A.REVIEW_COLUMNS]), S["tabs"]["main"]: FakeWS()}

    class Sheet:
        def worksheet(self, name):
            return tabs[name]

    (tmp_path / "mail").mkdir()
    (tmp_path / "ext").mkdir()
    settings = dict(S, mail_dir=str(tmp_path / "mail"), extraction_dir=str(tmp_path / "ext"))
    for key, sender, prov, items in [
        ("m1", "reports@eventim.de", "cts", [item(3, "2026-11-20", "Alpha", 12), item(4, "2026-11-22", "Fortuna Ehrenfeld", 140)]),
        ("m2", "x@unknown.de", "unknown", [item(None, "2026-11-21", "The Paper Lanterns", 468)]),
    ]:
        m = mail(sender, mid=key)
        (tmp_path / "mail" / f"{key}.json").write_text(json.dumps(m))
        (tmp_path / "ext" / f"{key}.json").write_text(json.dumps({"message_id": key, "provider": prov, "shows": items}))
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("TARGET", "test")
    monkeypatch.delenv("SHEET_XLSX", raising=False)
    monkeypatch.setattr(A, "load_settings", lambda: settings)
    monkeypatch.setattr("common.WORK", tmp_path)
    monkeypatch.setattr(A, "load_grid", lambda s, rt: (grid(), "fake"))
    monkeypatch.setattr(A, "load_tab_rows", lambda s, rt, tab: tabs[tab].rows)
    monkeypatch.setattr(A, "open_spreadsheet", lambda s, rt: Sheet())
    return tabs


def test_live_run_writes_only_sold_cell_then_rerun_is_noop(live, monkeypatch, tmp_path):
    assert A.main(["--as-of", "2026-11-12"]) == 0
    main_ws = live[S["tabs"]["main"]]
    assert main_ws.batch == [{"range": "AC3", "values": [[12]]}]          # formula cell AC4 untouched
    log = live["log"].rows
    assert len(log) == 1 + 1 + 2                                           # header, 1 change, 2 "processed" markers
    assert len(live["needs_review"].rows) == 1 + 2                         # formula cell + unknown show
    A.main(["--as-of", "2026-11-12"])                                      # second run: both mails already processed
    assert main_ws.batch == [{"range": "AC3", "values": [[12]]}]
    assert len(live["log"].rows) == 4 and len(live["needs_review"].rows) == 3


def test_writes_are_limited_to_sold_columns():
    sh, up = model()
    for p, spec in S["providers"].items():
        if p in ("andere",):
            continue
        sender = {"dice": "dice-report@dice.fm", "cts": "reports@eventim.de", "tickettoaster": "service@tickettoaster.de",
                  "band": "booking@agency.de"}[p]
        ds = A.decide(mail(sender), {"message_id": "m1", "provider": p, "shows": [item(3, "2026-11-20", "Alpha", 999)]},
                      sh.shows, up, S)
        assert all(d.column == spec["sold"]["col"] for d in ds if d.kind == "apply")
        assert all(d.column in ("AA", "AC", "AE", "AG", "AI") for d in ds if d.column)
