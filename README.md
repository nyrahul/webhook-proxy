# Webhook Proxy

A small HTTP/HTTPS webhook proxy that forwards incoming requests to a configured remote endpoint using the same path and method. JSON request bodies can be transformed using dot-path mappings from a YAML config. Responses are returned to the caller without processing.

## Install

```bash
python3 -m pip install -r requirements.txt
```

## Run

```bash
python3 webhook_proxy.py --config config.example.yaml
```

Verbose mode prints the incoming request and body plus the upstream response:

```bash
python3 webhook_proxy.py --config config.example.yaml --verbose
```

To accept inbound HTTPS requests, provide a certificate and key:

```bash
python3 webhook_proxy.py --config config.example.yaml --port 8443 --cert-file cert.pem --key-file key.pem
```

Use `--host` or `--port` to override the configured listen address.

## Configuration

```yaml
listen-address: "0.0.0.0:8080"
endpoint-config:
  /api/workflow/tenant-doctor/security-advisory-report/webhook:
    remote: https://agentz.accuknox.com/
    send-query-params: true
    only-send-defined-fields: true
    content-body:
      ".ContainerName": ".ContainerName"
      ".ProcessName": ".ProcessName"
```

`listen-address` controls where the proxy binds. If omitted, it defaults to `127.0.0.1:8080`.

For an incoming request to `/api/workflow/tenant-doctor/security-advisory-report/webhook`, the proxy forwards to:

```text
https://agentz.accuknox.com/api/workflow/tenant-doctor/security-advisory-report/webhook
```

If `send-query-params` is true, the incoming query string is copied to the upstream request.

`content-body` maps destination JSON paths to source JSON paths. With the sample above, the upstream body contains only `ContainerName` and `ProcessName` when `only-send-defined-fields` is true.

When `only-send-defined-fields` is false, the full input JSON object is sent, then configured mappings are applied on top.
