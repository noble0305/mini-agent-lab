"""S02: Planner(强模型) + Cheap Executor(小模型) 两阶段双模型路由流水线。"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any

from s01_react_loop.agent import ToolRegistry, run_agent


@dataclass(frozen=True)
class SubTask:
    id: str
    goal: str
    acceptance: str


@dataclass
class SubTaskReport:
    subtask: SubTask
    output: str
    passed: bool
    attempts: int
    feedback: str
    executor_tokens: int = 0
    verifier_tokens: int = 0


@dataclass
class PipelineResult:
    final_answer: str
    reports: list[SubTaskReport] = field(default_factory=list)
    planner_tokens: int = 0
    executor_tokens: int = 0
    all_passed: bool = False

    @property
    def total_tokens(self) -> int:
        return self.planner_tokens + self.executor_tokens

    @property
    def executor_token_ratio(self) -> float:
        return (self.executor_tokens / self.total_tokens) if self.total_tokens else 0.0


def _parse_json_object(raw: str) -> dict[str, Any]:
    """容错解析模型返回的 JSON（自动剥离 ```json 代码块）。"""
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object.")
    return data


def _chat_text(client: Any, model: str, system: str, user: str) -> tuple[str, int]:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    tokens = getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
    content = (resp.choices[0].message.content or "").strip()
    return content, tokens


def create_plan(
    client: Any,
    model: str,
    task: str,
    registry: ToolRegistry,
    max_subtasks: int = 4,
) -> tuple[list[SubTask], int]:
    """强模型负责将大任务拆解为带验收标准的有序子任务列表。"""
    tool_names = [s["function"]["name"] for s in registry.schemas()]
    system = (
        "You are a senior engineering planner. Break the user's task into concise, ordered subtasks "
        f"(at most {max_subtasks}). Available executor tools: {tool_names}. "
        'Reply ONLY with JSON: {"subtasks": [{"id": "T1", "goal": "...", "acceptance": "..."}]}'
    )
    raw, tokens = _chat_text(client, model, system, task)
    try:
        parsed = _parse_json_object(raw)
        items = parsed.get("subtasks") or []
        subtasks = [
            SubTask(
                id=str(item.get("id") or f"T{idx}"),
                goal=str(item["goal"]).strip(),
                acceptance=str(item.get("acceptance") or "Output directly answers the goal.").strip(),
            )
            for idx, item in enumerate(items[:max_subtasks], start=1)
            if isinstance(item, dict) and item.get("goal")
        ]
    except Exception:
        subtasks = []

    if not subtasks:
        subtasks = [SubTask(id="T1", goal=task, acceptance="Complete the user request accurately.")]
    return subtasks, tokens


def verify_subtask(
    client: Any,
    model: str,
    subtask: SubTask,
    output: str,
) -> tuple[bool, str, int]:
    """强模型对照验收标准审查小模型的产出，返回 (是否通过, 反馈, tokens)。"""
    system = (
        "You are a strict reviewer. Check whether the executor output satisfies the acceptance criteria. "
        'Reply ONLY with JSON: {"passed": true/false, "feedback": "one short sentence"}'
    )
    user = (
        f"Subtask goal: {subtask.goal}\n"
        f"Acceptance criteria: {subtask.acceptance}\n"
        f"Executor output:\n{output}"
    )
    raw, tokens = _chat_text(client, model, system, user)
    try:
        parsed = _parse_json_object(raw)
        return bool(parsed.get("passed", False)), str(parsed.get("feedback") or "").strip(), tokens
    except Exception as e:
        return False, f"Verifier JSON parse failed: {e}", tokens


def run_pipeline(
    planner_client: Any,
    planner_model: str,
    executor_model: str,
    task: str,
    registry: ToolRegistry,
    executor_client: Any | None = None,
    max_subtasks: int = 4,
    max_retries: int = 1,
    executor_max_steps: int = 4,
    verbose: bool = True,
) -> PipelineResult:
    """运行 Planner -> Cheap Executor(S01 Loop) -> Verifier 闭环。"""
    exec_client = executor_client or planner_client
    subtasks, planner_tokens = create_plan(planner_client, planner_model, task, registry, max_subtasks)
    if verbose:
        print(f"[planner:{planner_model}] split into {len(subtasks)} subtask(s):")
        for st in subtasks:
            print(f"  - {st.id}: {st.goal} | AC: {st.acceptance}")

    reports: list[SubTaskReport] = []
    completed_notes: list[str] = []
    executor_tokens = 0

    for st in subtasks:
        feedback = ""
        passed = False
        last_output = ""
        st_exec_tokens = 0
        st_ver_tokens = 0
        attempts = 0

        for attempt in range(1, max_retries + 2):
            attempts = attempt
            context_block = "\n".join(completed_notes) if completed_notes else "(none yet)"
            retry_note = f"\nPrevious attempt failed review: {feedback}\nFix this issue now." if feedback else ""
            exec_prompt = (
                f"Overall task: {task}\n"
                f"Completed context:\n{context_block}\n\n"
                f"Current subtask [{st.id}]: {st.goal}\n"
                f"Acceptance criteria: {st.acceptance}{retry_note}"
            )
            res = run_agent(
                client=exec_client,
                model=executor_model,
                user_prompt=exec_prompt,
                registry=registry,
                max_steps=executor_max_steps,
                verbose=verbose,
            )
            last_output = res.answer
            st_exec_tokens += res.total_tokens

            passed, feedback, v_tokens = verify_subtask(planner_client, planner_model, st, last_output)
            st_ver_tokens += v_tokens
            if verbose:
                status = "PASS" if passed else f"FAIL ({feedback})"
                print(f"[verify:{planner_model}] {st.id} attempt {attempt}: {status}")
            if passed:
                break

        executor_tokens += st_exec_tokens
        planner_tokens += st_ver_tokens
        reports.append(
            SubTaskReport(
                subtask=st,
                output=last_output,
                passed=passed,
                attempts=attempts,
                feedback=feedback,
                executor_tokens=st_exec_tokens,
                verifier_tokens=st_ver_tokens,
            )
        )
        completed_notes.append(f"- {st.id} ({'PASS' if passed else 'UNVERIFIED'}): {last_output[:400]}")

    final_answer = "\n\n".join(f"### {r.subtask.id}: {r.subtask.goal}\n{r.output}" for r in reports)
    return PipelineResult(
        final_answer=final_answer,
        reports=reports,
        planner_tokens=planner_tokens,
        executor_tokens=executor_tokens,
        all_passed=all(r.passed for r in reports),
    )
