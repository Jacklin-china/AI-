"""Exercise actual scoped memory, payload budgets and durable compaction offline."""

from __future__ import annotations

import json

import pytest

from kantoku.config import ToolError
from kantoku.core.conversation_context import ConversationContext, token_size
from kantoku.core.conversations import MessageRole, MessageType
from kantoku.core.runtime.models import ArtifactType
from kantoku.core.runtime.store import RuntimeStore


@pytest.fixture
def store(tmp_path):
    return RuntimeStore(tmp_path / "context.sqlite")


def message(store, conversation, content, role=MessageRole.USER):
    return store.add_conversation_message(conversation.id, role=role, type=MessageType.TEXT,
                                          content=content)


def decision(**changes):
    return {"action": "chat", "relation": "recall", "goal": "回答当前问题",
            "sources": [], "tasks": [], "reads": [], **changes}


def test_paging_and_ranges_cannot_escape_conversation(store):
    first, second = store.create_conversation(), store.create_conversation()
    rows = [message(store, first, str(i)) for i in range(8)]
    foreign = message(store, second, "foreign")
    page = store.list_conversation_messages(first.id, limit=3)
    assert [item.id for item in page] == [item.id for item in rows[-3:]]
    earlier = store.list_conversation_messages(first.id, before_id=page[0].id, limit=3)
    assert [item.id for item in earlier] == [item.id for item in rows[2:5]]
    assert [item.id for item in store.list_conversation_messages(
        first.id, after_id=rows[1].id, before_id=rows[5].id)] == [i.id for i in rows[2:5]]
    with pytest.raises(ToolError, match="当前会话"):
        store.list_conversation_messages(first.id, before_id=foreign.id, limit=2)


def test_query_recall_respects_the_requested_message_range(store):
    conversation = store.create_conversation()
    rows = [message(store, conversation, f"共同关键词：方案{i}") for i in range(8)]
    captured = []
    def model(messages):
        payload = json.loads(messages[1]["content"])
        captured.append(payload)
        return json.dumps(decision(reads=[{"kind": "messages", "query": "共同关键词",
            "after_id": rows[1].id, "before_id": rows[5].id}])) if not payload["recalled"] else (
                json.dumps(decision()))
    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    prepared = memory.prepare(conversation.id, "比较范围内的方案", model_call=model)
    # These bodies are already in native-role dialogue. Querying the range must
    # reuse them, without another body copy or an extra understanding call.
    assert len(captured) == 1
    assert prepared.public()["budget"]["exploratory_tokens"] == 0
    assert {item["id"] for item in prepared.public()["budget"]["sources"]} == {
        row.id for row in rows[2:5]}


def test_current_anchor_can_read_prior_history_with_native_dialogue_roles(store):
    conversation = store.create_conversation()
    original = message(store, conversation, "初始目标：制作一张角色图片")
    reply = message(store, conversation, "已保存图片创作目标", MessageRole.ASSISTANT)
    incoming = message(store, conversation, "核对此前目标")
    message(store, conversation, "后来的其他任务不能串入当前请求")
    captured = []
    def model(messages):
        payload = json.loads(messages[1]["content"])
        captured.append(payload)
        assert messages[-1] == {"role": "user", "content": incoming.content}
        assert any(item["role"] == "assistant" and reply.content in item["content"]
                   for item in messages)
        assert "后来的其他任务" not in json.dumps(messages, ensure_ascii=False)
        return json.dumps(decision(reads=[{"kind": "messages", "before_id": incoming.id,
            "query": "初始目标"}])) if not payload["recalled"] else json.dumps(decision())
    memory = ConversationContext(store, budget=18000, summary_budget=2000, recall_budget=6000)
    prepared = memory.prepare(conversation.id, incoming.content, model_call=model,
                              through_message_id=incoming.id)
    assert len(captured) == 1
    assert {item["id"] for item in prepared.public()["budget"]["sources"]} == {original.id}
    assert prepared.evidence[original.id]["content"] == original.content


