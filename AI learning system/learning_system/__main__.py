"""Run: python3 -m learning_system examples/course.json [--json]."""

import argparse
import json
from pathlib import Path

from .network import SimulationNetwork


def main() -> None:
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


if __name__ == "__main__":
    main()
