#!/usr/bin/env bash
# Установка парсера на Timeweb VPS (Ubuntu 22.04/24.04). Запуск: bash deploy/setup_vps.sh
set -euo pipefail
cd "$(dirname "$0")/.."

sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip git
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -e '.[dev,pg]'
python -m playwright install --with-deps chromium

[ -f .env ] || cp deploy/env.example .env
mkdir -p data logs
echo
echo "Готово. Дальше:"
echo "  1) проверить доступ:   . .venv/bin/activate && python -m lamoda_parser probe MP002XM1RMM3 --fields"
echo "  2) заполнить .env и skus/*.txt"
echo "  3) пробный сбор:       bash deploy/daily.sh"
echo "  4) cron раз в сутки:   (crontab -l 2>/dev/null; echo '0 6 * * * $(pwd)/deploy/daily.sh') | crontab -"
