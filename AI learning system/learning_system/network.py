"""Knowledge integration and prerequisite ordering; Python standard library only."""

from copy import deepcopy
import heapq
from typing import Any


class IntegrationError(ValueError):
    """An analysis cannot be integrated without violating network invariants."""


def _object(value: Any, required: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != required:
        raise IntegrationError(f"{label} must contain exactly: {sorted(required)}")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntegrationError(f"{label} must be a non-empty string")
    return value


def _list(value: Any, label: str) -> list:
    if not isinstance(value, list):
        raise IntegrationError(f"{label} must be an array")
    return value


class SimulationNetwork:
    """Append-only, idempotent integration of already analyzed resources/problems.

    Edges are (prerequisite, dependent). Resource edges and problem associations
    are separate from prerequisite edges. IDs define identity, not model guesses.
    """

    def __init__(self) -> None:
        self._points: dict[str, str] = {}
        self._resources: dict[str, str] = {}
        self._problems: dict[str, dict] = {}
        self._edges: set[tuple[str, str]] = set()
        self._resource_points: dict[str, set[str]] = {}
        self._problem_points: dict[str, set[str]] = {}

    def integrate(self, analysis: dict) -> None:
        """Integrate one AnalyzeReq atomically; invalid input leaves state intact."""
        self.integrate_many([analysis])

    def integrate_many(self, analyses: list[dict]) -> None:
        """Integrate a batch atomically; references may target later batch entries."""
        _list(analyses, "analyses")
        candidate = deepcopy(self)
        for analysis in analyses:
            candidate._apply(analysis)
        for before, after in candidate._edges:
            if before not in candidate._points or after not in candidate._points:
                raise IntegrationError(f"unknown point in prerequisite: {before} -> {after}")
        candidate._ordered_ids()  # Detect cycles before publishing any changes.
        self.__dict__.update(candidate.__dict__)

    def _apply(self, analysis: dict) -> None:
        if not isinstance(analysis, dict):
            raise IntegrationError("analysis must be an object")
        kind = analysis.get("type")
        if kind == "resource":
            _object(analysis, {"type", "resource", "points", "edges"}, "resource analysis")
            resource = _object(analysis["resource"], {"id", "path"}, "resource")
            rid = _text(resource["id"], "resource.id")
            path = _text(resource["path"], "resource.path")
            self._put(self._resources, rid, path, "resource")
        elif kind == "problem":
            _object(analysis, {"type", "problem", "points"}, "problem analysis")
            problem = _object(analysis["problem"], {"id", "name", "content"}, "problem")
            pid = _text(problem["id"], "problem.id")
            _text(problem["name"], "problem.name")
            _text(problem["content"], "problem.content")
            self._put(self._problems, pid, deepcopy(problem), "problem")
        else:
            raise IntegrationError("analysis.type must be 'resource' or 'problem'")

        point_ids = set()
        for point in _list(analysis["points"], "points"):
            _object(point, {"id", "name"}, "point")
            point_id = _text(point["id"], "point.id")
            name = _text(point["name"], "point.name")
            self._put(self._points, point_id, name, "point")
            point_ids.add(point_id)

        if kind == "resource":
            self._resource_points.setdefault(rid, set()).update(point_ids)
            for edge in _list(analysis["edges"], "edges"):
                _object(edge, {"from", "to"}, "edge")
                self._edges.add((_text(edge["from"], "edge.from"),
                                 _text(edge["to"], "edge.to")))
        else:
            # Associations merge when the same unchanged problem is analyzed again.
            self._problem_points.setdefault(pid, set()).update(point_ids)

    @staticmethod
    def _put(mapping: dict, key: str, value: Any, kind: str) -> None:
        if key in mapping and mapping[key] != value:
            raise IntegrationError(f"conflicting {kind} definition for id: {key}")
        mapping[key] = value

    def _ordered_ids(self) -> list[str]:
        indegree = dict.fromkeys(self._points, 0)
        successors: dict[str, list[str]] = {pid: [] for pid in self._points}
        for before, after in self._edges:
            successors[before].append(after)
            indegree[after] += 1
        ready = [pid for pid, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        result = []
        while ready:
            point_id = heapq.heappop(ready)
            result.append(point_id)
            for after in sorted(successors[point_id]):
                indegree[after] -= 1
                if indegree[after] == 0:
                    heapq.heappush(ready, after)
        if len(result) != len(self._points):
            blocked = sorted(pid for pid, degree in indegree.items() if degree)
            raise IntegrationError(f"prerequisite cycle; blocked points: {', '.join(blocked)}")
        return result

    def make_plan(self) -> list[dict]:
        """Every point exactly once, after all prerequisites; actions may be empty."""
        return [{"point": {"id": pid, "name": self._points[pid]}, "actions": []}
                for pid in self._ordered_ids()]

    def query_point(self, point_id: str) -> dict:
        """Return a point and its direct prerequisites, resources and problems."""
        if point_id not in self._points:
            raise KeyError(f"unknown point: {point_id}")
        return {
            "id": point_id,
            "name": self._points[point_id],
            "prerequisite_ids": sorted(a for a, b in self._edges if b == point_id),
            "resource_ids": sorted(rid for rid, points in self._resource_points.items()
                                   if point_id in points),
            "problem_ids": sorted(pid for pid, points in self._problem_points.items()
                                  if point_id in points),
        }

    def point_rank(self, rank: int) -> list[dict]:
        """Count distinct problems per point, not repeated integrations or resources."""
        if type(rank) is not int or rank < 0:
            raise ValueError("rank must be a non-negative integer")
        counts = {point: sum(point in points for points in self._problem_points.values())
                  for point in self._points}
        return [{"id": pid, "name": self._points[pid], "problem_count": counts[pid]}
                for pid in sorted(counts, key=lambda pid: (-counts[pid], pid))[:rank]]

    def export_graph(self) -> dict:
        """A detached, deterministic snapshot of every node and relation."""
        return {
            "points": [self.query_point(pid) for pid in sorted(self._points)],
            "resources": [{"id": rid, "path": path}
                          for rid, path in sorted(self._resources.items())],
            "problems": [deepcopy(self._problems[pid]) for pid in sorted(self._problems)],
            "relations": (
                [{"from": a, "relation": "prerequisite_of", "to": b}
                 for a, b in sorted(self._edges)]
                + [{"from": rid, "relation": "covers", "to": pid}
                   for rid, points in sorted(self._resource_points.items())
                   for pid in sorted(points)]
                + [{"from": qid, "relation": "examines", "to": pid}
                   for qid, points in sorted(self._problem_points.items())
                   for pid in sorted(points)]
            ),
        }