def test_120_turns_compact_recall_correction_and_reopen_without_leak(store):
    conversation = store.create_conversation()
    other = store.create_conversation()
    message(store, other, "另一个会话的机密主体")
    first = message(store, conversation, "角色衣服原定红色")
    corrected = message(store, conversation, "纠正：角色衣服改为蓝色，旧红色要求撤回")
    sources = [{"id": row.id, "kind": "message"} for row in (first, corrected)]
    facts = [{"value": "角色衣服原定红色", "source": sources[0],
              "origin": "user_request", "status": "withdrawn"},
             {"value": "角色衣服改为蓝色", "source": sources[1],
              "origin": "user_request", "status": "active"}]
    task = {"task_id": "early-task", "goal": "早期角色", "status": "pending",
            "facts": facts, "open_items": ["待生成"]}
    seen, summary_calls, read_calls = [], [], []

    def model(messages):
        payload = json.loads(messages[1]["content"])
        seen.append(payload)
        assert token_size(messages) <= memory.budget
        if "current_request" in payload:
            assert messages[-1] == {"role": "user", "content": payload["current_request"]}
        assert "另一个会话的机密主体" not in json.dumps(payload, ensure_ascii=False)
        if "previous_summary" in payload:
            summary_calls.append(payload)
            return json.dumps({"overview": "主题多次切换；早期衣服已改蓝色，红色撤回。",
                               "tasks": [task], "sources": sources}, ensure_ascii=False)
        loaded = {item["id"] for item in payload["recent_dialogue"]} | set(payload["recalled"])
        needed = [ref for ref in sources if ref["id"] not in loaded]
        if needed:
            read_calls.extend(needed)
            return json.dumps(decision(reads=[{"kind": "messages", "id": ref["id"]}
                                              for ref in needed]), ensure_ascii=False)
        return json.dumps(decision(tasks=[task], sources=sources), ensure_ascii=False)

    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    for i in range(120):
        request = f"第{i}轮，任务{i % 5}：" + "讨论并比较本主题的新目标和现有进度。" * 8
        incoming = message(store, conversation, request)
        prepared = memory.prepare(conversation.id, request, model_call=model,
                                  through_message_id=incoming.id)
        memory.remember(conversation.id, prepared, incoming.id)
        assert prepared.token_count <= memory.budget
        assert prepared.understanding.tasks[0].facts == task_facts(facts)
        message(store, conversation, "本轮完成，等待下一轮问题。" * 7, MessageRole.ASSISTANT)
    assert summary_calls and read_calls
    assert len(store.list_conversation_messages(conversation.id)) == 242
    assert store.list_artifacts(conversation_id=conversation.id) == []
    internal = store.list_artifacts(conversation_id=conversation.id, include_internal=True)
    assert all(item.type == ArtifactType.JSON and item.location is None for item in internal)
    assert any(item.version > 1 for item in internal if item.node_id == "conversation.summary")
    reopened = ConversationContext(RuntimeStore(store.path), budget=4000,
                                   summary_budget=700, recall_budget=900)
    prepared = reopened.prepare(conversation.id, "回到早期角色：现在衣服是什么颜色？",
                                model_call=model)
    assert prepared.understanding.tasks[0].facts[1].value == "角色衣服改为蓝色"
    assert seen[-1]["summary"]["tasks"][0]["facts"][0]["status"] == "withdrawn"
    isolated = reopened.prepare(other.id, "继续", model_call=lambda _: json.dumps(decision()))
    assert "角色衣服改为蓝色" not in json.dumps(isolated.messages, ensure_ascii=False)


def task_facts(facts):
    from kantoku.core.conversation_context import MemoryFact
    return [MemoryFact.model_validate(fact) for fact in facts]


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_summary_overflow_has_bounded_recompression_without_losing_raw_records(
    store, repair_succeeds,
):
    conversation = store.create_conversation()
    rows = [message(store, conversation, "待完成事项和本轮进度。" * 30) for _ in range(8)]
    calls = []
    memory = ConversationContext(store, budget=4000, summary_budget=500, recall_budget=900)
    def model(messages):
        assert token_size(messages) <= memory.budget
        payload = json.loads(messages[1]["content"])
        if "previous_summary" not in payload:
            return json.dumps(decision())
        calls.append(messages)
        overflow = len(calls) == 1 or not repair_succeeds
        return json.dumps({"overview": "冗长摘要" * 200 if overflow else "待完成事项保留。",
                           "tasks": [], "sources": []}, ensure_ascii=False)
    if repair_succeeds:
        prepared = memory.prepare(conversation.id, "继续", model_call=model)
        assert prepared.summary_id is not None
        assert len(calls) >= 2
    else:
        # Overflow degrades instead of failing the turn: no summary is written,
        # the truncation is reported, and the raw records stay intact.
        prepared = memory.prepare(conversation.id, "继续", model_call=model)
        assert prepared.summary_id is None
        assert "summary_truncated" in prepared.degraded
        assert len(calls) == 2
        assert store.list_artifacts(conversation_id=conversation.id, include_internal=True) == []
    assert [row.content for row in store.list_conversation_messages(conversation.id)] == [
        row.content for row in rows]


