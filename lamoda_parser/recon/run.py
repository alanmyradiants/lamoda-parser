"""Разведка Lamoda. Запускать с российского IP (Timeweb VPS или прокси РФ).

    python -m lamoda_parser.recon.run \
        --product https://www.lamoda.ru/p/<sku>/<slug>/ \
        --catalog https://www.lamoda.ru/c/477/clothes-muzhskaya-odezhda/ \
        --out recon_out

Что делает:
  1. Простой HTTP-запрос (httpx) — пускает ли Lamoda без браузера.
  2. Открывает страницы в Chromium (Playwright), перехватывает все JSON-ответы
     внутреннего API и состояние страницы (__NUXT__ и т.п.), JSON-LD.
  3. Ищет поля остатков, размеров, цен, продавца и пишет report.md.

Прокси: переменная окружения LAMODA_PROXY=http://user:pass@host:port
Свой Chromium (если версии Playwright не совпадают): LAMODA_CHROMIUM=/path/to/chrome
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..browser import wait_challenge_async
from .inspect import Findings, inspect_json, is_blocked, merge

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
STATE_GLOBALS = ("__NUXT__", "__INITIAL_STATE__", "__NEXT_DATA__", "__APOLLO_STATE__", "__PRELOADED_STATE__")
JSON_LD_RE = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)


@dataclass
class PageResult:
    label: str
    url: str
    http_status: int | None = None
    http_blocked: bool | None = None
    http_error: str | None = None
    browser_status: int | None = None
    browser_blocked: bool | None = None
    browser_error: str | None = None
    challenge: str | None = None
    api_calls: list[dict[str, Any]] = field(default_factory=list)
    state_globals: list[str] = field(default_factory=list)
    findings: Findings = field(default_factory=Findings)


def _proxy() -> str | None:
    return os.environ.get("LAMODA_PROXY") or None


def _slug(url: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", urlparse(url).path).strip("_")
    return s[:80] or "root"


def json_ld_blocks(html: str) -> list[Any]:
    out = []
    for raw in JSON_LD_RE.findall(html):
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def probe_http(res: PageResult, out_dir: Path) -> None:
    import httpx

    try:
        with httpx.Client(
            proxy=_proxy(),
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
            follow_redirects=True,
            timeout=30,
        ) as client:
            r = client.get(res.url)
        res.http_status = r.status_code
        res.http_blocked = is_blocked(r.text)
        (out_dir / f"{res.label}_http.html").write_text(r.text, encoding="utf-8")
        for block in json_ld_blocks(r.text):
            res.findings = merge(res.findings, inspect_json(block))
    except Exception as e:  # noqa: BLE001 — разведка должна дойти до отчёта
        res.http_error = f"{type(e).__name__}: {e}"


def _playwright_proxy() -> dict[str, str] | None:
    raw = _proxy()
    if not raw:
        return None
    u = urlparse(raw)
    cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        cfg["username"] = u.username
        cfg["password"] = u.password or ""
    return cfg


async def probe_browser(results: list[PageResult], out_dir: Path, headful: bool) -> None:
    from playwright.async_api import async_playwright

    api_dir = out_dir / "api"
    api_dir.mkdir(exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not headful,
            proxy=_playwright_proxy(),
            executable_path=os.environ.get("LAMODA_CHROMIUM") or None,
        )
        ctx = await browser.new_context(user_agent=USER_AGENT, locale="ru-RU", timezone_id="Europe/Moscow")
        for res in results:
            page = await ctx.new_page()
            pending: list[asyncio.Task] = []

            async def on_response(resp, res=res):
                ctype = resp.headers.get("content-type", "")
                if "json" not in ctype:
                    return
                try:
                    body = await resp.json()
                except Exception:  # noqa: BLE001
                    return
                n = len(res.api_calls)
                fname = f"{res.label}_{n:03d}_{_slug(resp.url)}.json"
                (api_dir / fname).write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
                f = inspect_json(body)
                res.findings = merge(res.findings, f)
                res.api_calls.append(
                    {
                        "method": resp.request.method,
                        "url": resp.url,
                        "status": resp.status,
                        "file": f"api/{fname}",
                        "stock_hits": len(f.by_category("stock")),
                        "numeric_stock": len(f.numeric_stock()),
                    }
                )

            page.on("response", lambda r: pending.append(asyncio.ensure_future(on_response(r))))
            try:
                resp = await page.goto(res.url, wait_until="domcontentloaded", timeout=60_000)
                res.browser_status = resp.status if resp else None
                # заглушка Servicepipe: ждём, пока JS-проверка пропустит на настоящую страницу
                res.challenge = await wait_challenge_async(page, 45.0)
                # даём догрузиться XHR (размеры, наличие часто приходят отдельно)
                await page.wait_for_timeout(5_000)
                await page.mouse.wheel(0, 3_000)
                await page.wait_for_timeout(3_000)
                html = await page.content()
                res.browser_blocked = is_blocked(html) or res.challenge != "ok"
                (out_dir / f"{res.label}_browser.html").write_text(html, encoding="utf-8")
                await page.screenshot(path=str(out_dir / f"{res.label}.png"), full_page=False)
                for block in json_ld_blocks(html):
                    res.findings = merge(res.findings, inspect_json(block))
                for name in STATE_GLOBALS:
                    state = await page.evaluate(
                        f"() => {{ try {{ return window['{name}'] ? JSON.parse(JSON.stringify(window['{name}'])) : null }}"
                        " catch (e) { return null } }"
                    )
                    if state is None:
                        continue
                    res.state_globals.append(name)
                    (out_dir / f"{res.label}_state_{name.strip('_')}.json").write_text(
                        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
                    )
                    res.findings = merge(res.findings, inspect_json(state))
            except Exception as e:  # noqa: BLE001
                res.browser_error = f"{type(e).__name__}: {e}"
            finally:
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await page.close()
        await browser.close()


VERDICT_TEXT = {
    "exact": "✅ Найдены числовые остатки (> 1) — продажи можно считать по разнице остатков.",
    "flag_only": "⚠️ Есть только признак наличия (да/нет) — нужна проверка через корзину или модель по отзывам.",
    "not_found": "❓ Поля остатков не найдены — смотреть вручную api/ и state-файлы.",
}


def render_report(results: list[PageResult]) -> str:
    lines = ["# Разведка Lamoda", "", f"Прокси: {'да' if _proxy() else 'нет'}", ""]
    total = merge(*(r.findings for r in results))
    lines += ["## Вывод по остаткам", "", VERDICT_TEXT[total.stock_verdict()], ""]
    for r in results:
        lines += [f"## {r.label}: {r.url}", ""]
        lines.append(
            f"- HTTP без браузера: статус {r.http_status}, блок {r.http_blocked}"
            + (f", ошибка {r.http_error}" if r.http_error else "")
        )
        lines.append(
            f"- Браузер: статус {r.browser_status}, блок {r.browser_blocked}, JS-проверка: {r.challenge}"
            + (f", ошибка {r.browser_error}" if r.browser_error else "")
        )
        lines.append(f"- Состояние страницы: {', '.join(r.state_globals) or 'нет'}")
        lines.append(f"- Остатки: {VERDICT_TEXT[r.findings.stock_verdict()]}")
        lines += ["", "### JSON-запросы", ""]
        if not r.api_calls:
            lines.append("нет")
        for c in r.api_calls:
            mark = " **← числовые остатки**" if c["numeric_stock"] else ""
            lines.append(f"- `{c['method']} {c['status']}` {c['url'][:160]} → `{c['file']}`{mark}")
        for cat in ("stock", "availability", "size", "seller", "price", "rating"):
            hits = r.findings.by_category(cat)
            if not hits:
                continue
            lines += ["", f"### Поля: {cat} ({len(hits)})", ""]
            for h in hits[:25]:
                lines.append(f"- `{h.path}` = `{str(h.value)[:80]}`")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Разведка Lamoda (запускать с IP РФ)")
    ap.add_argument("--product", action="append", default=[], help="URL карточки товара (можно несколько)")
    ap.add_argument("--catalog", action="append", default=[], help="URL категории (можно несколько)")
    ap.add_argument("--out", default="recon_out")
    ap.add_argument("--headful", action="store_true", help="показать окно браузера (нужен дисплей)")
    ap.add_argument("--no-browser", action="store_true", help="только простой HTTP")
    args = ap.parse_args(argv)
    if not args.product and not args.catalog:
        ap.error("укажите хотя бы один --product или --catalog")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = [PageResult(f"product{i}", u) for i, u in enumerate(args.product)]
    results += [PageResult(f"catalog{i}", u) for i, u in enumerate(args.catalog)]

    for r in results:
        probe_http(r, out_dir)
    if not args.no_browser:
        asyncio.run(probe_browser(results, out_dir, args.headful))

    report = render_report(results)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
