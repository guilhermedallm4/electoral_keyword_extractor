"""Relatório diário por e-mail: top 10 high, top 5 medium, top 5 low + anexos (JSON completo e CSV).

Uso:
  python -m keyword_extractor.report            # prévia: grava data/reports/<dia>.eml e .html, não envia
  python -m keyword_extractor.report --send     # envia para KEYWORD_EMAIL_PARA (.env)
  python -m keyword_extractor.report --send --to fulano@x  # sobrescreve destinatários
Credenciais em .env (ver README); nunca no código.
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import logging
import os
import smtplib
import ssl
import subprocess
import sys
from datetime import date, datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from zoneinfo import ZoneInfo

from . import storage
from .config import CONFIG, ROOT

log = logging.getLogger("keyword_extractor")

TOP = {"high": 10, "medium": 5, "low": 5}
CATEGORY_PT = {"person": "pessoa", "organization": "organização", "event": "evento", "topic": "tema",
               "location": "local", "specific_term": "termo específico", "combination": "combinação"}


def load_env(path: Path = ROOT / ".env") -> dict:
    """Lê KEY=VALUE de .env (sem dependência externa); variáveis de ambiente têm precedência."""
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith(("KEYWORD_EMAIL_", "SMTP_"))})
    return env


def top_by_priority(out: dict) -> dict[str, list[dict]]:
    """Top N de cada prioridade, ordenado por ocorrência (notícias, depois fontes)."""
    res = {}
    for p, n in TOP.items():
        ks = [k for k in out["keywords"] if k["priority"] == p]
        ks.sort(key=lambda k: (-k["article_count"], -k["source_count"], k["keyword"]))
        res[p] = ks[:n]
    return res


def _pct(k: dict, n_art: int) -> str:
    return f"{100 * k['article_count'] / max(n_art, 1):.1f}%"


def to_csv(out: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["posicao", "keyword", "prioridade", "categoria", "noticias", "pct_noticias_do_dia",
                "fontes", "aliases", "search_queries", "evidence_urls"])
    n_art = out["electoral_articles"]
    for i, k in enumerate(out["keywords"], 1):
        w.writerow([i, k["keyword"], k["priority"], CATEGORY_PT.get(k["category"], k["category"]),
                    k["article_count"], _pct(k, n_art), k["source_count"], " | ".join(k.get("aliases", [])),
                    " | ".join(k["search_queries"]), " | ".join(k.get("evidence_urls", []))])
    return ("﻿" + buf.getvalue()).encode("utf-8")   # BOM: Excel abre com acentos certos


def _section_text(title, ks, n_art, n_total):
    head = f"{title} — {len(ks)} de {n_total} no dia"
    if not ks:
        return f"{head}\n  (nenhuma)\n"
    rows = [f"  {i:>2}. {k['keyword']} — {k['article_count']} notícias ({_pct(k, n_art)}), "
            f"{k['source_count']} fontes [{CATEGORY_PT.get(k['category'], k['category'])}]"
            for i, k in enumerate(ks, 1)]
    return head + "\n" + "\n".join(rows) + "\n"


def build_message(out: dict, env: dict, to: list[str] | None = None) -> EmailMessage:
    day = out["collection_date"]
    n_art = out["electoral_articles"]
    tops = top_by_priority(out)
    pa = out.get("priority_analysis", {})
    st, th = pa.get("statistics", {}), pa.get("thresholds", {})
    counts = pa.get("counts", {})
    method = {"llm": "cortes definidos pelo LLM a partir da distribuição",
              "jenks": "quebras naturais (Jenks) da distribuição — o LLM não respondeu com cortes válidos"
              }.get(pa.get("method"), pa.get("method", "?"))
    failed = [s for s, v in out["sources"].items() if v != "ok"]

    stats_line = (f"{n_art} notícias eleitorais de hoje ({out['articles_collected']} coletadas, "
                  f"{out['sources_processed']}/{len(out['sources'])} fontes ok). "
                  f"{len(out['keywords'])} keywords; ocorrência por keyword (nº de notícias): "
                  f"mín {st.get('min')}, mediana {st.get('median')}, média {st.get('mean')}, máx {st.get('max')}.")
    rule = (f"high: ≥ {th.get('high_min')} notícias · medium: {th.get('medium_min')}–{th.get('high_min', 1) - 1} "
            f"· low: < {th.get('medium_min')}  ({method})")

    text = [f"Keywords eleitorais do dia {day} ({out['timezone']})", "", stats_line, "",
            "Classificação por ocorrência nas notícias de hoje:", "  " + rule]
    if pa.get("method") == "llm":
        text += [f"  Justificativa do LLM: {pa.get('justification', '')}"]
    text += [""]
    for p, title in (("high", "TOP HIGH"), ("medium", "TOP MEDIUM"), ("low", "TOP LOW")):
        text.append(_section_text(title, tops[p], n_art, counts.get(p, 0)))
    if failed:
        text.append("Fontes com falha hoje: " + ", ".join(failed))
    text += ["", "Anexos: JSON completo (com consultas de busca e URLs de evidência) e CSV com todas as keywords.",
             "Todas as keywords e contagens vêm exclusivamente das notícias publicadas hoje."]
    text_body = "\n".join(text)

    def table(ks):
        if not ks:
            return "<p><i>nenhuma</i></p>"
        rows = "".join(
            f"<tr><td>{i}</td><td><b>{html.escape(k['keyword'])}</b></td>"
            f"<td>{CATEGORY_PT.get(k['category'], k['category'])}</td>"
            f"<td align='right'>{k['article_count']}</td><td align='right'>{_pct(k, n_art)}</td>"
            f"<td align='right'>{k['source_count']}</td>"
            f"<td><code>{html.escape(k['search_queries'][0])}</code></td></tr>"
            for i, k in enumerate(ks, 1))
        return ("<table cellpadding='4' cellspacing='0' border='1' style='border-collapse:collapse;"
                "font-family:Arial,sans-serif;font-size:13px'><tr style='background:#eee'><th>#</th>"
                "<th>keyword</th><th>categoria</th><th>notícias</th><th>% do dia</th><th>fontes</th>"
                f"<th>consulta</th></tr>{rows}</table>")

    sections = "".join(
        f"<h3>{title} <small style='color:#666'>({len(tops[p])} de {counts.get(p, 0)} no dia)</small></h3>{table(tops[p])}"
        for p, title in (("high", "Top high"), ("medium", "Top medium"), ("low", "Top low")))
    just = (f"<p><b>Justificativa do LLM:</b> {html.escape(pa.get('justification', ''))}</p>"
            if pa.get("method") == "llm" else "")
    html_body = f"""<html><body style="font-family:Arial,sans-serif;font-size:14px">
