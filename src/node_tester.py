"""Node connectivity tester — TCP probe or real Mihomo url-test."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

_URI_SCHEMES = ("vless://", "trojan://", "vmess://", "ss://", "ssr://", "hysteria2://", "hy2://", "anytls://", "tuic://")

DEFAULT_MIHOMO = r"D:\Program Files\Sparkle\resources\sidecar\mihomo.exe"


@dataclass
class CheckConfig:
    timeout: float = 5.0
    concurrency: int = 20
    save_alive: bool = True
    alive_file: str = "alive.txt"
    mode: str = "proxy"  # "proxy" (mihomo url-test) or "tcp"
    mihomo_path: str = DEFAULT_MIHOMO
    test_url: str = "http://www.gstatic.com/generate_204"
    batch_size: int = 120


@dataclass
class NodeCandidate:
    """A parseable node ready for probing."""

    host: str
    port: int
    label: str
    raw: str                 # original URI or clash name line
    source: str = ""         # file / site name
    scheme: str = ""
    clash_proxy: dict | None = None  # full Clash dict when loaded from yaml


@dataclass
class ProbeResult:
    node: NodeCandidate
    ok: bool
    latency_ms: float | None = None
    error: str = ""


@dataclass
class CheckSummary:
    total: int = 0
    parsed: int = 0
    unique_endpoints: int = 0
    alive: int = 0
    dead: int = 0
    alive_nodes: list[NodeCandidate] = field(default_factory=list)
    results: list[ProbeResult] = field(default_factory=list)


def parse_uri(line: str) -> NodeCandidate | None:
    """Parse a single share-link URI into host/port."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    lower = line.lower()
    if not any(lower.startswith(s) for s in _URI_SCHEMES):
        return None

    try:
        if lower.startswith("vmess://"):
            return _parse_vmess(line)
        if lower.startswith("ss://"):
            return _parse_ss(line)
        if lower.startswith("ssr://"):
            return _parse_ssr(line)
        return _parse_standard_uri(line)
    except Exception as e:
        logger.debug("parse failed for %s: %s", line[:60], e)
        return None


def parse_clash_proxy(proxy: dict, source: str = "") -> NodeCandidate | None:
    """Parse a Clash proxy dict."""
    if not isinstance(proxy, dict):
        return None
    host = proxy.get("server")
    port = proxy.get("port")
    if not host or not port:
        return None
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return None
    name = str(proxy.get("name") or f"{host}:{port_i}")
    scheme = str(proxy.get("type") or "clash")
    # Keep a stable raw representation for console output
    raw = f"{scheme}://{host}:{port_i}#{name}"
    return NodeCandidate(
        host=str(host),
        port=port_i,
        label=name,
        raw=raw,
        source=source,
        scheme=scheme,
        clash_proxy=dict(proxy),
    )


def load_nodes_from_txt(path: Path, source: str | None = None) -> list[NodeCandidate]:
    """Load and parse share-link lines from a .txt subscription file."""
    src = source or path.name
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        decoded = base64.b64decode(text.strip()).decode("utf-8", errors="replace")
        # Only treat as base64 if it looks like URIs after decode
        if any(s in decoded for s in ("vless://", "vmess://", "trojan://", "ss://")):
            text = decoded
    except Exception:
        pass

    nodes: list[NodeCandidate] = []
    seen: set[str] = set()
    for line in text.splitlines():
        node = parse_uri(line)
        if not node:
            continue
        if node.raw in seen:
            continue
        seen.add(node.raw)
        node.source = src
        nodes.append(node)
    return nodes


def load_nodes_from_yaml(path: Path, source: str | None = None) -> list[NodeCandidate]:
    """Extract Clash proxies from a yaml file."""
    import yaml  # lazy — check-only on .txt works without PyYAML installed

    src = source or path.name
    text = path.read_text(encoding="utf-8", errors="replace")
    nodes: list[NodeCandidate] = []
    seen: set[str] = set()
    try:
        for doc in yaml.safe_load_all(text):
            if not isinstance(doc, dict):
                continue
            for proxy in doc.get("proxies") or []:
                node = parse_clash_proxy(proxy, source=src)
                if not node:
                    continue
                key = f"{node.host}:{node.port}:{node.scheme}:{node.label}"
                if key in seen:
                    continue
                seen.add(key)
                nodes.append(node)
    except Exception as e:
        logger.warning("yaml parse skip %s: %s", path.name, e)
    return nodes


