"""Pull new papers from every source into the database. Safe to re-run: duplicates are merged."""
import logging
from datetime import timedelta

from . import db
from .config import Config
from .sources import arxiv, openalex, pubmed

log = logging.getLogger("margin.ingest")


# How far back each source looks on every pull. They differ on purpose:
#  - PubMed filters by entry date (no lag) but returns at most 10,000 records, and a day is ~6,000, so keep it short.
#  - arXiv is paged newest-first and capped at 3,000, so a short window is all it can use anyway.
#  - OpenAlex has ~9,000+ new works a day (measured), so 3 days is ~130 requests; a longer window would exceed the page cap.
LOOKBACK_DAYS = {"pubmed": 1, "arxiv": 2, "openalex": 3}


def fetchers(cfg: Config, days: int | None = None):
    """`days` (from the command line) or cfg.ingest_days (MARGIN_INGEST_DAYS) overrides every source's default."""
    override = days if days is not None else cfg.ingest_days
    since = lambda name: (db.now_dt() - timedelta(days=override if override is not None else LOOKBACK_DAYS[name])).date()
    return {
        "openalex": lambda: openalex.fetch(since("openalex"), cfg.contact_email, cfg.openalex_api_key, cfg.openalex_max_pages),
        "arxiv": lambda: arxiv.fetch(since("arxiv"), cfg.arxiv_categories),
        "pubmed": lambda: pubmed.fetch(since("pubmed"), cfg.contact_email, cfg.ncbi_api_key),
    }


def run_ingest(conn, cfg: Config, days: int | None = None, only=None, fetch_map=None) -> dict:
    """Safe to re-run: duplicates are merged, and the matches primary key stops anyone being told twice."""
    sources = fetch_map or fetchers(cfg, days)
    stats = {}
    for name, fn in sources.items():
        if only and name not in only:
            continue
        new = seen = 0
        try:
            for p in fn():
                _, is_new = db.upsert_paper(conn, p)
                seen += 1
                new += is_new
                if seen % 500 == 0:
                    conn.commit()
                    log.info("%s: %d papers so far (still working, safe to leave)", name, seen)
            db.set_state(conn, f"last_ingest_{name}", db.now_iso())
        except RuntimeError as e:          # a refusal we already explain in plain English
            log.error("%s stopped: %s", name, e)
            stats.setdefault("_errors", {})[name] = str(e)
        except Exception:
            log.exception("source %s failed; continuing with the others", name)
        conn.commit()
        stats[name] = {"seen": seen, "new": new}
        log.info("%s: %d seen, %d new", name, seen, new)
    return stats
