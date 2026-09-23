# lamoda-parser

Внешняя аналитика Lamoda в стиле MPStats: продажи, выручка, остатки, цены и
позиции по товарам, брендам и продавцам.

## Как устроен сбор

```
артикулы ──► GraphQL Lamoda ──► snapshots (цена, остаток по размеру) ──► daily_sales
   ▲              (карточки)                                               (Δ остатка × цена)
   │
   ├─ страницы каталога/поиска в Chromium (discovery) ──► positions
   ├─ файлы skus/*.txt (JOTO, конкуренты)
   └─ все артикулы из базы за N дней (--known)
```

**Карточки** — анонимный эндпоинт `POST https://www.lamoda.ru/goapi/v2/catalog/graphql/products/`.
Принимает список артикулов, отдаёт `price_amount`, `old_price_amount`, `discount`,
`is_available` и **`stock_remains` по каждому размеру**. Батчи по 20, «битый»
артикул автоматически отсекается делением батча.
Рейтинга и отзывов в схеме нет; продавца и категорию проверяем командой `probe --fields`.

**Защита Lamoda (проверено 23.09.2026).** Сайт закрыт антиботом Servicepipe:
IP дата-центров (в т.ч. российских, Timeweb) получают «Запрос отклонен»; домашний IP
через резидентный прокси получает страницу с JS-проверкой (`"is_captcha": false`).
Поэтому карточки берутся через настоящий Chromium (`--browser` или `LAMODA_BROWSER=1`):
браузер проходит проверку, получает cookie и вызывает GraphQL через `fetch()` изнутри
страницы; когда пропуск истекает — проходит проверку заново. Нужен российский
резидентный/мобильный прокси в `LAMODA_PROXY`.

**Список артикулов и позиции** — только со страниц каталога/поиска (у GraphQL нет
поиска). Эти страницы закрыты антиботом сильнее: нужен российский IP и браузер.

## Быстрый старт на VPS (Timeweb, Ubuntu)

```bash
git clone https://github.com/alanmyradiants/lamoda-parser.git && cd lamoda-parser
bash deploy/setup_vps.sh
. .venv/bin/activate

# 1. Проходит ли браузер защиту и видны ли остатки числом (прокси — в .env):
export $(grep -v '^#' .env | xargs) && python -m lamoda_parser probe --browser RTLAEY634001

# 2. Работает ли каталог:
python -m lamoda_parser discover 'https://www.lamoda.ru/c/477/clothes-muzhskaya-odezhda/' --pages 2

# 3. Первый полный снимок:
bash deploy/daily.sh && tail logs/*.log

# 4. Раз в сутки (06:00 по времени сервера):
(crontab -l 2>/dev/null; echo "0 6 * * * $(pwd)/deploy/daily.sh") | crontab -
```

Настройки — в `.env` (шаблон `deploy/env.example`): база, прокси, Telegram для алертов.
Что обходить — `skus/catalog_urls.list` (категории/поиск) и `skus/*.txt` (артикулы или ссылки).

Если каталог упирается в антибот: поставьте прокси РФ в `LAMODA_PROXY` или
`LAMODA_HEADFUL=1` (нужен `sudo apt install xvfb`). Даже без каталога сбор
карточек по `skus/*.txt` и `--known` продолжает работать.

## Команды

| Команда | Что делает |
|---|---|
| `probe SKU [--fields]` | Проверка GraphQL на одном товаре; `--fields` перебирает доп. поля схемы (продавец, категория, сезон…) |
| `init-db` | Создать таблицы |
| `discover URL…` | Артикулы и позиции со страниц каталога/поиска |
| `collect …` | Ежедневный снимок: discovery + карточки + пересчёт продаж + журнал `runs` |
| `sales [--days 30]` | Пересчитать продажи, показать топ по выручке |
| `export TABLE -o file.csv` | Выгрузка для Excel (`;`, UTF-8 BOM) |
| `parse-raw data/raw/ДАТА.jsonl.gz` | Перезалить день из сырых ответов (сохраняются всегда) |

## База

По умолчанию SQLite `data/lamoda.db`, ничего настраивать не нужно. Для Supabase
или Postgres на VPS задайте `DATABASE_URL=postgresql://...`: таблицы создаются в
отдельной схеме `lamoda_market` (`db/migrations/001_lamoda_market.sql`).

| Таблица | Содержимое |
|---|---|
| `products` | артикул, название, бренд, первое/последнее появление |
| `snapshots` | дата × артикул × размер: цена, старая цена, остаток, наличие |
| `positions` | дата × категория/запрос × артикул: место в выдаче |
| `daily_sales` | рассчитанные продажи, шт и ₽; флаг поставки |
| `runs` | журнал запусков для контроля качества |

Продажи = уменьшение остатка между соседними снимками × цена предыдущего дня.
Рост остатка — поставка: день помечается `restocked`, отрицательных продаж нет.

## Калибровка (этап 3)

```bash
python -m lamoda_parser export daily_sales -o est.csv   # затем свести в sku,units за период
python -m lamoda_parser.calibration estimated.csv actual.csv
```

`actual.csv` — реальные продажи JOTO из lamoda-bot. Цель — ошибка ≤ 25%.

## Разведка (если GraphQL перестанет отвечать)

`python -m lamoda_parser.recon.run --product URL --catalog URL` открывает страницы
в Chromium, сохраняет все JSON внутреннего API и ищет поля остатков — см. `recon_out/report.md`.

## Тесты

```bash
pip install -e '.[dev,pg]' && pytest
# если версия Playwright не совпадает с установленным Chromium:
LAMODA_CHROMIUM=/path/to/chrome pytest
```
