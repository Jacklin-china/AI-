"""显式付费的四类导演实测：复用应用 API 与真实共享 LLM，不生成媒体。"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from uuid import uuid4

from kantoku.config import ToolError, get_settings
from kantoku.config.logging_setup import setup_logging
from kantoku.config.settings import ROOT
from kantoku.core.llm import chat
from kantoku.domains.comic.critic import require_approved_director
from kantoku.domains.comic.models import DirectorSpecDraft
from kantoku.shells.web_studio import StudioApplication


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--max-cny", type=Decimal, default=Decimal("2"))
    args = parser.parse_args()
    if not args.allow_paid or not Decimal("0") < args.max_cny <= Decimal("2"):
        parser.error("必须显式允许付费，且额度在 (0, 2] 元内")
    settings = get_settings()
    price = settings.llm.pricing_cny_per_million_by_model.get(settings.llm.model_chat)
    if price is None:
        parser.error("当前文本模型缺少配置的保守单价，禁止未知费用实测")
    # 仅修改本进程的存储位置；模型/端点/密钥/输出上限仍读取 settings.yaml。
    folder = ROOT / "data" / "qa" / f"director-live-{uuid4().hex[:12]}"
    folder.mkdir(parents=True)
    settings.storage.sqlite_path = folder / "runtime.db"
    setup_logging("INFO")
    app = StudioApplication()
    spend = Decimal("0")
    calls: list[dict] = []

    def model(messages: list[dict[str, str]]) -> str:
        nonlocal spend
        # UTF-8 字节数 + 消息协议余量作为输入 token 保守上界；不假定缓存命中。
        upper_input = len(json.dumps(messages, ensure_ascii=False).encode()) + 2048
        upper_cost = (upper_input * price.input_cny
                      + settings.llm.max_tokens * price.output_cny) / 1_000_000
        if spend + upper_cost > args.max_cny:
            raise ToolError("真实导演验收剩余额度不足，未提交下一调用")
        record = {"model": settings.llm.model_chat, "upper_cny": str(upper_cost),
                  "usage_reported": False}
        calls.append(record)
        spend += upper_cost  # 结果未知保留整个上界，不自动重试或降级。

        def usage(incoming: int, outgoing: int, reported: bool) -> None:
            nonlocal spend
            if not reported:
                raise ToolError("供应商用量未知，停止后续实测并保留预估额度")
            actual = (incoming * price.input_cny + outgoing * price.output_cny) / 1_000_000
            spend += actual - upper_cost
            record.update(input_tokens=incoming, output_tokens=outgoing,
                          estimated_cny=str(actual), usage_reported=True)

        response = chat(messages, response_format={"type": "json_object"},
                        single_attempt=True, on_usage=usage)
        return response.content or ""

    app._comic_director_model = model
    report: dict = {"model": settings.llm.model_chat, "limit_cny": str(args.max_cny),
                    "pricing": price.model_dump(mode="json"), "cases": [], "calls": calls,
                    "billing_note": "按配置高峰未缓存单价估算；供应商账单为最终事实"}
    suite = [json.loads(line) for line in (ROOT / "evals" / "director.jsonl").read_text(
        encoding="utf-8",
    ).splitlines() if line.strip()]
    strategies: set[str] = set()
    try:
        for case in suite:
            project = app.create_comic_project({"title": case["input"], "brief": {
                "original_request": case["input"], "hard_constraints": case["constraints"],
            }})
            project_id = project["project"]["project_id"]
            conversation = app.create_conversation({"interaction_mode": "guided",
                                                    "domain": "comic"})
            item = {"id": case["id"], "request": case["input"], "project_id": project_id,
                    "conversation_id": conversation["id"]}
            report["cases"].append(item)
            result = app.create_comic_director(project_id, {
                "expected_project_version": 1, "creation_mode": case["mode"],
                "task": case["input"], "conversation_id": conversation["id"],
            })
            run_record = app.runtime_store.get_run(result["run_id"])
            item.update(run_id=run_record.id, trace_id=run_record.state["trace_id"],
                        status=result["status"], debug=run_record.state["director_debug"],
                        critic=run_record.state.get("critic_result"))
            if result["status"] != "completed":
                raise ToolError("真实导演未通过审核，保留候选，不继续后续流程")
            raw = result["director_spec"]
            spec = DirectorSpecDraft.model_validate({key: raw[key] for key
                                                   in DirectorSpecDraft.model_fields if key in raw})
            require_approved_director(spec)
            item["director_spec"] = spec.model_dump(mode="json")
            text = json.dumps(spec.model_dump(), ensure_ascii=False)
            if any(term in text for term in case["forbidden"]):
                raise ToolError("真实导演出现其他案例主体污染")
            strategy = spec.director_plan.visual_strategy
            if strategy in strategies:
                raise ToolError("不同创意返回相同视觉策略，需人工分析模板化")
            strategies.add(strategy)
            item["passed"] = True
    except Exception as error:
        report["error_type"] = type(error).__name__
        report["error"] = "实测未全部通过，请按 Run/Trace 检查；未自动重试"
        if report["cases"]:
            item = report["cases"][-1]
            failed = next((record for record in app.runtime_store.list_runs(domain="comic")
                           if record.state.get("project_id") == item["project_id"]), None)
            if failed is not None:
                item.update(run_id=failed.id, trace_id=failed.state.get("trace_id"),
                            status=failed.status.value, error_id=failed.state.get("error_id"),
                            debug=failed.state.get("director_debug"),
                            critic=failed.state.get("critic_result"))
        # Provider 原始异常不进入报告；安全 error_id/traceback 已由 Coordinator 记录。
    finally:
        report["estimated_total_cny"] = str(spend)
        report["passed"] = len(report["cases"]) == 4 and all(
            item.get("passed") for item in report["cases"]
        )
        output = folder / "report.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "estimated_cny": str(spend),
                          "cases": [{key: item.get(key) for key in
                                     ("id", "status", "passed", "run_id", "trace_id")}
                                    for item in report["cases"]],
                          "report": str(output)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(run())
