import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from lamoda_parser import relay as relay_mod
from lamoda_parser.relay import Relay, effective_proxy


class Hello(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"privet"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_relay_waits_for_flaky_upstream():
    port = free_port()
    r = Relay("127.0.0.1", port, connect_timeout=0.3, attempts=100, pool_size=0)
    rport = r.start()

    def late_server():  # прокси «оживает» не сразу
        time.sleep(1.0)
        srv = ThreadingHTTPServer(("127.0.0.1", port), Hello)
        srv.serve_forever()

    threading.Thread(target=late_server, daemon=True).start()
    resp = httpx.get(f"http://127.0.0.1:{rport}/", timeout=20, trust_env=False)
    assert resp.status_code == 200 and resp.text == "privet"
    assert r.stats["connect_fail"] >= 1 and r.stats["connect_ok"] >= 1


def test_relay_uses_warm_pool():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Hello)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    r = Relay("127.0.0.1", srv.server_port, pool_size=2)
    rport = r.start()
    time.sleep(0.5)
    for _ in range(3):
        with httpx.Client(trust_env=False) as c:  # новое соединение на каждый запрос
            assert c.get(f"http://127.0.0.1:{rport}/").text == "privet"
    assert r.stats["pooled"] >= 1
    srv.shutdown()


def test_relay_gives_up_when_upstream_dead():
    r = Relay("127.0.0.1", free_port(), connect_timeout=0.2, attempts=3, pool_size=0)
    rport = r.start()
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"http://127.0.0.1:{rport}/", timeout=10, trust_env=False)


def test_effective_proxy(monkeypatch):
    monkeypatch.setattr(relay_mod, "_relay", None)
    monkeypatch.delenv("LAMODA_RELAY", raising=False)
    monkeypatch.setenv("LAMODA_PROXY", "http://user:pa55@us.res.proxy-seller.com:10000")
    assert effective_proxy() == "http://user:pa55@us.res.proxy-seller.com:10000"
    monkeypatch.setenv("LAMODA_RELAY", "1")
    url = effective_proxy()
    assert url.startswith("http://user:pa55@127.0.0.1:") and url == effective_proxy()  # один ретранслятор
    monkeypatch.delenv("LAMODA_PROXY")
    assert effective_proxy() is None
