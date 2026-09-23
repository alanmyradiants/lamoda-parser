"""Поиск артикулов: страницы каталога/поиска в Chromium (Playwright) или файл.

GraphQL умеет только «артикул → карточка», поэтому список артикулов и их
позиции в выдаче берём со страниц. Каталог закрыт антиботом сильнее, чем
GraphQL: нужен российский IP и настоящий браузер. Порядок ссылок на странице —
это позиция товара в выдаче.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .recon.inspect import is_blocked
from .recon.run import USER_AGENT, _playwright_proxy

log = logging.getLogger(__name__)

SKU_IN_URL = re.compile(r"/p/([a-z0-9]{8,20})/", re.I)
SKU_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{6,18}$")
CHALLENGE_MARKERS = ("Пожалуйста, пройдите проверку", "SmartCaptcha")


@dataclass
class ListingPage:
    url: str
    page: int
    skus: list[str] = field(default_factory=list)
    blocked: bool = False
    error: str | None = None


@dataclass
class Listing:
    source: str  # 'category' | 'search'
    query: str   # путь категории или поисковый запрос
    pages: list[ListingPage] = field(default_factory=list)

    def positions(self) -> list[tuple[str, int]]:
        """(артикул, позиция) по всем страницам, первое вхождение, с 1."""
        seen: dict[str, int] = {}
        for p in self.pages:
            for sku in p.skus:
                if sku not in seen:
                    seen[sku] = len(seen) + 1
        return list(seen.items())


def skus_from_links(hrefs: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for h in hrefs:
        m = SKU_IN_URL.search(h or "")
        if not m:
            continue
        sku = m.group(1).upper()
        if sku not in seen:
            seen.add(sku)
            out.append(sku)
    return out


def skus_from_payload(obj: Any) -> list[str]:
    """Артикулы из JSON с payload.products[] (ответ страницы каталога при SPA-навигации)."""
    products = obj.get("payload", {}).get("products") if isinstance(obj, dict) and isinstance(obj.get("payload"), dict) else None
    if not isinstance(products, list):
        return []
    return [str(p["sku"]).upper() for p in products if isinstance(p, dict) and p.get("sku")]


def read_skus_file(path: Path) -> list[str]:
    """Файл с артикулами или ссылками на товары, по одному в строке (# — комментарий)."""
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        found = skus_from_links([line]) or ([line.upper()] if SKU_RE.match(line.upper()) else [])
        out.extend(s for s in found if s not in out)
    return out


def page_url(url: str, page: int) -> str:
    u = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(u.query) if k != "page"]
    if page > 1:
        q.append(("page", str(page)))
    return urlunparse(u._replace(query=urlencode(q)))


def listing_key(url: str) -> tuple[str, str]:
    u = urlparse(url)
    if u.path.startswith("/catalogsearch"):
        return "search", dict(parse_qsl(u.query)).get("q", "")
    return "category", u.path


async def crawl(
    urls: list[str],
    max_pages: int = 5,
    headful: bool = False,
    delay: float = 4.0,
) -> list[Listing]:
    from playwright.async_api import async_playwright

    listings: list[Listing] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not headful,
            proxy=_playwright_proxy(),
            executable_path=os.environ.get("LAMODA_CHROMIUM") or None,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT, locale="ru-RU", timezone_id="Europe/Moscow", viewport={"width": 1920, "height": 1080}
        )
        await ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = await ctx.new_page()
        payload_skus: list[str] = []

        async def on_response(resp):
            if "json" not in resp.headers.get("content-type", ""):
                return
            try:
                payload_skus.extend(skus_from_payload(await resp.json()))
            except Exception:  # noqa: BLE001
                pass

        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

        for url in urls:
            source, query = listing_key(url)
            listing = Listing(source, query)
            listings.append(listing)
            seen: set[str] = set()
            for n in range(1, max_pages + 1):
                lp = ListingPage(page_url(url, n), n)
                listing.pages.append(lp)
                payload_skus.clear()
                try:
                    await page.goto(lp.url, wait_until="domcontentloaded", timeout=60_000)
                    await page.wait_for_timeout(3_000)
                    for _ in range(4):  # ленивые плитки догружаются при прокрутке
                        await page.mouse.wheel(0, 4_000)
                        await page.wait_for_timeout(800)
                    html = await page.content()
                    if is_blocked(html) or any(m in html for m in CHALLENGE_MARKERS):
                        lp.blocked = True
                        log.error("блок антибота на %s", lp.url)
                        break
                    hrefs = await page.eval_on_selector_all("a[href*='/p/']", "els => els.map(e => e.getAttribute('href'))")
                    lp.skus = skus_from_links(hrefs) or list(dict.fromkeys(payload_skus))
                except Exception as e:  # noqa: BLE001
                    lp.error = f"{type(e).__name__}: {e}"
                    log.error("ошибка на %s: %s", lp.url, lp.error)
                    break
                new = [s for s in lp.skus if s not in seen]
                log.info("%s стр.%d: %d товаров, новых %d", query, n, len(lp.skus), len(new))
                if not new:
                    break  # страницы кончились
                seen.update(new)
                await asyncio.sleep(delay * random.uniform(0.7, 1.4))
        await browser.close()
    return listings
