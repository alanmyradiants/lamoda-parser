"""Оценка продаж по разнице остатков между ежедневными снимками (как MPStats).

Правила:
  * продажи размера за день = max(остаток вчера − остаток сегодня, 0);
  * если остаток вырос — был приход; продажи за этот день по размеру считаем 0
    и помечаем день флагом restocked (оценка занижена);
  * если между снимками пропуск в несколько дней, продажи относим к дню
    текущего снимка (равномерно не размазываем — это делает агрегация);
  * выручка = штуки × цена предыдущего снимка (цена, по которой продавали);
  * размеры без числового остатка (stock is None) в расчёт не попадают.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Snapshot:
    snap_date: date
    sku: str
    size: str
    stock: int | None
    price: float | None
    in_stock: bool


@dataclass
class DailySale:
    sale_date: date
    sku: str
    units: int = 0
    revenue: float = 0.0
    in_stock: bool = False
    restocked: bool = False
    method: str = "stock_diff"


def estimate_daily_sales(snapshots: list[Snapshot]) -> list[DailySale]:
    by_size: dict[tuple[str, str], list[Snapshot]] = defaultdict(list)
    for s in snapshots:
        by_size[(s.sku, s.size)].append(s)

    days: dict[tuple[date, str], DailySale] = {}

    def day(d: date, sku: str) -> DailySale:
        key = (d, sku)
        if key not in days:
            days[key] = DailySale(sale_date=d, sku=sku)
        return days[key]

    for (sku, _size), rows in by_size.items():
        rows.sort(key=lambda r: r.snap_date)
        for r in rows:
            if r.in_stock:
                day(r.snap_date, sku).in_stock = True
        for prev, cur in zip(rows, rows[1:]):
            d = day(cur.snap_date, sku)
            if prev.stock is None or cur.stock is None:
                continue
            delta = prev.stock - cur.stock
            if delta > 0:
                d.units += delta
                d.revenue += delta * (prev.price or cur.price or 0.0)
            elif delta < 0:
                d.restocked = True

    return sorted(days.values(), key=lambda s: (s.sku, s.sale_date))


@dataclass
class SkuSummary:
    sku: str
    units: int
    revenue: float
    days: int
    days_in_stock: int
    avg_daily_units: float  # по дням в наличии
    lost_revenue: float     # дни без остатка × средние продажи в день × средняя цена


def summarize(sales: list[DailySale]) -> list[SkuSummary]:
    by_sku: dict[str, list[DailySale]] = defaultdict(list)
    for s in sales:
        by_sku[s.sku].append(s)

    out = []
    for sku, rows in sorted(by_sku.items()):
        units = sum(r.units for r in rows)
        revenue = sum(r.revenue for r in rows)
        in_stock = [r for r in rows if r.in_stock]
        avg_units = sum(r.units for r in in_stock) / len(in_stock) if in_stock else 0.0
        avg_price = revenue / units if units else 0.0
        days_out = len(rows) - len(in_stock)
        out.append(
            SkuSummary(
                sku=sku,
                units=units,
                revenue=round(revenue, 2),
                days=len(rows),
                days_in_stock=len(in_stock),
                avg_daily_units=round(avg_units, 3),
                lost_revenue=round(days_out * avg_units * avg_price, 2),
            )
        )
    return out
