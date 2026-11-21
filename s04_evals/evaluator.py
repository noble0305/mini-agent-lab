"""S04: Golden Dataset 离线评测引擎（确定性规则 + 可选 LLM-as-a-Judge + Baseline 回归对比）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
import time
from typing import Any

from s01_react_loop.agent import ToolRegistry, run_agent


@dataclass(frozen=True)
class EvalCase:
    id: str
    prompt: str
    expected_tools: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    expected_keywords: list[str] = field(default_factory=list)
    any_keywords: list[str] = field(default_factory=list)
    rubric: str = ""


@dataclass
class CaseOutcome:
    case_id: str
    passed: bool
    tool_ok: bool
    answer_ok: bool
    called_tools: list[str]
    steps: int
    tokens: int
    latency_ms: int
    stopped_reason: str
    answer_preview: str
    judge_score: int | None = None
    judge_reason: str = ""
    failure_reasons: list[str] = field(default_factory=list)


@dataclass
class EvalReport:
    model: str
    total_cases: int
    passed_cases: int
    tool_accuracy: float
    pass_rate: float
    avg_steps: float
    total_tokens: int
    avg_latency_ms: float
    avg_judge_score: float | None
    outcomes: list[CaseOutcome]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_dataset(path: Path) -> list[EvalCase]:
    raw_items = json.loads(path.read_text(encoding="utf-8"))
    return [
        EvalCase(
            id=item["id"],
            prompt=item["prompt"],
            expected_tools=list(item.get("expected_tools") or []),
            forbidden_tools=list(item.get("forbidden_tools") or []),
            expected_keywords=list(item.get("expected_keywords") or []),
            any_keywords=list(item.get("any_keywords") or []),
            rubric=str(item.get("rubric") or ""),
        )
        for item in raw_items
    ]


def _judge_answer(
    client: Any,
    model: str,
    case: EvalCase,
    answer: str,
    called_tools: list[str],
) -> tuple[int | None, str]:
    """可选的 LLM-as-a-Judge（1~5 分制），解析失败返回 None 不破坏主评测。"""
    system = (
        "You are an objective AI evaluation judge. Score the assistant answer from 1 to 5 "
        "strictly against the rubric. "
        'Reply ONLY with JSON: {"score": 1-5, "reason": "one short sentence"}'
    )
    user = (
        f"Prompt: {case.prompt}\n"
        f"Tools called: {called_tools}\n"
        f"Rubric: {case.rubric}\n"
        f"Answer: {answer}"
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        text = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", text, flags=re.S)
        data = json.loads(m.group(0) if m else text)
        score = int(data["score"])
        score = max(1, min(5, score))
        return score, str(data.get("reason") or "").strip()
    except Exception as e:
        return None, f"judge_error: {type(e).__name__}"


def evaluate_case(
    client: Any,
    model: str,
    case: EvalCase,
    registry: ToolRegistry,
    system_prompt: str | None = None,
    max_steps: int = 5,
    judge_client: Any | None = None,
    judge_model: str | None = None,
) -> CaseOutcome:
    t0 = time.perf_counter()
    kwargs: dict[str, Any] = {
        "client": client,
        "model": model,
        "user_prompt": case.prompt,
        "registry": registry,
        "max_steps": max_steps,
        "verbose": False,
    }
    if system_prompt is not None:
        kwargs["system_prompt"] = system_prompt

    res = run_agent(**kwargs)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    called_tools = [entry["tool"] for entry in res.tool_calls_log]
    called_set = set(called_tools)

    reasons: list[str] = []
    missing_tools = [t for t in case.expected_tools if t not in called_set]
    bad_tools = [t for t in case.forbidden_tools if t in called_set]
    unknown_calls = [e["tool"] for e in res.tool_calls_log if "unknown tool" in e["output"]]

    if missing_tools:
        reasons.append(f"missing tools: {missing_tools}")
    if bad_tools:
        reasons.append(f"called forbidden tools: {bad_tools}")
    if unknown_calls:
        reasons.append(f"unknown tools: {unknown_calls}")
    tool_ok = not (missing_tools or bad_tools or unknown_calls)

    lower_ans = res.answer.lower()
    answer_ok = res.stopped_reason == "completed"
    if not answer_ok:
        reasons.append(f"stopped_reason={res.stopped_reason}")

    if case.any_keywords:
        if not any(kw.lower() in lower_ans for kw in case.any_keywords):
            answer_ok = False
            reasons.append(f"missed all any_keywords: {case.any_keywords}")
    else:
        missing_kw = [kw for kw in case.expected_keywords if kw.lower() not in lower_ans]
        if missing_kw:
            answer_ok = False
            reasons.append(f"missing keywords: {missing_kw}")

    judge_score: int | None = None
    judge_reason = ""
    if judge_client is not None and case.rubric:
        judge_score, judge_reason = _judge_answer(
            judge_client, judge_model or model, case, res.answer, called_tools
        )

    passed = tool_ok and answer_ok and (judge_score is None or judge_score >= 4)
    if judge_score is not None and judge_score < 4:
        reasons.append(f"judge_score={judge_score} (<4)")

    return CaseOutcome(
        case_id=case.id,
        passed=passed,
        tool_ok=tool_ok,
        answer_ok=answer_ok,
        called_tools=called_tools,
        steps=res.steps,
        tokens=res.total_tokens,
        latency_ms=latency_ms,
        stopped_reason=res.stopped_reason,
        answer_preview=res.answer[:140],
        judge_score=judge_score,
        judge_reason=judge_reason,
        failure_reasons=reasons,
    )


def run_eval_suite(
    client: Any,
    model: str,
    cases: list[EvalCase],
    registry: ToolRegistry,
    system_prompt: str | None = None,
    judge_client: Any | None = None,
    judge_model: str | None = None,
) -> EvalReport:
    outcomes = [
        evaluate_case(
            client=client,
            model=model,
            case=c,
            registry=registry,
            system_prompt=system_prompt,
            judge_client=judge_client,
            judge_model=judge_model,
        )
        for c in cases
    ]
    n = len(outcomes)
    passed_count = sum(1 for o in outcomes if o.passed)
    tool_ok_count = sum(1 for o in outcomes if o.tool_ok)
    judge_scores = [o.judge_score for o in outcomes if o.judge_score is not None]

    return EvalReport(
        model=model,
        total_cases=n,
        passed_cases=passed_count,
        tool_accuracy=round(tool_ok_count / n, 4) if n else 0.0,
        pass_rate=round(passed_count / n, 4) if n else 0.0,
        avg_steps=round(sum(o.steps for o in outcomes) / n, 2) if n else 0.0,
        total_tokens=sum(o.tokens for o in outcomes),
        avg_latency_ms=round(sum(o.latency_ms for o in outcomes) / n, 1) if n else 0.0,
        avg_judge_score=round(sum(judge_scores) / len(judge_scores), 2) if judge_scores else None,
        outcomes=outcomes,
    )


def compare_reports(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """对比基线报告与当前报告，识别指标 Delta 与具体 Regression / Improvement。"""
    base_map = {o["case_id"]: o["passed"] for o in baseline.get("outcomes", [])}
    curr_map = {o["case_id"]: o["passed"] for o in current.get("outcomes", [])}

    regressions = sorted(cid for cid, ok in curr_map.items() if base_map.get(cid) is True and not ok)
    improvements = sorted(cid for cid, ok in curr_map.items() if base_map.get(cid) is False and ok)

    return {
        "baseline_model": baseline.get("model"),
        "current_model": current.get("model"),
        "pass_rate_delta": round(current["pass_rate"] - baseline["pass_rate"], 4),
        "tool_accuracy_delta": round(current["tool_accuracy"] - baseline["tool_accuracy"], 4),
        "tokens_delta": current["total_tokens"] - baseline["total_tokens"],
        "regressions": regressions,
        "improvements": improvements,
    }
