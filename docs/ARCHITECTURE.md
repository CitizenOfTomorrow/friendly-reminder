# From one workflow to many: a modular structure (proposal)

Status: proposal, 2026-11-12. Nothing here is built yet beyond what's marked **exists**.

The team has many ideas for automation, and almost all of them have the same shape:

> something happens (a mail arrives, a date approaches, someone fills a row) → read the sheet →
> apply some rules → write a few cells and/or prepare emails for a human to send.

So instead of one script per idea, build a few **reusable building blocks** and make each idea a small
**workflow** that plugs them together.

## 1. Three layers

```
 workflows/            what the team wants done          weekly_report, show_intake, promo_mails, ...
     │  uses
 domain/               what things are                   Show, Report, Contact, Draft, ReviewCase
     │  uses
 adapters/             where data lives                  sheets, mail_in, mail_out, drive, calendar
```

### Adapters: one interface each, exchangeable implementation

| Adapter | Does | Implementations | Today |
|---|---|---|---|
| `sheets` | read a tab fresh, write cells/rows, **enforce what a workflow may write** | gspread, xlsx (read-only) | **exists** in `common.py`; write guards are per-script asserts |
| `mail_in` | list new mails as canonical records | IMAP, Gmail connector import, Gmail API | **exists**: `fetch_reports.py`, `import_gmail_json.py`, `mail_model.py` |
| `mail_out` | turn a `Draft` into a draft somewhere; **no send method exists** | eml files, IMAP APPEND, Gmail drafts | **exists**: `outbox.py` (eml only) |
| `drive` | copy a template folder, link files | Google Drive API | Phase 4 |
| `calendar` | events as `.ics` attachments | ics writer | Phase 4 |

Swapping IMAP for the Gmail API, or eml for real drafts, is one config line, not a rewrite. That
matters here because IMAP is blocked in the cloud runtime.

### Domain: the vocabulary every workflow shares

`Show` (one row of the main tab, with providers, contacts, flags) **exists** in `sheet_model.py`.
Next to it: `MailRecord`, `Draft`, `ReviewCase`, and later `Contact` (from `Promo Kontakte`) and
`Venue`. A workflow never touches column letters; it asks for `show.providers["cts"].sold`.

### Workflows: small, declarative, testable

```
workflows/weekly_report/
  workflow.yaml        # trigger, inputs, allowed writes, drafts it may create
  run.py               # the deterministic part (rules)
  SKILL.md             # optional: the fuzzy part Claude does (extraction, matching, wording)
  schema.json          # what Claude must hand over (validated before anything is applied)
  templates/*.txt.j2   # the email texts, editable without touching code
  tests/
```

`workflow.yaml` sketch:

```yaml
name: weekly_report
trigger: {schedule: "Mon,Tue 10:00,14:00,18:00", also: manual}
reads:  [main, log, needs_review]
writes:
  main: [AA, AC, AE, AG, AI]       # enforced by the sheets adapter, not by convention
  tabs: [log, needs_review]
drafts: [contact_update, band_reminder, weekly_summary]
claude_step: {skill: SKILL.md, output_schema: schema.json}
```

One entry point runs everything the same way, locally or as a routine:

```
python -m fr list
python -m fr run weekly_report            # dry run by default
DRY_RUN=0 TARGET=test python -m fr run weekly_report
```

The runner does what every workflow needs and none should re-implement: config, DRY_RUN/TARGET
guards, a fresh sheet read, the run report, the heartbeat ping, and refusing writes outside `writes:`.

## 2. Four shared patterns

These come up in almost every idea, so they should be built once.

1. **Extract → validate → apply.** Claude turns messy input (report mails, deal memos, invoices,
   rider PDFs) into JSON matching a schema; code validates it and applies only what passes. Unclear
   cases go to review. This **exists** for report import (`apply_sales.py`); the same skeleton serves
   deal-memo intake or settlement mails.
2. **Queue tab with a status column.** A human fills a row and sets `bereit`; the workflow picks it up
   and writes `erledigt` / `fehler` plus a result. `neue_shows` (Phase 4) is the first one; any future
   request type (Gästeliste, Abrechnung, Promo-Anfrage) can be another tab with the same mechanics.
3. **Human in the loop via drafts and `needs_review`.** Nothing leaves the company without a person
   pressing send, and nothing ambiguous is decided by the machine. **Exists.**
4. **Journal.** Every workflow appends what it did (`log`) and what it couldn't decide
   (`needs_review`), so other workflows can build on it: completeness reads what the import logged;
   a digest could read everything. **Exists**, but each workflow has its own columns today. Proposal:
   one common shape `timestamp | run_id | workflow | kind | subject_key | payload_json`, with views
   for humans.

## 3. Ideas that fit these blocks

| Idea | Trigger | Blocks |
|---|---|---|
| Weekly ticket report (Phases 2–3) | Mon/Tue | mail_in → extract/validate → sheets → mail_out |
| Show intake (Phase 4) | queue tab `neue_shows` | sheets, drive, calendar, mail_out |
| Promo mails for a new show | queue / new row | `Promo Kontakte` + rules from its NOTIZ column ("ByteFM nicht, wenn radioeins an Bord") → drafts |
| Day-of-show info to the venue | 7 days before Termin | sheets → draft (doors, start, support, contact) |
| Sold-out notice to agency and promo partners | VVK ≥ KON | sheets → drafts |
| Missing-numbers chaser (band shops, Rausgegangen) | Tue after cutoff | journal → drafts / checklist |
| Settlement (Abrechnung) prep | after the show | Drive calculation + sheet → draft to agency |
| Digest for Dennis and Sönke | Friday | journal + history → one summary draft |
| Dice Partner API instead of Dice mails | Mon | new `mail_in`-like source, same validate/apply |

Each of these is mostly configuration plus a template and a few rules, once the blocks exist.

## 4. Getting there without a rewrite

1. **Now (done with Phase 3):** `outbox.py` is a standalone adapter; templates live in `config/templates/`.
2. **Next, small:** move the shared code into a package `fr/` (`config`, `sheets`, `mail_in`,
   `mail_out`) and add the `fr run <workflow>` runner. Existing scripts become thin wrappers.
3. **Then:** declare `writes:` per workflow and enforce it in the sheets adapter. This replaces the
   scattered asserts with one tested gate.
4. **Then:** turn Phases 1–3 into `workflows/history_snapshot`, `workflows/report_import`,
   `workflows/weekly_drafts`, and build Phase 4 directly in that shape.
5. **New ideas** start from a `workflows/_template/` folder.

Each step keeps all tests green and changes no behaviour.

## 5. What not to do (yet)

- No workflow engine (n8n, Airflow, …). Claude Code routines plus one runner are enough at this size.
- No database. The Google Sheet stays the UI the team already knows; helper tabs hold the journal.
- No sending, ever, without a person. If something should go out automatically one day, that is a
  separate, explicit decision per workflow.

## 6. Decisions for the team

1. Is the three-layer split and the `workflows/` folder shape OK?
2. One shared journal format, or keep a log per workflow?
3. Who may edit email templates (directly in the repo, or via a sheet tab the automation reads)?
4. Which next idea after Phase 4? It should be the first one built purely from the blocks.
