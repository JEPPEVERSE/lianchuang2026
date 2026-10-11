from __future__ import annotations

import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .gateway.service import GatewayService

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def strict_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复 JSON 字段：{key}")
        result[key] = value
    return result


def reject_constant(value: str):
    raise ValueError(f"非法 JSON 数值：{value}")


def make_server(service: GatewayService, token_users: dict[str, str],
                host: str = "127.0.0.1", port: int = 8081) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self.respond(200, {"status": "ok"})
            else:
                self.respond(404, {"error": "接口不存在"})

        def do_POST(self) -> None:
            authorization = self.headers.get("Authorization", "")
            user_id = next((user for token, user in token_users.items()
                            if hmac.compare_digest(authorization, f"Bearer {token}")), None)
            if user_id is None:
                self.respond(401, {"error": "缺少或无效的 Client 凭据"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    raise ValueError("请求体必须为 1 字节到 1 MiB")
                payload = json.loads(self.rfile.read(length), object_pairs_hook=strict_object,
                                     parse_constant=reject_constant)
            except (ValueError, UnicodeError) as exc:
                self.respond(400, {"error": f"请求 JSON 非法：{exc}"})
                return
            path = urlsplit(self.path).path
            try:
                if path == "/tool-calls":
                    result = service.submit(payload, user_id)
                elif (path.startswith("/approvals/") and path.endswith("/decision")
                      and len(path.split("/")) == 4):
                    result = service.decide(path.split("/")[2], payload, user_id)
                else:
                    self.respond(404, {"error": "接口不存在"})
                    return
            except Exception:
                # An infrastructure failure may occur after a side effect: do not claim denial.
                self.respond(500, {"status": "unknown", "error": "网关内部错误，请检查审计记录，勿盲目重试"})
                return
            self.respond(200, result)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Tool Gateway Server")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--sandbox", type=Path, default=PROJECT_ROOT / "sandbox")
    parser.add_argument("--audit", type=Path, default=PROJECT_ROOT / "runtime/audit.jsonl")
    args = parser.parse_args()
    token = os.environ.get("GATEWAY_CLIENT_TOKEN", "local-demo-token")
    if not token:
        parser.error("GATEWAY_CLIENT_TOKEN 不能为空")
    service = GatewayService(args.sandbox, args.audit)
    server = make_server(service, {token: "user:demo"}, port=args.port)
    print(f"Gateway: http://127.0.0.1:{args.port}\n审计日志: {args.audit}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
