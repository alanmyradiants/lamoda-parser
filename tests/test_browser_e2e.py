"""Фейковая Lamoda за «Servicepipe»: без cookie — JS-проверка, GraphQL без cookie — 403."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("playwright")

from lamoda_parser.__main__ import main  # noqa: E402
from lamoda_parser.browser import BrowserGraphQL, CaptchaError, page_state  # noqa: E402

# Реальная заглушка Lamoda (23.09.2026, через резидентный прокси), сокращена
REAL_CHALLENGE = """<!DOCTYPE html><html><head>
<noscript><meta http-equiv="refresh" content="0; url=/exhkqyad"></noscript>
<script src="https://BfedYmTP5Cg1.servicepipe.tech/loaders/6c34d90b.js" async=""></script></head>
<body><div id="id_spinner" class="spinner-container"><js-challenge-loader class="spinner-loader"></js-challenge-loader></div>
<div id="id_captcha_frame_div" style="display: none;height: 100vh;"></div>
<script>function get_options() { return JSON.parse('{"woprmswjtq":25842,"is_captcha":false,"is_partitioned":false}'); }</script>
</body></html>"""


def test_page_state_on_real_challenge():
    assert page_state(REAL_CHALLENGE) == "challenge"
    assert page_state(REAL_CHALLENGE.replace('"is_captcha":false', '"is_captcha":true')) == "captcha"
    assert page_state("<title>Запрос отклонен</title>") == "blocked"
    # реальная страница капчи с поворотом картинки (23.09.2026), без маркеров заглушки
    rotated = '<div class="captcha-wrap"><script src="./sp_rotated_captcha/js/bundle.js"></script></div>'
    assert page_state(rotated) == "captcha"
    assert page_state("<html><h1>Мужская одежда</h1></html>") == "ok"


class State:
    generation = 1        # текущее значение пропуска; сервер может «отозвать» его
    rotate_after_posts = 0  # через сколько GraphQL-запросов отозвать пропуск (0 — никогда)
    captcha = False
    posts = 0
    challenges = 0


def challenge_page(gen: int, captcha: bool) -> bytes:
    opts = '{"is_captcha":%s}' % ("true" if captcha else "false")
    script = "" if captcha else f"setTimeout(() => {{ document.cookie = 'sp={gen}; path=/'; location.reload(); }}, 400);"
    return (
        "<html><body><js-challenge-loader></js-challenge-loader>"
        f"<script>var o = JSON.parse('{opts}'); {script}</script></body></html>"
    ).encode()


class Handler(BaseHTTPRequestHandler):
    def _passed(self) -> bool:
        return f"sp={State.generation}" in (self.headers.get("Cookie") or "")

    def do_GET(self):  # noqa: N802
        if not self._passed():
            State.challenges += 1
            return self._send(challenge_page(State.generation, State.captcha), "text/html; charset=utf-8")
        if self.path.startswith("/c/"):
            links = "".join(f'<a href="/p/{s}/x/">{s}</a>' for s in ("mp002xm0bbb1", "mp002xm0bbb2"))
            if "page=2" in self.path:
                links = '<a href="/p/mp002xm0bbb2/x/">x</a>'
            return self._send(f"<html><body>{links}</body></html>".encode(), "text/html; charset=utf-8")
        self._send("<html><body><h1>Lamoda</h1></body></html>".encode(), "text/html; charset=utf-8")

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if not self._passed():
            return self._send("<title>Запрос отклонен</title>".encode(), "text/html; charset=utf-8", 403)
        State.posts += 1
        if State.rotate_after_posts and State.posts == State.rotate_after_posts:
            State.generation += 1  # пропуск «истёк» после этого ответа
        q = json.loads(body)["query"]
        result = [
            {"sku": s, "name": "Кеды", "brand_name": "JOTO", "price_amount": 4990, "is_available": True,
             "stock_remains": 12, "sizes": [{"size": "42", "is_available": True, "stock_remains": 12}]}
            for s in ("MP002XM0BBB1", "MP002XM0BBB2") if f'"{s}"' in q
        ]
        self._send(json.dumps({"error": None, "result": result or None}).encode(), "application/json")

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def servicepipe(monkeypatch, tmp_path):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy", "LAMODA_PROXY"):
        monkeypatch.delenv(var, raising=False)
    State.generation, State.rotate_after_posts, State.captcha, State.posts, State.challenges = 1, 0, False, 0, 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("LAMODA_GRAPHQL_URL", f"{base}/goapi/v2/catalog/graphql/products/")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/lamoda.db")
    monkeypatch.chdir(tmp_path)
    yield base
    srv.shutdown()


def test_browser_passes_challenge_and_fetches(servicepipe):
    with BrowserGraphQL(delay=0, challenge_timeout=15) as gql:
        cards, failed = gql.fetch(["MP002XM0BBB1", "MP002XM0BBB2"])
    assert sorted(c.sku for c in cards) == ["MP002XM0BBB1", "MP002XM0BBB2"] and failed == []
    assert cards[0].sizes[0].stock == 12
    assert State.challenges >= 1


def test_expired_pass_is_renewed(servicepipe):
    State.rotate_after_posts = 1
    with BrowserGraphQL(delay=0, batch_size=1, challenge_timeout=15) as gql:
        cards, failed = gql.fetch(["MP002XM0BBB1", "MP002XM0BBB2"])
    assert len(cards) == 2 and failed == []
    assert State.challenges >= 2  # прошёл проверку второй раз


def test_captcha_is_reported(servicepipe, capsys):
    State.captcha = True
    with pytest.raises(CaptchaError):
        BrowserGraphQL(delay=0, challenge_timeout=5)
    assert main(["probe", "--browser", "MP002XM0BBB1"]) == 1
    assert "капчу" in capsys.readouterr().out


def test_collect_via_browser_with_catalog(servicepipe, capsys):
    rc = main(["collect", "--browser", "--catalog", f"{servicepipe}/c/477/men/", "--pages", "3", "--delay", "0"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "карточек 2 из 2" in out and "блоков каталога 0" in out
