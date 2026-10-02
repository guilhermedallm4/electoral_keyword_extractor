"""Filtros determinísticos aplicados antes do LLM: janela do dia e relevância eleitoral."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from .collector import Article
from .text import fold, term_regexes


def day_window(day: date, tz: str) -> tuple[datetime, datetime]:
    """[00:00, 00:00 do dia seguinte) no fuso do projeto."""
    start = datetime.combine(day, time.min, tzinfo=ZoneInfo(tz))
    return start, start + timedelta(days=1)


def filter_by_date(articles: list[Article], day: date, cfg: dict) -> list[Article]:
    """Mantém só notícias publicadas no dia `day` (fuso cfg['timezone'])."""
    if not cfg.get("only_today", True):
        return articles
    start, end = day_window(day, cfg["timezone"])
    out = []
    for a in articles:
        if a.published is None:
            if cfg.get("accept_undated"):
                out.append(a)
            continue
        if start <= datetime.fromisoformat(a.published) < end:
            out.append(a)
    return out


@lru_cache(maxsize=8)
def _compiled(terms_key: tuple) -> dict:
    return {lvl: term_regexes(list(ts)) for lvl, ts in terms_key}


def in_election_period(day: date, cfg: dict) -> bool:
    p = cfg.get("election_period")
    return bool(p) and date.fromisoformat(p["start"]) <= day <= date.fromisoformat(p["end"])


def electoral_score(a: Article, day: date, cfg: dict) -> int:
    """Pontuação aditiva. strong=3, medium=2, weak=1 (soma de weak limitada a 2, para que
    'deputado' + 'votação' de notícia legislativa nunca passe sozinho).

    Contexto: feed de política (+1), tag/URL com 'eleic' (+2), período de campanha (+1, só se
    houver termo medium/strong). Assim 'Candidato anuncia proposta para segurança' passa
    durante a campanha mesmo sem a palavra 'eleição'.
    """
    terms = cfg["electoral_terms"]
    rx = _compiled(tuple((k, tuple(v)) for k, v in terms.items()))
    text = fold(a.text)
    hits = {lvl: sum(1 for _, r in rx[lvl] if r.search(text)) for lvl in ("strong", "medium", "weak")}
    score = 3 * min(hits["strong"], 2) + 2 * min(hits["medium"], 2) + min(hits["weak"], 2)
    meta = fold(" ".join(a.tags) + " " + a.url)
    if "eleic" in meta or "eleitor" in meta:
        score += 2
    if a.electoral_context:
        score += 1
    if (hits["strong"] or hits["medium"]) and in_election_period(day, cfg):
        score += 1
    return score


def filter_electoral(articles: list[Article], day: date, cfg: dict) -> list[Article]:
    out = []
    for a in articles:
        a.electoral_score = electoral_score(a, day, cfg)
        if a.electoral_score >= cfg["electoral_threshold"]:
            out.append(a)
    return out
