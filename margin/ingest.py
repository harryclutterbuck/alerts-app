"""Pull new papers from every source into the database. Safe to re-run: duplicates are merged."""
import logging
from datetime import date, timedelta

from . import db
from .config import Config
from .sources import arxiv, europepmc, openalex, pubmed

log = logging.getLogger("margin.ingest")


# How far back each source looks on every pull. They differ on purpose:
#  - PubMed filters by entry date (no lag) but returns at most 10,000 records, and a day is ~6,000, so keep it short.
#  - arXiv is paged newest-first and capped at 3,000, so a short window is all it can use anyway.
#  - OpenAlex has ~9,000+ new works a day (measured), so 7 days is ~330 requests (page cap 500). The window is wide because
#    OpenAlex files a paper under its publication date, which can be days before the paper first appears in OpenAlex;
#    a short window would never see those late arrivals. (Its "date added" filters are paid-only.)
#  - Europe PMC (preprints only) adds about 1,000 a day, so 3 days is a handful of requests.
LOOKBACK_DAYS = {"pubmed": 1, "arxiv": 2, "openalex": 7, "europepmc": 3}

# A paper first seen when it is already older than this counts as a "late arrival": it would have been missed by a window this short.
# We keep a running count so the real miss rate can be read from the logs. (Only meaningful for OpenAlex, which dates by publication.)
LATE_AFTER_DAYS = {"openalex": 3}


def fetchers(cfg: Config, days: int | None = None):
    """`days` (from the command line) or cfg.ingest_days (MARGIN_INGEST_DAYS) overrides every source's default."""
    override = days if days is not None else cfg.ingest_days
    since = lambda name: (db.now_dt() - timedelta(days=override if override is not None else LOOKBACK_DAYS[name])).date()
    return {
        "openalex": lambda: openalex.fetch(since("openalex"), cfg.contact_email, cfg.openalex_api_key, cfg.openalex_max_pages),
        "arxiv": lambda: arxiv.fetch(since("arxiv"), cfg.arxiv_categories),
        "pubmed": lambda: pubmed.fetch(since("pubmed"), cfg.contact_email, cfg.ncbi_api_key),
        "europepmc": lambda: europepmc.fetch(since("europepmc")),
    }


def run_ingest(conn, cfg: Config, days: int | None = None, only=None, fetch_map=None) -> dict:
    """Safe to re-run: duplicates are merged, and the matches primary key stops anyone being told twice."""
    sources = fetch_map or fetchers(cfg, days)
    stats = {}
    for name, fn in sources.items():
        if only and name not in only:
            continue
        new = seen = late = 0
        late_after = LATE_AFTER_DAYS.get(name)
        today = db.now_dt().date()
        try:
            for p in fn():
                _, is_new = db.upsert_paper(conn, p)
                seen += 1
                new += is_new
                if is_new and late_after is not None and _age_days(p.get("published"), today) > late_after:
                    late += 1
                if seen % 500 == 0:
                    conn.commit()
                    if seen % 5000 == 0:
                        log.info("%s: %d papers so far (still working, safe to leave)", name, seen)
            db.set_state(conn, f"last_ingest_{name}", db.now_iso())
        except RuntimeError as e:          # a refusal we already explain in plain English
            log.error("%s stopped: %s", name, e)
            stats.setdefault("_errors", {})[name] = str(e)
        except Exception:
            log.exception("source %s failed; continuing with the others", name)
        conn.commit()
        stats[name] = {"seen": seen, "new": new}
        if late_after is not None:
            stats[name]["late"] = late
            log.info("%s: %d seen, %d new. %s", name, seen, new, _late_report(conn, name, new, late, late_after))
        else:
            log.info("%s: %d seen, %d new", name, seen, new)
    return stats


def _age_days(published, today) -> int:
    try:
        return (today - date.fromisoformat(str(published)[:10])).days
    except ValueError:
        return 0


def _late_report(conn, name, new, late, late_after) -> str:
    """Keep a running tally of late arrivals so the miss rate of a short window can be read straight from the logs.
    The first run after this was introduced is not counted: it also catches up on papers older runs never asked for."""
    if db.get_state(conn, f"late_{name}_started") is None:
        db.set_state(conn, f"late_{name}_started", db.now_iso())
        return f"{late} of the new papers were more than {late_after} days old when first seen (first run, not counted)."
    tot_new = int(db.get_state(conn, f"late_{name}_new_total", 0)) + new
    tot_late = int(db.get_state(conn, f"late_{name}_late_total", 0)) + late
    db.set_state(conn, f"late_{name}_new_total", tot_new)
    db.set_state(conn, f"late_{name}_late_total", tot_late)
    pct = f"{100 * tot_late / tot_new:.1f}%" if tot_new else "n/a"
    return (f"{late} of the new papers were more than {late_after} days old when first seen. "
            f"RUNNING TOTAL since counting began: {tot_late} of {tot_new} new papers ({pct}) arrived late.")
