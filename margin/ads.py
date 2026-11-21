"""Sponsored messages: the sponsor list, and the choice of which one (if any) goes in a digest.

Rules this module enforces, so they hold no matter what is in the file:
  * Nothing is shown unless MARGIN_ADS is on AND an ad's keywords match one of the subscriber's own alerts.
  * At most one ad per email, and only the ad text, a title, a link and the sponsor's name; no images, no tracking.
  * A broken or missing file means no ads. It never stops a digest from being sent.

Each entry in ads.json looks like this (see ads.example.json):
  id, sponsor, title, body, url (must be https), keywords (same AND/OR/quotes syntax as alerts),
  and optionally cta, initials, start and end (YYYY-MM-DD), active (false pauses it).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date

from .matcher import Doc, Term
from .text import tokens

log = logging.getLogger("margin.ads")
DEFAULT_FILE = os.path.join(os.path.dirname(__file__), "ads.json")
LIMITS = {"sponsor": 60, "title": 90, "body": 160, "cta": 24}


def _text(r, key, required=True):
    v = r.get(key)
    v = " ".join(v.split()) if isinstance(v, str) else ""
    if required and not v:
        raise ValueError(f"missing {key}")
    return v if len(v) <= LIMITS[key] else v[:LIMITS[key] - 1].rstrip() + "…"


def _clean(r):
    """One validated ad, or None (and a log line saying why) if the entry isn't usable."""
    try:
        if not isinstance(r, dict) or r.get("active", True) is False:
            return None
        url = str(r.get("url", "")).strip()
        if not url.lower().startswith("https://") or " " in url:
            raise ValueError("url must start with https://")
        terms = [t for t in (Term.parse(k) for k in r.get("keywords") or [] if isinstance(k, str)) if t]
        if not terms:
            raise ValueError("needs at least one keyword")
        sponsor = _text(r, "sponsor")
        initials = (str(r.get("initials") or "").strip() or "".join(w[0] for w in sponsor.split()[:2]))[:2].upper()
        return {"id": str(r.get("id") or sponsor), "sponsor": sponsor, "title": _text(r, "title"), "body": _text(r, "body"),
                "cta": _text(r, "cta", False) or "Learn more", "url": url, "initials": initials, "terms": terms,
                "start": date.fromisoformat(r["start"]) if r.get("start") else None,
                "end": date.fromisoformat(r["end"]) if r.get("end") else None}
    except (ValueError, TypeError, KeyError) as e:
        log.warning("ignoring an ad that isn't usable (%s): %s", e, str(r)[:80])
        return None


def load(path: str = "") -> list[dict]:
    """Read the sponsor list. Never raises."""
    try:
        with open(path or DEFAULT_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        log.warning("ads file could not be read: %s", e)
        return []
    return [ad for ad in (_clean(r) for r in raw if True) if ad] if isinstance(raw, list) else []


def pick(ads: list[dict], groups, seed: int, today: date):
    """The ad to show this person today, as (ad, name of the alert that matched), or None.
    Ads match on the topic words of the person's own alerts (name and search terms), never on anything else about them.
    When several qualify they take turns, so one sponsor can't hold the slot."""
    topics = []
    for alert, _, _ in groups:
        try:
            words = " ".join([alert["name"]] + [str(t) for t in json.loads(alert["include"])])
        except (ValueError, TypeError, KeyError):
            continue
        topics.append((alert["name"], Doc(tokens(words))))
    found = []
    for ad in sorted(ads, key=lambda a: a["id"]):
        if (ad["start"] and today < ad["start"]) or (ad["end"] and today > ad["end"]):
            continue
        for name, doc in topics:
            if any(t.matches(doc) for t in ad["terms"]):
                found.append((ad, name))
                break
    return found[(seed + today.toordinal()) % len(found)] if found else None
