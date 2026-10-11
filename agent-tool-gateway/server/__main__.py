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
    validate_users(token_users)
    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(10)

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
            try:
                self.handle_post()
            except Exception:
                # Includes audit failures: never misreport an uncertain side effect.
                self.respond(500, {"status": "unknown", "error": "网关内部错误，请检查审计记录，勿盲目重试"})

        def reject(self, status: int, user_id: str, reason: str) -> None:
            result = service.reject_request(user_id, reason)
            self.respond(status, {**result, "error": reason})

        def handle_post(self) -> None:
            authorization = self.headers.get("Authorization", "")
            user_id = next((user for token, user in token_users.items()
                            if hmac.compare_digest(authorization.encode("utf-8"),
                                                   f"Bearer {token}".encode("utf-8"))), None)
            if user_id is None:
                self.reject(401, "unauthenticated", "缺少或无效的 Client 凭据")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    raise ValueError("请求体必须为 1 字节到 1 MiB")
                payload = json.loads(self.rfile.read(length), object_pairs_hook=strict_object,
                                     parse_constant=reject_constant)
            except (ValueError, UnicodeError, TimeoutError, RecursionError) as exc:
                self.reject(400, user_id, f"请求 JSON 非法：{exc}")
                return
            path = urlsplit(self.path).path
            if path == "/tool-calls":
                result = service.submit(payload, user_id)
            elif (path.startswith("/approvals/") and path.endswith("/decision")
                  and len(path.split("/")) == 4 and path.split("/")[2]):
                result = service.decide(path.split("/")[2], payload, user_id)
            else:
                self.reject(404, user_id, "接口不存在")
                return
            self.respond(200, result)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def validate_users(token_users: dict) -> None:
    if not isinstance(token_users, dict) or not token_users:
        raise ValueError("用户配置必须是非空的 token → user_id 对象")
    for token, user in token_users.items():
        if (not isinstance(token, str) or not token or not token.isascii()
                or any(ord(char) <= 32 or ord(char) >= 127 for char in token)
                or not isinstance(user, str) or not user.strip()):
            raise ValueError("每个 token 必须是无空白的可打印 ASCII 字符串，user_id 必须是非空字符串")


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Tool Gateway Server")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--sandbox", type=Path, default=PROJECT_ROOT / "sandbox")
    parser.add_argument("--audit", type=Path, default=PROJECT_ROOT / "runtime/audit.jsonl")
    parser.add_argument("--approval-ttl", type=int, default=300, help="确认有效期，正整数秒")
    parser.add_argument("--users-file", type=Path, help="服务端 token → user_id JSON 配置")
    args = parser.parse_args()
    token = os.environ.get("GATEWAY_CLIENT_TOKEN", "local-demo-token")
    try:
        users = (json.loads(args.users_file.read_text(encoding="utf-8"), object_pairs_hook=strict_object)
                 if args.users_file else {token: "user:demo"})
        validate_users(users)
        service = GatewayService(args.sandbox, args.audit, approval_ttl=args.approval_ttl)
        server = make_server(service, users, port=args.port)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"Gateway: http://127.0.0.1:{args.port}\n审计日志: {args.audit}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
