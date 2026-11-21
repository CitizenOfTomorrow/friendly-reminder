"""Phase 3 tests: week/cutoff, what counts as reported, show states, draft planning, outbox."""
import datetime as dt
import email
import email.policy
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import completeness as C  # noqa: E402
import outbox  # noqa: E402
from apply_sales import LOG_COLUMNS  # noqa: E402
from common import Cell, ConfigError, Grid, Runtime  # noqa: E402
from sheet_model import build_model  # noqa: E402
from test_phase0 import S, header_row, label, show  # noqa: E402

BER = ZoneInfo("Europe/Berlin")
MON = dt.datetime(2026, 11, 9, 0, 0, tzinfo=BER)
D = dt.datetime


def grid():
    return Grid({
        1: header_row(),
        2: label("NOVEMBER"),
        # complete candidate: Dice + TT allocated
        3: show(3, D(2026, 11, 20), "Alpha", "Neue Zukunft", AA=3, AB=20, AE=5, AF=50, AK="a@x.de",
                V=Cell("=..", 8), W=Cell("=..", 70)),
        # sold out
        4: show(4, D(2026, 11, 22), "Beta", "Privatclub", AC=100, AD=100, AK="b@x.de", V=Cell("=..", 100), W=Cell("=..", 100)),
        # band expected, AL with email / AL channel / AL empty
        5: show(5, D(2026, 11, 25), "Gamma", "Frannz Club", AC=10, AD=50, AG=1, AH=20, AK="g@x.de", AL="band@agency.de",
                V=Cell("=..", 11), W=Cell("=..", 70)),
        6: show(6, D(2026, 11, 26), "Delta", "Heimathafen", AC=10, AD=50, AG=1, AH=20, AK="d@x.de", AL="Rausgegangen",
                V=Cell("=..", 11), W=Cell("=..", 70)),
        7: show(7, D(2026, 11, 27), "Eps", "Badehaus", AC=10, AD=50, AG=1, AH=20, AK="e@x.de",
                V=Cell("=..", 11), W=Cell("=..", 70)),
        # no allocation at all, no contact
        8: show(8, D(2026, 11, 28), "Zeta", "Privatclub", V=Cell("=..", 0), W=Cell("=..", 0)),
        9: label("MASTER"),
        10: show(10, "xx", "xx", "xx"),
    })


def logrow(ts, termin, artist, venue, col, note="", dry="0"):
    r = [""] * len(LOG_COLUMNS)
    for k, v in dict(timestamp=ts, dry_run=dry, termin=termin, artist=artist, venue=venue, column=col, note=note).items():
        r[LOG_COLUMNS.index(k)] = v
    return r


def test_week_and_cutoff():
    now = dt.datetime(2026, 11, 12, 19, 0, tzinfo=BER)
    assert C.week_start(now) == MON
    assert C.cutoff_of(MON, S) == dt.datetime(2026, 11, 10, 12, 0, tzinfo=BER)


def test_reported_since_counts_changes_and_unchanged_only():
    rows = [LOG_COLUMNS,
            logrow("2026-11-09T10:00:00+01:00", "2026-11-20", "Alpha", "Neue Zukunft", "AA"),
            logrow("2026-11-09T10:00:00+01:00", "2026-11-20", "Alpha", "Neue Zukunft", "AE", "reported, unchanged (not written)"),
            logrow("2026-11-08T23:59:00+01:00", "2026-11-22", "Beta", "Privatclub", "AC"),                 # last week
            logrow("2026-11-09T10:00:00+01:00", "2026-11-22", "Beta", "Privatclub", "AC", dry="1"),         # dry run
            logrow("2026-11-09T10:00:00+01:00", "", "", "", "", "message processed: ..."),                  # marker
            logrow("2026-11-13T10:00:00+01:00", "2026-11-25", "Gamma", "Frannz Club", "AC")]                # after `until`
    r = C.reported_since(rows, MON, S, until=dt.datetime(2026, 11, 12, 20, 0, tzinfo=BER))
    assert r == {"2026-11-20|Alpha|Neue Zukunft": {"dice", "tickettoaster"}}


