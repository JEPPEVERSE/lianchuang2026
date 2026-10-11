"""Explicit deterministic fixtures, NOT a production AI implementation."""

from copy import deepcopy


RESOURCE_ONE = """---
kind: resource
---
# 矩阵基础
## 矩阵
矩阵是按行列排列的数表。
## 矩阵的秩
矩阵的秩等于主元的数量。矩阵秩也称 rank。
"""
RESOURCE_TWO = """---
kind: resource
---
# 方程组
## 矩阵秩与线性方程组
线性方程组有解当且仅当系数矩阵与增广矩阵的秩相等。
有解时，系数矩阵满列秩保证唯一解。
"""
PROBLEM = """---
kind: problem
---
# 模拟题
## Q1 求秩
求矩阵 [[1,0],[0,1]] 的秩。
"""


def point(local_id, name, cids, aliases=(), existing=None, confidence=1.0):
    return {"id": local_id, "name": name, "aliases": list(aliases), "chunk_ids": cids,
            "existing_id": existing, "confidence": confidence}


def analysis_for(payload):
    chunks = payload["chunks"]
    cids = [c["id"] for c in chunks]
    text = "".join(c["text"] for c in chunks)
    if payload["kind"] == "problem":
        chunk = next(c for c in chunks if "求矩阵" in c["text"])
        content = "求矩阵 [[1,0],[0,1]] 的秩。"
        return {"points": [point("r", "矩阵秩", [chunk["id"]])], "edges": [], "problems": [{
            "id": "Q1", "name": "求秩", "content": content, "question_type": "秩计算",
            "point_ids": ["r"], "chunk_id": chunk["id"], "source_quote": content}]}
    if "线性方程组" in text:
        return {"points": [point("r", "矩阵秩", cids), point("s", "线性方程组", cids)],
                "edges": [{"from": "r", "to": "s", "chunk_ids": cids}], "problems": []}
    return {"points": [point("m", "矩阵", cids), point("r", "矩阵的秩", cids, ["矩阵秩", "rank"])],
            "edges": [{"from": "m", "to": "r", "chunk_ids": cids}], "problems": []}


def answer_for(payload):
    if "张量" in payload["question"]:
        return {"status": "insufficient", "answer": "资料不足，没有张量分解的依据。",
                "citations": [], "missing_information": "缺少张量分解资料"}
    chunks = payload["retrieval"]["chunks"]
    source = next(c for c in chunks if "满列秩保证唯一解" in c["text"])
    quote = "有解时，系数矩阵满列秩保证唯一解。"
    return {"status": "answered", "answer": quote,
            "citations": [{"chunk_id": source["id"], "quote": quote}], "missing_information": ""}


class FixtureModel:
    def __init__(self):
        self.calls = 0

    def complete(self, system, payload, schema, correction=None):
        self.calls += 1
        return analysis_for(payload) if "kind" in payload else answer_for(payload)


class QueueModel:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def complete(self, system, payload, schema, correction=None):
        self.calls += 1
        result = self.outputs.pop(0)
        return result(payload) if callable(result) else deepcopy(result)
