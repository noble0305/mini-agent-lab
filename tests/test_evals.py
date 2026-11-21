"""S04 离线单元测试：验证确定性评测指标、LLM-as-a-Judge 与 Baseline 回归检测。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from s01_react_loop.tools import build_default_registry
from s04_evals.evaluator import EvalCase, compare_reports, load_dataset, run_eval_suite


def _make_response(content: str = "", tool_calls: list[dict] | None = None, tokens: int = 40):
    parsed = [
        SimpleNamespace(
            id=tc["id"],
            function=SimpleNamespace(name=tc["name"], arguments=tc["arguments"]),
        )
        for tc in (tool_calls or [])
    ]
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=parsed))],
        usage=SimpleNamespace(total_tokens=tokens),
    )


class FakeClient:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, tools=None):
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def test_eval_suite_metrics_and_tool_accuracy(tmp_path: Path):
    registry = build_default_registry(tmp_path)
    cases = [
        EvalCase(
            id="C1_pass",
            prompt="算 10 * 5",
            expected_tools=["calc"],
            expected_keywords=["50"],
        ),
        EvalCase(
            id="C2_wrong_tool",
            prompt="直接回答不要调工具",
            expected_tools=[],
            forbidden_tools=["calc"],
            expected_keywords=["OK"],
        ),
    ]
    client = FakeClient(
        [
            # C1: calls calc -> returns 50 (PASS)
            _make_response(tool_calls=[{"id": "1", "name": "calc", "arguments": '{"expression": "10 * 5"}'}]),
            _make_response(content="结果是 50"),
            # C2: violates forbidden_tools by calling calc (FAIL)
            _make_response(tool_calls=[{"id": "2", "name": "calc", "arguments": '{"expression": "1+1"}'}]),
            _make_response(content="OK"),
        ]
    )

    report = run_eval_suite(client, "mock-model", cases, registry)
    assert report.total_cases == 2
    assert report.passed_cases == 1
    assert report.pass_rate == 0.5
    assert report.tool_accuracy == 0.5
    assert report.outcomes[0].passed is True
    assert report.outcomes[1].passed is False
    assert "forbidden" in " ".join(report.outcomes[1].failure_reasons)


def test_llm_as_judge_scoring_and_error_resilience(tmp_path: Path):
    registry = build_default_registry(tmp_path)
    cases = [
        EvalCase(id="J1_high", prompt="Q1", expected_keywords=["ok"], rubric="Be clear."),
        EvalCase(id="J2_low", prompt="Q2", expected_keywords=["ok"], rubric="Be thorough."),
        EvalCase(id="J3_err", prompt="Q3", expected_keywords=["ok"], rubric="Any rubric."),
    ]
    agent_client = FakeClient(
        [
            _make_response(content="ok clear answer"),
            _make_response(content="ok vague"),
            _make_response(content="ok fine"),
        ]
    )
    judge_client = FakeClient(
        [
            _make_response(content='{"score": 5, "reason": "clear and accurate"}'),
            _make_response(content='{"score": 2, "reason": "too vague"}'),
            RuntimeError("judge timeout"),
        ]
    )

    report = run_eval_suite(agent_client, "agent-m", cases, registry, judge_client=judge_client, judge_model="judge-m")
    assert report.outcomes[0].passed is True and report.outcomes[0].judge_score == 5
    assert report.outcomes[1].passed is False and report.outcomes[1].judge_score == 2
    assert report.outcomes[2].passed is True and report.outcomes[2].judge_score is None
    assert report.avg_judge_score == 3.5


def test_baseline_regression_diff_and_bundled_dataset_loads():
    bundled = load_dataset(Path(__file__).resolve().parents[1] / "s04_evals" / "dataset.json")
    assert len(bundled) == 5

    baseline = {
        "model": "v1",
        "pass_rate": 0.8,
        "tool_accuracy": 0.8,
        "total_tokens": 500,
        "outcomes": [
            {"case_id": "E01", "passed": True},
            {"case_id": "E02", "passed": False},
        ],
    }
    current = {
        "model": "v2",
        "pass_rate": 0.6,
        "tool_accuracy": 1.0,
        "total_tokens": 420,
        "outcomes": [
            {"case_id": "E01", "passed": False},
            {"case_id": "E02", "passed": True},
        ],
    }
    diff = compare_reports(baseline, current)
    assert diff["pass_rate_delta"] == -0.2
    assert diff["tool_accuracy_delta"] == 0.2
    assert diff["tokens_delta"] == -80
    assert diff["regressions"] == ["E01"]
    assert diff["improvements"] == ["E02"]