def statuses(reported: dict, prev: dict | None = None):
    m = build_model(grid(), S)
    return [C.evaluate(s, reported.get(s.identity(), set()), S, (prev or {}).get(s.identity()))
            for s in m.upcoming(dt.date(2026, 11, 12))]


def test_states():
    st = {s.show.artist: s for s in statuses({"2026-11-20|Alpha|Neue Zukunft": {"dice", "tickettoaster"}},
                                             {"2026-11-20|Alpha|Neue Zukunft": 5})}
    assert st["Alpha"].state == "complete" and st["Alpha"].zuwachs == 3
    assert st["Beta"].state == "sold_out"
    assert st["Gamma"].state == "incomplete" and st["Gamma"].missing == ["cts", "band"]
    assert st["Zeta"].state == "no_allocation"


def test_default_expected_providers_when_no_allocation():
    m = build_model(grid(), S)
    zeta = next(s for s in m.shows if s.artist == "Zeta")
    assert C.expected_providers(zeta, S) == S["default_expected_providers"]


def plan(after_cutoff, reported, drafted=None):
    now = dt.datetime(2026, 11, 12, 19, 0, tzinfo=BER) if after_cutoff else dt.datetime(2026, 11, 9, 10, 0, tzinfo=BER)
    return C.plan_drafts(statuses(reported), after_cutoff=after_cutoff, now=now, cutoff=C.cutoff_of(MON, S),
                         review_lines=["x: y"], settings=S, drafted=drafted)


def test_before_cutoff_only_complete_shows_get_updates():
    drafts, summary, week, _ = plan(False, {"2026-11-20|Alpha|Neue Zukunft": {"dice", "tickettoaster"}})
    kinds = [(d.template, d.to) for d in drafts]
    assert kinds == [("contact_update", ["a@x.de"]), ("weekly_summary", ["ticketing@friendlyreminder.live"])]
    assert "Noch ausstehend" not in drafts[0].body and "(KW 46)" in drafts[0].subject
    assert week == "2026-W46"


def test_after_cutoff_incomplete_updates_and_band_reminders():
    drafts, summary, _, _ = plan(True, {"2026-11-20|Alpha|Neue Zukunft": {"dice", "tickettoaster"}})
    by = {(d.template, tuple(d.to)) for d in drafts}
    assert ("contact_update", ("g@x.de",)) in by and ("contact_update", ("b@x.de",)) not in by   # sold out: none
    assert ("band_reminder", ("band@agency.de",)) in by                                          # AL email
    assert ("band_reminder", ("e@x.de",)) in by                                                  # AL empty -> AK
    assert not any(d.template == "band_reminder" and d.to == ["d@x.de"] for d in drafts)         # AL channel
    delta = next(b for b in summary["band_missing"] if b["artist"] == "Delta")
    assert delta["to"] == [] and delta["where"] == "Rausgegangen"
    gamma = next(d for d in drafts if d.template == "contact_update" and d.to == ["g@x.de"])
    assert "CTS: 10 (Stand Vorwoche)" in gamma.body and "Noch ausstehend: CTS, Band" in gamma.body
    summary_draft = drafts[-1]
    assert "AUSVERKAUFT – kein Update (1)" in summary_draft.body and "Zeta" in summary_draft.body
    assert "nach dem Cutoff Di 10.11. 12:00" in summary_draft.body


def test_keys_are_stable_per_week_and_show():
    a, _, _, _ = plan(True, {})
    b, _, _, _ = plan(True, {})
    assert [d.key for d in a[:-1]] == [d.key for d in b[:-1]]
    assert all(d.key.split("|")[1] == "2026-W46" for d in a)


def test_outbox_eml_roundtrip(tmp_path):
    d = outbox.render("band_reminder", {"artist": "A", "datum": "01.01.2027", "venue": "V", "signature": "T"},
                      to=["x@y.de"], key="band_reminder|2026-W46|k")
    d.attachments.append(("show.ics", "text/calendar", b"BEGIN:VCALENDAR\nEND:VCALENDAR\n"))
    [p] = outbox.EmlBackend(tmp_path, "Ticketing <t@fr.live>").save([d], subdir="w")
    m = email.message_from_bytes(p.read_bytes(), policy=email.policy.default)
    assert m["To"] == "x@y.de" and m["X-Unsent"] == "1" and m["Subject"].startswith("Bandshop-Zahlen A")
    assert [a.get_filename() for a in m.iter_attachments()] == ["show.ics"]