<h2>Keywords eleitorais — {day}</h2>
<p>{html.escape(stats_line)}</p>
<p><b>Classificação por ocorrência nas notícias de hoje:</b> {html.escape(rule)}</p>{just}
{sections}
{"<p><b>Fontes com falha hoje:</b> " + html.escape(", ".join(failed)) + "</p>" if failed else ""}
<p style="color:#666">Anexos: JSON completo (consultas de busca e URLs de evidência) e CSV com todas as keywords.<br>
Todas as keywords e contagens vêm exclusivamente das notícias publicadas hoje.</p>
</body></html>"""

    msg = EmailMessage()
    msg["Subject"] = (f"[Keywords eleitorais] {day} — {counts.get('high', 0)} high, "
                      f"{counts.get('medium', 0)} medium, {counts.get('low', 0)} low")
    msg["From"] = formataddr((env.get("KEYWORD_EMAIL_NOME", "Agente Extrator de Keywords"), env["KEYWORD_EMAIL_DE"]))
    recipients = to or [a.strip() for a in env["KEYWORD_EMAIL_PARA"].split(",") if a.strip()]
    msg["To"] = ", ".join(recipients)
    msg["Message-ID"] = make_msgid(domain=env["KEYWORD_EMAIL_DE"].split("@")[-1])
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    msg.add_attachment(json.dumps(out, ensure_ascii=False, indent=1).encode("utf-8"), maintype="application",
                       subtype="json", filename=f"keywords_{day}.json")
    msg.add_attachment(to_csv(out), maintype="text", subtype="csv", filename=f"keywords_{day}.csv")
    return msg


def send(msg: EmailMessage, env: dict) -> None:
    host = env.get("SMTP_HOST", "")
    if not host:  # sem SMTP configurado: sendmail local
        subprocess.run(["/usr/sbin/sendmail", "-t", "-oi"], input=msg.as_bytes(), check=True)
        return
    port = int(env.get("SMTP_PORT", 587))
    sec = env.get("SMTP_SEGURANCA", "starttls").lower()
    ctx = ssl.create_default_context()
    if sec == "ssl":
        server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=60)
    else:
        server = smtplib.SMTP(host, port, timeout=60)
    with server:
        if sec == "starttls":
            server.starttls(context=ctx)
        if env.get("SMTP_USER"):
            server.login(env["SMTP_USER"], env["SMTP_PASS"])
        server.send_message(msg)


def run_report(day: date | None = None, do_send: bool = False, to: list[str] | None = None,
               cfg: dict = CONFIG) -> Path:
    day = day or datetime.now(ZoneInfo(cfg["timezone"])).date()
    out = storage.load_output(cfg["data_dir"], day)
    if out is None:
        raise FileNotFoundError(f"sem keywords para {day}; rode `python -m keyword_extractor` antes")
    env = load_env()
    msg = build_message(out, env, to)
    rep = Path(cfg["data_dir"]) / "reports"
    rep.mkdir(parents=True, exist_ok=True)
    eml = rep / f"{day.isoformat()}.eml"
    eml.write_bytes(msg.as_bytes())
    (rep / f"{day.isoformat()}.html").write_text(msg.get_body(("html",)).get_content())
    if do_send:
        send(msg, env)
        log.info("E-mail enviado para %s", msg["To"])
    else:
        log.info("Prévia gravada em %s (não enviado; use --send)", eml)
    return eml


def main():
    p = argparse.ArgumentParser(description="Relatório diário de keywords por e-mail")
    p.add_argument("--date", type=date.fromisoformat, default=None)
    p.add_argument("--send", action="store_true", help="envia de fato (padrão: só prévia)")
    p.add_argument("--to", nargs="+", help="destinatários (padrão: KEYWORD_EMAIL_PARA do .env)")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stderr)
    run_report(a.date, a.send, a.to)


if __name__ == "__main__":
    main()
