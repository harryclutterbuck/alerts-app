import os
from dataclasses import dataclass


@dataclass
class Config:
    brand: str = "Margin"                          # product name shown to subscribers (pages, emails, privacy notice)
    db_path: str = "margin.db"
    secret: str = "dev-secret-change-me"
    base_url: str = "http://localhost:5000"        # where this API is reachable (used in email links)
    frontend_url: str = "http://localhost:3000"    # where the manage page lives
    allowed_origin: str = ""                       # CORS origin; leave empty when the page is served by this app
    mailer: str = "console"                        # console | smtp
    outbox_dir: str = "outbox"
    from_addr: str = "Margin <alerts@example.com>"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    contact_email: str = "you@example.com"         # sent to OpenAlex/NCBI so they can contact you
    ncbi_api_key: str = ""
    openalex_max_pages: int = 300                  # per run; 1 credit each, 200 papers each
    openalex_api_key: str = ""                     # free key from openalex.org/settings/api (required since Feb 2026)
    arxiv_categories: tuple = ("cs.AI", "cs.CL", "cs.LG", "cs.CV", "stat.ML", "q-bio.GN", "quant-ph")
    max_per_alert: int = 25
    operator: str = ""                             # who runs the service; shown in the privacy page and email footers
    digest_hour_utc: int = 7                       # daily/weekly digests go out at or after this hour (UTC)
    digest_weekday: int = 0                        # weekly digests: 0 = Monday
    trust_proxy: bool = False                      # true behind a host's HTTPS proxy so rate limits see real visitor IPs
    backup_dir: str = "backups"
    ingest_every_hours: int = 6                    # how often the worker pulls new papers
    ingest_days: int | None = None                 # set to force one lookback for every source; default is per source
    paper_retention_days: int = 35                 # the live preview looks back 30 days, so keeping more only costs disk
    max_alerts_per_subscriber: int = 20

    @classmethod
    def from_env(cls):
        e = os.environ.get
        d = cls()
        return cls(
            brand=(e("MARGIN_BRAND", d.brand) or d.brand).strip(),
            db_path=e("MARGIN_DB", d.db_path),
            secret=e("MARGIN_SECRET", d.secret),
            base_url=e("MARGIN_BASE_URL", d.base_url).rstrip("/"),
            # The manage page is served by this same app, so default to the API address.
            frontend_url=(e("MARGIN_FRONTEND_URL") or e("MARGIN_BASE_URL", d.base_url)).rstrip("/"),
            allowed_origin=e("MARGIN_ALLOWED_ORIGIN", d.allowed_origin),
            mailer=e("MARGIN_MAILER", d.mailer),
            outbox_dir=e("MARGIN_OUTBOX", d.outbox_dir),
            from_addr=e("MARGIN_FROM", d.from_addr),
            smtp_host=e("SMTP_HOST", d.smtp_host),
            smtp_port=int(e("SMTP_PORT", d.smtp_port)),
            smtp_user=e("SMTP_USER", d.smtp_user),
            smtp_password=e("SMTP_PASSWORD", d.smtp_password),
            contact_email=e("MARGIN_CONTACT_EMAIL", d.contact_email),
            ncbi_api_key=e("NCBI_API_KEY", d.ncbi_api_key),
            operator=e("MARGIN_OPERATOR", d.operator),
            digest_hour_utc=int(e("MARGIN_DIGEST_HOUR", d.digest_hour_utc)),
            digest_weekday=int(e("MARGIN_DIGEST_WEEKDAY", d.digest_weekday)),
            trust_proxy=e("MARGIN_TRUST_PROXY", "").lower() in ("1", "true", "yes"),
            backup_dir=e("MARGIN_BACKUP_DIR", d.backup_dir),
            ingest_every_hours=int(e("MARGIN_INGEST_HOURS", d.ingest_every_hours)),
            ingest_days=int(e("MARGIN_INGEST_DAYS")) if e("MARGIN_INGEST_DAYS") else None,
            paper_retention_days=int(e("MARGIN_PAPER_RETENTION_DAYS", d.paper_retention_days)),
            openalex_api_key=e("OPENALEX_API_KEY", d.openalex_api_key).strip(),
            openalex_max_pages=int(e("OPENALEX_MAX_PAGES", d.openalex_max_pages)),
            arxiv_categories=tuple(
                c.strip() for c in e("ARXIV_CATEGORIES", ",".join(d.arxiv_categories)).split(",") if c.strip()
            ),
        )

    def problems(self) -> list[str]:
        """Things that must be fixed before real email is sent. Empty list = ready."""
        out = []
        if self.secret == Config.secret or len(self.secret) < 24:
            out.append("MARGIN_SECRET must be a long random value (24+ characters). Make one with: python3 -c \"import secrets; print(secrets.token_urlsafe(40))\"")
        if not self.base_url.startswith("https://"):
            out.append("MARGIN_BASE_URL must be your public https:// address, because every email link is built from it.")
        if "example.com" in self.from_addr or "example.com" in self.contact_email or not self.contact_email:
            out.append("MARGIN_FROM and MARGIN_CONTACT_EMAIL must be real addresses on your own domain.")
        if not (self.smtp_host and self.smtp_user and self.smtp_password):
            out.append("SMTP_HOST, SMTP_USER and SMTP_PASSWORD must be set (from your email provider).")
        if not self.operator:
            out.append("MARGIN_OPERATOR must be set to your name or organisation; it appears in the privacy page and email footers.")
        return out
