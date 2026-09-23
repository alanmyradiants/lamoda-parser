# lamoda-parser

Внешняя аналитика Lamoda в стиле MPStats: продажи, выручка, остатки, цены и
позиции по товарам, брендам и продавцам.

> Lamoda отвечает «Проверка безопасности. Запрос отклонен» на запросы не из РФ.
> Всё, что ходит на сайт, запускаем на Timeweb VPS или через российский прокси.

## Что есть сейчас

| Модуль | Этап плана | Что делает |
|---|---|---|
| `lamoda_parser/recon/` | 1. Разведка | Открывает карточку и каталог в Chromium, ловит все JSON внутреннего API и состояние страницы, ищет поля остатков/размеров/продавца, пишет `report.md` |
| `db/migrations/001_lamoda_market.sql` | 2. Парсер | Схема `lamoda_market`: `products`, `snapshots`, `positions`, `daily_sales`, `runs` |
| `lamoda_parser/sales.py` | 2–3 | Продажи по разнице остатков между снимками, выручка, дни в наличии, упущенная выручка |
| `lamoda_parser/calibration.py` | 3. Калибровка | Сравнение оценки с реальными продажами JOTO; цель — ошибка ≤ 25% |

## Этап 1: разведка на VPS

```bash
# Ubuntu на Timeweb
sudo apt update && sudo apt install -y python3-venv git
git clone https://github.com/alanmyradiants/lamoda-parser.git && cd lamoda-parser
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
playwright install --with-deps chromium

# 2–3 карточки (лучше с разным наличием размеров) + 1 категория
python -m lamoda_parser.recon.run \
  --product 'https://www.lamoda.ru/p/<артикул>/<...>/' \
  --product 'https://www.lamoda.ru/p/<артикул2>/<...>/' \
  --catalog 'https://www.lamoda.ru/c/477/clothes-muzhskaya-odezhda/' \
  --out recon_out

tar czf recon_out.tgz recon_out   # этот архив прислать для разбора
```

Через прокси: `export LAMODA_PROXY=http://user:pass@host:port` перед запуском.

В `recon_out/`:
- `report.md` — вывод: ✅ точные остатки / ⚠️ только «есть-нет» / ❓ не найдено; статус HTTP и браузера, блокировки, список JSON-запросов;
- `api/*.json` — все пойманные ответы внутреннего API;
- `*_state_*.json` — состояние страницы (`__NUXT__` и т.п.);
- `*.png`, `*_http.html`, `*_browser.html` — что реально показал сайт.

Если в отчёте «Браузер: блок True» — упёрлись в антибот, нужен прокси или `--headful`.

## Калибровка (этап 3)

```bash
python -m lamoda_parser.calibration estimated.csv actual.csv
```

Оба файла — CSV с колонками `sku,units` за один период. `actual.csv` —
реальные продажи JOTO из lamoda-bot (API Lamoda для селлеров).

## Тесты

```bash
pytest
# если версия Playwright не совпадает с установленным Chromium:
LAMODA_CHROMIUM=/path/to/chrome pytest
```
