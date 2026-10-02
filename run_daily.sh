#!/usr/bin/env bash
# Execução diária (cron): garante o LLM no ar, coleta as keywords do dia e (exceto com COLLECT_ONLY=1) envia o relatório
# para KEYWORD_EMAIL_PARA (.env). Se este script ligou o llama-server, desliga ao final (libera a GPU).
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
BONSAI="$HOME/bonsai_agent"
HEALTH="http://127.0.0.1:8080/health"
LOG="$DIR/data/cron.log"
mkdir -p "$DIR/data"
exec >>"$LOG" 2>&1
exec 9>"$DIR/data/.run_daily.lock"
flock -n 9 || { echo "[$(date '+%F %T')] outra execução em andamento; saindo"; exit 0; }

echo "===== [$(date '+%F %T')] início"
started=0
if ! curl -sf -m 5 "$HEALTH" >/dev/null; then
  echo "[$(date '+%T')] llama-server fora do ar; iniciando"
  (cd "$BONSAI" && nohup setsid ./start_server.sh > "$BONSAI/server.log" 2>&1 &)
  started=1
  for _ in $(seq 1 150); do curl -sf -m 5 "$HEALTH" >/dev/null && break; sleep 2; done
  curl -sf -m 5 "$HEALTH" >/dev/null && echo "[$(date '+%T')] llama-server pronto" \
    || echo "[$(date '+%T')] llama-server não respondeu; seguindo (prioridade cai no Jenks, notícias ficam pendentes)"
fi

# COLLECT_ONLY=1 ./run_daily.sh → só coleta (sem e-mail)
# DRY_RUN=1 ./run_daily.sh      → coleta e gera a prévia do e-mail, sem enviar
if [ "${COLLECT_ONLY:-0}" = 1 ]; then
  cd "$DIR" && .venv/bin/python -m keyword_extractor --show 0
elif [ "${DRY_RUN:-0}" = 1 ]; then
  cd "$DIR" && .venv/bin/python -m keyword_extractor --show 0 && .venv/bin/python -m keyword_extractor.report
else
  cd "$DIR" && .venv/bin/python -m keyword_extractor --email --show 0
fi
status=$?

if [ "$started" = 1 ] && [ -f "$BONSAI/server.pid" ]; then
  kill "$(cat "$BONSAI/server.pid")" 2>/dev/null && echo "[$(date '+%T')] llama-server desligado"
fi
echo "===== [$(date '+%F %T')] fim (status $status)"
exit $status
