from datetime import date

from lamoda_parser.sales import Snapshot, estimate_daily_sales, summarize

D1, D2, D3, D4 = (date(2026, 10, d) for d in (1, 2, 3, 4))


def snap(d, size, stock, price=1000.0, sku="A"):
    return Snapshot(d, sku, size, stock, price, in_stock=bool(stock))


def test_sales_from_stock_decrease_across_sizes():
    sales = estimate_daily_sales([
        snap(D1, "M", 10), snap(D2, "M", 7),
        snap(D1, "L", 5), snap(D2, "L", 4),
    ])
    d2 = [s for s in sales if s.sale_date == D2][0]
    assert d2.units == 4
    assert d2.revenue == 4000.0
    assert not d2.restocked


def test_restock_is_flagged_not_counted_as_negative_sales():
    sales = estimate_daily_sales([snap(D1, "M", 2), snap(D2, "M", 20)])
    d2 = sales[-1]
    assert d2.units == 0 and d2.restocked


def test_revenue_uses_previous_price():
    sales = estimate_daily_sales([snap(D1, "M", 5, price=2000), snap(D2, "M", 3, price=1500)])
    assert sales[-1].revenue == 4000.0


def test_unknown_stock_skipped():
    sales = estimate_daily_sales([
        Snapshot(D1, "A", "M", None, 1000, True),
        Snapshot(D2, "A", "M", None, 1000, True),
    ])
    assert sum(s.units for s in sales) == 0


def test_summary_and_lost_revenue():
    rows = [snap(D1, "M", 4), snap(D2, "M", 2), snap(D3, "M", 0), snap(D4, "M", 0)]
    [s] = summarize(estimate_daily_sales(rows))
    # продано 2 + 2 = 4 шт, в наличии дни D1, D2; нет в наличии D3, D4
    assert s.units == 4 and s.revenue == 4000.0
    assert s.days == 4 and s.days_in_stock == 2
    assert s.avg_daily_units == 1.0  # 2 шт за 2 дня в наличии (D1 без продаж)
    assert s.lost_revenue == 2 * 1.0 * 1000.0
