"""Coleta de notícias de fontes configuráveis (RSS/Atom/RDF), tolerante a falhas por fonte.

Sem scraping: só endpoints públicos de feed. Uma requisição por fonte por execução, sem
tentar contornar bloqueios (403/WAF viram status 'error' e a coleta segue).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

import feedparser
import requests

from .text import strip_html

log = logging.getLogger("keyword_extractor")


@dataclass
class Article:
    id: str
    source: str                    # portal
    feed: str
    title: str
    summary: str
    url: str
    published: str | None          # ISO 8601 com offset
    tags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)   # portais onde apareceu (após dedup)
    electoral_context: bool = False                    # feed de política/eleições
    electoral_score: int = 0
    candidates: list | None = None                     # saída do LLM; None = ainda não analisada

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Article":
        return cls(**d)

    @property
    def text(self) -> str:
        return f"{self.title}. {self.summary}"


# --- datas -----------------------------------------------------------------------------------
_PT_MONTHS = {"jan": "Jan", "fev": "Feb", "mar": "Mar", "abr": "Apr", "mai": "May", "jun": "Jun",
              "jul": "Jul", "ago": "Aug", "set": "Sep", "out": "Oct", "nov": "Nov", "dez": "Dec"}


def parse_date(entry) -> datetime | None:
    """Data de publicação com fuso. Cobre RFC 822 em português (UOL: 'Qui, 01 Out 2026 ...')."""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    raw = entry.get("published") or entry.get("updated") or entry.get("dc_date")
    if not raw:
        return None
    s = re.sub(r"^\s*\w+,\s*", "", raw)  # dia da semana (Qui, Thu...)
    s = re.sub(r"\b([A-Za-z]{3})\b", lambda m: _PT_MONTHS.get(m.group(1).lower(), m.group(1)), s)
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- fetchers ---------------------------------------------------------------------------------
class SourceError(Exception):
    pass


def _cookies(src: dict) -> dict:
    """Cookies opcionais lidos de variável de ambiente ('k=v; k2=v2'), nunca do código."""
    raw = os.environ.get(src.get("cookies_env", ""), "")
    return dict(p.strip().split("=", 1) for p in raw.split(";") if "=" in p)


def http_get(url: str, cfg: dict, src: dict | None = None) -> bytes:
    src = src or {}
    headers = {"User-Agent": cfg["user_agent"], **src.get("headers", {})}
    r = requests.get(url, timeout=cfg["request_timeout"], headers=headers, cookies=_cookies(src))
    if r.status_code != 200:
        raise SourceError(f"HTTP {r.status_code}")
    return r.content


def fetch_rss(src: dict, cfg: dict, get: Callable[..., bytes] = http_get) -> list[Article]:
    body = get(src["url"], cfg, src)
    d = feedparser.parse(body)
    if not d.entries and (d.bozo or not d.get("version")):
        # WAF/bloqueio costuma devolver HTML com 200: não é "0 notícias", é falha da fonte
        raise SourceError("resposta não é um feed RSS/Atom")
    out = []
    for e in d.entries[: cfg["max_articles_per_source"]]:
        dt = parse_date(e)
        url = e.get("link") or ""
        title = strip_html(e.get("title", ""))
        if not title:
            continue
        summary = strip_html(e.get("summary") or e.get("description") or "")
        if summary.startswith(title):  # alguns feeds repetem o título no resumo
            summary = summary[len(title):].lstrip(" .:-")
        guid = e.get("id") or url or title
        out.append(Article(
            id=hashlib.sha1(f"{src['name']}|{guid}".encode()).hexdigest()[:16],
            source=src["name"], feed=src.get("feed", ""),
            title=title, summary=summary[: cfg["max_summary_chars"]], url=url,
            published=dt.isoformat() if dt else None,
            tags=[t.get("term", "") for t in e.get("tags", []) if t.get("term")][:8],
            sources=[src["name"]], electoral_context=src.get("electoral_context", False),
        ))
    return out


FETCHERS: dict[str, Callable] = {"rss": fetch_rss, "atom": fetch_rss}


def source_label(src: dict) -> str:
    return f"{src['name']} ({src['feed']})" if src.get("feed") else src["name"]


def collect(cfg: dict, get: Callable[..., bytes] = http_get) -> tuple[list[Article], dict]:
    """Coleta todas as fontes. Falha de uma fonte não interrompe as outras."""
    articles, status = [], {}
    for src in cfg["sources"]:
        label = source_label(src)
        try:
            fetcher = FETCHERS[src.get("type", "rss")]
            got = fetcher(src, cfg, get)
            articles += got
            status[label] = "ok"
            log.info("Source %-40s -> OK (%d)", label, len(got))
        except Exception as e:  # noqa: BLE001 — qualquer falha da fonte é registrada e ignorada
            status[label] = f"error: {e}"[:120]
            log.warning("Source %-40s -> ERROR %s", label, e)
    return articles, status
