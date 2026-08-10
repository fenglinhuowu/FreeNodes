"""Dual-engine crawler: Crawl4AI for page structure, httpx for file downloads."""
import asyncio
from dataclasses import dataclass

import httpx
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode

# Cap concurrent Chromium instances across all sites/articles.
_BROWSER_SEM: asyncio.Semaphore | None = None
_DOWNLOAD_CLIENT: httpx.AsyncClient | None = None
_DOWNLOAD_LOCK: asyncio.Lock | None = None


def _browser_sem() -> asyncio.Semaphore:
    global _BROWSER_SEM
    if _BROWSER_SEM is None:
        _BROWSER_SEM = asyncio.Semaphore(6)
    return _BROWSER_SEM


def _download_lock() -> asyncio.Lock:
    global _DOWNLOAD_LOCK
    if _DOWNLOAD_LOCK is None:
        _DOWNLOAD_LOCK = asyncio.Lock()
    return _DOWNLOAD_LOCK


@dataclass
class Page:
    url: str
    markdown: str
    html: str
    links: list[dict]  # [{"href": "...", "text": "..."}]
    success: bool = True
    error: str = ""


async def fetch_page(url: str, timeout_ms: int = 60000) -> Page:
    """Fetch a page via Crawl4AI and return structured content."""
    try:
        async with _browser_sem():
            async with AsyncWebCrawler() as crawler:
                result = await crawler.arun(
                    url=url,
                    config=CrawlerRunConfig(
                        cache_mode=CacheMode.BYPASS,
                        page_timeout=timeout_ms,
                    ),
                )
        if not result.success:
            return Page(url=url, success=False, error=result.error_message, markdown="", html="", links=[])

        md_obj = result.markdown
        markdown_text = ""
        if md_obj and hasattr(md_obj, "raw_markdown"):
            markdown_text = md_obj.raw_markdown or ""

        links: list[dict] = []
        for scope in ("internal", "external"):
            for link in (result.links or {}).get(scope, []):
                href = link.get("href", "")
                if href and not href.startswith("javascript:"):
                    links.append({"href": href, "text": link.get("text", "")[:200]})

        return Page(url=url, markdown=markdown_text, html=result.html or "", links=links)

    except Exception as e:
        return Page(url=url, success=False, error=str(e), markdown="", html="", links=[])


async def _get_download_client() -> httpx.AsyncClient:
    global _DOWNLOAD_CLIENT
    if _DOWNLOAD_CLIENT is None or _DOWNLOAD_CLIENT.is_closed:
        async with _download_lock():
            if _DOWNLOAD_CLIENT is None or _DOWNLOAD_CLIENT.is_closed:
                _DOWNLOAD_CLIENT = httpx.AsyncClient(
                    timeout=httpx.Timeout(60.0, connect=15.0, read=60.0),
                    follow_redirects=True,
                    limits=httpx.Limits(max_keepalive_connections=20, max_connections=40),
                )
    return _DOWNLOAD_CLIENT


async def download_file(url: str) -> str:
    """Download file via shared httpx client with extended read timeout."""
    client = await _get_download_client()
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.text
