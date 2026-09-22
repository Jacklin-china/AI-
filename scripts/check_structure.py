"""Small repository boundary guard; no runtime dependency or file mutation."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_TOP_LEVEL = {
    ".git", ".idea", ".pytest_cache", ".ruff_cache", ".run", ".uv-cache",
    ".venv", ".vscode", ".workbuddy", "assets", "config", "data", "db", "docs",
    "evals", "frontend", "scripts", "skills", "src", "tests",
}
PARALLEL_NAME = re.compile(r"_(?:v2|new|fixed|backup)$", re.IGNORECASE)
SENSITIVE_LOG = re.compile(
    r"\blogger\.(?:debug|info|warning|error|critical)\([^\n]*"
    r"(?:api_key|secret_key|access_key|password|authorization|cookie)",
    re.IGNORECASE,
)
FORBIDDEN_IMPORTS = {
    "config": {"core", "capabilities", "adapters", "domains", "shells", "tools"},
    "core": {"adapters", "domains", "shells"},
    "domains": {"adapters", "shells"},
}


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def violations() -> list[str]:
    """Return actionable structure problems without touching generated/runtime data."""
    problems = [
        f"unauthorized top-level directory: {path.name}"
        for path in ROOT.iterdir() if path.is_dir() and path.name not in ALLOWED_TOP_LEVEL
    ]
    sources = [*((ROOT / "src" / "kantoku").rglob("*.py")),
               *((ROOT / "frontend" / "src").rglob("*.ts")),
               *((ROOT / "frontend" / "src").rglob("*.vue"))]
    for path in sources:
        relative = path.relative_to(ROOT)
        if PARALLEL_NAME.search(path.stem):
            problems.append(f"parallel implementation name: {relative}")
        if path.suffix != ".py":
            continue
        content = path.read_text(encoding="utf-8")
        if SENSITIVE_LOG.search(content):
            problems.append(f"possible raw secret in logger call: {relative}")
        layer = relative.parts[2] if len(relative.parts) > 2 else ""
        forbidden = FORBIDDEN_IMPORTS.get(layer, set())
        if forbidden:
            for name in _imports(ast.parse(content, filename=str(path))):
                parts = name.split(".")
                if len(parts) >= 2 and parts[0] == "kantoku" and parts[1] in forbidden:
                    problems.append(f"illegal dependency {relative}: {name}")
    return sorted(problems)


def main() -> int:
    problems = violations()
    for problem in problems:
        print(problem)
    if not problems:
        print("Structure guard: PASS")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
