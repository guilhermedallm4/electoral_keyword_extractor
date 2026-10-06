"""Configuração do módulo de keywords eleitorais diárias.

Tudo que é ajustável (fontes, limites, timezone, vocabulário) fica aqui.
Para adicionar/remover uma fonte basta editar SOURCES; nenhum outro arquivo precisa mudar.

Campos de uma fonte:
  name               portal (usado para contar "fontes diferentes"; feeds do mesmo portal compartilham o nome)
  feed               rótulo opcional do feed dentro do portal
  type               "rss" (RSS 2.0 / Atom / RDF) — outros tipos podem ser registrados em collector.FETCHERS
  url                endpoint público e documentado
  electoral_context  True se o feed já é de política/eleições (reforça o filtro eleitoral)
  headers            opcional: cabeçalhos HTTP extras só para esta fonte
  cookies_env        opcional: nome da variável de ambiente com cookies "k=v; k2=v2" desta fonte
  institution        True para órgãos (TSE, Câmara...): o nome continua valendo como keyword;
                     nomes de veículos de imprensa (UOL, G1...) nunca viram keyword
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Cabeçalhos de navegador, usados só em fontes que recusam clientes não-navegador.
# Perfil Firefox: o WAF do TSE (F5) aceita UA Firefox e recusa UA Chrome sem os sec-ch-ua
# que um Chrome real mandaria (testado em 01/10/2026). Cookie não foi necessário.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:156.0) Gecko/20100101 Firefox/156.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

SOURCES = [
    {"name": "UOL Notícias", "type": "rss", "url": "http://rss.uol.com.br/feed/noticias.xml"},
    {"name": "Agência Brasil", "feed": "últimas", "type": "rss",
     "url": "https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml"},
    {"name": "Agência Brasil", "feed": "política", "type": "rss",
     "url": "https://agenciabrasil.ebc.com.br/rss/politica/feed.xml", "electoral_context": True},
    # WAF devolve página HTML de bloqueio para UA não-navegador
    {"name": "STJ", "institution": True, "type": "rss", "url": "https://res.stj.jus.br/hrestp-c-portalp/RSS.xml",
     "headers": BROWSER_HEADERS},
    {"name": "Câmara dos Deputados", "institution": True, "feed": "últimas", "type": "rss",
     "url": "https://www.camara.leg.br/noticias/rss/ultimas-noticias"},
    {"name": "Câmara dos Deputados", "institution": True, "feed": "política", "type": "rss",
     "url": "https://www.camara.leg.br/noticias/rss/dinamico/POLITICA", "electoral_context": True},
    {"name": "Senado Federal", "institution": True, "type": "rss", "url": "https://www12.senado.leg.br/noticias/rss"},
    # WAF recusa UA não-navegador (403). Se voltar a bloquear, dá para acrescentar
    # "cookies_env": "TSE_COOKIES" com o cookie TS... de uma sessão do navegador.
    {"name": "TSE", "institution": True, "type": "rss", "url": "https://www.tse.jus.br/rss", "electoral_context": True,
     "headers": BROWSER_HEADERS},
    {"name": "CNJ", "institution": True, "type": "rss", "url": "https://www.cnj.jus.br/feed/?post_type=post"},
    {"name": "Gov.br", "institution": True, "feed": "Planalto", "type": "rss",
     "url": "https://www.gov.br/planalto/pt-br/acompanhe-o-planalto/noticias/RSS"},

    # portais jornalísticos (testados em 01/10/2026)
    {"name": "G1", "feed": "eleições", "type": "rss", "url": "https://g1.globo.com/rss/g1/politica/eleicoes/",
     "electoral_context": True},
    {"name": "G1", "feed": "política", "type": "rss", "url": "https://g1.globo.com/rss/g1/politica/",
     "electoral_context": True},
    {"name": "O Globo", "feed": "política", "type": "rss", "url": "https://oglobo.globo.com/rss/oglobo/politica/",
     "electoral_context": True},
    {"name": "Folha de S.Paulo", "feed": "poder", "type": "rss",
     "url": "https://feeds.folha.uol.com.br/poder/rss091.xml", "electoral_context": True},
    {"name": "Estadão", "feed": "política", "type": "rss",
     "url": "https://www.estadao.com.br/arc/outboundfeeds/feeds/rss/sections/politica/", "electoral_context": True},
    {"name": "Gazeta do Povo", "feed": "eleições", "type": "rss",
     "url": "https://www.gazetadopovo.com.br/feed/rss/eleicoes.xml", "electoral_context": True},
    {"name": "Poder360", "type": "rss", "url": "https://www.poder360.com.br/feed/", "headers": BROWSER_HEADERS},
    {"name": "CartaCapital", "feed": "política", "type": "rss",
     "url": "https://www.cartacapital.com.br/politica/feed/", "headers": BROWSER_HEADERS, "electoral_context": True},
    {"name": "Metrópoles", "type": "rss", "url": "https://www.metropoles.com/feed"},
    {"name": "BBC News Brasil", "type": "rss", "url": "https://feeds.bbci.co.uk/portuguese/rss.xml"},
    {"name": "Exame", "type": "rss", "url": "https://exame.com/feed/"},
]

# Vocabulário do filtro eleitoral (comparação sem acento e sem caixa; ver filters.electoral_score).
# strong: sozinho já caracteriza notícia eleitoral.
# medium: forte indício, mas depende de contexto (período de campanha, feed de política, tags).
# weak:   comum também em notícias legislativas/administrativas; nunca basta sozinho.
ELECTORAL_TERMS = {
    "strong": [
        "eleição", "eleições", "eleitoral", "eleitorais", "eleitor", "eleitores", "eleitorado",
        "TSE", "TRE", "urna", "urnas", "candidatura", "candidaturas", "registro de candidatura",
        "propaganda eleitoral", "pesquisa eleitoral", "primeiro turno", "segundo turno",
        "boca de urna", "Justiça Eleitoral", "horário eleitoral", "coligação", "coligações",
        "título de eleitor", "zona eleitoral", "seção eleitoral", "apuração", "reeleição",
    ],
    "medium": [
        "candidato", "candidata", "candidatos", "candidatas", "campanha", "comício", "comícios",
        "debate eleitoral", "debate presidencial", "debate entre candidatos", "presidenciáveis",
        "intenção de voto", "intenções de voto", "votar", "presidenciável",
        "chapa", "vice na chapa", "plano de governo", "programa de governo",
    ],
    "weak": [
        "presidente", "governador", "governadora", "prefeito", "prefeita", "senador", "senadora",
        "deputado", "deputada", "vereador", "vereadora", "partido", "partidos", "voto", "votos",
        "votação", "pesquisa", "debate", "debates", "Datafolha", "Quaest", "Ipec", "AtlasIntel",
    ],
}

# Termos que sozinhos (ou combinados só entre si) são genéricos demais para busca social.
GENERIC_TERMS = [
    "eleição", "eleições", "eleitoral", "eleitorais", "política", "político", "políticos",
    "candidato", "candidata", "candidatos", "campanha", "voto", "votos", "votação", "partido",
    "partidos", "governo", "brasil", "2026", "notícia", "notícias", "eleitor", "eleitores",
    "rádio", "tv", "televisão", "television", "internet", "vídeo", "hoje", "domingo",
    "debate", "debates", "entrevista", "pesquisa", "propaganda", "regra", "regras", "república",
    "presidente", "deputado", "deputados", "senador", "senadores", "governador", "prefeito",
    "vereador", "ministro", "ministra", "federal", "federais", "estadual", "municipal", "nacional",
    "governadores", "prefeitos", "vereadores", "ministros", "presidentes", "candidaturas",
    "estado", "estados", "número", "números", "disputa", "presidência", "missão", "país",
    "cidade", "cidades", "município", "municípios", "região", "resultado", "resultados",
    "levantamento", "levantamentos", "aliado", "aliados", "eleito", "eleitos", "podcast",
    "entrevistado", "programa", "canal",
]

# Siglas/apelidos de lugar -> nome completo (fundidos quando os dois aparecem no dia)
LOCATION_ALIASES = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia", "CE": "Ceará",
    "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás", "MA": "Maranhão",
    "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais", "PA": "Pará",
    "PB": "Paraíba", "PR": "Paraná", "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro",
    "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima",
    "SC": "Santa Catarina", "SP": "São Paulo", "SE": "Sergipe", "TO": "Tocantins",
    "Rio": "Rio de Janeiro", "BH": "Belo Horizonte", "Sampa": "São Paulo",
}

# Cargos removidos do início de nomes de pessoas ("ministra Estela Aranha" -> "Estela Aranha")
PERSON_TITLES = [
    "ministro", "ministra", "presidente", "senador", "senadora", "deputado", "deputada",
    "governador", "governadora", "prefeito", "prefeita", "vereador", "vereadora", "candidato",
    "candidata", "juiz", "juíza", "desembargador", "desembargadora", "ex-presidente", "vice",
]

CONFIG = {
    "timezone": "America/Sao_Paulo",
    "only_today": True,
    # Período oficial de campanha: dentro dele, termos "medium" (ex.: "candidato") contam como contexto
    # eleitoral mesmo sem a palavra "eleição".
    "election_period": {"start": "2026-08-16", "end": "2026-10-25"},

    # coleta
    "sources": SOURCES,
    "user_agent": "electoral-keyword-extractor/0.1 (pesquisa acadêmica UFPel; coleta de RSS público)",
    "request_timeout": 20,
    "max_articles_per_source": 50,
    "max_total_articles": 300,      # teto de notícias eleitorais guardadas por dia
    "max_summary_chars": 300,       # resumo truncado antes de ir ao LLM
    "accept_undated": False,        # sem data não dá para provar que é de hoje → descarta

    # filtro / dedup
    "electoral_threshold": 3,
    "electoral_terms": ELECTORAL_TERMS,
    "title_similarity": 0.85,       # Jaccard de tokens do título para considerar duplicata

    # LLM
    "llm_base_url": "http://127.0.0.1:8080/v1",   # llama-server do ~/bonsai_agent
    "llm_wait_ready": 60,                          # segundos esperando o /health antes de desistir
    "llm_batch_size": 6,           # notícias por chamada
    "max_articles_for_llm_per_run": 150,
    "max_candidates_per_batch": 30,
    "llm_temperature": 0.3,

    # keywords
    "max_keywords": 100,
    "max_keywords_per_article": 4,  # teto adaptativo: poucos artigos → poucas keywords
    "generic_terms": GENERIC_TERMS,
    "person_titles": PERSON_TITLES,
    "location_aliases": LOCATION_ALIASES,
    # sobrenome sozinho vira alias do nome completo quando só existe UM nome completo com aquele
    # sobrenome no dia e >= 50% das notícias dele trazem o nome completo ("Haddad" -> "Fernando Haddad")
    "surname_merge_coverage": 0.5,
    # termo contido em outro vira alias dele se >= 60% das suas notícias trazem o termo longo
    "contained_merge_coverage": 0.6,
    # termos nunca aceitos como keyword; os nomes dos portais configurados são somados a esta lista
    "exclude_terms": ["Agência Senado", "Agência Câmara", "Rádio Senado", "TV Senado", "TV Câmara",
                      "Rádio Câmara", "Agência Gov"],
    "min_cooccurrence_for_combo": 2,
    "combo_context_max_df": 0.15,   # contexto presente em >15% das notícias do dia não discrimina nada
    "combo_df_min_articles": 20,    # ...mas só faz sentido medir isso com volume
    "max_combo_share": 0.25,        # combinações ocupam no máximo 25% das keywords
    "combo_context_categories": ["event"],   # combinação só entidade + evento do dia
    # ambiguidade medida nas notícias do dia (consultas dessas keywords exigem contexto)
    "ambiguous_max_acronym_len": 3,
    "ambiguous_lowercase_share": 0.5,
    "max_keyword_tokens": 4,        # mais que isso costuma ser manchete, não termo de busca
    "max_search_variants_per_keyword": 3,
    "max_search_queries_per_keyword": 5,
    "max_evidence_urls": 2,

    # armazenamento
    "data_dir": str(ROOT / "data"),
}
