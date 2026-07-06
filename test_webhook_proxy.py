import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from webhook_proxy import EndpointConfig, load_config, make_handler, parse_listen_address, transform_json_body


class CaptureHandler(BaseHTTPRequestHandler):
    captured = {}

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        CaptureHandler.captured = {
            "method": self.command,
            "path": self.path,
            "body": body,
        }
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, fmt, *args):
        pass


class WebhookProxyTests(unittest.TestCase):
    def write_config(self, contents):
        config_file = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        try:
            config_file.write(contents)
            return config_file.name
        finally:
            config_file.close()

    def test_parse_listen_address(self):
        self.assertEqual(parse_listen_address("0.0.0.0:8080"), ("0.0.0.0", 8080))
        self.assertEqual(parse_listen_address("[::1]:8443"), ("::1", 8443))

    def test_load_config_reads_listen_address(self):
        config_path = self.write_config(
            """
listen-address: "0.0.0.0:9090"
endpoint-config:
  /hook:
    remote: http://example.test/
"""
        )
        try:
            config = load_config(config_path)
        finally:
            os.unlink(config_path)

        self.assertEqual(config.listen_host, "0.0.0.0")
        self.assertEqual(config.listen_port, 9090)
        self.assertEqual(set(config.endpoints), {"/hook"})

    def test_load_config_uses_default_listen_address(self):
        config_path = self.write_config(
            """
endpoint-config:
  /hook:
    remote: http://example.test/
"""
        )
        try:
            config = load_config(config_path)
        finally:
            os.unlink(config_path)

        self.assertEqual(config.listen_host, "127.0.0.1")
        self.assertEqual(config.listen_port, 8080)

    def test_load_config_reads_only_send_defined_fields(self):
        config_path = self.write_config(
            """
endpoint-config:
  /hook:
    remote: http://example.test/
    only-send-defined-fields: false
"""
        )
        try:
            config = load_config(config_path)
        finally:
            os.unlink(config_path)

        self.assertFalse(config.endpoints["/hook"].only_send_defined_fields)

    def test_load_config_normalizes_content_body_field_names(self):
        config_path = self.write_config(
            """
endpoint-config:
  /hook:
    remote: http://example.test/
    content-body:
      "ContainerName": "ContainerName"
      ".ProcessName": ".process"
"""
        )
        try:
            config = load_config(config_path)
        finally:
            os.unlink(config_path)

        self.assertEqual(
            config.endpoints["/hook"].content_body,
            {".ContainerName": ".ContainerName", ".ProcessName": ".process"},
        )

    def test_transform_only_defined_fields(self):
        endpoint = EndpointConfig(
            path="/hook",
            remote="http://example.test/",
            send_query_params=True,
            only_send_defined_fields=True,
            content_body={
                ".ContainerName": ".container.name",
                ".ProcessName": ".process",
            },
        )
        body, content_type = transform_json_body(
            b'{"container":{"name":"nginx","image":"nginx:latest"},"process":"bash","extra":true}',
            endpoint,
        )
        self.assertEqual(content_type, "application/json")
        self.assertEqual(json.loads(body), {"ContainerName": "nginx", "ProcessName": "bash"})

    def test_transform_keeps_all_fields_when_not_only_defined(self):
        endpoint = EndpointConfig(
            path="/hook",
            remote="http://example.test/",
            send_query_params=True,
            only_send_defined_fields=False,
            content_body={".ProcessName": ".process"},
        )
        body, _ = transform_json_body(b'{"container":{"name":"nginx"},"process":"bash"}', endpoint)
        self.assertEqual(
            json.loads(body),
            {"container": {"name": "nginx"}, "process": "bash", "ProcessName": "bash"},
        )

    def test_proxy_forwards_method_path_query_transformed_body_and_raw_response(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()

        endpoint = EndpointConfig(
            path="/hook",
            remote=f"http://127.0.0.1:{upstream.server_port}/",
            send_query_params=True,
            only_send_defined_fields=True,
            content_body={".ProcessName": ".process"},
        )
        proxy = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler({"/hook": endpoint}, verbose=False, timeout=5),
        )
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        proxy_thread.start()

        try:
            request = Request(
                f"http://127.0.0.1:{proxy.server_port}/hook?tenant=a",
                data=b'{"process":"bash","ignored":true}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 201)
                self.assertEqual(json.loads(response.read()), {"ok": True})

            self.assertEqual(CaptureHandler.captured["method"], "POST")
            self.assertEqual(CaptureHandler.captured["path"], "/hook?tenant=a")
            self.assertEqual(json.loads(CaptureHandler.captured["body"]), {"ProcessName": "bash"})
        finally:
            proxy.shutdown()
            proxy.server_close()
            upstream.shutdown()
            upstream.server_close()

    def test_proxy_transforms_json_body_without_json_content_type(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()

        endpoint = EndpointConfig(
            path="/hook",
            remote=f"http://127.0.0.1:{upstream.server_port}/",
            send_query_params=False,
            only_send_defined_fields=True,
            content_body={".ProcessName": ".process"},
        )
        proxy = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler({"/hook": endpoint}, verbose=False, timeout=5),
        )
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        proxy_thread.start()

        try:
            request = Request(
                f"http://127.0.0.1:{proxy.server_port}/hook",
                data=b'{"process":"bash","ignored":true}',
                headers={"Content-Type": "text/plain"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 201)
                self.assertEqual(json.loads(response.read()), {"ok": True})

            self.assertEqual(json.loads(CaptureHandler.captured["body"]), {"ProcessName": "bash"})
        finally:
            proxy.shutdown()
            proxy.server_close()
            upstream.shutdown()
            upstream.server_close()

    def test_unconfigured_endpoint_returns_404(self):
        proxy = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler({}, verbose=False, timeout=5),
        )
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        proxy_thread.start()
        try:
            with self.assertRaises(HTTPError) as raised:
                urlopen(f"http://127.0.0.1:{proxy.server_port}/missing", timeout=5)
            self.assertEqual(raised.exception.code, 404)
        finally:
            proxy.shutdown()
            proxy.server_close()


if __name__ == "__main__":
    unittest.main()
