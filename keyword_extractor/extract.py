"""Extração de keywords: o LLM propõe termos atômicos; o código valida evidência, normaliza,
conta, prioriza, combina e gera consultas — tudo determinístico e auditável.

Por que o LLM não decide prioridade nem combinações: o Bonsai-27B 1-bit alucina com confiança
(ver results/perguntas_*.jsonl). Aqui qualquer termo sem ocorrência literal (módulo caixa,
acentos, partículas e plural) numa notícia do dia é descartado.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Callable

from .collector import Article
from .priority import classify
from .text import STOPWORDS, contains_phrase, entity_key, fold, key_tokens, stem, strip_accents, tokens

log = logging.getLogger("keyword_extractor")

ATOMIC_CATEGORIES = ["person", "organization", "event", "topic", "location", "specific_term"]
ENTITY_CATEGORIES = {"person", "organization", "location"}
CONTEXT_CATEGORIES = {"event", "topic"}

SYSTEM = ("Você extrai termos de busca de notícias. Responda apenas com JSON. "
          "Nunca invente nomes, eventos ou termos que não estejam escritos nas notícias.")

PROMPT = """Notícias eleitorais publicadas hoje (índice, título — resumo):

{items}

Extraia até {max_items} termos que sirvam para buscar discussões sobre estas notícias em redes sociais (Reddit, fóruns).
Categorias:
- person: candidatos, políticos, autoridades (nome como escrito na notícia)
- organization: partidos, coligações, tribunais, movimentos, órgãos
- event: debates, convenções, decisões judiciais, pesquisas, votações, incidentes (ex.: "debate da Band", "registro de candidatura")
- topic: tema de campanha explicitamente citado (ex.: "segurança pública", "reforma tributária")
- location: cidade, estado ou região relevante para o fato
- specific_term: nome de proposta, projeto, operação, slogan ou hashtag

Regras:
- Inclua todas as pessoas, partidos e instituições citados, mesmo os que aparecem só uma vez.
- Copie cada termo exatamente como aparece no texto (mesma grafia, com acentos). Não traduza nem resuma.
- Pessoas sem o cargo: "Estela Aranha", não "ministra Estela Aranha".
- Um termo por entidade; não combine entidade + assunto no mesmo termo.
- Não inclua o nome do veículo que publicou a notícia nem datas.
- Não use termos genéricos sozinhos ("eleição", "política", "candidato", "campanha", "voto").
- Em "articles", liste os índices das notícias onde o termo aparece.
- Se não houver termos específicos, devolva lista vazia."""


def _schema(max_items: int, n_articles: int) -> dict:
    return {
        "type": "object",
        "properties": {"keywords": {"type": "array", "maxItems": max_items, "items": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "minLength": 2, "maxLength": 80},
                "category": {"type": "string", "enum": ATOMIC_CATEGORIES},
                "articles": {"type": "array", "minItems": 1, "maxItems": n_articles,
                             "items": {"type": "integer", "minimum": 0, "maximum": n_articles - 1}},
            },
            "required": ["keyword", "category", "articles"]}}},
        "required": ["keywords"],
    }


_STRAY_QUOTES = re.compile(r"(?<!\w)['’‘\"“”]|['’‘\"“”](?!\w)")


_LEADING_FUNCTION_WORDS = re.compile(r"^(?:(?:do|da|dos|das|de|o|a|os|as|no|na|em)\s+)+", re.I)


def clean_keyword(kw: str) -> str:
    """Tira espaços extras, pontuação nas pontas, aspas soltas ("censura' do TSE") e preposição/artigo
    inicial ("do TSE" -> "TSE"); mantém d'água."""
    kw = _STRAY_QUOTES.sub("", str(kw))
    kw = re.sub(r"\s+", " ", kw).strip(" .,;:-")
    return _LEADING_FUNCTION_WORDS.sub("", kw) if " " in kw else kw


