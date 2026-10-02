"""Interface para o próximo estágio (busca social): NEWS → DAILY KEYWORDS → SOCIAL SEARCH.

Por padrão entrega só as keywords do dia corrente. Dias anteriores apenas com allow_history=True.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import CONFIG
from . import storage

_RANK = {"high": 0, "medium": 1, "low": 2}


def load_keywords(day: date | None = None, min_priority: str = "low",
                  allow_history: bool = False, cfg: dict = CONFIG) -> list[dict]:
    today = datetime.now(ZoneInfo(cfg["timezone"])).date()
    day = day or today
    if day != today and not allow_history:
        raise ValueError(f"{day} não é hoje ({today}); use allow_history=True para auditoria")
    out = storage.load_output(cfg["data_dir"], day)
    if out is None:
        raise FileNotFoundError(f"sem keywords para {day}; rode `python -m keyword_extractor` antes")
    return [k for k in out["keywords"] if _RANK[k["priority"]] <= _RANK[min_priority]]


def iter_search_queries(day: date | None = None, min_priority: str = "medium", **kw):
    """Gera (query, keyword) prontos para Reddit/fóruns: '"X"', '"X" debate', ..."""
    for k in load_keywords(day, min_priority, **kw):
        for q in k["search_queries"]:
            yield q, k


def tool_definition(cfg: dict = CONFIG) -> dict:
    """Ferramenta para um agente consultar as keywords do dia (fase de busca).
    No bonsai_agent: `Tool(**tool_definition())`."""

    def fn(min_priority: str = "medium") -> str:
        ks = load_keywords(min_priority=min_priority, cfg=cfg)
        return json.dumps([{"keyword": k["keyword"], "priority": k["priority"],
                            "queries": k["search_queries"]} for k in ks], ensure_ascii=False)

    return dict(
        name="daily_election_keywords",
        description="Retorna as keywords eleitorais extraídas das notícias de HOJE, com consultas de busca.",
        parameters={"type": "object", "properties": {
            "min_priority": {"type": "string", "enum": ["high", "medium", "low"]}}},
        fn=fn)
