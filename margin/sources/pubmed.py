"""PubMed via NCBI E-utilities. Without an API key you may make ~3 requests/second."""
import json
import logging
import time
import xml.etree.ElementTree as ET
from datetime import date

from .http import get

log = logging.getLogger("margin.pubmed")
ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _text(el) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _date(art) -> str:
    d = art.find("ArticleDate")
    if d is not None and d.findtext("Year"):
        return f"{d.findtext('Year')}-{int(d.findtext('Month', '1')):02d}-{int(d.findtext('Day', '1')):02d}"
    pd = art.find("Journal/JournalIssue/PubDate")
    if pd is not None and pd.findtext("Year"):
        m = pd.findtext("Month", "1")
        m = _MONTHS.get(m[:3], int(m) if m.isdigit() else 1)
        return f"{pd.findtext('Year')}-{m:02d}-01"
    return date.today().isoformat()


def parse(xml_bytes: bytes) -> list[dict]:
    out = []
    for pa in ET.fromstring(xml_bytes).findall("PubmedArticle"):
        art = pa.find("MedlineCitation/Article")
        pmid = pa.findtext("MedlineCitation/PMID")
        if art is None or not pmid:
            continue
        ids = {i.get("IdType"): (i.text or "") for i in pa.findall("PubmedData/ArticleIdList/ArticleId")}
        authors = [f"{a.findtext('LastName', '')} {a.findtext('Initials', '')}".strip()
                   for a in art.findall("AuthorList/Author") if a.findtext("LastName")]
        out.append({
            "source": "pubmed",
            "source_id": pmid,
            "doi": ids.get("doi"),
            "title": _text(art.find("ArticleTitle")),
            "abstract": " ".join(_text(t) for t in art.findall("Abstract/AbstractText")),
            "authors": authors[:20],
            "venue": art.findtext("Journal/Title", ""),
            "published": _date(art),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "is_oa": "pmc" in ids,            # heuristic: a PMC record usually means free full text
            "is_preprint": False,
        })
    return [p for p in out if p["title"]]


def fetch(since: date, email: str, api_key: str = "", chunk=200, http=get, sleep=time.sleep):
    base = {"tool": "margin", "email": email, **({"api_key": api_key} if api_key else {})}
    res = json.loads(http(ESEARCH, {**base, "db": "pubmed", "term": "hasabstract", "datetype": "edat",
                                    "mindate": since.strftime("%Y/%m/%d"), "maxdate": "3000",
                                    "retmax": 10000, "retmode": "json"}))
    ids = res.get("esearchresult", {}).get("idlist", [])
    if len(ids) >= 10000:
        log.warning("PubMed returned its maximum of 10,000 records, so some papers in this window were not fetched. "
                    "Use a shorter window (--days 1) or run more often.")
    for i in range(0, len(ids), chunk):
        yield from parse(http(EFETCH, {**base, "db": "pubmed", "id": ",".join(ids[i:i + chunk]),
                                       "retmode": "xml"}))
        sleep(0.12 if api_key else 0.4)
