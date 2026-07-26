from __future__ import annotations

import atexit
import json
import select
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from typing import Any, Optional
from urllib.parse import unquote, urlsplit, urlunsplit

MAILBOX_PROXY_BYPASS_CONFIG = {"http": None, "https": None, "all": None}
_BROWSER_PROXY_BRIDGE_BIND_HOST = "0.0.0.0"
_BROWSER_PROXY_BRIDGE_PUBLIC_HOST = "127.0.0.1"
_BROWSER_PROXY_BRIDGE_TIMEOUT_SECONDS = 30.0
_BROWSER_PROXY_BRIDGES: dict[str, dict[str, Any]] = {}
_BROWSER_PROXY_BRIDGE_LOCK = threading.Lock()


def _is_auth_socks_proxy(scheme: str, username: str, password: str) -> bool:
    normalized = (scheme or "").lower()
    return normalized in {"socks5", "socks5h"} and bool(username or password)


class _BrowserProxyBridgeServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_cls, *, upstream: dict[str, Any]):
        super().__init__(server_address, handler_cls)
        self.upstream = dict(upstream)

    def open_upstream_socket(self, host: str, port: int) -> socket.socket:
        import socks

        proxy_type = socks.SOCKS5
        conn = socks.socksocket()
        conn.settimeout(_BROWSER_PROXY_BRIDGE_TIMEOUT_SECONDS)
        conn.set_proxy(
            proxy_type=proxy_type,
            addr=self.upstream["host"],
            port=self.upstream["port"],
            username=self.upstream.get("username") or None,
            password=self.upstream.get("password") or None,
            rdns=bool(self.upstream.get("rdns")),
        )
        conn.connect((host, port))
        return conn


class _BrowserProxyBridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self) -> None:
        try:
            super().handle()
        except OSError:
            return

    def log_message(self, format: str, *args) -> None:
        return

    def _split_host_port(self, target: str, default_port: int) -> tuple[str, int]:
        value = str(target or "").strip()
        if not value:
            raise ValueError("missing target host")
        if value.startswith("["):
            host, _, tail = value[1:].partition("]")
            if not host:
                raise ValueError("invalid ipv6 host")
            if tail.startswith(":"):
                return host, int(tail[1:])
            return host, default_port
        host, sep, port_text = value.rpartition(":")
        if not sep or not host or ":" in host:
            return value, default_port
        try:
            return host, int(port_text)
        except ValueError:
            return value, default_port

    def _relay_tunnel(self, upstream: socket.socket) -> None:
        sockets = [self.connection, upstream]
        try:
            while True:
                readable, _, _ = select.select(sockets, [], [], _BROWSER_PROXY_BRIDGE_TIMEOUT_SECONDS)
                if not readable:
                    break
                for current in readable:
                    try:
                        chunk = current.recv(65536)
                    except OSError:
                        return
                    if not chunk:
                        return
                    peer = upstream if current is self.connection else self.connection
                    try:
                        peer.sendall(chunk)
                    except OSError:
                        return
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def _proxy_http_request(self) -> None:
        target = urlsplit(self.path)
        host = str(target.hostname or self.headers.get("Host") or "").strip()
        if not host:
            self.send_error(400, "Missing target host")
            return
        port = int(target.port or 80)
        path = target.path or "/"
        if target.query:
            path = f"{path}?{target.query}"
        body = b""
        content_length = str(self.headers.get("Content-Length") or "").strip()
        if content_length:
            body = self.rfile.read(int(content_length))
        headers = []
        for key, value in self.headers.items():
            lowered = key.lower()
            if lowered in {"proxy-connection", "proxy-authorization"}:
                continue
            if lowered == "connection":
                continue
            headers.append((key, value))
        headers.append(("Connection", "close"))

        upstream = self.server.open_upstream_socket(host, port)
        try:
            request_head = [f"{self.command} {path} HTTP/1.1"]
            request_head.extend(f"{key}: {value}" for key, value in headers)
            request_head.append("")
            request_head.append("")
            upstream.sendall("\r\n".join(request_head).encode("utf-8"))
            if body:
                upstream.sendall(body)
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                self.connection.sendall(chunk)
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def do_CONNECT(self) -> None:
        try:
            host, port = self._split_host_port(self.path, 443)
            upstream = self.server.open_upstream_socket(host, port)
        except Exception as exc:
            self.send_error(502, str(exc))
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self._relay_tunnel(upstream)

    def do_GET(self) -> None:
        self._proxy_http_request()

    def do_POST(self) -> None:
        self._proxy_http_request()

    def do_PUT(self) -> None:
        self._proxy_http_request()

    def do_DELETE(self) -> None:
        self._proxy_http_request()

    def do_HEAD(self) -> None:
        self._proxy_http_request()

    def do_OPTIONS(self) -> None:
        self._proxy_http_request()

    def do_PATCH(self) -> None:
        self._proxy_http_request()


