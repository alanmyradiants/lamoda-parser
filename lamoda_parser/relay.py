"""Локальный TCP-ретранслятор до прокси: прячет нестабильное соединение с входом прокси.

Из российских дата-центров вход зарубежного прокси-сервиса доступен не с каждой
попытки (проверено: ~1 из 3 TCP-подключений). Браузеру на страницу нужны десятки
соединений, и он спотыкается. Ретранслятор слушает 127.0.0.1, сам пробивается к
прокси с повторами и держит несколько готовых соединений в запасе. Байты он
передаёт как есть, поэтому логин/пароль прокси и HTTPS идут насквозь.

    LAMODA_PROXY=http://user:pass@us.res.proxy-seller.com:10000
    LAMODA_RELAY=1           # включить (парсер сам поднимет ретранслятор)

или отдельно: python -m lamoda_parser.relay --upstream host:port --listen 127.0.0.1:8899
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import threading
import time
from urllib.parse import urlparse, urlunparse

log = logging.getLogger(__name__)


class Relay:
    def __init__(
        self,
        upstream_host: str,
        upstream_port: int,
        connect_timeout: float = 4.0,
        attempts: int = 30,
        pool_size: int = 4,
        pool_max_age: float = 15.0,
    ):
        self.upstream = (upstream_host, upstream_port)
        self.connect_timeout = connect_timeout
        self.attempts = attempts
        self.pool_size = pool_size
        self.pool_max_age = pool_max_age
        self.port: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pool: list[tuple[float, asyncio.StreamReader, asyncio.StreamWriter]] = []
        self._ready = threading.Event()
        self.stats = {"clients": 0, "connect_ok": 0, "connect_fail": 0, "pooled": 0}

    # --- соединение с прокси ------------------------------------------------

    async def _connect_once(self):
        return await asyncio.wait_for(asyncio.open_connection(*self.upstream), self.connect_timeout)

    async def _connect(self):
        for _ in range(self.attempts):
            try:
                conn = await self._connect_once()
                self.stats["connect_ok"] += 1
                return conn
            except (OSError, asyncio.TimeoutError):
                self.stats["connect_fail"] += 1
                await asyncio.sleep(0.3)
        raise ConnectionError(f"прокси {self.upstream[0]}:{self.upstream[1]} недоступен после {self.attempts} попыток")

    async def _take(self):
        now = time.monotonic()
        while self._pool:
            born, r, w = self._pool.pop(0)
            if now - born < self.pool_max_age and not r.at_eof():
                self.stats["pooled"] += 1
                return r, w
            w.close()
        return await self._connect()

    async def _fill_pool(self) -> None:
        while True:
            now = time.monotonic()
            for item in [p for p in self._pool if now - p[0] >= self.pool_max_age]:
                self._pool.remove(item)
                item[2].close()
            if len(self._pool) < self.pool_size:
                try:
                    r, w = await self._connect_once()
                    self.stats["connect_ok"] += 1
                    self._pool.append((time.monotonic(), r, w))
                    continue
                except (OSError, asyncio.TimeoutError):
                    self.stats["connect_fail"] += 1
            await asyncio.sleep(0.5)

    # --- клиенты ------------------------------------------------------------

    @staticmethod
    async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass

    async def _handle(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        self.stats["clients"] += 1
        try:
            ur, uw = await self._take()
        except ConnectionError as e:
            log.warning("%s", e)
            cw.close()
            return
        await asyncio.gather(self._pipe(cr, uw), self._pipe(ur, cw))

    async def _serve(self, host: str, port: int) -> None:
        server = await asyncio.start_server(self._handle, host, port)
        self.port = server.sockets[0].getsockname()[1]
        if self.pool_size:
            asyncio.ensure_future(self._fill_pool())
        self._ready.set()
        async with server:
            await server.serve_forever()

    def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        """Запустить в фоновом потоке; вернуть порт."""

        def run() -> None:
            self._loop = asyncio.new_event_loop()
            self._loop.run_until_complete(self._serve(host, port))

        threading.Thread(target=run, name="lamoda-relay", daemon=True).start()
        if not self._ready.wait(10):
            raise RuntimeError("ретранслятор не запустился")
        assert self.port is not None
        log.info("ретранслятор 127.0.0.1:%d → %s:%d", self.port, *self.upstream)
        return self.port


_relay: Relay | None = None
_relay_lock = threading.Lock()


def effective_proxy() -> str | None:
    """LAMODA_PROXY; при LAMODA_RELAY=1 — тот же URL, но через локальный ретранслятор."""
    raw = os.environ.get("LAMODA_PROXY") or None
    if not raw or os.environ.get("LAMODA_RELAY") != "1":
        return raw
    global _relay
    u = urlparse(raw)
    with _relay_lock:
        if _relay is None:
            _relay = Relay(u.hostname or "", u.port or 80)
            _relay.start()
    auth = f"{u.username}:{u.password}@" if u.username else ""
    return urlunparse(u._replace(netloc=f"{auth}127.0.0.1:{_relay.port}"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TCP-ретранслятор до нестабильного прокси")
    ap.add_argument("--upstream", required=True, help="host:port прокси")
    ap.add_argument("--listen", default="127.0.0.1:8899")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    uh, up = args.upstream.rsplit(":", 1)
    lh, lp = args.listen.rsplit(":", 1)
    relay = Relay(uh, int(up))
    relay.start(lh, int(lp))
    try:
        while True:
            time.sleep(30)
            log.info("статистика: %s", relay.stats)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
