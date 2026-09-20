"""无领域知识的状态图运行时。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import ValidationError

from kantoku.config import ToolError
from kantoku.core.state import RunState

from .models import (
    ApprovalDecision,
    ExecutionStatus,
    NodeExecutionRecord,
    RunRecord,
    utc_now,
)
from .store import RuntimeStore

START = "__start__"
END = "__end__"
StateT = TypeVar("StateT", bound=RunState)
NodeHandler = Callable[[StateT, "RuntimeContext"], Mapping[str, Any] | None]
RouteHandler = Callable[[StateT], str]


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Node 可使用的基础设施；领域服务由 workflow 注册时注入。"""

    run_id: str
    node_id: str
    store: RuntimeStore
    services: Mapping[str, Any]
    approval_decision: ApprovalDecision | None = None
    approval_response: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowNode(Generic[StateT]):
    """一个只返回局部 State Update 的执行单元。"""

    id: str
    handler: NodeHandler[StateT]
    retry_limit: int = 0
    error_target: str | None = None
    requires_approval: bool = False
    approval_request: Callable[[StateT], Mapping[str, Any]] | None = None


@dataclass(frozen=True, slots=True)
class ConditionalEdge(Generic[StateT]):
    """把路由器返回的 key 映射为下一节点。"""

    router: RouteHandler[StateT]
    routes: Mapping[str, str]


@dataclass(slots=True)
class WorkflowDefinition(Generic[StateT]):
    """可执行的领域工作流定义。"""

    id: str
    domain: str
    state_type: type[StateT]
    nodes: dict[str, WorkflowNode[StateT]]
    edges: dict[str, str | ConditionalEdge[StateT]]
    services: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """启动前校验图引用，避免运行一半才发现断边。"""
        if START not in self.edges:
            raise ToolError("Workflow 缺少 START 边", detail=self.id)
        for source, edge in self.edges.items():
            if source not in self.nodes and source != START:
                raise ToolError("Workflow 边引用未知源节点", detail=source)
            targets = edge.routes.values() if isinstance(edge, ConditionalEdge) else (edge,)
            for target in targets:
                if target != END and target not in self.nodes:
                    raise ToolError("Workflow 边引用未知目标节点", detail=target)
        for node in self.nodes.values():
            if node.retry_limit < 0:
                raise ToolError("Node retry_limit 不能小于零", detail=node.id)
            if (
                node.error_target
                and node.error_target != END
                and node.error_target not in self.nodes
            ):
                raise ToolError("Node error_target 不存在", detail=node.id)


