"""Real proxy delay testing via local Mihomo (Clash Meta) API."""
from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import yaml

from src.uri_to_clash import uri_to_clash


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def node_to_proxy(node, index: int) -> dict | None:
    """Build a Clash proxy dict from NodeCandidate."""
    name = f"n{index:04d}"
    if getattr(node, "clash_proxy", None):
        proxy = dict(node.clash_proxy)
        proxy["name"] = name
        return proxy
    raw = getattr(node, "raw", "") or ""
    if raw.startswith(("vless://", "trojan://", "vmess://", "ss://", "hysteria2://", "hy2://", "tuic://")):
        proxy = uri_to_clash(raw, name=name)
        return proxy
    return None


async def _wait_api(base: str, timeout: float = 15.0) -> bool:
    import httpx
    deadline = time.time() + timeout
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.time() < deadline:
            try:
                r = await client.get(f"{base}/version")
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.2)
    return False


async def _delay_one(client, base: str, name: str, test_url: str, timeout_ms: int) -> tuple[bool, float | None, str]:
    url = f"{base}/proxies/{quote(name, safe='')}/delay"
    try:
        r = await client.get(url, params={"url": test_url, "timeout": str(timeout_ms)})
        if r.status_code != 200:
            return False, None, f"http {r.status_code}"
        data = r.json()
        delay = data.get("delay")
        if delay is None or delay <= 0 or delay == 65535:
            return False, None, "delay=-1"
        return True, float(delay), ""
    except Exception as e:
        return False, None, str(e)


async def check_with_mihomo(
    nodes: list,
    *,
    mihomo_path: str,
    timeout: float = 5.0,
    concurrency: int = 20,
    test_url: str = "http://www.gstatic.com/generate_204",
    batch_size: int = 120,
    progress_cb=None,
) -> list[tuple[object, bool, float | None, str]]:
    """Test nodes via Mihomo URLTest API.

    Returns list of (node, ok, latency_ms, error) aligned with input order for
    successfully converted proxies only — callers should map by node identity.
    Unconvertible nodes are returned as (node, False, None, 'unsupported').
    """
    mihomo = Path(mihomo_path)
    if not mihomo.is_file():
        raise FileNotFoundError(f"mihomo not found: {mihomo_path}")

    # Prepare proxies with stable names: (index, node, proxy_dict)
    prepared: list[tuple[int, object, dict]] = []
    results_map: dict[int, tuple[object, bool, float | None, str]] = {}

    for i, node in enumerate(nodes):
        proxy = node_to_proxy(node, i)
        if not proxy:
            results_map[i] = (node, False, None, "unsupported")
            continue
        prepared.append((i, node, proxy))

    timeout_ms = max(1000, int(timeout * 1000))

    for batch_start in range(0, len(prepared), batch_size):
        batch = prepared[batch_start:batch_start + batch_size]
        if progress_cb:
            progress_cb(batch_start, len(prepared))
        batch_results = await _run_batch(
            mihomo=mihomo,
            batch=batch,
            timeout_ms=timeout_ms,
            concurrency=concurrency,
            test_url=test_url,
        )
        for idx, node, ok, latency, err in batch_results:
            results_map[idx] = (node, ok, latency, err)

    # Preserve original order
    out = []
    for i, node in enumerate(nodes):
        out.append(results_map.get(i, (node, False, None, "missing")))
    return out


async def _run_batch(
    *,
    mihomo: Path,
    batch: list[tuple[int, object, dict]],
    timeout_ms: int,
    concurrency: int,
    test_url: str,
) -> list[tuple[int, object, bool, float | None, str]]:
    import httpx

    api_port = find_free_port()
    proxies = [p for _, _, p in batch]
    names = [p["name"] for p in proxies]

    config = {
        "mixed-port": 0,
        "allow-lan": False,
        "mode": "global",
        "log-level": "silent",
        "ipv6": False,
        "external-controller": f"127.0.0.1:{api_port}",
        "dns": {
            "enable": True,
            "listen": "0.0.0.0:0",
            "enhanced-mode": "fake-ip",
            "nameserver": ["8.8.8.8", "1.1.1.1"],
        },
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "SELECT",
                "type": "select",
                "proxies": names + ["DIRECT"],
            }
        ],
        "rules": ["MATCH,SELECT"],
    }

    with tempfile.TemporaryDirectory(prefix="freenodes-mihomo-") as tmp:
        work = Path(tmp)
        cfg_path = work / "config.yaml"
        cfg_path.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        proc = await asyncio.create_subprocess_exec(
            str(mihomo),
            "-f", str(cfg_path),
            "-d", str(work),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{api_port}"
        try:
            if not await _wait_api(base, timeout=20.0):
                return [(idx, node, False, None, "mihomo api timeout") for idx, node, _ in batch]

            sem = asyncio.Semaphore(max(1, concurrency))

            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_ms / 1000 + 5)) as client:
                async def one(idx, node, proxy):
                    async with sem:
                        ok, latency, err = await _delay_one(
                            client, base, proxy["name"], test_url, timeout_ms
                        )
                        return idx, node, ok, latency, err

                return list(await asyncio.gather(*[
                    one(idx, node, proxy) for idx, node, proxy in batch
                ]))
        finally:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except Exception:
                pass