def test_oversized_input_stops_before_call_and_retains_raw_history(store):
    conversation = store.create_conversation()
    incoming = message(store, conversation, "完整保留" * 1000)
    called = []
    memory = ConversationContext(store, budget=2000, summary_budget=300, recall_budget=400)
    with pytest.raises(ToolError, match="预算"):
        memory.prepare(conversation.id, incoming.content, through_message_id=incoming.id,
                       model_call=lambda messages: called.append(messages))
    assert called == []
    assert store.list_conversation_messages(conversation.id)[0].content == incoming.content


def test_unregistered_action_and_future_history_are_rejected(store):
    conversation = store.create_conversation()
    incoming = message(store, conversation, "当前请求")
    future = message(store, conversation, "未来用户消息")
    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    for resolution in (decision(action="not_registered"),
                       decision(reads=[{"kind": "messages", "before_id": future.id}])):
        with pytest.raises(ToolError):
            memory.prepare(conversation.id, incoming.content, through_message_id=incoming.id,
                           model_call=lambda _, result=resolution: json.dumps(result))


@pytest.mark.parametrize("origin,content", [
    ("user_request", "模型生成的绿色衣服"), ("asset_fact", "模型生成的绿色衣服"),
])
def test_model_choice_cannot_be_promoted_to_fact(store, origin, content):
    conversation = store.create_conversation()
    row = message(store, conversation, content, MessageRole.ASSISTANT)
    task = {"task_id": "x", "goal": "x", "status": "active", "facts": [
        {"value": content, "source": {"id": row.id, "kind": "message"}, "origin": origin}]}
    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    with pytest.raises(ToolError):
        memory.prepare(conversation.id, "继续", model_call=lambda _: json.dumps(
            decision(tasks=[task])))
    assert len(store.list_conversation_messages(conversation.id)) == 1


@pytest.mark.parametrize("mismatch", ["model_choice", "user_quote", "asset_fact"])
def test_fact_source_correction_preserves_goal_and_verified_facts(store, mismatch):
    conversation = store.create_conversation()
    user = message(store, conversation, "保持原来的姿势，让镜头把他的脸看清")
    generated = message(store, conversation, "模型选择：脸上有风霜纹理", MessageRole.ASSISTANT)
    user_source = {"id": user.id, "kind": "message"}
    model_source = {"id": generated.id, "kind": "message"}
    bad = {"value": "脸上有风霜纹理", "source": model_source,
           "origin": "asset_fact" if mismatch == "asset_fact" else "user_request"}
    fixed = {**bad, "origin": "model_choice"}
    if mismatch == "user_quote":
        bad = {"value": "面部清晰可见", "source": user_source, "origin": "user_request"}
        fixed = {**bad, "value": "让镜头把他的脸看清"}
    stable = {"value": "保持原来的姿势", "source": user_source, "origin": "user_request"}
    task = {"task_id": "portrait", "goal": user.content, "status": "active",
            "facts": [stable, bad]}
    calls = []

    def model(messages):
        calls.append(messages)
        if '"FactCorrections"' in messages[0]["content"]:
            payload = json.loads(messages[1]["content"])
            assert payload["issues"][0]["fact_index"] == 1
            assert payload["evidence"][user.id]["role"] == "user"
            return json.dumps({"corrections": [{"task_index": 0, "fact_index": 1,
                                                 "fact": fixed}]})
        return json.dumps(decision(action="create", relation="continue", goal=user.content,
            current_task_id="portrait", tasks=[task]))

    memory = ConversationContext(store, budget=18000, summary_budget=2000, recall_budget=6000)
    prepared = memory.prepare(conversation.id, user.content, model_call=model)
    assert len(calls) == 2
    assert prepared.understanding.goal == user.content
    assert prepared.understanding.action == "create"
    assert prepared.understanding.tasks[0].facts[0].model_dump(exclude={"status"},
        exclude_none=True)["value"] == stable["value"]
    assert prepared.understanding.tasks[0].facts[1].value == fixed["value"]
    assert prepared.understanding.tasks[0].facts[1].origin == fixed["origin"]
    assert "fact_sources_corrected" in prepared.degraded
    memory.remember(conversation.id, prepared, user.id)
    assert store.list_artifacts(conversation_id=conversation.id) == []


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_summary_fact_sources_use_same_bounded_correction(store, repair_succeeds):
    conversation = store.create_conversation()
    original = message(store, conversation, "保留釉面裂纹与器皿比例")
    rows = [original, *(message(store, conversation, "讨论其他目标和当前进度。" * 30)
                        for _ in range(60))]
    source = {"kind": "message", "id": original.id}
    fact = {"value": "质感保持不变", "source": source, "origin": "user_request"}
    task = {"task_id": "vessel", "goal": "核对器皿", "status": "active", "facts": [fact]}
    summaries, repairs = [], []

    def model(messages):
        assert token_size(messages) <= memory.budget
        payload = json.loads(messages[1]["content"])
        if '"FactCorrections"' in messages[0]["content"]:
            repairs.append(payload)
            fixed = {**fact, "value": "保留釉面裂纹"} if repair_succeeds else fact
            return json.dumps({"corrections": [{"task_index": 0, "fact_index": 0,
                                                 "fact": fixed}]})
        if "previous_summary" in payload:
            summaries.append(payload)
            return json.dumps({"overview": "原始器皿约束继续保留", "tasks": [task],
                               "sources": [source]})
        return json.dumps(decision())

    memory = ConversationContext(store, budget=6000, summary_budget=900, recall_budget=1200)
    if repair_succeeds:
        prepared = memory.prepare(conversation.id, "继续核对", model_call=model)
        saved = store.get_artifact(prepared.summary_id)
        assert saved.metadata["summary"]["tasks"][0]["facts"][0]["value"] == "保留釉面裂纹"
        assert "fact_sources_corrected" in prepared.degraded
        assert len(repairs) == len(summaries) > 1
    else:
        with pytest.raises(ToolError, match="模型描述不能升级为用户事实"):
            memory.prepare(conversation.id, "继续核对", model_call=model)
        assert len(repairs) == len(summaries) == 1
        assert store.list_artifacts(conversation_id=conversation.id, include_internal=True) == []
    assert [row.id for row in store.list_conversation_messages(conversation.id)] == [
        row.id for row in rows]


