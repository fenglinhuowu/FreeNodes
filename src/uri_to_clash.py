"""Convert share-link URIs into Clash Meta / Mihomo proxy dicts."""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import parse_qs, unquote, urlparse


def uri_to_clash(uri: str, name: str | None = None) -> dict | None:
    """Convert a single share URI to a Clash proxy dict, or None if unsupported."""
    uri = (uri or "").strip()
    if not uri or uri.startswith("#"):
        return None
    lower = uri.lower()
    try:
        if lower.startswith("vmess://"):
            return _vmess(uri, name)
        if lower.startswith("ss://"):
            return _ss(uri, name)
        if lower.startswith("trojan://"):
            return _trojan(uri, name)
        if lower.startswith("vless://"):
            return _vless(uri, name)
        if lower.startswith("hysteria2://") or lower.startswith("hy2://"):
            return _hysteria2(uri, name)
        if lower.startswith("tuic://"):
            return _tuic(uri, name)
    except Exception:
        return None
    return None


def _b64decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad)
    except Exception:
        return base64.b64decode(data + pad)


def _qs(parsed) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in parse_qs(parsed.query, keep_blank_values=True).items():
        out[k] = v[0] if v else ""
    return out


def _name_from(uri: str, parsed, fallback: str, override: str | None) -> str:
    if override:
        return override
    if parsed.fragment:
        return unquote(parsed.fragment)
    return fallback


def _vmess(uri: str, name: str | None) -> dict | None:
    payload = uri[8:]
    if "#" in payload:
        payload, frag = payload.split("#", 1)
        default_name = unquote(frag)
    else:
        default_name = None
    data = json.loads(_b64decode(payload).decode("utf-8", errors="replace"))
    host = data.get("add") or data.get("host")
    port = data.get("port")
    if not host or not port:
        return None
    proxy = {
        "name": name or data.get("ps") or default_name or f"vmess-{host}:{port}",
        "type": "vmess",
        "server": str(host),
        "port": int(port),
        "uuid": str(data.get("id") or ""),
        "alterId": int(data.get("aid") or 0),
        "cipher": data.get("scy") or "auto",
        "udp": True,
    }
    net = (data.get("net") or "tcp").lower()
    proxy["network"] = net
    tls = str(data.get("tls") or "").lower()
    if tls in ("tls", "1", "true"):
        proxy["tls"] = True
        if data.get("sni"):
            proxy["servername"] = data["sni"]
        elif data.get("host"):
            proxy["servername"] = data["host"]
    if data.get("fp"):
        proxy["client-fingerprint"] = data["fp"]
    if net == "ws":
        opts: dict = {}
        if data.get("path"):
            opts["path"] = data["path"]
        host_h = data.get("host")
        if host_h:
            opts["headers"] = {"Host": host_h}
        if opts:
            proxy["ws-opts"] = opts
    elif net == "grpc":
        if data.get("path"):
            proxy["grpc-opts"] = {"grpc-service-name": data["path"]}
    if str(data.get("skip-cert-verify") or data.get("insecure") or "") in ("1", "true", "True"):
        proxy["skip-cert-verify"] = True
    return proxy


def _ss(uri: str, name: str | None) -> dict | None:
    rest = uri[5:]
    label = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
        label = unquote(frag)

    method = password = host = None
    port = None

    if "@" in rest:
        # SIP002: ss://base64(method:pass)@host:port
        userinfo, hostport = rest.rsplit("@", 1)
        try:
            decoded = _b64decode(userinfo).decode("utf-8", errors="replace")
            method, password = decoded.split(":", 1)
        except Exception:
            # plain method:pass
            if ":" in userinfo:
                method, password = unquote(userinfo).split(":", 1)
        parsed = urlparse("//" + hostport)
        host = parsed.hostname
        port = parsed.port
    else:
        decoded = _b64decode(rest).decode("utf-8", errors="replace")
        # method:pass@host:port
        m = re.match(r"^(?P<method>[^:]+):(?P<pass>.+)@(?P<host>[^@]+):(?P<port>\d+)$", decoded)
        if not m:
            return None
        method, password, host, port = m.group("method"), m.group("pass"), m.group("host"), int(m.group("port"))

    if not all([method, password, host, port]):
        return None
    return {
        "name": name or label or f"ss-{host}:{port}",
        "type": "ss",
        "server": host,
        "port": int(port),
        "cipher": method,
        "password": password,
        "udp": True,
    }


