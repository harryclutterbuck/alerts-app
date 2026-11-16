"""Reverse search: run many saved alerts against each new paper.

This is the heart of the product. Instead of one query over millions of papers, we have
thousands of queries and a stream of new papers. We build an inverted index from a few
"anchor" words of every alert term to the alerts that use it. For each paper we only test the
alerts whose anchor words appear in it, so cost scales with matches, not with the number of alerts.

Term semantics (each topic or "leave out" entry is one term)
  cancer therapy                    -> both words must appear (any order)
  cancer AND therapy                -> same thing, written out
  cancer OR tumour                  -> either word
  "base editing"                    -> exact phrase (quotes straight or curly)
  (cancer OR tumour) AND immuno*    -> brackets group; AND is applied before OR
  AND / OR may be written in any case. Inside quotes they are ordinary words.
  An alert matches if ANY include term matches and NO exclude term matches.
Parsing never fails: stray brackets and dangling operators are ignored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .text import tokens

_LEX = re.compile(r'[\u201c\u201d"][^\u201c\u201d"]*[\u201c\u201d"]?|[()]|[^\s()\u201c\u201d"]+')
_QUOTES = '"\u201c\u201d'


@dataclass(frozen=True)
class Word:
    tok: str

    def matches(self, doc: "Doc") -> bool:
        return self.tok in doc.tokset

    def anchors(self) -> frozenset:
        return frozenset((self.tok,))

    def fts(self) -> str:
        return f'"{self.tok}"'


@dataclass(frozen=True)
class Phrase:
    toks: tuple[str, ...]

    def matches(self, doc: "Doc") -> bool:
        return " " + " ".join(self.toks) + " " in doc.joined

    def anchors(self) -> frozenset:
        return frozenset((self.toks[0],))

    def fts(self) -> str:
        return '"' + " ".join(self.toks) + '"'


@dataclass(frozen=True)
class All:
    kids: tuple

    def matches(self, doc: "Doc") -> bool:
        return all(k.matches(doc) for k in self.kids)

    def anchors(self) -> frozenset:
        # Every kid must be present, so any one kid's anchors will do. Take the smallest set.
        return min((k.anchors() for k in self.kids), key=len)

    def fts(self) -> str:
        return "(" + " AND ".join(k.fts() for k in self.kids) + ")"


@dataclass(frozen=True)
class Any:
    kids: tuple

    def matches(self, doc: "Doc") -> bool:
        return any(k.matches(doc) for k in self.kids)

    def anchors(self) -> frozenset:
        return frozenset().union(*(k.anchors() for k in self.kids))

    def fts(self) -> str:
        return "(" + " OR ".join(k.fts() for k in self.kids) + ")"


def _join(kind, kids):
    kids = tuple(kids)
    return kids[0] if len(kids) == 1 else kind(kids)


class Term:
    """Parses what a person typed into a small expression tree. Returns None if there is nothing to search for."""

    @classmethod
    def parse(cls, raw: str):
        items = []                                     # ("word", node) | ("op", "and"/"or") | ("(", None) | (")", None)
        for m in _LEX.finditer((raw or "").strip()):
            piece = m.group()
            if piece[0] in _QUOTES:
                toks = tokens(piece)
                if toks:
                    items.append(("atom", Phrase(tuple(toks)) if len(toks) > 1 else Word(toks[0])))
            elif piece in "()":
                items.append((piece, None))
            elif piece.lower() in ("and", "or"):
                items.append(("op", piece.lower()))
            else:
                toks = tokens(piece)
                if toks:
                    items.append(("atom", _join(All, (Word(t) for t in toks))))
        pos = 0

        def parse_or(depth):
            nonlocal pos
            alts = []
            while pos < len(items):
                kind = items[pos][0]
                if kind == ")":
                    if depth:
                        break
                    pos += 1                           # stray closing bracket: ignore
                    continue
                part = parse_and(depth)
                if part is not None:
                    alts.append(part)
            return _join(Any, alts) if alts else None

        def parse_and(depth):
            nonlocal pos
            parts, saw_or = [], False
            while pos < len(items):
                kind, val = items[pos]
                if kind == ")":
                    if depth:
                        break
                    pos += 1
                elif kind == "op":
                    pos += 1
                    if val == "or":
                        saw_or = True
                        break
                elif kind == "(":
                    pos += 1
                    inner = parse_or(depth + 1)
                    if pos < len(items) and items[pos][0] == ")":
                        pos += 1                       # a missing closing bracket is tolerated
                    if inner is not None:
                        parts.append(inner)
                else:
                    pos += 1
                    parts.append(val)
            return _join(All, parts) if parts else None

        # parse_or loops over parse_and, which stops at each OR, so alternatives accumulate in order.
        return parse_or(0)


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
    include: list
    exclude: list = field(default_factory=list)
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
        self.index: dict[str, list[tuple[AlertSpec, object]]] = {}
        for a in alerts:
            for t in a.include:
                for anchor in t.anchors():
                    self.index.setdefault(anchor, []).append((a, t))

    def match(self, paper) -> list[tuple[AlertSpec, int]]:
        """Return [(alert, score)]. Title hits count 3, abstract-only hits count 1."""
        doc = Doc.of_paper(paper)
        hit: dict[int, tuple[AlertSpec, list]] = {}
        seen = set()                                   # a term can be indexed under several anchors; count it once
        for tok in doc.tokset & self.index.keys():
            for alert, term in self.index[tok]:
                if (alert.id, id(term)) not in seen and term.matches(doc):
                    seen.add((alert.id, id(term)))
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