@pytest.mark.parametrize("invalid", [
    "unrelated", "duplicate", "missing", "status", "invented", "foreign", "timeout"])
def test_fact_source_correction_is_bounded_and_cannot_bypass_validation(store, invalid):
    conversation = store.create_conversation()
    user = message(store, conversation, "保持姿势")
    generated = message(store, conversation, "模型选择的服装", MessageRole.ASSISTANT)
    foreign = message(store, store.create_conversation(), "其他对话的用户原话")
    task = {"task_id": "x", "goal": "继续", "status": "active", "facts": [
        {"value": user.content, "source": {"kind": "message", "id": user.id},
         "origin": "user_request"},
        {"value": generated.content, "source": {"kind": "message", "id": generated.id},
         "origin": "user_request"}]}
    corrections = []

    def model(messages):
        if '"FactCorrections"' not in messages[0]["content"]:
            return json.dumps(decision(tasks=[task]))
        corrections.append(True)
        if invalid == "timeout":
            raise TimeoutError("offline correction transport unknown")
        patch = {"task_index": 0, "fact_index": 0 if invalid == "unrelated" else 1,
                 "fact": {**task["facts"][1], "origin": "model_choice"}}
        if invalid == "status":
            patch["fact"]["status"] = "withdrawn"
        if invalid in {"invented", "foreign"}:
            patch["fact"].update(origin="user_request", value=foreign.content,
                source={"kind": "message", "id": foreign.id if invalid == "foreign" else user.id})
        return json.dumps({"corrections": [] if invalid == "missing" else
                          [patch, patch] if invalid == "duplicate" else [patch]})

    memory = ConversationContext(store, budget=18000, summary_budget=2000, recall_budget=6000)
    with pytest.raises((ToolError, TimeoutError)):
        memory.prepare(conversation.id, "继续", model_call=model)
    assert corrections == [True]
    assert store.list_artifacts(conversation_id=conversation.id, include_internal=True) == []


def test_ambiguous_reply_retains_original_request_and_navigation_anchor(store):
    conversation = store.create_conversation()
    original = message(store, conversation, "把其中一个方案的光线保留下来")
    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    prepared = memory.prepare(conversation.id, original.content, through_message_id=original.id,
                              model_call=lambda _: json.dumps(decision(
                                  action="clarify", question="你指的是哪个方案？")))
    memory.remember(conversation.id, prepared, original.id)
    message(store, conversation, "你指的是哪个方案？", MessageRole.ASSISTANT)
    reply = message(store, conversation, "第一个方案")
    captured = []

    def model(messages):
        captured.append({**json.loads(messages[1]["content"]), "actual_messages": messages})
        return json.dumps(decision(action="create", relation="continue"))

    prepared = memory.prepare(conversation.id, reply.content, through_message_id=reply.id,
                              model_call=model)
    assert captured[0]["task_state"]["action"] == "clarify"
    assert original.content in json.dumps(captured[0], ensure_ascii=False)
    message(store, conversation, "后续新任务，异步回复不能串入它")
    old = memory.prepare(conversation.id, reply.content, through_message_id=reply.id,
                         model_call=model)
    assert "异步回复不能串入" not in json.dumps(old.messages, ensure_ascii=False)


