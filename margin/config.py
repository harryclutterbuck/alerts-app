import os
from dataclasses import dataclass


@dataclass
class Config:
    db_path: str = "margin.db"
    secret: str = "dev-secret-change-me"
    base_url: str = "http://localhost:5000"        # where this API is reachable (used in email links)
    frontend_url: str = "http://localhost:3000"    # where the manage page lives
    allowed_origin: str = "*"                      # CORS origin for the signup page
    mailer: str = "console"                        # console | smtp
    outbox_dir: str = "outbox"
    from_addr: str = "Margin <alerts@example.com>"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    contact_email: str = "you@example.com"         # sent to OpenAlex/NCBI so they can contact you
    ncbi_api_key: str = ""
    openalex_api_key: str = ""                     # free key from openalex.org/settings/api (required since Feb 2026)
    arxiv_categories: tuple = ("cs.AI", "cs.CL", "cs.LG", "cs.CV", "stat.ML", "q-bio.GN", "quant-ph")
    max_per_alert: int = 25
    max_alerts_per_subscriber: int = 20

    @classmethod
    def from_env(cls):
        e = os.environ.get
        d = cls()
        return cls(
            db_path=e("MARGIN_DB", d.db_path),
            secret=e("MARGIN_SECRET", d.secret),
            base_url=e("MARGIN_BASE_URL", d.base_url).rstrip("/"),
            frontend_url=e("MARGIN_FRONTEND_URL", d.frontend_url).rstrip("/"),
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
            openalex_api_key=e("OPENALEX_API_KEY", d.openalex_api_key),
            arxiv_categories=tuple(
                c.strip() for c in e("ARXIV_CATEGORIES", ",".join(d.arxiv_categories)).split(",") if c.strip()
            ),
        )
