# electoral_keyword_extractor

Extrator diário de keywords eleitorais para busca social.
**Notícias de hoje → keywords de hoje → buscas sociais de hoje.**

Coleta notícias de RSS de portais e órgãos (G1, Folha, Estadão, O Globo, UOL, TSE, Câmara, Senado…), mantém só as publicadas **hoje** (fuso `America/Sao_Paulo`) e as eleitorais, deduplica, manda título e resumo a um LLM local (Bonsai-27B 1-bit, servido pelo `~/bonsai_agent`) e devolve até 100 keywords priorizadas, com consultas prontas para Reddit e fóruns.

## Uso

```bash
cd ~/electoral_keyword_extractor
# uma vez: uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt

# o LLM roda no bonsai_agent (se não estiver no ar, as notícias ficam pendentes p/ próxima execução)
(cd ~/bonsai_agent && nohup setsid ./start_server.sh > server.log 2>&1 &)

.venv/bin/python -m keyword_extractor              # coleta incremental do dia
.venv/bin/python -m keyword_extractor --recompute  # reaplica filtro + LLM a todas as notícias do dia
.venv/bin/python -m pytest                         # testes (sem rede, sem GPU)
```

Agendamento (instalado no crontab: todo dia às 18h, fuso do sistema = America/Sao_Paulo):
```
0 18 * * * /home/guilhermelima/electoral_keyword_extractor/run_daily.sh
```
O `run_daily.sh` liga o llama-server do `~/bonsai_agent` se estiver fora do ar, coleta, envia o relatório para `KEYWORD_EMAIL_PARA` e desliga o servidor se foi ele quem ligou. Usa `flock` contra execuções simultâneas e grava o log em `data/cron.log`.
Teste sem enviar: `DRY_RUN=1 ./run_daily.sh`.

Instalação em outra máquina:
```bash
git clone <repo> && cd electoral_keyword_extractor
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt   # ou python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env   # preencher
.venv/bin/python -m pytest
```
O LLM (Bonsai-27B + llama-server) não faz parte deste repositório: ver `~/bonsai_agent`, ou apontar `CONFIG["llm_base_url"]` para outro servidor OpenAI-compatível.

## Relatório por e-mail

```bash
.venv/bin/python -m keyword_extractor.report                  # prévia em data/reports/<dia>.eml/.html (não envia)
.venv/bin/python -m keyword_extractor.report --send           # envia para KEYWORD_EMAIL_PARA
.venv/bin/python -m keyword_extractor.report --send --to x@y  # teste para outro destinatário
```

- **Corpo:** top 10 high, top 5 medium e top 5 low, por nº de notícias do dia; mais a estatística descritiva, a regra de corte e a justificativa do LLM. Se o dia tiver menos de 10 high, mostra as que existem, sem completar com termos de outra faixa.
- **Anexos:** `keywords_<dia>.json` (completo) e `keywords_<dia>.csv` (todas as keywords; abre no Excel).
- **Credenciais:** ficam em `.env` (chmod 600, no `.gitignore`), com as chaves `KEYWORD_EMAIL_PARA`, `KEYWORD_EMAIL_DE`, `KEYWORD_EMAIL_NOME`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` e `SMTP_SEGURANCA` (starttls/ssl). Sem `SMTP_HOST`, o envio usa o `/usr/sbin/sendmail` local.

## Fluxo

```
RSS (21 feeds) ─► data de hoje ─► filtro eleitoral ─► dedup de notícias ─► título+resumo
   ─► LLM (só notícias ainda não analisadas; lotes de 6; JSON restrito por schema)
   ─► evidência literal no texto ─► fusão de duplicatas ─► contagens/prioridade ─► combinações ─► consultas
   ─► data/AAAA-MM-DD.json