def test_asset_source_does_not_certify_an_invented_fact(store):
    conversation = store.create_conversation()
    source = {"id": "uploaded-reference", "kind": "artifact", "version": 2}
    task = {"task_id": "x", "goal": "x", "status": "active", "facts": [{
        "value": "红色衣服", "source": source, "origin": "asset_fact"}]}
    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    with pytest.raises(ToolError, match="来源内容"):
        memory.prepare(conversation.id, "沿用资产", model_call=lambda _: json.dumps(
            decision(tasks=[task], sources=[source])), candidates=[{
                **source, "asset_fact": True, "content": {"clothing": "蓝色衣服"}}])
    task["facts"][0]["value"] = "蓝色衣服"
    prepared = memory.prepare(conversation.id, "沿用资产", model_call=lambda _: json.dumps(
        decision(tasks=[task], sources=[source])), candidates=[{
            **source, "asset_fact": True, "content": {"clothing": "蓝色衣服"}}])
    assert prepared.understanding.tasks[0].facts[0].value == "蓝色衣服"


def test_many_verified_task_facts_survive_without_rereading_every_source(store):
    conversation = store.create_conversation()
    tasks = []
    for index in range(10):
        facts = []
        for part in range(2):
            value = f"任务{index}的约束{part}"
            row = message(store, conversation, value)
            facts.append({"value": value, "origin": "user_request", "status": "active",
                "source": {"id": row.id, "kind": "message", "version": None, "fields": []}})
        tasks.append({"task_id": str(index), "goal": f"任务{index}", "status": "pending",
                      "facts": facts, "open_items": []})
    memory = ConversationContext(store, budget=18000, summary_budget=2000, recall_budget=6000)
    prepared = memory.prepare(conversation.id, "保留各任务进度", model_call=lambda _: json.dumps(
        decision(tasks=tasks)))
    memory.remember(conversation.id, prepared, prepared.through_message_id)
    original_ids = {fact["source"]["id"] for task in tasks for fact in task["facts"]}
    for index in range(120):
        message(store, conversation, f"其他主题{index}：" + "不修改原有任务的独立讨论。" * 7)
    def model(messages):
        payload = json.loads(messages[1]["content"])
        if "previous_summary" in payload:
            return json.dumps({"overview": "多个任务的原有约束继续有效。", "tasks": [],
                               "sources": []})
        assert not original_ids & {item["id"] for item in payload["recent_dialogue"]}
        return json.dumps(decision(tasks=tasks))
    prepared = memory.prepare(conversation.id, "继续记录所有任务进度", model_call=model)
    assert len(prepared.understanding.tasks) == 10
    changed = json.loads(json.dumps(tasks))
    changed[0]["facts"][0]["value"] = "并未提供过的新增约束"
    with pytest.raises(ToolError):
        memory.prepare(conversation.id, "继续", model_call=lambda _: json.dumps(
            decision(tasks=changed)))


def test_withdrawn_fact_requires_new_user_evidence_to_be_restored(store):
    conversation = store.create_conversation()
    row = message(store, conversation, "红色衣服")
    task = {"task_id": "x", "goal": "x", "status": "active", "facts": [{
        "value": row.content, "origin": "user_request", "status": "withdrawn",
        "source": {"id": row.id, "kind": "message"}}]}
    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    prepared = memory.prepare(conversation.id, "记录撤回", model_call=lambda _: json.dumps(
        decision(tasks=[task])))
    memory.remember(conversation.id, prepared, row.id)
    task["facts"][0]["status"] = "active"
    with pytest.raises(ToolError, match="撤回事实"):
        memory.prepare(conversation.id, "继续", model_call=lambda _: json.dumps(
            decision(tasks=[task])))


def test_invalid_model_output_does_not_leak_private_input_in_error(store):
    conversation = store.create_conversation()
    incoming = message(store, conversation, "原始请求保留")
    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    invalid = {**decision(), "private_thought": "不应进入错误日志的模型私有内容"}
    with pytest.raises(ToolError) as failure:
        memory.prepare(conversation.id, incoming.content, through_message_id=incoming.id,
                       model_call=lambda _: json.dumps(invalid))
    assert invalid["private_thought"] not in str(failure.value)
    assert "extra_forbidden" in str(failure.value)
    assert store.list_conversation_messages(conversation.id)[0].content == incoming.content


