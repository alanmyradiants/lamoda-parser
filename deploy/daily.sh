#!/usr/bin/env bash
# Ежедневный сбор. Код выхода != 0 -> сообщение в Telegram (если заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID).
set -uo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a
. .venv/bin/activate
mkdir -p logs
LOG="logs/$(date +%F).log"

ARGS=(collect --scope "${LAMODA_SCOPE:-default}" --known "${LAMODA_KNOWN_DAYS:-14}" --pages "${LAMODA_PAGES:-10}")
for f in skus/*.txt; do [ -f "$f" ] && ARGS+=(--skus-file "$f"); done
if [ -f skus/catalog_urls.list ]; then
  while read -r u; do [[ -n "$u" && "$u" != \#* ]] && ARGS+=(--catalog "$u"); done < skus/catalog_urls.list
fi

# xvfb-run даёт браузеру «экран», headful реже ловит антибот
if [ "${LAMODA_HEADFUL:-0}" = "1" ] && command -v xvfb-run >/dev/null; then
  xvfb-run -a python -m lamoda_parser "${ARGS[@]}" --headful >>"$LOG" 2>&1
else
  python -m lamoda_parser "${ARGS[@]}" >>"$LOG" 2>&1
fi
RC=$?

if [ $RC -ne 0 ] && [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=⚠️ Lamoda-парсер: код $RC. $(tail -n 3 "$LOG")" >/dev/null
fi
exit $RC
