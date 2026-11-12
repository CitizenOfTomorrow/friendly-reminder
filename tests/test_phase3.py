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


def plan(after_cutoff, reported):
    now = dt.datetime(2026, 11, 12, 19, 0, tzinfo=BER) if after_cutoff else dt.datetime(2026, 11, 9, 10, 0, tzinfo=BER)
    return C.plan_drafts(statuses(reported), after_cutoff=after_cutoff, now=now, cutoff=C.cutoff_of(MON, S),
                         review_lines=["x: y"], settings=S)


def test_before_cutoff_only_complete_shows_get_updates():
    drafts, summary, week = plan(False, {"2026-11-20|Alpha|Neue Zukunft": {"dice", "tickettoaster"}})
    kinds = [(d.template, d.to) for d in drafts]
    assert kinds == [("contact_update", ["a@x.de"]), ("weekly_summary", ["ticketing@friendlyreminder.live"])]
    assert "Noch ausstehend" not in drafts[0].body and "(KW 46)" in drafts[0].subject
    assert week == "2026-W46"


def test_after_cutoff_incomplete_updates_and_band_reminders():
    drafts, summary, _ = plan(True, {"2026-11-20|Alpha|Neue Zukunft": {"dice", "tickettoaster"}})
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
    a, _, _ = plan(True, {})
    b, _, _ = plan(True, {})
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