def extract_candidates(batch: list[Article], llm: Callable[..., dict], cfg: dict) -> None:
    """Uma chamada ao LLM para o lote; grava candidatos em cada artigo (artigo vira 'processado')."""
    items = "\n".join(f"[{i}] {a.title} — {a.summary}" for i, a in enumerate(batch))
    max_items = cfg["max_candidates_per_batch"]
    out = llm(PROMPT.format(items=items, max_items=max_items), _schema(max_items, len(batch)),
              system=SYSTEM, temperature=cfg["llm_temperature"])
    for a in batch:
        a.candidates = []
    for c in out.get("keywords", []):
        kw = clean_keyword(c.get("keyword", ""))
        if not kw or c.get("category") not in ATOMIC_CATEGORIES:
            continue
        cand = {"keyword": kw, "category": c["category"]}
        for i in set(c.get("articles", [])):
            if isinstance(i, int) and 0 <= i < len(batch) and cand not in batch[i].candidates:
                batch[i].candidates.append(cand)


# --- agregação determinística ----------------------------------------------------------------
_DATEISH = re.compile(r"^(\d+[ao]?|janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|"
                      r"novembro|dezembro|segunda|terca|quarta|quinta|sexta|sabado|domingo|feira|"
                      r"hoje|ontem|amanha|primeiro|primeira|º|o)$")


def _content_toks(key: str) -> list[str]:
    return [t for t in key.split() if t not in STOPWORDS]


def _initials(key: str) -> str:
    """'tribunal superior eleitoral' -> 'tse'; 'advocacia-geral uniao' -> 'agu'."""
    return "".join(p[0] for t in _content_toks(key) for p in t.split("-") if p)


def _strip_title(kw: str, titles: list[str]) -> str:
    """'ministra Estela Aranha' -> 'Estela Aranha' (só se sobrar um nome)."""
    words = kw.split()
    while len(words) > 1 and fold(words[0]) in {fold(t) for t in titles}:
        words = words[1:]
    return " ".join(words)


def _literal_form(form: str, texts: list[str]) -> str | None:
    """Recupera a grafia exata usada na notícia ('joao da silva' -> 'João da Silva')."""
    target = [fold(w) for w in re.findall(r"[#\w]+", form)]
    n = len(target)
    for text in texts:
        words = list(re.finditer(r"[#\w]+", text))
        folded = [fold(w.group()) for w in words]
        for i in range(len(words) - n + 1):
            if folded[i:i + n] == target:
                return text[words[i].start(): words[i + n - 1].end()]
    return None


def _display(forms: Counter, texts: list[str]) -> str:
    def rank(f):  # mais frequente; depois com acentos; depois não-caixa-alta ("JOÃO" < "João")
        return (forms[f], sum(c != fold(c) for c in f), not f.isupper(), -len(f))
    for f in sorted(forms, key=rank, reverse=True):
        lit = _literal_form(f, texts)
        if lit:
            return lit
    return max(forms, key=rank)


ORG_WRAPPERS = {"tv", "rede", "grupo", "canal", "emissora", "portal", "jornal",   # "TV Globo" -> "Globo"
                "pesquisa", "instituto"}                                         # "pesquisa Datafolha" -> "Datafolha"
PARTY_WORDS = {"partido"}                                                        # "Novo" -> "Partido Novo"


