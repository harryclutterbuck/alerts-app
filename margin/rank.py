"""Learn from "More like this" / "Not relevant" clicks.

Each alert keeps its own profile: the papers its owner liked and the papers they rejected. When a digest
is built, every candidate paper is compared with those two sets and nudged up or down. Nothing is ever
hidden: a paper the person is likely to dislike just ranks lower, and falls into the "N more" line once the
digest is full. That keeps a wrong guess from silently swallowing something they wanted.

Similarity is Jaccard overlap of the informative words in title+abstract, with the alert's own search terms
removed (every match contains those, so they say nothing about which matches are better).
"""
from __future__ import annotations

from .text import tokens

WEIGHT = 15.0     # scales similarity (typically 0.02-0.3) to be comparable with term scores (1-3 per term)
TOP_K = 3         # compare with the K most similar liked / rejected papers, not the average of all

_STOP = frozenset("""
a about above after again all also an and any are as at be because been before being between both but by can could
did do does doing down during each few for from further had has have having he her here hers him his how i if in
into is it its itself just may me might more most my no nor not now of off on once only or other our out over own
same she should so some such than that the their them then there these they this those through to too under until
up use used using very was we were what when where which while who whom why will with within without would you your
paper papers study studies result results show shows shown present presents approach method methods based new novel
analysis propose proposed proposes find found finding findings however also thus therefore et al we-show
""".split())


def doc_tokens(title: str, abstract: str, drop=frozenset()) -> frozenset:
    words = tokens(title) + tokens((abstract or "")[:2000])
    return frozenset(w for w in words if len(w) > 2 and w not in _STOP and w not in drop and not w.isdigit())


def alert_terms(include_json: str) -> frozenset:
    import json
    out = set()
    for t in json.loads(include_json):
        out.update(tokens(t))
    return frozenset(out)


def latest_feedback(conn, alert_id: int):
    """One vote per paper: if someone clicks both links, the later click wins."""
    return conn.execute(
        """SELECT f.paper_id, f.value, p.title, p.abstract
           FROM feedback f JOIN papers p ON p.id = f.paper_id
           WHERE f.alert_id=? AND f.id = (SELECT MAX(id) FROM feedback WHERE alert_id=f.alert_id AND paper_id=f.paper_id)""",
        (alert_id,)).fetchall()


class Profile:
    def __init__(self, conn, alert):
        drop = alert_terms(alert["include"])
        self.drop = drop
        self.up, self.down = [], []
        for r in latest_feedback(conn, alert["id"]):
            toks = doc_tokens(r["title"], r["abstract"], drop)
            if toks:
                (self.up if r["value"] == "up" else self.down).append(toks)

    @property
    def empty(self) -> bool:
        return not self.up and not self.down

    @staticmethod
    def _sim(toks, group) -> float:
        if not group:
            return 0.0
        sims = sorted((len(toks & g) / len(toks | g) for g in group), reverse=True)[:TOP_K]
        return sum(sims) / len(sims)

    def adjustment(self, title: str, abstract: str) -> float:
        toks = doc_tokens(title, abstract, self.drop)
        if not toks:
            return 0.0
        return WEIGHT * (self._sim(toks, self.up) - self._sim(toks, self.down))


def rerank(conn, alert, rows):
    """Order matched papers best-first, folding feedback into the keyword score."""
    prof = Profile(conn, alert)
    if prof.empty:
        return list(rows)
    scored = [(r["score"] + prof.adjustment(r["title"], r["abstract"]), r["published"], r) for r in rows]
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [r for _, _, r in scored]


def feedback_stats(conn):
    """Per alert: papers emailed, and how the person rated them. The product's own relevance scorecard."""
    out = []
    for a in conn.execute("SELECT id, name FROM alerts ORDER BY id"):
        sent = conn.execute("SELECT COUNT(*) c FROM matches WHERE alert_id=? AND sent_at IS NOT NULL", (a["id"],)).fetchone()["c"]
        votes = latest_feedback(conn, a["id"])
        up = sum(1 for v in votes if v["value"] == "up")
        down = sum(1 for v in votes if v["value"] == "down")
        out.append({"alert_id": a["id"], "name": a["name"], "sent": sent, "up": up, "down": down,
                    "relevant_rate": round(up / (up + down), 2) if up + down else None,
                    "response_rate": round((up + down) / sent, 2) if sent else None})
    return out
