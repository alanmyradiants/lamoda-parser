"""Настоящий браузер для Lamoda: прохождение JS-проверки Servicepipe и запросы изнутри страницы.

Lamoda закрыта антиботом Servicepipe. Без браузера (httpx/curl) и с дата-центра —
«Запрос отклонен». С домашнего IP — страница-заглушка с JS-проверкой
(«js-challenge-loader», "is_captcha": false): браузер выполняет скрипт,
получает cookie и уходит на настоящую страницу. Поэтому:

  1. открываем сайт в Chromium и ждём, пока заглушка сменится настоящей страницей;
  2. GraphQL вызываем через fetch() из этой страницы — с cookie-пропуском
     и отпечатком настоящего браузера.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any
from urllib.parse import urlparse

from .graphql import BlockedError, LamodaGraphQL, parse_envelope
from .recon.inspect import is_blocked

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
CHALLENGE_MARKERS = ("js-challenge-loader", "servicepipe", "id_captcha_frame_div", "Пожалуйста, пройдите проверку")
HEAVY_RESOURCES = frozenset({"image", "media", "font"})
WEBDRIVER_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"

FETCH_JS = """async ({url, query}) => {
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
      body: JSON.stringify({query}),
      credentials: 'include',
    });
    return {status: r.status, text: await r.text()};
  } catch (e) {
    return {status: 0, text: String(e)};
  }
}"""


class CaptchaError(BlockedError):
    """Servicepipe показал настоящую капчу (is_captcha: true) — автоматически не пройти."""


def is_challenge(html: str) -> bool:
    return any(m in html for m in CHALLENGE_MARKERS)


def is_captcha(html: str) -> bool:
    return '"is_captcha":true' in html.replace(" ", "")


def page_state(html: str) -> str:
    """'ok' | 'challenge' | 'captcha' | 'blocked'."""
    if is_captcha(html):
        return "captcha"
    if is_challenge(html):
        return "challenge"
    if is_blocked(html):
        return "blocked"
    return "ok"


def proxy_config() -> dict[str, str] | None:
    raw = os.environ.get("LAMODA_PROXY")
    if not raw:
        return None
    u = urlparse(raw)
    cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        cfg["username"] = u.username
        cfg["password"] = u.password or ""
    return cfg


def launch_kwargs(headful: bool) -> dict[str, Any]:
    return {
        "headless": not headful,
        "proxy": proxy_config(),
        "executable_path": os.environ.get("LAMODA_CHROMIUM") or None,
        "args": ["--disable-blink-features=AutomationControlled"],
    }


def context_kwargs() -> dict[str, Any]:
    return {
        "user_agent": USER_AGENT,
        "locale": "ru-RU",
        "timezone_id": "Europe/Moscow",
        "viewport": {"width": 1920, "height": 1080},
    }


def load_media() -> bool:
    return os.environ.get("LAMODA_LOAD_MEDIA") == "1"


async def wait_challenge_async(page, timeout: float = 45.0) -> str:
    """Ждёт, пока JS-проверка пропустит на настоящую страницу. Возвращает page_state."""
    deadline = time.monotonic() + timeout
    state = "challenge"
    while time.monotonic() < deadline:
        try:
            state = page_state(await page.content())
        except Exception:  # noqa: BLE001 — страница как раз перенаправляется
            state = "challenge"
        if state != "challenge":
            return state
        await page.wait_for_timeout(1000)
    return state


def wait_challenge_sync(page, timeout: float = 45.0) -> str:
    deadline = time.monotonic() + timeout
    state = "challenge"
    while time.monotonic() < deadline:
        try:
            state = page_state(page.content())
        except Exception:  # noqa: BLE001
            state = "challenge"
        if state != "challenge":
            return state
        page.wait_for_timeout(1000)
    return state


class BrowserGraphQL(LamodaGraphQL):
    """Тот же интерфейс, что у LamodaGraphQL, но запросы идут из страницы Chromium."""

    def __init__(
        self,
        headful: bool = False,
        nav_retries: int = 6,
        challenge_timeout: float = 45.0,
        **kw: Any,
    ):
        super().__init__(**kw)
        from playwright.sync_api import sync_playwright

        self.nav_retries = nav_retries
        self.challenge_timeout = challenge_timeout
        u = urlparse(self.url)
        self.origin = f"{u.scheme}://{u.netloc}/"
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(**launch_kwargs(headful))
            self._ctx = self._browser.new_context(**context_kwargs())
            self._ctx.add_init_script(WEBDRIVER_JS)
            if not load_media():
                self._ctx.route(
                    "**/*",
                    lambda route: route.abort() if route.request.resource_type in HEAVY_RESOURCES else route.continue_(),
                )
            self.page = self._ctx.new_page()
            self.warm()
        except BaseException:
            self.close()
            raise

    def warm(self) -> None:
        """Открыть сайт и пройти JS-проверку; после этого fetch() идёт с cookie-пропуском."""
        from playwright.sync_api import Error as PlaywrightError

        last = "нет попыток"
        for n in range(1, self.nav_retries + 1):
            try:
                self.page.goto(self.origin, wait_until="domcontentloaded", timeout=45_000)
            except PlaywrightError as e:
                last = f"навигация: {str(e).splitlines()[0]}"
                log.info("открыть %s не удалось (%d/%d): %s", self.origin, n, self.nav_retries, last)
                time.sleep(2)
                continue
            state = wait_challenge_sync(self.page, self.challenge_timeout)
            if state == "ok":
                log.info("JS-проверка пройдена (попытка %d)", n)
                return
            if state == "captcha":
                raise CaptchaError("Servicepipe показал капчу — нужен другой IP/прокси или ручное прохождение")
            last = f"страница: {state}"
            log.info("проверка не пройдена (%d/%d): %s", n, self.nav_retries, state)
        raise BlockedError(f"не удалось пройти JS-проверку Lamoda: {last}")

    def query(self, query: str) -> list[dict[str, Any]]:
        from playwright.sync_api import Error as PlaywrightError

        last = ""
        attempt = 0
        net_fails = 0
        while True:
            self._pace()
            try:
                res = self.page.evaluate(FETCH_JS, {"url": self.url, "query": query})
            except PlaywrightError as e:  # страница упала/перезагрузилась
                res = {"status": 0, "text": str(e)}
            status, text = int(res.get("status") or 0), str(res.get("text") or "")
            if status == 0:
                # fetch не дошёл (обрыв соединения с прокси) — быстрый повтор
                net_fails += 1
                last = f"сеть: {text[:120]}"
                if net_fails <= self.connect_retries:
                    time.sleep(1.0)
                    continue
                break
            if status == 200:
                import json

                try:
                    return parse_envelope(json.loads(text))  # QueryError не повторяем
                except json.JSONDecodeError:
                    last = f"HTTP 200, не JSON: {text[:120]}"
            else:
                last = f"HTTP {status}: {text[:120]}"
            if attempt >= self.retries:
                break
            if status in (401, 403, 429) or is_challenge(text) or is_blocked(text):
                log.warning("пропуск истёк (%s) — прохожу проверку заново", last[:60])
                self.warm()
            else:
                time.sleep(min(60.0, 5.0 * 2**attempt))
            attempt += 1
        raise BlockedError(last)

    def close(self) -> None:
        for obj, meth in ((getattr(self, "_browser", None), "close"), (getattr(self, "_pw", None), "stop")):
            if obj is not None:
                try:
                    getattr(obj, meth)()
                except Exception:  # noqa: BLE001
                    pass
        super().close()
