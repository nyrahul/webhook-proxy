#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import logging
import re
import ssl
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

import yaml


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

MISSING = object()
PATH_TOKEN_RE = re.compile(r"\.([A-Za-z0-9_-]+)|\[([0-9]+)\]")


@dataclass(frozen=True)
class EndpointConfig:
    path: str
    remote: str
    send_query_params: bool
    only_send_defined_fields: bool
    content_body: dict[str, str]


@dataclass(frozen=True)
class ProxyConfig:
    listen_host: str
    listen_port: int
    endpoints: dict[str, EndpointConfig]


def parse_dot_path(path: str) -> list[str | int]:
    if not path or path[0] != ".":
        raise ValueError(f"JSON path must start with '.': {path!r}")

    tokens: list[str | int] = []
    pos = 0
    while pos < len(path):
        match = PATH_TOKEN_RE.match(path, pos)
        if not match:
            raise ValueError(f"Invalid JSON path near {path[pos:]!r} in {path!r}")
        key, index = match.groups()
        tokens.append(key if key is not None else int(index))
        pos = match.end()
    return tokens


def get_path(value: Any, path: str) -> Any:
    current = value
    for token in parse_dot_path(path):
        if isinstance(token, int):
            if not isinstance(current, list) or token >= len(current):
                return MISSING
            current = current[token]
        else:
            if not isinstance(current, dict) or token not in current:
                return MISSING
            current = current[token]
    return current


def set_path(target: dict[str, Any], path: str, value: Any) -> None:
    tokens = parse_dot_path(path)
    if any(isinstance(token, int) for token in tokens):
        raise ValueError(f"Destination paths with array indexes are not supported: {path!r}")

    current: dict[str, Any] = target
    for token in tokens[:-1]:
        child = current.setdefault(str(token), {})
        if not isinstance(child, dict):
            child = {}
            current[str(token)] = child
        current = child
    current[str(tokens[-1])] = value


def transform_json_body(body: bytes, endpoint: EndpointConfig) -> tuple[bytes, str]:
    if not body:
        return body, ""

    parsed = json.loads(body.decode("utf-8"))
    if endpoint.only_send_defined_fields:
        transformed: dict[str, Any] = {}
    else:
        transformed = copy.deepcopy(parsed) if isinstance(parsed, dict) else {"body": parsed}

    for destination_path, source_path in endpoint.content_body.items():
        mapped_value = get_path(parsed, source_path)
        if mapped_value is not MISSING:
            set_path(transformed, destination_path, mapped_value)

    return json.dumps(transformed, separators=(",", ":")).encode("utf-8"), "application/json"


def normalize_json_path(path: Any) -> str:
    normalized = str(path)
    if normalized.startswith("."):
        return normalized
    return f".{normalized}"


def parse_listen_address(value: Any) -> tuple[str, int]:
    if not isinstance(value, str) or not value:
        raise ValueError("listen-address must be a non-empty string")

    if value.startswith("["):
        host_end = value.find("]")
        if host_end == -1 or len(value) <= host_end + 2 or value[host_end + 1] != ":":
            raise ValueError("listen-address must be in host:port format")
        host = value[1:host_end]
        port_text = value[host_end + 2 :]
    else:
        if ":" not in value:
            raise ValueError("listen-address must be in host:port format")
        host, port_text = value.rsplit(":", 1)

    if not host:
        raise ValueError("listen-address host must not be empty")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("listen-address port must be an integer") from exc
    if port < 0 or port > 65535:
        raise ValueError("listen-address port must be between 0 and 65535")
    return host, port


def load_config(path: str) -> ProxyConfig:
    with open(path, "r", encoding="utf-8") as config_file:
        raw = yaml.safe_load(config_file) or {}

    endpoint_configs = raw.get("endpoint-config")
    if not isinstance(endpoint_configs, dict):
        raise ValueError("Config must contain an 'endpoint-config' mapping")

    listen_host = "127.0.0.1"
    listen_port = 8080
    if "listen-address" in raw:
        listen_host, listen_port = parse_listen_address(raw["listen-address"])

    loaded: dict[str, EndpointConfig] = {}
    for endpoint_path, endpoint_raw in endpoint_configs.items():
        if not isinstance(endpoint_raw, dict):
            raise ValueError(f"Endpoint config for {endpoint_path!r} must be a mapping")
        remote = endpoint_raw.get("remote")
        if not isinstance(remote, str) or not remote:
            raise ValueError(f"Endpoint config for {endpoint_path!r} requires a non-empty remote")
        content_body = endpoint_raw.get("content-body") or {}
        if not isinstance(content_body, dict):
            raise ValueError(f"content-body for {endpoint_path!r} must be a mapping")

        loaded[str(endpoint_path)] = EndpointConfig(
            path=str(endpoint_path),
            remote=remote,
            send_query_params=bool(endpoint_raw.get("send-query-params", False)),
            only_send_defined_fields=bool(endpoint_raw.get("only-send-defined-fields", True)),
            content_body={normalize_json_path(k): normalize_json_path(v) for k, v in content_body.items()},
        )

    return ProxyConfig(listen_host=listen_host, listen_port=listen_port, endpoints=loaded)


def build_upstream_url(remote: str, incoming_path: str, query: str, send_query: bool) -> str:
    remote_parts = urlsplit(remote)
    base_path = remote_parts.path.rstrip("/")
    path = f"{base_path}/{incoming_path.lstrip('/')}"
    return urlunsplit(
        (
            remote_parts.scheme,
            remote_parts.netloc,
            path,
            query if send_query else "",
            "",
        )
    )