def collect_nodes(
    nodes_dir: str | Path,
    *,
    prefer_merged: bool = True,
    site: str | None = None,
    source_file: str | None = None,
) -> list[NodeCandidate]:
    """Collect nodes from nodes/ directory.

    Priority:
      1. explicit source_file
      2. single site files (site.txt / site.yaml)
      3. merged.txt (+ merged.yaml if no txt)
      4. all site txt/yaml files
    """
    root = Path(nodes_dir)
    if source_file:
        path = Path(source_file)
        if not path.is_absolute():
            path = root / path if not path.exists() else path
        if path.suffix.lower() in (".yaml", ".yml"):
            return load_nodes_from_yaml(path)
        return load_nodes_from_txt(path)

    if site:
        nodes: list[NodeCandidate] = []
        txt = root / f"{site}.txt"
        yml = root / f"{site}.yaml"
        if txt.exists():
            nodes.extend(load_nodes_from_txt(txt, source=site))
        if yml.exists():
            nodes.extend(load_nodes_from_yaml(yml, source=site))
        return _dedup_candidates(nodes)

    if prefer_merged:
        merged_txt = root / "merged.txt"
        if merged_txt.exists():
            return load_nodes_from_txt(merged_txt, source="merged")
        merged_yaml = root / "merged.yaml"
        if merged_yaml.exists():
            return load_nodes_from_yaml(merged_yaml, source="merged")

    nodes = []
    for f in sorted(root.glob("*.txt")):
        if f.name in ("merged.txt", "alive.txt"):
            continue
        nodes.extend(load_nodes_from_txt(f))
    for f in sorted(root.glob("*.yaml")):
        if f.name in ("merged.yaml", "provider.yaml", "alive.yaml"):
            continue
        nodes.extend(load_nodes_from_yaml(f))
    return _dedup_candidates(nodes)


async def tcp_probe(host: str, port: int, timeout: float) -> tuple[bool, float | None, str]:
    """Async TCP connect probe. Returns (ok, latency_ms, error)."""
    start = time.perf_counter()
    try:
        conn = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(conn, timeout=timeout)
        latency = (time.perf_counter() - start) * 1000
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        del reader
        return True, latency, ""
    except asyncio.TimeoutError:
        return False, None, "timeout"
    except OSError as e:
        return False, None, str(e) or e.__class__.__name__
    except Exception as e:
        return False, None, str(e)


async def check_nodes(
    nodes: list[NodeCandidate],
    *,
    timeout: float = 3.0,
    concurrency: int = 100,
) -> CheckSummary:
    """Probe unique host:port endpoints, map results back to all nodes."""
    summary = CheckSummary(total=len(nodes), parsed=len(nodes))
    if not nodes:
        return summary

    # Group by endpoint so Cloudflare duplicates are only probed once
    by_endpoint: dict[tuple[str, int], list[NodeCandidate]] = {}
    for n in nodes:
        by_endpoint.setdefault((n.host, n.port), []).append(n)
    summary.unique_endpoints = len(by_endpoint)

    sem = asyncio.Semaphore(max(1, concurrency))
    endpoint_ok: dict[tuple[str, int], ProbeResult] = {}

    async def _one(host: str, port: int, sample: NodeCandidate):
        async with sem:
            ok, latency, err = await tcp_probe(host, port, timeout)
            endpoint_ok[(host, port)] = ProbeResult(
                node=sample, ok=ok, latency_ms=latency, error=err
            )

    await asyncio.gather(*[
        _one(host, port, group[0])
        for (host, port), group in by_endpoint.items()
    ])

    alive: list[NodeCandidate] = []
    results: list[ProbeResult] = []
    for (host, port), group in by_endpoint.items():
        ep = endpoint_ok[(host, port)]
        for node in group:
            pr = ProbeResult(
                node=node,
                ok=ep.ok,
                latency_ms=ep.latency_ms,
                error=ep.error,
            )
            results.append(pr)
            if ep.ok:
                alive.append(node)

    # Sort alive by latency then label
    alive.sort(key=lambda n: (
        next((r.latency_ms or 99999 for r in results if r.node is n), 99999),
        n.label,
    ))
    summary.alive_nodes = alive
    summary.alive = len(alive)
    summary.dead = summary.parsed - summary.alive
    summary.results = results
    return summary


