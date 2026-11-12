"""Outbox: render email drafts from templates and hand them to a draft backend. Never sends.

Reusable by any workflow (weekly updates, reminders, intake mails, ...):

    from outbox import Draft, render, get_backend
    d = render("contact_update", ctx, to=["a@b.de"], key="contact_update|2026-W46|<show_key>")
    get_backend(settings, rt).save([d])

Templates live in config/templates/<name>.txt.j2. First line "Subject: ...", then a blank line, then the body.
Backends:
    eml   -> .eml files in work/drafts/<week>/ (open in any mail client; marked X-Unsent so it opens as a draft)
    imap  -> IMAP APPEND to the Drafts folder      (not enabled yet: needs explicit approval)
    gmail -> Gmail API drafts.create               (not enabled yet: needs explicit approval)
There is deliberately no backend that sends.
"""
from __future__ import annotations

import dataclasses
import email.utils
import re
from email.message import EmailMessage
from pathlib import Path

import jinja2

from common import ROOT, ConfigError, Runtime

TEMPLATE_DIR = ROOT / "config" / "templates"


@dataclasses.dataclass
class Draft:
    key: str                      # idempotency key, e.g. "contact_update|2026-W46|<show_key>"
    template: str
    subject: str
    body: str
    to: list[str]
    cc: list[str] = dataclasses.field(default_factory=list)
    attachments: list[tuple[str, str, bytes]] = dataclasses.field(default_factory=list)  # (filename, mime, data)

    def to_message(self, sender: str) -> EmailMessage:
        m = EmailMessage()
        m["From"] = sender
        if self.to:
            m["To"] = ", ".join(self.to)
        if self.cc:
            m["Cc"] = ", ".join(self.cc)
        m["Subject"] = self.subject
        m["Date"] = email.utils.formatdate(localtime=True)
        m["X-Unsent"] = "1"                    # Outlook / Apple Mail open the file as an editable draft
        m["X-Draft-Key"] = self.key
        m.set_content(self.body)
        for fn, mime, data in self.attachments:
            maintype, _, subtype = mime.partition("/")
            m.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=fn)
        return m


_env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)), undefined=jinja2.StrictUndefined,
                          keep_trailing_newline=True, autoescape=False)


def render(template: str, ctx: dict, *, to: list[str], key: str, cc: list[str] | None = None) -> Draft:
    text = _env.get_template(f"{template}.txt.j2").render(**ctx)
    first, _, body = text.partition("\n")
    if not first.startswith("Subject:"):
        raise ConfigError(f"template {template}: first line must be 'Subject: ...'")
    return Draft(key=key, template=template, subject=first[len("Subject:"):].strip(),
                 body=body.lstrip("\n"), to=to, cc=cc or [])


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


class EmlBackend:
    name = "eml"

    def __init__(self, out_dir: Path, sender: str):
        self.out_dir, self.sender = out_dir, sender

    def save(self, drafts: list[Draft], subdir: str = "") -> list[Path]:
        d = self.out_dir / subdir
        d.mkdir(parents=True, exist_ok=True)
        paths = []
        for dr in drafts:
            p = d / f"{slug(dr.key.replace('|', '-'))}.eml"
            p.write_bytes(bytes(dr.to_message(self.sender)))
            paths.append(p)
        return paths


def get_backend(settings: dict, rt: Runtime):
    cfg = settings["drafts"]
    backend = cfg.get("backend", "eml")
    if rt.dry_run or backend == "eml":
        return EmlBackend(ROOT / cfg["out_dir"], cfg["from"])
    raise ConfigError(f"draft backend {backend!r} is not enabled yet. Only 'eml' exists; putting drafts into "
                      "a mailbox needs an explicit OK first (HANDOVER §6.5).")