def test_outbox_refuses_mailbox_backends_until_approved():
    settings = dict(S, drafts=dict(S["drafts"], backend="imap"))
    with pytest.raises(ConfigError):
        outbox.get_backend(settings, Runtime(dry_run=False, target="test", sheet_xlsx=None))
    assert isinstance(outbox.get_backend(settings, Runtime(True, "test", None)), outbox.EmlBackend)


def test_no_code_path_can_send():
    src = "\n".join(p.read_text() for p in (Path(__file__).parent.parent / "scripts").glob("*.py"))
    for forbidden in ("smtplib", "send_message(", ".sendmail(", "messages().send", "drafts().send"):
        assert forbidden not in src, forbidden


# ----------------------------------------------------------------- idempotency (HANDOVER §6.5)

ALPHA = {"2026-11-20|Alpha|Neue Zukunft": {"dice", "tickettoaster"}}
GAMMA = "2026-11-25|Gamma|Frannz Club"


def test_summary_is_redrafted_only_when_its_content_changes():
    first, _, _, rec1 = plan(False, ALPHA)
    key1 = first[-1].key
    assert key1.startswith("weekly_summary|2026-W46|before_cutoff|")
    # nothing changed: same key, no new summary
    again, summary, _, rec2 = plan(False, ALPHA, drafted={r["key"]: r for r in rec1})
    assert again == [] and rec2 == [] and key1 in summary["skipped"]
    # same phase, Alpha's VVK changed since its draft: a new summary that says so
    changed, summary, _, _ = plan(False, ALPHA, drafted={r["key"]: {**r, "vvk": 6} for r in rec1})
    assert [d.template for d in changed] == ["weekly_summary"] and changed[0].key != key1
    assert "Alpha (Neue Zukunft): im Entwurf 6, jetzt 8 / 70" in changed[0].body
    # after the cutoff: a different phase, so a separate summary
    after, _, _, _ = plan(True, ALPHA, drafted={r["key"]: r for r in rec1})
    assert after[-1].key.startswith("weekly_summary|2026-W46|after_cutoff|")


def test_already_drafted_keys_are_skipped_and_newer_numbers_noted():
    first, _, _, records = plan(False, ALPHA)
    assert [r["key"] for r in records] == [d.key for d in first]
    alpha = next(r for r in records if r["template"] == "contact_update")
    assert alpha["vvk"] == 8 and alpha["artist"] == "Alpha" and alpha["termin"] == "2026-11-20"
    assert alpha["key"] == "contact_update|2026-W46|2026-11-20|Alpha|Neue Zukunft" and alpha["missing"] == []

    drafted = {r["key"]: {**r, "vvk": 6} for r in records}
    after, summary, _, _ = plan(True, ALPHA, drafted=drafted)
    assert not any(d.template == "contact_update" and d.to == ["a@x.de"] for d in after)
    assert [(n["artist"], n["before"], n["vvk"]) for n in summary["newer"]] == [("Alpha", 6, 8)]
    assert alpha["key"] in summary["skipped"]
    body = after[-1].body
    assert "Alpha (Neue Zukunft): 8 / 70 (Entwurf bereits erstellt)" in body


def test_incomplete_draft_is_followed_by_one_complete_update():
    # after the cutoff Gamma is incomplete (CTS and Band missing): update listing them
    first, _, _, rec = plan(True, ALPHA)
    gamma_inc = f"contact_update|2026-W46|{GAMMA}|incomplete"
    assert gamma_inc in {r["key"] for r in rec}
    drafted = {r["key"]: r for r in rec}
    # CTS reports, VVK unchanged: still incomplete, no second draft, but the summary notes the change
    _, summary, _, rec2 = plan(True, {**ALPHA, GAMMA: {"cts"}}, drafted=drafted)
    assert gamma_inc not in {r["key"] for r in rec2}
    gamma = next(n for n in summary["newer"] if n["artist"] == "Gamma")
    assert gamma["before"] == gamma["vvk"] == 11 and gamma["before_missing"] == ["CTS", "Band"]
    assert gamma["missing"] == ["Band"]
    # band reports too, VVK unchanged: now complete -> one complete update, flagged as replacing the old one
    drafts, summary, _, rec3 = plan(True, {**ALPHA, GAMMA: {"cts", "band"}}, drafted=drafted)
    new = [d for d in drafts if d.template == "contact_update" and d.to == ["g@x.de"]]
    assert len(new) == 1 and new[0].key == f"contact_update|2026-W46|{GAMMA}"
    assert "Noch ausstehend" not in new[0].body
    assert "-> Entwurf an g@x.de (ersetzt den unvollständigen Entwurf)" in drafts[-1].body


