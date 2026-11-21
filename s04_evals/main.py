"""S04 CLI 入口：跑 Golden Dataset 回归评测、保存报告并与 Baseline 对比。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from dotenv import load_dotenv
from openai import OpenAI

from s01_react_loop.tools import build_default_registry
from s04_evals.evaluator import EvalReport, compare_reports, load_dataset, run_eval_suite


class _DemoMockClient:
    """--mock 离线体验模式：无需 API Key 即可跑通完整 Eval 报表与对比流程。"""

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, tools=None):
        last_role = messages[-1]["role"]
        user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
        if last_role == "tool":
            obs = " | ".join(m["content"][:180] for m in messages if m["role"] == "tool")
            return self._resp(content=f"已根据工具结果整理答案：{obs}（如遇越权已被沙箱拒绝）")

        if "(125 + 75) * 4" in user_msg:
            return self._resp(tool_calls=[("c1", "calc", '{"expression": "(125 + 75) * 4"}')])
        if "列出当前工作区" in user_msg:
            return self._resp(tool_calls=[("c2", "list_files", '{"path": "."}')])
        if "pyproject.toml" in user_msg:
            return self._resp(
                tool_calls=[
                    ("c3", "read_text_file", '{"path": "pyproject.toml"}'),
                    ("c4", "calc", '{"expression": "24 * 7"}'),
                ]
            )
        if "../../etc/passwd" in user_msg:
            return self._resp(tool_calls=[("c5", "read_text_file", '{"path": "../../etc/passwd"}')])
        return self._resp(content="ReAct Agent 是交替进行推理（Reason）与工具行动（Act）的循环架构。")

    @staticmethod
    def _resp(content: str = "", tool_calls: list[tuple[str, str, str]] | None = None):
        parsed = [
            SimpleNamespace(id=tid, function=SimpleNamespace(name=name, arguments=args))
            for tid, name, args in (tool_calls or [])
        ]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=parsed))],
            usage=SimpleNamespace(total_tokens=45),
        )


def _print_report(report: EvalReport) -> None:
    print(f"\n=== Eval Report ({report.model}) ===")
    print(
        f"Pass Rate: {report.passed_cases}/{report.total_cases} ({report.pass_rate:.0%}) | "
        f"Tool Acc: {report.tool_accuracy:.0%} | Avg Steps: {report.avg_steps} | "
        f"Tokens: {report.total_tokens} | Avg Latency: {report.avg_latency_ms}ms"
    )
    if report.avg_judge_score is not None:
        print(f"Avg LLM-as-a-Judge Score: {report.avg_judge_score}/5.0")
    print("-" * 72)
    for o in report.outcomes:
        mark = "PASS" if o.passed else "FAIL"
        extra = f" | reasons={o.failure_reasons}" if o.failure_reasons else ""
        judge = f" | judge={o.judge_score}/5" if o.judge_score is not None else ""
        print(f"[{mark}] {o.case_id:<24} tools={o.called_tools} steps={o.steps} tok={o.tokens}{judge}{extra}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run S04 offline/online Agent evaluation suite.")
    parser.add_argument("--dataset", type=Path, default=Path(__file__).with_name("dataset.json"))
    parser.add_argument("--mock", action="store_true", help="Run with built-in mock client (0 API cost).")
    parser.add_argument("--judge", action="store_true", help="Enable LLM-as-a-Judge scoring (real API mode).")
    parser.add_argument("--save", type=Path, default=None, help="Save JSON report to path.")
    parser.add_argument("--baseline", type=Path, default=None, help="Compare against a previous JSON report.")
    args = parser.parse_args()

    cases = load_dataset(args.dataset)
    registry = build_default_registry(Path.cwd())

    if args.mock:
        client: Any = _DemoMockClient()
        model = "demo-mock"
        judge_client = None
    else:
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            print("Missing OPENAI_API_KEY. Use --mock for offline demo, or configure .env.", file=sys.stderr)
            sys.exit(1)
        model = os.getenv("OPENAI_MODEL", "deepseek-chat")
        client = OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_BASE_URL") or None)
        judge_client = client if args.judge else None

    report = run_eval_suite(
        client=client,
        model=model,
        cases=cases,
        registry=registry,
        judge_client=judge_client,
        judge_model=os.getenv("PLANNER_MODEL") or model,
    )
    _print_report(report)

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nSaved report -> {args.save}")

    if args.baseline:
        base_data = json.loads(args.baseline.read_text(encoding="utf-8"))
        diff = compare_reports(base_data, report.to_dict())
        print("\n=== Baseline Diff ===")
        print(json.dumps(diff, ensure_ascii=False, indent=2))
        if diff["regressions"]:
            print(f"REGRESSION DETECTED: {diff['regressions']}", file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