```

O LLM **só propõe** termos atômicos. Todo o resto é determinístico, porque o modelo 1-bit alucina (ver `~/bonsai_agent/results`):

- **Evidência:** o termo precisa aparecer literalmente (ignorando caixa, acentos, palavras vazias, plural e "1º" = "primeiro") em uma notícia do dia. Senão é descartado; os descartes ficam em `data/state/<dia>.json → runs[-1].rejected_terms`.
- **Duplicatas fundidas:** quem é fundido vira `aliases` e entra nas variantes e consultas.
  - sigla ⇄ nome: TSE → Tribunal Superior Eleitoral;
  - "TV Globo"/"Rede Globo" → Globo; "pesquisa Datafolha" → Datafolha;
  - SP → São Paulo (tabela de UFs no config);
  - apelido → nome: "Luiz Inácio Lula da Silva" → Lula;
  - sobrenome único → nome completo: Haddad → Fernando Haddad. Com vários nomes completos para o mesmo sobrenome (Bolsonaro: Flávio, Jair, Michelle), fica separado;
  - "debate na Globo" = "debate da Globo";
  - primeiro nome sozinho ("Flávio") é descartado por ambiguidade.
- **Prioridade só pela ocorrência do dia** (`priority.py`):
  - o código calcula em quantas notícias de hoje cada keyword aparece e a distribuição desses valores (quartis, histograma);
  - o LLM recebe **só esses números** e define os cortes high/medium, justificando pelos saltos da distribuição;
  - o código aplica os cortes, de forma monotônica: nenhuma keyword mais citada fica abaixo de uma menos citada;
  - cortes inválidos ou LLM fora do ar → quebras naturais de Jenks, calculadas sobre a mesma distribuição;
  - não há limite nem proporção fixa; o registro fica em `priority_analysis` no JSON do dia.
- **Combinações** (entidade + evento/tema extraídos do dia; sem vocabulário fixo) exigem co-ocorrência em ≥2 notícias. As consultas de busca também usam só termos do dia que co-ocorrem com a keyword. Contextos presentes em mais de 15% das notícias do dia ("primeiro turno" na semana da eleição) são ignorados. Combinações ocupam no máximo 25% das keywords.

## Arquivos

| | |
|---|---|
| `keyword_extractor/config.py` | **tudo configurável**: fontes, timezone, período de campanha, limites, vocabulário, genéricos, aliases de lugar |
| `collector.py` | RSS/Atom/RDF, datas RFC 822 em português, headers/cookies por fonte, falha isolada |
| `filters.py` | janela do dia; pontuação eleitoral (strong/medium/weak + contexto) |
| `dedup.py` | dedup de notícias: URL canônica, id, título+data, Jaccard; funde portais |
| `extract.py` | prompt/schema; evidência, fusão de duplicatas, prioridade, combinações, consultas |
| `llm.py` | cliente do llama-server (OpenAI-compatível) |
| `priority.py` | estatística de ocorrência, cortes do LLM, fallback Jenks |
| `report.py` | e-mail (top 10/5/5) com anexos JSON e CSV |
| `storage.py` | `data/<dia>.json` (saída) e `data/state/<dia>.json` (estado incremental) |
| `pipeline.py` | orquestração e logs |
| `social.py` | interface para o próximo estágio |

## Configuração

- **Nova fonte:** `{"name": ..., "type": "rss", "url": ...}` em `SOURCES`. Campos opcionais:
  - `feed`: rótulo do feed; feeds do mesmo portal usam o mesmo `name`, para não contar fonte em dobro;
  - `electoral_context`: feed de política;
  - `institution`: órgão cujo nome pode virar keyword (TSE); nomes de veículos (G1, UOL) nunca viram;
  - `headers`: ex. `BROWSER_HEADERS` para TSE, STJ, Poder360 e CartaCapital, que recusam UA não-navegador;
  - `cookies_env`: nome de variável de ambiente com cookies.
- **Timezone:** `CONFIG["timezone"]`, com qualquer nome IANA.

## Consumir no próximo estágio

```python
from keyword_extractor.social import iter_search_queries, load_keywords, tool_definition
for query, kw in iter_search_queries(min_priority="medium"):   # só keywords de HOJE
    ...  # reddit.search(query, time_filter="day")
load_keywords(date(2026, 9, 30), allow_history=True)           # outro dia: só explicitamente

# como ferramenta do agente em ~/bonsai_agent:
from agent import BonsaiAgent, Tool
agent = BonsaiAgent(tools=[Tool(**tool_definition())])
```