class GraphRuntime:
    """同步可恢复 Graph Runtime；可由 Worker 并发运行多个 Run。"""

    def __init__(self, store: RuntimeStore) -> None:
        self.store = store
        self._workflows: dict[str, WorkflowDefinition[Any]] = {}

    def register(self, workflow: WorkflowDefinition[Any]) -> None:
        """注册 workflow，ID 冲突时拒绝静默覆盖。"""
        workflow.validate()
        if workflow.id in self._workflows:
            raise ToolError("Workflow 已注册", detail=workflow.id)
        self._workflows[workflow.id] = workflow

    def workflow(self, workflow_id: str) -> WorkflowDefinition[Any]:
        """读取已注册 Workflow。"""
        try:
            return self._workflows[workflow_id]
        except KeyError:
            raise ToolError("Workflow 未注册", detail=workflow_id) from None

    def start(self, workflow_id: str, initial_state: RunState | Mapping[str, Any]) -> RunRecord:
        """创建 Run 并执行到完成、失败或等待审批。"""
        workflow = self.workflow(workflow_id)
        try:
            state = workflow.state_type.model_validate(initial_state)
        except ValidationError as error:
            raise ToolError("Workflow 初始 State 不合法", detail=workflow_id) from error
        first = self._next(workflow, START, state)
        run = self.store.create_run(
            workflow.domain, workflow.id, state.model_dump(mode="json"), first
        )
        return self._execute(run, workflow, state)

    def resume(self, run_id: str) -> RunRecord:
        """从数据库恢复指针继续，不重放已完成 Node。"""
        run = self.store.get_run(run_id)
        workflow = self.workflow(run.workflow)
        if run.status in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            return run
        checkpoint = self.store.latest_checkpoint(run_id)
        state_data = checkpoint.state if checkpoint is not None else run.state
        current_node = checkpoint.next_node if checkpoint is not None else run.current_node
        try:
            state = workflow.state_type.model_validate(state_data)
        except ValidationError as error:
            raise ToolError("Checkpoint State 无法恢复", detail=run_id) from error
        if current_node != run.current_node or state_data != run.state:
            run = self.store.update_run(
                run_id, status=run.status, state=state_data, current_node=current_node
            )
        return self._execute(run, workflow, state)

    def cancel(self, run_id: str) -> RunRecord:
        """取消未结束 Run；已结束 Run 保持原状态。"""
        run = self.store.get_run(run_id)
        if run.status in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            return run
        return self.store.update_run(
            run.id,
            status=ExecutionStatus.CANCELLED,
            state=run.state,
            current_node=run.current_node,
            error="cancelled_by_user",
        )

    def _execute(
        self,
        run: RunRecord,
        workflow: WorkflowDefinition[StateT],
        state: StateT,
    ) -> RunRecord:
        current = run.current_node
        if current == END:
            return self.store.update_run(
                run.id,
                status=ExecutionStatus.COMPLETED,
                state=state.model_dump(mode="json"),
                current_node=END,
            )
        while current != END:
            node = workflow.nodes[current]
            approval_decision: ApprovalDecision | None = None
            approval_response: Mapping[str, Any] = {}
            if node.requires_approval:
                request = dict(node.approval_request(state)) if node.approval_request else {}
                approval = self.store.create_approval(run.id, node.id, request)
                if approval.decision is ApprovalDecision.PENDING:
                    waiting = NodeExecutionRecord(
                        run_id=run.id,
                        node_id=node.id,
                        status=ExecutionStatus.WAITING,
                    )
                    self.store.save_node(waiting)
                    snapshot = state.model_dump(mode="json")
                    self.store.save_checkpoint(
                        run.id, node.id, node.id, ExecutionStatus.WAITING, snapshot
                    )
                    return self.store.update_run(
                        run.id,
                        status=ExecutionStatus.WAITING,
                        state=snapshot,
                        current_node=node.id,
                    )
                approval_decision = approval.decision
                approval_response = approval.response

            started = utc_now()
            self.store.save_node(NodeExecutionRecord(
                run_id=run.id,
                node_id=node.id,
                status=ExecutionStatus.RUNNING,
                started_at=started,
            ))
            self.store.update_run(
                run.id,
                status=ExecutionStatus.RUNNING,
                state=state.model_dump(mode="json"),
                current_node=node.id,
            )
            retry_count = 0
            while True:
                context = RuntimeContext(
                    run_id=run.id,
                    node_id=node.id,
                    store=self.store,
                    services=workflow.services,
                    approval_decision=approval_decision,
                    approval_response=approval_response,
                )
                try:
                    update = dict(node.handler(state, context) or {})
                    state = workflow.state_type.model_validate(
                        state.model_copy(update=update).model_dump()
                    )
                    break
                except Exception as error:
                    if retry_count < node.retry_limit:
                        retry_count += 1
                        continue
                    message = f"{type(error).__name__}: {error}"
                    self.store.save_node(NodeExecutionRecord(
                        run_id=run.id,
                        node_id=node.id,
                        status=ExecutionStatus.FAILED,
                        started_at=started,
                        completed_at=utc_now(),
                        retry_count=retry_count,
                        error=message,
                    ))
                    if node.error_target is None:
                        return self.store.update_run(
                            run.id,
                            status=ExecutionStatus.FAILED,
                            state=state.model_dump(mode="json"),
                            current_node=node.id,
                            error=message,
                        )
                    current = node.error_target
                    snapshot = state.model_dump(mode="json")
                    self.store.save_checkpoint(
                        run.id, node.id, current, ExecutionStatus.FAILED, snapshot
                    )
                    self.store.update_run(
                        run.id,
                        status=ExecutionStatus.RUNNING,
                        state=snapshot,
                        current_node=current,
                        error=message,
                    )
                    break
            else:  # pragma: no cover - loop only exits through break/return
                raise AssertionError("unreachable")

            # error_target 已经改变 current；失败节点不应被标记 completed。
            latest = {item.node_id: item for item in self.store.list_nodes(run.id)}[node.id]
            if latest.status is ExecutionStatus.FAILED:
                if current == END:
                    return self.store.update_run(
                        run.id,
                        status=ExecutionStatus.COMPLETED,
                        state=state.model_dump(mode="json"),
                        current_node=END,
                    )
                continue

            next_node = self._next(workflow, node.id, state)
            completed = utc_now()
            self.store.save_node(NodeExecutionRecord(
                run_id=run.id,
                node_id=node.id,
                status=ExecutionStatus.COMPLETED,
                started_at=started,
                completed_at=completed,
                retry_count=retry_count,
                outputs=update,
            ))
            snapshot = state.model_dump(mode="json")
            self.store.save_checkpoint(
                run.id, node.id, next_node, ExecutionStatus.COMPLETED, snapshot
            )
            self.store.update_run(
                run.id,
                status=(ExecutionStatus.COMPLETED if next_node == END
                        else ExecutionStatus.RUNNING),
                state=snapshot,
                current_node=next_node,
            )
            current = next_node

        return self.store.get_run(run.id)

    @staticmethod
    def _next(
        workflow: WorkflowDefinition[StateT], source: str, state: StateT
    ) -> str:
        try:
            edge = workflow.edges[source]
        except KeyError:
            raise ToolError("Workflow 节点缺少后继边", detail=source) from None
        if isinstance(edge, str):
            return edge
        route = edge.router(state)
        try:
            return edge.routes[route]
        except KeyError:
            raise ToolError("Conditional Routing 返回未知分支", detail=route) from None
