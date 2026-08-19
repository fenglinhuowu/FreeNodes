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
    parallel_batches: int = 4,
    progress_cb=None,
) -> list[tuple[object, bool, float | None, str]]:
    """Test nodes via Mihomo URLTest API.

    Runs several Mihomo processes in parallel (parallel_batches), each testing
    a batch of proxies with up to `concurrency` concurrent delay probes.
    """
    mihomo = Path(mihomo_path)
    if not mihomo.is_file():
        raise FileNotFoundError(f"mihomo not found: {mihomo_path}")

    prepared: list[tuple[int, object, dict]] = []
    results_map: dict[int, tuple[object, bool, float | None, str]] = {}

    for i, node in enumerate(nodes):
        proxy = node_to_proxy(node, i)
        if not proxy:
            results_map[i] = (node, False, None, "unsupported")
            continue
        prepared.append((i, node, proxy))

    timeout_ms = max(1000, int(timeout * 1000))
    batches = [
        prepared[i:i + batch_size]
        for i in range(0, len(prepared), batch_size)
    ]
    if not batches:
        return [results_map.get(i, (node, False, None, "missing")) for i, node in enumerate(nodes)]

    workers = max(1, min(parallel_batches, len(batches)))
    sem = asyncio.Semaphore(workers)
    done_nodes = 0
    lock = asyncio.Lock()
    total = len(prepared)

    async def run_one(batch: list[tuple[int, object, dict]], batch_index: int):
        nonlocal done_nodes
        async with sem:
            if progress_cb:
                async with lock:
                    progress_cb(done_nodes, total)
            batch_results = await _run_batch(
                mihomo=mihomo,
                batch=batch,
                timeout_ms=timeout_ms,
                concurrency=concurrency,
                test_url=test_url,
            )
            async with lock:
                for idx, node, ok, latency, err in batch_results:
                    results_map[idx] = (node, ok, latency, err)
                done_nodes += len(batch)
                if progress_cb:
                    progress_cb(done_nodes, total)
            return batch_index

    await asyncio.gather(*[run_one(b, i) for i, b in enumerate(batches)])

    return [results_map.get(i, (node, False, None, "missing")) for i, node in enumerate(nodes)]


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

    with tempfile.TemporaryDirectory(prefix="freenodes-mihomo-", ignore_cleanup_errors=True) as tmp:
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


async def speed_test_nodes(
    nodes: list,
    *,
    mihomo_path: str,
    min_mbps: float = 0.1,
    speed_bytes: int = 200_000,
    speed_timeout: float = 12.0,
    concurrency: int = 8,
    speed_url: str = "http://cachefly.cachefly.net/200kb.test",
    compat_urls: list[str] | None = None,
    progress_cb=None,
) -> list[tuple[object, bool, float | None, str]]:
    """Probe YouTube/Google through each proxy, then measure non-CF download Mbps.

    Returns (node, ok, mbps, error) in input order.
    """
    mihomo = Path(mihomo_path)
    if not mihomo.is_file():
        raise FileNotFoundError(f"mihomo not found: {mihomo_path}")

    try:
        url = speed_url.format(bytes=speed_bytes)
    except (KeyError, ValueError, IndexError):
        url = speed_url

    probes = list(compat_urls) if compat_urls else []

    prepared: list[tuple[int, object, dict]] = []
    results_map: dict[int, tuple[object, bool, float | None, str]] = {}

    for i, node in enumerate(nodes):
        proxy = node_to_proxy(node, i)
        if not proxy:
            results_map[i] = (node, False, None, "unsupported")
            continue
        prepared.append((i, node, proxy))

    sem = asyncio.Semaphore(max(1, concurrency))
    done = 0
    total = len(prepared)
    lock = asyncio.Lock()

    async def one(idx: int, node, proxy: dict):
        nonlocal done
        async with sem:
            ok, mbps, err = await _speed_one_proxy(
                mihomo=mihomo,
                proxy=proxy,
                url=url,
                expect_bytes=speed_bytes,
                timeout=speed_timeout,
                min_mbps=min_mbps,
                compat_urls=probes,
            )
            async with lock:
                done += 1
                if progress_cb and (done == total or done % max(1, concurrency) == 0):
                    progress_cb(done, total)
            return idx, node, ok, mbps, err

    batch = await asyncio.gather(*[one(i, n, p) for i, n, p in prepared])
    for idx, node, ok, mbps, err in batch:
        results_map[idx] = (node, ok, mbps, err)

    return [results_map.get(i, (node, False, None, "missing")) for i, node in enumerate(nodes)]


async def _speed_one_proxy(
    *,
    mihomo: Path,
    proxy: dict,
    url: str,
    expect_bytes: int,
    timeout: float,
    min_mbps: float,
    compat_urls: list[str],
) -> tuple[bool, float | None, str]:
    """Spin up a single-proxy Mihomo; require YouTube/Google, then measure Mbps."""
    import httpx

    api_port = find_free_port()
    mixed_port = find_free_port()
    name = proxy["name"]
    config = {
        "mixed-port": mixed_port,
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
        "proxies": [proxy],
        "proxy-groups": [
            {"name": "SELECT", "type": "select", "proxies": [name, "DIRECT"]},
        ],
        "rules": ["MATCH,SELECT"],
    }

    with tempfile.TemporaryDirectory(prefix="freenodes-speed-", ignore_cleanup_errors=True) as tmp:
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
            if not await _wait_api(base, timeout=12.0):
                return False, None, "mihomo api timeout"

            async with httpx.AsyncClient(timeout=3.0) as api:
                try:
                    await api.put(f"{base}/proxies/SELECT", json={"name": name})
                except Exception:
                    pass

            proxy_url = f"http://127.0.0.1:{mixed_port}"
            async with httpx.AsyncClient(
                proxy=proxy_url,
                timeout=httpx.Timeout(timeout),
                follow_redirects=True,
            ) as client:
                # YouTube / Google must work through this proxy
                for probe in compat_urls:
                    try:
                        r = await client.get(probe)
                        # generate_204 -> 204; some stacks return 200 empty
                        if r.status_code not in (200, 204):
                            host = probe.split("/")[2]
                            return False, None, f"compat {host} http {r.status_code}"
                    except Exception as e:
                        host = probe.split("/")[2] if "://" in probe else probe
                        return False, None, f"compat {host}: {str(e)[:80]}"

                t0 = time.perf_counter()
                got = 0
                try:
                    async with client.stream("GET", url) as resp:
                        if resp.status_code >= 400:
                            return False, None, f"speed http {resp.status_code}"
                        async for chunk in resp.aiter_bytes():
                            got += len(chunk)
                            if got >= expect_bytes:
                                break
                except Exception as e:
                    if got < 1024:
                        return False, None, f"speed: {str(e)[:100]}"

            elapsed = max(time.perf_counter() - t0, 1e-3)
            if got < 1024:
                return False, None, "too few bytes"
            mbps = (got * 8) / elapsed / 1_000_000
            if mbps < min_mbps:
                return False, mbps, f"slow {mbps:.3f}<{min_mbps}"
            return True, mbps, ""
        finally:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except Exception:
                pass
