import json
from pathlib import Path
import unittest

from learning_system import IntegrationError, SimulationNetwork

ROOT = Path(__file__).resolve().parents[1]


def resource(rid, ids, edges=()):
    return {"type": "resource", "resource": {"id": rid, "path": rid + ".md"},
            "points": [{"id": pid, "name": pid} for pid in ids],
            "edges": [{"from": a, "to": b} for a, b in edges]}


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.network = SimulationNetwork()

    def test_course_and_incremental_update(self):
        self.network.integrate_many(json.loads((ROOT / "examples/course.json").read_text()))
        self.assertEqual(["matrix", "row_operations", "rank", "linear_system"],
                         [item["point"]["id"] for item in self.network.make_plan()])
        self.assertEqual(["res_basics", "res_linear_system"],
                         self.network.query_point("rank")["resource_ids"])
        self.assertEqual(["q1", "q2"], self.network.query_point("rank")["problem_ids"])
        self.network.integrate_many(json.loads((ROOT / "examples/extra.json").read_text()))
        self.assertEqual(5, len(self.network.make_plan()))
        self.assertEqual(2, self.network.point_rank(1)[0]["problem_count"])

    def test_diamond_disconnected_and_isolated_nodes(self):
        edges = [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"), ("x", "y")]
        self.network.integrate(resource("r", ["y", "x", "d", "c", "b", "a", "isolated"], edges))
        plan = self.network.make_plan()
        ids = [item["point"]["id"] for item in plan]
        self.assertEqual(7, len(set(ids)))
        self.assertEqual(7, len(ids))
        for before, after in edges:
            self.assertLess(ids.index(before), ids.index(after))
        self.assertTrue(all(item["actions"] == [] for item in plan))

    def test_duplicate_upload_does_not_duplicate_nodes_edges_or_frequency(self):
        data = json.loads((ROOT / "examples/course.json").read_text())
        self.network.integrate_many(data)
        before = self.network.export_graph()
        self.network.integrate_many(data)
        self.assertEqual(before, self.network.export_graph())
        self.assertEqual(2, self.network.point_rank(1)[0]["problem_count"])

    def test_cycle_and_self_loop_are_atomic(self):
        self.network.integrate(resource("r", ["a", "b"], [("a", "b")]))
        before = self.network.export_graph()
        for edges in [[("b", "a")], [("c", "c")]]:
            with self.assertRaisesRegex(IntegrationError, "cycle"):
                self.network.integrate(resource("bad", ["c"], edges))
            self.assertEqual(before, self.network.export_graph())

    def test_unknown_reference_and_conflicting_definition_are_atomic(self):
        self.network.integrate(resource("r", ["a"]))
        before = self.network.export_graph()
        with self.assertRaisesRegex(IntegrationError, "unknown point"):
            self.network.integrate(resource("bad", ["b"], [("a", "missing")]))
        conflicting = resource("new", ["a"])
        conflicting["points"][0]["name"] = "another name"
        with self.assertRaisesRegex(IntegrationError, "conflicting point"):
            self.network.integrate(conflicting)
        self.assertEqual(before, self.network.export_graph())

    def test_batch_can_reference_later_entries(self):
        self.network.integrate_many([resource("r1", ["a"], [("a", "b")]),
                                     resource("r2", ["b"])])
        self.assertEqual(["a", "b"], [x["point"]["id"] for x in self.network.make_plan()])

    def test_empty_network_and_malformed_input(self):
        self.assertEqual([], self.network.make_plan())
        for data in [None, {}, {"type": "unexpected"}, resource("r", [" "])]:
            with self.assertRaises(IntegrationError):
                self.network.integrate(data)
        self.assertEqual([], self.network.export_graph()["points"])

    def test_returned_data_cannot_mutate_network(self):
        self.network.integrate_many(json.loads((ROOT / "examples/course.json").read_text()))
        graph = self.network.export_graph()
        graph["problems"][0]["content"] = "changed"
        graph["points"][0]["problem_ids"].clear()
        self.assertNotEqual("changed", self.network.export_graph()["problems"][0]["content"])
        self.assertEqual(["q1", "q2"], self.network.query_point("rank")["problem_ids"])


if __name__ == "__main__":
    unittest.main()
