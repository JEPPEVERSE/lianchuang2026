from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from learning_system.analysis import ingest_file
from learning_system.contracts import ANALYSIS_SCHEMA, validate
from learning_system.markdown import metadata, split_markdown
from learning_system.model import ModelError
from learning_system.qa import ask
from learning_system.retrieval import retrieve
from learning_system.store import Store
from support import FixtureModel, PROBLEM, QueueModel, RESOURCE_ONE, RESOURCE_TWO, analysis_for, point


class Level2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "learning.sqlite3"
        self.store = Store(self.db)
        self.model = FixtureModel()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def file(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def populate(self):
        ingest_file(self.store, self.model, self.file("a.md", RESOURCE_ONE), "resource")
        ingest_file(self.store, self.model, self.file("b.md", RESOURCE_TWO), "resource")
        ingest_file(self.store, self.model, self.file("q.md", PROBLEM), "problem")

    def test_persistent_import_alias_merge_problems_and_plan(self):
        self.populate()
        self.assertEqual(3, len(self.store.known_points()))
        detail = self.store.query_point("rank")
        self.assertEqual("矩阵的秩", detail["name"])
        self.assertEqual(3, len({c["document_id"] for c in detail["chunks"]}))
        self.assertEqual("秩计算", detail["problems"][0]["question_type"])
        self.assertEqual(["矩阵", "矩阵的秩", "线性方程组"],
                         [item["point"]["name"] for item in self.store.network().make_plan()])
        with Store(self.db) as reopened:
            self.assertEqual(detail, reopened.query_point("矩阵秩"))
            self.assertEqual(1, reopened.network().point_rank(1)[0]["problem_count"])

    def test_duplicate_skips_model_changed_file_is_rejected(self):
        path = self.file("a.md", RESOURCE_ONE)
        ingest_file(self.store, self.model, path, "resource")
        before_calls, before_graph = self.model.calls, self.store.graph()
        self.assertEqual("skipped", ingest_file(self.store, self.model, path, "resource")["status"])
        self.assertEqual(before_calls, self.model.calls)
        path.write_text(RESOURCE_ONE + "新增文字")
        with self.assertRaisesRegex(ValueError, "修改"):
            ingest_file(self.store, self.model, path, "resource")
        self.assertEqual(before_graph, self.store.graph())

    def test_cycle_rolls_back_document_aliases_and_chunks(self):
        path = self.file("a.md", RESOURCE_ONE)
        ingest_file(self.store, self.model, path, "resource")
        before = self.store.graph()
        def cycle(payload):
            data = analysis_for(payload)
            data["edges"].append({"from": "r", "to": "m", "chunk_ids": [payload["chunks"][0]["id"]]})
            return data
        with self.assertRaisesRegex(ValueError, "cycle"):
            ingest_file(self.store, QueueModel([cycle]), self.file("bad.md", RESOURCE_ONE), "resource")
        self.assertEqual(before, self.store.graph())

    def test_invalid_schema_then_valid_correction(self):
        model = QueueModel([{}, analysis_for])
        ingest_file(self.store, model, self.file("a.md", RESOURCE_ONE), "resource")
        self.assertEqual(2, model.calls)

    def test_unknown_source_and_unknown_existing_id_are_rejected(self):
        for field in ("chunk", "existing"):
            def bad(payload):
                data = analysis_for(payload)
                if field == "chunk":
                    data["points"][0]["chunk_ids"] = ["invented"]
                else:
                    data["points"][0]["existing_id"] = "invented"
                return data
            with self.assertRaises(ModelError):
                ingest_file(self.store, QueueModel([bad, bad]), self.file(field + ".md", RESOURCE_ONE), "resource")
        self.assertEqual([], self.store.known_points())

    def test_fabricated_problem_content_rejected(self):
        def bad(payload):
            data = analysis_for(payload)
            data["problems"][0]["content"] = "模型改写的题目"
            return data
        with self.assertRaises(ModelError):
            ingest_file(self.store, QueueModel([bad, bad]), self.file("q.md", PROBLEM), "problem")
        self.assertEqual([], self.store.graph()["documents"])

    def test_low_confidence_does_not_merge_distinct_concepts(self):
        ingest_file(self.store, self.model, self.file("a.md", RESOURCE_ONE), "resource")
        existing = self.store.query_point("矩阵秩")["id"]
        def output(payload):
            return {"points": [point("new", "特征向量", [payload["chunks"][0]["id"]],
                                     existing=existing, confidence=0.3)], "edges": [], "problems": []}
        ingest_file(self.store, QueueModel([output]), self.file("new.md", "# 特征向量\n特征向量非零。\n"), "resource")
        self.assertNotEqual(existing, self.store.query_point("特征向量")["id"])

    def test_conflicting_alias_cannot_merge_two_points(self):
        ingest_file(self.store, self.model, self.file("a.md", RESOURCE_ONE), "resource")
        before = self.store.graph()
        def output(payload):
            return {"points": [point("p", "矩阵", [payload["chunks"][0]["id"]], ["矩阵秩"])],
                    "edges": [], "problems": []}
        with self.assertRaisesRegex(ValueError, "多个已有节点"):
            ingest_file(self.store, QueueModel([output]), self.file("bad.md", RESOURCE_ONE), "resource")
        self.assertEqual(before, self.store.graph())

    def test_resource_search_excludes_problem_text_and_enforces_budget(self):
        self.populate()
        result = retrieve(self.store, "矩阵秩与方程组", max_chars=2000)
        self.assertTrue(result["chunks"])
        self.assertLessEqual(result["context_chars"], 2000)
        self.assertTrue(all(c["kind"] == "resource" for c in result["chunks"]))
        self.assertTrue(result["point_ids"])

    def test_one_hop_expansion_finds_non_lexical_related_passage(self):
        self.populate()
        def nonlexical(payload):
            return {"points": [point("s", "线性方程组", [payload["chunks"][0]["id"]])],
                    "edges": [], "problems": []}
        ingest_file(self.store, QueueModel([nonlexical]),
                    self.file("related.md", "# 相容性\n右端项应满足一致性约束。\n"), "resource")
        result = retrieve(self.store, "rank")
        self.assertTrue(any("graph_expansion" in c["retrieval_reason"] for c in result["chunks"]))
        self.assertTrue(any("一致性约束" in c["text"] for c in result["chunks"]))

    def test_grounded_qa_persists_snapshot_and_source_locations(self):
        self.populate()
        result = ask(self.store, self.model, "线性方程组什么时候唯一可解？")
        self.assertEqual("answered", result["status"])
        self.assertEqual("answered", result["state"]["answer_status"])
        cite = result["citations"][0]
        self.assertTrue(cite["content_hash"])
        with Store(self.db) as reopened:
            history = reopened.history()
            self.assertEqual(result["id"], history[0]["id"])
            row = reopened.rows("SELECT retrieval_json FROM qa_records WHERE id=?", (result["id"],))[0]
            snapshot = json.loads(row["retrieval_json"])
            self.assertTrue(any(cite["quote"] in c["text"] for c in snapshot["chunks"]))

    def test_empty_retrieval_refuses_without_model_call(self):
        result = ask(self.store, self.model, "张量分解是什么？")
        self.assertEqual("insufficient", result["status"])
        self.assertEqual(0, self.model.calls)

    def test_insufficient_and_partial_answers(self):
        self.populate()
        result = ask(self.store, self.model, "矩阵与张量分解有什么关系？")
        self.assertEqual("insufficient", result["status"])
        def partial(payload):
            chunk = payload["retrieval"]["chunks"][0]
            return {"status": "partial", "answer": "仅能说明资料中的定义。",
                    "citations": [{"chunk_id": chunk["id"], "quote": chunk["text"]}],
                    "missing_information": "缺少算法细节"}
        result = ask(self.store, QueueModel([partial]), "矩阵秩的算法与复杂度？")
        self.assertEqual("partial", result["status"])

    def test_forged_qa_citation_never_published(self):
        self.populate()
        bad = {"status": "answered", "answer": "猜测答案", "citations": [
            {"chunk_id": "not_retrieved", "quote": "伪造引用"}], "missing_information": ""}
        with self.assertRaises(ModelError):
            ask(self.store, QueueModel([bad, bad]), "矩阵秩是什么？")
        self.assertEqual([], self.store.history())

    def test_answer_quote_not_in_source_and_status_rules(self):
        self.populate()
        def bad(payload):
            chunk = payload["retrieval"]["chunks"][0]
            return {"status": "answered", "answer": "错误", "citations": [
                {"chunk_id": chunk["id"], "quote": "这句原文不存在"}], "missing_information": ""}
        with self.assertRaises(ModelError):
            ask(self.store, QueueModel([bad, bad]), "矩阵秩是什么？")
        bad_partial = {"status": "partial", "answer": "部分内容", "citations": [], "missing_information": ""}
        with self.assertRaises(ModelError):
            ask(self.store, QueueModel([bad_partial, bad_partial]), "矩阵秩是什么？")

    def test_solution_kind_is_not_ingested_as_resource(self):
        path = self.file("answers.md", "---\nkind: solution\n---\n# 答案\n答案正文。")
        with self.assertRaisesRegex(ValueError, "kind"):
            ingest_file(self.store, self.model, path, "resource")
        self.assertEqual(0, self.model.calls)

    def test_markdown_line_ranges_fences_and_long_sections(self):
        text = "---\nkind: resource\n---\n# 标题\n## 小节\n```text\n# 不是标题\n```\n" + ("正文。" * 40 + "\n\n") * 4
        lines = text.splitlines(keepends=True)
        chunks = split_markdown(text, "doc", max_chars=100)
        self.assertGreater(len(chunks), 2)
        for chunk in chunks:
            self.assertEqual(chunk.text, "".join(lines[chunk.start_line - 1:chunk.end_line]))
        self.assertTrue(any("```text\n# 不是标题\n```" in c.text for c in chunks))
        self.assertFalse(any("不是标题" in c.title for c in chunks))
        with self.assertRaises(ValueError):
            metadata("---\nkind: resource\n未闭合")

    def test_schema_rejects_boolean_confidence(self):
        payload = {"chunks": [{"id": "c", "text": "矩阵基础"}], "kind": "resource"}
        output = analysis_for(payload)
        output["points"][0]["confidence"] = True
        with self.assertRaises(ValueError):
            validate(output, ANALYSIS_SCHEMA)


if __name__ == "__main__":
    unittest.main()