def test_draft_log_rows_roundtrip():
    rows = [LOG_COLUMNS,
            C.draft_log_row({"key": "contact_update|2026-W46|k|incomplete", "vvk": 8, "termin": "2026-11-20",
                             "artist": "Alpha", "venue": "Neue Zukunft", "missing": ["cts", "band"]},
                            "2026-11-09T10:00:00+01:00", "r1"),
            C.draft_log_row({"key": "weekly_summary|2026-W46|before_cutoff|abc", "vvk": None},
                            "2026-11-09T10:00:00+01:00", "r1"),
            logrow("2026-11-09T10:00:00+01:00", "", "", "", "", "draft created: x|dry", dry="1"),
            logrow("2026-11-09T10:00:00+01:00", "2026-11-20", "Alpha", "Neue Zukunft", "AA")]
    assert C.drafted_from_log(rows) == {
        "contact_update|2026-W46|k|incomplete": {"vvk": 8, "missing": ["cts", "band"]},
        "weekly_summary|2026-W46|before_cutoff|abc": {"vvk": None, "missing": []}}
    # draft rows never count as a provider report
    assert C.reported_since(rows, MON, S) == {"2026-11-20|Alpha|Neue Zukunft": {"dice"}}


# ----------------------------------------------------------------- main() end to end, fake sheet

class FakeLog:
    def __init__(self, rows, fail=False):
        self.rows, self.appends, self.fail = rows, 0, fail

    def get(self):
        return [list(r) for r in self.rows]

    def append_rows(self, payload, value_input_option, table_range):
        assert value_input_option == "RAW"
        if self.fail:
            raise RuntimeError("quota exceeded")
        self.rows.extend(payload)
        self.appends += 1


class FakeSpreadsheet:
    def __init__(self, log):
        self.log = log

    def worksheet(self, name):
        assert name == S["tabs"]["log"], "completeness may only ever append to the log tab"
        return self.log


@pytest.fixture
def env(monkeypatch, tmp_path):
    log = FakeLog([list(LOG_COLUMNS),
                   logrow("2026-11-09T10:00:00+01:00", "2026-11-20", "Alpha", "Neue Zukunft", "AA"),
                   logrow("2026-11-09T10:00:00+01:00", "2026-11-20", "Alpha", "Neue Zukunft", "AE")])
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("TARGET", "test")
    monkeypatch.delenv("SHEET_XLSX", raising=False)
    monkeypatch.setattr("common.WORK", tmp_path)
    monkeypatch.setattr(C, "load_grid", lambda s, rt: (grid(), "fake"))
    monkeypatch.setattr(C, "load_tab_rows", lambda s, rt, tab: log.get() if tab == S["tabs"]["log"] else None)
    monkeypatch.setattr(C, "drafts_root", lambda s: tmp_path / "drafts")
    monkeypatch.setattr(C, "get_backend", lambda s, rt: outbox.EmlBackend(tmp_path / "drafts", "t@fr.live"))
    return log, tmp_path / "drafts" / "2026-W46"


def emls(d):
    return sorted(p.name for p in d.glob("*.eml"))


def no_sheet(monkeypatch):
    def boom(*a):
        raise AssertionError("dry run must not open the sheet")
    monkeypatch.setattr(C, "open_spreadsheet", boom)


