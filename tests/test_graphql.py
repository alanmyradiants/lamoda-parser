import json

import httpx
import pytest

from lamoda_parser.graphql import BlockedError, LamodaGraphQL, QueryError, build_query, parse_envelope, parse_product

# Реальный ответ Lamoda (июль 2026, из открытого коннектора ru-marketplace-mcp)
REAL_FOUND = {
    "error": None,
    "result": [
        {
            "brand_name": "Finn Flare",
            "old_price_amount": 18499,
            "sizes": [
                {"is_available": False, "size": "48", "stock_remains": 0},
                {"is_available": False, "size": "50", "stock_remains": 0},
            ],
            "discount": 15,
            "sku": "MP002XM1RMM3",
            "name": "Куртка кожаная",
            "price_amount": 15724,
            "is_available": False,
            "is_sellable": True,
            "stock_remains": 0,
        }
    ],
}


def card(sku, stocks):
    return {
        "sku": sku, "name": "Футболка", "brand_name": "JOTO", "price_amount": 1990, "old_price_amount": 2990,
        "discount": 33, "is_available": any(stocks), "is_sellable": True, "stock_remains": sum(stocks),
        "sizes": [{"size": s, "is_available": bool(n), "stock_remains": n} for s, n in zip(["M", "L", "XL"], stocks)],
    }


def test_parse_real_envelope():
    [raw] = parse_envelope(REAL_FOUND)
    c = parse_product(raw)
    assert c.sku == "MP002XM1RMM3" and c.brand == "Finn Flare"
    assert c.price == 15724 and c.old_price == 18499 and c.discount == 15
    assert [(s.size, s.stock) for s in c.sizes] == [("48", 0), ("50", 0)]
    assert c.url == "https://www.lamoda.ru/p/mp002xm1rmm3/"


def test_unknown_sku_and_bad_field():
    assert parse_envelope({"error": None, "result": None}) == []
    with pytest.raises(QueryError):
        parse_envelope({"error": "Internal server error", "code": -32603})


def test_build_query_uppercases_and_escapes():
    q = build_query(["mp002xm1rmm3", "RTLAEY634001"])
    assert 'products(skus: ["MP002XM1RMM3", "RTLAEY634001"])' in q
    assert "stock_remains" in q


def make_client(handler, **kw):
    return LamodaGraphQL(transport=httpx.MockTransport(handler), delay=0, retries=kw.pop("retries", 0), **kw)


def test_fetch_batches_and_bisects_bad_sku():
    calls = []

    def handler(request):
        q = json.loads(request.content)["query"]
        calls.append(q)
        if "BADSKU0001" in q:
            return httpx.Response(200, json={"error": "Internal server error", "code": -32603})
        skus = [s for s in ("AA00000001", "AA00000002", "AA00000003") if s in q]
        return httpx.Response(200, json={"error": None, "result": [card(s, [3, 0, 5]) for s in skus]})

    with make_client(handler, batch_size=4) as gql:
        cards, failed = gql.fetch(["AA00000001", "AA00000002", "BADSKU0001", "AA00000003"])
    assert sorted(c.sku for c in cards) == ["AA00000001", "AA00000002", "AA00000003"]
    assert failed == ["BADSKU0001"]
    assert cards[0].stock_total == 8


def test_blocked_batch_reported_not_raised():
    with make_client(lambda r: httpx.Response(403, text="Запрос отклонен")) as gql:
        cards, failed = gql.fetch(["AA00000001"])
    assert cards == [] and failed == ["AA00000001"]


def test_network_error_becomes_blocked():
    def handler(request):
        raise httpx.ConnectError("boom")

    with make_client(handler, connect_retries=0) as gql:
        with pytest.raises(BlockedError):
            gql.query(build_query(["AA00000001"]))
        assert gql.fetch(["AA00000001"]) == ([], ["AA00000001"])


def test_probe_fields():
    def handler(request):
        q = json.loads(request.content)["query"]
        if "seller_name" in q:
            return httpx.Response(200, json={"error": None, "result": [{"sku": "AA00000001", "seller_name": "JOTO"}]})
        return httpx.Response(200, json={"error": "Internal server error", "code": -32603})

    with make_client(handler) as gql:
        res = gql.probe_fields("AA00000001", ["seller_name", "rating"])
    assert res["seller_name"] == "JOTO"
    assert res["rating"].startswith("✗")


def test_flaky_proxy_connect_is_retried_quickly(monkeypatch):
    monkeypatch.setattr("lamoda_parser.graphql.time.sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 5:  # вход прокси отвечает не с каждой попытки
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(200, json={"error": None, "result": [card("AA00000001", [2, 0, 1])]})

    with make_client(handler) as gql:  # retries=0: ответные ошибки не повторяем, а соединение — да
        cards, failed = gql.fetch(["AA00000001"])
    assert [c.sku for c in cards] == ["AA00000001"] and failed == []
    assert calls["n"] == 5


def test_connect_retries_are_bounded(monkeypatch):
    monkeypatch.setattr("lamoda_parser.graphql.time.sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectTimeout("timed out")

    with make_client(handler, connect_retries=3) as gql:
        assert gql.fetch(["AA00000001"]) == ([], ["AA00000001"])
    assert calls["n"] == 4
