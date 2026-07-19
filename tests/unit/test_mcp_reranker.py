from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Dict, List

from codebase_rag.services.agent.errors import AgentTaskComplete
from codebase_rag.services.mcp_service.models import CandidatePoint
from codebase_rag.services.mcp_service.service import McpService


def _candidates(*ids: str) -> List[CandidatePoint]:
    return [
        CandidatePoint(
            id=cid,
            file_path=f"{cid}.py",
            business_description=f"desc-{cid}",
            vector_score=0.5,
        )
        for cid in ids
    ]


class _AgentMock:
    """Mock agent that drives tool_handlers according to a scripted plan."""

    def __init__(self, plan) -> None:
        # plan: async callable(tool_handlers) -> None
        self._plan = plan
        self.calls = 0

    async def run_agent(self, **kwargs) -> SimpleNamespace:
        self.calls += 1
        tool_handlers: Dict[str, Any] = kwargs["tool_handlers"]
        await self._plan(tool_handlers)
        return SimpleNamespace()


class McpRerankerTest(unittest.IsolatedAsyncioTestCase):
    def _service(self, agent) -> McpService:
        return McpService(
            config_manager=SimpleNamespace(
                config=SimpleNamespace(
                    mcp=SimpleNamespace(
                        reranker=SimpleNamespace(
                            recall_limit=20,
                            timeout=600,
                            max_rate_limit_retries=1,
                            providers=[],
                        ),
                        knowledge_submission=SimpleNamespace(point_token_limit=8000),
                    )
                )
            ),
            embedding=SimpleNamespace(),
            memory_qdrant=SimpleNamespace(),
            agent=agent,
        )

    async def test_single_submit_reaching_n_ends_without_end_task(self) -> None:
        async def plan(handlers):
            with self.assertRaises(AgentTaskComplete):
                await handlers["submit_scores"](
                    [
                        {"id": "a", "score": 0.9},
                        {"id": "b", "score": 0.8},
                    ]
                )

        agent = _AgentMock(plan)
        # Pretend a registered reranker agent exists.
        service = self._service(agent)
        service._reranker_agent_names = ["reranker_0"]

        scores = await service._run_reranker(
            query="q",
            candidates=_candidates("a", "b", "c"),
            top_k=2,
        )

        self.assertEqual(agent.calls, 1)
        by_id = {s.id: s.score for s in scores}
        self.assertEqual(by_id, {"a": 0.9, "b": 0.8})

    async def test_two_batch_submit_ends_on_second(self) -> None:
        stages: List[str] = []

        async def plan(handlers):
            msg1 = await handlers["submit_scores"]([{"id": "a", "score": 0.7}])
            stages.append(msg1)
            self.assertIn("累计唯一有效 1/3", msg1)
            with self.assertRaises(AgentTaskComplete):
                await handlers["submit_scores"](
                    [
                        {"id": "b", "score": 0.6},
                        {"id": "c", "score": 0.5},
                    ]
                )
            stages.append("ended")

        agent = _AgentMock(plan)
        service = self._service(agent)
        service._reranker_agent_names = ["reranker_0"]

        scores = await service._run_reranker(
            query="q",
            candidates=_candidates("a", "b", "c", "d"),
            top_k=3,
        )

        self.assertEqual(stages[-1], "ended")
        self.assertEqual({s.id for s in scores}, {"a", "b", "c"})

    async def test_duplicate_id_keeps_higher_score_and_counts_once(self) -> None:
        async def plan(handlers):
            msg = await handlers["submit_scores"](
                [
                    {"id": "a", "score": 0.4},
                    {"id": "a", "score": 0.9},
                    {"id": "b", "score": 0.2},
                ]
            )
            # unique ids = 2, target = 3 -> not enough, no AgentTaskComplete
            self.assertIn("累计唯一有效 2/3", msg)
            with self.assertRaises(AgentTaskComplete):
                await handlers["submit_scores"]([{"id": "c", "score": 0.5}])

        agent = _AgentMock(plan)
        service = self._service(agent)
        service._reranker_agent_names = ["reranker_0"]

        scores = await service._run_reranker(
            query="q",
            candidates=_candidates("a", "b", "c"),
            top_k=3,
        )

        by_id = {s.id: s.score for s in scores}
        self.assertEqual(by_id["a"], 0.9)
        self.assertEqual(set(by_id), {"a", "b", "c"})

    async def test_invalid_id_ignored(self) -> None:
        async def plan(handlers):
            msg = await handlers["submit_scores"](
                [
                    {"id": "ghost", "score": 1.0},
                    {"id": "a", "score": 0.8},
                ]
            )
            self.assertIn("已接收 1 条有效评分", msg)
            self.assertIn("累计唯一有效 1/2", msg)
            with self.assertRaises(AgentTaskComplete):
                await handlers["submit_scores"]([{"id": "b", "score": 0.7}])

        agent = _AgentMock(plan)
        service = self._service(agent)
        service._reranker_agent_names = ["reranker_0"]

        scores = await service._run_reranker(
            query="q",
            candidates=_candidates("a", "b"),
            top_k=2,
        )

        self.assertEqual({s.id for s in scores}, {"a", "b"})

    async def test_end_task_can_finish_below_n(self) -> None:
        async def plan(handlers):
            msg = await handlers["submit_scores"]([{"id": "a", "score": 0.55}])
            self.assertIn("累计唯一有效 1/3", msg)
            with self.assertRaises(AgentTaskComplete):
                await handlers["end_task"](signal="exit")

        agent = _AgentMock(plan)
        service = self._service(agent)
        service._reranker_agent_names = ["reranker_0"]

        scores = await service._run_reranker(
            query="q",
            candidates=_candidates("a", "b", "c"),
            top_k=3,
        )

        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0].id, "a")
        self.assertEqual(scores[0].score, 0.55)


if __name__ == "__main__":
    unittest.main()
