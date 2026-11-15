"""Reverse search: run many saved alerts against each new paper.

This is the heart of the product. Instead of one query over millions of papers, we have
thousands of queries and a stream of new papers. We build an inverted index from the first
word of every alert term to the alerts that use it. For each paper we only test the alerts
whose index words appear in it, so cost scales with matches, not with the number of alerts.

Term semantics
  cancer therapy      -> both words must appear (any order)
  "base editing"      -> exact phrase (quotes straight or curly)
  An alert matches if ANY include term matches and NO exclude term matches.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .text import tokens

_QUOTES = '"\u201c\u201d\u2018\u2019\''


@dataclass(frozen=True)
class Term:
    toks: tuple[str, ...]
    phrase: bool = False

    @classmethod
    def parse(cls, raw: str) -> "Term | None":
        raw = (raw or "").strip()
        phrase = len(raw) > 1 and raw[0] in _QUOTES and raw[-1] in _QUOTES
        toks = tuple(tokens(raw))
        return cls(toks, phrase and len(toks) > 1) if toks else None

    def matches(self, doc: "Doc") -> bool:
        if self.phrase:
            return " " + " ".join(self.toks) + " " in doc.joined
        return all(t in doc.tokset for t in self.toks)


class Doc:
    __slots__ = ("tokset", "joined")

    def __init__(self, toks: list[str]):
        self.tokset = set(toks)
        self.joined = " " + " ".join(toks) + " "

    @classmethod
    def of_paper(cls, p) -> "Doc":
        # "|" can never be produced by the tokenizer, so phrases can't straddle title/abstract.
        return cls(tokens(p["title"]) + ["|"] + tokens(p["abstract"] or ""))


@dataclass
class AlertSpec:
    id: int
    include: list[Term]
    exclude: list[Term] = field(default_factory=list)
    sources: frozenset = frozenset()   # empty = any source
    oa_only: bool = False
    preprints: bool = True
    activated_at: str = ""

    @classmethod
    def build(cls, id, include, exclude=(), sources=(), oa_only=False, preprints=True, activated_at=""):
        inc = [t for t in (Term.parse(x) for x in include) if t]
        exc = [t for t in (Term.parse(x) for x in exclude) if t]
        return cls(id, inc, exc, frozenset(sources), bool(oa_only), bool(preprints), activated_at)

    def filters_ok(self, p) -> bool:
        if self.sources and p["source"] not in self.sources:
            return False
        if self.oa_only and not p["is_oa"]:
            return False
        if not self.preprints and p["is_preprint"]:
            return False
        return True


class Matcher:
    def __init__(self, alerts: list[AlertSpec]):
        self.index: dict[str, list[tuple[AlertSpec, Term]]] = {}
        for a in alerts:
            for t in a.include:
                self.index.setdefault(t.toks[0], []).append((a, t))

    def match(self, paper) -> list[tuple[AlertSpec, int]]:
        """Return [(alert, score)]. Title hits count 3, abstract-only hits count 1."""
        doc = Doc.of_paper(paper)
        hit: dict[int, tuple[AlertSpec, list[Term]]] = {}
        for tok in doc.tokset & self.index.keys():
            for alert, term in self.index[tok]:
                if term.matches(doc):
                    hit.setdefault(alert.id, (alert, []))[1].append(term)
        if not hit:
            return []
        title = Doc(tokens(paper["title"]))
        out = []
        for alert, terms in hit.values():
            if not alert.filters_ok(paper):
                continue
            if any(t.matches(doc) for t in alert.exclude):
                continue
            out.append((alert, sum(3 if t.matches(title) else 1 for t in terms)))
        return out
