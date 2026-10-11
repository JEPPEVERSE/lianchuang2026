from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


def post(base_url: str, endpoint: str, payload: dict, token: str) -> dict:
    request = Request(base_url.rstrip("/") + endpoint,
                      data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json",
                               "Authorization": f"Bearer {token}"}, method="POST")
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def run_call(base_url: str, tool_call: dict, token: str) -> dict:
    print("\n提交 ToolCall：")
    print(json.dumps(tool_call, ensure_ascii=False, indent=2))
    result = post(base_url, "/tool-calls", tool_call, token)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("decision") == "require_approval" and result.get("status") == "pending":
        print("\n请检查以下实际操作：")
        print(f"工具：{result['tool_name']}")
        print("完整参数：" + json.dumps(result["arguments"], ensure_ascii=False, indent=2))
        print(f"原因：{result['reason']}\n有效期至：{result['expires_at']}")
        try:
            choice = input("输入 approve 确认，其他输入均拒绝：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "deny"
        action = "approve" if choice == "approve" else "deny"
        result = post(base_url, f"/approvals/{result['approval_id']}/decision",
                      {"action": action}, token)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="固定 JSON 模拟 Agent 的命令行 Client")
    parser.add_argument("--url", default="http://127.0.0.1:8081")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--file", type=Path, help="读取单个 ToolCall JSON")
    group.add_argument("--scenario", choices=("read", "email", "delete", "escape", "demo"), default="demo")
    args = parser.parse_args()
    token = os.environ.get("GATEWAY_CLIENT_TOKEN", "local-demo-token")
    calls = {
        "read": ("read_note", {"path": "todo.md"}),
        "email": ("send_email", {"to": "mentor@example.com", "subject": "作业链接",
                                 "body": "这是本次作业的文档链接。"}),
        "delete": ("delete_note", {"path": "draft.txt"}),
        "escape": ("delete_note", {"path": "../../config.json"}),
    }
    try:
        if args.file:
            run_call(args.url, json.loads(args.file.read_text(encoding="utf-8")), token)
        else:
            scenarios = ("read", "email", "escape") if args.scenario == "demo" else (args.scenario,)
            for scenario in scenarios:
                name, arguments = calls[scenario]
                run_call(args.url, {"call_id": f"call_{uuid4().hex}", "tool_name": name,
                                    "arguments": arguments}, token)
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        parser.exit(1, f"Client 请求失败：{exc}\n超时不代表未执行；请保留原 call_id 并检查审计记录。\n")


if __name__ == "__main__":
    main()
