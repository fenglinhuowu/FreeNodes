"""Fetch direct subscription URLs (GitHub/CDN) without browser/LLM."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from src.config import SubscriptionConfig
from src.pipeline import process_txt


@dataclass
class FetchResult:
    name: str
    ok: bool = False
    bytes: int = 0
    lines: int = 0
    source_url: str = ""
    error: str = ""


def _guess_kind(url: str, kind: str) -> str:
    if kind in ("txt", "yaml"):
        return kind
    path = urlparse(url).path.lower()
    if path.endswith((".yaml", ".yml")):
        return "yaml"
    return "txt"


def _mirror_candidates(url: str, mirrors: list[str]) -> list[str]:
    """Build ordered URL list: explicit mirrors, then common GitHub mirrors, then original."""
    seen: set[str] = set()
    out: list[str] = []

    def add(u: str):
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    for m in mirrors:
        add(m)

    # jsDelivr / ghproxy fallbacks for raw.githubusercontent.com
    if "raw.githubusercontent.com/" in url:
        # https://raw.githubusercontent.com/{user}/{repo}/{branch}/path
        rest = url.split("raw.githubusercontent.com/", 1)[1]
        parts = rest.split("/", 3)
        if len(parts) >= 4:
            user, repo, branch, path = parts[0], parts[1], parts[2], parts[3]
            add(f"https://cdn.jsdelivr.net/gh/{user}/{repo}@{branch}/{path}")
            add(f"https://fastly.jsdelivr.net/gh/{user}/{repo}@{branch}/{path}")
            add(f"https://ghproxy.net/{url}")
            add(f"https://mirror.ghproxy.com/{url}")

    if "github.com/" in url and "/raw/" in url:
        add(f"https://ghproxy.net/{url}")

    add(url)
    return out


async def _download_one(client: httpx.AsyncClient, url: str) -> tuple[str | None, str]:
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None, f"http {resp.status_code}"
        text = resp.text
        if not text or len(text) < 20:
            return None, "empty body"
        return text, ""
    except Exception as e:
        return None, str(e)[:120]


async def fetch_subscription(
    sub: SubscriptionConfig,
    *,
    out_dir: str = "nodes",
    timeout: float = 30.0,
) -> FetchResult:
    """Download one subscription (trying mirrors) and save under nodes/."""
    if not sub.enabled:
        return FetchResult(name=sub.name, error="disabled")

    kind = _guess_kind(sub.url, sub.kind)
    candidates = _mirror_candidates(sub.url, sub.mirrors)
    result = FetchResult(name=sub.name)

    limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=15.0),
        follow_redirects=True,
        headers={"User-Agent": "FreeNodes/1.0"},
        limits=limits,
    ) as client:
        body = None
        last_err = "no candidates"
        for url in candidates:
            body, err = await _download_one(client, url)
            if body:
                result.source_url = url
                break
            last_err = err or "download failed"
            print(f"  [{sub.name}] miss {url[:70]}... ({last_err})")

    if not body:
        result.error = last_err
        print(f"  FAIL [{sub.name}] {last_err}")
        return result

    # Normalize & save
    if kind == "txt":
        content = process_txt(body)
        ext = ".txt"
        lines = content.count("\n") + (1 if content else 0)
    else:
        content = body
        ext = ".yaml"
        lines = content.count("\n") + (1 if content else 0)

    path = Path(out_dir)
    path.mkdir(exist_ok=True)
    filepath = path / f"{sub.name}{ext}"
    filepath.write_text(content, encoding="utf-8")

    result.ok = True
    result.bytes = len(content)
    result.lines = lines
    print(f"  OK  [{sub.name}] {filepath} ({result.bytes}B, ~{lines} lines) via {result.source_url[:60]}")
    return result


async def fetch_all_subscriptions(
    subs: list[SubscriptionConfig],
    *,
    out_dir: str = "nodes",
    concurrency: int = 6,
    timeout: float = 30.0,
) -> list[FetchResult]:
    """Fetch all enabled subscriptions concurrently."""
    enabled = [s for s in subs if s.enabled]
    if not enabled:
        print("\n[subscriptions] none configured")
        return []

    print(f"\n{'='*70}")
    print(f"SUBSCRIPTIONS ({len(enabled)} sources)")
    print(f"{'='*70}")

    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(sub: SubscriptionConfig) -> FetchResult:
        async with sem:
            return await fetch_subscription(sub, out_dir=out_dir, timeout=timeout)

    results = await asyncio.gather(*[one(s) for s in enabled])
    ok = sum(1 for r in results if r.ok)
    print(f"\n[subscriptions] {ok}/{len(enabled)} succeeded")
    return list(results)


def touch_site_meta(config, name: str, node_count: int):
    """Update matching SiteConfig up_date/node_count if a blog site shares the name."""
    from datetime import date
    today = date.today().isoformat()
    for s in config.sites:
        if s.name == name:
            s.up_date = today
            s.node_count = node_count
            return
