"""SQLite authority: original documents, validated graph and immutable QA records."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import uuid

from .markdown import digest
from .network import SimulationNetwork


def normalized(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
 content TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chunks (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 title TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS points (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS aliases (
 normalized TEXT PRIMARY KEY, name TEXT NOT NULL, point_id TEXT NOT NULL REFERENCES points(id));
CREATE TABLE IF NOT EXISTS point_chunks (
 point_id TEXT NOT NULL REFERENCES points(id), chunk_id TEXT NOT NULL REFERENCES chunks(id),
 PRIMARY KEY(point_id, chunk_id));
CREATE TABLE IF NOT EXISTS prerequisites (
 before_id TEXT NOT NULL REFERENCES points(id), after_id TEXT NOT NULL REFERENCES points(id),
 chunk_id TEXT NOT NULL REFERENCES chunks(id), PRIMARY KEY(before_id, after_id, chunk_id));
CREATE TABLE IF NOT EXISTS problems (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES documents(id),
 name TEXT NOT NULL, content TEXT NOT NULL, question_type TEXT NOT NULL,
 chunk_id TEXT NOT NULL REFERENCES chunks(id), source_quote TEXT NOT NULL,
 UNIQUE(document_id, source_id));
CREATE TABLE IF NOT EXISTS problem_points (
 problem_id TEXT NOT NULL REFERENCES problems(id), point_id TEXT NOT NULL REFERENCES points(id),
 PRIMARY KEY(problem_id, point_id));
CREATE TABLE IF NOT EXISTS qa_records (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, question TEXT NOT NULL,
 retrieval_json TEXT NOT NULL, answer_json TEXT NOT NULL, state_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_point_chunks_chunk ON point_chunks(chunk_id);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
"""


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def rows(self, sql, args=()):
        return [dict(row) for row in self.connection.execute(sql, args)]

    def document(self, path):
        result = self.rows("SELECT * FROM documents WHERE path=?", (str(path),))
        return result[0] if result else None

    def known_points(self):
        result = self.rows("SELECT * FROM points ORDER BY id")
        for point in result:
            point["aliases"] = [a["name"] for a in self.rows(
                "SELECT name FROM aliases WHERE point_id=? ORDER BY normalized", (point["id"],))]
        return result

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def publish(self, path, kind, content, chunks, analyses):
        """Model calls finish before this transaction; publication is per document."""
        doc_id = "doc_" + digest(str(path))[:24]
        with self.transaction():
            prior = self.document(path)
            if prior:
                if prior["content_hash"] == digest(content) and prior["kind"] == kind:
                    return {"status": "skipped", "document_id": prior["id"], "path": str(path)}
                raise ValueError("同路径资料已变更或类型冲突；最小版不支持替换")
            self.connection.execute("INSERT INTO documents VALUES (?,?,?,?,?,?)", (
                doc_id, str(path), kind, content, digest(content), datetime.now(timezone.utc).isoformat()))
            for chunk in chunks:
                self.connection.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)", (
                    chunk.id, doc_id, chunk.title, chunk.start_line, chunk.end_line, chunk.text))
            for analysis in analyses:
                mapping = {}
                for point in analysis["points"]:
                    pid = self._resolve(point)
                    mapping[point["id"]] = pid
                    for cid in point["chunk_ids"]:
                        self.connection.execute("INSERT OR IGNORE INTO point_chunks VALUES (?,?)", (pid, cid))
                for edge in analysis["edges"]:
                    for cid in edge["chunk_ids"]:
                        self.connection.execute("INSERT OR IGNORE INTO prerequisites VALUES (?,?,?)", (
                            mapping[edge["from"]], mapping[edge["to"]], cid))
                for problem in analysis["problems"]:
                    qid = "problem_" + digest(doc_id + ":" + problem["id"])[:24]
                    existing = self.rows("SELECT id FROM problems WHERE id=?", (qid,))
                    if existing:
                        raise ValueError(f"模型重复提取题目 ID：{problem['id']}")
                    self.connection.execute("INSERT INTO problems VALUES (?,?,?,?,?,?,?,?)", (
                        qid, problem["id"], doc_id, problem["name"], problem["content"],
                        problem["question_type"], problem["chunk_id"], problem["source_quote"]))
                    for local_id in problem["point_ids"]:
                        self.connection.execute("INSERT OR IGNORE INTO problem_points VALUES (?,?)", (
                            qid, mapping[local_id]))
            # Reuse Level 1 topology validation; an invalid graph rolls back all writes.
            self.network().make_plan()
        return {"status": "imported", "document_id": doc_id, "path": str(path), "chunks": len(chunks)}

    def _resolve(self, point):
        names = [point["name"], *point["aliases"]]
        if any(not normalized(name) for name in names):
            raise ValueError("知识点名称或别名不能只有标点")
        matches = set()
        for name in names:
            matches.update(a["point_id"] for a in self.rows(
                "SELECT point_id FROM aliases WHERE normalized=?", (normalized(name),)))
        chosen = point["existing_id"]
        if chosen:
            if not self.rows("SELECT id FROM points WHERE id=?", (chosen,)):
                raise ValueError("模型选择了不存在的知识点 ID")
            if point["confidence"] >= 0.85:
                matches.add(chosen)
        if len(matches) > 1:
            raise ValueError("知识点名称/别名关联到多个已有节点，拒绝自动合并")
        pid = next(iter(matches)) if matches else "point_" + digest(normalized(point["name"]))[:24]
        self.connection.execute("INSERT OR IGNORE INTO points VALUES (?,?)", (pid, point["name"]))
        for name in names:
            self.connection.execute("INSERT OR IGNORE INTO aliases VALUES (?,?,?)", (normalized(name), name, pid))
        return pid

    def network(self):
        network = SimulationNetwork()
        points = self.rows("SELECT id,name FROM points ORDER BY id")
        edges = self.rows("SELECT DISTINCT before_id AS 'from', after_id AS 'to' FROM prerequisites")
        network.integrate({"type": "resource", "resource": {"id": "db_graph", "path": "SQLite"},
                           "points": points, "edges": edges})
        for problem in self.rows("SELECT id,name,content FROM problems ORDER BY id"):
            related = self.rows("SELECT p.id,p.name FROM points p JOIN problem_points pp ON p.id=pp.point_id "
                                "WHERE pp.problem_id=?", (problem["id"],))
            network.integrate({"type": "problem", "problem": problem, "points": related})
        return network

    def graph(self):
        return {"points": self.known_points(),
                "prerequisites": self.rows("SELECT * FROM prerequisites ORDER BY before_id,after_id,chunk_id"),
                "documents": self.rows("SELECT id,path,kind,content_hash FROM documents ORDER BY path"),
                "point_chunks": self.rows("SELECT * FROM point_chunks ORDER BY point_id,chunk_id"),
                "problems": self.rows("SELECT * FROM problems ORDER BY id"),
                "problem_points": self.rows("SELECT * FROM problem_points ORDER BY problem_id,point_id")}

    def query_point(self, name_or_id):
        found = self.rows("SELECT id,name FROM points WHERE id=?", (name_or_id,))
        if not found:
            found = self.rows("SELECT p.id,p.name FROM aliases a JOIN points p ON a.point_id=p.id "
                              "WHERE a.normalized=?", (normalized(name_or_id),))
        if not found:
            raise ValueError(f"未找到知识点：{name_or_id}")
        point = found[0]
        pid = point["id"]
        point["aliases"] = [a["name"] for a in self.rows("SELECT name FROM aliases WHERE point_id=?", (pid,))]
        point["chunks"] = self.rows(
            "SELECT c.*,d.path,d.kind,d.content_hash FROM chunks c JOIN documents d ON d.id=c.document_id "
            "JOIN point_chunks pc ON pc.chunk_id=c.id WHERE pc.point_id=? ORDER BY d.path,c.start_line", (pid,))
        point["problems"] = self.rows(
            "SELECT q.*,d.path FROM problems q JOIN problem_points pp ON pp.problem_id=q.id "
            "JOIN documents d ON d.id=q.document_id WHERE pp.point_id=? ORDER BY q.id", (pid,))
        point["prerequisites"] = self.rows(
            "SELECT DISTINCT p.id,p.name FROM points p JOIN prerequisites e ON p.id=e.before_id "
            "WHERE e.after_id=?", (pid,))
        return point

    def record_qa(self, question, retrieval, answer):
        qid = "qa_" + uuid.uuid4().hex
        state = {"question_id": qid, "question": question, "answer_status": answer["status"],
                 "retrieval_result_id": retrieval["id"], "answer_record_id": qid}
        with self.transaction():
            self.connection.execute("INSERT INTO qa_records VALUES (?,?,?,?,?,?)", (
                qid, datetime.now(timezone.utc).isoformat(), question,
                json.dumps(retrieval, ensure_ascii=False), json.dumps(answer, ensure_ascii=False),
                json.dumps(state, ensure_ascii=False)))
        return {"id": qid, **answer, "state": state}

    def history(self, limit=20):
        return [{"id": r["id"], "created_at": r["created_at"], "question": r["question"],
                 "answer": json.loads(r["answer_json"]), "state": json.loads(r["state_json"])}
                for r in self.rows("SELECT * FROM qa_records ORDER BY created_at DESC LIMIT ?", (limit,))]
