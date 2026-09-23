"""Прогон разведки на локальной «фейковой Lamoda»: страница с __NUXT__ и XHR с остатками."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("playwright")

from lamoda_parser.recon.run import main  # noqa: E402

PAGE = """<html><head>
<script type="application/ld+json">{"@type":"Product","sku":"MP002XM0ABC","offers":{"price":"2990"}}</script>
</head><body><h1>Футболка JOTO</h1>
<script>
window.__NUXT__ = {state: {product: {sku: "MP002XM0ABC", seller: "JOTO"}}};
fetch('/api/v1/product/sizes').then(r => r.json());
</script></body></html>"""

SIZES = {"sizes": [{"size": "M", "stock_quantity": 7}, {"size": "L", "stock_quantity": 0}]}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/"):
            body, ctype = json.dumps(SIZES).encode(), "application/json"
        elif self.path.startswith("/blocked"):
            body, ctype = "Проверка безопасности. Запрос отклонен".encode(), "text/html; charset=utf-8"
        else:
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "LAMODA_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_recon_finds_exact_stock(server, tmp_path):
    assert main(["--product", f"{server}/p/MP002XM0ABC/", "--catalog", f"{server}/blocked/", "--out", str(tmp_path)]) == 0
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "✅ Найдены числовые остатки" in report
    assert "__NUXT__" in report
    assert "stock_quantity" in report
    assert "блок True" in report  # страница-заглушка распознана
    assert list((tmp_path / "api").glob("product0_*.json"))