def _trojan(uri: str, name: str | None) -> dict | None:
    parsed = urlparse(uri)
    host = parsed.hostname
    port = parsed.port
    password = unquote(parsed.username or "")
    if not host or not port or not password:
        return None
    q = _qs(parsed)
    proxy = {
        "name": _name_from(uri, parsed, f"trojan-{host}:{port}", name),
        "type": "trojan",
        "server": host,
        "port": int(port),
        "password": password,
        "udp": True,
    }
    if q.get("sni") or q.get("peer"):
        proxy["sni"] = q.get("sni") or q.get("peer")
    if q.get("allowInsecure") in ("1", "true") or q.get("insecure") in ("1", "true"):
        proxy["skip-cert-verify"] = True
    network = (q.get("type") or "tcp").lower()
    if network == "ws":
        proxy["network"] = "ws"
        opts: dict = {}
        if q.get("path"):
            opts["path"] = q["path"]
        if q.get("host"):
            opts["headers"] = {"Host": q["host"]}
        if opts:
            proxy["ws-opts"] = opts
    if q.get("fp"):
        proxy["client-fingerprint"] = q["fp"]
    return proxy


def _vless(uri: str, name: str | None) -> dict | None:
    parsed = urlparse(uri)
    host = parsed.hostname
    port = parsed.port
    uuid = unquote(parsed.username or "")
    if not host or not port or not uuid:
        return None
    q = _qs(parsed)
    proxy = {
        "name": _name_from(uri, parsed, f"vless-{host}:{port}", name),
        "type": "vless",
        "server": host,
        "port": int(port),
        "uuid": uuid,
        "udp": True,
    }
    security = (q.get("security") or "none").lower()
    if security in ("tls", "reality"):
        proxy["tls"] = True
        if q.get("sni"):
            proxy["servername"] = q["sni"]
        if q.get("fp"):
            proxy["client-fingerprint"] = q["fp"]
        if q.get("alpn"):
            proxy["alpn"] = q["alpn"].split(",")
        if security == "reality":
            proxy["reality-opts"] = {
                "public-key": q.get("pbk") or "",
                "short-id": q.get("sid") or "",
            }
    network = (q.get("type") or "tcp").lower()
    if network in ("raw",):
        network = "tcp"
    proxy["network"] = network
    if network == "ws":
        opts = {}
        if q.get("path"):
            opts["path"] = q["path"]
        if q.get("host"):
            opts["headers"] = {"Host": q["host"]}
        if opts:
            proxy["ws-opts"] = opts
    elif network == "grpc":
        if q.get("serviceName") or q.get("path"):
            proxy["grpc-opts"] = {"grpc-service-name": q.get("serviceName") or q.get("path")}
    if q.get("flow"):
        proxy["flow"] = q["flow"]
    if q.get("allowInsecure") in ("1", "true") or q.get("insecure") in ("1", "true"):
        proxy["skip-cert-verify"] = True
    if q.get("packetEncoding"):
        proxy["packet-encoding"] = q["packetEncoding"]
    return proxy


def _hysteria2(uri: str, name: str | None) -> dict | None:
    # hysteria2://password@host:port?params#name
    parsed = urlparse(uri.replace("hy2://", "hysteria2://", 1))
    host = parsed.hostname
    port = parsed.port
    password = unquote(parsed.username or "")
    if not host or not port:
        return None
    q = _qs(parsed)
    proxy = {
        "name": _name_from(uri, parsed, f"hy2-{host}:{port}", name),
        "type": "hysteria2",
        "server": host,
        "port": int(port),
        "password": password,
    }
    if q.get("sni"):
        proxy["sni"] = q["sni"]
    if q.get("obfs"):
        proxy["obfs"] = q["obfs"]
    if q.get("obfs-password"):
        proxy["obfs-password"] = q["obfs-password"]
    if q.get("allowInsecure") in ("1", "true") or q.get("insecure") in ("1", "true"):
        proxy["skip-cert-verify"] = True
    return proxy


def _tuic(uri: str, name: str | None) -> dict | None:
    # tuic://uuid:password@host:port?params#name
    parsed = urlparse(uri)
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        return None
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    q = _qs(parsed)
    proxy = {
        "name": _name_from(uri, parsed, f"tuic-{host}:{port}", name),
        "type": "tuic",
        "server": host,
        "port": int(port),
        "uuid": user,
        "password": password,
    }
    if q.get("sni"):
        proxy["sni"] = q["sni"]
    if q.get("alpn"):
        proxy["alpn"] = q["alpn"].split(",")
    if q.get("congestion_control"):
        proxy["congestion-controller"] = q["congestion_control"]
    if q.get("udp_relay_mode"):
        proxy["udp-relay-mode"] = q["udp_relay_mode"]
    if q.get("allowInsecure") in ("1", "true") or q.get("insecure") in ("1", "true"):
        proxy["skip-cert-verify"] = True
    return proxy
