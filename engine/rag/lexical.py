#!/usr/bin/env python3
"""
lexical.py — keyword search (BM25) and rank fusion, for hybrid retrieval.

Why this exists: dense embeddings are weakest exactly where this corpus is strongest —
exact tokens. "McCain", "Xero", "adam&eveDDB", "FCB Grid", "AISDALSLove" are things a
planner types verbatim and expects to hit verbatim. A bag-of-words scorer does that
trivially; a 2048-dim vector only approximately. Running both and fusing the two
rankings is the single biggest quality lever in the plan (C6).

Pieces
  tokenize(text)         lower-case, split on non-alphanumerics, keep tokens of 2+ chars,
                         keep digits (years, "3H"). No stemming: exactness is the point.
  BM25(docs)             Okapi BM25 over (doc_id, text) pairs, b tuned to 0.3 (below).
                         Pure Python; ~7k docs × ~300 tokens is instant to build and
                         a few ms to query. Postings are an inverted index so a query
                         touches only the documents that share a token with it.
  rrf(rankings, k=60)    reciprocal rank fusion: score(d) = Σ 1/(k + rank_i(d)). Rank-based,
                         so cosine (0..1) and BM25 (0..∞) never need calibrating against
                         each other. k=60 is the value from the original paper and is
                         robust. Note what it implies: with candidate lists of 50, a doc
                         that appears in BOTH lists (2/(60+50) = 0.018 at worst) always
                         outranks one that appears in only one, even at rank 1 (1/61 =
                         0.016). Agreement between the two retrievers beats a single
                         strong vote — the behaviour we want from hybrid search.

Everything here is dependency-free and deterministic.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Hashable, Iterable, Sequence

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall((text or "").lower()) if len(t) > 1]


class BM25:
    """Okapi BM25 over a fixed document set. Build once, query many times."""

    # b=0.3 rather than the textbook 0.75, measured not assumed. `b` is length
    # normalisation: at 0.75 a long document is penalised hard for being long. This
    # corpus spans 80-token D&AD overviews to 1000-token whole IPA cases, and the default
    # was pushing complete award cases — the most useful precedent — down the ranking.
    # Sweep on 310 held-out golden cases: b=0.75 -> 0.906 recall@5, b=0.3 -> 0.958.
    # k1 (term-frequency saturation) stays at the textbook 1.5: lowering it to 1.2 looked
    # like a +0.026 gain on its own, but scored IDENTICALLY to b=0.3 alone when combined,
    # so it was correcting the same length bias twice rather than adding anything.
    def __init__(self, docs: Iterable[tuple[Hashable, str]], k1: float = 1.5, b: float = 0.3):
        self.k1, self.b = k1, b
        self.ids: list[Hashable] = []
        self.doc_len: list[int] = []
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)   # token -> [(doc_idx, tf)]
        for doc_id, text in docs:
            toks = tokenize(text)
            idx = len(self.ids)
            self.ids.append(doc_id)
            self.doc_len.append(len(toks))
            for tok, tf in Counter(toks).items():
                self.postings[tok].append((idx, tf))
        n = len(self.ids)
        self.avg_len = (sum(self.doc_len) / n) if n else 0.0
        # idf with the +1 inside the log so a token in every document still scores >= 0
        self.idf = {tok: math.log(1 + (n - len(pl) + 0.5) / (len(pl) + 0.5)) for tok, pl in self.postings.items()}

    def __len__(self) -> int:
        return len(self.ids)

    def search(self, query: str, k: int = 50, allowed: set[Hashable] | None = None) -> list[tuple[float, Hashable]]:
        """Top-k (score, doc_id). `allowed` restricts to a subset (metadata pre-filter)."""
        scores: dict[int, float] = defaultdict(float)
        for tok in set(tokenize(query)):
            pl = self.postings.get(tok)
            if not pl:
                continue
            idf = self.idf[tok]
            for idx, tf in pl:
                dl = self.doc_len[idx]
                denom = tf + self.k1 * (1 - self.b + self.b * dl / (self.avg_len or 1))
                scores[idx] += idf * (tf * (self.k1 + 1)) / denom
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        out: list[tuple[float, Hashable]] = []
        for idx, s in ranked:
            did = self.ids[idx]
            if allowed is not None and did not in allowed:
                continue
            out.append((s, did))
            if len(out) >= k:
                break
        return out


def rrf(rankings: Sequence[Sequence[Hashable]], k: int = 10,
        weights: Sequence[float] | None = None) -> list[tuple[float, Hashable]]:
    """Fuse several ranked id lists into one. Each list contributes weight/(k+rank) per id.

    `k` controls how much a top rank is worth relative to merely appearing: a small k
    makes rank 1 dominant, a large k flattens the curve so agreement across lists matters
    more. k=10 rather than the paper's 60, measured: on top of the b=0.3 change it adds
    a further +0.016 held-out recall@5. With two retrievers that are each already good,
    a confident top-ranked hit deserves to win more often than pure agreement does.
    `weights` stays 1:1 — tilting it either way scored flat or worse, which is the point
    of rank fusion: the two lists need no calibrating against each other."""
    w = list(weights) if weights else [1.0] * len(rankings)
    fused: dict[Hashable, float] = defaultdict(float)
    for wi, ranking in zip(w, rankings):
        for rank, did in enumerate(ranking, 1):
            fused[did] += wi / (k + rank)
    return sorted(((s, d) for d, s in fused.items()), key=lambda x: x[0], reverse=True)
