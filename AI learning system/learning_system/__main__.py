"""Run: python3 -m learning_system examples/course.json [--json]."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .network import SimulationNetwork


def level1_main() -> None:
    parser = argparse.ArgumentParser(description="Level 1 模拟知识网络")
    parser.add_argument("inputs", nargs="+", type=Path, help="包含分析结果数组的 JSON 文件")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args()
    network = SimulationNetwork()
    try:
        for path in args.inputs:
            network.integrate_many(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"输入或整合失败：{exc}\n")
    graph = network.export_graph()
    plan = network.make_plan()
    if args.json:
        print(json.dumps({"graph": graph, "plan": plan,
                          "point_rank": network.point_rank(len(graph["points"]))},
                         ensure_ascii=False, indent=2))
        return
    print("知识点（前置 / 资料 / 真题）：")
    for point in graph["points"]:
        print(f"  {point['id']} · {point['name']} | 前置={point['prerequisite_ids']}"
              f" | 资料={point['resource_ids']} | 真题={point['problem_ids']}")
    print("\n资料：")
    for resource in graph["resources"]:
        print(f"  {resource['id']}: {resource['path']}")
    print("\n真题：")
    for problem in graph["problems"]:
        print(f"  {problem['id']} · {problem['name']}: {problem['content']}")
    print("\n全部关系：")
    for relation in graph["relations"]:
        print(f"  {relation['from']} --{relation['relation']}--> {relation['to']}")
    print("\n学习计划：")
    print(" → ".join(item["point"]["name"] for item in plan) or "（空网络）")
    print("\n真题考频：")
    for point in network.point_rank(len(graph["points"])):
        print(f"  {point['name']}: {point['problem_count']} 题")


def main() -> None:
    commands = {"ingest", "point", "ask", "search", "graph", "plan", "history"}
    if len(sys.argv) > 1 and sys.argv[1] not in commands and sys.argv[1] not in ("--help", "-h", "--db"):
        level1_main()
        return
    from .analysis import ingest_file
    from .model import ChatModel, ModelError, load_env
    from .qa import ask
    from .retrieval import retrieve
    from .store import Store

    parser = argparse.ArgumentParser(description="AI 学习系统 Level 2（原 Level 1 JSON 入口仍可用）")
    parser.add_argument("--db", default="runtime/learning.sqlite3", help="SQLite 数据库路径")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in sorted(commands):
        command = sub.add_parser(name)
        command.add_argument("--db", default=argparse.SUPPRESS)
        command.add_argument("--json", action="store_true")
        if name == "ingest":
            command.add_argument("path", type=Path)
            command.add_argument("--kind", required=True, choices=["resource", "problem"])
            command.add_argument("--limit", type=int, help="只处理排序后的前 N 个文件，用于小规模试跑")
        elif name == "point":
            command.add_argument("name", help="知识点名称、别名或 ID")
        elif name in ("ask", "search"):
            command.add_argument("question")
            command.add_argument("--top-k", type=int, default=8)
            command.add_argument("--max-chars", type=int, default=12000)
    args = parser.parse_args()
    load_env()
    try:
        with Store(args.db) as store:
            if args.command == "ingest":
                if not args.path.exists():
                    raise ValueError("输入路径不存在")
                files = sorted(args.path.rglob("*.md")) if args.path.is_dir() else [args.path]
                if args.limit is not None:
                    if args.limit <= 0:
                        raise ValueError("limit 必须为正整数")
                    files = files[:args.limit]
                if not files:
                    raise ValueError("目录没有 Markdown 文件")
                model = ChatModel()
                for path in files:
                    # Each line is an individual durable publication, not a batch promise.
                    result = ingest_file(store, model, path, args.kind)
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                return
            if args.command == "point":
                result = store.query_point(args.name)
            elif args.command == "search":
                result = retrieve(store, args.question, args.top_k, args.max_chars)
            elif args.command == "ask":
                result = ask(store, ChatModel(), args.question, args.top_k, args.max_chars)
            elif args.command == "graph":
                result = store.graph()
            elif args.command == "plan":
                result = store.network().make_plan()
            else:
                result = store.history()
            if args.command == "ask" and not args.json:
                print(f"状态：{result['status']}\n{result['answer']}")
                for citation in result["citations"]:
                    print(f"\n依据：{citation['path']}:{citation['start_line']}–{citation['end_line']}")
                    print(citation["quote"])
                if result["missing_information"]:
                    print(f"\n缺失：{result['missing_information']}")
                print(f"\n记录：{result['id']}")
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, ModelError, sqlite3.Error) as exc:
        parser.exit(1, f"操作失败：{exc}\n")


if __name__ == "__main__":
    main()
