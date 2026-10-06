"""Pipeline diário: RSS → data → filtro eleitoral → dedup → título+resumo → seleção → LLM → keywords.

Incremental: cada execução só adiciona notícias novas ao estado do dia e só manda ao LLM as que
ainda não foram analisadas. As keywords são sempre recalculadas (de forma determinística) a partir
de todas as notícias do dia guardadas no estado.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Callable
from zoneinfo import ZoneInfo

from .collector import collect, http_get
from .config import CONFIG
from .dedup import canonical_url, dedup
from .extract import build_keywords, extract_candidates
from .filters import day_window, filter_by_date, filter_electoral
from .llm import LLMClient
from .priority import classify
from . import storage

log = logging.getLogger("keyword_extractor")


def default_llm(cfg: dict) -> Callable[..., dict]:
    client = LLMClient(cfg["llm_base_url"])
    client.wait_ready(timeout=cfg.get("llm_wait_ready", 60))
    return client.complete_json


def run(now: datetime | None = None, cfg: dict = CONFIG, get=http_get,
        llm: Callable[..., dict] | None = None, recompute: bool = False) -> dict:
    tz = ZoneInfo(cfg["timezone"])
    now = (now or datetime.now(tz)).astimezone(tz)
    day: date = now.date()
    log.info("Collection date: %s (%s)", day, cfg["timezone"])

    st = storage.load_state(cfg["data_dir"], day, cfg["timezone"])
    if st["last_collection"]:
        log.info("Incremental run; last collection: %s", st["last_collection"])
    if recompute:  # reaplica o filtro eleitoral atual e reanalisa tudo com o LLM
        before = len(st["articles"])
        st["articles"] = filter_electoral(st["articles"], day, cfg)
        for a in st["articles"]:
            a.candidates = None
        log.info("Recompute: %d stored articles re-filtered -> %d", before, len(st["articles"]))

    # 1. coleta (falha por fonte é isolada)
    fetched, status = collect(cfg, get)
    log.info("Sources: %d (%d ok)", len(status), sum(v == "ok" for v in status.values()))
    log.info("Articles fetched: %d", len(fetched))

    # 2. filtro temporal
    today = filter_by_date(fetched, day, cfg)
    seen = set(st["seen_today"])
    for a in today:
        seen.add(canonical_url(a.url) or a.id)
    st["seen_today"] = sorted(seen)
    log.info("Articles after date filter: %d", len(today))

    # 3. filtro eleitoral
    electoral = filter_electoral(today, day, cfg)
    log.info("Electoral articles (this run): %d", len(electoral))

    # 4. dedup (dentro do lote e contra o que já está no estado do dia)
    fresh, dups = dedup(electoral, st["articles"], cfg["title_similarity"])
    log.info("Duplicates removed: %d", dups)
    room = max(0, cfg["max_total_articles"] - len(st["articles"]))
    if len(fresh) > room:
        fresh.sort(key=lambda a: -a.electoral_score)
        log.info("Daily article cap reached: keeping %d of %d new", room, len(fresh))
        fresh = fresh[:room]
    st["articles"] += fresh
    log.info("New electoral articles stored: %d (day total %d)", len(fresh), len(st["articles"]))

    # 5. LLM só nas notícias ainda não analisadas, mais relevantes primeiro
    pending = sorted((a for a in st["articles"] if a.candidates is None),
                     key=lambda a: (-a.electoral_score, -len(a.sources)))
    pending = pending[: cfg["max_articles_for_llm_per_run"]]
    llm_error = None
    if pending:
        try:
            llm = llm or default_llm(cfg)
        except Exception as e:  # noqa: BLE001 — servidor fora do ar: notícias ficam pendentes
            llm_error = f"{type(e).__name__}: {e}"[:200]
            log.error("LLM unavailable, %d articles kept pending: %s", len(pending), llm_error)
            pending = []
        bs = cfg["llm_batch_size"]
        for i in range(0, len(pending), bs):
            batch = pending[i:i + bs]
            try:
                extract_candidates(batch, llm, cfg)
            except Exception as e:  # noqa: BLE001 — LLM fora do ar: tenta de novo na próxima execução
                llm_error = f"{type(e).__name__}: {e}"[:200]
                log.error("LLM batch failed (%d articles kept pending): %s", len(batch), llm_error)
                for a in batch:
                    a.candidates = None
        log.info("Articles sent to LLM: %d", len(pending))

    # 6. agregação determinística sobre todas as notícias do dia
    keywords, kstats = build_keywords(st["articles"], cfg)

    # 7. prioridade: o LLM define os cortes a partir da estatística de ocorrência do dia
    if keywords and llm is None:
        try:
            llm = default_llm(cfg)
        except Exception as e:  # noqa: BLE001
            log.warning("LLM unavailable for priority analysis, using Jenks: %s", e)
    analysis = classify(keywords, len(st["articles"]), llm)
    rank = {"high": 0, "medium": 1, "low": 2}
    keywords.sort(key=lambda k: (rank[k["priority"]], -k["article_count"], -k["source_count"], k["keyword"]))
    log.info("Keywords generated: %d (rejected: %d no evidence, %d generic; %d over cap)",
             len(keywords), kstats.get("rejected_no_evidence", 0),
             kstats.get("rejected_generic", 0), kstats.get("truncated", 0))

    st["last_collection"] = now.isoformat(timespec="seconds")
    st["runs"].append({"at": st["last_collection"], "fetched": len(fetched), "today": len(today),
                       "electoral": len(electoral), "duplicates": dups, "new": len(fresh),
                       "sent_to_llm": len(pending), "llm_error": llm_error,
                       "rejected_terms": kstats["rejected_terms"],
                       "sources_failed": [k for k, v in status.items() if v != "ok"]})
    storage.save_state(cfg["data_dir"], day, st)

    out = {
        "collection_date": day.isoformat(),
        "timezone": cfg["timezone"],
        "last_collection": st["last_collection"],
        "runs_today": len(st["runs"]),
        "sources_processed": sum(v == "ok" for v in status.values()),
        "sources": status,
        "articles_collected": len(st["seen_today"]),
        "electoral_articles": len(st["articles"]),
        "articles_analyzed": sum(a.candidates is not None for a in st["articles"]),
        "llm_error": llm_error,
        "rejected": {"no_evidence": kstats.get("rejected_no_evidence", 0),
                     "generic": kstats.get("rejected_generic", 0)},
        # janela para a busca social: só posts publicados no dia (API do Bluesky: since/until)
        "search_window": {"since": day_window(day, cfg["timezone"])[0].isoformat(),
                          "until": day_window(day, cfg["timezone"])[1].isoformat()},
        "priority_analysis": analysis,
        "keywords": keywords,
    }
    path = storage.save_output(cfg["data_dir"], day, out)
    log.info("Saved %s", path)
    return out