def _merge_duplicates(atomic: list[dict], cfg: dict, stats: Counter) -> list[dict]:
    """Funde termos que nomeiam a mesma coisa; o fundido vira alias do que fica."""
    def absorb(keep, gone):
        ids = {a.id for a in keep["articles"]}
        keep["articles"] = keep["articles"] + [a for a in gone["articles"] if a.id not in ids]
        keep.setdefault("aliases", [])
        for al in [gone["keyword"], *gone.get("aliases", [])]:
            if al not in keep["aliases"] and al != keep["keyword"]:
                keep["aliases"].append(al)
        gone["_merged"] = True
        stats["duplicates_removed"] += 1

    orgs = [k for k in atomic if k["category"] in ("organization", "event", "specific_term")]
    people = [k for k in atomic if k["category"] == "person"]

    # 1) mesma chave canônica (sem "TV", "rede", "pesquisa"...): "debate na Globo" = "debate da TV Globo"
    def canon(k):
        toks = [t for t in k["key"].split() if t not in ORG_WRAPPERS]
        return " ".join(toks) or k["key"]
    by_canon: dict[str, list] = {}
    for k in atomic:
        by_canon.setdefault(canon(k), []).append(k)
    for group in by_canon.values():
        if len(group) > 1:
            group.sort(key=lambda k: (len(k["key"]) != len(canon(k)), -len(k["articles"])))
            for g in group[1:]:
                absorb(group[0], g)

    # 2) sigla/apelido de lugar -> nome completo ("SP" -> "São Paulo", "Rio" -> "Rio de Janeiro")
    loc = {entity_key(a): entity_key(full) for a, full in cfg.get("location_aliases", {}).items()}
    by_key = {k["key"]: k for k in atomic if not k.get("_merged")}
    for k in list(by_key.values()):
        full = by_key.get(loc.get(k["key"], ""))
        if full is not None and full is not k and not k.get("_merged"):
            absorb(full, k)

    # 3) sobrenome sozinho -> nome completo, se houver UM único nome completo com esse sobrenome
    #    ("Haddad" -> "Fernando Haddad"); com vários ("Bolsonaro": Flávio, Jair, Michelle) fica separado
    cov = cfg.get("surname_merge_coverage", 0.5)
    for p in people:
        if p.get("_merged") or len(p["key"].split()) != 1:
            continue
        longer = [q for q in people if not q.get("_merged") and q is not p
                  and q["key"].split()[-1] == p["key"]]
        if len(longer) == 1:
            ids_long = {a.id for a in longer[0]["articles"]}
            if sum(a.id in ids_long for a in p["articles"]) >= cov * len(p["articles"]):
                absorb(longer[0], p)

    # 4) termo contido em outro (qualquer categoria) cujas notícias quase sempre trazem o termo longo:
    #    "Nossa Senhora"/"Aparecida" -> "Nossa Senhora Aparecida"
    cov_any = cfg.get("contained_merge_coverage", 0.8)
    #    (pessoa contida em pessoa não: sobrenome/primeiro nome têm regras próprias)
    live = [k for k in atomic if not k.get("_merged")]
    for a in sorted(live, key=lambda k: len(k["key"].split())):
        if a.get("_merged"):
            continue
        at = a["key"].split()
        for b in live:
            bt = b["key"].split()
            if (b is a or b.get("_merged") or len(bt) <= len(at) or not contains_phrase(bt, at)
                    or a["category"] == b["category"] == "person"):
                continue
            ids_b = {x.id for x in b["articles"]}
            if sum(x.id in ids_b for x in a["articles"]) >= cov_any * len(a["articles"]):
                absorb(b, a)
                break

    for a in atomic:
        if a.get("_merged"):
            continue
        at = a["key"].split()
        for b in atomic:
            if b is a or b.get("_merged") or a.get("_merged"):
                continue
            bt = b["key"].split()
            # sigla ⇄ nome completo: "TSE" + "Tribunal Superior Eleitoral" (fica o nome, sigla vira alias)
            if (a["category"] == b["category"] == "organization" and len(at) == 1 and len(bt) > 1
                    and _initials(b["key"]) == at[0]):
                absorb(b, a)
            # "TV Globo"/"Rede Globo" -> "Globo"; "Novo" -> "Partido Novo"
            elif a in orgs and b in orgs and len(bt) > len(at) and contains_phrase(bt, at):
                extra = [t for t in bt if t not in at]
                if extra and all(t in ORG_WRAPPERS for t in extra):
                    absorb(a, b)
                elif extra and all(t in PARTY_WORDS for t in extra):
                    absorb(b, a)
            # apelido no meio do nome: "Luiz Inácio Lula da Silva" -> "Lula" (sobrenome final não:
            # "Bolsonaro" pode ser Jair, Flávio, Eduardo...)
            elif (a in people and b in people and len(at) == 1 and len(bt) > 2
                  and at[0] in bt[1:-1]):
                absorb(a, b)
            # "Nunes Marques" ⊂ "Kassio Nunes Marques": nome de 2+ palavras vira alias do completo
            elif a in people and b in people and 1 < len(at) < len(bt) and contains_phrase(bt, at):
                absorb(b, a)
    return [k for k in atomic if not k.pop("_merged", False)]