async def check_nodes_proxy(
    nodes: list[NodeCandidate],
    *,
    mihomo_path: str,
    timeout: float = 5.0,
    concurrency: int = 20,
    test_url: str = "http://www.gstatic.com/generate_204",
    batch_size: int = 120,
) -> CheckSummary:
    """Real protocol delay test via local Mihomo (same as client url-test)."""
    from src.mihomo_checker import check_with_mihomo

    summary = CheckSummary(total=len(nodes), parsed=len(nodes))
    summary.unique_endpoints = len({(n.host, n.port) for n in nodes})
    if not nodes:
        return summary

    def _progress(done: int, total: int):
        _safe_print(f"  [mihomo] testing batch starting at {done}/{total}...")

    rows = await check_with_mihomo(
        nodes,
        mihomo_path=mihomo_path,
        timeout=timeout,
        concurrency=concurrency,
        test_url=test_url,
        batch_size=batch_size,
        progress_cb=_progress,
    )

    results: list[ProbeResult] = []
    alive: list[NodeCandidate] = []
    for node, ok, latency, err in rows:
        pr = ProbeResult(node=node, ok=ok, latency_ms=latency, error=err)
        results.append(pr)
        if ok:
            alive.append(node)

    alive.sort(key=lambda n: (
        next((r.latency_ms or 99999 for r in results if r.node is n), 99999),
        n.label,
    ))
    summary.alive_nodes = alive
    summary.alive = len(alive)
    summary.dead = summary.parsed - summary.alive
    summary.results = results
    return summary


def _safe_print(text: str = "") -> None:
    """Print with fallback when console encoding cannot represent characters."""
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def print_alive(summary: CheckSummary, *, show_dead_stats: bool = True) -> None:
    """Print only alive nodes to console (share links / clash refs)."""
    _safe_print(f"\n{'=' * 70}")
    _safe_print(f"{'ALIVE NODES':^70}")
    _safe_print(f"{'=' * 70}")
    _safe_print(
        f"parsed={summary.parsed}  endpoints={summary.unique_endpoints}  "
        f"alive={summary.alive}  dead={summary.dead}"
    )
    _safe_print("-" * 70)

    # Build latency lookup
    latency_map = {
        id(r.node): r.latency_ms
        for r in summary.results
        if r.ok
    }

    if not summary.alive_nodes:
        _safe_print("(no reachable nodes)")
        _safe_print(f"{'=' * 70}")
        return

    for node in summary.alive_nodes:
        ms = latency_map.get(id(node))
        ms_s = f"{ms:.0f}ms" if ms is not None else "?ms"
        label = node.label[:40]
        _safe_print(f"[{ms_s:>7}] {node.host}:{node.port:<5}  {label}")
        # Full usable share link / clash ref on its own line for copy-paste
        if node.raw.startswith(_URI_SCHEMES):
            _safe_print(node.raw)

    _safe_print("-" * 70)
    _safe_print(f"+ {summary.alive} working node(s) printed above")
    if show_dead_stats:
        _safe_print(f"- {summary.dead} failed (delay=-1 / timeout / unsupported)")
    _safe_print(f"{'=' * 70}")


def save_alive(summary: CheckSummary, nodes_dir: str | Path, filename: str = "alive.txt") -> Path | None:
    """Write alive share-links (URI scheme only) to nodes/alive.txt."""
    uris = [
        n.raw for n in summary.alive_nodes
        if n.raw.startswith(_URI_SCHEMES)
    ]
    # Also keep clash-style refs if no URI available
    if not uris:
        uris = [n.raw for n in summary.alive_nodes]

    if not uris:
        return None

    out_dir = Path(nodes_dir)
    out_dir.mkdir(exist_ok=True)
    path = out_dir / filename
    path.write_text("\n".join(uris) + "\n", encoding="utf-8")
    _safe_print(f"  Saved alive nodes -> {path} ({len(uris)} lines)")
    return path


async def run_check(
    nodes_dir: str | Path = "nodes",
    *,
    check: CheckConfig | None = None,
    site: str | None = None,
    source_file: str | None = None,
    prefer_merged: bool = True,
) -> CheckSummary:
    """Load nodes, probe, print alive to console, optionally save."""
    cfg = check or CheckConfig()
    nodes = collect_nodes(
        nodes_dir,
        prefer_merged=prefer_merged,
        site=site,
        source_file=source_file,
    )
    mode = (cfg.mode or "proxy").lower()
    _safe_print(
        f"\n[check] mode={mode} loaded {len(nodes)} node(s) "
        f"(timeout={cfg.timeout}s, concurrency={cfg.concurrency})..."
    )

    if mode == "tcp":
        summary = await check_nodes(
            nodes,
            timeout=cfg.timeout,
            concurrency=cfg.concurrency,
        )
    else:
        _safe_print(f"  mihomo: {cfg.mihomo_path}")
        summary = await check_nodes_proxy(
            nodes,
            mihomo_path=cfg.mihomo_path,
            timeout=cfg.timeout,
            concurrency=cfg.concurrency,
            test_url=cfg.test_url,
            batch_size=cfg.batch_size,
        )
    print_alive(summary)
    if cfg.save_alive:
        alive_name = cfg.alive_file
        # Avoid overwriting the global alive list when checking a single site/source
        if site and alive_name == "alive.txt":
            alive_name = f"alive_{site}.txt"
        elif source_file and alive_name == "alive.txt":
            stem = Path(source_file).stem
            alive_name = f"alive_{stem}.txt"
        save_alive(summary, nodes_dir, alive_name)
    return summary


