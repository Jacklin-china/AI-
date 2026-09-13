"""分镜评测：读取固定案例、执行生成器并输出可追溯 Markdown 报告。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from kantoku.config import KantokuError, ToolError
from kantoku.memory import Persona
from kantoku.schemas.storyboard import Shot, Storyboard
from kantoku.tools.prompt_factory import PROMPT_VERSION, build_prompt


class EvalExpectation(BaseModel):
    """可自动判定的分镜期望。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    shot_count: int = Field(gt=0)
    required_characters: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_character_names(self) -> Self:
        if any(not name.strip() for name in self.required_characters):
            raise ValueError("required_characters 不允许空名称")
        return self


class EvalCase(BaseModel):
    """一条 JSONL 评测案例。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    input: str = Field(min_length=1)
    expect: EvalExpectation
    rubric: list[str] = Field(min_length=1)


class EvalResult(BaseModel):
    """单案例的自动评测结果。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    passed: bool
    shot_count: int
    field_completeness: float = Field(ge=0, le=1)
    missing_characters: list[str]
    error: str | None


def load_suite(path: Path) -> list[EvalCase]:
    """逐行读取 UTF-8 JSONL，并用行号报告安全错误。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ToolError(
            "无法读取评测集", detail=f"path={path}；error={type(error).__name__}"
        ) from error

    cases: list[EvalCase] = []
    ids: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            case = EvalCase.model_validate_json(line)
        except ValidationError as error:
            raise ToolError(
                "评测案例格式错误",
                detail=f"line={line_number}；error={type(error).__name__}",
            ) from error
        if case.id in ids:
            raise ToolError("评测案例 ID 重复", detail=f"line={line_number}；id={case.id}")
        ids.add(case.id)
        cases.append(case)
    if not cases:
        raise ToolError("评测集不能为空", detail=f"path={path}")
    return cases


def _field_completeness(storyboard: Storyboard) -> float:
    """计算六个下游字段满足数据契约的比例。"""
    if not storyboard.shots:
        return 0.0
    completed = 0
    for shot in storyboard.shots:
        completed += int(shot.shot_no > 0)
        completed += int(bool(shot.desc.strip()))
        completed += int(isinstance(shot.dialogue, str))
        completed += int(bool(shot.camera.strip()))
        completed += int(shot.duration_s > 0)
        completed += int(isinstance(shot.characters, list))
    return completed / (len(storyboard.shots) * 6)


def evaluate_case(
    case: EvalCase,
    generator: Callable[[str], Storyboard],
) -> EvalResult:
    """运行一例，只暴露项目异常的安全消息或未知异常类型。"""
    try:
        storyboard = generator(case.input)
    except KantokuError as error:
        return EvalResult(
            id=case.id,
            passed=False,
            shot_count=0,
            field_completeness=0.0,
            missing_characters=case.expect.required_characters,
            error=f"{type(error).__name__}: {error.message}",
        )
    except Exception as error:
        return EvalResult(
            id=case.id,
            passed=False,
            shot_count=0,
            field_completeness=0.0,
            missing_characters=case.expect.required_characters,
            error=f"UnexpectedError: {type(error).__name__}",
        )

    observed_characters = {
        character for shot in storyboard.shots for character in shot.characters
    }
    missing = sorted(set(case.expect.required_characters) - observed_characters)
    completeness = _field_completeness(storyboard)
    passed = (
        len(storyboard.shots) == case.expect.shot_count
        and completeness == 1.0
        and not missing
    )
    return EvalResult(
        id=case.id,
        passed=passed,
        shot_count=len(storyboard.shots),
        field_completeness=completeness,
        missing_characters=missing,
        error=None,
    )


def run_suite(
    cases: Sequence[EvalCase],
    generator: Callable[[str], Storyboard],
) -> list[EvalResult]:
    """串行运行评测，避免并发扩大花费或触发供应商限流。"""
    return [evaluate_case(case, generator) for case in cases]


def render_report(
    *,
    cases: Sequence[EvalCase],
    results: Sequence[EvalResult],
    model_name: str,
) -> str:
    """生成包含口径、汇总和逐例证据的 Markdown。"""
    if len(cases) != len(results):
        raise ToolError("评测案例与结果数量不一致")
    passed = sum(result.passed for result in results)
    rate = passed / len(results) if results else 0.0
    average_completeness = (
        sum(result.field_completeness for result in results) / len(results) if results else 0.0
    )
    lines = [
        "# M1 分镜生成评测报告",
        "",
        f"- 运行时间（UTC）：{datetime.now(UTC).isoformat()}",
        f"- 模型：`{model_name}`",
        f"- 案例：{len(results)} 条",
        f"- 通过：{passed}/{len(results)}（{rate:.1%}）",
        f"- 平均字段完整率：{average_completeness:.1%}",
        "- 自动口径：目标镜数、六字段数据契约、指定人物名称全部满足才通过。",
        "- 人工口径：叙事节奏、画面可执行性和连续性仍需逐例复核，不由自动分数替代。",
        "",
        "| 案例 | 结果 | 镜数 | 字段完整率 | 缺失人物 | 错误 |",
        "|---|---:|---:|---:|---|---|",
    ]
    for result in results:
        lines.append(
            "| {id} | {status} | {count} | {completeness:.1%} | {missing} | {error} |".format(
                id=result.id,
                status="通过" if result.passed else "失败",
                count=result.shot_count,
                completeness=result.field_completeness,
                missing="、".join(result.missing_characters) or "—",
                error=(result.error or "—").replace("|", "\\|"),
            )
        )
    lines.extend(["", "## 人工复核 Rubric", ""])
    for case in cases:
        lines.append(f"- `{case.id}`：{'；'.join(case.rubric)}")
    return "\n".join(lines) + "\n"


def write_report(path: Path, report: str) -> None:
    """原子保存报告，避免中断时留下半份验收记录。"""
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(report, encoding="utf-8", newline="\n")
        temporary.replace(path)
    except (OSError, UnicodeError) as error:
        raise ToolError(
            "无法保存评测报告", detail=f"path={path}；error={type(error).__name__}"
        ) from error


class ConsistencyInput(BaseModel):
    """一致性案例的镜头和相关人设。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    shot: Shot
    personas: list[Persona]


