"""Поиск в произвольном JSON полей, похожих на остатки, размеры, цены и продавца.

Внутренний API Lamoda нам заранее неизвестен, поэтому не парсим конкретную
схему, а обходим все пойманные JSON-ответы и состояние страницы и ищем ключи
по названию. Главный вопрос разведки — есть ли у размеров числовой остаток
(> 1), а не только признак «есть / нет».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator

BLOCK_MARKERS = ("Проверка безопасности", "Запрос отклонен", "Запрос отклонён")

CATEGORIES: dict[str, re.Pattern[str]] = {
    "stock": re.compile(
        r"stock|quantit|qty|remain|left_?count|available_?(count|amount|qty)|остат",
        re.I,
    ),
    "availability": re.compile(r"^(is_?)?(available|in_?stock|sellable|has_?stock)$", re.I),
    "size": re.compile(r"size|razmer|размер", re.I),
    "price": re.compile(r"price|цена|discount|скидк", re.I),
    "seller": re.compile(r"seller|merchant|supplier|vendor|partner|продав", re.I),
    "rating": re.compile(r"rating|review|рейтинг|отзыв", re.I),
    "sku": re.compile(r"^(sku|simple_?sku|article|артикул|product_?id)$", re.I),
}


@dataclass
class Hit:
    category: str
    path: str
    value: Any


@dataclass
class Findings:
    hits: list[Hit] = field(default_factory=list)

    def by_category(self, category: str) -> list[Hit]:
        return [h for h in self.hits if h.category == category]

    def numeric_stock(self) -> list[Hit]:
        """Поля остатков с числом больше 1 — признак того, что видны точные штуки."""
        out = []
        for h in self.by_category("stock"):
            v = h.value
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) and v > 1:
                out.append(h)
            elif isinstance(v, str) and v.isdigit() and int(v) > 1:
                out.append(h)
        return out

    def stock_verdict(self) -> str:
        if self.numeric_stock():
            return "exact"  # есть числовой остаток
        if self.by_category("stock") or self.by_category("availability"):
            return "flag_only"  # поля есть, но только 0/1/true/false
        return "not_found"


def is_blocked(text: str) -> bool:
    return any(m in text for m in BLOCK_MARKERS)


def _walk(obj: Any, path: str = "$") -> Iterator[tuple[str, str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            yield p, str(k), v
            yield from _walk(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")


def _is_scalar(v: Any) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def inspect_json(obj: Any, max_hits_per_category: int = 200) -> Findings:
    findings = Findings()
    counts: dict[str, int] = {}
    for path, key, value in _walk(obj):
        if not _is_scalar(value):
            continue
        for cat, rx in CATEGORIES.items():
            if counts.get(cat, 0) >= max_hits_per_category:
                continue
            if rx.search(key):
                findings.hits.append(Hit(cat, path, value))
                counts[cat] = counts.get(cat, 0) + 1
    return findings


def merge(*items: Findings) -> Findings:
    out = Findings()
    for f in items:
        out.hits.extend(f.hits)
    return out
