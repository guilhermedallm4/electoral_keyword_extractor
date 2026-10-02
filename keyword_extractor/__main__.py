"""CLI: python -m keyword_extractor [--recompute] [--show N] [--email]"""
import argparse
import logging
import sys

from .pipeline import run


def main():
    p = argparse.ArgumentParser(description="Coleta diária de keywords eleitorais")
    p.add_argument("--recompute", action="store_true",
                   help="reaplica o filtro e reanalisa com o LLM todas as notícias do dia (padrão: só as novas)")
    p.add_argument("--show", type=int, default=15, help="quantas keywords imprimir")
    p.add_argument("--email", action="store_true", help="envia o relatório do dia por e-mail ao final (.env)")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stderr)
    out = run(recompute=a.recompute)
    if a.email:
        from .report import run_report
        run_report(do_send=True)
    for k in out["keywords"][: a.show]:
        print(f"{k['priority']:<6} {k['category']:<13} {k['article_count']:>2}n/{k['source_count']}f  {k['keyword']}")


if __name__ == "__main__":
    main()