class ConsistencyExpectation(BaseModel):
    """Prompt 中必须出现和不得出现的稳定锚点。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    must_include: list[str] = Field(min_length=1)
    must_exclude: list[str] = Field(default_factory=list)


class ConsistencyEvalCase(BaseModel):
    """一条无需调用模型的一致性评测案例。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    input: ConsistencyInput
    expect: ConsistencyExpectation
    rubric: list[str] = Field(min_length=1)


class ConsistencyEvalResult(BaseModel):
    """一条 Prompt 锚点评测结果。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    passed: bool
    prompt_version: str
    missing: list[str]
    forbidden: list[str]
    error: str | None


def load_consistency_suite(path: Path) -> list[ConsistencyEvalCase]:
    """读取一致性 JSONL，并拒绝坏行和重复 ID。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ToolError(
            "无法读取一致性评测集",
            detail=f"path={path}；error={type(error).__name__}",
        ) from error

    cases: list[ConsistencyEvalCase] = []
    ids: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            case = ConsistencyEvalCase.model_validate_json(line)
        except ValidationError as error:
            raise ToolError(
                "一致性评测案例格式错误",
                detail=f"line={line_number}；error={type(error).__name__}",
            ) from error
        if case.id in ids:
            raise ToolError("一致性评测案例 ID 重复", detail=case.id)
        ids.add(case.id)
        cases.append(case)
    if not cases:
        raise ToolError("一致性评测集不能为空", detail=f"path={path}")
    return cases


def evaluate_consistency_case(case: ConsistencyEvalCase) -> ConsistencyEvalResult:
    """构建 Prompt 并检查角色锚点包含/排除规则。"""
    try:
        prompt = build_prompt(case.input.shot, case.input.personas)
    except KantokuError as error:
        return ConsistencyEvalResult(
            id=case.id,
            passed=False,
            prompt_version=PROMPT_VERSION,
            missing=case.expect.must_include,
            forbidden=[],
            error=f"{type(error).__name__}: {error.message}",
        )
    missing = [token for token in case.expect.must_include if token not in prompt]
    forbidden = [token for token in case.expect.must_exclude if token in prompt]
    return ConsistencyEvalResult(
        id=case.id,
        passed=not missing and not forbidden,
        prompt_version=PROMPT_VERSION,
        missing=missing,
        forbidden=forbidden,
        error=None,
    )


def run_consistency_suite(
    cases: Sequence[ConsistencyEvalCase],
) -> list[ConsistencyEvalResult]:
    """确定性运行全部 Prompt 一致性案例。"""
    return [evaluate_consistency_case(case) for case in cases]


def render_consistency_report(
    *,
    cases: Sequence[ConsistencyEvalCase],
    results: Sequence[ConsistencyEvalResult],
) -> str:
    """输出自动锚点结果，并保留人工五镜验收栏位。"""
    if len(cases) != len(results):
        raise ToolError("一致性案例与结果数量不一致")
    passed = sum(result.passed for result in results)
    rate = passed / len(results) if results else 0.0
    lines = [
        "# M2 Prompt 一致性评测报告",
        "",
        f"- 运行时间（UTC）：{datetime.now(UTC).isoformat()}",
        f"- Prompt 版本：`{PROMPT_VERSION}`",
        f"- 自动锚点通过：{passed}/{len(results)}（{rate:.1%}）",
        "- 人工五镜通过率：待人工看图后填写；不得用字符串命中率代替画面质量。",
        "",
        "| 案例 | 结果 | 缺失锚点 | 混入锚点 | 错误 |",
        "|---|---:|---|---|---|",
    ]
    for result in results:
        lines.append(
            "| {id} | {status} | {missing} | {forbidden} | {error} |".format(
                id=result.id,
                status="通过" if result.passed else "失败",
                missing="、".join(result.missing) or "—",
                forbidden="、".join(result.forbidden) or "—",
                error=(result.error or "—").replace("|", "\\|"),
            )
        )
    lines.extend(["", "## 人工复核 Rubric", ""])
    for case in cases:
        lines.append(f"- `{case.id}`：{'；'.join(case.rubric)}")
    return "\n".join(lines) + "\n"
