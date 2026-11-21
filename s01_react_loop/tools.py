"""S01 内置的 3 个最小安全工具：AST 计算器 + 沙箱目录列表 + 沙箱文件读取。"""

from __future__ import annotations

import ast
import operator
from pathlib import Path
from typing import Callable

from s01_react_loop.agent import Tool, ToolRegistry

_BIN_OPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def safe_calc(expression: str) -> str:
    """只允许四则/幂运算的 AST 计算器，避免 eval() 注入风险。"""

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            return _BIN_OPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _UNARY_OPS[type(node.op)](_eval(node.operand))
        raise ValueError(f"Unsupported expression syntax: {type(node).__name__}")

    tree = ast.parse(expression, mode="eval")
    result = _eval(tree)
    return str(int(result) if isinstance(result, float) and result.is_integer() else result)


def _resolve_in_sandbox(root: Path, rel_path: str) -> Path:
    base = root.resolve()
    target = (base / rel_path).resolve()
    if target != base and base not in target.parents:
        raise PermissionError(f"Path escapes sandbox: {rel_path}")
    return target


def build_default_registry(workspace_dir: Path, max_read_chars: int = 4000) -> ToolRegistry:
    """构造绑定在指定沙箱目录下的默认工具集。"""
    root = workspace_dir.resolve()

    def list_files(path: str = ".") -> str:
        target = _resolve_in_sandbox(root, path)
        if not target.is_dir():
            return f"Error: '{path}' is not a directory."
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir() if not p.name.startswith("."))
        return "\n".join(entries) if entries else "(empty directory)"

    def read_text_file(path: str) -> str:
        target = _resolve_in_sandbox(root, path)
        if not target.is_file():
            return f"Error: '{path}' is not a file."
        content = target.read_text(encoding="utf-8")
        if len(content) > max_read_chars:
            return content[:max_read_chars] + f"\n... [truncated at {max_read_chars} chars]"
        return content

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="calc",
            description="Safely evaluate a basic arithmetic expression (e.g. '(12 + 8) * 3').",
            parameters={
                "type": "object",
                "properties": {"expression": {"type": "string", "description": "Arithmetic expression"}},
                "required": ["expression"],
            },
            fn=safe_calc,
        )
    )
    registry.register(
        Tool(
            name="list_files",
            description="List files and subdirectories inside the project workspace.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative directory path, default '.'"}},
            },
            fn=list_files,
        )
    )
    registry.register(
        Tool(
            name="read_text_file",
            description="Read a UTF-8 text file inside the project workspace.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative file path"}},
                "required": ["path"],
            },
            fn=read_text_file,
        )
    )
    return registry
