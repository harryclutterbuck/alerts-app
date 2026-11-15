"""OpenAlex: the broadest open index (all fields). Docs: https://docs.openalex.org

Check OpenAlex's current terms, rate limits and any API-key requirements before launch."""
import json
import logging
import urllib.error
from datetime import date

from .http import get

log = logging.getLogger("margin.openalex")
BASE = "https://api.openalex.org/works"
SELECT = ("id,doi,display_name,publication_date,authorships,primary_location,"
          "open_access,type,abstract_inverted_index")


def rebuild_abstract(inv: dict | None) -> str:
    """OpenAlex ships abstracts as {word: [positions]}; turn that back into text."""
    if not inv:
        return ""
    words = {}
    for w, positions in inv.items():
        for i in positions:
            words[i] = w
    return " ".join(words[i] for i in sorted(words))


def normalise(w: dict) -> dict | None:
    title = (w.get("display_name") or "").strip()
    if not title or not w.get("publication_date"):
        return None
    loc = w.get("primary_location") or {}
    venue = ((loc.get("source") or {}).get("display_name")) or ""
    vl = venue.lower()
    is_preprint = w.get("type") == "preprint"
    if vl in ("biorxiv", "medrxiv"):
        source = "biorxiv"
    elif vl.startswith("arxiv"):
        source = "arxiv"
    else:
        source = "openalex"
    authors = [(a.get("author") or {}).get("display_name", "") for a in (w.get("authorships") or [])]
    return {
        "source": source,
        "source_id": w["id"].rsplit("/", 1)[-1],
        "doi": w.get("doi"),
        "title": title,
        "abstract": rebuild_abstract(w.get("abstract_inverted_index")),
        "authors": [a for a in authors if a][:20],
        "venue": venue,
        "published": w["publication_date"],
        "url": w.get("doi") or loc.get("landing_page_url") or w["id"],
        "is_oa": bool((w.get("open_access") or {}).get("is_oa")),
        "is_preprint": is_preprint,
    }


def explain(code: int) -> str:
    """Turn an HTTP refusal into something a non-expert can act on."""
    if code == 403:
        return ("OpenAlex rejected the key (403). If you just created it, open the verification email OpenAlex sent "
                "and confirm your address, then try again.")
    if code == 429:
        return ("OpenAlex says the daily budget is used up, or requests came too fast (429). Whatever was already "
                "downloaded is saved. The budget resets at midnight UTC, and the next run will carry on.")
    if code == 401:
        return ("OpenAlex refused the request (401). Check that OPENALEX_API_KEY is set to your real key "
                "(from openalex.org/settings/api), with no spaces or quotes.")
    return f"OpenAlex returned an error (HTTP {code})."


def fetch(since: date, mailto: str, api_key: str = "", max_pages: int = 300, http=get):
    """Bulk-list every new article/preprint with an abstract. A list request costs 1 credit and returns up to
    200 works, so even a busy day is a few hundred credits; `max_pages` is a safety cap on spend per run."""
    cursor = "*"
    for _ in range(max_pages):
        params = {"filter": f"from_publication_date:{since.isoformat()},type:article|preprint,has_abstract:true",
                  "per-page": 200, "cursor": cursor, "select": SELECT, "mailto": mailto,
                  "sort": "publication_date:desc"}      # newest first, so hitting the page cap only drops the oldest
        if api_key:
            params["api_key"] = api_key
        try:
            data = json.loads(http(BASE, params))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 429):
                raise RuntimeError(explain(e.code)) from e
            raise
        for w in data.get("results", []):
            p = normalise(w)
            if p:
                yield p
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor:
            return
    log.warning("OpenAlex: stopped at the %d-page cap (%d papers) before reaching the start of the window, so the "
                "oldest papers in it were skipped. Raise OPENALEX_MAX_PAGES or shorten the window if this repeats.",
                max_pages, max_pages * 200)


def budget(api_key: str, http=get) -> dict:
    """Ask OpenAlex how much of today's allowance is left (this call is free)."""
    return json.loads(http("https://api.openalex.org/rate-limit", {"api_key": api_key} if api_key else None))
