"""Deduplicação de notícias: URL canônica, id, título+data e similaridade de título.

Duplicatas são fundidas (não descartadas às cegas): a lista `sources` acumula os portais, o que
alimenta o `source_count` das keywords.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit

from .collector import Article
from .text import tokens

_TRACKING = ("utm_", "fbclid", "gclid", "cmpid", "origin", "ref")


def canonical_url(url: str) -> str:
    if not url:
        return ""
    p = urlsplit(url.strip())
    host = p.netloc.lower().removeprefix("www.")
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(_TRACKING)])
    return f"{host}{p.path.rstrip('/')}" + (f"?{q}" if q else "")


def title_key(a: Article) -> str:
    return " ".join(tokens(a.title)) + "|" + (a.published or "")[:10]


def _jaccard(x: set, y: set) -> float:
    return len(x & y) / len(x | y) if x and y else 0.0


def _merge(keep: Article, dup: Article):
    for s in dup.sources:
        if s not in keep.sources:
            keep.sources.append(s)
    if len(dup.summary) > len(keep.summary):
        keep.summary = dup.summary
    keep.electoral_context = keep.electoral_context or dup.electoral_context
    keep.electoral_score = max(keep.electoral_score, dup.electoral_score)


def dedup(new: list[Article], existing: list[Article], threshold: float) -> tuple[list[Article], int]:
    """Retorna (artigos realmente novos, nº de duplicatas). Duplicatas de `existing` são fundidas nele."""
    pool = list(existing)
    by_url = {canonical_url(a.url): a for a in pool if a.url}
    by_id = {a.id: a for a in pool}
    by_title = {title_key(a): a for a in pool}
    title_toks = [(set(tokens(a.title, drop_stopwords=True)), a) for a in pool]
    fresh, removed = [], 0
    for a in new:
        u, tk = canonical_url(a.url), title_key(a)
        match = by_id.get(a.id) or (by_url.get(u) if u else None) or by_title.get(tk)
        if match is None:
            toks = set(tokens(a.title, drop_stopwords=True))
            match = next((b for t, b in title_toks if _jaccard(toks, t) >= threshold), None)
        if match is not None:
            _merge(match, a)
            removed += 1
            continue
        fresh.append(a)
        pool.append(a)
        by_id[a.id] = a
        if u:
            by_url[u] = a
        by_title[tk] = a
        title_toks.append((set(tokens(a.title, drop_stopwords=True)), a))
    return fresh, removed

