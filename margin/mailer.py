import json
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path
from urllib.parse import quote

from .config import Config


def build_message(cfg: Config, to, subject, html, text, headers=None) -> EmailMessage:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = cfg.from_addr, to, subject
    domain = parseaddr(cfg.from_addr)[1].rpartition("@")[2] or None     # a Message-ID on our own domain looks less like spam than the server's name
    m["Date"], m["Message-ID"] = formatdate(localtime=False), make_msgid(domain=domain)
    reply = parseaddr(cfg.contact_email or "")[1]
    if "@" in reply and "example.com" not in reply:                     # replies go to a real mailbox; people replying is a good sign to mail providers
        m["Reply-To"] = formataddr((cfg.brand, reply))
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(text)
    m.add_alternative(html, subtype="html")
    return m


class ConsoleMailer:
    """Development mailer: writes each email to ./outbox as .html and .eml instead of sending it."""

    def __init__(self, cfg: Config):
        self.cfg, self.dir, self.n = cfg, Path(cfg.outbox_dir), 0
        self.sent: list[dict] = []

    def send(self, to, subject, html, text, headers=None):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n += 1
        stem = f"{int(time.time())}-{self.n:03d}-{quote(to, safe='')}"
        (self.dir / f"{stem}.html").write_text(html, encoding="utf-8")
        (self.dir / f"{stem}.eml").write_text(build_message(self.cfg, to, subject, html, text, headers).as_string())
        self.sent.append({"to": to, "subject": subject, "html": html, "text": text, "headers": headers or {}})
        print(f"[mail] to={to} subject={subject!r} -> {self.dir / (stem + '.html')}")


class SMTPMailer:
    """Works with Resend, Postmark, Amazon SES or any provider that offers SMTP."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def send(self, to, subject, html, text, headers=None):
        msg = build_message(self.cfg, to, subject, html, text, headers)
        with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30) as s:
            s.starttls()
            s.login(self.cfg.smtp_user, self.cfg.smtp_password)
            s.send_message(msg)


def make_mailer(cfg: Config):
    return SMTPMailer(cfg) if cfg.mailer == "smtp" else ConsoleMailer(cfg)
