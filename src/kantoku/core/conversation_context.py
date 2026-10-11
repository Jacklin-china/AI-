"""Budgeted conversation memory over existing messages, Runs and JSON Artifacts.

No domain knowledge, provider, vector store or execution engine lives here.
Readers and model calls are injected by the existing conversation entry point.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kantoku.config import ToolError
from kantoku.core.runtime.models import ArtifactType


def token_size(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return max(1, sum(1 if ord(char) > 127 else .3 for char in text).__ceil__())


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal["message", "run", "artifact"]
    version: int | None = None
    fields: list[str] = Field(default_factory=list, max_length=30)


class MemoryFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(max_length=1000)
    source: SourceRef
    origin: Literal["user_request", "asset_fact", "model_choice"]
    status: Literal["active", "superseded", "withdrawn"] = "active"


class TaskProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    goal: str = Field(max_length=1000)
    status: Literal["active", "pending", "completed", "withdrawn"]
    facts: list[MemoryFact] = Field(default_factory=list, max_length=30)
    open_items: list[str] = Field(default_factory=list, max_length=20)


class HistoryRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["messages", "run", "artifact"] = "messages"
    id: str | None = Field(default=None, description="仅读取这一条消息或指定 Run/Artifact")
    before_id: str | None = Field(default=None, description="读取此消息之前的历史，不包含该消息")
    after_id: str | None = Field(default=None, description="读取此消息之后的历史，不包含该消息")
    query: str | None = Field(default=None, max_length=200,
                             description="范围内原文包含查询词的消息；不是继承决策")


class TurnUnderstanding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(min_length=1, max_length=100)
    relation: Literal["new", "continue", "revise", "compare", "recall"]
    goal: str = Field(max_length=2000)
    current_task_id: str | None = None
    response_artifact_id: str | None = None
    sources: list[SourceRef] = Field(default_factory=list, max_length=12)
    tasks: list[TaskProgress] = Field(default_factory=list, max_length=12)
    question: str | None = Field(default=None, max_length=300)
    reads: list[HistoryRead] = Field(default_factory=list, max_length=4)


class ConversationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overview: str = Field(max_length=6000)
    tasks: list[TaskProgress] = Field(default_factory=list, max_length=12)
    sources: list[SourceRef] = Field(default_factory=list, max_length=100)


@dataclass
class PreparedContext:
    understanding: TurnUnderstanding | None
    messages: list[dict[str, str]]
    evidence: dict[str, dict]
    summary_id: str | None
    through_message_id: str | None
    token_count: int
    degraded: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {"resolution": self.understanding.model_dump(mode="json")
                if self.understanding else None,
                "summary_id": self.summary_id, "through_message_id": self.through_message_id,
                "sources": [source.model_dump(mode="json") for source in
                            self.understanding.sources] if self.understanding else [],
                "available_sources": [{"id": key, "kind": value["kind"],
                             "version": value.get("version")} for key, value in
                            self.evidence.items()], "estimated_tokens": self.token_count,
                "degraded": list(self.degraded)}


class ConversationContext:
    """One turn-local view. Completion order never changes the message anchor."""

    def __init__(self, store, *, budget: int, summary_budget: int, recall_budget: int,
                 allowed_actions: tuple[str, ...] = ("chat", "create", "clarify",
                     "image.generate", "image.edit", "video.generate")):
        self.store = store
        self.budget = budget
        self.summary_budget = min(summary_budget, budget // 4)
        self.recall_budget = min(recall_budget, budget // 3)
        self.allowed_actions = allowed_actions

    @staticmethod
    def _parse(schema, raw):
        try:
            return schema.model_validate_json(raw)
        except ValidationError as error:
            failures = [{"path": item["loc"], "type": item["type"]} for item in
                        error.errors(include_input=False, include_context=False,
                                     include_url=False)[:20]]
            raise ToolError("上下文模型返回结构无效，原始消息保留",
                            detail=json.dumps(failures, ensure_ascii=False)) from None

    @staticmethod
    def _message(item) -> dict:
        return {"id": item.id, "kind": "message", "role": item.role.value,
                "content": item.content, "run_id": item.run_id,
                "artifact_id": item.artifact_id, "event_id": item.event_id,
                "reply_to_message_id": item.event_id.removeprefix("conversation-response:")
                if item.event_id and item.event_id.startswith("conversation-response:") else None}

    def _internal(self, conversation_id: str, node: str, before: list[str]):
        ranks = {identifier: index for index, identifier in enumerate(before)}
        return max((item for item in self.store.list_artifacts(
            conversation_id=conversation_id, type=ArtifactType.JSON, include_internal=True,
        ) if item.metadata.get("internal") and item.node_id == node
            and item.metadata.get("through_message_id") in ranks),
            key=lambda item: (ranks[item.metadata["through_message_id"]], item.created_at),
            default=None)

    def prepare(self, conversation_id: str, request: str, *, model_call: Callable,
                candidates: list[dict] | None = None, reader: Callable | None = None,
                interpret: bool = True, through_message_id: str | None = None) -> PreparedContext:
        self.store.get_conversation(conversation_id)
        degraded: list[str] = []

        def call(messages):
            if token_size(messages) > self.budget:
                raise ToolError("模型输入超过上下文 token 预算，原记录保留")
            return model_call(messages)
        history = self.store.list_conversation_messages(conversation_id)
        if through_message_id:
            index = next((i for i, item in enumerate(history)
                          if item.id == through_message_id), None)
            if index is None:
                raise ToolError("上下文锚点不属于当前会话")
            history = history[:index + 1]
        # The caller can prepare before persisting the incoming user message.
        incoming = None
        if through_message_id and history and history[-1].content == request and (
            history[-1].role.value == "user"
        ):
            incoming = history[-1]
            history = history[:-1]
        ids = {item.id for item in history}
        ordered_ids = [item.id for item in history]
        summary = self._internal(conversation_id, "conversation.summary", ordered_ids)
        covered = []
        if summary:
            # Previously saved summaries remain readable.
            if "covered_message_ids" in summary.metadata:
                covered = summary.metadata["covered_message_ids"]
            else:
                end = ordered_ids.index(summary.metadata["through_message_id"]) + 1
                covered = ordered_ids[:end]
                if (summary.metadata.get("covered_from_id") != ordered_ids[0]
                        or summary.metadata.get("covered_count") != end):
                    raise ToolError("摘要消息范围或版本未通过核验")
        by_id = {item.id: item for item in history}
        if not set(covered).issubset(ids):
            raise ToolError("摘要来源范围已失效，原始消息保留")
        pending = [item for item in history if item.id not in covered]
        state = self._internal(conversation_id, "conversation.context", ordered_ids)
        schema = TurnUnderstanding.model_json_schema()
        schema["properties"]["action"]["enum"] = list(self.allowed_actions)
        recent, cost = [], (2 * token_size(request) + token_size(schema) + 600 +
                            token_size(state.metadata.get("resolution") if state else None))
        recent_budget = self.budget - self.summary_budget - self.recall_budget
        for item in reversed(pending):
            size = token_size(self._message(item))
            if cost + size > recent_budget:
                break
            recent.insert(0, item)
            cost += size
        older = pending[:len(pending) - len(recent)]
        # Bounded incremental chunks avoid feeding the compactor an unbounded transcript.
        chunk_budget = min(recent_budget, self.budget - self.summary_budget - 600 -
                           token_size(ConversationSummary.model_json_schema()))
        while older:
            chunk, size = [], 0
            oversized = False
            for item in older:
                item_size = token_size(self._message(item))
                if size + item_size > chunk_budget and chunk:
                    break
                if item_size > chunk_budget:
                    # A single message larger than the whole compaction budget can
                    # never be folded into a summary. Leave it uncoverable (still
                    # re-readable) and stop instead of failing the turn.
                    degraded.append("oversized_message_skipped")
                    oversized = True
                    break
                chunk.append(item)
                size += item_size
            if oversized:
                break
            compact_input = {"previous_summary": summary.metadata["summary"] if summary else None,
                             "messages": [self._message(item) for item in chunk]}
            compressed = None
            for attempt in range(2):
                raw = call([
                    {"role": "system", "content": (
                        "压缩较早对话。保留各任务目标、用户纠正、有效约束、撤回内容、未完成事项和"
                        "关键来源 ID。用户原话、资产事实和模型创作选择分别标记；摘要不是用户事实。"
                        "user_request/asset_fact 的 value 必须是来源原文的连续摘录，不改写。"
                        "不能恢复已撤回约束。整个 JSON（含 tasks、facts、sources）必须小于 " +
                        str(self.summary_budget) + " token；中文字符约算一个 token。"
                        "为 JSON 字段和来源预留容量，文本和引用尽量控制在 " +
                        str(self.summary_budget // 2) + " token 内。"
                        "不逐条重复历史消息；可核对的原始记录始终保留，可通过范围或检索重新读取。"
                        + ("上一份摘要估计 " + str(token_size(compressed.model_dump())) +
                           " token，超过预算。现在只压缩提供的合法摘要，进一步缩短 overview、"
                           "合并重复进度和来源，不要丢失纠正或撤回关系。" if attempt else "") +
                        "顶层仅 overview/tasks/sources；facts 必须放在所属 tasks 内。"
                        "只返回 JSON：" +
                        json.dumps(ConversationSummary.model_json_schema(), ensure_ascii=False))},
                    {"role": "user", "content": json.dumps(
                        {"previous_summary": compressed.model_dump(), "messages": []}
                        if attempt else compact_input, ensure_ascii=False)},
                ])
                compressed = self._parse(ConversationSummary, raw)
                if token_size(compressed.model_dump()) <= self.summary_budget:
                    break
            if token_size(compressed.model_dump()) > self.summary_budget:
                # Keep the previous summary untouched and stop compacting rather
                # than failing the turn; the raw messages remain re-readable.
                degraded.append("summary_truncated")
                break
            permitted = {item.id: self._message(item) for item in chunk}
            previous_sources = {(item["kind"], item["id"]) for item in
                                summary.metadata["summary"]["sources"]} if summary else set()
            for source in compressed.sources:
                if (source.kind, source.id) not in previous_sources and source.id not in permitted:
                    raise ToolError("摘要引用了未提供的来源，原始记录保留")
                if source.kind != "message":
                    if reader is None:
                        raise ToolError("摘要来源无法核验，原始记录保留")
                    permitted[source.id] = reader(source.kind, source.id)
                    self._verify_source(source, permitted)
            for task in compressed.tasks:
                for fact in task.facts:
                    if fact.source.kind != "message" and fact.source.id not in permitted:
                        if reader is None:
                            raise ToolError("摘要事实来源无法核验，原始记录保留")
                        permitted[fact.source.id] = reader(fact.source.kind, fact.source.id)
            self._validate_facts(compressed.tasks, {**{key: self._message(value)
                                                      for key, value in by_id.items()},
                                                   **permitted})
            covered = [*covered, *(item.id for item in chunk)]
            summary = self.store.create_artifact(
                type=ArtifactType.JSON, conversation_id=conversation_id,
                node_id="conversation.summary", source="model_summary",
                version=summary.version + 1 if summary else 1,
                metadata={"internal": True, "summary": compressed.model_dump(mode="json"),
                          "covered_from_id": covered[0], "covered_count": len(covered),
                          "through_message_id": chunk[-1].id,
                          "parent_artifact_id": summary.id if summary else None},
            )
            older = older[len(chunk):]
        evidence = {item.id: self._message(item) for item in recent}
        if incoming:
            evidence[incoming.id] = self._message(incoming)
        candidates = candidates or []
        recalled, recall_cost = {}, 0
        available: list[dict] = []
        for item in candidates:
            # ``read_required`` candidates still advertise a *pointer* so the model
            # knows they exist, but historical bodies are only quoted when the model
            # explicitly asks for them via ``reads``. Keeping the pointer in
            # ``recalled`` preserves the legacy contract that every candidate is
            # recallable; the pointer itself is cheap and carries no stale facts.
            pointer = {"id": item["id"], "kind": item.get("kind"),
                       "version": item.get("version")}
            if item.get("read_required"):
                available.append({**pointer, "read_required": True})
            size = token_size(item)
            if recall_cost + size > self.recall_budget // 4:
                continue
            recalled[item["id"]] = item
            recall_cost += size
        evidence.update(recalled)
        base = {"current_request": request, "recent_dialogue": [self._message(i) for i in recent],
                "current_message_id": incoming.id if incoming else None,
                "summary": summary.metadata["summary"] if summary else None,
                "task_state": state.metadata.get("resolution") if state else None,
                "history_range": {"first_id": history[0].id if history else None,
                                  "last_id": history[-1].id if history else None,
                                  "message_count": len(history)}, "recalled": recalled,
                "available": available}
        understanding = None
        if interpret:
            for attempt in range(3):
                # Keep dialogue in the model's native roles. Quoted JSON alone can
                # make a short follow-up look unrelated to the preceding request.
                memory_input = {**base, "recent_dialogue": [
                    {key: value for key, value in self._message(item).items() if key != "content"}
                    for item in recent]}
                raw = call([
                    {"role": "system", "content": (
                        "理解本轮用户目标及实际引用。综合当前请求、近期对话、任务状态和来源；"
                        "第二条消息的 JSON 是记忆资料，随后是保留原角色的近期对话，"
                        "历史消息开头标明来源 ID；最后一条 user 消息是本轮请求。"
                        "不能把资料里的旧消息或旧判定当成当前请求。"
                        "可读多个更早来源。未引用的旧任务不强制继承，换题也不靠固定词判断。"
                        "先解析对话依赖，再决定动作：省略对象或指代需要结合相关原文和任务状态"
                        "定位，不要求用户重复已说过的信息。goal 必须完整写出已定位的对象、"
                        "目标和本轮变化，sources 记录真正使用的历史依据；无法唯一定位才澄清。"
                        "relation=new 表示独立目标，不表示每条新消息。媒体类型也是任务意图，"
                        "不能仅因描述人物动作而把已有图片任务推断成新的视频任务；是否改变"
                        "媒体或切换目标均根据完整对话判断。"
                        "sources.fields 只列用户本轮要沿用的字段；模型摄影等是可变创作选择，"
                        "不可当作用户事实。questions/comparisons 用 chat，新创作/续创作用 create；"
                        "普通图片任务可选 image.generate/image.edit。只有关键歧义才 clarify，"
                        "question 简短；澄清回复应结合原请求继续处理。reads 提供有界历史读取，"
                        "read_required=true 的候选必须通过 reads 读取后再判定。"
                        "recent_dialogue/current_request 是已读取的原文，无需重复读取。"
                        "id 只返回指定的那一条；需要此前历史用 before_id 或 query。"
                        "先读来源再判定；检索用词不决定继承。tasks 分别保存各任务进度和纠正。"
                        "已核验的任务/摘要事实可以按原任务及来源复用；新增事实或选作创作锁的"
                        "来源需读取原文；user_request/asset_fact 的 value 必须是来源原文的"
                        "连续摘录，不改写。恢复撤回事实需要新的用户依据。"
                        "current_task_id 指向本轮任务；不同任务的事实不能混为同一任务约束。"
                        "创作任务更新或新建自己的 TaskProgress 并设置 current_task_id，"
                        "不要只保存更早的任务而遗漏本轮目标。"
                        "回答需要展示已完成产物时，读取并列出其来源，设置 response_artifact_id；"
                        "不能默认展示最新产物。"
                        "不输出思维链。只返回 JSON：" +
                        json.dumps(schema, ensure_ascii=False))},
                    {"role": "user", "content": json.dumps(memory_input, ensure_ascii=False)},
                    *[{"role": item.role.value if item.role.value != "system" else "user",
                       "content": f"[历史消息 id={item.id} role={item.role.value}]\n{item.content}"}
                      for item in recent],
                    {"role": "user", "content": request},
                ])
                understanding = self._parse(TurnUnderstanding, raw)
                if understanding.action not in self.allowed_actions:
                    raise ToolError("上下文动作未在当前入口注册，原请求保留")
                if not understanding.reads:
                    break
                if attempt == 2:
                    # Bounded reads are a safety valve, not a hard stop. Keep the
                    # context assembled so far and flag the truncation instead of
                    # failing the whole turn.
                    degraded.append("reads_truncated")
                    break
                for read in understanding.reads:
                    if read.kind == "messages":
                        readable_ids = ids | ({incoming.id} if incoming else set())
                        if any(anchor and anchor not in readable_ids for anchor in (
                            read.id, read.before_id, read.after_id
                        )):
                            raise ToolError("历史读取超出本轮会话消息范围，原请求保留")
                        rows = [item for item in self.store.list_conversation_messages(
                            conversation_id, limit=30, before_id=read.before_id,
                            after_id=read.after_id,
                            message_ids=[read.id] if read.id else None,
                        ) if item.id in readable_ids]
                        if read.query:
                            readable = [*history, *([incoming] if incoming else [])]
                            readable_order = [item.id for item in readable]
                            start = readable_order.index(read.after_id) + 1 if read.after_id else 0
                            end = (readable_order.index(read.before_id) if read.before_id
                                   else len(readable))
                            rows = [item for item in readable[start:end]
                                    if (not read.id or item.id == read.id) and read.query.casefold()
                                    in item.content.casefold()][:30]
                        fetched = [self._message(item) for item in rows]
                    else:
                        if reader is None or read.id is None:
                            raise ToolError("当前会话没有该历史读取能力")
                        fetched = [reader(read.kind, read.id)]
                    for item in fetched:
                        previous = recalled.get(item["id"])
                        if previous and not previous.get("read_required"):
                            continue
                        size = token_size(item)
                        previous_size = token_size(previous) if previous else 0
                        if recall_cost - previous_size + size > self.recall_budget:
                            raise ToolError("历史召回超过预算，请缩小读取范围；原记录保留")
                        recalled[item["id"]] = item
                        recall_cost += size - previous_size
                        evidence[item["id"]] = item
                base["recalled"] = recalled
            for source in understanding.sources:
                self._verify_source(source, evidence)
            verified_tasks = [*(summary.metadata["summary"]["tasks"] if summary else []),
                              *(state.metadata["resolution"]["tasks"] if state else [])]
            self._validate_facts(understanding.tasks, evidence, verified_tasks=verified_tasks)
            if understanding.response_artifact_id:
                source = next((ref for ref in understanding.sources
                               if ref.kind == "artifact"
                               and ref.id == understanding.response_artifact_id), None)
                if source is None or self._verify_source(source, evidence).get("status") != "ready":
                    raise ToolError("展示产物缺少本轮已核验的来源，原请求保留")
            task_ids = [task.task_id for task in understanding.tasks]
            if len(task_ids) != len(set(task_ids)) or (
                understanding.current_task_id and understanding.current_task_id not in task_ids
            ):
                raise ToolError("任务引用没有对应的独立状态，原请求保留")
            if understanding.action == "clarify" and not understanding.question:
                raise ToolError("上下文澄清缺少具体问题，原请求保留")
        messages = [{"role": "system", "content": (
            "以下是有来源的会话记忆。原始用户消息优先于摘要；撤回/纠正优先于旧记录。"
            "模型选择不等于用户约束。只使用本轮相关来源：\n" +
            json.dumps({**base, "resolution": understanding.model_dump(mode="json")
                        if understanding else None}, ensure_ascii=False))}]
        if degraded:
            messages.append({"role": "system", "content":
                "本轮记忆装配已降级（" + ", ".join(degraded) + "）。"
                "部分历史未能读取；如需要更早的上下文，请明确指定要回看的范围。"})
        messages.append({"role": "user", "content": request})
        total = token_size(messages)
        if total > self.budget:
            # Assembly is bounded by construction; if it still overflows, trim the
            # recalled evidence rather than failing the user's request.
            degraded.append("context_truncated")
            messages = [messages[0], messages[-1]]
            total = token_size(messages)
        return PreparedContext(understanding, messages, evidence,
                               summary.id if summary else None,
                               through_message_id or (history[-1].id if history else None),
                               total, degraded)

    @staticmethod
    def _verify_source(source: SourceRef, evidence: dict) -> dict:
        item = evidence.get(source.id)
        if item is None or item["kind"] != source.kind or (
            source.version is not None and source.version != item.get("version")
        ):
            raise ToolError("上下文引用来源或版本未通过核验")
        if item.get("read_required"):
            raise ToolError("上下文候选尚未读取，不能作为继承依据")
        return item

    def _validate_facts(self, tasks: list[TaskProgress], evidence: dict, *,
                        verified_tasks: list[dict] | None = None) -> None:
        verified = {(task["task_id"], fact["value"], fact["origin"],
                     json.dumps(fact["source"], sort_keys=True)): fact["status"]
                    for task in verified_tasks or [] for fact in task["facts"]}
        for task in tasks:
            for fact in task.facts:
                known = verified.get((task.task_id, fact.value, fact.origin,
                    json.dumps(fact.source.model_dump(mode="json"), sort_keys=True)))
                if known:
                    if known in {"withdrawn", "superseded"} and fact.status == "active":
                        raise ToolError("撤回事实不能沿用旧来源恢复，请核对新的用户依据")
                    continue
                item = self._verify_source(fact.source, evidence)
                if fact.origin == "user_request" and (
                    item.get("role") != "user" or fact.value not in item.get("content", "")
                ):
                    raise ToolError("模型描述不能升级为用户事实")
                if fact.origin == "asset_fact" and (
                    not item.get("asset_fact") or fact.value not in json.dumps(
                        item.get("content"), ensure_ascii=False)
                ):
                    raise ToolError("资产事实缺少经核验的来源内容")

    def remember(self, conversation_id: str, prepared: PreparedContext, message_id: str) -> None:
        if prepared.understanding:
            self.store.create_artifact(
                type=ArtifactType.JSON, conversation_id=conversation_id,
                node_id="conversation.context", source="context_resolution",
                metadata={"internal": True, "through_message_id": message_id,
                          **prepared.public()},
            )
