"""Полный цикл CLI на фейковой Lamoda: каталог в Chromium → GraphQL → база → продажи."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright")

from lamoda_parser.__main__ import main  # noqa: E402

PAGES = {1: ["mp002xm0aaa1", "mp002xm0aaa2"], 2: ["mp002xm0aaa3"]}
STOCK = {"MP002XM0AAA1": [4, 2], "MP002XM0AAA2": [0, 1], "MP002XM0AAA3": [9, 9]}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        page = int(parse_qs(u.query).get("page", ["1"])[0])
        skus = PAGES.get(page, PAGES[2])  # за пределами — та же последняя страница
        links = "".join(f'<a href="/p/{s}/clothes-joto-futbolka/">{s}</a>' for s in skus)
        self._send(f"<html><body><div class='grid'>{links}</div></body></html>".encode(), "text/html; charset=utf-8")

    def do_POST(self):  # noqa: N802
        q = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["query"]
        result = [
            {"sku": s, "name": "Футболка", "brand_name": "JOTO", "price_amount": 1990, "old_price_amount": 2990,
             "is_available": True, "stock_remains": sum(st),
             "sizes": [{"size": z, "is_available": bool(n), "stock_remains": n} for z, n in zip(["M", "L"], st)]}
            for s, st in STOCK.items() if f'"{s}"' in q
        ]
        self._send(json.dumps({"error": None, "result": result or None}).encode(), "application/json")

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_lamoda(monkeypatch, tmp_path):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy", "LAMODA_PROXY"):
        monkeypatch.delenv(var, raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("LAMODA_GRAPHQL_URL", f"{base}/goapi/v2/catalog/graphql/products/")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/lamoda.db")
    monkeypatch.chdir(tmp_path)
    yield base
    srv.shutdown()


def test_collect_from_catalog(fake_lamoda, tmp_path, capsys):
    skus = tmp_path / "joto.txt"
    skus.write_text("MP002XM0AAA1\nMP002XM0ZZZ9  # нет на сайте\n", encoding="utf-8")
    rc = main(["collect", "--catalog", f"{fake_lamoda}/c/477/clothes-muzhskaya-odezhda/", "--pages", "5",
               "--skus-file", str(skus), "--delay", "0", "--scope", "test"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "карточек 3 из 4" in out

    assert main(["export", "positions", "-o", "pos.csv"]) == 0
    pos = (tmp_path / "pos.csv").read_text(encoding="utf-8-sig").splitlines()
    assert len(pos) == 4 and pos[1].endswith("MP002XM0AAA1;1") and pos[3].endswith("MP002XM0AAA3;3")

    assert main(["export", "snapshots"]) == 0
    assert capsys.readouterr().out.count("MP002XM0AAA") == 6  # 3 товара × 2 размера

    assert list((tmp_path / "data" / "raw").glob("*.jsonl.gz"))
    raw = next((tmp_path / "data" / "raw").glob("*.jsonl.gz"))
    assert main(["parse-raw", str(raw)]) == 0
    assert main(["sales"]) == 0
