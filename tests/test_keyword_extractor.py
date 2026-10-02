"""Testes do módulo keywords — sem rede e sem LLM (feeds e modelo falsos)."""
import copy
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import feedparser
import pytest

from keyword_extractor import social, storage
from keyword_extractor.collector import Article, SourceError, collect, parse_date
from keyword_extractor.config import CONFIG
from keyword_extractor.dedup import dedup
from keyword_extractor.extract import build_keywords
from keyword_extractor.filters import filter_by_date, filter_electoral
from keyword_extractor.pipeline import run

TZ = ZoneInfo("America/Sao_Paulo")
DAY1 = datetime(2026, 10, 1, 15, 0, tzinfo=TZ)
DAY2 = datetime(2026, 10, 2, 9, 0, tzinfo=TZ)


# --- fixtures ----------------------------------------------------------------------------------
def rss(items):
    body = "".join(
        f"<item><title>{t}</title><link>{link}</link><description>{d}</description>"
        f"<pubDate>{pub}</pubDate><guid>{link}</guid></item>" for t, d, link, pub in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


D1 = "Thu, 01 Oct 2026 10:00:00 -0300"
D1_LATE = "Thu, 01 Oct 2026 23:59:00 -0300"
D0 = "Wed, 30 Sep 2026 23:30:00 -0300"
D2 = "Fri, 02 Oct 2026 08:00:00 -0300"

NEWS_A = [  # portal A
    ("João da Silva vence debate eleitoral na TV", "Candidato do Partido Azul falou de segurança pública no debate.",
     "https://a.com/n1?utm_source=rss", D1),
    ("Maria Souza lidera pesquisa eleitoral em Pelotas", "Candidata do Partido Verde tem 40% das intenções de voto em Pelotas.",
     "https://a.com/n2", D1),
    ("Juiz nega pedido em caso de homicídio nos EUA", "Tribunal de Boston rejeita recurso da defesa.",
     "https://a.com/n3", D1),
    ("Eleição de ontem: resultado da apuração", "Notícia antiga sobre urna.", "https://a.com/old", D0),
]
NEWS_B = [  # portal B (republica a n1 com outro título parecido)
    ("JOÃO DA SILVA vence debate eleitoral na TV", "Candidato do Partido Azul falou sobre segurança pública.",
     "https://b.com/x1", D1),
    ("TSE divulga regras para votar no domingo", "Eleitor pode usar e-Título; João Silva comentou as regras.",
     "https://b.com/x2", D1),
]


class FakeLLM:
    """Devolve, para cada termo conhecido, os índices das notícias do prompt que o contêm."""

    def __init__(self, terms: dict, forced: list | None = None):
        self.terms, self.forced, self.calls = terms, forced or [], 0

    def __call__(self, prompt, schema, system=None, temperature=0.3, **kw):
        if "high_min" in schema["properties"]:   # análise de prioridade: não conta como extração
            self.priority_calls = getattr(self, "priority_calls", 0) + 1
            return {"high_min": 0, "medium_min": 0, "justificativa": "inválido → Jenks"}
        self.calls += 1
        lines = re.findall(r"^\[(\d+)\] (.*)$", prompt, flags=re.M)
        out = []
        for term, cat in self.terms.items():
            idx = [int(i) for i, txt in lines if term.lower() in txt.lower()]
            if idx:
                out.append({"keyword": term, "category": cat, "articles": idx})
        for f in self.forced:
            out.append({**f, "articles": [0]})
        return {"keywords": out}


TERMS = {"João da Silva": "person", "Maria Souza": "person", "Partido Azul": "organization",
         "Partido Verde": "organization", "segurança pública": "topic", "debate eleitoral": "event",
         "Pelotas": "location", "TSE": "organization", "e-Título": "specific_term"}


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(CONFIG)
    c["data_dir"] = str(tmp_path)
    c["sources"] = [{"name": "Portal A", "type": "rss", "url": "http://a/rss"},
                    {"name": "Portal B", "type": "rss", "url": "http://b/rss"}]
    return c


def make_get(feeds):
    def get(url, cfg, src=None):
        v = feeds[url]
        if isinstance(v, Exception):
            raise v
        return v
    return get


def art(title, summary="", published="2026-10-01T10:00:00-03:00", url="", source="A", **kw):
    return Article(id=f"{source}{title}"[:40], source=source, feed="", title=title, summary=summary,
                   url=url or f"https://{source}.com/{abs(hash(title))}", published=published,
                   sources=[source], **kw)


# --- 1/2. filtro temporal ----------------------------------------------------------------------
def test_noticia_de_ontem_rejeitada(cfg):
    ontem = art("Eleição", published="2026-09-30T23:30:00-03:00")
    ontem_utc = art("Eleição 2", published="2026-10-01T02:30:00+00:00")  # 23:30 de 30/09 em SP
    assert filter_by_date([ontem, ontem_utc], DAY1.date(), cfg) == []


def test_noticia_de_hoje_aceita(cfg):
    hoje = [art("a", published="2026-10-01T00:00:00-03:00"), art("b", published="2026-10-01T23:59:59-03:00")]
    assert filter_by_date(hoje, DAY1.date(), cfg) == hoje


def test_noticia_sem_data_rejeitada(cfg):
    assert filter_by_date([art("x", published=None)], DAY1.date(), cfg) == []


def test_data_rfc822_em_portugues():
    e = feedparser.FeedParserDict(published="Qui, 01 Out 2026 18:50:00 -0300")
    assert parse_date(e).isoformat() == "2026-10-01T18:50:00-03:00"


# --- 3/4. filtro eleitoral ---------------------------------------------------------------------
def test_noticia_nao_eleitoral_rejeitada(cfg):
    a = [art("Juiz nega pedido em caso de homicídio", "Tribunal de Boston rejeita recurso."),
         art("Câmara aprova projeto em votação", "Deputado relator defende texto do partido.")]
    assert filter_electoral(a, DAY1.date(), cfg) == []


def test_eleitoral_sem_palavra_eleicao(cfg):
    a = art("Candidato anuncia nova proposta para segurança pública", "Proposta prevê mais policiais.")
    assert filter_electoral([a], DAY1.date(), cfg) == [a]
    # fora do período de campanha e sem contexto, "candidato" sozinho não basta
    assert filter_electoral([a], date(2027, 3, 1), cfg) == []
    # ...mas um feed de política dá o contexto
    a.electoral_context = True
    assert filter_electoral([a], date(2027, 3, 1), cfg) == [a]


# --- 5. dedup ------------------------------------------------------------------------------------
def test_duplicatas_eliminadas():
    a1 = art("João da Silva vence debate", url="https://www.a.com/n1/?utm_source=rss", source="A")
    a2 = art("João da Silva vence debate", url="https://a.com/n1", source="A2")         # mesma URL
    b1 = art("JOÃO DA SILVA vence o debate", url="https://b.com/zz", source="B")        # título similar
    c1 = art("Maria Souza lidera pesquisa", url="https://c.com/1", source="C")
    fresh, removed = dedup([a1, a2, b1, c1], [], threshold=0.85)
    assert fresh == [a1, c1] and removed == 2
    assert set(a1.sources) == {"A", "A2", "B"}


# --- 6. normalização -----------------------------------------------------------------------------
def test_mesma_entidade_normalizada():
    arts = [art("João da Silva fala em comício", source="A"), art("JOÃO DA SILVA no debate", source="B"),
            art("joao da silva e Maria", source="C")]
    for a, form in zip(arts, ["JOÃO DA SILVA", "joao da silva", "João Silva"]):
        a.candidates = [{"keyword": form, "category": "person"}]
    kws, _ = build_keywords(arts, CONFIG)
    people = [k for k in kws if k["category"] == "person"]
    assert len(people) == 1
    assert people[0]["keyword"] == "João da Silva"         # acentos preservados, forma do texto
    assert people[0]["article_count"] == 3 and people[0]["source_count"] == 3
    assert people[0]["reason"].startswith("3 notícias")
    assert "Joao da Silva" in people[0]["search_variants"]


# --- 9. evidência --------------------------------------------------------------------------------
def test_keyword_sem_evidencia_rejeitada():
    a = art("Maria Souza lidera pesquisa em Pelotas")
    a.candidates = [{"keyword": "Maria Souza", "category": "person"},
                    {"keyword": "Fulano Inventado", "category": "person"},       # alucinação
                    {"keyword": "Pelotas Futebol Clube", "category": "organization"},  # extrapolação
                    {"keyword": "eleição", "category": "topic"}]                  # genérico
    kws, stats = build_keywords([a], CONFIG)
    assert [k["keyword"] for k in kws if k["category"] != "combination"] == ["Maria Souza"]
    assert stats["rejected_no_evidence"] == 2 and stats["rejected_generic"] == 1


def test_combinacao_exige_coocorrencia():
    a = art("Maria Souza fala de segurança pública", source="A")
    a2 = art("Maria Souza detalha plano de segurança pública", source="B")
    b = art("João da Silva visita escola; segurança pública", source="C")   # só 1 coocorrência
    for x in (a, a2):
        x.candidates = [{"keyword": "Maria Souza", "category": "person"},
                        {"keyword": "segurança pública", "category": "topic"}]
    b.candidates = [{"keyword": "João da Silva", "category": "person"}]
    kws, _ = build_keywords([a, a2, b], CONFIG)
    combos = [k["keyword"] for k in kws if k["category"] == "combination"]
    assert combos == ["Maria Souza segurança pública"]


# --- pipeline completo -----------------------------------------------------------------------
def feeds(a=NEWS_A, b=NEWS_B):
    return {"http://a/rss": rss(a), "http://b/rss": rss(b)}


def test_pipeline_ponta_a_ponta(cfg):
    llm = FakeLLM(TERMS, forced=[{"keyword": "Candidato Fantasma", "category": "person"}])
    out = run(now=DAY1, cfg=cfg, get=make_get(feeds()), llm=llm)
    assert out["collection_date"] == "2026-10-01"
    assert out["sources"] == {"Portal A": "ok", "Portal B": "ok"}
    assert out["articles_collected"] == 5          # a notícia de 30/09 ficou de fora
    assert out["electoral_articles"] == 3          # homicídio fora; n1 de B fundida com n1 de A
    kw = {k["keyword"]: k for k in out["keywords"]}
    assert "Candidato Fantasma" not in kw
    assert kw["João da Silva"]["source_count"] == 2
    assert kw["João da Silva"]["article_count"] == 2   # n1 (A+B) e x2 ("João Silva")
    assert all(len(k["search_queries"]) <= cfg["max_search_queries_per_keyword"] for k in out["keywords"])
    assert kw["Pelotas"]["search_queries"][0] == '"Pelotas"'
    assert storage.output_path(cfg["data_dir"], DAY1.date()).exists()


# --- 7. fonte indisponível ---------------------------------------------------------------------
def test_fonte_indisponivel_nao_interrompe(cfg):
    cfg["sources"].append({"name": "Portal C", "type": "rss", "url": "http://c/rss"})
    cfg["sources"].append({"name": "Portal D", "type": "rss", "url": "http://d/rss"})
    f = feeds()
    f["http://c/rss"] = SourceError("HTTP 403")
    f["http://d/rss"] = b"<html><body>Request Rejected</body></html>"   # WAF devolvendo HTML
    out = run(now=DAY1, cfg=cfg, get=make_get(f), llm=FakeLLM(TERMS))
    assert out["sources"]["Portal A"] == "ok" and out["sources"]["Portal B"] == "ok"
    assert out["sources"]["Portal C"].startswith("error")
    assert out["sources"]["Portal D"].startswith("error")
    assert out["sources_processed"] == 2 and out["keywords"]


def test_llm_fora_do_ar_nao_perde_noticias(cfg):
    def broken(*a, **k):
        raise ConnectionError("llama-server down")
    out = run(now=DAY1, cfg=cfg, get=make_get(feeds()), llm=broken)
    assert out["llm_error"] and out["electoral_articles"] == 3 and out["articles_analyzed"] == 0
    llm = FakeLLM(TERMS)
    out = run(now=DAY1.replace(hour=16), cfg=cfg, get=make_get(feeds()), llm=llm)
    assert out["articles_analyzed"] == 3 and llm.calls == 1


# --- 8. limite de keywords ---------------------------------------------------------------------
def test_limite_maximo_de_keywords(cfg):
    cfg["max_keywords"] = 5
    out = run(now=DAY1, cfg=cfg, get=make_get(feeds()), llm=FakeLLM(TERMS))
    assert len(out["keywords"]) == 5
    assert [k["priority"] for k in out["keywords"]] == sorted(
        (k["priority"] for k in out["keywords"]), key=["high", "medium", "low"].index)


# --- 10. execução múltipla no mesmo dia --------------------------------------------------------
def test_execucao_multipla_nao_duplica(cfg):
    llm = FakeLLM(TERMS)
    out1 = run(now=DAY1.replace(hour=8), cfg=cfg, get=make_get(feeds()), llm=llm)
    out2 = run(now=DAY1.replace(hour=12), cfg=cfg, get=make_get(feeds()), llm=llm)
    assert out2["electoral_articles"] == out1["electoral_articles"]
    assert llm.calls == 1                                  # nada novo → LLM não é chamado
    assert out2["keywords"] == out1["keywords"] and out2["runs_today"] == 2

    nova = ("Maria Souza promete reforçar segurança pública", "Candidata detalha plano de governo.",
            "https://a.com/n9", "Thu, 01 Oct 2026 17:00:00 -0300")
    out3 = run(now=DAY1.replace(hour=18), cfg=cfg, get=make_get(feeds(a=NEWS_A + [nova])), llm=llm)
    assert out3["electoral_articles"] == out1["electoral_articles"] + 1
    st = storage.load_state(cfg["data_dir"], DAY1.date(), cfg["timezone"])
    assert st["runs"][-1]["new"] == 1 and st["runs"][-1]["sent_to_llm"] == 1
    assert st["last_collection"] == "2026-10-01T18:00:00-03:00"


# --- 11. mudança de data -----------------------------------------------------------------------
def test_mudanca_de_data_gera_novo_conjunto(cfg):
    run(now=DAY1, cfg=cfg, get=make_get(feeds()), llm=FakeLLM(TERMS))
    novo_dia = [("Partido Verde anuncia apoio no segundo turno", "Eleição em Pelotas terá segundo turno.",
                 "https://a.com/d2", D2)]
    out = run(now=DAY2, cfg=cfg, get=make_get(feeds(a=NEWS_A + novo_dia, b=NEWS_B)), llm=FakeLLM(TERMS))
    assert out["collection_date"] == "2026-10-02" and out["electoral_articles"] == 1
    kws = {k["keyword"] for k in out["keywords"]}
    assert "João da Silva" not in kws and "Maria Souza" not in kws    # nada herdado de 01/10
    assert "Partido Verde" in kws
    assert storage.load_output(cfg["data_dir"], DAY1.date())["collection_date"] == "2026-10-01"


def test_social_so_entrega_hoje(cfg):
    run(now=DAY1, cfg=cfg, get=make_get(feeds()), llm=FakeLLM(TERMS))
    with pytest.raises(ValueError):           # dia que não é hoje exige allow_history
        social.load_keywords(date(2020, 1, 1), cfg=cfg)
    hist = social.load_keywords(date(2026, 10, 1), allow_history=True, cfg=cfg)
    assert hist and all("search_queries" in k for k in hist)


# --- ajustes vindos da execução real (01/10/2026) ----------------------------------------------
def test_debate_como_verbo_nao_e_eleitoral(cfg):
    a = art("Conselho de Comunicação debate audiovisual na segunda-feira", "Reunião sobre comunicação pública.")
    assert filter_electoral([a], DAY1.date(), cfg) == []


def test_cargo_portal_e_data_nao_viram_keyword():
    a = art("Ministra Estela Aranha proíbe exibição no debate", "Decisão do TSE em 4 de outubro.", source="UOL Notícias")
    a.candidates = [{"keyword": "ministra Estela Aranha", "category": "person"},
                    {"keyword": "UOL Notícias", "category": "organization"},
                    {"keyword": "4 de outubro", "category": "event"}]
    c = {**CONFIG, "sources": [{"name": "UOL Notícias"}]}
    kws, _ = build_keywords([a], c)
    assert [k["keyword"] for k in kws] == ["Estela Aranha"]


def test_termos_genericos_isolados_rejeitados():
    a = art("Debate: entrevista e regras do 1º turno da República")
    a.candidates = [{"keyword": k, "category": "event"} for k in ("debate", "Entrevista", "regras", "1º", "da República")]
    kws, stats = build_keywords([a], CONFIG)
    assert kws == [] and stats["rejected_generic"] == 5


def test_combinacao_so_com_termos_extraidos_do_dia():
    arts = [art("Lula falta ao debate na Globo", source="A"), art("Cadeira vazia de Lula no debate na Globo", source="B")]
    for a in arts:
        a.candidates = [{"keyword": "Lula", "category": "person"}]
    kws, _ = build_keywords(arts, CONFIG)
    assert not [k for k in kws if k["category"] == "combination"]     # "debate" não foi extraído: sem combo
    assert all("debate" not in q for k in kws for q in k["search_queries"])
    for a in arts:
        a.candidates.append({"keyword": "debate na Globo", "category": "event"})
    kws, _ = build_keywords(arts, CONFIG)
    combo = next(k for k in kws if k["category"] == "combination")
    assert combo["keyword"] == "Lula debate na Globo" and combo["article_count"] == 2
    lula = next(k for k in kws if k["keyword"] == "Lula")
    assert '"Lula" "debate na Globo"' in lula["search_queries"]


def test_headers_e_cookies_por_fonte(monkeypatch):
    from keyword_extractor import collector
    seen = {}

    class R:
        status_code, content = 200, rss([])

    def fake_get(url, timeout, headers, cookies):
        seen.update(headers=headers, cookies=cookies)
        return R()
    monkeypatch.setattr(collector.requests, "get", fake_get)
    monkeypatch.setenv("X_COOKIES", "a=1; bm_sz=xyz")
    src = {"url": "http://x", "headers": {"User-Agent": "Mozilla/5.0", "Accept-Language": "pt-BR"},
           "cookies_env": "X_COOKIES"}
    collector.http_get("http://x", CONFIG, src)
    assert seen["headers"]["User-Agent"] == "Mozilla/5.0" and seen["headers"]["Accept-Language"] == "pt-BR"
    assert seen["cookies"] == {"a": "1", "bm_sz": "xyz"}
    collector.http_get("http://x", CONFIG)                      # fonte sem extras: UA do projeto
    assert seen["headers"] == {"User-Agent": CONFIG["user_agent"]} and seen["cookies"] == {}


def test_nome_parcial_ambiguo_e_sigla_da_propria_entidade():
    arts = [art("Flávio Bolsonaro sobe em pesquisa do TSE", source="A"),
            art("Flávio Dino nega pedido; Flávio Bolsonaro recorre ao TSE", source="B"),
            art("Tribunal Superior Eleitoral (TSE) divulga regras", source="C"),
            art("Tribunal Superior Eleitoral TSE amplia horário", source="D")]
    for a in arts:
        a.candidates = [{"keyword": k, "category": "person"} for k in ("Flávio", "Flávio Bolsonaro", "Flávio Dino")]
        a.candidates.append({"keyword": "Tribunal Superior Eleitoral", "category": "organization"})
    kws, stats = build_keywords(arts, CONFIG)
    names = {k["keyword"] for k in kws}
    assert "Flávio" not in names and {"Flávio Bolsonaro", "Flávio Dino"} <= names
    assert "Tribunal Superior Eleitoral TSE" not in names


def test_manchete_inteira_nao_vira_keyword():
    a = art("Quem está na frente para presidente 2026 segundo nova pesquisa")
    a.candidates = [{"keyword": "Quem está na frente para presidente 2026 segundo nova pesquisa", "category": "topic"}]
    assert build_keywords([a], CONFIG)[0] == []


def test_primeiro_nome_sozinho_descartado_sobrenome_mantido():
    arts = [art("Flávio Bolsonaro e Lula no debate", source="A"), art("Flávio Dino fala; Lula responde", source="B"),
            art("Luiz Inácio Lula da Silva vota", source="C")]
    for a in arts:
        a.candidates = [{"keyword": k, "category": "person"}
                        for k in ("Flávio", "Flávio Bolsonaro", "Lula", "Luiz Inácio Lula da Silva")]
    kws = build_keywords(arts, CONFIG)[0]
    names = {k["keyword"] for k in kws}
    assert "Flávio" not in names and names == {"Lula", "Flávio Bolsonaro"}
    lula = next(k for k in kws if k["keyword"] == "Lula")          # nome completo vira alias
    assert lula["aliases"] == ["Luiz Inácio Lula da Silva"] and lula["article_count"] == 3


def test_duplicatas_de_keyword_fundidas():
    arts = [art("TSE e Tribunal Superior Eleitoral; TV Globo e Rede Globo", source="A"),
            art("Globo exibe debate na Globo; debate da Globo; Partido Novo e Novo", source="B")]
    for a in arts:
        a.candidates = [{"keyword": k, "category": c} for k, c in [
            ("TSE", "organization"), ("Tribunal Superior Eleitoral", "organization"),
            ("Globo", "organization"), ("TV Globo", "organization"), ("Rede Globo", "organization"),
            ("debate na Globo", "event"), ("debate da Globo", "event"),
            ("Novo", "organization"), ("Partido Novo", "organization")]]
    kws, stats = build_keywords(arts, CONFIG)
    atomic = {k["keyword"]: k for k in kws if k["category"] != "combination"}
    assert set(atomic) == {"Tribunal Superior Eleitoral", "Globo", "debate na Globo", "Partido Novo"}
    assert atomic["Tribunal Superior Eleitoral"]["aliases"] == ["TSE"]
    assert '"TSE"' in atomic["Tribunal Superior Eleitoral"]["search_queries"]
    assert set(atomic["Globo"]["aliases"]) == {"TV Globo", "Rede Globo"}
    keys = [" ".join(sorted(k["keyword"].lower().split())) for k in kws]
    assert len(keys) == len(set(keys))


def test_contexto_ubiquo_nao_gera_combinacao_e_prioridade_relativa():
    arts = [art(f"Lula e Zema no primeiro turno, notícia {i}", source=f"S{i % 5}") for i in range(30)]
    arts += [art(f"Kassio Nunes Marques fala; Nunes Marques decide {i}", source=f"S{i}") for i in range(4)]
    for a in arts:
        a.candidates = [{"keyword": k, "category": c} for k, c in [
            ("Lula", "person"), ("Zema", "person"), ("primeiro turno", "event"),
            ("Kassio Nunes Marques", "person"), ("Nunes Marques", "person")]]
    kws, _ = build_keywords(arts, CONFIG)
    names = {k["keyword"] for k in kws}
    assert "Lula primeiro turno" not in names                  # 'primeiro turno' em 88% das notícias
    assert "Nunes Marques" not in names and "Kassio Nunes Marques" in names
    rank = {"high": 2, "medium": 1, "low": 0}
    for a in kws:                     # monotônico: prioridade maior nunca tem menos notícias
        for b in kws:
            if rank[a["priority"]] > rank[b["priority"]]:
                assert a["article_count"] > b["article_count"]


def test_sobrenome_sigla_de_estado_e_ordinal_fundidos():
    arts = [art(f"Eduardo Paes lidera no RJ; Rio de Janeiro vota no 1º turno {i}", source=f"S{i}") for i in range(6)]
    arts.append(art("Paes e Bolsonaro; Flávio Bolsonaro no primeiro turno", source="X"))
    arts.append(art("Jair Bolsonaro comenta", source="Y"))
    for a in arts:
        a.candidates = [{"keyword": k, "category": c} for k, c in [
            ("Eduardo Paes", "person"), ("Paes", "person"), ("RJ", "location"), ("Rio de Janeiro", "location"),
            ("1º turno", "event"), ("primeiro turno", "event"), ("Bolsonaro", "person"), ("Flávio Bolsonaro", "person"),
            ("Jair Bolsonaro", "person")]]
    kws, _ = build_keywords(arts, CONFIG)
    atomic = {k["keyword"]: k for k in kws if k["category"] != "combination"}
    assert "Paes" not in atomic and "Paes" in atomic["Eduardo Paes"]["aliases"]
    assert "RJ" not in atomic and "RJ" in atomic["Rio de Janeiro"]["aliases"]
    assert len([k for k in atomic if "turno" in k]) == 1
    assert "Bolsonaro" in atomic          # dois nomes completos com o sobrenome: ambíguo, fica separado


def test_sigla_so_entre_organizacoes_e_sobrenome_unico():
    arts = [art(f"PT no primeiro turno; Fernando Haddad fala {i}", source=f"S{i}") for i in range(3)]
    arts += [art(f"Haddad responde {i}", source=f"H{i}") for i in range(2)]
    arts += [art("Nossa Senhora Aparecida; Nossa Senhora", source="N")]
    for a in arts:
        a.candidates = [{"keyword": k, "category": c} for k, c in [
            ("PT", "organization"), ("primeiro turno", "event"), ("Fernando Haddad", "person"),
            ("Haddad", "person"), ("Nossa Senhora", "person"), ("Nossa Senhora Aparecida", "specific_term")]]
    kws, _ = build_keywords(arts, CONFIG)
    atomic = {k["keyword"]: k for k in kws if k["category"] != "combination"}
    assert "PT" in atomic and "PT" not in atomic["primeiro turno"].get("aliases", [])
    assert "Haddad" not in atomic and atomic["Fernando Haddad"]["article_count"] == 5
    assert "Nossa Senhora" not in atomic and "Nossa Senhora Aparecida" in atomic


def test_aspas_soltas_removidas():
    from keyword_extractor.extract import clean_keyword
    assert clean_keyword("censura' do TSE") == "censura do TSE"
    assert clean_keyword('"Lula"') == "Lula"
    assert clean_keyword("pau-d'arco") == "pau-d'arco"
    assert clean_keyword("do TSE") == "TSE" and clean_keyword("a Globo") == "Globo"
    assert clean_keyword("Rio de Janeiro") == "Rio de Janeiro"


def test_pessoas_nao_sao_fundidas_por_contencao_generica():
    arts = [art(f"Flávio Bolsonaro lidera {i}", source=f"S{i}") for i in range(8)]
    arts += [art("Bolsonaro comenta", source="X"), art("Jair Bolsonaro vota", source="Y"),
             art("Michelle Bolsonaro discursa", source="Z")]
    for a in arts:
        a.candidates = [{"keyword": k, "category": "person"} for k in
                        ("Flávio", "Bolsonaro", "Flávio Bolsonaro", "Jair Bolsonaro", "Michelle Bolsonaro")]
    kws, _ = build_keywords(arts, CONFIG)
    atomic = {k["keyword"]: k for k in kws if k["category"] != "combination"}
    assert "Bolsonaro" in atomic and "Flávio" not in atomic
    assert not atomic["Flávio Bolsonaro"].get("aliases")


# --- prioridade por ocorrência (priority.py) ----------------------------------------------------
from keyword_extractor import priority


def _kws(counts):
    return [{"keyword": f"k{i}", "article_count": c, "source_count": 1} for i, c in enumerate(counts)]


def test_jenks_separa_patamares_da_distribuicao():
    vals = [40, 38, 35, 12, 11, 10, 9, 2, 1, 1, 1, 1]
    lo, hi = priority.jenks_breaks(vals)
    assert hi == 35 and lo == 9


def test_cortes_do_llm_aplicados_e_monotonicos():
    kws = _kws([40, 38, 35, 12, 11, 10, 9, 2, 1, 1])
    seen = {}
    def llm(prompt, schema, **kw):
        seen["prompt"] = prompt
        return {"high_min": 30, "medium_min": 5, "justificativa": "Salto de 35 para 12 e de 9 para 2."}
    a = priority.classify(kws, 50, llm)
    assert a["method"] == "llm" and a["counts"] == {"high": 3, "medium": 4, "low": 3}
    assert "k0 (40, 1)" in seen["prompt"] and "mediana" in seen["prompt"]
    assert kws[0]["reason"] == "40 notícias (80.0% do dia) em 1 fonte."


def test_cortes_invalidos_do_llm_caem_no_jenks():
    kws = _kws([40, 38, 35, 12, 11, 10, 9, 2, 1, 1])
    for bad in ({"high_min": 1, "medium_min": 1}, {"high_min": 5, "medium_min": 9}, {"high_min": 99, "medium_min": 1}):
        a = priority.classify(kws, 50, lambda *x, **k: {**bad, "justificativa": "x"})
        assert a["method"] == "jenks" and "llm_error" in a
    a = priority.classify(kws, 50, lambda *x, **k: (_ for _ in ()).throw(ConnectionError("down")))
    assert a["method"] == "jenks"


# --- relatório por e-mail (report.py) -----------------------------------------------------------
from keyword_extractor import report

ENV = {"KEYWORD_EMAIL_PARA": "a@x.br, b@x.br", "KEYWORD_EMAIL_DE": "bot@gmail.com",
       "KEYWORD_EMAIL_NOME": "Agente", "SMTP_HOST": "smtp.test", "SMTP_PORT": "587",
       "SMTP_USER": "bot@gmail.com", "SMTP_PASS": "segredo", "SMTP_SEGURANCA": "starttls"}


def test_relatorio_top_por_prioridade_e_anexos(cfg):
    out = run(now=DAY1, cfg=cfg, get=make_get(feeds()), llm=FakeLLM(TERMS))
    msg = report.build_message(out, ENV)
    tops = report.top_by_priority(out)
    assert all(len(tops[p]) <= n for p, n in report.TOP.items())
    for p in tops:   # cada top é ordenado por ocorrência
        assert [k["article_count"] for k in tops[p]] == sorted((k["article_count"] for k in tops[p]), reverse=True)
    assert msg["To"] == "a@x.br, b@x.br" and "Agente" in msg["From"]
    names = [a.get_filename() for a in msg.iter_attachments()]
    assert names == ["keywords_2026-10-01.json", "keywords_2026-10-01.csv"]
    csv_text = next(a for a in msg.iter_attachments() if a.get_filename().endswith(".csv")).get_content()
    assert csv_text.count("\n") == len(out["keywords"]) + 1
    assert "segredo" not in msg.as_string()


def test_envio_smtp_usa_starttls_e_login(monkeypatch, cfg):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def starttls(self, context):
            calls.append(("starttls",))
        def login(self, u, p):
            calls.append(("login", u))
        def send_message(self, m):
            calls.append(("send", m["To"]))
    monkeypatch.setattr(report.smtplib, "SMTP", FakeSMTP)
    out = run(now=DAY1, cfg=cfg, get=make_get(feeds()), llm=FakeLLM(TERMS))
    report.send(report.build_message(out, ENV, to=["teste@x.br"]), ENV)
    assert calls == [("connect", "smtp.test", 587), ("starttls",), ("login", "bot@gmail.com"), ("send", "teste@x.br")]