@pytest.mark.parametrize("kind", ["run", "artifact", "resource"])
def test_selected_pointer_is_automatically_read_and_reinterpreted(store, kind):
    conversation = store.create_conversation()
    source = {"kind": kind, "id": "selected-record", "version": 2}
    calls, fetched = [], []
    def model(messages):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        if len(calls) == 1:
            assert payload["recalled"] == {}
            assert payload["available"][0]["id"] == source["id"]
            assert "未读取的旧设定" not in json.dumps(messages, ensure_ascii=False)
            return json.dumps(decision(goal="尚未核对的猜测", sources=[source]))
        assert payload["recalled"][source["id"]]["content"] == "核实后的设定"
        return json.dumps(decision(goal="核实后的目标", sources=[source]))
    def reader(read_kind, identifier):
        fetched.append((read_kind, identifier))
        return {**source, "content": "核实后的设定"}
    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    prepared = memory.prepare(conversation.id, "继续", model_call=model, reader=reader,
        candidates=[{**source, "read_required": True, "content": "未读取的旧设定"}])
    assert fetched == [(kind, source["id"])]
    assert len(calls) == 2
    assert prepared.understanding.goal == "核实后的目标"
    assert "尚未核对的猜测" not in json.dumps(prepared.messages, ensure_ascii=False)


@pytest.mark.parametrize("chain_length", [3, 4])
def test_three_read_rounds_have_a_final_resolution_without_unread_inheritance(store, chain_length):
    conversation = store.create_conversation()
    calls, fetched = [], []
    def model(messages):
        calls.append(messages)
        identifier = f"record-{min(len(calls), chain_length)}"
        return json.dumps(decision(action="create", relation="continue", goal="继续原任务",
            sources=[{"kind": "run", "id": identifier, "version": 1}],
            reads=[{"kind": "run", "id": identifier}]))
    def reader(kind, identifier):
        fetched.append(identifier)
        return {"kind": kind, "id": identifier, "version": 1, "content": "已核对"}
    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    prepared = memory.prepare(conversation.id, "继续", model_call=model, reader=reader)
    assert len(calls) == 4
    assert fetched == ["record-1", "record-2", "record-3"]
    assert prepared.understanding.reads == []
    if chain_length == 3:
        assert prepared.understanding.action == "create"
        assert prepared.understanding.sources[0].id == "record-3"
    else:
        assert prepared.understanding.action == "clarify"
        assert prepared.understanding.sources == []
        assert "reads_truncated" in prepared.degraded


def test_new_fact_and_display_artifact_dependencies_are_read_without_explicit_reads(store):
    conversation = store.create_conversation()
    source = {"kind": "resource", "id": "reference", "version": 1}
    task = {"task_id": "current", "goal": "查看参考", "status": "active", "facts": [{
        "value": "蓝色衣服", "origin": "asset_fact", "source": source}]}
    calls, fetched = [], []
    def model(messages):
        calls.append(messages)
        return json.dumps(decision(tasks=[task], response_artifact_id="image"))
    def reader(kind, identifier):
        fetched.append(identifier)
        return {"kind": kind, "id": identifier, "version": 1, "status": "ready",
                "asset_fact": identifier == "reference", "content": {"outfit": "蓝色衣服"}}
    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    prepared = memory.prepare(conversation.id, "查看参考", model_call=model, reader=reader)
    assert len(calls) == 2
    assert fetched == ["reference", "image"]
    assert prepared.understanding.response_artifact_id == "image"
    assert prepared.understanding.sources[0].id == "image"


@pytest.mark.parametrize("problem", ["version", "identity", "failure", "budget"])
def test_automatic_reading_preserves_validation_and_failure_boundaries(store, problem):
    conversation = store.create_conversation()
    incoming = message(store, conversation, "原请求保留")
    source = {"kind": "run", "id": "record", "version": 2}
    def reader(kind, identifier):
        if problem == "failure":
            raise ToolError("读取服务故障")
        return {"kind": kind, "id": "wrong-id" if problem == "identity" else identifier,
                "version": 1 if problem == "version" else 2,
                "content": "过长正文" * 500 if problem == "budget" else "已读取"}
    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    with pytest.raises(ToolError):
        memory.prepare(conversation.id, incoming.content, through_message_id=incoming.id,
            model_call=lambda _: json.dumps(decision(sources=[source])), reader=reader)
    assert store.list_conversation_messages(conversation.id)[0].content == incoming.content


