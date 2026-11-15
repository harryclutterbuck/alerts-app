"""arXiv Atom API. Etiquette: one request every ~3 seconds."""
import logging
import time
import xml.etree.ElementTree as ET
from datetime import date

from .http import get

log = logging.getLogger("margin.arxiv")
BASE = "http://export.arxiv.org/api/query"
NS = {"a": "http://www.w3.org/2005/Atom"}


def parse(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    out = []
    for e in root.findall("a:entry", NS):
        abs_url = e.findtext("a:id", "", NS)
        aid = abs_url.rsplit("/abs/", 1)[-1]
        base_id = aid.rsplit("v", 1)[0] if "v" in aid else aid
        title = " ".join((e.findtext("a:title", "", NS) or "").split())
        if not title:
            continue
        out.append({
            "source": "arxiv",
            "source_id": base_id,
            "doi": f"10.48550/arxiv.{base_id}",   # arXiv-issued DOI, lets us dedupe against OpenAlex
            "title": title,
            "abstract": " ".join((e.findtext("a:summary", "", NS) or "").split()),
            "authors": [a.findtext("a:name", "", NS) for a in e.findall("a:author", NS)][:20],
            "venue": "arXiv",
            "published": (e.findtext("a:published", "", NS) or "")[:10],
            "url": f"https://arxiv.org/abs/{base_id}",
            "is_oa": True,
            "is_preprint": True,
        })
    return out


def fetch(since: date, categories, page=100, max_pages=30, http=get, sleep=time.sleep):
    query = " OR ".join(f"cat:{c}" for c in categories)
    for i in range(max_pages):
        papers = parse(http(BASE, {"search_query": query, "sortBy": "submittedDate", "sortOrder": "descending",
                                   "start": i * page, "max_results": page}))
        if not papers:
            return
        for p in papers:
            if p["published"] and p["published"] < since.isoformat():
                return
            yield p
        sleep(3)
    log.warning("arXiv hit its %d-paper cap before reaching the start of the window, so older papers in it were skipped. "
                "That is fine for a regular schedule; use a shorter window if you see it constantly.", page * max_pages)
