"""S02 离线单元测试：验证双模型路由的规划、执行、验收返工、重试止损与 Token 分账。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from s01_react_loop.tools import build_default_registry
from s02_planner_executor.router import run_pipeline


def _make_response(content: str = "", tool_calls: list[dict] | None = None, tokens: int = 40):
    parsed_calls = [
        SimpleNamespace(
            id=tc["id"],
            function=SimpleNamespace(name=tc["name"], arguments=tc["arguments"]),
        )
        for tc in (tool_calls or [])
    ]
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=parsed_calls))],
        usage=SimpleNamespace(total_tokens=tokens),
    )


class FakeClient:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, tools=None):
        self.calls.append({"model": model, "messages": [dict(m) for m in messages], "tools": tools})
        return self._responses.pop(0)


def test_planner_executor_happy_path_and_token_accounting(tmp_path: Path):
    (tmp_path / "data.txt").write_text("price=25", encoding="utf-8")
    registry = build_default_registry(tmp_path)

    planner_client = FakeClient(
        [
            # 1. Plan (wrapped in markdown fence to verify tolerant JSON parser)
            _make_response(
                content='```json\n{"subtasks": ['
                '{"id": "T1", "goal": "读取 data.txt", "acceptance": "给出文件中的 price 值"},'
                '{"id": "T2", "goal": "计算 price * 4", "acceptance": "得出准确整数结果"}'
                "]}\n```",
                tokens=80,
            ),
            # 2. Verify T1
            _make_response(content='{"passed": true, "feedback": "ok"}', tokens=30),
            # 3. Verify T2
            _make_response(content='{"passed": true, "feedback": "ok"}', tokens=30),
        ]
    )
    executor_client = FakeClient(
        [
            # T1: call read_text_file -> answer
            _make_response(
                tool_calls=[{"id": "c1", "name": "read_text_file", "arguments": '{"path": "data.txt"}'}],
                tokens=100,
            ),
            _make_response(content="price=25", tokens=100),
            # T2: call calc -> answer
            _make_response(
                tool_calls=[{"id": "c2", "name": "calc", "arguments": '{"expression": "25 * 4"}'}],
                tokens=100,
            ),
            _make_response(content="结果是 100", tokens=100),
        ]
    )

    res = run_pipeline(
        planner_client=planner_client,
        executor_client=executor_client,
        planner_model="strong-planner",
        executor_model="cheap-executor",
        task="读价格并乘 4",
        registry=registry,
        verbose=False,
    )

    assert res.all_passed is True
    assert len(res.reports) == 2
    assert res.planner_tokens == 140
    assert res.executor_tokens == 400
    assert res.executor_token_ratio > 0.7
    assert all(c["model"] == "strong-planner" for c in planner_client.calls)
    assert all(c["model"] == "cheap-executor" for c in executor_client.calls)


def test_verifier_triggers_rework_and_passes_on_retry(tmp_path: Path):
    registry = build_default_registry(tmp_path)
    plan_json = json.dumps(
        {"subtasks": [{"id": "T1", "goal": "算 12 * 12", "acceptance": "必须用 calc 工具并给出 144"}]}
    )
    planner_client = FakeClient(
        [
            _make_response(content=plan_json, tokens=50),
            _make_response(content='{"passed": false, "feedback": "算错了，请调用 calc 重算"}', tokens=20),
            _make_response(content='{"passed": true, "feedback": "144 正确"}', tokens=20),
        ]
    )
    executor_client = FakeClient(
        [
            _make_response(content="我觉得是 124", tokens=60),  # attempt 1: wrong
            _make_response(
                tool_calls=[{"id": "c1", "name": "calc", "arguments": '{"expression": "12 * 12"}'}],
                tokens=60,
            ),
            _make_response(content="调用 calc 得到 144", tokens=60),  # attempt 2: fixed
        ]
    )

    res = run_pipeline(
        planner_client=planner_client,
        executor_client=executor_client,
        planner_model="strong-planner",
        executor_model="cheap-executor",
        task="算 12 * 12",
        registry=registry,
        max_retries=1,
        verbose=False,
    )

    assert res.all_passed is True
    assert res.reports[0].attempts == 2
    assert "144" in res.reports[0].output
    # Ensure reviewer feedback was injected into attempt 2's prompt
    retry_user_msg = executor_client.calls[1]["messages"][-1]["content"]
    assert "算错了，请调用 calc 重算" in retry_user_msg


def test_retry_limit_stops_gracefully_and_invalid_plan_falls_back(tmp_path: Path):
    registry = build_default_registry(tmp_path)
    planner_client = FakeClient(
        [
            _make_response(content="not valid json at all", tokens=30),  # triggers fallback single subtask
            _make_response(content='{"passed": false, "feedback": "still wrong"}', tokens=20),
            _make_response(content='{"passed": false, "feedback": "still wrong 2"}', tokens=20),
        ]
    )
    executor_client = FakeClient(
        [
            _make_response(content="bad 1", tokens=40),
            _make_response(content="bad 2", tokens=40),
        ]
    )

    res = run_pipeline(
        planner_client=planner_client,
        executor_client=executor_client,
        planner_model="strong-planner",
        executor_model="cheap-executor",
        task="兜底测试任务",
        registry=registry,
        max_retries=1,
        verbose=False,
    )

    assert res.all_passed is False
    assert len(res.reports) == 1
    assert res.reports[0].attempts == 2
    assert res.reports[0].passed is False
