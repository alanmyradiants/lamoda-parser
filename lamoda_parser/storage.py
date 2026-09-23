"""Хранилище снимков: Postgres/Supabase (DATABASE_URL=postgresql://...) или SQLite-файл.

В Postgres таблицы живут в схеме lamoda_market (db/migrations), в SQLite —
те же таблицы без схемы. SQL пишем один раз с плейсхолдерами «?».
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .graphql import ProductCard
from .sales import DailySale, Snapshot

MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"

SQLITE_DDL = """
create table if not exists products (
    sku text primary key, name text, brand text, seller text, category_path text,
    season text, composition text, url text,
    first_seen date not null, last_seen date not null,
    updated_at timestamp not null default current_timestamp
);
create table if not exists snapshots (
    snap_date date not null, sku text not null, size text not null default '',
    price real, old_price real, stock integer, in_stock boolean not null,
    rating real, reviews_count integer,
    collected_at timestamp not null default current_timestamp,
    primary key (snap_date, sku, size)
);
create table if not exists positions (
    snap_date date not null, source text not null, query text not null,
    sku text not null, position integer not null,
    primary key (snap_date, source, query, sku)
);
create table if not exists daily_sales (
    sale_date date not null, sku text not null, units integer not null, revenue real not null,
    in_stock boolean not null, restocked boolean not null default false, method text not null,
    computed_at timestamp not null default current_timestamp,
    primary key (sale_date, sku)
);
create table if not exists runs (
    id integer primary key autoincrement,
    started_at timestamp not null default current_timestamp, finished_at timestamp,
    scope text not null, products integer, errors integer, blocked integer, note text
);
"""


def _d(v: Any) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


class Storage:
    def __init__(self, url: str):
        self.url = url
        self.pg = url.startswith(("postgres://", "postgresql://"))
        if self.pg:
            import psycopg

            self.conn = psycopg.connect(url)
            self.prefix = "lamoda_market."
        else:
            path = url.removeprefix("sqlite:///").removeprefix("sqlite://")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path)
            self.prefix = ""

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _sql(self, sql: str) -> str:
        sql = sql.replace("{p}", self.prefix)
        return sql.replace("?", "%s") if self.pg else sql

    def _many(self, sql: str, rows: list[tuple]) -> None:
        if not rows:
            return
        cur = self.conn.cursor()
        cur.executemany(self._sql(sql), rows)
        self.conn.commit()

    def _one(self, sql: str, params: tuple = ()) -> list[tuple]:
        cur = self.conn.cursor()
        cur.execute(self._sql(sql), params)
        rows = cur.fetchall() if cur.description else []
        self.conn.commit()
        return rows

    def init(self) -> None:
        if self.pg:
            for f in sorted(MIGRATIONS.glob("*.sql")):
                self.conn.execute(f.read_text(encoding="utf-8"))
            self.conn.commit()
        else:
            self.conn.executescript(SQLITE_DDL)

    # --- запись ---------------------------------------------------------

    def save_cards(self, day: date, cards: Iterable[ProductCard]) -> int:
        cards = list(cards)
        self._many(
            """insert into {p}products as t (sku, name, brand, url, first_seen, last_seen)
               values (?, ?, ?, ?, ?, ?)
               on conflict (sku) do update set
                 name = coalesce(excluded.name, t.name),
                 brand = coalesce(excluded.brand, t.brand),
                 last_seen = excluded.last_seen,
                 updated_at = current_timestamp""",
            [(c.sku, c.name, c.brand, c.url, day, day) for c in cards],
        )
        rows = []
        for c in cards:
            if c.sizes:
                rows += [(day, c.sku, s.size, c.price, c.old_price, s.stock, s.is_available) for s in c.sizes]
            else:
                rows.append((day, c.sku, "", c.price, c.old_price, c.stock_total, c.is_available))
        self._many(
            """insert into {p}snapshots (snap_date, sku, size, price, old_price, stock, in_stock)
               values (?, ?, ?, ?, ?, ?, ?)
               on conflict (snap_date, sku, size) do update set
                 price = excluded.price, old_price = excluded.old_price,
                 stock = excluded.stock, in_stock = excluded.in_stock,
                 collected_at = current_timestamp""",
            rows,
        )
        return len(rows)

    def touch_skus(self, day: date, skus: Iterable[str]) -> None:
        """Регистрирует артикулы из выдачи, даже если карточку ещё не сняли."""
        self._many(
            """insert into {p}products as t (sku, url, first_seen, last_seen) values (?, ?, ?, ?)
               on conflict (sku) do update set last_seen = excluded.last_seen""",
            [(s, f"https://www.lamoda.ru/p/{s.lower()}/", day, day) for s in skus],
        )

    def save_positions(self, day: date, source: str, query: str, positions: list[tuple[str, int]]) -> None:
        self._many(
            """insert into {p}positions (snap_date, source, query, sku, position) values (?, ?, ?, ?, ?)
               on conflict (snap_date, source, query, sku) do update set position = excluded.position""",
            [(day, source, query, sku, pos) for sku, pos in positions],
        )

    def save_daily_sales(self, sales: list[DailySale]) -> None:
        self._many(
            """insert into {p}daily_sales (sale_date, sku, units, revenue, in_stock, restocked, method)
               values (?, ?, ?, ?, ?, ?, ?)
               on conflict (sale_date, sku) do update set
                 units = excluded.units, revenue = excluded.revenue, in_stock = excluded.in_stock,
                 restocked = excluded.restocked, method = excluded.method, computed_at = current_timestamp""",
            [(s.sale_date, s.sku, s.units, s.revenue, s.in_stock, s.restocked, s.method) for s in sales],
        )

    def start_run(self, scope: str) -> int:
        if self.pg:
            return self._one("insert into {p}runs (scope) values (?) returning id", (scope,))[0][0]
        cur = self.conn.execute("insert into runs (scope) values (?)", (scope,))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, products: int, errors: int, blocked: int, note: str = "") -> None:
        self._one(
            "update {p}runs set finished_at = current_timestamp, products = ?, errors = ?, blocked = ?, note = ? where id = ?",
            (products, errors, blocked, note, run_id),
        )

    # --- чтение ---------------------------------------------------------

    def known_skus(self, seen_within_days: int = 14) -> list[str]:
        """Артикулы, виденные в выдаче/карточках за последние N дней — список для ежедневного обхода."""
        rows = self._one("select sku, last_seen from {p}products")
        today = date.today()
        return sorted(sku for sku, last in rows if (today - _d(last)).days <= seen_within_days)

    def load_snapshots(self, since: date | None = None) -> list[Snapshot]:
        sql = "select snap_date, sku, size, stock, price, in_stock from {p}snapshots"
        params: tuple = ()
        if since:
            sql += " where snap_date >= ?"
            params = (since,)
        return [
            Snapshot(_d(d), sku, size, stock, float(price) if price is not None else None, bool(ins))
            for d, sku, size, stock, price, ins in self._one(sql, params)
        ]

    def export(self, table: str) -> tuple[list[str], list[tuple]]:
        if table not in {"products", "snapshots", "positions", "daily_sales", "runs"}:
            raise ValueError(table)
        cur = self.conn.cursor()
        cur.execute(self._sql(f"select * from {{p}}{table}"))
        return [c[0] for c in cur.description], cur.fetchall()
