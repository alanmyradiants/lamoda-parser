"""Клиент анонимного GraphQL-эндпоинта карточек Lamoda.

    POST https://www.lamoda.ru/goapi/v2/catalog/graphql/products/
    {"query": "query { products(skus: [\"MP002XM1RMM3\"]) { sku ... } }"}

Эндпоинт принимает список артикулов и отдаёт цену, скидку, наличие и
остаток (stock_remains) по размерам. Поиска по каталогу в нём нет — артикулы
берём из discovery. Особенности (сняты с живого сайта в июле 2026):

  * конверт нестандартный: {"error": null, "result": [...]};
  * неизвестный артикул  -> {"error": null, "result": null};
  * неизвестное поле     -> {"error": "Internal server error", "code": -32603}
    и никаких данных — поэтому набор полей фиксированный, а новые поля
    проверяем по одному командой probe-fields;
  * rating/отзывов в схеме нет, интроспекция выключена.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://www.lamoda.ru/goapi/v2/catalog/graphql/products/"

PRODUCT_FIELDS = (
    "sku name brand_name price_amount old_price_amount discount "
    "is_available is_sellable stock_remains sizes { size is_available stock_remains }"
)

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": "https://www.lamoda.ru",
    "Referer": "https://www.lamoda.ru/",
}

# Кандидаты для probe-fields: чего нет в подтверждённом наборе, но нужно для MPStats-метрик.
CANDIDATE_FIELDS = (
    "seller_name", "seller { name }", "seller { title }", "seller_id", "supplier_name", "merchant_name",
    "is_marketplace", "category_name", "category { name }", "categories { name }", "gender", "season",
    "material", "composition", "color", "color_family", "model_name", "country", "url", "rating",
    "reviews_count", "discount_amount", "sizes { sku }", "sizes { brand_size }", "sizes { price_amount }",
)


class LamodaError(RuntimeError):
    pass


class BlockedError(LamodaError):
    """HTTP-ответ не JSON или 403/429 — нас не пускают."""


class QueryError(LamodaError):
    """Lamoda отвергла запрос (обычно — неизвестное поле)."""


@dataclass
class SizeRow:
    size: str
    is_available: bool
    stock: int | None


@dataclass
class ProductCard:
    sku: str
    name: str | None
    brand: str | None
    price: float | None
    old_price: float | None
    discount: float | None
    is_available: bool
    stock_total: int | None
    sizes: list[SizeRow] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def url(self) -> str:
        return f"https://www.lamoda.ru/p/{self.sku.lower()}/"


def _int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_product(raw: dict[str, Any]) -> ProductCard:
    sizes = [
        SizeRow(size=str(s.get("size") or ""), is_available=bool(s.get("is_available")), stock=_int(s.get("stock_remains")))
        for s in raw.get("sizes") or []
        if isinstance(s, dict)
    ]
    return ProductCard(
        sku=str(raw["sku"]).upper(),
        name=raw.get("name"),
        brand=raw.get("brand_name"),
        price=_float(raw.get("price_amount")),
        old_price=_float(raw.get("old_price_amount")),
        discount=_float(raw.get("discount")),
        is_available=bool(raw.get("is_available")),
        stock_total=_int(raw.get("stock_remains")),
        sizes=sizes,
        raw=raw,
    )


def build_query(skus: Iterable[str], fields: str = PRODUCT_FIELDS) -> str:
    return "query { products(skus: [%s]) { %s } }" % (", ".join(json.dumps(s.upper()) for s in skus), fields)


def parse_envelope(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise QueryError(f"ответ не объект: {str(payload)[:200]}")
    err = payload.get("error")
    if isinstance(err, str) and err:
        raise QueryError(f"{err} (code {payload.get('code')})")
    if isinstance(payload.get("errors"), list) and payload["errors"]:
        raise QueryError(str(payload["errors"][0])[:200])
    result = payload.get("result")
    if result is None and isinstance(payload.get("data"), dict):
        result = payload["data"].get("products")
    if result is None:
        return []
    if not isinstance(result, list):
        raise QueryError(f"result имеет тип {type(result).__name__}, ожидался список")
    return [r for r in result if isinstance(r, dict) and r.get("sku")]


class LamodaGraphQL:
    def __init__(
        self,
        url: str | None = None,
        proxy: str | None = None,
        batch_size: int = 20,
        delay: float = 1.5,
        retries: int = 3,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.url = url or os.environ.get("LAMODA_GRAPHQL_URL") or GRAPHQL_URL
        self.batch_size = batch_size
        self.delay = delay
        self.retries = retries
        self.client = httpx.Client(
            headers=HEADERS,
            proxy=proxy if proxy is not None else (os.environ.get("LAMODA_PROXY") or None),
            timeout=timeout,
            transport=transport,
            trust_env=transport is None,
        )
        self._last = 0.0

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "LamodaGraphQL":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _pace(self) -> None:
        wait = self.delay * random.uniform(0.7, 1.3) - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def query(self, query: str) -> list[dict[str, Any]]:
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            self._pace()
            try:
                r = self.client.post(self.url, json={"query": query})
            except httpx.TransportError as e:
                last_exc = e
            else:
                if r.status_code in (403, 429) or r.status_code >= 500:
                    last_exc = BlockedError(f"HTTP {r.status_code}: {r.text[:200]}")
                else:
                    try:
                        payload = r.json()
                    except json.JSONDecodeError:
                        last_exc = BlockedError(f"HTTP {r.status_code}, не JSON: {r.text[:200]}")
                    else:
                        return parse_envelope(payload)  # QueryError не повторяем
            if attempt < self.retries:
                backoff = min(60.0, 5.0 * 2**attempt)
                log.warning("попытка %d не удалась (%s), жду %.0f с", attempt + 1, last_exc, backoff)
                time.sleep(backoff)
        if isinstance(last_exc, LamodaError):
            raise last_exc
        raise BlockedError(f"сеть: {last_exc}") from last_exc

    def fetch(self, skus: list[str]) -> tuple[list[ProductCard], list[str]]:
        """Карточки по списку артикулов. Возвращает (карточки, артикулы с ошибкой)."""
        cards: list[ProductCard] = []
        failed: list[str] = []
        queue = [skus[i : i + self.batch_size] for i in range(0, len(skus), self.batch_size)]
        while queue:
            batch = queue.pop(0)
            try:
                cards.extend(parse_product(r) for r in self.query(build_query(batch)))
            except QueryError as e:
                if len(batch) > 1:
                    # один «битый» артикул может ронять весь батч — делим пополам
                    mid = len(batch) // 2
                    queue[:0] = [batch[:mid], batch[mid:]]
                else:
                    log.warning("артикул %s отвергнут: %s", batch[0], e)
                    failed.extend(batch)
            except LamodaError as e:
                log.error("батч из %d артикулов не получен: %s", len(batch), e)
                failed.extend(batch)
        return cards, failed

    def probe_fields(self, sku: str, candidates: Iterable[str] = CANDIDATE_FIELDS) -> dict[str, Any]:
        """Проверяет по одному, какие поля принимает схема. {поле: значение | ошибка}."""
        out: dict[str, Any] = {}
        for f in candidates:
            try:
                rows = self.query(build_query([sku], f"sku {f}"))
                name = f.split()[0]
                out[f] = rows[0].get(name) if rows else "(товар не найден)"
            except QueryError as e:
                out[f] = f"✗ {e}"
        return out
