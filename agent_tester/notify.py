"""Email the report and milestones (FR-23) to the user's own inbox only. Another recipient needs confirmation."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

from .redact import redact


class EmailNotAllowed(PermissionError):
    pass


def email_config() -> dict:
    return {"to": os.environ.get("REPORT_EMAIL_TO", ""), "user": os.environ.get("SMTP_USER", ""),
            "password_env": "SMTP_APP_PASSWORD", "host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
            "port": int(os.environ.get("SMTP_PORT", "587"))}


def make_notifier(confirm=input, smtp_factory=smtplib.SMTP, out=print):
    """Returns notify(subject, body, attachment_path, to=None). Sends only to REPORT_EMAIL_TO unless the user
    confirms a different address at the console."""
    cfg = email_config()

    def notify(subject: str, body: str, attachment: Path | None, to: str | None = None) -> bool:
        own = cfg["to"]
        if not own or not cfg["user"] or not os.environ.get(cfg["password_env"]):
            raise EmailNotAllowed("email is not set up (REPORT_EMAIL_TO, SMTP_USER, SMTP_APP_PASSWORD)")
        target = to or own
        if target.lower() != own.lower():
            a = confirm(f"Send '{subject}' to {target}, which is not your own address? [y/N] ").strip().lower()
            if a not in ("y", "yes"):
                raise EmailNotAllowed(f"not confirmed: {target}")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = cfg["user"], target, subject
        msg.set_content(redact(body, keep_emails=(own,)))
        if attachment and Path(attachment).exists():
            p = Path(attachment)
            sub = "pdf" if p.suffix == ".pdf" else "html"
            msg.add_attachment(p.read_bytes(), maintype="application" if sub == "pdf" else "text", subtype=sub, filename=p.name)
        with smtp_factory(cfg["host"], cfg["port"], timeout=30) as s:
            s.starttls()
            s.login(cfg["user"], os.environ[cfg["password_env"]])
            s.send_message(msg)
        out(f"Emailed '{subject}' to {target}.")
        return True

    return notify