def test_final_context_drops_unselected_bodies_but_keeps_selected_evidence(store):
    conversation = store.create_conversation()
    source = {"kind": "run", "id": "chosen", "version": 1}
    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    prepared = memory.prepare(conversation.id, "只用所选方案",
        model_call=lambda _: json.dumps(decision(sources=[source])), candidates=[
            {**source, "content": "选中设定"},
            {"kind": "run", "id": "other", "version": 1, "content": "无关旧设定"}])
    final = json.dumps(prepared.messages, ensure_ascii=False)
    assert "选中设定" in final
    assert "无关旧设定" not in final


def test_mandatory_evidence_cannot_be_removed_to_hide_final_context_overflow(store):
    conversation = store.create_conversation()
    source = {"kind": "resource", "id": "reference", "version": 1}
    value = "硬约束" * 40
    task = {"task_id": "current", "goal": "保留约束", "status": "active", "facts": [
        {"value": value, "origin": "asset_fact", "source": source} for _ in range(30)]}
    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    with pytest.raises(ToolError, match="必需来源"):
        memory.prepare(conversation.id, "保留约束", model_call=lambda _: json.dumps(
            decision(tasks=[task])), candidates=[{**source, "asset_fact": True, "content": value}])


@pytest.mark.parametrize("explicit", [False, True])
def test_truncated_resource_directory_requires_a_choice_except_for_explicit_selection(
    store, explicit,
):
    conversation = store.create_conversation()
    source = {"kind": "resource", "id": "chosen", "version": 1}
    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    candidates = [{**source, "read_required": True, "required": explicit}, *[
        {"kind": "resource", "id": f"other-{index}", "version": 1,
         "read_required": True, "description": "另一个相似资产的说明" * 10} for index in range(8)]]
    prepared = memory.prepare(conversation.id, "沿用资产",
        model_call=lambda _: json.dumps(decision(action="create", sources=[source])),
        candidates=candidates, reader=lambda _kind, _identifier: {**source, "content": "已核对"})
    assert "resources_truncated" in prepared.degraded
    assert prepared.understanding.action == ("create" if explicit else "clarify")


def test_required_sources_borrow_unused_capacity_and_reinterpret(store):
    conversation = store.create_conversation()
    sources = [{"kind": kind, "id": f"evidence-{index}", "version": 2}
               for index, kind in enumerate(("run", "artifact"))]
    bodies = {source["id"]: {**source, "content": "证" * length}
              for source, length in zip(sources, (4000, 5000), strict=True)}
    captured, fetched = [], []
    memory = ConversationContext(store, budget=18000, summary_budget=2000, recall_budget=6000)
    def model(messages):
        assert token_size(messages) <= memory.budget
        payload = json.loads(messages[1]["content"])
        captured.append(payload)
        if len(captured) == 1:
            return json.dumps(decision(goal="读取前猜测", sources=sources,
                reads=[{"kind": "run", "id": sources[0]["id"]}] * 2))
        assert payload["recalled"] == bodies
        return json.dumps(decision(goal="读取后核实的目标", sources=sources,
            reads=[{"kind": "run", "id": sources[0]["id"]}]))
    def reader(_kind, identifier):
        fetched.append(identifier)
        return bodies[identifier]
    prepared = memory.prepare(conversation.id, "组合选中的依据", model_call=model, reader=reader)
    assert sum(token_size(body) for body in bodies.values()) > memory.recall_budget
    assert len(captured) == 2 and fetched == [source["id"] for source in sources]
    assert prepared.understanding.goal == "读取后核实的目标"
    assert prepared.public()["budget"]["required_tokens"] > memory.recall_budget
    assert prepared.public()["budget"]["exploratory_tokens"] == 0
    assert "读取前猜测" not in json.dumps(prepared.messages, ensure_ascii=False)
    assert set(prepared.evidence) == set(bodies)


