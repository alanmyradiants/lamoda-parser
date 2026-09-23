"""CLI парсера Lamoda.

    python -m lamoda_parser probe MP002XM1RMM3          # работает ли GraphQL с этого IP, какие поля есть
    python -m lamoda_parser init-db                     # создать таблицы
    python -m lamoda_parser discover URL [URL ...]      # артикулы и позиции со страниц каталога/поиска
    python -m lamoda_parser collect --skus-file skus.txt --catalog URL   # ежедневный снимок
    python -m lamoda_parser sales                       # пересчитать продажи по снимкам
    python -m lamoda_parser export snapshots -o snapshots.csv

База: DATABASE_URL (postgresql://... для Supabase/VPS) или по умолчанию SQLite data/lamoda.db.
Прокси РФ: LAMODA_PROXY=http://user:pass@host:port
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from .discovery import crawl, read_skus_file
from .graphql import LamodaError, LamodaGraphQL, parse_product
from .sales import estimate_daily_sales, summarize
from .storage import Storage

log = logging.getLogger("lamoda_parser")
DEFAULT_DB = "sqlite:///data/lamoda.db"


def _gql(args: argparse.Namespace, **kw) -> LamodaGraphQL:
    """Через браузер (JS-проверка Servicepipe) или простым HTTP."""
    if args.browser or os.environ.get("LAMODA_BROWSER") == "1":
        from .browser import BrowserGraphQL

        return BrowserGraphQL(headful=args.headful, **kw)
    return LamodaGraphQL(**kw)


def _storage() -> Storage:
    return Storage(os.environ.get("DATABASE_URL") or DEFAULT_DB)


def cmd_probe(args: argparse.Namespace) -> int:
    try:
        gql = _gql(args, retries=1, delay=args.delay)
    except LamodaError as e:
        print(f"✗ {e}")
        return 1
    with gql:
        cards, failed = gql.fetch([args.sku])
        if not cards:
            print(f"✗ карточка {args.sku} не получена (failed={failed}). Нет доступа или артикул неверный.")
            return 1
        c = cards[0]
        print(f"✓ GraphQL отвечает. {c.sku} · {c.brand} · {c.name}")
        print(f"  цена {c.price} (было {c.old_price}, скидка {c.discount}), в наличии {c.is_available}, остаток всего {c.stock_total}")
        for s in c.sizes:
            print(f"  размер {s.size:>8}: в наличии {s.is_available!s:5}  остаток {s.stock}")
        stocks = [s.stock for s in c.sizes if s.stock is not None]
        if any(v > 1 for v in stocks) or (c.stock_total or 0) > 1:
            print("  → остатки ЧИСЛОВЫЕ: продажи считаем по разнице остатков ✅")
        else:
            print("  → остатки 0/1 на этом товаре: проверьте товар с большим наличием")
        if args.fields:
            print("\nПроверка дополнительных полей схемы:")
            for f, v in gql.probe_fields(args.sku).items():
                print(f"  {f:28} {str(v)[:100]}")
    return 0


def cmd_init_db(args: argparse.Namespace) -> int:
    with _storage() as st:
        st.init()
        print(f"✓ таблицы созданы ({'Postgres, схема lamoda_market' if st.pg else st.url})")
    return 0


def _discover(st: Storage, urls: list[str], pages: int, headful: bool, day: date) -> tuple[list[str], int]:
    listings = asyncio.run(crawl(urls, max_pages=pages, headful=headful))
    skus: list[str] = []
    blocked = 0
    for lst in listings:
        pos = lst.positions()
        blocked += sum(p.blocked for p in lst.pages)
        st.touch_skus(day, [s for s, _ in pos])
        st.save_positions(day, lst.source, lst.query, pos)
        log.info("%s %s: %d артикулов", lst.source, lst.query, len(pos))
        skus += [s for s, _ in pos if s not in skus]
    return skus, blocked


def cmd_discover(args: argparse.Namespace) -> int:
    with _storage() as st:
        st.init()
        skus, blocked = _discover(st, args.urls, args.pages, args.headful, date.today())
    print(f"найдено артикулов: {len(skus)}, страниц с блоком: {blocked}")
    return 0 if skus else 1


def cmd_collect(args: argparse.Namespace) -> int:
    day = date.today()
    with _storage() as st:
        st.init()
        run_id = st.start_run(args.scope)
        skus: list[str] = []
        blocked = 0
        for f in args.skus_file:
            skus += [s for s in read_skus_file(Path(f)) if s not in skus]
        if args.catalog:
            found, blocked = _discover(st, args.catalog, args.pages, args.headful, day)
            skus += [s for s in found if s not in skus]
        if args.known:
            skus += [s for s in st.known_skus(args.known) if s not in skus]
        if args.limit:
            skus = skus[: args.limit]
        if not skus:
            st.finish_run(run_id, 0, 0, blocked, "нет артикулов")
            print("нет артикулов: укажите --skus-file, --catalog или --known")
            return 1

        log.info("снимаю %d карточек", len(skus))
        try:
            gql = _gql(args, batch_size=args.batch, delay=args.delay)
        except LamodaError as e:
            st.finish_run(run_id, 0, len(skus), blocked + 1, f"браузер: {e}")
            print(f"✗ {e}")
            return 2
        with gql:
            cards, failed = gql.fetch(skus)

        raw_dir = Path(args.raw_dir)
        raw_dir.mkdir(parents=True, exist_ok=True)
        with gzip.open(raw_dir / f"{day.isoformat()}.jsonl.gz", "at", encoding="utf-8") as f:
            for c in cards:
                f.write(json.dumps(c.raw, ensure_ascii=False) + "\n")

        rows = st.save_cards(day, cards)
        st.save_daily_sales(estimate_daily_sales(st.load_snapshots(since=day - timedelta(days=args.sales_days))))
        missing = len(skus) - len(cards) - len(failed)  # сняты с продажи / неверный артикул
        note = f"строк снимка {rows}; ошибок {len(failed)}; не найдено {missing}"
        st.finish_run(run_id, len(cards), len(failed), blocked, note)
    print(
        f"✓ {day}: карточек {len(cards)} из {len(skus)}, строк по размерам {rows}, "
        f"ошибок {len(failed)}, не найдено {missing}, блоков каталога {blocked}"
    )
    # код 2 — для алерта из cron: заметная доля запросов отвергнута или каталог заблокирован
    return 2 if len(failed) > 0.2 * len(skus) or blocked else 0


def cmd_sales(args: argparse.Namespace) -> int:
    with _storage() as st:
        snaps = st.load_snapshots(since=date.today() - timedelta(days=args.days) if args.days else None)
        sales = estimate_daily_sales(snaps)
        st.save_daily_sales(sales)
    top = sorted(summarize(sales), key=lambda s: s.revenue, reverse=True)[: args.top]
    print(f"{'артикул':16} {'шт':>6} {'выручка ₽':>12} {'дней':>5} {'в налич.':>8} {'упущ. ₽':>10}")
    for s in top:
        print(f"{s.sku:16} {s.units:>6} {s.revenue:>12,.0f} {s.days:>5} {s.days_in_stock:>8} {s.lost_revenue:>10,.0f}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    with _storage() as st:
        cols, rows = st.export(args.table)
    out = open(args.out, "w", newline="", encoding="utf-8-sig") if args.out else sys.stdout
    w = csv.writer(out, delimiter=";" if args.out else ",")
    w.writerow(cols)
    w.writerows(rows)
    if args.out:
        out.close()
        print(f"✓ {len(rows)} строк → {args.out}")
    return 0


def cmd_parse_raw(args: argparse.Namespace) -> int:
    """Перезалить снимок из сохранённого raw/<дата>.jsonl.gz (после исправления парсера)."""
    path = Path(args.file)
    day = date.fromisoformat(path.name[:10])
    with gzip.open(path, "rt", encoding="utf-8") as f:
        cards = [parse_product(json.loads(line)) for line in f if line.strip()]
    with _storage() as st:
        st.init()
        rows = st.save_cards(day, cards)
    print(f"✓ {day}: {len(cards)} карточек, {rows} строк")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="lamoda_parser", description="Парсер Lamoda")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="проверить GraphQL на одном артикуле")
    p.add_argument("sku")
    p.add_argument("--fields", action="store_true", help="перебрать дополнительные поля схемы")
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--browser", action="store_true", help="запросы из Chromium (обход JS-проверки); или LAMODA_BROWSER=1")
    p.add_argument("--headful", action="store_true", help="браузер с окном (на сервере — через xvfb-run)")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("init-db", help="создать таблицы")
    p.set_defaults(fn=cmd_init_db)

    p = sub.add_parser("discover", help="артикулы и позиции со страниц каталога/поиска")
    p.add_argument("urls", nargs="+")
    p.add_argument("--pages", type=int, default=5)
    p.add_argument("--headful", action="store_true")
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("collect", help="ежедневный снимок карточек")
    p.add_argument("--skus-file", action="append", default=[], help="файл с артикулами/ссылками")
    p.add_argument("--catalog", action="append", default=[], help="URL категории/поиска для discovery")
    p.add_argument("--pages", type=int, default=5, help="страниц на каждый URL каталога")
    p.add_argument("--known", type=int, default=0, metavar="ДНЕЙ", help="плюс все артикулы из базы, виденные за N дней")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--batch", type=int, default=20, help="артикулов в одном GraphQL-запросе")
    p.add_argument("--delay", type=float, default=1.5, help="пауза между запросами, с")
    p.add_argument("--scope", default="default", help="метка запуска (ниша)")
    p.add_argument("--raw-dir", default="data/raw")
    p.add_argument("--sales-days", type=int, default=3, help="за сколько дней пересчитать продажи")
    p.add_argument("--headful", action="store_true")
    p.add_argument("--browser", action="store_true", help="карточки через Chromium (обход JS-проверки); или LAMODA_BROWSER=1")
    p.set_defaults(fn=cmd_collect)

    p = sub.add_parser("sales", help="пересчитать продажи и показать топ")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--top", type=int, default=30)
    p.set_defaults(fn=cmd_sales)

    p = sub.add_parser("export", help="выгрузить таблицу в CSV (для Excel)")
    p.add_argument("table", choices=["products", "snapshots", "positions", "daily_sales", "runs"])
    p.add_argument("-o", "--out")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("parse-raw", help="перезалить снимок из data/raw/<дата>.jsonl.gz")
    p.add_argument("file")
    p.set_defaults(fn=cmd_parse_raw)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
