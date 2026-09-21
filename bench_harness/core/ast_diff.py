"""Python AST-level change measurement (reward surgical edits)."""

from __future__ import annotations

import ast
import difflib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AstDiffResult:
    """Quantified difference between two Python sources."""

    changed_nodes: int
    total_nodes_before: int
    total_nodes_after: int
    lines_added: int
    lines_deleted: int
    node_change_ratio: float
    penalty: float
    syntax_error: bool = False

    def to_dict(self) -> dict:
        return {
            "changed_nodes": self.changed_nodes,
            "total_nodes_before": self.total_nodes_before,
            "total_nodes_after": self.total_nodes_after,
            "lines_added": self.lines_added,
            "lines_deleted": self.lines_deleted,
            "node_change_ratio": self.node_change_ratio,
            "penalty": self.penalty,
            "syntax_error": self.syntax_error,
        }

    def summary_dict(self) -> dict:
        return {"changed_nodes": self.changed_nodes, "penalty": self.penalty}


def _node_sequence(source: str) -> list[str] | None:
    """Flatten an AST into DFS pre-order structural tokens with attributes; None when unparseable."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None

    seq: list[str] = []

    def _dfs(node: ast.AST) -> None:
        name = type(node).__name__
        attr = ""
        if isinstance(node, ast.Name):
            attr = f":{node.id}"
        elif isinstance(node, ast.Attribute):
            attr = f":{node.attr}"
        elif isinstance(node, ast.FunctionDef):
            attr = f":{node.name}"
        elif isinstance(node, ast.arg):
            attr = f":{node.arg}"
        elif isinstance(node, ast.Constant):
            attr = f":{type(node.value).__name__}"
        seq.append(f"{name}{attr}")
        for child in ast.iter_child_nodes(node):
            _dfs(child)

    _dfs(tree)
    return seq


def compute_penalty(
    node_change_ratio: float,
    changed_nodes: int,
    changed_lines: int,
) -> float:
    """Map change magnitude to a penalty factor in [0.1, 1.0].

    1.0 marks a surgical single-line / minimal edit; the factor decays
    linearly towards 0.1 for wholesale rewrites.
    """
    if node_change_ratio <= 0.0:
        return 1.0
    if changed_nodes <= 4 and changed_lines <= 2:
        return 1.0
    penalty = 1.0 - (0.7 * node_change_ratio + 0.3 * min(1.0, changed_lines / 50.0))
    return round(max(0.1, min(1.0, penalty)), 3)


class AstDiffAnalyzer:
    """AST node-change vs line-change analyzer with rewrite penalty."""

    def analyze(self, before_source: str, after_source: str) -> AstDiffResult:
        before_seq = _node_sequence(before_source)
        after_seq = _node_sequence(after_source)
        before_lines = before_source.splitlines()
        after_lines = after_source.splitlines()

        lines_added = 0
        lines_deleted = 0
        for line in difflib.unified_diff(before_lines, after_lines, lineterm=""):
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                lines_added += 1
            elif line.startswith("-"):
                lines_deleted += 1

        if before_seq is None or after_seq is None:
            changed_lines = lines_added + lines_deleted
            total_lines = max(len(before_lines), 1)
            ratio = min(1.0, changed_lines / max(total_lines, 1))
            penalty = compute_penalty(ratio, changed_lines, changed_lines)
            return AstDiffResult(
                changed_nodes=changed_lines,
                total_nodes_before=len(before_seq or []),
                total_nodes_after=len(after_seq or []),
                lines_added=lines_added,
                lines_deleted=lines_deleted,
                node_change_ratio=round(ratio, 4),
                penalty=penalty,
                syntax_error=True,
            )

        matcher = difflib.SequenceMatcher(a=before_seq, b=after_seq, autojunk=False)
        changed = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag != "equal":
                changed += (i2 - i1) + (j2 - j1)
        denominator = max(len(before_seq), len(after_seq), 1)
        # A pure replacement touches both sides; normalize so a full
        # rewrite saturates at ~1.0 instead of ~2.0.
        node_ratio = min(1.0, changed / (2.0 * denominator))
        penalty = compute_penalty(node_ratio, changed, lines_added + lines_deleted)
        return AstDiffResult(
            changed_nodes=changed,
            total_nodes_before=len(before_seq),
            total_nodes_after=len(after_seq),
            lines_added=lines_added,
            lines_deleted=lines_deleted,
            node_change_ratio=round(node_ratio, 4),
            penalty=penalty,
            syntax_error=False,
        )

    def analyze_files(self, before_path: str | Path, after_path: str | Path) -> AstDiffResult:
        before_source = Path(before_path).read_text(encoding="utf-8")
        after_source = Path(after_path).read_text(encoding="utf-8")
        return self.analyze(before_source, after_source)
