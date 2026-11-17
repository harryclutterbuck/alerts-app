"""Europe PMC: preprints from about 30 servers (Research Square, SSRN, PsyArXiv, SportRxiv, Preprints.org,
bioRxiv, medRxiv and more), each with its abstract. Docs: https://europepmc.org/RestfulWebService

We ask only for preprints (SRC:PPR). Journal articles already arrive from PubMed and OpenAlex."""
import html
import json
import logging
import re
from datetime import date, timedelta

from .http import get

log = logging.getLogger("margin.europepmc")
BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_BLOCK = re.compile(r"</?(?:p|br|h\d|div|li|ul|ol|sec|title|abstract)\b[^>]*>", re.I)   # these separate words
_INLINE = re.compile(r"<[^>]+>")                                                                       # italic, sub, sup: just drop


def clean(text) -> str:
    """Europe PMC abstracts can contain markup such as <h4>Background</h4>; keep only the words."""
    return " ".join(html.unescape(_INLINE.sub("", _BLOCK.sub(" ", text or ""))).split())


def source_for(doi: str | None) -> str:
    """Label preprints by their server where we already have a source for it, so they merge with the OpenAlex copy
    and follow the same 'Where to look' ticks. Everything else is 'europepmc' (shown as 'Other preprints')."""
    d = (doi or "").lower()
    if d.startswith("10.1101/"):
        return "biorxiv"          # bioRxiv and medRxiv share this DOI prefix
    if d.startswith("10.48550/arxiv"):
        return "arxiv"
    return "europepmc"


def normalise(r: dict) -> dict | None:
    title = clean(r.get("title"))
    abstract = clean(r.get("abstractText"))
    published = (r.get("firstPublicationDate") or "")[:10]
    if not published and r.get("pubYear"):
        published = f"{r['pubYear']}-01-01"
    rid = str(r.get("id") or "")
    if not (title and abstract and published and rid):
        return None
    doi = r.get("doi") or None
    authors = [a.strip().rstrip(".") for a in (r.get("authorString") or "").split(",") if a.strip().rstrip(".")]
    venue = ((r.get("bookOrReportDetails") or {}).get("publisher") or "").strip() or "Preprint"
    return {
        "source": source_for(doi),
        "source_id": rid,
        "doi": doi,
        "title": title,
        "abstract": abstract,
        "authors": authors[:20],
        "venue": venue,
        "published": published,
        "url": f"https://doi.org/{doi}" if doi else f"https://europepmc.org/article/PPR/{rid}",
        "is_oa": str(r.get("isOpenAccess") or "").upper() == "Y",
        "is_preprint": True,
    }


def fetch(since: date, http=get, page_size=1000, max_pages=20):
    """Every preprint with an abstract first published since `since`. About 1,000 new preprints a day, so a few requests."""
    until = date.today() + timedelta(days=1)
    query = f"SRC:PPR AND HAS_ABSTRACT:y AND FIRST_PDATE:[{since.isoformat()} TO {until.isoformat()}]"
    cursor, got, seen = "*", 0, 0
    for _ in range(max_pages):
        data = json.loads(http(BASE, {"query": query, "format": "json", "resultType": "core",
                                      "pageSize": page_size, "cursorMark": cursor}))
        rows = (data.get("resultList") or {}).get("result") or []
        for r in rows:
            seen += 1
            p = normalise(r)
            if p:
                got += 1
                yield p
        nxt = data.get("nextCursorMark")
        if not rows or not nxt or nxt == cursor:
            break
        cursor = nxt
    else:
        log.warning("Europe PMC: stopped at the %d-page cap, so the oldest preprints in the window were skipped.", max_pages)
    if seen and not got:
        log.warning("Europe PMC returned %d records but none could be read. Its response format may have changed.", seen)