@pytest.mark.parametrize("optional_length", [1500, 2900])
def test_optional_reads_cannot_displace_required_evidence(store, optional_length):
    conversation = store.create_conversation()
    source = {"kind": "resource", "id": "selected", "version": 1}
    bodies = {"selected": {**source, "content": "证" * 4500},
              "unused": {"kind": "run", "id": "unused", "version": 1,
                         "content": "余" * optional_length}}
    captured, fetched = [], []
    memory = ConversationContext(store, budget=8000, summary_budget=1000, recall_budget=2000)
    def model(messages):
        assert token_size(messages) <= memory.budget
        payload = json.loads(messages[1]["content"])
        captured.append(payload)
        if len(captured) == 1:
            return json.dumps(decision(sources=[source], reads=[
                {"kind": "run", "id": "unused"}, {"kind": "resource", "id": "selected"}]))
        assert payload["recalled"] == {"selected": bodies["selected"]}
        return json.dumps(decision(sources=[source]))
    def reader(_kind, identifier):
        fetched.append(identifier)
        return bodies[identifier]
    prepared = memory.prepare(conversation.id, "只使用选中依据", model_call=model, reader=reader)
    assert fetched == ["selected", "unused"] and len(captured) == 2
    assert "recall_truncated" in prepared.degraded
    assert prepared.evidence["selected"] == bodies["selected"]
    assert "余" not in json.dumps(prepared.messages, ensure_ascii=False)
    assert prepared.public()["budget"]["exploratory_tokens"] == 0


def test_recent_source_is_reused_without_reading_or_duplicate_body(store):
    conversation = store.create_conversation()
    original = message(store, conversation, "保留这条完整的用户依据")
    incoming = message(store, conversation, "继续处理")
    calls = []
    source = {"kind": "message", "id": original.id}
    def model(messages):
        calls.append(messages)
        return json.dumps(decision(sources=[source], reads=[
            {"kind": "messages", "id": original.id},
            {"kind": "messages", "id": original.id}]))
    def forbidden_reader(*_args):
        raise AssertionError("Already read source must be reused")
    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    prepared = memory.prepare(conversation.id, incoming.content, through_message_id=incoming.id,
        model_call=model, reader=forbidden_reader)
    assert len(calls) == 1
    assert original.content in calls[0][2]["content"]
    assert prepared.evidence[original.id]["content"] == original.content
    assert json.dumps(prepared.messages, ensure_ascii=False).count(original.content) == 1
    assert prepared.public()["budget"]["exploratory_tokens"] == 0


def test_evicted_source_reselection_reuses_body_and_requires_reinterpretation(store):
    conversation = store.create_conversation()
    bodies = {key: {"kind": "run", "id": key, "version": 1, "content": "证" * length}
              for key, length in (("first", 1500), ("second", 2000))}
    calls, fetched = [], []
    def model(messages):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        identifier = "second" if len(calls) == 2 else "first"
        if len(calls) == 3:
            assert "first" not in payload["recalled"]
        if len(calls) == 4:
            assert payload["recalled"]["first"] == bodies["first"]
        return json.dumps(decision(goal="核实完成" if len(calls) == 4 else "尚待核实",
            sources=[{"kind": "run", "id": identifier, "version": 1}]))
    def reader(_kind, identifier):
        fetched.append(identifier)
        return bodies[identifier]
    memory = ConversationContext(store, budget=6500, summary_budget=700, recall_budget=1000)
    prepared = memory.prepare(conversation.id, "重新选取依据", model_call=model, reader=reader)
    assert fetched == ["first", "second"] and len(calls) == 4
    assert prepared.understanding.goal == "核实完成"


def test_true_capacity_overflow_preserves_request_and_safe_trace_diagnostics(store):
    from loguru import logger

    from kantoku.config.observability import request_trace

    conversation = store.create_conversation()
    incoming = message(store, conversation, "当前原始请求必须保留")
    source = {"kind": "run", "id": "large-source", "version": 3}
    private_body = "不能出现在日志的来源正文" * 1000
    calls, records = [], []
    sink = logger.add(lambda message: records.append(message.record))
    memory = ConversationContext(store, budget=4000, summary_budget=700, recall_budget=900)
    def model(messages):
        calls.append(messages)
        return json.dumps(decision(sources=[source]))
    try:
        with request_trace("trace-capacity-regression"), pytest.raises(ToolError) as raised:
            memory.prepare(conversation.id, incoming.content, through_message_id=incoming.id,
                model_call=model, reader=lambda _kind, _id: {**source, "content": private_body})
    finally:
        logger.remove(sink)
    diagnostic = json.loads(raised.value.detail)
    assert len(calls) == 1
    assert diagnostic["stage"] == "understanding:2"
    assert diagnostic["estimated_tokens"] > memory.budget
    assert diagnostic["sources"][0]["id"] == source["id"]
    assert diagnostic["sources"][0]["version"] == source["version"]
    assert diagnostic["sources"][0]["required"] is True
    assert store.list_conversation_messages(conversation.id)[0].content == incoming.content
    assert records[-1]["extra"]["trace_id"] == "trace-capacity-regression"
    assert "不能出现在日志" not in str(raised.value)
    assert "不能出现在日志" not in str(records)
