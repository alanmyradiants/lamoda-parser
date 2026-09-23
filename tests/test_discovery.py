from lamoda_parser.discovery import Listing, ListingPage, listing_key, page_url, read_skus_file, skus_from_links, skus_from_payload


def test_skus_from_links_dedup_and_order():
    hrefs = [
        "/p/rtlaey634001/shoes-adidasoriginals-kedy/",
        "/p/rtlaey634001/shoes-adidasoriginals-kedy/?sku=RTLAEY634001B030&source_rec_type=catalog",
        "https://www.lamoda.ru/p/mp002xb085kj/shoes-fila-krossovki/",
        "/c/477/clothes-muzhskaya-odezhda/",
    ]
    assert skus_from_links(hrefs) == ["RTLAEY634001", "MP002XB085KJ"]


def test_skus_from_payload():
    assert skus_from_payload({"payload": {"products": [{"sku": "mp002xm1rmm3"}, {"x": 1}]}}) == ["MP002XM1RMM3"]
    assert skus_from_payload({"payload": []}) == []


def test_page_url_and_listing_key():
    u = "https://www.lamoda.ru/c/477/clothes-muzhskaya-odezhda/?sort=price"
    assert page_url(u, 1) == u
    assert page_url(u, 3).endswith("?sort=price&page=3")
    assert listing_key(u) == ("category", "/c/477/clothes-muzhskaya-odezhda/")
    assert listing_key("https://www.lamoda.ru/catalogsearch/result/?q=футболка") == ("search", "футболка")


def test_positions_across_pages():
    lst = Listing("category", "/c/1/", [ListingPage("u", 1, ["A", "B"]), ListingPage("u", 2, ["B", "C"])])
    assert lst.positions() == [("A", 1), ("B", 2), ("C", 3)]


def test_read_skus_file(tmp_path):
    f = tmp_path / "skus.txt"
    f.write_text("# JOTO\nMP002XM1RMM3\nhttps://www.lamoda.ru/p/rtlaey634001/x/\n\nmp002xm1rmm3  # дубль\nfoo\n", encoding="utf-8")
    assert read_skus_file(f) == ["MP002XM1RMM3", "RTLAEY634001"]