def filtered_headers(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in handler.headers.items():
        if key.lower() not in HOP_BY_HOP_HEADERS:
            headers[key] = value
    return headers


def make_handler(
    endpoint_configs: dict[str, EndpointConfig],
    verbose: bool,
    timeout: float,
) -> type[BaseHTTPRequestHandler]:
    class WebhookProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self.proxy()

        def do_POST(self) -> None:
            self.proxy()

        def do_PUT(self) -> None:
            self.proxy()

        def do_PATCH(self) -> None:
            self.proxy()

        def do_DELETE(self) -> None:
            self.proxy()

        def do_HEAD(self) -> None:
            self.proxy()

        def do_OPTIONS(self) -> None:
            self.proxy()

        def proxy(self) -> None:
            request_parts = urlsplit(self.path)
            endpoint = endpoint_configs.get(request_parts.path)
            if endpoint is None:
                self.send_error(404, f"No endpoint configured for {request_parts.path}")
                return

            content_length = int(self.headers.get("Content-Length", "0") or "0")
            incoming_body = self.rfile.read(content_length) if content_length else b""
            upstream_body = incoming_body
            content_type = self.headers.get("Content-Type", "")
            upstream_content_type = ""

            is_json_request = "application/json" in content_type.lower()
            should_transform_body = incoming_body and (is_json_request or endpoint.content_body)
            if should_transform_body:
                try:
                    upstream_body, upstream_content_type = transform_json_body(incoming_body, endpoint)
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                    self.send_error(400, f"Invalid JSON body: {exc}")
                    return

            upstream_url = build_upstream_url(
                endpoint.remote,
                request_parts.path,
                request_parts.query,
                endpoint.send_query_params,
            )
            headers = filtered_headers(self)
            if upstream_content_type:
                headers["Content-Type"] = upstream_content_type

            if verbose:
                log_request(self, incoming_body, upstream_url, upstream_body)

            request = Request(
                upstream_url,
                data=None if self.command in {"GET", "HEAD"} else upstream_body,
                headers=headers,
                method=self.command,
            )

            try:
                with urlopen(request, timeout=timeout) as response:
                    response_body = b"" if self.command == "HEAD" else response.read()
                    self.send_response(response.status, response.reason)
                    self.copy_response_headers(response.headers.items(), len(response_body))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(response_body)
                    if verbose:
                        log_response(response.status, response.reason, response.headers.items(), response_body)
            except HTTPError as exc:
                response_body = b"" if self.command == "HEAD" else exc.read()
                self.send_response(exc.code, exc.reason)
                self.copy_response_headers(exc.headers.items(), len(response_body))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(response_body)
                if verbose:
                    log_response(exc.code, exc.reason, exc.headers.items(), response_body)
            except URLError as exc:
                logging.exception("Upstream request failed")
                self.send_error(502, f"Upstream request failed: {exc.reason}")

        def copy_response_headers(self, headers: Any, body_length: int) -> None:
            for key, value in headers:
                if key.lower() not in HOP_BY_HOP_HEADERS:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(body_length))

        def log_message(self, fmt: str, *args: Any) -> None:
            logging.info("%s - %s", self.address_string(), fmt % args)

    return WebhookProxyHandler


def decode_for_log(body: bytes) -> str:
    if not body:
        return ""
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return repr(body)


def log_request(
    handler: BaseHTTPRequestHandler,
    incoming_body: bytes,
    upstream_url: str,
    upstream_body: bytes,
) -> None:
    logging.info("Incoming request: %s %s", handler.command, handler.path)
    logging.info("Incoming headers: %s", dict(handler.headers.items()))
    logging.info("Incoming body: %s", decode_for_log(incoming_body))
    logging.info("Upstream request: %s %s", handler.command, upstream_url)
    logging.info("Upstream body: %s", decode_for_log(upstream_body))


def log_response(status: int, reason: str, headers: Any, body: bytes) -> None:
    logging.info("Upstream response: %s %s", status, reason)
    logging.info("Upstream response headers: %s", dict(headers))
    logging.info("Upstream response body: %s", decode_for_log(body))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a JSON-transforming webhook proxy.")
    parser.add_argument("--config", required=True, help="Path to YAML config file.")
    parser.add_argument("--host", help="Host to bind. Overrides config listen-address host.")
    parser.add_argument("--port", type=int, help="Port to bind. Overrides config listen-address port.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Upstream timeout in seconds.")
    parser.add_argument("--verbose", action="store_true", help="Print request/body and response/body details.")
    parser.add_argument("--cert-file", help="TLS certificate file for serving HTTPS.")
    parser.add_argument("--key-file", help="TLS private key file for serving HTTPS.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        config = load_config(args.config)
    except Exception as exc:
        logging.error("Could not load config: %s", exc)
        return 2
    if bool(args.cert_file) != bool(args.key_file):
        logging.error("--cert-file and --key-file must be provided together")
        return 2

    host = args.host if args.host is not None else config.listen_host
    port = args.port if args.port is not None else config.listen_port
    handler = make_handler(config.endpoints, args.verbose, args.timeout)
    server = ThreadingHTTPServer((host, port), handler)
    scheme = "http"
    if args.cert_file and args.key_file:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=args.cert_file, keyfile=args.key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"

    logging.info("Listening on %s://%s:%s", scheme, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