def _cleanup_browser_proxy_bridges() -> None:
    with _BROWSER_PROXY_BRIDGE_LOCK:
        bridges = list(_BROWSER_PROXY_BRIDGES.values())
        _BROWSER_PROXY_BRIDGES.clear()
    for entry in bridges:
        server = entry.get("server")
        if server is None:
            continue
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass


atexit.register(_cleanup_browser_proxy_bridges)


def _build_browser_proxy_bridge_key(parts) -> str:
    scheme = (parts.scheme or "").lower()
    username = unquote(parts.username or "")
    password = unquote(parts.password or "")
    host = str(parts.hostname or "").strip().lower()
    port = int(parts.port or 0)
    return f"{scheme}|{username}|{password}|{host}|{port}"


def _get_or_start_browser_proxy_bridge(proxy_url: str) -> str:
    parts = urlsplit(str(proxy_url or "").strip())
    if not parts.scheme or not parts.hostname or parts.port is None:
        raise ValueError(f"Invalid proxy url: {proxy_url!r}")
    key = _build_browser_proxy_bridge_key(parts)
    with _BROWSER_PROXY_BRIDGE_LOCK:
        existing = _BROWSER_PROXY_BRIDGES.get(key)
        if existing:
            return str(existing["server_url"])

        upstream = {
            "host": str(parts.hostname or "").strip(),
            "port": int(parts.port),
            "username": unquote(parts.username or ""),
            "password": unquote(parts.password or ""),
            "rdns": (parts.scheme or "").lower() == "socks5h",
        }
        server = _BrowserProxyBridgeServer(
            (_BROWSER_PROXY_BRIDGE_BIND_HOST, 0),
            _BrowserProxyBridgeHandler,
            upstream=upstream,
        )
        port = int(server.server_address[1])
        thread = threading.Thread(
            target=server.serve_forever,
            name=f"browser-proxy-bridge-{port}",
            daemon=True,
        )
        thread.start()
        server_url = f"http://{_BROWSER_PROXY_BRIDGE_PUBLIC_HOST}:{port}"
        _BROWSER_PROXY_BRIDGES[key] = {
            "server": server,
            "thread": thread,
            "server_url": server_url,
        }
        return server_url


def is_authenticated_socks5_proxy(proxy_url: Optional[str]) -> bool:
    if not proxy_url:
        return False

    value = str(proxy_url).strip()
    if not value:
        return False

    if value.startswith("{"):
        try:
            data = json.loads(value)
            if isinstance(data, dict):
                server = str(data.get("server") or "").strip()
                if not server:
                    return False
                scheme = (urlsplit(server).scheme or "").lower()
                username = str(data.get("username") or "").strip()
                password = str(data.get("password") or "").strip()
                return _is_auth_socks_proxy(scheme, username, password)
        except Exception:
            return False

    parts = urlsplit(value)
    return _is_auth_socks_proxy(
        parts.scheme or "",
        unquote(parts.username or ""),
        unquote(parts.password or ""),
    )


def normalize_proxy_url(proxy_url: Optional[str]) -> Optional[str]:
    """将 socks5:// 规范化为 socks5h://，避免本地 DNS 泄漏。"""
    if proxy_url is None:
        return None

    value = str(proxy_url).strip()
    if not value:
        return None

    parts = urlsplit(value)
    if (parts.scheme or "").lower() == "socks5":
        parts = parts._replace(scheme="socks5h")
        return urlunsplit(parts)
    return value


def build_requests_proxy_config(proxy_url: Optional[str]) -> Optional[dict[str, str]]:
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def build_mailbox_proxy_config(
    proxy_url: Optional[str],
) -> Optional[dict[str, Optional[str]]]:
    """Mailbox provider HTTP requests should bypass configured and env proxies."""
    _ = proxy_url
    return dict(MAILBOX_PROXY_BYPASS_CONFIG)


def create_mailbox_requests_session(
    proxy_config: Optional[dict[str, Optional[str]]] = None,
):
    import requests

    session = requests.Session()
    session.trust_env = False
    if proxy_config is not None:
        session.proxies = dict(proxy_config)
    return session


def build_playwright_proxy_config(proxy_url: Optional[str]) -> Optional[dict[str, str]]:
    if not proxy_url:
        return None

    value = str(proxy_url).strip()
    if not value:
        return None
    parts = urlsplit(value)
    if not parts.scheme or not parts.hostname or parts.port is None:
        server = value
        if server.startswith("socks5h://"):
            server = "socks5://" + server[len("socks5h://") :]
        return {"server": server}

    scheme = (parts.scheme or "").lower()
    if _is_auth_socks_proxy(scheme, parts.username or "", parts.password or ""):
        return {"server": _get_or_start_browser_proxy_bridge(value)}
    if scheme == "socks5h":
        scheme = "socks5"

    config = {"server": f"{scheme}://{parts.hostname}:{parts.port}"}
    if parts.username:
        config["username"] = unquote(parts.username)
    if parts.password:
        config["password"] = unquote(parts.password)
    return config
