from lamoda_parser.recon.inspect import inspect_json, is_blocked


def test_exact_stock_detected():
    payload = {"product": {"sku": "MP002XM0ABC", "sizes": [{"size": "M", "stock_quantity": 7}, {"size": "L", "stock_quantity": 0}]}}
    f = inspect_json(payload)
    assert f.stock_verdict() == "exact"
    assert [h.path for h in f.numeric_stock()] == ["$.product.sizes[0].stock_quantity"]


def test_flag_only_when_just_booleans():
    payload = {"sizes": [{"size": "M", "is_available": True, "in_stock": True}, {"size": "L", "in_stock": False}]}
    assert inspect_json(payload).stock_verdict() == "flag_only"


def test_quantity_of_one_is_not_exact():
    # максимум 1 в корзину ≠ точный остаток
    assert inspect_json({"sizes": [{"quantity": 1}]}).stock_verdict() == "flag_only"


def test_not_found():
    assert inspect_json({"title": "Футболка", "brand": "JOTO"}).stock_verdict() == "not_found"


def test_string_digits_count():
    assert inspect_json({"stock": "12"}).stock_verdict() == "exact"


def test_block_page_detected():
    assert is_blocked("<h1>Проверка безопасности</h1><p>Запрос отклонен</p>")
    assert not is_blocked("<h1>Футболка JOTO</h1>")