# ── parsers ──────────────────────────────────────────────────


def _parse_standard_uri(line: str) -> NodeCandidate | None:
    """vless/trojan/hysteria2/anytls/tuic: scheme://user@host:port?...#name"""
    parsed = urlparse(line)
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        # Some links put host in netloc oddly; try regex fallback
        m = re.search(r"@([^@/?#:]+):(\d+)", line)
        if not m:
            m = re.search(r"://([^@/?#:]+):(\d+)", line)
        if not m:
            return None
        host, port = m.group(1), int(m.group(2))
    else:
        port = int(port)

    label = unquote(parsed.fragment) if parsed.fragment else f"{host}:{port}"
    return NodeCandidate(
        host=host,
        port=port,
        label=label,
        raw=line.strip(),
        scheme=(parsed.scheme or "").lower(),
    )


def _parse_vmess(line: str) -> NodeCandidate | None:
    payload = line[8:]
    # strip fragment if present before b64
    if "#" in payload:
        payload = payload.split("#", 1)[0]
    pad = "=" * (-len(payload) % 4)
    data = json.loads(base64.b64decode(payload + pad).decode("utf-8", errors="replace"))
    host = data.get("add") or data.get("host")
    port = data.get("port")
    if not host or not port:
        return None
    label = str(data.get("ps") or f"{host}:{port}")
    return NodeCandidate(
        host=str(host),
        port=int(port),
        label=label,
        raw=line.strip(),
        scheme="vmess",
    )


def _parse_ss(line: str) -> NodeCandidate | None:
    """SIP002 or legacy ss://base64(method:pass@host:port)."""
    rest = line[5:]
    label = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
        label = unquote(frag)

    # SIP002: ss://base64(method:pass)@host:port
    if "@" in rest:
        parsed = urlparse("ss://" + rest)
        host = parsed.hostname
        port = parsed.port
        if host and port:
            return NodeCandidate(
                host=host,
                port=int(port),
                label=label or f"{host}:{port}",
                raw=line.strip(),
                scheme="ss",
            )

    # Legacy: entire userinfo@host:port is base64
    pad = "=" * (-len(rest) % 4)
    try:
        decoded = base64.urlsafe_b64decode(rest + pad).decode("utf-8", errors="replace")
    except Exception:
        decoded = base64.b64decode(rest + pad).decode("utf-8", errors="replace")
    m = re.search(r"@([^@/?#:]+):(\d+)", decoded)
    if not m:
        m = re.search(r"([^@/?#:]+):(\d+)\s*$", decoded)
    if not m:
        return None
    host, port = m.group(1), int(m.group(2))
    return NodeCandidate(
        host=host,
        port=port,
        label=label or f"{host}:{port}",
        raw=line.strip(),
        scheme="ss",
    )


def _parse_ssr(line: str) -> NodeCandidate | None:
    """ssr://base64(host:port:proto:method:obfs:pass_b64/...)"""
    rest = line[6:]
    if "#" in rest:
        rest = rest.split("#", 1)[0]
    pad = "=" * (-len(rest) % 4)
    try:
        decoded = base64.urlsafe_b64decode(rest + pad).decode("utf-8", errors="replace")
    except Exception:
        return None
    parts = decoded.split(":")
    if len(parts) < 2:
        return None
    host = parts[0]
    try:
        port = int(parts[1])
    except ValueError:
        return None
    return NodeCandidate(
        host=host,
        port=port,
        label=f"{host}:{port}",
        raw=line.strip(),
        scheme="ssr",
    )


def _dedup_candidates(nodes: list[NodeCandidate]) -> list[NodeCandidate]:
    seen: set[str] = set()
    out: list[NodeCandidate] = []
    for n in nodes:
        key = n.raw if n.raw.startswith(_URI_SCHEMES) else f"{n.host}:{n.port}:{n.scheme}:{n.label}"
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out
