from datetime import date

import pytest

from lamoda_parser.graphql import parse_product
from lamoda_parser.sales import estimate_daily_sales
from lamoda_parser.storage import Storage

D1, D2 = date(2026, 10, 1), date(2026, 10, 2)


def card(sku, stocks, price=1990):
    return parse_product({
        "sku": sku, "name": "Футболка", "brand_name": "JOTO", "price_amount": price, "old_price_amount": 2990,
        "is_available": any(stocks), "stock_remains": sum(stocks),
        "sizes": [{"size": s, "is_available": bool(n), "stock_remains": n} for s, n in zip(["M", "L"], stocks)],
    })


@pytest.fixture(params=["sqlite", "postgres"])
def storage(request, tmp_path):
    if request.param == "sqlite":
        st = Storage(f"sqlite:///{tmp_path}/t.db")
    else:
        st = Storage(request.getfixturevalue("pg_url"))
        st.conn.execute("drop schema if exists lamoda_market cascade")
        st.conn.commit()
    st.init()
    st.init()  # идемпотентно
    yield st
    st.close()


def test_two_days_to_sales(storage):
    storage.save_cards(D1, [card("AA00000001", [10, 5]), card("AA00000002", [0, 0])])
    storage.save_cards(D2, [card("AA00000001", [7, 5], price=1790), card("AA00000002", [0, 3])])
    storage.save_cards(D2, [card("AA00000001", [7, 5], price=1790)])  # повторный запуск в тот же день — upsert

    snaps = storage.load_snapshots()
    assert len(snaps) == 8
    sales = {(s.sku, s.sale_date): s for s in estimate_daily_sales(snaps)}
    a = sales[("AA00000001", D2)]
    assert a.units == 3 and a.revenue == 3 * 1990  # цена вчерашнего снимка
    assert sales[("AA00000002", D2)].restocked

    storage.save_daily_sales(list(sales.values()))
    storage.save_daily_sales(list(sales.values()))
    cols, rows = storage.export("daily_sales")
    assert len(rows) == 4 and "units" in cols


def test_positions_runs_known(storage):
    today = date.today()
    storage.touch_skus(today, ["AA00000001", "AA00000002"])
    storage.save_positions(today, "category", "/c/477/", [("AA00000001", 1), ("AA00000002", 2)])
    storage.save_positions(today, "category", "/c/477/", [("AA00000001", 2), ("AA00000002", 1)])
    _, rows = storage.export("positions")
    assert sorted(r[3:] for r in rows) == [("AA00000001", 2), ("AA00000002", 1)]
    assert storage.known_skus(14) == ["AA00000001", "AA00000002"]

    storage.save_cards(today, [card("AA00000001", [1, 1])])
    _, prods = storage.export("products")
    assert {p[0]: p[2] for p in prods} == {"AA00000001": "JOTO", "AA00000002": None}

    run = storage.start_run("test")
    storage.finish_run(run, 1, 0, 0, "ok")
    cols, runs = storage.export("runs")
    assert runs[0][cols.index("note")] == "ok"