def build_keywords(articles: list[Article], cfg: dict) -> tuple[list[dict], dict]:
    """Agrega candidatos de todas as notícias do dia em keywords finais. Retorna (keywords, stats)."""
    processed = [a for a in articles if a.candidates is not None]
    art_toks = {a.id: key_tokens(a.text) for a in articles}
    generic = {stem(fold(g)) for g in cfg["generic_terms"]}
    # nome do veículo ≠ assunto; compara o nome inteiro ("O Globo" é veículo, "Globo" é a emissora)
    def exact(t):
        return " ".join(tokens(t))
    excluded = {exact(t) for t in cfg.get("exclude_terms", [])}
    excluded |= {exact(s["name"]) for s in cfg.get("sources", []) if not s.get("institution")}
    stats = Counter()
    rejected = {"no_evidence": [], "generic": []}

    groups: dict[str, dict] = {}
    for a in processed:
        for c in a.candidates:
            kw = clean_keyword(c["keyword"])
            if c["category"] == "person":
                kw = clean_keyword(_strip_title(kw, cfg.get("person_titles", [])))  # "Ministra do TSE" -> "TSE"
            key = entity_key(kw)
            if not key:
                continue
            g = groups.setdefault(key, {"forms": Counter(), "cats": Counter()})
            g["forms"][kw] += 1
            g["cats"][c["category"]] += 1

    atomic = []
    for key, g in groups.items():
        content = _content_toks(key)
        if (not content or any(exact(f) in excluded for f in g["forms"]) or all(t in generic for t in content)
                or all(_DATEISH.match(t) for t in content)
                or len(content) > cfg.get("max_keyword_tokens", 4)):
            stats["rejected_generic"] += 1
            rejected["generic"].append(g["forms"].most_common(1)[0][0])
            continue
        ktoks = key.split()
        # evidência: o termo precisa aparecer literalmente (sem caixa/acentos/partículas/plural)
        # em alguma notícia do dia. O índice citado pelo LLM não é confiável, o texto é.
        matched = [a for a in articles if contains_phrase(art_toks[a.id], ktoks)]
        if not matched:
            stats["rejected_no_evidence"] += 1
            rejected["no_evidence"].append(g["forms"].most_common(1)[0][0])
            continue
        atomic.append({"key": key, "category": g["cats"].most_common(1)[0][0],
                       "keyword": clean_keyword(_display(g["forms"], [a.text for a in matched])),
                       "articles": matched})

    atomic = _merge_duplicates(atomic, cfg, stats)

    # primeiro nome sozinho é ambíguo ("Flávio" → Bolsonaro ou Dino), sobrenome/apelido não ("Lula");
    # e um nome curto cujas notícias já estão todas cobertas pelo nome longo não acrescenta nada
    people = [k for k in atomic if k["category"] == "person"]
    drop = set()
    for p in people:
        ptoks = p["key"].split()
        longer = [q for q in people if q is not p and len(q["key"].split()) > len(ptoks)
                  and contains_phrase(q["key"].split(), ptoks)]
        ids = {a.id for a in p["articles"]}
        first_name = len(ptoks) == 1 and any(q["key"].split()[0] == ptoks[0] for q in longer)
        if first_name or any(ids <= {a.id for a in q["articles"]} for q in longer):
            drop.add(p["key"])
    stats["merged_partial_names"] = len(drop)
    atomic = [k for k in atomic if k["key"] not in drop]

    # combinações entidade + evento/tema: só quando co-ocorrem em >= N notícias do dia (com N=1 a
    # combinação repetiria a própria entidade; o contexto já entra nas search_queries dela)
    combos = []
    max_df = (cfg.get("combo_context_max_df", 1.0) * len(articles)
              if len(articles) >= cfg.get("combo_df_min_articles", 20) else float("inf"))
    entities = [k for k in atomic if k["category"] in ENTITY_CATEGORIES]
    contexts = [k for k in atomic if k["category"] in CONTEXT_CATEGORIES and len(k["articles"]) <= max_df]
    for e in entities:
        ids_e = {a.id for a in e["articles"]}
        scored = []
        for t in contexts:
            co = [a for a in t["articles"] if a.id in ids_e]
            if (len(co) >= cfg["min_cooccurrence_for_combo"]
                    and t["key"] not in e["key"] and e["key"] not in t["key"]
                    and t["key"].replace(" ", "") != _initials(e["key"])):   # "Tribunal Superior Eleitoral" + "TSE"
                scored.append((len(co), t, co))
        for n, t, co in sorted(scored, key=lambda x: -x[0])[:2]:
            combos.append({"key": f"{e['key']} {t['key']}", "category": "combination",
                           "keyword": f"{e['keyword']} {t['keyword']}", "articles": co,
                           "components": [e["keyword"], t["keyword"]]})

    out = []
    for k in atomic + combos:
        arts = k["articles"]
        srcs = sorted({s for a in arts for s in a.sources})
        ac, sc = len(arts), len(srcs)
        item = {
            "keyword": k["keyword"], "category": k["category"], "priority": None, "reason": "",
            "source_count": sc, "article_count": ac,
            "_key": k["key"], "_articles": arts,
        }
        if "components" in k:
            item["components"] = k["components"]
        if k.get("aliases"):
            item["aliases"] = k["aliases"]
        out.append(item)

    # ordem só por ocorrência no dia: nº de notícias, depois nº de fontes
    out.sort(key=lambda k: (-k["article_count"], -k["source_count"], k["keyword"]))
    # passada final: nenhuma chave repetida (ex.: combinação igual a um termo atômico)
    seen, unique = set(), []
    for k in out:
        ident = " ".join(sorted(k["_key"].split()))
        if ident not in seen:
            seen.add(ident)
            unique.append(k)
    stats["duplicates_removed"] = stats.get("duplicates_removed", 0) + len(out) - len(unique)
    out = unique
    cap = min(cfg["max_keywords"], cfg["max_keywords_per_article"] * max(len(processed), 1))
    max_combos = int(cap * cfg.get("max_combo_share", 1.0))
    kept, n_combo = [], 0
    for k in out:
        if k["category"] == "combination":
            if n_combo >= max_combos:
                continue
            n_combo += 1
        kept.append(k)
    stats["truncated"] = max(0, len(kept) - cap) + (len(out) - len(kept))
    out = kept[:cap]
    # prioridade provisória por quebras naturais da ocorrência do dia; o pipeline refaz com o LLM
    stats["priority_analysis"] = classify(out, len(articles), llm=None)

    for k in out:
        k["search_variants"] = search_variants(k, cfg)
        k["search_queries"] = search_queries(k, contexts, cfg)
        k["evidence_urls"] = [a.url for a in k["_articles"][: cfg["max_evidence_urls"]] if a.url]
        for f in ("_key", "_articles"):
            k.pop(f)
    return out, {**stats, "rejected_terms": {k: v[:30] for k, v in rejected.items()}}


