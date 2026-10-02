"""Prioridade high/medium/low a partir SÓ da ocorrência nas notícias do dia.

1. O código calcula a estatística de ocorrência de cada keyword (nº de notícias e de fontes do dia
   que a mencionam) e a distribuição desses valores.
2. O LLM recebe apenas essa distribuição (sem nenhuma lista ou limite pré-definido) e decide os
   pontos de corte entre high, medium e low, justificando.
3. O código aplica os cortes: a classificação é monotônica por construção (nenhuma keyword mais
   citada fica abaixo de uma menos citada). Cortes inválidos → quebras naturais (Jenks) calculadas
   sobre a mesma distribuição do dia.
"""
from __future__ import annotations

import logging
import statistics
from collections import Counter
from typing import Callable

log = logging.getLogger("keyword_extractor")

SYSTEM = ("Você é um analista de dados. Responda apenas com JSON. Baseie-se exclusivamente nos números "
          "fornecidos; não use conhecimento externo sobre os termos.")

PROMPT = """Abaixo está a ocorrência de {n_kw} termos extraídos de {n_art} notícias eleitorais publicadas hoje.
"notícias" = em quantas notícias do dia o termo aparece; "fontes" = em quantos portais diferentes.

Estatística descritiva de "notícias": mínimo {mn}, 1º quartil {q1}, mediana {med}, média {mean}, 3º quartil {q3}, máximo {mx}.
Distribuição (valor de "notícias": quantidade de termos com esse valor):
{hist}

Termos (notícias, fontes), do mais citado ao menos citado:
{rows}

Tarefa: com base apenas nessa distribuição, defina dois pontos de corte em número de notícias:
- high_min: termos com notícias >= high_min são "high" (os mais comentados do dia, claramente destacados dos demais)
- medium_min: termos com medium_min <= notícias < high_min são "medium"; abaixo disso, "low"
Escolha cortes onde a distribuição muda de patamar (saltos entre valores consecutivos), não para preencher quantidades.
Explique em "justificativa" (até 3 frases) por que esses cortes, citando os números."""


def _schema(mx: int) -> dict:
    return {"type": "object", "properties": {
        "high_min": {"type": "integer", "minimum": 1, "maximum": mx},
        "medium_min": {"type": "integer", "minimum": 1, "maximum": mx},
        "justificativa": {"type": "string", "maxLength": 600}},
        "required": ["high_min", "medium_min", "justificativa"]}


def describe(values: list[int]) -> dict:
    q = statistics.quantiles(values, n=4) if len(values) >= 2 else [values[0]] * 3
    return {"n": len(values), "min": min(values), "q1": round(q[0], 1), "median": statistics.median(values),
            "mean": round(statistics.mean(values), 1), "q3": round(q[2], 1), "max": max(values),
            "stdev": round(statistics.pstdev(values), 1)}


def jenks_breaks(values: list[int], k: int = 3) -> list[float]:
    """Quebras naturais de Jenks (minimiza a variância dentro das classes). Retorna os k-1 limites
    inferiores das classes superiores, em ordem crescente."""
    data = sorted(values)
    uniq = sorted(set(data))
    if len(uniq) <= k:
        return uniq[1:] if len(uniq) > 1 else [uniq[0] + 1]
    n = len(data)
    inf = float("inf")
    # variância acumulada via somas prefixadas
    s1 = [0.0] * (n + 1)
    s2 = [0.0] * (n + 1)
    for i, v in enumerate(data):
        s1[i + 1] = s1[i] + v
        s2[i + 1] = s2[i] + v * v

    def ssd(i, j):  # soma dos desvios² de data[i:j]
        m = j - i
        return s2[j] - s2[i] - (s1[j] - s1[i]) ** 2 / m

    cost = [[inf] * (n + 1) for _ in range(k + 1)]
    back = [[0] * (n + 1) for _ in range(k + 1)]
    cost[0][0] = 0.0
    for c in range(1, k + 1):
        for j in range(c, n + 1):
            for i in range(c - 1, j):
                v = cost[c - 1][i] + ssd(i, j)
                if v < cost[c][j]:
                    cost[c][j], back[c][j] = v, i
    cuts, j = [], n
    for c in range(k, 1, -1):
        j = back[c][j]
        cuts.append(data[j])
    return sorted(cuts)


def _apply(keywords: list[dict], high_min: float, medium_min: float, n_art: int) -> None:
    for k in keywords:
        ac = k["article_count"]
        k["priority"] = "high" if ac >= high_min else "medium" if ac >= medium_min else "low"
        k["reason"] = (f"{ac} notícia{'s' if ac != 1 else ''} ({100 * ac / max(n_art, 1):.1f}% do dia) "
                       f"em {k['source_count']} fonte{'s' if k['source_count'] != 1 else ''}.")


def classify(keywords: list[dict], n_articles: int, llm: Callable[..., dict] | None) -> dict:
    """Define high/medium/low pela ocorrência. Retorna o registro da análise (vai para o JSON do dia)."""
    if not keywords:
        return {"method": "none"}
    values = [k["article_count"] for k in keywords]
    stats = describe(values)
    jb = jenks_breaks(values)
    fallback = {"high_min": jb[-1], "medium_min": jb[0] if len(jb) > 1 else min(values)}
    analysis = {"statistics": stats, "jenks": fallback}

    chosen, method, why = None, "jenks", "Quebras naturais (Jenks) da distribuição de ocorrência do dia."
    if llm is not None:
        hist = Counter(values)
        prompt = PROMPT.format(
            n_kw=len(keywords), n_art=n_articles, mn=stats["min"], q1=stats["q1"], med=stats["median"],
            mean=stats["mean"], q3=stats["q3"], mx=stats["max"],
            hist="\n".join(f"{v}: {hist[v]}" for v in sorted(hist, reverse=True)),
            rows="\n".join(f"{k['keyword']} ({k['article_count']}, {k['source_count']})"
                           for k in sorted(keywords, key=lambda k: (-k["article_count"], -k["source_count"]))))
        try:
            out = llm(prompt, _schema(stats["max"]), system=SYSTEM, temperature=0.0, max_tokens=800)  # determinístico: mesmos números → mesmos cortes
            h, m = int(out["high_min"]), int(out["medium_min"])
            n_high = sum(v >= h for v in values)
            if not (stats["min"] < h <= stats["max"] and stats["min"] <= m < h):
                raise ValueError(f"cortes fora da distribuição: high_min={h}, medium_min={m}")
            if n_high == len(values):
                raise ValueError("todos os termos ficariam high")
            chosen, method, why = {"high_min": h, "medium_min": m}, "llm", out["justificativa"].strip()
        except Exception as e:  # noqa: BLE001 — cortes inválidos/LLM fora → Jenks
            analysis["llm_error"] = f"{type(e).__name__}: {e}"[:200]
            log.warning("LLM priority analysis rejected, using Jenks: %s", analysis["llm_error"])
    chosen = chosen or fallback
    _apply(keywords, chosen["high_min"], chosen["medium_min"], n_articles)
    counts = Counter(k["priority"] for k in keywords)
    analysis.update({"method": method, "thresholds": chosen, "justification": why,
                     "counts": {p: counts.get(p, 0) for p in ("high", "medium", "low")}})
    log.info("Priority (%s): high>=%s medium>=%s -> %s", method, chosen["high_min"],
             chosen["medium_min"], analysis["counts"])
    return analysis