def test_main_dry_run_drafts_once_then_ledger_holds(env, monkeypatch):
    log, out = env
    no_sheet(monkeypatch)
    assert C.main(["--now", "2026-11-09T13:00"]) == 0
    first = emls(out)
    assert len(first) == 2 and any("alpha" in n for n in first) and any("before-cutoff" in n for n in first)
    assert len(C.json.loads((out / "drafted_test_sheet.json").read_text())) == 2
    for p in out.glob("*.eml"):
        p.unlink()
    assert C.main(["--now", "2026-11-09T15:00"]) == 0          # nothing changed: nothing new
    assert emls(out) == []
    assert C.main(["--now", "2026-11-09T15:00", "--fresh"]) == 0
    assert emls(out) == first


def test_main_dry_run_ledgers_are_per_source(env, monkeypatch, tmp_path):
    log, out = env
    no_sheet(monkeypatch)
    monkeypatch.setenv("SHEET_XLSX", str(tmp_path / "Demo Export.xlsx"))
    assert C.main(["--now", "2026-11-09T13:00"]) == 0
    assert (out / "drafted_test_xlsx-demo-export.json").exists()
    for p in out.glob("*.eml"):
        p.unlink()
    monkeypatch.delenv("SHEET_XLSX")
    assert C.main(["--now", "2026-11-09T13:00"]) == 0          # sheet dry run isn't hidden by the xlsx one
    assert len(emls(out)) == 2


def test_main_fresh_keeps_log_keys_out_of_the_local_ledger(env, monkeypatch):
    log, out = env
    no_sheet(monkeypatch)
    log.rows.append(C.draft_log_row({"key": "contact_update|2026-W46|2026-11-20|Alpha|Neue Zukunft", "vvk": 8},
                                    "2026-11-09T11:00:00+01:00", "r0"))
    assert C.main(["--now", "2026-11-09T13:00", "--fresh"]) == 0
    assert not any("alpha" in n for n in emls(out))             # recorded live: never drafted again
    local = C.json.loads((out / "drafted_test_sheet.json").read_text())
    assert all(k.startswith("weekly_summary") for k in local)


def test_main_live_records_drafts_in_log_once(env, monkeypatch):
    log, out = env
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(C, "open_spreadsheet", lambda s, rt: FakeSpreadsheet(log))
    assert C.main(["--now", "2026-11-10T13:00"]) == 0           # after cutoff
    assert log.appends == 1
    keys = set(C.drafted_from_log(log.rows))
    assert any(k.startswith("weekly_summary|2026-W46|after_cutoff|") for k in keys)
    assert "contact_update|2026-W46|2026-11-20|Alpha|Neue Zukunft" in keys
    assert f"contact_update|2026-W46|{GAMMA}|incomplete" in keys
    assert f"band_reminder|2026-W46|{GAMMA}" in keys
    assert not list(out.glob("drafted_*.json"))
    n = len(log.rows)
    assert C.main(["--now", "2026-11-10T14:00"]) == 0
    assert log.appends == 1 and len(log.rows) == n                # second run: no new drafts, no new rows


def test_main_live_log_failure_saves_no_drafts_and_writes_report(env, monkeypatch, tmp_path):
    log, out = env
    log.fail = True
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(C, "open_spreadsheet", lambda s, rt: FakeSpreadsheet(log))
    with pytest.raises(RuntimeError):
        C.main(["--now", "2026-11-10T13:00"])
    assert emls(out) == []
    [report] = tmp_path.glob("run_*_completeness.md")
    assert "appending" in report.read_text() and "quota exceeded" in report.read_text()


@pytest.mark.parametrize("dry", ["0", "1"])
def test_main_wrong_log_header_aborts(env, monkeypatch, dry):
    log, out = env
    log.rows[0] = ["timestamp", "message_id"]                 # prototype / reordered tab
    monkeypatch.setenv("DRY_RUN", dry)
    monkeypatch.setattr(C, "open_spreadsheet", lambda s, rt: pytest.fail("must not write"))
    with pytest.raises(ConfigError):
        C.main(["--now", "2026-11-10T13:00"])
    assert emls(out) == [] and log.appends == 0


def test_main_live_without_log_tab_aborts(env, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(C, "load_tab_rows", lambda s, rt, tab: None)
    monkeypatch.setattr(C, "open_spreadsheet", lambda s, rt: pytest.fail("must not write"))
    with pytest.raises(ConfigError):
        C.main(["--now", "2026-11-10T13:00"])