def search_variants(k: dict, cfg: dict) -> list[str]:
    """Forma principal (com acentos), entre aspas, aliases fundidos ("TSE") e sem acentos.
    Combinações: cada componente entre aspas — a frase inteira não existe no texto."""
    kw = k["keyword"]
    quoted = " ".join(f'"{c}"' for c in k["components"]) if "components" in k else f'"{kw}"'
    v = [kw, quoted] + [f'"{a}"' for a in k.get("aliases", [])]
    if strip_accents(kw) != kw:
        v.append(strip_accents(kw))
    return v[: cfg["max_search_variants_per_keyword"]]


def search_queries(k: dict, contexts: list[dict], cfg: dict) -> list[str]:
    """Consultas de alta precisão: termo entre aspas + eventos/temas EXTRAÍDOS DAS NOTÍCIAS DO DIA que
    co-ocorrem com ele (nenhum vocabulário fixo)."""
    base = f'"{k.get("components", [k["keyword"]])[0]}"'
    if k["category"] == "combination":
        qs = [f'"{k["components"][0]}" "{k["components"][1]}"', f'"{k["components"][0]}" {k["components"][1]}']
    else:
        qs = [base] + [f'"{a}"' for a in k.get("aliases", [])[:1]]
        ids = {a.id for a in k["_articles"]}
        co = [(sum(a.id in ids for a in t["articles"]), t) for t in contexts
              if t["key"] not in k["_key"] and k["_key"] not in t["key"]]
        for n, t in sorted(co, key=lambda x: (-x[0], x[1]["keyword"]))[:3]:
            if n:
                qs.append(f'{base} "{t["keyword"]}"')
    seen, out = set(), []
    for q in qs:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out[: cfg["max_search_queries_per_keyword"]]
