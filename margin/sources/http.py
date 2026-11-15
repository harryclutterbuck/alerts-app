import time
import urllib.error
import urllib.parse
import urllib.request


def get(url: str, params: dict | None = None, headers: dict | None = None, retries=4, timeout=60) -> bytes:
    """GET with polite retries. Honours Retry-After on 429 and backs off on 5xx."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "margin-alerts/0.1", **(headers or {})})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After", 0) or 0) or 2 ** (attempt + 1)
                time.sleep(min(wait, 60))
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")
