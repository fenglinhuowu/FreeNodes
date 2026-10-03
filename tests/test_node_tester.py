"""Tests for node_tester: URI parsing, clash parsing, collect, and TCP probe."""
import asyncio
from pathlib import Path

import pytest

from src.node_tester import (
    CheckConfig,
    NodeCandidate,
    check_nodes,
    collect_nodes,
    load_nodes_from_txt,
    parse_clash_proxy,
    parse_uri,
    print_alive,
    save_alive,
    tcp_probe,
)


class TestParseUri:

    def test_vless(self):
        uri = "vless://uuid@1.2.3.4:443?encryption=none&type=tcp#HK-01"
        n = parse_uri(uri)
        assert n is not None
        assert n.host == "1.2.3.4"
        assert n.port == 443
        assert n.label == "HK-01"
        assert n.scheme == "vless"

    def test_trojan(self):
        uri = "trojan://pass@example.com:8443?security=tls#JP"
        n = parse_uri(uri)
        assert n is not None
        assert n.host == "example.com"
        assert n.port == 8443

    def test_vmess(self):
        import base64
        import json
        payload = base64.b64encode(json.dumps({
            "v": "2", "ps": "US-1", "add": "8.8.8.8", "port": "443",
            "id": "x", "aid": "0", "net": "tcp", "type": "none",
        }).encode()).decode()
        n = parse_uri(f"vmess://{payload}")
        assert n is not None
        assert n.host == "8.8.8.8"
        assert n.port == 443
        assert n.label == "US-1"

    def test_ss_sip002(self):
        uri = "ss://YWVzLTI1Ni1nY206cGFzcw@10.0.0.1:8388#ss-node"
        n = parse_uri(uri)
        assert n is not None
        assert n.host == "10.0.0.1"
        assert n.port == 8388
        assert n.label == "ss-node"

    def test_comment_and_empty(self):
        assert parse_uri("# comment") is None
        assert parse_uri("") is None
        assert parse_uri("http://example.com") is None


class TestParseClash:

    def test_basic(self):
        n = parse_clash_proxy({
            "name": "HK",
            "type": "trojan",
            "server": "1.1.1.1",
            "port": 443,
        })
        assert n is not None
        assert n.host == "1.1.1.1"
        assert n.port == 443
        assert n.label == "HK"

    def test_missing_server(self):
        assert parse_clash_proxy({"name": "x", "port": 443}) is None


class TestCollectAndCheck:

    def test_load_txt(self, tmp_path: Path):
        f = tmp_path / "a.txt"
        f.write_text(
            "vless://u@1.1.1.1:443#A\n"
            "trojan://p@2.2.2.2:8443#B\n"
            "# skip\n",
            encoding="utf-8",
        )
        nodes = load_nodes_from_txt(f)
        assert len(nodes) == 2

    def test_collect_site(self, tmp_path: Path):
        (tmp_path / "foo.txt").write_text(
            "vless://u@9.9.9.9:443#X\n", encoding="utf-8"
        )
        nodes = collect_nodes(tmp_path, site="foo")
        assert len(nodes) == 1
        assert nodes[0].host == "9.9.9.9"

    @pytest.mark.asyncio
    async def test_tcp_probe_localhost_closed(self):
        # High unlikely-open port
        ok, latency, err = await tcp_probe("127.0.0.1", 1, timeout=0.5)
        assert ok is False
        assert err

    @pytest.mark.asyncio
    async def test_check_nodes_marks_dead(self):
        nodes = [
            NodeCandidate(host="127.0.0.1", port=1, label="dead", raw="vless://u@127.0.0.1:1#dead", scheme="vless"),
        ]
        summary = await check_nodes(nodes, timeout=0.3, concurrency=10)
        assert summary.parsed == 1
        assert summary.alive == 0
        assert summary.dead == 1

    def test_save_alive(self, tmp_path: Path):
        from src.node_tester import CheckSummary
        summary = CheckSummary(
            alive_nodes=[
                NodeCandidate(
                    host="1.1.1.1", port=443, label="A",
                    raw="vless://u@1.1.1.1:443#A", scheme="vless",
                )
            ]
        )
        path = save_alive(summary, tmp_path)
        assert path is not None
        assert "vless://" in path.read_text(encoding="utf-8")
